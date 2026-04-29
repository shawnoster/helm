"""Tests for the MCP server module."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from aya.mcp_server import _TOOLS, call_tool

# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


def test_list_tools_names():
    """All expected tools are declared."""
    names = {t.name for t in _TOOLS}
    assert names == {
        "aya_status",
        "aya_inbox",
        "aya_send",
        "aya_receive",
        "aya_schedule_remind",
        "aya_schedule_watch",
        "aya_ack",
        "aya_read",
        "aya_config_set",
        "aya_config_show",
        "aya_packets",
        "aya_relay_status",
    }


def test_list_tools_have_schemas():
    """Every tool has a non-empty inputSchema."""
    for tool in _TOOLS:
        assert tool.inputSchema, f"{tool.name} missing inputSchema"
        assert tool.inputSchema.get("type") == "object"


# ---------------------------------------------------------------------------
# aya_status
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_gather_status(monkeypatch):
    """Patch _gather_status so we don't need a real profile on disk."""
    from datetime import UTC, datetime

    from aya.credentials import CredentialsReport

    fake_data = {
        "now_local": datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        "ship": "GSV Test Ship",
        "user": "Tester",
        "next_eval": "2026-04-03",
        "checks": [],
        "checks_ok": 0,
        "checks_total": 0,
        "credentials": CredentialsReport(services=[], lit=0, partial=0, dark=0),
        "unseen": [],
        "due": [],
        "upcoming": [],
        "active_watches": [],
    }
    monkeypatch.setattr("aya.status._gather_status", lambda: fake_data)


@pytest.mark.usefixtures("_mock_gather_status")
async def test_status_tool():
    """aya_status returns valid JSON with expected keys."""
    result = await call_tool("aya_status", {})
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert "greeting" in payload
    assert "systems" in payload


# ---------------------------------------------------------------------------
# aya_schedule_remind
# ---------------------------------------------------------------------------


async def test_schedule_remind_tool(tmp_path, monkeypatch):
    """aya_schedule_remind creates a reminder via the scheduler."""
    sched_file = tmp_path / "scheduler.json"
    sched_file.write_text(json.dumps({"schema_version": 2, "items": []}))

    lock_file = tmp_path / ".scheduler.lock"

    monkeypatch.setattr("aya.scheduler.storage._scheduler_file", lambda: sched_file)
    monkeypatch.setattr("aya.scheduler.storage._lock_file", lambda: lock_file)

    # Reset cached lazy attrs so monkeypatch takes effect
    import aya.scheduler as sched_mod

    for attr in ("SCHEDULER_FILE", "LOCK_FILE"):
        sched_mod.__dict__.pop(attr, None)

    result = await call_tool(
        "aya_schedule_remind", {"message": "Test reminder", "due": "in 1 hour"}
    )
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert payload["type"] == "reminder"
    assert payload["message"] == "Test reminder"
    assert payload["status"] == "pending"


# ---------------------------------------------------------------------------
# aya_send
# ---------------------------------------------------------------------------


async def test_send_tool():
    """aya_send builds a packet and publishes it via a mocked RelayClient."""
    from aya.identity import Identity, Profile, TrustedKey

    fake_identity = Identity.generate("default")
    peer_identity = Identity.generate("peer")
    fake_profile = Profile(
        alias="Test",
        ship_mind_name="GSV Test",
        user_name="Tester",
        instances={"default": fake_identity},
        trusted_keys={
            "peer": TrustedKey(
                did=peer_identity.did,
                label="peer",
                nostr_pubkey=peer_identity.nostr_public_hex,
            ),
        },
        ingested_ids=[],
        default_relays=["wss://relay.example.com"],
        last_checked={},
    )

    mock_publish = AsyncMock(return_value="abc123eventid")

    with (
        patch("aya.mcp_server._load_profile", return_value=fake_profile),
        patch("aya.relay.RelayClient.publish", mock_publish),
    ):
        result = await call_tool(
            "aya_send",
            {
                "to": "peer",
                "intent": "test-intent",
                "content": "Hello from MCP",
            },
        )

    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert "packet_id" in payload
    assert payload["event_id"] == "abc123eventid"
    mock_publish.assert_awaited_once()


async def test_send_tool_with_in_reply_to(tmp_path):
    """aya_send with in_reply_to sets the field on the published packet."""
    from aya.identity import Identity, Profile, TrustedKey

    fake_identity = Identity.generate("default")
    peer_identity = Identity.generate("peer")
    fake_profile = Profile(
        alias="Test",
        ship_mind_name="",
        user_name="Tester",
        instances={"default": fake_identity},
        trusted_keys={
            "peer": TrustedKey(
                did=peer_identity.did,
                label="peer",
                nostr_pubkey=peer_identity.nostr_public_hex,
            ),
        },
        ingested_ids=[],
        default_relays=["wss://relay.example.com"],
        last_checked={},
    )

    mock_publish = AsyncMock(return_value="def456eventid")

    with (
        patch("aya.mcp_server._load_profile", return_value=fake_profile),
        patch("aya.relay.RelayClient.publish", mock_publish),
    ):
        result = await call_tool(
            "aya_send",
            {
                "to": "peer",
                "intent": "reply-test",
                "content": "This is a reply",
                "in_reply_to": "01JABC1234PARENT00000000000",
            },
        )

    payload = json.loads(result[0].text)
    assert "packet_id" in payload
    # Verify the published packet has in_reply_to set
    published_packet = mock_publish.call_args[0][0]
    assert published_packet.in_reply_to == "01JABC1234PARENT00000000000"


# ---------------------------------------------------------------------------
# aya_inbox
# ---------------------------------------------------------------------------


async def test_inbox_tool(tmp_path):
    """aya_inbox returns a list of packets (empty when relay yields nothing)."""
    from aya.identity import Identity, Profile, TrustedKey

    local = Identity.generate("default")
    home = Identity.generate("home")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["default"] = local
    profile.trusted_keys["home"] = TrustedKey(
        did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
    )
    profile_path = tmp_path / "profile.json"
    profile.save(profile_path)

    async def mock_fetch(*args, **kwargs):
        if False:  # pragma: no cover
            yield

    with (
        patch("aya.paths.PROFILE_PATH", profile_path),
        patch("aya.relay.RelayClient") as mock_cls,
    ):
        mock_cls.return_value.fetch_pending = mock_fetch
        result = await call_tool("aya_inbox", {"instance": "default"})

    payload = json.loads(result[0].text)
    assert isinstance(payload, list)


async def test_inbox_filters_dropped_packets(tmp_path):
    """aya_inbox must not return packets whose IDs are in dropped_ids.

    Regression for surface-drift bug: MCP inbox was missing the dropped_ids
    filter that CLI inbox already applied, causing `aya drop` to silence CLI
    but not MCP.
    """
    from aya.identity import Identity, Profile, TrustedKey
    from aya.packet import Packet

    local = Identity.generate("default")
    sender = Identity.generate("work")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["default"] = local
    profile.trusted_keys["work"] = TrustedKey(
        did=sender.did, label="work", nostr_pubkey=sender.nostr_public_hex
    )
    profile_path = tmp_path / "profile.json"

    kept = Packet(**{"from": sender.did, "to": local.did}, intent="kept packet").sign(sender)
    dropped = Packet(**{"from": sender.did, "to": local.did}, intent="dropped packet").sign(sender)

    # Pre-populate dropped_ids with the second packet's ID.
    profile.dropped_ids.append(dropped.id)
    profile.save(profile_path)

    async def mock_fetch(*args, **kwargs):
        yield kept
        yield dropped

    with (
        patch("aya.paths.PROFILE_PATH", profile_path),
        patch("aya.mcp_server._load_profile", return_value=profile),
        patch("aya.relay.RelayClient") as mock_cls,
    ):
        mock_cls.return_value.fetch_pending = mock_fetch
        result = await call_tool("aya_inbox", {"instance": "default"})

    payload = json.loads(result[0].text)
    assert isinstance(payload, list)
    returned_ids = [p["id"] for p in payload]
    assert kept.id in returned_ids, "non-dropped packet must appear in inbox"
    assert dropped.id not in returned_ids, "dropped packet must be filtered from inbox"


async def test_inbox_includes_trusted_flag(tmp_path):
    """aya_inbox summaries must include a 'trusted' boolean field.

    Ensures callers (agents) can distinguish trusted vs untrusted senders
    without a separate lookup, matching the CLI inbox JSON output.
    """
    from aya.identity import Identity, Profile, TrustedKey
    from aya.packet import Packet

    local = Identity.generate("default")
    trusted_sender = Identity.generate("friend")
    untrusted_sender = Identity.generate("stranger")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["default"] = local
    profile.trusted_keys["friend"] = TrustedKey(
        did=trusted_sender.did, label="friend", nostr_pubkey=trusted_sender.nostr_public_hex
    )
    profile_path = tmp_path / "profile.json"
    profile.save(profile_path)

    trusted_pkt = Packet(
        **{"from": trusted_sender.did, "to": local.did}, intent="trusted msg"
    ).sign(trusted_sender)
    untrusted_pkt = Packet(
        **{"from": untrusted_sender.did, "to": local.did}, intent="untrusted msg"
    ).sign(untrusted_sender)

    async def mock_fetch(*args, **kwargs):
        yield trusted_pkt
        yield untrusted_pkt

    with (
        patch("aya.paths.PROFILE_PATH", profile_path),
        patch("aya.mcp_server._load_profile", return_value=profile),
        patch("aya.relay.RelayClient") as mock_cls,
    ):
        mock_cls.return_value.fetch_pending = mock_fetch
        result = await call_tool("aya_inbox", {"instance": "default"})

    payload = json.loads(result[0].text)
    by_id = {p["id"]: p for p in payload}

    assert by_id[trusted_pkt.id]["trusted"] is True
    assert by_id[untrusted_pkt.id]["trusted"] is False


# ---------------------------------------------------------------------------
# aya_receive
# ---------------------------------------------------------------------------


async def test_receive_writes_packet_body_to_disk(tmp_path):
    """aya_receive persists ingested packet content to PACKETS_DIR.

    Regression for the cursor-advances-but-body-discarded bug: previously the
    MCP handler appended to ``ingested_ids`` without calling ``_ingest``, so
    subsequent polls skipped the event and the content was unreachable via
    ``aya_read``/``aya_packets``.
    """
    from aya.identity import Identity, Profile, TrustedKey
    from aya.packet import Packet

    local = Identity.generate("default")
    home = Identity.generate("home")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["default"] = local
    profile.trusted_keys["home"] = TrustedKey(
        did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
    )
    profile_path = tmp_path / "profile.json"
    profile.save(profile_path)

    packets_dir = tmp_path / "packets"

    signed = Packet(**{"from": home.did, "to": local.did}, intent="regression check").sign(home)

    async def mock_fetch(*args, **kwargs):
        yield signed

    with (
        patch("aya.paths.PROFILE_PATH", profile_path),
        patch("aya.paths.PACKETS_DIR", packets_dir),
        patch("aya.mcp_server._load_profile", return_value=profile),
        patch("aya.relay.RelayClient") as mock_cls,
    ):
        mock_cls.return_value.fetch_pending = mock_fetch
        result = await call_tool("aya_receive", {"instance": "default"})

    payload = json.loads(result[0].text)
    assert len(payload) == 1
    assert payload[0]["id"] == signed.id
    assert payload[0]["ingested"] is True

    written = packets_dir / f"{signed.id}.json"
    assert written.exists(), "packet body must be persisted to PACKETS_DIR"
    assert Packet.from_json(written.read_text()).intent == "regression check"
    assert any(entry["id"] == signed.id for entry in profile.ingested_ids)


async def test_receive_skips_cursor_when_persist_fails(tmp_path):
    """If _ingest fails to write the packet body, the cursor must not advance.

    Otherwise we re-introduce the original cursor-advances-but-body-discarded
    bug under a different failure mode (disk full, permission denied, etc.).
    """
    from aya.identity import Identity, Profile, TrustedKey
    from aya.packet import Packet

    local = Identity.generate("default")
    home = Identity.generate("home")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["default"] = local
    profile.trusted_keys["home"] = TrustedKey(
        did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
    )
    profile_path = tmp_path / "profile.json"
    profile.save(profile_path)

    # Point PACKETS_DIR at an empty directory; stub _ingest to a no-op so the
    # expected file never gets written. Mirrors _ingest's real behavior when
    # its best-effort write step hits OSError (disk full, permissions, etc.)
    # and is swallowed by its blanket except.
    empty_dir = tmp_path / "packets_empty"
    empty_dir.mkdir()

    signed = Packet(**{"from": home.did, "to": local.did}, intent="persist-fail").sign(home)

    async def mock_fetch(*args, **kwargs):
        yield signed

    with (
        patch("aya.paths.PROFILE_PATH", profile_path),
        patch("aya.paths.PACKETS_DIR", empty_dir),
        patch("aya.mcp_server._load_profile", return_value=profile),
        patch("aya.relay.RelayClient") as mock_cls,
        patch("aya.ingest.ingest", lambda pkt, *, quiet=False: None),
    ):
        mock_cls.return_value.fetch_pending = mock_fetch
        result = await call_tool("aya_receive", {"instance": "default"})

    payload = json.loads(result[0].text)
    assert len(payload) == 1
    assert payload[0]["ingested"] is False
    assert payload[0]["error"] == "persist_failed"
    assert all(entry["id"] != signed.id for entry in profile.ingested_ids)


async def test_receive_no_since_filter(tmp_path):
    """aya_receive calls fetch_pending() with no since, even when last_checked is set.

    Regression for issue #246: the MCP handler had the same last_checked-derived
    since cursor as cli.py receive, permanently excluding packets that arrived
    before the cursor but were never ingested.
    """
    from datetime import UTC, datetime, timedelta

    from aya.identity import Identity, Profile

    local = Identity.generate("default")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["default"] = local
    relay_url = "wss://relay.example.com"
    profile.default_relays = [relay_url]
    last_check_time = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    profile.last_checked[relay_url] = (
        last_check_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    profile_path = tmp_path / "profile.json"
    profile.save(profile_path)

    fetch_calls: list[tuple] = []

    async def mock_fetch(*args, **kwargs):
        fetch_calls.append((args, kwargs))
        if False:  # pragma: no cover
            yield  # makes this an async generator

    with (
        patch("aya.paths.PROFILE_PATH", profile_path),
        patch("aya.mcp_server._load_profile", return_value=profile),
        patch("aya.relay.RelayClient") as mock_cls,
    ):
        mock_cls.return_value.fetch_pending = mock_fetch
        await call_tool("aya_receive", {"instance": "default"})

    assert len(fetch_calls) == 1
    assert fetch_calls[0][1].get("since") is None


# ---------------------------------------------------------------------------
# aya_ack
# ---------------------------------------------------------------------------


async def test_ack_tool(tmp_path):
    """aya_ack sends an ACK packet and returns confirmation."""
    from datetime import UTC, datetime

    from aya.identity import Identity, Profile, TrustedKey
    from aya.packet import Packet

    local = Identity.generate("default")
    home = Identity.generate("home")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["default"] = local
    profile.trusted_keys["home"] = TrustedKey(
        did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
    )
    pkt = Packet(**{"from": home.did, "to": local.did}, intent="test")
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    profile.ingested_ids.append({"id": pkt.id, "ingested_at": now_iso, "from_did": home.did})
    profile_path = tmp_path / "profile.json"
    profile.save(profile_path)

    mock_publish = AsyncMock(return_value="ack" * 21 + "aa")
    with (
        patch("aya.paths.PROFILE_PATH", profile_path),
        patch("aya.relay.RelayClient") as mock_cls,
    ):
        mock_cls.return_value.publish = mock_publish
        result = await call_tool("aya_ack", {"packet_id": pkt.id})

    payload = json.loads(result[0].text)
    assert payload["in_reply_to"] == pkt.id
    assert "packet_id" in payload
    mock_publish.assert_awaited_once()


# ---------------------------------------------------------------------------
# aya_read
# ---------------------------------------------------------------------------


async def test_read_tool_content_only(tmp_path):
    """aya_read returns content only by default."""
    from aya.packet import Packet

    pkt = Packet(**{"from": "did:key:sender", "to": "did:key:receiver"}, intent="test")
    pkt.content = "Hello world"
    packets_dir = tmp_path / "packets"
    packets_dir.mkdir()
    (packets_dir / f"{pkt.id}.json").write_text(pkt.to_json())

    with patch("aya.paths.PACKETS_DIR", packets_dir):
        result = await call_tool("aya_read", {"packet_id": pkt.id[:8]})

    payload = json.loads(result[0].text)
    assert payload == {"content": "Hello world"}


async def test_read_tool_with_meta(tmp_path):
    """aya_read with meta=True returns full metadata."""
    from aya.packet import Packet

    pkt = Packet(**{"from": "did:key:sender", "to": "did:key:receiver"}, intent="test")
    pkt.content = "Hello world"
    packets_dir = tmp_path / "packets"
    packets_dir.mkdir()
    (packets_dir / f"{pkt.id}.json").write_text(pkt.to_json())

    with patch("aya.paths.PACKETS_DIR", packets_dir):
        result = await call_tool("aya_read", {"packet_id": pkt.id[:8], "meta": True})

    payload = json.loads(result[0].text)
    assert payload["id"] == pkt.id
    assert payload["intent"] == "test"
    assert payload["content"] == "Hello world"


async def test_read_tool_short_prefix():
    """aya_read rejects prefixes shorter than 8 chars."""
    result = await call_tool("aya_read", {"packet_id": "abc"})
    payload = json.loads(result[0].text)
    assert "error" in payload


# ---------------------------------------------------------------------------
# aya_config_show
# ---------------------------------------------------------------------------


async def test_config_show_tool():
    """aya_config_show returns current config."""
    fake_config = {"instance_label": "home"}

    with patch("aya.config.load_config", return_value=fake_config):
        result = await call_tool("aya_config_show", {})

    payload = json.loads(result[0].text)
    assert payload["instance_label"] == "home"


# ---------------------------------------------------------------------------
# aya_config_set
# ---------------------------------------------------------------------------


async def test_config_set_tool():
    """aya_config_set sets a value and returns updated config."""
    fake_result = {"instance_label": "home", "foo": "bar"}

    with patch("aya.config.set_config_value", return_value=fake_result):
        result = await call_tool("aya_config_set", {"key": "foo", "value": "bar"})

    payload = json.loads(result[0].text)
    assert payload["foo"] == "bar"
    assert payload["instance_label"] == "home"


# ---------------------------------------------------------------------------
# aya_packets
# ---------------------------------------------------------------------------


async def test_packets_tool(tmp_path):
    """aya_packets lists stored packets."""
    from aya.packet import Packet

    packets_dir = tmp_path / "packets"
    packets_dir.mkdir()

    for i in range(3):
        pkt = Packet(
            **{"from": "did:key:sender", "to": "did:key:receiver"},
            intent=f"test-{i}",
        )
        (packets_dir / f"{pkt.id}.json").write_text(pkt.to_json())

    with patch("aya.paths.PACKETS_DIR", packets_dir):
        result = await call_tool("aya_packets", {"limit": 2})

    payload = json.loads(result[0].text)
    assert isinstance(payload, list)
    assert len(payload) == 2


async def test_packets_tool_empty(tmp_path):
    """aya_packets returns empty list when no packets dir."""
    missing_dir = tmp_path / "no_packets"
    with patch("aya.paths.PACKETS_DIR", missing_dir):
        result = await call_tool("aya_packets", {})

    payload = json.loads(result[0].text)
    assert payload == []


# ---------------------------------------------------------------------------
# aya_relay_status
# ---------------------------------------------------------------------------


async def test_relay_status_tool():
    """aya_relay_status returns relay info."""
    from aya.identity import Identity, Profile, TrustedKey

    local = Identity.generate("default")
    peer = Identity.generate("peer")
    profile = Profile(
        alias="Test",
        ship_mind_name="",
        user_name="Tester",
        instances={"default": local},
        trusted_keys={
            "peer": TrustedKey(
                did=peer.did,
                label="peer",
                nostr_pubkey=peer.nostr_public_hex,
            ),
        },
        ingested_ids=[],
        default_relays=["wss://relay.example.com"],
        last_checked={"wss://relay.example.com": "2026-04-01T10:00:00Z"},
    )

    with patch("aya.mcp_server._load_profile", return_value=profile):
        result = await call_tool("aya_relay_status", {"instance": "default"})

    payload = json.loads(result[0].text)
    assert payload["instance"] == "default"
    assert payload["did"] == local.did
    assert payload["relays"] == ["wss://relay.example.com"]
    assert "peer" in payload["trusted_keys"]
    assert payload["last_checked"]["wss://relay.example.com"] == "2026-04-01T10:00:00Z"


# ---------------------------------------------------------------------------
# unknown tool
# ---------------------------------------------------------------------------


async def test_unknown_tool():
    """Calling an unknown tool returns an error, not a crash."""
    result = await call_tool("nonexistent_tool", {})
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert "error" in payload


# ---------------------------------------------------------------------------
# aya_schedule_watch
# ---------------------------------------------------------------------------


async def test_schedule_watch_tool(tmp_path, monkeypatch):
    """aya_schedule_watch creates a watch item via the scheduler."""
    sched_file = tmp_path / "scheduler.json"
    sched_file.write_text(json.dumps({"schema_version": 2, "items": []}))
    lock_file = tmp_path / ".scheduler.lock"

    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", sched_file)
    monkeypatch.setattr("aya.scheduler.LOCK_FILE", lock_file)

    result = await call_tool(
        "aya_schedule_watch",
        {
            "provider": "github-pr",
            "target": "owner/repo#42",
            "message": "Watch my PR",
        },
    )
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert payload["type"] == "watch"
    assert payload["message"] == "Watch my PR"
    assert payload["provider"] == "github-pr"
    assert payload["status"] == "active"


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


async def test_tool_error_handling():
    """A tool that raises returns a graceful error response."""
    with patch("aya.mcp_server._handle_status", side_effect=RuntimeError("boom")):
        result = await call_tool("aya_status", {})

    payload = json.loads(result[0].text)
    assert "error" in payload
    assert "boom" in payload["error"]
