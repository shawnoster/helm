"""Time parsing, activity tracking, idle detection, and work-hours helpers."""

from __future__ import annotations

import functools
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ── timezone ─────────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _get_local_tz() -> ZoneInfo:
    """Get the local timezone from AYA_TZ env var, with system detection fallback.

    Resolution order:
    1. AYA_TZ environment variable (explicit override)
    2. /etc/timezone (plain text IANA name, common on Debian/Ubuntu/WSL)
    3. /etc/localtime symlink target (common on most Linux distros)
    4. UTC as last resort

    Always returns a proper ZoneInfo with DST rules. Caching ensures
    consistent timezone throughout the session.
    """
    import pathlib

    tz_name = os.environ.get("AYA_TZ", "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except KeyError:
            logger.warning("Invalid AYA_TZ %r; falling back to system timezone", tz_name)

    # Try /etc/timezone (Debian/Ubuntu/WSL — plain text IANA name)
    try:
        p = pathlib.Path("/etc/timezone")
        if p.exists():
            iana = p.read_text().strip()
            if iana:
                return ZoneInfo(iana)
    except Exception:
        logger.debug("Failed to read /etc/timezone")

    # Try /etc/localtime symlink (most Linux distros)
    try:
        p = pathlib.Path("/etc/localtime")
        resolved = str(p.resolve())
        if "zoneinfo/" in resolved:
            iana = resolved.split("zoneinfo/", 1)[1]
            return ZoneInfo(iana)
    except Exception:
        logger.debug("Failed to resolve /etc/localtime")

    logger.warning("Could not detect system timezone; falling back to UTC")
    return ZoneInfo("UTC")


# ── alert expiry constant ────────────────────────────────────────────────────

_ALERT_MAX_AGE_DAYS = 7

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


# ── duration parsing ─────────────────────────────────────────────────────────

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

    Accepts "HH:MM-HH:MM", e.g. "08:00-18:00" -> ((8, 0), (18, 0)).

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


# ── activity / idle helpers ──────────────────────────────────────────────────

# These functions import from storage at call time to avoid circular imports.


def record_activity(now: datetime | None = None) -> None:
    """Record the current time as the last-known user activity.

    Writes ``{"last_activity_at": "<ISO timestamp>"}`` to the activity file
    and refreshes the session lock so that ``is_session_active()`` reflects
    the active REPL.

    Safe to call from any hook or command that indicates user presence.
    """
    from .storage import _activity_file, _atomic_write, _file_lock, write_session_lock

    if now is None:
        now = datetime.now(_get_local_tz())
    path = _activity_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock():
        _atomic_write(path, {"last_activity_at": now.isoformat()})
    # Refresh session lock alongside activity — keeps lock fresh as long as
    # the REPL is alive, and goes stale naturally via the 15-min check.
    write_session_lock()
    logger.debug("activity: recorded at %s (session lock refreshed)", now.isoformat())


def get_last_activity() -> datetime | None:
    """Return the timestamp of the last recorded user activity, or None."""
    from .storage import _activity_file, _locked_read

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
        logger.debug("idle check: threshold=%s, last_activity=never, idle=False", threshold_str)
        return False
    if now is None:
        now = datetime.now(_get_local_tz())
    result = (now - last) >= threshold
    logger.debug(
        "idle check: threshold=%s, last_activity=%s, idle=%s",
        threshold_str,
        last.isoformat(),
        result,
    )
    return result
