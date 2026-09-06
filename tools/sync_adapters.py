"""Bounded subprocess adapters; no raw external output is written to logs."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
from tools.reliable_sync import SyncFailure, SyncDeferred

ROOT = Path(__file__).resolve().parents[1]


def run_bounded(command, *, timeout, content=None, pass_fds=(), cancel_event=None):
    process = subprocess.Popen(command, stdin=subprocess.PIPE if content is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=pass_fds)
    try:
        if cancel_event is None:
            stdout, stderr = process.communicate(content, timeout=timeout)
        else:
            deadline=time.monotonic()+timeout
            pending=content
            while True:
                if cancel_event.is_set():
                    raise InterruptedError('operation cancelled')
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    raise subprocess.TimeoutExpired(command,timeout)
                try:
                    stdout,stderr=process.communicate(pending,timeout=min(.1,remaining))
                    break
                except subprocess.TimeoutExpired:
                    pending=None
    except BaseException:
        # Includes the app-server child or PTY helper, not unrelated system jobs.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    if process.returncode:
        # A killed supervisor can close its pipes while the transport leaf is
        # still alive. Clean its group before the SAME owner may retry a send.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class CodexRenderer:
    def __init__(self, binary: Path, *, validated=False, cancel_event=None, language="zh-CN"):
        self.binary = Path(binary).expanduser().resolve()
        if language not in ("zh-CN", "en"):
            raise ValueError("unsupported display language")
        self.language=language
        self.validated=validated
        self.cancel_event=cancel_event

    def __call__(self, state):
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise SyncFailure('configuration_error', permanent=True)
        try:
            command=[sys.executable, str(ROOT / 'tools/codex_frame_source.py'), str(self.binary)]
            if self.validated: command.append('--validated')
            command += ['--language', self.language]
            result = run_bounded(command,timeout=105,content=json.dumps(state).encode(),cancel_event=self.cancel_event)
        except InterruptedError as error:
            raise SyncDeferred() from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SyncFailure('source_unavailable') from error
        if result.returncode:
            raise SyncFailure('source_unavailable')
        try:
            with Image.open(io.BytesIO(result.stdout)) as image:
                image.load()
                return image.copy()
        except (OSError, ValueError) as error:
            raise SyncFailure('invalid_frame', permanent=True) from error


class CLISender:
    """Legacy CLI transport for opt-in experiments, NOT proof of app BLE access."""
    def __init__(self, binary: Path, device: str):
        self.binary = Path(binary).expanduser().resolve()
        self.device = device

    def __call__(self, frame: Path, owner_fd=None):
        if not self.device.strip():
            raise SyncFailure('device_not_configured', permanent=True)
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise SyncFailure('sender_missing', permanent=True)
        command = [sys.executable, str(ROOT/'tools/owned_sender.py'), str(self.binary), str(frame), self.device,
                   str(owner_fd if owner_fd is not None else -1)]
        try:
            result = run_bounded(command, timeout=130, pass_fds=() if owner_fd is None else (owner_fd,))
        except subprocess.TimeoutExpired as error:
            raise SyncFailure('send_timeout') from error
        except OSError as error:
            raise SyncFailure('send_failed') from error
        output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
        if result.returncode or 'WRITE_COMPLETE packets=129 bytes=30511' not in output:
            if '蓝牙不可用，状态值 3' in output or '蓝牙不可用，状态值3' in output:
                raise SyncFailure('permission_denied', permanent=True)
            if '蓝牙不可用，状态值 4' in output or '蓝牙不可用，状态值4' in output:
                raise SyncFailure('bluetooth_off', permanent=True)
            raise SyncFailure('send_failed')
