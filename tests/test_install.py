"""Tests for aya schedule install/uninstall."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from aya.cli import app
from aya.install import (
    CANONICAL_HOOKS,
    CRON_COMMENT,
    _build_cron_line,
    _has_aya_cron,
    _is_aya_hook_entry,
    install_scheduler,
    uninstall_scheduler,
)

runner = CliRunner()


# ── Hook detection ───────────────────────────────────────────────────────────


class TestIsAyaHookEntry:
    def test_positive_schedule_activity(self) -> None:
        entry = {"hooks": [{"type": "command", "command": "aya schedule activity"}]}
        assert _is_aya_hook_entry(entry) is True

    def test_positive_hook_crons(self) -> None:
        entry = {"hooks": [{"type": "command", "command": "aya hook crons"}]}
        assert _is_aya_hook_entry(entry) is True

    def test_positive_ci_watch(self) -> None:
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "aya ci watch 2>/dev/null || true"}],
        }
        assert _is_aya_hook_entry(entry) is True

    def test_positive_with_full_path(self) -> None:
        entry = {"hooks": [{"type": "command", "command": "/usr/local/bin/aya receive --quiet"}]}
        assert _is_aya_hook_entry(entry) is True

    def test_negative_non_aya(self) -> None:
        entry = {"hooks": [{"type": "command", "command": "echo hello"}]}
        assert _is_aya_hook_entry(entry) is False

    def test_negative_empty(self) -> None:
        assert _is_aya_hook_entry({}) is False
        assert _is_aya_hook_entry({"hooks": []}) is False

    def test_negative_no_command(self) -> None:
        entry = {"hooks": [{"type": "command"}]}
        assert _is_aya_hook_entry(entry) is False


# ── Crontab detection ────────────────────────────────────────────────────────


class TestHasAyaCron:
    def test_present_by_command(self) -> None:
        crontab = "*/5 * * * * /home/user/.local/bin/aya schedule tick --quiet\n"
        assert _has_aya_cron(crontab) is True

    def test_present_by_comment(self) -> None:
        crontab = f"*/10 * * * * /some/path/to/aya tick  {CRON_COMMENT}\n"
        assert _has_aya_cron(crontab) is True

    def test_absent(self) -> None:
        crontab = "0 * * * * /usr/bin/backup.sh\n"
        assert _has_aya_cron(crontab) is False

    def test_empty(self) -> None:
        assert _has_aya_cron("") is False


class TestBuildCronLine:
    def test_format(self) -> None:
        line = _build_cron_line("/home/user/.local/bin/aya")
        assert line.startswith("*/5 * * * *")
        assert "/home/user/.local/bin/aya schedule tick --quiet" in line
        assert CRON_COMMENT in line


# ── Hook install/uninstall (real files) ──────────────────────────────────────


class TestInstallHooks:
    def test_fresh_install(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        result = install_scheduler(dry_run=True, settings_path=settings)
        assert set(result.hooks_installed) == {"SessionStart", "PreToolUse", "PostToolUse"}
        assert not result.hooks_already_present
        assert not settings.exists()  # dry run

    def test_fresh_install_writes_file(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        with patch("aya.install._resolve_aya_path", return_value=None):
            install_scheduler(settings_path=settings)
        assert settings.exists()
        data = json.loads(settings.read_text())
        assert "SessionStart" in data["hooks"]
        assert "PreToolUse" in data["hooks"]
        assert "PostToolUse" in data["hooks"]

    def test_preserves_permissions(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(git:*)"]}}) + "\n")
        with patch("aya.install._resolve_aya_path", return_value=None):
            install_scheduler(settings_path=settings)
        data = json.loads(settings.read_text())
        assert data["permissions"]["allow"] == ["Bash(git:*)"]
        assert "hooks" in data

    def test_idempotent(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        with patch("aya.install._resolve_aya_path", return_value=None):
            install_scheduler(settings_path=settings)
            result = install_scheduler(settings_path=settings)
        assert set(result.hooks_already_present) == {"SessionStart", "PreToolUse", "PostToolUse"}
        assert not result.hooks_installed
        assert not result.hooks_updated

    def test_updates_stale(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        stale = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "aya hook crons"}]}]
            }
        }
        settings.write_text(json.dumps(stale) + "\n")
        with patch("aya.install._resolve_aya_path", return_value=None):
            result = install_scheduler(settings_path=settings)
        assert "SessionStart" in result.hooks_updated

    def test_preserves_non_aya_hooks(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        custom_hook = {"hooks": [{"type": "command", "command": "echo custom"}]}
        existing = {"hooks": {"SessionStart": [custom_hook]}}
        settings.write_text(json.dumps(existing) + "\n")
        with patch("aya.install._resolve_aya_path", return_value=None):
            install_scheduler(settings_path=settings)
        data = json.loads(settings.read_text())
        commands = [
            h["command"] for entry in data["hooks"]["SessionStart"] for h in entry.get("hooks", [])
        ]
        assert "echo custom" in commands


class TestUninstallHooks:
    def test_removes_aya_only(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        custom = {"hooks": [{"type": "command", "command": "echo custom"}]}
        existing = {
            "hooks": {
                "SessionStart": CANONICAL_HOOKS["SessionStart"] + [custom],
                "PreToolUse": CANONICAL_HOOKS["PreToolUse"],
            }
        }
        settings.write_text(json.dumps(existing) + "\n")
        result = uninstall_scheduler(settings_path=settings)
        assert "SessionStart" in result.hooks_removed
        assert "PreToolUse" in result.hooks_removed
        data = json.loads(settings.read_text())
        assert len(data["hooks"]["SessionStart"]) == 1
        assert data["hooks"]["SessionStart"][0] == custom
        assert "PreToolUse" not in data["hooks"]

    def test_noop_when_absent(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"permissions": {}}) + "\n")
        result = uninstall_scheduler(settings_path=settings)
        assert not result.hooks_removed


class TestRoundtrip:
    def test_install_then_uninstall(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        original = {"permissions": {"allow": ["Bash(git:*)"]}}
        settings.write_text(json.dumps(original, indent=2) + "\n")
        with patch("aya.install._resolve_aya_path", return_value=None):
            install_scheduler(settings_path=settings)
        assert "hooks" in json.loads(settings.read_text())
        uninstall_scheduler(settings_path=settings)
        data = json.loads(settings.read_text())
        assert "hooks" not in data
        assert data["permissions"] == original["permissions"]


# ── Crontab (mocked subprocess) ─────────────────────────────────────────────


class TestInstallCron:
    def test_fresh_install(self, tmp_path: Path) -> None:
        written: list[str] = []

        def mock_run(cmd, **kwargs):
            if cmd == ["crontab", "-l"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no crontab")
            if cmd == ["crontab", "-"]:
                written.append(kwargs.get("input", ""))
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch("aya.install.subprocess.run", side_effect=mock_run),
            patch("aya.install._resolve_aya_path", return_value="/usr/local/bin/aya"),
        ):
            result = install_scheduler(settings_path=tmp_path / "s.json")

        assert result.cron_installed is True
        assert result.cron_already_present is False
        assert len(written) == 1
        assert "aya schedule tick" in written[0]

    def test_already_present(self, tmp_path: Path) -> None:
        existing = "*/5 * * * * /usr/local/bin/aya schedule tick --quiet\n"

        def mock_run(cmd, **kwargs):
            if cmd == ["crontab", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=existing)
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch("aya.install.subprocess.run", side_effect=mock_run),
            patch("aya.install._resolve_aya_path", return_value="/usr/local/bin/aya"),
        ):
            result = install_scheduler(settings_path=tmp_path / "s.json")

        assert result.cron_installed is False
        assert result.cron_already_present is True

    def test_no_aya_binary(self, tmp_path: Path) -> None:
        with patch("aya.install._resolve_aya_path", return_value=None):
            result = install_scheduler(settings_path=tmp_path / "s.json")
        assert any("PATH" in e for e in result.errors)
        assert result.cron_installed is False


class TestUninstallCron:
    def test_removes_line(self) -> None:
        existing = (
            "0 * * * * /usr/bin/backup.sh\n"
            f"*/5 * * * * /usr/local/bin/aya schedule tick --quiet  {CRON_COMMENT}\n"
        )
        written: list[str] = []

        def mock_run(cmd, **kwargs):
            if cmd == ["crontab", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=existing)
            if cmd == ["crontab", "-"]:
                written.append(kwargs.get("input", ""))
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("aya.install.subprocess.run", side_effect=mock_run):
            result = uninstall_scheduler(settings_path=Path("/nonexistent"))

        assert result.cron_removed is True
        assert len(written) == 1
        assert "backup.sh" in written[0]
        assert "aya" not in written[0]

    def test_noop(self) -> None:
        def mock_run(cmd, **kwargs):
            if cmd == ["crontab", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="0 * * * * echo hi\n")
            return subprocess.CompletedProcess(cmd, 0)

        with patch("aya.install.subprocess.run", side_effect=mock_run):
            result = uninstall_scheduler(settings_path=Path("/nonexistent"))

        assert result.cron_removed is False


# ── CLI integration ──────────────────────────────────────────────────────────


class TestCLI:
    def test_install_dry_run(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        with (
            patch("aya.install._resolve_aya_path", return_value="/usr/local/bin/aya"),
            patch("aya.install._get_current_crontab", return_value=""),
            patch("aya.install.subprocess.run"),
        ):
            result = runner.invoke(
                app,
                ["schedule", "install", "--dry-run"],
                env={"AYA_SETTINGS_PATH": str(settings)},
            )
        assert result.exit_code == 0, result.output

    def test_uninstall_dry_run(self, tmp_path: Path) -> None:
        with (
            patch("aya.install._get_current_crontab", return_value=""),
        ):
            result = runner.invoke(
                app,
                ["schedule", "uninstall", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
