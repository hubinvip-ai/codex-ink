import unittest
from dataclasses import replace
from datetime import datetime, timezone

from tools.codex_app_server import AppServerSnapshot

try:
    from tools.companion_data import validated_snapshot, DataUnavailable
except ImportError:
    validated_snapshot = None
    DataUnavailable = ValueError

NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)


def source():
    return AppServerSnapshot(threads=(), rates={'rateLimitsByLimitId':{'codex':{
        'primary':{'windowDurationMins':300,'usedPercent':99,'resetsAt':1789000000},
        'secondary':{'windowDurationMins':10080,'usedPercent':39,'resetsAt':1789000000},
    }}}, usage={'dailyUsageBuckets':[{'startDate':'2026-09-02','tokens':20}], 'summary':{'lifetimeTokens':20}},
        account={'account':{'email':'example@example.test','planType':'pro'}})


class DataTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(validated_snapshot, 'validated companion data adapter missing')

    def build(self, app):
        return validated_snapshot(app, {'version':1, 'threads':{}}, NOW)

    def test_weekly_window_selected_by_duration_not_slot_name(self):
        data = self.build(source())
        self.assertEqual(data.remaining_percent, 61)
        self.assertEqual(data.used_percent, 39)
        self.assertEqual(data.plan_label, 'Pro')
        self.assertEqual(sum(data.usage_buckets), 20)
        self.assertEqual(len(data.usage_buckets), 46)

    def test_unknown_or_wrong_window_cannot_display_full_remaining(self):
        for rates in ({}, {'rateLimits':{'primary':{'windowDurationMins':300,'usedPercent':0,'resetsAt':1789000000}}}):
            with self.assertRaises(DataUnavailable):
                self.build(replace(source(), rates=rates))

    def test_other_model_limit_is_not_codex_quota(self):
        rates={'rateLimits':{'limitId':'codex_spark','primary':{'windowDurationMins':10080,'usedPercent':0,'resetsAt':1789000000}}}
        with self.assertRaises(DataUnavailable):self.build(replace(source(),rates=rates))

    def test_invalid_usage_and_stale_reset_fail_closed(self):
        for used, reset in ((None,1789000000), (True,1789000000), (float('nan'),1789000000), (-1,1789000000), (101,1789000000), (0,0)):
            rates={'rateLimits':{'primary':{'windowDurationMins':10080, 'usedPercent':used,'resetsAt':reset}}}
            with self.assertRaises(DataUnavailable):
                self.build(replace(source(), rates=rates))

    def test_genuine_zero_remaining_is_preserved(self):
        rates={'rateLimits':{'primary':{'windowDurationMins':10080,'usedPercent':100,'resetsAt':1789000000}}}
        self.assertEqual(self.build(replace(source(), rates=rates)).remaining_percent, 0)

    def test_missing_account_or_plan_is_not_fabricated(self):
        for account in ({}, {'account':{'email':'x@y'}}, {'account':{'email':'x@y','planType':'unknown'}}):
            with self.assertRaises(DataUnavailable):
                self.build(replace(source(), account=account))

    def test_empty_history_is_not_demo_history(self):
        data = self.build(replace(source(), usage={'dailyUsageBuckets':[]}))
        self.assertEqual(data.usage_buckets, (0,) * 46)

    def test_unknown_history_is_not_zero(self):
        for usage in ({}, {'dailyUsageBuckets':None}):
            with self.assertRaises(DataUnavailable):
                self.build(replace(source(), usage=usage))

    def test_thirty_day_window_preserves_total_and_excludes_old_and_future(self):
        history=[{'startDate':'2026-08-03','tokens':900}, {'startDate':'2026-08-04','tokens':10},
                 {'startDate':'2026-09-02','tokens':20}, {'startDate':'2026-09-03','tokens':900}]
        data=self.build(replace(source(),usage={'dailyUsageBuckets':history}))
        self.assertEqual(sum(data.usage_buckets), 30)
        self.assertEqual(data.usage_buckets[0], 10)
        self.assertEqual(data.usage_buckets[-1], 20)

    def test_duplicate_or_malformed_bucket_is_rejected(self):
        for history in ([{'startDate':'2026-09-02','tokens':20}]*2, [{'startDate':'bad','tokens':2}],
                        [{'startDate':'2026-09-02','tokens':None}], [{'startDate':'2026-09-02','tokens':-1}]):
            with self.assertRaises(DataUnavailable):
                self.build(replace(source(),usage={'dailyUsageBuckets':history}))

    def test_snapshot_clock_is_fresh_but_task_event_time_is_preserved(self):
        app=replace(source(),threads=({'id':'thread','name':'task','cwd':'/project','updatedAt':100},))
        state={'version':1,'threads':{'thread':{'session_id':'thread','status':'waiting','updated_at':100}}}
        data=validated_snapshot(app,state,NOW)
        self.assertEqual(data.updated_at_epoch,int(NOW.timestamp()))
        self.assertEqual(data.tasks[0].updated_at_epoch,100)

    def test_missing_task_name_does_not_display_prompt_preview(self):
        app=replace(source(),threads=({'id':'thread','name':None,'preview':'private prompt body','cwd':'/project','updatedAt':100},))
        state={'version':1,'threads':{'thread':{'session_id':'thread','status':'running','updated_at':100}}}
        data=validated_snapshot(app,state,NOW)
        self.assertEqual(data.tasks[0].conversation,'未命名任务')


if __name__ == '__main__':
    unittest.main()
