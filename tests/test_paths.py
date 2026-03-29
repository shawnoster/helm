"""Tests for aya.paths — centralized path resolution and migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def aya_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AYA_HOME at a temp directory and reload paths module."""
    home = tmp_path / ".aya"
    monkeypatch.setenv("AYA_HOME", str(home))

    # Reload paths so it picks up the env var
    import aya.paths

    monkeypatch.setattr(aya.paths, "AYA_HOME", home)
    monkeypatch.setattr(aya.paths, "PROFILE_PATH", home / "profile.json")
    monkeypatch.setattr(aya.paths, "CONFIG_PATH", home / "config.json")
    monkeypatch.setattr(aya.paths, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(aya.paths, "SCHEDULER_FILE", home / "memory" / "scheduler.json")
    monkeypatch.setattr(aya.paths, "ALERTS_FILE", home / "memory" / "alerts.json")
    monkeypatch.setattr(aya.paths, "ACTIVITY_FILE", home / "memory" / "activity.json")
    monkeypatch.setattr(aya.paths, "LOCK_FILE", home / "memory" / ".scheduler.lock")
    monkeypatch.setattr(aya.paths, "CLAIMS_DIR", home / "memory" / "claims")
    monkeypatch.setattr(aya.paths, "CRON_SCHEDULES_PATH", home / "memory" / "cron-schedules.md")
    return home


@pytest.fixture
def aya_scheduler(aya_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up scheduler isolation alongside aya_home."""
    mem = aya_home / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    scheduler_file = mem / "scheduler.json"
    alerts_file = mem / "alerts.json"
    scheduler_file.write_text(json.dumps({"items": []}))
    alerts_file.write_text(json.dumps({"alerts": []}))

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)
    return aya_home


class TestSeedDefaults:
    def test_seeds_health_break(self, aya_scheduler: Path) -> None:
        from aya.paths import seed_defaults
        from aya.scheduler import load_items

        seeded = seed_defaults()
        assert len(seeded) == 1
        assert "health-break" in seeded[0]

        items = load_items()
        recurring = [i for i in items if i.get("type") == "recurring"]
        assert len(recurring) == 1
        assert recurring[0]["message"] == "health-break"
        assert recurring[0]["cron"] == "*/20 * * * *"
        assert recurring[0]["idle_back_off"] == "10m"

    def test_skips_when_recurring_exists(self, aya_scheduler: Path) -> None:
        from aya.paths import seed_defaults

        # First call seeds
        seed_defaults()
        # Second call should skip
        seeded = seed_defaults()
        assert seeded == []


class TestEnsureHome:
    def test_creates_memory_dir(self, aya_home: Path) -> None:
        from aya.paths import ensure_home

        ensure_home()
        assert (aya_home / "memory").is_dir()

    def test_idempotent(self, aya_home: Path) -> None:
        from aya.paths import ensure_home

        ensure_home()
        ensure_home()  # second call should not raise
        assert (aya_home / "memory").is_dir()


class TestMigration:
    def _setup_legacy_workspace(self, tmp_path: Path) -> Path:
        """Create old workspace-relative layout with sample data."""
        ws = tmp_path / "workspace"
        mem = ws / "assistant" / "memory"
        mem.mkdir(parents=True)

        (ws / "assistant" / "profile.json").write_text('{"alias": "Ace"}')
        (ws / "assistant" / "config.json").write_text('{"key": "val"}')
        (mem / "scheduler.json").write_text(json.dumps({"items": []}))
        (mem / "alerts.json").write_text(json.dumps({"alerts": []}))
        (mem / "activity.json").write_text('{"last_activity_at": "2026-03-28T10:00:00"}')
        (mem / "activity-tracker.md").write_text("## 2026-03-28\n- did stuff")
        return ws

    def test_migrates_files(
        self, tmp_path: Path, aya_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = self._setup_legacy_workspace(tmp_path)
        monkeypatch.chdir(ws)

        from aya.paths import migrate_if_needed

        migrated = migrate_if_needed()
        assert len(migrated) > 0
        assert (aya_home / "profile.json").exists()
        assert (aya_home / "memory" / "scheduler.json").exists()
        assert (aya_home / "memory" / "alerts.json").exists()
        assert (aya_home / "memory" / "activity.json").exists()

        # Old files should be gone (moved, not copied)
        assert not (ws / "assistant" / "profile.json").exists()
        assert not (ws / "assistant" / "memory" / "scheduler.json").exists()

    def test_skips_when_already_migrated(
        self, tmp_path: Path, aya_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = self._setup_legacy_workspace(tmp_path)
        monkeypatch.chdir(ws)

        # Pre-create aya home with scheduler.json
        (aya_home / "memory").mkdir(parents=True)
        (aya_home / "memory" / "scheduler.json").write_text(json.dumps({"items": []}))

        from aya.paths import migrate_if_needed

        migrated = migrate_if_needed()
        assert migrated == []
        # Old files should still exist (not touched)
        assert (ws / "assistant" / "profile.json").exists()

    def test_skips_when_no_legacy_workspace(
        self, aya_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)

        from aya.paths import migrate_if_needed

        migrated = migrate_if_needed()
        assert migrated == []
