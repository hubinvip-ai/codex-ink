import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from tools.companion_bridge import BridgeSession, BridgeWorker
from tools.reliable_sync import frame_bytes
from tools.sync_journal import SyncJournal


class ResumePolicyTests(unittest.TestCase):
    def exercise(self, *, refresh=False, recovery=False, changed=False, now=110):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); journal = SyncJournal(root)
        old = Image.new('RGB', (400,300), 'white')
        digest, _ = frame_bytes(old)
        journal.request(100, force=True); journal.acknowledge(1,digest,100,sent=True)
        if recovery: journal.fail('bluetooth_off',101,permanent=True)
        session = BridgeSession(None,io.BytesIO(),'test-session')
        sent=[];session.send=lambda path,owner_fd=None:sent.append(True)
        session.receive({'version':1,'type':'resume','session_id':'test-session','retry':recovery})
        if refresh:session.receive({'version':1,'type':'refresh','session_id':'test-session'})
        original=session.emit
        def emit(kind,**fields):
            original(kind,**fields)
            if kind=='status':session.close()
        session.emit=emit
        image=Image.new('RGB',(400,300),'black') if changed else old
        with patch('tools.companion_bridge.time.time',return_value=now):
            bridge=BridgeWorker(journal,root/'status.json',lambda state:image,session)
            bridge.worker.clock=lambda:now
            bridge.run()
        return journal,bridge,sent

    def test_automatic_resume_does_not_force_or_bypass_cooldown(self):
        journal,bridge,sent=self.exercise(changed=True)
        self.assertEqual(sent,[])
        self.assertEqual(journal.read()['force_revision'],1)
        bridge.worker.clock=lambda:279
        self.assertEqual(bridge.worker.tick()['status'],'pending')
        bridge.worker.clock=lambda:280
        self.assertEqual(bridge.worker.tick()['status'],'sent')
        self.assertEqual(len(sent),1)

    def test_same_frame_is_deduplicated_after_resume_merge_window(self):
        journal,bridge,sent=self.exercise(now=400)
        self.assertEqual(sent,[])
        bridge.worker.clock=lambda:429
        self.assertEqual(bridge.worker.tick()['status'],'pending')
        bridge.worker.clock=lambda:430
        self.assertEqual(bridge.worker.tick()['status'],'unchanged')
        self.assertEqual(sent,[])
        self.assertEqual(journal.read()['last_sent_at'],100)

    def test_explicit_refresh_still_forces_identical_frame(self):
        journal,bridge,sent=self.exercise(refresh=True)
        self.assertEqual(len(sent),1)
        self.assertEqual(journal.read()['last_sent_at'],110)

    def test_explicit_error_retry_still_recovers(self):
        journal,bridge,sent=self.exercise(recovery=True)
        self.assertEqual(len(sent),1)
        self.assertEqual(journal.read()['failure_count'],0)
