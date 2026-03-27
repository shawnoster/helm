"""Tests for idle-aware back-off: parse_duration, parse_work_hours,
record_activity, get_last_activity, is_idle, is_within_work_hours,
and get_pending filtering logic."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from aya.scheduler import (
    LOCAL_TZ,
    add_recurring,
    get_last_activity,
    get_pending,
    is_idle,
    is_within_work_hours,
    load_items,
    parse_duration,
    parse_work_hours,
    record_activity,
    save_items,
)


@pytest.fixture(autouse=True)
def _isolate_scheduler(tmp_path, monkeypatch):
    """Point scheduler globals at tmp paths so tests don't touch real data."""
    scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
    alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
    activity_file = tmp_path / "assistant" / "memory" / "activity.json"
    scheduler_file.parent.mkdir(parents=True)
    scheduler_file.write_text(json.dumps({"items": []}))
    alerts_file.write_text(json.dumps({"alerts": []}))

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)
    monkeypatch.setattr("aya.scheduler.ACTIVITY_FILE", activity_file)


# ── parse_duration ────────────────────────────────────────────────────────────


class TestParseDuration:
    def test_minutes_short(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_minutes_long(self):
        assert parse_duration("45min") == timedelta(minutes=45)

    def test_hours_short(self):
        assert parse_duration("1h") == timedelta(hours=1)

    def test_hours_long(self):
        assert parse_duration("2hr") == timedelta(hours=2)

    def test_hours_and_minutes(self):
        assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)

    def test_hours_and_minutes_with_spaces(self):
        assert parse_duration("2h 15m") == timedelta(hours=2, minutes=15)

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse duration"):
            parse_duration("gibberish")

    def test_zero_raises(self):
        with pytest.raises(ValueError):
            parse_duration("0m")


# ── parse_work_hours ──────────────────────────────────────────────────────────


class TestParseWorkHours:
    def test_basic(self):
        start, end = parse_work_hours("08:00-18:00")
        assert start == (8, 0)
        assert end == (18, 0)

    def test_no_leading_zero(self):
        start, end = parse_work_hours("9:00-17:30")
        assert start == (9, 0)
        assert end == (17, 30)

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse work hours"):
            parse_work_hours("8am-6pm")

    def test_invalid_missing_minutes_raises(self):
        with pytest.raises(ValueError, match="Cannot parse work hours"):
            parse_work_hours("08-18")


# ── is_within_work_hours ──────────────────────────────────────────────────────


class TestIsWithinWorkHours:
    def test_inside_window(self):
        now = datetime(2026, 3, 27, 10, 0, tzinfo=LOCAL_TZ)
        assert is_within_work_hours("08:00-18:00", now) is True

    def test_before_window(self):
        now = datetime(2026, 3, 27, 7, 59, tzinfo=LOCAL_TZ)
        assert is_within_work_hours("08:00-18:00", now) is False

    def test_at_start_of_window(self):
        now = datetime(2026, 3, 27, 8, 0, tzinfo=LOCAL_TZ)
        assert is_within_work_hours("08:00-18:00", now) is True

    def test_at_end_of_window_exclusive(self):
        now = datetime(2026, 3, 27, 18, 0, tzinfo=LOCAL_TZ)
        assert is_within_work_hours("08:00-18:00", now) is False

    def test_after_window(self):
        now = datetime(2026, 3, 27, 19, 0, tzinfo=LOCAL_TZ)
        assert is_within_work_hours("08:00-18:00", now) is False

    def test_empty_string_always_true(self):
        now = datetime(2026, 3, 27, 2, 0, tzinfo=LOCAL_TZ)
        assert is_within_work_hours("", now) is True


# ── record_activity / get_last_activity ───────────────────────────────────────


class TestActivityTracking:
    def test_record_and_read(self):
        now = datetime(2026, 3, 27, 14, 0, tzinfo=LOCAL_TZ)
        record_activity(now)
        last = get_last_activity()
        assert last is not None
        assert abs((last - now).total_seconds()) < 1

    def test_no_activity_file_returns_none(self):
        assert get_last_activity() is None

    def test_record_updates_existing(self):
        t1 = datetime(2026, 3, 27, 10, 0, tzinfo=LOCAL_TZ)
        t2 = datetime(2026, 3, 27, 14, 0, tzinfo=LOCAL_TZ)
        record_activity(t1)
        record_activity(t2)
        last = get_last_activity()
        assert last is not None
        assert abs((last - t2).total_seconds()) < 1


# ── is_idle ───────────────────────────────────────────────────────────────────


class TestIsIdle:
    def test_not_idle_when_recently_active(self):
        activity_time = datetime(2026, 3, 27, 14, 0, tzinfo=LOCAL_TZ)
        check_time = datetime(2026, 3, 27, 14, 20, tzinfo=LOCAL_TZ)  # 20 min later
        record_activity(activity_time)
        assert is_idle("30m", check_time) is False

    def test_idle_after_threshold(self):
        activity_time = datetime(2026, 3, 27, 14, 0, tzinfo=LOCAL_TZ)
        check_time = datetime(2026, 3, 27, 14, 31, tzinfo=LOCAL_TZ)  # 31 min later
        record_activity(activity_time)
        assert is_idle("30m", check_time) is True

    def test_exactly_at_threshold_is_idle(self):
        activity_time = datetime(2026, 3, 27, 14, 0, tzinfo=LOCAL_TZ)
        check_time = datetime(2026, 3, 27, 14, 30, tzinfo=LOCAL_TZ)  # exactly 30m
        record_activity(activity_time)
        assert is_idle("30m", check_time) is True

    def test_one_minute_before_threshold_is_not_idle(self):
        activity_time = datetime(2026, 3, 27, 14, 0, tzinfo=LOCAL_TZ)
        check_time = datetime(2026, 3, 27, 14, 29, tzinfo=LOCAL_TZ)  # 29 min — just under
        record_activity(activity_time)
        assert is_idle("30m", check_time) is False

    def test_no_activity_is_not_idle(self):
        # No activity recorded yet → treat as active (first-run safety)
        check_time = datetime(2026, 3, 27, 14, 0, tzinfo=LOCAL_TZ)
        assert is_idle("30m", check_time) is False

    def test_empty_threshold_never_idle(self):
        activity_time = datetime(2026, 3, 27, 8, 0, tzinfo=LOCAL_TZ)
        check_time = datetime(2026, 3, 27, 20, 0, tzinfo=LOCAL_TZ)  # 12h later
        record_activity(activity_time)
        assert is_idle("", check_time) is False


# ── add_recurring with idle fields ────────────────────────────────────────────


class TestAddRecurringIdleFields:
    def test_stores_idle_back_off(self):
        item = add_recurring("Test", "27 * * * *", idle_back_off="30m")
        assert item["idle_back_off"] == "30m"

    def test_stores_only_during(self):
        item = add_recurring("Test", "27 * * * *", only_during="08:00-18:00")
        assert item["only_during"] == "08:00-18:00"

    def test_omits_idle_fields_when_empty(self):
        item = add_recurring("Test", "27 * * * *")
        assert "idle_back_off" not in item
        assert "only_during" not in item

    def test_persists_fields_to_disk(self):
        add_recurring("Nudge", "27 * * * *", idle_back_off="1h", only_during="09:00-17:00")
        items = load_items()
        assert items[0]["idle_back_off"] == "1h"
        assert items[0]["only_during"] == "09:00-17:00"

    def test_invalid_idle_back_off_raises(self):
        with pytest.raises(ValueError):
            add_recurring("Bad", "27 * * * *", idle_back_off="bad-format")

    def test_invalid_only_during_raises(self):
        with pytest.raises(ValueError):
            add_recurring("Bad", "27 * * * *", only_during="9am-5pm")


# ── get_pending filtering ─────────────────────────────────────────────────────


class TestGetPendingFiltering:
    def test_no_suppression_without_fields(self):
        add_recurring("Plain cron", "27 * * * *")
        pending = get_pending("test-instance")
        assert len(pending["session_crons"]) == 1
        assert len(pending["suppressed_crons"]) == 0

    def test_suppressed_when_idle(self):
        activity_time = datetime(2026, 3, 27, 8, 0, tzinfo=LOCAL_TZ)
        record_activity(activity_time)

        add_recurring("Stand-and-move", "27 * * * *", idle_back_off="30m")

        # Now check 2h after last activity — idle
        from unittest.mock import patch

        check_time = datetime(2026, 3, 27, 10, 0, tzinfo=LOCAL_TZ)
        with patch("aya.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = check_time
            mock_dt.fromisoformat = datetime.fromisoformat
            pending = get_pending("test-instance")

        assert len(pending["session_crons"]) == 0
        assert len(pending["suppressed_crons"]) == 1
        assert "idle" in pending["suppressed_crons"][0]["reason"]

    def test_not_suppressed_when_active(self):
        now = datetime(2026, 3, 27, 10, 0, tzinfo=LOCAL_TZ)
        recent = now - timedelta(minutes=10)
        record_activity(recent)

        add_recurring("Stand-and-move", "27 * * * *", idle_back_off="30m")

        from unittest.mock import patch

        with patch("aya.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            pending = get_pending("test-instance")

        assert len(pending["session_crons"]) == 1
        assert len(pending["suppressed_crons"]) == 0

    def test_suppressed_outside_work_hours(self):
        add_recurring("Work cron", "27 * * * *", only_during="08:00-18:00")

        from unittest.mock import patch

        # 22:00 is outside 08:00-18:00
        outside_time = datetime(2026, 3, 27, 22, 0, tzinfo=LOCAL_TZ)
        with patch("aya.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = outside_time
            mock_dt.fromisoformat = datetime.fromisoformat
            pending = get_pending("test-instance")

        assert len(pending["session_crons"]) == 0
        assert len(pending["suppressed_crons"]) == 1
        assert "work hours" in pending["suppressed_crons"][0]["reason"]

    def test_not_suppressed_within_work_hours(self):
        add_recurring("Work cron", "27 * * * *", only_during="08:00-18:00")

        from unittest.mock import patch

        inside_time = datetime(2026, 3, 27, 10, 0, tzinfo=LOCAL_TZ)
        with patch("aya.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = inside_time
            mock_dt.fromisoformat = datetime.fromisoformat
            pending = get_pending("test-instance")

        assert len(pending["session_crons"]) == 1
        assert len(pending["suppressed_crons"]) == 0

    def test_work_hours_check_takes_priority_over_idle(self):
        """Outside work hours suppression should take effect even without idle_back_off."""
        save_items(
            [
                {
                    "id": "wh-only",
                    "type": "recurring",
                    "status": "active",
                    "created_at": datetime.now(LOCAL_TZ).isoformat(),
                    "message": "Work-hours only cron",
                    "tags": [],
                    "session_required": True,
                    "cron": "27 * * * *",
                    "prompt": "test",
                    "only_during": "08:00-18:00",
                }
            ]
        )
        from unittest.mock import patch

        outside_time = datetime(2026, 3, 27, 20, 0, tzinfo=LOCAL_TZ)
        with patch("aya.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = outside_time
            mock_dt.fromisoformat = datetime.fromisoformat
            pending = get_pending("test-instance")

        assert len(pending["suppressed_crons"]) == 1

    def test_suppressed_crons_in_result_even_when_json_format(self):
        """get_pending always returns suppressed_crons key."""
        pending = get_pending("test-instance")
        assert "suppressed_crons" in pending
