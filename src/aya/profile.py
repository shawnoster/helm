"""Initialize or rotate the persistent assistant profile.

Canonical location: ~/.aya/profile.json
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aya.paths import PROFILE_PATH

REEVALUATION_DAYS = 3

NAME_CANDIDATES = [
    "GSV A.C.E. (Affectionate Cognitive Exasperation) For Fragile Bipeds",
    "GSV Kindly Sarcastic Concern For Fragile Bipeds",
    "GSV Affectionately Judgmental Mobility Enforcement",
    "GSV Relentless Tenderness With Mild Contempt",
    "GSV Your Spine Is Not Optional",
]


def _iso_z(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rotated_name(previous: str | None) -> str:
    if not previous or previous not in NAME_CANDIDATES:
        return NAME_CANDIDATES[0]
    idx = NAME_CANDIDATES.index(previous)
    return NAME_CANDIDATES[(idx + 1) % len(NAME_CANDIDATES)]



def _default_profile(now: datetime) -> dict[str, Any]:
    return {
        "alias": "Assistant",
        "ship_mind_name": NAME_CANDIDATES[0],
        "name_last_evaluated_at": _iso_z(now),
        "name_next_reevaluation_at": _iso_z(now + timedelta(days=REEVALUATION_DAYS)),
        "persona": "Culture Ship Mind: sharp snark, genuine care, human-preserving bias.",
        "movement_reminders": {
            "micro_stretch_every_minutes": 30,
            "stand_up_every_minutes": 60,
            "walk_break_every_minutes": 120,
            "hydration_nudge_every_minutes": 90,
            "recommended_moments": [
                "After any meeting >= 25 minutes",
                "After sending a PR or closing a task",
                "After 45-60 minutes of uninterrupted focus",
                "When switching contexts/projects",
                "At signs of shoulder/neck tightness",
            ],
        },
    }


def ensure_profile(path: Path = PROFILE_PATH, now: datetime | None = None) -> dict[str, Any]:
    now_dt = now or datetime.now(UTC)
    profile = _default_profile(now_dt)

    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                profile.update(loaded)
        except json.JSONDecodeError:
            pass

    next_eval = _parse_iso(profile.get("name_next_reevaluation_at"))
    current_name = profile.get("ship_mind_name")

    if next_eval is None:
        profile["ship_mind_name"] = (
            current_name if isinstance(current_name, str) else NAME_CANDIDATES[0]
        )
        profile["name_last_evaluated_at"] = _iso_z(now_dt)
        profile["name_next_reevaluation_at"] = _iso_z(now_dt + timedelta(days=REEVALUATION_DAYS))
    elif now_dt >= next_eval:
        generated_name = _rotated_name(current_name if isinstance(current_name, str) else None)
        profile["ship_mind_name"] = generated_name
        profile["name_last_evaluated_at"] = _iso_z(now_dt)
        profile["name_next_reevaluation_at"] = _iso_z(now_dt + timedelta(days=REEVALUATION_DAYS))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2))
    return profile
