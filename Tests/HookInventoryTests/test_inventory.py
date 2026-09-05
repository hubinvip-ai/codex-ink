import copy
import unittest
import tempfile
from pathlib import Path

try:
    from tools.codex_hook_inventory import discover_inventory, InventoryUnavailable
except ImportError:
    discover_inventory=None
    InventoryUnavailable=ValueError


def hook():
    return dict(handlerType='command',command='/usr/bin/true',eventName='stop',source='user',sourcePath='/Users/example/.codex/hooks.json',
                pluginId=None,key='hook-1',currentHash='hash-1',enabled=True,isManaged=False,trustStatus='trusted',timeoutSec=3,matcher=None,displayOrder=1)


class Client:
    def __init__(self):
        self.calls=[]
        self.config={'config':{'projects':{'/project/child':{'trust_level':'trusted'}},'hooks':{'state':{'opaque':{'enabled':True}}},'plugins':{'unrelated':{'enabled':True}},'private_value':'never-retain'},
            'layers':[{'name':{'type':'user','file':'/Users/example/.codex/config.toml'},'version':'v1','config':{'private_value':'never-retain'}}]}
        self.row=hook()
        self.errors=[]
        self.omit=False
        self.empty=False
        self.empty_cwds=set()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def _request(self,method,params):
        self.calls.append((method,params))
        if method=='config/read':return copy.deepcopy(self.config)
        return {'data':[{'cwd':cwd,'hooks':[] if self.empty or cwd in self.empty_cwds else [copy.deepcopy(self.row)],'errors':self.errors,'warnings':[]} for cwd in params['cwds'] if not self.omit]}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(discover_inventory,'authoritative hook inventory missing')
        self.client=Client()
    def discover(self):
        return discover_inventory(Path('/codex'),Path('/project'),client_factory=lambda **kwargs:self.client)

    def test_uses_effective_projects_and_canonical_hook_metadata(self):
        result=self.discover()
        self.assertEqual(result['cwds'],['/project','/project/child'])
        self.assertIn('/Users/example/.codex/config.toml',result['paths'])
        self.assertIn('/project/.codex/hooks.json',result['paths'])
        self.assertEqual(len(result['hooks']),1)
        self.assertEqual(result['hooks'][0]['trustStatus'],'trusted')
        self.assertNotIn('never-retain',repr(result))

    def test_plugins_are_discovered_not_blindly_rejected(self):
        self.client.row.update(source='plugin',pluginId='plugin-id',sourcePath='/plugin/.codex-plugin/plugin.json',handlerType='mcpTool',server='server',tool='tool')
        self.client.row.pop('command')
        result=self.discover()
        self.assertEqual(result['hooks'][0]['source'],'plugin')
        self.assertIn('/plugin/.codex-plugin/plugin.json',result['paths'])

    def test_missing_cwd_or_hook_errors_fail_closed(self):
        self.client.omit=True
        with self.assertRaises(InventoryUnavailable):self.discover()
        self.client.omit=False;self.client.errors=[{'path':'/bad','message':'bad'}]
        with self.assertRaises(InventoryUnavailable):self.discover()

    def test_managed_or_unknown_source_not_claimed_locally_verified(self):
        for source in ('unknown','cloudManagedConfig','mdm'):
            self.client.row['source']=source
            with self.assertRaises(InventoryUnavailable):self.discover()

    def test_changes_to_trust_or_definition_change_stamp(self):
        before=self.discover()['stamp']
        self.client.row['trustStatus']='modified'
        self.assertNotEqual(self.discover()['stamp'],before)
        self.client.row['currentHash']='hash-2'
        self.assertNotEqual(self.discover()['stamp'],before)

    def test_relative_project_and_invalid_metadata_rejected(self):
        self.client.config['config']['projects']={'relative':{}}
        with self.assertRaises(InventoryUnavailable):self.discover()
        self.client.config['config']['projects']={}
        self.client.row['enabled']=1
        with self.assertRaises(InventoryUnavailable):self.discover()

    def test_logical_cwd_aliases_are_not_collapsed_before_codex_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();target=root/'target';target.mkdir()
            alias=root/'alias';alias.symlink_to(target,target_is_directory=True)
            self.client.config['config']['projects']={str(alias):{},str(target):{}}
            result=discover_inventory(Path('/codex'),target,client_factory=lambda **kwargs:self.client)
            self.assertEqual(set(result['cwds']),{str(alias),str(target)})

    def test_empty_catalog_still_fingerprints_potential_user_hooks(self):
        self.client.empty=True
        self.assertIn('/Users/example/.codex/hooks.json',self.discover()['paths'])

    def test_hooks_path_is_derived_before_resolving_config_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();user=root/'user';user.mkdir()
            other=root/'elsewhere';other.mkdir();(other/'config.toml').write_text('')
            (user/'config.toml').symlink_to(other/'config.toml')
            self.client.config['layers'][0]['name']['file']=str(user/'config.toml')
            self.client.empty=True
            result=self.discover()
            self.assertIn(str(user/'hooks.json'),result['paths'])
            self.assertIn(str(other/'config.toml'),result['paths'])

    def test_unknown_event_or_invalid_display_order_rejected(self):
        for field,value in (('eventName','notAnOfficialEvent'),('displayOrder','1'),('displayOrder',True)):
            self.client.row=hook();self.client.row[field]=value
            with self.assertRaises(InventoryUnavailable):self.discover()
        self.client.row=hook();self.client.row.pop('displayOrder')
        with self.assertRaises(InventoryUnavailable):self.discover()

    def test_hook_order_is_part_of_fingerprint(self):
        before=self.discover()['stamp'];self.client.row['displayOrder']=2
        self.assertNotEqual(self.discover()['stamp'],before)

    def test_per_cwd_empty_coverage_is_preserved_and_changes_stamp(self):
        before=self.discover()['stamp']
        self.client.empty_cwds={'/project/child'}
        result=self.discover()
        self.assertEqual(result['by_cwd']['/project/child'],[])
        self.assertEqual(len(result['by_cwd']['/project']),1)
        self.assertNotEqual(result['stamp'],before)


if __name__=='__main__':unittest.main()
