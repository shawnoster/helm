"""Tests for session-aware noise reduction — session lock, severity filtering, tick deferral."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from aya.scheduler import (
    SEVERITY_ACTIONABLE,
    SEVERITY_HEARTBEAT,
    SEVERITY_INFO,
    _passes_severity_filter,
    get_pending,
    is_session_active,
    run_tick,
    write_session_lock,
)


@pytest.fixture(autouse=True)
def _isolate_scheduler(tmp_path, monkeypatch):
    """Point scheduler at a temp directory so tests don't touch real data."""
    scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
    alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
    activity_file = tmp_path / "activity.json"
    session_lock_file = tmp_path / "session.lock"

    scheduler_file.parent.mkdir(parents=True)
    scheduler_file.write_text(json.dumps({"items": []}))
    alerts_file.write_text(json.dumps({"alerts": []}))

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)
    monkeypatch.setattr("aya.scheduler.ACTIVITY_FILE", activity_file)
    monkeypatch.setattr("aya.scheduler.SESSION_LOCK_FILE", session_lock_file)


# ── Session lock ────────────────────────────────────────────────────────────


class TestSessionLock:
    def test_write_and_detect_active(self):
        """Writing a session lock with recent activity makes is_session_active True."""
        from aya.scheduler import record_activity

        record_activity()  # also refreshes session lock
        assert is_session_active() is True

    def test_no_lock_means_inactive(self):
        """Without a session lock, is_session_active is False."""
        assert is_session_active() is False

    def test_lock_without_activity_means_inactive(self):
        """A session lock with no activity file is treated as inactive (stale)."""
        write_session_lock("test-instance")
        assert is_session_active() is False

    def test_stale_activity_means_inactive(self):
        """Activity older than 15 minutes makes the session inactive."""
        from aya.scheduler import _activity_file, _get_local_tz

        write_session_lock("test-instance")
        # Write activity from 20 minutes ago
        stale_time = datetime.now(_get_local_tz()) - timedelta(minutes=20)
        _activity_file().parent.mkdir(parents=True, exist_ok=True)
        _activity_file().write_text(json.dumps({"last_activity_at": stale_time.isoformat()}))
        assert is_session_active() is False


# ── Severity filtering ──────────────────────────────────────────────────────


class TestSeverityFilter:
    def _make_alert(self, severity: str) -> dict:
        return {
            "id": "test",
            "source_item_id": "s1",
            "created_at": datetime.now(UTC).isoformat(),
            "message": "Test",
            "details": {},
            "seen": False,
            "severity": severity,
        }

    def test_actionable_passes_actionable_filter(self):
        alert = self._make_alert(SEVERITY_ACTIONABLE)
        assert _passes_severity_filter(alert, SEVERITY_ACTIONABLE) is True

    def test_info_fails_actionable_filter(self):
        alert = self._make_alert(SEVERITY_INFO)
        assert _passes_severity_filter(alert, SEVERITY_ACTIONABLE) is False

    def test_heartbeat_fails_actionable_filter(self):
        alert = self._make_alert(SEVERITY_HEARTBEAT)
        assert _passes_severity_filter(alert, SEVERITY_ACTIONABLE) is False

    def test_actionable_passes_heartbeat_filter(self):
        alert = self._make_alert(SEVERITY_ACTIONABLE)
        assert _passes_severity_filter(alert, SEVERITY_HEARTBEAT) is True

    def test_info_passes_heartbeat_filter(self):
        alert = self._make_alert(SEVERITY_INFO)
        assert _passes_severity_filter(alert, SEVERITY_HEARTBEAT) is True

    def test_heartbeat_passes_heartbeat_filter(self):
        alert = self._make_alert(SEVERITY_HEARTBEAT)
        assert _passes_severity_filter(alert, SEVERITY_HEARTBEAT) is True

    def test_info_passes_info_filter(self):
        alert = self._make_alert(SEVERITY_INFO)
        assert _passes_severity_filter(alert, SEVERITY_INFO) is True

    def test_heartbeat_fails_info_filter(self):
        alert = self._make_alert(SEVERITY_HEARTBEAT)
        assert _passes_severity_filter(alert, SEVERITY_INFO) is False

    def test_missing_severity_treated_as_actionable(self):
        alert = self._make_alert(SEVERITY_ACTIONABLE)
        del alert["severity"]
        assert _passes_severity_filter(alert, SEVERITY_ACTIONABLE) is True


# ── run_tick with session active ────────────────────────────────────────────


class TestRunTickSessionActive:
    def test_tick_skips_poll_when_session_active(self):
        """When a session is active, run_tick should skip polling entirely."""
        with (
            patch("aya.scheduler.core.is_session_active", return_value=True),
            patch("aya.scheduler.core.run_poll") as mock_poll,
        ):
            result = run_tick(quiet=True)

        assert result.get("polls_skipped") is True
        mock_poll.assert_not_called()

    def test_tick_polls_when_no_session(self):
        """When no session is active, run_tick should poll normally."""
        with (
            patch("aya.scheduler.core.is_session_active", return_value=False),
            patch("aya.scheduler.core.run_poll") as mock_poll,
        ):
            result = run_tick(quiet=True)

        assert result.get("polls_skipped") is None
        mock_poll.assert_called_once_with(quiet=True)

    def test_tick_always_sweeps_and_expires(self):
        """Sweep and expiry happen regardless of session state."""
        with (
            patch("aya.scheduler.core.is_session_active", return_value=True),
            patch("aya.scheduler.core.run_poll"),
        ):
            result = run_tick(quiet=True)

        assert "claims_swept" in result
        assert "alerts_expired" in result


# ── get_pending with severity filtering ─────────────────────────────────────


class TestGetPendingSeverityFiltering:
    def test_default_filters_out_heartbeat(self):
        """Default min_severity=SEVERITY_ACTIONABLE filters out heartbeat alerts."""
        from aya.scheduler import _alerts_file

        _alerts_file().write_text(
            json.dumps(
                {
                    "alerts": [
                        {
                            "id": "a-actionable",
                            "source_item_id": "s1",
                            "created_at": datetime.now(UTC).isoformat(),
                            "message": "Important",
                            "details": {},
                            "seen": False,
                            "severity": SEVERITY_ACTIONABLE,
                        },
                        {
                            "id": "a-heartbeat",
                            "source_item_id": "s2",
                            "created_at": datetime.now(UTC).isoformat(),
                            "message": "Routine check",
                            "details": {},
                            "seen": False,
                            "severity": SEVERITY_HEARTBEAT,
                        },
                    ]
                }
            )
        )

        pending = get_pending("test-session", min_severity=SEVERITY_ACTIONABLE)
        assert len(pending["alerts"]) == 1
        assert pending["alerts"][0]["id"] == "a-actionable"

    def test_heartbeat_filter_includes_all(self):
        """min_severity=SEVERITY_HEARTBEAT includes all severity levels."""
        from aya.scheduler import _alerts_file

        _alerts_file().write_text(
            json.dumps(
                {
                    "alerts": [
                        {
                            "id": "a-actionable",
                            "source_item_id": "s1",
                            "created_at": datetime.now(UTC).isoformat(),
                            "message": "Important",
                            "details": {},
                            "seen": False,
                            "severity": SEVERITY_ACTIONABLE,
                        },
                        {
                            "id": "a-info",
                            "source_item_id": "s2",
                            "created_at": datetime.now(UTC).isoformat(),
                            "message": "FYI",
                            "details": {},
                            "seen": False,
                            "severity": SEVERITY_INFO,
                        },
                        {
                            "id": "a-heartbeat",
                            "source_item_id": "s3",
                            "created_at": datetime.now(UTC).isoformat(),
                            "message": "Routine",
                            "details": {},
                            "seen": False,
                            "severity": SEVERITY_HEARTBEAT,
                        },
                    ]
                }
            )
        )

        pending = get_pending("test-session", min_severity=SEVERITY_HEARTBEAT)
        assert len(pending["alerts"]) == 3
