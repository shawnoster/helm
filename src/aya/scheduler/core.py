"""Scheduler core operations — CRUD, poll, tick, pending, and programmatic API."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta
from typing import Any, cast

from .display import _create_alert, _format_watch_alert
from .providers import _evaluate_auto_remove, poll_watch
from .storage import (
    _alerts_file,
    _atomic_write,
    _file_lock,
    _find,
    _load_alerts_unlocked,
    _load_items_unlocked,
    _new_id,
    _parse_tags,
    _scheduler_file,
    claim_alert,
    get_instance_id,
    get_unseen_alerts,
    is_session_active,
    load_alerts,
    load_items,
    sweep_stale_claims,
)
from .time_utils import (
    _ALERT_MAX_AGE_DAYS,
    _get_local_tz,
    is_idle,
    is_within_work_hours,
    parse_due,
    parse_duration,
    parse_work_hours,
)
from .types import (
    SEVERITY_ACTIONABLE,
    SEVERITY_ORDER,
    AlertDetails,
    AlertItem,
    AlertSeverity,
    CiChecksConfig,
    GithubPrConfig,
    JiraQueryConfig,
    JiraTicketConfig,
    PendingResult,
    SchedulerItem,
    SchedulerStatus,
    SuppressedCron,
    _alerts_data,
    _scheduler_data,
)

logger = logging.getLogger(__name__)


def _dt_now(tz: object) -> datetime:
    """Resolve ``datetime.now`` through the package namespace.

    Tests that ``patch("aya.scheduler.datetime")`` expect the mock to affect
    every ``datetime.now()`` call in the old monolithic module.  After the
    split, ``core.py`` has its own ``datetime`` reference so the patch no
    longer propagates.  This helper looks up ``datetime`` on the *package*
    first, falling back to the stdlib class if the attribute is absent or
    hasn't been patched.
    """
    pkg = sys.modules.get("aya.scheduler")
    dt_cls = getattr(pkg, "datetime", None) if pkg else None
    if dt_cls is None or dt_cls is datetime:
        return datetime.now(tz)  # type: ignore[arg-type]
    return dt_cls.now(tz)  # type: ignore[no-any-return]


# ── CRUD ─────────────────────────────────────────────────────────────────────


def add_reminder(message: str, due_text: str, tags: str = "") -> SchedulerItem:
    """Add a one-shot reminder. Returns the created item."""
    now = _dt_now(_get_local_tz())
    due = parse_due(due_text, now)
    item: SchedulerItem = {
        "id": _new_id(),
        "type": "reminder",
        "status": "pending",
        "created_at": now.isoformat(),
        "message": message,
        "tags": _parse_tags(tags),
        "session_required": False,
        "due_at": due.isoformat(),
        "delivered_at": None,
        "snoozed_until": None,
    }
    with _file_lock():
        items = _load_items_unlocked()
        items.append(item)
        _atomic_write(_scheduler_file(), _scheduler_data(items))
    return item


def add_watch(
    provider: str,
    target: str,
    message: str,
    tags: str = "",
    condition: str = "",
    interval: int = 30,
    remove_when: str = "",
) -> SchedulerItem:
    """Add a condition-based watch. Returns the created item."""
    now = _dt_now(_get_local_tz())
    watch_config: GithubPrConfig | JiraQueryConfig | JiraTicketConfig | CiChecksConfig

    if provider == "github-pr":
        m = re.match(r"([^/]+)/([^#]+)#(\d+)", target)
        if not m:
            raise ValueError("Format: owner/repo#123")
        watch_config = {"owner": m.group(1), "repo": m.group(2), "pr": int(m.group(3))}
        condition = condition or "approved_or_merged"
        if interval == 30:
            interval = 5
    elif provider == "ci-checks":
        m = re.match(r"([^/]+)/([^#]+)#(\d+)", target)
        if not m:
            raise ValueError("Format: owner/repo#123")
        watch_config = {
            "owner": m.group(1),
            "repo": m.group(2),
            "pr": int(m.group(3)),
            "branch": "",
        }
        condition = condition or "checks_failed"
        if interval == 30:
            interval = 1
    elif provider == "jira-query":
        watch_config = {"jql": target}
        condition = condition or "new_results"
    elif provider == "jira-ticket":
        watch_config = {"ticket": target.upper()}
        condition = condition or "status_changed"
    else:
        raise ValueError(f"Unknown provider: {provider}")

    item: SchedulerItem = {
        "id": _new_id(),
        "type": "watch",
        "status": "active",
        "created_at": now.isoformat(),
        "message": message,
        "tags": _parse_tags(tags),
        "session_required": False,
        "provider": provider,
        "watch_config": watch_config,
        "condition": condition,
        "poll_interval_minutes": interval,
        "last_checked_at": None,
        "last_state": None,
        "remove_when": remove_when,
    }
    with _file_lock():
        items = _load_items_unlocked()
        items.append(item)
        _atomic_write(_scheduler_file(), _scheduler_data(items))
    return item


def add_recurring(
    message: str,
    cron: str,
    prompt: str = "",
    tags: str = "",
    idle_back_off: str = "",
    only_during: str = "",
) -> SchedulerItem:
    """Add a persistent recurring session job. Returns the created item.

    Args:
        message:       Short human-readable label shown in schedule list.
        cron:          Standard five-field cron expression (e.g. "27 * * * *").
        prompt:        Instruction delivered to Claude each time the cron fires.
        tags:          Comma-separated tags for filtering.
        idle_back_off: Suppress this cron when the session has been idle for
                       longer than this duration (e.g. "30m", "1h").  Empty
                       string disables idle suppression.
        only_during:   Only allow this cron to fire within this time window
                       (e.g. "08:00-18:00").  Empty string disables the window
                       check.
    """
    # Validate optional fields eagerly so callers get clear errors.
    if idle_back_off:
        parse_duration(idle_back_off)  # raises ValueError on bad input
    if only_during:
        parse_work_hours(only_during)  # raises ValueError on bad input

    now = _dt_now(_get_local_tz())
    item: SchedulerItem = {
        "id": _new_id(),
        "type": "recurring",
        "status": "active",
        "created_at": now.isoformat(),
        "message": message,
        "tags": _parse_tags(tags),
        "session_required": True,
        "cron": cron,
        "prompt": prompt,
    }
    if idle_back_off:
        item["idle_back_off"] = idle_back_off
    if only_during:
        item["only_during"] = only_during
    with _file_lock():
        items = _load_items_unlocked()
        items.append(item)
        _atomic_write(_scheduler_file(), _scheduler_data(items))
    return item


def add_seed_alert(
    intent: str,
    opener: str,
    context_summary: str,
    open_questions: list[str],
    from_label: str,
    packet_id: str = "",
) -> AlertItem:
    """Persist a seed packet as an unseen alert so it surfaces via pending on next session start."""
    now = _dt_now(_get_local_tz())
    detail_lines = [f"**From:** {from_label}", f"**Opener:** {opener}"]
    if context_summary:
        detail_lines.append(f"**Context:** {context_summary}")
    if open_questions:
        detail_lines.append("**Open questions:**")
        detail_lines.extend(f"  \u2022 {q}" for q in open_questions)
    alert = _create_alert(
        source_item_id=packet_id or _new_id(),
        message=f"Seed from {from_label}: {intent}",
        details={
            "type": "seed",
            "intent": intent,
            "opener": opener,
            "context_summary": context_summary,
            "open_questions": open_questions,
            "from_label": from_label,
            "body": "\n".join(detail_lines),
        },
        now=now,
    )
    with _file_lock():
        alerts = _load_alerts_unlocked()
        alerts.append(alert)
        _atomic_write(_alerts_file(), _alerts_data(alerts))
    return alert


# ── operations ───────────────────────────────────────────────────────────────


def list_items(
    show_all: bool = False,
    item_type: str | None = None,
) -> list[SchedulerItem]:
    """Return filtered list of scheduler items."""
    items = load_items()
    if item_type:
        items = [i for i in items if i["type"] == item_type]
    if not show_all:
        items = [i for i in items if i["status"] in ("pending", "active", "snoozed")]
    return items


def check_due() -> tuple[list[SchedulerItem], list[AlertItem]]:
    """Check for due reminders and unseen alerts. Returns (due_items, unseen_alerts).

    Holds exclusive lock when snooze->pending transitions require a write.
    """
    with _file_lock():
        items = _load_items_unlocked()
        now = _dt_now(_get_local_tz())
        modified = False
        due_items = []

        for item in items:
            if item.get("type") != "reminder" or item.get("status") not in ("pending", "snoozed"):
                continue
            snoozed_until = item.get("snoozed_until")
            if item.get("status") == "snoozed" and snoozed_until:
                snooze_end = datetime.fromisoformat(snoozed_until)
                if snooze_end > now:
                    continue
                item["status"] = "pending"
                item["snoozed_until"] = None
                modified = True
            due_at = item.get("due_at", "")
            if not due_at:
                continue
            due = datetime.fromisoformat(due_at)
            if due <= now:
                due_items.append(item)

        if modified:
            _atomic_write(_scheduler_file(), _scheduler_data(items))

        unseen = [a for a in _load_alerts_unlocked() if not a.get("seen")]
    return due_items, unseen


def dismiss_item(item_id: str) -> SchedulerItem:
    """Dismiss an item by ID (prefix match). Returns the dismissed item."""
    with _file_lock():
        items = _load_items_unlocked()
        item = _find(items, item_id)
        if not item:
            raise ValueError(f"Item {item_id} not found.")
        item["status"] = "dismissed"
        if item["type"] == "reminder":
            item["delivered_at"] = _dt_now(_get_local_tz()).isoformat()
        _atomic_write(_scheduler_file(), _scheduler_data(items))
    return item


def snooze_item(item_id: str, until_text: str) -> tuple[SchedulerItem, datetime]:
    """Snooze a reminder. Returns (item, snooze_until_datetime)."""
    with _file_lock():
        items = _load_items_unlocked()
        item = _find(items, item_id)
        if not item:
            raise ValueError(f"Item {item_id} not found.")
        now = _dt_now(_get_local_tz())
        snooze_until = parse_due(until_text, now)
        item["status"] = "snoozed"
        item["snoozed_until"] = snooze_until.isoformat()
        _atomic_write(_scheduler_file(), _scheduler_data(items))
    return item, snooze_until


# ── poll ─────────────────────────────────────────────────────────────────────


def run_poll(quiet: bool = False) -> None:
    """Run one poll cycle — check all watches and due reminders.

    Holds a single exclusive lock for the entire load->poll->save cycle
    to prevent interleaving with other CLI commands or sessions.
    """
    with _file_lock():
        items = _load_items_unlocked()
        alerts = _load_alerts_unlocked()
        now = _dt_now(_get_local_tz())
        items_modified = False
        alerts_modified = False
        existing_sources = {a["source_item_id"] for a in alerts if not a.get("seen")}

        active_watches = [
            i
            for i in items
            if i["type"] == "watch" and i["status"] == "active" and not i.get("session_required")
        ]
        logger.debug("poll: checking %d active watches", len(active_watches))

        for item in items:
            if (
                item["type"] == "watch"
                and item["status"] == "active"
                and not item.get("session_required")
            ):
                last = item.get("last_checked_at")
                interval = item.get("poll_interval_minutes", 30)
                if last:
                    next_check = datetime.fromisoformat(last) + timedelta(minutes=interval)
                    if now < next_check:
                        continue

                new_state, changed = poll_watch(item)
                provider = item.get("provider", "unknown")
                logger.debug(
                    "poll: watch %s provider=%s changed=%s",
                    item["id"][:8],
                    provider,
                    changed,
                )
                if new_state is not None:
                    item["last_checked_at"] = now.isoformat()
                    item["last_state"] = new_state
                    items_modified = True

                    if changed:
                        alert = _create_alert(
                            source_item_id=item["id"],
                            message=_format_watch_alert(item, new_state),
                            # WatchState fields overlap with AlertDetails (total=False);
                            # safe because AlertDetails accepts any subset of its keys.
                            details=cast(AlertDetails, new_state),
                            now=now,
                        )
                        alerts.append(alert)
                        alerts_modified = True
                        logger.info("poll: generated alert for watch %s", item["id"][:8])
                        if not quiet:
                            pass

                    if _evaluate_auto_remove(item, new_state):
                        item["status"] = "dismissed"
                        items_modified = True
                        if not quiet:
                            pass

                elif not quiet:
                    pass

            elif item["type"] == "reminder" and item["status"] == "pending":
                due = datetime.fromisoformat(item["due_at"])
                if due <= now:
                    if item["id"] not in existing_sources:
                        alert = _create_alert(
                            source_item_id=item["id"],
                            message=f"Reminder due: {item['message']}",
                            details={"due_at": item["due_at"]},
                            now=now,
                        )
                        alerts.append(alert)
                        alerts_modified = True
                        if not quiet:
                            pass

        due_reminders = [
            i
            for i in items
            if i["type"] == "reminder"
            and i["status"] == "pending"
            and datetime.fromisoformat(i.get("due_at", "9999-12-31")) <= now
        ]
        if due_reminders:
            logger.info("poll: %d due reminders found", len(due_reminders))

        if items_modified:
            _atomic_write(_scheduler_file(), _scheduler_data(items))
        if alerts_modified:
            _atomic_write(_alerts_file(), _alerts_data(alerts))


def run_tick(quiet: bool = False) -> dict[str, Any]:
    """Run one scheduler tick — poll watches, check reminders, sweep stale claims.

    This is the canonical entry point for system cron:
        */5 * * * * aya schedule tick --quiet

    When an active REPL session is detected (via session lock + recent
    activity), polling is skipped entirely — no new alerts are created.
    The REPL pulls existing pending alerts at natural breakpoints via
    ``aya schedule pending``.

    Returns a summary dict: {"claims_swept": N, "alerts_expired": N, "session_active": bool}
    """
    result: dict[str, Any] = {}
    session_active = is_session_active()

    if session_active:
        if not quiet:
            logger.info("Active session detected — skipping poll, delivery via REPL")
        result["polls_skipped"] = True
    else:
        logger.info("tick: starting poll cycle")
        run_poll(quiet=quiet)

    swept = sweep_stale_claims()
    expired = expire_old_alerts()
    result["claims_swept"] = swept
    result["alerts_expired"] = expired
    result["session_active"] = session_active
    logger.info(
        "tick: complete — swept=%d claims, expired=%d alerts, session_active=%s",
        swept,
        expired,
        session_active,
    )
    return result


def expire_old_alerts(max_age_days: int = _ALERT_MAX_AGE_DAYS) -> int:
    """Remove alerts older than max_age_days. Returns count removed."""
    with _file_lock():
        alerts = _load_alerts_unlocked()
        if not alerts:
            return 0
        now = _dt_now(_get_local_tz())
        cutoff = now - timedelta(days=max_age_days)
        original_count = len(alerts)
        alerts = [a for a in alerts if datetime.fromisoformat(a["created_at"]) > cutoff]
        removed = original_count - len(alerts)
        if removed > 0:
            _atomic_write(_alerts_file(), _alerts_data(alerts))
        return removed


# ── pending ──────────────────────────────────────────────────────────────────


def _passes_severity_filter(
    alert: AlertItem, min_severity: AlertSeverity = SEVERITY_ACTIONABLE
) -> bool:
    """Return True if an alert's severity meets the minimum threshold.

    Severity ordering: actionable > info > heartbeat.
    An alert passes if its severity index <= min_severity index
    (lower index = higher priority).
    """
    alert_sev = alert.get("severity", SEVERITY_ACTIONABLE)
    try:
        alert_idx = SEVERITY_ORDER.index(alert_sev)
    except ValueError:
        alert_idx = 0  # unknown severity treated as actionable
    try:
        min_idx = SEVERITY_ORDER.index(min_severity)
    except ValueError:
        min_idx = 0
    return alert_idx <= min_idx


def get_pending(
    instance_id: str | None = None,
    min_severity: AlertSeverity = SEVERITY_ACTIONABLE,
) -> PendingResult:
    """Get pending items for a session — alerts to deliver + session crons to register.

    This is the SessionStart hook entry point:
        aya schedule pending --format text

    Claims each alert it returns so other sessions don't re-deliver.

    Args:
        instance_id: Override instance identifier (default: auto-detect).
        min_severity: Minimum severity to include. ``"actionable"`` (default)
            returns only actionable alerts. ``"heartbeat"`` returns everything.

    Returns:
        {
            "alerts": [list of unclaimed alert dicts],
            "session_crons": [list of session-required recurring items that are active],
            "suppressed_crons": [list of {"item": ..., "reason": str} for crons skipped
                                 due to idle back-off or outside work hours],
            "instance_id": str,
        }
    """
    instance_id = instance_id or get_instance_id()
    logger.debug("pending: checking for instance=%s, min_severity=%s", instance_id, min_severity)
    unseen = get_unseen_alerts()

    # Claim and collect deliverable alerts (filtered by severity)
    deliverable = []
    claimed_ids: set[str] = set()
    for alert in unseen:
        if not _passes_severity_filter(alert, min_severity):
            continue
        if claim_alert(alert["id"], instance_id):
            deliverable.append(alert)
            claimed_ids.add(alert["id"])

    logger.info(
        "pending: %d unseen alerts, %d claimed for delivery",
        len(unseen),
        len(deliverable),
    )

    # Stamp delivery receipts on claimed alerts
    if claimed_ids:
        now = _dt_now(_get_local_tz())
        with _file_lock():
            alerts = _load_alerts_unlocked()
            for a in alerts:
                if a["id"] in claimed_ids:
                    a["delivered_at"] = now.isoformat()
                    a["delivered_by"] = instance_id
            _atomic_write(_alerts_file(), _alerts_data(alerts))

    session_crons, suppressed_crons = get_session_crons()
    logger.debug(
        "pending: %d session crons active, %d suppressed",
        len(session_crons),
        len(suppressed_crons),
    )

    return {
        "alerts": deliverable,
        "session_crons": session_crons,
        "suppressed_crons": suppressed_crons,
        "instance_id": instance_id,
    }


def get_session_crons() -> tuple[list[SchedulerItem], list[SuppressedCron]]:
    """Return active session crons, filtered by idle back-off and work hours.

    Returns (active_crons, suppressed_crons) without any alert side effects.
    Use this when you only need crons — get_pending() also claims alerts.
    """
    now = _dt_now(_get_local_tz())
    items = load_items()
    active = [
        i
        for i in items
        if i.get("type") == "recurring"
        and i.get("status", "active") == "active"
        and i.get("session_required")
    ]
    session_crons: list[SchedulerItem] = []
    suppressed_crons: list[SuppressedCron] = []
    for item in active:
        only_during = item.get("only_during", "")
        idle_back_off_str = item.get("idle_back_off", "")
        if only_during and not is_within_work_hours(only_during, now):
            reason = f"outside work hours ({only_during})"
            suppressed_crons.append(SuppressedCron(item=item, reason=reason))
            logger.debug("cron suppressed: %s — %s", item.get("message", "")[:40], reason)
        elif idle_back_off_str and is_idle(idle_back_off_str, now):
            reason = f"session idle (threshold: {idle_back_off_str})"
            suppressed_crons.append(SuppressedCron(item=item, reason=reason))
            logger.debug("cron suppressed: %s — %s", item.get("message", "")[:40], reason)
        else:
            session_crons.append(item)
    logger.debug(
        "session_crons: %d active, %d suppressed",
        len(session_crons),
        len(suppressed_crons),
    )
    return session_crons, suppressed_crons


# ── programmatic API ─────────────────────────────────────────────────────────


def get_due_reminders(now: datetime | None = None) -> list[SchedulerItem]:
    """Return pending reminders that are due. No side effects."""
    items = load_items()
    if now is None:
        now = _dt_now(_get_local_tz())
    due = []
    for item in items:
        if item.get("type") != "reminder" or item.get("status") not in ("pending", "snoozed"):
            continue
        try:
            snoozed_until = item.get("snoozed_until")
            if item.get("status") == "snoozed" and snoozed_until:
                if datetime.fromisoformat(snoozed_until) > now:
                    continue
            due_at = item.get("due_at", "")
            if due_at and datetime.fromisoformat(due_at) <= now:
                due.append(item)
        except ValueError:
            # Skip items with malformed timestamps
            continue
    return due


def get_upcoming_reminders(now: datetime | None = None, hours: int = 24) -> list[SchedulerItem]:
    """Return pending reminders due within N hours."""
    items = load_items()
    if now is None:
        now = _dt_now(_get_local_tz())
    horizon = now + timedelta(hours=hours)
    upcoming = []
    for item in items:
        if item.get("type") != "reminder" or item.get("status") not in ("pending", "snoozed"):
            continue
        try:
            reminder_due = datetime.fromisoformat(item["due_at"])
            if now < reminder_due <= horizon:
                upcoming.append(item)
        except ValueError:
            # Skip items with malformed timestamps
            continue
    upcoming.sort(key=lambda r: r["due_at"])
    return upcoming


def get_active_watches() -> list[SchedulerItem]:
    """Return all active watches."""
    return [i for i in load_items() if i["type"] == "watch" and i["status"] == "active"]


def get_scheduler_status() -> SchedulerStatus:
    """Return a structured overview of the scheduler state.

    Used by `aya schedule status` and `make assistant-status`.
    """
    items = load_items()
    alerts = load_alerts()
    now = _dt_now(_get_local_tz())

    active_watches = [i for i in items if i.get("type") == "watch" and i.get("status") == "active"]
    pending_reminders = [
        i
        for i in items
        if i.get("type") == "reminder" and i.get("status") in ("pending", "snoozed")
    ]
    session_crons = [
        i
        for i in items
        if (
            i.get("type") == "recurring"
            and i.get("status", "active") == "active"
            and i.get("session_required")
        )
    ]
    unseen_alerts = [a for a in alerts if not a.get("seen")]
    recent_deliveries = [
        a
        for a in alerts
        if a.get("delivered_at")
        and (now - datetime.fromisoformat(a["delivered_at"])).total_seconds() < 86400
    ]

    return {
        "active_watches": active_watches,
        "pending_reminders": pending_reminders,
        "session_crons": session_crons,
        "unseen_alerts": unseen_alerts,
        "recent_deliveries": recent_deliveries,
        "total_items": len(items),
        "total_alerts": len(alerts),
    }
