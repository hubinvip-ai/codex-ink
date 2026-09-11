from dataclasses import replace
from datetime import datetime
import unittest

from tools.codex_frame_source import content_digest
from tools.codex_status_core import DashboardSnapshot, DisplayTask, DisplayStatus


class ContentDigestTests(unittest.TestCase):
    def setUp(self):
        self.stamp = int(datetime(2026, 9, 6, 12).astimezone().timestamp())
        self.task = DisplayTask('task', 'project', 'title', DisplayStatus.RUNNING, self.stamp)
        self.data = DashboardSnapshot(80, 20, self.stamp + 86400, (1, 2), 3,
                                      self.stamp, (self.task,), 'test-account', 'Pro')

    def test_read_and_event_times_do_not_trigger_refresh(self):
        newer = replace(self.data, updated_at_epoch=self.stamp+180,
                        tasks=(replace(self.task, updated_at_epoch=self.stamp+180),))
        self.assertEqual(content_digest(self.data, 'zh-CN'), content_digest(newer, 'zh-CN'))

    def test_visible_content_and_language_changes_are_detected(self):
        changes = [replace(self.data, remaining_percent=79),
                   replace(self.data, reset_at_epoch=self.stamp+90000),
                   replace(self.data, usage_buckets=(2, 3)),
                   replace(self.data, account_label='different-account'),
                   replace(self.data, plan_label='Plus'),
                   replace(self.data, updated_at_epoch=self.stamp+86400),
                   replace(self.data, tasks=()),
                   replace(self.data, tasks=(replace(self.task, status=DisplayStatus.WAITING),)),
                   replace(self.data, tasks=(replace(self.task, conversation='new title'),))]
        original = content_digest(self.data, 'zh-CN')
        for data in changes:
            with self.subTest(data=data):
                self.assertNotEqual(original, content_digest(data, 'zh-CN'))
        self.assertNotEqual(original, content_digest(self.data, 'en'))

    def test_task_order_changes_are_detected(self):
        other = replace(self.task, thread_id='other', conversation='other')
        self.assertNotEqual(content_digest(replace(self.data, tasks=(self.task, other)), 'en'),
                            content_digest(replace(self.data, tasks=(other, self.task)), 'en'))
