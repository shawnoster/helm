"""Install/uninstall aya scheduler integrations — crontab + Claude Code hooks."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_TICK_INTERVAL = "5m"  # Conservative default; configurable via --tick-interval
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
                    "command": "aya hook crons --reset",
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
            "hooks": [
                {
                    "type": "command",
                    "command": "aya hook crons --event PostToolUse 2>/dev/null || true",
                    "statusMessage": "",
                }
            ]
        },
        {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": "aya hook watch 2>/dev/null || true",
                    "statusMessage": "Checking watches...",
                    "asyncRewake": True,
                },
                {
                    "type": "command",
                    "command": "aya log auto >/dev/null 2>&1 || true",
                    "statusMessage": "",
                    "async": True,
                },
            ],
        },
        {
            "matcher": "Write",
            "hooks": [
                {
                    "type": "command",
                    "command": "aya log auto >/dev/null 2>&1 || true",
                    "statusMessage": "",
                    "async": True,
                }
            ],
        },
        {
            "matcher": "Edit",
            "hooks": [
                {
                    "type": "command",
                    "command": "aya log auto >/dev/null 2>&1 || true",
                    "statusMessage": "",
                    "async": True,
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
    cron_lines: list[str] = field(default_factory=list)
    tick_interval: str = ""
    hooks_installed: list[str] = field(default_factory=list)
    hooks_already_present: list[str] = field(default_factory=list)
    hooks_updated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def cron_line(self) -> str:
        """Backwards-compat: first line of cron_lines, or empty string."""
        return self.cron_lines[0] if self.cron_lines else ""


@dataclass
class UninstallResult:
    cron_removed: bool = False
    hooks_removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── Crontab helpers ──────────────────────────────────────────────────────────


def _resolve_aya_path() -> str | None:
    """Resolve full path to the aya binary."""
    return shutil.which("aya")


def parse_tick_interval(text: str) -> int:
    """Parse a tick-interval string into total seconds.

    Accepts forms like ``"30s"``, ``"1m"``, ``"5m"``, ``"1h"`` (single
    unit only). Returns the duration in seconds.

    Raises ValueError on bad input or non-positive values, or on values
    that exceed the supported range (1s … 60m). The upper bound exists
    because cron's ``*/N`` syntax doesn't generalize cleanly past 60.
    """
    text = text.strip().lower()
    if not text:
        raise ValueError("Empty tick interval")

    if text.endswith("s"):
        unit_secs = 1
        num_str = text[:-1]
    elif text.endswith("m"):
        unit_secs = 60
        num_str = text[:-1]
    elif text.endswith("h"):
        unit_secs = 3600
        num_str = text[:-1]
    else:
        raise ValueError(f"Tick interval must end in s/m/h: {text!r}")

    try:
        n = int(num_str)
    except ValueError as exc:
        raise ValueError(
            f"Tick interval must be a positive integer with s/m/h suffix: {text!r}"
        ) from exc

    if n <= 0:
        raise ValueError(f"Tick interval must be positive: {text!r}")

    seconds = n * unit_secs
    if seconds < 1 or seconds > 3600:
        raise ValueError(f"Tick interval must be between 1s and 60m, got {text!r} ({seconds}s)")
    return seconds


def _build_cron_lines(aya_path: str, interval_seconds: int) -> list[str]:
    """Return crontab line(s) for the given tick interval.

    For intervals ≥ 60s and ≤ 60m: emits a single ``*/N * * * *`` line
    (or ``* * * * *`` when N == 1, since ``*/1`` is non-standard).

    For sub-minute intervals: emits multiple ``* * * * *`` lines, one
    per offset (0, interval, 2*interval, …) within the minute, using
    ``( sleep N && cmd )`` for the offset entries. Standard pattern is
    30s = two lines (one immediate, one with sleep 30).
    """
    if interval_seconds >= 60:
        minutes = interval_seconds // 60
        if minutes == 1:
            cron_expr = "* * * * *"  # */1 is non-standard
        elif minutes == 60:
            cron_expr = "0 * * * *"  # */60 is invalid; use minute-0 of every hour
        else:
            cron_expr = f"*/{minutes} * * * *"
        return [f"{cron_expr} {aya_path} schedule tick --quiet  {CRON_COMMENT}"]

    # Sub-minute: walk offsets through the 60-second window.
    lines: list[str] = []
    for offset in range(0, 60, interval_seconds):
        if offset == 0:
            lines.append(f"* * * * * {aya_path} schedule tick --quiet  {CRON_COMMENT}")
        else:
            lines.append(
                f"* * * * * ( sleep {offset} && {aya_path} schedule tick --quiet )  "
                f"{CRON_COMMENT}-{offset}s"
            )
    return lines


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


def _add_cron_entry(
    aya_path: str,
    interval_seconds: int,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[bool, bool, list[str]]:
    """Add cron entry. Returns (installed, already_present, lines).

    Without ``force``, an existing aya cron entry causes a no-op
    (returns ``already_present=True``). With ``force``, any existing
    aya entries are removed first and the new lines for the requested
    interval are written.
    """
    current = _get_current_crontab()
    cron_lines = _build_cron_lines(aya_path, interval_seconds)

    if _has_aya_cron(current) and not force:
        return False, True, cron_lines

    if dry_run:
        return True, False, cron_lines

    # Strip any existing aya entries (force or no — when force, we replace;
    # when not force, _has_aya_cron above returned True so we don't reach here).
    surviving = [
        line
        for line in current.splitlines()
        if "aya schedule tick" not in line and CRON_COMMENT not in line
    ]
    new_crontab_parts = surviving + cron_lines
    new_crontab = "\n".join(new_crontab_parts) + "\n"
    if not new_crontab.strip():
        new_crontab = "\n".join(cron_lines) + "\n"

    subprocess.run(
        ["crontab", "-"],  # noqa: S607
        input=new_crontab,
        text=True,
        check=True,
    )
    return True, False, cron_lines


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


def _is_aya_command(cmd: str) -> bool:
    """Check if a shell command invokes the aya binary.

    Matches commands starting with ``aya`` or an absolute path ending in
    ``/aya`` (e.g. ``/home/user/.local/bin/aya schedule activity``).
    """
    first_token = cmd.split(maxsplit=1)[0] if cmd.strip() else ""
    binary = "aya"
    return first_token == binary or first_token.endswith(f"/{binary}")


def _is_aya_hook_entry(entry: dict[str, Any]) -> bool:
    """Check if a hook entry contains an aya command."""
    return any(_is_aya_command(hook.get("command", "")) for hook in entry.get("hooks", []))


def _load_claude_settings(path: Path | None = None) -> dict[str, Any]:
    """Load settings.json, returning {} if not found.

    Raises ``json.JSONDecodeError`` on corrupt JSON so callers can surface
    the error rather than silently overwriting the file.
    """
    path = path or CLAUDE_SETTINGS_PATH
    if not path.exists():
        return {}
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _save_claude_settings(data: dict[str, Any], path: Path | None = None) -> None:
    """Write settings.json with pretty formatting."""
    path = path or CLAUDE_SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _hooks_match(existing: list[dict[str, Any]], canonical: list[dict[str, Any]]) -> bool:
    """Check if existing aya hooks match the canonical set."""
    return existing == canonical


def _install_hooks(
    dry_run: bool = False, settings_path: Path | None = None
) -> tuple[list[str], list[str], list[str]]:
    """Install Claude Code hooks. Returns (installed, already_present, updated)."""
    path = settings_path or CLAUDE_SETTINGS_PATH
    settings = _load_claude_settings(path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"Expected 'hooks' to be a dict, got {type(hooks).__name__}")

    installed: list[str] = []
    already_present: list[str] = []
    updated: list[str] = []

    for event, canonical_entries in CANONICAL_HOOKS.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            raise ValueError(
                f"Expected hooks[{event!r}] to be a list, got {type(existing).__name__}"
            )
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
    if not isinstance(hooks, dict):
        raise ValueError(f"Expected 'hooks' to be a dict, got {type(hooks).__name__}")

    removed: list[str] = []
    for event in list(hooks.keys()):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
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


def install_scheduler(
    dry_run: bool = False,
    settings_path: Path | None = None,
    tick_interval: str = DEFAULT_TICK_INTERVAL,
    force: bool = False,
) -> InstallResult:
    """Install all scheduler integrations — crontab + Claude Code hooks.

    Args:
        dry_run: Preview changes without applying.
        settings_path: Override the Claude Code settings.json location (for tests).
        tick_interval: How often the scheduler should tick. Accepts forms
            like "30s", "1m", "5m", "1h" (1s … 60m). Sub-minute intervals
            generate multi-line crontab entries with sleep offsets.
        force: Replace any existing aya cron entries instead of treating
            them as already-installed.
    """
    result = InstallResult()
    result.tick_interval = tick_interval

    try:
        interval_seconds = parse_tick_interval(tick_interval)
    except ValueError as exc:
        result.errors.append(f"invalid tick_interval: {exc}")
        return result

    # Crontab
    aya_path = _resolve_aya_path()
    if aya_path is None:
        result.errors.append("Could not find 'aya' on PATH — skipping crontab")
    else:
        try:
            installed, already, lines = _add_cron_entry(
                aya_path, interval_seconds, dry_run=dry_run, force=force
            )
            result.cron_installed = installed
            result.cron_already_present = already
            result.cron_lines = lines
        except subprocess.CalledProcessError as exc:
            result.errors.append(f"crontab failed: {exc}")

    # Hooks
    try:
        h_installed, h_already, h_updated = _install_hooks(
            dry_run=dry_run, settings_path=settings_path
        )
        result.hooks_installed = h_installed
        result.hooks_already_present = h_already
        result.hooks_updated = h_updated
    except (OSError, json.JSONDecodeError, ValueError) as exc:
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
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.errors.append(f"settings.json failed: {exc}")

    return result
