import io
import json
import os
import queue
import select
import tempfile
import threading
import unittest
from pathlib import Path
from PIL import Image
from tools.reliable_sync import SyncFailure

try:
    from tools.companion_bridge import BridgeSession, BridgeWorker, MAX_LINE
except ImportError:
    BridgeSession = None

SESSION = '00000000-0000-4000-8000-000000000001'


class Sink(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.messages = queue.Queue()

    def write(self, data):
        result = super().write(data)
        self.messages.put(json.loads(data))
        return result


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(BridgeSession, 'private bridge is not implemented')
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'frame.png'
        Image.new('RGB',(400,300),'white').save(self.path)
        self.output=Sink()
        self.session=BridgeSession(None,self.output,SESSION,timeout=1)
        self.session.start()
        self.output.messages.get(timeout=1)
        self.session.receive(self.message('resume'))
        self.addCleanup(self.session.close)

    def message(self, kind, **values):
        return dict(version=1,type=kind,session_id=SESSION,**values)

    def sender(self):
        outcomes=[]
        def send():
            try:
                self.session.send(self.path, None)
                outcomes.append('ok')
            except Exception as error:
                outcomes.append(error)
        thread=threading.Thread(target=send)
        thread.start()
        self.addCleanup(lambda:thread.join(timeout=2))
        request=self.output.messages.get(timeout=1)
        self.assertEqual(request['type'],'send')
        return thread,request,outcomes

    def test_matching_ack_requires_complete_write_and_disconnect(self):
        thread,request,outcomes=self.sender()
        self.session.receive(self.message('send_result',request_id=request['request_id'],ok=True,packets=129,bytes=30511,disconnected=True))
        thread.join(timeout=1)
        self.assertEqual(outcomes,['ok'])

    def test_wrong_session_and_request_do_not_ack_current_send(self):
        thread,request,outcomes=self.sender()
        reply=self.message('send_result',request_id=request['request_id'],ok=True,packets=129,bytes=30511,disconnected=True)
        self.session.receive({**reply,'session_id':'different'})
        self.session.receive({**reply,'request_id':'different'})
        self.assertEqual(outcomes,[])
        self.session.receive(reply)
        thread.join(timeout=1)
        self.assertEqual(outcomes,['ok'])

    def test_partial_success_is_protocol_failure_not_delivery(self):
        thread,request,outcomes=self.sender()
        self.session.receive(self.message('send_result',request_id=request['request_id'],ok=True,packets=128,bytes=30511,disconnected=True))
        thread.join(timeout=1)
        self.assertIsInstance(outcomes[0], SyncFailure)
        self.assertTrue(self.session.stopping.is_set())

    def test_duplicate_reply_does_not_override_first_reply(self):
        thread,request,outcomes=self.sender()
        reply=self.message('send_result',request_id=request['request_id'],ok=True,packets=129,bytes=30511,disconnected=True)
        self.session.receive(reply)
        self.session.receive({**reply,'ok':False,'error_code':'send_failed'})
        thread.join(timeout=1)
        self.assertEqual(outcomes,['ok'])

    def test_timeout_ends_session_before_possible_retry(self):
        self.session.timeout=.03
        thread,request,outcomes=self.sender()
        thread.join(timeout=1)
        self.assertEqual(outcomes[0].code,'send_timeout')
        self.assertTrue(self.session.stopping.is_set())

    def test_eof_wakes_pending_send(self):
        thread,request,outcomes=self.sender()
        self.session.close()
        thread.join(timeout=1)
        self.assertIsInstance(outcomes[0],SyncFailure)

    def test_pause_before_send_emits_no_frame_and_keeps_pending(self):
        from tools.sync_journal import SyncJournal
        from tools.reliable_sync import ReliableWorker
        journal=SyncJournal(Path(self.temp.name))
        journal.request(100,force=True)
        def render(state):
            self.session.receive(self.message('pause'))
            return Image.new('RGB',(400,300),'white')
        worker=ReliableWorker(journal,Path(self.temp.name)/'status.json',render,self.session.send,clock=lambda:100)
        self.assertEqual(worker.tick()['status'],'paused')
        self.assertEqual(journal.read()['acknowledged_revision'],0)
        self.assertEqual(journal.read()['failure_count'],0)
        self.assertTrue(self.output.messages.empty())

    def test_pause_allows_already_sent_frame_to_finish(self):
        thread,request,outcomes=self.sender()
        self.session.receive(self.message('pause'))
        self.session.receive(self.message('send_result',request_id=request['request_id'],ok=True,packets=129,bytes=30511,disconnected=True))
        thread.join(timeout=1)
        self.assertEqual(outcomes,['ok'])
        self.assertTrue(self.session.paused.is_set())

    def test_fragmented_pipe_message_and_idle_eof(self):
        read_fd,write_fd=os.pipe()
        source=os.fdopen(read_fd,'rb',buffering=0)
        session=BridgeSession(source,Sink(),SESSION)
        session.start()
        try:
            content=json.dumps(self.message('resume')).encode()+b'\n'
            os.write(write_fd,content[:9]); os.write(write_fd,content[9:])
            os.close(write_fd)
            self.assertTrue(session.stopping.wait(1))
            self.assertFalse(session.paused.is_set())
        finally:
            source.close();session.close()

    def test_invalid_and_oversized_lines_stop_without_logging_content(self):
        for content in (b'{"secret":1}\n', b'x'*(1024*1024+1), b'\xff\n'):
            source=io.BytesIO(content)
            session=BridgeSession(source,Sink(),SESSION)
            session.start()
            self.assertTrue(session.stopping.wait(1))
            self.assertEqual(session.error_code,'configuration_error')
            session.close()

    def test_bridge_worker_starts_paused_and_eof_releases_ownership(self):
        from tools.sync_journal import SyncJournal
        root=Path(self.temp.name)
        session=BridgeSession(None,Sink(),SESSION)
        worker=BridgeWorker(SyncJournal(root),root/'status.json',lambda state:Image.new('RGB',(400,300),'white'),session)
        thread=threading.Thread(target=worker.run)
        thread.start()
        try:
            message=session.output.messages.get(timeout=1)
            self.assertEqual(message['type'],'ready')
            session.close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(worker.journal.read()['last_sent_at'])
            with worker.journal.sender_lock() as owner:
                self.assertTrue(owner)
        finally:
            session.close();thread.join(timeout=2)

    def test_close_interrupts_backpressured_output(self):
        read_fd,write_fd=os.pipe()
        output=os.fdopen(write_fd,'wb',buffering=0)
        session=BridgeSession(None,output,SESSION)
        session.start();os.read(read_fd,4096)
        session.receive(self.message('resume'))
        self.path.write_bytes(b'x'*600000)
        errors=[]
        def send():
            try:session.send(self.path)
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=send);thread.start()
        try:
            self.assertTrue(select.select([read_fd],[],[],2)[0])
            os.read(read_fd,4096)
            session.close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(errors[0],SyncFailure)
        finally:
            session.close();output.close();os.close(read_fd);thread.join(timeout=2)

    def test_automatic_resume_preserves_permanent_error_until_explicit_retry(self):
        from tools.sync_journal import SyncJournal
        root=Path(self.temp.name);journal=SyncJournal(root)
        journal.request(100);journal.fail('permission_denied',101,permanent=True)
        output=Sink();session=BridgeSession(None,output,SESSION)
        worker=BridgeWorker(journal,root/'status.json',lambda state:Image.new('RGB',(400,300),'white'),session)
        thread=threading.Thread(target=worker.run);thread.start()
        try:
            output.messages.get(timeout=1)
            session.receive(self.message('resume'))
            while True:
                message=output.messages.get(timeout=2)
                if message.get('payload',{}).get('status')=='blocked':break
            self.assertEqual(journal.read()['failure_count'],1)
            session.receive(self.message('resume',retry=True))
            while True:
                request=output.messages.get(timeout=2)
                if request['type']=='send':break
            session.receive(self.message('send_result',request_id=request['request_id'],ok=True,packets=129,bytes=30511,disconnected=True))
            while True:
                message=output.messages.get(timeout=2)
                if message.get('payload',{}).get('status')=='sent':break
            self.assertEqual(journal.read()['failure_count'],0)
        finally:
            session.close();thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
