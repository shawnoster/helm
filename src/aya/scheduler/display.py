"""Scheduler display and formatting — human-readable output for CLI."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import cast

from .storage import (
    _alerts_file,
    _atomic_write,
    _file_lock,
    _find,
    _load_alerts_unlocked,
    _new_id,
    load_alerts,
)
from .time_utils import _get_local_tz
from .types import (
    STATUS_ACTIVE,
    STATUS_DELIVERED,
    STATUS_DISMISSED,
    STATUS_PENDING,
    STATUS_SNOOZED,
    TYPE_EVENT,
    TYPE_RECURRING,
    TYPE_REMINDER,
    TYPE_WATCH,
    AlertDetails,
    AlertItem,
    GithubPrState,
    JiraQueryState,
    JiraTicketState,
    PendingResult,
    SchedulerItem,
    SchedulerStatus,
    WatchState,
    _alerts_data,
)

logger = logging.getLogger(__name__)

# ── filter helpers (used internally) ─────────────────────────────────────────


def _items_of_type(items: list[SchedulerItem], *types: str) -> list[SchedulerItem]:
    """Filter items by type."""
    return [i for i in items if i.get("type") in types]


def _items_with_status(items: list[SchedulerItem], *statuses: str) -> list[SchedulerItem]:
    """Filter items by status."""
    return [i for i in items if i.get("status") in statuses]


def _unseen(alerts: list[AlertItem]) -> list[AlertItem]:
    """Filter unseen alerts."""
    return [a for a in alerts if not a.get("seen")]


# ── alert creation ───────────────────────────────────────────────────────────


def _create_alert(
    source_item_id: str, message: str, details: AlertDetails, now: datetime
) -> AlertItem:
    """Create an alert dict with standard fields."""
    return {
        "id": _new_id(),
        "source_item_id": source_item_id,
        "created_at": now.isoformat(),
        "message": message,
        "details": details,
        "seen": False,
    }


# ── watch alert formatting ──────────────────────────────────────────────────


def _format_watch_alert(item: SchedulerItem, state: WatchState) -> str:
    """Format a human-readable alert message from watch state change."""
    provider = item.get("provider", "")
    base = item.get("message", "Watch triggered")

    if provider == "github-pr":
        gh_state = cast(GithubPrState, state)
        if gh_state["merged"]:
            return f"{base} — MERGED"
        if gh_state["has_approval"]:
            approvers = [r["user"] for r in gh_state["reviews"] if r["state"] == "APPROVED"]
            return f"{base} — APPROVED by {', '.join(approvers)}"
        return f"{base} — state changed"

    if provider == "jira-query":
        jq_state = cast(JiraQueryState, state)
        new_issues = jq_state["issues"][:3]
        keys = ", ".join(i["key"] for i in new_issues)
        return f"{base} — new: {keys}" if keys else f"{base} — results changed"

    if provider == "jira-ticket":
        jt_state = cast(JiraTicketState, state)
        return f"{base} — now: {jt_state['status']}"

    return base


# ── formatting ───────────────────────────────────────────────────────────────


def format_pending(pending: PendingResult) -> str:
    """Format pending items as human-readable text for session injection."""
    lines: list[str] = []
    alerts = pending.get("alerts", [])
    crons = pending.get("session_crons", [])
    suppressed = pending.get("suppressed_crons", [])

    if alerts:
        lines.append(f"\U0001f4cb {len(alerts)} pending alert(s):")
        now = datetime.now(_get_local_tz())
        for a in alerts:
            created = datetime.fromisoformat(a["created_at"])
            delta = now - created
            if delta.total_seconds() < 3600:
                ago = f"{int(delta.total_seconds() / 60)} min ago"
            elif delta.total_seconds() < 86400:
                ago = f"{int(delta.total_seconds() / 3600)}h ago"
            else:
                ago = f"{int(delta.total_seconds() / 86400)}d ago"
            lines.append(f"  \u2022 {a['message'][:70]} ({ago})")

    if crons:
        lines.append(f"\n\u23f0 {len(crons)} session cron(s) to register:")
        for c in crons:
            cron_id = c["id"][:12]
            cron_expr = c["cron"]
            cron_msg = c.get("message", c.get("prompt", ""))[:50]
            meta_parts = []
            if c.get("idle_back_off"):
                meta_parts.append(f"idle-back-off={c['idle_back_off']}")
            if c.get("only_during"):
                meta_parts.append(f"only-during={c['only_during']}")
            meta = f"  [{', '.join(meta_parts)}]" if meta_parts else ""
            lines.append(f'  \u2022 {cron_id}: "{cron_expr}" \u2014 {cron_msg}{meta}')

    if suppressed:
        lines.append(f"\n\U0001f515 {len(suppressed)} cron(s) suppressed:")
        for entry in suppressed:
            c = entry["item"]
            reason = entry["reason"]
            cron_id = c["id"][:12]
            cron_msg = c.get("message", c.get("prompt", ""))[:50]
            lines.append(f"  \u2022 {cron_id}: {cron_msg} ({reason})")

    if not alerts and not crons and not suppressed:
        lines.append("No pending items.")

    return "\n".join(lines)


def format_scheduler_status(status: SchedulerStatus) -> str:
    """Format scheduler status as human-readable text."""
    lines: list[str] = []
    now = datetime.now(_get_local_tz())

    watches = status["active_watches"]
    if watches:
        lines.append(f"\U0001f441  {len(watches)} active watch(es):")
        for w in watches:
            provider = w.get("provider", "?")
            last = w.get("last_checked_at")
            interval = w.get("poll_interval_minutes", 30)
            if last:
                last_dt = datetime.fromisoformat(last)
                next_dt = last_dt + timedelta(minutes=interval)
                last_str = last_dt.strftime("%H:%M")
                next_str = next_dt.strftime("%H:%M")
                timing = f"last: {last_str}, next: ~{next_str}"
            else:
                timing = "never polled"
            lines.append(f"  \u2022 [{provider}] {w.get('message', '?')[:50]} ({timing})")
    else:
        lines.append("\U0001f441  No active watches")

    reminders = status["pending_reminders"]
    if reminders:
        lines.append(f"\n\u23f3 {len(reminders)} pending reminder(s):")
        for r in reminders:
            due = datetime.fromisoformat(r["due_at"])
            overdue = " \u26a0\ufe0f OVERDUE" if due <= now else ""
            due_str = due.strftime("%a %b %d %I:%M %p")
            lines.append(f"  \u2022 {r['message'][:50]} \u2014 due {due_str}{overdue}")

    crons = status["session_crons"]
    if crons:
        lines.append(f"\n\U0001f504 {len(crons)} session cron(s):")
        for c in crons:
            cron_msg = c.get("message", c.get("prompt", "?"))[:50]
            lines.append(f'  \u2022 "{c.get("cron", "?")}" \u2014 {cron_msg}')

    unseen = status["unseen_alerts"]
    if unseen:
        lines.append(f"\n\U0001f514 {len(unseen)} unseen alert(s):")
        for a in unseen:
            lines.append(f"  \u2022 {a['message'][:60]}")

    deliveries = status["recent_deliveries"]
    if deliveries:
        lines.append(f"\n\U0001f4ec {len(deliveries)} delivery(ies) in last 24h:")
        for d in deliveries:
            by = d.get("delivered_by", "?")
            at = datetime.fromisoformat(d["delivered_at"]).strftime("%H:%M")
            lines.append(f"  \u2022 {d['message'][:45]} \u2192 {by} at {at}")

    lines.append(f"\n\U0001f4ca {status['total_items']} items, {status['total_alerts']} alerts")

    return "\n".join(lines)


def _display_items(items: list[SchedulerItem]) -> None:
    """Pretty-print scheduler items grouped by type."""
    if not items:
        return

    now = datetime.now(_get_local_tz())

    status_icons = {
        STATUS_PENDING: "\u23f3",
        STATUS_ACTIVE: "\u2705",
        STATUS_SNOOZED: "\U0001f4a4",
        STATUS_DELIVERED: "\U0001f4ec",
        STATUS_DISMISSED: "\u2717",
    }

    for item_type in (TYPE_REMINDER, TYPE_WATCH, TYPE_RECURRING, TYPE_EVENT):
        typed = _items_of_type(items, item_type)
        if not typed:
            continue

        print(f"\n{item_type.upper()}:")  # noqa: T201
        for i in typed:
            status = i.get("status", STATUS_ACTIVE)
            icon = status_icons.get(status, "\u2022")
            tags_str = f" [{', '.join(i['tags'])}]" if i.get("tags") else ""
            message = i.get("message", "")

            if item_type == TYPE_REMINDER:
                due = datetime.fromisoformat(i["due_at"])
                due_str = due.strftime("%a %b %d, %I:%M %p")
                is_overdue = "\u26a0\ufe0f" if due <= now and status == STATUS_PENDING else ""
                print(  # noqa: T201
                    f"  {icon} {message}{tags_str} \u2014 due {due_str} {is_overdue}".rstrip()
                )
            elif item_type == TYPE_WATCH:
                provider = i.get("provider", "?")
                interval = i.get("poll_interval_minutes", "?")
                last = i.get("last_checked_at")
                last_checked = datetime.fromisoformat(last).strftime("%H:%M") if last else "never"
                print(  # noqa: T201
                    f"  {icon} {message}{tags_str} \u2014 {provider} (every {interval}m, "
                    f"checked {last_checked})"
                )
            elif item_type == TYPE_RECURRING:
                cron = i.get("cron", "?")
                session_flag = " [session]" if i.get("session_required") else ""
                print(f"  {icon} {message}{tags_str} \u2014 {cron}{session_flag}")  # noqa: T201
            elif item_type == TYPE_EVENT:
                trigger = i.get("trigger", "?")
                print(f"  {icon} {message}{tags_str} \u2014 on {trigger}")  # noqa: T201


def show_alerts(mark_seen: bool = False) -> list[AlertItem]:
    """Show and optionally clear alerts. Returns unseen alerts."""
    if mark_seen:
        with _file_lock():
            alerts = _load_alerts_unlocked()
            unseen = [a for a in alerts if not a.get("seen")]
            if unseen:
                for a in alerts:
                    a["seen"] = True
                _atomic_write(_alerts_file(), _alerts_data(alerts))
        return unseen

    alerts = load_alerts()
    return [a for a in alerts if not a.get("seen")]


def dismiss_alert(alert_id: str) -> AlertItem:
    """Dismiss an alert by ID (prefix match). Returns the dismissed alert."""
    with _file_lock():
        alerts = _load_alerts_unlocked()
        alert = _find(alerts, alert_id)
        if not alert:
            raise ValueError(f"Alert {alert_id} not found.")
        alert["seen"] = True
        _atomic_write(_alerts_file(), _alerts_data(alerts))
    return alert
