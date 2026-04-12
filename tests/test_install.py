"""Tests for aya schedule install/uninstall."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from aya.cli import app
from aya.install import (
    CANONICAL_HOOKS,
    CRON_COMMENT,
    _build_cron_lines,
    _has_aya_cron,
    _is_aya_command,
    _is_aya_hook_entry,
    install_scheduler,
    parse_tick_interval,
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

    def test_positive_hook_watch(self) -> None:
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "aya hook watch 2>/dev/null || true"}],
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

    def test_negative_aya_substring_in_non_aya_command(self) -> None:
        entry = {"hooks": [{"type": "command", "command": "echo aya hello"}]}
        assert _is_aya_hook_entry(entry) is False


class TestIsAyaCommand:
    def test_bare_aya(self) -> None:
        assert _is_aya_command("aya schedule activity") is True

    def test_absolute_path(self) -> None:
        assert _is_aya_command("/home/user/.local/bin/aya hook crons") is True

    def test_non_aya(self) -> None:
        assert _is_aya_command("echo aya hello") is False

    def test_empty(self) -> None:
        assert _is_aya_command("") is False


# ── Crontab detection ────────────────────────────────────────────────────────


class TestHasAyaCron:
    def test_present_by_canonical_marker(self) -> None:
        # Real aya-emitted lines always include CRON_COMMENT.
        crontab = f"*/5 * * * * /home/user/.local/bin/aya schedule tick --quiet  {CRON_COMMENT}\n"
        assert _has_aya_cron(crontab) is True

    def test_present_by_marker_only(self) -> None:
        # Even minimal lines that contain the marker are detected.
        crontab = f"*/10 * * * * /some/path/to/aya tick  {CRON_COMMENT}\n"
        assert _has_aya_cron(crontab) is True

    def test_absent_unrelated_line(self) -> None:
        crontab = "0 * * * * /usr/bin/backup.sh\n"
        assert _has_aya_cron(crontab) is False

    def test_absent_user_comment_mentioning_aya(self) -> None:
        # Detection must NOT be substring-matched on "aya schedule tick" —
        # user comments mentioning the command shouldn't be misclassified
        # as aya entries (and then stripped on rewrite).
        crontab = "# reminder: investigate aya schedule tick latency\n"
        assert _has_aya_cron(crontab) is False

    def test_absent_unmarked_legacy_line(self) -> None:
        # A bare `aya schedule tick` line without the canonical comment
        # is NOT detected. This is intentional — back-compat for users
        # with very old aya entries is sacrificed for safety against
        # false positives. Such users would need to re-run install.
        crontab = "*/5 * * * * /usr/local/bin/aya schedule tick --quiet\n"
        assert _has_aya_cron(crontab) is False

    def test_empty(self) -> None:
        assert _has_aya_cron("") is False


class TestBuildCronLines:
    def test_5m_format(self) -> None:
        lines = _build_cron_lines("/home/user/.local/bin/aya", 300)
        assert len(lines) == 1
        assert lines[0].startswith("*/5 * * * *")
        assert "/home/user/.local/bin/aya schedule tick --quiet" in lines[0]
        assert CRON_COMMENT in lines[0]

    def test_1m_format_uses_star(self) -> None:
        lines = _build_cron_lines("/home/user/.local/bin/aya", 60)
        assert len(lines) == 1
        # */1 is non-standard; we emit "* * * * *" instead
        assert lines[0].startswith("* * * * *")
        assert "*/1" not in lines[0]

    def test_30s_format_emits_two_lines(self) -> None:
        lines = _build_cron_lines("/home/user/.local/bin/aya", 30)
        assert len(lines) == 2
        # First line fires at :00
        assert lines[0].startswith("* * * * *")
        assert "sleep" not in lines[0]
        # Second line fires at :30 via sleep offset
        assert "( sleep 30 && /home/user/.local/bin/aya schedule tick --quiet )" in lines[1]
        assert "aya-scheduler-tick-30s" in lines[1]

    def test_15s_format_emits_four_lines(self) -> None:
        lines = _build_cron_lines("/home/user/.local/bin/aya", 15)
        assert len(lines) == 4
        # Offsets at 0, 15, 30, 45
        assert "sleep" not in lines[0]
        assert "sleep 15" in lines[1]
        assert "sleep 30" in lines[2]
        assert "sleep 45" in lines[3]

    def test_1h_format_uses_minute_zero(self) -> None:
        lines = _build_cron_lines("/home/user/.local/bin/aya", 3600)
        assert len(lines) == 1
        # */60 is invalid cron syntax; we emit "0 * * * *" instead
        assert lines[0].startswith("0 * * * *")
        assert "*/60" not in lines[0]


class TestCanonicalHookEntries:
    """Lock-in tests for hook entries that have specific async/sync requirements."""

    def test_post_tool_use_hook_crons_is_async(self) -> None:
        """The PostToolUse `aya hook crons` entry must be async — every tool
        call would otherwise pay a Python interpreter cold-start, blocking
        the next tool. The sibling `aya log auto` and `aya hook watch`
        entries are also async; the crons entry must match."""
        post_tool_use = CANONICAL_HOOKS["PostToolUse"]
        crons_entries = [
            h
            for entry in post_tool_use
            for h in entry.get("hooks", [])
            if "aya hook crons" in h.get("command", "")
        ]
        assert len(crons_entries) == 1, "Expected exactly one `aya hook crons` PostToolUse hook"
        assert crons_entries[0].get("async") is True, (
            "PostToolUse `aya hook crons` must have `async: True` — see PR review of #198"
        )


class TestParseTickInterval:
    def test_seconds(self) -> None:
        assert parse_tick_interval("30s") == 30
        assert parse_tick_interval("1s") == 1
        assert parse_tick_interval("59s") == 59

    def test_minutes(self) -> None:
        assert parse_tick_interval("1m") == 60
        assert parse_tick_interval("5m") == 300
        assert parse_tick_interval("30m") == 1800

    def test_hours(self) -> None:
        assert parse_tick_interval("1h") == 3600

    def test_whitespace_and_case(self) -> None:
        assert parse_tick_interval("  5M  ") == 300
        assert parse_tick_interval("30S") == 30

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="Empty"):
            parse_tick_interval("")

    def test_rejects_no_suffix(self) -> None:
        with pytest.raises(ValueError, match="end in"):
            parse_tick_interval("30")

    def test_rejects_bad_unit(self) -> None:
        with pytest.raises(ValueError, match="end in"):
            parse_tick_interval("30d")

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(ValueError, match="positive integer"):
            parse_tick_interval("abcm")

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            parse_tick_interval("0s")

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            parse_tick_interval("-5m")

    def test_rejects_above_60m(self) -> None:
        with pytest.raises(ValueError, match="between 1s and 60m"):
            parse_tick_interval("2h")


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

    def test_corrupt_json_surfaces_error(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("{not valid json")
        with patch("aya.install._resolve_aya_path", return_value=None):
            result = install_scheduler(settings_path=settings)
        assert any("settings.json" in e for e in result.errors)
        # File should NOT be overwritten
        assert settings.read_text() == "{not valid json"

    def test_malformed_hooks_key_surfaces_error(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": "not-a-dict"}) + "\n")
        with patch("aya.install._resolve_aya_path", return_value=None):
            result = install_scheduler(settings_path=settings)
        assert any("settings.json" in e for e in result.errors)


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
        existing = f"*/5 * * * * /usr/local/bin/aya schedule tick --quiet  {CRON_COMMENT}\n"

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
