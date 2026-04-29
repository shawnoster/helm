"""Tests for scheduler/display.py — alert formatting, pending display, scheduler status."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aya.scheduler.display import (
    _create_alert,
    _format_ago,
    _format_watch_alert,
    _items_of_type,
    _items_with_status,
    _unseen,
    dismiss_alert,
    format_pending,
    format_scheduler_status,
    show_alerts,
)
from aya.scheduler.types import (
    SEVERITY_ACTIONABLE,
    STATUS_ACTIVE,
    STATUS_PENDING,
    TYPE_REMINDER,
    TYPE_WATCH,
    AlertItem,
    CiChecksState,
    GithubPrState,
    JiraQueryState,
    JiraTicketState,
    PendingResult,
    SchedulerItem,
    SchedulerStatus,
)


@pytest.fixture(autouse=True)
def _isolate_scheduler(tmp_path, monkeypatch):
    """Point scheduler at a temp directory so tests don't touch real data."""
    scheduler_file = tmp_path / "scheduler.json"
    alerts_file = tmp_path / "alerts.json"
    registered_file = tmp_path / "session_registered_crons.json"
    lock_file = tmp_path / ".scheduler.lock"
    scheduler_file.write_text(json.dumps({"schema_version": 2, "items": []}))
    alerts_file.write_text(json.dumps({"schema_version": 1, "alerts": []}))

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)
    monkeypatch.setattr("aya.scheduler.REGISTERED_CRONS_FILE", registered_file)
    monkeypatch.setattr("aya.scheduler.LOCK_FILE", lock_file)


# ── filter helpers ────────────────────────────────────────────────────────────


class TestItemsOfType:
    def test_filters_by_type(self):
        items: list[SchedulerItem] = [
            {"id": "a", "type": "reminder", "status": "pending", "message": "", "created_at": ""},
            {"id": "b", "type": "watch", "status": "active", "message": "", "created_at": ""},
        ]
        result = _items_of_type(items, "reminder")
        assert len(result) == 1
        assert result[0]["id"] == "a"

    def test_filters_multiple_types(self):
        items: list[SchedulerItem] = [
            {"id": "a", "type": "reminder", "status": "pending", "message": "", "created_at": ""},
            {"id": "b", "type": "watch", "status": "active", "message": "", "created_at": ""},
            {"id": "c", "type": "recurring", "status": "active", "message": "", "created_at": ""},
        ]
        result = _items_of_type(items, "reminder", "watch")
        assert len(result) == 2

    def test_empty_list_returns_empty(self):
        assert _items_of_type([], "reminder") == []


class TestItemsWithStatus:
    def test_filters_by_status(self):
        items: list[SchedulerItem] = [
            {"id": "a", "type": "reminder", "status": "pending", "message": "", "created_at": ""},
            {"id": "b", "type": "reminder", "status": "dismissed", "message": "", "created_at": ""},
        ]
        result = _items_with_status(items, "pending")
        assert len(result) == 1
        assert result[0]["id"] == "a"


class TestUnseen:
    def test_filters_unseen(self):
        alerts: list[AlertItem] = [
            {
                "id": "a",
                "source_item_id": "x",
                "created_at": "2026-01-01T00:00:00",
                "message": "one",
                "details": {},
                "seen": False,
                "severity": SEVERITY_ACTIONABLE,
            },
            {
                "id": "b",
                "source_item_id": "y",
                "created_at": "2026-01-01T00:00:00",
                "message": "two",
                "details": {},
                "seen": True,
                "severity": SEVERITY_ACTIONABLE,
            },
        ]
        result = _unseen(alerts)
        assert len(result) == 1
        assert result[0]["id"] == "a"


# ── _create_alert ─────────────────────────────────────────────────────────────


class TestCreateAlert:
    def test_creates_alert_with_required_fields(self):
        now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        alert = _create_alert("item-123", "Watch triggered", {}, now)
        assert "id" in alert
        assert alert["source_item_id"] == "item-123"
        assert alert["message"] == "Watch triggered"
        assert alert["seen"] is False
        assert alert["severity"] == SEVERITY_ACTIONABLE

    def test_custom_severity(self):
        now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        alert = _create_alert("item-456", "Info event", {}, now, severity="info")
        assert alert["severity"] == "info"


# ── _format_watch_alert ───────────────────────────────────────────────────────


class TestFormatWatchAlert:
    def _item(self, provider="github-pr", message="My watch"):
        return {
            "id": "01JTEST00000000000000000001",
            "type": "watch",
            "status": "active",
            "message": message,
            "provider": provider,
            "created_at": "2026-01-01T00:00:00",
        }

    def test_github_pr_merged(self):
        item = self._item(provider="github-pr")
        state = GithubPrState(
            pr_state="closed",
            merged=True,
            draft=False,
            title="My PR",
            reviews=[],
            has_approval=False,
        )
        result = _format_watch_alert(item, state)
        assert "MERGED" in result

    def test_github_pr_approved(self):
        item = self._item(provider="github-pr")
        state = GithubPrState(
            pr_state="open",
            merged=False,
            draft=False,
            title="My PR",
            reviews=[{"user": "alice", "state": "APPROVED"}],
            has_approval=True,
        )
        result = _format_watch_alert(item, state)
        assert "APPROVED" in result
        assert "alice" in result

    def test_github_pr_state_changed(self):
        item = self._item(provider="github-pr", message="PR Watch")
        state = GithubPrState(
            pr_state="open",
            merged=False,
            draft=False,
            title="My PR",
            reviews=[],
            has_approval=False,
        )
        result = _format_watch_alert(item, state)
        assert "state changed" in result

    def test_jira_query_with_issues(self):
        item = self._item(provider="jira-query")
        state: JiraQueryState = {
            "total": 2,
            "issues": [
                {"key": "TEST-1", "summary": "x", "status": "Open"},
                {"key": "TEST-2", "summary": "y", "status": "Open"},
            ],
        }
        result = _format_watch_alert(item, state)
        assert "TEST-1" in result
        assert "new" in result.lower()

    def test_jira_query_no_issues(self):
        item = self._item(provider="jira-query")
        state: JiraQueryState = {"total": 0, "issues": []}
        result = _format_watch_alert(item, state)
        assert "results changed" in result

    def test_jira_ticket_status(self):
        item = self._item(provider="jira-ticket")
        state: JiraTicketState = {
            "key": "CSD-123",
            "summary": "Ticket",
            "status": "In Review",
            "assignee": "alice",
        }
        result = _format_watch_alert(item, state)
        assert "In Review" in result

    def test_ci_checks_failed(self):
        item = self._item(provider="ci-checks")
        state = CiChecksState(
            all_complete=True, passed=[], failed=["unit-test", "lint"], pending=[]
        )
        result = _format_watch_alert(item, state)
        assert "FAILED" in result
        assert "unit-test" in result

    def test_ci_checks_all_passed(self):
        item = self._item(provider="ci-checks")
        state = CiChecksState(all_complete=True, passed=["lint", "test"], failed=[], pending=[])
        result = _format_watch_alert(item, state)
        assert "all checks passed" in result

    def test_unknown_provider_returns_base(self):
        item = self._item(provider="unknown-provider", message="base message")
        result = _format_watch_alert(item, {"some": "state"})
        assert result == "base message"


# ── _format_ago ──────────────────────────────────────────────────────────────


class TestFormatAgo:
    def _alert(self, created_at_str):
        return AlertItem(
            id="x",
            source_item_id="y",
            created_at=created_at_str,
            message="test",
            details={},
            seen=False,
            severity=SEVERITY_ACTIONABLE,
        )

    def test_minutes_ago(self):
        tz = ZoneInfo("UTC")
        now = datetime(2026, 4, 1, 12, 30, tzinfo=tz)
        created = now - timedelta(minutes=25)
        alert = self._alert(created.isoformat())
        result = _format_ago(alert, now)
        assert "min ago" in result

    def test_hours_ago(self):
        tz = ZoneInfo("UTC")
        now = datetime(2026, 4, 1, 12, 0, tzinfo=tz)
        created = now - timedelta(hours=3)
        alert = self._alert(created.isoformat())
        result = _format_ago(alert, now)
        assert "h ago" in result

    def test_days_ago(self):
        tz = ZoneInfo("UTC")
        now = datetime(2026, 4, 5, 12, 0, tzinfo=tz)
        created = now - timedelta(days=2)
        alert = self._alert(created.isoformat())
        result = _format_ago(alert, now)
        assert "d ago" in result


# ── format_pending ────────────────────────────────────────────────────────────


class TestFormatPending:
    def _alert(self, msg="Test alert", severity=SEVERITY_ACTIONABLE, seen=False):
        now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        return AlertItem(
            id="alert-001",
            source_item_id="item-001",
            created_at=now.isoformat(),
            message=msg,
            details={},
            seen=seen,
            severity=severity,
        )

    def test_no_items_returns_no_pending(self):
        pending: PendingResult = {
            "alerts": [],
            "session_crons": [],
            "suppressed_crons": [],
        }
        result = format_pending(pending)
        assert "No pending items" in result

    def test_actionable_alerts_shown(self):
        pending: PendingResult = {
            "alerts": [self._alert("Important notification")],
            "session_crons": [],
            "suppressed_crons": [],
        }
        result = format_pending(pending)
        assert "Important notification" in result
        assert "pending alert" in result

    def test_non_actionable_alerts_summarized_by_default(self):
        pending: PendingResult = {
            "alerts": [self._alert("Info event", severity="info")],
            "session_crons": [],
            "suppressed_crons": [],
        }
        result = format_pending(pending)
        assert "info/heartbeat" in result
        # Specific message shouldn't appear in summary
        assert "Info event" not in result

    def test_non_actionable_shown_with_show_all(self):
        pending: PendingResult = {
            "alerts": [self._alert("Heartbeat ping", severity="info")],
            "session_crons": [],
            "suppressed_crons": [],
        }
        result = format_pending(pending, show_all=True)
        assert "Heartbeat ping" in result

    def test_session_crons_shown(self):
        cron = {
            "id": "01JCRON0000000000000000001",
            "cron": "*/20 * * * *",
            "message": "health-break",
            "type": "recurring",
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
        }
        pending: PendingResult = {
            "alerts": [],
            "session_crons": [cron],
            "suppressed_crons": [],
        }
        result = format_pending(pending)
        assert "session cron" in result
        assert "*/20 * * * *" in result
        assert "health-break" in result

    def test_session_crons_with_idle_back_off(self):
        cron = {
            "id": "01JCRON0000000000000000002",
            "cron": "*/30 * * * *",
            "message": "stretch",
            "idle_back_off": "10m",
            "type": "recurring",
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
        }
        pending: PendingResult = {
            "alerts": [],
            "session_crons": [cron],
            "suppressed_crons": [],
        }
        result = format_pending(pending)
        assert "idle-back-off=10m" in result

    def test_session_crons_with_only_during(self):
        cron = {
            "id": "01JCRON0000000000000000003",
            "cron": "0 * * * *",
            "message": "check",
            "only_during": "08:00-18:00",
            "type": "recurring",
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
        }
        pending: PendingResult = {
            "alerts": [],
            "session_crons": [cron],
            "suppressed_crons": [],
        }
        result = format_pending(pending)
        assert "only-during=08:00-18:00" in result

    def test_suppressed_crons_shown(self):
        cron = {
            "id": "01JCRON0000000000000000004",
            "cron": "*/5 * * * *",
            "message": "relay-poll",
            "type": "recurring",
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
        }
        pending: PendingResult = {
            "alerts": [],
            "session_crons": [],
            "suppressed_crons": [{"item": cron, "reason": "outside work hours (08:00-18:00)"}],
        }
        result = format_pending(pending)
        assert "suppressed" in result
        assert "outside work hours" in result


# ── format_scheduler_status ───────────────────────────────────────────────────


class TestFormatSchedulerStatus:
    def _status(
        self,
        watches=None,
        reminders=None,
        crons=None,
        unseen=None,
        deliveries=None,
        total_items=0,
        total_alerts=0,
    ):
        return SchedulerStatus(
            active_watches=watches or [],
            pending_reminders=reminders or [],
            session_crons=crons or [],
            unseen_alerts=unseen or [],
            recent_deliveries=deliveries or [],
            total_items=total_items,
            total_alerts=total_alerts,
        )

    def test_no_watches_shows_none_active(self):
        status = self._status()
        result = format_scheduler_status(status)
        assert "No active watches" in result

    def test_active_watch_listed(self):
        watch = {
            "id": "w1",
            "type": "watch",
            "status": "active",
            "message": "My PR watch",
            "provider": "github-pr",
            "poll_interval_minutes": 5,
            "created_at": "2026-01-01T00:00:00",
        }
        status = self._status(watches=[watch], total_items=1)
        result = format_scheduler_status(status)
        assert "github-pr" in result
        assert "My PR watch" in result

    def test_active_watch_with_last_checked(self):
        now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        watch = {
            "id": "w1",
            "type": "watch",
            "status": "active",
            "message": "PR watch",
            "provider": "github-pr",
            "poll_interval_minutes": 5,
            "last_checked_at": now.isoformat(),
            "created_at": "2026-01-01T00:00:00",
        }
        status = self._status(watches=[watch])
        result = format_scheduler_status(status)
        assert "last:" in result
        assert "next:" in result

    def test_active_watch_never_polled(self):
        watch = {
            "id": "w1",
            "type": "watch",
            "status": "active",
            "message": "PR watch",
            "provider": "github-pr",
            "created_at": "2026-01-01T00:00:00",
        }
        status = self._status(watches=[watch])
        result = format_scheduler_status(status)
        assert "never polled" in result

    def test_pending_reminders_shown(self):
        future = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)
        reminder = {
            "id": "r1",
            "type": "reminder",
            "status": "pending",
            "message": "Check deploy",
            "due_at": future.isoformat(),
            "created_at": "2026-01-01T00:00:00",
        }
        status = self._status(reminders=[reminder])
        result = format_scheduler_status(status)
        assert "Check deploy" in result
        assert "pending reminder" in result

    def test_overdue_reminder_flagged(self):
        past = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
        reminder = {
            "id": "r1",
            "type": "reminder",
            "status": "pending",
            "message": "Old reminder",
            "due_at": past.isoformat(),
            "created_at": "2026-01-01T00:00:00",
        }
        status = self._status(reminders=[reminder])
        result = format_scheduler_status(status)
        assert "OVERDUE" in result

    def test_session_crons_shown(self):
        cron = {
            "id": "c1",
            "type": "recurring",
            "status": "active",
            "message": "health-break",
            "cron": "*/20 * * * *",
            "created_at": "2026-01-01T00:00:00",
        }
        status = self._status(crons=[cron])
        result = format_scheduler_status(status)
        assert "session cron" in result
        assert "health-break" in result

    def test_unseen_alerts_shown(self):
        alert = AlertItem(
            id="a1",
            source_item_id="i1",
            created_at="2026-01-01T00:00:00",
            message="Watch fired",
            details={},
            seen=False,
            severity=SEVERITY_ACTIONABLE,
        )
        status = self._status(unseen=[alert])
        result = format_scheduler_status(status)
        assert "Watch fired" in result

    def test_recent_deliveries_shown(self):
        delivery = {
            "id": "d1",
            "type": "reminder",
            "status": "dismissed",
            "message": "Deploy check",
            "created_at": "2026-01-01T00:00:00",
            "delivered_at": datetime(2026, 4, 1, 9, 0, tzinfo=UTC).isoformat(),
            "delivered_by": "session",
        }
        status = self._status(deliveries=[delivery])
        result = format_scheduler_status(status)
        assert "Deploy check" in result
        assert "delivery" in result.lower()

    def test_totals_shown(self):
        status = self._status(total_items=7, total_alerts=3)
        result = format_scheduler_status(status)
        assert "7 items" in result
        assert "3 alerts" in result


# ── show_alerts ───────────────────────────────────────────────────────────────


class TestShowAlerts:
    def _write_alert(self, alerts_file, seen=False):
        alert = {
            "id": "01JALERT000000000000000001",
            "source_item_id": "01JITEM000000000000000001",
            "created_at": "2026-01-01T00:00:00+00:00",
            "message": "Test alert",
            "details": {},
            "seen": seen,
            "severity": SEVERITY_ACTIONABLE,
        }
        alerts_file.write_text(json.dumps({"schema_version": 1, "alerts": [alert]}))
        return alert

    def test_returns_unseen_alerts(self, tmp_path):
        alerts_file = tmp_path / "alerts.json"
        self._write_alert(alerts_file, seen=False)

        alerts = show_alerts()
        assert len(alerts) == 1
        assert alerts[0]["message"] == "Test alert"

    def test_excludes_seen_alerts(self, tmp_path):
        alerts_file = tmp_path / "alerts.json"
        self._write_alert(alerts_file, seen=True)

        alerts = show_alerts()
        assert alerts == []

    def test_mark_seen_marks_all_seen(self, tmp_path):
        alerts_file = tmp_path / "alerts.json"
        self._write_alert(alerts_file, seen=False)

        returned = show_alerts(mark_seen=True)
        assert len(returned) == 1
        # Now check that it was marked seen in the file
        data = json.loads(alerts_file.read_text())
        assert data["alerts"][0]["seen"] is True

    def test_mark_seen_returns_empty_when_no_unseen(self, tmp_path):
        alerts_file = tmp_path / "alerts.json"
        self._write_alert(alerts_file, seen=True)

        returned = show_alerts(mark_seen=True)
        assert returned == []


# ── dismiss_alert ────────────────────────────────────────────────────────────


class TestDismissAlert:
    def _setup_alert(self, tmp_path):
        alerts_file = tmp_path / "alerts.json"
        alert_id = "01JALERT000000000000000001"
        alert = {
            "id": alert_id,
            "source_item_id": "01JITEM000000000000000001",
            "created_at": "2026-01-01T00:00:00+00:00",
            "message": "Dismiss me",
            "details": {},
            "seen": False,
            "severity": SEVERITY_ACTIONABLE,
        }
        alerts_file.write_text(json.dumps({"schema_version": 1, "alerts": [alert]}))
        return alert_id

    def test_dismiss_marks_seen(self, tmp_path):
        alerts_file = tmp_path / "alerts.json"
        alert_id = self._setup_alert(tmp_path)

        result = dismiss_alert(alert_id)
        assert result["seen"] is True

        data = json.loads(alerts_file.read_text())
        assert data["alerts"][0]["seen"] is True

    def test_dismiss_prefix_match(self, tmp_path):
        tmp_path / "alerts.json"
        alert_id = self._setup_alert(tmp_path)

        prefix = alert_id[:8]
        result = dismiss_alert(prefix)
        assert result["seen"] is True

    def test_dismiss_not_found_raises(self, tmp_path):
        self._setup_alert(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            dismiss_alert("nonexistent-id")


# ── _display_items ────────────────────────────────────────────────────────────


class TestDisplayItems:
    """Tests for _display_items — verifies it runs without error for all item types."""

    from aya.scheduler.display import _display_items

    def _reminder(self, due_in_future=True):

        now = datetime.now(UTC)
        due = now + timedelta(days=1) if due_in_future else now - timedelta(days=365)
        return {
            "id": "01JTEST00000000000000000001",
            "type": TYPE_REMINDER,
            "status": STATUS_PENDING,
            "message": "Remember to review",
            "due_at": due.isoformat(),
            "created_at": "2026-01-01T00:00:00",
        }

    def _watch(self):
        return {
            "id": "01JTEST00000000000000000002",
            "type": TYPE_WATCH,
            "status": STATUS_ACTIVE,
            "message": "Watch PR",
            "provider": "github-pr",
            "poll_interval_minutes": 5,
            "created_at": "2026-01-01T00:00:00",
        }

    def _recurring(self):
        return {
            "id": "01JTEST00000000000000000003",
            "type": "recurring",
            "status": STATUS_ACTIVE,
            "message": "health-break",
            "cron": "*/20 * * * *",
            "created_at": "2026-01-01T00:00:00",
        }

    def _event(self):
        return {
            "id": "01JTEST00000000000000000004",
            "type": "event",
            "status": STATUS_ACTIVE,
            "message": "On session start",
            "trigger": "session_start",
            "created_at": "2026-01-01T00:00:00",
        }

    def test_empty_items_no_output(self, capsys):
        from aya.scheduler.display import _display_items

        _display_items([])
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_reminder_item_printed(self, capsys):
        from aya.scheduler.display import _display_items

        _display_items([self._reminder()])
        captured = capsys.readouterr()
        assert "Remember to review" in captured.out

    def test_overdue_reminder_flagged(self, capsys):
        from aya.scheduler.display import _display_items

        _display_items([self._reminder(due_in_future=False)])
        captured = capsys.readouterr()
        # Overdue items show the warning emoji
        assert "OVERDUE" in captured.out or "\u26a0\ufe0f" in captured.out

    def test_watch_item_printed(self, capsys):
        from aya.scheduler.display import _display_items

        _display_items([self._watch()])
        captured = capsys.readouterr()
        assert "Watch PR" in captured.out
        assert "github-pr" in captured.out

    def test_watch_with_last_checked(self, capsys):

        from aya.scheduler.display import _display_items

        watch = self._watch()
        watch["last_checked_at"] = datetime(2026, 4, 1, 11, 0, tzinfo=UTC).isoformat()
        _display_items([watch])
        captured = capsys.readouterr()
        assert "11:00" in captured.out

    def test_recurring_item_printed(self, capsys):
        from aya.scheduler.display import _display_items

        _display_items([self._recurring()])
        captured = capsys.readouterr()
        assert "health-break" in captured.out
        assert "*/20 * * * *" in captured.out

    def test_event_item_printed(self, capsys):
        from aya.scheduler.display import _display_items

        _display_items([self._event()])
        captured = capsys.readouterr()
        assert "On session start" in captured.out
        assert "session_start" in captured.out

    def test_items_with_tags_shown(self, capsys):
        from aya.scheduler.display import _display_items

        item = self._recurring()
        item["tags"] = ["health", "movement"]
        _display_items([item])
        captured = capsys.readouterr()
        assert "health" in captured.out
