#!/usr/bin/env python3
"""Private app-owned JSONL transport. No socket listener and no BLE in Python."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import select
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from tools.reliable_sync import ReliableWorker, SyncFailure, SyncDeferred, frame_bytes
from tools.sync_journal import ERROR_CODES, SyncJournal, JournalError

MAX_LINE = 1024 * 1024


def parse_message(line):
    def reject_constant(value):
        raise ValueError('nonstandard JSON constant')
    def unique_pairs(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    return json.loads(line.decode('utf-8'),parse_constant=reject_constant,object_pairs_hook=unique_pairs)


class BridgeSession:
    def __init__(self, source, output, session_id, *, timeout=130):
        self.source, self.output, self.session_id = source, output, session_id
        self.timeout = timeout
        self.stopping, self.paused, self.wake = threading.Event(), threading.Event(), threading.Event()
        self.paused.set()
        self.error_code = None
        self.condition = threading.Condition(threading.RLock())
        self.write_lock = threading.Lock()
        self.pending = self.reply = None
        self.resume_requested = self.refresh_requested = self.recovery_requested = False
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        self.emit('ready')
        if self.source is not None:
            threading.Thread(target=self._read_loop, daemon=True).start()

    def close(self, error_code=None):
        self.error_code = self.error_code or error_code
        self.stopping.set()
        self.wake.set()
        with self.condition:
            self.condition.notify_all()

    def emit(self, kind, **fields):
        content=(json.dumps(dict(version=1,type=kind,session_id=self.session_id,**fields),separators=(',',':'),allow_nan=False)+'\n').encode()
        if len(content)>MAX_LINE:
            self.close('configuration_error')
            raise SyncFailure('configuration_error',permanent=True)
        with self.write_lock:
            try:
                try:
                    descriptor=self.output.fileno()
                except (AttributeError, io.UnsupportedOperation):
                    self.output.write(content);self.output.flush()
                    return
                os.set_blocking(descriptor,False)
                end=time.monotonic()+5
                offset=0
                while offset<len(content):
                    if self.stopping.is_set() or time.monotonic()>=end:
                        raise BrokenPipeError()
                    if select.select([], [descriptor], [], .1)[1]:
                        try:
                            offset+=os.write(descriptor,content[offset:])
                        except BlockingIOError:
                            pass
            except OSError as error:
                self.close('send_failed')
                raise SyncFailure('send_failed') from error

    def _read_loop(self):
        buffer=b''
        try:
            try:
                descriptor=self.source.fileno()
            except (AttributeError, io.UnsupportedOperation):
                descriptor=None
            while not self.stopping.is_set():
                chunk=os.read(descriptor,65536) if descriptor is not None else self.source.read(65536)
                if not chunk:
                    self.close('configuration_error' if buffer else None)
                    return
                buffer+=chunk
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1)
                    if len(line)+1>MAX_LINE:
                        self.close('configuration_error');return
                    self.receive(parse_message(line))
                    if self.stopping.is_set():
                        return
                if len(buffer)>=MAX_LINE:
                    self.close('configuration_error');return
        except (OSError, ValueError, UnicodeError, RecursionError):
            self.close('configuration_error')

    def receive(self, message):
        if not isinstance(message,dict) or type(message.get('version')) is not int or message['version']!=1 or not isinstance(message.get('type'),str) or not isinstance(message.get('session_id'),str):
            self.close('configuration_error');return
        if message['session_id']!=self.session_id:
            return
        kind=message['type']
        with self.condition:
            if kind=='stop':
                self.close();return
            if kind in ('pause','resume','refresh'):
                if kind=='pause': self.paused.set()
                elif kind=='resume':
                    retry=message.get('retry',False)
                    if type(retry) is not bool:
                        self.close('configuration_error');return
                    self.paused.clear();self.resume_requested=True
                    self.recovery_requested=self.recovery_requested or retry
                else: self.refresh_requested=True
                self.wake.set();return
            if kind!='send_result':
                self.close('configuration_error');return
            if self.pending is None or message.get('request_id')!=self.pending or self.reply is not None:
                return
            ok=message.get('ok')
            if type(ok) is not bool:
                self.close('configuration_error');return
            if ok and not (type(message.get('packets')) is int and message['packets']==129 and type(message.get('bytes')) is int and message['bytes']==30511 and message.get('disconnected') is True):
                self.close('configuration_error');return
            code=message.get('error_code')
            if not ok and (not isinstance(code,str) or code not in ERROR_CODES):
                self.close('configuration_error');return
            self.reply=dict(message)
            self.condition.notify_all()

    def commands(self):
        with self.condition:
            result=self.resume_requested,self.refresh_requested,self.recovery_requested
            self.resume_requested=self.refresh_requested=self.recovery_requested=False
            return result

    def send(self,path,owner_fd=None):
        content=Path(path).read_bytes()
        if len(content)>MAX_LINE//4*3-1024:
            raise SyncFailure('invalid_frame',permanent=True)
        with self.condition:
            if self.stopping.is_set(): raise SyncFailure('send_failed')
            if self.paused.is_set(): raise SyncDeferred()
            if self.pending is not None: raise SyncFailure('send_failed')
            self.pending=str(uuid.uuid4())
            self.reply=None
            try:
                self.emit('send',request_id=self.pending,png_base64=base64.b64encode(content).decode(),png_sha256=hashlib.sha256(content).hexdigest())
                end=time.monotonic()+self.timeout
                while self.reply is None and not self.stopping.is_set():
                    remaining=end-time.monotonic()
                    if remaining<=0:
                        self.close('send_timeout');raise SyncFailure('send_timeout')
                    self.condition.wait(remaining)
                if self.stopping.is_set():
                    raise SyncFailure(self.error_code or 'send_failed')
                if not self.reply['ok']:
                    code=self.reply['error_code']
                    raise SyncFailure(code,permanent=code in {'permission_denied','bluetooth_off','configuration_error','device_not_configured','invalid_frame'})
            finally:
                self.pending=self.reply=None


class BridgeWorker:
    def __init__(self,journal,state_file,render,session):
        self.journal,self.session=journal,session
        self.data_read_at=None
        def observed_render(state):
            image=render(state)
            frame_bytes(image)
            self.data_read_at=time.time()
            return image
        self.worker=ReliableWorker(journal,state_file,observed_render,session.send)

    def run(self):
        self.session.start()
        try:
            with self.journal.sender_lock() as owner:
                if not owner:
                    self.session.emit('status',payload={'status':'busy'});return
                previous=None
                while not self.session.stopping.is_set():
                    resume,refresh,recovery=self.session.commands()
                    if recovery: self.journal.retry(time.time())
                    elif refresh: self.journal.request(time.time(),force=True)
                    elif resume: self.journal.request(time.time())
                    result={'status':'paused'} if self.session.paused.is_set() else self.worker._tick_owned(owner_fd=owner)
                    if self.data_read_at is not None:
                        result={**result,'data_read_at':self.data_read_at}
                    if result!=previous and not self.session.stopping.is_set():
                        self.session.emit('status',payload=result)
                        previous=result
                    if result['status'] in {'sent','unchanged'}:
                        continue
                    self.session.wake.wait(5)
                    self.session.wake.clear()
        except (OSError,JournalError,SyncFailure):
            self.session.close('send_failed')
        finally:
            self.session.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--state-file',type=Path,required=True)
    parser.add_argument('--codex-binary',type=Path,required=True)
    parser.add_argument('--session-id',required=True)
    parser.add_argument('--language',choices=('zh-CN','en'),default='zh-CN')
    args=parser.parse_args()
    try:
        uuid.UUID(args.session_id)
        from tools.sync_adapters import CodexRenderer
        session=BridgeSession(sys.stdin.buffer,sys.stdout.buffer,args.session_id)
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,lambda number,frame:session.close())
        renderer=CodexRenderer(args.codex_binary,validated=True,cancel_event=session.stopping,language=args.language)
        BridgeWorker(SyncJournal(args.state_dir),args.state_file,renderer,session).run()
        return 0 if session.error_code is None else 1
    except (ValueError,OSError):
        print('{"error_code":"configuration_error"}',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
