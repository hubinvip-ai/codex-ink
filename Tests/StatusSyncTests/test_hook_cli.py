import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.codex_status_hook import run_sync_worker


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/codex_status_hook.py"


class HookCliTests(unittest.TestCase):
    def test_hook_writes_atomic_metadata_state_and_returns_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "status.json"
            event = {
                "session_id": "thr_real",
                "turn_id": "turn_real",
                "cwd": "/workspace/project",
                "hook_event_name": "UserPromptSubmit",
                "prompt": "must not persist",
            }
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--state-file", str(state_file)],
                input=json.dumps(event),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})
            state = json.loads(state_file.read_text())
            self.assertEqual(state["threads"]["thr_real"]["status"], "running")
            self.assertNotIn("prompt", state_file.read_text())
            self.assertFalse((Path(directory) / "status.json.tmp").exists())

    def test_malformed_input_does_not_block_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "status.json"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--state-file", str(state_file)],
                input="not-json",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {})

    @mock.patch("tools.codex_status_hook.subprocess.run")
    def test_async_hook_worker_runs_one_shot_sync_in_same_process(self, run):
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            (root / "tools").mkdir(parents=True)
            result = run_sync_worker(root=root, state_dir=Path(directory) / "state", delay=5)

        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertEqual(command[1], str(root / "tools/codex_eink_sync.py"))
        self.assertEqual(command[2:], ["--once", "--delay", "5"])
        self.assertEqual(run.call_args.kwargs["cwd"], root)
        self.assertFalse(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
