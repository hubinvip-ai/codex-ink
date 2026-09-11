"""Companion-only data validation. Legacy/demo rendering contracts stay intact."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

from tools.codex_status_core import build_dashboard_snapshot


class DataUnavailable(ValueError):
    pass


def validated_snapshot(app, hook_state, now, *, language="zh-CN"):
    rates = app.rates
    by_id = rates.get('rateLimitsByLimitId') or {}
    rate = by_id.get('codex') if isinstance(by_id, dict) else None
    if rate is None:
        rate = rates.get('rateLimits')
    if not isinstance(rate, dict):
        raise DataUnavailable('weekly_quota_unavailable')
    if rate.get('limitId') not in (None,'codex'):
        raise DataUnavailable('weekly_quota_unavailable')
    windows = [rate.get(slot) for slot in ('primary', 'secondary')]
    weekly = [w for w in windows if isinstance(w, dict) and type(w.get('windowDurationMins')) is int and w['windowDurationMins'] == 10080]
    if len(weekly) != 1:
        raise DataUnavailable('weekly_quota_unavailable')
    window = weekly[0]
    used, reset = window.get('usedPercent'), window.get('resetsAt')
    if type(used) not in (int, float) or not 0 <= used <= 100 or not math.isfinite(used):
        raise DataUnavailable('quota_invalid')
    if type(reset) is not int or not now.timestamp() < reset <= 10**12:
        raise DataUnavailable('quota_stale')
    account = app.account.get('account')
    if not isinstance(account, dict) or not isinstance(account.get('email'), str) or not account['email'].strip():
        raise DataUnavailable('account_unavailable')
    plans = {'free':'Free','plus':'Plus','pro':'Pro','team':'Team','business':'Business','enterprise':'Enterprise','edu':'Edu'}
    plan = account.get('planType')
    if not isinstance(plan, str) or plan not in plans:
        raise DataUnavailable('plan_unavailable')
    history = app.usage.get('dailyUsageBuckets')
    if not isinstance(history, list):
        raise DataUnavailable('usage_unavailable')
    start = now.date() - timedelta(days=29)
    slots = [0] * 30
    seen = set()
    for bucket in history:
        if not isinstance(bucket, dict) or not isinstance(bucket.get('startDate'), str):
            raise DataUnavailable('usage_invalid')
        try:
            day = date.fromisoformat(bucket['startDate'])
        except ValueError as error:
            raise DataUnavailable('usage_invalid') from error
        tokens = bucket.get('tokens')
        if day in seen or type(tokens) is not int or not 0 <= tokens <= 10**18:
            raise DataUnavailable('usage_invalid')
        seen.add(day)
        offset = (day - start).days
        if 0 <= offset < 30:
            slots[offset] = tokens
    threads=[]
    for thread in app.threads:
        if not isinstance(thread,dict):raise DataUnavailable('tasks_invalid')
        name=thread.get('name')
        record={key:thread.get(key) for key in ('id','cwd','updatedAt')}
        # The legacy mapper can use prompt preview as a missing-name fallback.
        # A desk display is authorized for task names, not conversation bodies.
        record['name']=name if isinstance(name,str) and name.strip() else ('Untitled task' if language == 'en' else '未命名任务')
        threads.append(record)
    snapshot = build_dashboard_snapshot(hook_state=hook_state, threads=threads,
        rates={'rateLimits':{'primary':window}}, usage={}, now=now, account=app.account)
    return replace(snapshot, usage_buckets=tuple(slots), plan_label=plans[plan],
                   updated_at_epoch=int(now.timestamp()))
