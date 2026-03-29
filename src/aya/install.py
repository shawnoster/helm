"""Install/uninstall aya scheduler integrations — crontab + Claude Code hooks."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

CRON_SCHEDULE = "*/5 * * * *"
CRON_COMMENT = "# aya-scheduler-tick"

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

CANONICAL_HOOKS: dict[str, list[dict[str, Any]]] = {
    "SessionStart": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "aya schedule activity 2>/dev/null || true",
                    "statusMessage": "Marking session active...",
                }
            ]
        },
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "aya hook crons",
                    "statusMessage": "Registering crons...",
                }
            ]
        },
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "aya receive --quiet --auto-ingest 2>/dev/null || true",
                    "statusMessage": "Checking for packets...",
                    "async": True,
                }
            ]
        },
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "aya schedule pending --format text 2>/dev/null || true",
                    "statusMessage": "Loading scheduler...",
                }
            ]
        },
    ],
    "PreToolUse": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "aya schedule activity 2>/dev/null || true",
                    "statusMessage": "Recording activity...",
                    "async": True,
                }
            ]
        },
    ],
    "PostToolUse": [
        {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": "aya ci watch 2>/dev/null || true",
                    "statusMessage": "Watching CI...",
                    "asyncRewake": True,
                }
            ],
        },
    ],
}


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class InstallResult:
    cron_installed: bool = False
    cron_already_present: bool = False
    cron_line: str = ""
    hooks_installed: list[str] = field(default_factory=list)
    hooks_already_present: list[str] = field(default_factory=list)
    hooks_updated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class UninstallResult:
    cron_removed: bool = False
    hooks_removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── Crontab helpers ──────────────────────────────────────────────────────────


def _resolve_aya_path() -> str | None:
    """Resolve full path to the aya binary."""
    return shutil.which("aya")


def _build_cron_line(aya_path: str) -> str:
    """Return the full crontab line with comment marker."""
    return f"{CRON_SCHEDULE} {aya_path} schedule tick --quiet  {CRON_COMMENT}"


def _get_current_crontab() -> str:
    """Read current crontab. Returns empty string if none exists."""
    result = subprocess.run(
        ["crontab", "-l"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _has_aya_cron(crontab_text: str) -> bool:
    """Check if any line contains an aya tick entry."""
    for line in crontab_text.splitlines():
        if "aya schedule tick" in line or CRON_COMMENT in line:
            return True
    return False


def _add_cron_entry(aya_path: str, dry_run: bool = False) -> tuple[bool, bool, str]:
    """Add cron entry. Returns (installed, already_present, line)."""
    current = _get_current_crontab()
    cron_line = _build_cron_line(aya_path)

    if _has_aya_cron(current):
        return False, True, cron_line

    if dry_run:
        return True, False, cron_line

    new_crontab = current.rstrip("\n") + "\n" + cron_line + "\n"
    if not current.strip():
        new_crontab = cron_line + "\n"

    subprocess.run(
        ["crontab", "-"],  # noqa: S607
        input=new_crontab,
        text=True,
        check=True,
    )
    return True, False, cron_line


def _remove_cron_entry(dry_run: bool = False) -> bool:
    """Remove aya cron entries. Returns True if something was removed."""
    current = _get_current_crontab()
    if not _has_aya_cron(current):
        return False

    if dry_run:
        return True

    lines = [
        line
        for line in current.splitlines()
        if "aya schedule tick" not in line and CRON_COMMENT not in line
    ]
    new_crontab = "\n".join(lines) + "\n" if lines else ""

    if new_crontab.strip():
        subprocess.run(
            ["crontab", "-"],  # noqa: S607
            input=new_crontab,
            text=True,
            check=True,
        )
    else:
        subprocess.run(
            ["crontab", "-r"],  # noqa: S607
            check=True,
        )
    return True


# ── Hook helpers ─────────────────────────────────────────────────────────────


def _is_aya_hook_entry(entry: dict[str, Any]) -> bool:
    """Check if a hook entry contains an aya command."""
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "")
        if "aya " in cmd:
            return True
    return False


def _load_claude_settings(path: Path | None = None) -> dict[str, Any]:
    """Load settings.json, returning {} if not found."""
    path = path or CLAUDE_SETTINGS_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_claude_settings(data: dict[str, Any], path: Path | None = None) -> None:
    """Write settings.json with pretty formatting."""
    path = path or CLAUDE_SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _hooks_match(existing: list[dict[str, Any]], canonical: list[dict[str, Any]]) -> bool:
    """Check if existing aya hooks match the canonical set."""
    return json.dumps(existing, sort_keys=True) == json.dumps(canonical, sort_keys=True)


def _install_hooks(
    dry_run: bool = False, settings_path: Path | None = None
) -> tuple[list[str], list[str], list[str]]:
    """Install Claude Code hooks. Returns (installed, already_present, updated)."""
    path = settings_path or CLAUDE_SETTINGS_PATH
    settings = _load_claude_settings(path)
    hooks = settings.setdefault("hooks", {})

    installed: list[str] = []
    already_present: list[str] = []
    updated: list[str] = []

    for event, canonical_entries in CANONICAL_HOOKS.items():
        existing = hooks.get(event, [])
        aya_entries = [e for e in existing if _is_aya_hook_entry(e)]
        non_aya_entries = [e for e in existing if not _is_aya_hook_entry(e)]

        if aya_entries and _hooks_match(aya_entries, canonical_entries):
            already_present.append(event)
        elif aya_entries:
            hooks[event] = canonical_entries + non_aya_entries
            updated.append(event)
        else:
            hooks[event] = canonical_entries + non_aya_entries
            installed.append(event)

    if not dry_run and (installed or updated):
        _save_claude_settings(settings, path)

    return installed, already_present, updated


def _remove_hooks(dry_run: bool = False, settings_path: Path | None = None) -> list[str]:
    """Remove all aya hook entries. Returns list of event names cleaned."""
    path = settings_path or CLAUDE_SETTINGS_PATH
    settings = _load_claude_settings(path)
    hooks = settings.get("hooks", {})

    removed: list[str] = []
    for event in list(hooks.keys()):
        entries = hooks[event]
        filtered = [e for e in entries if not _is_aya_hook_entry(e)]
        if len(filtered) < len(entries):
            removed.append(event)
            if filtered:
                hooks[event] = filtered
            else:
                del hooks[event]

    if not hooks and "hooks" in settings:
        del settings["hooks"]

    if not dry_run and removed:
        _save_claude_settings(settings, path)

    return removed


# ── Top-level install/uninstall ──────────────────────────────────────────────


def install_scheduler(dry_run: bool = False, settings_path: Path | None = None) -> InstallResult:
    """Install all scheduler integrations — crontab + Claude Code hooks."""
    result = InstallResult()

    # Crontab
    aya_path = _resolve_aya_path()
    if aya_path is None:
        result.errors.append("Could not find 'aya' on PATH — skipping crontab")
    else:
        try:
            installed, already, line = _add_cron_entry(aya_path, dry_run=dry_run)
            result.cron_installed = installed
            result.cron_already_present = already
            result.cron_line = line
        except subprocess.CalledProcessError as exc:
            result.errors.append(f"crontab failed: {exc}")

    # Hooks
    try:
        installed, already, updated = _install_hooks(dry_run=dry_run, settings_path=settings_path)
        result.hooks_installed = installed
        result.hooks_already_present = already
        result.hooks_updated = updated
    except (OSError, json.JSONDecodeError) as exc:
        result.errors.append(f"settings.json failed: {exc}")

    return result


def uninstall_scheduler(
    dry_run: bool = False, settings_path: Path | None = None
) -> UninstallResult:
    """Remove all scheduler integrations — crontab + Claude Code hooks."""
    result = UninstallResult()

    # Crontab
    try:
        result.cron_removed = _remove_cron_entry(dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        result.errors.append(f"crontab failed: {exc}")

    # Hooks
    try:
        result.hooks_removed = _remove_hooks(dry_run=dry_run, settings_path=settings_path)
    except (OSError, json.JSONDecodeError) as exc:
        result.errors.append(f"settings.json failed: {exc}")

    return result
