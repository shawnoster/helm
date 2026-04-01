"""Centralized path resolution for aya data storage.

All aya data lives under AYA_HOME (~/.aya by default).
Override with the AYA_HOME environment variable (useful for tests).

Workspace-relative paths (CLAUDE.md, AGENTS.md, daily notes) are NOT
defined here — those belong to the notebook repo, not to aya.
"""

from __future__ import annotations

import os
from pathlib import Path

_aya_home_env = os.environ.get("AYA_HOME")
AYA_HOME = Path(_aya_home_env).expanduser() if _aya_home_env else Path.home() / ".aya"

# ── identity ────────────────────────────────────────────────────────────────
PROFILE_PATH = AYA_HOME / "profile.json"

# ── workspace config ────────────────────────────────────────────────────────
CONFIG_PATH = AYA_HOME / "config.json"

# ── scheduler data ──────────────────────────────────────────────────────────
SCHEDULER_FILE = AYA_HOME / "scheduler.json"
ALERTS_FILE = AYA_HOME / "alerts.json"
ACTIVITY_FILE = AYA_HOME / "activity.json"
LOCK_FILE = AYA_HOME / ".scheduler.lock"
CLAIMS_DIR = AYA_HOME / "claims"
