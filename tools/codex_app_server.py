"""Minimal read-only client for the bundled Codex app-server JSONL protocol."""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO


DEFAULT_CODEX_BINARY = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


@dataclass(frozen=True)
class AppServerSnapshot:
    threads: tuple[dict[str, Any], ...]
    rates: dict[str, Any]
    usage: dict[str, Any]
    account: dict[str, Any] = field(default_factory=dict)


class JsonlRpcConnection:
    def __init__(self, *, stdin: TextIO, stdout: TextIO, timeout: float = 20.0):
        self.stdin = stdin
        self.stdout = stdout
        self.timeout = timeout
        self.notifications: list[dict[str, Any]] = []
        self._buffer = bytearray()
        self._max_line_bytes = 16 * 1024 * 1024

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None, *, request_id: int) -> dict[str, Any]:
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            line = self._readline(deadline - time.monotonic())
            if not line:
                raise RuntimeError(f"app-server closed before response {request_id}")
            payload = json.loads(line)
            if payload.get("id") != request_id:
                self.notifications.append(payload)
                continue
            if "error" in payload:
                error = payload["error"] or {}
                raise RuntimeError(str(error.get("message") or error))
            return dict(payload.get("result") or {})
        raise TimeoutError(f"app-server request timed out: {method}")

    def _readline(self, timeout: float) -> str:
        try:
            file_descriptor = self.stdout.fileno()
        except (AttributeError, OSError):
            return self.stdout.readline()
        deadline = time.monotonic() + timeout
        while True:
            end = self._buffer.find(b'\n')
            if end >= 0:
                if end + 1 > self._max_line_bytes:
                    raise ValueError('app-server response exceeds line limit')
                line = bytes(self._buffer[:end + 1])
                del self._buffer[:end + 1]
                return line.decode('utf-8')
            if len(self._buffer) >= self._max_line_bytes:
                raise ValueError('app-server response exceeds line limit')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('app-server response timed out')
            ready, _, _ = select.select([file_descriptor], [], [], remaining)
            if not ready:
                raise TimeoutError('app-server response timed out')
            chunk = os.read(file_descriptor, 65536)
            if not chunk:
                if self._buffer:
                    raise RuntimeError('app-server closed with an incomplete response')
                return ""
            self._buffer.extend(chunk)


class CodexAppServerClient:
    def __init__(self, binary: Path = DEFAULT_CODEX_BINARY, timeout: float = 20.0):
        self.binary = binary
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self.connection: JsonlRpcConnection | None = None
        self._next_id = 1

    def __enter__(self) -> "CodexAppServerClient":
        self.process = subprocess.Popen(
            [str(self.binary), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        try:
            self.connection = JsonlRpcConnection(stdin=self.process.stdin, stdout=self.process.stdout, timeout=self.timeout)
            self._request(
                "initialize",
                {
                    "clientInfo": {"name": "codex-eink-sync", "title": "Codex E-Ink Sync", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": False},
                },
            )
            self.connection.notify("initialized", {})
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.connection is None:
            raise RuntimeError("app-server client is not started")
        request_id = self._next_id
        self._next_id += 1
        return self.connection.request(method, params, request_id=request_id)

    def fetch(self) -> AppServerSnapshot:
        thread_result = self._request(
            "thread/list",
            {
                "cursor": None,
                "limit": 50,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "sourceKinds": [],
            },
        )
        rates = self._request("account/rateLimits/read", {})
        usage = self._request("account/usage/read", {})
        account = self._request("account/read", {})
        return AppServerSnapshot(
            threads=tuple(thread_result.get("data") or ()),
            rates=rates,
            usage=usage,
            account=account,
        )

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
            self.process = None
            self.connection = None
