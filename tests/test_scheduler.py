"""Tests for the scheduler module — reminders, watches, dismissal, time parsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from ai_assist.scheduler import (
    LOCAL_TZ,
    _find,
    _parse_tags,
    add_recurring,
    add_reminder,
    add_watch,
    check_due,
    dismiss_item,
    list_items,
    load_items,
    parse_due,
    save_items,
    show_alerts,
    snooze_item,
)


@pytest.fixture(autouse=True)
def _isolate_scheduler(tmp_path, monkeypatch):
    """Point scheduler at a temp directory so tests don't touch real data."""
    scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
    alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
    scheduler_file.parent.mkdir(parents=True)
    scheduler_file.write_text(json.dumps({"items": []}))
    alerts_file.write_text(json.dumps({"alerts": []}))

    monkeypatch.setattr("ai_assist.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("ai_assist.scheduler.ALERTS_FILE", alerts_file)


# ── Time parsing ─────────────────────────────────────────────────────────────


class TestParseDue:
    def test_relative_minutes(self):
        now = datetime(2026, 3, 21, 14, 0, tzinfo=LOCAL_TZ)
        result = parse_due("in 30 minutes", now)
        assert result == now + timedelta(minutes=30)

    def test_relative_hours(self):
        now = datetime(2026, 3, 21, 14, 0, tzinfo=LOCAL_TZ)
        result = parse_due("in 2 hours", now)
        assert result == now + timedelta(hours=2)

    def test_tomorrow(self):
        now = datetime(2026, 3, 21, 14, 0, tzinfo=LOCAL_TZ)
        result = parse_due("tomorrow 9am", now)
        assert result.day == 22
        assert result.hour == 9
        assert result.minute == 0

    def test_eod(self):
        now = datetime(2026, 3, 21, 14, 0, tzinfo=LOCAL_TZ)
        result = parse_due("eod", now)
        assert result.hour == 17
        assert result.minute == 0

    def test_iso_format(self):
        result = parse_due("2026-03-21T15:30:00")
        assert result.hour == 15
        assert result.minute == 30

    def test_iso_format_with_timezone_offset(self):
        # Regression: parse_due("2026-03-23T09:00:00-06:00") was silently
        # returning today at 20:00 because .lower() broke the 'T' separator
        # and _TIME_RE then matched "20" from the year "2026..."
        result = parse_due("2026-03-23T09:00:00-06:00")
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 23
        assert result.hour == 9
        assert result.minute == 0

    def test_iso_format_with_timezone_offset_z(self):
        # UTC offset using Z suffix
        result = parse_due("2026-03-23T15:30:00+00:00")
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 23
        assert result.hour == 15

    def test_iso_format_preserves_correct_date(self):
        # Regression: must not silently produce a different date
        result = parse_due("2026-03-23T09:00:00")
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 23
        assert result.hour == 9

    def test_today_time(self):
        now = datetime(2026, 3, 21, 10, 0, tzinfo=LOCAL_TZ)
        result = parse_due("today 3pm", now)
        assert result.hour == 15
        assert result.day == 21

    def test_today_past_time_rolls_to_tomorrow(self):
        now = datetime(2026, 3, 21, 16, 0, tzinfo=LOCAL_TZ)
        result = parse_due("3pm", now)
        assert result.day == 22
        assert result.hour == 15

    def test_weekday(self):
        now = datetime(2026, 3, 21, 10, 0, tzinfo=LOCAL_TZ)  # Saturday
        result = parse_due("monday 9am", now)
        assert result.weekday() == 0  # Monday
        assert result.hour == 9

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_due("gibberish nonsense")


# ── Add reminder ─────────────────────────────────────────────────────────────


class TestAddReminder:
    def test_creates_item(self):
        item = add_reminder("Test reminder", "in 1 hour")
        assert item["type"] == "reminder"
        assert item["status"] == "pending"
        assert item["message"] == "Test reminder"
        assert item["due_at"]
        assert len(item["id"]) == 36  # UUID

    def test_persists_to_disk(self):
        add_reminder("Persisted", "in 1 hour")
        items = load_items()
        assert len(items) == 1
        assert items[0]["message"] == "Persisted"

    def test_tags_parsed(self):
        item = add_reminder("Tagged", "in 1 hour", tags="foo, bar")
        assert item["tags"] == ["foo", "bar"]

    def test_empty_tags(self):
        item = add_reminder("No tags", "in 1 hour")
        assert item["tags"] == []


# ── Add watch ────────────────────────────────────────────────────────────────


class TestAddWatch:
    def test_github_pr(self):
        item = add_watch("github-pr", "owner/repo#123", "Watch PR")
        assert item["type"] == "watch"
        assert item["provider"] == "github-pr"
        assert item["watch_config"]["owner"] == "owner"
        assert item["watch_config"]["repo"] == "repo"
        assert item["watch_config"]["pr"] == 123
        assert item["condition"] == "approved_or_merged"

    def test_jira_query(self):
        item = add_watch("jira-query", "project=CSD", "Watch JQL")
        assert item["watch_config"]["jql"] == "project=CSD"
        assert item["condition"] == "new_results"

    def test_jira_ticket(self):
        item = add_watch("jira-ticket", "csd-225", "Watch ticket")
        assert item["watch_config"]["ticket"] == "CSD-225"
        assert item["condition"] == "status_changed"

    def test_custom_interval(self):
        item = add_watch("github-pr", "o/r#1", "Fast poll", interval=5)
        assert item["poll_interval_minutes"] == 5

    def test_invalid_provider(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            add_watch("bogus", "target", "msg")

    def test_bad_github_format(self):
        with pytest.raises(ValueError, match="Format"):
            add_watch("github-pr", "not-valid", "msg")


# ── Add recurring ────────────────────────────────────────────────────────────


class TestAddRecurring:
    def test_creates_recurring(self):
        item = add_recurring("Hourly check", "0 * * * *", prompt="do the thing")
        assert item["type"] == "recurring"
        assert item["cron"] == "0 * * * *"
        assert item["prompt"] == "do the thing"
        assert item["session_required"] is True


# ── List items ───────────────────────────────────────────────────────────────


class TestListItems:
    def test_empty(self):
        assert list_items() == []

    def test_filters_dismissed(self):
        add_reminder("Active", "in 1 hour")
        item2 = add_reminder("Will dismiss", "in 2 hours")
        dismiss_item(item2["id"])
        assert len(list_items()) == 1
        assert len(list_items(show_all=True)) == 2

    def test_filters_by_type(self):
        add_reminder("Reminder", "in 1 hour")
        add_recurring("Recurring", "0 * * * *")
        assert len(list_items(item_type="reminder")) == 1
        assert len(list_items(item_type="recurring")) == 1


# ── Check due ────────────────────────────────────────────────────────────────


class TestCheckDue:
    def test_nothing_due(self):
        add_reminder("Future", "in 1 hour")
        due, alerts = check_due()
        assert len(due) == 0

    def test_past_due(self):
        # Directly insert an already-due item
        items = load_items()
        past = (datetime.now(LOCAL_TZ) - timedelta(hours=1)).isoformat()
        items.append({
            "id": "test-due-id",
            "type": "reminder",
            "status": "pending",
            "created_at": past,
            "message": "Overdue",
            "tags": [],
            "session_required": False,
            "due_at": past,
            "delivered_at": None,
            "snoozed_until": None,
        })
        save_items(items)
        due, _ = check_due()
        assert len(due) == 1
        assert due[0]["id"] == "test-due-id"

    def test_snoozed_not_yet_due(self):
        items = load_items()
        future = (datetime.now(LOCAL_TZ) + timedelta(hours=1)).isoformat()
        past = (datetime.now(LOCAL_TZ) - timedelta(hours=1)).isoformat()
        items.append({
            "id": "snoozed-id",
            "type": "reminder",
            "status": "snoozed",
            "created_at": past,
            "message": "Snoozed",
            "tags": [],
            "session_required": False,
            "due_at": past,
            "delivered_at": None,
            "snoozed_until": future,
        })
        save_items(items)
        due, _ = check_due()
        assert len(due) == 0


# ── Dismiss ──────────────────────────────────────────────────────────────────


class TestDismiss:
    def test_dismiss_by_full_id(self):
        item = add_reminder("To dismiss", "in 1 hour")
        dismissed = dismiss_item(item["id"])
        assert dismissed["status"] == "dismissed"

    def test_dismiss_by_prefix(self):
        item = add_reminder("Prefix dismiss", "in 1 hour")
        dismissed = dismiss_item(item["id"][:8])
        assert dismissed["status"] == "dismissed"

    def test_dismiss_sets_delivered_at(self):
        item = add_reminder("Delivered", "in 1 hour")
        dismissed = dismiss_item(item["id"])
        assert dismissed["delivered_at"] is not None

    def test_dismiss_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            dismiss_item("nonexistent-id")


# ── Snooze ───────────────────────────────────────────────────────────────────


class TestSnooze:
    def test_snooze(self):
        item = add_reminder("Snooze me", "in 1 hour")
        snoozed, until = snooze_item(item["id"], "in 2 hours")
        assert snoozed["status"] == "snoozed"
        assert snoozed["snoozed_until"] is not None
        assert until > datetime.now(LOCAL_TZ)

    def test_snooze_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            snooze_item("nope", "in 1 hour")


# ── Show alerts ──────────────────────────────────────────────────────────────


class TestShowAlerts:
    def test_empty(self):
        assert show_alerts() == []

    def test_mark_seen(self, monkeypatch):
        from ai_assist import scheduler
        alerts = [
            {"id": "a1", "source_item_id": "s1", "created_at": datetime.now(LOCAL_TZ).isoformat(),
             "message": "Alert 1", "seen": False},
        ]
        scheduler.ALERTS_FILE.write_text(json.dumps({"alerts": alerts}))
        unseen = show_alerts(mark_seen=True)
        assert len(unseen) == 1
        # Verify they're now marked seen
        reloaded = json.loads(scheduler.ALERTS_FILE.read_text())
        assert all(a["seen"] for a in reloaded["alerts"])


# ── Helpers ──────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_find_exact(self):
        items = [{"id": "abc-123"}, {"id": "def-456"}]
        assert _find(items, "abc-123")["id"] == "abc-123"

    def test_find_prefix(self):
        items = [{"id": "abc-123"}, {"id": "def-456"}]
        assert _find(items, "abc")["id"] == "abc-123"

    def test_find_not_found(self):
        assert _find([], "nope") is None

    def test_parse_tags(self):
        assert _parse_tags("foo, bar, baz") == ["foo", "bar", "baz"]
        assert _parse_tags("") == []
        assert _parse_tags("single") == ["single"]
