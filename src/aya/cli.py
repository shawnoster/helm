"""CLI entry point — aya command."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aya import __version__
from aya.ci import watch_pr_checks
from aya.identity import Identity, Profile, TrustedKey
from aya.install import install_scheduler, uninstall_scheduler
from aya.packet import ConflictStrategy, ContentType, Packet, human_age
from aya.pair import (
    PairingError,
    generate_code,
    hash_code,
    join_pairing,
    poll_for_pair_response,
    publish_pair_request,
)
from aya.paths import PROFILE_PATH
from aya.profile import ensure_profile
from aya.relay import RelayClient

# Subcommand modules — imported at top-level; each is only invoked when its
# subcommand is actually called, so startup cost is acceptable.
from aya.scheduler import (
    _display_items,
    add_recurring,
    add_reminder,
    add_seed_alert,
    add_watch,
    check_due,
    dismiss_alert,
    dismiss_item,
    format_pending,
    format_scheduler_status,
    get_pending,
    get_scheduler_status,
    get_session_crons,
    is_idle,
    list_items,
    parse_due,
    record_activity,
    run_poll,
    run_tick,
    show_alerts,
    snooze_item,
)
from aya.status import run_status


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


app = typer.Typer(
    name="aya",
    help="Personal AI assistant toolkit — sync, schedule, identity.",
    no_args_is_help=True,
)

# ── Schedule sub-app ─────────────────────────────────────────────────────────

schedule_app = typer.Typer(
    name="schedule",
    help="Reminders, watches, and recurring jobs.",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule")

# ── Hook sub-app ─────────────────────────────────────────────────────────────

hook_app = typer.Typer(
    name="hook",
    help="Claude Code hook integrations.",
    no_args_is_help=True,
)
app.add_typer(hook_app, name="hook")

# ── CI sub-app ────────────────────────────────────────────────────────────────

ci_app = typer.Typer(
    name="ci",
    help="CI integration — watch checks, report failures.",
    no_args_is_help=True,
)
app.add_typer(ci_app, name="ci")

console = Console()
err = Console(stderr=True)


DEFAULT_PROFILE = PROFILE_PATH


def _load_profile(profile_path: Path) -> Profile:
    if not profile_path.exists():
        err.print(
            f"[red]Profile not found at {profile_path}.[/red]\nRun [bold]aya init[/bold] first."
        )
        raise typer.Exit(1)
    return Profile.load(profile_path)


def _resolve_instance(p: Profile, instance: str, *, quiet: bool = False) -> Identity:
    """Return the local Identity for *instance*, with a smart single-instance fallback.

    Resolution order:
    1. Exact match on *instance* name — returned immediately.
    2. If exactly one instance is registered, that instance is used automatically
       regardless of the requested name (smart default for fresh ``aya init`` users).
    3. Otherwise a descriptive error is printed (unless *quiet* is True) and
       ``typer.Exit(1)`` is raised.

    The *quiet* flag suppresses error output; it is intended for background hooks
    (e.g. ``aya receive --quiet``) where silent failure is preferable to log noise.
    """
    local = p.instances.get(instance)
    if local is not None:
        return local

    available = list(p.instances.keys())

    # Smart default: exactly one instance — use it without fuss.
    if len(available) == 1:
        return next(iter(p.instances.values()))

    if not quiet:
        if available:
            names = ", ".join(available)
            err.print(
                f"[red]Instance '{instance}' not found.[/red] "
                f"Available instances: [cyan]{names}[/cyan].\n"
                f"Use [bold]--instance {available[0]}[/bold] "
                f"or run: [bold]aya init --label {instance}[/bold]"
            )
        else:
            err.print(
                f"[red]Instance '{instance}' not found.[/red] Run [bold]aya init[/bold] first."
            )
    raise typer.Exit(1)


# ── version ──────────────────────────────────────────────────────────────────


@app.command()
def version(
    format_: OutputFormat = typer.Option(
        OutputFormat.TEXT, "--format", "-f", help="Output format: text or json"
    ),
) -> None:
    """Show the installed aya version."""
    if format_ == OutputFormat.JSON:
        console.out(json.dumps({"version": __version__}))
    else:
        console.print(f"aya {__version__}")


# ── init ─────────────────────────────────────────────────────────────────────


@app.command()
def init(
    label: str = typer.Option("default", help="Label for this instance (work, home, laptop…)"),
    profile: Path = typer.Option(DEFAULT_PROFILE, help="Path to profile.json"),
    relay: str | None = typer.Option(
        None, help="Override the default relay URL (omit to use the built-in two-relay default)"
    ),
) -> None:
    """Generate a keypair for this instance and register it in your profile."""
    identity = Identity.generate(label)

    if profile.exists():
        p = Profile.load(profile)
    else:
        profile.parent.mkdir(parents=True, exist_ok=True)
        p = Profile(alias="Ace", ship_mind_name="", user_name="")

    p.instances[label] = identity
    if relay is not None:
        p.default_relay = relay  # sets default_relays = [relay]
    p.save(profile)

    relay_display = relay or ", ".join(p.default_relays)
    console.print(
        Panel.fit(
            f"[bold green]✓ Instance created[/bold green]\n\n"
            f"Label:  [cyan]{label}[/cyan]\n"
            f"DID:    [dim]{identity.did}[/dim]\n"
            f"Relay:  [cyan]{relay_display}[/cyan]\n\n"
            "[dim]Share your DID with other instances you want to trust.[/dim]",
            title="aya — init",
        )
    )


# ── trust ─────────────────────────────────────────────────────────────────────


@app.command()
def trust(
    did: str = typer.Argument(help="DID to trust (did:key:z6Mk…)"),
    label: str = typer.Option(..., help="Human label for this key (home, friend:alice)"),
    nostr_pubkey: str = typer.Option(
        None,
        help="Nostr pubkey hex (required for send/receive; pairing fills this automatically)",
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Add a DID to your trusted keys list."""
    p = _load_profile(profile)
    p.trusted_keys[label] = TrustedKey(
        did=did,
        label=label,
        nostr_pubkey=nostr_pubkey,
    )
    p.save(profile)
    console.print(f"[green]✓[/green] Trusted: [cyan]{label}[/cyan]  [dim]{did}[/dim]")
    if not nostr_pubkey:
        console.print(
            "[dim]Note: No Nostr pubkey provided. "
            "Use --nostr-pubkey or pair to enable relay delivery.[/dim]"
        )


# ── pack ──────────────────────────────────────────────────────────────────────


@app.command()
def pack(
    to: str = typer.Option(..., help="Recipient label (home) or DID"),
    intent: str = typer.Option(..., help="What is this packet and why"),
    files: list[Path] = typer.Option([], help="Files to include"),
    context: str = typer.Option(None, help="Annotation for the receiving assistant"),
    seed: bool = typer.Option(False, help="Create a conversation seed instead of content"),
    opener: str = typer.Option(None, help="[seed] Opening question for the receiving assistant"),
    out: Path = typer.Option(None, help="Write packet JSON to file (default: stdout)"),
    instance: str = typer.Option("default", help="Local instance name (identity to act as)"),
    conflict: ConflictStrategy = typer.Option(
        ConflictStrategy.LAST_WRITE_WINS, help="Conflict resolution strategy"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Pack a knowledge packet ready to send."""
    p = _load_profile(profile)
    local = _resolve_instance(p, instance)

    # Resolve recipient DID
    to_did = _resolve_did(to, p)

    if seed:
        if not opener:
            err.print("[red]--opener required for seed packets.[/red]")
            raise typer.Exit(1)
        packet = Packet.as_seed(
            from_did=local.did,
            to_did=to_did,
            intent=intent,
            opener=opener,
            context_summary=context or "",
        )
    elif files:
        packet = Packet.from_files(
            paths=[str(f) for f in files],
            from_did=local.did,
            to_did=to_did,
            intent=intent,
            context=context,
        )
    else:
        content = sys.stdin.read()
        packet = Packet(
            **{"from": local.did, "to": to_did},
            intent=intent,
            context=context,
            content_type=ContentType.MARKDOWN,
            content=content,
            conflict_strategy=conflict,
        )

    signed = packet.sign(local)
    json_output = signed.to_json()

    if out:
        out.write_text(json_output)
        console.print(f"[green]✓[/green] Packet written to [cyan]{out}[/cyan]")
    else:
        sys.stdout.write(json_output)


# ── send ──────────────────────────────────────────────────────────────────────


@app.command()
def send(
    packet_file: Path = typer.Argument(help="Packet JSON file to send"),
    relay: str = typer.Option(None, help="Relay URL (overrides profile default)"),
    instance: str = typer.Option("default", help="Local instance name (identity to act as)"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Send a packet to a Nostr relay."""
    p = _load_profile(profile)
    local = _resolve_instance(p, instance)

    relay_urls = [relay] if relay else p.default_relays
    packet = Packet.from_json(packet_file.read_text())
    client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

    # Resolve recipient's Nostr pubkey
    recipient_nostr_pub = _resolve_nostr_pubkey(packet.to_did, p)
    event_id = asyncio.run(client.publish(packet, recipient_nostr_pub))
    relay_count = len(relay_urls)
    relay_display = relay_urls[0] if relay_count == 1 else f"{relay_urls[0]} (+{relay_count - 1})"
    console.print(
        f"[green]✓[/green] Sent [cyan]{packet.intent}[/cyan]\n"
        f"  Packet: [dim]{packet.id[:8]}[/dim]  "
        f"Event: [dim]{event_id[:8]}[/dim]  "
        f"Relay: [dim]{relay_display}[/dim]"
    )


# ── dispatch ──────────────────────────────────────────────────────────────────


@app.command()
def dispatch(
    to: str = typer.Option(..., help="Recipient label (home) or DID"),
    intent: str = typer.Option(..., help="What is this packet and why"),
    files: list[Path] = typer.Option([], help="Files to include"),
    context: str = typer.Option(None, help="Annotation for the receiving assistant"),
    seed: bool = typer.Option(False, help="Create a conversation seed instead of content"),
    opener: str = typer.Option(None, help="[seed] Opening question for the receiving assistant"),
    instance: str = typer.Option("default", help="Local instance name (identity to act as)"),
    relay: str = typer.Option(None, help="Relay URL (overrides profile default)"),
    conflict: ConflictStrategy = typer.Option(
        ConflictStrategy.LAST_WRITE_WINS, help="Conflict resolution strategy"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Pack and send in one step — the natural 'pack for home' flow."""

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, instance)

        to_did = _resolve_did(to, p)

        if seed:
            if not opener:
                err.print("[red]--opener required for seed packets.[/red]")
                raise typer.Exit(1)
            packet = Packet.as_seed(
                from_did=local.did,
                to_did=to_did,
                intent=intent,
                opener=opener,
                context_summary=context or "",
            )
        elif files:
            packet = Packet.from_files(
                paths=[str(f) for f in files],
                from_did=local.did,
                to_did=to_did,
                intent=intent,
                context=context,
            )
        else:
            content = sys.stdin.read()
            packet = Packet(
                **{"from": local.did, "to": to_did},
                intent=intent,
                context=context,
                content_type=ContentType.MARKDOWN,
                content=content,
                conflict_strategy=conflict,
            )

        signed = packet.sign(local)

        relay_urls = [relay] if relay else p.default_relays
        recipient_nostr_pub = _resolve_nostr_pubkey(signed.to_did, p)
        if recipient_nostr_pub is None:
            err.print(
                "[red]No Nostr pubkey found for recipient.[/red]\n"
                "Add one with [bold]aya trust --nostr-pubkey ...[/bold] "
                "or establish pairing with [bold]aya pair[/bold]."
            )
            raise typer.Exit(1)

        client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)
        try:
            event_id = await client.publish(signed, recipient_nostr_pub)
        except Exception:
            err.print("[yellow]Could not reach relay — dispatch failed.[/yellow]")
            raise typer.Exit(1) from None

        relay_count = len(relay_urls)
        relay_display = (
            relay_urls[0] if relay_count == 1 else f"{relay_urls[0]} (+{relay_count - 1})"
        )
        console.print(
            Panel.fit(
                f"[bold green]✓ Dispatched[/bold green]\n\n"
                f"Intent:  [cyan]{signed.intent}[/cyan]\n"
                f"Packet:  [dim]{signed.id[:8]}[/dim]\n"
                f"Event:   [dim]{event_id[:8]}[/dim]\n"
                f"Relay:   [dim]{relay_display}[/dim]\n"
                f"To:      [dim]{to}[/dim]",
                title="aya — dispatch",
            )
        )

    asyncio.run(_run())


# ── receive ───────────────────────────────────────────────────────────────────


@app.command()
def receive(
    relay: str = typer.Option(None),
    instance: str = typer.Option("default", help="Local instance name (identity to act as)"),
    auto_ingest: bool = typer.Option(False, help="Ingest all trusted packets without prompting"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-confirm all prompts (non-interactive mode)"
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress output when inbox is empty (for startup hooks)"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Poll for pending packets and surface them for review."""

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, instance, quiet=quiet)

        relay_urls = [relay] if relay else p.default_relays
        client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

        # Fetch pending packets for this instance (subject to fetch_pending()'s
        # limit); ingested_ids is the authoritative dedup mechanism and filters
        # already-seen packets below.
        packets: list[Packet] = []
        try:
            async for packet in client.fetch_pending():
                packets.append(packet)
        except Exception:
            if not quiet:
                err.print("[yellow]Could not reach relay — skipping relay fetch.[/yellow]")
            return

        if not packets:
            if not quiet:
                console.print("[dim]No pending packets.[/dim]")
            return

        # Verify signatures — reject tampered or unsigned packets
        verified: list[Packet] = []
        ingested_set = set(p.ingested_ids)
        for packet in packets:
            if packet.id in ingested_set:
                continue  # already ingested — skip silently
            if packet.verify_from_did():
                verified.append(packet)
            else:
                if not quiet:
                    err.print(
                        f"[red]⚠ Packet {packet.id[:8]} failed signature verification "
                        f"(from {packet.from_did[:30]}…) — discarded[/red]"
                    )

        if not verified:
            if not quiet:
                console.print("[dim]No valid packets.[/dim]")
            return

        _show_inbox(verified, p)

        for packet in verified:
            trusted = p.is_trusted(packet.from_did)
            trust_label = "[green]trusted[/green]" if trusted else "[yellow]unknown sender[/yellow]"

            if auto_ingest and trusted:
                _ingest(packet)
                p.ingested_ids.append(packet.id)
                continue

            ingest = yes or typer.confirm(
                f"\nIngest '{packet.intent}' ({trust_label})?",
                default=trusted,
            )
            if ingest:
                _ingest(packet)
                p.ingested_ids.append(packet.id)
                sender_nostr_pub = _resolve_nostr_pubkey(packet.from_did, p)
                if sender_nostr_pub:
                    await client.send_receipt(packet, sender_nostr_pub)

        # Persist updated ingested_ids.
        p.save(profile)

    asyncio.run(_run())


# ── inbox ─────────────────────────────────────────────────────────────────────


@app.command()
def inbox(
    relay: str = typer.Option(None),
    instance: str = typer.Option("default", help="Local instance name (identity to act as)"),
    format_: OutputFormat = typer.Option(
        OutputFormat.TEXT, "--format", "-f", help="Output format: text or json"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """List pending packets without ingesting."""

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, instance)

        relay_urls = [relay] if relay else p.default_relays
        client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

        packets = [pkt async for pkt in client.fetch_pending()]
        if format_ == OutputFormat.JSON:
            console.out(
                json.dumps(
                    [_packet_to_dict(pkt, p) for pkt in packets],
                    indent=2,
                    default=str,
                )
            )
        elif not packets:
            console.print("[dim]Inbox empty.[/dim]")
        else:
            _show_inbox(packets, p)

    asyncio.run(_run())


# ── pair ──────────────────────────────────────────────────────────────────────


@app.command()
def pair(
    code: str = typer.Option(None, help="Pairing code from the other instance (joiner mode)"),
    label: str = typer.Option(..., help="Name to assign to the remote peer being paired with"),
    instance: str = typer.Option("default", help="Local instance name (identity to act as)"),
    relay: str = typer.Option(None, help="Relay URL (overrides profile default)"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Pair two instances with a short-lived code — no manual DID exchange."""
    p = _load_profile(profile)
    local = _resolve_instance(p, instance)

    relay_urls = [relay] if relay else p.default_relays

    if code:
        # ── Joiner mode ──────────────────────────────────────────────
        try:
            trusted = asyncio.run(join_pairing(local, label, code, relay_urls))
        except PairingError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        p.trusted_keys[trusted.label] = trusted
        p.save(profile)
        console.print(
            Panel.fit(
                f"[bold green]✓ Paired![/bold green]\n\n"
                f"Trusted: [cyan]{trusted.label}[/cyan]\n"
                f"DID:     [dim]{trusted.did}[/dim]",
                title="aya — pair (joined)",
            )
        )

    else:
        # ── Initiator mode ───────────────────────────────────────────
        pairing_code = generate_code()
        code_h = hash_code(pairing_code)

        # Publish the request
        console.print("[dim]Publishing pairing request…[/dim]")
        request_event_id = asyncio.run(publish_pair_request(local, label, code_h, relay_urls))

        # Show the code — user reads this aloud or types it on the other machine
        console.print(
            Panel.fit(
                f"[bold]Pairing code:[/bold]  [bold cyan]{pairing_code}[/bold cyan]\n\n"
                "Enter this on your other machine:\n"
                f"  [dim]aya pair --code {pairing_code} --label <their-label>[/dim]\n\n"
                "[dim]Expires in 10 minutes.[/dim]",
                title="aya — pair",
            )
        )

        # Poll for response
        with console.status("[bold cyan]Waiting for the other instance…[/bold cyan]"):
            trusted = asyncio.run(
                poll_for_pair_response(relay_urls, local.nostr_public_hex, request_event_id)
            )

        if trusted is None:
            console.print(
                "[bold yellow]Pairing timed out.[/bold yellow] "
                "Run [bold]aya pair[/bold] again for a new code."
            )
            raise typer.Exit(1)

        p.trusted_keys[trusted.label] = trusted
        p.save(profile)
        console.print(
            Panel.fit(
                f"[bold green]✓ Paired![/bold green]\n\n"
                f"Trusted: [cyan]{trusted.label}[/cyan]\n"
                f"DID:     [dim]{trusted.did}[/dim]",
                title="aya — pair (complete)",
            )
        )


# ── schedule subcommands ──────────────────────────────────────────────────────


@schedule_app.command("remind")
def schedule_remind(
    message: str = typer.Option(..., "--message", "-m", help="Reminder message"),
    due: str = typer.Option(..., "--due", "-d", help="When: 'tomorrow 9am', 'in 2 hours', ISO8601"),
    tag: str = typer.Option("", "--tag", "-t", help="Comma-separated tags"),
) -> None:
    """Add a one-shot reminder."""
    item = add_reminder(message, due, tag)
    due_dt = parse_due(due)
    console.print(
        f"[green]✓[/green] Reminder {item['id'][:8]} — {due_dt.strftime('%a %b %d, %I:%M %p')}"
    )
    console.print(f"  {message}")


@schedule_app.command("watch")
def schedule_watch(
    provider: str = typer.Argument(help="Provider: github-pr, jira-query, jira-ticket"),
    target: str = typer.Argument(help="Target: owner/repo#123, JQL, or TICKET-123"),
    message: str = typer.Option(..., "--message", "-m", help="Watch description"),
    tag: str = typer.Option("", "--tag", "-t", help="Comma-separated tags"),
    condition: str = typer.Option(
        "", "--condition", "-c", help="Condition: approved_or_merged, etc."
    ),
    interval: int = typer.Option(30, "--interval", "-i", help="Poll interval minutes"),
    remove_when: str = typer.Option("", help="Auto-remove: merged_or_closed"),
) -> None:
    """Add a condition-based watch."""
    try:
        item = add_watch(provider, target, message, tag, condition, interval, remove_when)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]✓[/green] Watch {item['id'][:8]} ({provider})")
    console.print(f"  {message}")
    console.print(f"  Condition: {item['condition']}, poll every {item['poll_interval_minutes']}m")


@schedule_app.command("recurring")
def schedule_recurring(
    message: str = typer.Option(..., "--message", "-m", help="Short label for this recurring job"),
    cron: str = typer.Option(..., "--cron", "-c", help="Cron expression, e.g. '13,43 * * * *'"),
    prompt: str = typer.Option("", "--prompt", "-p", help="Prompt delivered to Claude each firing"),
    tag: str = typer.Option("", "--tag", "-t", help="Comma-separated tags"),
    idle_back_off: str = typer.Option(
        "",
        "--idle-back-off",
        help="Suppress when idle for longer than this (e.g. '30m', '1h')",
    ),
    only_during: str = typer.Option(
        "",
        "--only-during",
        help="Only fire within this time window, e.g. '08:00-18:00'",
    ),
) -> None:
    """Add a persistent recurring session job (session_required cron)."""
    try:
        item = add_recurring(message, cron, prompt, tag, idle_back_off, only_during)
    except ValueError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]✓[/green] Recurring {item['id'][:8]} — {cron}")
    console.print(f"  {message}")
    if idle_back_off:
        console.print(f"  Idle back-off: {idle_back_off}")
    if only_during:
        console.print(f"  Only during: {only_during}")


@schedule_app.command("activity")
def schedule_activity() -> None:
    """Record user activity — resets the idle back-off timer.

    Call this whenever the user is known to be active (e.g. on each new message
    or via a SessionStart / PreToolUse hook) so that idle-aware recurring crons
    are not suppressed unnecessarily.
    """
    record_activity()
    console.print("[green]✓[/green] Activity recorded.")


@schedule_app.command("is-idle")
def schedule_is_idle(
    threshold: str = typer.Option(
        "30m", "--threshold", "-t", help="Idle threshold (e.g. '30m', '1h')"
    ),
) -> None:
    """Check whether the session is currently idle.

    Exits with code 0 (active) or 1 (idle) so shell scripts can branch on it.
    """
    try:
        idle = is_idle(threshold)
    except ValueError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc
    if idle:
        console.print(f"[yellow]idle[/yellow] (threshold: {threshold})")
        raise typer.Exit(1)
    console.print(f"[green]active[/green] (threshold: {threshold})")


@schedule_app.command("list")
def schedule_list(
    all_items: bool = typer.Option(False, "--all", "-a", help="Include dismissed/delivered"),
    item_type: str = typer.Option(None, "--type", help="Filter: reminder, watch, recurring, event"),
    format_: OutputFormat = typer.Option(
        OutputFormat.TEXT, "--format", "-f", help="Output format: text or json"
    ),
) -> None:
    """List scheduled items."""
    items = list_items(show_all=all_items, item_type=item_type)
    if format_ == OutputFormat.JSON:
        console.out(json.dumps(items, indent=2, default=str))
    else:
        _display_items(items)


@schedule_app.command("check")
def schedule_check(
    format_: OutputFormat = typer.Option(
        OutputFormat.TEXT, "--format", "-f", help="Output format: text or json"
    ),
) -> None:
    """Check for due reminders and alerts."""
    due_items, unseen = check_due()

    if format_ == OutputFormat.JSON:
        console.out(json.dumps({"due": due_items, "alerts": unseen}, indent=2, default=str))
        return

    if not due_items and not unseen:
        console.print("[dim]Nothing due. No alerts.[/dim]")
        return

    if due_items:
        console.print(f"\n  [bold]⏰ {len(due_items)} reminder(s) due:[/bold]")
        for r in due_items:
            due_dt = datetime.fromisoformat(r["due_at"])
            console.print(
                f"    🔴 {r['id'][:8]}  {due_dt.strftime('%I:%M %p')}  {r['message'][:55]}"
            )

    if unseen:
        console.print(f"\n  [bold]🔔 {len(unseen)} alert(s):[/bold]")
        for a in unseen:
            console.print(f"    📢 {a['source_item_id'][:8]}  {a['message'][:60]}")


@schedule_app.command("dismiss")
def schedule_dismiss(
    item_id: str = typer.Argument(help="Item ID (prefix match ok)"),
) -> None:
    """Dismiss a scheduled item or alert."""
    try:
        item = dismiss_item(item_id)
    except ValueError:
        try:
            item = dismiss_alert(item_id)
        except ValueError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    console.print(f"[green]✓[/green] Dismissed {item['id'][:8]} — {item['message'][:60]}")


@schedule_app.command("snooze")
def schedule_snooze(
    item_id: str = typer.Argument(help="Item ID (prefix match ok)"),
    until: str = typer.Option(
        ..., "--until", "-u", help="Snooze until: 'in 1 hour', 'tomorrow 9am'"
    ),
) -> None:
    """Snooze a reminder."""
    try:
        item, snooze_until = snooze_item(item_id, until)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(
        f"💤 Snoozed {item['id'][:8]} until {snooze_until.strftime('%a %b %d, %I:%M %p')}"
    )


@schedule_app.command("poll")
def schedule_poll(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress output on no changes"),
) -> None:
    """Run one poll cycle (legacy — use 'tick' instead)."""
    run_poll(quiet=quiet)


@schedule_app.command("tick")
def schedule_tick(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress output"),
) -> None:
    """Run one scheduler tick — poll watches, check reminders, sweep stale claims.

    Canonical entry point for system cron:
        */5 * * * * aya scheduler tick --quiet
    """
    result = run_tick(quiet=quiet)
    if not quiet:
        console.print(f"[dim]Tick complete. Claims swept: {result['claims_swept']}[/dim]")


@schedule_app.command("pending")
def schedule_pending(
    format_: OutputFormat = typer.Option(
        OutputFormat.TEXT, "--format", "-f", help="Output format: text or json"
    ),
) -> None:
    """Show pending items for this session — alerts to deliver + session crons.

    SessionStart hook entry point:
        aya scheduler pending --format text
    """
    pending = get_pending()
    if format_ == OutputFormat.JSON:
        console.out(json.dumps(pending, indent=2, default=str))
    else:
        console.print(format_pending(pending))


@schedule_app.command("status")
def schedule_status(
    format_: OutputFormat = typer.Option(
        OutputFormat.TEXT, "--format", "-f", help="Output format: text or json"
    ),
) -> None:
    """Show scheduler overview — watches, reminders, crons, deliveries."""
    status = get_scheduler_status()
    if format_ == OutputFormat.JSON:
        console.out(json.dumps(status, indent=2, default=str))
    else:
        console.print(format_scheduler_status(status))


@schedule_app.command("alerts")
def schedule_alerts(
    format_: OutputFormat = typer.Option(
        OutputFormat.TEXT, "--format", "-f", help="Output format: text or json"
    ),
    mark_seen: bool = typer.Option(False, "--mark-seen", help="Mark all alerts as seen"),
) -> None:
    """Show alerts from background watcher."""
    unseen = show_alerts(mark_seen=mark_seen)

    if format_ == OutputFormat.JSON:
        console.out(json.dumps(unseen, indent=2, default=str))
        return

    if not unseen:
        console.print("[dim]No unseen alerts.[/dim]")
        return

    console.print(f"\n  [bold]🔔 {len(unseen)} alert(s):[/bold]")
    for a in unseen:
        ts = datetime.fromisoformat(a["created_at"]).strftime("%b %d %I:%M %p")
        console.print(f"    📢 {a['source_item_id'][:8]}  {ts}  {a['message'][:55]}")

    if mark_seen:
        console.print(f"\n  Marked {len(unseen)} alert(s) as seen.")


# ── install / uninstall ───────────────────────────────────────────────────────


@schedule_app.command("install")
def schedule_install(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview changes without applying"),
) -> None:
    """Install scheduler integrations — system crontab + Claude Code hooks."""
    result = install_scheduler(dry_run=dry_run)

    if result.errors:
        for e in result.errors:
            err.print(f"[red]Error:[/red] {e}")

    prefix = "[dim](dry run)[/dim] " if dry_run else ""

    if result.cron_already_present:
        console.print(f"  {prefix}[dim]Crontab:[/dim] already installed")
    elif result.cron_installed:
        console.print(f"  {prefix}[green]Crontab:[/green] installed")
        console.print(f"    [dim]{result.cron_line}[/dim]")

    for event in result.hooks_already_present:
        console.print(f"  {prefix}[dim]{event}:[/dim] already installed")
    for event in result.hooks_installed:
        console.print(f"  {prefix}[green]{event}:[/green] installed")
    for event in result.hooks_updated:
        console.print(f"  {prefix}[yellow]{event}:[/yellow] updated")

    if not dry_run and not result.errors:
        console.print("\n[green]✓[/green] Scheduler integrations installed.")
    elif result.errors:
        raise typer.Exit(1)


@schedule_app.command("uninstall")
def schedule_uninstall(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview changes without applying"),
) -> None:
    """Remove scheduler integrations — system crontab + Claude Code hooks."""
    result = uninstall_scheduler(dry_run=dry_run)

    if result.errors:
        for e in result.errors:
            err.print(f"[red]Error:[/red] {e}")

    prefix = "[dim](dry run)[/dim] " if dry_run else ""

    if result.cron_removed:
        console.print(f"  {prefix}[yellow]Crontab:[/yellow] removed")
    else:
        console.print(f"  {prefix}[dim]Crontab:[/dim] not present")

    for event in result.hooks_removed:
        console.print(f"  {prefix}[yellow]{event}:[/yellow] removed")

    if not result.hooks_removed:
        console.print(f"  {prefix}[dim]Hooks:[/dim] not present")

    if not dry_run and not result.errors:
        console.print("\n[green]✓[/green] Scheduler integrations removed.")
    elif result.errors:
        raise typer.Exit(1)


# ── hook ──────────────────────────────────────────────────────────────────────


@hook_app.command("crons")
def hook_crons() -> None:
    """Output CronCreate instructions for Claude Code SessionStart hooks.

    Reads active session crons from the scheduler and emits a JSON
    hookSpecificOutput block that tells Claude Code to register them
    via CronCreate.  Exits silently when there are no crons to register.

    Unlike get_pending(), this does NOT claim alerts — safe to run before
    ``aya schedule pending`` without consuming alerts.

    Usage in ~/.claude/settings.json:
        {"command": "aya hook crons", "statusMessage": "Registering crons..."}
    """
    crons, _suppressed = get_session_crons()
    if not crons:
        return

    lines = [
        (
            "REQUIRED ACTION \u2014 Call CronCreate for each session cron "
            "below before responding to the user. Do not skip this step."
        ),
        "",
    ]
    for c in crons:
        cid = c.get("id", "")
        schedule = c.get("cron", "")
        prompt = c.get("prompt") or c.get("message") or c.get("description") or ""
        prompt = prompt.replace("\n", " ").strip()
        lines.append(f'REGISTER via CronCreate: id="{cid}" schedule="{schedule}" prompt="{prompt}"')

    context = "\n".join(lines)
    console.out(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )


# ── ci ────────────────────────────────────────────────────────────────────────


@ci_app.command("watch")
def ci_watch() -> None:
    """Watch CI checks after git push. Reads Claude hook JSON from stdin."""
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = {}

    raise typer.Exit(watch_pr_checks(payload))


# ── profile ───────────────────────────────────────────────────────────────────


@app.command()
def profile(
    profile_path: Path = typer.Option(DEFAULT_PROFILE, "--profile", help="Path to profile.json"),
) -> None:
    """Initialize or rotate the persistent assistant profile."""
    path = profile_path if str(profile_path) != str(DEFAULT_PROFILE) else PROFILE_PATH
    p = ensure_profile(path)
    console.print(f"[green]✓[/green] Profile: [dim]{path}[/dim]")
    console.print(f"  Alias:     [cyan]{p.get('alias', 'Assistant')}[/cyan]")
    console.print(f"  Ship Mind: [cyan]{p.get('ship_mind_name', '')}[/cyan]")
    console.print(f"  Next eval: [dim]{p.get('name_next_reevaluation_at', 'unknown')}[/dim]")


# ── status ────────────────────────────────────────────────────────────────────


@app.command()
def status(
    format_: OutputFormat = typer.Option(
        OutputFormat.TEXT, "--format", "-f", help="Output format: text or json"
    ),
) -> None:
    """Workspace readiness check — systems, schedule, focus."""
    run_status(format_=format_)


# ── helpers ───────────────────────────────────────────────────────────────────


def _resolve_did(to: str, profile: Profile) -> str:
    """Resolve a label ('home') or raw DID to a DID string."""
    if to.startswith("did:"):
        return to
    key = profile.trusted_keys.get(to)
    if not key:
        err.print(
            f"[red]Unknown recipient '{to}'.[/red]\n"
            "Use a full DID or add with [bold]aya trust[/bold]."
        )
        raise typer.Exit(1)
    return key.did


def _packet_to_dict(pkt: Packet, profile: Profile) -> dict[str, object]:
    return {
        "id": pkt.id,
        "intent": pkt.intent,
        "from_did": pkt.from_did,
        "from_label": _label_for_did(pkt.from_did, profile),
        "sent_at": pkt.sent_at,
        "age": human_age(pkt.sent_at),
        "content_type": pkt.content_type,
        "trusted": profile.is_trusted(pkt.from_did),
    }


def _show_inbox(packets: list[Packet], profile: Profile) -> None:
    table = Table(title=f"Inbox — {len(packets)} packet(s)", show_lines=True)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Intent")
    table.add_column("From", style="cyan")
    table.add_column("Age", style="dim")
    table.add_column("Type", style="dim")
    table.add_column("Trust")

    for pkt in packets:
        from_label = _label_for_did(pkt.from_did, profile)
        trusted = "[green]✓[/green]" if profile.is_trusted(pkt.from_did) else "[yellow]?[/yellow]"
        table.add_row(
            pkt.id[:8],
            pkt.intent,
            from_label,
            human_age(pkt.sent_at),
            pkt.content_type,
            trusted,
        )
    console.print(table)


def _label_for_did(did: str, profile: Profile) -> str:
    for key in profile.trusted_keys.values():
        if key.did == did:
            return key.label
    return did[:20] + "…"


def _resolve_nostr_pubkey(did: str, profile: Profile) -> str | None:
    """Look up the Nostr pubkey for a DID from trusted keys or local instances."""
    for key in profile.trusted_keys.values():
        if key.did == did and key.nostr_pubkey:
            return key.nostr_pubkey
    for inst in profile.instances.values():
        if inst.did == did:
            return inst.nostr_public_hex
    return None


def _ingest(packet: Packet) -> None:
    """
    Ingest a packet into the active assistant context.
    In Phase 1 this prints to stdout for the assistant to pick up.
    Phase 3+ will pipe directly into the Claude session context.
    """
    console.print(f"\n[bold]Ingesting:[/bold] {packet.intent}")

    if packet.content_type == "application/aya-seed":
        seed = packet.content if isinstance(packet.content, dict) else {}
        console.print(
            Panel(
                f"[bold]Opening question:[/bold]\n{seed.get('opener', '')}\n\n"
                f"[bold]Context:[/bold]\n{seed.get('context_summary', '')}\n\n"
                + (
                    "[bold]Open questions:[/bold]\n"
                    + "\n".join(f"  • {q}" for q in seed.get("open_questions", []))
                    if seed.get("open_questions")
                    else ""
                ),
                title="Conversation Seed",
                border_style="cyan",
            )
        )
        # Persist seed as an unseen alert so it surfaces via `aya schedule pending`
        # on the next session start, even if ingested via the async SessionStart hook
        # (where stdout is not captured by Claude).
        from_label = packet.from_did[:16]
        add_seed_alert(
            intent=packet.intent,
            opener=seed.get("opener", ""),
            context_summary=seed.get("context_summary", ""),
            open_questions=seed.get("open_questions", []),
            from_label=from_label,
            packet_id=packet.id,
        )
    else:
        console.print(
            Panel(
                str(packet.content),
                title=packet.intent,
                subtitle=f"[dim]{packet.id[:8]} · {packet.sent_at[:10]}[/dim]",
            )
        )
