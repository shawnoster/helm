"""Nostr relay client — send and receive packets via NIP-01 WebSocket protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import websockets
from coincurve import PrivateKey as Secp256k1PrivateKey
from websockets.asyncio.client import ClientConnection

from aya.packet import Packet

logger = logging.getLogger(__name__)

# Assistant Sync uses kind 5999 — within the NIP-90 Data Vending Machine range
ACE_SYNC_KIND = 5999
ACE_SYNC_RESULT_KIND = 6999  # read receipts / replies


class RelayClient:
    """
    Minimal Nostr relay client for Assistant Sync packet delivery.

    Handles:
      - Publishing packets as signed Nostr events (kind 5999)
      - Querying for pending packets addressed to a pubkey
      - Sending read receipts (kind 6999)

    Uses secp256k1 (Nostr) keys for transport, not ed25519 (did:key).
    """

    def __init__(self, relay_url: str, nostr_private_hex: str, nostr_public_hex: str) -> None:
        self.relay_url = relay_url
        self._private_key_hex = nostr_private_hex
        self.public_key_hex = nostr_public_hex

    async def publish(self, packet: Packet, recipient_nostr_pubkey: str) -> str:
        """Publish a packet to the relay. Returns the Nostr event ID."""
        event = self._build_event(packet, recipient_nostr_pubkey)
        async with websockets.connect(self.relay_url) as ws:
            await ws.send(json.dumps(["EVENT", event]))
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if response[0] == "OK" and response[2]:
                logger.info("Published packet %s as event %s", packet.id[:8], event["id"][:8])
                return event["id"]
            raise RelayError(f"Relay rejected event: {response}")

    async def fetch_pending(
        self,
        since: datetime | None = None,
        limit: int = 50,
    ) -> AsyncIterator[Packet]:
        """
        Yield packets addressed to this instance's pubkey.
        Filters by `since` timestamp — use last_checked from profile.
        """
        filter_: dict = {
            "kinds": [ACE_SYNC_KIND],
            "#p": [self.public_key_hex],
            "limit": limit,
        }
        if since:
            filter_["since"] = int(since.timestamp())

        sub_id = f"assistant-sync-{datetime.now(UTC).timestamp():.0f}"

        async with websockets.connect(self.relay_url) as ws:
            await ws.send(json.dumps(["REQ", sub_id, filter_]))

            async for raw in _read_until_eose(ws, sub_id):
                try:
                    packet = Packet.from_json(raw["content"])
                    if not packet.is_expired():
                        yield packet
                except Exception as exc:
                    logger.warning("Skipping malformed event: %s", exc)

            await ws.send(json.dumps(["CLOSE", sub_id]))

    async def send_receipt(self, packet: Packet, sender_nostr_pubkey: str) -> None:
        """Publish a read receipt for the given packet."""
        event = self._build_receipt(packet, sender_nostr_pubkey)
        async with websockets.connect(self.relay_url) as ws:
            await ws.send(json.dumps(["EVENT", event]))
            await asyncio.wait_for(ws.recv(), timeout=10)

    def _build_event(self, packet: Packet, recipient_nostr_pubkey: str) -> dict:
        """Build a NIP-01 compliant Nostr event wrapping the Assistant Sync packet."""
        recipient_pubkey = recipient_nostr_pubkey

        content = packet.to_json()
        tags = [
            ["p", recipient_pubkey],
            ["expiration", str(int(datetime.fromisoformat(packet.expires_at).timestamp()))],
            ["assistant-sync-version", "0.1"],
            ["assistant-sync-packet-id", packet.id],
        ]

        created_at = int(datetime.now(UTC).timestamp())
        event_id = _compute_event_id(
            pubkey=self.public_key_hex,
            created_at=created_at,
            kind=ACE_SYNC_KIND,
            tags=tags,
            content=content,
        )
        sig = _sign_hex(event_id, self._private_key_hex)

        return {
            "id": event_id,
            "pubkey": self.public_key_hex,
            "created_at": created_at,
            "kind": ACE_SYNC_KIND,
            "tags": tags,
            "content": content,
            "sig": sig,
        }

    def _build_receipt(self, packet: Packet, sender_nostr_pubkey: str) -> dict:
        content = json.dumps({"packet_id": packet.id, "status": "received"})
        recipient_pubkey = sender_nostr_pubkey
        tags = [
            ["p", recipient_pubkey],
            ["e", packet.id],
            ["assistant-sync-version", "0.1"],
        ]
        created_at = int(datetime.now(UTC).timestamp())
        event_id = _compute_event_id(
            pubkey=self.public_key_hex,
            created_at=created_at,
            kind=ACE_SYNC_RESULT_KIND,
            tags=tags,
            content=content,
        )
        sig = _sign_hex(event_id, self._private_key_hex)
        return {
            "id": event_id,
            "pubkey": self.public_key_hex,
            "created_at": created_at,
            "kind": ACE_SYNC_RESULT_KIND,
            "tags": tags,
            "content": content,
            "sig": sig,
        }


async def _read_until_eose(ws: ClientConnection, sub_id: str) -> AsyncIterator[dict]:
    """Yield EVENT payloads until EOSE (end of stored events) from the relay."""
    async for raw_msg in ws:
        msg = json.loads(raw_msg)
        match msg:
            case ["EVENT", sid, event] if sid == sub_id:
                yield event
            case ["EOSE", sid] if sid == sub_id:
                return
            case ["NOTICE", notice]:
                logger.debug("Relay notice: %s", notice)
            case _:
                logger.debug("Unexpected relay message: %s", msg)


def _compute_event_id(
    pubkey: str,
    created_at: int,
    kind: int,
    tags: list,
    content: str,
) -> str:
    """NIP-01: event ID is SHA-256 of canonical serialisation."""
    serialised = json.dumps(
        [0, pubkey, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialised.encode()).hexdigest()


def _sign_hex(event_id_hex: str, private_key_hex: str) -> str:
    """Sign a Nostr event ID with secp256k1 Schnorr (BIP-340) and return hex signature."""
    key = Secp256k1PrivateKey(bytes.fromhex(private_key_hex))
    sig_bytes = key.sign_schnorr(bytes.fromhex(event_id_hex))
    return sig_bytes.hex()


class RelayError(Exception):
    pass
