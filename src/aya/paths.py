"""Centralized path resolution for aya data storage.

All aya data lives under AYA_HOME (~/.aya by default).
Override with the AYA_HOME environment variable (useful for tests).

Workspace-relative paths (CLAUDE.md, AGENTS.md, daily notes) are NOT
defined here — those belong to the notebook repo, not to aya.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_aya_home_env = os.environ.get("AYA_HOME")
AYA_HOME = Path(_aya_home_env).expanduser() if _aya_home_env else Path.home() / ".aya"

# ── identity ────────────────────────────────────────────────────────────────
PROFILE_PATH = AYA_HOME / "profile.json"
CONFIG_PATH = AYA_HOME / "config.json"

# ── scheduler data ──────────────────────────────────────────────────────────
MEMORY_DIR = AYA_HOME / "memory"
SCHEDULER_FILE = MEMORY_DIR / "scheduler.json"
ALERTS_FILE = MEMORY_DIR / "alerts.json"
ACTIVITY_FILE = MEMORY_DIR / "activity.json"
LOCK_FILE = MEMORY_DIR / ".scheduler.lock"
CLAIMS_DIR = MEMORY_DIR / "claims"

# ── legacy markdown (kept for migration, may be removed) ───────────────────
CRON_SCHEDULES_PATH = MEMORY_DIR / "cron-schedules.md"


# ── migration ───────────────────────────────────────────────────────────────


def _migrate_files() -> dict[str, Path]:
    """Build migration map from current module-level paths (test-patchable)."""
    _self = sys.modules[__name__]

    return {
        "assistant/profile.json": _self.PROFILE_PATH,
        "assistant/config.json": _self.CONFIG_PATH,
        "assistant/memory/scheduler.json": _self.SCHEDULER_FILE,
        "assistant/memory/alerts.json": _self.ALERTS_FILE,
        "assistant/memory/activity.json": _self.ACTIVITY_FILE,
        "assistant/memory/cron-schedules.md": _self.CRON_SCHEDULES_PATH,
    }


def _find_legacy_workspace() -> Path | None:
    """Find old workspace-relative aya data (pre-~/.aya layout)."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "assistant" / "memory" / "scheduler.json").exists():
            return parent
    return None


def ensure_home() -> None:
    """Create ~/.aya/memory if it doesn't exist."""
    _self = sys.modules[__name__]

    _self.MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def migrate_if_needed() -> list[str]:
    """One-time migration from workspace-relative layout to ~/.aya.

    Returns list of migrated file descriptions (empty if nothing to do).
    Only migrates if ~/.aya doesn't have a scheduler.json yet AND the old
    workspace layout exists.
    """
    _self = sys.modules[__name__]

    if _self.SCHEDULER_FILE.exists():
        return []  # already migrated or fresh install

    workspace = _find_legacy_workspace()
    if workspace is None:
        return []

    ensure_home()
    migrated = []
    for old_rel, new_path in _migrate_files().items():
        old_path = workspace / old_rel
        if old_path.exists() and not new_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
            migrated.append(f"{old_rel} → {new_path}")

    return migrated
