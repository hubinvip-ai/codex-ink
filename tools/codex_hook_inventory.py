"""Read-only, authoritative hook discovery. Never writes Codex trust/configuration.

Callers must snapshot the returned source paths and compare fresh inventories
around publication; an inventory alone is NOT an atomic filesystem snapshot.
Only needed metadata is retained, not arbitrary effective configuration values.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.codex_app_server import CodexAppServerClient


class InventoryUnavailable(ValueError):
    pass


EVENT_NAMES=frozenset({'preToolUse','permissionRequest','postToolUse','preCompact','postCompact','sessionStart',
                      'sessionEnd','userPromptSubmit','subagentStart','subagentStop','stop','interrupt'})


def absolute(value):
    if not isinstance(value,str) or not value or len(value)>4096 or any(c in value for c in '\x00\r\n'):
        raise InventoryUnavailable('invalid_inventory_path')
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise InventoryUnavailable('invalid_inventory_path')
    # Logical cwd aliases may select different config/trust layers. Let Codex
    # resolve them; canonicalization is for file fingerprints, not query scope.
    return str(path)


def canonical_file(value):
    return str(Path(absolute(value)).resolve())


def metadata(raw):
    if not isinstance(raw,dict):raise InventoryUnavailable('invalid_hook_metadata')
    fields=('handlerType','eventName','source','sourcePath','key','currentHash','trustStatus')
    if any(not isinstance(raw.get(k),str) or not raw[k] or len(raw[k])>65536 for k in fields):
        raise InventoryUnavailable('invalid_hook_metadata')
    if raw['source'] not in ('user','project','plugin','system'):
        raise InventoryUnavailable('unsupported_hook_source')
    if raw['trustStatus'] not in ('trusted','untrusted','modified','managed'):
        raise InventoryUnavailable('invalid_hook_metadata')
    if raw['eventName'] not in EVENT_NAMES or type(raw.get('displayOrder')) is not int:
        raise InventoryUnavailable('invalid_hook_metadata')
    if any(type(raw.get(k)) is not bool for k in ('enabled','isManaged')):
        raise InventoryUnavailable('invalid_hook_metadata')
    if type(raw.get('timeoutSec')) is not int or not 0<=raw['timeoutSec']<=86400:
        raise InventoryUnavailable('invalid_hook_metadata')
    if type(raw.get('async',False)) is not bool:
        raise InventoryUnavailable('invalid_hook_metadata')
    for key in ('matcher','pluginId'):
        if raw.get(key) is not None and not isinstance(raw[key],str):
            raise InventoryUnavailable('invalid_hook_metadata')
    result={k:raw[k] for k in fields}
    result['sourcePath']=canonical_file(raw['sourcePath'])
    result.update(enabled=raw['enabled'],isManaged=raw['isManaged'],timeoutSec=raw['timeoutSec'],matcher=raw.get('matcher'),pluginId=raw.get('pluginId'),displayOrder=raw['displayOrder'])
    result['async']=raw.get('async',False)
    kind=raw['handlerType']
    if kind=='command':
        if not isinstance(raw.get('command'),str) or not raw['command'] or len(raw['command'])>65536:
            raise InventoryUnavailable('invalid_hook_metadata')
        result['command']=raw['command']
    elif kind=='mcpTool':
        for key in ('server','tool'):
            if not isinstance(raw.get(key),str) or not raw[key]:raise InventoryUnavailable('invalid_hook_metadata')
            result[key]=raw[key]
    elif kind not in ('prompt','agent'):
        raise InventoryUnavailable('invalid_hook_metadata')
    return result


def discover_inventory(binary:Path,cwd:Path,*,client_factory=CodexAppServerClient):
    """Fresh client per call; no plugin/list, shell execution or config writes."""
    cwd=absolute(str(cwd))
    try:
        with client_factory(binary=Path(binary)) as client:
            config=client._request('config/read',{'cwd':cwd,'includeLayers':True})
            if not isinstance(config.get('config'),dict) or not isinstance(config.get('layers'),list):
                raise InventoryUnavailable('invalid_config_inventory')
            projects=config['config'].get('projects',{})
            if not isinstance(projects,dict):raise InventoryUnavailable('invalid_project_inventory')
            cwds=sorted({cwd,*(absolute(p) for p in projects)})
            if len(cwds)>256:raise InventoryUnavailable('inventory_limit')
            paths=set()
            # Include missing potential files too, so creating a new hook/config
            # between inventory and publish is visible to the caller's snapshot.
            for location in cwds:
                leaf=Path(location);resolved=leaf.resolve()
                for parent in {leaf,*leaf.parents,resolved,*resolved.parents}:
                    for name in ('hooks.json','config.toml'):
                        paths.add(str((parent/'.codex'/name).resolve()))
            layers=[]
            for layer in config['layers']:
                if not isinstance(layer,dict) or not isinstance(layer.get('name'),dict) or not isinstance(layer.get('version'),str):
                    raise InventoryUnavailable('invalid_config_inventory')
                name=layer['name'];kind=name.get('type')
                if kind in ('user','system','packagedDefaults','legacyManagedConfigTomlFromFile'):
                    # Hooks live beside the logical config layer, even when the
                    # config itself is a symlink to a file in another directory.
                    paths.add(canonical_file(str(Path(absolute(name.get('file'))).parent/'hooks.json')))
                    paths.add(canonical_file(name.get('file')))
                elif kind=='project':
                    directory=Path(canonical_file(name.get('dotCodexFolder')))
                    paths.update(str(directory/n) for n in ('config.toml','hooks.json'))
                else:
                    raise InventoryUnavailable('unsupported_config_layer')
                layers.append({'name':name,'version':layer['version'],'disabled':bool(layer.get('disabledReason'))})
            response=client._request('hooks/list',{'cwds':cwds})
            entries=response.get('data')
            if not isinstance(entries,list):raise InventoryUnavailable('invalid_hook_inventory')
            found=set();unique={};by_cwd={}
            for entry in entries:
                if not isinstance(entry,dict):raise InventoryUnavailable('invalid_hook_inventory')
                location=absolute(entry.get('cwd'))
                if location not in cwds or location in found:raise InventoryUnavailable('incomplete_hook_inventory')
                found.add(location)
                if entry.get('errors')!=[] or entry.get('warnings')!=[] or not isinstance(entry.get('hooks'),list):
                    raise InventoryUnavailable('incomplete_hook_inventory')
                by_cwd[location]=[]
                for raw in entry['hooks']:
                    hook=metadata(raw)
                    paths.add(hook['sourcePath'])
                    by_cwd[location].append(hook)
                    unique[json.dumps(hook,sort_keys=True)]=dict(hook)
                    if len(unique)>4096:raise InventoryUnavailable('inventory_limit')
            if found!=set(cwds):raise InventoryUnavailable('incomplete_hook_inventory')
            # The union is for source enumeration, never proof that a hook is
            # active in every context. Empty per-cwd lists remain meaningful.
            result={'cwds':cwds,'paths':sorted(paths),'hooks':[unique[k] for k in sorted(unique)],'by_cwd':by_cwd}
            content=json.dumps({'inventory':result,'layers':layers},sort_keys=True,separators=(',',':')).encode()
            result['stamp']=hashlib.sha256(content).hexdigest()
            return result
    except InventoryUnavailable:
        raise
    except Exception as error:
        raise InventoryUnavailable('hook_inventory_unavailable') from error
