"""JSON output contract tests for AI consumers.

These tests validate the structure (schema) of JSON output from every
``aya`` command that supports ``--format json``.  They exist to catch
breaking changes in the machine-readable contract — the exact *values*
may change between runs, but the *shape* (keys, types, nesting) must
remain stable.

Every test explicitly passes ``--format json`` rather than relying on
AUTO format resolution.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aya.cli import app

runner = CliRunner()


# ── Isolation fixture ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Isolate scheduler + status from real data files."""
    scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
    alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
    scheduler_file.parent.mkdir(parents=True)
    scheduler_file.write_text(json.dumps({"items": []}))
    alerts_file.write_text(json.dumps({"alerts": []}))

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

    # Status module reads its own PROFILE and checks _paths.SCHEDULER_FILE —
    # patch both so contract tests never touch real ~/.aya files.
    fake_profile = tmp_path / "profile.json"
    fake_profile.write_text(json.dumps({"aya": {"instances": {}, "trusted_keys": {}}}))
    monkeypatch.setattr("aya.status.PROFILE", fake_profile)
    monkeypatch.setattr("aya.paths.SCHEDULER_FILE", scheduler_file)

    # Stub scheduler helpers so _gather_status doesn't touch real disk.
    monkeypatch.setattr("aya.status.get_unseen_alerts", list)
    monkeypatch.setattr("aya.status.get_due_reminders", lambda *a, **kw: [])
    monkeypatch.setattr("aya.status.get_upcoming_reminders", lambda *a, **kw: [])
    monkeypatch.setattr("aya.status.get_active_watches", list)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _invoke_json(*args: str) -> dict | list:
    """Invoke a CLI command with --format json and return parsed output."""
    result = runner.invoke(app, [*args, "--format", "json"])
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
    return json.loads(result.output)


# ── version ──────────────────────────────────────────────────────────────────


class TestVersionContract:
    def test_valid_json_with_version_key(self):
        """aya version --format json -> {"version": str}"""
        data = _invoke_json("version")
        assert "version" in data
        assert isinstance(data["version"], str)
        assert len(data["version"]) > 0


# ── status ───────────────────────────────────────────────────────────────────


class TestStatusContract:
    def test_top_level_keys(self):
        """aya status --format json has required top-level keys."""
        data = _invoke_json("status")
        for key in ("greeting", "systems", "alerts", "watches"):
            assert key in data, f"missing top-level key: {key}"

    def test_systems_shape(self):
        """systems must have ok (bool) and checks (list)."""
        data = _invoke_json("status")
        systems = data["systems"]
        assert isinstance(systems["ok"], bool)
        assert isinstance(systems["checks"], list)

    def test_systems_check_shape(self):
        """Each check must have name, ok, detail."""
        data = _invoke_json("status")
        for check in data["systems"]["checks"]:
            assert "name" in check
            assert "ok" in check
            assert isinstance(check["ok"], bool)
            assert "detail" in check

    def test_alerts_is_list(self):
        """alerts must be a list."""
        data = _invoke_json("status")
        assert isinstance(data["alerts"], list)

    def test_watches_is_list(self):
        """watches must be a list."""
        data = _invoke_json("status")
        assert isinstance(data["watches"], list)

    def test_extra_stable_keys(self):
        """due, upcoming, next_eval, perspective are also present."""
        data = _invoke_json("status")
        for key in ("due", "upcoming", "next_eval", "perspective"):
            assert key in data, f"missing key: {key}"


# ── inbox ────────────────────────────────────────────────────────────────────


class TestInboxContract:
    """aya inbox --format json returns a list of packet dicts.

    Inbox requires a profile and relay connection.  We mock the relay to
    return an empty list so the test exercises the serialisation path
    without network I/O.
    """

    def test_empty_inbox_is_wrapped_object(self, tmp_path, monkeypatch):
        """An inbox with no packets returns {"packets": []}."""
        from aya.identity import Identity, Profile

        profile_path = tmp_path / "profile.json"
        identity = Identity.generate("default")
        profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
        profile.instances["default"] = identity
        profile.save(profile_path)

        async def _empty_fetch(self):
            return
            yield  # pragma: no cover — makes this an async generator

        monkeypatch.setattr("aya.relay.RelayClient.fetch_pending", _empty_fetch)

        result = runner.invoke(
            app,
            ["inbox", "--format", "json", "--profile", str(profile_path)],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert "packets" in data
        assert isinstance(data["packets"], list)
        assert data["packets"] == []

    def test_packet_shape(self, tmp_path, monkeypatch):
        """Each packet dict must have id, intent, from_did, trusted."""
        from aya.identity import Identity, Profile
        from aya.packet import Packet

        profile_path = tmp_path / "profile.json"
        identity = Identity.generate("default")
        profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
        profile.instances["default"] = identity
        profile.save(profile_path)

        fake_packet = Packet(
            **{"from": identity.did, "to": identity.did},
            intent="note",
            content="hello",
        )

        async def _one_packet(self):
            yield fake_packet

        monkeypatch.setattr("aya.relay.RelayClient.fetch_pending", _one_packet)

        result = runner.invoke(
            app,
            ["inbox", "--format", "json", "--profile", str(profile_path)],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert "packets" in data
        packets = data["packets"]
        assert isinstance(packets, list)
        assert len(packets) == 1
        pkt = packets[0]
        for key in ("id", "intent", "from_did", "trusted"):
            assert key in pkt, f"missing packet key: {key}"


# ── schedule list ────────────────────────────────────────────────────────────


class TestScheduleListContract:
    def test_empty_list_is_wrapped_object(self):
        """schedule list --format json returns {"items": []}."""
        data = _invoke_json("schedule", "list")
        assert isinstance(data, dict)
        assert "items" in data
        assert isinstance(data["items"], list)
        assert data["items"] == []

    def test_item_shape_when_populated(self):
        """Each item has id, type, and status keys."""
        from aya.scheduler import add_recurring, add_reminder

        add_reminder("Test reminder", "in 1 hour")
        add_recurring("Test cron", "0 * * * *", prompt="run thing")

        data = _invoke_json("schedule", "list")
        assert isinstance(data, dict)
        assert "items" in data
        items = data["items"]
        assert isinstance(items, list)
        assert len(items) >= 2
        for item in items:
            assert "id" in item
            assert "type" in item


# ── schedule pending ─────────────────────────────────────────────────────────


class TestSchedulePendingContract:
    def test_top_level_keys(self):
        """schedule pending --format json has alerts and session_crons."""
        data = _invoke_json("schedule", "pending")
        assert "alerts" in data
        assert "session_crons" in data
        assert isinstance(data["alerts"], list)
        assert isinstance(data["session_crons"], list)

    def test_instance_id_present(self):
        """instance_id key is included."""
        data = _invoke_json("schedule", "pending")
        assert "instance_id" in data


# ── schedule status ──────────────────────────────────────────────────────────


class TestScheduleStatusContract:
    def test_top_level_keys(self):
        """schedule status --format json has the expected overview keys."""
        data = _invoke_json("schedule", "status")
        for key in (
            "active_watches",
            "pending_reminders",
            "session_crons",
            "unseen_alerts",
            "recent_deliveries",
            "total_items",
            "total_alerts",
        ):
            assert key in data, f"missing key: {key}"

    def test_list_types(self):
        """List-valued keys are actually lists."""
        data = _invoke_json("schedule", "status")
        for key in (
            "active_watches",
            "pending_reminders",
            "session_crons",
            "unseen_alerts",
            "recent_deliveries",
        ):
            assert isinstance(data[key], list), f"{key} should be a list"

    def test_count_types(self):
        """Count-valued keys are integers."""
        data = _invoke_json("schedule", "status")
        assert isinstance(data["total_items"], int)
        assert isinstance(data["total_alerts"], int)


# ── schedule alerts ──────────────────────────────────────────────────────────


class TestScheduleAlertsContract:
    def test_empty_alerts_is_wrapped_object(self):
        """schedule alerts --format json returns {"alerts": []}."""
        data = _invoke_json("schedule", "alerts")
        assert isinstance(data, dict)
        assert "alerts" in data
        assert isinstance(data["alerts"], list)
        assert data["alerts"] == []

    def test_alert_shape_when_populated(self, tmp_path, monkeypatch):
        """Each alert has id, source_item_id, message, seen."""
        from aya import scheduler

        alerts = [
            {
                "id": "alert-001",
                "source_item_id": "watch-abcd1234",
                "created_at": "2026-03-29T10:00:00-07:00",
                "message": "PR 42 merged",
                "seen": False,
            }
        ]
        scheduler.ALERTS_FILE.write_text(json.dumps({"alerts": alerts}))

        data = _invoke_json("schedule", "alerts")
        assert isinstance(data, dict)
        assert "alerts" in data
        alerts_list = data["alerts"]
        assert isinstance(alerts_list, list)
        assert len(alerts_list) == 1
        alert = alerts_list[0]
        for key in ("id", "source_item_id", "message", "seen"):
            assert key in alert, f"missing alert key: {key}"


# ── schedule check ───────────────────────────────────────────────────────────


class TestScheduleCheckContract:
    def test_top_level_keys(self):
        """schedule check --format json has due and alerts."""
        data = _invoke_json("schedule", "check")
        assert "due" in data
        assert "alerts" in data
        assert isinstance(data["due"], list)
        assert isinstance(data["alerts"], list)
