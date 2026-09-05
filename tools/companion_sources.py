"""File-fingerprint/trust guard around the main-owned stable hook inventory.

This module does not implement RPC discovery or select plugin cache versions.
The provider owns config/read, hooks/list, validation, and probe lifetime.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from tools.codex_app_server import CodexAppServerClient


class SourceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ClosingProbeClient(CodexAppServerClient):
    """Lifecycle-only wrapper: a failed __enter__ does not invoke __exit__."""
    def __enter__(self):
        try:
            return super().__enter__()
        except BaseException:
            self.close()
            raise

    def close(self):
        process = self.process
        try:
            super().close()
        finally:
            if process:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()


def absolute(value):
    if not isinstance(value, (str, Path)) or any(c in str(value) for c in '\x00\n\r'):
        raise SourceError('source_api_invalid')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise SourceError('source_api_invalid')
    return path


def production_inventory(binary, cwd):
    # Lazy import: unavailable main-owned adapter fails closed, never guesses.
    from tools.codex_hook_inventory import discover_inventory
    return discover_inventory(binary=binary, cwd=cwd, client_factory=ClosingProbeClient)


def inventory_projection(value, cwd):
    if not isinstance(value, dict) or not {'cwds', 'paths', 'hooks', 'stamp', 'by_cwd'}.issubset(value):
        raise SourceError('source_api_invalid')
    if any(not isinstance(value[key], list) for key in ('cwds', 'paths', 'hooks')):
        raise SourceError('source_api_invalid')
    if not isinstance(value['stamp'], str) or not re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', value['stamp']):
        raise SourceError('source_api_invalid')
    cwds = sorted({str(absolute(path)) for path in value['cwds']})
    paths = sorted({str(absolute(path)) for path in value['paths']})
    if str(cwd) not in cwds or len(cwds) != len(value['cwds']) or len(cwds) > 256 or len(paths) > 2048:
        raise SourceError('source_api_invalid')
    if not isinstance(value['by_cwd'], dict) or set(value['by_cwd']) != set(cwds):
        raise SourceError('source_api_invalid')
    union = normalize_hooks(value['hooks'], paths)
    by_cwd = {key: normalize_hooks(value['by_cwd'][key], paths) for key in cwds}
    union_keys = {json.dumps(hook, sort_keys=True) for hook in union}
    scoped_keys = {json.dumps(hook, sort_keys=True) for records in by_cwd.values() for hook in records}
    if union_keys != scoped_keys:
        raise SourceError('source_api_invalid')
    return {'cwds': cwds, 'paths': paths, 'hooks': union, 'by_cwd': by_cwd, 'stamp': value['stamp']}


def normalize_hooks(hooks, paths):
    if not isinstance(hooks, list):
        raise SourceError('source_api_invalid')
    records = {}
    for hook in hooks:
        required = {'handlerType', 'source', 'sourcePath', 'eventName', 'currentHash', 'key',
                    'enabled', 'isManaged', 'trustStatus', 'timeoutSec'}
        if not isinstance(hook, dict) or not required.issubset(hook):
            raise SourceError('source_api_invalid')
        if hook['source'] not in ('user', 'project', 'plugin') or hook['isManaged'] or hook['trustStatus'] == 'managed':
            raise SourceError('unsupported_managed_source')
        source = absolute(hook['sourcePath'])
        if str(source) not in paths or source.name in ('auth.json', 'credentials.json'):
            raise SourceError('source_api_invalid')
        if (type(hook['enabled']) is not bool or type(hook['isManaged']) is not bool or
                hook['trustStatus'] not in ('trusted', 'untrusted', 'modified') or
                any(not isinstance(hook[key], str) or not hook[key] for key in ('key', 'currentHash', 'eventName')) or
                hook['handlerType'] not in ('command', 'mcpTool', 'prompt', 'agent') or
                type(hook['timeoutSec']) is not int or hook['timeoutSec'] < 0):
            raise SourceError('source_api_invalid')
        normalized = {key: hook[key] for key in required}
        normalized.update(sourcePath=str(source), matcher=hook.get('matcher'), pluginId=hook.get('pluginId'))
        if hook['handlerType'] == 'command':
            if not isinstance(hook.get('command'), str) or not hook['command'].strip() or type(hook.get('async', False)) is not bool:
                raise SourceError('source_api_invalid')
            normalized.update(command=hook['command'], **{'async': hook.get('async', False)})
        elif hook['handlerType'] == 'mcpTool':
            if any(not isinstance(hook.get(key), str) or not hook[key] for key in ('server', 'tool')):
                raise SourceError('source_api_invalid')
            normalized.update(server=hook['server'], tool=hook['tool'])
        # Ignore display order only. Conflicting trust/hash/enablement records
        # remain distinct; they must not satisfy the readiness predicate.
        records[json.dumps(normalized, sort_keys=True)] = normalized
    return [records[key] for key in sorted(records)]


class APISourceReader:
    def __init__(self, binary, *, discover=production_inventory):
        self.binary, self.discover = binary, discover

    def collect(self, *, home, cwd, files, initial_images):
        images = dict(initial_images)
        selector = home / '.codex/config.toml'
        if selector not in images:
            images[selector] = files.read(selector)
        try:
            first = inventory_projection(self.discover(binary=self.binary, cwd=cwd), cwd)
            for value in first['paths']:
                path = Path(value)
                if path not in images:
                    images[path] = files.read_source(path)
            for hook in first['hooks']:
                if images[Path(hook['sourcePath'])] is None:
                    raise SourceError('source_api_invalid')
            second = inventory_projection(self.discover(binary=self.binary, cwd=cwd), cwd)
            if first != second:
                raise SourceError('config_changed')
            for path, image in images.items():
                read = files.read if path in initial_images or path == selector else files.read_source
                if read(path) != image:
                    raise SourceError('config_changed')
            return {'hooks_by_cwd': first['by_cwd'],
                    'images': images, 'signature': first['stamp'], 'source_paths': [Path(p) for p in first['paths']]}
        except SourceError:
            raise
        except Exception as error:
            code = getattr(error, 'code', None)
            if type(error).__name__ == 'InventoryUnavailable':
                # Provider messages are diagnostic identifiers, not raw RPC
                # output. Recognize only explicit codes; never echo arbitrary text.
                code = {'incomplete_hook_inventory': 'source_api_diagnostics',
                        'unsupported_hook_source': 'unsupported_managed_source',
                        'unsupported_config_layer': 'unsupported_managed_source',
                        'invalid_inventory_path': 'source_api_invalid',
                        'invalid_hook_metadata': 'source_api_invalid',
                        'invalid_config_inventory': 'source_api_invalid',
                        'invalid_project_inventory': 'source_api_invalid',
                        'invalid_hook_inventory': 'source_api_invalid',
                        'inventory_limit': 'source_api_invalid'}.get(str(error), 'source_api_unavailable')
            if code not in ('unsafe_path', 'file_invalid', 'config_changed', 'config_invalid', 'unknown_hook_source',
                            'source_api_invalid', 'source_api_diagnostics', 'unsupported_managed_source'):
                code = 'source_api_unavailable'
            raise SourceError(code) from None


def installed_hooks_trusted(snapshot, path, definitions):
    """Exact enabled own definitions must be trusted; receipt alone is insufficient."""
    if not snapshot or not snapshot.get('hooks_by_cwd'):
        return False
    for hooks in snapshot['hooks_by_cwd'].values():
        for event, group in definitions.items():
            handler = group['hooks'][0]
            matches = [hook for hook in hooks if hook['source'] == 'user' and hook['sourcePath'] == str(path)
                       and hook['eventName'] == event[0].lower() + event[1:]
                       and hook['handlerType'] == 'command' and hook.get('command') == handler['command']
                       and hook.get('matcher') == group.get('matcher') and hook['timeoutSec'] == handler.get('timeout', 600)
                       and hook.get('async', False) == handler.get('async', False)]
            if len(matches) != 1 or matches[0]['trustStatus'] != 'trusted' or not matches[0]['enabled'] or matches[0]['isManaged']:
                return False
    return True
