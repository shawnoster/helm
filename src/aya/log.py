"""Daily progress logging — append entries to notebook daily notes.

Writes timestamped progress entries to ``notebook/daily/YYYY-MM-DD.md``
under a ``## Progress`` section.  State tracking in ``~/.aya/log_state.json``
prevents duplicate entries from ``aya log auto``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from aya.config import get_notebook_path
from aya.paths import LOG_STATE_FILE, PACKETS_DIR
from aya.scheduler.time_utils import _get_local_tz, get_last_activity

logger = logging.getLogger(__name__)

_PROGRESS_HEADING = "## Progress"
_DEDUP_SECONDS = 300  # 5 minutes


# ── state persistence ────────────────────────────────────────────────────────


def _load_state() -> dict[str, Any]:
    if not LOG_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(LOG_STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    LOG_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ── daily note helpers ───────────────────────────────────────────────────────


def _daily_path(notebook: Path, date: datetime) -> Path:
    return notebook / "daily" / f"{date.strftime('%Y-%m-%d')}.md"


def _ensure_daily_file(path: Path, date: datetime) -> None:
    """Create a daily note with a date header if it doesn't exist."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {date.strftime('%Y-%m-%d')}\n")


def _append_under_progress(path: Path, entry: str) -> None:
    """Append an entry under the ``## Progress`` section.

    If the section doesn't exist, it's appended at the end of the file.
    If it does exist, the entry is added after the last non-blank line
    in that section (before the next ``##`` heading or EOF).
    """
    text = path.read_text()
    lines = text.split("\n")

    # Find existing ## Progress section
    progress_idx = None
    for i, line in enumerate(lines):
        if line.strip() == _PROGRESS_HEADING:
            progress_idx = i
            break

    if progress_idx is None:
        # Append new section at the end
        if text and not text.endswith("\n"):
            path.write_text(text + f"\n\n{_PROGRESS_HEADING}\n\n{entry}\n")
        else:
            path.write_text(text + f"\n{_PROGRESS_HEADING}\n\n{entry}\n")
        return

    # Find insertion point: last line before next heading or EOF
    insert_at = len(lines)
    for i in range(progress_idx + 1, len(lines)):
        if lines[i].startswith("## ") and lines[i].strip() != _PROGRESS_HEADING:
            insert_at = i
            break

    # Back up past trailing blank lines in the section
    while insert_at > progress_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    lines.insert(insert_at, entry)
    path.write_text("\n".join(lines))


# ── format helpers ───────────────────────────────────────────────────────────


def _format_entry(now: datetime, message: str, tags: str | None = None) -> str:
    ts = now.strftime("%H:%M")
    tz_abbrev = now.strftime("%Z") or "PT"
    line = f"[{ts} {tz_abbrev}] {message}"
    if tags:
        line += f" — {tags}"
    return line


# ── public API ───────────────────────────────────────────────────────────────


def append_entry(
    message: str,
    tags: str | None = None,
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Append a timestamped progress entry to today's daily note.

    Returns (daily_file_path, formatted_entry).

    Raises:
        ValueError: If notebook_path is not configured or doesn't exist.
    """
    notebook = get_notebook_path()
    if not notebook:
        msg = "notebook_path not set. Run: aya config set notebook_path ~/notebook"
        raise ValueError(msg)
    if not notebook.exists():
        msg = f"Notebook path does not exist: {notebook}"
        raise ValueError(msg)

    if now is None:
        now = datetime.now(_get_local_tz())

    daily = _daily_path(notebook, now)
    _ensure_daily_file(daily, now)

    entry = _format_entry(now, message, tags)
    _append_under_progress(daily, entry)

    # Update state
    state = _load_state()
    state["last_logged_at"] = now.isoformat()
    _save_state(state)

    return daily, entry


def show_entries(
    date: datetime | None = None,
) -> list[dict[str, str]]:
    """Read progress entries from a daily note.

    Returns a list of dicts with ``time``, ``message``, and optional ``tags``.
    """
    notebook = get_notebook_path()
    if not notebook:
        msg = "notebook_path not set. Run: aya config set notebook_path ~/notebook"
        raise ValueError(msg)

    if date is None:
        date = datetime.now(_get_local_tz())

    daily = _daily_path(notebook, date)
    if not daily.exists():
        return []

    text = daily.read_text()
    lines = text.split("\n")

    # Find ## Progress section
    progress_idx = None
    for i, line in enumerate(lines):
        if line.strip() == _PROGRESS_HEADING:
            progress_idx = i
            break
    if progress_idx is None:
        return []

    entries: list[dict[str, str]] = []
    entry_re = re.compile(r"^\[(\d{2}:\d{2}\s+\w+)\]\s+(.+?)(?:\s+—\s+(.+))?$")

    for i in range(progress_idx + 1, len(lines)):
        line = lines[i]
        if line.startswith("## "):
            break
        m = entry_re.match(line.strip())
        if m:
            entry: dict[str, str] = {"time": m.group(1), "message": m.group(2)}
            if m.group(3):
                entry["tags"] = m.group(3)
            entries.append(entry)

    return entries


def auto_log(now: datetime | None = None) -> tuple[Path, str] | None:
    """Inspect recent activity and log a summary if warranted.

    Returns ``(path, entry)`` if an entry was written, or ``None`` if
    nothing noteworthy was detected or the dedup window hasn't elapsed.
    """
    if now is None:
        now = datetime.now(_get_local_tz())

    # Dedup check
    state = _load_state()
    last_raw = state.get("last_logged_at")
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if (now - last).total_seconds() < _DEDUP_SECONDS:
                logger.debug("Skipping auto-log: last entry was %s ago", now - last)
                return None
        except ValueError:
            pass

    # Gather signals
    signals: list[str] = []

    # 1. Recent git commits
    notebook = get_notebook_path()
    commits = _recent_git_commits(notebook)
    if commits:
        signals.append(f"{len(commits)} commit(s): {commits[0]}")

    # 2. Recent ingested packets
    packet_count = _recent_packet_count(now)
    if packet_count:
        signals.append(f"{packet_count} packet(s) ingested")

    # 3. Activity recorded
    last_activity = get_last_activity()
    if last_activity and (now - last_activity).total_seconds() < 600:
        signals.append("active session")

    if not signals:
        logger.debug("No noteworthy activity detected")
        return None

    summary = "; ".join(signals)
    return append_entry(f"[auto] {summary}", now=now)


# ── internal helpers ─────────────────────────────────────────────────────────


def _recent_git_commits(notebook: Path | None, since_minutes: int = 30) -> list[str]:
    """Return one-line summaries of recent commits in the notebook repo."""
    if not notebook or not notebook.exists():
        return []
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"--since={since_minutes} minutes ago",
                "--oneline",
                "--no-decorate",
            ],
            cwd=str(notebook),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def _recent_packet_count(now: datetime, window_minutes: int = 30) -> int:
    """Count packets ingested within the recent window."""
    if not PACKETS_DIR.exists():
        return 0
    cutoff = now.timestamp() - (window_minutes * 60)
    count = 0
    for p in PACKETS_DIR.iterdir():
        if p.suffix == ".json" and p.stat().st_mtime > cutoff:
            count += 1
    return count
