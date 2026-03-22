"""Tests for relay-mediated pairing — code generation, hashing, event structure."""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock, patch

import pytest

from aya.identity import Identity
from aya.pair import (
    _TAG_PAIR_REQ,
    _TAG_PAIR_RESP,
    PAIR_POLL_INTERVAL,
    PAIR_TTL_SECONDS,
    WORD_LIST,
    _build_pair_request,
    _build_pair_response,
    _find_pair_request,
    generate_code,
    hash_code,
    join_pairing,
    poll_for_pair_response,
    publish_pair_request,
)


@pytest.fixture
def work() -> Identity:
    return Identity.generate("work")


@pytest.fixture
def home() -> Identity:
    return Identity.generate("home")


class TestCodeGeneration:
    def test_code_format(self):
        code = generate_code()
        assert re.match(r"^[A-Z]+-[A-Z]+-\d{4}$", code)

    def test_codes_are_unique(self):
        codes = {generate_code() for _ in range(100)}
        assert len(codes) == 100

    def test_words_from_word_list(self):
        code = generate_code()
        parts = code.split("-")
        assert parts[0] in WORD_LIST
        assert parts[1] in WORD_LIST

    def test_number_is_zero_padded(self):
        # Generate enough to likely hit a number < 1000
        for _ in range(200):
            code = generate_code()
            num_part = code.split("-")[2]
            assert len(num_part) == 4


class TestCodeHashing:
    def test_deterministic(self):
        assert hash_code("MESA-TIGER-4927") == hash_code("MESA-TIGER-4927")

    def test_case_insensitive(self):
        assert hash_code("Mesa-Tiger-4927") == hash_code("MESA-TIGER-4927")

    def test_different_codes_different_hashes(self):
        assert hash_code("MESA-TIGER-4927") != hash_code("MESA-TIGER-4928")

    def test_hash_is_hex_sha256(self):
        h = hash_code("TEST-CODE-0000")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestPairRequestEvent:
    def test_has_correct_tags(self, work):
        code_h = hash_code("TEST-CODE-0000")
        event = _build_pair_request(work, "work", code_h, "wss://relay.example.com")

        tags_dict = {t[0]: t[1] for t in event["tags"]}
        assert tags_dict["t"] == _TAG_PAIR_REQ
        assert tags_dict["d"] == code_h
        assert "expiration" in tags_dict

    def test_content_contains_did_and_label(self, work):
        event = _build_pair_request(work, "work", "fakehash", "wss://relay.example.com")
        content = json.loads(event["content"])
        assert content["did"] == work.did
        assert content["label"] == "work"

    def test_expiration_is_10_minutes(self, work):
        event = _build_pair_request(work, "work", "fakehash", "wss://relay.example.com")
        created = event["created_at"]
        exp_tag = next(t[1] for t in event["tags"] if t[0] == "expiration")
        assert int(exp_tag) - created == PAIR_TTL_SECONDS

    def test_event_is_signed(self, work):
        event = _build_pair_request(work, "work", "fakehash", "wss://relay.example.com")
        assert event["sig"]
        assert event["id"]
        assert event["pubkey"] == work.nostr_public_hex


class TestPairResponseEvent:
    def test_references_request(self, home):
        event = _build_pair_response(
            home, "home", "initiator_pubkey_hex", "request_event_id_abc", "wss://relay.example.com"
        )
        e_tags = [t for t in event["tags"] if t[0] == "e"]
        assert len(e_tags) == 1
        assert e_tags[0][1] == "request_event_id_abc"

    def test_uses_standard_e_tag_for_nostr_event_id(self, home):
        """Standard 'e' tag should reference the pair-request event id (a real Nostr event ID)."""
        event = _build_pair_response(
            home, "home", "initiator_pubkey_hex", "request_event_id_abc", "wss://relay.example.com"
        )
        e_tags = [t for t in event["tags"] if t[0] == "e"]
        assert len(e_tags) == 1, "standard e tag must reference the pair-request event id"
        custom_tags = [t for t in event["tags"] if t[0] == "aya-pair-request-id"]
        assert custom_tags == [], "aya-pair-request-id tag should not be used"

    def test_addresses_initiator(self, home):
        event = _build_pair_response(
            home, "home", "initiator_pubkey_hex", "req123", "wss://relay.example.com"
        )
        p_tags = [t for t in event["tags"] if t[0] == "p"]
        assert len(p_tags) == 1
        assert p_tags[0][1] == "initiator_pubkey_hex"

    def test_content_contains_did_and_label(self, home):
        event = _build_pair_response(home, "home", "init_pub", "req123", "wss://relay.example.com")
        content = json.loads(event["content"])
        assert content["did"] == home.did
        assert content["label"] == "home"

    def test_type_tag_is_pair_response(self, home):
        event = _build_pair_response(home, "home", "init_pub", "req123", "wss://relay.example.com")
        t_tags = [t for t in event["tags"] if t[0] == "t"]
        assert t_tags[0][1] == _TAG_PAIR_RESP


def _make_ws_mock(messages: list[str], ok_response: bool = True) -> AsyncMock:
    """Create a mock websocket that yields messages as an async iterator and responds OK."""

    class FakeWS:
        def __init__(self):
            self._messages = list(messages)
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            return json.dumps(["OK", "event_id", ok_response, ""])

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._messages:
                return self._messages.pop(0)
            raise StopAsyncIteration

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    return FakeWS()


class TestPairingFlowMocked:
    """Integration tests with mocked WebSocket connections."""

    async def test_publish_pair_request_returns_event_id(self, work):
        ws = _make_ws_mock([])
        with patch("aya.pair.websockets.connect", return_value=ws):
            event_id = await publish_pair_request(work, "work", "code_hash", "wss://relay.test")
            assert event_id
            assert len(event_id) == 64  # sha256 hex

    async def test_join_pairing_success(self, work, home):
        code = "TEST-CODE-1234"
        code_h = hash_code(code)
        request_event = _build_pair_request(work, "work", code_h, "wss://relay.test")

        # Patch at the function level — _find_pair_request returns the event,
        # publish goes through a simple mock ws
        with (
            patch(
                "aya.pair._find_pair_request",
                return_value=request_event,
            ),
            patch(
                "aya.pair.websockets.connect",
                return_value=_make_ws_mock([]),
            ),
        ):
            trusted = await join_pairing(home, "home", code, "wss://relay.test")

        assert trusted.did == work.did
        assert trusted.label == "work"
        assert trusted.nostr_pubkey == work.nostr_public_hex

    async def test_find_pair_request_no_match(self):
        # Relay returns EOSE immediately — no matching events
        ws = _make_ws_mock([json.dumps(["EOSE", "sub1"])])

        with patch("aya.pair.websockets.connect", return_value=ws):
            result = await _find_pair_request("wss://relay.test", "nonexistent_hash")

        assert result is None


class TestPollForPairResponseErrors:
    """Verify that poll_for_pair_response distinguishes timeout from connection errors."""

    async def test_returns_none_on_eose_timeout(self, work):
        """EOSE timeout is normal operation — returns None, no exception raised."""
        with patch("aya.pair._read_until_eose", side_effect=TimeoutError), patch(
            "aya.pair.asyncio.sleep"
        ):
            mock_ws = AsyncMock()
            mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws.__aexit__ = AsyncMock(return_value=False)
            mock_ws.send = AsyncMock()

            with patch("aya.pair.websockets.connect", return_value=mock_ws):
                result = await poll_for_pair_response(
                    "wss://relay.test", work.nostr_public_hex, "req_event_id", timeout_seconds=1
                )

        assert result is None

    async def test_returns_none_on_connection_error(self, work):
        """Connection errors are logged as warnings and return None instead of raising."""

        class FakeConnectionError(Exception):
            pass

        with patch(
            "aya.pair.websockets.connect",
            side_effect=FakeConnectionError("connection refused"),
        ):
            result = await poll_for_pair_response(
                "wss://relay.test", work.nostr_public_hex, "req_event_id", timeout_seconds=1
            )

        assert result is None

    async def test_uses_standard_e_tag_filter(self, work):
        """poll_for_pair_response must use #e (standard Nostr event tag) in its filter."""
        sent_filters = []

        class FilterCapturingWS:
            async def send(self, data):
                msg = json.loads(data)
                if msg[0] == "REQ":
                    sent_filters.append(msg[2])

            async def recv(self):
                return json.dumps(["EOSE", "sub"])

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("aya.pair.websockets.connect", return_value=FilterCapturingWS()), patch(
            "aya.pair.asyncio.sleep"
        ):
            await poll_for_pair_response(
                "wss://relay.test", work.nostr_public_hex, "req_event_id_abc", timeout_seconds=1
            )

        assert sent_filters, "Expected at least one REQ to be sent"
        assert "#e" in sent_filters[0]
        assert sent_filters[0]["#e"] == ["req_event_id_abc"]
        assert "#aya-pair-request-id" not in sent_filters[0]

    async def test_sleep_respects_remaining_deadline(self, work):
        """poll_for_pair_response sleeps at most until the deadline, never longer."""
        sleep_durations = []

        original_sleep = asyncio.sleep

        async def capturing_sleep(t):
            sleep_durations.append(t)
            await original_sleep(0)  # don't actually wait

        with patch("aya.pair._read_until_eose", side_effect=TimeoutError), patch(
            "aya.pair.asyncio.sleep", side_effect=capturing_sleep
        ):
            mock_ws = AsyncMock()
            mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws.__aexit__ = AsyncMock(return_value=False)
            mock_ws.send = AsyncMock()

            with patch("aya.pair.websockets.connect", return_value=mock_ws):
                await poll_for_pair_response(
                    "wss://relay.test", work.nostr_public_hex, "req_event_id", timeout_seconds=1
                )

        # Every sleep duration must be ≤ PAIR_POLL_INTERVAL
        for duration in sleep_durations:
            assert duration <= PAIR_POLL_INTERVAL
