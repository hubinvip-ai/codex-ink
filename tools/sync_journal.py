"""Durable, content-free refresh revisions. Separate from legacy hook state v1."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


ERROR_CODES = frozenset({
    'source_unavailable', 'source_state_invalid', 'render_failed', 'invalid_frame',
    'sender_missing', 'device_not_configured', 'bluetooth_off', 'permission_denied',
    'send_failed', 'send_timeout', 'storage_error', 'configuration_error',
})


class JournalError(ValueError):
    """Invalid journal; caller must preserve it and report a local repair need."""


def atomic_write(path: Path, content: bytes) -> None:
    """Publish one complete file, never sharing a temporary name across writers."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _initial():
    return {
        'version': 1, 'requested_revision': 0, 'acknowledged_revision': 0,
        'force_revision': 0, 'pending_since': None, 'last_sent_frame_hash': None,
        'last_attempt_at': None, 'last_sent_at': None, 'failure_count': 0,
        'retry_at': None, 'last_error_code': None, 'observed_state_hash': None,
    }


def _valid_time(value):
    return type(value) in (int, float) and 0 <= value <= 10 ** 12 and math.isfinite(value)


def _valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _validate(state):
    if not isinstance(state, dict) or set(state) != set(_initial()):
        raise JournalError('invalid journal fields')
    if type(state['version']) is not int or state['version'] != 1:
        raise JournalError('unsupported journal version')
    for key in ('requested_revision', 'acknowledged_revision', 'force_revision', 'failure_count'):
        if type(state[key]) is not int or state[key] < 0:
            raise JournalError('invalid journal counter')
    requested, acknowledged = state['requested_revision'], state['acknowledged_revision']
    if acknowledged > requested or state['force_revision'] > requested:
        raise JournalError('invalid journal revision order')
    for key in ('pending_since', 'last_attempt_at', 'last_sent_at', 'retry_at'):
        if state[key] is not None and not _valid_time(state[key]):
            raise JournalError('invalid journal timestamp')
    if (requested > acknowledged) != (state['pending_since'] is not None):
        raise JournalError('invalid pending batch')
    for key in ('last_sent_frame_hash', 'observed_state_hash'):
        if state[key] is not None and not _valid_hash(state[key]):
            raise JournalError('invalid journal digest')
    if state['last_error_code'] is not None and (not isinstance(state['last_error_code'], str) or state['last_error_code'] not in ERROR_CODES):
        raise JournalError('unknown error code')
    if bool(state['failure_count']) != (state['last_error_code'] is not None):
        raise JournalError('invalid error state')
    if not state['failure_count'] and state['retry_at'] is not None:
        raise JournalError('retry without failure')
    if (state['last_sent_at'] is None) != (state['last_sent_frame_hash'] is None):
        raise JournalError('incomplete delivery record')


class SyncJournal:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / 'sync-journal.json'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def _lock(self, filename, *, blocking=True):
        fd = os.open(self.directory / filename, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a+') as stream:
            try:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), flags)
            except BlockingIOError:
                yield False
                return
            # Close, rather than explicitly LOCK_UN: a transport subprocess may
            # still hold an inherited copy after its parent dies. The last close
            # releases ownership, never the first process leaving this context.
            yield stream.fileno()

    def sender_lock(self):
        return self._lock('reliable-sender.lock', blocking=False)

    def _read_unlocked(self):
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            return _initial()
        except (json.JSONDecodeError, UnicodeError) as error:
            raise JournalError('unreadable journal') from error
        _validate(state)
        return state

    def read(self):
        with self._lock('sync-journal.lock'):
            return self._read_unlocked()

    def _change(self, operation):
        with self._lock('sync-journal.lock'):
            state = self._read_unlocked()
            before = dict(state)
            result = operation(state)
            _validate(state)
            if state != before:
                atomic_write(self.path, (json.dumps(state, sort_keys=True) + '\n').encode())
            return result

    @staticmethod
    def _request(state, now, force):
        if not _valid_time(now):
            raise JournalError('invalid request time')
        if state['requested_revision'] == state['acknowledged_revision']:
            state['pending_since'] = now
        state['requested_revision'] += 1
        if force:
            state['force_revision'] = state['requested_revision']
        return state['requested_revision']

    def request(self, now, force=False):
        return self._change(lambda state: self._request(state, now, force))

    def observe(self, state_hash, now):
        if not _valid_hash(state_hash):
            raise JournalError('invalid source digest')
        def operation(state):
            if state['observed_state_hash'] != state_hash:
                self._request(state, now, False)
                state['observed_state_hash'] = state_hash
            return state['requested_revision']
        return self._change(operation)

    def begin(self, now):
        if not _valid_time(now):
            raise JournalError('invalid attempt time')
        def operation(state):
            if state['acknowledged_revision'] == state['requested_revision']:
                return None
            if state['failure_count']:
                if state['retry_at'] is None or now < state['retry_at']:
                    return None
            if state['force_revision'] <= state['acknowledged_revision']:
                ready_at = state['pending_since'] + 30
                if state['last_sent_at'] is not None:
                    ready_at = max(ready_at, state['last_sent_at'] + 180)
                if now < ready_at:
                    return None
            state['last_attempt_at'] = now
            return dict(state)
        return self._change(operation)

    def acknowledge(self, revision, frame_hash, now, *, sent):
        if not _valid_hash(frame_hash) or not _valid_time(now):
            raise JournalError('invalid acknowledgement')
        def operation(state):
            if type(revision) is not int or not state['acknowledged_revision'] < revision <= state['requested_revision']:
                raise JournalError('invalid acknowledged revision')
            if not sent and frame_hash != state['last_sent_frame_hash']:
                raise JournalError('cannot deduplicate an unsent frame')
            state['acknowledged_revision'] = revision
            if sent:
                state['last_sent_frame_hash'] = frame_hash
                state['last_sent_at'] = now
            if revision == state['requested_revision']:
                state['pending_since'] = None
            state.update(failure_count=0, retry_at=None, last_error_code=None)
        self._change(operation)

    def fail(self, code, now, *, permanent=False):
        if code not in ERROR_CODES or not _valid_time(now):
            raise JournalError('invalid failure')
        def operation(state):
            state['failure_count'] += 1
            delay = (5, 15, 30, 60)[min(state['failure_count'] - 1, 3)]
            state['retry_at'] = None if permanent else now + delay
            state['last_error_code'] = code
        self._change(operation)

    def retry(self, now):
        def operation(state):
            revision = self._request(state, now, True)
            state.update(failure_count=0, retry_at=None, last_error_code=None)
            return revision
        return self._change(operation)
