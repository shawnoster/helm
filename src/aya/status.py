"""Ship Mind status ritual — aya readiness check."""

from __future__ import annotations

import json
import logging
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


# ── display limits ────────────────────────────────────────────────────────────

ALERT_DISPLAY_LIMIT = 4
DUE_DISPLAY_LIMIT = 4
UPCOMING_DISPLAY_LIMIT = 3
WATCH_DISPLAY_LIMIT = 4
ID_PREVIEW_LENGTH = 8

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


def _parse_next_eval(next_eval: Any, now_local: datetime) -> tuple[str, int] | None:
    """Parse next_eval ISO string and return (date_str, days_until) if due soon, else None."""
    if not isinstance(next_eval, str) or len(next_eval) < 10:
        return None
    try:
        eval_dt = datetime.fromisoformat(next_eval.replace("Z", "+00:00"))
        days_until = (eval_dt.date() - now_local.date()).days
        if days_until <= 1:
            return (next_eval[:10], days_until)
    except ValueError:
        pass
    return None


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

    # Pre-compute check totals once, reuse in all render functions
    checks_ok = sum(1 for c in checks if c.ok)
    checks_total = len(checks)

    unseen: list[dict[str, Any]] = []
    due: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    active_watches: list[dict[str, Any]] = []
    degraded = False
    try:
        unseen = get_unseen_alerts()
        due = get_due_reminders(now_local)
        upcoming = get_upcoming_reminders(now_local, hours=12)
        active_watches = get_active_watches()
    except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError) as e:
        # Log scheduler fetch failures but don't crash — show degraded state
        logging.warning("Failed to load scheduler status: %s", e)
        degraded = True

    return {
        "now_local": now_local,
        "ship": ship,
        "user": user,
        "next_eval": next_eval,
        "checks": checks,
        "checks_ok": checks_ok,
        "checks_total": checks_total,
        "unseen": unseen,
        "due": due,
        "upcoming": upcoming,
        "active_watches": active_watches,
        "degraded": degraded,
    }


def _render_plain(data: dict[str, Any]) -> str:
    """Compact plain-text status — no Rich markup, minimal lines."""
    ok = data["checks_ok"]
    total = data["checks_total"]
    checks = data["checks"]

    lines: list[str] = []
    lines.append(_greeting(data["now_local"], data["user"], data["ship"]))
    lines.append(_time_flavor(data["now_local"]))

    if ok == total:
        lines.append(f"Systems {ok}/{total} OK")
    else:
        failed = [c for c in checks if not c.ok]
        lines.append(f"Systems {ok}/{total} — failed: {', '.join(c.name for c in failed)}")

    for a in data["unseen"][:ALERT_DISPLAY_LIMIT]:
        lines.append(f"  alert: {a['source_item_id'][:ID_PREVIEW_LENGTH]}  {a['message'][:60]}")

    for r in data["due"][:DUE_DISPLAY_LIMIT]:
        due_dt = datetime.fromisoformat(r["due_at"])
        msg = r["message"][:55]
        lines.append(f"  due: {r['id'][:ID_PREVIEW_LENGTH]}  {due_dt.strftime('%I:%M %p')}  {msg}")

    for r in data["upcoming"][:UPCOMING_DISPLAY_LIMIT]:
        rd = datetime.fromisoformat(r["due_at"])
        lines.append(f"  upcoming: {rd.strftime('%I:%M %p')}  {r['message'][:55]}")

    for w in data["active_watches"][:WATCH_DISPLAY_LIMIT]:
        lines.append(f"  watch: {w['id'][:ID_PREVIEW_LENGTH]}  {w['message'][:50]}")

    next_eval_result = _parse_next_eval(data["next_eval"], data["now_local"])
    if next_eval_result:
        date_str, _ = next_eval_result
        lines.append(f"  Name re-eval due: {date_str}")

    lines.append(_perspective())
    return "\n".join(lines)


def _render_json(data: dict[str, Any]) -> str:
    """Machine-readable JSON status."""
    ok = data["checks_ok"]
    total = data["checks_total"]
    checks = data["checks"]

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
                "id": a.get("id", "")[:ID_PREVIEW_LENGTH],
                "source_item_id": a["source_item_id"][:ID_PREVIEW_LENGTH],
                "message": a["message"],
            }
            for a in data["unseen"]
        ],
        "due": [
            {"id": r["id"][:ID_PREVIEW_LENGTH], "due_at": r["due_at"], "message": r["message"]}
            for r in data["due"]
        ],
        "upcoming": [{"due_at": r["due_at"], "message": r["message"]} for r in data["upcoming"]],
        "watches": [
            {"id": w["id"][:ID_PREVIEW_LENGTH], "message": w["message"]}
            for w in data["active_watches"]
        ],
        "next_eval": data["next_eval"],
        "perspective": _perspective(),
    }
    return json.dumps(payload, indent=2, default=str)


def _render_rich(data: dict[str, Any], console: Console) -> None:
    """Full Rich-formatted status for interactive terminal use."""
    now_local = data["now_local"]
    checks = data["checks"]
    ok = data["checks_ok"]
    total = data["checks_total"]
    all_ok = ok == total

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

    next_eval_result = _parse_next_eval(data["next_eval"], now_local)
    if next_eval_result:
        date_str, _ = next_eval_result
        console.print(f"  [dim]Name re-eval due: {date_str}[/dim]")

    console.print()

    unseen = data["unseen"]
    if unseen:
        console.print(f"[bold red]🔔 {len(unseen)} alert(s):[/bold red]")
        for a in unseen[:ALERT_DISPLAY_LIMIT]:
            console.print(f"  📢 {a['source_item_id'][:ID_PREVIEW_LENGTH]}  {a['message'][:60]}")
        if len(unseen) > ALERT_DISPLAY_LIMIT:
            console.print(f"  [dim]… and {len(unseen) - ALERT_DISPLAY_LIMIT} more[/dim]")
        console.print()

    due = data["due"]
    if due:
        console.print(f"[bold yellow]⏰ {len(due)} reminder(s) due:[/bold yellow]")
        for r in due[:DUE_DISPLAY_LIMIT]:
            due_dt = datetime.fromisoformat(r["due_at"])
            msg = r["message"][:55]
            console.print(
                f"  🔴 {r['id'][:ID_PREVIEW_LENGTH]}  {due_dt.strftime('%I:%M %p')}  {msg}"
            )
        if len(due) > DUE_DISPLAY_LIMIT:
            console.print(f"  [dim]… and {len(due) - DUE_DISPLAY_LIMIT} more[/dim]")
        console.print()

    upcoming = data["upcoming"]
    if upcoming:
        console.print("[bold]Upcoming (12h):[/bold]")
        for r in upcoming[:UPCOMING_DISPLAY_LIMIT]:
            rd = datetime.fromisoformat(r["due_at"])
            console.print(f"  ⏳ {rd.strftime('%I:%M %p')}  {r['message'][:55]}")
        console.print()

    active_watches = data["active_watches"]
    if active_watches:
        console.print(f"[bold]Watches ({len(active_watches)} active):[/bold]")
        for w in active_watches[:WATCH_DISPLAY_LIMIT]:
            last = w.get("last_checked_at")
            last_str = datetime.fromisoformat(last).strftime("%H:%M") if last else "never"
            msg = w["message"][:50]
            console.print(
                f"  👁  {w['id'][:ID_PREVIEW_LENGTH]}  {msg}  [dim]checked {last_str}[/dim]"
            )
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
