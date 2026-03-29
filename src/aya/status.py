"""Ship Mind status ritual — aya readiness check."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.rule import Rule

from aya import paths as _paths
from aya.scheduler import (
    LOCAL_TZ,
    get_active_watches,
    get_due_reminders,
    get_unseen_alerts,
    get_upcoming_reminders,
    load_items,
)

# ── aya data paths (from ~/.aya) ────────────────────────────────────────────
PROFILE = _paths.PROFILE_PATH
CONFIG = _paths.CONFIG_PATH


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
        (range(6, 9), "Coffee consumed? Let's make the day count."),
        (
            range(9, 12),
            "Morning focus window. Best cognition of the day — use it before the meetings eat it.",
        ),
        (range(12, 14), "Post-lunch territory. Carbs are the enemy of momentum. Push through."),
        (range(14, 17), "Afternoon. Attention debt accumulates here. One thing at a time."),
        (range(17, 19), "End-of-day push. Close the loop on something before you log off."),
        (range(19, 22), "Late session. Diminishing returns are real. Mind the clock."),
    ]
    for rng, flavor in table:
        if hour in rng:
            return flavor
    return "Unconventional hours. The ship is watching regardless."


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


def _active_scheduler_items() -> list[dict[str, Any]]:
    """Return all active scheduler items (watches, recurring, reminders)."""
    return [i for i in load_items() if i.get("status") == "active"]


def main(console: Console | None = None) -> None:
    console = console or Console()
    now_local = datetime.now(LOCAL_TZ)

    # Profile
    profile = _read_json(PROFILE)
    ship = profile.get("ship_mind_name", "GSV Unknown Vessel") if profile else "GSV Unknown Vessel"
    user = profile.get("user_name", "Shawn") if profile else "Shawn"
    next_eval = profile.get("name_next_reevaluation_at", "unknown") if profile else "unknown"

    # System checks — aya data only
    checks: list[CheckResult] = [
        CheckResult("profile", profile is not None, str(PROFILE)),
        CheckResult("workflow config", _read_json(CONFIG) is not None, str(CONFIG)),
        CheckResult(
            name="scheduler",
            ok=_paths.SCHEDULER_FILE.exists(),
            detail=str(_paths.SCHEDULER_FILE),
        ),
    ]
    ok = sum(1 for c in checks if c.ok)
    total = len(checks)
    all_ok = ok == total

    # ── Output ──────────────────────────────────────────────────────────────

    # Greeting
    console.print()
    console.print(f"[bold]{_greeting(now_local, user, ship)}[/bold]")
    console.print(f"[dim]{_time_flavor(now_local)}[/dim]")
    console.print()

    # Systems — compact when green, verbose on failure
    if all_ok:
        console.print(f"[green]✓[/green] Systems  [dim]{ok}/{total} checks passed[/dim]")
    else:
        console.print(f"[yellow]⚠[/yellow] Systems  [yellow]{ok}/{total} checks passed[/yellow]")
        for c in checks:
            if not c.ok:
                console.print(f"  [red]✗[/red] {c.name}  [dim]{c.detail}[/dim]")

    if isinstance(next_eval, str) and len(next_eval) >= 10:
        try:
            eval_dt = datetime.fromisoformat(next_eval.replace("Z", "+00:00"))
            days_until = (eval_dt.date() - now_local.date()).days
            if days_until <= 1:
                console.print(f"  [dim]Name re-eval due: {next_eval[:10]}[/dim]")
        except ValueError:
            pass

    console.print()

    # Reminders and alerts
    try:
        now_tz = datetime.now(LOCAL_TZ)

        # Unseen alerts from daemon
        unseen = get_unseen_alerts()
        if unseen:
            console.print(f"[bold red]🔔 {len(unseen)} alert(s):[/bold red]")
            for a in unseen[:4]:
                console.print(f"  📢 {a['source_item_id'][:8]}  {a['message'][:60]}")
            if len(unseen) > 4:
                console.print(f"  [dim]… and {len(unseen) - 4} more[/dim]")
            console.print()

        # Due reminders
        due = get_due_reminders(now_tz)
        if due:
            console.print(f"[bold yellow]⏰ {len(due)} reminder(s) due:[/bold yellow]")
            for r in due[:4]:
                due_dt = datetime.fromisoformat(r["due_at"])
                console.print(
                    f"  🔴 {r['id'][:8]}  {due_dt.strftime('%I:%M %p')}  {r['message'][:55]}"
                )
            if len(due) > 4:
                console.print(f"  [dim]… and {len(due) - 4} more[/dim]")
            console.print()

        # Upcoming reminders
        upcoming = get_upcoming_reminders(now_tz, hours=12)
        if upcoming:
            console.print("[bold]Upcoming (12h):[/bold]")
            for r in upcoming[:3]:
                rd = datetime.fromisoformat(r["due_at"])
                console.print(f"  ⏳ {rd.strftime('%I:%M %p')}  {r['message'][:55]}")
            console.print()

        # Active watches
        active_watches = get_active_watches()
        if active_watches:
            console.print(f"[bold]Watches ({len(active_watches)} active):[/bold]")
            for w in active_watches[:4]:
                last = w.get("last_checked_at")
                last_str = datetime.fromisoformat(last).strftime("%H:%M") if last else "never"
                console.print(
                    f"  👁  {w['id'][:8]}  {w['message'][:50]}  [dim]checked {last_str}[/dim]"
                )
            console.print()

    except Exception:
        pass  # scheduler runtime error — skip silently

    # Perspective + sign-off
    console.print(Rule(style="dim"))
    console.print(f"[dim italic]{_perspective()}[/dim italic]")
    if not all_ok:
        console.print(f"[yellow]⚠ {total - ok} check(s) degraded — verify paths above.[/yellow]")
    console.print()


def run_status() -> None:
    """Entry point for aya status subcommand."""
    main()
