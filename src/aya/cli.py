"""CLI entry point — aya command."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from aya import __version__
from aya.ci import watch_pr_checks
from aya.config import get_notebook_path, load_config, set_config_value
from aya.context import build_context_block
from aya.identity import Identity, Profile, TrustedKey, _assert_valid_ulid
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
from aya.paths import CONFIG_PATH, PROFILE_PATH
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

logger = logging.getLogger(__name__)


class OutputFormat(StrEnum):
    AUTO = "auto"
    TEXT = "text"
    JSON = "json"


class StatusFormat(StrEnum):
    AUTO = "auto"
    TEXT = "text"
    JSON = "json"
    RICH = "rich"


def resolve_format(fmt: OutputFormat) -> OutputFormat:
    """Resolve AUTO to a concrete format based on env var or TTY detection."""
    if fmt is not OutputFormat.AUTO:
        return fmt
    env = os.environ.get("AYA_FORMAT", "").strip().lower()
    if env in ("text", "json"):
        return OutputFormat(env)
    return OutputFormat.TEXT if sys.stdout.isatty() else OutputFormat.JSON


def resolve_status_format(fmt: StatusFormat) -> StatusFormat:
    """Resolve AUTO to a concrete format based on env var or TTY detection."""
    if fmt is not StatusFormat.AUTO:
        return fmt
    env = os.environ.get("AYA_FORMAT", "").strip().lower()
    if env in ("text", "json", "rich"):
        return StatusFormat(env)
    return StatusFormat.TEXT if sys.stdout.isatty() else StatusFormat.JSON


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

# ── Config sub-app ────────────────────────────────────────────────────────────

config_app = typer.Typer(
    name="config",
    help="Workspace configuration (notebook path, etc.).",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")

console = Console()
err = Console(stderr=True)


# ── Structured error codes ──────────────────────────────────────────────────


class ErrorCode:
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    INSTANCE_NOT_FOUND = "INSTANCE_NOT_FOUND"
    RELAY_UNREACHABLE = "RELAY_UNREACHABLE"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    PACKET_NOT_FOUND = "PACKET_NOT_FOUND"
    PEER_NOT_TRUSTED = "PEER_NOT_TRUSTED"
    PAIR_FAILED = "PAIR_FAILED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    AMBIGUOUS_PREFIX = "AMBIGUOUS_PREFIX"
    DISPATCH_FAILED = "DISPATCH_FAILED"


def _want_json_errors() -> bool:
    """True when errors should be emitted as structured JSON."""
    env = os.environ.get("AYA_FORMAT", "").strip().lower()
    if env == "json":
        return True
    if env == "text":
        return False
    return not sys.stderr.isatty()


def _emit_error(
    code: str,
    message: str,
    context: dict[str, object] | None = None,
    exit_code: int = 1,
) -> NoReturn:
    """Emit an error — structured JSON on stderr in JSON mode, Rich-formatted otherwise."""
    if _want_json_errors():
        payload: dict[str, object] = {"error": {"code": code, "message": message}}
        if context:
            payload["error"]["context"] = context  # type: ignore[index]
        err.out(json.dumps(payload, default=str))
    else:
        err.print(f"[red]{message}[/red]")
    raise typer.Exit(exit_code)


DEFAULT_PROFILE = PROFILE_PATH


def _load_profile(profile_path: Path) -> Profile:
    if not profile_path.exists():
        _emit_error(
            ErrorCode.PROFILE_NOT_FOUND,
            f"Profile not found at {profile_path}. Run 'aya init' first.",
            {"path": str(profile_path)},
        )
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
            _emit_error(
                ErrorCode.INSTANCE_NOT_FOUND,
                f"Instance '{instance}' not found. Available: {names}.",
                {"instance": instance, "available": available},
            )
        else:
            _emit_error(
                ErrorCode.INSTANCE_NOT_FOUND,
                f"Instance '{instance}' not found. Run 'aya init' first.",
                {"instance": instance},
            )
    raise typer.Exit(1)


# ── version ──────────────────────────────────────────────────────────────────


@app.command()
def version(
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Show the installed aya version."""
    format_ = resolve_format(format_)
    if format_ == OutputFormat.JSON:
        _output_json({"version": __version__})
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
    peer: str = typer.Option(None, "--peer", help="Name for the remote peer"),
    label: str = typer.Option(None, "--label", help="[deprecated] Use --peer instead", hidden=True),
    nostr_pubkey: str = typer.Option(
        None,
        help="Nostr pubkey hex (required for send/receive; pairing fills this automatically)",
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Add a DID to your trusted keys list."""
    if label is not None and peer is not None:
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --peer and --label together. Use --peer only.",
            exit_code=2,
        )
    if label is not None:
        err.print("[yellow]Warning: --label is deprecated, use --peer instead[/yellow]")
        peer = label
    if peer is None:
        _emit_error(ErrorCode.INVALID_ARGUMENT, "Missing option '--peer'.", exit_code=2)
    p = _load_profile(profile)
    p.trusted_keys[peer] = TrustedKey(
        did=did,
        label=peer,
        nostr_pubkey=nostr_pubkey,
    )
    p.save(profile)
    console.print(f"[green]✓[/green] Trusted: [cyan]{peer}[/cyan]  [dim]{did}[/dim]")
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
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    instance: str = typer.Option(
        None, "--instance", help="[deprecated] Use --as instead", hidden=True
    ),
    conflict: ConflictStrategy = typer.Option(
        ConflictStrategy.LAST_WRITE_WINS, help="Conflict resolution strategy"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Pack a knowledge packet ready to send."""
    if instance is not None and as_ != "default":
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --as and --instance together. Use --as only.",
            exit_code=2,
        )
    if instance is not None:
        err.print("[yellow]Warning: --instance is deprecated, use --as instead[/yellow]")
        as_ = instance
    p = _load_profile(profile)
    local = _resolve_instance(p, as_)

    # Resolve recipient DID
    to_did, _to_label = _resolve_did(to, p)

    if seed:
        if not opener:
            _emit_error(ErrorCode.INVALID_ARGUMENT, "--opener required for seed packets.")
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
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    instance: str = typer.Option(
        None, "--instance", help="[deprecated] Use --as instead", hidden=True
    ),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show packet without publishing"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Send a packet to a Nostr relay."""
    if instance is not None and as_ != "default":
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --as and --instance together. Use --as only.",
            exit_code=2,
        )
    if instance is not None:
        err.print("[yellow]Warning: --instance is deprecated, use --as instead[/yellow]")
        as_ = instance
    p = _load_profile(profile)
    local = _resolve_instance(p, as_)

    relay_urls = [relay] if relay else p.default_relays
    packet = Packet.from_json(packet_file.read_text())

    if dry_run:
        _output_json(json.loads(packet.to_json()))
        raise typer.Exit(0)

    client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

    # Resolve recipient's Nostr pubkey
    recipient_nostr_pub = _resolve_nostr_pubkey(packet.to_did, p)
    event_id = asyncio.run(client.publish(packet, recipient_nostr_pub, encrypt=packet.encrypted))
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
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    instance: str = typer.Option(
        None, "--instance", help="[deprecated] Use --as instead", hidden=True
    ),
    relay: str = typer.Option(None, help="Relay URL (overrides profile default)"),
    conflict: ConflictStrategy = typer.Option(
        ConflictStrategy.LAST_WRITE_WINS, help="Conflict resolution strategy"
    ),
    no_encrypt: bool = typer.Option(
        False, "--no-encrypt", help="Send plaintext (debug or private-relay mode)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show packet without publishing"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Pack and send in one step — the natural 'pack for home' flow."""
    if instance is not None and as_ != "default":
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --as and --instance together. Use --as only.",
            exit_code=2,
        )
    if instance is not None:
        err.print("[yellow]Warning: --instance is deprecated, use --as instead[/yellow]")
        as_ = instance

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, as_)

        to_did, to_label = _resolve_did(to, p)

        if seed:
            if not opener:
                _emit_error(ErrorCode.INVALID_ARGUMENT, "--opener required for seed packets.")
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

        # Mark the packet encrypted before signing so the flag is covered by the signature.
        if not no_encrypt:
            packet.encrypted = True

        signed = packet.sign(local)

        if dry_run:
            _output_json(json.loads(signed.to_json()))
            return

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
            event_id = await client.publish(signed, recipient_nostr_pub, encrypt=not no_encrypt)
        except Exception:
            logger.exception("Relay publish failed during dispatch")
            _emit_error(
                ErrorCode.DISPATCH_FAILED,
                "Dispatch failed — event could not be published to relay(s).",
                {"relay": relay_urls[0] if relay_urls else None},
            )

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
                f"To:      [dim]{to_label}[/dim]",
                title="aya — dispatch",
            )
        )

    asyncio.run(_run())


# ── ack ───────────────────────────────────────────────────────────────────────


@app.command()
def ack(
    packet_id: str = typer.Argument(help="Packet ID or prefix to acknowledge"),
    message: str | None = typer.Argument(
        None, help="Short reply message (default: 'acknowledged')"
    ),
    dismiss: bool = typer.Option(
        False, "--dismiss", help="No-action acknowledgment; message defaults to 'acknowledged'"
    ),
    as_: str = typer.Option(
        "default", "--as", "--instance", help="Local identity to act as (legacy alias: --instance)"
    ),
    relay: str = typer.Option(None, help="Relay URL (overrides profile default)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show ACK packet without publishing"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Acknowledge a received seed packet — sends a reply back to the sender."""

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, as_)

        # Resolve full packet ID from ingested_ids (prefix match, min 8 chars)
        if len(packet_id) < 8:
            _emit_error(
                ErrorCode.INVALID_ARGUMENT,
                "Packet ID prefix must be at least 8 characters.",
                {"packet_id": packet_id},
            )
        ingested_ids = [entry["id"] for entry in p.ingested_ids]
        matched = [pid for pid in ingested_ids if pid.startswith(packet_id)]
        if not matched:
            _emit_error(
                ErrorCode.PACKET_NOT_FOUND,
                f"Packet ID '{packet_id}' not found in ingested_ids.",
                {"packet_id": packet_id},
            )
        if len(matched) > 1:
            _emit_error(
                ErrorCode.AMBIGUOUS_PREFIX,
                f"Ambiguous prefix '{packet_id}' — matches {len(matched)} packets.",
                {"packet_id": packet_id, "matches": len(matched)},
            )

        full_packet_id = matched[0]

        # Look up the sender's DID from the ingested_ids entry (stored since #132).
        entry = next((e for e in p.ingested_ids if e["id"] == full_packet_id), None)
        sender_did = entry.get("from_did") if entry else None

        to_label: str | None = None
        to_key: TrustedKey | None = None
        to_did: str | None = None
        recipient_nostr_pub: str | None = None

        if sender_did:
            for label, tk in p.trusted_keys.items():
                if tk.did == sender_did and tk.nostr_pubkey:
                    to_label, to_key = label, tk
                    to_did = tk.did
                    recipient_nostr_pub = tk.nostr_pubkey
                    break
            else:
                # sender_did found but not in trusted_keys or no nostr_pubkey
                sender_did = None  # fall through to existing logic

        if not sender_did:
            # Fallback: pick the sole trusted peer with a Nostr pubkey (pre-#132 entries).
            trusted_with_nostr = [
                (label, tk) for label, tk in p.trusted_keys.items() if tk.nostr_pubkey
            ]

            if not trusted_with_nostr:
                err.print(
                    "[red]No trusted peers with a Nostr pubkey found.[/red]\n"
                    "Pair with the sender first: [bold]aya pair[/bold]"
                )
                raise typer.Exit(1)

            if len(trusted_with_nostr) > 1:
                names = ", ".join(lbl for lbl, _ in trusted_with_nostr)
                err.print(
                    "[red]Multiple trusted peers — cannot determine ACK recipient.[/red]\n"
                    f"Available: [cyan]{names}[/cyan]\n"
                    "[dim]Support for --to <peer> will be added in a future release.[/dim]"
                )
                raise typer.Exit(1)

            to_label, to_key = trusted_with_nostr[0]
            to_did = to_key.did
            recipient_nostr_pub = to_key.nostr_pubkey  # guaranteed non-None above

        reply_text = message if message else "acknowledged"

        ack_packet = Packet(
            **{"from": local.did, "to": to_did},
            intent="ack",
            content_type=ContentType.JSON,
            content={
                "in_reply_to": full_packet_id,
                "message": reply_text,
                "dismiss": dismiss,
            },
            in_reply_to=full_packet_id,
        )
        signed = ack_packet.sign(local)

        if dry_run:
            _output_json(json.loads(signed.to_json()))
            return

        relay_urls = [relay] if relay else p.default_relays
        client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)
        try:
            event_id = await client.publish(signed, recipient_nostr_pub, encrypt=True)
        except Exception:
            err.print("[yellow]Could not reach relay — ack failed.[/yellow]")
            raise typer.Exit(1) from None

        # Mark any matching seed alert as seen (best-effort)
        try:
            alerts = show_alerts(mark_seen=False)
            for alert in alerts:
                if alert.get("source_item_id", "").startswith(packet_id):
                    dismiss_alert(alert["id"])
                    break
        except Exception:  # noqa: S110
            pass  # alert cleanup is best-effort; do not block the ACK response

        relay_count = len(relay_urls)
        relay_display = (
            relay_urls[0] if relay_count == 1 else f"{relay_urls[0]} (+{relay_count - 1})"
        )
        console.print(
            Panel.fit(
                f"[bold green]✓ ACK sent[/bold green]\n\n"
                f"In reply to: [dim]{full_packet_id[:8]}[/dim]\n"
                f"To:          [dim]{to_label}[/dim]\n"
                f"Message:     [cyan]{reply_text}[/cyan]\n"
                f"Event:       [dim]{event_id[:8]}[/dim]\n"
                f"Relay:       [dim]{relay_display}[/dim]",
                title="aya — ack",
            )
        )

    asyncio.run(_run())


# ── receive ───────────────────────────────────────────────────────────────────


@app.command()
def receive(
    relay: str = typer.Option(None),
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    instance: str = typer.Option(
        None, "--instance", help="[deprecated] Use --as instead", hidden=True
    ),
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
    if instance is not None and as_ != "default":
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --as and --instance together. Use --as only.",
            exit_code=2,
        )
    if instance is not None:
        err.print("[yellow]Warning: --instance is deprecated, use --as instead[/yellow]")
        as_ = instance

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, as_, quiet=quiet)

        relay_urls = [relay] if relay else p.default_relays
        client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

        # Compute since from last_checked to avoid re-scanning the full window on
        # every poll.  60-second lookback guards against minor clock drift.
        # Clamp to at most 7 days back so a very stale last_checked doesn't
        # trigger an unbounded relay scan or override the relay's default window.
        since: datetime | None = None
        if p.last_checked:
            now = datetime.now(UTC)
            oldest = min(datetime.fromisoformat(v) for v in p.last_checked.values())
            since = max(oldest - timedelta(seconds=60), now - timedelta(days=7))

        # Fetch pending packets for this instance; ingested_ids is the authoritative
        # dedup mechanism and filters already-seen packets below.
        packets: list[Packet] = []
        since_kwargs: dict = {"since": since} if since is not None else {}
        try:
            async for packet in client.fetch_pending(**since_kwargs):
                packets.append(packet)
        except Exception:
            logger.exception("Relay fetch failed during receive")
            if not quiet:
                err.print("[yellow]Could not reach relay — skipping relay fetch.[/yellow]")
            return

        # Record that we checked these relays — persist even when inbox is empty
        # so future polls use a narrow since window rather than the full 7-day default.
        now_check_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for url in relay_urls:
            p.last_checked[url] = now_check_iso

        if not packets:
            if not quiet:
                console.print("[dim]No pending packets.[/dim]")
            p.save(profile)
            return

        # Verify signatures — reject tampered or unsigned packets
        verified: list[Packet] = []
        ingested_set = {entry["id"] for entry in p.ingested_ids}
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
            p.save(profile)
            return

        _show_inbox(verified, p)

        for packet in verified:
            trusted = p.is_trusted(packet.from_did)
            trust_label = "[green]trusted[/green]" if trusted else "[yellow]unknown sender[/yellow]"

            now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if auto_ingest and trusted:
                _assert_valid_ulid(packet.id)
                _ingest(packet)
                p.ingested_ids.append(
                    {
                        "id": packet.id,
                        "ingested_at": now_iso,
                        "from_did": packet.from_did,
                    }
                )
                continue

            ingest = yes or typer.confirm(
                f"\nIngest '{packet.intent}' ({trust_label})?",
                default=trusted,
            )
            if ingest:
                _assert_valid_ulid(packet.id)
                _ingest(packet)
                p.ingested_ids.append(
                    {
                        "id": packet.id,
                        "ingested_at": now_iso,
                        "from_did": packet.from_did,
                    }
                )
                sender_nostr_pub = _resolve_nostr_pubkey(packet.from_did, p)
                if sender_nostr_pub:
                    await client.send_receipt(packet, sender_nostr_pub)

        # Persist updated ingested_ids and last_checked.
        p.save(profile)

    asyncio.run(_run())


# ── inbox ─────────────────────────────────────────────────────────────────────


@app.command()
def inbox(
    relay: str = typer.Option(None),
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    instance: str = typer.Option(
        None, "--instance", help="[deprecated] Use --as instead", hidden=True
    ),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
    show_all: bool = typer.Option(
        False, "--all", help="Show all packets including already-ingested ones"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """List pending packets without ingesting."""
    if instance is not None and as_ != "default":
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --as and --instance together. Use --as only.",
            exit_code=2,
        )
    if instance is not None:
        err.print("[yellow]Warning: --instance is deprecated, use --as instead[/yellow]")
        as_ = instance
    format_ = resolve_format(format_)

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, as_)

        relay_urls = [relay] if relay else p.default_relays
        client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

        all_packets = [pkt async for pkt in client.fetch_pending()]
        ingested_set = {entry["id"] for entry in p.ingested_ids}

        new_packets = [pkt for pkt in all_packets if pkt.id not in ingested_set]
        display_packets = all_packets if show_all else new_packets

        if format_ == OutputFormat.JSON:
            ingested_for_json = ingested_set if show_all else None
            _output_json([_packet_to_dict(pkt, p, ingested_for_json) for pkt in display_packets])
        elif not display_packets:
            console.print("[dim]Inbox empty.[/dim]")
        else:
            _show_inbox(display_packets, p, ingested_set if show_all else None)
            if show_all and len(all_packets) != len(new_packets):
                total = len(all_packets)
                new = len(new_packets)
                console.print(f"[dim]{total} total, {new} new[/dim]")

    asyncio.run(_run())


# ── pair ──────────────────────────────────────────────────────────────────────


@app.command()
def pair(
    code: str = typer.Option(None, help="Pairing code from the other instance (joiner mode)"),
    peer: str = typer.Option(None, "--peer", help="Name for the remote peer"),
    label: str = typer.Option(None, "--label", help="[deprecated] Use --peer instead", hidden=True),
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    instance: str = typer.Option(
        None, "--instance", help="[deprecated] Use --as instead", hidden=True
    ),
    relay: str = typer.Option(None, help="Relay URL (overrides profile default)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show pairing intent without publishing"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
) -> None:
    """Pair two instances with a short-lived code — no manual DID exchange."""
    if label is not None and peer is not None:
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --peer and --label together. Use --peer only.",
            exit_code=2,
        )
    if label is not None:
        err.print("[yellow]Warning: --label is deprecated, use --peer instead[/yellow]")
        peer = label
    if instance is not None and as_ != "default":
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --as and --instance together. Use --as only.",
            exit_code=2,
        )
    if instance is not None:
        err.print("[yellow]Warning: --instance is deprecated, use --as instead[/yellow]")
        as_ = instance
    if peer is None:
        _emit_error(ErrorCode.INVALID_ARGUMENT, "Missing option '--peer'.", exit_code=2)
    p = _load_profile(profile)
    local = _resolve_instance(p, as_)

    relay_urls = [relay] if relay else p.default_relays

    if dry_run:
        summary = {
            "action": "join_pairing" if code else "initiate_pairing",
            "local_did": local.did,
            "peer_label": peer,
            "relay": relay_urls[0] if relay_urls else None,
        }
        if code:
            summary["code"] = code
        _output_json(summary)
        raise typer.Exit(0)

    if code:
        # ── Joiner mode ──────────────────────────────────────────────
        try:
            trusted = asyncio.run(join_pairing(local, code, relay_urls))
        except PairingError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        trusted.label = peer
        p.trusted_keys[peer] = trusted
        p.save(profile)
        console.print(
            Panel.fit(
                f"[bold green]✓ Paired![/bold green]\n\n"
                f"Trusted: [cyan]{peer}[/cyan]\n"
                f"DID:     [dim]{trusted.did}[/dim]",
                title="aya — pair (joined)",
            )
        )

    else:
        # ── Initiator mode ───────────────────────────────────────────
        pairing_code = generate_code()
        code_h = hash_code(pairing_code)

        # Publish the request — embed our own label so the joiner knows what to call us
        console.print("[dim]Publishing pairing request…[/dim]")
        request_event_id = asyncio.run(publish_pair_request(local, local.label, code_h, relay_urls))

        # Show the code — user reads this aloud or types it on the other machine
        console.print(
            Panel.fit(
                f"[bold]Pairing code:[/bold]  [bold cyan]{pairing_code}[/bold cyan]\n\n"
                "Enter this on your other machine:\n"
                f"  [dim]aya pair --code {pairing_code}"
                " --peer <their-name> --as <local-identity>[/dim]\n\n"
                "[dim]Expires in 10 minutes.[/dim]",
                title="aya — pair",
            )
        )

        # Poll for response
        with console.status("[bold cyan]Waiting for the other peer…[/bold cyan]"):
            trusted = asyncio.run(
                poll_for_pair_response(relay_urls, local.nostr_public_hex, request_event_id)
            )

        if trusted is None:
            console.print(
                "[bold yellow]Pairing timed out.[/bold yellow] "
                "Run [bold]aya pair[/bold] again for a new code."
            )
            raise typer.Exit(1)

        trusted.label = peer
        p.trusted_keys[peer] = trusted
        p.save(profile)
        console.print(
            Panel.fit(
                f"[bold green]✓ Paired![/bold green]\n\n"
                f"Trusted: [cyan]{peer}[/cyan]\n"
                f"DID:     [dim]{trusted.did}[/dim]",
                title="aya — pair (complete)",
            )
        )


# ── schedule subcommands ──────────────────────────────────────────────────────


@schedule_app.command("remind")
def schedule_remind(
    message: str = typer.Option(..., "--message", "-m", help="Reminder message"),
    due: str = typer.Option(..., "--due", "-d", help="When: 'tomorrow 9am', 'in 2 hours', ISO8601"),
    tag: str = typer.Option("", "--tags", "-t", help="Comma-separated tags"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show reminder without saving"),
) -> None:
    """Add a one-shot reminder."""
    if dry_run:
        due_dt = parse_due(due)
        preview = {
            "type": "reminder",
            "status": "pending",
            "message": message,
            "tags": [t.strip() for t in tag.split(",") if t.strip()] if tag else [],
            "due_at": due_dt.isoformat(),
        }
        _output_json(preview)
        raise typer.Exit(0)
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
    tag: str = typer.Option("", "--tags", "-t", help="Comma-separated tags"),
    condition: str = typer.Option(
        "", "--condition", "-c", help="Condition: approved_or_merged, etc."
    ),
    interval: int = typer.Option(30, "--interval", "-i", help="Poll interval minutes"),
    remove_when: str = typer.Option("", help="Auto-remove: merged_or_closed"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show watch without saving"),
) -> None:
    """Add a condition-based watch."""
    # Validate provider/target before dry-run output
    if provider == "github-pr":
        if not re.match(r"([^/]+)/([^#]+)#(\d+)", target):
            err.print("[red]Format: owner/repo#123[/red]")
            raise typer.Exit(1)
    elif provider not in ("jira-query", "jira-ticket"):
        err.print(f"[red]Unknown provider: {provider}[/red]")
        raise typer.Exit(1)

    if dry_run:
        preview = {
            "type": "watch",
            "status": "active",
            "message": message,
            "tags": [t.strip() for t in tag.split(",") if t.strip()] if tag else [],
            "provider": provider,
            "target": target,
            "condition": condition
            or {
                "github-pr": "approved_or_merged",
                "jira-query": "new_results",
                "jira-ticket": "status_changed",
            }.get(provider, ""),
            "poll_interval_minutes": interval,
            "remove_when": remove_when,
        }
        _output_json(preview)
        raise typer.Exit(0)
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
    tag: str = typer.Option("", "--tags", "-t", help="Comma-separated tags"),
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
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show cron without saving"),
) -> None:
    """Add a persistent recurring session job (session_required cron)."""
    # Validate cron expression before dry-run output
    parts = cron.strip().split()
    if len(parts) != 5:
        err.print(f"[red]Invalid cron expression (expected 5 fields): {cron}[/red]")
        raise typer.Exit(1)

    if dry_run:
        preview = {
            "type": "recurring",
            "status": "active",
            "message": message,
            "tags": [t.strip() for t in tag.split(",") if t.strip()] if tag else [],
            "session_required": True,
            "cron": cron,
            "prompt": prompt,
            "idle_back_off": idle_back_off,
            "only_during": only_during,
        }
        _output_json(preview)
        raise typer.Exit(0)
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
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """List scheduled items."""
    format_ = resolve_format(format_)
    items = list_items(show_all=all_items, item_type=item_type)
    if format_ == OutputFormat.JSON:
        _output_json(items)
    else:
        _display_items(items)


@schedule_app.command("check")
def schedule_check(
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Check for due reminders and alerts."""
    format_ = resolve_format(format_)
    due_items, unseen = check_due()

    if format_ == OutputFormat.JSON:
        _output_json({"due": due_items, "alerts": unseen})
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
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Show pending items for this session — alerts to deliver + session crons.

    SessionStart hook entry point:
        aya scheduler pending --format text
    """
    format_ = resolve_format(format_)
    pending = get_pending()
    if format_ == OutputFormat.JSON:
        _output_json(pending)
    else:
        console.print(format_pending(pending))


@schedule_app.command("status")
def schedule_status(
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Show scheduler overview — watches, reminders, crons, deliveries."""
    format_ = resolve_format(format_)
    status = get_scheduler_status()
    if format_ == OutputFormat.JSON:
        _output_json(status)
    else:
        console.print(format_scheduler_status(status))


@schedule_app.command("alerts")
def schedule_alerts(
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
    mark_seen: bool = typer.Option(False, "--mark-seen", help="Mark all alerts as seen"),
) -> None:
    """Show alerts from background watcher."""
    format_ = resolve_format(format_)
    unseen = show_alerts(mark_seen=mark_seen)

    if format_ == OutputFormat.JSON:
        _output_json(unseen)
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

    # Emit one hookSpecificOutput per cron so each gets its own system
    # reminder and can't be truncated when multiple crons are bundled.
    for c in crons:
        cid = c.get("id", "")
        schedule = c.get("cron", "")
        prompt = c.get("prompt") or c.get("message") or c.get("description") or ""
        prompt = prompt.replace("\n", " ").replace('"', '\\"').strip()
        context = (
            "REQUIRED ACTION \u2014 Call CronCreate for each session cron "
            "below before responding to the user. Do not skip this step.\n\n"
            f'REGISTER via CronCreate: id="{cid}" schedule="{schedule}" prompt="{prompt}"'
        )
        print(  # noqa: T201 — raw stdout for hook JSON
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
    format_: StatusFormat = typer.Option(
        StatusFormat.AUTO,
        "--format",
        "-f",
        help="Output format: auto (default), text, json, or rich",
    ),
) -> None:
    """Workspace readiness check — systems, schedule, focus."""
    format_ = resolve_status_format(format_)
    run_status(format_=format_)


# ── helpers ───────────────────────────────────────────────────────────────────


def _resolve_did(to: str, profile: Profile) -> tuple[str, str]:
    """Resolve a label ('home') or raw DID to ``(did, resolved_label)``.

    Resolution order:
    1. Raw DID (starts with "did:") — returned immediately.
    2. Exact match on label in trusted_keys.
    3. Smart single-recipient fallback: if exactly one trusted key exists, use it
       regardless of the requested label (mirrors ``_resolve_instance`` behaviour).
    4. Otherwise print a descriptive error that lists available labels.
    """
    if to.startswith("did:"):
        return to, to
    key = profile.trusted_keys.get(to)
    if key:
        return key.did, to

    available = list(profile.trusted_keys.keys())

    # Smart default: exactly one trusted key — use it without fuss.
    if len(available) == 1:
        label = available[0]
        return next(iter(profile.trusted_keys.values())).did, label

    if available:
        names = ", ".join(available)
        err.print(
            f"[red]Unknown recipient '{to}'.[/red] "
            f"Available recipients: [cyan]{names}[/cyan].\n"
            "Use a full DID or one of the labels above."
        )
    else:
        err.print(
            f"[red]Unknown recipient '{to}'.[/red]\n"
            "Use a full DID or add with [bold]aya trust[/bold]."
        )
    raise typer.Exit(1)


def _output_json(data: object) -> None:
    """Output data as formatted JSON to console."""
    console.out(json.dumps(data, indent=2, default=str))


def _extract_packet_data(pkt: Packet, profile: Profile) -> dict[str, object]:
    """Extract all packet fields and computed values for reuse across displays."""
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


def _packet_to_dict(
    pkt: Packet, profile: Profile, ingested_set: set[str] | None = None
) -> dict[str, object]:
    """Convert packet to dict for JSON output, optionally marking ingested packets."""
    d = _extract_packet_data(pkt, profile)
    if ingested_set is not None:
        d["ingested"] = pkt.id in ingested_set
    return d


def _show_inbox(
    packets: list[Packet], profile: Profile, ingested_set: set[str] | None = None
) -> None:
    table = Table(title=f"Inbox — {len(packets)} packet(s)", show_lines=True)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Intent")
    table.add_column("From", style="cyan")
    table.add_column("Age", style="dim")
    table.add_column("Type", style="dim")
    table.add_column("Trust")

    for pkt in packets:
        data = _extract_packet_data(pkt, profile)
        trusted_display = "[green]✓[/green]" if data["trusted"] else "[yellow]?[/yellow]"
        already_ingested = ingested_set is not None and pkt.id in ingested_set
        if already_ingested:
            intent: str | Text = Text.assemble((data["intent"], "dim"), (" [ingested]", "dim"))
        else:
            intent = data["intent"]
        table.add_row(
            data["id"][:8],
            intent,
            data["from_label"],
            data["age"],
            data["content_type"],
            trusted_display,
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


# ── Config commands ───────────────────────────────────────────────────────────


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (e.g. notebook_path)"),
    value: str = typer.Argument(..., help="Value to set"),
) -> None:
    """Set a config value in ~/.aya/config.json."""
    set_config_value(key, value)
    console.print(f"[green]✓[/green] {key} = {value}")
    console.print(f"[dim]Saved to {CONFIG_PATH}[/dim]")


@config_app.command("show")
def config_show() -> None:
    """Show current config."""
    config = load_config()
    if not config:
        console.print("[dim]No config set. Use `aya config set <key> <value>`.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Key")
    table.add_column("Value")
    for k, v in sorted(config.items()):
        table.add_row(k, str(v))
    console.print(table)


# ── Clipboard helper ─────────────────────────────────────────────────────────


def _copy_to_clipboard(text: str) -> None:
    xclip = shutil.which("xclip")
    xsel = shutil.which("xsel")
    clip = shutil.which("clip.exe")
    if xclip:
        result = subprocess.run(  # noqa: S603
            [xclip, "-selection", "clipboard"], input=text.encode(), check=False
        )
        if result.returncode == 0:
            console.print("[dim]Copied to clipboard (xclip)[/dim]")
        else:
            err.print(f"[yellow]--copy: xclip failed (exit {result.returncode})[/yellow]")
    elif xsel:
        result = subprocess.run(  # noqa: S603
            [xsel, "--clipboard", "--input"], input=text.encode(), check=False
        )
        if result.returncode == 0:
            console.print("[dim]Copied to clipboard (xsel)[/dim]")
        else:
            err.print(f"[yellow]--copy: xsel failed (exit {result.returncode})[/yellow]")
    elif clip:
        result = subprocess.run([clip], input=text.encode(), check=False)  # noqa: S603
        if result.returncode == 0:
            console.print("[dim]Copied to clipboard (clip.exe)[/dim]")
        else:
            err.print(f"[yellow]--copy: clip.exe failed (exit {result.returncode})[/yellow]")
    else:
        err.print("[yellow]--copy: no clipboard tool found (xclip, xsel, clip.exe)[/yellow]")


# ── Context command ───────────────────────────────────────────────────────────


@app.command("context")
def context_cmd(
    short: bool = typer.Option(False, "--short", help="Compact one-line format"),
    copy: bool = typer.Option(False, "--copy", help="Copy output to clipboard"),
    all_projects: bool = typer.Option(False, "--all", help="Include brainstorming projects"),
    project: str | None = typer.Option(None, "--project", help="Filter to a single project"),
) -> None:
    """Assemble a paste-ready session handshake block from the notebook."""
    notebook_path = get_notebook_path()
    if not notebook_path:
        err.print(
            "[red]notebook_path not set.[/red] "
            "Run: [bold]aya config set notebook_path ~/notebook[/bold]"
        )
        raise typer.Exit(1)
    if not notebook_path.exists():
        err.print(f"[red]Notebook path does not exist:[/red] {notebook_path}")
        raise typer.Exit(1)

    output = build_context_block(
        notebook_path,
        short=short,
        include_brainstorming=all_projects,
        project_filter=project,
    )
    console.print(output)

    if copy:
        _copy_to_clipboard(output)
