#!/usr/bin/env python3
"""Opt-in reliable consumer. Never installs or modifies the existing service."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.codex_status_core import apply_hook_event
from tools.reliable_sync import ReliableWorker, SyncFailure, read_hook_state, state_digest
from tools.sync_journal import JournalError, SyncJournal, atomic_write


def emit(value, *, error=False):
    print(json.dumps(value, ensure_ascii=False), file=sys.stderr if error else sys.stdout, flush=True)


def ingest_event(journal, state_file, event, now):
    if not isinstance(event, dict) or not isinstance(event.get('session_id'), str) or not event['session_id'].strip():
        return
    if not isinstance(event.get('hook_event_name'), str) or event['hook_event_name'] not in {'SessionStart', 'UserPromptSubmit', 'PermissionRequest', 'Stop', 'SessionEnd'}:
        return
    # Reject malformed metadata before it can poison the existing v1 format.
    if any(key in event and not isinstance(event[key], str) for key in ('cwd', 'turn_id')):
        return
    state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_file.with_suffix(state_file.suffix + '.lock')
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), 'a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_hook_state(state_file)
        updated = apply_hook_event(state, event, int(now))
        atomic_write(state_file, (json.dumps(updated, ensure_ascii=False, sort_keys=True) + '\n').encode())
    # A crash here is repaired by the consumer's source-digest reconciliation.
    journal.observe(state_digest(updated), now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--state-file', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    request = commands.add_parser('request')
    request.add_argument('--force', action='store_true')
    commands.add_parser('status')
    commands.add_parser('retry')
    commands.add_parser('hook')
    preview = commands.add_parser('preview')
    preview.add_argument('--codex-binary', type=Path, required=True)
    preview.add_argument('--validated', action='store_true')
    work = commands.add_parser('work')
    work.add_argument('--codex-binary', type=Path, required=True)
    work.add_argument('--push-binary', type=Path, required=True)
    work.add_argument('--device', required=True)
    args = parser.parse_args(argv)
    try:
        journal = SyncJournal(args.state_dir)
        if args.command == 'hook':
            try:
                ingest_event(journal, args.state_file, json.load(sys.stdin), time.time())
            except (ValueError, OSError, SyncFailure) as error:
                emit({'error_code':error.code if isinstance(error, SyncFailure) else 'hook_input_or_storage_invalid'}, error=True)
            emit({})
        elif args.command == 'request':
            emit({'status':'queued', 'revision':journal.request(time.time(), force=args.force)})
        elif args.command == 'retry':
            emit({'status':'queued', 'revision':journal.retry(time.time())})
        elif args.command == 'status':
            emit(journal.read())
        else:
            from tools.sync_adapters import CLISender, CodexRenderer
            cancel_event=None
            if args.command=='preview' and args.validated:
                cancel_event=threading.Event()
                for sig in (signal.SIGTERM,signal.SIGINT):
                    signal.signal(sig,lambda number,frame:cancel_event.set())
            renderer = CodexRenderer(args.codex_binary,validated=getattr(args,'validated',False),cancel_event=cancel_event)
            sender = CLISender(args.push_binary, args.device) if args.command == 'work' else None
            worker = ReliableWorker(journal, args.state_file, renderer, sender)
            if args.command == 'preview':
                result = worker.tick(no_push=True)
                if args.validated and result['status']=='preview':
                    result['data_read_at']=time.time()
                emit(result)
                return 0 if result['status'] == 'preview' else 1
            stopping = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda signum, frame:stopping.set())
            worker.run(stopping, emit)
        return 0
    except (JournalError, OSError, SyncFailure) as error:
        code = 'journal_invalid' if isinstance(error, JournalError) else error.code if isinstance(error, SyncFailure) else 'storage_error'
        emit({'error_code':code}, error=True)
        if args.command == 'hook':
            emit({})
            return 0
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
