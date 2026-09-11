#!/usr/bin/env python3
"""Local development update: preserve trusted runtime, atomically swap, recover.

No downloads, hooks edits, trust writes, or forced termination. Run from a new
release outside the installed app. The hidden transaction directory is retained.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import plistlib
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

BUNDLE_ID = 'com.ben.codex-eink.companion.dev'
RESOURCE = Path('Contents/Resources')


class UpdateError(RuntimeError):
    pass


def run(args, timeout=20):
    try:
        result = subprocess.run(list(map(str, args)), capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError('command_failed: ' + str(args[0])) from error
    if result.returncode:
        raise UpdateError('command_failed: ' + str(args[0]))
    return result.stdout


def no_links(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise UpdateError('symlink_path')
    for item in path.rglob('*'):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise UpdateError('unsupported_package_entry')


def info(app):
    try:
        value = plistlib.loads((app/'Contents/Info.plist').read_bytes())
        if value.get('CFBundleIdentifier') != BUNDLE_ID or value.get('CFBundleExecutable') != 'eink-companion':
            raise UpdateError('wrong_product')
        return value
    except (OSError, ValueError) as error:
        raise UpdateError('invalid_app') from error


def verify_signature(app):
    run(['/usr/bin/codesign', '--verify', '--strict', app])


def sign(app):
    run(['/usr/bin/codesign', '--force', '--sign', '-', app])
    verify_signature(app)


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def verify_runtime(runtime):
    try:
        value = json.loads((runtime/'manifest.json').read_text())
        if value['version'] != 1 or not isinstance(value['files'], dict):
            raise ValueError()
        files = {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in runtime.rglob('*') if p.is_file() and p != runtime/'manifest.json'}
        if files != value['files']:
            raise ValueError()
    except (OSError, KeyError, ValueError) as error:
        raise UpdateError('runtime_digest_mismatch') from error


def write_record(tx, record):
    path = tx/'transaction.json'
    temporary = tx/'transaction.tmp'
    with temporary.open('w') as stream:
        json.dump(record, stream, sort_keys=True)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(tx, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise UpdateError('busy') from error
        yield
    finally: os.close(fd)


def update_lock(target):
    return file_lock(target.parent/'.codex-ink-update.lock')


def prepare(source, target):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve() or target in source.parents or source in target.parents:
        raise UpdateError('source_must_be_separate')
    for app in (source, target):
        no_links(app); info(app); verify_signature(app)
    marker = info(source).get('CodexInkUpdateProtocol')
    if type(marker) is not int or marker != 1:
        raise UpdateError('unsupported_update_protocol')
    verify_runtime(source/RESOURCE/'runtime')
    verify_runtime(source/RESOURCE/'sync-runtime')
    old_runtime = target/RESOURCE/'runtime'
    if not old_runtime.is_dir(): raise UpdateError('missing_frozen_runtime')
    original_hash = tree_digest(old_runtime)
    tx = Path(tempfile.mkdtemp(prefix='.codex-ink-update-', dir=target.parent))
    candidate = tx/'candidate.app'
    shutil.copytree(source, candidate)
    # Do not turn a concurrent source rebuild into a newly signed candidate.
    no_links(candidate); verify_signature(candidate)
    if info(candidate).get('CodexInkUpdateProtocol') != 1:
        raise UpdateError('unsupported_update_protocol')
    verify_runtime(candidate/RESOURCE/'runtime')
    verify_runtime(candidate/RESOURCE/'sync-runtime')
    shutil.rmtree(candidate/RESOURCE/'runtime')
    shutil.copytree(old_runtime, candidate/RESOURCE/'runtime')
    if tree_digest(candidate/RESOURCE/'runtime') != original_hash or tree_digest(old_runtime) != original_hash:
        raise UpdateError('frozen_runtime_changed')
    sign(candidate)
    verify_runtime(candidate/RESOURCE/'sync-runtime')
    for path in candidate.rglob('*'):
        if path.is_file():
            with path.open('rb') as stream: os.fsync(stream.fileno())
    write_record(tx, {'version': 1, 'phase': 'prepared', 'target': str(target),
                      'original_inode': target.stat().st_ino, 'candidate_inode': candidate.stat().st_ino,
                      'frozen_hash': original_hash})
    return tx


def exchange(first, second):
    """Atomic same-filesystem exchange keeps the trusted hook path continuously valid."""
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(first), os.fsencode(second), 2):  # RENAME_SWAP
        raise UpdateError('atomic_exchange_failed: ' + str(ctypes.get_errno()))
    for directory in {first.parent, second.parent}:
        fd = os.open(directory, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)


def record_for(tx, target):
    no_links(tx)
    record = json.loads((tx/'transaction.json').read_text())
    if record.get('version') != 1 or record.get('target') != str(target):
        raise UpdateError('invalid_transaction')
    pair = {target.stat().st_ino, (tx/'candidate.app').stat().st_ino}
    if pair != {record['original_inode'], record['candidate_inode']}:
        raise UpdateError('transaction_conflict')
    return record


def recover(tx, target, *, stop, launch, ownership=nullcontext):
    record = record_for(tx, target)
    if target.stat().st_ino == record['candidate_inode']:
        stop()  # Failure here retains both versions; never overwrite a live app.
        with ownership():
            record_for(tx, target)
            exchange(target, tx/'candidate.app')
            record['phase'] = 'rolled_back'; write_record(tx, record)
        launch()
    else:
        record['phase'] = 'rolled_back'; write_record(tx, record)
        launch()


def activate(tx, target, *, launch, healthy, stop, ownership=nullcontext):
    record = record_for(tx, target)
    if record['phase'] != 'prepared' or target.stat().st_ino != record['original_inode']:
        raise UpdateError('transaction_conflict')
    verify_signature(tx/'candidate.app')
    if tree_digest(target/RESOURCE/'runtime') != record['frozen_hash']:
        raise UpdateError('frozen_runtime_changed')
    try:
        with ownership():
            record_for(tx, target)
            exchange(target, tx/'candidate.app')
            record['phase'] = 'published'; write_record(tx, record)
        launch()
        if not healthy(): raise UpdateError('startup_check_failed')
        record['phase'] = 'complete'; write_record(tx, record)
    except Exception as error:
        recover(tx, target, stop=stop, launch=launch, ownership=ownership)
        raise UpdateError('update_failed_rolled_back') from error


def process_rows():
    output = run(['/bin/ps', '-axo', 'pid=,command=']).decode()
    rows = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2: rows.append((int(fields[0]), fields[1]))
    return rows


class LocalApp:
    def __init__(self, target, state, home):
        self.target, self.state, self.home = target, state, home
        self.settings = json.loads((state/'settings.json').read_text())
        self.python = self.settings['python_binary']
        self.owner = home/'Library/Application Support/CodexEInk/companion-app.lock'

    def pids(self):
        path = str(self.target/'Contents/MacOS/eink-companion')
        return [pid for pid, command in process_rows() if command == path or command.startswith(path+' ')]

    def inspect(self, *, timeout=25):
        value = json.loads(run([self.python, self.target/RESOURCE/'runtime/tools/companion_setup.py',
                                '--state-dir', self.state, 'inspect'], timeout=timeout))
        if not value.get('ok') or value.get('stage') != 'ready':
            raise UpdateError('existing_setup_not_ready')

    def launch(self):
        if self.pids(): return
        log = self.state/'update-startup.log'
        fd = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        run(['/usr/bin/open', '-n', '--env', 'CODEX_INK_UPDATE_DIAGNOSTICS=1',
             '--stderr', log, self.target, '--args', '--state-dir', self.state])

    @contextmanager
    def legacy_idle(self):
        # Old apps cancel sends on quit. Lock the idle journal so no new ticket
        # can begin between the settled observation and the quit request.
        if info(self.target).get('CodexInkUpdateProtocol') == 1 or not self.pids():
            yield; return
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            try:
                # A stopped/crashed consumer can leave queued events. They are
                # not an in-flight frame; retain them and exclude a new consumer.
                with file_lock(self.state/'reliable-sender.lock'):
                    yield; return
            except UpdateError as error:
                if str(error) != 'busy': raise
            try:
                with file_lock(self.state/'sync-journal.lock'):
                    journal = json.loads((self.state/'sync-journal.json').read_text())
                    if journal['requested_revision'] == journal['acknowledged_revision']:
                        yield; return
            except UpdateError as error:
                if str(error) != 'busy': raise
            time.sleep(.1)
        raise UpdateError('legacy_send_not_idle')

    def stop(self):
        with self.legacy_idle():
            if self.pids():
                # Pass the path as argv, never interpolate it into AppleScript.
                script = 'on run argv\ntell application (item 1 of argv) to quit\nend run'
                run(['/usr/bin/osascript', '-e', script, self.target], timeout=300)
            deadline = time.monotonic() + 20
            while self.pids() and time.monotonic() < deadline: time.sleep(.2)
            if self.pids(): raise UpdateError('application_still_running')
        with file_lock(self.owner), file_lock(self.state/'reliable-sender.lock'):
            pass

    @contextmanager
    def ownership(self):
        with file_lock(self.owner), file_lock(self.state/'reliable-sender.lock'):
            if self.pids(): raise UpdateError('application_still_running')
            yield

    def healthy(self):
        # A native inspect can take 10s, then recovery waits 15s before retrying.
        # 35s could roll back just as the second inspect completed. Allow bounded
        # recovery, while still requiring fresh readiness before committing.
        deadline = time.monotonic() + 90
        expected = str(self.target/RESOURCE/'sync-runtime/tools/companion_bridge.py')
        enabled = self.settings.get('sync_enabled') and not self.settings.get('paused')
        stable = 0
        while time.monotonic() < deadline:
            rows = process_rows()
            worker = any(expected+' --state-dir '+str(self.state)+' ' in command for _, command in rows)
            if self.pids() and (worker or not enabled):
                stable += 1
                if stable >= 3:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: return False
                    try:
                        self.inspect(timeout=min(25, remaining))
                    except UpdateError as error:
                        if not str(error).startswith('command_failed: '): raise
                        stable = 0
                    else:
                        return time.monotonic() < deadline
            else: stable = 0
            time.sleep(1)
        return False


def state_directory(home, explicit):
    if explicit: return Path(explicit).expanduser().resolve()
    startup = home/'Library/Application Support/CodexEInk/startup.json'
    if not startup.exists(): return home/'Library/Application Support/CodexEInk/companion'
    value = json.loads(startup.read_text())
    if value.get('version') != 1 or not isinstance(value.get('state_directory'), str):
        raise UpdateError('invalid_startup_record')
    return Path(value['state_directory']).resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-app', type=Path, required=True)
    parser.add_argument('--target-app', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--recover', type=Path)
    args = parser.parse_args()
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        target = (args.target_app or home/'Applications/Codex Ink.app').absolute()
        if args.source_app.is_symlink(): raise UpdateError('symlink_source_app')
        # macOS /var and /tmp are system aliases; canonicalize source parents.
        source = args.source_app.resolve()
        state = state_directory(home, args.state_dir)
        no_links(target); no_links(state)
        app = LocalApp(target, state, home)
        with update_lock(target):
            if args.recover:
                recover(args.recover.resolve(), target, stop=app.stop, launch=app.launch, ownership=app.ownership)
                print('已恢复旧版。', flush=True); return 0
            for path in target.parent.glob('.codex-ink-update-*/transaction.json'):
                record = json.loads(path.read_text())
                if record.get('target') == str(target) and record.get('phase') not in ('complete','rolled_back'):
                    raise UpdateError('unfinished_update: use --recover ' + str(path.parent))
            app.inspect()
            print('正在校验新版并保留已信任的事件入口…', flush=True)
            tx = prepare(source, target)
            print('等待当前发送结束并重启 Codex Ink；Codex 可继续使用…', flush=True)
            try:
                app.stop()
                activate(tx, target, launch=app.launch, healthy=app.healthy, stop=app.stop, ownership=app.ownership)
            except Exception:
                # Prepared-only failures preserve the target and can be retried.
                record = record_for(tx, target)
                if record['phase'] == 'prepared' and target.stat().st_ino == record['original_inode']:
                    record['phase'] = 'rolled_back'; write_record(tx, record)
                    if not app.pids(): app.launch()
                raise
            print(json.dumps({'status':'updated', 'app':str(target), 'rollback':str(tx),
                              'hooks_changed':False}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        print('更新未完成：' + str(error), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
