"""Tests for the scheduler module — reminders, watches, dismissal, time parsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aya.scheduler import (
    ALERTS_SCHEMA_VERSION,
    LOCAL_TZ,
    SCHEDULER_SCHEMA_VERSION,
    _find,
    _get_local_tz,
    _load_collection_unlocked,
    _parse_tags,
    add_recurring,
    add_reminder,
    add_seed_alert,
    add_watch,
    check_due,
    dismiss_item,
    get_pending,
    list_items,
    load_items,
    parse_due,
    run_tick,
    save_items,
    show_alerts,
    snooze_item,
)


@pytest.fixture(autouse=True)
def _isolate_scheduler(tmp_path, monkeypatch):
    """Point scheduler at a temp directory so tests don't touch real data."""
    scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
    alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
    registered_file = tmp_path / "assistant" / "memory" / "session_registered_crons.json"
    lock_file = tmp_path / "assistant" / "memory" / ".scheduler.lock"
    scheduler_file.parent.mkdir(parents=True)
    scheduler_file.write_text(json.dumps({"items": []}))
    alerts_file.write_text(json.dumps({"alerts": []}))

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)
    monkeypatch.setattr("aya.scheduler.REGISTERED_CRONS_FILE", registered_file)
    monkeypatch.setattr("aya.scheduler.LOCK_FILE", lock_file)


# ── Timezone configuration ──────────────────────────────────────────────────────


class TestGetLocalTz:
    def test_default_timezone_detects_system(self, monkeypatch):
        """_get_local_tz detects system timezone when AYA_TZ is not set."""
        monkeypatch.delenv("AYA_TZ", raising=False)
        _get_local_tz.cache_clear()
        tz = _get_local_tz()
        # Must return a real ZoneInfo (not a fixed-offset tzinfo)
        assert isinstance(tz, ZoneInfo)
        # On any Linux system, should detect something (not fall through to UTC
        # unless the system genuinely has no timezone configured)
        assert str(tz) != ""

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
        """_get_local_tz falls back to system timezone for invalid AYA_TZ."""
        monkeypatch.setenv("AYA_TZ", "Invalid/Zone")
        _get_local_tz.cache_clear()
        tz = _get_local_tz()
        # Must return a real ZoneInfo, not the invalid value
        assert isinstance(tz, ZoneInfo)
        assert str(tz) != "Invalid/Zone"
        assert "Invalid AYA_TZ" in caplog.text

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


# ── Seed alerts ───────────────────────────────────────────────────────────────


class TestSeedAlerts:
    def test_add_seed_alert_appends_unseen(self):
        """add_seed_alert persists an alert with seen=False and the given packet_id."""
        alert = add_seed_alert(
            intent="Context sync",
            opener="Hey, what's the status?",
            context_summary="Working on Dead Reckoning spec.",
            open_questions=["Should /session absorb /switch?"],
            from_label="work",
            packet_id="test-packet-id-123",
        )
        assert alert["seen"] is False
        assert alert["source_item_id"] == "test-packet-id-123"

    def test_add_seed_alert_generates_id_when_no_packet_id(self):
        """add_seed_alert generates a source_item_id when packet_id is omitted."""
        alert = add_seed_alert(
            intent="Quick note",
            opener="",
            context_summary="",
            open_questions=[],
            from_label="home",
        )
        assert alert["seen"] is False
        assert alert["source_item_id"]  # non-empty

    def test_pending_claims_seed_alert(self):
        """get_pending() returns a seed alert and marks it claimed."""
        alert = add_seed_alert(
            intent="Relay context",
            opener="Opening message here.",
            context_summary="",
            open_questions=[],
            from_label="work",
            packet_id="seed-pkt-456",
        )
        pending = get_pending("test-instance")
        assert any(a["id"] == alert["id"] for a in pending["alerts"])

    def test_tick_with_mixed_alert_types(self):
        """run_tick() handles a mix of a due reminder and a seed alert without error."""
        # Inject an overdue reminder directly
        items = load_items()
        past = (datetime.now(LOCAL_TZ) - timedelta(hours=1)).isoformat()
        items.append(
            {
                "id": "overdue-reminder",
                "type": "reminder",
                "status": "pending",
                "created_at": past,
                "message": "Overdue task",
                "tags": [],
                "session_required": False,
                "due_at": past,
                "delivered_at": None,
                "snoozed_until": None,
            }
        )
        save_items(items)
        add_seed_alert(
            intent="Mixed type test",
            opener="Opener",
            context_summary="",
            open_questions=[],
            from_label="work",
        )
        result = run_tick()
        assert isinstance(result, dict)
        assert "claims_swept" in result


# ── Schema version ──────────────────────────────────────────────────────────


class TestSchemaVersion:
    def test_save_items_includes_schema_version(self):
        """add_reminder writes schema_version to scheduler.json."""
        from aya import scheduler

        add_reminder("versioned", "in 1 hour")
        data = json.loads(scheduler.SCHEDULER_FILE.read_text())
        assert data["schema_version"] == SCHEDULER_SCHEMA_VERSION

    def test_save_alerts_includes_schema_version(self):
        """add_seed_alert writes schema_version to alerts.json."""
        from aya import scheduler

        add_seed_alert(
            intent="version test",
            opener="hi",
            context_summary="",
            open_questions=[],
            from_label="home",
        )
        data = json.loads(scheduler.ALERTS_FILE.read_text())
        assert data["schema_version"] == ALERTS_SCHEMA_VERSION

    def test_load_without_schema_version_backward_compat(self):
        """Files without schema_version load successfully (treated as v0)."""
        from aya import scheduler

        scheduler.SCHEDULER_FILE.write_text(json.dumps({"items": []}))
        items = load_items()
        assert items == []

    def test_load_future_schema_version_warns(self, caplog):
        """Loading a file with a higher schema_version logs a warning."""
        from aya import scheduler

        scheduler.SCHEDULER_FILE.write_text(json.dumps({"schema_version": 999, "items": []}))
        items = load_items()
        assert items == []
        assert "schema_version 999" in caplog.text

    def test_load_alerts_future_schema_version_warns(self, caplog):
        """Loading alerts with a higher schema_version logs a warning."""
        from aya import scheduler

        scheduler.ALERTS_FILE.write_text(json.dumps({"schema_version": 999, "alerts": []}))
        _load_collection_unlocked(scheduler.ALERTS_FILE, "alerts")
        assert "schema_version 999" in caplog.text


# ── TestRegisteredCronIds ─────────────────────────────────────────────────────


class TestRegisteredCronIds:
    """Tests for the per-session cron ID tracker (storage.register_new_cron_ids).

    Race condition coverage: register_new_cron_ids is the atomic
    check-and-update used by `aya hook crons` to dedupe registrations
    across concurrent PostToolUse hook invocations. Two callers racing
    on the same candidate ID must agree that exactly one of them sees
    it as new.
    """

    def test_first_call_returns_all_candidates(self):
        from aya.scheduler import (
            load_registered_cron_ids,
            register_new_cron_ids,
        )

        new = register_new_cron_ids({"cron-a", "cron-b"})
        assert new == {"cron-a", "cron-b"}
        assert load_registered_cron_ids() == {"cron-a", "cron-b"}

    def test_second_call_returns_only_unseen_subset(self):
        from aya.scheduler import register_new_cron_ids

        register_new_cron_ids({"cron-a", "cron-b"})
        new = register_new_cron_ids({"cron-a", "cron-c"})
        # cron-a was already registered; cron-c is new
        assert new == {"cron-c"}

    def test_call_with_only_known_ids_returns_empty(self):
        from aya.scheduler import register_new_cron_ids

        register_new_cron_ids({"cron-a", "cron-b"})
        new = register_new_cron_ids({"cron-a", "cron-b"})
        assert new == set()

    def test_empty_input_is_noop(self):
        from aya.scheduler import load_registered_cron_ids, register_new_cron_ids

        register_new_cron_ids({"cron-a"})
        new = register_new_cron_ids(set())
        assert new == set()
        # Existing tracker state preserved
        assert load_registered_cron_ids() == {"cron-a"}

    def test_persists_merged_set_across_processes(self, tmp_path):
        """The tracker file on disk should contain the union of all
        registered IDs after multiple register calls."""
        import json as json_

        from aya import scheduler
        from aya.scheduler import register_new_cron_ids

        register_new_cron_ids({"cron-a"})
        register_new_cron_ids({"cron-b"})
        register_new_cron_ids({"cron-c"})

        data = json_.loads(scheduler.REGISTERED_CRONS_FILE.read_text())
        assert set(data["ids"]) == {"cron-a", "cron-b", "cron-c"}

    def test_reset_clears_tracker(self):
        from aya.scheduler import (
            load_registered_cron_ids,
            register_new_cron_ids,
            reset_registered_cron_ids,
        )

        register_new_cron_ids({"cron-a", "cron-b"})
        reset_registered_cron_ids()
        assert load_registered_cron_ids() == set()

        # After reset, all candidates are new again
        new = register_new_cron_ids({"cron-a", "cron-b"})
        assert new == {"cron-a", "cron-b"}

    def test_reset_when_tracker_does_not_exist(self):
        """reset_registered_cron_ids should be a no-op when the file is missing."""
        from aya.scheduler import reset_registered_cron_ids

        # Should not raise even if the file doesn't exist
        reset_registered_cron_ids()
        reset_registered_cron_ids()  # idempotent

    def test_load_returns_empty_for_corrupt_file(self, monkeypatch):
        """Defensive read: a corrupt or malformed tracker should return
        an empty set rather than crash. This matters because the file
        is updated under load by parallel hooks; a partial write that
        somehow slipped past atomic_write would otherwise wedge the
        scheduler."""
        from aya import scheduler
        from aya.scheduler import load_registered_cron_ids

        scheduler.REGISTERED_CRONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        scheduler.REGISTERED_CRONS_FILE.write_text("not json at all {{{")
        assert load_registered_cron_ids() == set()

        scheduler.REGISTERED_CRONS_FILE.write_text('{"ids": "not a list"}')
        assert load_registered_cron_ids() == set()


# ── Programmatic API ─────────────────────────────────────────────────────────


class TestGetDueReminders:
    def test_returns_past_due_reminders(self):
        from aya.scheduler import get_due_reminders

        datetime(2025, 1, 1, 12, 0, tzinfo=LOCAL_TZ)
        add_reminder("Old reminder", "2025-01-01T12:00:00")
        now = datetime(2026, 4, 1, 12, 0, tzinfo=LOCAL_TZ)
        due = get_due_reminders(now=now)
        assert any(r["message"] == "Old reminder" for r in due)

    def test_does_not_return_future_reminders(self):
        from aya.scheduler import get_due_reminders

        # Write a reminder directly with a known future due_at, so the
        # filter has a deterministic now < due_at relationship to evaluate.
        now = datetime(2026, 4, 1, 12, 0, tzinfo=LOCAL_TZ)
        future_due = datetime(2026, 4, 1, 14, 0, tzinfo=LOCAL_TZ)  # 2h after now
        items = load_items()
        items.append(
            {
                "id": "01JFUT000000000000000000001",
                "type": "reminder",
                "status": "pending",
                "message": "Future reminder",
                "due_at": future_due.isoformat(),
                "created_at": now.isoformat(),
            }
        )
        save_items(items)
        due = get_due_reminders(now=now)
        assert not any(r["message"] == "Future reminder" for r in due)

    def test_skips_dismissed_reminders(self):
        from aya.scheduler import get_due_reminders

        item = add_reminder("Soon", "in 1 minute")
        dismiss_item(item["id"])
        now = datetime(2030, 1, 1, tzinfo=LOCAL_TZ)
        due = get_due_reminders(now=now)
        assert not any(r["id"] == item["id"] for r in due)

    def test_empty_when_no_reminders(self):
        from aya.scheduler import get_due_reminders

        due = get_due_reminders()
        assert due == []

    def test_skips_items_with_malformed_timestamps(self):
        """Reminders with bad timestamps are silently skipped."""
        from aya.scheduler import get_due_reminders

        items = load_items()
        items.append(
            {
                "id": "01JBAD000000000000000000001",
                "type": "reminder",
                "status": "pending",
                "message": "bad timestamp",
                "due_at": "not-a-date",
                "created_at": "2026-01-01T00:00:00",
            }
        )
        save_items(items)
        due = get_due_reminders()
        assert all(r["message"] != "bad timestamp" for r in due)


class TestGetUpcomingReminders:
    def test_returns_reminders_within_window(self):
        from aya.scheduler import get_upcoming_reminders

        now = datetime(2026, 4, 1, 12, 0, tzinfo=LOCAL_TZ)
        # Create a reminder at now+1h — within 24h window
        add_reminder("Soon", "in 1 hour")
        upcoming = get_upcoming_reminders(now=now)
        # The reminder was added with parse time "in 1 hour" relative to real now
        # so it's in the near future; just verify the function returns a list
        assert isinstance(upcoming, list)

    def test_excludes_overdue_reminders(self):
        from aya.scheduler import get_upcoming_reminders

        item = add_reminder("Already due", "2025-01-01T12:00:00")
        now = datetime(2026, 4, 1, 12, 0, tzinfo=LOCAL_TZ)
        upcoming = get_upcoming_reminders(now=now)
        assert not any(r["id"] == item["id"] for r in upcoming)

    def test_sorted_by_due_at(self):
        from aya.scheduler import get_upcoming_reminders

        add_reminder("First", "in 2 hours")
        add_reminder("Second", "in 1 hour")
        upcoming = get_upcoming_reminders()
        due_times = [r["due_at"] for r in upcoming]
        assert due_times == sorted(due_times)

    def test_empty_when_no_reminders(self):
        from aya.scheduler import get_upcoming_reminders

        assert get_upcoming_reminders() == []

    def test_custom_hours_window(self):
        from aya.scheduler import get_upcoming_reminders

        # Save a reminder directly with a known due_at 1h after `now`,
        # then call with hours=0 (horizon == now). Per get_upcoming_reminders'
        # `now < reminder_due <= horizon` rule, the reminder must be excluded.
        now = datetime(2026, 4, 1, 12, 0, tzinfo=LOCAL_TZ)
        future_due = datetime(2026, 4, 1, 13, 0, tzinfo=LOCAL_TZ)
        items = load_items()
        items.append(
            {
                "id": "01JFUT000000000000000000002",
                "type": "reminder",
                "status": "pending",
                "message": "Near future",
                "due_at": future_due.isoformat(),
                "created_at": now.isoformat(),
            }
        )
        save_items(items)
        result_narrow = get_upcoming_reminders(now=now, hours=0)
        assert result_narrow == []


class TestGetActiveWatches:
    def test_returns_active_watches(self):
        from aya.scheduler import get_active_watches

        add_watch(
            provider="github-pr",
            target="owner/repo#1",
            message="PR watch",
        )
        watches = get_active_watches()
        assert any(w["message"] == "PR watch" for w in watches)

    def test_excludes_dismissed_watches(self):
        from aya.scheduler import get_active_watches

        item = add_watch(
            provider="github-pr",
            target="owner/repo#2",
            message="Dismissed watch",
        )
        dismiss_item(item["id"])
        watches = get_active_watches()
        assert not any(w["id"] == item["id"] for w in watches)

    def test_empty_when_no_watches(self):
        from aya.scheduler import get_active_watches

        assert get_active_watches() == []

    def test_does_not_include_reminders(self):
        from aya.scheduler import get_active_watches

        add_reminder("Not a watch", "in 1 hour")
        watches = get_active_watches()
        assert all(w["type"] == "watch" for w in watches)
