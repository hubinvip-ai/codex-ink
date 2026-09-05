"""Single-consumer latest-state synchronization, independent of the live install."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
from pathlib import Path

from tools.codex_status_core import empty_hook_state
from tools.sync_journal import ERROR_CODES, SyncJournal, atomic_write


class SyncFailure(Exception):
    def __init__(self, code, *, permanent=False):
        if code not in ERROR_CODES:
            raise ValueError('unsupported sync error')
        self.code = code
        self.permanent = permanent
        super().__init__(code)


class SyncDeferred(Exception):
    """The owner intentionally paused before transmission; keep the request pending."""


def read_hook_state(path: Path):
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return empty_hook_state()
    except (OSError, ValueError, UnicodeError) as error:
        raise SyncFailure('source_state_invalid', permanent=True) from error
    if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1 or not isinstance(state.get('threads'), dict):
        raise SyncFailure('source_state_invalid', permanent=True)
    for identifier, record in state['threads'].items():
        if not isinstance(record, dict) or record.get('session_id') != identifier or not isinstance(record.get('status'), str) or record['status'] not in {'queued', 'running', 'waiting', 'ended', 'failed'}:
            raise SyncFailure('source_state_invalid', permanent=True)
        if type(record.get('updated_at')) is not int or record['updated_at'] < 0:
            raise SyncFailure('source_state_invalid', permanent=True)
    return state


def state_digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def frame_bytes(image):
    if image.size != (400, 300) or image.mode != 'RGB':
        raise SyncFailure('invalid_frame', permanent=True)
    colors = image.getcolors(maxcolors=3)
    if colors is None or not {color for _, color in colors} <= {(255,255,255), (0,0,0), (198,40,40)}:
        raise SyncFailure('invalid_frame', permanent=True)
    digest = hashlib.sha256(image.tobytes()).hexdigest()
    stream = io.BytesIO()
    image.save(stream, format='PNG', optimize=False)
    return digest, stream.getvalue()


class ReliableWorker:
    def __init__(self, journal: SyncJournal, state_file: Path, render, send, *, clock=time.time):
        self.journal = journal
        self.state_file = Path(state_file)
        self.render = render
        self.send = send
        self.clock = clock
        self.next_poll = clock() + 60
        self.preview = journal.directory / 'preview.png'

    def tick(self, *, no_push=False):
        if no_push:
            return self._tick_owned(no_push=True)
        with self.journal.sender_lock() as owner:
            if not owner:
                return {'status':'busy'}
            return self._tick_owned(no_push=no_push, owner_fd=owner)

    def _render(self, state):
        try:
            return frame_bytes(self.render(state))
        except (SyncFailure, SyncDeferred):
            raise
        except Exception as error:
            raise SyncFailure('render_failed') from error

    def _record_failure(self, failure):
        state = self.journal.read()
        if state['requested_revision'] == state['acknowledged_revision']:
            self.journal.request(self.clock())
        # A permanent source error is inspected but not retried/written at 1 Hz.
        if not (failure.permanent and state['last_error_code'] == failure.code and state['failure_count'] and state['retry_at'] is None):
            self.journal.fail(failure.code, self.clock(), permanent=failure.permanent)
        return {'status':'blocked' if failure.permanent else 'retrying', 'error_code':failure.code}

    def _tick_owned(self, *, no_push=False, owner_fd=None):
        try:
            source = read_hook_state(self.state_file)
            if no_push:
                _, content = self._render(source)
                atomic_write(self.preview, content)
                return {'status':'preview'}
            now = self.clock()
            self.journal.observe(state_digest(source), now)
            if now >= self.next_poll:
                self.journal.request(now)
                self.next_poll = now + 60
            ticket = self.journal.begin(now)
            if ticket is None:
                state = self.journal.read()
                if state['failure_count']:
                    return {'status':'blocked' if state['retry_at'] is None else 'retrying', 'error_code':state['last_error_code']}
                return {'status':'idle' if state['requested_revision'] == state['acknowledged_revision'] else 'pending'}
            # The revision is captured BEFORE reading render input. Reading it in
            # the opposite order can acknowledge a newly queued revision with old data.
            source = read_hook_state(self.state_file)
            digest, content = self._render(source)
            atomic_write(self.preview, content)
            changed = digest != ticket['last_sent_frame_hash']
            forced = ticket['force_revision'] > ticket['acknowledged_revision']
            if changed or forced:
                frames = self.journal.directory / 'frames'
                frames.mkdir(mode=0o700, exist_ok=True)
                fd, filename = tempfile.mkstemp(prefix='frame-', suffix='.png', dir=frames)
                path = Path(filename)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        self.send(path, owner_fd)
                    except (SyncFailure, SyncDeferred):
                        raise
                    except Exception as error:
                        raise SyncFailure('send_failed') from error
                finally:
                    path.unlink(missing_ok=True)
            revision = ticket['requested_revision']
            self.journal.acknowledge(revision, digest, self.clock(), sent=changed or forced)
            return {'status':'sent' if changed or forced else 'unchanged', 'revision':revision}
        except SyncDeferred:
            return {'status':'paused'}
        except SyncFailure as failure:
            if no_push:
                return {'status':'blocked', 'error_code':failure.code}
            return self._record_failure(failure)
        except OSError:
            # If the journal itself is unwritable this raises, preserving pending work.
            if no_push:
                return {'status':'blocked', 'error_code':'storage_error'}
            return self._record_failure(SyncFailure('storage_error', permanent=True))

    def run(self, stopping, report):
        """Retain the sender lock until shutdown; do not create a lost-wakeup gap."""
        with self.journal.sender_lock() as owner:
            if not owner:
                report({'status':'busy'})
                return
            state = self.journal.read()
            if state['failure_count'] and state['retry_at'] is None:
                # One recovery probe per explicit consumer start, never per tick.
                self.journal.retry(self.clock())
            previous = None
            while not stopping.is_set():
                result = self._tick_owned(owner_fd=owner)
                if result != previous:
                    report(result)
                    previous = result
                if result['status'] in {'sent', 'unchanged'}:
                    continue
                stopping.wait(1)
