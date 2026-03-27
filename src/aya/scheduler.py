#!/usr/bin/env python3
"""Unified scheduler — reminders, watches, recurring items, and events.

Replaces reminders.py. Persists across AI sessions via scheduler.json.
Out-of-session polling via watcher_daemon.py + systemd timer.

Usage:
    scheduler.py remind  --due "tomorrow 9am" -m "Check the PR"
    scheduler.py watch   github-pr owner/repo#123 -m "PR approved"
    scheduler.py watch   jira-query "project=CSD AND created>=-1d" -m "New CSD tickets"
    scheduler.py watch   jira-ticket CSD-225 -m "Ticket status changed"
    scheduler.py list    [--all] [--type TYPE]
    scheduler.py check   [--json]
    scheduler.py dismiss <id>
    scheduler.py snooze  <id> --until "in 1 hour"
    scheduler.py poll    [--quiet]
    scheduler.py alerts  [--json]
"""

from __future__ import annotations

import fcntl
import functools
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def _find_workspace_root() -> Path:
    """Walk up from cwd looking for assistant/memory/scheduler.json."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "assistant" / "memory" / "scheduler.json").exists():
            return parent
    return cwd  # fallback


# ── Lazy module globals ──────────────────────────────────────────────────────
# ROOT, SCHEDULER_FILE, ALERTS_FILE, CONFIG_FILE, and LOCAL_TZ are resolved on
# first access (not at import time) via module-level __getattr__.  This avoids
# filesystem walks and ZoneInfo construction when the module is imported but
# these names aren't yet needed (e.g. cli.py importing scheduler functions).
# Internal code uses the _get_*() / _*_file() helpers directly.


@functools.lru_cache(maxsize=1)
def _get_root() -> Path:
    return _find_workspace_root()


@functools.lru_cache(maxsize=1)
def _get_local_tz() -> ZoneInfo:
    return ZoneInfo("America/Denver")


def _scheduler_file() -> Path:
    # Check globals first so monkeypatch("aya.scheduler.SCHEDULER_FILE", ...) works
    return globals().get("SCHEDULER_FILE") or (
        _get_root() / "assistant" / "memory" / "scheduler.json"
    )


def _alerts_file() -> Path:
    return globals().get("ALERTS_FILE") or (_get_root() / "assistant" / "memory" / "alerts.json")


def _config_file() -> Path:
    return globals().get("CONFIG_FILE") or (_get_root() / "assistant" / "config.json")


def _activity_file() -> Path:
    return globals().get("ACTIVITY_FILE") or (
        _get_root() / "assistant" / "memory" / "activity.json"
    )


_LAZY_ATTRS: dict[str, Any] = {
    "ROOT": _get_root,
    "SCHEDULER_FILE": _scheduler_file,
    "ALERTS_FILE": _alerts_file,
    "CONFIG_FILE": _config_file,
    "ACTIVITY_FILE": _activity_file,
    "LOCAL_TZ": _get_local_tz,
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_ATTRS:
        value = _LAZY_ATTRS[name]()
        globals()[name] = value  # cache in module dict for subsequent access
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── time parsing ─────────────────────────────────────────────────────────────

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_RELATIVE_RE = re.compile(
    r"^in\s+(\d+)\s+(minute|min|hour|hr|day|week)s?$",
    re.IGNORECASE,
)

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)


def _next_weekday(now: datetime, target_day: int) -> datetime:
    days_ahead = target_day - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return now + timedelta(days=days_ahead)


def _apply_time(dt: datetime, hour: int, minute: int) -> datetime:
    return dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _parse_time_component(text: str) -> tuple[int, int]:
    m = _TIME_RE.search(text)
    if not m:
        return 9, 0
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    return hour, minute


def parse_due(text: str, now: datetime | None = None) -> datetime:
    """Parse human-readable due time into timezone-aware datetime.

    Supports: ISO 8601, relative (in N units), tomorrow/today + time,
    weekday + time, eod/end of day.
    """
    if now is None:
        now = datetime.now(_get_local_tz())

    # Try ISO 8601 first (before lowercasing — the 'T' separator is case-sensitive).
    # fromisoformat() handles timezone offsets like -06:00 correctly in Python 3.7+.
    try:
        dt = datetime.fromisoformat(text.strip())
        return dt.replace(tzinfo=_get_local_tz()) if dt.tzinfo is None else dt
    except ValueError:
        pass

    text = text.strip().lower()

    m = _RELATIVE_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        delta = {
            "minute": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "hr": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
        }
        return now + delta.get(unit, timedelta())

    if text in ("eod", "end of day"):
        return _apply_time(now, 17, 0)

    if text.startswith("tomorrow"):
        h, mn = _parse_time_component(text)
        return _apply_time(now + timedelta(days=1), h, mn)

    if text.startswith("today") or _TIME_RE.match(text):
        h, mn = _parse_time_component(text)
        candidate = _apply_time(now, h, mn)
        return candidate + timedelta(days=1) if candidate <= now else candidate

    cleaned = text.replace("next ", "")
    for day_name, day_num in _WEEKDAYS.items():
        if cleaned.startswith(day_name):
            h, mn = _parse_time_component(cleaned)
            return _apply_time(_next_weekday(now, day_num), h, mn)

    raise ValueError(f"Cannot parse due time: {text!r}")


# ── idle / work-hours helpers ────────────────────────────────────────────────

_DURATION_RE = re.compile(
    r"^(?:(\d+)\s*h(?:r|ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?$",
    re.IGNORECASE,
)

_WORK_HOURS_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def parse_duration(text: str) -> timedelta:
    """Parse a human-readable duration string into a timedelta.

    Supports: "30m", "1h", "2h30m", "90min", "1hr", "2 hours 30 minutes".

    Raises ValueError if the string cannot be parsed or represents zero duration.
    """
    text = text.strip()
    m = _DURATION_RE.match(text)
    if m and (m.group(1) or m.group(2)):
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        try:
            delta = timedelta(hours=hours, minutes=minutes)
        except (OverflowError, TypeError) as exc:
            raise ValueError(f"Duration too large to represent: {text!r}") from exc
        if delta.total_seconds() <= 0:
            raise ValueError(f"Duration must be positive: {text!r}")
        return delta
    raise ValueError(f"Cannot parse duration: {text!r}")


def parse_work_hours(text: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Parse a work-hours window string into ((start_h, start_m), (end_h, end_m)).

    Accepts "HH:MM-HH:MM", e.g. "08:00-18:00" → ((8, 0), (18, 0)).

    Raises ValueError if the string cannot be parsed or represents an invalid window.
    """
    m = _WORK_HOURS_RE.match(text.strip())
    if not m:
        raise ValueError(f"Cannot parse work hours: {text!r}  (expected HH:MM-HH:MM)")
    sh, sm, eh, em = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
        raise ValueError(
            f"Invalid time in work hours: {text!r}  (hours must be 0-23, minutes 0-59)"
        )
    if sh * 60 + sm >= eh * 60 + em:
        raise ValueError(
            f"Invalid work hours window: {text!r}"
            "  (start time must be before end time on the same day)"
        )
    return (sh, sm), (eh, em)


def is_within_work_hours(only_during: str, now: datetime | None = None) -> bool:
    """Return True if *now* falls within the *only_during* window.

    *only_during* must be a string in "HH:MM-HH:MM" format (e.g. "08:00-18:00").
    Returns True if *only_during* is empty so that callers can unconditionally test.
    """
    if not only_during:
        return True
    if now is None:
        now = datetime.now(_get_local_tz())
    (sh, sm), (eh, em) = parse_work_hours(only_during)
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    current_minutes = now.hour * 60 + now.minute
    return start_minutes <= current_minutes < end_minutes


def record_activity(now: datetime | None = None) -> None:
    """Record the current time as the last-known user activity.

    Writes ``{"last_activity_at": "<ISO timestamp>"}`` to the activity file.
    Safe to call from any hook or command that indicates user presence.
    """
    if now is None:
        now = datetime.now(_get_local_tz())
    path = _activity_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock():
        _atomic_write(path, {"last_activity_at": now.isoformat()})


def get_last_activity() -> datetime | None:
    """Return the timestamp of the last recorded user activity, or None."""
    data = _locked_read(_activity_file())
    if not data:
        return None
    raw = data.get("last_activity_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_get_local_tz())
    return dt


def is_idle(threshold_str: str, now: datetime | None = None) -> bool:
    """Return True if the session appears idle.

    A session is idle when the elapsed time since the last recorded activity
    exceeds *threshold_str* (e.g. "30m", "1h").  If no activity has ever been
    recorded the session is considered *not* idle so that first-run behaviour is
    unaffected.
    """
    if not threshold_str:
        return False
    threshold = parse_duration(threshold_str)
    last = get_last_activity()
    if last is None:
        return False
    if now is None:
        now = datetime.now(_get_local_tz())
    return (now - last) >= threshold


# ── file safety (fcntl lock + atomic write) ──────────────────────────────────


def _lock_file() -> Path:
    """Return the advisory lock file path, co-located with scheduler.json."""
    return _scheduler_file().parent / ".scheduler.lock"


@contextmanager
def _file_lock(*, shared: bool = False) -> Iterator[int]:
    """Acquire an advisory lock on the scheduler lock file.

    Args:
        shared: If True, acquire a shared (read) lock (LOCK_SH).
                If False (default), acquire an exclusive (write) lock (LOCK_EX).

    Yields the lock file descriptor.
    """
    lock_path = _lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically: tmp file → fsync → rename.

    Caller must already hold an exclusive _file_lock().
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, default=str) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        encoded = content.encode()
        total = 0
        while total < len(encoded):
            written = os.write(fd, encoded[total:])
            if written == 0:
                raise OSError("os.write returned 0 bytes during atomic write")
            total += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        Path(tmp).replace(path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        raise


def _locked_read(path: Path) -> dict[str, Any] | None:
    """Read a JSON file under a shared lock. Returns None if missing or corrupt."""
    if not path.exists():
        return None
    with _file_lock(shared=True):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None


# ── harness detection + instance identity ────────────────────────────────────


def _detect_harness() -> str:
    """Detect which AI harness is running this process.

    Checks environment variables to identify the caller:
    - CLAUDE_CODE or CLAUDE_* → "claude"
    - GITHUB_COPILOT or COPILOT_* → "copilot"
    - Otherwise → "unknown"
    """
    env = os.environ
    if any(k.startswith("CLAUDE") for k in env):
        return "claude"
    if any(k.startswith(("COPILOT", "GITHUB_COPILOT")) for k in env):
        return "copilot"
    return "unknown"


def get_instance_id() -> str:
    """Return a unique instance identifier: {harness}-{pid}."""
    return f"{_detect_harness()}-{os.getpid()}"


# ── claim files (alert delivery dedup) ───────────────────────────────────────

_CLAIM_TTL_SECONDS = 300  # 5 minutes — if a session crashes, claim expires


def _claims_dir() -> Path:
    """Return the claims directory, co-located with scheduler.json."""
    return _scheduler_file().parent / "claims"


def claim_alert(alert_id: str, instance_id: str | None = None) -> bool:
    """Attempt to claim an alert for delivery. Returns True if claim succeeded.

    Uses O_CREAT|O_EXCL for atomic file creation — first writer wins.
    Stale claims (past TTL) are removed and re-claimable.
    """
    claims = _claims_dir()
    claims.mkdir(parents=True, exist_ok=True)
    claim_path = claims / f"{alert_id}.claimed"

    # Check for stale claim
    if claim_path.exists():
        try:
            data = json.loads(claim_path.read_text())
            claimed_at = datetime.fromisoformat(data["claimed_at"])
            ttl = data.get("ttl_seconds", _CLAIM_TTL_SECONDS)
            if datetime.now(_get_local_tz()) - claimed_at < timedelta(seconds=ttl):
                return False  # Valid claim exists
            # Stale — remove and re-claim
            claim_path.unlink(missing_ok=True)
        except (json.JSONDecodeError, KeyError, OSError):
            claim_path.unlink(missing_ok=True)

    # Attempt atomic create
    instance_id = instance_id or get_instance_id()
    content = json.dumps(
        {
            "instance": instance_id,
            "claimed_at": datetime.now(_get_local_tz()).isoformat(),
            "ttl_seconds": _CLAIM_TTL_SECONDS,
        }
    )
    try:
        fd = os.open(str(claim_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False


def sweep_stale_claims(max_age_seconds: int = 86400) -> int:
    """Remove claim files older than max_age_seconds. Returns count removed."""
    claims = _claims_dir()
    if not claims.exists():
        return 0
    removed = 0
    now = datetime.now(_get_local_tz())
    for claim_file in claims.glob("*.claimed"):
        try:
            data = json.loads(claim_file.read_text())
            claimed_at = datetime.fromisoformat(data["claimed_at"])
            if (now - claimed_at).total_seconds() > max_age_seconds:
                claim_file.unlink()
                removed += 1
        except (json.JSONDecodeError, KeyError, OSError):
            claim_file.unlink(missing_ok=True)
            removed += 1
    return removed


# ── storage ──────────────────────────────────────────────────────────────────


def load_items() -> list[dict[str, Any]]:
    data = _locked_read(_scheduler_file())
    return data.get("items", []) if data else []


def save_items(items: list[dict[str, Any]]) -> None:
    with _file_lock():
        _atomic_write(_scheduler_file(), {"items": items})


def load_alerts() -> list[dict[str, Any]]:
    data = _locked_read(_alerts_file())
    return data.get("alerts", []) if data else []


def save_alerts(alerts: list[dict[str, Any]]) -> None:
    with _file_lock():
        _atomic_write(_alerts_file(), {"alerts": alerts})


def add_seed_alert(
    intent: str,
    opener: str,
    context_summary: str,
    open_questions: list[str],
    from_label: str,
    packet_id: str = "",
) -> dict[str, Any]:
    """Persist a seed packet as an unseen alert so it surfaces via pending on next session start."""
    now = datetime.now(_get_local_tz())
    detail_lines = [f"**From:** {from_label}", f"**Opener:** {opener}"]
    if context_summary:
        detail_lines.append(f"**Context:** {context_summary}")
    if open_questions:
        detail_lines.append("**Open questions:**")
        detail_lines.extend(f"  • {q}" for q in open_questions)
    alert = {
        "id": _new_id(),
        # source_item_id is required by run_poll/tick for existing_sources dedup;
        # use the originating packet ID so the alert can be traced back to its source.
        "source_item_id": packet_id or _new_id(),
        "created_at": now.isoformat(),
        "message": f"Seed from {from_label}: {intent}",
        "details": {
            "type": "seed",
            "intent": intent,
            "opener": opener,
            "context_summary": context_summary,
            "open_questions": open_questions,
            "from_label": from_label,
            "body": "\n".join(detail_lines),
        },
        "seen": False,
    }
    with _file_lock():
        alerts = _load_alerts_unlocked()
        alerts.append(alert)
        _atomic_write(_alerts_file(), {"alerts": alerts})
    return alert


def _find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    for item in items:
        if item["id"] == item_id or item["id"].startswith(item_id):
            return item
    return None


def _new_id() -> str:
    return str(uuid.uuid4())


# ── watch providers ──────────────────────────────────────────────────────────


def _run_gh(args: list[str], timeout: int = 15) -> dict[str, Any] | list | None:
    """Run gh CLI and parse JSON output."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _check_github_pr(config: dict[str, Any]) -> dict[str, Any] | None:
    """Check GitHub PR status and reviews."""
    owner = config["owner"]
    repo = config["repo"]
    pr = config["pr"]

    pr_data = _run_gh(
        [
            "api",
            f"/repos/{owner}/{repo}/pulls/{pr}",
            "--jq",
            "{ state: .state, merged: .merged, draft: .draft, title: .title }",
        ]
    )
    if not pr_data:
        return None

    reviews = _run_gh(
        [
            "api",
            f"/repos/{owner}/{repo}/pulls/{pr}/reviews",
            "--jq",
            "[.[] | { user: .user.login, state: .state }]",
        ]
    )

    return {
        "pr_state": pr_data.get("state"),
        "merged": pr_data.get("merged", False),
        "draft": pr_data.get("draft", False),
        "title": pr_data.get("title", ""),
        "reviews": reviews or [],
        "has_approval": any(r.get("state") == "APPROVED" for r in (reviews or [])),
    }


def _check_jira_query(config: dict[str, Any]) -> dict[str, Any] | None:
    """Run a JQL query and return results."""
    jql = config["jql"]
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    server = os.environ.get("ATLASSIAN_SERVER_URL", "").rstrip("/")

    if not all([email, token, server]):
        return None

    try:
        import httpx

        resp = httpx.post(
            f"{server}/rest/api/3/search",
            auth=(email, token),
            json={"jql": jql, "maxResults": 20, "fields": ["key", "summary", "status"]},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "total": data.get("total", 0),
            "issues": [
                {
                    "key": i["key"],
                    "summary": i["fields"]["summary"],
                    "status": i["fields"]["status"]["name"],
                }
                for i in data.get("issues", [])
            ],
        }
    except Exception:
        return None


def _check_jira_ticket(config: dict[str, Any]) -> dict[str, Any] | None:
    """Check a specific Jira ticket's status."""
    ticket = config["ticket"]
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    server = os.environ.get("ATLASSIAN_SERVER_URL", "").rstrip("/")

    if not all([email, token, server]):
        return None

    try:
        import httpx

        resp = httpx.get(
            f"{server}/rest/api/3/issue/{ticket}",
            auth=(email, token),
            params={"fields": "summary,status,assignee,priority"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        fields = data.get("fields", {})
        return {
            "key": data["key"],
            "summary": fields.get("summary", ""),
            "status": fields.get("status", {}).get("name", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", "Unassigned"),
        }
    except Exception:
        return None


WATCH_PROVIDERS = {
    "github-pr": _check_github_pr,
    "jira-query": _check_jira_query,
    "jira-ticket": _check_jira_ticket,
}


def poll_watch(item: dict[str, Any]) -> tuple[dict | None, bool]:
    """Poll a watch item. Returns (new_state, changed)."""
    provider = item.get("provider", "")
    check_fn = WATCH_PROVIDERS.get(provider)
    if not check_fn:
        return None, False

    new_state = check_fn(item.get("watch_config", {}))
    if new_state is None:
        return None, False

    last_state = item.get("last_state")
    changed = False
    condition = item.get("condition", "")

    if provider == "github-pr":
        if condition == "approved_or_merged":
            was_approved = (last_state or {}).get("has_approval", False)
            was_merged = (last_state or {}).get("merged", False)
            changed = (new_state["has_approval"] and not was_approved) or (
                new_state["merged"] and not was_merged
            )
        elif condition == "merged":
            changed = new_state["merged"] and not (last_state or {}).get("merged", False)
        else:
            changed = json.dumps(new_state, sort_keys=True) != json.dumps(
                last_state, sort_keys=True
            )

    elif provider == "jira-query":
        if condition == "new_results":
            old_keys = {i["key"] for i in (last_state or {}).get("issues", [])}
            new_keys = {i["key"] for i in new_state.get("issues", [])}
            changed = bool(new_keys - old_keys)
        else:
            changed = new_state.get("total", 0) != (last_state or {}).get("total", 0)

    elif provider == "jira-ticket":
        if condition == "status_changed":
            changed = new_state.get("status") != (last_state or {}).get("status")
        else:
            changed = json.dumps(new_state, sort_keys=True) != json.dumps(
                last_state, sort_keys=True
            )

    return new_state, changed


def _evaluate_auto_remove(item: dict[str, Any], state: dict[str, Any]) -> bool:
    """Check if a watch should be auto-removed based on remove_when condition."""
    remove_when = item.get("remove_when", "")
    if not remove_when:
        return False
    if remove_when == "merged_or_closed" and item.get("provider") == "github-pr":
        return state.get("merged", False) or state.get("pr_state") == "closed"
    return False


# ── core operations ──────────────────────────────────────────────────────────


def add_reminder(message: str, due_text: str, tags: str = "") -> dict[str, Any]:
    """Add a one-shot reminder. Returns the created item."""
    now = datetime.now(_get_local_tz())
    due = parse_due(due_text, now)
    item = {
        "id": _new_id(),
        "type": "reminder",
        "status": "pending",
        "created_at": now.isoformat(),
        "message": message,
        "tags": _parse_tags(tags),
        "session_required": False,
        "due_at": due.isoformat(),
        "delivered_at": None,
        "snoozed_until": None,
    }
    with _file_lock():
        items = _load_items_unlocked()
        items.append(item)
        _atomic_write(_scheduler_file(), {"items": items})
    return item


def add_watch(
    provider: str,
    target: str,
    message: str,
    tags: str = "",
    condition: str = "",
    interval: int = 30,
    remove_when: str = "",
) -> dict[str, Any]:
    """Add a condition-based watch. Returns the created item."""
    now = datetime.now(_get_local_tz())
    watch_config: dict[str, Any] = {}

    if provider == "github-pr":
        m = re.match(r"([^/]+)/([^#]+)#(\d+)", target)
        if not m:
            raise ValueError("Format: owner/repo#123")
        watch_config = {"owner": m.group(1), "repo": m.group(2), "pr": int(m.group(3))}
        condition = condition or "approved_or_merged"
        if interval == 30:
            interval = 5
    elif provider == "jira-query":
        watch_config = {"jql": target}
        condition = condition or "new_results"
    elif provider == "jira-ticket":
        watch_config = {"ticket": target.upper()}
        condition = condition or "status_changed"
    else:
        raise ValueError(f"Unknown provider: {provider}")

    item = {
        "id": _new_id(),
        "type": "watch",
        "status": "active",
        "created_at": now.isoformat(),
        "message": message,
        "tags": _parse_tags(tags),
        "session_required": False,
        "provider": provider,
        "watch_config": watch_config,
        "condition": condition,
        "poll_interval_minutes": interval,
        "last_checked_at": None,
        "last_state": None,
        "remove_when": remove_when,
    }
    with _file_lock():
        items = _load_items_unlocked()
        items.append(item)
        _atomic_write(_scheduler_file(), {"items": items})
    return item


def add_recurring(
    message: str,
    cron: str,
    prompt: str = "",
    tags: str = "",
    idle_back_off: str = "",
    only_during: str = "",
) -> dict[str, Any]:
    """Add a persistent recurring session job. Returns the created item.

    Args:
        message:       Short human-readable label shown in schedule list.
        cron:          Standard five-field cron expression (e.g. "27 * * * *").
        prompt:        Instruction delivered to Claude each time the cron fires.
        tags:          Comma-separated tags for filtering.
        idle_back_off: Suppress this cron when the session has been idle for
                       longer than this duration (e.g. "30m", "1h").  Empty
                       string disables idle suppression.
        only_during:   Only allow this cron to fire within this time window
                       (e.g. "08:00-18:00").  Empty string disables the window
                       check.
    """
    # Validate optional fields eagerly so callers get clear errors.
    if idle_back_off:
        parse_duration(idle_back_off)  # raises ValueError on bad input
    if only_during:
        parse_work_hours(only_during)  # raises ValueError on bad input

    now = datetime.now(_get_local_tz())
    item: dict[str, Any] = {
        "id": _new_id(),
        "type": "recurring",
        "status": "active",
        "created_at": now.isoformat(),
        "message": message,
        "tags": _parse_tags(tags),
        "session_required": True,
        "cron": cron,
        "prompt": prompt,
    }
    if idle_back_off:
        item["idle_back_off"] = idle_back_off
    if only_during:
        item["only_during"] = only_during
    with _file_lock():
        items = _load_items_unlocked()
        items.append(item)
        _atomic_write(_scheduler_file(), {"items": items})
    return item


def list_items(
    show_all: bool = False,
    item_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return filtered list of scheduler items."""
    items = load_items()
    if item_type:
        items = [i for i in items if i["type"] == item_type]
    if not show_all:
        items = [i for i in items if i["status"] in ("pending", "active", "snoozed")]
    return items


def check_due() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check for due reminders and unseen alerts. Returns (due_items, unseen_alerts).

    Holds exclusive lock when snooze→pending transitions require a write.
    """
    with _file_lock():
        items = _load_items_unlocked()
        now = datetime.now(_get_local_tz())
        modified = False
        due_items = []

        for item in items:
            if item.get("type") != "reminder" or item.get("status") not in ("pending", "snoozed"):
                continue
            if item.get("status") == "snoozed" and item.get("snoozed_until"):
                snooze_end = datetime.fromisoformat(item["snoozed_until"])
                if snooze_end > now:
                    continue
                item["status"] = "pending"
                item["snoozed_until"] = None
                modified = True
            due = datetime.fromisoformat(item["due_at"])
            if due <= now:
                due_items.append(item)

        if modified:
            _atomic_write(_scheduler_file(), {"items": items})

        unseen = [a for a in _load_alerts_unlocked() if not a.get("seen")]
    return due_items, unseen


def dismiss_item(item_id: str) -> dict[str, Any]:
    """Dismiss an item by ID (prefix match). Returns the dismissed item."""
    with _file_lock():
        items = _load_items_unlocked()
        item = _find(items, item_id)
        if not item:
            raise ValueError(f"Item {item_id} not found.")
        item["status"] = "dismissed"
        if item["type"] == "reminder":
            item["delivered_at"] = datetime.now(_get_local_tz()).isoformat()
        _atomic_write(_scheduler_file(), {"items": items})
    return item


def dismiss_alert(alert_id: str) -> dict[str, Any]:
    """Dismiss an alert by ID (prefix match). Returns the dismissed alert."""
    with _file_lock():
        alerts = _load_alerts_unlocked()
        alert = _find(alerts, alert_id)
        if not alert:
            raise ValueError(f"Alert {alert_id} not found.")
        alert["seen"] = True
        _atomic_write(_alerts_file(), {"alerts": alerts})
    return alert


def snooze_item(item_id: str, until_text: str) -> tuple[dict[str, Any], datetime]:
    """Snooze a reminder. Returns (item, snooze_until_datetime)."""
    with _file_lock():
        items = _load_items_unlocked()
        item = _find(items, item_id)
        if not item:
            raise ValueError(f"Item {item_id} not found.")
        now = datetime.now(_get_local_tz())
        snooze_until = parse_due(until_text, now)
        item["status"] = "snoozed"
        item["snoozed_until"] = snooze_until.isoformat()
        _atomic_write(_scheduler_file(), {"items": items})
    return item, snooze_until


def _load_items_unlocked() -> list[dict[str, Any]]:
    """Read scheduler items without acquiring a lock (caller holds lock)."""
    path = _scheduler_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("items", []) if isinstance(data, dict) else []


def _load_alerts_unlocked() -> list[dict[str, Any]]:
    """Read alerts without acquiring a lock (caller holds lock)."""
    path = _alerts_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("alerts", []) if isinstance(data, dict) else []


def run_poll(quiet: bool = False) -> None:
    """Run one poll cycle — check all watches and due reminders.

    Holds a single exclusive lock for the entire load→poll→save cycle
    to prevent interleaving with other CLI commands or sessions.
    """
    with _file_lock():
        items = _load_items_unlocked()
        alerts = _load_alerts_unlocked()
        now = datetime.now(_get_local_tz())
        items_modified = False
        alerts_modified = False

        for item in items:
            if (
                item["type"] == "watch"
                and item["status"] == "active"
                and not item.get("session_required")
            ):
                last = item.get("last_checked_at")
                interval = item.get("poll_interval_minutes", 30)
                if last:
                    next_check = datetime.fromisoformat(last) + timedelta(minutes=interval)
                    if now < next_check:
                        continue

                new_state, changed = poll_watch(item)
                if new_state is not None:
                    item["last_checked_at"] = now.isoformat()
                    item["last_state"] = new_state
                    items_modified = True

                    if changed:
                        alert = {
                            "id": _new_id(),
                            "source_item_id": item["id"],
                            "created_at": now.isoformat(),
                            "message": _format_watch_alert(item, new_state),
                            "details": new_state,
                            "seen": False,
                        }
                        alerts.append(alert)
                        alerts_modified = True
                        if not quiet:
                            pass

                    if _evaluate_auto_remove(item, new_state):
                        item["status"] = "dismissed"
                        items_modified = True
                        if not quiet:
                            pass

                elif not quiet:
                    pass

            elif item["type"] == "reminder" and item["status"] == "pending":
                due = datetime.fromisoformat(item["due_at"])
                if due <= now:
                    existing_sources = {a["source_item_id"] for a in alerts if not a.get("seen")}
                    if item["id"] not in existing_sources:
                        alert = {
                            "id": _new_id(),
                            "source_item_id": item["id"],
                            "created_at": now.isoformat(),
                            "message": f"Reminder due: {item['message']}",
                            "details": {"due_at": item["due_at"]},
                            "seen": False,
                        }
                        alerts.append(alert)
                        alerts_modified = True
                        if not quiet:
                            pass

        if items_modified:
            _atomic_write(_scheduler_file(), {"items": items})
        if alerts_modified:
            _atomic_write(_alerts_file(), {"alerts": alerts})


def show_alerts(as_json: bool = False, mark_seen: bool = False) -> list[dict[str, Any]]:
    """Show and optionally clear alerts. Returns unseen alerts."""
    if mark_seen:
        with _file_lock():
            alerts = _load_alerts_unlocked()
            unseen = [a for a in alerts if not a.get("seen")]
            if unseen:
                for a in alerts:
                    a["seen"] = True
                _atomic_write(_alerts_file(), {"alerts": alerts})
        return unseen

    alerts = load_alerts()
    return [a for a in alerts if not a.get("seen")]


def _parse_tags(tags: str) -> list[str]:
    return [t.strip() for t in tags.split(",") if t.strip()] if tags else []


def _format_watch_alert(item: dict[str, Any], state: dict[str, Any]) -> str:
    """Format a human-readable alert message from watch state change."""
    provider = item.get("provider", "")
    base = item.get("message", "Watch triggered")

    if provider == "github-pr":
        if state.get("merged"):
            return f"{base} — MERGED"
        if state.get("has_approval"):
            approvers = [r["user"] for r in state.get("reviews", []) if r["state"] == "APPROVED"]
            return f"{base} — APPROVED by {', '.join(approvers)}"
        return f"{base} — state changed"

    if provider == "jira-query":
        new_issues = state.get("issues", [])[:3]
        keys = ", ".join(i["key"] for i in new_issues)
        return f"{base} — new: {keys}" if keys else f"{base} — results changed"

    if provider == "jira-ticket":
        return f"{base} — now: {state.get('status', '?')}"

    return base


# ── programmatic API (for status_check.py, morning.md) ──────────────────────


def get_due_reminders(now: datetime | None = None) -> list[dict[str, Any]]:
    """Return pending reminders that are due. No side effects."""
    items = load_items()
    if now is None:
        now = datetime.now(_get_local_tz())
    due = []
    for item in items:
        if item.get("type") != "reminder" or item.get("status") not in ("pending", "snoozed"):
            continue
        if item.get("status") == "snoozed" and item.get("snoozed_until"):
            if datetime.fromisoformat(item["snoozed_until"]) > now:
                continue
        if datetime.fromisoformat(item["due_at"]) <= now:
            due.append(item)
    return due


def get_upcoming_reminders(now: datetime | None = None, hours: int = 24) -> list[dict[str, Any]]:
    """Return pending reminders due within N hours."""
    items = load_items()
    if now is None:
        now = datetime.now(_get_local_tz())
    horizon = now + timedelta(hours=hours)
    upcoming = []
    for item in items:
        if item.get("type") != "reminder" or item.get("status") not in ("pending", "snoozed"):
            continue
        reminder_due = datetime.fromisoformat(item["due_at"])
        if now < reminder_due <= horizon:
            upcoming.append(item)
    upcoming.sort(key=lambda r: r["due_at"])
    return upcoming


def get_unseen_alerts() -> list[dict[str, Any]]:
    """Return unseen alerts from daemon."""
    return [a for a in load_alerts() if not a.get("seen")]


def get_active_watches() -> list[dict[str, Any]]:
    """Return all active watches."""
    return [i for i in load_items() if i["type"] == "watch" and i["status"] == "active"]


# ── tick + pending (Phase 5B) ────────────────────────────────────────────────


def run_tick(quiet: bool = False) -> dict[str, int]:
    """Run one scheduler tick — poll watches, check reminders, sweep stale claims.

    This is the canonical entry point for system cron:
        */5 * * * * aya scheduler tick --quiet

    Returns a summary dict: {"watches_checked": N, "alerts_generated": N, "claims_swept": N}
    """
    run_poll(quiet=quiet)
    swept = sweep_stale_claims()
    expired = expire_old_alerts()
    return {"claims_swept": swept, "alerts_expired": expired}


_ALERT_MAX_AGE_DAYS = 7


def expire_old_alerts(max_age_days: int = _ALERT_MAX_AGE_DAYS) -> int:
    """Remove alerts older than max_age_days. Returns count removed."""
    with _file_lock():
        alerts = _load_alerts_unlocked()
        if not alerts:
            return 0
        now = datetime.now(_get_local_tz())
        cutoff = now - timedelta(days=max_age_days)
        original_count = len(alerts)
        alerts = [a for a in alerts if datetime.fromisoformat(a["created_at"]) > cutoff]
        removed = original_count - len(alerts)
        if removed > 0:
            _atomic_write(_alerts_file(), {"alerts": alerts})
        return removed


def get_pending(instance_id: str | None = None) -> dict[str, Any]:
    """Get pending items for a session — alerts to deliver + session crons to register.

    This is the SessionStart hook entry point:
        aya scheduler pending --format text

    Claims each alert it returns so other sessions don't re-deliver.

    Returns:
        {
            "alerts": [list of unclaimed alert dicts],
            "session_crons": [list of session-required recurring items that are active],
            "suppressed_crons": [list of {"item": ..., "reason": str} for crons skipped
                                 due to idle back-off or outside work hours],
            "instance_id": str,
        }
    """
    instance_id = instance_id or get_instance_id()
    unseen = get_unseen_alerts()

    # Claim and collect deliverable alerts
    deliverable = []
    claimed_ids: set[str] = set()
    for alert in unseen:
        if claim_alert(alert["id"], instance_id):
            deliverable.append(alert)
            claimed_ids.add(alert["id"])

    # Stamp delivery receipts on claimed alerts
    if claimed_ids:
        now = datetime.now(_get_local_tz())
        with _file_lock():
            alerts = _load_alerts_unlocked()
            for a in alerts:
                if a["id"] in claimed_ids:
                    a["delivered_at"] = now.isoformat()
                    a["delivered_by"] = instance_id
            _atomic_write(_alerts_file(), {"alerts": alerts})

    # Collect session-required recurring items, applying idle / work-hours filters.
    now = datetime.now(_get_local_tz())
    items = load_items()
    active_crons = [
        i
        for i in items
        if i.get("type") == "recurring"
        and i.get("status", "active") == "active"
        and i.get("session_required")
    ]
    session_crons = []
    suppressed_crons = []
    for item in active_crons:
        only_during = item.get("only_during", "")
        idle_back_off_str = item.get("idle_back_off", "")
        if only_during and not is_within_work_hours(only_during, now):
            reason = f"outside work hours ({only_during})"
            suppressed_crons.append({"item": item, "reason": reason})
        elif idle_back_off_str and is_idle(idle_back_off_str, now):
            reason = f"session idle (threshold: {idle_back_off_str})"
            suppressed_crons.append({"item": item, "reason": reason})
        else:
            session_crons.append(item)

    return {
        "alerts": deliverable,
        "session_crons": session_crons,
        "suppressed_crons": suppressed_crons,
        "instance_id": instance_id,
    }


def format_pending(pending: dict[str, Any]) -> str:
    """Format pending items as human-readable text for session injection."""
    lines: list[str] = []
    alerts = pending.get("alerts", [])
    crons = pending.get("session_crons", [])
    suppressed = pending.get("suppressed_crons", [])

    if alerts:
        lines.append(f"📋 {len(alerts)} pending alert(s):")
        now = datetime.now(_get_local_tz())
        for a in alerts:
            created = datetime.fromisoformat(a["created_at"])
            delta = now - created
            if delta.total_seconds() < 3600:
                ago = f"{int(delta.total_seconds() / 60)} min ago"
            elif delta.total_seconds() < 86400:
                ago = f"{int(delta.total_seconds() / 3600)}h ago"
            else:
                ago = f"{int(delta.total_seconds() / 86400)}d ago"
            lines.append(f"  • {a['message'][:70]} ({ago})")

    if crons:
        lines.append(f"\n⏰ {len(crons)} session cron(s) to register:")
        for c in crons:
            cron_id = c["id"][:12]
            cron_expr = c["cron"]
            cron_msg = c.get("message", c.get("prompt", ""))[:50]
            meta_parts = []
            if c.get("idle_back_off"):
                meta_parts.append(f"idle-back-off={c['idle_back_off']}")
            if c.get("only_during"):
                meta_parts.append(f"only-during={c['only_during']}")
            meta = f"  [{', '.join(meta_parts)}]" if meta_parts else ""
            lines.append(f'  • {cron_id}: "{cron_expr}" — {cron_msg}{meta}')

    if suppressed:
        lines.append(f"\n🔕 {len(suppressed)} cron(s) suppressed:")
        for entry in suppressed:
            c = entry["item"]
            reason = entry["reason"]
            cron_id = c["id"][:12]
            cron_msg = c.get("message", c.get("prompt", ""))[:50]
            lines.append(f"  • {cron_id}: {cron_msg} ({reason})")

    if not alerts and not crons and not suppressed:
        lines.append("No pending items.")

    return "\n".join(lines)


# ── scheduler status (Phase 5C) ─────────────────────────────────────────────


def get_scheduler_status() -> dict[str, Any]:
    """Return a structured overview of the scheduler state.

    Used by `aya scheduler status` and `make assistant-status`.
    """
    items = load_items()
    alerts = load_alerts()
    now = datetime.now(_get_local_tz())

    active_watches = [i for i in items if i.get("type") == "watch" and i.get("status") == "active"]
    pending_reminders = [
        i
        for i in items
        if i.get("type") == "reminder" and i.get("status") in ("pending", "snoozed")
    ]
    session_crons = [
        i
        for i in items
        if (
            i.get("type") == "recurring"
            and i.get("status", "active") == "active"
            and i.get("session_required")
        )
    ]
    unseen_alerts = [a for a in alerts if not a.get("seen")]
    recent_deliveries = [
        a
        for a in alerts
        if a.get("delivered_at")
        and (now - datetime.fromisoformat(a["delivered_at"])).total_seconds() < 86400
    ]

    return {
        "active_watches": active_watches,
        "pending_reminders": pending_reminders,
        "session_crons": session_crons,
        "unseen_alerts": unseen_alerts,
        "recent_deliveries": recent_deliveries,
        "total_items": len(items),
        "total_alerts": len(alerts),
    }


def format_scheduler_status(status: dict[str, Any]) -> str:
    """Format scheduler status as human-readable text."""
    lines: list[str] = []
    now = datetime.now(_get_local_tz())

    watches = status["active_watches"]
    if watches:
        lines.append(f"👁  {len(watches)} active watch(es):")
        for w in watches:
            provider = w.get("provider", "?")
            last = w.get("last_checked_at")
            interval = w.get("poll_interval_minutes", 30)
            if last:
                last_dt = datetime.fromisoformat(last)
                next_dt = last_dt + timedelta(minutes=interval)
                last_str = last_dt.strftime("%H:%M")
                next_str = next_dt.strftime("%H:%M")
                timing = f"last: {last_str}, next: ~{next_str}"
            else:
                timing = "never polled"
            lines.append(f"  • [{provider}] {w.get('message', '?')[:50]} ({timing})")
    else:
        lines.append("👁  No active watches")

    reminders = status["pending_reminders"]
    if reminders:
        lines.append(f"\n⏳ {len(reminders)} pending reminder(s):")
        for r in reminders:
            due = datetime.fromisoformat(r["due_at"])
            overdue = " ⚠️ OVERDUE" if due <= now else ""
            due_str = due.strftime("%a %b %d %I:%M %p")
            lines.append(f"  • {r['message'][:50]} — due {due_str}{overdue}")

    crons = status["session_crons"]
    if crons:
        lines.append(f"\n🔄 {len(crons)} session cron(s):")
        for c in crons:
            cron_msg = c.get("message", c.get("prompt", "?"))[:50]
            lines.append(f'  • "{c.get("cron", "?")}" — {cron_msg}')

    unseen = status["unseen_alerts"]
    if unseen:
        lines.append(f"\n🔔 {len(unseen)} unseen alert(s):")
        for a in unseen:
            lines.append(f"  • {a['message'][:60]}")

    deliveries = status["recent_deliveries"]
    if deliveries:
        lines.append(f"\n📬 {len(deliveries)} delivery(ies) in last 24h:")
        for d in deliveries:
            by = d.get("delivered_by", "?")
            at = datetime.fromisoformat(d["delivered_at"]).strftime("%H:%M")
            lines.append(f"  • {d['message'][:45]} → {by} at {at}")

    lines.append(f"\n📊 {status['total_items']} items, {status['total_alerts']} alerts")

    return "\n".join(lines)


def _display_items(items: list[dict[str, Any]]) -> None:
    """Pretty-print scheduler items grouped by type."""
    if not items:
        return

    now = datetime.now(_get_local_tz())

    for item_type in ("reminder", "watch", "recurring", "event"):
        typed = [i for i in items if i.get("type") == item_type]
        if not typed:
            continue
        for i in typed:
            {
                "pending": "⏳",
                "active": "✅",
                "snoozed": "💤",
                "delivered": "📬",
                "dismissed": "✗",
            }.get(i.get("status", "active"), "•")
            f" [{', '.join(i['tags'])}]" if i.get("tags") else ""

            if i.get("type") == "reminder":
                due = datetime.fromisoformat(i["due_at"])
                due.strftime("%a %b %d, %I:%M %p")
                due <= now and i.get("status") == "pending"
            elif i.get("type") == "watch":
                i.get("provider", "?")
                i.get("poll_interval_minutes", "?")
                last = i.get("last_checked_at")
                datetime.fromisoformat(last).strftime("%H:%M") if last else "never"
            elif i.get("type") == "recurring":
                i.get("cron", "?")
                " [session]" if i.get("session_required") else ""
            elif i.get("type") == "event":
                i.get("trigger", "?")
