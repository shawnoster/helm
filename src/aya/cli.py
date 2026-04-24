"""CLI entry point — aya command."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from contextlib import nullcontext, suppress
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
from aya.config import get_notebook_path, load_config, set_config_value
from aya.context import build_context_block
from aya.identity import Identity, Profile, TrustedKey, _assert_valid_ulid
from aya.ingest import ingest as _ingest
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
from aya.relay import RelayClient, RelayUnreachableError

# Subcommand modules — imported at top-level; each is only invoked when its
# subcommand is actually called, so startup cost is acceptable.
from aya.rewake import emit as rewake_emit
from aya.scheduler import (
    SEVERITY_ACTIONABLE,
    SEVERITY_HEARTBEAT,
    AlertSeverity,
    _display_items,
    _format_watch_alert,
    add_recurring,
    add_reminder,
    add_watch,
    check_due,
    dismiss_alert,
    dismiss_item,
    format_pending,
    format_scheduler_status,
    get_active_watches,
    get_pending,
    get_scheduler_status,
    get_session_crons,
    is_idle,
    list_items,
    parse_due,
    poll_watch,
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


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """aya — personal AI assistant toolkit."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)


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

# ── Config sub-app ────────────────────────────────────────────────────────────

config_app = typer.Typer(
    name="config",
    help="Workspace configuration (notebook path, etc.).",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")

# ── Relay sub-app ─────────────────────────────────────────────────────────────

relay_app = typer.Typer(
    name="relay",
    help="Manage default Nostr relays used by send/send-raw/receive.",
    no_args_is_help=True,
)
app.add_typer(relay_app, name="relay")

console = Console()
err = Console(stderr=True)


# ── Structured error codes ──────────────────────────────────────────────────


class ErrorCode:
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    INSTANCE_NOT_FOUND = "INSTANCE_NOT_FOUND"
    RELAY_UNREACHABLE = "RELAY_UNREACHABLE"
    RELAY_TIMEOUT = "RELAY_TIMEOUT"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    PACKET_NOT_FOUND = "PACKET_NOT_FOUND"
    PEER_NOT_TRUSTED = "PEER_NOT_TRUSTED"
    PAIR_FAILED = "PAIR_FAILED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    AMBIGUOUS_PREFIX = "AMBIGUOUS_PREFIX"
    SEND_FAILED = "SEND_FAILED"
    PAIR_TIMEOUT = "PAIR_TIMEOUT"


# Relay fetch timeout in seconds — applies to commands that stream
# pending packets from the relay for a bounded operation (e.g. aya drop
# prefix resolution). Large or slow relays shouldn't wedge the CLI
# indefinitely; after this many seconds, the fetch is abandoned and the
# caller sees RELAY_TIMEOUT. Chosen to be comfortable for a healthy
# relay + ~100 packets but short enough to feel interactive.
_RELAY_FETCH_TIMEOUT_SECONDS = 30


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


# ── Idempotency helpers ────────────────────────────────────────────────────


def _idempotency_key_hash(key: str) -> str:
    """Hash the idempotency key so raw secrets aren't stored on disk."""
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()


def _check_idempotency(key: str) -> dict | None:
    """Check if an idempotency key was already used. Returns cached result or None."""
    from aya.paths import SENT_CACHE

    if not SENT_CACHE.exists():
        return None
    hashed = _idempotency_key_hash(key)
    try:
        with SENT_CACHE.open() as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            raw = json.loads(f.read())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    entry = raw.get(hashed)
    if not isinstance(entry, dict):
        return None
    try:
        if datetime.fromisoformat(entry["sent_at"]) > datetime.now(UTC) - timedelta(hours=24):
            return entry
    except (KeyError, ValueError, TypeError):
        return None
    return None


def _record_idempotency(key: str, packet_id: str, event_id: str) -> None:
    """Record a sent packet for idempotency dedup. Atomic write with file lock."""
    import tempfile

    from aya.paths import SENT_CACHE

    hashed = _idempotency_key_hash(key)
    SENT_CACHE.parent.mkdir(parents=True, exist_ok=True)

    try:
        with SENT_CACHE.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                raw = json.loads(f.read() or "{}")
                cache = raw if isinstance(raw, dict) else {}
            except json.JSONDecodeError:
                cache = {}

            cache[hashed] = {
                "packet_id": packet_id,
                "event_id": event_id,
                "sent_at": datetime.now(UTC).isoformat(),
            }
            # Prune entries older than 24 hours
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            pruned: dict[str, object] = {}
            for k, v in cache.items():
                if not isinstance(v, dict):
                    continue
                try:
                    if datetime.fromisoformat(str(v.get("sent_at", ""))) > cutoff:
                        pruned[k] = v
                except (ValueError, TypeError):
                    continue
            cache = pruned

            # Atomic write: temp file → Path.replace
            fd, tmp = tempfile.mkstemp(dir=str(SENT_CACHE.parent), suffix=".tmp")
            try:
                encoded = json.dumps(cache, indent=2).encode()
                total = 0
                while total < len(encoded):
                    written = os.write(fd, encoded[total:])
                    total += written
                os.fsync(fd)
                os.close(fd)
                Path(tmp).replace(SENT_CACHE)
                with suppress(OSError):
                    SENT_CACHE.chmod(0o600)
            except Exception:
                with suppress(OSError):
                    os.close(fd)
                with suppress(OSError):
                    Path(tmp).unlink()
                raise
    except OSError:
        logger.debug("Failed to record idempotency key %s", key, exc_info=True)


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


@app.command("mcp-server")
def mcp_server_cmd() -> None:
    """Start the MCP server (stdio transport) for AI tool integration."""
    from aya.mcp_server import main as mcp_main

    asyncio.run(mcp_main())


# ── init ─────────────────────────────────────────────────────────────────────


@app.command()
def init(
    label: str = typer.Option("default", help="Label for this instance (work, home, laptop…)"),
    profile: Path = typer.Option(DEFAULT_PROFILE, help="Path to profile.json"),
    relay: str | None = typer.Option(
        None, help="Override the default relay URL (omit to use the built-in two-relay default)"
    ),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Generate a keypair for this instance and register it in your profile."""
    format_ = resolve_format(format_)
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

    if format_ == OutputFormat.JSON:
        _output_json({"profile_path": str(profile), "did": identity.did, "instance": label})
        raise typer.Exit(0)

    relay_display = relay or ", ".join(p.default_relays)
    console.print(
        Panel.fit(
            f"[bold green]✓ Instance created[/bold green]\n\n"
            f"Instance: [cyan]{label}[/cyan]\n"
            f"DID:      [dim]{identity.did}[/dim]  "
            "[dim italic](ed25519 · identity & signing)[/dim italic]\n"
            f"Nostr:    [dim]{identity.nostr_public_hex[:16]}…[/dim]  "
            "[dim italic](secp256k1 · relay transport)[/dim italic]\n"
            f"Relay:    [cyan]{relay_display}[/cyan]\n\n"
            "[dim]Share your DID with other instances you want to trust.[/dim]\n\n"
            "[bold]Next steps:[/bold]\n"
            "  [cyan]aya schedule install[/cyan]    Set up hooks and cron\n"
            "  [cyan]aya pair --peer <name>[/cyan]  Connect to another instance",
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
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
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
    format_ = resolve_format(format_)
    p = _load_profile(profile)
    p.trusted_keys[peer] = TrustedKey(
        did=did,
        label=peer,
        nostr_pubkey=nostr_pubkey,
    )
    p.save(profile)

    if format_ == OutputFormat.JSON:
        _output_json({"did": did, "label": peer, "nostr_pubkey": nostr_pubkey or None})
        raise typer.Exit(0)

    console.print(
        f"[green]✓[/green] Trusted: [cyan]{peer}[/cyan]  [dim]{did}[/dim]  "
        f"[dim italic](ed25519 · identity & signing)[/dim italic]"
    )
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
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Pack a knowledge packet ready to send.

    To pack and send in one step: aya send --to <label> --intent "..."
    See also: aya send-raw (send a pre-built packet file)
    """
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

    if format_ == OutputFormat.JSON:
        _output_json(json.loads(json_output))
        raise typer.Exit(0)

    if not out:
        sys.stdout.write(json_output)
    else:
        console.print(f"[green]✓[/green] Packet written to [cyan]{out}[/cyan]")


# ── send-raw ──────────────────────────────────────────────────────────────────


@app.command("send-raw")
def send_raw(
    packet_file: Path = typer.Argument(help="Packet JSON file to send"),
    relay: str = typer.Option(None, help="Relay URL (overrides profile default)"),
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    instance: str = typer.Option(
        None, "--instance", help="[deprecated] Use --as instead", hidden=True
    ),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show packet without publishing"),
    idempotency_key: str = typer.Option(
        None,
        "--idempotency-key",
        "-k",
        help="Dedup key — if already sent within 24h, return cached result",
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Send a pre-built packet file to a Nostr relay.

    This sends a pre-built packet file. To compose and send in one step:
      aya send --to <label> --intent "..."

    See also: aya pack (create a packet without sending)
    """
    logger.debug("send-raw: packet_file=%s, as=%s", packet_file, as_)
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
    p = _load_profile(profile)
    local = _resolve_instance(p, as_)

    relay_urls = [relay] if relay else p.default_relays
    packet = Packet.from_json(packet_file.read_text())

    # Validate the packet's signature before publishing. Two failure modes
    # to handle separately:
    #
    # 1. Signature is missing or invalid AND from_did matches the local
    #    instance → user authored this packet but didn't sign it (common
    #    when hand-editing JSON). Re-sign with the local key automatically.
    #
    # 2. Signature is missing or invalid AND from_did is someone else →
    #    refuse. Forwarding an unsigned packet that claims to be from
    #    another sender would let the relay carry forged-looking data.
    #    The user must either get the original sender to sign it, or
    #    rewrite the from_did to match a local instance.
    if not packet.verify_from_did():
        if packet.from_did == local.did:
            packet = packet.sign(local)
            logger.info("Re-signed packet %s with local instance key", packet.id)
            if format_ != OutputFormat.JSON:
                err.print(
                    "[dim]Re-signed packet with local instance key "
                    "(signature was missing or invalid).[/dim]"
                )
        else:
            _emit_error(
                ErrorCode.INVALID_ARGUMENT,
                (
                    f"Packet has missing or invalid signature, and from_did "
                    f"({packet.from_did[:40]}…) does not match local instance "
                    f"({local.did[:40]}…). Refusing to forward an unsigned "
                    f"packet that claims to be from another sender."
                ),
                {"packet_id": packet.id, "from_did": packet.from_did},
            )

    if dry_run:
        _output_json(json.loads(packet.to_json()))
        raise typer.Exit(0)

    if idempotency_key:
        cached = _check_idempotency(idempotency_key)
        if cached:
            if format_ == OutputFormat.JSON:
                _output_json({**cached, "cached": True})
                raise typer.Exit(0)
            console.print(
                f"[dim]Already sent (cached) — packet {cached.get('packet_id', '?')[:8]}[/dim]"
            )
            return

    client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

    # Resolve recipient's Nostr pubkey
    recipient_nostr_pub = _resolve_nostr_pubkey(packet.to_did, p)
    event_id = asyncio.run(client.publish(packet, recipient_nostr_pub, encrypt=packet.encrypted))

    if idempotency_key:
        _record_idempotency(idempotency_key, packet.id, event_id)

    relay_count = len(relay_urls)
    relay_display = relay_urls[0] if relay_count == 1 else f"{relay_urls[0]} (+{relay_count - 1})"

    if format_ == OutputFormat.JSON:
        _output_json({"packet_id": packet.id, "event_id": event_id, "relay": relay_display})
        raise typer.Exit(0)

    console.print(
        f"[green]✓[/green] Sent [cyan]{packet.intent}[/cyan]\n"
        f"  Packet: [dim]{packet.id[:8]}[/dim]  "
        f"Event: [dim]{event_id[:8]}[/dim]  "
        f"Relay: [dim]{relay_display}[/dim]"
    )


# ── send ──────────────────────────────────────────────────────────────────────


@app.command("send")
def send_cmd(
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
    in_reply_to: str = typer.Option(None, "--in-reply-to", help="Packet ID this is a reply to"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show packet without publishing"),
    idempotency_key: str = typer.Option(
        None,
        "--idempotency-key",
        "-k",
        help="Dedup key — if already sent within 24h, return cached result",
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Pack and send in one step — the natural 'pack for home' flow.

    Combines aya pack + aya send-raw: creates the packet, signs it, and
    publishes to the relay. This is the command most users want.
    """
    logger.debug("send: to=%s, intent=%s, as=%s", to, intent, as_)
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

        if in_reply_to:
            if len(in_reply_to) < 8:
                _emit_error(
                    ErrorCode.INVALID_ARGUMENT,
                    "Packet ID for --in-reply-to must be at least 8 characters.",
                )
            packet.in_reply_to = in_reply_to

        # Mark the packet encrypted before signing so the flag is covered by the signature.
        if not no_encrypt:
            packet.encrypted = True

        signed = packet.sign(local)

        if dry_run:
            _output_json(json.loads(signed.to_json()))
            return

        if idempotency_key:
            cached = _check_idempotency(idempotency_key)
            if cached:
                if format_ == OutputFormat.JSON:
                    _output_json({**cached, "cached": True})
                    return
                console.print(
                    f"[dim]Already sent (cached) — packet {cached.get('packet_id', '?')[:8]}[/dim]"
                )
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
            logger.exception("Relay publish failed during send")
            _emit_error(
                ErrorCode.SEND_FAILED,
                "Send failed — event could not be published to relay(s).",
                {"relay": relay_urls[0] if relay_urls else None},
            )

        if idempotency_key:
            _record_idempotency(idempotency_key, signed.id, event_id)

        relay_count = len(relay_urls)
        relay_display = (
            relay_urls[0] if relay_count == 1 else f"{relay_urls[0]} (+{relay_count - 1})"
        )

        if format_ == OutputFormat.JSON:
            _output_json(
                {
                    "packet_id": signed.id,
                    "event_id": event_id,
                    "relay": relay_display,
                    "intent": signed.intent,
                }
            )
            return

        console.print(
            Panel.fit(
                f"[bold green]✓ Sent[/bold green]\n\n"
                f"Intent:  [cyan]{signed.intent}[/cyan]\n"
                f"Packet:  [dim]{signed.id[:8]}[/dim]\n"
                f"Event:   [dim]{event_id[:8]}[/dim]\n"
                f"Relay:   [dim]{relay_display}[/dim]\n"
                f"To:      [dim]{to_label}[/dim]",
                title="aya — send",
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
    idempotency_key: str = typer.Option(
        None,
        "--idempotency-key",
        "-k",
        help="Dedup key — if already sent within 24h, return cached result",
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Acknowledge a received seed packet — sends a reply back to the sender."""
    format_ = resolve_format(format_)

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

        if idempotency_key:
            cached = _check_idempotency(idempotency_key)
            if cached:
                if format_ == OutputFormat.JSON:
                    _output_json({**cached, "cached": True})
                    return
                console.print(
                    f"[dim]Already sent (cached) — ack {cached.get('packet_id', '?')[:8]}[/dim]"
                )
                return

        relay_urls = [relay] if relay else p.default_relays
        client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)
        try:
            event_id = await client.publish(signed, recipient_nostr_pub, encrypt=True)
        except Exception:
            err.print("[yellow]Could not reach relay — ack failed.[/yellow]")
            raise typer.Exit(1) from None

        if idempotency_key:
            _record_idempotency(idempotency_key, signed.id, event_id)

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

        if format_ == OutputFormat.JSON:
            _output_json(
                {
                    "packet_id": signed.id,
                    "event_id": event_id,
                    "in_reply_to": full_packet_id,
                    "to": to_label,
                }
            )
            return

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
    skip_untrusted: bool = typer.Option(
        False,
        "--skip-untrusted",
        help="Skip untrusted packets silently (use with --auto-ingest)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-confirm all prompts (non-interactive mode)"
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress output when inbox is empty (for startup hooks)"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Poll for pending packets and surface them for review."""
    logger.debug(
        "receive: as=%s, auto_ingest=%s, skip_untrusted=%s, quiet=%s",
        as_,
        auto_ingest,
        skip_untrusted,
        quiet,
    )
    if instance is not None and as_ != "default":
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot use --as and --instance together. Use --as only.",
            exit_code=2,
        )
    if instance is not None:
        err.print("[yellow]Warning: --instance is deprecated, use --as instead[/yellow]")
        as_ = instance
    if skip_untrusted and not auto_ingest and not yes:
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "--skip-untrusted requires --auto-ingest or --yes for non-interactive use.",
            exit_code=2,
        )
    format_ = resolve_format(format_)

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, as_, quiet=quiet)

        relay_urls = [relay] if relay else p.default_relays
        client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

        # Fetch pending packets for this instance; ingested_ids is the authoritative
        # dedup mechanism and filters already-seen packets below.  No `since` filter
        # is applied — the relay's default 7-day TTL window is the correct bound, and
        # a cursor derived from last_checked can permanently exclude packets that
        # arrived before the cursor but were never ingested (see issue #246).
        packets: list[Packet] = []
        try:
            async for packet in client.fetch_pending():
                packets.append(packet)
        except Exception:
            logger.exception("Relay fetch failed during receive")
            if not quiet:
                err.print("[yellow]Could not reach relay — skipping relay fetch.[/yellow]")
            if format_ == OutputFormat.JSON:
                _output_json({"packets": []})
            return

        # Record last poll time per relay for status display.
        now_check_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for url in relay_urls:
            p.last_checked[url] = now_check_iso

        if not packets:
            if format_ == OutputFormat.JSON:
                _output_json({"packets": []})
            elif not quiet:
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
            if format_ == OutputFormat.JSON:
                _output_json({"packets": []})
            elif not quiet:
                console.print("[dim]No valid packets.[/dim]")
            p.save(profile)
            return

        if format_ != OutputFormat.JSON:
            _show_inbox(verified, p)

        received_summaries: list[dict[str, object]] = []
        for packet in verified:
            trusted = p.is_trusted(packet.from_did)
            trust_label = "[green]trusted[/green]" if trusted else "[yellow]unknown sender[/yellow]"

            now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if auto_ingest and trusted:
                _assert_valid_ulid(packet.id)
                _ingest(packet, quiet=format_ == OutputFormat.JSON)
                p.ingested_ids.append(
                    {
                        "id": packet.id,
                        "ingested_at": now_iso,
                        "from_did": packet.from_did,
                    }
                )
                received_summaries.append(
                    {
                        "id": packet.id,
                        "intent": packet.intent,
                        "from": packet.from_did,
                        "ingested": True,
                    }
                )
                continue

            if skip_untrusted and not trusted:
                logger.debug(
                    "Skipping untrusted packet %s from %s",
                    packet.id[:8],
                    packet.from_did[:30],
                )
                received_summaries.append(
                    {
                        "id": packet.id,
                        "intent": packet.intent,
                        "from": packet.from_did,
                        "ingested": False,
                        "skipped": True,
                    }
                )
                if format_ != OutputFormat.JSON and not quiet:
                    err.print(f"[dim]Skipped untrusted: {packet.id[:8]} ({packet.intent})[/dim]")
                continue

            ingest = yes or typer.confirm(
                f"\nIngest '{packet.intent}' ({trust_label})?",
                default=trusted,
            )
            if ingest:
                _assert_valid_ulid(packet.id)
                _ingest(packet, quiet=format_ == OutputFormat.JSON)
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
                received_summaries.append(
                    {
                        "id": packet.id,
                        "intent": packet.intent,
                        "from": packet.from_did,
                        "ingested": True,
                    }
                )
            else:
                received_summaries.append(
                    {
                        "id": packet.id,
                        "intent": packet.intent,
                        "from": packet.from_did,
                        "ingested": False,
                    }
                )

        if format_ == OutputFormat.JSON:
            _output_json({"packets": received_summaries})

        if format_ != OutputFormat.JSON and not quiet and auto_ingest:
            ingested_count = sum(1 for s in received_summaries if s.get("ingested"))
            skipped_count = sum(1 for s in received_summaries if s.get("skipped"))
            total = len(received_summaries)
            if total > 0:
                parts = [f"[green]✓[/green] Ingested {ingested_count} of {total} packet(s)"]
                if skipped_count:
                    parts.append(f"({skipped_count} untrusted, skipped)")
                declined = total - ingested_count - skipped_count
                if declined:
                    parts.append(f"({declined} declined)")
                console.print("  ".join(parts))

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
        dropped_set = set(p.dropped_ids)

        # Filter dropped packets out of both default and --all views.
        # Dropped IDs are explicitly user-marked-ignore (bad-sig, spam, etc.)
        # and should never resurface regardless of ingested status.
        all_packets = [pkt for pkt in all_packets if pkt.id not in dropped_set]

        new_packets = [pkt for pkt in all_packets if pkt.id not in ingested_set]
        display_packets = all_packets if show_all else new_packets

        if format_ == OutputFormat.JSON:
            ingested_for_json = ingested_set if show_all else None
            packet_dicts = [_packet_to_dict(pkt, p, ingested_for_json) for pkt in display_packets]
            _output_json({"packets": packet_dicts})
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
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
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
    format_ = resolve_format(format_)
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

        if format_ == OutputFormat.JSON:
            _output_json({"status": "paired", "peer_label": peer, "peer_did": trusted.did})
            raise typer.Exit(0)

        lines = [
            "[bold green]✓ Paired![/bold green]\n",
            f"Trusted: [cyan]{peer}[/cyan]",
            f"DID:     [dim]{trusted.did}[/dim]"
            "  [dim italic](ed25519 · identity & signing)[/dim italic]",
        ]
        if trusted.nostr_pubkey:
            lines.append(
                f"Nostr:   [dim]{trusted.nostr_pubkey[:16]}…[/dim]  "
                "[dim italic](secp256k1 · relay transport)[/dim italic]"
            )
        console.print(
            Panel.fit(
                "\n".join(lines),
                title="aya — pair (joined)",
            )
        )

    else:
        # ── Initiator mode ───────────────────────────────────────────
        pairing_code = generate_code()
        code_h = hash_code(pairing_code)

        # Publish the request — embed our own label so the joiner knows what to call us
        if format_ != OutputFormat.JSON:
            console.print("[dim]Publishing pairing request…[/dim]")
        request_event_id = asyncio.run(publish_pair_request(local, local.label, code_h, relay_urls))

        # Show the code — user reads this aloud or types it on the other machine
        if format_ == OutputFormat.JSON:
            _output_json(
                {
                    "status": "awaiting_peer",
                    "pairing_code": pairing_code,
                    "local_did": local.did,
                    "peer_label": peer,
                    "relay": relay_urls[0] if relay_urls else None,
                }
            )
        if format_ != OutputFormat.JSON:
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
        if format_ != OutputFormat.JSON:
            ctx_mgr = console.status("[bold cyan]Waiting for the other peer…[/bold cyan]")
        else:
            ctx_mgr = nullcontext()
        with ctx_mgr:
            trusted = asyncio.run(
                poll_for_pair_response(relay_urls, local.nostr_public_hex, request_event_id)
            )

        if trusted is None:
            if format_ == OutputFormat.JSON:
                _emit_error(ErrorCode.PAIR_TIMEOUT, "Pairing timed out")
            console.print(
                "[bold yellow]Pairing timed out.[/bold yellow] "
                "Run [bold]aya pair[/bold] again for a new code."
            )
            raise typer.Exit(1)

        trusted.label = peer
        p.trusted_keys[peer] = trusted
        p.save(profile)

        if format_ == OutputFormat.JSON:
            _output_json({"status": "paired", "peer_label": peer, "peer_did": trusted.did})
            raise typer.Exit(0)

        lines = [
            "[bold green]✓ Paired![/bold green]\n",
            f"Trusted: [cyan]{peer}[/cyan]",
            f"DID:     [dim]{trusted.did}[/dim]"
            "  [dim italic](ed25519 · identity & signing)[/dim italic]",
        ]
        if trusted.nostr_pubkey:
            lines.append(
                f"Nostr:   [dim]{trusted.nostr_pubkey[:16]}…[/dim]  "
                "[dim italic](secp256k1 · relay transport)[/dim italic]"
            )
        console.print(
            Panel.fit(
                "\n".join(lines),
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
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Add a one-shot reminder."""
    format_ = resolve_format(format_)
    if dry_run:
        due_dt = parse_due(due)
        preview = {
            "type": "reminder",
            "status": "pending",
            "message": message,
            "tags": [t.strip() for t in tag.split(",") if t.strip()] if tag else [],
            "due_at": due_dt.isoformat(),
        }
        _output_json({"item": preview})
        raise typer.Exit(0)
    item = add_reminder(message, due, tag)
    if format_ == OutputFormat.JSON:
        _output_json({"item": item})
        raise typer.Exit(0)
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
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Add a condition-based watch."""
    format_ = resolve_format(format_)
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
        _output_json({"item": preview})
        raise typer.Exit(0)
    try:
        item = add_watch(provider, target, message, tag, condition, interval, remove_when)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if format_ == OutputFormat.JSON:
        _output_json({"item": item})
        raise typer.Exit(0)
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
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Add a persistent recurring session job (session_required cron)."""
    format_ = resolve_format(format_)
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
        _output_json({"item": preview})
        raise typer.Exit(0)
    try:
        item = add_recurring(message, cron, prompt, tag, idle_back_off, only_during)
    except ValueError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    if format_ == OutputFormat.JSON:
        _output_json({"item": item})
        raise typer.Exit(0)
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
        _output_json({"items": items})
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
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Dismiss a scheduled item or alert."""
    format_ = resolve_format(format_)
    try:
        item = dismiss_item(item_id)
    except ValueError:
        try:
            item = dismiss_alert(item_id)
        except ValueError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    if format_ == OutputFormat.JSON:
        _output_json({"dismissed": item["id"], "status": "dismissed"})
        raise typer.Exit(0)
    console.print(f"[green]✓[/green] Dismissed {item['id'][:8]} — {item['message'][:60]}")


@schedule_app.command("snooze")
def schedule_snooze(
    item_id: str = typer.Argument(help="Item ID (prefix match ok)"),
    until: str = typer.Option(
        ..., "--until", "-u", help="Snooze until: 'in 1 hour', 'tomorrow 9am'"
    ),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Snooze a reminder."""
    format_ = resolve_format(format_)
    try:
        item, snooze_until = snooze_item(item_id, until)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if format_ == OutputFormat.JSON:
        _output_json({"snoozed": item["id"], "until": snooze_until.isoformat()})
        raise typer.Exit(0)
    console.print(
        f"💤 Snoozed {item['id'][:8]} until {snooze_until.strftime('%a %b %d, %I:%M %p')}"
    )


@schedule_app.command("poll")
def schedule_poll(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress output on no changes"),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Run one poll cycle (legacy — use 'tick' instead)."""
    format_ = resolve_format(format_)
    run_poll(quiet=quiet)
    if format_ == OutputFormat.JSON:
        _output_json({"status": "ok"})
        raise typer.Exit(0)


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
        active = result.get("session_active")
        session_note = " (session active — delivery deferred)" if active else ""
        console.print(
            f"[dim]Tick complete. Claims swept: {result['claims_swept']}{session_note}[/dim]"
        )


@schedule_app.command("pending")
def schedule_pending(
    all_severities: bool = typer.Option(
        False, "--all", "-a", help="Show all alerts including info/heartbeat"
    ),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Show pending items for this session — alerts to deliver + session crons.

    SessionStart hook entry point:
        aya scheduler pending --format text
    """
    format_ = resolve_format(format_)
    if format_ == OutputFormat.JSON:
        min_severity: AlertSeverity = SEVERITY_HEARTBEAT if all_severities else SEVERITY_ACTIONABLE
        pending = get_pending(min_severity=min_severity)
        _output_json(pending)
    else:
        # Always fetch all severities for text output so format_pending
        # can summarize queued non-actionable alerts without --all.
        pending = get_pending(min_severity=SEVERITY_HEARTBEAT)
        console.print(format_pending(pending, show_all=all_severities))


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
        _output_json({"alerts": unseen})
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
    tick_interval: str | None = typer.Option(
        None,
        "--tick-interval",
        help=(
            "How often the scheduler ticks (e.g. '30s', '1m', '5m', '1h'). "
            "Sub-minute intervals generate multi-line crontab entries with "
            "sleep offsets. Persisted to ~/.aya/config.json so re-runs without "
            "this flag preserve the chosen value. Default: 5m on first install."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace any existing aya cron entries instead of treating them as already-installed.",
    ),
) -> None:
    """Install scheduler integrations — system crontab + Claude Code hooks."""
    from aya.config import load_config, set_config_value
    from aya.install import DEFAULT_TICK_INTERVAL

    # Resolve the effective tick interval: explicit flag > persisted config > default.
    if tick_interval is None:
        cfg = load_config()
        tick_interval = cfg.get("tick_interval", DEFAULT_TICK_INTERVAL)

    result = install_scheduler(dry_run=dry_run, tick_interval=tick_interval, force=force)

    if result.errors:
        for e in result.errors:
            err.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    # Persist the chosen interval (only on a successful real install,
    # not dry-run or already-present cases — those don't change state).
    if not dry_run and result.cron_installed:
        set_config_value("tick_interval", tick_interval)

    prefix = "[dim](dry run)[/dim] " if dry_run else ""

    if result.cron_already_present:
        console.print(
            f"  {prefix}[dim]Crontab:[/dim] already installed "
            f"[dim](use --force to replace with --tick-interval {tick_interval})[/dim]"
        )
    elif result.cron_installed:
        console.print(f"  {prefix}[green]Crontab:[/green] installed (tick={tick_interval})")
        for line in result.cron_lines:
            console.print(f"    [dim]{line}[/dim]")

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
def hook_crons(
    reset: bool = typer.Option(
        False,
        "--reset",
        help=(
            "Clear the per-session registered-crons tracker before emitting. "
            "Use at SessionStart so a fresh session re-registers everything."
        ),
    ),
    event: str = typer.Option(
        "SessionStart",
        "--event",
        help=(
            "Hook event name to use in the emitted hookSpecificOutput JSON. "
            "Defaults to SessionStart for the SessionStart hook entry; "
            "PostToolUse hook should pass --event PostToolUse so the "
            "additionalContext is delivered after the tool result."
        ),
    ),
) -> None:
    """Output CronCreate instructions for Claude Code hooks.

    Reads active session crons from the scheduler and emits a JSON
    hookSpecificOutput block that tells Claude Code to register them
    via CronCreate. Exits silently when there are no NEW crons to
    register.

    Tracks already-registered cron IDs in
    ``~/.aya/session_registered_crons.json`` so a follow-up call only
    emits crons that haven't been seen yet in the current session. This
    is what makes mid-session ``aya schedule recurring`` calls actually
    fire — the PostToolUse hook re-runs ``aya hook crons`` on the next
    tool boundary, the new cron isn't in the tracker, and it gets
    registered just like a SessionStart cron would.

    Unlike get_pending(), this does NOT claim alerts — safe to run before
    ``aya schedule pending`` without consuming alerts.

    Usage in ~/.claude/settings.json:
        SessionStart: ``aya hook crons --reset``
        PostToolUse:  ``aya hook crons --event PostToolUse``
    """
    from aya.scheduler import register_new_cron_ids, reset_registered_cron_ids

    if reset:
        reset_registered_cron_ids()

    crons, _suppressed = get_session_crons()
    if not crons:
        return

    # Atomically merge candidate cron IDs into the per-session tracker
    # under a single file lock. The returned set is the IDs that were
    # NOT previously in the tracker — i.e. the ones we should emit.
    # Two concurrent processes racing on the same cron will both call
    # register_new_cron_ids; only the lock winner sees the IDs as new.
    # The other gets an empty set back and emits nothing. This prevents
    # duplicate CronCreate registrations when Claude Code dispatches
    # parallel tool calls and the PostToolUse hook fires concurrently.
    candidate_ids = {c.get("id", "") for c in crons if c.get("id")}
    new_ids = register_new_cron_ids(candidate_ids)
    if not new_ids:
        return

    new_crons = [c for c in crons if c.get("id", "") in new_ids]

    # Emit one hookSpecificOutput per new cron so each gets its own system
    # reminder and can't be truncated when multiple crons are bundled.
    for c in new_crons:
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
                        "hookEventName": event,
                        "additionalContext": context,
                    }
                }
            )
        )


# ── ci ────────────────────────────────────────────────────────────────────────


@hook_app.command("watch")
def hook_watch() -> None:
    """Poll all due scheduler watches and emit asyncRewake on change.

    Replaces the old ``aya ci watch`` command.  Registered as a single
    PostToolUse asyncRewake hook — handles CI checks, GitHub PR watches,
    Jira watches, and any future provider.

    On ``git push``, auto-creates a transient ``ci-checks`` watch that
    polls PR checks and wakes Claude if they fail or time out.

    Reads Claude hook JSON from stdin.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = {}

    exit_code = _hook_watch_impl(payload)
    raise typer.Exit(exit_code)


def _hook_watch_impl(payload: dict) -> int:
    """Core logic for hook watch — testable without typer.Exit."""
    from aya.scheduler.storage import (
        _alerts_file,
        _atomic_write,
        _file_lock,
        _load_alerts_unlocked,
        _load_items_unlocked,
        _scheduler_file,
    )
    from aya.scheduler.types import AlertDetails, _alerts_data, _scheduler_data

    now = datetime.now().astimezone()
    rewake_messages: list[str] = []

    # ── Step 1: detect git push → create ci-checks watch ────────────────
    command = payload.get("tool_input", {}).get("command", "")
    if "git push" in command:
        _maybe_create_ci_watch()

    # ── Step 2: poll all due watches ────────────────────────────────────
    with _file_lock():
        items = _load_items_unlocked()
        alerts = _load_alerts_unlocked()
        items_modified = False
        alerts_modified = False

        for item in items:
            if item.get("type") != "watch" or item.get("status") != "active":
                continue

            # Respect poll interval
            last = item.get("last_checked_at")
            interval = item.get("poll_interval_minutes", 30)
            if last:
                next_check = datetime.fromisoformat(last) + timedelta(minutes=interval)
                if now < next_check:
                    continue

            new_state, changed = poll_watch(item)
            if new_state is None:
                continue

            item["last_checked_at"] = now.isoformat()
            item["last_state"] = new_state
            items_modified = True

            if changed:
                alert_msg = _format_watch_alert(item, new_state)
                from aya.scheduler.display import _create_alert as create_alert

                alert = create_alert(
                    source_item_id=item["id"],
                    message=alert_msg,
                    details=AlertDetails(**new_state),  # type: ignore[arg-type]
                    now=now,
                )
                alerts.append(alert)
                alerts_modified = True
                rewake_messages.append(alert_msg)

            from aya.scheduler.providers import _evaluate_auto_remove

            if _evaluate_auto_remove(item, new_state):
                item["status"] = "dismissed"
                items_modified = True

        if items_modified:
            _atomic_write(_scheduler_file(), _scheduler_data(items))
        if alerts_modified:
            _atomic_write(_alerts_file(), _alerts_data(alerts))

    # ── Step 3: emit single asyncRewake with all changes ────────────────
    if rewake_messages:
        rewake_emit(" | ".join(rewake_messages))
        return 2
    return 0


def _maybe_create_ci_watch() -> None:
    """If this was a git push to a GitHub PR branch, create a ci-checks watch."""
    timeout = 15

    def _run_cmd(cmd: list[str]) -> tuple[int, str]:
        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            return result.returncode, (result.stdout or "").strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return 127, ""

    rc, remote = _run_cmd(["git", "remote", "get-url", "origin"])
    if rc != 0 or "github.com" not in remote:
        return

    rc, branch = _run_cmd(["git", "branch", "--show-current"])
    if rc != 0 or not branch or branch in ("main", "master"):
        return

    # Parse owner/repo via gh CLI (handles all URL formats including dots in names)
    rc, name_with_owner = _run_cmd(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    )
    if rc != 0 or "/" not in name_with_owner:
        return
    owner, repo = name_with_owner.split("/", 1)

    # Check if PR exists for this branch
    rc, pr_num = _run_cmd(
        [
            "gh",
            "pr",
            "view",
            branch,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "number",
            "-q",
            ".number",
        ]
    )
    if rc != 0 or not pr_num:
        return

    # Check if we already have an active ci-checks watch for this PR
    existing = get_active_watches()
    for w in existing:
        if w.get("provider") != "ci-checks":
            continue
        cfg = w.get("watch_config", {})
        if cfg.get("owner") == owner and cfg.get("repo") == repo and cfg.get("pr") == int(pr_num):
            return  # already watching

    add_watch(
        provider="ci-checks",
        target=f"{owner}/{repo}#{pr_num}",
        message=f"CI checks on PR #{pr_num} ({owner}/{repo}, branch: {branch})",
        condition="checks_failed",
        interval=1,
        remove_when="checks_complete",
    )


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


# ── show ──────────────────────────────────────────────────────────────────────


@app.command()
def show(
    packet_id: str = typer.Argument(help="Packet ID or prefix (min 8 chars)"),
    format_: OutputFormat = typer.Option(OutputFormat.AUTO, "--format", "-f", help="Output format"),
) -> None:
    """Show the content of a previously ingested packet."""
    from aya.paths import PACKETS_DIR

    format_ = resolve_format(format_)

    if len(packet_id) < 8:
        _emit_error(ErrorCode.INVALID_ARGUMENT, "Packet ID prefix must be at least 8 characters.")

    # Find matching packet files
    if not PACKETS_DIR.exists():
        _emit_error(ErrorCode.PACKET_NOT_FOUND, "No ingested packets found.")

    matches = [f for f in PACKETS_DIR.glob("*.json") if f.stem.startswith(packet_id)]
    if not matches:
        _emit_error(
            ErrorCode.PACKET_NOT_FOUND,
            f"Packet '{packet_id}' not found.",
            {"packet_id": packet_id},
        )
    if len(matches) > 1:
        _emit_error(
            ErrorCode.AMBIGUOUS_PREFIX,
            f"Ambiguous prefix — matches {len(matches)} packets.",
            {"packet_id": packet_id, "matches": len(matches)},
        )

    from aya.packet import Packet

    packet = Packet.from_json(matches[0].read_text())

    if format_ == OutputFormat.JSON:
        _output_json(json.loads(packet.to_json()))
        raise typer.Exit(0)

    # Rich text display
    console.print(
        Panel(
            str(packet.content),
            title=f"{packet.intent}  ·  {packet.id[:8]}",
            subtitle=f"from {packet.from_did[:30]}…  ·  {packet.sent_at[:10]}",
        )
    )


# ── read ──────────────────────────────────────────────────────────────────────


def _extract_body(content: object, content_type: ContentType | None = None) -> str:
    """Extract a packet body string from raw content for display.

    For seed packets (`application/aya-seed`), content is a dict with
    ``opener``, ``context_summary``, and ``open_questions``. For content
    packets (markdown, text), content is a plain string. JSON dict content
    that isn't a seed is serialized with ``json.dumps``. Anything else
    falls back to ``str(content)``.
    """
    lines: list[str] = []
    if isinstance(content, dict):
        if content_type == ContentType.SEED:
            opener = content.get("opener")
            if opener:
                lines.append(str(opener))
            context_summary = content.get("context_summary")
            if context_summary:
                if lines:
                    lines.append("")
                lines.append("--- context ---")
                lines.append(str(context_summary))
            open_questions = content.get("open_questions") or []
            if open_questions:
                if lines:
                    lines.append("")
                lines.append("--- open questions ---")
                for q in open_questions:
                    lines.append(f"- {q}")
        else:
            lines.append(json.dumps(content, indent=2, default=str))
    elif isinstance(content, str):
        lines.append(content)
    else:
        lines.append(str(content))
    return "\n".join(lines)


@app.command()
def read(
    packet_id: str = typer.Argument(help="Packet ID or prefix (min 8 chars)"),
    meta: bool = typer.Option(False, "--meta", help="Also print packet metadata header"),
    format_: OutputFormat = typer.Option(OutputFormat.AUTO, "--format", "-f", help="Output format"),
) -> None:
    """Read a previously ingested packet — extracts body without dumping the envelope JSON.

    For seed packets, prints the opener (and context_summary, open_questions
    if present). For content packets, prints the content directly. Use
    ``--meta`` to also print id/from/sent_at/intent header.
    """
    from aya.paths import PACKETS_DIR

    format_ = resolve_format(format_)

    if len(packet_id) < 8:
        _emit_error(ErrorCode.INVALID_ARGUMENT, "Packet ID prefix must be at least 8 characters.")

    if not PACKETS_DIR.exists():
        _emit_error(ErrorCode.PACKET_NOT_FOUND, "No ingested packets found.")

    matches = [f for f in PACKETS_DIR.glob("*.json") if f.stem.startswith(packet_id)]
    if not matches:
        _emit_error(
            ErrorCode.PACKET_NOT_FOUND,
            f"Packet '{packet_id}' not found.",
            {"packet_id": packet_id},
        )
    if len(matches) > 1:
        _emit_error(
            ErrorCode.AMBIGUOUS_PREFIX,
            f"Ambiguous prefix — matches {len(matches)} packets.",
            {"packet_id": packet_id, "matches": len(matches)},
        )

    from aya.packet import Packet

    packet = Packet.from_json(matches[0].read_text())

    if format_ == OutputFormat.JSON:
        # In JSON output mode, non-seed dict content (e.g. application/json
        # packets) is passed through as a structured value rather than
        # stringified. Callers that ``jq`` or ``python -c 'json.load'`` over
        # the output get a real object, not a string containing pretty-printed
        # JSON. Seed-shape dicts still go through _extract_body so the
        # ``body`` field stays a readable string (opener + context + qs).
        body_value: object
        if isinstance(packet.content, dict) and packet.content_type != ContentType.SEED:
            body_value = packet.content
        else:
            body_value = _extract_body(packet.content, packet.content_type)

        result: dict[str, object] = {"id": packet.id, "body": body_value}
        if meta:
            result["from"] = packet.from_did
            result["sent_at"] = packet.sent_at
            result["intent"] = packet.intent
            result["in_reply_to"] = getattr(packet, "in_reply_to", None)
        _output_json(result)
        raise typer.Exit(0)

    # Text mode — always render as a string via _extract_body.
    body = _extract_body(packet.content, packet.content_type)
    if meta:
        console.print(f"[bold]{packet.intent}[/bold]  ·  {packet.id[:12]}")
        console.print(f"[dim]from {packet.from_did[:30]}…  ·  {packet.sent_at[:16]}[/dim]")
        console.print()
    console.print(body, markup=False, highlight=False)


# ── drop ──────────────────────────────────────────────────────────────────────


@app.command()
def drop(
    packet_id: str = typer.Argument(help="Packet ID or prefix (min 8 chars) to drop from inbox"),
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    relay: str | None = typer.Option(None, help="Relay URL (overrides profile default)"),
    profile: Path = typer.Option(DEFAULT_PROFILE),
    format_: OutputFormat = typer.Option(OutputFormat.AUTO, "--format", "-f", help="Output format"),
) -> None:
    """Drop a packet from inbox view so it stops re-surfacing on each poll.

    Useful for bad-signature packets, spam, or any packet you want to ignore
    permanently. The drop is local to this profile — the packet stays on the
    relay until its natural expiry. Prefix matching resolves the full ID by
    looking at ingested packets first, then querying the relay if needed.
    """
    format_ = resolve_format(format_)

    if len(packet_id) < 8:
        _emit_error(ErrorCode.INVALID_ARGUMENT, "Packet ID prefix must be at least 8 characters.")

    async def _run() -> None:
        p = _load_profile(profile)
        local = _resolve_instance(p, as_)

        # Try to resolve full ID from ingested_ids first (cheap, no network).
        ingested_matches = [
            entry["id"] for entry in p.ingested_ids if entry["id"].startswith(packet_id)
        ]

        # Also check existing dropped_ids so the user can re-confirm a drop
        # using a prefix without hitting the relay.
        dropped_matches = [pid for pid in p.dropped_ids if pid.startswith(packet_id)]

        local_matches = list(set(ingested_matches + dropped_matches))

        if len(local_matches) == 1:
            full_id = local_matches[0]
        elif len(local_matches) > 1:
            _emit_error(
                ErrorCode.AMBIGUOUS_PREFIX,
                f"Ambiguous prefix '{packet_id}' — matches {len(local_matches)} known packets.",
                {"packet_id": packet_id, "matches": len(local_matches)},
            )
            return  # unreachable, _emit_error raises
        else:
            # Fall back to the relay for packets that were never ingested
            # (bad-sig, spam, untrusted senders that aya skipped). Wrap
            # the stream in asyncio.timeout() so a slow or large relay
            # can't wedge the command indefinitely — after
            # _RELAY_FETCH_TIMEOUT_SECONDS we abandon the fetch and
            # report RELAY_TIMEOUT so the caller can retry with a full
            # ID or a different --relay.
            relay_urls = [relay] if relay else p.default_relays
            client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)
            relay_matches: list[str] = []
            try:
                async with asyncio.timeout(_RELAY_FETCH_TIMEOUT_SECONDS):
                    async for pkt in client.fetch_pending():
                        if pkt.id.startswith(packet_id):
                            relay_matches.append(pkt.id)
                            if len(relay_matches) > 1:
                                break  # ambiguous — stop early
            except TimeoutError:
                _emit_error(
                    ErrorCode.RELAY_TIMEOUT,
                    (
                        f"Relay fetch timed out after {_RELAY_FETCH_TIMEOUT_SECONDS}s "
                        f"while resolving prefix '{packet_id}'. The relay may be slow "
                        f"or the inbox very large — retry with the full packet ID, "
                        f"or use --relay to point at a different relay."
                    ),
                    {
                        "packet_id": packet_id,
                        "timeout_seconds": _RELAY_FETCH_TIMEOUT_SECONDS,
                    },
                )
                return  # unreachable
            except RelayUnreachableError:
                _emit_error(
                    ErrorCode.RELAY_UNREACHABLE,
                    (
                        f"Could not connect to relay while resolving prefix '{packet_id}'. "
                        "Check your network connection or use --relay to point at a "
                        "different relay."
                    ),
                    {"packet_id": packet_id},
                )
                return  # unreachable

            if not relay_matches:
                _emit_error(
                    ErrorCode.PACKET_NOT_FOUND,
                    f"No packet matching '{packet_id}' found locally or on relay. "
                    "Use the full packet ID if it's already past relay retention.",
                    {"packet_id": packet_id},
                )
                return  # unreachable
            if len(relay_matches) > 1:
                _emit_error(
                    ErrorCode.AMBIGUOUS_PREFIX,
                    f"Ambiguous prefix '{packet_id}' — matches {len(relay_matches)} relay packets.",
                    {"packet_id": packet_id, "matches": len(relay_matches)},
                )
                return  # unreachable

            full_id = relay_matches[0]

        already_dropped = full_id in p.dropped_ids
        if not already_dropped:
            p.dropped_ids.append(full_id)
            p.save(profile)

        if format_ == OutputFormat.JSON:
            _output_json(
                {
                    "dropped": full_id,
                    "already_dropped": already_dropped,
                }
            )
        else:
            if already_dropped:
                console.print(f"[dim]Packet[/dim] {full_id[:12]}… [dim]was already dropped.[/dim]")
            else:
                console.print(f"[green]Dropped[/green] {full_id[:12]}…")

    asyncio.run(_run())


# ── packets ───────────────────────────────────────────────────────────────────


@app.command()
def packets(
    limit: int = typer.Option(20, "--limit", "-n", help="Max packets to show"),
    format_: OutputFormat = typer.Option(OutputFormat.AUTO, "--format", "-f", help="Output format"),
) -> None:
    """List recently ingested packets."""
    from aya.paths import PACKETS_DIR

    format_ = resolve_format(format_)
    if limit < 1:
        limit = 20

    if not PACKETS_DIR.exists():
        if format_ == OutputFormat.JSON:
            _output_json({"packets": []})
            raise typer.Exit(0)
        console.print("[dim]No ingested packets found.[/dim]")
        return

    from aya.packet import Packet

    def _safe_mtime(f: Path) -> float:
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    files = sorted(PACKETS_DIR.glob("*.json"), key=_safe_mtime, reverse=True)[:limit]
    items: list[dict[str, str]] = []
    for f in files:
        try:
            pkt = Packet.from_json(f.read_text())
            items.append(
                {
                    "id": pkt.id,
                    "intent": pkt.intent,
                    "from": pkt.from_did,
                    "sent_at": pkt.sent_at,
                    "content_type": pkt.content_type,
                }
            )
        except Exception:  # noqa: S112
            continue

    if format_ == OutputFormat.JSON:
        _output_json({"packets": items})
        raise typer.Exit(0)

    # Rich table display
    table = Table(title=f"Ingested Packets ({len(items)})")
    table.add_column("ID", width=10)
    table.add_column("Intent")
    table.add_column("From", width=8)
    table.add_column("Sent")
    for item in items:
        from_display = item["from"][:30] + "…" if len(item["from"]) > 30 else item["from"]
        table.add_row(item["id"][:8], item["intent"], from_display, item["sent_at"][:10])
    console.print(table)


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


# ── Relay subcommands ────────────────────────────────────────────────────────


def _load_profile_for_relay(profile_path: Path) -> Profile:
    """Load a profile for relay commands using the standard profile loader."""
    return _load_profile(profile_path)


def _validate_relay_url(url: str) -> None:
    """Reject anything that isn't a valid ws:// or wss:// URL with a non-empty host.

    Rejects whitespace anywhere in the URL, not just leading/trailing — urlparse
    happily accepts 'wss://relay .example' with a space in the netloc, which
    would later fail at websockets.connect() rather than at the CLI boundary.
    """
    parsed = urllib.parse.urlparse(url)
    has_whitespace = any(c.isspace() for c in url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or has_whitespace:
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            f"Relay URL must be a valid wss:// or ws:// address with a hostname (got {url!r}).",
            context={"url": url},
            exit_code=2,
        )


@relay_app.command("list")
def relay_list(
    profile: Path = typer.Option(DEFAULT_PROFILE, help="Path to profile.json"),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Show the current default relays (in polling order)."""
    format_ = resolve_format(format_)
    p = _load_profile_for_relay(profile)
    relays = list(p.default_relays)

    if format_ == OutputFormat.JSON:
        _output_json({"relays": relays, "count": len(relays)})
        return

    if not relays:
        console.print("[dim]No relays configured.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Relay URL")
    for i, url in enumerate(relays, start=1):
        table.add_row(str(i), url)
    console.print(table)


@relay_app.command("add")
def relay_add(
    url: str = typer.Argument(..., help="Relay URL (wss://… or ws://…)"),
    first: bool = typer.Option(
        False, "--first", help="Prepend instead of append (makes this the primary relay)"
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE, help="Path to profile.json"),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Add a relay to default_relays. Duplicates are a no-op."""
    format_ = resolve_format(format_)
    _validate_relay_url(url)
    p = _load_profile_for_relay(profile)

    relays = list(p.default_relays)
    if url in relays:
        if format_ == OutputFormat.JSON:
            _output_json({"ok": True, "already_present": True, "relays": relays})
            return
        console.print(f"[dim]{url} is already in default_relays — no change.[/dim]")
        return

    if first:
        relays.insert(0, url)
    else:
        relays.append(url)
    p.default_relays = relays
    p.save(profile)

    if format_ == OutputFormat.JSON:
        _output_json(
            {
                "ok": True,
                "added": url,
                "position": "first" if first else "last",
                "relays": relays,
            }
        )
        return
    role = "primary" if first else "fallback"
    console.print(f"[green]✓[/green] Added [cyan]{url}[/cyan] ({role})")
    console.print(f"[dim]Saved to {profile}[/dim]")


@relay_app.command("remove")
def relay_remove(
    target: str = typer.Argument(..., help="Relay URL or 1-based list index to remove"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow removing the last remaining relay. Note: Profile.load() auto-refills an "
        "empty list with the bootstrap defaults on next load, so this effectively resets "
        "to defaults.",
    ),
    profile: Path = typer.Option(DEFAULT_PROFILE, help="Path to profile.json"),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Remove a relay by URL or 1-based index."""
    format_ = resolve_format(format_)
    p = _load_profile_for_relay(profile)
    relays = list(p.default_relays)

    # Resolve target: integer index or URL match
    removed: str | None = None
    if target.isdigit():
        idx = int(target) - 1
        if idx < 0 or idx >= len(relays):
            _emit_error(
                ErrorCode.INVALID_ARGUMENT,
                f"Index {target} out of range (list has {len(relays)} relays).",
                context={"index": target, "count": len(relays)},
                exit_code=2,
            )
        removed = relays.pop(idx)
    elif target in relays:
        relays.remove(target)
        removed = target
    else:
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            f"Relay {target!r} not found in default_relays.",
            context={"target": target, "relays": list(p.default_relays)},
            exit_code=2,
        )

    if not relays and not force:
        _emit_error(
            ErrorCode.INVALID_ARGUMENT,
            "Refusing to remove the last relay. Use --force to empty the list.",
            context={"removed": removed},
            exit_code=2,
        )

    p.default_relays = relays
    p.save(profile)

    if format_ == OutputFormat.JSON:
        _output_json({"ok": True, "removed": removed, "relays": relays})
        return
    console.print(f"[green]✓[/green] Removed [cyan]{removed}[/cyan]")
    if not relays and force:
        console.print(
            "[yellow]⚠ default_relays was saved empty, but bootstrap defaults will be "
            "restored on the next profile load.[/yellow]"
        )
    console.print(f"[dim]Saved to {profile}[/dim]")


@relay_app.command("status")
def relay_status(
    as_: str = typer.Option("default", "--as", help="Local identity to act as"),
    profile: Path = typer.Option(DEFAULT_PROFILE, help="Path to profile.json"),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO, "--format", "-f", help="Output format: auto (default), text, or json"
    ),
) -> None:
    """Relay health check: identity, trusted peers, relays, last poll."""
    format_ = resolve_format(format_)
    p = _load_profile_for_relay(profile)

    # Resolve and validate the requested instance
    _resolve_instance(p, as_)
    # Derive display label: use as_ if explicit, otherwise the single registered instance
    instance_label = as_ if as_ != "default" else next(iter(p.instances.keys()), "default")

    # Trusted peers
    trusted_peers = [v.label for v in p.trusted_keys.values() if v.label]

    # Relay URLs
    relays = list(p.default_relays)

    # Last poll per relay
    last_checked: dict[str, str] = {}
    if p.last_checked:
        last_checked = {url: ts for url, ts in p.last_checked.items() if url in relays}

    if format_ == OutputFormat.JSON:
        _output_json(
            {
                "instance": instance_label,
                "trusted_peers": trusted_peers,
                "relays": relays,
                "last_checked": last_checked,
            }
        )
        return

    console.print(f"Instance:       {instance_label}")
    console.print("Trusted peers:  " + (", ".join(trusted_peers) if trusted_peers else "(none)"))
    console.print("Relays:         " + (", ".join(relays) if relays else "(none)"))
    if last_checked:
        for url, ts in last_checked.items():
            console.print(f"Last poll:      {url} → {ts}")
    else:
        console.print("Last poll:      (never)")


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
            "Set [bold]AYA_NOTEBOOK_PATH[/bold] or run: "
            "[bold]aya config set notebook_path ~/notebook[/bold]"
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


# ── Log sub-app ──────────────────────────────────────────────────────────────

log_app = typer.Typer(
    name="log",
    help="Daily progress logging.",
    no_args_is_help=True,
)
app.add_typer(log_app, name="log")


@log_app.command("append")
def log_append(
    message: str = typer.Option(..., "--message", "-m", help="Progress entry text"),
    tags: str | None = typer.Option(
        None,
        "--tags",
        "-t",
        help="Comma-separated tags (e.g. pr/174,fix/170)",
    ),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    """Append a timestamped entry to today's daily note."""
    from aya.log import append_entry

    fmt = resolve_format(format_)
    try:
        daily, entry = append_entry(message, tags=tags)
    except ValueError as exc:
        _emit_error(ErrorCode.INVALID_ARGUMENT, str(exc))
    if fmt == OutputFormat.JSON:
        _output_json({"entry": entry, "file": str(daily)})
    else:
        console.print(f"[green]✓[/green] {entry}")
        console.print(f"[dim]{daily}[/dim]")


@log_app.command("auto")
def log_auto(
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    """Inspect recent activity and log a summary if warranted.

    Exits silently if nothing noteworthy is detected or if the last entry
    was written less than 5 minutes ago.
    """
    from aya.log import auto_log

    fmt = resolve_format(format_)
    try:
        result = auto_log()
    except ValueError as exc:
        _emit_error(ErrorCode.INVALID_ARGUMENT, str(exc))
    if result is None:
        if fmt == OutputFormat.JSON:
            _output_json({"logged": False})
        # Text mode: silent when nothing logged (per design spec)
        return
    daily, entry = result
    if fmt == OutputFormat.JSON:
        _output_json({"logged": True, "entry": entry, "file": str(daily)})
    # Text mode: silent by default — entry is in the daily note


@log_app.command("show")
def log_show(
    date: str | None = typer.Option(
        None,
        "--date",
        "-d",
        help="Date to show (YYYY-MM-DD, default: today)",
    ),
    format_: OutputFormat = typer.Option(
        OutputFormat.AUTO,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    """Display progress entries for today (or a given date)."""
    from aya.log import show_entries
    from aya.scheduler.time_utils import _get_local_tz

    fmt = resolve_format(format_)
    dt = None
    if date:
        from datetime import datetime as _dt

        try:
            dt = _dt.strptime(date, "%Y-%m-%d").replace(tzinfo=_get_local_tz())
        except ValueError:
            _emit_error(
                ErrorCode.INVALID_ARGUMENT,
                f"Invalid date format: {date!r} (expected YYYY-MM-DD)",
            )

    try:
        entries = show_entries(date=dt)
    except ValueError as exc:
        _emit_error(ErrorCode.INVALID_ARGUMENT, str(exc))

    if fmt == OutputFormat.JSON:
        _output_json({"date": date or "today", "entries": entries})
        return

    if not entries:
        console.print("[dim]No progress entries found.[/dim]")
        return

    for e in entries:
        line = f"[{e['time']}] {e['message']}"
        if "tags" in e:
            line += f" — {e['tags']}"
        console.print(line)
