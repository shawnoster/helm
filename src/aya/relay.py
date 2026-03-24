"""Nostr relay client — send and receive packets via NIP-01 WebSocket protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import websockets
from coincurve import PrivateKey as Secp256k1PrivateKey
from websockets.asyncio.client import ClientConnection

from aya.packet import Packet

logger = logging.getLogger(__name__)

# aya uses kind 5999 — within the NIP-90 Data Vending Machine range
AYA_KIND = 5999
AYA_RESULT_KIND = 6999  # read receipts / replies

# Retry / backoff configuration
_BACKOFF_BASE = 1.0  # seconds for first retry
_BACKOFF_CAP = 60.0  # maximum sleep between retries
_BACKOFF_JITTER = 0.25  # ±25% random jitter
_MAX_RETRIES_PUBLISH = 5
_MAX_RETRIES_FETCH = 3


def _backoff_delay(attempt: int) -> float:
    """Return exponential-backoff delay with ±25% jitter.

    attempt=0 → ~1 s, attempt=1 → ~2 s, attempt=2 → ~4 s, …, capped at 60 s.
    """
    base = min(_BACKOFF_BASE * (2**attempt), _BACKOFF_CAP)
    jitter = base * _BACKOFF_JITTER * (2 * random.random() - 1)  # noqa: S311
    return max(0.0, base + jitter)


def _is_rate_limited(response: list) -> bool:
    """Return True if an OK response indicates rate-limiting."""
    return (
        len(response) >= 4
        and response[0] == "OK"
        and not response[2]
        and isinstance(response[3], str)
        and response[3].startswith("rate-limited")
    )


def _is_transient_error(exc: BaseException) -> bool:
    """Return True for connection/OS errors worth retrying."""
    return isinstance(exc, (OSError, websockets.exceptions.WebSocketException, TimeoutError))


class RelayClient:
    """
    Minimal Nostr relay client for aya packet delivery.

    Handles:
      - Publishing packets as signed Nostr events (kind 5999)
      - Querying for pending packets addressed to a pubkey
      - Sending read receipts (kind 6999)

    *relay_urls* may be a single URL (str) or a list of URLs.
    Publish fans out to all relays and succeeds if at least one accepts.
    Fetch queries all relays and deduplicates by packet ID.

    Uses secp256k1 (Nostr) keys for transport, not ed25519 (did:key).
    """

    def __init__(
        self,
        relay_urls: str | list[str],
        nostr_private_hex: str,
        nostr_public_hex: str,
    ) -> None:
        if isinstance(relay_urls, str):
            self._relay_urls: list[str] = [relay_urls]
        else:
            self._relay_urls = list(relay_urls)
        if not self._relay_urls or any(
            not isinstance(url, str) or not url.strip() for url in self._relay_urls
        ):
            raise ValueError("relay_urls must contain at least one non-empty string URL")
        # Keep relay_url as a single-URL alias for backward compatibility.
        self.relay_url: str = self._relay_urls[0]
        self._private_key_hex = nostr_private_hex
        self.public_key_hex = nostr_public_hex

    async def publish(self, packet: Packet, recipient_nostr_pubkey: str) -> str:
        """Publish a packet to all configured relays.

        Fans out to every relay regardless of individual results.
        Retries individual relays on transient failures (rate-limit, 503, network)
        with exponential back-off + jitter.  Raises *RelayError* only if all
        relays fail after exhausting retries.
        """
        event = self._build_event(packet, recipient_nostr_pubkey)
        errors: list[str] = []
        last_event_id: str | None = None

        for relay_url in self._relay_urls:
            event_id = await self._publish_to_relay(event, relay_url, packet)
            if event_id is not None:
                last_event_id = event_id
            else:
                errors.append(relay_url)

        if last_event_id is not None:
            return last_event_id

        raise RelayError(f"All relays rejected the event: {errors}")

    async def _publish_to_relay(self, event: dict, relay_url: str, packet: Packet) -> str | None:
        """Try to publish *event* to *relay_url* with retries. Returns event ID or None."""
        for attempt in range(_MAX_RETRIES_PUBLISH):
            try:
                async with websockets.connect(relay_url) as ws:
                    await ws.send(json.dumps(["EVENT", event]))
                    response = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if response[0] == "OK" and response[2]:
                        logger.info(
                            "Published packet %s as event %s via %s",
                            packet.id[:8],
                            event["id"][:8],
                            relay_url,
                        )
                        return event["id"]
                    if _is_rate_limited(response):
                        delay = _backoff_delay(attempt)
                        logger.warning(
                            "Rate-limited by %s (attempt %d/%d), retrying in %.1fs",
                            relay_url,
                            attempt + 1,
                            _MAX_RETRIES_PUBLISH,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("Relay %s rejected event: %s", relay_url, response)
                    return None
            except Exception as exc:
                if _is_transient_error(exc) and attempt < _MAX_RETRIES_PUBLISH - 1:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "Transient error publishing to %s (attempt %d/%d): %s — retry in %.1fs",
                        relay_url,
                        attempt + 1,
                        _MAX_RETRIES_PUBLISH,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning("Failed to publish to %s: %s", relay_url, exc)
                    return None
        return None

    async def fetch_pending(
        self,
        since: datetime | None = None,
        limit: int = 50,
    ) -> AsyncIterator[Packet]:
        """
        Yield packets addressed to this instance's pubkey, querying all relays.

        Results are deduplicated by packet ID across relays.  Callers that want
        a time-bounded fetch can pass *since*; omitting it fetches the most
        recent matching events up to *limit* from each relay.
        """
        seen_ids: set[str] = set()
        for relay_url in self._relay_urls:
            async for packet in self._fetch_from_relay(relay_url, since, limit):
                if packet.id not in seen_ids:
                    seen_ids.add(packet.id)
                    yield packet

    async def _fetch_from_relay(
        self,
        relay_url: str,
        since: datetime | None,
        limit: int,
    ) -> AsyncIterator[Packet]:
        """Fetch packets from a single relay with retries."""
        filter_: dict = {
            "kinds": [AYA_KIND],
            "#p": [self.public_key_hex],
            "limit": limit,
        }
        if since:
            filter_["since"] = int(since.timestamp())

        sub_id = f"aya-{datetime.now(UTC).timestamp():.0f}"

        for attempt in range(_MAX_RETRIES_FETCH):
            try:
                async with websockets.connect(relay_url) as ws:
                    await ws.send(json.dumps(["REQ", sub_id, filter_]))
                    try:
                        async for raw in _read_until_eose(ws, sub_id):
                            try:
                                packet = Packet.from_json(raw["content"])
                                if not packet.is_expired():
                                    yield packet
                            except Exception as exc:
                                logger.warning("Skipping malformed event: %s", exc)
                    except TimeoutError:
                        logger.warning(
                            "Relay %s did not send EOSE within timeout; closing subscription",
                            relay_url,
                        )
                    await ws.send(json.dumps(["CLOSE", sub_id]))
                return  # success — stop retrying
            except Exception as exc:
                if _is_transient_error(exc) and attempt < _MAX_RETRIES_FETCH - 1:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "Transient error fetching from %s (attempt %d/%d): %s — retry in %.1fs",
                        relay_url,
                        attempt + 1,
                        _MAX_RETRIES_FETCH,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning("Failed to fetch from %s: %s", relay_url, exc)
                    return

    async def send_receipt(self, packet: Packet, sender_nostr_pubkey: str) -> None:
        """Publish a read receipt for the given packet to all configured relays."""
        event = self._build_receipt(packet, sender_nostr_pubkey)
        for relay_url in self._relay_urls:
            try:
                async with websockets.connect(relay_url) as ws:
                    await ws.send(json.dumps(["EVENT", event]))
                    await asyncio.wait_for(ws.recv(), timeout=10)
            except Exception as exc:
                logger.warning("Failed to send receipt to %s: %s", relay_url, exc)

    def _build_event(self, packet: Packet, recipient_nostr_pubkey: str) -> dict:
        """Build a NIP-01 compliant Nostr event wrapping the Assistant Sync packet."""
        recipient_pubkey = recipient_nostr_pubkey

        content = packet.to_json()
        tags = [
            ["p", recipient_pubkey],
            ["expiration", str(int(datetime.fromisoformat(packet.expires_at).timestamp()))],
            ["aya-version", "0.2"],
            ["aya-packet-id", packet.id],
        ]

        created_at = int(datetime.now(UTC).timestamp())
        event_id = _compute_event_id(
            pubkey=self.public_key_hex,
            created_at=created_at,
            kind=AYA_KIND,
            tags=tags,
            content=content,
        )
        sig = _sign_hex(event_id, self._private_key_hex)

        return {
            "id": event_id,
            "pubkey": self.public_key_hex,
            "created_at": created_at,
            "kind": AYA_KIND,
            "tags": tags,
            "content": content,
            "sig": sig,
        }

    def _build_receipt(self, packet: Packet, sender_nostr_pubkey: str) -> dict:
        content = json.dumps({"packet_id": packet.id, "status": "received"})
        recipient_pubkey = sender_nostr_pubkey
        tags = [
            ["p", recipient_pubkey],
            ["aya-packet-id", packet.id],
            ["aya-version", "0.2"],
        ]
        created_at = int(datetime.now(UTC).timestamp())
        event_id = _compute_event_id(
            pubkey=self.public_key_hex,
            created_at=created_at,
            kind=AYA_RESULT_KIND,
            tags=tags,
            content=content,
        )
        sig = _sign_hex(event_id, self._private_key_hex)
        return {
            "id": event_id,
            "pubkey": self.public_key_hex,
            "created_at": created_at,
            "kind": AYA_RESULT_KIND,
            "tags": tags,
            "content": content,
            "sig": sig,
        }


_EOSE_TIMEOUT = 30.0  # seconds to wait for EOSE before giving up


async def _read_until_eose(
    ws: ClientConnection, sub_id: str, eose_timeout: float = _EOSE_TIMEOUT
) -> AsyncIterator[dict]:
    """Yield EVENT payloads until EOSE (end of stored events) from the relay.

    Raises `TimeoutError` if EOSE is not received within *eose_timeout* seconds.
    """
    async with asyncio.timeout(eose_timeout):
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
