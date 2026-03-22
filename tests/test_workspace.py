"""Tests for workspace.py — bootstrap_workspace, dotfile setup, idempotency."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aya.workspace import (
    DIRS,
    PRESERVED_ON_RESET,
    RESET_FILES,
    SKILL_NAMES,
    bootstrap_workspace,
    reset_workspace,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """Isolated fake home directory."""
    return tmp_path / "home"


# ── bootstrap_workspace ───────────────────────────────────────────────────────


class TestBootstrapWorkspace:
    def test_creates_expected_directories(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        for d in DIRS:
            assert (root / d).is_dir(), f"Expected directory {d} to exist"

    def test_creates_expected_files(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        expected_files = [
            "CLAUDE.md",
            "assistant/AGENTS.md",
            "assistant/CLAUDE.md",
            "assistant/persona.md",
            "assistant/config.json",
            "assistant/memory/README.md",
            "assistant/memory/scheduler.json",
            "Makefile",
        ]
        for f in expected_files:
            assert (root / f).exists(), f"Expected file {f} to exist"

    def test_claude_md_contains_root_path(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "myworkspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        content = (root / "CLAUDE.md").read_text()
        assert str(root) in content

    def test_config_json_has_correct_root(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        config = json.loads((root / "assistant/config.json").read_text())
        assert config["projects_dir"] == f"{root}/projects"
        assert f"{root}/code" in config["code_dirs"]

    def test_scheduler_json_starts_empty(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        data = json.loads((root / "assistant/memory/scheduler.json").read_text())
        assert data == {"items": []}


# ── Idempotency ───────────────────────────────────────────────────────────────


class TestBootstrapIdempotency:
    def test_skips_existing_files(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        # First run
        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        # Overwrite CLAUDE.md with custom content
        custom_content = "# MY CUSTOM CONTENT DO NOT OVERWRITE\n"
        (root / "CLAUDE.md").write_text(custom_content)

        # Second run — must not overwrite
        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        assert (root / "CLAUDE.md").read_text() == custom_content

    def test_skips_existing_directories(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        # Pre-create a directory with content
        (root / "assistant").mkdir(parents=True)
        marker = root / "assistant" / "my_custom_file.md"
        marker.write_text("keep me")

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        assert marker.exists()
        assert marker.read_text() == "keep me"

    def test_noop_when_fully_bootstrapped(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())
            # Second run should silently succeed
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        # All expected files still exist
        assert (root / "CLAUDE.md").exists()
        assert (root / "assistant/memory/scheduler.json").exists()


# ── Dotfile setup ─────────────────────────────────────────────────────────────


class TestDotfileSetup:
    def test_creates_assistant_profile(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        profile_path = fake_home / ".copilot" / "assistant_profile.json"
        assert profile_path.exists()

        data = json.loads(profile_path.read_text())
        assert "alias" in data
        assert "movement_reminders" in data

    def test_creates_claude_settings_with_hooks(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        settings_path = fake_home / ".claude" / "settings.json"
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        hooks = settings.get("hooks", {})
        session_start = hooks.get("SessionStart", [])
        assert len(session_start) > 0

        # Check that aya receive hook is present
        all_commands = [
            h.get("hooks", [{}])[0].get("command", "") for h in session_start if h.get("hooks")
        ]
        assert any("aya receive" in cmd for cmd in all_commands)

    def test_creates_health_crons_script(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        health_script = fake_home / ".claude" / "hooks" / "health_crons.sh"
        assert health_script.exists()
        assert health_script.stat().st_mode & 0o111  # executable

    def test_skips_existing_profile(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        # Pre-create profile with custom alias
        profile_path = fake_home / ".copilot" / "assistant_profile.json"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(json.dumps({"alias": "CustomAlias"}))

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        # Should not have been overwritten
        data = json.loads(profile_path.read_text())
        assert data["alias"] == "CustomAlias"

    def test_merges_hooks_into_existing_settings(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        settings_path = fake_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        existing = {"theme": "dark", "hooks": {"SessionStart": []}}
        settings_path.write_text(json.dumps(existing))

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        settings = json.loads(settings_path.read_text())
        # Original settings key preserved
        assert settings["theme"] == "dark"
        # Hooks were added
        assert len(settings["hooks"]["SessionStart"]) > 0

    def test_hooks_not_added_twice_on_second_run(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        settings_path = fake_home / ".claude" / "settings.json"
        first_run_count = len(json.loads(settings_path.read_text())["hooks"]["SessionStart"])

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        second_run_count = len(json.loads(settings_path.read_text())["hooks"]["SessionStart"])

        assert first_run_count == second_run_count


# ── reset_workspace ───────────────────────────────────────────────────────────


class TestResetWorkspace:
    def test_removes_config_files(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())
            reset_workspace(root, interactive=False, console=_silent_console())

        for f in RESET_FILES:
            assert not (root / f).exists(), f"Expected {f} to be removed after reset"

    def test_preserves_persona(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        persona_path = root / "assistant" / "persona.md"
        custom_content = "# My custom persona\n"
        persona_path.write_text(custom_content)

        with _patch_home(fake_home):
            reset_workspace(root, interactive=False, console=_silent_console())

        assert persona_path.exists(), "persona.md must not be removed on reset"
        assert persona_path.read_text() == custom_content

    def test_preserves_notes(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        note = root / "assistant" / "notes" / "daily" / "TODAY.md"
        note.write_text("# Today\n- item")
        meeting = root / "assistant" / "notes" / "meetings" / "2025-01-01.md"
        meeting.write_text("# Meeting\n")

        with _patch_home(fake_home):
            reset_workspace(root, interactive=False, console=_silent_console())

        assert note.exists(), "daily note must not be removed on reset"
        assert meeting.exists(), "meeting note must not be removed on reset"

    def test_preserves_projects(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        status = root / "projects" / "my-project" / "status.md"
        status.parent.mkdir(parents=True)
        status.write_text("# Status\n")

        with _patch_home(fake_home):
            reset_workspace(root, interactive=False, console=_silent_console())

        assert status.exists(), "project files must not be removed on reset"

    def test_removes_skills(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        # Confirm at least one skill is installed before reset
        installed = [
            name
            for name in SKILL_NAMES
            if (root / ".claude" / "commands" / f"{name}.md").exists()
            or (root / "skills" / name / "SKILL.md").exists()
        ]
        assert installed, (
            "Expected at least one bundled skill to be installed before reset; "
            "SKILL_NAMES or bootstrap_workspace behavior may be out of sync."
        )

        with _patch_home(fake_home):
            reset_workspace(root, interactive=False, console=_silent_console())

        for name in installed:
            assert not (root / ".claude" / "commands" / f"{name}.md").exists(), (
                f"Skill command {name}.md must be removed on reset"
            )
            # The entire skill directory must be gone, not just SKILL.md
            assert not (root / "skills" / name).exists(), (
                f"Skill directory skills/{name} must be removed on reset"
            )

    def test_preserves_scheduler(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        scheduler_path = root / "assistant" / "memory" / "scheduler.json"
        custom_content = '{"items": [{"id": "custom-reminder"}]}'
        scheduler_path.write_text(custom_content)

        with _patch_home(fake_home):
            reset_workspace(root, interactive=False, console=_silent_console())

        assert scheduler_path.exists(), "scheduler.json must not be removed on reset"
        assert scheduler_path.read_text() == custom_content

    def test_noop_when_nothing_to_reset(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        # Should not raise even when there are no bootstrap files present
        reset_workspace(root, interactive=False, console=_silent_console())

    def test_idempotent_double_reset(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())
            reset_workspace(root, interactive=False, console=_silent_console())
            # Second reset on already-reset workspace must not raise
            reset_workspace(root, interactive=False, console=_silent_console())

    def test_bootstrap_after_reset_recreates_files(self, tmp_path: Path, fake_home: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        with _patch_home(fake_home):
            bootstrap_workspace(root, interactive=False, console=_silent_console())
            reset_workspace(root, interactive=False, console=_silent_console())
            bootstrap_workspace(root, interactive=False, console=_silent_console())

        for f in RESET_FILES:
            assert (root / f).exists(), f"Expected {f} to be recreated after bootstrap"

    def test_reset_files_matches_get_files(self) -> None:
        """RESET_FILES must stay in sync with _get_files() minus PRESERVED_ON_RESET.

        This test fails when someone adds a file to _get_files() without updating
        either RESET_FILES or PRESERVED_ON_RESET, preventing silent drift.
        """
        from aya.workspace import _get_files

        all_bootstrapped = {path for path, _ in _get_files("")}
        expected = all_bootstrapped - PRESERVED_ON_RESET
        assert set(RESET_FILES) == expected, (
            f"RESET_FILES is out of sync with _get_files().\n"
            f"  Missing from RESET_FILES: {expected - set(RESET_FILES)}\n"
            f"  Extra in RESET_FILES:     {set(RESET_FILES) - expected}"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _silent_console() -> MagicMock:
    """Return a no-op Console mock so tests don't print to stdout."""
    console = MagicMock()
    console.print = MagicMock()
    console.status = MagicMock()
    return console


def _patch_home(fake_home: Path):
    """Context manager that redirects Path.home() to a temp directory."""
    fake_home.mkdir(parents=True, exist_ok=True)
    return patch("aya.workspace.Path.home", return_value=fake_home)
