import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools/codex_eink_reliable.py'


class CliTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'explicit-path reliable sync CLI is not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'external-source' / 'status.json'
        self.directory = self.root / 'consumer'

    def command(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(SCRIPT), '--state-dir', str(self.directory), '--state-file', str(self.state), *args], input=stdin, text=True, capture_output=True, timeout=10)

    def test_requires_explicit_paths(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'request'], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)

    def test_request_and_status_persist_across_processes(self):
        result = self.command('request', '--force')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'queued')
        status = json.loads(self.command('status').stdout)
        self.assertEqual(status['requested_revision'], 1)
        self.assertEqual(status['acknowledged_revision'], 0)
        self.assertEqual(status['force_revision'], 1)
        self.assertIsNone(status['last_sent_at'])
        self.assertFalse(self.state.exists())

    def test_hook_ingests_metadata_without_waiting_for_ble(self):
        event = {'session_id':'thread', 'turn_id':'turn', 'cwd':'/private/project', 'hook_event_name':'Stop', 'prompt':'secret prompt', 'tool_input':{'secret':'secret arguments'}}
        result = self.command('hook', stdin=json.dumps(event))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
        state = json.loads(self.state.read_text())
        self.assertEqual(state['threads']['thread']['status'], 'waiting')
        self.assertNotIn('secret', self.state.read_text())
        journal = json.loads(self.command('status').stdout)
        self.assertEqual(journal['requested_revision'], 1)
        self.assertIsNone(journal['last_sent_at'])
        self.assertNotIn('/private', json.dumps(journal))

    def test_bad_hook_input_is_neutral_and_never_creates_state(self):
        for payload in ('broken', '[]', '{"session_id":"a", "hook_event_name":"unexpected"}', '{"session_id":"a", "hook_event_name":[]}'):
            result = self.command('hook', stdin=payload)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {})
            self.assertFalse(self.state.exists())

    def test_corrupted_source_is_preserved_on_hook(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text('broken')
        result = self.command('hook', stdin=json.dumps({'session_id':'thread', 'hook_event_name':'Stop'}))
        self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(self.state.read_text(), 'broken')
        self.assertIn('source_state_invalid', result.stderr)

    def test_corrupted_journal_errors_without_reset(self):
        self.command('request')
        journal = self.directory / 'sync-journal.json'
        journal.write_text('broken')
        result = self.command('status')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(journal.read_text(), 'broken')
        self.assertEqual(json.loads(result.stderr)['error_code'], 'journal_invalid')

    def test_work_requires_explicit_executables_and_device(self):
        result = self.command('work')
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
