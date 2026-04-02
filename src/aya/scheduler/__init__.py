"""Unified scheduler — reminders, watches, recurring items, and events.

Persists across AI sessions via scheduler.json.
Out-of-session polling via systemd timer.

Usage (via CLI):
    aya schedule remind  --due "tomorrow 9am" -m "Check the PR"
    aya schedule watch   github-pr owner/repo#123 -m "PR approved"
    aya schedule watch   jira-query "project=CSD AND created>=-1d" -m "New CSD tickets"
    aya schedule watch   jira-ticket CSD-225 -m "Ticket status changed"
    aya schedule list    [--all] [--type TYPE]
    aya schedule check   [--format json]
    aya schedule dismiss <id>
    aya schedule snooze  <id> --until "in 1 hour"
    aya schedule poll    [--quiet]
    aya schedule alerts  [--format json]
"""

from __future__ import annotations

from datetime import datetime  # exposed for test monkeypatching
from typing import Any

# ── types & constants ────────────────────────────────────────────────────────
from .types import (
    ALERTS_SCHEMA_VERSION,
    CONDITION_APPROVED_OR_MERGED,
    CONDITION_MERGED,
    CONDITION_NEW_RESULTS,
    CONDITION_STATUS_CHANGED,
    PROVIDER_GITHUB_PR,
    PROVIDER_JIRA_QUERY,
    PROVIDER_JIRA_TICKET,
    SCHEDULER_SCHEMA_VERSION,
    STATUS_ACTIVE,
    STATUS_DELIVERED,
    STATUS_DISMISSED,
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_SNOOZED,
    TYPE_EVENT,
    TYPE_RECURRING,
    TYPE_REMINDER,
    TYPE_WATCH,
    AlertDetails,
    AlertItem,
    ClaimData,
    GithubPrConfig,
    GithubPrState,
    JiraQueryConfig,
    JiraQueryState,
    JiraTicketConfig,
    JiraTicketState,
    PendingResult,
    SchedulerItem,
    SchedulerStatus,
    SuppressedCron,
    WatchState,
    _alerts_data,
    _check_schema_version,
    _scheduler_data,
)

# ── time utilities ───────────────────────────────────────────────────────────
from .time_utils import (
    _ALERT_MAX_AGE_DAYS,
    _get_local_tz,
    _parse_time_component,
    get_last_activity,
    is_idle,
    is_within_work_hours,
    parse_due,
    parse_duration,
    parse_work_hours,
    record_activity,
)

# ── storage ──────────────────────────────────────────────────────────────────
from .storage import (
    _CLAIM_TTL_SECONDS,
    _activity_file,
    _alerts_file,
    _atomic_write,
    _claims_dir,
    _detect_harness,
    _file_lock,
    _find,
    _load_alerts_unlocked,
    _load_collection_unlocked,
    _load_items_unlocked,
    _lock_file,
    _locked_read,
    _new_id,
    _parse_tags,
    _scheduler_file,
    claim_alert,
    get_instance_id,
    get_unseen_alerts,
    load_alerts,
    load_items,
    save_alerts,
    save_items,
    sweep_stale_claims,
)

# ── providers ────────────────────────────────────────────────────────────────
from .providers import (
    WATCH_PROVIDERS,
    _CHANGE_DETECTORS,
    _check_github_pr,
    _check_jira_query,
    _check_jira_ticket,
    _detect_github_approved_or_merged,
    _detect_github_merged,
    _detect_jira_count_change,
    _detect_jira_new_results,
    _detect_jira_status_changed,
    _detect_json_diff,
    _evaluate_auto_remove,
    _get_jira_credentials,
    _run_gh,
    poll_watch,
)

# ── display ──────────────────────────────────────────────────────────────────
from .display import (
    _create_alert,
    _display_items,
    _format_watch_alert,
    _items_of_type,
    _items_with_status,
    _unseen,
    dismiss_alert,
    format_pending,
    format_scheduler_status,
    show_alerts,
)

# ── core operations ──────────────────────────────────────────────────────────
from .core import (
    add_recurring,
    add_reminder,
    add_seed_alert,
    add_watch,
    check_due,
    dismiss_item,
    expire_old_alerts,
    get_active_watches,
    get_due_reminders,
    get_pending,
    get_scheduler_status,
    get_session_crons,
    get_upcoming_reminders,
    list_items,
    run_poll,
    run_tick,
    snooze_item,
)

# ── Lazy module attrs ────────────────────────────────────────────────────────
# Lets tests monkeypatch these names via setattr on the package
# (e.g. monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", tmp_path / "s.json")).
#
# When code does `scheduler.SCHEDULER_FILE`, Python calls __getattr__ here,
# which lazily resolves via the accessor functions in storage.py.

_LAZY_ATTRS: dict[str, Any] = {
    "SCHEDULER_FILE": _scheduler_file,
    "ALERTS_FILE": _alerts_file,
    "ACTIVITY_FILE": _activity_file,
    "LOCK_FILE": lambda: _lock_file(),  # noqa: PLW0108 — forward ref
    "CLAIMS_DIR": lambda: _claims_dir(),  # noqa: PLW0108 — forward ref
    "LOCAL_TZ": _get_local_tz,
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_ATTRS:
        value = _LAZY_ATTRS[name]()
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Constants
    "TYPE_REMINDER",
    "TYPE_WATCH",
    "TYPE_RECURRING",
    "TYPE_EVENT",
    "STATUS_PENDING",
    "STATUS_ACTIVE",
    "STATUS_SNOOZED",
    "STATUS_DELIVERED",
    "STATUS_DISMISSED",
    "STATUS_DONE",
    "PROVIDER_GITHUB_PR",
    "PROVIDER_JIRA_QUERY",
    "PROVIDER_JIRA_TICKET",
    "SCHEDULER_SCHEMA_VERSION",
    "ALERTS_SCHEMA_VERSION",
    "CONDITION_APPROVED_OR_MERGED",
    "CONDITION_MERGED",
    "CONDITION_NEW_RESULTS",
    "CONDITION_STATUS_CHANGED",
    # TypedDicts
    "SchedulerItem",
    "AlertItem",
    "AlertDetails",
    "ClaimData",
    "GithubPrConfig",
    "JiraQueryConfig",
    "JiraTicketConfig",
    "GithubPrState",
    "JiraQueryState",
    "JiraTicketState",
    "WatchState",
    "SuppressedCron",
    "PendingResult",
    "SchedulerStatus",
    # Lazy attrs
    "SCHEDULER_FILE",
    "ALERTS_FILE",
    "ACTIVITY_FILE",
    "LOCK_FILE",
    "CLAIMS_DIR",
    "LOCAL_TZ",
    # Core functions
    "add_reminder",
    "add_watch",
    "add_recurring",
    "add_seed_alert",
    "list_items",
    "dismiss_item",
    "dismiss_alert",
    "snooze_item",
    "check_due",
    "run_poll",
    "run_tick",
    "expire_old_alerts",
    "get_pending",
    "get_session_crons",
    "format_pending",
    "format_scheduler_status",
    "get_scheduler_status",
    "get_due_reminders",
    "get_upcoming_reminders",
    "get_unseen_alerts",
    "get_active_watches",
    # Storage
    "load_items",
    "save_items",
    "load_alerts",
    "save_alerts",
    "claim_alert",
    "sweep_stale_claims",
    "get_instance_id",
    # Display
    "show_alerts",
    "_display_items",
    # Time
    "parse_due",
    "parse_duration",
    "is_idle",
    "is_within_work_hours",
    "record_activity",
    "get_last_activity",
    # Internal (used by tests)
    "_scheduler_file",
    "_alerts_file",
    "_activity_file",
    "_lock_file",
    "_claims_dir",
    "_file_lock",
    "_atomic_write",
    "_locked_read",
    "_load_items_unlocked",
    "_load_alerts_unlocked",
    "_load_collection_unlocked",
    "_find",
    "_new_id",
    "_parse_tags",
    "_get_local_tz",
    "_parse_time_component",
    "_create_alert",
    "_format_watch_alert",
    "_evaluate_auto_remove",
    "_get_jira_credentials",
    "_run_gh",
    "_check_github_pr",
    "_check_jira_query",
    "_check_jira_ticket",
    "_detect_json_diff",
    "_detect_github_approved_or_merged",
    "_detect_github_merged",
    "_detect_jira_new_results",
    "_detect_jira_count_change",
    "_detect_jira_status_changed",
    "_CHANGE_DETECTORS",
    "_ALERT_MAX_AGE_DAYS",
    "_items_of_type",
    "_items_with_status",
    "_unseen",
    "_alerts_data",
    "_scheduler_data",
    "_check_schema_version",
    "WATCH_PROVIDERS",
    "poll_watch",
    "parse_work_hours",
]
