"""Scheduler types, constants, and schema helpers."""

from __future__ import annotations

import logging
from typing import Any, Literal, NotRequired, TypedDict

logger = logging.getLogger(__name__)

# ── item types ───────────────────────────────────────────────────────────────
TYPE_REMINDER = "reminder"
TYPE_WATCH = "watch"
TYPE_RECURRING = "recurring"
TYPE_EVENT = "event"

# ── item statuses ────────────────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_SNOOZED = "snoozed"
STATUS_DELIVERED = "delivered"
STATUS_DISMISSED = "dismissed"
STATUS_DONE = "done"

# ── watch providers ──────────────────────────────────────────────────────────
PROVIDER_GITHUB_PR = "github-pr"
PROVIDER_JIRA_QUERY = "jira-query"
PROVIDER_JIRA_TICKET = "jira-ticket"

# ── schema versions ──────────────────────────────────────────────────────────
SCHEDULER_SCHEMA_VERSION = 1
ALERTS_SCHEMA_VERSION = 1

# ── alert severity ──────────────────────────────────────────────────────────
SEVERITY_ACTIONABLE = "actionable"
SEVERITY_INFO = "info"
SEVERITY_HEARTBEAT = "heartbeat"

# Ordered from highest to lowest priority
SEVERITY_ORDER: list[str] = [SEVERITY_ACTIONABLE, SEVERITY_INFO, SEVERITY_HEARTBEAT]

AlertSeverity = Literal["actionable", "info", "heartbeat"]

# ── watch conditions ─────────────────────────────────────────────────────────
CONDITION_APPROVED_OR_MERGED = "approved_or_merged"
CONDITION_MERGED = "merged"
CONDITION_NEW_RESULTS = "new_results"
CONDITION_STATUS_CHANGED = "status_changed"


# ── TypedDict schemas ────────────────────────────────────────────────────────


class SchedulerItem(TypedDict):
    """Base fields present on every scheduler item."""

    id: str
    type: str
    status: str
    created_at: str
    message: str
    tags: list[str]
    session_required: bool
    # Reminder-specific (absent on watch / recurring items)
    due_at: NotRequired[str]
    delivered_at: NotRequired[str | None]
    snoozed_until: NotRequired[str | None]
    # Watch-specific
    provider: NotRequired[str]
    watch_config: NotRequired[GithubPrConfig | JiraQueryConfig | JiraTicketConfig]
    condition: NotRequired[str]
    poll_interval_minutes: NotRequired[int]
    last_checked_at: NotRequired[str | None]
    last_state: NotRequired[WatchState | None]
    remove_when: NotRequired[str]
    # Recurring-specific
    cron: NotRequired[str]
    prompt: NotRequired[str]
    idle_back_off: NotRequired[str]
    only_during: NotRequired[str]
    # Event-specific
    trigger: NotRequired[str]


class AlertDetails(TypedDict, total=False):
    """Details payload stored inside an AlertItem.  All fields are optional
    because different alert sources populate different subsets."""

    # Generic reminder detail
    due_at: str
    # Seed-packet detail
    type: str
    intent: str
    opener: str
    context_summary: str
    open_questions: list[str]
    from_label: str
    body: str
    # Watch-state snapshot fields (GitHub PR)
    pr_state: str
    merged: bool
    draft: bool
    title: str
    reviews: list[dict[str, Any]]
    has_approval: bool
    # Watch-state snapshot fields (Jira query)
    total: int
    issues: list[dict[str, Any]]
    # Watch-state snapshot fields (Jira ticket)
    key: str
    summary: str
    status: str
    assignee: str


class AlertItem(TypedDict):
    """A persisted alert record."""

    id: str
    source_item_id: str
    created_at: str
    message: str
    details: AlertDetails
    seen: bool
    severity: NotRequired[AlertSeverity]
    delivered_at: NotRequired[str]
    delivered_by: NotRequired[str]


class ClaimData(TypedDict):
    """Contents of a `.claimed` file used for alert-delivery deduplication."""

    instance: str
    claimed_at: str
    ttl_seconds: int


class GithubPrConfig(TypedDict):
    """watch_config for a github-pr watch."""

    owner: str
    repo: str
    pr: int


class JiraQueryConfig(TypedDict):
    """watch_config for a jira-query watch."""

    jql: str


class JiraTicketConfig(TypedDict):
    """watch_config for a jira-ticket watch."""

    ticket: str


class GithubPrState(TypedDict):
    """State snapshot returned by _check_github_pr."""

    pr_state: str | None
    merged: bool
    draft: bool
    title: str
    reviews: list[dict[str, Any]]
    has_approval: bool


class JiraQueryState(TypedDict):
    """State snapshot returned by _check_jira_query."""

    total: int
    issues: list[dict[str, Any]]


class JiraTicketState(TypedDict):
    """State snapshot returned by _check_jira_ticket."""

    key: str
    summary: str
    status: str
    assignee: str


# Union of all possible watch-state shapes
WatchState = GithubPrState | JiraQueryState | JiraTicketState


class SuppressedCron(TypedDict):
    """Entry in the suppressed_crons list returned by get_session_crons."""

    item: SchedulerItem
    reason: str


class PendingResult(TypedDict):
    """Return value of get_pending()."""

    alerts: list[AlertItem]
    session_crons: list[SchedulerItem]
    suppressed_crons: list[SuppressedCron]
    instance_id: str


class SchedulerStatus(TypedDict):
    """Return value of get_scheduler_status()."""

    active_watches: list[SchedulerItem]
    pending_reminders: list[SchedulerItem]
    session_crons: list[SchedulerItem]
    unseen_alerts: list[AlertItem]
    recent_deliveries: list[AlertItem]
    total_items: int
    total_alerts: int


# ── schema helpers ───────────────────────────────────────────────────────────


def _scheduler_data(items: list[SchedulerItem]) -> dict[str, Any]:
    """Build the top-level dict for scheduler.json writes."""
    return {"schema_version": SCHEDULER_SCHEMA_VERSION, "items": items}


def _alerts_data(alerts: list[AlertItem]) -> dict[str, Any]:
    """Build the top-level dict for alerts.json writes."""
    return {"schema_version": ALERTS_SCHEMA_VERSION, "alerts": alerts}


def _check_schema_version(data: dict[str, Any], expected: int, filename: str) -> None:
    """Log a warning if the file's schema_version is newer than expected."""
    raw = data.get("schema_version", 0)
    file_version = raw if isinstance(raw, int) else 0
    if not isinstance(raw, int) and raw is not None:
        logger.warning("%s has non-integer schema_version: %r — treating as 0", filename, raw)
    if file_version > expected:
        logger.warning("%s schema_version %d > expected %d", filename, file_version, expected)
