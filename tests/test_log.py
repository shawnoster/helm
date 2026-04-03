"""Tests for the daily progress logging module."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from aya.cli import app
from aya.log import (
    _PROGRESS_HEADING,
    _append_under_progress,
    _format_entry,
    _recent_packet_count,
    append_entry,
    auto_log,
    show_entries,
)

runner = CliRunner()
MDT = ZoneInfo("America/Denver")


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    """Point log at temp directories so tests don't touch real data."""
    notebook = tmp_path / "notebook"
    daily = notebook / "daily"
    daily.mkdir(parents=True)

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"notebook_path": str(notebook)}))

    log_state = tmp_path / "log_state.json"
    packets_dir = tmp_path / "packets"
    packets_dir.mkdir()

    monkeypatch.setattr("aya.log.LOG_STATE_FILE", log_state)
    monkeypatch.setattr("aya.log.PACKETS_DIR", packets_dir)
    monkeypatch.setattr("aya.log.get_notebook_path", lambda: notebook)
    monkeypatch.setattr("aya.config.CONFIG_PATH", config_path)


# ── format_entry ─────────────────────────────────────────────────────────────


class TestFormatEntry:
    def test_basic_entry(self):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        result = _format_entry(now, "shipped feature X")
        assert result == "[14:30 MDT] shipped feature X"

    def test_entry_with_tags(self):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        result = _format_entry(now, "fixed auth bug", tags="pr/174,fix/170")
        assert result == "[14:30 MDT] fixed auth bug — pr/174,fix/170"

    def test_entry_without_tags(self):
        now = datetime(2026, 4, 3, 9, 5, tzinfo=MDT)
        result = _format_entry(now, "morning standup")
        assert "—" not in result


# ── append_under_progress ────────────────────────────────────────────────────


class TestAppendUnderProgress:
    def test_creates_section_if_missing(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# 2026-04-03\n")
        _append_under_progress(f, "[14:30 MDT] test entry")
        text = f.read_text()
        assert _PROGRESS_HEADING in text
        assert "[14:30 MDT] test entry" in text

    def test_appends_to_existing_section(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# 2026-04-03\n\n## Progress\n\n[10:00 MDT] first entry\n")
        _append_under_progress(f, "[14:30 MDT] second entry")
        text = f.read_text()
        assert "[10:00 MDT] first entry" in text
        assert "[14:30 MDT] second entry" in text
        # First entry should come before second
        assert text.index("first entry") < text.index("second entry")

    def test_inserts_before_next_heading(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text(
            "# 2026-04-03\n\n## Progress\n\n[10:00 MDT] first\n\n## Notes\n\nSome notes.\n"
        )
        _append_under_progress(f, "[14:30 MDT] second")
        text = f.read_text()
        progress_pos = text.index("## Progress")
        notes_pos = text.index("## Notes")
        second_pos = text.index("[14:30 MDT] second")
        assert progress_pos < second_pos < notes_pos


# ── append_entry ─────────────────────────────────────────────────────────────


class TestAppendEntry:
    def test_creates_daily_file(self, tmp_path):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        daily, _entry = append_entry("test message", now=now)
        assert daily.exists()
        assert daily.name == "2026-04-03.md"
        text = daily.read_text()
        assert "# 2026-04-03" in text
        assert "[14:30 MDT] test message" in text

    def test_appends_to_existing_file(self, tmp_path):
        now1 = datetime(2026, 4, 3, 10, 0, tzinfo=MDT)
        now2 = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        append_entry("first", now=now1)
        append_entry("second", now=now2)
        notebook = tmp_path / "notebook"
        daily = notebook / "daily" / "2026-04-03.md"
        text = daily.read_text()
        assert "first" in text
        assert "second" in text

    def test_with_tags(self, tmp_path):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        _, entry = append_entry("fixed bug", tags="pr/174", now=now)
        assert "pr/174" in entry

    def test_updates_log_state(self, tmp_path, monkeypatch):
        log_state = tmp_path / "log_state.json"
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        append_entry("test", now=now)
        assert log_state.exists()
        state = json.loads(log_state.read_text())
        assert "last_logged_at" in state


# ── show_entries ─────────────────────────────────────────────────────────────


class TestShowEntries:
    def test_empty_when_no_file(self):
        dt = datetime(2099, 1, 1, tzinfo=MDT)
        entries = show_entries(date=dt)
        assert entries == []

    def test_parses_entries(self, tmp_path):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        append_entry("shipped feature", tags="pr/175", now=now)
        entries = show_entries(date=now)
        assert len(entries) == 1
        assert entries[0]["message"] == "shipped feature"
        assert entries[0]["tags"] == "pr/175"

    def test_multiple_entries(self, tmp_path):
        now1 = datetime(2026, 4, 3, 10, 0, tzinfo=MDT)
        now2 = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        append_entry("morning work", now=now1)
        append_entry("afternoon work", now=now2)
        entries = show_entries(date=now1)
        assert len(entries) == 2


# ── auto_log ─────────────────────────────────────────────────────────────────


class TestAutoLog:
    def test_returns_none_when_no_activity(self):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        result = auto_log(now=now)
        assert result is None

    def test_skips_within_dedup_window(self, tmp_path):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        log_state = tmp_path / "log_state.json"
        log_state.write_text(
            json.dumps(
                {
                    "last_logged_at": now.isoformat(),
                }
            )
        )
        result = auto_log(now=now)
        assert result is None

    def test_logs_when_packets_exist(self, tmp_path):
        now = datetime.now(MDT)
        packets_dir = tmp_path / "packets"
        pkt = packets_dir / "test.json"
        pkt.write_text("{}")
        result = auto_log(now=now)
        assert result is not None
        _, entry = result
        assert "packet" in entry

    def test_detects_recent_activity(self, tmp_path, monkeypatch):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        # Mock get_last_activity to return recent time
        monkeypatch.setattr(
            "aya.log.get_last_activity",
            lambda: now,
        )
        result = auto_log(now=now)
        assert result is not None
        _, entry = result
        assert "active session" in entry


# ── recent_packet_count ──────────────────────────────────────────────────────


class TestRecentPacketCount:
    def test_zero_when_empty(self, tmp_path):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        assert _recent_packet_count(now) == 0

    def test_counts_recent_packets(self, tmp_path):
        now = datetime.now(MDT)
        packets_dir = tmp_path / "packets"
        (packets_dir / "a.json").write_text("{}")
        (packets_dir / "b.json").write_text("{}")
        count = _recent_packet_count(now)
        assert count == 2

    def test_ignores_non_json(self, tmp_path):
        now = datetime(2026, 4, 3, 14, 30, tzinfo=MDT)
        packets_dir = tmp_path / "packets"
        (packets_dir / "a.txt").write_text("not json")
        assert _recent_packet_count(now) == 0


# ── CLI integration ──────────────────────────────────────────────────────────


class TestLogCLI:
    def test_append_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AYA_FORMAT", "json")
        result = runner.invoke(app, ["log", "append", "-m", "test entry"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "entry" in data
        assert "test entry" in data["entry"]

    def test_show_json_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AYA_FORMAT", "json")
        result = runner.invoke(app, ["log", "show"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["entries"] == []

    def test_auto_json_no_activity(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AYA_FORMAT", "json")
        # Ensure no recent activity signal
        monkeypatch.setattr("aya.log.get_last_activity", lambda: None)
        result = runner.invoke(app, ["log", "auto"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["logged"] is False

    def test_show_invalid_date(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AYA_FORMAT", "json")
        result = runner.invoke(app, ["log", "show", "--date", "not-a-date"])
        assert result.exit_code == 1
