"""Tests for the status module — readiness checks, greeting, time parsing, daily notes."""

from __future__ import annotations

from datetime import datetime

import pytest

from aya.status import (
    _exists,
    _greeting,
    _parse_block_header,
    _parse_daily_notes,
    _parse_time,
    _perspective,
    _read_json,
    _time_flavor,
)

# ── CheckResult / _exists ────────────────────────────────────────────────────


class TestCheckResult:
    def test_exists_true(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        result = _exists(f, "test file")
        assert result.ok is True
        assert result.name == "test file"

    def test_exists_false(self, tmp_path):
        result = _exists(tmp_path / "nope.txt", "missing")
        assert result.ok is False


# ── _read_json ───────────────────────────────────────────────────────────────


class TestReadJson:
    def test_valid_json(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        assert _read_json(f) == {"key": "value"}

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        assert _read_json(f) is None

    def test_missing_file(self, tmp_path):
        assert _read_json(tmp_path / "nope.json") is None

    def test_array_returns_none(self, tmp_path):
        f = tmp_path / "arr.json"
        f.write_text("[1, 2, 3]")
        assert _read_json(f) is None


# ── Greeting ─────────────────────────────────────────────────────────────────


class TestGreeting:
    def test_morning(self):
        now = datetime(2026, 3, 21, 8, 0)
        result = _greeting(now, "Shawn", "GSV Test Ship")
        assert "Good morning" in result
        assert "Shawn" in result
        assert "GSV Test Ship" in result

    def test_afternoon(self):
        now = datetime(2026, 3, 21, 14, 0)
        assert "Good afternoon" in _greeting(now, "Shawn", "Ship")

    def test_evening(self):
        now = datetime(2026, 3, 21, 19, 0)
        assert "Evening" in _greeting(now, "Shawn", "Ship")

    def test_late_night(self):
        now = datetime(2026, 3, 21, 23, 0)
        assert "Still at it" in _greeting(now, "Shawn", "Ship")

    def test_very_early(self):
        now = datetime(2026, 3, 21, 3, 0)
        assert "Still running" in _greeting(now, "Shawn", "Ship")


# ── Time flavor ──────────────────────────────────────────────────────────────


class TestTimeFlavor:
    def test_morning_coffee(self):
        now = datetime(2026, 3, 21, 7, 0)
        assert "Coffee" in _time_flavor(now)

    def test_focus_window(self):
        now = datetime(2026, 3, 21, 10, 0)
        assert "focus" in _time_flavor(now).lower()

    def test_afternoon(self):
        now = datetime(2026, 3, 21, 15, 0)
        assert "Afternoon" in _time_flavor(now)

    def test_unconventional(self):
        now = datetime(2026, 3, 21, 3, 0)
        assert "Unconventional" in _time_flavor(now)


# ── Time parsing ─────────────────────────────────────────────────────────────


class TestParseTime:
    def test_basic_am(self):
        result = _parse_time("9:00", pm_context=False)
        assert result.hour == 9
        assert result.minute == 0

    def test_pm_context(self):
        result = _parse_time("2:30", pm_context=True)
        assert result.hour == 14
        assert result.minute == 30

    def test_noon_no_pm(self):
        result = _parse_time("12:00", pm_context=False)
        assert result.hour == 0  # 12 AM = midnight

    def test_invalid(self):
        assert _parse_time("not a time") is None


# ── Block header parsing ─────────────────────────────────────────────────────


class TestParseBlockHeader:
    def test_shared_pm(self):
        start, end, _ = _parse_block_header("2:30–2:55 PM")
        assert start.hour == 14
        assert start.minute == 30
        assert end.hour == 14
        assert end.minute == 55

    def test_mixed_am_pm(self):
        start, end, _ = _parse_block_header("11:05 AM–12:00 PM")
        assert start.hour == 11
        assert end.hour == 12

    def test_single_time(self):
        start, end, _ = _parse_block_header("4:00 PM")
        assert start.hour == 16
        assert end.hour == 17  # defaults to +1 hour

    def test_no_time(self):
        start, end, _label = _parse_block_header("No time here")
        assert start is None
        assert end is None


# ── Daily notes parsing ──────────────────────────────────────────────────────


class TestParseDailyNotes:
    @pytest.fixture(autouse=True)
    def _setup_notes_dir(self, tmp_path, monkeypatch):
        self.notes_dir = tmp_path / "assistant" / "notes" / "daily"
        self.notes_dir.mkdir(parents=True)
        monkeypatch.setattr("aya.status.ASSISTANT", tmp_path / "assistant")

    def test_no_file(self):
        result = _parse_daily_notes("2026-03-21")
        assert result["found"] is False

    def test_empty_file(self):
        (self.notes_dir / "2026-03-21.md").write_text("# Daily Plan\n\nNothing here.\n")
        result = _parse_daily_notes("2026-03-21")
        assert result["found"] is True
        assert result["priorities"] == []

    def test_priorities_extracted(self):
        content = """\
# Daily Plan

## Priority Stack

```
1. Fix the bug
2. Review the PR
3. ✅ Already done
```
"""
        (self.notes_dir / "2026-03-21.md").write_text(content)
        result = _parse_daily_notes("2026-03-21")
        assert len(result["priorities"]) == 2
        assert "Fix the bug" in result["priorities"][0]
        assert "Review the PR" in result["priorities"][1]


# ── Perspective ──────────────────────────────────────────────────────────────


class TestPerspective:
    def test_returns_string(self):
        result = _perspective()
        assert isinstance(result, str)
        assert len(result) > 10

    def test_deterministic_per_day(self):
        assert _perspective() == _perspective()
