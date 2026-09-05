#!/usr/bin/env python3
"""Merge real Codex state, render the dashboard, and refresh the BLE e-ink display."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.codex_app_server import AppServerSnapshot, CodexAppServerClient
from tools.codex_status_core import build_dashboard_snapshot, empty_hook_state
from tools.eink_text_renderer import FINAL_PROFILE, render_dashboard


DEFAULT_STATE_DIR = Path.home() / "Library/Application Support/CodexEInk"
DEFAULT_STATE_FILE = DEFAULT_STATE_DIR / "status.json"


def default_output_path(root: Path, state_dir: Path) -> Path:
    return state_dir / "dashboard.png" if (root / "bin/eink-push").exists() else root / "output/eink-dashboard-400x300.png"


DEFAULT_OUTPUT = default_output_path(ROOT, DEFAULT_STATE_DIR)


def default_push_binary(root: Path) -> Path:
    deployed = root / "bin/eink-push"
    return deployed if deployed.exists() else root / ".build/release/eink-push"


DEFAULT_PUSH_BINARY = default_push_binary(ROOT)


class FrameDeduplicator:
    def __init__(self, hash_file: Path):
        self.hash_file = hash_file

    @staticmethod
    def digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def should_push(self, content: bytes) -> bool:
        if not self.hash_file.exists():
            return True
        try:
            return self.hash_file.read_text().strip() != self.digest(content)
        except OSError:
            return True

    def commit(self, content: bytes) -> None:
        self.hash_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.hash_file.with_suffix(self.hash_file.suffix + ".tmp")
        temporary.write_text(self.digest(content) + "\n")
        os.replace(temporary, self.hash_file)


def load_hook_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_hook_state()
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else empty_hook_state()
    except (OSError, json.JSONDecodeError):
        return empty_hook_state()


def render_real_snapshot(app: AppServerSnapshot, hook_state: dict[str, Any], now: datetime):
    snapshot = build_dashboard_snapshot(
        hook_state=hook_state,
        threads=app.threads,
        rates=app.rates,
        usage=app.usage,
        now=now,
        account=app.account or {},
    )
    return snapshot, render_dashboard(profile=FINAL_PROFILE, data=snapshot)


def _image_bytes(image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _write_frame(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def build_push_command(push_binary: Path, output: Path) -> list[str]:
    # CoreBluetooth does not transition out of .unknown for this CLI when
    # launchd provides no terminal. `script` supplies a PTY without a shell.
    return [
        "/usr/bin/script",
        "-q",
        "/dev/null",
        str(push_binary),
        "--input",
        str(output),
        "--quantized-output",
        str(output),
    ]


def sync_once(
    *,
    state_file: Path,
    output: Path,
    push_binary: Path,
    deduplicator: FrameDeduplicator,
    no_push: bool,
    force: bool,
) -> dict[str, Any]:
    with CodexAppServerClient() as client:
        app_snapshot = client.fetch()
    dashboard, image = render_real_snapshot(app_snapshot, load_hook_state(state_file), datetime.now().astimezone())
    content = _image_bytes(image)
    _write_frame(output, content)
    changed = force or deduplicator.should_push(content)
    pushed = False
    if changed and not no_push:
        if not push_binary.exists():
            raise FileNotFoundError(f"missing BLE sender: {push_binary}")
        subprocess.run(build_push_command(push_binary, output), cwd=ROOT, check=True)
        deduplicator.commit(content)
        pushed = True
    return {
        "remainingPercent": dashboard.remaining_percent,
        "usedPercent": dashboard.used_percent,
        "taskCount": len(dashboard.tasks),
        "changed": changed,
        "pushed": pushed,
        "output": str(output),
    }


def run_daemon(args) -> int:
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_dir / "daemon.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("codex-eink sync daemon is already running", file=sys.stderr)
            return 0

        deduplicator = FrameDeduplicator(args.state_dir / "last-frame.sha256")
        last_state_mtime = -1.0
        pending_at = 0.0
        next_poll = 0.0
        while True:
            now = time.monotonic()
            try:
                current_mtime = args.state_file.stat().st_mtime if args.state_file.exists() else 0.0
                if current_mtime != last_state_mtime:
                    last_state_mtime = current_mtime
                    pending_at = now + args.debounce
                if now >= next_poll or (pending_at and now >= pending_at):
                    result = sync_once(
                        state_file=args.state_file,
                        output=args.output,
                        push_binary=args.push_binary,
                        deduplicator=deduplicator,
                        no_push=args.no_push,
                        force=False,
                    )
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    next_poll = now + args.poll
                    pending_at = 0.0
            except Exception as error:
                print(f"codex-eink sync error: {error}", file=sys.stderr, flush=True)
                next_poll = now + min(args.poll, 30)
                pending_at = 0.0
            time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--push-binary", type=Path, default=DEFAULT_PUSH_BINARY)
    parser.add_argument("--poll", type=float, default=60.0)
    parser.add_argument("--debounce", type=float, default=5.0)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()
    if args.once:
        if args.delay > 0:
            time.sleep(args.delay)
        args.state_dir.mkdir(parents=True, exist_ok=True)
        once_lock_path = args.state_dir / "sync-once.lock"
        with once_lock_path.open("a+") as once_lock:
            try:
                fcntl.flock(once_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"skipped": "sync already running"}))
                return 0
            deduplicator = FrameDeduplicator(args.state_dir / "last-frame.sha256")
            result = sync_once(
                state_file=args.state_file,
                output=args.output,
                push_binary=args.push_binary,
                deduplicator=deduplicator,
                no_push=args.no_push,
                force=args.force,
            )
            print(json.dumps(result, ensure_ascii=False))
        return 0
    return run_daemon(args)


if __name__ == "__main__":
    raise SystemExit(main())
