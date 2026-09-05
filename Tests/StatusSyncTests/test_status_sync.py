import unittest
from datetime import datetime, timezone

from tools.codex_status_core import (
    DisplayStatus,
    apply_hook_event,
    build_dashboard_snapshot,
)


class StatusReducerTests(unittest.TestCase):
    def setUp(self):
        self.base = {
            "session_id": "thr_1",
            "turn_id": "turn_1",
            "cwd": "/workspace/Codex Ink",
        }

    def test_lifecycle_maps_to_real_display_states_without_storing_content(self):
        state = {"version": 1, "threads": {}}
        running = apply_hook_event(state, {**self.base, "hook_event_name": "UserPromptSubmit", "prompt": "private"}, 100)
        self.assertEqual(running["threads"]["thr_1"]["status"], "running")
        self.assertNotIn("prompt", running["threads"]["thr_1"])

        waiting = apply_hook_event(running, {**self.base, "hook_event_name": "PermissionRequest"}, 110)
        self.assertEqual(waiting["threads"]["thr_1"]["status"], "waiting")

        stopped = apply_hook_event(waiting, {**self.base, "hook_event_name": "Stop", "last_assistant_message": "private"}, 120)
        self.assertEqual(stopped["threads"]["thr_1"]["status"], "waiting")
        self.assertNotIn("last_assistant_message", stopped["threads"]["thr_1"])

        ended = apply_hook_event(stopped, {**self.base, "hook_event_name": "SessionEnd"}, 130)
        self.assertEqual(ended["threads"]["thr_1"]["status"], "ended")

    def test_session_start_is_queued_until_a_prompt_starts(self):
        state = apply_hook_event(
            {"version": 1, "threads": {}},
            {**self.base, "hook_event_name": "SessionStart"},
            100,
        )
        self.assertEqual(state["threads"]["thr_1"]["status"], "queued")


class SnapshotTests(unittest.TestCase):
    def test_real_quota_usage_and_hook_state_build_dashboard_snapshot(self):
        hook_state = {
            "version": 1,
            "threads": {
                "thr_1": {"session_id": "thr_1", "cwd": "/workspace/Codex Ink", "status": "running", "updated_at": 200},
                "thr_2": {"session_id": "thr_2", "cwd": "/workspace/供应链平台", "status": "waiting", "updated_at": 190},
            },
        }
        threads = [
            {"id": "thr_1", "name": "实现真实状态同步", "cwd": "/workspace/Codex Ink", "updatedAt": 200},
            {"id": "thr_1", "name": "旧重复记录", "cwd": "/workspace/Codex Ink", "updatedAt": 100},
            {"id": "thr_2", "name": "审核供应商联系方式", "cwd": "/workspace/供应链平台", "updatedAt": 190},
        ]
        rates = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 45, "windowDurationMins": 10080, "resetsAt": 1788771680},
            }
        }
        usage = {
            "summary": {"lifetimeTokens": 13_493_155_058},
            "dailyUsageBuckets": [
                {"startDate": "2026-08-30", "tokens": 100},
                {"startDate": "2026-08-31", "tokens": 250},
            ],
        }
        snapshot = build_dashboard_snapshot(
            hook_state=hook_state,
            threads=threads,
            rates=rates,
            usage=usage,
            now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            account={
                "account": {
                    "type": "chatgpt",
                    "email": "demo@example.com",
                    "planType": "pro",
                }
            },
        )
        self.assertEqual(snapshot.remaining_percent, 55)
        self.assertEqual(snapshot.used_percent, 45)
        self.assertEqual(snapshot.reset_at_epoch, 1788771680)
        self.assertEqual(snapshot.usage_buckets, (100, 250))
        self.assertEqual(snapshot.lifetime_tokens, 13_493_155_058)
        self.assertEqual(snapshot.account_label, "demo@example.com")
        self.assertEqual(snapshot.plan_label, "Pro 20x")
        self.assertEqual(len(snapshot.tasks), 2)
        self.assertEqual(snapshot.tasks[0].project, "Codex Ink")
        self.assertEqual(snapshot.tasks[0].conversation, "实现真实状态同步")
        self.assertEqual(snapshot.tasks[0].status, DisplayStatus.RUNNING)
        self.assertEqual(snapshot.tasks[1].status, DisplayStatus.WAITING)

    def test_ended_threads_and_duplicate_metadata_are_not_rendered(self):
        snapshot = build_dashboard_snapshot(
            hook_state={
                "version": 1,
                "threads": {
                    "ended": {"session_id": "ended", "cwd": "/workspace/ended", "status": "ended", "updated_at": 300}
                },
            },
            threads=[{"id": "ended", "name": "Should not show", "cwd": "/workspace/ended", "updatedAt": 300}],
            rates={"rateLimits": {"primary": {"usedPercent": 0, "resetsAt": 0}}},
            usage={"summary": {}, "dailyUsageBuckets": []},
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot.tasks, ())

    def test_internal_hook_session_without_thread_metadata_is_not_rendered(self):
        snapshot = build_dashboard_snapshot(
            hook_state={
                "version": 1,
                "threads": {
                    "internal": {
                        "session_id": "internal",
                        "cwd": "/",
                        "status": "waiting",
                        "updated_at": 300,
                    }
                },
            },
            threads=[],
            rates={"rateLimits": {"primary": {"usedPercent": 0, "resetsAt": 0}}},
            usage={"summary": {}, "dailyUsageBuckets": []},
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot.tasks, ())

    def test_last_updated_time_comes_from_displayed_tasks_not_unrelated_history(self):
        snapshot = build_dashboard_snapshot(
            hook_state={
                "version": 1,
                "threads": {
                    "active": {
                        "session_id": "active",
                        "cwd": "/workspace/project",
                        "status": "running",
                        "updated_at": 200,
                    }
                },
            },
            threads=[
                {"id": "active", "name": "当前任务", "cwd": "/workspace/project", "updatedAt": 200},
                {"id": "history", "name": "无关历史", "cwd": "/workspace/other", "updatedAt": 999},
            ],
            rates={"rateLimits": {"primary": {"usedPercent": 0, "resetsAt": 0}}},
            usage={"summary": {}, "dailyUsageBuckets": []},
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot.updated_at_epoch, 200)


if __name__ == "__main__":
    unittest.main()
