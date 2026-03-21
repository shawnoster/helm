"""CLI entry point — helm command."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from helm.identity import Identity, Profile, TrustedKey
from helm.packet import ConflictStrategy, ContentType, Packet, _human_age
from helm.pair import (
    PairingError,
    generate_code,
    hash_code,
    join_pairing,
    poll_for_pair_response,
    publish_pair_request,
)
from helm.relay import RelayClient

app = typer.Typer(
    name="helm",
    help="Personal AI assistant toolkit — sync, schedule, bootstrap.",
    no_args_is_help=True,
)

# ── Schedule sub-app ─────────────────────────────────────────────────────────

schedule_app = typer.Typer(
    name="schedule",
    help="Reminders, watches, and recurring jobs.",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule")

console = Console()
err = Console(stderr=True)

DEFAULT_PROFILE = Path.home() / ".copilot" / "assistant_profile.json"


def _load_profile(profile_path: Path) -> Profile:
    if not profile_path.exists():
        err.print(
            f"[red]Profile not found at {profile_path}.[/red]\n"
            "Run [bold]helm init[/bold] first."
        )
        raise typer.Exit(1)
    return Profile.load(profile_path)


# ── init ─────────────────────────────────────────────────────────────────────


@app.command()
def init(
    label: str = typer.Option("default", help="Label for this instance (work, home, laptop…)"),
    profile: Path = typer.Option(DEFAULT_PROFILE, help="Path to assistant_profile.json"),
    relay: str = typer.Option("wss://relay.damus.io", help="Default Nostr relay URL"),
) -> None:
    """Generate a keypair for this instance and register it in your profile."""
    identity = Identity.generate(label)

    if profile.exists():
        p = Profile.load(profile)
    else:
        profile.parent.mkdir(parents=True, exist_ok=True)
        p = Profile(alias="Ace", ship_mind_name="", user_name="")

    p.instances[label] = identity
    p.default_relay = relay
    p.save(profile)

    console.print(Panel.fit(
        f"[bold green]✓ Instance created[/bold green]\n\n"
        f"Label:  [cyan]{label}[/cyan]\n"
        f"DID:    [dim]{identity.did}[/dim]\n"
        f"Relay:  [cyan]{relay}[/cyan]\n\n"
        "[dim]Share your DID with other instances you want to trust.[/dim]",
        title="Helm — init",
    ))


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
    instance: str = typer.Option("default", help="Which local instance to send from"),
    conflict: ConflictStrategy = typer.Option(
        ConflictStrategy.LAST_WRITE_WINS, help="Conflict resolution strategy"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Pack a knowledge packet ready to send."""
    p = _load_profile(profile)
    local = p.instances.get(instance)
    if not local:
        err.print(f"[red]Instance '{instance}' not found. Run helm init.[/red]")
        raise typer.Exit(1)

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
    instance: str = typer.Option("default"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Send a packet to a Nostr relay."""
    p = _load_profile(profile)
    local = p.instances.get(instance)
    if not local:
        err.print(f"[red]Instance '{instance}' not found.[/red]")
        raise typer.Exit(1)

    relay_url = relay or p.default_relay
    packet = Packet.from_json(packet_file.read_text())
    client = RelayClient(relay_url, local.nostr_private_hex, local.nostr_public_hex)

    # Resolve recipient's Nostr pubkey
    recipient_nostr_pub = _resolve_nostr_pubkey(packet.to_did, p)
    event_id = asyncio.run(client.publish(packet, recipient_nostr_pub))
    console.print(
        f"[green]✓[/green] Sent [cyan]{packet.intent}[/cyan]\n"
        f"  Packet: [dim]{packet.id[:8]}[/dim]  "
        f"Event: [dim]{event_id[:8]}[/dim]  "
        f"Relay: [dim]{relay_url}[/dim]"
    )


# ── receive ───────────────────────────────────────────────────────────────────


@app.command()
def receive(
    relay: str = typer.Option(None),
    instance: str = typer.Option("default"),
    auto_ingest: bool = typer.Option(False, help="Ingest all trusted packets without prompting"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress output when inbox is empty (for startup hooks)"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Poll for pending packets and surface them for review."""

    async def _run() -> None:
        p = _load_profile(profile)
        local = p.instances.get(instance)
        if not local:
            if not quiet:
                err.print(f"[red]Instance '{instance}' not found.[/red]")
            raise typer.Exit(1)

        relay_url = relay or p.default_relay
        client = RelayClient(relay_url, local.nostr_private_hex, local.nostr_public_hex)

        packets: list[Packet] = []
        try:
            async for packet in client.fetch_pending():
                packets.append(packet)
        except Exception:
            if not quiet:
                err.print("[yellow]Could not reach relay — skipping inbox check.[/yellow]")
            return

        if not packets:
            if not quiet:
                console.print("[dim]No pending packets.[/dim]")
            return

        _show_inbox(packets, p)

        for packet in packets:
            trusted = p.is_trusted(packet.from_did)
            trust_label = "[green]trusted[/green]" if trusted else "[yellow]unknown sender[/yellow]"

            if auto_ingest and trusted:
                _ingest(packet)
                continue

            ingest = typer.confirm(
                f"\nIngest '{packet.intent}' ({trust_label})?",
                default=trusted,
            )
            if ingest:
                _ingest(packet)
                sender_nostr_pub = _resolve_nostr_pubkey(packet.from_did, p)
                if sender_nostr_pub:
                    await client.send_receipt(packet, sender_nostr_pub)

    asyncio.run(_run())


# ── inbox ─────────────────────────────────────────────────────────────────────


@app.command()
def inbox(
    relay: str = typer.Option(None),
    instance: str = typer.Option("default"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """List pending packets without ingesting."""

    async def _run() -> None:
        p = _load_profile(profile)
        local = p.instances.get(instance)
        if not local:
            err.print(f"[red]Instance '{instance}' not found.[/red]")
            raise typer.Exit(1)

        relay_url = relay or p.default_relay
        client = RelayClient(relay_url, local.nostr_private_hex, local.nostr_public_hex)

        packets = [pkt async for pkt in client.fetch_pending()]
        if not packets:
            console.print("[dim]Inbox empty.[/dim]")
        else:
            _show_inbox(packets, p)

    asyncio.run(_run())


# ── pair ──────────────────────────────────────────────────────────────────────


@app.command()
def pair(
    code: str = typer.Option(None, help="Pairing code from the other instance (joiner mode)"),
    label: str = typer.Option(..., help="Label for this instance (work, home, laptop)"),
    instance: str = typer.Option("default"),
    relay: str = typer.Option(None, help="Relay URL (overrides profile default)"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Pair two instances with a short-lived code — no manual DID exchange."""
    p = _load_profile(profile)
    local = p.instances.get(instance)
    if not local:
        err.print(f"[red]Instance '{instance}' not found. Run helm init first.[/red]")
        raise typer.Exit(1)

    relay_url = relay or p.default_relay

    if code:
        # ── Joiner mode ──────────────────────────────────────────────
        try:
            trusted = asyncio.run(join_pairing(local, label, code, relay_url))
        except PairingError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        p.trusted_keys[trusted.label] = trusted
        p.save(profile)
        console.print(Panel.fit(
            f"[bold green]✓ Paired![/bold green]\n\n"
            f"Trusted: [cyan]{trusted.label}[/cyan]\n"
            f"DID:     [dim]{trusted.did}[/dim]",
            title="Helm — pair (joined)",
        ))

    else:
        # ── Initiator mode ───────────────────────────────────────────
        pairing_code = generate_code()
        code_h = hash_code(pairing_code)

        # Publish the request
        console.print("[dim]Publishing pairing request…[/dim]")
        request_event_id = asyncio.run(
            publish_pair_request(local, label, code_h, relay_url)
        )

        # Show the code — user reads this aloud or types it on the other machine
        console.print(Panel.fit(
            f"[bold]Pairing code:[/bold]  [bold cyan]{pairing_code}[/bold cyan]\n\n"
            "Enter this on your other machine:\n"
            f"  [dim]helm pair --code {pairing_code} --label <their-label>[/dim]\n\n"
            "[dim]Expires in 10 minutes.[/dim]",
            title="Helm — pair",
        ))

        # Poll for response
        with console.status("[bold cyan]Waiting for the other instance…[/bold cyan]"):
            trusted = asyncio.run(
                poll_for_pair_response(relay_url, local.nostr_public_hex, request_event_id)
            )

        if trusted is None:
            console.print(
                "[bold yellow]Pairing timed out.[/bold yellow] "
                "Run [bold]helm pair[/bold] again for a new code."
            )
            raise typer.Exit(1)

        p.trusted_keys[trusted.label] = trusted
        p.save(profile)
        console.print(Panel.fit(
            f"[bold green]✓ Paired![/bold green]\n\n"
            f"Trusted: [cyan]{trusted.label}[/cyan]\n"
            f"DID:     [dim]{trusted.did}[/dim]",
            title="Helm — pair (complete)",
        ))


# ── schedule subcommands ──────────────────────────────────────────────────────


@schedule_app.command("remind")
def schedule_remind(
    message: str = typer.Option(..., "--message", "-m", help="Reminder message"),
    due: str = typer.Option(..., "--due", "-d", help="When: 'tomorrow 9am', 'in 2 hours', ISO8601"),
    tag: str = typer.Option("", "--tag", "-t", help="Comma-separated tags"),
) -> None:
    """Add a one-shot reminder."""
    from helm.scheduler import add_reminder, parse_due
    item = add_reminder(message, due, tag)
    due_dt = parse_due(due)
    console.print(f"[green]✓[/green] Reminder {item['id'][:8]} — {due_dt.strftime('%a %b %d, %I:%M %p')}")
    console.print(f"  {message}")


@schedule_app.command("watch")
def schedule_watch(
    provider: str = typer.Argument(help="Provider: github-pr, jira-query, jira-ticket"),
    target: str = typer.Argument(help="Target: owner/repo#123, JQL, or TICKET-123"),
    message: str = typer.Option(..., "--message", "-m", help="Watch description"),
    tag: str = typer.Option("", "--tag", "-t", help="Comma-separated tags"),
    condition: str = typer.Option("", "--condition", "-c", help="Condition: approved_or_merged, etc."),
    interval: int = typer.Option(30, "--interval", "-i", help="Poll interval minutes"),
    remove_when: str = typer.Option("", help="Auto-remove: merged_or_closed"),
) -> None:
    """Add a condition-based watch."""
    from helm.scheduler import add_watch
    try:
        item = add_watch(provider, target, message, tag, condition, interval, remove_when)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]✓[/green] Watch {item['id'][:8]} ({provider})")
    console.print(f"  {message}")
    console.print(f"  Condition: {item['condition']}, poll every {item['poll_interval_minutes']}m")


@schedule_app.command("list")
def schedule_list(
    all_items: bool = typer.Option(False, "--all", "-a", help="Include dismissed/delivered"),
    item_type: str = typer.Option(None, "--type", help="Filter: reminder, watch, recurring, event"),
) -> None:
    """List scheduled items."""
    from helm.scheduler import list_items, _display_items
    items = list_items(show_all=all_items, item_type=item_type)
    _display_items(items)


@schedule_app.command("check")
def schedule_check(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Check for due reminders and alerts."""
    from helm.scheduler import check_due
    due_items, unseen = check_due()

    if as_json:
        import json as _json
        print(_json.dumps({"due_reminders": due_items, "alerts": unseen}, indent=2, default=str))
        return

    if not due_items and not unseen:
        console.print("[dim]Nothing due. No alerts.[/dim]")
        return

    if due_items:
        console.print(f"\n  [bold]⏰ {len(due_items)} reminder(s) due:[/bold]")
        for r in due_items:
            from datetime import datetime
            due_dt = datetime.fromisoformat(r["due_at"])
            console.print(f"    🔴 {r['id'][:8]}  {due_dt.strftime('%I:%M %p')}  {r['message'][:55]}")

    if unseen:
        console.print(f"\n  [bold]🔔 {len(unseen)} alert(s):[/bold]")
        for a in unseen:
            console.print(f"    📢 {a['source_item_id'][:8]}  {a['message'][:60]}")


@schedule_app.command("dismiss")
def schedule_dismiss(
    item_id: str = typer.Argument(help="Item ID (prefix match ok)"),
) -> None:
    """Dismiss an item."""
    from helm.scheduler import dismiss_item
    try:
        item = dismiss_item(item_id)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]✓[/green] Dismissed {item['id'][:8]} — {item['message'][:60]}")


@schedule_app.command("snooze")
def schedule_snooze(
    item_id: str = typer.Argument(help="Item ID (prefix match ok)"),
    until: str = typer.Option(..., "--until", "-u", help="Snooze until: 'in 1 hour', 'tomorrow 9am'"),
) -> None:
    """Snooze a reminder."""
    from helm.scheduler import snooze_item
    try:
        item, snooze_until = snooze_item(item_id, until)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"💤 Snoozed {item['id'][:8]} until {snooze_until.strftime('%a %b %d, %I:%M %p')}")


@schedule_app.command("poll")
def schedule_poll(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress output on no changes"),
) -> None:
    """Run one poll cycle (for daemon/cron)."""
    from helm.scheduler import run_poll
    run_poll(quiet=quiet)


@schedule_app.command("alerts")
def schedule_alerts(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    mark_seen: bool = typer.Option(False, "--mark-seen", help="Mark all alerts as seen"),
) -> None:
    """Show alerts from background watcher."""
    from helm.scheduler import show_alerts
    unseen = show_alerts(as_json=as_json, mark_seen=mark_seen)

    if as_json:
        import json as _json
        print(_json.dumps(unseen, indent=2, default=str))
        return

    if not unseen:
        console.print("[dim]No unseen alerts.[/dim]")
        return

    console.print(f"\n  [bold]🔔 {len(unseen)} alert(s):[/bold]")
    for a in unseen:
        from datetime import datetime
        ts = datetime.fromisoformat(a["created_at"]).strftime("%b %d %I:%M %p")
        console.print(f"    📢 {a['source_item_id'][:8]}  {ts}  {a['message'][:55]}")

    if mark_seen:
        console.print(f"\n  Marked {len(unseen)} alert(s) as seen.")


# ── status ────────────────────────────────────────────────────────────────────


@app.command()
def status() -> None:
    """Workspace readiness check — systems, schedule, focus."""
    from helm.status import run_status
    run_status()


# ── bootstrap ────────────────────────────────────────────────────────────────


@app.command()
def bootstrap(
    root: Path = typer.Option(
        Path.cwd(), help="Workspace root directory (default: current directory)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts"),
) -> None:
    """Scaffold a personal assistant workspace."""
    from helm.workspace import bootstrap_workspace

    bootstrap_workspace(root.expanduser().resolve(), interactive=not yes, console=console)


# ── helpers ───────────────────────────────────────────────────────────────────


def _resolve_did(to: str, profile: Profile) -> str:
    """Resolve a label ('home') or raw DID to a DID string."""
    if to.startswith("did:"):
        return to
    key = profile.trusted_keys.get(to)
    if not key:
        err.print(
            f"[red]Unknown recipient '{to}'.[/red]\n"
            "Use a full DID or add with [bold]helm trust[/bold]."
        )
        raise typer.Exit(1)
    return key.did


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
            _human_age(pkt.sent_at),
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

    if packet.content_type == "application/ace-seed":
        seed = packet.content if isinstance(packet.content, dict) else {}
        console.print(Panel(
            f"[bold]Opening question:[/bold]\n{seed.get('opener', '')}\n\n"
            f"[bold]Context:[/bold]\n{seed.get('context_summary', '')}\n\n"
            + (
                "[bold]Open questions:[/bold]\n"
                + "\n".join(f"  • {q}" for q in seed.get("open_questions", []))
                if seed.get("open_questions") else ""
            ),
            title="Conversation Seed",
            border_style="cyan",
        ))
    else:
        console.print(Panel(
            str(packet.content),
            title=packet.intent,
            subtitle=f"[dim]{packet.id[:8]} · {packet.sent_at[:10]}[/dim]",
        ))
