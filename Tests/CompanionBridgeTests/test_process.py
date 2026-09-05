from Tests.support import python_executable_header
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
SESSION='00000000-0000-4000-8000-000000000001'


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.binary=self.root/'codex'
        self.binary.write_text(python_executable_header()+'''import json, sys, time
responses={'initialize':{}, 'thread/list':{'data':[]}, 'account/rateLimits/read':{'rateLimits':{'primary':{'windowDurationMins':10080,'usedPercent':20,'resetsAt':int(time.time())+60000}}}, 'account/usage/read':{'dailyUsageBuckets':[]}, 'account/read':{'account':{'email':'test@example.test','planType':'pro'}}}
for line in sys.stdin:
    request=json.loads(line)
    if 'id' in request: print(json.dumps({'id':request['id'],'result':responses[request['method']]}),flush=True)
''')
        self.binary.chmod(0o700)

    def launch(self):
        process=subprocess.Popen([sys.executable,str(ROOT/'tools/companion_bridge.py'),'--state-dir',str(self.root/'state'),
            '--state-file',str(self.root/'source.json'),'--codex-binary',str(self.binary),'--session-id',SESSION],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        messages=queue.Queue()
        def read():
            for line in process.stdout:
                messages.put(json.loads(line))
        threading.Thread(target=read,daemon=True).start()
        def cleanup():
            if process.poll() is None: process.kill()
            process.wait(timeout=5)
            process.stdin.close();process.stdout.close();process.stderr.close()
        self.addCleanup(cleanup)
        return process,messages

    def command(self,p,kind,**values):
        p.stdin.write((json.dumps(dict(version=1,type=kind,session_id=SESSION,**values))+'\n').encode());p.stdin.flush()

    def next(self,messages,kind,timeout=5):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            message=messages.get(timeout=max(.1,end-time.monotonic()))
            if message['type']==kind:return message
        self.fail('message absent')

    def test_idle_parent_eof_exits_without_sending(self):
        p,m=self.launch()
        self.assertEqual(self.next(m,'ready')['session_id'],SESSION)
        p.stdin.close()
        self.assertEqual(p.wait(timeout=3),0,p.stderr.read().decode())
        journal=self.root/'state/sync-journal.json'
        self.assertFalse(journal.exists())

    def test_send_ack_updates_real_journal_then_stop(self):
        p,m=self.launch()
        self.next(m,'ready')
        self.command(p,'resume')
        message=self.next(m,'send')
        self.command(p,'send_result',request_id=message['request_id'],ok=True,packets=129,bytes=30511,disconnected=True)
        end=time.monotonic()+5
        while time.monotonic()<end:
            status=self.next(m,'status')
            if status['payload']['status']=='sent':break
        self.assertEqual(status['payload']['status'],'sent')
        self.assertIn('data_read_at',status['payload'])
        self.command(p,'stop')
        self.assertEqual(p.wait(timeout=3),0)
        data=json.loads((self.root/'state/sync-journal.json').read_text())
        self.assertEqual(data['requested_revision'],data['acknowledged_revision'])
        self.assertIsNotNone(data['last_sent_at'])

    def test_eof_cancels_blocked_render_child(self):
        marker=self.root/'child-started'
        self.binary.write_text(python_executable_header()+'import time\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text("started")\ntime.sleep(30)\n')
        p,m=self.launch();self.next(m,'ready');self.command(p,'resume')
        end=time.monotonic()+3
        while not marker.exists() and time.monotonic()<end:threading.Event().wait(.02)
        self.assertTrue(marker.exists())
        p.stdin.close()
        self.assertEqual(p.wait(timeout=3),0)
        data=json.loads((self.root/'state/sync-journal.json').read_text())
        self.assertEqual(data['acknowledged_revision'],0)

    def test_validated_preview_outputs_only_native_frame(self):
        p=subprocess.run([sys.executable,str(ROOT/'tools/codex_eink_reliable.py'),'--state-dir',str(self.root/'preview'),
            '--state-file',str(self.root/'source.json'),'preview','--codex-binary',str(self.binary),'--validated'],capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr)
        result=json.loads(p.stdout)
        self.assertEqual(result['status'],'preview')
        self.assertIn('data_read_at',result)
        self.assertFalse((self.root/'preview/sync-journal.json').exists())

    def test_cancel_validated_preview_stops_its_render_child(self):
        marker=self.root/'render-pid'
        self.binary.write_text(python_executable_header()+'import os,time,json\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text(json.dumps({"pid":os.getpid(),"group":os.getpgrp()}))\ntime.sleep(30)\n')
        p=subprocess.Popen([sys.executable,str(ROOT/'tools/codex_eink_reliable.py'),'--state-dir',str(self.root/'preview'),
            '--state-file',str(self.root/'source.json'),'preview','--codex-binary',str(self.binary),'--validated'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        identity=None
        try:
            end=time.monotonic()+3
            while not marker.exists() and time.monotonic()<end:threading.Event().wait(.02)
            self.assertTrue(marker.exists())
            identity=json.loads(marker.read_text())
            self.assertNotEqual(identity['group'],os.getpgrp())
            p.terminate();p.wait(timeout=3)
            end=time.monotonic()+2
            alive=True
            while time.monotonic()<end:
                result=subprocess.run(['/bin/ps','-p',str(identity['pid']),'-o','stat='],capture_output=True,text=True)
                if not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                    alive=False;break
                threading.Event().wait(.03)
            self.assertFalse(alive,'preview left its Codex source process alive')
        finally:
            if p.poll() is None:p.kill()
            p.wait(timeout=3);p.stdout.close();p.stderr.close()
            if identity:
                try:os.killpg(identity['group'],signal.SIGKILL)
                except ProcessLookupError:pass

    def test_invalid_json_exits_and_releases_lock_without_resuming(self):
        from tools.sync_journal import SyncJournal
        bad_messages=[b'['*2000+b'0'+b']'*2000+b'\n',
            ('{"version":1,"session_id":"'+SESSION+'","type":"resume","extra":NaN}\n').encode()]
        for bad in bad_messages:
            with self.subTest(payload=bad[:25]):
                p,m=self.launch();self.next(m,'ready')
                try:
                    p.stdin.write(bad);p.stdin.flush();p.stdin.close()
                    self.assertEqual(p.wait(timeout=3),1)
                    journal=SyncJournal(self.root/'state')
                    self.assertEqual(journal.read()['requested_revision'],0)
                    with journal.sender_lock() as owner:self.assertTrue(owner)
                finally:
                    if p.poll() is None:p.kill()
                    p.wait(timeout=3)


if __name__=='__main__':unittest.main()
