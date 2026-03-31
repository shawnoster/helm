"""Tests for the scheduler module — reminders, watches, dismissal, time parsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from aya.scheduler import (
    LOCAL_TZ,
    _find,
    _get_local_tz,
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

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)


# ── Timezone configuration ──────────────────────────────────────────────────────


class TestGetLocalTz:
    def test_default_timezone(self, monkeypatch):
        """_get_local_tz returns America/Denver by default."""
        monkeypatch.delenv("AYA_TZ", raising=False)
        # Clear the cache so we get a fresh evaluation
        _get_local_tz.cache_clear()
        tz = _get_local_tz()
        assert str(tz) == "America/Denver"

    def test_custom_timezone(self, monkeypatch):
        """_get_local_tz respects AYA_TZ env var."""
        monkeypatch.setenv("AYA_TZ", "America/Los_Angeles")
        _get_local_tz.cache_clear()
        tz = _get_local_tz()
        assert str(tz) == "America/Los_Angeles"

    def test_timezone_with_whitespace(self, monkeypatch):
        """_get_local_tz strips whitespace from AYA_TZ value."""
        monkeypatch.setenv("AYA_TZ", " UTC ")
        _get_local_tz.cache_clear()
        tz = _get_local_tz()
        assert str(tz) == "UTC"

    def test_invalid_timezone_fallback(self, monkeypatch, caplog):
        """_get_local_tz falls back to America/Denver for invalid timezone."""
        monkeypatch.setenv("AYA_TZ", "Invalid/Zone")
        _get_local_tz.cache_clear()
        tz = _get_local_tz()
        assert str(tz) == "America/Denver"
        assert "Invalid timezone" in caplog.text

    def test_cache_clears_with_env_var_change(self, monkeypatch):
        """_get_local_tz cache responds to AYA_TZ changes."""
        monkeypatch.setenv("AYA_TZ", "America/Los_Angeles")
        _get_local_tz.cache_clear()
        tz1 = _get_local_tz()

        # Change env var and clear cache
        monkeypatch.setenv("AYA_TZ", "UTC")
        _get_local_tz.cache_clear()
        tz2 = _get_local_tz()

        assert str(tz1) == "America/Los_Angeles"
        assert str(tz2) == "UTC"


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
        due, _alerts = check_due()
        assert len(due) == 0

    def test_past_due(self):
        # Directly insert an already-due item
        items = load_items()
        past = (datetime.now(LOCAL_TZ) - timedelta(hours=1)).isoformat()
        items.append(
            {
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
            }
        )
        save_items(items)
        due, _ = check_due()
        assert len(due) == 1
        assert due[0]["id"] == "test-due-id"

    def test_snoozed_not_yet_due(self):
        items = load_items()
        future = (datetime.now(LOCAL_TZ) + timedelta(hours=1)).isoformat()
        past = (datetime.now(LOCAL_TZ) - timedelta(hours=1)).isoformat()
        items.append(
            {
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
            }
        )
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
        from aya import scheduler

        alerts = [
            {
                "id": "a1",
                "source_item_id": "s1",
                "created_at": datetime.now(LOCAL_TZ).isoformat(),
                "message": "Alert 1",
                "seen": False,
            },
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


# ── Regression: missing fields ───────────────────────────────────────────────


class TestMissingFields:
    """Items with missing status/type fields should not crash the scheduler."""

    def test_display_items_missing_status(self):
        """Regression: _display_items crashed with KeyError on items missing 'status'."""
        from aya.scheduler import _display_items

        items = [
            {"id": "no-status", "type": "recurring", "session_required": True, "cron": "0 * * * *"},
        ]
        # Should not raise
        _display_items(items)

    def test_display_items_missing_type(self):
        """Items missing 'type' should be silently skipped."""
        from aya.scheduler import _display_items

        items = [
            {"id": "no-type", "status": "active"},
        ]
        _display_items(items)

    def test_list_items_with_missing_status(self):
        """list_items should handle items that lack a status field."""
        from aya.scheduler import save_items

        save_items(
            [
                {"id": "bare", "type": "recurring", "session_required": True, "cron": "0 * * * *"},
            ]
        )
        # list_items filters on status — missing status should not crash
        result = list_items(show_all=True)
        assert len(result) == 1

    def test_get_pending_missing_status(self):
        """get_pending should treat missing status as 'active' for recurring items."""
        from aya.scheduler import get_pending, save_items

        save_items(
            [
                {
                    "id": "no-status-cron",
                    "type": "recurring",
                    "session_required": True,
                    "cron": "*/30 * * * *",
                    "prompt": "test",
                },
            ]
        )
        pending = get_pending("test-instance")
        assert len(pending["session_crons"]) == 1

    def test_check_due_missing_status(self):
        """check_due should not crash on items missing status."""
        from aya.scheduler import check_due, save_items

        save_items(
            [
                {
                    "id": "bare-reminder",
                    "type": "reminder",
                    "due_at": "2020-01-01T00:00:00-07:00",
                    "message": "old",
                },
            ]
        )
        # Should not raise — item is skipped because status not in ("pending", "snoozed")
        due, _unseen = check_due()
        assert isinstance(due, list)
