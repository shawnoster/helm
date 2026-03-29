"""Initialize or rotate the persistent assistant profile.

Canonical location: ~/.aya/profile.json
Legacy location:    ~/.copilot/assistant_profile.json (migrated on first run)
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from aya.paths import ACTIVITY_TRACKER_PATH, PROFILE_PATH

LEGACY_COPILOT_PATH = Path.home() / ".copilot" / "assistant_profile.json"
LEGACY_ACE_PATH = Path.home() / ".copilot" / "ace_profile.json"
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


def _activity_entries_last_days(path: Path, now: datetime, days: int = 3) -> list[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []

    cutoff = now.date() - timedelta(days=days - 1)
    active = False
    entries: list[str] = []

    for raw in lines:
        line = raw.strip()
        if line.startswith("## "):
            date_text = line[3:].strip()
            try:
                section_date = date.fromisoformat(date_text)
            except ValueError:
                active = False
                continue
            active = section_date >= cutoff
            continue
        if active and line.startswith("- "):
            entries.append(line[2:].strip())
    return entries


def _activity_themes(entries: list[str]) -> dict[str, int]:
    keyword_groups: dict[str, tuple[str, ...]] = {
        "workflow": ("workflow", "status", "ritual", "launch", "setup"),
        "memory": ("memory", "tracker", "done-log", "cadence", "profile"),
        "docs": ("doc", "docs", "readme", "guide", "map", "confluence"),
        "automation": ("script", "tooling", "make", "automation", "helper"),
        "architecture": ("architecture", "diagram", "c4", "domain", "platform"),
    }
    scores = dict.fromkeys(keyword_groups, 0)
    for item in entries:
        text = item.lower()
        for theme, keywords in keyword_groups.items():
            if any(keyword in text for keyword in keywords):
                scores[theme] += 1
    return scores


def _name_from_activity(now: datetime, entries: list[str]) -> str:
    if not entries:
        return NAME_CANDIDATES[now.toordinal() % len(NAME_CANDIDATES)]

    scored = _activity_themes(entries)
    top_themes = sorted(scored.items(), key=lambda pair: (pair[1], pair[0]), reverse=True)[:2]
    theme_order = [name for name, score in top_themes if score > 0]

    if not theme_order:
        return NAME_CANDIDATES[now.toordinal() % len(NAME_CANDIDATES)]

    descriptors = {
        "workflow": "Ritualized Momentum",
        "memory": "Persistent Memory",
        "docs": "Documentation Gravity",
        "automation": "Practical Automation",
        "architecture": "Architectural Clarity",
    }
    mission = {
        "workflow": "for Reluctant Chaos",
        "memory": "for Forgetful Mortals",
        "docs": "for Future Humans",
        "automation": "for Repetitive Tasks",
        "architecture": "for Complicated Systems",
    }

    primary = theme_order[0]
    secondary = theme_order[1] if len(theme_order) > 1 else primary
    return f"GSV {descriptors[primary]} and {descriptors[secondary]} {mission[primary]}"


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

    # Migrate from legacy locations if canonical path doesn't exist yet.
    # Priority: ~/.copilot/assistant_profile.json > ~/.copilot/ace_profile.json
    if not path.exists():
        for legacy in (LEGACY_COPILOT_PATH, LEGACY_ACE_PATH):
            if legacy.exists() and not legacy.is_symlink():
                path.parent.mkdir(parents=True, exist_ok=True)
                legacy.rename(path)
                break

    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                profile.update(loaded)
        except json.JSONDecodeError:
            pass

    next_eval = _parse_iso(profile.get("name_next_reevaluation_at"))
    current_name = profile.get("ship_mind_name")
    recent_activity = _activity_entries_last_days(ACTIVITY_TRACKER_PATH, now_dt, days=3)

    if next_eval is None:
        profile["ship_mind_name"] = (
            current_name if isinstance(current_name, str) else NAME_CANDIDATES[0]
        )
        profile["name_last_evaluated_at"] = _iso_z(now_dt)
        profile["name_next_reevaluation_at"] = _iso_z(now_dt + timedelta(days=REEVALUATION_DAYS))
    elif now_dt >= next_eval:
        generated_name = _name_from_activity(now_dt, recent_activity)
        if generated_name == (current_name if isinstance(current_name, str) else None):
            generated_name = _rotated_name(current_name if isinstance(current_name, str) else None)
        profile["ship_mind_name"] = generated_name
        profile["name_last_evaluated_at"] = _iso_z(now_dt)
        profile["name_next_reevaluation_at"] = _iso_z(now_dt + timedelta(days=REEVALUATION_DAYS))
        profile["name_basis"] = {
            "source": "assistant/memory/activity-tracker.md",
            "window_days": 3,
            "activity_items_considered": len(recent_activity),
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2))
    return profile
