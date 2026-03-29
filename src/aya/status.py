"""Ship Mind status ritual — aya readiness check."""

from __future__ import annotations

import json
import sys
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


def _gather_status() -> dict[str, Any]:
    """Collect all status data into a plain dict."""
    now_local = datetime.now(LOCAL_TZ)

    profile = _read_json(PROFILE)
    ship = profile.get("ship_mind_name", "GSV Unknown Vessel") if profile else "GSV Unknown Vessel"
    user = profile.get("user_name", "Shawn") if profile else "Shawn"
    next_eval = profile.get("name_next_reevaluation_at", "unknown") if profile else "unknown"

    checks: list[CheckResult] = [
        CheckResult("profile", profile is not None, str(PROFILE)),
        CheckResult(
            name="scheduler",
            ok=_paths.SCHEDULER_FILE.exists(),
            detail=str(_paths.SCHEDULER_FILE),
        ),
    ]

    unseen: list[dict[str, Any]] = []
    due: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    active_watches: list[dict[str, Any]] = []
    try:
        unseen = get_unseen_alerts()
        due = get_due_reminders(now_local)
        upcoming = get_upcoming_reminders(now_local, hours=12)
        active_watches = get_active_watches()
    except Exception:
        pass

    return {
        "now_local": now_local,
        "ship": ship,
        "user": user,
        "next_eval": next_eval,
        "checks": checks,
        "unseen": unseen,
        "due": due,
        "upcoming": upcoming,
        "active_watches": active_watches,
    }


def _render_plain(data: dict[str, Any]) -> str:
    """Compact plain-text status — no Rich markup, minimal lines."""
    checks = data["checks"]
    ok = sum(1 for c in checks if c.ok)
    total = len(checks)

    lines: list[str] = []
    lines.append(_greeting(data["now_local"], data["user"], data["ship"]))
    lines.append(_time_flavor(data["now_local"]))

    if ok == total:
        lines.append(f"Systems {ok}/{total} OK")
    else:
        failed = [c for c in checks if not c.ok]
        lines.append(f"Systems {ok}/{total} — failed: {', '.join(c.name for c in failed)}")

    for a in data["unseen"][:4]:
        lines.append(f"  alert: {a['source_item_id'][:8]}  {a['message'][:60]}")

    for r in data["due"][:4]:
        due_dt = datetime.fromisoformat(r["due_at"])
        lines.append(f"  due: {r['id'][:8]}  {due_dt.strftime('%I:%M %p')}  {r['message'][:55]}")

    for r in data["upcoming"][:3]:
        rd = datetime.fromisoformat(r["due_at"])
        lines.append(f"  upcoming: {rd.strftime('%I:%M %p')}  {r['message'][:55]}")

    for w in data["active_watches"][:4]:
        lines.append(f"  watch: {w['id'][:8]}  {w['message'][:50]}")

    next_eval = data["next_eval"]
    if isinstance(next_eval, str) and len(next_eval) >= 10:
        try:
            eval_dt = datetime.fromisoformat(next_eval.replace("Z", "+00:00"))
            days_until = (eval_dt.date() - data["now_local"].date()).days
            if days_until <= 1:
                lines.append(f"  Name re-eval due: {next_eval[:10]}")
        except ValueError:
            pass

    lines.append(_perspective())
    return "\n".join(lines)


def _render_json(data: dict[str, Any]) -> str:
    """Machine-readable JSON status."""
    checks = data["checks"]
    ok = sum(1 for c in checks if c.ok)
    total = len(checks)

    payload: dict[str, Any] = {
        "greeting": _greeting(data["now_local"], data["user"], data["ship"]),
        "time_flavor": _time_flavor(data["now_local"]),
        "systems": {
            "ok": ok == total,
            "passed": ok,
            "total": total,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        },
        "alerts": [
            {
                "id": a.get("id", "")[:8],
                "source_item_id": a["source_item_id"][:8],
                "message": a["message"],
            }
            for a in data["unseen"]
        ],
        "due": [
            {"id": r["id"][:8], "due_at": r["due_at"], "message": r["message"]} for r in data["due"]
        ],
        "upcoming": [{"due_at": r["due_at"], "message": r["message"]} for r in data["upcoming"]],
        "watches": [{"id": w["id"][:8], "message": w["message"]} for w in data["active_watches"]],
        "next_eval": data["next_eval"],
        "perspective": _perspective(),
    }
    return json.dumps(payload, indent=2, default=str)


def _render_rich(data: dict[str, Any], console: Console) -> None:
    """Full Rich-formatted status for interactive terminal use."""
    now_local = data["now_local"]
    checks = data["checks"]
    ok = sum(1 for c in checks if c.ok)
    total = len(checks)
    all_ok = ok == total
    next_eval = data["next_eval"]

    console.print()
    console.print(f"[bold]{_greeting(now_local, data['user'], data['ship'])}[/bold]")
    console.print(f"[dim]{_time_flavor(now_local)}[/dim]")
    console.print()

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

    unseen = data["unseen"]
    if unseen:
        console.print(f"[bold red]🔔 {len(unseen)} alert(s):[/bold red]")
        for a in unseen[:4]:
            console.print(f"  📢 {a['source_item_id'][:8]}  {a['message'][:60]}")
        if len(unseen) > 4:
            console.print(f"  [dim]… and {len(unseen) - 4} more[/dim]")
        console.print()

    due = data["due"]
    if due:
        console.print(f"[bold yellow]⏰ {len(due)} reminder(s) due:[/bold yellow]")
        for r in due[:4]:
            due_dt = datetime.fromisoformat(r["due_at"])
            console.print(f"  🔴 {r['id'][:8]}  {due_dt.strftime('%I:%M %p')}  {r['message'][:55]}")
        if len(due) > 4:
            console.print(f"  [dim]… and {len(due) - 4} more[/dim]")
        console.print()

    upcoming = data["upcoming"]
    if upcoming:
        console.print("[bold]Upcoming (12h):[/bold]")
        for r in upcoming[:3]:
            rd = datetime.fromisoformat(r["due_at"])
            console.print(f"  ⏳ {rd.strftime('%I:%M %p')}  {r['message'][:55]}")
        console.print()

    active_watches = data["active_watches"]
    if active_watches:
        console.print(f"[bold]Watches ({len(active_watches)} active):[/bold]")
        for w in active_watches[:4]:
            last = w.get("last_checked_at")
            last_str = datetime.fromisoformat(last).strftime("%H:%M") if last else "never"
            console.print(f"  👁  {w['id'][:8]}  {w['message'][:50]}  [dim]checked {last_str}[/dim]")
        console.print()

    console.print(Rule(style="dim"))
    console.print(f"[dim italic]{_perspective()}[/dim italic]")
    if not all_ok:
        console.print(f"[yellow]⚠ {total - ok} check(s) degraded — verify paths above.[/yellow]")
    console.print()


def run_status(format_: str = "text") -> None:
    """Entry point for aya status subcommand."""
    data = _gather_status()
    if format_ == "json":
        print(_render_json(data))  # noqa: T201 — raw stdout for JSON
    elif format_ == "rich":
        _render_rich(data, Console())
    elif format_ == "text":
        print(_render_plain(data))  # noqa: T201 — raw stdout for plain text
    else:
        sys.stderr.write(
            f"aya status: unknown format '{format_}'. Expected one of: text, json, rich.\n"
        )
        raise SystemExit(2)
