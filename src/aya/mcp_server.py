"""MCP server — expose aya capabilities as Claude-native tools via stdio transport."""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger(__name__)

server = Server("aya")

# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

_TOOLS: list[types.Tool] = [
    types.Tool(
        name="aya_status",
        description="Return workspace readiness status (systems, alerts, reminders, watches).",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_inbox",
        description="List pending (un-ingested) relay packets for an instance.",
        inputSchema={
            "type": "object",
            "properties": {
                "instance": {
                    "type": "string",
                    "description": "Local identity to act as (default: 'default').",
                    "default": "default",
                },
            },
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_send",
        description="Build, sign, and publish a packet to a relay.",
        inputSchema={
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient label (e.g. 'home') or DID.",
                },
                "intent": {
                    "type": "string",
                    "description": "What this packet is and why it is being sent.",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown body of the packet.",
                },
                "instance": {
                    "type": "string",
                    "description": "Local identity to act as (default: 'default').",
                    "default": "default",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Dedup key — if already sent within 24h, return cached result.",
                },
                "in_reply_to": {
                    "type": "string",
                    "description": "Packet ID this message is a reply to.",
                },
            },
            "required": ["to", "intent", "content"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_receive",
        description="Poll the relay, auto-ingest trusted packets, and return summaries.",
        inputSchema={
            "type": "object",
            "properties": {
                "instance": {
                    "type": "string",
                    "description": "Local identity to act as (default: 'default').",
                    "default": "default",
                },
            },
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_schedule_remind",
        description="Create a one-shot reminder in the scheduler.",
        inputSchema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Reminder text.",
                },
                "due": {
                    "type": "string",
                    "description": "When the reminder is due (e.g. 'tomorrow 9am', 'in 2 hours').",
                },
            },
            "required": ["message", "due"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_schedule_watch",
        description="Create a condition-based watch in the scheduler.",
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Watch provider (e.g. 'github-pr').",
                },
                "target": {
                    "type": "string",
                    "description": "Provider-specific target (e.g. 'owner/repo#123').",
                },
                "message": {
                    "type": "string",
                    "description": "Alert message when the condition fires.",
                },
            },
            "required": ["provider", "target", "message"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_ack",
        description="Acknowledge a received packet, sending a reply back to the sender.",
        inputSchema={
            "type": "object",
            "properties": {
                "packet_id": {
                    "type": "string",
                    "description": "Packet ID or prefix (min 8 chars) to acknowledge.",
                },
                "message": {
                    "type": "string",
                    "description": "Short reply message (default: 'acknowledged').",
                    "default": "acknowledged",
                },
                "instance": {
                    "type": "string",
                    "description": "Local identity to act as (default: 'default').",
                    "default": "default",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Dedup key — if already sent within 24h, return cached result.",
                },
            },
            "required": ["packet_id"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_show",
        description="Show the full content of a previously ingested packet by ID or prefix.",
        inputSchema={
            "type": "object",
            "properties": {
                "packet_id": {
                    "type": "string",
                    "description": "Packet ID or prefix (min 8 chars).",
                },
            },
            "required": ["packet_id"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_read",
        description="Read the content of a stored packet by ID or prefix.",
        inputSchema={
            "type": "object",
            "properties": {
                "packet_id": {
                    "type": "string",
                    "description": "Packet ID or prefix (min 8 chars).",
                },
                "meta": {
                    "type": "boolean",
                    "description": "If true, return full metadata; otherwise return content only.",
                    "default": False,
                },
            },
            "required": ["packet_id"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_config_set",
        description="Set a workspace configuration value.",
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Configuration key to set.",
                },
                "value": {
                    "type": "string",
                    "description": "Value to assign.",
                },
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_config_show",
        description="Show the current workspace configuration.",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_packets",
        description="List stored packets, most recent first.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum number of packets to return (default: 20).",
                    "default": 20,
                },
            },
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="aya_relay_status",
        description="Show relay health and identity info for an instance.",
        inputSchema={
            "type": "object",
            "properties": {
                "instance": {
                    "type": "string",
                    "description": "Local identity to check (default: 'default').",
                    "default": "default",
                },
            },
            "additionalProperties": False,
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return _TOOLS


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _text(data: object) -> list[types.TextContent]:
    """Wrap *data* as a single JSON TextContent block."""
    return [types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _error(message: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps({"error": message}))]


def _load_profile() -> Any:
    from aya.identity import Profile
    from aya.paths import PROFILE_PATH

    return Profile.load(PROFILE_PATH)


def _resolve_instance(profile: Any, instance: str) -> Any:
    local = profile.instances.get(instance)
    if local is not None:
        return local
    available = list(profile.instances.keys())
    if len(available) == 1:
        return next(iter(profile.instances.values()))
    msg = f"Instance '{instance}' not found. Available: {', '.join(available)}."
    raise ValueError(msg)


def _resolve_did(to: str, profile: Any) -> tuple[str, str]:
    if to.startswith("did:"):
        return to, to
    key = profile.trusted_keys.get(to)
    if key:
        return key.did, to
    available = list(profile.trusted_keys.keys())
    if len(available) == 1:
        label = available[0]
        return next(iter(profile.trusted_keys.values())).did, label
    raise ValueError(f"Unknown recipient '{to}'. Available: {', '.join(available)}.")


def _resolve_nostr_pubkey(did: str, profile: Any) -> str | None:
    for key in profile.trusted_keys.values():
        if key.did == did and key.nostr_pubkey:
            return str(key.nostr_pubkey)
    for inst in profile.instances.values():
        if inst.did == did:
            return str(inst.nostr_public_hex)
    return None


# ── individual handlers ──────────────────────────────────────────────────────


async def _handle_status() -> list[types.TextContent]:
    from aya.status import _gather_status, _render_json

    data = _gather_status()
    return [types.TextContent(type="text", text=_render_json(data))]


async def _handle_inbox(arguments: dict[str, Any]) -> list[types.TextContent]:
    instance = arguments.get("instance", "default")
    profile = _load_profile()
    local = _resolve_instance(profile, instance)

    from aya.relay import RelayClient

    relay_urls = profile.default_relays
    client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

    all_packets = [pkt async for pkt in client.fetch_pending()]
    ingested_set = {entry["id"] for entry in profile.ingested_ids}
    new_packets = [pkt for pkt in all_packets if pkt.id not in ingested_set]

    summaries = [
        {
            "id": pkt.id,
            "intent": pkt.intent,
            "from": pkt.from_did,
            "sent_at": pkt.sent_at,
            "summary": pkt.summary(),
        }
        for pkt in new_packets
    ]
    return _text(summaries)


async def _handle_send(arguments: dict[str, Any]) -> list[types.TextContent]:
    from aya.cli import _check_idempotency, _record_idempotency

    idempotency_key = arguments.get("idempotency_key")
    if idempotency_key:
        cached = _check_idempotency(idempotency_key)
        if cached:
            return _text(
                {
                    "packet_id": cached["packet_id"],
                    "event_id": cached["event_id"],
                    "cached": True,
                }
            )

    instance = arguments.get("instance", "default")
    to = arguments["to"]
    intent = arguments["intent"]
    content = arguments["content"]

    profile = _load_profile()
    local = _resolve_instance(profile, instance)
    to_did, _to_label = _resolve_did(to, profile)

    from aya.packet import ContentType, Packet
    from aya.relay import RelayClient

    in_reply_to = arguments.get("in_reply_to")

    packet = Packet(
        **{"from": local.did, "to": to_did},
        intent=intent,
        content_type=ContentType.MARKDOWN,
        content=content,
        in_reply_to=in_reply_to,
    )
    packet.encrypted = True
    signed = packet.sign(local)

    recipient_nostr_pub = _resolve_nostr_pubkey(signed.to_did, profile)
    if recipient_nostr_pub is None:
        return _error("No Nostr pubkey found for recipient. Pair first.")

    relay_urls = profile.default_relays
    client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)
    event_id = await client.publish(signed, recipient_nostr_pub, encrypt=True)

    if idempotency_key:
        _record_idempotency(idempotency_key, signed.id, event_id)

    return _text({"packet_id": signed.id, "event_id": event_id})


async def _handle_receive(arguments: dict[str, Any]) -> list[types.TextContent]:
    instance = arguments.get("instance", "default")

    from datetime import UTC, datetime

    from aya.identity import _assert_valid_ulid
    from aya.ingest import ingest
    from aya.packet import Packet
    from aya.paths import PACKETS_DIR, PROFILE_PATH
    from aya.relay import RelayClient

    profile = _load_profile()
    local = _resolve_instance(profile, instance)

    relay_urls = profile.default_relays
    client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)

    # No `since` filter — ingested_ids is the authoritative dedup mechanism and
    # the relay's 7-day TTL window is the correct lower bound.  A last_checked-
    # derived cursor permanently excludes packets that arrived before the cursor
    # but were never ingested (see issue #246).
    packets: list[Packet] = []
    async for packet in client.fetch_pending():
        packets.append(packet)

    now_check_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for url in relay_urls:
        profile.last_checked[url] = now_check_iso

    ingested_set = {entry["id"] for entry in profile.ingested_ids}
    verified = [pkt for pkt in packets if pkt.id not in ingested_set and pkt.verify_from_did()]

    received: list[dict[str, Any]] = []
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for pkt in verified:
        trusted = profile.is_trusted(pkt.from_did)
        if trusted:
            _assert_valid_ulid(pkt.id)
            ingest(pkt, quiet=True)
            # ingest persists best-effort (debug-log on failure). Under MCP the
            # body write is our only record — if it didn't land, leave ingested_ids
            # alone so the next poll retries instead of losing the packet.
            if not (PACKETS_DIR / f"{pkt.id}.json").exists():
                logger.warning("Persistence failed for packet %s; not advancing cursor", pkt.id)
                received.append(
                    {
                        "id": pkt.id,
                        "intent": pkt.intent,
                        "from": pkt.from_did,
                        "ingested": False,
                        "error": "persist_failed",
                    }
                )
                continue
            profile.ingested_ids.append(
                {"id": pkt.id, "ingested_at": now_iso, "from_did": pkt.from_did}
            )
            received.append(
                {"id": pkt.id, "intent": pkt.intent, "from": pkt.from_did, "ingested": True}
            )
        else:
            # MCP is always non-interactive — skip untrusted packets silently.
            logger.debug("Skipping untrusted packet %s from %s", pkt.id[:8], pkt.from_did[:30])
            received.append(
                {
                    "id": pkt.id,
                    "intent": pkt.intent,
                    "from": pkt.from_did,
                    "ingested": False,
                    "skipped": True,
                }
            )

    profile.save(PROFILE_PATH)
    return _text(received)


async def _handle_schedule_remind(arguments: dict[str, Any]) -> list[types.TextContent]:
    from aya.scheduler import add_reminder

    item = add_reminder(arguments["message"], arguments["due"])
    return _text(item)


async def _handle_schedule_watch(arguments: dict[str, Any]) -> list[types.TextContent]:
    from aya.scheduler import add_watch

    item = add_watch(
        provider=arguments["provider"],
        target=arguments["target"],
        message=arguments["message"],
    )
    return _text(item)


async def _handle_ack(arguments: dict[str, Any]) -> list[types.TextContent]:
    from aya.cli import _check_idempotency, _record_idempotency

    idempotency_key = arguments.get("idempotency_key")
    if idempotency_key:
        cached = _check_idempotency(idempotency_key)
        if cached:
            return _text(
                {
                    "packet_id": cached["packet_id"],
                    "event_id": cached["event_id"],
                    "cached": True,
                }
            )

    packet_id = arguments["packet_id"]
    message = arguments.get("message", "acknowledged")

    from aya.packet import ContentType, Packet
    from aya.relay import RelayClient

    profile = _load_profile()

    if len(packet_id) < 8:
        return _error("Packet ID prefix must be at least 8 characters.")

    ingested_ids = [entry["id"] for entry in profile.ingested_ids]
    matched = [pid for pid in ingested_ids if pid.startswith(packet_id)]

    if not matched:
        return _error(f"Packet ID '{packet_id}' not found in ingested_ids.")
    if len(matched) > 1:
        return _error(f"Ambiguous prefix '{packet_id}' -- matches {len(matched)} packets.")

    full_packet_id = matched[0]

    # Look up sender DID
    entry = next((e for e in profile.ingested_ids if e["id"] == full_packet_id), None)
    sender_did = entry.get("from_did") if entry else None

    to_did: str | None = None
    recipient_nostr_pub: str | None = None

    if sender_did:
        for _label, tk in profile.trusted_keys.items():
            if tk.did == sender_did and tk.nostr_pubkey:
                to_did = tk.did
                recipient_nostr_pub = tk.nostr_pubkey
                break

    if not to_did:
        trusted_with_nostr = [
            (lbl, tk) for lbl, tk in profile.trusted_keys.items() if tk.nostr_pubkey
        ]
        if not trusted_with_nostr:
            return _error("No trusted peers with a Nostr pubkey found.")
        if len(trusted_with_nostr) > 1:
            return _error("Multiple trusted peers -- cannot determine ACK recipient.")
        _lbl, tk = trusted_with_nostr[0]
        to_did = tk.did
        recipient_nostr_pub = tk.nostr_pubkey

    local = _resolve_instance(profile, arguments.get("instance", "default"))

    ack_packet = Packet(
        **{"from": local.did, "to": to_did},
        intent="ack",
        content_type=ContentType.JSON,
        content={"in_reply_to": full_packet_id, "message": message, "dismiss": False},
        in_reply_to=full_packet_id,
    )
    ack_packet.encrypted = True
    signed = ack_packet.sign(local)

    if recipient_nostr_pub is None:
        return _error("No Nostr pubkey found for ACK recipient.")

    relay_urls = profile.default_relays
    client = RelayClient(relay_urls, local.nostr_private_hex, local.nostr_public_hex)
    event_id = await client.publish(signed, recipient_nostr_pub, encrypt=True)

    if idempotency_key:
        _record_idempotency(idempotency_key, signed.id, event_id)

    return _text({"packet_id": signed.id, "event_id": event_id, "in_reply_to": full_packet_id})


async def _handle_show(arguments: dict[str, Any]) -> list[types.TextContent]:
    packet_id = arguments["packet_id"]

    from aya.packet import Packet
    from aya.paths import PACKETS_DIR

    if len(packet_id) < 8:
        return _error("Packet ID prefix must be at least 8 characters.")

    if not PACKETS_DIR.exists():
        return _error("No ingested packets found.")

    matches = [f for f in PACKETS_DIR.glob("*.json") if f.stem.startswith(packet_id)]
    if not matches:
        return _error(f"Packet '{packet_id}' not found.")
    if len(matches) > 1:
        return _error(f"Ambiguous prefix '{packet_id}' -- matches {len(matches)} packets.")

    pkt = Packet.from_json(matches[0].read_text())
    return _text(json.loads(pkt.to_json()))


async def _handle_read(arguments: dict[str, Any]) -> list[types.TextContent]:
    packet_id = arguments["packet_id"]
    meta = arguments.get("meta", False)

    from aya.packet import Packet
    from aya.paths import PACKETS_DIR

    if len(packet_id) < 8:
        return _error("Packet ID prefix must be at least 8 characters.")

    if not PACKETS_DIR.exists():
        return _error("No stored packets found.")

    matches = [f for f in PACKETS_DIR.glob("*.json") if f.stem.startswith(packet_id)]
    if not matches:
        return _error(f"Packet '{packet_id}' not found.")
    if len(matches) > 1:
        return _error(f"Ambiguous prefix '{packet_id}' -- matches {len(matches)} packets.")

    pkt = Packet.from_json(matches[0].read_text())

    if meta:
        return _text(
            {
                "id": pkt.id,
                "intent": pkt.intent,
                "from": pkt.from_did,
                "sent_at": pkt.sent_at,
                "content_type": (
                    pkt.content_type.value
                    if hasattr(pkt.content_type, "value")
                    else str(pkt.content_type)
                ),
                "content": pkt.content,
            }
        )
    return _text({"content": pkt.content})


async def _handle_config_set(arguments: dict[str, Any]) -> list[types.TextContent]:
    from aya.config import set_config_value

    key = arguments["key"]
    value = arguments["value"]
    config = set_config_value(key, value)
    return _text(config)


async def _handle_config_show(_arguments: dict[str, Any]) -> list[types.TextContent]:
    from aya.config import load_config

    config = load_config()
    return _text(config)


async def _handle_packets(arguments: dict[str, Any]) -> list[types.TextContent]:
    from aya.packet import Packet
    from aya.paths import PACKETS_DIR

    limit = max(int(arguments.get("limit", 20)), 1)

    if not PACKETS_DIR.exists():
        return _text([])

    def _safe_mtime(f: Any) -> float:
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    files = sorted(PACKETS_DIR.glob("*.json"), key=_safe_mtime, reverse=True)
    files = files[:limit]

    summaries = []
    for f in files:
        try:
            pkt = Packet.from_json(f.read_text())
            summaries.append(
                {
                    "id": pkt.id,
                    "intent": pkt.intent,
                    "from": pkt.from_did,
                    "sent_at": pkt.sent_at,
                }
            )
        except Exception:
            logger.debug("Skipping unparseable packet file %s", f.name)
    return _text(summaries)


async def _handle_relay_status(arguments: dict[str, Any]) -> list[types.TextContent]:
    instance = arguments.get("instance", "default")
    profile = _load_profile()
    local = _resolve_instance(profile, instance)

    trusted = {label: tk.did for label, tk in profile.trusted_keys.items()}

    relays = profile.default_relays
    last_checked = {url: ts for url, ts in profile.last_checked.items() if url in relays}

    result: dict[str, Any] = {
        "instance": instance,
        "did": local.did,
        "relays": relays,
        "trusted_keys": trusted,
        "last_checked": last_checked,
    }
    return _text(result)


# ── dispatcher ───────────────────────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "aya_status": lambda args: _handle_status(),
    "aya_inbox": _handle_inbox,
    "aya_send": _handle_send,
    "aya_receive": _handle_receive,
    "aya_schedule_remind": _handle_schedule_remind,
    "aya_schedule_watch": _handle_schedule_watch,
    "aya_ack": _handle_ack,
    "aya_show": _handle_show,
    "aya_read": _handle_read,
    "aya_config_set": _handle_config_set,
    "aya_config_show": _handle_config_show,
    "aya_packets": _handle_packets,
    "aya_relay_status": _handle_relay_status,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown tool: {name}")
    try:
        return await handler(arguments)
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return _error(str(exc))


# ── entry point ──────────────────────────────────────────────────────────────


async def main() -> None:
    """Run the MCP server over stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
