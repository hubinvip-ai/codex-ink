import json
import tempfile
import unittest
from pathlib import Path
from tools.build_companion import package_app


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old_dir = self.root/'installed'; self.old_dir.mkdir()
        self.new_dir = self.root/'release'; self.new_dir.mkdir()
        self.target = package_app(Path('/usr/bin/true'), self.old_dir)
        self.source = package_app(Path('/usr/bin/true'), self.new_dir)

    def updater(self):
        from tools import update_companion
        return update_companion

    def test_preserves_frozen_runtime_and_replaces_sync_runtime(self):
        u = self.updater()
        # Add a file before re-signing to represent a distinct installed hook runtime.
        old_file = self.target/'Contents/Resources/runtime/old-marker'
        old_file.write_text('trusted-original')
        u.sign(self.target)
        tx = u.prepare(self.source, self.target)
        candidate = tx/'candidate.app'
        self.assertEqual((candidate/'Contents/Resources/runtime/old-marker').read_text(), 'trusted-original')
        self.assertFalse((candidate/'Contents/Resources/sync-runtime/old-marker').exists())
        self.assertEqual(old_file.read_text(), 'trusted-original')
        u.verify_signature(candidate)

    def test_success_keeps_original_as_rollback(self):
        u = self.updater(); tx = u.prepare(self.source, self.target)
        old_inode = self.target.stat().st_ino
        u.activate(tx, self.target, launch=lambda: None, healthy=lambda: True, stop=lambda: None)
        self.assertNotEqual(self.target.stat().st_ino, old_inode)
        self.assertEqual((tx/'candidate.app').stat().st_ino, old_inode)
        self.assertEqual(json.loads((tx/'transaction.json').read_text())['phase'], 'complete')

    def test_failed_start_restores_original(self):
        u = self.updater(); tx = u.prepare(self.source, self.target)
        inode = self.target.stat().st_ino; events = []
        with self.assertRaises(u.UpdateError):
            u.activate(tx, self.target, launch=lambda: events.append('launch'),
                       healthy=lambda: False, stop=lambda: events.append('stop'))
        self.assertEqual(self.target.stat().st_ino, inode)
        self.assertEqual(events, ['launch','stop','launch'])
        self.assertEqual(json.loads((tx/'transaction.json').read_text())['phase'], 'rolled_back')

    def test_unclean_stop_does_not_overwrite_running_new_app(self):
        u = self.updater(); tx = u.prepare(self.source, self.target)
        inode = self.target.stat().st_ino
        def stop(): raise u.UpdateError('busy')
        with self.assertRaises(u.UpdateError):
            u.activate(tx, self.target, launch=lambda: None, healthy=lambda: False, stop=stop)
        self.assertNotEqual(self.target.stat().st_ino, inode)
        self.assertEqual((tx/'candidate.app').stat().st_ino, inode)

    def test_recovery_recognizes_swap_before_phase_commit(self):
        u = self.updater(); tx = u.prepare(self.source, self.target)
        inode = self.target.stat().st_ino
        u.exchange(self.target, tx/'candidate.app')
        u.recover(tx, self.target, stop=lambda: None, launch=lambda: None)
        self.assertEqual(self.target.stat().st_ino, inode)

    def test_tampered_candidate_rejected_before_publication(self):
        u = self.updater()
        (self.source/'Contents/Resources/sync-runtime/tools/reliable_sync.py').write_text('tampered')
        with self.assertRaises(u.UpdateError): u.prepare(self.source, self.target)

    def test_symlink_in_package_rejected(self):
        u = self.updater()
        (self.source/'Contents/Resources/alias').symlink_to('/tmp')
        with self.assertRaises(u.UpdateError): u.prepare(self.source, self.target)

    def test_another_updater_is_excluded(self):
        u = self.updater()
        with u.update_lock(self.target):
            with self.assertRaises(u.UpdateError):
                with u.update_lock(self.target): pass

    def test_busy_publication_does_not_swap_and_restores_old_launch(self):
        from contextlib import contextmanager
        u = self.updater(); tx = u.prepare(self.source, self.target)
        inode = self.target.stat().st_ino; events=[]
        @contextmanager
        def busy():
            raise u.UpdateError('busy')
            yield
        with self.assertRaises(u.UpdateError):
            u.activate(tx, self.target, launch=lambda:events.append('launch'), healthy=lambda:True,
                       stop=lambda:None, ownership=busy)
        self.assertEqual(self.target.stat().st_ino, inode)
        self.assertEqual(events, ['launch'])

    def test_package_signed_again_with_bad_manifest_is_rejected(self):
        u = self.updater()
        (self.source/'Contents/Resources/sync-runtime/tools/reliable_sync.py').write_text('tampered')
        u.sign(self.source)
        with self.assertRaisesRegex(u.UpdateError, 'runtime_digest_mismatch'):
            u.prepare(self.source, self.target)

    def test_source_changed_during_copy_is_not_resigned_as_valid(self):
        from unittest.mock import patch
        import shutil
        u = self.updater(); original = shutil.copytree
        def copying(source, destination, *args, **kwargs):
            if Path(source) == self.source:
                (self.source/'Contents/Resources/sync-runtime/tools/reliable_sync.py').write_text('changed during copy')
            return original(source, destination, *args, **kwargs)
        with patch.object(u.shutil, 'copytree', side_effect=copying):
            with self.assertRaises(u.UpdateError): u.prepare(self.source, self.target)

    def test_recovery_before_swap_restarts_stopped_original(self):
        u = self.updater(); tx = u.prepare(self.source, self.target); calls=[]
        u.recover(tx, self.target, stop=lambda: self.fail('must not stop original'),
                  launch=lambda: calls.append('launch'))
        self.assertEqual(calls, ['launch'])

    def test_recovery_retries_launch_after_rollback(self):
        u = self.updater(); tx = u.prepare(self.source, self.target); calls=[]
        u.exchange(self.target, tx/'candidate.app')
        def fail_launch(): raise u.UpdateError('launch_failed')
        with self.assertRaises(u.UpdateError):
            u.recover(tx, self.target, stop=lambda:None, launch=fail_launch)
        u.recover(tx, self.target, stop=lambda:self.fail('original already restored'),
                  launch=lambda:calls.append('launch'))
        self.assertEqual(calls, ['launch'])

    def test_legacy_without_worker_can_quit_with_pending_events(self):
        import plistlib
        from unittest.mock import patch
        u = self.updater()
        path = self.target/'Contents/Info.plist'
        value=plistlib.loads(path.read_bytes());value.pop('CodexInkUpdateProtocol');path.write_bytes(plistlib.dumps(value))
        state=self.root/'state';state.mkdir()
        (state/'sync-journal.json').write_text(json.dumps({'requested_revision':2,'acknowledged_revision':1}))
        app=object.__new__(u.LocalApp);app.target=self.target;app.state=state;app.pids=lambda:[123]
        with patch.object(u.time, 'monotonic', side_effect=[0,1,200]):
            with app.legacy_idle():
                with self.assertRaises(u.UpdateError):
                    with u.file_lock(state/'reliable-sender.lock'): pass
