"""Pure state and mapping logic for Codex-to-e-ink synchronization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


class DisplayStatus(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    QUEUED = "queued"
    FAILED = "failed"


@dataclass(frozen=True)
class DisplayTask:
    thread_id: str
    project: str
    conversation: str
    status: DisplayStatus
    updated_at_epoch: int


@dataclass(frozen=True)
class DashboardSnapshot:
    remaining_percent: int
    used_percent: int
    reset_at_epoch: int
    usage_buckets: tuple[int, ...]
    lifetime_tokens: int
    updated_at_epoch: int
    tasks: tuple[DisplayTask, ...]
    account_label: str = "账号"
    plan_label: str = "Pro 20x"


_EVENT_STATUS = {
    "SessionStart": "queued",
    "UserPromptSubmit": "running",
    "PermissionRequest": "waiting",
    "Stop": "waiting",
    "SessionEnd": "ended",
}

_DISPLAY_STATUS = {
    "running": DisplayStatus.RUNNING,
    "waiting": DisplayStatus.WAITING,
    "queued": DisplayStatus.QUEUED,
    "failed": DisplayStatus.FAILED,
}


def empty_hook_state() -> dict[str, Any]:
    return {"version": 1, "threads": {}}


def apply_hook_event(state: Mapping[str, Any], event: Mapping[str, Any], now: int) -> dict[str, Any]:
    """Apply one official Codex hook event while retaining metadata only."""
    result = deepcopy(dict(state)) if state else empty_hook_state()
    result["version"] = 1
    result.setdefault("threads", {})
    session_id = str(event.get("session_id") or "").strip()
    event_name = str(event.get("hook_event_name") or "").strip()
    if not session_id or event_name not in _EVENT_STATUS:
        return result

    previous = result["threads"].get(session_id, {})
    record = {
        "session_id": session_id,
        "turn_id": str(event.get("turn_id") or previous.get("turn_id") or ""),
        "cwd": str(event.get("cwd") or previous.get("cwd") or ""),
        "status": _EVENT_STATUS[event_name],
        "event": event_name,
        "updated_at": int(now),
    }
    result["threads"][session_id] = record
    return result


def _dedupe_threads(threads: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    deduped: dict[str, Mapping[str, Any]] = {}
    for thread in threads:
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            continue
        if thread_id not in deduped or int(thread.get("updatedAt") or 0) > int(deduped[thread_id].get("updatedAt") or 0):
            deduped[thread_id] = thread
    return deduped


def _rate_snapshot(rates: Mapping[str, Any]) -> Mapping[str, Any]:
    by_id = rates.get("rateLimitsByLimitId") or {}
    if isinstance(by_id, Mapping) and isinstance(by_id.get("codex"), Mapping):
        return by_id["codex"]
    fallback = rates.get("rateLimits") or {}
    return fallback if isinstance(fallback, Mapping) else {}


def build_dashboard_snapshot(
    *,
    hook_state: Mapping[str, Any],
    threads: Iterable[Mapping[str, Any]],
    rates: Mapping[str, Any],
    usage: Mapping[str, Any],
    now: datetime,
    account: Mapping[str, Any] | None = None,
) -> DashboardSnapshot:
    metadata = _dedupe_threads(threads)
    rate = _rate_snapshot(rates)
    primary = rate.get("primary") or {}
    used = max(0, min(100, round(float(primary.get("usedPercent") or 0))))
    reset_at = int(primary.get("resetsAt") or 0)

    summary = usage.get("summary") or {}
    buckets = usage.get("dailyUsageBuckets") or []
    usage_values = tuple(max(0, int(bucket.get("tokens") or 0)) for bucket in buckets[-46:] if isinstance(bucket, Mapping))
    lifetime_tokens = int(summary.get("lifetimeTokens") or 0)

    account_record = (account or {}).get("account") or {}
    account_label = str(account_record.get("email") or "账号")
    plan_type = str(account_record.get("planType") or "unknown")
    plan_labels = {
        "pro": "Pro 20x",
        "plus": "Plus",
        "free": "Free",
        "team": "Team",
        "business": "Business",
        "enterprise": "Enterprise",
    }
    plan_label = plan_labels.get(plan_type, plan_type.replace("_", " ").title() if plan_type != "unknown" else "订阅类型")

    active_records = [
        record
        for record in (hook_state.get("threads") or {}).values()
        if isinstance(record, Mapping)
        and record.get("status") in _DISPLAY_STATUS
        and str(record.get("session_id") or "") in metadata
    ]
    active_records.sort(key=lambda record: int(record.get("updated_at") or 0), reverse=True)
    tasks = []
    for record in active_records[:3]:
        thread_id = str(record.get("session_id") or "")
        meta = metadata.get(thread_id, {})
        cwd = str(meta.get("cwd") or record.get("cwd") or "")
        project = Path(cwd).name if cwd else "Codex"
        conversation = str(meta.get("name") or meta.get("preview") or thread_id[:8] or "Codex 任务")
        tasks.append(
            DisplayTask(
                thread_id=thread_id,
                project=project,
                conversation=conversation,
                status=_DISPLAY_STATUS[str(record.get("status"))],
                updated_at_epoch=int(record.get("updated_at") or meta.get("updatedAt") or 0),
            )
        )

    latest_source_time = max((task.updated_at_epoch for task in tasks), default=int(now.timestamp()))
    return DashboardSnapshot(
        remaining_percent=100 - used,
        used_percent=used,
        reset_at_epoch=reset_at,
        usage_buckets=usage_values,
        lifetime_tokens=lifetime_tokens,
        updated_at_epoch=latest_source_time,
        tasks=tuple(tasks),
        account_label=account_label,
        plan_label=plan_label,
    )
