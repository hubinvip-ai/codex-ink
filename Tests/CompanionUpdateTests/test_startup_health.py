"""Startup health must allow recovery, without treating a live PID as readiness."""
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import update_companion as u


class StartupHealthTests(unittest.TestCase):
    def app(self):
        app = object.__new__(u.LocalApp)
        app.target = Path('/fixture/Codex Ink.app')
        app.state = Path('/fixture/state')
        app.settings = {'sync_enabled': True, 'paused': False}
        app.pids = lambda: [42]
        return app

    def exercise(self, app, worker_at, inspect):
        now = [0.0]
        expected = str(app.target / u.RESOURCE / 'sync-runtime/tools/companion_bridge.py')
        def rows():
            return [(43, expected + ' --state-dir ' + str(app.state) + ' --session-id test')] if now[0] >= worker_at else []
        def sleep(seconds):
            now[0] += seconds
        app.inspect = lambda **kwargs: inspect()
        with patch.object(u.time, 'monotonic', side_effect=lambda: now[0]), patch.object(u.time, 'sleep', side_effect=sleep), patch.object(u, 'process_rows', side_effect=rows):
            return app.healthy(), now[0]

    def test_worker_recovers_after_two_startup_check_timeouts(self):
        ok, elapsed = self.exercise(self.app(), 50, lambda: None)
        self.assertTrue(ok)
        self.assertGreaterEqual(elapsed, 52)

    def test_transient_final_inspection_is_retried(self):
        calls = []
        def inspect():
            calls.append(True)
            if len(calls) == 1:
                raise u.UpdateError('command_failed: /fixture/python')
        ok, _ = self.exercise(self.app(), 0, inspect)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)

    def test_live_app_without_required_worker_never_passes(self):
        ok, elapsed = self.exercise(self.app(), 1000, lambda: self.fail('no worker'))
        self.assertFalse(ok)
        self.assertLessEqual(elapsed, 90)

    def test_persistent_inspection_failure_never_passes(self):
        def inspect():
            raise u.UpdateError('command_failed: /fixture/python')
        ok, elapsed = self.exercise(self.app(), 0, inspect)
        self.assertFalse(ok)
        self.assertLessEqual(elapsed, 90)

    def test_configuration_blocker_still_fails_immediately(self):
        def inspect():
            raise u.UpdateError('existing_setup_not_ready')
        with self.assertRaisesRegex(u.UpdateError, 'existing_setup_not_ready'):
            self.exercise(self.app(), 0, inspect)

    def test_paused_app_needs_no_worker_but_still_checks_setup(self):
        app = self.app(); app.settings['paused'] = True
        calls = []
        ok, _ = self.exercise(app, 1000, lambda: calls.append(True))
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
