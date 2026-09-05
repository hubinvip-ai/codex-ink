import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.codex_app_server import AppServerSnapshot
from tools.codex_eink_sync import (
    FrameDeduplicator,
    build_push_command,
    default_output_path,
    default_push_binary,
    render_real_snapshot,
)


class SyncServiceTests(unittest.TestCase):
    def test_deployed_runtime_resolves_sender_from_bin_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployed = root / "bin/eink-push"
            deployed.parent.mkdir()
            deployed.touch()

            self.assertEqual(default_push_binary(root), deployed)

    def test_deployed_runtime_shares_daemon_dashboard_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            (root / "bin").mkdir(parents=True)
            (root / "bin/eink-push").touch()
            state_dir = Path(directory) / "state"

            self.assertEqual(default_output_path(root, state_dir), state_dir / "dashboard.png")

    def test_real_snapshot_renders_real_quota_and_exact_palette(self):
        app = AppServerSnapshot(
            threads=(
                {"id": "thr_1", "name": "真实状态更新", "cwd": "/workspace/project", "updatedAt": 200},
            ),
            rates={"rateLimits": {"primary": {"usedPercent": 45, "resetsAt": 1788771680}}},
            usage={"summary": {"lifetimeTokens": 1000}, "dailyUsageBuckets": [{"startDate": "2026-09-01", "tokens": 50}]},
        )
        hook_state = {
            "version": 1,
            "threads": {
                "thr_1": {"session_id": "thr_1", "cwd": "/workspace/project", "status": "running", "updated_at": 200}
            },
        }
        snapshot, image = render_real_snapshot(app, hook_state, datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(snapshot.remaining_percent, 55)
        self.assertEqual(image.size, (400, 300))
        self.assertEqual(set(image.getdata()), {(255, 255, 255), (0, 0, 0), (198, 40, 40)})

    def test_frame_deduplicator_skips_identical_content(self):
        with tempfile.TemporaryDirectory() as directory:
            hash_file = Path(directory) / "last-frame.sha256"
            deduplicator = FrameDeduplicator(hash_file)
            self.assertTrue(deduplicator.should_push(b"first"))
            deduplicator.commit(b"first")
            self.assertFalse(deduplicator.should_push(b"first"))
            self.assertTrue(deduplicator.should_push(b"second"))

    def test_ble_sender_uses_a_pseudo_terminal_for_launchd_corebluetooth(self):
        command = build_push_command(Path("/runtime/eink-push"), Path("/state/dashboard.png"))
        self.assertEqual(
            command,
            [
                "/usr/bin/script",
                "-q",
                "/dev/null",
                "/runtime/eink-push",
                "--input",
                "/state/dashboard.png",
                "--quantized-output",
                "/state/dashboard.png",
            ],
        )


if __name__ == "__main__":
    unittest.main()
