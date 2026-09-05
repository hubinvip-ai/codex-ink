"""Migration tests use real files; only OS process/launchd observations are injected."""
import copy
import importlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / 'tools/companion_setup.py'
LABEL = 'com.ben.codex-eink-sync'
EVENTS = ('SessionStart', 'UserPromptSubmit', 'PermissionRequest', 'Stop', 'SessionEnd')
THIRD = {'type': 'command', 'command': '/usr/bin/true', 'timeout': 9}


class FileSourceClient:
    """Local-file API fixture for transaction tests; adapter protocol has separate fixtures."""
    def __init__(self, system, home):
        self.system, self.home = system, home

    def __enter__(self):
        self.system.active_probes += 1
        return self

    def close(self):
        self.system.active_probes -= 1

    def _request(self, method, params):
        from tools import companion_setup as setup
        from test_sources import hook
        if self.system.api_unavailable:
            raise RuntimeError('PRIVATE API unavailable')
        if method == 'config/read':
            target = Path(params['cwd'])
            files = [self.home / '.codex/config.toml']
            files.extend(p / '.codex/config.toml' for p in reversed([target] + list(target.parents)) if p != self.home)
            projects, layers = {}, []
            for path in dict.fromkeys(files):
                if not path.is_file():
                    continue
                raw = path.read_bytes()
                projects.update({str(p): {'trust_level': 'trusted'} for p in setup.toml_sources(raw)})
                name = {'type': 'user', 'file': str(path)} if path == files[0] else {'type': 'project', 'dotCodexFolder': str(path.parent)}
                layers.append({'name': name, 'version': setup.digest(raw), 'config': {}, 'disabledReason': None})
            return {'config': {'projects': projects}, 'layers': layers, 'origins': {}}
        assert method == 'hooks/list'
        result = []
        for cwd in params['cwds']:
            root = Path(cwd)
            paths = [self.home / '.codex/hooks.json'] + [p / '.codex/hooks.json' for p in [root] + list(root.parents) if p != self.home]
            values = []
            for path in dict.fromkeys(paths):
                if not path.is_file():
                    continue
                config = setup.validate_hooks(setup.json_object(path.read_bytes()))
                for event, index, offset, group, handler in setup.handlers(config):
                    value = hook(path, source='user' if path == paths[0] else 'project', event=event[0].lower() + event[1:],
                                 command=handler.get('command', ''), trust=self.system.hook_trust)
                    value.update(key=str(path) + ':' + event + ':' + str(index) + ':' + str(offset),
                                 currentHash=setup.digest(setup.encode({'event': event, 'group': group})),
                                 matcher=group.get('matcher'), timeoutSec=handler.get('timeout', 600))
                    value['async'] = handler.get('async', False)
                    values.append(value)
            result.append({'cwd': cwd, 'hooks': values, 'errors': [], 'warnings': []})
        return {'data': result}

    def discover(self, *, binary, cwd):
        from tools import companion_setup as setup
        self.__enter__()
        try:
            cwds, configs, paths = {str(cwd)}, {}, set()
            while cwds - set(configs):
                target = sorted(cwds - set(configs))[0]
                config = self._request('config/read', {'cwd': target, 'includeLayers': True})
                configs[target] = config
                cwds.update(config['config']['projects'])
                for layer in config['layers']:
                    name = layer['name']
                    path = Path(name['file']) if 'file' in name else Path(name['dotCodexFolder']) / 'config.toml'
                    paths.update((str(path), str(path.parent / 'hooks.json')))
            reply = self._request('hooks/list', {'cwds': sorted(cwds)})
            hooks = [hook for entry in reply['data'] for hook in entry['hooks']]
            paths.update(hook['sourcePath'] for hook in hooks)
            return {'cwds': sorted(cwds), 'paths': sorted(paths), 'hooks': hooks,
                    'by_cwd': {entry['cwd']: entry['hooks'] for entry in reply['data']},
                    'stamp': setup.digest(setup.encode({'configs': configs, 'entries': reply['data']}))}
        finally:
            self.close()


class FakeSystem:
    def __init__(self):
        self.uid = os.getuid()
        self.clock = 2000.0
        self.running = []
        self.loaded = None
        self.bootouts = []
        self.before_bootout = None
        self.process_error = False
        self.environment = {}
        self.temporary = []
        self.active_probes = 0
        self.hook_trust = 'trusted'
        self.api_unavailable = False

    def now(self):
        return self.clock

    def processes(self):
        assert self.active_probes == 0, 'source probe must close before checking Codex processes'
        if self.process_error:
            raise OSError('PRIVATE process payload')
        return self.running

    def job(self, label):
        assert label == LABEL
        return self.loaded

    def bootout(self, label):
        assert label == LABEL
        self.bootouts.append(label)
        if self.before_bootout:
            self.before_bootout()
        self.loaded = None

    def system_roots(self):
        return []

    def temporary_roots(self):
        return self.temporary

    def source_snapshot(self, *, home, state_dir, cwd, files, initial_images):
        from tools.companion_sources import APISourceReader
        return APISourceReader(Path(sys.executable), discover=FileSourceClient(self, home).discover).collect(
            home=home, cwd=cwd, files=files, initial_images=initial_images)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'safe setup helper has not been implemented')
        self.mod = importlib.import_module('tools.companion_setup')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / 'home'
        self.home.mkdir(mode=0o700)
        self.state = self.home / 'Library/Application Support/CodexEInk/companion'
        self.legacy = self.home / 'Library/Application Support/CodexEInk/runtime'
        self.config = self.home / '.codex/hooks.json'
        self.plist = self.home / ('Library/LaunchAgents/' + LABEL + '.plist')
        self.runtime = self.home / 'Runtime With Spaces'
        shutil.copytree(ROOT / 'tools', self.runtime / 'tools', ignore=shutil.ignore_patterns('__pycache__'))
        # This suite validates installer paths; hook execution uses sys.executable
        # separately. Avoid depending on a host-wide Python installation's modes.
        self.python = self.home / 'bin/python3'
        self.python.parent.mkdir(mode=0o700)
        self.python.write_text('#!/bin/sh\nexit 99\n')
        self.python.chmod(0o700)
        self.system = FakeSystem()
        self.setup = self.mod.Setup(home=self.home, state_dir=self.state,
                                    cwd=self.home, system=self.system)

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())

    def write_json(self, path, value):
        self.write(path, json.dumps(value))

    def read(self, path):
        return json.loads(path.read_text())

    def old_config(self):
        command = shlex.join(['/usr/bin/python3', str(self.legacy / 'tools/codex_status_hook.py'), '--sync'])
        return {'description': 'keep this', 'hooks': {'Stop': [
            {'matcher': '*', 'hooks': [copy.deepcopy(THIRD), {'type': 'command', 'command': command, 'async': True}]}]}}

    def legacy_agent(self):
        value = {'Label': LABEL, 'ProgramArguments': ['/usr/bin/python3', str(self.legacy / 'tools/codex_eink_sync.py'), '--no-push'],
                 'WorkingDirectory': str(self.legacy), 'KeepAlive': True}
        self.write(self.plist, plistlib.dumps(value))
        self.system.loaded = copy.deepcopy(value)

    def install(self):
        return self.setup.install(python=self.python, runtime_root=self.runtime)

    def assert_blocked(self, result, code):
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['stage'], 'blocked')
        self.assertIn(code, result['blockers'])

    def test_inspect_fresh_is_read_only_and_exact_contract(self):
        result = self.setup.inspect()
        self.assertEqual(result, {'ok': True, 'stage': 'needs_install', 'blockers': [],
                                  'installation_id': None, 'legacy_present': False})
        self.assertFalse(self.state.exists())
        self.assertFalse(self.config.exists())

    def test_first_install_without_hooks_then_restore(self):
        result = self.install()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['stage'], 'awaiting_trust')
        hooks = self.read(self.config)['hooks']
        self.assertEqual(set(hooks), set(EVENTS))
        command = shlex.split(hooks['Stop'][0]['hooks'][0]['command'])
        self.assertIn('hook', command)
        self.assertIn('--installation-id', command)
        self.assertNotIn('--sync', command)
        self.assertEqual(self.setup.restore()['stage'], 'needs_install')
        self.assertFalse(self.config.exists())

    def test_preserves_unrelated_install_idempotent_and_restore(self):
        original = self.old_config()
        self.write_json(self.config, original)
        first = self.install()
        self.assertTrue(first['ok'], first)
        installed = self.config.read_bytes()
        self.assertEqual(self.read(self.config)['hooks']['Stop'][0]['hooks'][0], THIRD)
        self.assertEqual(self.install()['installation_id'], first['installation_id'])
        self.assertEqual(self.config.read_bytes(), installed)
        self.assertTrue(self.setup.restore()['ok'])
        self.assertEqual(self.read(self.config), original)
        self.assertTrue(self.setup.restore()['ok'])

    def test_malformed_partial_duplicate_or_wrong_shape_config_fail_closed(self):
        for raw in ('{', '[]', '{"hooks":null}', '{"hooks":{"Stop":{}}}',
                    '{"hooks":{"Stop":[{"hooks":[null]}]}}', '{"hooks":{},"hooks":{}}'):
            with self.subTest(raw=raw):
                self.write(self.config, raw)
                self.assert_blocked(self.install(), 'config_invalid')
                self.assertEqual(self.config.read_text(), raw)
                self.assertFalse(self.state.exists())

    def test_lookalike_owned_command_is_not_removed_and_blocks(self):
        for command in (shlex.join(['/usr/bin/python3', str(self.legacy / 'tools/codex_status_hook.py')]) + ' ; touch /tmp/unsafe',
                        '/usr/bin/python3 /elsewhere/codex_status_hook.py --sync'):
            original = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': command}]}]}}
            self.write_json(self.config, original)
            self.assert_blocked(self.install(), 'unknown_hook_source')
            self.assertEqual(self.read(self.config), original)

    def test_real_legacy_paths_recognized_but_only_explicit_install_boots_out(self):
        self.write_json(self.config, self.old_config())
        self.legacy_agent()
        self.assertEqual(self.setup.inspect()['stage'], 'legacy_active')
        self.assertEqual(self.system.bootouts, [])
        result = self.install()
        self.assertEqual(result['stage'], 'awaiting_trust', result)
        self.assertEqual(self.system.bootouts, [LABEL])
        self.assertFalse(self.plist.exists())
        self.assertTrue(self.setup.restore()['ok'])
        self.assertTrue(self.plist.exists())
        self.assertIsNone(self.system.loaded, 'restore must not bootstrap a sender')

    def test_app_and_sender_guard_no_killing_or_writes(self):
        cases = [(['/Applications/Codex.app/Contents/MacOS/Codex'], 'codex_running'),
                 (['/opt/homebrew/bin/codex', 'app-server'], 'codex_running'),
                 (['/usr/bin/python3', str(self.legacy / 'tools/codex_status_hook.py'), '--sync'], 'sender_running'),
                 ([str(self.legacy / 'bin/eink-push'), '--input', '/tmp/frame.png'], 'sender_running'),
                 (['/Applications/EInk Companion.app/Contents/MacOS/eink-companion'], 'companion_running')]
        for argv, code in cases:
            with self.subTest(argv=argv):
                self.system.running = [{'pid': 44, 'uid': os.getuid(), 'argv': argv}]
                self.assert_blocked(self.install(), code)
                self.assertFalse(self.config.exists())
        self.system.running = [{'pid': 45, 'uid': os.getuid(), 'argv': ['/usr/bin/printf', str(self.legacy / 'bin/eink-push')]}]
        self.assertTrue(self.install()['ok'], 'a path in another program argument is not a sender')

    def test_failed_process_listing_does_not_imply_stopped(self):
        self.system.process_error = True
        self.assert_blocked(self.install(), 'process_inspection_failed')
        self.assertFalse(self.config.exists())

    def test_unknown_inline_project_plugin_and_managed_sources_block(self):
        config = self.home / '.codex/config.toml'
        for raw in ('[hooks]\nStop = []\n', '[plugins."remote@example"]\nenabled = true\n',
                    'hooks_file = "/unknown/hooks.json"\n', '[projects."relative"]\ntrust_level="trusted"\n'):
            with self.subTest(raw=raw):
                self.write(config, raw)
                self.assert_blocked(self.install(), 'unknown_hook_source')
                self.assertFalse(self.config.exists())

    def test_project_hook_layer_detected_without_reading_credentials(self):
        project = self.home / 'project'
        project.mkdir()
        self.write(self.home / '.codex/config.toml', '[projects.' + json.dumps(str(project)) + ']\ntrust_level="trusted"\n')
        self.write_json(project / '.codex/hooks.json', self.old_config())
        auth = self.home / '.codex/auth.json'
        self.write(auth, 'PRIVATE not valid JSON')
        self.assert_blocked(self.install(), 'unknown_hook_source')
        self.write_json(project / '.codex/hooks.json', {'hooks': {'Stop': [{'hooks': [THIRD]}]}})
        self.assertTrue(self.install()['ok'])
        self.assertEqual(auth.read_text(), 'PRIVATE not valid JSON')

    def test_unknown_plist_identity_blocks_bootout(self):
        self.legacy_agent()
        self.system.loaded['ProgramArguments'][1] = '/unrelated/codex_eink_sync.py'
        self.assert_blocked(self.install(), 'legacy_agent_conflict')
        self.assertEqual(self.system.bootouts, [])
        self.assertTrue(self.plist.exists())

    def test_restore_keeps_concurrent_unrelated_edits(self):
        original = self.old_config()
        self.write_json(self.config, original)
        self.assertTrue(self.install()['ok'])
        changed = self.read(self.config)
        changed['description'] = 'new description'
        added = {'type': 'command', 'command': '/usr/bin/true added'}
        changed['hooks']['Stop'][0]['hooks'].append(added)
        self.write_json(self.config, changed)
        self.assertTrue(self.setup.restore()['ok'])
        restored = self.read(self.config)
        self.assertEqual(restored['description'], 'new description')
        self.assertIn(added, restored['hooks']['Stop'][0]['hooks'])
        self.assertIn(original['hooks']['Stop'][0]['hooks'][1], restored['hooks']['Stop'][0]['hooks'])

    def test_modified_owned_entry_blocks_restore_without_overwrite(self):
        self.assertTrue(self.install()['ok'])
        changed = self.read(self.config)
        changed['hooks']['Stop'][0]['hooks'][0]['timeout'] = 42
        self.write_json(self.config, changed)
        self.assert_blocked(self.setup.restore(), 'owned_hook_conflict')
        self.assertEqual(self.read(self.config), changed)

    def test_bootout_failure_rolls_back_files_not_processes(self):
        original = self.old_config()
        self.write_json(self.config, original)
        self.legacy_agent()
        def fail():
            raise OSError('PRIVATE launch error')
        self.system.before_bootout = fail
        result = self.install()
        self.assert_blocked(result, 'bootout_failed')
        self.assertEqual(self.read(self.config), original)
        self.assertTrue(self.plist.exists())
        self.assertNotEqual(self.setup.inspect()['stage'], 'ready')

    def test_failure_rollback_never_overwrites_concurrent_user_edit(self):
        self.write_json(self.config, self.old_config())
        self.legacy_agent()
        modified = []
        def fail():
            changed = self.read(self.config)
            changed['description'] = 'USER MODIFIED'
            self.write_json(self.config, changed)
            modified.append(changed)
            raise OSError('failure')
        self.system.before_bootout = fail
        result = self.install()
        self.assert_blocked(result, 'rollback_conflict')
        self.assertEqual(self.read(self.config), modified[0])
        self.assertNotEqual(self.setup.inspect()['stage'], 'ready')

    def test_symlink_state_root_and_untrusted_runtime_refused(self):
        outside = self.home / 'outside'
        outside.mkdir()
        self.state.parent.mkdir(parents=True)
        self.state.symlink_to(outside, target_is_directory=True)
        self.assert_blocked(self.install(), 'unsafe_path')
        self.assertEqual(list(outside.iterdir()), [])
        self.state.unlink()
        self.python.chmod(0o777)
        self.assert_blocked(self.install(), 'unsafe_path')
        self.python.chmod(0o700)
        (self.runtime / 'tools/companion_setup.py').chmod(0o666)
        self.assert_blocked(self.install(), 'unsafe_path')
        self.assertFalse(self.config.exists())

    def test_receipt_requires_valid_event_current_identity_nonce_and_time(self):
        result = self.install()
        installation_id = result['installation_id']
        manifest = self.read(self.state / 'setup-installation.json')
        valid = {'session_id': 'thread', 'hook_event_name': 'Stop', 'cwd': str(self.home),
                 'prompt': 'PRIVATE', 'tool_input': {'value': 'PRIVATE'}}
        for event, identity, nonce in ((valid, 'wrong', manifest['nonce']),
                                        (valid, installation_id, 'wrong'),
                                        ({'session_id': 'thread'}, installation_id, manifest['nonce']),
                                        ({**valid, 'cwd': 4}, installation_id, manifest['nonce'])):
            self.assertEqual(self.setup.hook(event, installation_id=identity, nonce=nonce), {})
            self.assertFalse((self.state / 'setup-receipt.json').exists())
        self.system.clock += 1
        self.assertEqual(self.setup.hook(valid, installation_id=installation_id, nonce=manifest['nonce']), {})
        receipt = (self.state / 'setup-receipt.json').read_bytes()
        self.assertEqual(self.setup.inspect()['stage'], 'ready')
        self.assertEqual(self.read(self.state / 'status.json')['threads']['thread']['status'], 'waiting')
        self.assertGreater(self.read(self.state / 'sync-journal.json')['requested_revision'], 0)
        for path in self.state.rglob('*.json'):
            self.assertNotIn('PRIVATE', path.read_text())
        self.system.clock += 1
        self.setup.hook(valid, installation_id=installation_id, nonce=manifest['nonce'])
        self.assertEqual((self.state / 'setup-receipt.json').read_bytes(), receipt, 'receipt identity is stable')
        stale = json.loads(receipt)
        stale['event_timestamp'] = manifest['installed_at'] - 1
        self.write_json(self.state / 'setup-receipt.json', stale)
        self.assertNotEqual(self.setup.inspect()['stage'], 'ready')

    def test_receipt_not_written_on_ingest_failure_or_modified_definition(self):
        installed = self.install()
        manifest = self.read(self.state / 'setup-installation.json')
        self.system.clock += 1
        self.write(self.state / 'sync-journal.json', '{')
        self.setup.hook({'session_id': 's', 'hook_event_name': 'Stop'},
                        installation_id=installed['installation_id'], nonce=manifest['nonce'])
        self.assertFalse((self.state / 'setup-receipt.json').exists())
        (self.state / 'sync-journal.json').unlink()
        changed = self.read(self.config)
        changed['hooks']['Stop'][0]['matcher'] = 'never'
        self.write_json(self.config, changed)
        self.setup.hook({'session_id': 's', 'hook_event_name': 'Stop'},
                        installation_id=installed['installation_id'], nonce=manifest['nonce'])
        self.assertFalse((self.state / 'setup-receipt.json').exists())

    def test_restore_and_reinstall_cannot_reuse_old_receipt(self):
        first = self.install()
        manifest = self.read(self.state / 'setup-installation.json')
        self.system.clock += 1
        event = {'session_id': 'one', 'hook_event_name': 'Stop'}
        self.setup.hook(event, installation_id=first['installation_id'], nonce=manifest['nonce'])
        self.assertEqual(self.setup.inspect()['stage'], 'ready')
        self.assertTrue(self.setup.restore()['ok'])
        self.system.clock += 1
        second = self.install()
        self.assertNotEqual(second['installation_id'], first['installation_id'])
        self.assertEqual(second['stage'], 'awaiting_trust')
        manifest = self.read(self.state / 'setup-installation.json')
        self.system.clock += 1
        self.setup.hook(event, installation_id=second['installation_id'], nonce=manifest['nonce'])
        self.assertEqual(self.setup.inspect()['stage'], 'ready')

    def test_restore_partial_failure_rolls_back_and_is_retryable(self):
        self.write_json(self.config, self.old_config())
        self.legacy_agent()
        self.assertTrue(self.install()['ok'])
        installed = self.config.read_bytes()
        cas = self.setup.files.cas
        def fail_agent(path, expected, raw):
            if path == self.plist and raw is not None:
                raise OSError('disk error')
            return cas(path, expected, raw)
        self.setup.files.cas = fail_agent
        self.assert_blocked(self.setup.restore(), 'storage_error')
        self.assertEqual(self.config.read_bytes(), installed)
        self.setup.files.cas = cas
        self.assertTrue(self.setup.restore()['ok'])

    def test_backup_cas_preserves_concurrent_global_and_project_config(self):
        self.write_json(self.config, self.old_config())
        installed = self.config.read_bytes()
        cas = self.setup.files.cas
        def race(path, expected, raw):
            result = cas(path, expected, raw)
            if path == self.state / 'setup-installation.json' and json.loads(raw)['phase'] == 'installing':
                self.write(self.home / '.codex/config.toml', '[hooks]\nStop=[]\n')
            return result
        self.setup.files.cas = race
        self.assert_blocked(self.install(), 'config_changed')
        self.assertEqual(self.config.read_bytes(), installed)
        self.assertEqual((self.home / '.codex/config.toml').read_text(), '[hooks]\nStop=[]\n')

    def test_partial_manifest_and_tampered_backup_fail_closed(self):
        self.write_json(self.state / 'setup-installation.json', {'version': 1})
        self.assert_blocked(self.install(), 'installation_invalid')
        (self.state / 'setup-installation.json').unlink()
        self.write_json(self.config, self.old_config())
        self.assertTrue(self.install()['ok'])
        installed = self.config.read_bytes()
        manifest = self.read(self.state / 'setup-installation.json')
        manifest['backups']['hooks']['sha256'] = '0' * 64
        self.write_json(self.state / 'setup-installation.json', manifest)
        self.assert_blocked(self.setup.restore(), 'installation_invalid')
        self.assertEqual(self.config.read_bytes(), installed)

    def test_unknown_top_level_hook_source_and_partial_toml_block(self):
        self.write_json(self.config, {'hooks': {}, 'include': ['/unknown/hooks.json']})
        self.assert_blocked(self.install(), 'unknown_hook_source')
        self.config.unlink()
        self.write(self.home / '.codex/config.toml', 'model = {\n')
        self.assert_blocked(self.install(), 'config_invalid')

    def test_same_helper_different_state_cannot_install_second_sender_source(self):
        self.assertTrue(self.install()['ok'])
        other = self.mod.Setup(home=self.home, state_dir=self.home / 'other', cwd=self.home, system=self.system)
        self.assert_blocked(other.install(python=self.python, runtime_root=self.runtime), 'unknown_hook_source')

    def test_invalid_interpreter_is_not_executed_or_installed(self):
        self.assert_blocked(self.setup.install(python=Path('/usr/bin/true'), runtime_root=self.runtime), 'source_invalid')
        self.assertFalse(self.config.exists())

    def test_python_flags_and_canonical_build_sender_do_not_bypass_guard(self):
        self.system.running = [{'pid': 44, 'uid': os.getuid(), 'argv':
                               ['/usr/bin/python3', '-u', str(self.legacy / 'tools/codex_status_hook.py'), '--sync']}]
        self.assert_blocked(self.install(), 'sender_running')
        self.system.running = [{'pid': 44, 'uid': os.getuid(), 'argv':
                               [str(ROOT / '.build/arm64-apple-macosx/debug/eink-push'), '--input', '/tmp/frame.png']}]
        self.assert_blocked(self.install(), 'sender_running')

    def test_modified_legacy_job_flags_fail_closed(self):
        self.legacy_agent()
        self.system.loaded['ProgramArguments'] += ['--push-binary', '/unrelated/sender']
        self.write(self.plist, plistlib.dumps(self.system.loaded))
        self.assert_blocked(self.install(), 'legacy_agent_conflict')
        self.assertEqual(self.system.bootouts, [])

    def test_read_only_inspector_does_not_create_lock_and_honors_existing_lock(self):
        import fcntl
        self.state.mkdir(parents=True)
        lock_path = self.state / 'setup-migration.lock'
        with lock_path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_blocked(self.install(), 'migration_busy')
            self.assertFalse(self.config.exists())

    def test_procargs_preserves_spaced_arguments_and_ignores_environment(self):
        argv = ['/usr/bin/python3', str(self.legacy / 'tools/codex_status_hook.py'), '--sync']
        raw = struct.pack('i', 3) + b'/actual/python\0\0' + b'\0'.join(arg.encode() for arg in argv) + b'\0SECRET=value\0'
        self.assertEqual(self.mod.parse_procargs(raw), {'executable': '/actual/python', 'argv': argv})
        with self.assertRaises(self.mod.SetupError):
            self.mod.parse_procargs(b'broken')

    def test_cli_errors_json_and_hooks_always_neutral_without_live_install(self):
        for args, stdin, expected, exitcode in (([], None, 'blocked', 1),
                                               (['hook'], '{bad PRIVATE', None, 0),
                                               (['--state-dir', str(self.state), 'hook', '--installation-id', 'bad'], '{}', None, 0)):
            result = subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, exitcode, result.stderr)
            value = json.loads(result.stdout)
            if expected:
                self.assertEqual(value['stage'], expected)
                self.assertEqual(set(value), {'ok', 'stage', 'blockers', 'installation_id', 'legacy_present'})
            else:
                self.assertEqual(value, {})
            self.assertNotIn('PRIVATE', result.stdout + result.stderr)

    def test_native_direct_caller_allowed_but_worker_must_be_stopped(self):
        self.system.running = [{'pid': os.getppid(), 'uid': os.getuid(), 'argv':
                               ['/Applications/EInk Companion.app/Contents/MacOS/eink-companion']}]
        self.assertTrue(self.install()['ok'])
        self.system.running.append({'pid': 456, 'uid': os.getuid(), 'argv':
                                    ['/usr/bin/python3', str(self.runtime / 'tools/companion_bridge.py')]})
        self.assert_blocked(self.setup.restore(), 'sender_running')

    def test_source_replaced_after_backup_is_rolled_back(self):
        self.write_json(self.config, self.old_config())
        before = self.config.read_bytes()
        cas = self.setup.files.cas
        def race(path, expected, raw):
            result = cas(path, expected, raw)
            if path == self.config:
                self.write(self.runtime / 'tools/codex_status_core.py', 'unknown changed source')
            return result
        self.setup.files.cas = race
        self.assert_blocked(self.install(), 'source_changed')
        self.assertEqual(self.config.read_bytes(), before)

    def test_post_publish_failure_rolls_back_own_file(self):
        self.write_json(self.config, self.old_config())
        before = self.config.read_bytes()
        cas = self.setup.files.cas
        failed = []
        def publish_then_fail(path, expected, raw):
            result = cas(path, expected, raw)
            if path == self.config and not failed:
                failed.append(True)
                raise OSError('directory fsync failed after rename')
            return result
        self.setup.files.cas = publish_then_fail
        self.assert_blocked(self.install(), 'storage_error')
        self.assertEqual(self.config.read_bytes(), before)

    def test_orphaned_legacy_poll_process_is_not_assumed_stopped(self):
        self.system.running = [{'pid': 44, 'uid': os.getuid(), 'argv':
                               ['/usr/bin/python3', str(self.legacy / 'tools/codex_eink_sync.py'), '--no-push']}]
        self.assert_blocked(self.install(), 'sender_running')
        self.assert_blocked(self.setup.inspect(), 'sender_running')

    def test_known_registered_poller_is_the_only_process_bootout_may_stop(self):
        self.legacy_agent()
        self.system.loaded['pid'] = 444
        self.system.running = [{'pid': 444, 'uid': os.getuid(), 'argv': self.system.loaded['ProgramArguments']}]
        self.system.before_bootout = lambda: self.system.running.clear()
        self.assertTrue(self.install()['ok'])
        self.assertEqual(self.system.bootouts, [LABEL])

    def registered_apple_poller(self):
        self.legacy_agent()
        self.system.loaded['pid'] = 444
        executable = '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python'
        record = {'pid': 444, 'uid': os.getuid(), 'executable': executable,
                  'argv': [executable, *self.system.loaded['ProgramArguments'][1:]]}
        self.system.running = [record]
        return record

    def test_apple_launcher_poller_inspection_reports_legacy_without_writes(self):
        record = self.registered_apple_poller()
        before = self.plist.read_bytes()
        for argv0 in (record['executable'], '/usr/bin/python3', 'python3'):
            with self.subTest(argv0=argv0):
                record['argv'][0] = argv0
                self.assertEqual(self.setup.inspect(), {
                    'ok': True, 'stage': 'legacy_active', 'blockers': [],
                    'installation_id': None, 'legacy_present': True})
                self.assertEqual(self.plist.read_bytes(), before)
                self.assertFalse(self.state.exists())
                self.assertEqual(self.system.bootouts, [])

    def test_apple_launcher_poller_explicit_install_backs_up_and_stops_only_job(self):
        self.registered_apple_poller()
        original = self.old_config()
        self.write_json(self.config, original)
        self.system.before_bootout = lambda: self.system.running.clear()
        result = self.install()
        self.assertEqual(result['stage'], 'awaiting_trust', result)
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.read(self.config)['hooks']['Stop'][0]['hooks'][0], THIRD)
        self.assertEqual(self.system.bootouts, [LABEL])
        self.assertTrue(self.setup.restore()['ok'])
        self.assertEqual(self.read(self.config), original)
        self.assertTrue(self.plist.exists())

    def test_registered_poller_identity_mismatch_never_allows_bootout(self):
        record = self.registered_apple_poller()
        baseline = copy.deepcopy(record)
        cases = {
            'other pid': {'pid': 445},
            'not python': {'executable': '/usr/bin/true'},
            'changed flag': {'argv': [record['executable'], record['argv'][1], '--force']},
            'extra flag': {'argv': record['argv'] + ['--once']},
            'other script': {'argv': [record['executable'], '/elsewhere/codex_eink_sync.py', '--no-push']},
        }
        before = self.plist.read_bytes()
        for name, changes in cases.items():
            with self.subTest(name=name):
                self.system.running = [{**baseline, **changes}]
                self.assert_blocked(self.setup.inspect(), 'sender_running')
                self.assert_blocked(self.install(), 'sender_running')
                self.assertEqual(self.plist.read_bytes(), before)
                self.assertFalse(self.state.exists())
                self.assertEqual(self.system.bootouts, [])

    def test_registered_poller_does_not_hide_additional_hook_or_ble_sender(self):
        poller = self.registered_apple_poller()
        for argv in ([ '/usr/bin/python3', str(self.legacy / 'tools/codex_status_hook.py'), '--sync'],
                     [str(self.legacy / 'bin/eink-push'), '--input', '/tmp/frame.png'],
                     poller['argv']):
            with self.subTest(argv=argv):
                self.system.running = [poller, {'pid': 445, 'uid': os.getuid(), 'argv': argv}]
                self.assert_blocked(self.setup.inspect(), 'sender_running')
                self.assert_blocked(self.install(), 'sender_running')
                self.assertEqual(self.system.bootouts, [])
                self.assertFalse(self.state.exists())

    def test_encoded_toml_hook_key_or_duplicate_key_does_not_evade_source_guard(self):
        path = self.home / '.codex/config.toml'
        for raw, code in (('["ho\\u006fks"]\nStop=[]\n', 'unknown_hook_source'),
                          ('model="one"\nmodel="two"\n', 'config_invalid')):
            self.write(path, raw)
            self.assert_blocked(self.install(), code)
            self.assertFalse(self.config.exists())

    def test_old_backup_is_retained_across_new_installations(self):
        self.write_json(self.config, self.old_config())
        first = self.install()
        backup = self.state / ('setup-backup-' + first['installation_id'] + '.json')
        self.assertTrue(backup.exists())
        data = backup.read_bytes()
        self.assertTrue(self.setup.restore()['ok'])
        self.assertTrue(self.install()['ok'])
        self.assertEqual(backup.read_bytes(), data)

    def test_state_file_lock_symlink_cannot_write_external_metadata(self):
        first = self.install()
        manifest = self.read(self.state / 'setup-installation.json')
        external = self.home / 'keep.json'
        self.write(external, 'DO NOT MODIFY')
        (self.state / 'status.json').symlink_to(external)
        self.system.clock += 1
        self.setup.hook({'session_id': 's', 'hook_event_name': 'Stop'},
                        installation_id=first['installation_id'], nonce=manifest['nonce'])
        self.assertFalse((self.state / 'setup-receipt.json').exists())
        self.assertEqual(external.read_text(), 'DO NOT MODIFY')

    def test_missing_owned_hook_invalidates_receipt_readiness(self):
        first = self.install()
        manifest = self.read(self.state / 'setup-installation.json')
        self.system.clock += 1
        self.setup.hook({'session_id': 's', 'hook_event_name': 'Stop'},
                        installation_id=first['installation_id'], nonce=manifest['nonce'])
        self.assertEqual(self.setup.inspect()['stage'], 'ready')
        current = self.read(self.config)
        del current['hooks']['SessionEnd']
        self.write_json(self.config, current)
        self.assert_blocked(self.setup.inspect(), 'owned_hook_conflict')

    def test_tampered_removed_entries_cannot_inject_a_restore_hook(self):
        self.write_json(self.config, self.old_config())
        self.assertTrue(self.install()['ok'])
        installed = self.config.read_bytes()
        manifest = self.read(self.state / 'setup-installation.json')
        manifest['removed'][0]['handler']['command'] = '/tmp/not-ours'
        self.write_json(self.state / 'setup-installation.json', manifest)
        self.assert_blocked(self.setup.restore(), 'installation_invalid')
        self.assertEqual(self.config.read_bytes(), installed)

    def test_install_refuses_oversize_backup_before_mutating(self):
        config = {'description': 'x' * 800000, 'hooks': {}}
        self.write_json(self.config, config)
        before = self.config.read_bytes()
        self.assert_blocked(self.install(), 'backup_too_large')
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state / 'setup-installation.json').exists())

    def test_foreign_owned_state_root_and_hardlinks_refused(self):
        self.system.uid = os.getuid() + 1
        other = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assert_blocked(other.install(python=self.python, runtime_root=self.runtime), 'unsafe_path')
        self.system.uid = os.getuid()
        self.config.parent.mkdir()
        self.write(self.home / 'original.json', '{"hooks": {}}')
        os.link(self.home / 'original.json', self.config)
        self.assert_blocked(self.install(), 'unsafe_path')

    def test_duplicate_installed_hook_cannot_make_ready(self):
        self.assertTrue(self.install()['ok'])
        changed = self.read(self.config)
        changed['hooks']['Stop'].append(copy.deepcopy(changed['hooks']['Stop'][0]))
        self.write_json(self.config, changed)
        self.assert_blocked(self.setup.inspect(), 'owned_hook_conflict')

    def test_actual_macos_adapter_commands_are_read_only_except_exact_bootout(self):
        # The subprocess boundary is simulated: never changes live launchd state.
        adapter = self.mod.MacSystem()
        calls = []
        def run(args):
            calls.append(args)
            if args[1] == 'print':
                return subprocess.CompletedProcess(args, 0, b'job = {\n arguments = {\n /usr/bin/python3\n /owned/script.py\n --no-push\n }\n pid = 123\n}\n', b'')
            return subprocess.CompletedProcess(args, 0, b'', b'')
        adapter.run = run
        job = adapter.job(LABEL)
        self.assertEqual(job['ProgramArguments'], ['/usr/bin/python3', '/owned/script.py', '--no-push'])
        self.assertEqual(job['pid'], 123)
        adapter.bootout(LABEL)
        self.assertEqual(calls, [['/bin/launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + LABEL],
                                 ['/bin/launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + LABEL]])
        with self.assertRaises(self.mod.SetupError):
            adapter.bootout('someone.else')
        adapter.run = lambda args: subprocess.CompletedProcess(args, 5, b'', b'Input/output error PRIVATE')
        with self.assertRaises(self.mod.SetupError):
            adapter.job(LABEL)
        adapter.run = lambda args: subprocess.CompletedProcess(args, 113, b'', b'Could not find service')
        self.assertIsNone(adapter.job(LABEL))

    def test_temporary_bundle_can_be_inspected_but_not_installed(self):
        self.system.temporary = [self.runtime.parent]
        self.assertEqual(self.setup.inspect()['stage'], 'needs_install')
        self.assert_blocked(self.install(), 'runtime_location_unstable')
        self.assertFalse(self.config.exists())
        self.assertFalse(self.state.exists())

    def test_removed_runtime_blocks_existing_receipt_readiness(self):
        first = self.install()
        manifest = self.read(self.state / 'setup-installation.json')
        self.system.clock += 1
        self.setup.hook({'session_id': 's', 'hook_event_name': 'Stop'},
                        installation_id=first['installation_id'], nonce=manifest['nonce'])
        self.assertEqual(self.setup.inspect()['stage'], 'ready')
        self.runtime.rename(self.home / 'moved-runtime')
        self.assert_blocked(self.setup.inspect(), 'source_invalid')
        # Recovery is file-only and must remain possible after the app was moved.
        self.assertTrue(self.setup.restore()['ok'])

    def test_review_snapshot_transform_backup_and_cas_use_one_image(self):
        self.write_json(self.config, self.old_config())
        read = self.setup.files.read
        count = []
        added = {'type': 'command', 'command': '/usr/bin/true third-party-add'}
        def race(path):
            if path == self.config:
                count.append(True)
                if len(count) == 2:
                    current = self.read(self.config)
                    current['hooks']['Stop'][0]['hooks'].append(added)
                    self.write_json(self.config, current)
            return read(path)
        self.setup.files.read = race
        result = self.install()
        self.assertFalse(result['ok'])
        self.assertIn(added, self.read(self.config)['hooks']['Stop'][0]['hooks'])

    def test_review_exclusive_publish_preserves_intervening_destination(self):
        self.write(self.config, 'ORIGINAL')
        before = self.setup.files.read(self.config)
        original_replace = os.replace
        exclusive = getattr(self.setup.files, 'rename_exclusive', original_replace)
        injected = []
        def race(source, target):
            if Path(target) == self.config and not injected:
                injected.append(True)
                self.write(self.config, 'THIRD PARTY')
            return exclusive(source, target)
        self.setup.files.rename_exclusive = race
        with patch.object(self.mod.os, 'replace', side_effect=race):
            try:
                self.setup.files.cas(self.config, before, b'OUR CANDIDATE')
            except (OSError, self.mod.SetupError):
                pass
        self.assertTrue(injected)
        self.assertEqual(self.config.read_text(), 'THIRD PARTY')
        self.assertTrue(any(p.is_file() and p.read_bytes() == b'ORIGINAL' for p in self.config.parent.iterdir()),
                        'withdrawn original must be retained')

    def test_review_dotted_project_and_ancestor_hook_layer_are_not_omitted(self):
        project = self.home / 'workspace/child'
        project.mkdir(parents=True)
        self.write(self.home / '.codex/config.toml', 'projects.' + json.dumps(str(project)) + '.trust_level="trusted"\n')
        self.write_json(project.parent / '.codex/hooks.json', self.old_config())
        self.assert_blocked(self.install(), 'unknown_hook_source')
        self.assertFalse(self.config.exists())

    def test_review_actual_executable_is_kept_when_argv0_is_relative(self):
        argv = ['python3', str(self.legacy / 'tools/codex_status_hook.py'), '--sync']
        raw = struct.pack('i', 3) + b'/usr/bin/python3\0\0' + b'\0'.join(v.encode() for v in argv) + b'\0'
        parsed = self.mod.parse_procargs(raw)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed['executable'], '/usr/bin/python3')
        self.assertEqual(parsed['argv'], argv)
        self.system.running = [{'pid': 456, 'uid': os.getuid(), **parsed}]
        self.assert_blocked(self.install(), 'sender_running')

    def test_review_published_manifest_fsync_failure_cannot_leave_installed_marker(self):
        self.write_json(self.config, self.old_config())
        before = self.config.read_bytes()
        cas = self.setup.files.cas
        failed = []
        def fault(path, expected, raw):
            result = cas(path, expected, raw)
            if path == self.state / 'setup-installation.json' and json.loads(raw)['phase'] == 'installed' and not failed:
                failed.append(True)
                raise OSError('fsync after publication')
            return result
        self.setup.files.cas = fault
        self.assertFalse(self.install()['ok'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertNotEqual(self.read(self.state / 'setup-installation.json')['phase'], 'installed')

    def test_review_interrupted_restoring_can_resume_with_fresh_helper(self):
        self.write_json(self.config, self.old_config())
        self.legacy_agent()
        self.assertTrue(self.install()['ok'])
        cas = self.setup.files.cas
        class Crash(BaseException):
            pass
        def crash(path, expected, raw):
            result = cas(path, expected, raw)
            if path == self.config:
                raise Crash()
            return result
        self.setup.files.cas = crash
        with self.assertRaises(Crash):
            self.setup.restore()
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assertTrue(fresh.restore()['ok'])
        self.assertEqual(self.read(self.config), self.old_config())
        self.assertTrue(self.plist.exists())
        self.assertIsNone(self.system.loaded)

    def test_review_first_manifest_publish_failure_is_restorable(self):
        self.write_json(self.config, self.old_config())
        cas = self.setup.files.cas
        failed = []
        def fail(path, expected, raw):
            result = cas(path, expected, raw)
            if path == self.state / 'setup-installation.json' and not failed:
                failed.append(True)
                raise OSError('manifest publication durability failure')
            return result
        self.setup.files.cas = fail
        self.assertFalse(self.install()['ok'])
        self.setup.files.cas = cas
        self.assertTrue(self.setup.restore()['ok'])

    def test_review_identical_group_metadata_restores_without_merging_groups(self):
        original = self.old_config()
        original['hooks']['Stop'].append(copy.deepcopy(original['hooks']['Stop'][0]))
        original['hooks']['Stop'][1]['hooks'][0]['command'] = '/usr/bin/true second-group'
        self.write_json(self.config, original)
        self.assertTrue(self.install()['ok'])
        self.assertTrue(self.setup.restore()['ok'])
        self.assertEqual(self.read(self.config), original)

    def test_review_projects_parent_and_feature_flag_are_not_hook_sources(self):
        project = self.home / 'project'
        self.write(self.home / '.codex/config.toml', '[projects]\n[projects.' + json.dumps(str(project)) + ']\ntrust_level="trusted"\n[features]\nhooks=true\n')
        self.assertTrue(self.install()['ok'])

    def test_review_crash_after_withdrawal_recovers_vacant_path_without_overwrite(self):
        self.write(self.config, 'ORIGINAL')
        before = self.setup.files.read(self.config)
        self.assertTrue(hasattr(self.setup.files, 'rename_exclusive'), 'no exclusive publication primitive')
        rename = self.setup.files.rename_exclusive
        class Crash(BaseException):
            pass
        def crash(source, target):
            result = rename(source, target)
            if Path(source) == self.config:
                raise Crash()
            return result
        self.setup.files.rename_exclusive = crash
        with self.assertRaises(Crash):
            self.setup.files.cas(self.config, before, b'NEW')
        self.assertFalse(self.config.exists())
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assert_blocked(fresh.inspect(), 'migration_incomplete')
        fresh.files.recover_pending()
        self.assertEqual(self.config.read_text(), 'ORIGINAL')

    def test_review_crash_recovery_preserves_intervening_file_and_retained_original(self):
        self.write(self.config, 'ORIGINAL')
        before = self.setup.files.read(self.config)
        self.assertTrue(hasattr(self.setup.files, 'rename_exclusive'), 'no exclusive publication primitive')
        rename = self.setup.files.rename_exclusive
        class Crash(BaseException):
            pass
        def crash(source, target):
            result = rename(source, target)
            if Path(source) == self.config:
                raise Crash()
            return result
        self.setup.files.rename_exclusive = crash
        with self.assertRaises(Crash):
            self.setup.files.cas(self.config, before, b'NEW')
        self.write(self.config, 'USER NEW')
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        with self.assertRaises(self.mod.SetupError):
            fresh.files.recover_pending()
        self.assertEqual(self.config.read_text(), 'USER NEW')
        self.assertTrue(any(p.is_file() and p.read_bytes() == b'ORIGINAL' for p in self.config.parent.iterdir()))

    def test_review_completely_identical_legacy_groups_keep_multiplicity(self):
        original = self.old_config()
        original['hooks']['Stop'].append(copy.deepcopy(original['hooks']['Stop'][0]))
        self.write_json(self.config, original)
        self.assertTrue(self.install()['ok'])
        self.assertTrue(self.setup.restore()['ok'])
        self.assertEqual(self.read(self.config), original)

    def test_review_real_manifest_directory_fsync_failure_is_recoverable(self):
        self.write_json(self.config, self.old_config())
        before = self.config.read_bytes()
        sync = self.setup.files.sync_directory
        failed = []
        def fault(directory):
            manifest_path = self.state / 'setup-installation.json'
            if directory == self.state and manifest_path.exists() and self.read(manifest_path)['phase'] == 'installed' and not failed:
                failed.append(True)
                raise OSError('real directory fsync failure')
            return sync(directory)
        self.setup.files.sync_directory = fault
        self.assertFalse(self.install()['ok'])
        self.assertTrue(failed)
        self.assertEqual(self.config.read_bytes(), before)
        self.setup.files.sync_directory = sync
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assertTrue(fresh.restore()['ok'])
        self.assertFalse(fresh.files.pending())

    def test_review_modified_restore_intent_cannot_inject_unowned_command(self):
        self.write_json(self.config, self.old_config())
        self.assertTrue(self.install()['ok'])
        cas = self.setup.files.cas
        class Crash(BaseException):
            pass
        def crash(path, expected, raw):
            result = cas(path, expected, raw)
            if path == self.state / 'setup-installation.json' and json.loads(raw)['phase'] == 'restoring':
                raise Crash()
            return result
        self.setup.files.cas = crash
        with self.assertRaises(Crash):
            self.setup.restore()
        manifest = self.read(self.state / 'setup-installation.json')
        intent_path = self.state / ('setup-restore-' + manifest['installation_id'] + '.json')
        intent = self.read(intent_path)
        intent['hooks_after'] = self.setup.backup({'raw': b'{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"/tmp/unowned"}]}]}}'})
        self.write_json(intent_path, intent)
        before = self.config.read_bytes()
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assert_blocked(fresh.restore(), 'transaction_invalid')
        self.assertEqual(self.config.read_bytes(), before)

    def test_hundreds_of_events_keep_proof_and_transaction_files_bounded(self):
        first = self.install()
        manifest = self.read(self.state / 'setup-installation.json')
        self.system.clock += 1
        event = {'session_id': 'long-use', 'hook_event_name': 'Stop', 'turn_id': 'initial'}
        self.setup.hook(event, installation_id=first['installation_id'], nonce=manifest['nonce'])
        receipt = (self.state / 'setup-receipt.json').read_bytes()
        files_before = {p.relative_to(self.home) for p in self.home.rglob('*') if p.is_file()}
        for index in range(400):
            self.system.clock += 1
            event['turn_id'] = 'turn-' + str(index)
            self.setup.hook(event, installation_id=first['installation_id'], nonce=manifest['nonce'])
        files_after = {p.relative_to(self.home) for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(files_after, files_before, 'events must not add receipts, WAL entries, or retained versions')
        self.assertEqual((self.state / 'setup-receipt.json').read_bytes(), receipt)
        latest = self.read(self.state / 'status.json')['threads']['long-use']
        self.assertEqual(latest['turn_id'], 'turn-399')
        self.assertEqual(latest['updated_at'], int(self.system.clock))
        self.assertEqual(self.read(self.state / 'sync-journal.json')['requested_revision'], 401)
        self.assertEqual(self.setup.inspect()['stage'], 'ready')

    def test_api_trust_is_required_in_addition_to_valid_receipt(self):
        first = self.install()
        manifest = self.read(self.state / 'setup-installation.json')
        self.system.clock += 1
        self.setup.hook({'session_id': 's', 'hook_event_name': 'Stop'},
                        installation_id=first['installation_id'], nonce=manifest['nonce'])
        self.system.hook_trust = 'untrusted'
        self.assertEqual(self.setup.inspect()['stage'], 'awaiting_trust')
        self.system.hook_trust = 'modified'
        self.assertEqual(self.setup.inspect()['stage'], 'awaiting_trust')
        self.system.hook_trust = 'trusted'
        self.assertEqual(self.setup.inspect()['stage'], 'ready')
        self.assertEqual(self.system.active_probes, 0)

    def test_unavailable_stable_api_blocks_install_without_ignoring_sources(self):
        self.write_json(self.config, self.old_config())
        before = self.config.read_bytes()
        self.system.api_unavailable = True
        self.assert_blocked(self.install(), 'source_api_unavailable')
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.system.active_probes, 0)

    def test_current_chatgpt_bundle_gui_must_stop_before_install(self):
        self.system.running = [{'pid': 44, 'uid': os.getuid(), 'argv': ['ChatGPT'],
                                'executable': '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'}]
        self.assert_blocked(self.install(), 'codex_running')
        self.assertFalse(self.config.exists())

    def test_review2_recovery_checks_installed_runtime_before_any_file_write(self):
        self.assertTrue(self.install()['ok'])
        before = self.setup.files.read(self.config)
        rename = self.setup.files.rename_exclusive
        class Crash(BaseException):
            pass
        def crash(source, target):
            result = rename(source, target)
            if Path(source) == self.config:
                raise Crash()
            return result
        self.setup.files.rename_exclusive = crash
        with self.assertRaises(Crash):
            self.setup.files.cas(self.config, before, b'{"hooks":{}}')
        self.assertFalse(self.config.exists())
        self.system.running = [{'pid': 999, 'uid': os.getuid(), 'argv':
                               ['/usr/bin/python3', str(self.runtime / 'tools/companion_bridge.py')]}]
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assert_blocked(fresh.restore(), 'sender_running')
        self.assertFalse(self.config.exists(), 'recovery must not publish before checking runtime A')
        self.assertTrue(fresh.files.pending())

    def test_review2_relative_owned_python_script_fails_closed(self):
        self.system.running = [{'pid': 456, 'uid': os.getuid(), 'executable': '/usr/bin/python3',
                                'argv': ['python3', 'tools/codex_status_hook.py', '--sync']}]
        self.assert_blocked(self.install(), 'sender_identity_ambiguous')
        self.assertFalse(self.config.exists())

    def test_review2_new_restore_attempt_keeps_old_intent_and_unrelated_edit(self):
        self.write_json(self.config, self.old_config())
        self.legacy_agent()
        installed = self.install()
        self.assertTrue(installed['ok'])
        cas = self.setup.files.cas
        def fail(path, expected, raw):
            if path == self.plist and raw is not None:
                raise OSError('fixture restore failure')
            return cas(path, expected, raw)
        self.setup.files.cas = fail
        self.assertFalse(self.setup.restore()['ok'])
        old_intent = (self.state / ('setup-restore-' + installed['installation_id'] + '.json')).read_bytes()
        current = self.read(self.config)
        current['description'] = 'unrelated edit after failure'
        self.write_json(self.config, current)
        self.setup.files.cas = cas
        self.assertTrue(self.setup.restore()['ok'])
        self.assertEqual(self.read(self.config)['description'], 'unrelated edit after failure')
        attempts = list(self.state.glob('setup-restore-' + installed['installation_id'] + '-*.json'))
        self.assertGreaterEqual(len(attempts), 2)
        self.assertTrue(any(p.read_bytes() == old_intent for p in attempts))

    def test_review2_interrupted_external_recovery_is_reported_not_forgotten(self):
        self.write(self.config, 'BASE')
        before = self.setup.files.read(self.config)
        class Crash(BaseException):
            pass
        def crash_finish(operation, status):
            raise Crash()
        self.setup.files.finish_operation = crash_finish
        with self.assertRaises(Crash):
            self.setup.files.cas(self.config, before, b'CANDIDATE')
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        rename = fresh.files.rename_exclusive
        def race(source, target):
            if Path(source) == self.config:
                self.write(self.config, 'EXTERNAL NEW VERSION')
                rename(source, target)
                raise Crash()
            return rename(source, target)
        fresh.files.rename_exclusive = race
        with self.assertRaises(Crash):
            fresh.files.recover_pending()
        again = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        with self.assertRaises(self.mod.SetupError) as error:
            again.files.recover_pending()
        self.assertEqual(error.exception.code, 'recovery_conflict')
        self.assertTrue(again.files.pending())
        self.assertTrue(any(p.is_file() and p.read_bytes() == b'EXTERNAL NEW VERSION' for p in self.config.parent.iterdir()))

    def test_review2_system_app_parent_boundary_not_writable_bundle(self):
        import stat
        import types
        target = Path('/Applications/Fixture.app/Contents/Resources/runtime/tools/helper.py')
        original = Path.lstat
        bundle_mode = [0o755]
        def fake_stat(path, *args, **kwargs):
            if path == Path('/Applications'):
                return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0, st_gid=80, st_nlink=1)
            if Path('/Applications/Fixture.app') == path or Path('/Applications/Fixture.app') in path.parents:
                mode = stat.S_IFREG | 0o644 if path == target else stat.S_IFDIR | bundle_mode[0]
                return types.SimpleNamespace(st_mode=mode, st_uid=0, st_gid=0, st_nlink=1)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', fake_stat):
            self.assertEqual(self.mod.safe_path(target, os.getuid()), target)
            self.assertEqual(self.mod.safe_path(target, os.getuid(), root=Path('/Applications/Fixture.app')), target)
            bundle_mode[0] = 0o775
            with self.assertRaises(self.mod.SetupError):
                self.mod.safe_path(target, os.getuid())

    def test_review2_vacant_manifest_uses_backup_identity_before_recovery(self):
        self.assertTrue(self.install()['ok'])
        manifest_path = self.state / 'setup-installation.json'
        before = self.setup.files.read(manifest_path)
        rename = self.setup.files.rename_exclusive
        class Crash(BaseException):
            pass
        def crash(source, target):
            result = rename(source, target)
            if Path(source) == manifest_path:
                raise Crash()
            return result
        self.setup.files.rename_exclusive = crash
        with self.assertRaises(Crash):
            self.setup.files.cas(manifest_path, before, before['raw'])
        self.assertFalse(manifest_path.exists())
        self.system.running = [{'pid': 999, 'uid': os.getuid(), 'argv':
                               ['/usr/bin/python3', str(self.runtime / 'tools/companion_bridge.py')]}]
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assert_blocked(fresh.restore(), 'sender_running')
        self.assertFalse(manifest_path.exists())
        self.assertTrue(fresh.files.pending())

    def test_review2_untrusted_backup_does_not_authorize_recovery(self):
        first = self.install()
        before = self.setup.files.read(self.config)
        rename = self.setup.files.rename_exclusive
        class Crash(BaseException):
            pass
        def crash(source, target):
            result = rename(source, target)
            if Path(source) == self.config:
                raise Crash()
            return result
        self.setup.files.rename_exclusive = crash
        with self.assertRaises(Crash):
            self.setup.files.cas(self.config, before, b'{"hooks":{}}')
        archive = self.state / ('setup-backup-' + first['installation_id'] + '.json')
        self.write(archive, '{broken')
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assert_blocked(fresh.restore(), 'installation_invalid')
        self.assertFalse(self.config.exists())

    def test_review2_pending_recovery_guards_retained_previous_runtime_too(self):
        self.assertTrue(self.install()['ok'])
        old_runtime = self.runtime
        self.assertTrue(self.setup.restore()['ok'])
        self.runtime = self.home / 'Runtime B'
        shutil.copytree(old_runtime, self.runtime)
        self.assertTrue(self.install()['ok'])
        before = self.setup.files.read(self.config)
        rename = self.setup.files.rename_exclusive
        class Crash(BaseException):
            pass
        def crash(source, target):
            result = rename(source, target)
            if Path(source) == self.config:
                raise Crash()
            return result
        self.setup.files.rename_exclusive = crash
        with self.assertRaises(Crash):
            self.setup.files.cas(self.config, before, b'{"hooks":{}}')
        self.system.running = [{'pid': 444, 'uid': os.getuid(), 'argv':
                               ['/usr/bin/python3', str(old_runtime / 'tools/companion_bridge.py')]}]
        fresh = self.mod.Setup(home=self.home, state_dir=self.state, cwd=self.home, system=self.system)
        self.assert_blocked(fresh.restore(), 'sender_running')
        self.assertFalse(self.config.exists())


if __name__ == '__main__':
    unittest.main()
