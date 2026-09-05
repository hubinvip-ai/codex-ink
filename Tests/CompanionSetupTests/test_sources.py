"""Inventory/file consistency fixtures; main owns the RPC adapter tests."""
import copy
import importlib
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def hook(path, *, source='user', event='stop', command='/usr/bin/true unrelated', trust='untrusted'):
    return {'handlerType': 'command', 'command': command, 'async': False, 'eventName': event,
            'source': source, 'sourcePath': str(path), 'pluginId': 'fixture@local' if source == 'plugin' else None,
            'key': source + ':' + event, 'currentHash': 'sha256:' + '1' * 64,
            'enabled': True, 'isManaged': False, 'trustStatus': trust,
            'timeoutSec': 3, 'matcher': None, 'displayOrder': 0}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module('tools.companion_sources')
        self.setupmod = importlib.import_module('tools.companion_setup')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'home'
        self.home.mkdir(mode=0o700)
        self.cwd = self.home / 'project'
        self.cwd.mkdir()
        self.config = self.home / '.codex/config.toml'
        self.config.parent.mkdir()
        self.config.write_text('[hooks.state.example]\ntrusted_hash="opaque"\n[plugins."fixture@local"]\nenabled=true\n')
        self.hookfile = self.home / '.codex/hooks.json'
        self.hookfile.write_text('{"hooks":{}}')
        self.plugin = self.home / 'plugins/authoritative-version/.codex-plugin/plugin.json'
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_text('{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"/usr/bin/true unrelated"}]}]}}')
        self.files = self.setupmod.Files(self.home, os.getuid(), self.home / 'state')
        self.value = {'cwds': [str(self.cwd)], 'paths': [str(self.config), str(self.hookfile), str(self.plugin)],
                      'hooks': [hook(self.plugin, source='plugin')], 'stamp': 'a' * 64}
        self.calls = []
        self.reader = self.mod.APISourceReader(Path('/fixture/codex'), discover=self.discover)

    def discover(self, *, binary, cwd):
        self.calls.append((binary, cwd))
        value = copy.deepcopy(self.value)
        if 'by_cwd' not in value:
            value['by_cwd'] = {str(self.cwd): copy.deepcopy(value['hooks'])}
        return value

    def collect(self):
        return self.reader.collect(home=self.home, cwd=self.cwd, files=self.files,
                                   initial_images={self.hookfile: self.files.read(self.hookfile)})

    def test_exact_plugin_paths_are_fingerprinted_and_metadata_is_read_twice(self):
        snapshot = self.collect()
        self.assertIn(self.plugin, snapshot['images'])
        self.assertIn(self.config, snapshot['images'])
        self.assertEqual(snapshot['hooks_by_cwd'][str(self.cwd)][0]['sourcePath'], str(self.plugin))
        self.assertEqual(self.calls, [(Path('/fixture/codex'), self.cwd)] * 2)

    def test_inventory_stamp_change_blocks_even_when_source_bytes_are_unchanged(self):
        def change(**kwargs):
            value = self.discover(**kwargs)
            if len(self.calls) == 2:
                value['stamp'] = 'b' * 64
            return value
        self.reader.discover = change
        with self.assertRaises(self.mod.SourceError) as error:
            self.collect()
        self.assertEqual(error.exception.code, 'config_changed')

    def test_api_failure_is_sanitized_not_a_permission_to_ignore_plugins(self):
        def fail(**kwargs):
            raise RuntimeError('PRIVATE error body')
        self.reader.discover = fail
        with self.assertRaises(self.mod.SourceError) as error:
            self.collect()
        self.assertEqual(error.exception.code, 'source_api_unavailable')
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_managed_unknown_or_partial_metadata_fails_closed(self):
        for source in ('mdm', 'cloudRequirements', 'sessionFlags', 'unknown'):
            self.value['hooks'][0]['source'] = source
            with self.assertRaises(self.mod.SourceError) as error:
                self.collect()
            self.assertEqual(error.exception.code, 'unsupported_managed_source')
        self.value['hooks'][0]['source'] = 'plugin'
        del self.value['hooks'][0]['currentHash']
        with self.assertRaises(self.mod.SourceError):
            self.collect()

    def test_missing_reported_source_and_missing_cwd_fail_closed(self):
        self.plugin.unlink()
        with self.assertRaises(self.mod.SourceError):
            self.collect()
        self.value['cwds'] = []
        with self.assertRaises(self.mod.SourceError):
            self.collect()

    def test_changed_file_after_first_inventory_does_not_replace_initial_image(self):
        def race(**kwargs):
            value = self.discover(**kwargs)
            if len(self.calls) == 1:
                self.hookfile.write_text('{"description":"USER EDIT","hooks":{}}')
            return value
        self.reader.discover = race
        with self.assertRaises(self.mod.SourceError) as error:
            self.collect()
        self.assertEqual(error.exception.code, 'config_changed')
        self.assertIn('USER EDIT', self.hookfile.read_text())

    def test_untrusted_own_definition_cannot_become_ready_from_receipt(self):
        command = 'NONCE=value /python /helper hook --installation-id fixture'
        definitions = {'Stop': {'hooks': [{'type': 'command', 'command': command, 'timeout': 3}]}}
        own = hook(self.hookfile, command=command, trust='untrusted')
        snapshot = {'hooks_by_cwd': {str(self.cwd): [own]}}
        self.assertFalse(self.mod.installed_hooks_trusted(snapshot, self.hookfile, definitions))
        own['trustStatus'] = 'trusted'
        self.assertTrue(self.mod.installed_hooks_trusted(snapshot, self.hookfile, definitions))
        own['enabled'] = False
        self.assertFalse(self.mod.installed_hooks_trusted(snapshot, self.hookfile, definitions))

    def test_identical_cross_cwd_records_dedupe_but_conflicting_trust_does_not(self):
        self.value['hooks'] = [hook(self.hookfile, command='ours', trust='trusted')]
        self.value['hooks'].append(copy.deepcopy(self.value['hooks'][0]))
        snapshot = self.collect()
        definitions = {'Stop': {'hooks': [{'type': 'command', 'command': 'ours', 'timeout': 3}]}}
        self.assertTrue(self.mod.installed_hooks_trusted(snapshot, self.hookfile, definitions))
        self.value['hooks'][1]['trustStatus'] = 'modified'
        snapshot = self.collect()
        self.assertFalse(self.mod.installed_hooks_trusted(snapshot, self.hookfile, definitions))

    def test_untrusted_unrelated_mcp_plugin_does_not_need_companion_trust(self):
        plugin = hook(self.plugin, source='plugin')
        del plugin['command']
        plugin.update(handlerType='mcpTool', server='fixture', tool='observe')
        self.value['hooks'] = [plugin]
        snapshot = self.collect()
        self.assertEqual(snapshot['hooks_by_cwd'][str(self.cwd)][0]['handlerType'], 'mcpTool')

    def test_missing_potential_file_on_shared_volume_is_fingerprinted_as_absent(self):
        shared = self.home.parent / 'shared-volume'
        shared.mkdir(mode=0o775)
        shared.chmod(0o775)
        potential = shared / '.codex/config.toml'
        self.value['paths'].append(str(potential))
        snapshot = self.collect()
        self.assertIn(potential, snapshot['images'])
        self.assertIsNone(snapshot['images'][potential])
        self.assertFalse(potential.exists())
        self.assertEqual(shared.stat().st_mode & 0o777, 0o775)

    def test_potential_file_appearing_mid_capture_is_still_blocked(self):
        shared = self.home.parent / 'shared-volume'
        shared.mkdir(mode=0o775)
        shared.chmod(0o775)
        potential = shared / '.codex/config.toml'
        self.value['paths'].append(str(potential))
        def race(**kwargs):
            result = self.discover(**kwargs)
            if len(self.calls) == 2:
                potential.parent.mkdir()
                potential.write_text('model="user-created"\n')
            return result
        self.reader.discover = race
        with self.assertRaises(self.mod.SourceError):
            self.collect()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(potential.read_text(), 'model="user-created"\n')

    def test_initialize_failure_closes_its_owned_probe_process_and_pipes(self):
        self.assertTrue(hasattr(self.mod, 'ClosingProbeClient'), 'probe initialization needs failure cleanup')
        from tools.codex_app_server import CodexAppServerClient
        processes = []
        def fail_after_spawn(client):
            client.process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            processes.append(client.process)
            raise RuntimeError('fixture initialization failure')
        client = self.mod.ClosingProbeClient(binary=Path(sys.executable))
        try:
            with patch.object(CodexAppServerClient, '__enter__', fail_after_spawn):
                with self.assertRaises(RuntimeError):
                    client.__enter__()
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdin.closed)
            self.assertTrue(processes[0].stdout.closed)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                process.stdin.close()
                process.stdout.close()

    def test_review2_union_cannot_make_empty_project_ready(self):
        other = self.home / 'empty-project'
        events = ('SessionStart', 'UserPromptSubmit', 'PermissionRequest', 'Stop', 'SessionEnd')
        own = [hook(self.hookfile, event=event[0].lower()+event[1:], command='ours', trust='trusted') for event in events]
        self.value.update(cwds=[str(self.cwd), str(other)], hooks=own,
                          by_cwd={str(self.cwd): own, str(other): []})
        snapshot = self.collect()
        self.assertEqual(snapshot['hooks_by_cwd'][str(other)], [])
        definitions = {event: {'hooks': [{'type': 'command', 'command': 'ours', 'timeout': 3}]} for event in events}
        self.assertFalse(self.mod.installed_hooks_trusted(snapshot, self.hookfile, definitions))

    def test_review2_scope_change_cannot_hide_behind_same_union_and_stamp(self):
        other = self.home / 'other-project'
        records = [hook(self.hookfile, command='ours', trust='trusted')]
        self.value.update(cwds=[str(self.cwd), str(other)], hooks=records,
                          by_cwd={str(self.cwd): records, str(other): records})
        def change(**kwargs):
            result = self.discover(**kwargs)
            if len(self.calls) == 2:
                result['by_cwd'][str(other)] = []
            return result
        self.reader.discover = change
        with self.assertRaises(self.mod.SourceError) as error:
            self.collect()
        self.assertEqual(error.exception.code, 'config_changed')

    def test_review2_missing_by_cwd_does_not_fall_back_to_union(self):
        def old_catalog(**kwargs):
            result = self.discover(**kwargs)
            del result['by_cwd']
            return result
        self.reader.discover = old_catalog
        with self.assertRaises(self.mod.SourceError) as error:
            self.collect()
        self.assertEqual(error.exception.code, 'source_api_invalid')


if __name__ == '__main__':
    unittest.main()
