#!/usr/bin/env python3
"""Explicit, fail-closed companion migration. No BLE, app exit, or trust writes.

CLI (current OS user only):
  companion_setup.py --state-dir D inspect
  companion_setup.py --state-dir D install --python P --runtime-root R
  companion_setup.py --state-dir D restore
  companion_setup.py --state-dir D hook --installation-id UUID

inspect/install/restore emit {ok, stage, blockers, installation_id, legacy_present}.
hook ALWAYS emits {}. Only installed commands carry CODEX_EINK_INSTALL_NONCE.
Hook metadata: D/status.json; journal: D/sync-journal.json. Receipt is not Codex
trust: it proves successful metadata ingestion through the current definition.

The OS adapter is injectable in Python, NOT through readiness CLI switches.
All file changes are locked, backed up and content/identity compared. Restore
never launches old services; the user must review trust/startup separately.
Unknown inline/plugin/managed sources require manual resolution, not overrides.

Install only from a stable application/runtime location, never a temporary
packaging/test bundle. Moving, deleting or changing installed runtime files
invalidates readiness. Inspect is safe in a temporary bundle. The direct native
caller is allowed to remain open, but MUST invoke install/restore only from its
explicit user action after stopping its worker and waiting for BLE cleanup.
This helper independently checks Codex and sender processes; it never sends.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from copy import deepcopy
import ctypes
import errno
import fcntl
import grp
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LABEL = 'com.ben.codex-eink-sync'
EVENTS = ('SessionStart', 'UserPromptSubmit', 'PermissionRequest', 'Stop', 'SessionEnd')
LIMIT = 1024 * 1024
NONCE_ENV = 'CODEX_EINK_INSTALL_NONCE'
RUNTIME_FILES = ('companion_setup.py', 'codex_eink_reliable.py', 'codex_status_core.py',
                 'reliable_sync.py', 'sync_journal.py', 'companion_sources.py', 'codex_hook_inventory.py',
                 'codex_app_server.py', '__init__.py')


class SetupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()


def json_object(raw, code='config_invalid'):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError('object required')
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise SetupError(code) from None


def valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 10**12


def absolute(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or any(c in str(path) for c in '\x00\n\r'):
        raise SetupError('unsafe_path')
    return path


def below(path, root):
    return path != root and root in path.parents


def trusted_app_parent(candidate, path, info):
    """Only the normal root:admin /Applications parent, never a bundle node."""
    if candidate != Path('/Applications') or not below(path, candidate):
        return False
    if not path.relative_to(candidate).parts[0].endswith('.app'):
        return False
    try:
        admin_gid = grp.getgrnam('admin').gr_gid
    except KeyError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_gid == admin_gid and
            stat.S_IMODE(info.st_mode) in (0o755, 0o775))


def safe_path(path, uid, *, root=None):
    """No symlink traversal or foreign/writable ownership in an owned subtree."""
    path = absolute(path)
    if root is not None and path != root and not below(path, root):
        raise SetupError('unsafe_path')
    system_bundle = root is not None and root.parent == Path('/Applications') and root.suffix == '.app'
    if system_bundle and not trusted_app_parent(root.parent, path, root.parent.lstat()):
        raise SetupError('unsafe_path')
    ancestors = [path] + list(path.parents)
    if root is not None:
        ancestors = ancestors[:ancestors.index(root) + 1]
    for candidate in ancestors:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, uid):
            raise SetupError('unsafe_path')
        # System sticky temp ancestors are permitted; never an owned target.
        sticky_ancestor = candidate != path and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky_ancestor and not trusted_app_parent(candidate, path, info):
            raise SetupError('unsafe_path')
        if candidate == path:
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise SetupError('unsafe_path')
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise SetupError('unsafe_path')
        elif not stat.S_ISDIR(info.st_mode):
            raise SetupError('unsafe_path')
        if root is not None and candidate == root and info.st_uid != uid and not (system_bundle and info.st_uid == 0):
            raise SetupError('unsafe_path')
    return path


class Files:
    """No-overwrite publication, not atomic CAS. Retain captured inodes forever.

    Each prepared record is durable before withdrawal. A missing completion
    marker blocks inspection until explicit recovery. Open-FD writes to a moved
    original remain in its retained same-directory file, never discarded.
    """
    def __init__(self, home, uid, state_dir):
        self.home, self.uid, self.state = home, uid, state_dir
        self.transactions = state_dir / 'setup-file-transactions'

    def check(self, path):
        return safe_path(path, self.uid, root=self.home if path == self.home or below(path, self.home) else None)

    def read(self, path):
        self.check(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                raise SetupError('file_invalid')
            raw = stream.read(LIMIT + 1)
            after = os.fstat(stream.fileno())
            if len(raw) > LIMIT or self.identity(info) != self.identity(after):
                raise SetupError('config_changed')
            return {'raw': raw, 'identity': self.identity(info)}

    def read_source(self, path):
        """Read-only inventory fingerprint. Missing candidates contain no data.

        Only absence may be observed under a shared ancestor. Existing files
        still go through every ownership/symlink check; writers never use this.
        """
        absolute(path)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        return self.read(path)

    @staticmethod
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, stat.S_IMODE(info.st_mode))

    def cas(self, path, expected, raw):
        """Historical method name; exclusive withdrawal/publish, not true CAS."""
        self.owned_target(path)
        self.check(path)
        if self.read(path) != expected:
            raise SetupError('config_changed')
        # A previous rename of this same inode may have succeeded while its
        # fsync failed. Flush and settle that exact operation before superseding
        # it (e.g. a high-level rollback), not by rewriting its intent record.
        for previous in self.pending():
            if self.home / previous['target'] != path:
                continue
            self.check_recovery_evidence(previous)
            status = ('committed' if self.matches(expected, previous['after']) else
                      'aborted' if self.matches(expected, previous['before']) else None)
            if status is None:
                raise SetupError('recovery_conflict')
            self.sync_directory(path.parent)
            self.finish_operation(previous, status)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.check(path)
        identity = str(uuid.uuid4())
        captured, candidate, _ = self.operation_paths(path, identity)
        if raw is not None:
            self.exclusive_bytes(candidate, raw, expected['identity'][-1] if expected else 0o600)
        operation = {'version': 1, 'id': identity, 'target': str(path.relative_to(self.home)),
                     'before': self.signature(expected), 'after': self.signature(self.read(candidate)) if raw is not None else None}
        self.check(self.transactions)
        self.transactions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.exclusive_bytes(self.transactions / (identity + '.json'), encode(operation))
        if expected is not None:
            self.rename_exclusive(path, captured)
            self.sync_directory(path.parent)
            if not self.matches(self.read(captured), operation['before']):
                self.return_captured(captured, path)
                raise SetupError('config_changed')
        if raw is not None:
            try:
                self.rename_exclusive(candidate, path)
            except Exception:
                self.return_captured(captured, path)
                raise
        self.sync_directory(path.parent)
        self.finish_operation(operation, 'committed')

    def owned_target(self, path):
        legacy = self.home / ('Library/LaunchAgents/' + LABEL + '.plist')
        state_file = path.parent == self.state and (
            path.name in ('setup-installation.json', 'setup-receipt.json') or
            re.fullmatch(r'setup-(?:backup-[0-9a-f-]{36}|restore-[0-9a-f-]{36}(?:-[0-9a-f-]{36})?)\.json', path.name))
        if not below(path, self.home) or not (path == self.home / '.codex/hooks.json' or path == legacy or state_file):
            raise SetupError('unsafe_path')

    @staticmethod
    def sync_directory(directory):
        fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def rename_exclusive(self, source, target):
        """Darwin RENAME_EXCL|RENAME_NOFOLLOW_ANY. Never fall back to replace."""
        if sys.platform != 'darwin':
            raise SetupError('exclusive_rename_unavailable')
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(target), 0x04 | 0x10) != 0:
            number = ctypes.get_errno()
            if number in (errno.EEXIST, errno.ENOENT):
                raise SetupError('config_changed')
            if number in (errno.EINVAL, errno.ENOTSUP):
                raise SetupError('exclusive_rename_unavailable')
            raise OSError(number, 'exclusive publication failed')

    def exclusive_bytes(self, path, raw, mode=0o600):
        self.check(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.setup-prepared-', dir=path.parent)
        temporary = Path(name)
        # Prepared files are intentionally retained on errors, never unlinked
        # by a comparison followed by deletion of a potentially changed path.
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.rename_exclusive(temporary, path)
        self.sync_directory(path.parent)

    @staticmethod
    def signature(image):
        if image is None:
            return None
        return {'sha256': digest(image['raw']), 'identity': list(image['identity'])}

    @classmethod
    def matches(cls, image, signature, *, content_only=False):
        if image is None or signature is None:
            return image is None and signature is None
        current = cls.signature(image)
        if current['sha256'] != signature['sha256']:
            return False
        indexes = (5,) if content_only else (0, 1, 2, 3, 5)
        # rename can change ctime; the captured inode/content/mode/mtime cannot.
        return all(current['identity'][i] == signature['identity'][i] for i in indexes)

    @staticmethod
    def operation_paths(path, identity):
        prefix = '.' + path.name + '.companion-' + identity
        return tuple(path.parent / (prefix + suffix) for suffix in ('.retained', '.candidate', '.recovery'))

    def return_captured(self, captured, target):
        if self.read(captured) is not None and self.read(target) is None:
            self.rename_exclusive(captured, target)
            self.sync_directory(target.parent)

    def finish_operation(self, operation, status):
        path = self.transactions / (operation['id'] + '.done')
        value = {'id': operation['id'], 'status': status}
        existing = self.read(path)
        if existing is not None:
            if json_object(existing['raw'], 'transaction_invalid') != value:
                raise SetupError('transaction_invalid')
            return
        self.exclusive_bytes(path, encode(value))

    def pending(self):
        self.check(self.transactions)
        if not self.transactions.exists():
            return []
        result = []
        for path in sorted(self.transactions.glob('*.json')):
            operation = json_object(self.read(path)['raw'], 'transaction_invalid')
            try:
                if set(operation) != {'version', 'id', 'target', 'before', 'after'} or operation['version'] != 1:
                    raise ValueError()
                if str(uuid.UUID(operation['id'])) != path.stem:
                    raise ValueError()
                target = self.home / operation['target']
                absolute(target)
                self.owned_target(target)
                for signature in (operation['before'], operation['after']):
                    if signature is not None and (set(signature) != {'sha256', 'identity'} or
                            not re.fullmatch('[a-f0-9]{64}', signature['sha256']) or len(signature['identity']) != 6 or
                            not all(type(n) is int and n >= 0 for n in signature['identity'])):
                        raise ValueError()
                done = self.read(path.with_suffix('.done'))
                if done:
                    value = json_object(done['raw'], 'transaction_invalid')
                    if value not in ({'id': operation['id'], 'status': 'committed'}, {'id': operation['id'], 'status': 'aborted'}):
                        raise ValueError()
                    continue
                result.append(operation)
            except (KeyError, ValueError, TypeError):
                raise SetupError('transaction_invalid') from None
        return result

    def recover_pending(self):
        """Explicit file-only recovery. Never replace an intervening destination."""
        for operation in self.pending():
            self.check_recovery_evidence(operation)
            target = self.home / operation['target']
            captured, candidate, recovery = self.operation_paths(target, operation['id'])
            current = self.read(target)
            if self.matches(current, operation['before'], content_only=True):
                self.finish_operation(operation, 'aborted')
                continue
            retained = self.read(captured)
            if current is None:
                if retained is not None:
                    self.return_captured(captured, target)
                elif operation['before'] is not None:
                    raise SetupError('recovery_conflict')
                self.finish_operation(operation, 'aborted')
                continue
            if not self.matches(current, operation['after']):
                raise SetupError('recovery_conflict')
            # Withdraw our known candidate exclusively. If a writer won before
            # withdrawal, put its captured inode back, never replace it.
            self.rename_exclusive(target, recovery)
            self.sync_directory(target.parent)
            if not self.matches(self.read(recovery), operation['after']):
                self.return_captured(recovery, target)
                raise SetupError('recovery_conflict')
            self.return_captured(captured, target)
            self.finish_operation(operation, 'aborted')

    def check_recovery_evidence(self, operation):
        target = self.home / operation['target']
        recovery = self.operation_paths(target, operation['id'])[2]
        image = self.read(recovery)
        if image is not None and not self.matches(image, operation['after']):
            # A crash may have interrupted us after capturing an intervening
            # writer but before classifying it. Never silently discard that fact.
            raise SetupError('recovery_conflict')


def parse_procargs(raw):
    """Darwin KERN_PROCARGS2: argc, exec path, padding, argv. Never parse env."""
    if len(raw) < 5:
        raise SetupError('process_inspection_failed')
    argc = struct.unpack_from('i', raw)[0]
    if not 1 <= argc <= 65536:
        raise SetupError('process_inspection_failed')
    end = raw.find(b'\0', 4)
    if end < 0:
        raise SetupError('process_inspection_failed')
    start = end + 1
    while start < len(raw) and raw[start] == 0:
        start += 1
    values = raw[start:].split(b'\0', argc)
    if len(values) <= argc:
        raise SetupError('process_inspection_failed')
    executable = os.fsdecode(raw[4:end])
    if not Path(executable).is_absolute():
        raise SetupError('process_inspection_failed')
    return {'executable': executable, 'argv': [os.fsdecode(value) for value in values[:argc]]}


class MacSystem:
    """Read actual argv, not shlex-split ps display strings (paths have spaces)."""
    def __init__(self):
        self.uid = os.getuid()
        self.environment = {key: os.environ[key] for key in ('CODEX_HOME',) if key in os.environ}

    def now(self):
        return time.time()

    def system_roots(self):
        return [Path('/etc/codex').resolve(), Path('/Library/Application Support/Codex')]

    def temporary_roots(self):
        return [Path(path).resolve() for path in ('/tmp', '/var/tmp', '/var/folders', tempfile.gettempdir())]

    def source_snapshot(self, *, home, state_dir, cwd, files, initial_images):
        from tools.companion_sources import APISourceReader, SourceError
        from tools.codex_app_server import DEFAULT_CODEX_BINARY
        images = dict(initial_images)
        settings_path = state_dir / 'settings.json'
        settings = files.read(settings_path)
        images[settings_path] = settings
        if settings is None:
            binary = DEFAULT_CODEX_BINARY
        else:
            value = json_object(settings['raw'], 'settings_invalid')
            if not isinstance(value.get('codex_binary'), str):
                raise SetupError('settings_invalid')
            binary = absolute(value['codex_binary'])
        try:
            # Applications is normally admin-group writable. Check the entire
            # app bundle as the executable's ownership boundary, not that parent.
            bundle = next((p for p in binary.parents if p.suffix == '.app'), None)
            safe_path(binary, self.uid, root=bundle)
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise ValueError()
        except (ValueError, OSError, SetupError):
            raise SourceError('source_api_unavailable') from None
        return APISourceReader(binary).collect(home=home, cwd=cwd, files=files, initial_images=images)

    @staticmethod
    def run(argv):
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)

    def processes(self):
        if sys.platform != 'darwin':
            raise SetupError('process_inspection_failed')
        result = self.run(['/bin/ps', '-axo', 'pid=,uid='])
        if result.returncode != 0:
            raise SetupError('process_inspection_failed')
        libc = ctypes.CDLL(None, use_errno=True)
        records = []
        for line in result.stdout.splitlines():
            pid, uid = map(int, line.split())
            if uid != self.uid or pid == os.getpid():
                continue
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
            buffer = ctypes.create_string_buffer(LIMIT)
            length = ctypes.c_size_t(LIMIT)
            if libc.sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) != 0:
                if ctypes.get_errno() == errno.ESRCH:
                    continue  # process exited after the listing
                if self.confirm_zombie(pid):
                    continue
                raise SetupError('process_inspection_failed')
            try:
                arguments = parse_procargs(buffer.raw[:length.value])
            except SetupError:
                if self.confirm_zombie(pid):
                    continue
                raise
            records.append({'pid': pid, 'uid': uid, **arguments})
        return records

    def confirm_zombie(self, pid):
        """Only fresh proof of a dead same-user process permits skipping unreadable argv."""
        result = self.run(['/bin/ps', '-p', str(pid), '-o', 'pid=,uid=,stat='])
        rows = result.stdout.splitlines()
        if result.returncode != 0 or len(rows) != 1:
            return False
        fields = rows[0].split()
        return (len(fields) == 3 and fields[0] == str(pid).encode() and
                fields[1] == str(self.uid).encode() and fields[2].startswith(b'Z'))

    def job(self, label):
        if label != LABEL:
            raise SetupError('legacy_agent_conflict')
        result = self.run(['/bin/launchctl', 'print', 'gui/' + str(self.uid) + '/' + label])
        if result.returncode:
            # Only the specific absent-service result proves absence.
            if result.returncode == 113 and b'Could not find service' in result.stderr:
                return None
            raise SetupError('launchagent_inspection_failed')
        text = result.stdout.decode('utf-8', errors='strict')
        args = re.search(r'^\s*arguments = \{\n(.*?)^\s*\}', text, re.M | re.S)
        if not args:
            raise SetupError('legacy_agent_conflict')
        argv = [line.strip() for line in args.group(1).splitlines() if line.strip()]
        pid = re.search(r'^\s*pid = (\d+)\s*$', text, re.M)
        return {'Label': label, 'ProgramArguments': argv, 'pid': int(pid.group(1)) if pid else None}

    def bootout(self, label):
        if label != LABEL:
            raise SetupError('legacy_agent_conflict')
        result = self.run(['/bin/launchctl', 'bootout', 'gui/' + str(self.uid) + '/' + label])
        if result.returncode:
            raise SetupError('bootout_failed')


def validate_hooks(value):
    if set(value) - {'description', 'hooks'}:
        raise SetupError('unknown_hook_source')
    hooks = value.get('hooks', {})
    if not isinstance(hooks, dict):
        raise SetupError('config_invalid')
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise SetupError('config_invalid')
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise SetupError('config_invalid')
            if 'matcher' in group and not isinstance(group['matcher'], str):
                raise SetupError('config_invalid')
            for handler in group['hooks']:
                if not isinstance(handler, dict) or handler.get('type') not in ('command', 'mcp_tool', 'prompt', 'agent'):
                    raise SetupError('config_invalid')
                if handler['type'] == 'command' and (not isinstance(handler.get('command'), str) or not handler['command'].strip()):
                    raise SetupError('config_invalid')
    return value


def handlers(config):
    for event, groups in config.get('hooks', {}).items():
        for index, group in enumerate(groups):
            for offset, handler in enumerate(group['hooks']):
                yield event, index, offset, group, handler


def python_command(token):
    return Path(token).is_absolute() and re.fullmatch(r'(?:python3(?:\.\d+)?|Python)', Path(token).name) is not None


def python_script(argv):
    """Recognize Python's script position; a script path in user arguments is not it."""
    if not argv or not python_command(argv[0]):
        return None
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            return argv[index + 1] if index + 1 < len(argv) else None
        if arg in ('-c', '-m'):
            return None
        if not arg.startswith('-'):
            return arg
        if arg in ('-W', '-X'):
            index += 1
        index += 1
    return None


def legacy_handler(handler, legacy):
    if handler.get('type') != 'command':
        return False
    command = handler['command']
    try:
        tokens = shlex.split(command)
    except ValueError:
        raise SetupError('config_invalid') from None
    target = str(legacy / 'tools/codex_status_hook.py')
    match = (len(tokens) in (2, 3) and python_command(tokens[0]) and tokens[1] == target
             and (len(tokens) == 2 or tokens[2] == '--sync'))
    if match:
        # No shell expansion/alternate platform command hidden in an owned entry.
        if any(c in command for c in ('`', '$', '\n', '\r')) or 'commandWindows' in handler or 'command_windows' in handler:
            raise SetupError('unknown_hook_source')
        return True
    if any(name in command for name in ('codex_status_hook.py', 'codex_eink_sync.py', 'companion_setup.py', 'eink-push', 'owned_sender.py')):
        raise SetupError('unknown_hook_source')
    return False


def group_meta(group):
    return {key: value for key, value in group.items() if key != 'hooks'}


def install_hooks(original, legacy, command):
    """Pure transform; retain group positions, unrelated handlers and top metadata."""
    result = deepcopy(original)
    hooks = result.setdefault('hooks', {})
    removed = []
    for event, groups in hooks.items():
        for index, group in enumerate(groups):
            retained = []
            for offset, handler in enumerate(group['hooks']):
                if legacy_handler(handler, legacy):
                    removed.append({'event': event, 'group_index': index, 'offset': offset,
                                    'meta': group_meta(group), 'handler': deepcopy(handler)})
                else:
                    retained.append(handler)
            group['hooks'] = retained
    installed = {}
    for event in EVENTS:
        group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 3}]}
        hooks.setdefault(event, []).append(group)
        installed[event] = deepcopy(group)
    return result, removed, installed


def remove_installed(current, installed):
    result = deepcopy(current)
    for event, wanted in installed.items():
        groups = result.get('hooks', {}).get(event, [])
        matches = [(index, group) for index, group in enumerate(groups)
                   if group_meta(group) == group_meta(wanted) and wanted['hooks'][0] in group['hooks']]
        count = sum(group['hooks'].count(wanted['hooks'][0]) for _, group in matches)
        if len(matches) != 1 or count != 1:
            raise SetupError('owned_hook_conflict')
        index, group = matches[0]
        group['hooks'].remove(wanted['hooks'][0])
        if not group['hooks']:
            groups.pop(index)
        if not groups:
            result['hooks'].pop(event)
    return result


def restore_hooks(current, manifest):
    result = remove_installed(current, manifest['installed'])
    original_bytes = Setup.unbackup(manifest['backups']['hooks'])
    original = json_object(original_bytes) if original_bytes is not None else {'hooks': {}}
    baseline = deepcopy(original)
    removed_slots = {}
    for record in manifest['removed']:
        removed_slots.setdefault((record['event'], record['group_index']), set()).add(record['offset'])
    for (event, index), offsets in removed_slots.items():
        group = baseline['hooks'][event][index]
        group['hooks'] = [handler for offset, handler in enumerate(group['hooks']) if offset not in offsets]
    selected = {}
    unchanged_events = {event: result.get('hooks', {}).get(event) == groups
                        for event, groups in baseline.get('hooks', {}).items()}
    for record in manifest['removed']:
        groups = result.setdefault('hooks', {}).setdefault(record['event'], [])
        slot = (record['event'], record['group_index'])
        if slot not in selected:
            expected = baseline['hooks'][record['event']]
            anchors = expected[record['group_index']]['hooks']
            matches = [group for group in groups if group_meta(group) == record['meta']
                       and all(handler in group['hooks'] for handler in anchors)]
            if len(matches) == 1:
                selected[slot] = matches[0]
            elif unchanged_events.get(record['event']):
                # Identical empty groups are distinguishable by their unchanged positions.
                selected[slot] = groups[record['group_index']]
            else:
                raise SetupError('owned_hook_conflict')
        group = selected[slot]
        if record['handler'] in group['hooks']:
            raise SetupError('owned_hook_conflict')
        group['hooks'].insert(min(record['offset'], len(group['hooks'])), deepcopy(record['handler']))
    return result


def toml_sources(raw):
    """Read only source selectors. Python 3.9 ships without a TOML parser.

    Do not guess inline, multiline or computed hook definitions. Unknown syntax
    affecting layer selection fails closed. No auth/credential files are read.
    """
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise SetupError('config_invalid') from None
    projects, tables, keys, section = [], set(), set(), ()
    token = r'(?:"(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27|[A-Za-z0-9_-]+)'
    def parse_key(text):
        try:
            parts = re.findall(token, text)
            return tuple(json.loads(part) if part.startswith('"') else part[1:-1] if part.startswith("'") else part for part in parts)
        except ValueError:
            raise SetupError('config_invalid') from None

    def check_source_key(parts):
        if parts == ('features', 'hooks') or parts == ('hooks',) or parts[:2] == ('hooks', 'state'):
            return
        if parts and parts[0] == 'projects':
            if len(parts) > 1:
                path = Path(parts[1])
                if not path.is_absolute() or '..' in path.parts:
                    raise SetupError('unknown_hook_source')
                projects.append(path)
            return
        if any(part.lower() in ('hooks', 'plugins', 'include', 'hooks_file', 'config_file', 'config_path', 'requirements') for part in parts):
            raise SetupError('unknown_hook_source')
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):
            match = re.fullmatch(r'\[\s*(' + token + r'(?:\s*\.\s*' + token + r')*)\s*\]\s*(?:#.*)?', line)
            if not match:
                raise SetupError('config_invalid')
            parts = parse_key(match.group(1))
            check_source_key(parts)
            if parts in tables:
                raise SetupError('config_invalid')
            tables.add(parts)
            section = parts
        elif '=' not in line or '"""' in line or "'''" in line:
            raise SetupError('config_invalid')
        else:
            # Only bounded scalar/array selectors are interpreted on Python3.9.
            assignment = re.fullmatch(r'(' + token + r'(?:\s*\.\s*' + token + r')*)\s*=\s*(.+)', line)
            if not assignment:
                raise SetupError('config_invalid')
            key = section + parse_key(assignment.group(1))
            check_source_key(key)
            if key[:2] == ('hooks', 'state') and (len(key) != 4 or key[-1] != 'trusted_hash'):
                raise SetupError('unknown_hook_source')
            if key in keys:
                raise SetupError('config_invalid')
            keys.add(key)
            scalar = r'(?:"(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27|true|false|[+-]?(?:\d[\d_]*)(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)'
            if not re.fullmatch(r'(?:' + scalar + r'|\[\s*(?:' + scalar + r'\s*(?:,\s*' + scalar + r'\s*)*,?)?\])\s*(?:#.*)?', assignment.group(2)):
                raise SetupError('config_invalid')
    return projects


class Setup:
    def __init__(self, *, home, state_dir, cwd, system=None):
        self.home, self.state, self.cwd = Path(home), Path(state_dir), Path(cwd)
        self.system = system if system is not None else MacSystem()
        self.files = Files(self.home, self.system.uid, self.state)
        self.codex = self.home / '.codex'
        self.config = self.codex / 'hooks.json'
        self.base = self.home / 'Library/Application Support/CodexEInk'
        self.legacy = self.base / 'runtime'
        self.plist = self.home / ('Library/LaunchAgents/' + LABEL + '.plist')
        self.manifest_path = self.state / 'setup-installation.json'
        self.receipt_path = self.state / 'setup-receipt.json'
        self._manifest_image = None
        self._source_snapshot = None
        self._source_paths = set()
        self._observed_legacy = False

    def validate_roots(self):
        self.files.check(self.home)
        self.files.check(self.state)
        if (not below(self.state, self.home) or self.state == self.base or
                self.state == self.codex or below(self.state, self.codex) or
                self.state == self.legacy or below(self.state, self.legacy)):
            raise SetupError('unsafe_path')
        custom = self.system.environment.get('CODEX_HOME')
        if custom and Path(custom) != self.codex:
            raise SetupError('unknown_hook_source')

    @contextmanager
    def lock(self):
        self.validate_roots()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state / 'setup-migration.lock'
        self.files.check(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SetupError('migration_busy') from None
            yield

    def manifest(self):
        image = self.files.read(self.manifest_path)
        self._manifest_image = image
        return self.validate_manifest(image)

    def validate_manifest(self, image):
        if image is None:
            return None
        value = json_object(image['raw'], 'installation_invalid')
        try:
            fields = {'version', 'installation_id', 'nonce', 'installed_at', 'phase', 'home', 'state_dir',
                      'python', 'runtime_root', 'runtime_hashes', 'definition_sha256', 'installed', 'removed',
                      'legacy_present', 'backups'}
            if (set(value) != fields or type(value['version']) is not int or value['version'] != 1
                    or str(uuid.UUID(value['installation_id'])) != value['installation_id']
                    or not re.fullmatch('[0-9a-f]{64}', value['nonce']) or not valid_time(value['installed_at'])
                    or value['state_dir'] != str(self.state) or value['home'] != str(self.home)
                    or value['phase'] not in ('installing', 'installed', 'restoring', 'restored', 'rolled_back', 'recovery_required')
                    or set(value['installed']) != set(EVENTS) or type(value['legacy_present']) is not bool
                    or set(value['runtime_hashes']) != set(RUNTIME_FILES)
                    or any(not re.fullmatch('[0-9a-f]{64}', item) for item in value['runtime_hashes'].values())
                    or set(value['backups']) != {'hooks', 'agent', 'status'}):
                raise ValueError()
            absolute(value['python'])
            absolute(value['runtime_root'])
            command = self.command(Path(value['python']), Path(value['runtime_root']), value['installation_id'], value['nonce'])
            for event in EVENTS:
                if value['installed'][event] != {'hooks': [{'type': 'command', 'command': command, 'timeout': 3}]}:
                    raise ValueError()
            if digest(encode(value['installed'])) != value['definition_sha256']:
                raise ValueError()
            for backup in value['backups'].values():
                self.unbackup(backup)
            original = self.unbackup(value['backups']['hooks'])
            config = validate_hooks(json_object(original)) if original is not None else {'hooks': {}}
            _, removed, _ = install_hooks(config, self.legacy, command)
            if removed != value['removed']:
                raise ValueError()
            archive = self.files.read(self.state / ('setup-backup-' + value['installation_id'] + '.json'))
            if archive is None:
                raise ValueError()
            archived = json_object(archive['raw'], 'installation_invalid')
            if {key: item for key, item in archived.items() if key != 'phase'} != {key: item for key, item in value.items() if key != 'phase'}:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SetupError('installation_invalid') from None
        return value

    def recovery_manifests(self):
        """Resolve and authenticate every possible installed identity read-only."""
        found = {}
        current = self.manifest()
        if current is not None:
            found[current['installation_id']] = current
        for operation in self.files.pending():
            target = self.home / operation['target']
            if target != self.manifest_path:
                continue
            for path in self.files.operation_paths(target, operation['id']):
                image = self.files.read(path)
                if image is not None:
                    value = self.validate_manifest(image)
                    found[value['installation_id']] = value
        # File-operation records predate an installation-id field. A retained
        # runtime can still target this state directory, even if the current
        # manifest belongs to B. Authenticate all bounded retained identities.
        archives = sorted(self.state.glob('setup-backup-*.json'))
        if len(archives) > 128:
            raise SetupError('recovery_identity_unavailable')
        for path in archives:
            value = self.validate_manifest(self.files.read(path))
            found.setdefault(value['installation_id'], value)
        if not found:
            raise SetupError('recovery_identity_unavailable')
        return tuple(found[key] for key in sorted(found))

    def command(self, python, runtime, identity, nonce):
        return NONCE_ENV + '=' + nonce + ' ' + shlex.join([
            str(python), str(runtime / 'tools/companion_setup.py'), '--state-dir', str(self.state),
            'hook', '--installation-id', identity])

    def runtime_inputs(self, python, runtime):
        python, runtime = absolute(python), absolute(runtime)
        if any(runtime == root or below(runtime, root) for root in self.system.temporary_roots()):
            raise SetupError('runtime_location_unstable')
        # Resolve interpreter symlinks explicitly; hook script/runtime symlinks are forbidden.
        python = python.resolve(strict=True)
        self.files.check(python)
        self.files.check(runtime)
        if not python_command(str(python)) or not python.is_file() or not os.access(python, os.X_OK) or not runtime.is_dir():
            raise SetupError('source_invalid')
        if self.state == runtime or below(runtime, self.state) or below(self.state, runtime) or runtime == self.legacy:
            raise SetupError('source_invalid')
        hashes = {}
        for name in RUNTIME_FILES:
            image = self.files.read(runtime / 'tools' / name)
            if image is None or (not image['raw'] and name != '__init__.py'):
                raise SetupError('source_invalid')
            hashes[name] = digest(image['raw'])
        # A user-selected script must be this helper, not an arbitrary executable.
        if hashes['companion_setup.py'] != digest(Path(__file__).read_bytes()):
            raise SetupError('source_invalid')
        return python, runtime, hashes

    def legacy_sources(self, current, manifest, config_image):
        # This exact image also supplied current; never mix A's tree with B's bytes.
        images = {self.config: config_image}
        roots = {self.codex}
        roots.update(self.system.system_roots())
        # CWD and all ancestors are potential active project layers, even without git.
        roots.update(parent / '.codex' for parent in [self.cwd] + list(self.cwd.parents))
        checked = set()
        while roots - checked:
            root = sorted(roots - checked, key=str)[0]
            checked.add(root)
            for name in ('config.toml', 'hooks.json', 'requirements.toml', 'managed_config.toml'):
                path = root / name
                if path == self.config:
                    continue
                image = self.files.read(path)
                images[path] = image
                if image is None:
                    continue
                if name.endswith('.toml'):
                    for project in toml_sources(image['raw']):
                        roots.update(parent / '.codex' for parent in [project] + list(project.parents))
                elif path != self.config:
                    config = validate_hooks(json_object(image['raw']))
                    for _, _, _, _, handler in handlers(config):
                        if legacy_handler(handler, self.legacy):
                            raise SetupError('unknown_hook_source')
            if len(checked) > 512:
                raise SetupError('unknown_hook_source')
        # Unknown plugin discovery registries cannot be silently treated as no hooks.
        for path in (self.codex / 'plugins/config.json', self.codex / 'plugins/installed_plugins.json'):
            image = self.files.read(path)
            images[path] = image
            if image is not None:
                raise SetupError('unknown_hook_source')
        known = manifest['installed'] if manifest and manifest['phase'] in ('installed', 'installing', 'restoring', 'recovery_required') else {}
        legacy = False
        for event, _, _, group, handler in handlers(current):
            if event in known and handler == known[event]['hooks'][0] and group_meta(group) == group_meta(known[event]):
                continue
            if 'companion_setup.py' in handler.get('command', '') and manifest:
                raise SetupError('owned_hook_conflict')
            legacy = legacy_handler(handler, self.legacy) or legacy
        return images, legacy

    def sources(self, current, manifest, config_image):
        from tools.companion_sources import SourceError
        self._source_snapshot = None
        self._source_paths = set()
        self._observed_legacy = False
        known = manifest['installed'] if manifest and manifest['phase'] in ('installed', 'installing', 'restoring', 'recovery_required') else {}
        for event, _, _, group, handler in handlers(current):
            if event in known and handler == known[event]['hooks'][0] and group_meta(group) == group_meta(known[event]):
                continue
            if 'companion_setup.py' in handler.get('command', '') and manifest:
                raise SetupError('owned_hook_conflict')
            self._observed_legacy = legacy_handler(handler, self.legacy) or self._observed_legacy
        try:
            snapshot = self.system.source_snapshot(home=self.home, state_dir=self.state, cwd=self.cwd,
                                                   files=self.files, initial_images={self.config: config_image})
        except SourceError as error:
            # Conservative fallback is diagnosis only. Never promote a file-only
            # guess (especially plugins) into install permission or readiness.
            if error.code == 'source_api_unavailable':
                try:
                    self.legacy_sources(current, manifest, config_image)
                except SetupError:
                    pass
            raise SetupError(error.code) from None
        except (AttributeError, OSError, RuntimeError):
            raise SetupError('source_api_unavailable') from None
        for records in snapshot['hooks_by_cwd'].values():
            for record in records:
                if record['handlerType'] != 'command':
                    continue
                is_global = record['source'] == 'user' and record['sourcePath'] == str(self.config)
                if is_global and any(record['command'] == group['hooks'][0]['command'] for group in known.values()):
                    continue
                if legacy_handler({'type': 'command', 'command': record['command']}, self.legacy):
                    if not is_global:
                        raise SetupError('unknown_hook_source')
                    self._observed_legacy = True
        self._source_snapshot = snapshot
        self._source_paths = set(snapshot.get('source_paths', ()))
        return snapshot['images'], self._observed_legacy

    def agent_owned(self, value):
        if not isinstance(value, dict) or value.get('Label') != LABEL:
            return False
        args = value.get('ProgramArguments')
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args) or len(args) < 2:
            return False
        if not python_command(args[0]) or args[1] != str(self.legacy / 'tools/codex_eink_sync.py'):
            return False
        if value.get('Program', args[0]) != args[0] or value.get('WorkingDirectory', str(self.legacy)) != str(self.legacy):
            return False
        paths = {'--state-dir': str(self.base), '--state-file': str(self.base / 'status.json'),
                 '--output': str(self.base / 'dashboard.png'), '--push-binary': str(self.legacy / 'bin/eink-push')}
        index, seen = 2, set()
        while index < len(args):
            option = args[index]
            if option in seen:
                return False
            seen.add(option)
            if option == '--no-push':
                index += 1
                continue
            if index + 1 >= len(args):
                return False
            argument = args[index + 1]
            if option in paths:
                if argument != paths[option]:
                    return False
            elif option in ('--poll', '--debounce'):
                try:
                    number = float(argument)
                    if not math.isfinite(number) or number <= 0:
                        return False
                except ValueError:
                    return False
            else:
                return False
            index += 2
        if '--no-push' not in seen:
            return False
        return True

    def agent(self):
        image = self.files.read(self.plist)
        value = None
        if image:
            try:
                value = plistlib.loads(image['raw'])
            except Exception:
                raise SetupError('legacy_agent_conflict') from None
            if not self.agent_owned(value):
                raise SetupError('legacy_agent_conflict')
        try:
            job = self.system.job(LABEL)
        except SetupError:
            raise
        except Exception:
            raise SetupError('launchagent_inspection_failed') from None
        if job is not None and (not self.agent_owned(job) or value is None or job['ProgramArguments'] != value['ProgramArguments']):
            raise SetupError('legacy_agent_conflict')
        return image, job

    def processes(self, manifest=None, *, changing=False, job=None, extra_manifests=()):
        try:
            records = self.system.processes()
            if not isinstance(records, list):
                raise ValueError()
            blockers = set()
            scripts = {str(self.legacy / 'tools' / name) for name in ('codex_status_hook.py', 'codex_eink_sync.py', 'owned_sender.py')}
            roots = {self.legacy, Path(__file__).resolve().parents[1]}
            if manifest:
                roots.add(Path(manifest['runtime_root']))
            roots.update(Path(value['runtime_root']) for value in extra_manifests)
            for root in roots:
                scripts.update(str(root / 'tools' / name) for name in ('owned_sender.py', 'companion_bridge.py', 'codex_eink_reliable.py', 'codex_eink_sync.py'))
            for record in records:
                if record['uid'] != self.system.uid:
                    continue
                argv = record['argv']
                if not argv or not all(isinstance(arg, str) for arg in argv):
                    raise ValueError()
                executable = record.get('executable', argv[0])
                exe = Path(executable).name
                current_gui = Path(executable).parts[-4:] == ('ChatGPT.app', 'Contents', 'MacOS', 'ChatGPT')
                if changing and (exe in ('Codex', 'codex') or current_gui):
                    blockers.add('codex_running')
                # Native setup host may remain open only as this direct caller.
                # Its worker/sender must still be stopped before a mutation.
                if changing and exe == 'eink-companion' and record['pid'] != os.getppid():
                    blockers.add('companion_running')
                if job and type(job.get('pid')) is int and job['pid'] > 0 and record['pid'] == job['pid']:
                    # launchd supplies ownership; the OS supplies the actual
                    # executable. Apple's /usr/bin/python3 launcher can exec a
                    # framework Python, changing argv[0] without changing the
                    # script or any options. Never extend this exception to a
                    # different PID, changed arguments or a non-Python process.
                    if not (self.agent_owned(job) and python_command(executable)
                            and argv[1:] == job['ProgramArguments'][1:]):
                        blockers.add('sender_running')
                    # This verified --no-push job is legacy_active, not a BLE
                    # sender. Only explicit install may boot it out; after
                    # bootout, checks without `job` still detect a survivor.
                    continue
                script = python_script([executable] + argv[1:])
                owned_names = {Path(value).name for value in scripts}
                if script is not None and not Path(script).is_absolute() and Path(script).name in owned_names:
                    cwd = record.get('cwd')
                    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                        blockers.add('sender_identity_ambiguous')
                        continue
                    script = str((Path(cwd) / script).resolve())
                if python_command(executable) and '-m' in argv:
                    index = argv.index('-m') + 1
                    if index < len(argv) and argv[index] in {'tools.' + Path(name).stem for name in owned_names}:
                        blockers.add('sender_identity_ambiguous')
                if script in scripts:
                    if changing or script.startswith(str(self.legacy) + '/'):
                        blockers.add('sender_running')
                binaries = {str(root / 'bin/eink-push') for root in roots}
                binaries.update(str(root / suffix) for root in roots for suffix in ('.build/debug/eink-push', '.build/release/eink-push'))
                build_sender = Path(executable).name == 'eink-push' and any(below(Path(executable), root / '.build') for root in roots)
                if executable in binaries or build_sender:
                    blockers.add('sender_running')
            return sorted(blockers)
        except SetupError:
            raise
        except Exception:
            raise SetupError('process_inspection_failed') from None

    def snapshot(self, *, changing=False):
        self._observed_legacy = False
        self.validate_roots()
        if self.files.pending():
            raise SetupError('migration_incomplete')
        manifest = self.manifest()
        image = self.files.read(self.config)
        config = validate_hooks(json_object(image['raw'])) if image else {'hooks': {}}
        images, legacy = self.sources(config, manifest, image)
        agent_image, job = self.agent()
        images[self.plist] = agent_image
        blockers = self.processes(manifest, changing=changing, job=job)
        return manifest, config, images, job, legacy or agent_image is not None or job is not None, blockers

    def result(self, *, stage='blocked', blockers=(), manifest=None, legacy=False):
        return {'ok': not bool(blockers), 'stage': stage, 'blockers': list(blockers),
                'installation_id': manifest['installation_id'] if manifest else None,
                'legacy_present': bool(legacy or self._observed_legacy)}

    def receipt_valid(self, manifest, image):
        if image is None:
            return False
        value = json_object(image['raw'], 'receipt_invalid')
        return (set(value) == {'installation_id', 'nonce', 'definition_sha256', 'event_timestamp'} and
                value['installation_id'] == manifest['installation_id'] and value['nonce'] == manifest['nonce'] and
                value['definition_sha256'] == manifest['definition_sha256'] and valid_time(value['event_timestamp']) and
                manifest['installed_at'] < value['event_timestamp'] <= self.system.now())

    def inspect(self):
        manifest, legacy = None, False
        try:
            manifest, config, _, job, legacy, blockers = self.snapshot()
            if blockers:
                return self.result(blockers=blockers, manifest=manifest, legacy=legacy)
            if not manifest or manifest['phase'] in ('restored', 'rolled_back'):
                return self.result(stage='legacy_active' if legacy else 'needs_install', legacy=legacy)
            if manifest['phase'] != 'installed':
                raise SetupError('migration_incomplete')
            remove_installed(config, manifest['installed'])
            _, _, hashes = self.runtime_inputs(Path(manifest['python']), Path(manifest['runtime_root']))
            if hashes != manifest['runtime_hashes']:
                raise SetupError('source_changed')
            if legacy or job:
                raise SetupError('legacy_active')
            valid = self.receipt_valid(manifest, self.files.read(self.receipt_path))
            from tools.companion_sources import installed_hooks_trusted
            trusted = installed_hooks_trusted(self._source_snapshot, self.config, manifest['installed'])
            return self.result(stage='ready' if valid and trusted else 'awaiting_trust', manifest=manifest)
        except SetupError as error:
            return self.result(blockers=[error.code], manifest=manifest, legacy=legacy)
        except Exception:
            return self.result(blockers=['storage_error'], manifest=manifest, legacy=legacy)

    @staticmethod
    def backup(image):
        if image is None:
            return None
        return {'base64': base64.b64encode(image['raw']).decode('ascii'), 'sha256': digest(image['raw'])}

    @staticmethod
    def unbackup(value):
        if value is None:
            return None
        raw = base64.b64decode(value['base64'], validate=True)
        if digest(raw) != value['sha256']:
            raise ValueError('backup digest')
        return raw

    def save_manifest(self, manifest):
        raw = encode(manifest)
        try:
            self.files.cas(self.manifest_path, self._manifest_image, raw)
        finally:
            # A rename can succeed before directory fsync fails. Track only our
            # known published bytes, never adopt an intervening user's edit.
            current = self.files.read(self.manifest_path)
            if current is not None and current['raw'] == raw:
                self._manifest_image = current

    def publish(self, path, before, content, published):
        """Record successful publication even if fsync subsequently reports failure."""
        try:
            self.files.cas(path, before, content)
        finally:
            after = self.files.read(path)
            if after != before:
                if (after['raw'] if after else None) != content:
                    raise SetupError('rollback_conflict')
                published.append((path, after, before))

    def rollback(self, published):
        conflicts = False
        for path, after, before in reversed(published):
            try:
                self.files.cas(path, after, before['raw'] if before else None)
            except Exception:
                conflicts = True
        return conflicts

    def compare_sources(self, images):
        for path, image in images.items():
            read = self.files.read_source if path in self._source_paths else self.files.read
            if read(path) != image:
                raise SetupError('config_changed')

    def install(self, *, python, runtime_root):
        manifest, legacy = None, False
        try:
            manifest, config, images, job, legacy, blockers = self.snapshot(changing=True)
            if blockers:
                return self.result(blockers=blockers, manifest=manifest, legacy=legacy)
            python, runtime, hashes = self.runtime_inputs(python, runtime_root)
            if manifest and manifest['phase'] == 'installed':
                if str(python) != manifest['python'] or str(runtime) != manifest['runtime_root']:
                    raise SetupError('installation_conflict')
                return self.inspect()
            if manifest and manifest['phase'] not in ('restored', 'rolled_back'):
                raise SetupError('migration_incomplete')
            identity, nonce, now = str(uuid.uuid4()), secrets.token_hex(32), self.system.now()
            installed_config, removed, installed = install_hooks(config, self.legacy, self.command(python, runtime, identity, nonce))
            old_state = self.files.read(self.base / 'status.json')
            if old_state:
                json_object(old_state['raw'], 'legacy_state_invalid')
            new = {'version': 1, 'installation_id': identity, 'nonce': nonce, 'installed_at': now,
                   'phase': 'installing', 'home': str(self.home), 'state_dir': str(self.state),
                   'python': str(python), 'runtime_root': str(runtime), 'runtime_hashes': hashes,
                   'definition_sha256': digest(encode(installed)), 'installed': installed, 'removed': removed,
                   'legacy_present': legacy, 'backups': {'hooks': self.backup(images[self.config]),
                   'agent': self.backup(images[self.plist]), 'status': self.backup(old_state)}}
            if len(encode(new)) > LIMIT or len(encode(installed_config)) > LIMIT:
                raise SetupError('backup_too_large')
            with self.lock():
                self.compare_sources(images)
                current = self.manifest()
                if current != manifest:
                    raise SetupError('config_changed')
                blockers = self.processes(manifest, changing=True, job=job)
                if blockers:
                    return self.result(blockers=blockers, manifest=manifest, legacy=legacy)
                backup_path = self.state / ('setup-backup-' + identity + '.json')
                self.files.cas(backup_path, None, encode(new))
                self.save_manifest(new)
                published = []
                try:
                    self.compare_sources(images)
                    # Disable automatic restart before bootout. Do not re-enable it on success.
                    for path, raw in ((self.config, encode(installed_config)), (self.plist, None)):
                        expected = images[path]
                        if expected is None and raw is None:
                            continue
                        self.publish(path, expected, raw, published)
                    if job is not None:
                        # Registered job must still be precisely the one inspected.
                        if self.system.job(LABEL) != job:
                            raise SetupError('legacy_agent_conflict')
                        try:
                            self.system.bootout(LABEL)
                        except Exception:
                            raise SetupError('bootout_failed') from None
                        if self.system.job(LABEL) is not None:
                            raise SetupError('bootout_failed')
                    blockers = self.processes(new, changing=True)
                    if blockers:
                        raise SetupError(blockers[0])
                    # Cover source/config changes throughout backup and bootout.
                    if self.runtime_inputs(python, runtime)[2] != hashes:
                        raise SetupError('source_changed')
                    self.compare_sources({path: image for path, image in images.items() if path not in (self.config, self.plist)})
                    self.sources(installed_config, new, self.files.read(self.config))
                    new['phase'] = 'installed'
                    self.save_manifest(new)
                except Exception as failure:
                    conflicts = self.rollback(published) or isinstance(failure, SetupError) and failure.code == 'rollback_conflict'
                    new['phase'] = 'recovery_required' if conflicts else 'rolled_back'
                    try:
                        self.save_manifest(new)
                    except Exception:
                        conflicts = True
                    code = failure.code if isinstance(failure, SetupError) else 'storage_error'
                    return self.result(blockers=[code] + (['rollback_conflict'] if conflicts else []), manifest=new, legacy=legacy)
            return self.inspect()
        except SetupError as error:
            return self.result(blockers=[error.code], manifest=manifest, legacy=legacy)
        except (FileNotFoundError, NotADirectoryError):
            return self.result(blockers=['source_invalid'], manifest=manifest, legacy=legacy)
        except Exception:
            return self.result(blockers=['storage_error'], manifest=manifest, legacy=legacy)

    def validate_restore_intent(self, intent, manifest):
        try:
            if (set(intent) != {'version', 'installation_id', 'hooks_before', 'hooks_after', 'agent_before', 'agent_after'} or
                    type(intent['version']) is not int or intent['version'] != 1 or
                    intent['installation_id'] != manifest['installation_id']):
                raise ValueError()
            before, after = self.unbackup(intent['hooks_before']), self.unbackup(intent['hooks_after'])
            original = self.unbackup(manifest['backups']['hooks'])
            if before == original:
                expected = original
            else:
                restored = restore_hooks(validate_hooks(json_object(before)), manifest)
                original_tree = json_object(original) if original is not None else {'hooks': {}}
                expected = original if restored == original_tree else encode(restored)
            old_agent = self.unbackup(manifest['backups']['agent'])
            if (after != expected or self.unbackup(intent['agent_after']) != old_agent or
                    self.unbackup(intent['agent_before']) not in (None, old_agent)):
                raise ValueError()
        except Exception:
            raise SetupError('transaction_invalid') from None
        return after

    def restore(self):
        manifest, legacy = None, False
        try:
            self.validate_roots()
            if self.files.pending():
                identities = self.recovery_manifests()
                blockers = self.processes(changing=True, extra_manifests=identities)
                if blockers:
                    return self.result(blockers=blockers)
                with self.lock():
                    repeated = self.recovery_manifests()
                    if repeated != identities:
                        raise SetupError('config_changed')
                    blockers = self.processes(changing=True, extra_manifests=repeated)
                    if blockers:
                        return self.result(blockers=blockers)
                    self.files.recover_pending()
            manifest, config, images, job, legacy, blockers = self.snapshot(changing=True)
            if blockers:
                return self.result(blockers=blockers, manifest=manifest, legacy=legacy)
            if not manifest or manifest['phase'] in ('restored', 'rolled_back'):
                return self.inspect()
            if manifest['phase'] not in ('installed', 'installing', 'restoring', 'recovery_required'):
                raise SetupError('migration_incomplete')
            if job is not None:
                raise SetupError('legacy_active')
            original = self.unbackup(manifest['backups']['hooks'])
            intent_path = self.state / ('setup-restore-' + manifest['installation_id'] + '.json')
            intent_image = self.files.read(intent_path)
            current_bytes = images[self.config]['raw'] if images[self.config] else None
            old_agent = self.unbackup(manifest['backups']['agent'])
            if manifest['phase'] == 'restoring':
                if intent_image is None:
                    raise SetupError('migration_incomplete')
                intent = json_object(intent_image['raw'], 'transaction_invalid')
                raw = self.validate_restore_intent(intent, manifest)
                # Resume only exact before/after states recorded BEFORE restoration.
                for current, before, after in ((current_bytes, self.unbackup(intent['hooks_before']), raw),
                        (images[self.plist]['raw'] if images[self.plist] else None, self.unbackup(intent['agent_before']), old_agent)):
                    if current not in (before, after):
                        raise SetupError('config_changed')
            else:
                if manifest['phase'] != 'installed' and current_bytes == original:
                    # The initial manifest may have published before any hook change.
                    raw = original
                else:
                    restored = restore_hooks(config, manifest)
                    before_config = json_object(original) if original is not None else {'hooks': {}}
                    raw = original if restored == before_config else encode(restored)
                intent = {'version': 1, 'installation_id': manifest['installation_id'],
                          'hooks_before': self.backup(images[self.config]),
                          'hooks_after': self.backup({'raw': raw}) if raw is not None else None,
                          'agent_before': self.backup(images[self.plist]),
                          'agent_after': manifest['backups']['agent']}
                if intent_image is not None:
                    self.validate_restore_intent(json_object(intent_image['raw'], 'transaction_invalid'), manifest)
                if len(encode(intent)) > LIMIT:
                    raise SetupError('backup_too_large')
            if images[self.plist] is not None and images[self.plist]['raw'] != old_agent:
                raise SetupError('legacy_agent_conflict')
            with self.lock():
                self.compare_sources(images)
                if self.manifest() != manifest:
                    raise SetupError('config_changed')
                blockers = self.processes(manifest, changing=True)
                if blockers:
                    return self.result(blockers=blockers, manifest=manifest, legacy=legacy)
                previous_phase = manifest['phase']
                if previous_phase != 'restoring':
                    # Every explicit retry gets an immutable intent. The fixed
                    # filename is merely the guarded current-attempt checkpoint.
                    attempt = self.state / ('setup-restore-' + manifest['installation_id'] + '-' + str(uuid.uuid4()) + '.json')
                    self.files.cas(attempt, None, encode(intent))
                    if intent_image is None or intent_image['raw'] != encode(intent):
                        self.files.cas(intent_path, intent_image, encode(intent))
                manifest['phase'] = 'restoring'
                self.save_manifest(manifest)
                published = []
                try:
                    self.compare_sources(images)
                    for path, content in ((self.config, raw), (self.plist, old_agent)):
                        before = images[path]
                        if (before['raw'] if before else None) == content:
                            continue
                        self.publish(path, before, content, published)
                    manifest['phase'] = 'restored'
                    self.save_manifest(manifest)
                except Exception as failure:
                    conflicts = self.rollback(published) or isinstance(failure, SetupError) and failure.code == 'rollback_conflict'
                    manifest['phase'] = 'recovery_required' if conflicts else previous_phase
                    try:
                        self.save_manifest(manifest)
                    except Exception:
                        conflicts = True
                    code = failure.code if isinstance(failure, SetupError) else 'storage_error'
                    return self.result(blockers=[code] + (['rollback_conflict'] if conflicts else []), manifest=manifest, legacy=legacy)
            return self.inspect()
        except SetupError as error:
            return self.result(blockers=[error.code], manifest=manifest, legacy=legacy)
        except Exception:
            return self.result(blockers=['storage_error'], manifest=manifest, legacy=legacy)

    def hook(self, event, *, installation_id, nonce):
        """Metadata only; never imports a sender or writes a trust database."""
        try:
            self.validate_roots()
            manifest = self.manifest()
            if not manifest or manifest['phase'] != 'installed' or installation_id != manifest['installation_id'] or nonce != manifest['nonce']:
                return {}
            if (not isinstance(event, dict) or event.get('hook_event_name') not in EVENTS or
                    not isinstance(event.get('session_id'), str) or not event['session_id'].strip() or
                    any(key in event and not isinstance(event[key], str) for key in ('cwd', 'turn_id'))):
                return {}
            metadata = {key: event[key] for key in ('hook_event_name', 'session_id', 'cwd', 'turn_id') if key in event}
            if any(len(value) > 4096 or '\x00' in value for value in metadata.values()):
                return {}
            with self.lock():
                if self.manifest() != manifest:
                    return {}
                config = self.files.read(self.config)
                if config is None:
                    return {}
                remove_installed(validate_hooks(json_object(config['raw'])), manifest['installed'])
                _, _, hashes = self.runtime_inputs(Path(manifest['python']), Path(manifest['runtime_root']))
                if hashes != manifest['runtime_hashes']:
                    return {}
                now = self.system.now()
                if not valid_time(now) or now <= manifest['installed_at']:
                    return {}
                for name in ('status.json', 'status.json.lock', 'sync-journal.json', 'sync-journal.lock'):
                    self.files.check(self.state / name)
                root = str(Path(__file__).resolve().parents[1])
                if root not in sys.path:
                    sys.path.insert(0, root)
                from tools.codex_eink_reliable import ingest_event
                from tools.sync_journal import SyncJournal
                ingest_event(SyncJournal(self.state), self.state / 'status.json', metadata, now)
                if self.files.read(self.config) != config:
                    return {}
                receipt = self.files.read(self.receipt_path)
                if receipt is None or not self.receipt_valid(manifest, receipt):
                    value = {'installation_id': installation_id, 'event_timestamp': now,
                             'nonce': nonce, 'definition_sha256': manifest['definition_sha256']}
                    self.files.cas(self.receipt_path, receipt, encode(value))
        except Exception:
            pass  # Hook contract is neutral, including malformed input/storage failures.
        return {}


class JSONParser(argparse.ArgumentParser):
    def error(self, message):
        raise SetupError('invalid_arguments')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    hook = 'hook' in argv
    try:
        parser = JSONParser(add_help=False)
        parser.add_argument('--state-dir', required=True, type=Path)
        commands = parser.add_subparsers(dest='command', required=True, parser_class=JSONParser)
        commands.add_parser('inspect', add_help=False)
        install = commands.add_parser('install', add_help=False)
        install.add_argument('--python', required=True, type=Path)
        install.add_argument('--runtime-root', required=True, type=Path)
        commands.add_parser('restore', add_help=False)
        child = commands.add_parser('hook', add_help=False)
        child.add_argument('--installation-id', required=True)
        args = parser.parse_args(argv)
        # Use the OS account home, not an arbitrary HOME environment override.
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        setup = Setup(home=home, state_dir=args.state_dir, cwd=Path.cwd().resolve())
        if args.command == 'hook':
            raw = sys.stdin.buffer.read(LIMIT + 1)
            event = json_object(raw) if len(raw) <= LIMIT else None
            result = setup.hook(event, installation_id=args.installation_id, nonce=os.environ.get(NONCE_ENV))
        elif args.command == 'install':
            result = setup.install(python=args.python, runtime_root=args.runtime_root)
        else:
            result = getattr(setup, args.command)()
    except Exception as error:
        code = error.code if isinstance(error, SetupError) else 'storage_error'
        result = {} if hook else {'ok': False, 'stage': 'blocked', 'blockers': [code],
                                 'installation_id': None, 'legacy_present': False}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if hook or result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
