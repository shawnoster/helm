"""Scheduler persistence — file paths, locking, atomic writes, load/save."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast, overload

from aya import paths as _paths

from .time_utils import _get_local_tz
from .types import (
    ALERTS_SCHEMA_VERSION,
    SCHEDULER_SCHEMA_VERSION,
    AlertItem,
    SchedulerItem,
    _alerts_data,
    _check_schema_version,
    _scheduler_data,
)

logger = logging.getLogger(__name__)

# ── Module-level path accessors ─────────────────────────────────────────────
# These functions check the *package* __init__ globals first so that
# monkeypatch still works in tests (e.g. monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", ...)).
# Otherwise they delegate to the canonical paths in aya.paths.


def _get_package_globals() -> dict[str, Any]:
    """Return the package __init__ globals dict for monkeypatch support."""
    import aya.scheduler as _pkg

    return vars(_pkg)


def _scheduler_file() -> Path:
    pkg = _get_package_globals()
    return pkg.get("SCHEDULER_FILE") or _paths.SCHEDULER_FILE


def _alerts_file() -> Path:
    pkg = _get_package_globals()
    return pkg.get("ALERTS_FILE") or _paths.ALERTS_FILE


def _activity_file() -> Path:
    pkg = _get_package_globals()
    return pkg.get("ACTIVITY_FILE") or _paths.ACTIVITY_FILE


def _lock_file() -> Path:
    """Return the advisory lock file path, co-located with scheduler.json."""
    pkg = _get_package_globals()
    if "LOCK_FILE" in pkg and pkg["LOCK_FILE"] is not None:
        val = pkg["LOCK_FILE"]
        if isinstance(val, Path):
            return val
    # Derive from scheduler file parent so test isolation via SCHEDULER_FILE works.
    if "SCHEDULER_FILE" in pkg and pkg["SCHEDULER_FILE"] is not None:
        val = pkg["SCHEDULER_FILE"]
        if isinstance(val, Path):
            return val.parent / ".scheduler.lock"
    return _paths.LOCK_FILE


def _claims_dir() -> Path:
    """Return the claims directory, co-located with scheduler.json."""
    pkg = _get_package_globals()
    if "CLAIMS_DIR" in pkg and pkg["CLAIMS_DIR"] is not None:
        val = pkg["CLAIMS_DIR"]
        if isinstance(val, Path):
            return val
    # Derive from scheduler file parent so test isolation via SCHEDULER_FILE works.
    if "SCHEDULER_FILE" in pkg and pkg["SCHEDULER_FILE"] is not None:
        val = pkg["SCHEDULER_FILE"]
        if isinstance(val, Path):
            return val.parent / "claims"
    return _paths.CLAIMS_DIR


# ── locking ──────────────────────────────────────────────────────────────────


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


# ── atomic writes ────────────────────────────────────────────────────────────


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically: tmp file -> fsync -> rename.

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


# ── load / save ──────────────────────────────────────────────────────────────


def _load_collection_unlocked(path: Path, key: str) -> list[dict[str, Any]]:
    """Generic loader for JSON collections (caller holds lock)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    # Forward compatibility: warn if schema is newer than expected
    if key == "items":
        _check_schema_version(data, SCHEDULER_SCHEMA_VERSION, "scheduler.json")
    elif key == "alerts":
        _check_schema_version(data, ALERTS_SCHEMA_VERSION, "alerts.json")
    result = data.get(key, [])
    return result if isinstance(result, list) else []


def _load_items_unlocked() -> list[SchedulerItem]:
    """Read scheduler items without acquiring a lock (caller holds lock)."""
    return cast(list[SchedulerItem], _load_collection_unlocked(_scheduler_file(), "items"))


def _load_alerts_unlocked() -> list[AlertItem]:
    """Read alerts without acquiring a lock (caller holds lock)."""
    return cast(list[AlertItem], _load_collection_unlocked(_alerts_file(), "alerts"))


def load_items() -> list[SchedulerItem]:
    data = _locked_read(_scheduler_file())
    if not data:
        return []
    _check_schema_version(data, SCHEDULER_SCHEMA_VERSION, "scheduler.json")
    items = data.get("items", [])
    return cast(list[SchedulerItem], items if isinstance(items, list) else [])


def save_items(items: list[SchedulerItem]) -> None:
    with _file_lock():
        _atomic_write(_scheduler_file(), _scheduler_data(items))


def load_alerts() -> list[AlertItem]:
    data = _locked_read(_alerts_file())
    if not data:
        return []
    _check_schema_version(data, ALERTS_SCHEMA_VERSION, "alerts.json")
    alerts = data.get("alerts", [])
    return cast(list[AlertItem], alerts if isinstance(alerts, list) else [])


def save_alerts(alerts: list[AlertItem]) -> None:
    with _file_lock():
        _atomic_write(_alerts_file(), _alerts_data(alerts))


# ── claims (alert delivery dedup) ───────────────────────────────────────────

_CLAIM_TTL_SECONDS = 300  # 5 minutes — if a session crashes, claim expires


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


# ── helpers ──────────────────────────────────────────────────────────────────


def _new_id() -> str:
    return str(uuid.uuid4())


def _parse_tags(tags: str) -> list[str]:
    return [t.strip() for t in tags.split(",") if t.strip()] if tags else []


@overload
def _find(items: list[SchedulerItem], item_id: str) -> SchedulerItem | None: ...


@overload
def _find(items: list[AlertItem], item_id: str) -> AlertItem | None: ...


def _find(
    items: list[SchedulerItem] | list[AlertItem], item_id: str
) -> SchedulerItem | AlertItem | None:
    for item in items:
        if item["id"] == item_id or item["id"].startswith(item_id):
            return item
    return None


def get_unseen_alerts() -> list[AlertItem]:
    """Return unseen alerts from daemon."""
    return [a for a in load_alerts() if not a.get("seen")]


# ── harness detection + instance identity ────────────────────────────────────


def _detect_harness() -> str:
    """Detect which AI harness is running this process.

    Checks environment variables to identify the caller:
    - CLAUDE_CODE or CLAUDE_* -> "claude"
    - GITHUB_COPILOT or COPILOT_* -> "copilot"
    - Otherwise -> "unknown"
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


# ── session lock ────────────────────────────────────────────────────────────

_SESSION_LOCK_STALE_MINUTES = 15


def _session_lock_file() -> Path:
    """Return the session lock file path."""
    pkg = _get_package_globals()
    if "SESSION_LOCK_FILE" in pkg and pkg["SESSION_LOCK_FILE"] is not None:
        val = pkg["SESSION_LOCK_FILE"]
        if isinstance(val, Path):
            return val
    return _paths.AYA_HOME / "session.lock"


def write_session_lock(instance_id: str | None = None) -> None:
    """Write a session lock indicating an active REPL session.

    Called alongside activity recording so the lock stays fresh
    as long as the session is active.
    """
    instance_id = instance_id or get_instance_id()
    lock_path = _session_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {
            "instance_id": instance_id,
            "locked_at": datetime.now(_get_local_tz()).isoformat(),
        }
    )
    # Atomic write — safe for concurrent readers
    fd, tmp = tempfile.mkstemp(dir=str(lock_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(content.encode())
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).replace(lock_path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        raise
    logger.debug("session lock: written for %s", instance_id)


def clear_session_lock(instance_id: str | None = None) -> bool:
    """Remove session lock, optionally scoped to a specific instance.

    When ``instance_id`` is provided, only clears the lock if it belongs
    to that instance. When omitted, clears the lock unconditionally.

    The primary cleanup mechanism is stale detection: ``is_session_active()``
    checks whether ``activity.json`` has been updated within the last 15
    minutes, so crashed or abandoned sessions are automatically treated as
    inactive without explicit cleanup.

    This function exists for explicit cleanup in future SessionEnd hooks,
    where the REPL can proactively clear the lock on graceful shutdown
    rather than waiting for the staleness timeout.

    Returns True if lock was cleared, False if it didn't exist or
    belonged to another instance.
    """
    lock_path = _session_lock_file()
    if not lock_path.exists():
        return False
    try:
        data = json.loads(lock_path.read_text())
        if instance_id and data.get("instance_id") != instance_id:
            return False
        lock_path.unlink(missing_ok=True)
        logger.debug("session lock: cleared for %s", instance_id)
        return True
    except (json.JSONDecodeError, OSError):
        lock_path.unlink(missing_ok=True)
        return True


def is_session_active() -> bool:
    """Return True if a REPL session is currently active.

    A session is active when the lock file exists AND activity.json
    last_activity_at is within _SESSION_LOCK_STALE_MINUTES minutes
    (stale lock protection for REPL crashes).
    """
    lock_path = _session_lock_file()
    if not lock_path.exists():
        return False

    # Validate the lock isn't stale by checking activity
    from .time_utils import get_last_activity

    last_activity = get_last_activity()
    if last_activity is None:
        return False

    now = datetime.now(_get_local_tz())
    stale_threshold = timedelta(minutes=_SESSION_LOCK_STALE_MINUTES)
    is_active = (now - last_activity) < stale_threshold
    logger.debug(
        "session lock: exists=%s, last_activity=%s, active=%s",
        True,
        last_activity.isoformat() if last_activity else "never",
        is_active,
    )
    return is_active


# ── per-session registered crons tracker ───────────────────────────────────
#
# `aya hook crons` reads active session crons and emits hookSpecificOutput
# JSON instructing Claude Code to register them via CronCreate. To support
# mid-session registration (i.e. running `aya schedule recurring` after
# the session has already started), we track which cron IDs have already
# been emitted within the current session, so a follow-up call only emits
# the newly created ones.
#
# The tracker is reset at SessionStart via `aya hook crons --reset`, and
# is consulted (without reset) by the PostToolUse hook entry to surface
# any newly-created crons on the next tool boundary.


def _registered_crons_file() -> Path:
    """Return the per-session registered crons tracker file path.

    Honors a package-level override (``REGISTERED_CRONS_FILE``) so tests
    can redirect the path without touching ``AYA_HOME``.
    """
    pkg = _get_package_globals()
    if "REGISTERED_CRONS_FILE" in pkg and pkg["REGISTERED_CRONS_FILE"] is not None:
        val = pkg["REGISTERED_CRONS_FILE"]
        if isinstance(val, Path):
            return val
    return _paths.AYA_HOME / "session_registered_crons.json"


def load_registered_cron_ids() -> set[str]:
    """Return the set of cron IDs already registered in this session."""
    path = _registered_crons_file()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(data, dict):
        return set()
    raw_ids = data.get("ids", [])
    if not isinstance(raw_ids, list):
        return set()
    return {pid for pid in raw_ids if isinstance(pid, str)}


def save_registered_cron_ids(ids: set[str]) -> None:
    """Persist the set of cron IDs registered in this session."""
    path = _registered_crons_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(_get_local_tz()).isoformat(),
        "ids": sorted(ids),
    }
    _atomic_write(path, payload)


def reset_registered_cron_ids() -> None:
    """Clear the per-session registered crons tracker.

    Called at SessionStart so a fresh session re-registers everything.
    """
    path = _registered_crons_file()
    if path.exists():
        path.unlink(missing_ok=True)
