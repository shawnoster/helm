"""Tests for relay.py — event building, receipt building, fetch_pending, helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aya.identity import Identity
from aya.packet import Packet
from aya.relay import (
    AYA_KIND,
    AYA_RESULT_KIND,
    RelayClient,
    RelayError,
    _compute_event_id,
    _read_until_eose,
    _sign_hex,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sender() -> Identity:
    return Identity.generate("work")


@pytest.fixture
def recipient() -> Identity:
    return Identity.generate("home")


@pytest.fixture
def client(sender: Identity) -> RelayClient:
    return RelayClient(
        relay_urls="wss://relay.example.com",
        nostr_private_hex=sender.nostr_private_hex,
        nostr_public_hex=sender.nostr_public_hex,
    )


@pytest.fixture
def packet(sender: Identity, recipient: Identity) -> Packet:
    return Packet(
        **{"from": sender.did, "to": recipient.did},
        intent="Test packet for relay",
        content="Hello from work.",
    )


# ── _compute_event_id ─────────────────────────────────────────────────────────


class TestComputeEventId:
    def test_deterministic(self) -> None:
        result1 = _compute_event_id("aabbcc", 1000, 5999, [], "content")
        result2 = _compute_event_id("aabbcc", 1000, 5999, [], "content")
        assert result1 == result2

    def test_returns_64_hex_chars(self) -> None:
        event_id = _compute_event_id("aabbcc", 1000, 5999, [], "hi")
        assert len(event_id) == 64
        assert all(c in "0123456789abcdef" for c in event_id)

    def test_different_pubkeys_give_different_ids(self) -> None:
        id1 = _compute_event_id("aabbcc", 1000, 5999, [], "content")
        id2 = _compute_event_id("ddeeff", 1000, 5999, [], "content")
        assert id1 != id2

    def test_serialisation_nip01_format(self) -> None:
        """Verify NIP-01 canonical serialisation: [0, pubkey, created_at, kind, tags, content]."""
        pubkey = "aabbcc"
        created_at = 1234567890
        kind = 5999
        tags = [["p", "deadbeef"]]
        content = "hello"

        serialised = json.dumps(
            [0, pubkey, created_at, kind, tags, content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        expected = hashlib.sha256(serialised.encode()).hexdigest()
        assert _compute_event_id(pubkey, created_at, kind, tags, content) == expected


# ── _sign_hex ─────────────────────────────────────────────────────────────────


class TestSignHex:
    def test_returns_128_hex_chars(self, sender: Identity) -> None:
        event_id = "a" * 64  # 32 bytes hex
        sig = _sign_hex(event_id, sender.nostr_private_hex)
        assert len(sig) == 128
        assert all(c in "0123456789abcdef" for c in sig)

    def test_different_keys_give_different_signatures(
        self, sender: Identity, recipient: Identity
    ) -> None:
        event_id = "c" * 64
        sig1 = _sign_hex(event_id, sender.nostr_private_hex)
        sig2 = _sign_hex(event_id, recipient.nostr_private_hex)
        assert sig1 != sig2


# ── _build_event ──────────────────────────────────────────────────────────────


class TestBuildEvent:
    def test_nostr_event_structure(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        assert set(event.keys()) == {"id", "pubkey", "created_at", "kind", "tags", "content", "sig"}

    def test_kind_is_aya(self, client: RelayClient, packet: Packet, recipient: Identity) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        assert event["kind"] == AYA_KIND

    def test_pubkey_is_sender(
        self, client: RelayClient, packet: Packet, sender: Identity, recipient: Identity
    ) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        assert event["pubkey"] == sender.nostr_public_hex

    def test_content_is_packet_json(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        restored = Packet.from_json(event["content"])
        assert restored.id == packet.id
        assert restored.intent == packet.intent

    def test_has_recipient_p_tag(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        p_tags = [t for t in event["tags"] if t[0] == "p"]
        assert len(p_tags) == 1
        assert p_tags[0][1] == recipient.nostr_public_hex

    def test_has_expiration_tag(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        exp_tags = [t for t in event["tags"] if t[0] == "expiration"]
        assert len(exp_tags) == 1
        assert exp_tags[0][1].isdigit()

    def test_has_packet_id_tag(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        pid_tags = [t for t in event["tags"] if t[0] == "aya-packet-id"]
        assert len(pid_tags) == 1
        assert pid_tags[0][1] == packet.id

    def test_event_id_is_sha256_of_canonical_form(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        expected_id = _compute_event_id(
            pubkey=event["pubkey"],
            created_at=event["created_at"],
            kind=event["kind"],
            tags=event["tags"],
            content=event["content"],
        )
        assert event["id"] == expected_id

    def test_sig_length_is_128_hex_chars(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        event = client._build_event(packet, recipient.nostr_public_hex)
        assert len(event["sig"]) == 128


# ── _build_receipt ────────────────────────────────────────────────────────────


class TestBuildReceipt:
    def test_receipt_structure(self, client: RelayClient, packet: Packet, sender: Identity) -> None:
        receipt = client._build_receipt(packet, sender.nostr_public_hex)
        assert set(receipt.keys()) == {
            "id",
            "pubkey",
            "created_at",
            "kind",
            "tags",
            "content",
            "sig",
        }

    def test_kind_is_result(self, client: RelayClient, packet: Packet, sender: Identity) -> None:
        receipt = client._build_receipt(packet, sender.nostr_public_hex)
        assert receipt["kind"] == AYA_RESULT_KIND

    def test_content_has_packet_id_and_status(
        self, client: RelayClient, packet: Packet, sender: Identity
    ) -> None:
        receipt = client._build_receipt(packet, sender.nostr_public_hex)
        data = json.loads(receipt["content"])
        assert data["packet_id"] == packet.id
        assert data["status"] == "received"

    def test_has_aya_packet_id_tag(
        self, client: RelayClient, packet: Packet, sender: Identity
    ) -> None:
        receipt = client._build_receipt(packet, sender.nostr_public_hex)
        pid_tags = [t for t in receipt["tags"] if t[0] == "aya-packet-id"]
        assert len(pid_tags) == 1
        assert pid_tags[0][1] == packet.id

    def test_has_p_tag_with_sender(
        self, client: RelayClient, packet: Packet, sender: Identity
    ) -> None:
        receipt = client._build_receipt(packet, sender.nostr_public_hex)
        p_tags = [t for t in receipt["tags"] if t[0] == "p"]
        assert len(p_tags) == 1
        assert p_tags[0][1] == sender.nostr_public_hex

    def test_receipt_id_matches_canonical(
        self, client: RelayClient, packet: Packet, sender: Identity
    ) -> None:
        receipt = client._build_receipt(packet, sender.nostr_public_hex)
        expected_id = _compute_event_id(
            pubkey=receipt["pubkey"],
            created_at=receipt["created_at"],
            kind=receipt["kind"],
            tags=receipt["tags"],
            content=receipt["content"],
        )
        assert receipt["id"] == expected_id


# ── _read_until_eose ──────────────────────────────────────────────────────────


class TestReadUntilEose:
    async def test_yields_events_until_eose(self) -> None:
        sub_id = "test-sub"
        event1 = {"id": "e1", "content": "hello"}
        event2 = {"id": "e2", "content": "world"}

        messages = [
            json.dumps(["EVENT", sub_id, event1]),
            json.dumps(["EVENT", sub_id, event2]),
            json.dumps(["EOSE", sub_id]),
        ]

        async def _aiter(msgs: list[str]):
            for m in msgs:
                yield m

        mock_ws = MagicMock()
        mock_ws.__aiter__ = lambda self: _aiter(messages)

        collected = []
        async for evt in _read_until_eose(mock_ws, sub_id):
            collected.append(evt)

        assert len(collected) == 2
        assert collected[0]["id"] == "e1"
        assert collected[1]["id"] == "e2"

    async def test_stops_at_eose(self) -> None:
        sub_id = "sub-x"
        messages = [
            json.dumps(["EOSE", sub_id]),
            json.dumps(["EVENT", sub_id, {"id": "after-eose"}]),
        ]

        async def _aiter(msgs: list[str]):
            for m in msgs:
                yield m

        mock_ws = MagicMock()
        mock_ws.__aiter__ = lambda self: _aiter(messages)

        collected = []
        async for evt in _read_until_eose(mock_ws, sub_id):
            collected.append(evt)

        assert collected == []

    async def test_ignores_events_for_other_sub_id(self) -> None:
        sub_id = "my-sub"
        messages = [
            json.dumps(["EVENT", "other-sub", {"id": "not-mine"}]),
            json.dumps(["EOSE", sub_id]),
        ]

        async def _aiter(msgs: list[str]):
            for m in msgs:
                yield m

        mock_ws = MagicMock()
        mock_ws.__aiter__ = lambda self: _aiter(messages)

        collected = []
        async for evt in _read_until_eose(mock_ws, sub_id):
            collected.append(evt)

        assert collected == []

    async def test_raises_timeout_if_eose_never_arrives(self) -> None:
        """_read_until_eose raises TimeoutError when EOSE is not received in time."""
        sub_id = "hang-sub"

        async def _aiter_infinite():
            while True:
                await asyncio.sleep(10)  # never yields a message
                yield ""  # pragma: no cover

        mock_ws = MagicMock()
        mock_ws.__aiter__ = lambda self: _aiter_infinite()

        with pytest.raises(TimeoutError):
            async for _ in _read_until_eose(mock_ws, sub_id, eose_timeout=0.05):
                pass  # pragma: no cover


# ── fetch_pending ─────────────────────────────────────────────────────────────


class TestFetchPending:
    async def test_yields_valid_packets(
        self, client: RelayClient, sender: Identity, recipient: Identity
    ) -> None:
        """fetch_pending should yield un-expired packets from relay events."""
        p = Packet(
            **{"from": sender.did, "to": recipient.did},
            intent="Remote task",
            content="Context data.",
        )
        raw_event = {"id": "evt1", "content": p.to_json()}

        async def fake_read_until_eose(ws, sub_id):
            yield raw_event

        with patch("aya.relay._read_until_eose", side_effect=fake_read_until_eose):
            mock_ws = AsyncMock()
            mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws.__aexit__ = AsyncMock(return_value=False)

            with patch("aya.relay.websockets.connect", return_value=mock_ws):
                packets = [pkt async for pkt in client.fetch_pending()]

        assert len(packets) == 1
        assert packets[0].id == p.id
        assert packets[0].intent == p.intent

    async def test_skips_expired_packets(
        self, client: RelayClient, sender: Identity, recipient: Identity
    ) -> None:
        past = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        p = Packet(
            **{"from": sender.did, "to": recipient.did},
            intent="Old packet",
            content="Stale.",
            expires_at=past,
        )
        raw_event = {"id": "evt-old", "content": p.to_json()}

        async def fake_read_until_eose(ws, sub_id):
            yield raw_event

        with patch("aya.relay._read_until_eose", side_effect=fake_read_until_eose):
            mock_ws = AsyncMock()
            mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws.__aexit__ = AsyncMock(return_value=False)

            with patch("aya.relay.websockets.connect", return_value=mock_ws):
                packets = [pkt async for pkt in client.fetch_pending()]

        assert packets == []

    async def test_skips_malformed_events(self, client: RelayClient) -> None:
        bad_event = {"id": "bad", "content": "not-json-packet!!!"}

        async def fake_read_until_eose(ws, sub_id):
            yield bad_event

        with patch("aya.relay._read_until_eose", side_effect=fake_read_until_eose):
            mock_ws = AsyncMock()
            mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws.__aexit__ = AsyncMock(return_value=False)

            with patch("aya.relay.websockets.connect", return_value=mock_ws):
                packets = [pkt async for pkt in client.fetch_pending()]

        assert packets == []

    async def test_handles_eose_timeout_gracefully(
        self, client: RelayClient, sender: Identity, recipient: Identity
    ) -> None:
        """fetch_pending should return whatever packets arrived before EOSE timeout."""
        p = Packet(
            **{"from": sender.did, "to": recipient.did},
            intent="Partial fetch",
            content="Some data.",
        )
        raw_event = {"id": "evt-partial", "content": p.to_json()}

        async def fake_read_timeout(ws, sub_id):
            yield raw_event
            raise TimeoutError

        with patch("aya.relay._read_until_eose", side_effect=fake_read_timeout):
            mock_ws = AsyncMock()
            mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws.__aexit__ = AsyncMock(return_value=False)

            with patch("aya.relay.websockets.connect", return_value=mock_ws):
                packets = [pkt async for pkt in client.fetch_pending()]

        assert len(packets) == 1
        assert packets[0].id == p.id


# ── publish ───────────────────────────────────────────────────────────────────


class TestPublish:
    async def test_publish_returns_event_id(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        fake_event_id = "a" * 64
        mock_ws.recv = AsyncMock(return_value=json.dumps(["OK", fake_event_id, True, ""]))

        with patch("aya.relay.websockets.connect", return_value=mock_ws):
            event_id = await client.publish(packet, recipient.nostr_public_hex)

        assert isinstance(event_id, str)
        assert len(event_id) == 64

    async def test_publish_raises_on_relay_rejection(
        self, client: RelayClient, packet: Packet, recipient: Identity
    ) -> None:
        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        mock_ws.recv = AsyncMock(return_value=json.dumps(["OK", "x" * 64, False, "rate-limited"]))

        with (
            patch("aya.relay.websockets.connect", return_value=mock_ws),
            pytest.raises(RelayError),
        ):
            await client.publish(packet, recipient.nostr_public_hex)


# ── Backoff helpers ───────────────────────────────────────────────────────────


class TestBackoffHelpers:
    def test_backoff_delay_increases_with_attempt(self) -> None:
        from aya.relay import _backoff_delay

        with patch("aya.relay.random.random", return_value=0.5):  # no jitter
            d0 = _backoff_delay(0)
            d1 = _backoff_delay(1)
            d2 = _backoff_delay(2)

        assert d0 < d1 < d2

    def test_backoff_delay_caps_at_60s(self) -> None:
        from aya.relay import _backoff_delay

        with patch("aya.relay.random.random", return_value=0.5):  # no jitter
            assert _backoff_delay(20) <= 60.0

    def test_backoff_delay_nonnegative(self) -> None:
        from aya.relay import _backoff_delay

        with patch("aya.relay.random.random", return_value=0.0):  # max negative jitter
            assert _backoff_delay(0) >= 0.0

    def test_is_rate_limited_true(self) -> None:
        from aya.relay import _is_rate_limited

        assert _is_rate_limited(["OK", "abc", False, "rate-limited: slow down"])

    def test_is_rate_limited_false_for_ok_accepted(self) -> None:
        from aya.relay import _is_rate_limited

        assert not _is_rate_limited(["OK", "abc", True, ""])

    def test_is_rate_limited_false_for_other_rejection(self) -> None:
        from aya.relay import _is_rate_limited

        assert not _is_rate_limited(["OK", "abc", False, "blocked: spam"])


# ── Multi-relay publish ───────────────────────────────────────────────────────


class TestMultiRelayPublish:
    async def test_publish_fans_out_to_all_relays(
        self, sender: Identity, packet: Packet, recipient: Identity
    ) -> None:
        """publish() should attempt both relay URLs."""
        connected_urls: list[str] = []

        def fake_connect(url, **kwargs):
            connected_urls.append(url)
            mock_ws = AsyncMock()
            mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws.__aexit__ = AsyncMock(return_value=False)
            mock_ws.recv = AsyncMock(return_value=json.dumps(["OK", "a" * 64, True, ""]))
            return mock_ws

        multi_client = RelayClient(
            relay_urls=["wss://relay1.example.com", "wss://relay2.example.com"],
            nostr_private_hex=sender.nostr_private_hex,
            nostr_public_hex=sender.nostr_public_hex,
        )

        with patch("aya.relay.websockets.connect", side_effect=fake_connect):
            event_id = await multi_client.publish(packet, recipient.nostr_public_hex)

        assert set(connected_urls) == {"wss://relay1.example.com", "wss://relay2.example.com"}
        assert len(event_id) == 64

    async def test_publish_succeeds_if_one_relay_accepts(
        self, sender: Identity, packet: Packet, recipient: Identity
    ) -> None:
        """publish() succeeds even when the first relay rejects."""
        call_count = 0

        def fake_connect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_ws = AsyncMock()
            mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws.__aexit__ = AsyncMock(return_value=False)
            # First relay rejects; second accepts
            if call_count == 1:
                mock_ws.recv = AsyncMock(
                    return_value=json.dumps(["OK", "a" * 64, False, "blocked"])
                )
            else:
                mock_ws.recv = AsyncMock(return_value=json.dumps(["OK", "b" * 64, True, ""]))
            return mock_ws

        multi_client = RelayClient(
            relay_urls=["wss://relay1.example.com", "wss://relay2.example.com"],
            nostr_private_hex=sender.nostr_private_hex,
            nostr_public_hex=sender.nostr_public_hex,
        )

        with patch("aya.relay.websockets.connect", side_effect=fake_connect):
            event_id = await multi_client.publish(packet, recipient.nostr_public_hex)

        assert len(event_id) == 64

    async def test_publish_raises_when_all_relays_fail(
        self, sender: Identity, packet: Packet, recipient: Identity
    ) -> None:
        """publish() raises RelayError only if all relays fail."""
        multi_client = RelayClient(
            relay_urls=["wss://relay1.example.com", "wss://relay2.example.com"],
            nostr_private_hex=sender.nostr_private_hex,
            nostr_public_hex=sender.nostr_public_hex,
        )

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)
        mock_ws.recv = AsyncMock(return_value=json.dumps(["OK", "a" * 64, False, "blocked"]))

        with (
            patch("aya.relay.websockets.connect", return_value=mock_ws),
            pytest.raises(RelayError),
        ):
            await multi_client.publish(packet, recipient.nostr_public_hex)

    async def test_publish_retries_on_rate_limit(
        self, sender: Identity, packet: Packet, recipient: Identity
    ) -> None:
        """publish() retries after a rate-limited response."""
        client_single = RelayClient(
            relay_urls="wss://relay.example.com",
            nostr_private_hex=sender.nostr_private_hex,
            nostr_public_hex=sender.nostr_public_hex,
        )
        recv_iter = iter(
            [
                json.dumps(["OK", "a" * 64, False, "rate-limited: too fast"]),
                json.dumps(["OK", "a" * 64, True, ""]),
            ]
        )
        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)
        mock_ws.recv = AsyncMock(side_effect=lambda: next(recv_iter))

        with (
            patch("aya.relay.websockets.connect", return_value=mock_ws),
            patch("aya.relay.asyncio.sleep"),
        ):
            event_id = await client_single.publish(packet, recipient.nostr_public_hex)

        assert len(event_id) == 64


# ── Multi-relay fetch ─────────────────────────────────────────────────────────


class TestMultiRelayFetch:
    async def test_fetch_deduplicates_across_relays(
        self, sender: Identity, recipient: Identity
    ) -> None:
        """fetch_pending() deduplicates packets returned by multiple relays."""
        p = Packet(
            **{"from": sender.did, "to": recipient.did},
            intent="Dedup test",
            content="Content.",
        )

        async def fake_read_eose(ws, sub_id):
            yield {"id": "evt1", "content": p.to_json()}

        multi_client = RelayClient(
            relay_urls=["wss://relay1.example.com", "wss://relay2.example.com"],
            nostr_private_hex=sender.nostr_private_hex,
            nostr_public_hex=sender.nostr_public_hex,
        )

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aya.relay._read_until_eose", side_effect=fake_read_eose),
            patch("aya.relay.websockets.connect", return_value=mock_ws),
        ):
            packets = [pkt async for pkt in multi_client.fetch_pending()]

        # Same packet from two relays → only one copy
        assert len(packets) == 1
        assert packets[0].id == p.id

    async def test_single_url_string_backward_compat(self, sender: Identity) -> None:
        """RelayClient still accepts a plain string relay_urls."""
        c = RelayClient(
            relay_urls="wss://relay.example.com",
            nostr_private_hex=sender.nostr_private_hex,
            nostr_public_hex=sender.nostr_public_hex,
        )
        assert c.relay_url == "wss://relay.example.com"
        assert c._relay_urls == ["wss://relay.example.com"]
