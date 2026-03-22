"""Ship Mind status ritual — workspace readiness check."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_assist.scheduler import _find_workspace_root

ROOT = _find_workspace_root()
ASSISTANT = ROOT / "assistant"
MEMORY = ASSISTANT / "memory"
PROFILE = Path.home() / ".copilot" / "assistant_profile.json"
CONFIG = ASSISTANT / "config.json"


# ── data ──────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


# ── helpers ───────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _exists(path: Path, name: str) -> CheckResult:
    return CheckResult(name=name, ok=path.exists(), detail=str(path))


# ── greeting ──────────────────────────────────────────────────────────────────

def _greeting(now: datetime, user: str, ship: str) -> str:
    hour = now.hour
    if hour < 6:
        salutation = f"Still running at this hour, {user}."
    elif hour < 12:
        salutation = f"Good morning, {user}."
    elif hour < 17:
        salutation = f"Good afternoon, {user}."
    elif hour < 21:
        salutation = f"Evening, {user}."
    else:
        salutation = f"Still at it, {user}."
    return f"{salutation} {ship} online."


def _time_flavor(now: datetime) -> str:
    hour = now.hour
    table = [
        (range(6, 9),  "Coffee consumed? Let's make the day count."),
        (range(9, 12), "Morning focus window. Best cognition of the day — use it before the meetings eat it."),
        (range(12, 14),"Post-lunch territory. Carbs are the enemy of momentum. Push through."),
        (range(14, 17),"Afternoon. Attention debt accumulates here. One thing at a time."),
        (range(17, 19),"End-of-day push. Close the loop on something before you log off."),
        (range(19, 22),"Late session. Diminishing returns are real. Mind the clock."),
    ]
    for rng, flavor in table:
        if hour in rng:
            return flavor
    return "Unconventional hours. The ship is watching regardless."


# ── daily notes parser ────────────────────────────────────────────────────────

def _parse_time(time_str: str, pm_context: bool = False) -> datetime | None:
    """Parse 'H:MM' or 'HH:MM' into today's datetime, respecting AM/PM context."""
    m = re.match(r"(\d{1,2}):(\d{2})", time_str)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if pm_context and hour != 12:
        hour += 12
    elif not pm_context and hour == 12:
        hour = 0
    try:
        return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None


def _parse_block_header(header: str) -> tuple[datetime | None, datetime | None, str]:
    """
    Parse a time block header like '9:00–10:00 AM', '11:05 AM–12:00 PM',
    '2:30–2:55 PM', '4:00 PM+', '11:05 AM'.
    Returns (start_dt, end_dt_or_None, label).
    """
    upper = header.upper()

    # Range with explicit AM/PM on each side: '11:05 AM–12:00 PM'
    mixed_m = re.match(
        r"(\d{1,2}:\d{2})\s*(AM|PM)[\u2013\-](\d{1,2}:\d{2})\s*(AM|PM)",
        header, re.IGNORECASE,
    )
    if mixed_m:
        start = _parse_time(mixed_m.group(1), mixed_m.group(2).upper() == "PM")
        end   = _parse_time(mixed_m.group(3), mixed_m.group(4).upper() == "PM")
        return start, end, header

    # Range with shared AM/PM suffix: '9:00–10:00 AM' or '2:30–2:55 PM'
    shared_m = re.match(r"(\d{1,2}:\d{2})[\u2013\-](\d{1,2}:\d{2})\s*(AM|PM)", header, re.IGNORECASE)
    if shared_m:
        is_pm = shared_m.group(3).upper() == "PM"
        start = _parse_time(shared_m.group(1), is_pm)
        end   = _parse_time(shared_m.group(2), is_pm)
        if start and end and end <= start:  # e.g. 12:15–2:00 PM where start wraps
            end = end.replace(hour=end.hour + 12)
        return start, end, header

    # Single time with AM/PM: '11:05 AM' or '4:00 PM+'
    single_m = re.match(r"(\d{1,2}:\d{2})\s*(AM|PM)", header, re.IGNORECASE)
    if single_m:
        is_pm = single_m.group(2).upper() == "PM"
        start = _parse_time(single_m.group(1), is_pm)
        end = (start + timedelta(hours=1)) if start else None
        return start, end, header

    return None, None, header


def _parse_daily_notes(today: str) -> dict[str, Any]:
    notes_path = ASSISTANT / "notes" / "daily" / f"{today}.md"
    result: dict[str, Any] = {
        "found": False,
        "priorities": [],
        "current_block": None,
        "next_block": None,
    }
    if not notes_path.exists():
        return result

    result["found"] = True
    content = notes_path.read_text()
    now = datetime.now()

    # Priority stack — numbered lines inside ``` block
    # Skip completed (✅) and struck-through (~~) lines
    prio_m = re.search(r"## Priority Stack.*?```\n(.*?)```", content, re.DOTALL)
    if prio_m:
        result["priorities"] = [
            ln.strip()
            for ln in prio_m.group(1).strip().splitlines()
            if re.match(r"^\d+\.", ln.strip()) and "✅" not in ln and not ln.strip().startswith("~~")
        ]

    # Filter out priorities referencing past time-of-day events
    if result["priorities"]:
        filtered = []
        time_ref_re = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)
        for p in result["priorities"]:
            m_time = time_ref_re.search(p)
            if m_time:
                ref_time = _parse_time(m_time.group(1) + ":" + m_time.group(2),
                                       m_time.group(3).upper() == "PM")
                if ref_time and ref_time < now:
                    continue
            filtered.append(p)
        result["priorities"] = filtered

    # Time blocks — ### <time> — <label>
    block_pattern = re.compile(
        r"^### ([^\n]+?) —[^\n]*\n((?:(?!^###).)*)",
        re.MULTILINE | re.DOTALL,
    )

    current: dict[str, Any] | None = None
    next_blk: dict[str, Any] | None = None
    last_past_blk: dict[str, Any] | None = None

    for m in block_pattern.finditer(content):
        header = m.group(1).strip()
        # Skip struck-through blocks (e.g., ~~2:15 PM — cancelled~~)
        if header.startswith("~~") or header.endswith("~~"):
            continue
        body = m.group(2)

        start, end, _ = _parse_block_header(header)
        if start is None:
            continue

        bullets = [
            ln.strip().lstrip("- *").strip()
            for ln in body.splitlines()
            if ln.strip().startswith("-")
        ]
        bullets = [b for b in bullets if b]

        block_end = end or start + timedelta(hours=1)
        if start <= now < block_end:
            current = {"time": header, "tasks": bullets[:4]}
        elif block_end <= now:
            last_past_blk = {"time": header, "tasks": bullets[:4]}
        elif start > now and next_blk is None:
            next_blk = {"time": header, "tasks": bullets[:3]}

    result["current_block"] = current
    result["next_block"] = next_blk
    result["last_past_block"] = last_past_blk
    return result


# ── cron watch summary ────────────────────────────────────────────────────────

def _cron_watches() -> list[str]:
    """
    Extract named cron job entries from cron-schedules.md.
    A valid entry has a job ID (hex token) or a cron expression in backticks.
    """
    path = MEMORY / "cron-schedules.md"
    if not path.exists():
        return []

    job_id_re    = re.compile(r"`[0-9a-f]{8}`")          # e.g. `e6a8407c`
    cron_expr_re = re.compile(r"`[\d\*,/\- ]+ [\d\*,/\- ]+")  # e.g. `17,47 * * * *`

    watches = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("**"):
            continue
        if not (job_id_re.search(stripped) or cron_expr_re.search(stripped)):
            continue
        # Extract bold label
        end = stripped.index("**", 2)
        label = stripped[2:end]
        # Short descriptor: text after the label, before first parens or long dash
        rest = stripped[end + 2:].strip(" —–-").strip()
        short = re.split(r"\s*[\(—]", rest)[0].strip(" -–").strip()
        entry = label + (f" — {short}" if short else "")
        watches.append(entry)

    return watches[:6]


# ── perspective ───────────────────────────────────────────────────────────────

def _perspective() -> str:
    lines = [
        "Break the impossible into next actions and proceed with unreasonable calm.",
        "Purpose is local, meaning is cumulative, and git history remembers everything.",
        "Most crises are just queued decisions wearing dramatic hats.",
        "Entropy hates momentum. Ship small, ship often.",
        "The answer is 42, but the method is: observe, decide, act, iterate.",
        "Hydrate. Stretch. The biological subsystems are not optional peripherals.",
    ]
    return lines[datetime.now(UTC).toordinal() % len(lines)]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    now_local = datetime.now()
    today = now_local.strftime("%Y-%m-%d")

    # Profile
    profile = _read_json(PROFILE)
    ship = profile.get("ship_mind_name", "GSV Unknown Vessel") if profile else "GSV Unknown Vessel"
    user = profile.get("user_name", "Shawn") if profile else "Shawn"
    next_eval = profile.get("name_next_reevaluation_at", "unknown") if profile else "unknown"

    # System checks
    checks: list[CheckResult] = [
        _exists(ROOT / "CLAUDE.md",                        "root CLAUDE"),
        _exists(ASSISTANT / "AGENTS.md",                   "assistant AGENTS"),
        _exists(ASSISTANT / "CLAUDE.md",                   "assistant CLAUDE"),
        _exists(ASSISTANT / "ship_mind_persona.prompt",    "Ship persona prompt"),
        _exists(MEMORY / "README.md",                      "memory::README.md"),
        _exists(MEMORY / "preferences.md",                 "memory::preferences.md"),
        _exists(MEMORY / "cron-schedules.md",              "memory::cron-schedules.md"),
        _exists(MEMORY / "activity-tracker.md",            "memory::activity-tracker.md"),
        _exists(MEMORY / "done-log.md",                    "memory::done-log.md"),
        CheckResult("Assistant profile", profile is not None, str(PROFILE)),
        CheckResult("workflow config", _read_json(CONFIG) is not None, str(CONFIG)),
    ]
    ok = sum(1 for c in checks if c.ok)
    total = len(checks)
    all_ok = ok == total

    # Daily notes
    notes = _parse_daily_notes(today)

    # ── Output ──────────────────────────────────────────────────────────────

    W = 72
    print()
    print("─" * W)

    # Greeting
    print()
    print(f"  {_greeting(now_local, user, ship)}")
    print(f"  {_time_flavor(now_local)}")
    print()

    # Systems — compact when green, verbose on failure
    if all_ok:
        print(f"  ◈  Systems     {ok}/{total} green. Control plane nominal.")
    else:
        print(f"  ◈  Systems     ⚠  {ok}/{total} — degraded:")
        for c in checks:
            if not c.ok:
                print(f"       ✗  {c.name:<24} {c.detail}")

    if isinstance(next_eval, str) and len(next_eval) >= 10:
        print(f"     Name rotation  {next_eval[:10]}")
    print()

    # Focus — current time block
    if notes["found"] and notes["current_block"]:
        blk = notes["current_block"]
        print(f"  ◈  Right now   {blk['time']}")
        for task in blk["tasks"]:
            print(f"       →  {task[:70]}")
    elif notes["found"] and notes["priorities"]:
        remaining = len(notes["priorities"])
        print(f"  ◈  Focus       {remaining} remaining priorities:")
        for p in notes["priorities"][:3]:
            print(f"       →  {p.strip()[:70]}")
    elif notes["found"]:
        print("  ◈  Focus       Day's priorities are done or past. Nice work.")
    else:
        print("  ◈  Focus       No daily notes found. Run /morning to load your day.")
    print()

    # Up next
    if notes["found"] and notes["next_block"]:
        nb = notes["next_block"]
        print(f"  ◈  Up next     {nb['time']}")
        for task in nb["tasks"][:2]:
            print(f"       →  {task[:70]}")
        print()

    # Reminders and alerts
    try:
        from ai_assist.scheduler import get_due_reminders, get_upcoming_reminders, get_unseen_alerts, get_active_watches, LOCAL_TZ
        local_tz = LOCAL_TZ
        now_tz = datetime.now(local_tz)

        # Unseen alerts from daemon
        unseen = get_unseen_alerts()
        if unseen:
            print(f"  🔔 Alerts      {len(unseen)} from background watcher:")
            for a in unseen[:4]:
                print(f"       📢 {a['source_item_id'][:8]}  {a['message'][:58]}")
            if len(unseen) > 4:
                print(f"       … and {len(unseen) - 4} more")
            print()

        # Due reminders
        due = get_due_reminders(now_tz)
        if due:
            print(f"  ⏰ Reminders   {len(due)} due NOW:")
            for r in due[:4]:
                print(f"       🔴 {r['id'][:8]}  {r['message'][:60]}")
            if len(due) > 4:
                print(f"       … and {len(due) - 4} more")
            print()

        # Upcoming reminders
        upcoming = get_upcoming_reminders(now_tz, hours=12)
        if upcoming:
            print(f"  ◈  Upcoming    {len(upcoming)} in next 12h:")
            for r in upcoming[:3]:
                rd = datetime.fromisoformat(r["due_at"])
                time_str = rd.strftime("%I:%M %p")
                print(f"       ⏳ {time_str}  {r['message'][:58]}")
            print()

        # Active watches
        active_watches = get_active_watches()
        if active_watches:
            print(f"  ◈  Watches     {len(active_watches)} active:")
            for w in active_watches[:4]:
                last = w.get("last_checked_at")
                last_str = datetime.fromisoformat(last).strftime("%H:%M") if last else "never"
                print(f"       👁 {w['id'][:8]}  {w['message'][:48]}  (last: {last_str})")
            print()
    except Exception:
        pass  # scheduler module not available — skip silently

    # Active watches (legacy cron-schedules.md — fallback only)
    if 'active_watches' not in dir() or not active_watches:
        watches = _cron_watches()
        if watches:
            print("  ◈  Watches")
            for w in watches:
                print(f"       ◦  {w[:66]}")
            print()

    # Perspective + sign-off
    print(f"  ◈  {_perspective()}")
    print()
    if all_ok:
        print("  Bridge clear. What are we getting done?")
    else:
        print("  Bridge partially clear — repair flagged items above.")
    print()
    print("─" * W)
    print()


def run_status() -> None:
    """Entry point for assist status subcommand."""
    main()
