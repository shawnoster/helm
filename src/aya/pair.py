"""Relay-mediated pairing — short-code trust exchange between instances."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime

import websockets

from aya.identity import Identity, TrustedKey
from aya.relay import (
    AYA_KIND,
    _backoff_delay,
    _compute_event_id,
    _is_rate_limited,
    _read_until_eose,
    _sign_hex,
)

logger = logging.getLogger(__name__)

PAIR_TTL_SECONDS = 600  # 10 minutes
PAIR_POLL_INTERVAL = 3  # seconds

# 256 words — easy to read aloud, no homophones, no offensive terms.
# 8 bits per word x 2 words + ~13 bits (4-digit number) ≈ 29 bits.
# Enough entropy for a 10-minute window.
WORD_LIST: tuple[str, ...] = (
    "AMBER",
    "ANCHOR",
    "ARROW",
    "ATLAS",
    "BADGE",
    "BASIN",
    "BEACH",
    "BIRCH",
    "BLADE",
    "BLAZE",
    "BLOOM",
    "BOARD",
    "BOLT",
    "BOWER",
    "BRAKE",
    "BRAVE",
    "BRICK",
    "BROOK",
    "BRUSH",
    "CAIRN",
    "CALM",
    "CANOE",
    "CARGO",
    "CEDAR",
    "CHAIN",
    "CHALK",
    "CHASE",
    "CHIEF",
    "CLIFF",
    "CLIMB",
    "CLOUD",
    "CLOVER",
    "COBRA",
    "CORAL",
    "CRANE",
    "CREEK",
    "CREST",
    "CROWN",
    "CRUSH",
    "DAGGER",
    "DELTA",
    "DIVER",
    "DOCK",
    "DRAFT",
    "DRIFT",
    "DRUM",
    "DUNE",
    "EAGLE",
    "EARTH",
    "EMBER",
    "FABLE",
    "FALCON",
    "FERN",
    "FIELD",
    "FLAME",
    "FLARE",
    "FLASH",
    "FLINT",
    "FLOOD",
    "FORGE",
    "FROST",
    "GALE",
    "GATE",
    "GAVEL",
    "GHOST",
    "GLADE",
    "GLEAM",
    "GLOBE",
    "GRAIN",
    "GRANT",
    "GRAPE",
    "GROVE",
    "GUARD",
    "GUILD",
    "HAVEN",
    "HAWK",
    "HAZEL",
    "HEART",
    "HEDGE",
    "HERON",
    "HIKER",
    "HONOR",
    "HOVER",
    "INLET",
    "IVORY",
    "JADE",
    "JETTY",
    "JEWEL",
    "KARMA",
    "KAYAK",
    "LANCE",
    "LARCH",
    "LASER",
    "LATCH",
    "LEDGE",
    "LEVER",
    "LIGHT",
    "LILAC",
    "LINEN",
    "LODGE",
    "LUNAR",
    "MAPLE",
    "MARCH",
    "MARSH",
    "MASON",
    "MEDAL",
    "MERGE",
    "MESA",
    "METAL",
    "MIRTH",
    "MIST",
    "MOOSE",
    "MOUNT",
    "NOBLE",
    "NORTH",
    "OASIS",
    "OLIVE",
    "ONYX",
    "ORBIT",
    "OTTER",
    "OXIDE",
    "PANEL",
    "PATCH",
    "PEARL",
    "PERCH",
    "PILOT",
    "PINE",
    "PIXEL",
    "PLAIN",
    "PLANT",
    "PLAZA",
    "PLUMB",
    "PLUME",
    "POLAR",
    "POND",
    "PORTER",
    "PRISM",
    "PULSE",
    "QUAIL",
    "QUARTZ",
    "QUEST",
    "RAPID",
    "RAVEN",
    "REALM",
    "REEF",
    "RIDGE",
    "RIVET",
    "ROOST",
    "ROVER",
    "ROYAL",
    "SAGE",
    "SCALE",
    "SCOUT",
    "SHARD",
    "SHELL",
    "SHORE",
    "SIGMA",
    "SLATE",
    "SLOPE",
    "SOLAR",
    "SPARK",
    "SPIRE",
    "SPOKE",
    "SPRAY",
    "SQUID",
    "STAG",
    "STEAM",
    "STEEL",
    "STONE",
    "STORM",
    "STORK",
    "SURGE",
    "SWIFT",
    "THORN",
    "TIGER",
    "TIMBER",
    "TORCH",
    "TOWER",
    "TRAIL",
    "TROUT",
    "TULIP",
    "TUNDRA",
    "UNITY",
    "VALOR",
    "VAULT",
    "VENOM",
    "VERGE",
    "VIPER",
    "VIVID",
    "VOICE",
    "WALNUT",
    "WARDEN",
    "WATER",
    "WAVE",
    "WHEAT",
    "WILLOW",
    "WINGS",
    "WOLF",
    "WRAITH",
    "YARN",
    "ZENITH",
    "FLICKER",
    "COMET",
    "DEPOT",
    "FINCH",
    "GRAIL",
    "CONDOR",
    "IRIS",
    "KNOLL",
    "THICKET",
    "MANOR",
    "NEXUS",
    "OPAL",
    "PETAL",
    "QUILT",
    "ROBIN",
    "SABLE",
    "TALON",
    "ULTRA",
    "VIGOR",
    "WEDGE",
    "XERUS",
    "YUCCA",
    "ZEPHYR",
    "ASPEN",
    "BASIL",
    "CINDER",
    "DRAKE",
    "MIRAGE",
    "FJORD",
    "GLACIER",
    "HARBOR",
    "ICICLE",
    "JASPER",
    "KESTREL",
    "LANTERN",
    "MAGNET",
    "NECTAR",
    "OSPREY",
    "PEBBLE",
    "QUARRY",
    "RAPIDS",
    "SUMMIT",
    "TRELLIS",
    "UMBER",
    "VALLEY",
    "WANDER",
    "YARROW",
    "PARCEL",
    "ALPINE",
    "BREEZE",
    "COPPER",
    "DAPPLE",
    "ECLIPSE",
    "FATHOM",
    "GARNET",
)

_TAG_PAIR_REQ = "aya-pair-req"
_TAG_PAIR_RESP = "aya-pair-resp"


def generate_code() -> str:
    """Generate a human-readable pairing code: WORD-WORD-NNNN."""
    w1 = secrets.choice(WORD_LIST)
    w2 = secrets.choice(WORD_LIST)
    num = f"{secrets.randbelow(10000):04d}"
    return f"{w1}-{w2}-{num}"


def hash_code(code: str) -> str:
    """SHA-256 of the uppercased code — relay sees hash, not code."""
    return hashlib.sha256(code.upper().encode()).hexdigest()


async def publish_pair_request(
    identity: Identity,
    label: str,
    code_hash: str,
    relay_url: str | list[str],
) -> str:
    """Publish a pair-request event to all configured relays. Returns the event ID."""
    relay_urls = [relay_url] if isinstance(relay_url, str) else relay_url
    request_event = _build_pair_request(identity, label, code_hash, relay_urls[0])
    published = False
    last_event_id = request_event["id"]
    for url in relay_urls:
        for attempt in range(3):
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps(["EVENT", request_event]))
                    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if resp[0] == "OK" and resp[2]:
                        logger.info("Published pair request %s via %s", last_event_id[:8], url)
                        published = True
                        break
                    if _is_rate_limited(resp) and attempt < 2:
                        delay = _backoff_delay(attempt)
                        logger.warning("Rate-limited by %s, retrying in %.1fs", url, delay)
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("Relay %s rejected pair request: %s", url, resp)
                    break
            except Exception as exc:
                if attempt < 2:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "Error publishing pair request to %s: %s — retry in %.1fs",
                        url,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning("Failed to publish pair request to %s: %s", url, exc)
    if not published:
        raise PairingError("All relays rejected the pair request")
    return last_event_id


async def poll_for_pair_response(
    relay_url: str | list[str],
    my_pubkey: str,
    request_event_id: str,
    timeout_seconds: int = PAIR_TTL_SECONDS,
) -> TrustedKey | None:
    """Poll all configured relays for a pair-response.

    Polls each relay in turn, returning as soon as any relay yields a response.
    Applies per-relay exponential backoff when a relay has transient failures to
    avoid hammering rate-limited relays on reconnect.
    """
    relay_urls = [relay_url] if isinstance(relay_url, str) else relay_url
    since_ts = int(datetime.now(UTC).timestamp()) - 5
    deadline = datetime.now(UTC).timestamp() + timeout_seconds
    relay_failures: dict[str, int] = dict.fromkeys(relay_urls, 0)

    while datetime.now(UTC).timestamp() < deadline:
        for url in relay_urls:
            failures = relay_failures[url]
            if failures > 0:
                delay = _backoff_delay(failures - 1)
                remaining = deadline - datetime.now(UTC).timestamp()
                if remaining <= 0:
                    return None
                await asyncio.sleep(min(delay, remaining))
            result, had_error = await _poll_single_relay(
                url, my_pubkey, request_event_id, since_ts, deadline
            )
            if result is not None:
                return result
            relay_failures[url] = (failures + 1) if had_error else 0
        remaining = deadline - datetime.now(UTC).timestamp()
        if remaining <= 0:
            break
        await asyncio.sleep(min(PAIR_POLL_INTERVAL, remaining))

    return None


async def _poll_single_relay(
    relay_url: str,
    my_pubkey: str,
    request_event_id: str,
    since_ts: int,
    deadline: float,
) -> tuple[TrustedKey | None, bool]:
    """Poll a single relay once for a pair-response.

    Returns ``(TrustedKey, False)`` on success, ``(None, False)`` when no
    response is available yet, and ``(None, True)`` on a connection/transient
    error so the caller can apply backoff before retrying.
    """
    try:
        async with websockets.connect(relay_url) as ws:
            filter_ = {
                "kinds": [AYA_KIND],
                "#t": [_TAG_PAIR_RESP],
                "#p": [my_pubkey],
                "#e": [request_event_id],
                "since": since_ts,
                "limit": 1,
            }
            sub_id = f"pair-poll-{datetime.now(UTC).timestamp():.0f}"
            await ws.send(json.dumps(["REQ", sub_id, filter_]))
            try:
                eose_timeout = max(1.0, deadline - datetime.now(UTC).timestamp())
                async for event in _read_until_eose(ws, sub_id, eose_timeout=eose_timeout):
                    await ws.send(json.dumps(["CLOSE", sub_id]))
                    content = json.loads(event["content"])
                    return TrustedKey(
                        did=content["did"],
                        label=content["label"],
                        nostr_pubkey=event["pubkey"],
                    ), False
            except TimeoutError:
                logger.debug("EOSE not received within timeout on %s; continuing", relay_url)
            await ws.send(json.dumps(["CLOSE", sub_id]))
    except TimeoutError:
        logger.debug("Pair polling timed out on %s", relay_url)
        return None, True
    except Exception as exc:
        logger.warning("Pair polling connection error on %s: %s", relay_url, exc)
        return None, True
    return None, False


async def join_pairing(
    identity: Identity,
    label: str,
    code: str,
    relay_url: str | list[str],
) -> TrustedKey:
    """
    Joiner flow:
      1. Hash the code, find matching pair-request on relay
      2. Publish pair-response
      3. Return TrustedKey for the initiator
    """
    relay_urls = [relay_url] if isinstance(relay_url, str) else relay_url
    code_h = hash_code(code)

    # Find the pair request on any relay
    request = await _find_pair_request(relay_urls, code_h)
    if not request:
        raise PairingError("No matching pairing request found. Check the code and try again.")

    req_content = json.loads(request["content"])
    initiator_did = req_content["did"]
    initiator_label = req_content["label"]
    initiator_pubkey = request["pubkey"]
    request_event_id = request["id"]

    # Publish response to all relays; succeed if at least one accepts
    response_event = _build_pair_response(
        identity, label, initiator_pubkey, request_event_id, relay_urls[0]
    )
    published = False
    for url in relay_urls:
        try:
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps(["EVENT", response_event]))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if resp[0] == "OK" and resp[2]:
                    published = True
                else:
                    logger.warning("Relay %s rejected pair response: %s", url, resp)
        except Exception as exc:
            logger.warning("Failed to publish pair response to %s: %s", url, exc)
    if not published:
        raise PairingError("All relays rejected the pair response")

    return TrustedKey(did=initiator_did, label=initiator_label, nostr_pubkey=initiator_pubkey)


# ── Event builders ───────────────────────────────────────────────────────────


def _build_pair_request(
    identity: Identity,
    label: str,
    code_hash: str,
    relay_url: str,  # noqa: ARG001
) -> dict:
    """Build a Nostr event for a pairing request."""
    created_at = int(datetime.now(UTC).timestamp())
    expiration = created_at + PAIR_TTL_SECONDS
    content = json.dumps({"did": identity.did, "label": label})
    tags = [
        ["t", _TAG_PAIR_REQ],
        ["d", code_hash],
        ["expiration", str(expiration)],
        ["aya-version", "0.2"],
    ]
    event_id = _compute_event_id(
        pubkey=identity.nostr_public_hex,
        created_at=created_at,
        kind=AYA_KIND,
        tags=tags,
        content=content,
    )
    sig = _sign_hex(event_id, identity.nostr_private_hex)
    return {
        "id": event_id,
        "pubkey": identity.nostr_public_hex,
        "created_at": created_at,
        "kind": AYA_KIND,
        "tags": tags,
        "content": content,
        "sig": sig,
    }


def _build_pair_response(
    identity: Identity,
    label: str,
    initiator_pubkey: str,
    request_event_id: str,
    relay_url: str,  # noqa: ARG001
) -> dict:
    """Build a Nostr event responding to a pairing request."""
    created_at = int(datetime.now(UTC).timestamp())
    expiration = created_at + PAIR_TTL_SECONDS
    content = json.dumps({"did": identity.did, "label": label})
    tags = [
        ["t", _TAG_PAIR_RESP],
        ["p", initiator_pubkey],
        ["e", request_event_id],
        ["expiration", str(expiration)],
        ["aya-version", "0.2"],
    ]
    event_id = _compute_event_id(
        pubkey=identity.nostr_public_hex,
        created_at=created_at,
        kind=AYA_KIND,
        tags=tags,
        content=content,
    )
    sig = _sign_hex(event_id, identity.nostr_private_hex)
    return {
        "id": event_id,
        "pubkey": identity.nostr_public_hex,
        "created_at": created_at,
        "kind": AYA_KIND,
        "tags": tags,
        "content": content,
        "sig": sig,
    }


# ── Relay queries ────────────────────────────────────────────────────────────


async def _find_pair_request(relay_url: str | list[str], code_hash: str) -> dict | None:
    """Find a pair-request on any of the configured relays matching the given code hash."""
    relay_urls = [relay_url] if isinstance(relay_url, str) else relay_url
    since_ts = int(datetime.now(UTC).timestamp()) - PAIR_TTL_SECONDS
    filter_ = {
        "kinds": [AYA_KIND],
        "#t": [_TAG_PAIR_REQ],
        "#d": [code_hash],
        "since": since_ts,
        "limit": 1,
    }
    for url in relay_urls:
        try:
            sub_id = f"pair-find-{datetime.now(UTC).timestamp():.0f}"
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps(["REQ", sub_id, filter_]))
                async for event in _read_until_eose(ws, sub_id):
                    await ws.send(json.dumps(["CLOSE", sub_id]))
                    return event
                await ws.send(json.dumps(["CLOSE", sub_id]))
        except Exception as exc:
            logger.warning("Failed to query %s for pair request: %s", url, exc)
    return None


class PairingError(Exception):
    pass
