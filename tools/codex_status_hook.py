#!/usr/bin/env python3
"""Persist Codex lifecycle metadata and optionally run an async screen sync."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.codex_status_core import apply_hook_event, empty_hook_state


DEFAULT_STATE_FILE = Path.home() / "Library/Application Support/CodexEInk/status.json"
DEFAULT_STATE_DIR = DEFAULT_STATE_FILE.parent


def run_sync_worker(*, root: Path = ROOT, state_dir: Path = DEFAULT_STATE_DIR, delay: int = 5) -> int:
    """Run inside an async Codex hook so CoreBluetooth inherits Codex app context."""
    state_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root)
    with (state_dir / "hook-sync.log").open("a") as stdout, (state_dir / "hook-sync-error.log").open("a") as stderr:
        result = subprocess.run(
            [
                sys.executable,
                str(root / "tools/codex_eink_sync.py"),
                "--once",
                "--delay",
                str(delay),
            ],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    return result.returncode


def update_state_file(state_file: Path, event: dict, now: int) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = state_file.with_suffix(state_file.suffix + ".lock")
    with lock_file.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(state_file.read_text()) if state_file.exists() else empty_hook_state()
        except (OSError, json.JSONDecodeError):
            state = empty_hook_state()
        updated = apply_hook_event(state, event, now)
        temporary = state_file.with_suffix(state_file.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(updated, output, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, state_file)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--sync", action="store_true")
    args, _ = parser.parse_known_args()
    try:
        event = json.load(sys.stdin)
        if isinstance(event, dict):
            update_state_file(args.state_file, event, int(time.time()))
    except Exception as error:
        print(f"codex-eink hook warning: {error}", file=sys.stderr)
    print("{}")
    sys.stdout.flush()
    if args.sync and os.environ.get("CODEX_EINK_DISABLE_SYNC") != "1":
        run_sync_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
