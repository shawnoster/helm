"""Tests for cli.py — smoke tests using typer.testing.CliRunner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from aya.cli import app
from aya.identity import Identity, Profile, TrustedKey
from aya.packet import Packet
from aya.scheduler import add_reminder

runner = CliRunner()


# ── TestVersion ───────────────────────────────────────────────────────────────


class TestVersion:
    def test_outputs_version(self) -> None:
        from importlib.metadata import version

        expected = version("aya-ai-assist")
        result = runner.invoke(app, ["version", "--format", "text"])
        assert result.exit_code == 0, result.output
        assert f"aya {expected}" in result.output


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    return tmp_path / "assistant_profile.json"


@pytest.fixture
def profile_with_instance(profile_path: Path) -> Path:
    """Create a minimal profile with a 'default' instance already initialised."""
    identity = Identity.generate("default")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["default"] = identity
    profile.save(profile_path)
    return profile_path


@pytest.fixture
def profile_with_trusted(profile_with_instance: Path) -> Path:
    """Profile that also has a trusted 'home' key."""
    p = Profile.load(profile_with_instance)
    home = Identity.generate("home")
    p.trusted_keys["home"] = TrustedKey(
        did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
    )
    p.save(profile_with_instance)
    return profile_with_instance


@pytest.fixture
def profile_with_named_instance(profile_path: Path) -> Path:
    """Profile with a single 'work' instance — no 'default' instance."""
    identity = Identity.generate("work")
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["work"] = identity
    profile.save(profile_path)
    return profile_path


@pytest.fixture
def profile_with_multiple_instances(profile_path: Path) -> Path:
    """Profile with 'work' and 'laptop' instances — no 'default' instance."""
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.instances["work"] = Identity.generate("work")
    profile.instances["laptop"] = Identity.generate("laptop")
    profile.save(profile_path)
    return profile_path


@pytest.fixture
def profile_with_no_instances(profile_path: Path) -> Path:
    """Profile with no instances registered — simulates pre-init state."""
    profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
    profile.save(profile_path)
    return profile_path


@pytest.fixture
def profile_with_multiple_trusted(profile_with_instance: Path) -> Path:
    """Profile with two trusted keys — for testing ambiguous recipient errors."""
    p = Profile.load(profile_with_instance)
    home = Identity.generate("home")
    laptop = Identity.generate("laptop")
    p.trusted_keys["home"] = TrustedKey(
        did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
    )
    p.trusted_keys["laptop"] = TrustedKey(
        did=laptop.did, label="laptop", nostr_pubkey=laptop.nostr_public_hex
    )
    p.save(profile_with_instance)
    return profile_with_instance


# ── init ─────────────────────────────────────────────────────────────────────


class TestInit:
    def test_creates_profile(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.json"
        result = runner.invoke(app, ["init", "--profile", str(path), "--label", "work"])
        assert result.exit_code == 0, result.output
        assert path.exists()

        data = json.loads(path.read_text())
        assert "work" in data["aya"]["instances"]

    def test_adds_instance_to_existing_profile(self, profile_with_instance: Path) -> None:
        result = runner.invoke(
            app, ["init", "--profile", str(profile_with_instance), "--label", "laptop"]
        )
        assert result.exit_code == 0, result.output

        data = json.loads(profile_with_instance.read_text())
        assert "laptop" in data["aya"]["instances"]
        assert "default" in data["aya"]["instances"]  # original still present

    def test_shows_did_in_output(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.json"
        result = runner.invoke(app, ["init", "--profile", str(path), "--label", "test"])
        assert result.exit_code == 0
        # Verify the DID was saved to the profile (Rich may escape the colon)
        data = json.loads(path.read_text())
        did = data["aya"]["instances"]["test"]["did"]
        assert did.startswith("did:key:")

    def test_saves_relay_url(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.json"
        relay = "wss://custom.relay.example.com"
        result = runner.invoke(app, ["init", "--profile", str(path), "--relay", relay])
        assert result.exit_code == 0
        data = json.loads(path.read_text())
        assert data["aya"]["default_relays"] == [relay]


# ── trust ─────────────────────────────────────────────────────────────────────


class TestTrust:
    def test_adds_trusted_key(self, profile_with_instance: Path) -> None:
        home = Identity.generate("home")
        result = runner.invoke(
            app,
            [
                "trust",
                home.did,
                "--peer",
                "home",
                "--profile",
                str(profile_with_instance),
            ],
        )
        assert result.exit_code == 0, result.output

        data = json.loads(profile_with_instance.read_text())
        assert "home" in data["aya"]["trusted_keys"]
        assert data["aya"]["trusted_keys"]["home"]["did"] == home.did

    def test_trust_requires_profile(self, tmp_path: Path) -> None:
        missing = tmp_path / "no_profile.json"
        home = Identity.generate("home")
        result = runner.invoke(
            app,
            [
                "trust",
                home.did,
                "--peer",
                "home",
                "--profile",
                str(missing),
            ],
        )
        assert result.exit_code != 0

    def test_trust_warns_without_nostr_pubkey(self, profile_with_instance: Path) -> None:
        home = Identity.generate("home")
        result = runner.invoke(
            app,
            [
                "trust",
                home.did,
                "--peer",
                "home",
                "--profile",
                str(profile_with_instance),
                "--format",
                "text",
            ],
        )
        assert result.exit_code == 0
        assert "No Nostr pubkey" in result.output

    def test_trust_with_nostr_pubkey(self, profile_with_instance: Path) -> None:
        home = Identity.generate("home")
        result = runner.invoke(
            app,
            [
                "trust",
                home.did,
                "--peer",
                "home",
                "--nostr-pubkey",
                home.nostr_public_hex,
                "--profile",
                str(profile_with_instance),
            ],
        )
        assert result.exit_code == 0
        # Should NOT warn about missing nostr pubkey
        assert "No Nostr pubkey" not in result.output


# ── pair ──────────────────────────────────────────────────────────────────────


class TestPair:
    def test_initiator_stores_peer_under_peer_label(self, profile_with_instance: Path) -> None:
        """Initiator must store the peer DID under --peer label, not the local label.

        Regression test: before the fix, p.trusted_keys[trusted.label] used the
        label from the response content (which was the initiator's own label), so
        the peer DID overwrote the local self-trust entry.
        """
        from aya.pair import TrustedKey as PairTrustedKey

        local_identity = Identity.generate("guild-shawnoster")
        peer_identity = Identity.generate("sean-okeefe")

        p = Profile.load(profile_with_instance)
        p.instances["guild-shawnoster"] = local_identity
        p.save(profile_with_instance)

        # Simulate what poll_for_pair_response returns: TrustedKey whose label
        # is the initiator's own name (the bug: content["label"] was local label)
        buggy_trusted = PairTrustedKey(
            did=peer_identity.did,
            label="guild-shawnoster",  # wrong label — the old bug
            nostr_pubkey=peer_identity.nostr_public_hex,
        )

        with (
            patch("aya.cli.generate_code", return_value="TEST-CODE-0001"),
            patch("aya.cli.hash_code", return_value="deadbeef"),
            patch("aya.cli.publish_pair_request", return_value="req_event_id"),
            patch("aya.cli.poll_for_pair_response", return_value=buggy_trusted),
        ):
            result = runner.invoke(
                app,
                [
                    "pair",
                    "--peer",
                    "sean-okeefe",
                    "--as",
                    "guild-shawnoster",
                    "--profile",
                    str(profile_with_instance),
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(profile_with_instance.read_text())
        trusted_keys = data["aya"]["trusted_keys"]

        # Peer DID must be stored under the --peer label
        assert "sean-okeefe" in trusted_keys, "Peer not stored under --peer label"
        assert trusted_keys["sean-okeefe"]["did"] == peer_identity.did

        # Local label must NOT be overwritten with the peer DID
        assert (
            "guild-shawnoster" not in trusted_keys
            or trusted_keys.get("guild-shawnoster", {}).get("did") != peer_identity.did
        ), "Peer DID must not overwrite local label entry"

    def test_joiner_stores_peer_under_peer_label(self, profile_with_instance: Path) -> None:
        """Joiner must store the initiator DID under --peer label."""
        from aya.pair import TrustedKey as PairTrustedKey

        local_identity = Identity.generate("sean-okeefe")
        initiator_identity = Identity.generate("guild-shawnoster")

        p = Profile.load(profile_with_instance)
        p.instances["sean-okeefe"] = local_identity
        p.save(profile_with_instance)

        # join_pairing returns TrustedKey with the initiator's label from request content
        initiator_trusted = PairTrustedKey(
            did=initiator_identity.did,
            label="guild-shawnoster",
            nostr_pubkey=initiator_identity.nostr_public_hex,
        )

        with patch("aya.cli.join_pairing", return_value=initiator_trusted):
            result = runner.invoke(
                app,
                [
                    "pair",
                    "--code",
                    "CRUSH-BASIL-9046",
                    "--peer",
                    "guild-shawnoster",
                    "--as",
                    "sean-okeefe",
                    "--profile",
                    str(profile_with_instance),
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(profile_with_instance.read_text())
        trusted_keys = data["aya"]["trusted_keys"]

        assert "guild-shawnoster" in trusted_keys, "Initiator not stored under --peer label"
        assert trusted_keys["guild-shawnoster"]["did"] == initiator_identity.did


class TestPack:
    def test_pack_produces_json_to_file(self, profile_with_trusted: Path, tmp_path: Path) -> None:
        out_file = tmp_path / "packet.json"
        p = Profile.load(profile_with_trusted)
        home_did = p.trusted_keys["home"].did

        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                home_did,
                "--intent",
                "Test pack from work",
                "--out",
                str(out_file),
                "--profile",
                str(profile_with_trusted),
                "--format",
                "text",
            ],
            input="Some content\n",
        )
        assert result.exit_code == 0, result.output
        assert out_file.exists()

        data = json.loads(out_file.read_text())
        assert data["intent"] == "Test pack from work"
        assert data["to"] == home_did

    def test_pack_resolves_label(self, profile_with_trusted: Path, tmp_path: Path) -> None:
        out_file = tmp_path / "packet.json"
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "home",  # label, not raw DID
                "--intent",
                "Resolved by label",
                "--out",
                str(out_file),
                "--profile",
                str(profile_with_trusted),
                "--format",
                "text",
            ],
            input="data\n",
        )
        assert result.exit_code == 0, result.output
        assert out_file.exists()

    def test_pack_unknown_recipient_fails(self, profile_with_instance: Path) -> None:
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "nobody",
                "--intent",
                "fail",
                "--profile",
                str(profile_with_instance),
            ],
            input="data\n",
        )
        assert result.exit_code != 0

    def test_pack_missing_profile_fails(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "did:key:z6Mkfake",
                "--intent",
                "fail",
                "--profile",
                str(tmp_path / "missing.json"),
            ],
            input="data\n",
        )
        assert result.exit_code != 0

    def test_pack_missing_instance_fails(self, profile_path: Path, tmp_path: Path) -> None:
        # Profile exists but has no instances
        profile_path.write_text(json.dumps({}))
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "did:key:z6Mkfake",
                "--intent",
                "fail",
                "--as",
                "default",
                "--profile",
                str(profile_path),
            ],
            input="data\n",
        )
        assert result.exit_code != 0

    def test_pack_smart_default_single_named_instance(
        self, profile_with_named_instance: Path, tmp_path: Path
    ) -> None:
        """When only one instance exists and its name differs from --as, use it anyway."""
        p = Profile.load(profile_with_named_instance)
        # Add a trusted key so the pack can resolve a recipient
        remote = Identity.generate("remote")
        p.trusted_keys["remote"] = TrustedKey(
            did=remote.did, label="remote", nostr_pubkey=remote.nostr_public_hex
        )
        p.save(profile_with_named_instance)

        out_file = tmp_path / "packet.json"
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "remote",
                "--intent",
                "smart default test",
                "--out",
                str(out_file),
                "--as",
                "default",  # no 'default' instance — only 'work' exists
                "--profile",
                str(profile_with_named_instance),
                "--format",
                "text",
            ],
            input="hello\n",
        )
        assert result.exit_code == 0, result.output
        assert out_file.exists()

    def test_pack_multiple_instances_shows_available_names(
        self, profile_with_multiple_instances: Path, tmp_path: Path
    ) -> None:
        """When multiple instances exist and requested one is absent, error lists them."""
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "did:key:z6Mkfake",
                "--intent",
                "fail",
                "--as",
                "default",
                "--profile",
                str(profile_with_multiple_instances),
            ],
            input="data\n",
        )
        assert result.exit_code != 0
        # Error should mention all available instance names
        combined = result.stdout + (result.stderr or "")
        assert "work" in combined
        assert "laptop" in combined

    def test_pack_no_instances_prompts_init(
        self, profile_with_no_instances: Path, tmp_path: Path
    ) -> None:
        """When no instances exist, error tells user to run aya init."""
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "did:key:z6Mkfake",
                "--intent",
                "fail",
                "--as",
                "default",
                "--profile",
                str(profile_with_no_instances),
            ],
            input="data\n",
        )
        assert result.exit_code != 0
        combined = result.stdout + (result.stderr or "")
        assert "aya init" in combined


# ── schedule remind ──────────────────────────────────────────────────────────


class TestScheduleRemind:
    def test_creates_reminder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
        scheduler_file.write_text(json.dumps({"items": []}))
        alerts_file.write_text(json.dumps({"alerts": []}))

        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        result = runner.invoke(
            app,
            [
                "schedule",
                "remind",
                "--message",
                "Stand up and stretch",
                "--due",
                "in 1 hour",
            ],
        )
        assert result.exit_code == 0, result.output

        data = json.loads(scheduler_file.read_text())
        assert len(data["items"]) == 1
        assert data["items"][0]["message"] == "Stand up and stretch"
        assert data["items"][0]["type"] == "reminder"

    def test_remind_requires_message(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        scheduler_file = tmp_path / "scheduler.json"
        scheduler_file.write_text(json.dumps({"items": []}))
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)

        result = runner.invoke(
            app,
            [
                "schedule",
                "remind",
                "--due",
                "in 1 hour",
            ],
        )
        assert result.exit_code != 0


# ── schedule dismiss ─────────────────────────────────────────────────────────


class TestScheduleDismiss:
    def test_dismiss_by_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
        scheduler_file.write_text(json.dumps({"items": []}))
        alerts_file.write_text(json.dumps({"alerts": []}))

        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        item = add_reminder("Dismiss me via CLI", "in 1 hour")
        prefix = item["id"][:8]

        result = runner.invoke(app, ["schedule", "dismiss", prefix, "--format", "text"])
        assert result.exit_code == 0, result.output
        assert "Dismissed" in result.output

        data = json.loads(scheduler_file.read_text())
        assert data["items"][0]["status"] == "dismissed"

    def test_dismiss_not_found_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
        scheduler_file.write_text(json.dumps({"items": []}))
        alerts_file.write_text(json.dumps({"alerts": []}))

        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        result = runner.invoke(app, ["schedule", "dismiss", "nonexistent"])
        assert result.exit_code != 0


# ── dispatch ──────────────────────────────────────────────────────────────────


class TestDispatch:
    def test_dispatch_sends_stdin_content(
        self, profile_with_trusted: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_publish = AsyncMock(return_value="a" * 64)
        with patch("aya.cli.RelayClient") as mock_client_cls:
            mock_client_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "End of day notes",
                    "--profile",
                    str(profile_with_trusted),
                    "--format",
                    "text",
                ],
                input="Today I worked on useAlgolia error handling.\n",
            )
        assert result.exit_code == 0, result.output
        assert "Dispatched" in result.output
        assert "End of day notes" in result.output
        mock_publish.assert_awaited_once()

    def test_dispatch_seed(
        self, profile_with_trusted: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_publish = AsyncMock(return_value="b" * 64)
        with patch("aya.cli.RelayClient") as mock_client_cls:
            mock_client_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "Pick up dinner party thread",
                    "--seed",
                    "--opener",
                    "Ask about the guest count decision",
                    "--profile",
                    str(profile_with_trusted),
                    "--format",
                    "text",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Dispatched" in result.output
        mock_publish.assert_awaited_once()

    def test_dispatch_seed_requires_opener(self, profile_with_trusted: Path) -> None:
        result = runner.invoke(
            app,
            [
                "dispatch",
                "--to",
                "home",
                "--intent",
                "seed without opener",
                "--seed",
                "--profile",
                str(profile_with_trusted),
            ],
        )
        assert result.exit_code != 0

    def test_dispatch_unknown_recipient_fails(self, profile_with_instance: Path) -> None:
        result = runner.invoke(
            app,
            [
                "dispatch",
                "--to",
                "nobody",
                "--intent",
                "fail",
                "--profile",
                str(profile_with_instance),
            ],
            input="data\n",
        )
        assert result.exit_code != 0

    def test_dispatch_default_resolves_to_single_trusted_key(
        self, profile_with_trusted: Path
    ) -> None:
        """'--to default' should succeed when exactly one trusted key exists."""
        mock_publish = AsyncMock(return_value="b" * 64)
        with patch("aya.cli.RelayClient") as mock_client_cls:
            mock_client_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "default",
                    "--intent",
                    "test",
                    "--profile",
                    str(profile_with_trusted),
                ],
                input="hello\n",
            )
        assert result.exit_code == 0, result.output
        assert "Unknown recipient" not in (result.output or "")
        mock_publish.assert_awaited_once()

    def test_dispatch_unknown_recipient_lists_available(
        self, profile_with_multiple_trusted: Path
    ) -> None:
        """Error for unknown --to should list available recipient labels."""
        result = runner.invoke(
            app,
            [
                "dispatch",
                "--to",
                "nobody",
                "--intent",
                "fail",
                "--profile",
                str(profile_with_multiple_trusted),
            ],
            input="data\n",
        )
        assert result.exit_code != 0
        assert "home" in result.output
        assert "laptop" in result.output

    def test_dispatch_missing_instance_fails(self, profile_with_multiple_instances: Path) -> None:
        """When multiple instances exist and requested one is absent, dispatch must fail.

        Uses a multi-instance profile so the smart single-instance fallback doesn't
        silently succeed — the non-existent name must produce a non-zero exit.
        """
        p = Profile.load(profile_with_multiple_instances)
        home = Identity.generate("home")
        p.trusted_keys["home"] = TrustedKey(
            did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
        )
        p.save(profile_with_multiple_instances)

        result = runner.invoke(
            app,
            [
                "dispatch",
                "--to",
                "home",
                "--intent",
                "fail",
                "--as",
                "nonexistent",
                "--profile",
                str(profile_with_multiple_instances),
            ],
            input="data\n",
        )
        assert result.exit_code != 0

    def test_dispatch_missing_nostr_pubkey_fails(self, profile_with_instance: Path) -> None:
        """Trusted key without a Nostr pubkey should exit with a clear message."""
        p = Profile.load(profile_with_instance)
        home = Identity.generate("home")
        p.trusted_keys["home"] = TrustedKey(did=home.did, label="home", nostr_pubkey=None)
        p.save(profile_with_instance)

        result = runner.invoke(
            app,
            [
                "dispatch",
                "--to",
                "home",
                "--intent",
                "no pubkey",
                "--profile",
                str(profile_with_instance),
            ],
            input="data\n",
        )
        assert result.exit_code != 0
        assert "Nostr pubkey" in result.output

    def test_dispatch_relay_error_exits_cleanly(self, profile_with_trusted: Path) -> None:
        """Relay connection failure should print a friendly message, not a traceback."""
        with patch("aya.cli.RelayClient") as mock_client_cls:
            mock_client_cls.return_value.publish = AsyncMock(side_effect=Exception("conn refused"))
            result = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "relay down",
                    "--profile",
                    str(profile_with_trusted),
                ],
                input="data\n",
            )
        assert result.exit_code != 0
        assert "Dispatch failed" in result.output

    def test_dispatch_in_reply_to(self, profile_with_trusted: Path) -> None:
        """--in-reply-to sets in_reply_to on the published packet."""
        captured_packet = None

        async def _capture_publish(signed, *a, **kw):
            nonlocal captured_packet
            captured_packet = signed
            return "c" * 64

        with patch("aya.cli.RelayClient") as mock_client_cls:
            mock_client_cls.return_value.publish = AsyncMock(side_effect=_capture_publish)
            result = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "follow-up notes",
                    "--in-reply-to",
                    "01JABC1234PARENT00000",
                    "--profile",
                    str(profile_with_trusted),
                    "--format",
                    "text",
                ],
                input="This is a reply.\n",
            )
        assert result.exit_code == 0, result.output
        assert captured_packet is not None
        assert captured_packet.in_reply_to == "01JABC1234PARENT00000"

    def test_dispatch_in_reply_to_json(self, profile_with_trusted: Path) -> None:
        """--in-reply-to with --format json includes in_reply_to in output."""

        async def _capture_publish(signed, *a, **kw):
            return "d" * 64

        with patch("aya.cli.RelayClient") as mock_client_cls:
            mock_client_cls.return_value.publish = AsyncMock(side_effect=_capture_publish)
            result = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "threaded reply",
                    "--in-reply-to",
                    "01JABC1234PARENT00000",
                    "--profile",
                    str(profile_with_trusted),
                    "--format",
                    "json",
                    "--dry-run",
                ],
                input="Reply content.\n",
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["in_reply_to"] == "01JABC1234PARENT00000"


# ── schedule status ──────────────────────────────────────────────────────────


@pytest.fixture
def _isolate_scheduler(tmp_path, monkeypatch):
    """Point scheduler at a temp directory for CLI tests."""
    scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
    alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
    scheduler_file.parent.mkdir(parents=True)
    scheduler_file.write_text(json.dumps({"items": []}))
    alerts_file.write_text(json.dumps({"alerts": []}))
    monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
    monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)


@pytest.mark.usefixtures("_isolate_scheduler")
class TestHookCrons:
    @pytest.fixture
    def isolated_scheduler(self, tmp_path, monkeypatch):
        """Patch SCHEDULER_FILE, ALERTS_FILE, and REGISTERED_CRONS_FILE to a tmp dir.

        Without REGISTERED_CRONS_FILE patching the tests would leak writes to
        the real ~/.aya/session_registered_crons.json across the test suite.
        """
        sched_dir = tmp_path / "sched"
        sched_dir.mkdir()
        scheduler_file = sched_dir / "scheduler.json"
        alerts_file = sched_dir / "alerts.json"
        registered_file = sched_dir / "session_registered_crons.json"
        scheduler_file.write_text(json.dumps({"items": []}))
        alerts_file.write_text(json.dumps({"alerts": []}))
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)
        monkeypatch.setattr("aya.scheduler.REGISTERED_CRONS_FILE", registered_file)
        return sched_dir

    def test_no_crons_exits_silently(self, isolated_scheduler):
        result = runner.invoke(app, ["hook", "crons"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_outputs_valid_json_with_crons(self, isolated_scheduler):
        scheduler_file = isolated_scheduler / "scheduler.json"
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "test-cron",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "test",
                            "session_required": True,
                            "cron": "*/20 * * * *",
                            "prompt": "Do the thing.",
                        }
                    ]
                }
            )
        )

        result = runner.invoke(app, ["hook", "crons"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "hookSpecificOutput" in data
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "CronCreate" in ctx
        assert "test-cron" in ctx

    def test_multiple_crons_emit_separate_lines(self, isolated_scheduler):
        """Each session cron must produce its own JSON line so Claude Code
        creates a separate system reminder per cron — prevents truncation
        when multiple crons are bundled into a single hookSpecificOutput."""
        scheduler_file = isolated_scheduler / "scheduler.json"
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "cron-health",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "health-break",
                            "session_required": True,
                            "cron": "*/20 * * * *",
                            "prompt": "Take a break.",
                        },
                        {
                            "id": "cron-relay",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "relay-poll",
                            "session_required": True,
                            "cron": "*/10 * * * *",
                            "prompt": "Poll the relay.",
                        },
                    ]
                }
            )
        )

        result = runner.invoke(app, ["hook", "crons"])
        assert result.exit_code == 0

        lines = [ln for ln in result.output.strip().splitlines() if ln.strip()]
        assert len(lines) == 2, f"Expected 2 JSON lines, got {len(lines)}: {lines}"

        parsed = [json.loads(ln) for ln in lines]
        ids = set()
        for obj in parsed:
            assert "hookSpecificOutput" in obj
            ctx = obj["hookSpecificOutput"]["additionalContext"]
            assert "REQUIRED ACTION" in ctx
            assert "CronCreate" in ctx
            # Extract the cron id from the context
            for cron_id in ("cron-health", "cron-relay"):
                if cron_id in ctx:
                    ids.add(cron_id)

        assert ids == {"cron-health", "cron-relay"}, f"Missing cron IDs: {ids}"

    def test_escapes_double_quotes_in_prompt(self, isolated_scheduler):
        """Prompts with double quotes must be escaped to avoid malformed output."""
        scheduler_file = isolated_scheduler / "scheduler.json"
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "cron-quotes",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "test",
                            "session_required": True,
                            "cron": "*/5 * * * *",
                            "prompt": 'Say "hello" to the user.',
                        }
                    ]
                }
            )
        )

        result = runner.invoke(app, ["hook", "crons"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        # Quotes in the prompt must be escaped
        assert r"\"hello\"" in ctx
        # Must not contain unescaped quotes that would break parsing
        assert 'prompt="Say \\"hello\\" to the user."' in ctx

    def test_does_not_claim_alerts(self, isolated_scheduler):
        """hook crons must not consume alerts — they belong to schedule pending."""
        alerts_file = isolated_scheduler / "alerts.json"
        alerts_file.write_text(
            json.dumps(
                {
                    "alerts": [
                        {
                            "id": "alert-1",
                            "source_item_id": "watch-1",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "PR merged",
                            "details": {},
                            "seen": False,
                        }
                    ]
                }
            )
        )

        # Run hook crons
        runner.invoke(app, ["hook", "crons"])

        # Alerts must still be unseen
        alerts = json.loads(alerts_file.read_text())["alerts"]
        assert len(alerts) == 1
        assert alerts[0]["seen"] is False
        assert "delivered_at" not in alerts[0]

    def test_second_call_emits_nothing_when_already_registered(self, isolated_scheduler):
        """The mid-session re-registration guard: hook crons should track
        which IDs it has emitted and skip them on subsequent calls."""
        scheduler_file = isolated_scheduler / "scheduler.json"
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "tracker-cron",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "test",
                            "session_required": True,
                            "cron": "* * * * *",
                            "prompt": "Tick.",
                        }
                    ]
                }
            )
        )

        first = runner.invoke(app, ["hook", "crons"])
        assert first.exit_code == 0
        assert "tracker-cron" in first.output

        second = runner.invoke(app, ["hook", "crons"])
        assert second.exit_code == 0
        assert second.output.strip() == ""  # already registered, nothing new

    def test_reset_flag_clears_tracker_and_re_emits(self, isolated_scheduler):
        """--reset (used at SessionStart) should clear the tracker so a fresh
        session re-registers everything from scratch."""
        scheduler_file = isolated_scheduler / "scheduler.json"
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "reset-cron",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "test",
                            "session_required": True,
                            "cron": "* * * * *",
                            "prompt": "Tick.",
                        }
                    ]
                }
            )
        )

        first = runner.invoke(app, ["hook", "crons"])
        assert "reset-cron" in first.output

        second = runner.invoke(app, ["hook", "crons", "--reset"])
        assert second.exit_code == 0
        # After --reset the tracker is empty, so the cron is re-emitted
        assert "reset-cron" in second.output

    def test_new_cron_added_mid_session_is_picked_up(self, isolated_scheduler):
        """Add a cron after the first hook crons call, then re-run — only
        the new cron should be emitted on the second call. This is the
        end-to-end behavior the PostToolUse hook relies on."""
        scheduler_file = isolated_scheduler / "scheduler.json"
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "old-cron",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "old",
                            "session_required": True,
                            "cron": "* * * * *",
                            "prompt": "Old.",
                        }
                    ]
                }
            )
        )

        first = runner.invoke(app, ["hook", "crons"])
        assert "old-cron" in first.output
        assert "new-cron" not in first.output

        # Mid-session: add a new cron
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "old-cron",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "old",
                            "session_required": True,
                            "cron": "* * * * *",
                            "prompt": "Old.",
                        },
                        {
                            "id": "new-cron",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "new",
                            "session_required": True,
                            "cron": "* * * * *",
                            "prompt": "New.",
                        },
                    ]
                }
            )
        )

        second = runner.invoke(app, ["hook", "crons"])
        assert second.exit_code == 0
        assert "new-cron" in second.output
        assert "old-cron" not in second.output  # already in tracker

    def test_event_flag_changes_hook_event_name(self, isolated_scheduler):
        """--event PostToolUse routes the additionalContext through the
        PostToolUse hook channel instead of SessionStart."""
        scheduler_file = isolated_scheduler / "scheduler.json"
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "event-cron",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "test",
                            "session_required": True,
                            "cron": "* * * * *",
                            "prompt": "Tick.",
                        }
                    ]
                }
            )
        )

        result = runner.invoke(app, ["hook", "crons", "--event", "PostToolUse"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


@pytest.mark.usefixtures("_isolate_scheduler")
class TestScheduleStatusCLI:
    def test_status_exits_zero(self):
        result = runner.invoke(app, ["schedule", "status"])
        assert result.exit_code == 0

    def test_status_json_is_valid(self):
        result = runner.invoke(app, ["schedule", "status", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "active_watches" in data
        assert "pending_reminders" in data
        assert "total_items" in data

    def test_status_text_has_summary(self):
        result = runner.invoke(app, ["schedule", "status"])
        assert result.exit_code == 0
        assert "items" in result.output

    def test_pending_exits_zero(self):
        result = runner.invoke(app, ["schedule", "pending"])
        assert result.exit_code == 0

    def test_pending_json_is_valid(self):
        result = runner.invoke(app, ["schedule", "pending", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "alerts" in data
        assert "session_crons" in data

    def test_pending_json_long_prompt_no_wrapping(self, tmp_path, monkeypatch):
        """Regression: Rich console.print() wraps at 80 cols, injecting literal
        newlines inside JSON string values.  console.out() must be used instead.
        See https://github.com/shawnoster/aya/issues/66"""
        scheduler_file = tmp_path / "sched" / "scheduler.json"
        alerts_file = tmp_path / "sched" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
        long_prompt = "A" * 200  # well past any terminal width
        scheduler_file.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "test-long",
                            "type": "recurring",
                            "status": "active",
                            "created_at": "2026-01-01T00:00:00-07:00",
                            "message": "test",
                            "session_required": True,
                            "cron": "*/20 * * * *",
                            "prompt": long_prompt,
                        }
                    ]
                }
            )
        )
        alerts_file.write_text(json.dumps({"alerts": []}))
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        result = runner.invoke(app, ["schedule", "pending", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)  # must not raise
        crons = data["session_crons"]
        assert len(crons) == 1
        assert crons[0]["prompt"] == long_prompt

    def test_tick_exits_zero(self):
        result = runner.invoke(app, ["schedule", "tick", "--quiet"])
        assert result.exit_code == 0


# ── receive ───────────────────────────────────────────────────────────────────


class TestReceive:
    @pytest.fixture
    def sender(self) -> Identity:
        return Identity.generate("work")

    @pytest.fixture
    def profile_with_sender(self, profile_with_instance: Path, sender: Identity) -> Path:
        """Profile with a 'default' instance and 'work' registered as a trusted sender."""
        p = Profile.load(profile_with_instance)
        p.trusted_keys["work"] = TrustedKey(
            did=sender.did, label="work", nostr_pubkey=sender.nostr_public_hex
        )
        p.save(profile_with_instance)
        return profile_with_instance

    def _signed_packet(self, sender: Identity, to_did: str, intent: str = "Test packet") -> Packet:
        pkt = Packet(
            **{"from": sender.did, "to": to_did},
            intent=intent,
            content="Test content.",
        )
        return pkt.sign(sender)

    def test_fetch_pending_called_without_since(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """receive must call fetch_pending() with no since argument."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did)

        fetch_calls: list[tuple] = []

        async def mock_fetch(*args, **kwargs):
            fetch_calls.append((args, kwargs))
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            runner.invoke(
                app,
                ["receive", "--auto-ingest", "--quiet", "--profile", str(profile_with_sender)],
            )

        assert len(fetch_calls) == 1
        assert fetch_calls[0] == ((), {})  # called with no positional or keyword args

    def test_skips_already_ingested_packets(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """Packets whose IDs are already in ingested_ids must be silently skipped."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="Already seen")
        recent_ts = (
            (datetime.now(UTC) - timedelta(days=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        p.ingested_ids.append({"id": packet.id, "ingested_at": recent_ts})
        p.save(profile_with_sender)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["receive", "--auto-ingest", "--profile", str(profile_with_sender)],
            )

        assert "Already seen" not in result.output

    def test_auto_ingest_persists_packet_id(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """After auto-ingesting a trusted packet, its ID must be saved to ingested_ids."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="New packet")

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["receive", "--auto-ingest", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        saved = Profile.load(profile_with_sender)
        assert any(e["id"] == packet.id for e in saved.ingested_ids)

    def test_relay_error_shows_friendly_message(self, profile_with_sender: Path) -> None:
        """A relay connection failure must print a friendly message, not raise."""

        async def mock_fetch(*args, **kwargs):
            if False:  # pragma: no cover
                yield  # makes this an async generator
            raise OSError("connection refused")

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["receive", "--profile", str(profile_with_sender)],
            )

        assert "Could not reach relay" in result.output

    def test_yes_flag_ingests_untrusted_packet_without_prompt(
        self, profile_with_instance: Path
    ) -> None:
        """--yes must ingest packets from untrusted senders without prompting."""
        unknown_sender = Identity.generate("unknown")
        p = Profile.load(profile_with_instance)
        packet = self._signed_packet(unknown_sender, p.instances["default"].did, intent="Untrusted")

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            mock_cls.return_value.send_receipt = AsyncMock()
            result = runner.invoke(
                app,
                ["receive", "--yes", "--profile", str(profile_with_instance)],
            )

        assert result.exit_code == 0, result.output
        saved = Profile.load(profile_with_instance)
        assert any(e["id"] == packet.id for e in saved.ingested_ids)

    def test_yes_short_flag_works(self, profile_with_instance: Path) -> None:
        """-y must behave identically to --yes for untrusted senders and skip prompts."""
        unknown_sender = Identity.generate("unknown")
        p = Profile.load(profile_with_instance)
        packet = self._signed_packet(
            unknown_sender, p.instances["default"].did, intent="Short flag"
        )

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("typer.confirm") as mock_confirm:
            mock_confirm.side_effect = AssertionError(
                "typer.confirm should not be called when -y is used"
            )
            with patch("aya.cli.RelayClient") as mock_cls:
                mock_cls.return_value.fetch_pending = mock_fetch
                mock_cls.return_value.send_receipt = AsyncMock()
                result = runner.invoke(
                    app,
                    ["receive", "-y", "--profile", str(profile_with_instance)],
                )

        assert result.exit_code == 0, result.output
        saved = Profile.load(profile_with_instance)
        assert any(e["id"] == packet.id for e in saved.ingested_ids)

    def test_receive_since_lookback(self, profile_with_sender: Path, sender: Identity) -> None:
        """When last_checked is set, receive passes since = last_checked - 60s."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did)

        # Record a previous check time on one relay. Use a recent relative
        # timestamp (1 hour ago) so it stays within cli.py's 7-day lookback
        # clamp regardless of when the test runs. Round to seconds to match
        # the iso serialization on line 1309.
        relay_url = p.default_relays[0]
        last_check_time = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
        p.last_checked[relay_url] = (
            last_check_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        p.save(profile_with_sender)

        fetch_calls: list[tuple] = []

        async def mock_fetch(*args, **kwargs):
            fetch_calls.append((args, kwargs))
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            runner.invoke(
                app,
                ["receive", "--auto-ingest", "--quiet", "--profile", str(profile_with_sender)],
            )

        assert len(fetch_calls) == 1
        called_since = fetch_calls[0][1].get("since")
        assert called_since is not None
        expected_since = last_check_time - timedelta(seconds=60)
        assert called_since == expected_since

    def test_receive_last_checked_persistence(self, profile_with_sender: Path) -> None:
        """receive saves last_checked for each relay even when inbox is empty."""
        p = Profile.load(profile_with_sender)
        relay_url = p.default_relays[0]
        assert relay_url not in p.last_checked  # clean slate

        async def mock_fetch(*args, **kwargs):
            if False:  # pragma: no cover
                yield  # makes this an async generator

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["receive", "--quiet", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        saved = Profile.load(profile_with_sender)
        assert relay_url in saved.last_checked
        assert saved.last_checked[relay_url]  # non-empty ISO timestamp

    def test_receive_skip_untrusted(self, profile_with_sender: Path, sender: Identity) -> None:
        """--skip-untrusted must silently skip untrusted packets and ingest trusted ones."""
        p = Profile.load(profile_with_sender)
        to_did = p.instances["default"].did

        trusted_packet = self._signed_packet(sender, to_did, intent="Trusted msg")
        untrusted_sender = Identity.generate("stranger")
        untrusted_packet = self._signed_packet(untrusted_sender, to_did, intent="Untrusted msg")

        async def mock_fetch(*args, **kwargs):
            yield trusted_packet
            yield untrusted_packet

        with patch("typer.confirm") as mock_confirm:
            mock_confirm.side_effect = AssertionError(
                "typer.confirm should not be called with --skip-untrusted"
            )
            with patch("aya.cli.RelayClient") as mock_cls:
                mock_cls.return_value.fetch_pending = mock_fetch
                result = runner.invoke(
                    app,
                    [
                        "receive",
                        "--auto-ingest",
                        "--skip-untrusted",
                        "--profile",
                        str(profile_with_sender),
                    ],
                )

        assert result.exit_code == 0, result.output
        saved = Profile.load(profile_with_sender)
        assert any(e["id"] == trusted_packet.id for e in saved.ingested_ids)
        assert not any(e["id"] == untrusted_packet.id for e in saved.ingested_ids)

    def test_receive_skip_untrusted_json(self, profile_with_sender: Path, sender: Identity) -> None:
        """--skip-untrusted with --format json must include skipped=true for untrusted packets."""
        p = Profile.load(profile_with_sender)
        to_did = p.instances["default"].did

        trusted_packet = self._signed_packet(sender, to_did, intent="Trusted json")
        untrusted_sender = Identity.generate("stranger")
        untrusted_packet = self._signed_packet(untrusted_sender, to_did, intent="Untrusted json")

        async def mock_fetch(*args, **kwargs):
            yield trusted_packet
            yield untrusted_packet

        with patch("typer.confirm") as mock_confirm:
            mock_confirm.side_effect = AssertionError(
                "typer.confirm should not be called with --skip-untrusted"
            )
            with patch("aya.cli.RelayClient") as mock_cls:
                mock_cls.return_value.fetch_pending = mock_fetch
                result = runner.invoke(
                    app,
                    [
                        "receive",
                        "--auto-ingest",
                        "--skip-untrusted",
                        "--format",
                        "json",
                        "--profile",
                        str(profile_with_sender),
                    ],
                )

        assert result.exit_code == 0, result.output
        import json

        data = json.loads(result.output)
        packets = data["packets"]
        assert len(packets) == 2

        trusted_entry = next(p for p in packets if p["id"] == trusted_packet.id)
        assert trusted_entry["ingested"] is True
        assert "skipped" not in trusted_entry

        untrusted_entry = next(p for p in packets if p["id"] == untrusted_packet.id)
        assert untrusted_entry["ingested"] is False
        assert untrusted_entry["skipped"] is True

    def test_receive_auto_ingest_prints_summary(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """receive --auto-ingest must print a text summary showing ingested count."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            mock_cls.return_value.send_receipt = AsyncMock()
            result = runner.invoke(
                app,
                [
                    "receive",
                    "--auto-ingest",
                    "--format",
                    "text",
                    "--profile",
                    str(profile_with_sender),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Ingested 1 of 1" in result.output


# ── inbox ─────────────────────────────────────────────────────────────────────


class TestInbox:
    @pytest.fixture
    def sender(self) -> Identity:
        return Identity.generate("work")

    @pytest.fixture
    def profile_with_sender(self, profile_with_instance: Path, sender: Identity) -> Path:
        p = Profile.load(profile_with_instance)
        p.trusted_keys["work"] = TrustedKey(
            did=sender.did, label="work", nostr_pubkey=sender.nostr_public_hex
        )
        p.save(profile_with_instance)
        return profile_with_instance

    def _signed_packet(self, sender: Identity, to_did: str, intent: str = "Test packet") -> Packet:
        pkt = Packet(
            **{"from": sender.did, "to": to_did},
            intent=intent,
            content="Test content.",
        )
        return pkt.sign(sender)

    def test_filters_ingested_packets_by_default(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """inbox must hide already-ingested packets unless --all is passed."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="Old packet")
        recent_ts = (
            (datetime.now(UTC) - timedelta(days=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        p.ingested_ids.append({"id": packet.id, "ingested_at": recent_ts})
        p.save(profile_with_sender)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["inbox", "--format", "text", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        assert "Old packet" not in result.output
        assert "Inbox empty" in result.output

    def test_shows_new_packets(self, profile_with_sender: Path, sender: Identity) -> None:
        """inbox must show packets not yet in ingested_ids."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="Fresh packet")

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["inbox", "--format", "text", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        assert "Fresh packet" in result.output

    def test_all_flag_shows_ingested_packets(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """inbox --all must show ingested packets marked as [ingested]."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="Old packet")
        recent_ts = (
            (datetime.now(UTC) - timedelta(days=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        p.ingested_ids.append({"id": packet.id, "ingested_at": recent_ts})
        p.save(profile_with_sender)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["inbox", "--all", "--format", "text", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        assert "Old packet" in result.output
        assert "[ingested]" in result.output

    def test_all_flag_shows_count_summary(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """inbox --all with some ingested packets must show a 'N total, M new' summary."""
        p = Profile.load(profile_with_sender)
        ingested_packet = self._signed_packet(sender, p.instances["default"].did, intent="Ingested")
        new_packet = self._signed_packet(sender, p.instances["default"].did, intent="New")
        recent_ts = (
            (datetime.now(UTC) - timedelta(days=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        p.ingested_ids.append({"id": ingested_packet.id, "ingested_at": recent_ts})
        p.save(profile_with_sender)

        async def mock_fetch(*args, **kwargs):
            yield ingested_packet
            yield new_packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["inbox", "--all", "--format", "text", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        assert "2 total, 1 new" in result.output

    def test_json_output_includes_ingested_field_with_all_flag(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """inbox --all --format json must include an 'ingested' field for each packet."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="Already seen")
        recent_ts = (
            (datetime.now(UTC) - timedelta(days=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        p.ingested_ids.append({"id": packet.id, "ingested_at": recent_ts})
        p.save(profile_with_sender)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["inbox", "--all", "--format", "json", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "packets" in data
        assert len(data["packets"]) == 1
        assert data["packets"][0]["ingested"] is True

    def test_json_output_no_ingested_field_without_all_flag(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """inbox --format json (default) must not include an 'ingested' field."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="Fresh")

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["inbox", "--format", "json", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "packets" in data
        assert len(data["packets"]) == 1
        assert "ingested" not in data["packets"][0]


class TestAutoFormat:
    def test_auto_resolves_to_text_in_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When stdout is a TTY, AUTO should produce text output."""
        from aya.cli import OutputFormat, resolve_format

        monkeypatch.delenv("AYA_FORMAT", raising=False)
        with patch("aya.cli.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = True
            assert resolve_format(OutputFormat.AUTO) == OutputFormat.TEXT

        # And verify via CLI with explicit --format text
        result = runner.invoke(app, ["version", "--format", "text"])
        assert result.exit_code == 0, result.output
        assert result.output.startswith("aya ")

    def test_auto_resolves_to_json_when_not_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When stdout.isatty() returns False, AUTO should resolve to JSON.
        CliRunner provides a non-TTY stdout, so the default should be JSON."""
        monkeypatch.delenv("AYA_FORMAT", raising=False)
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "version" in data

    def test_aya_format_env_overrides_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AYA_FORMAT=json should force JSON even in a TTY context."""
        monkeypatch.setenv("AYA_FORMAT", "json")
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "version" in data

    def test_aya_format_env_text_overrides_non_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AYA_FORMAT=text should force text even in a non-TTY context."""
        monkeypatch.setenv("AYA_FORMAT", "text")
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.output
        assert result.output.startswith("aya ")

    def test_explicit_format_text_overrides_auto(self) -> None:
        """--format text must always produce text, regardless of TTY."""
        result = runner.invoke(app, ["version", "--format", "text"])
        assert result.exit_code == 0, result.output
        assert result.output.startswith("aya ")

    def test_explicit_format_json_overrides_auto(self) -> None:
        """--format json must always produce JSON, regardless of TTY."""
        result = runner.invoke(app, ["version", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "version" in data

    def test_auto_can_be_passed_explicitly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--format auto should be accepted and resolve to JSON under non-TTY."""
        monkeypatch.delenv("AYA_FORMAT", raising=False)
        result = runner.invoke(app, ["version", "--format", "auto"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "version" in data


# ── deprecation warnings ──────────────────────────────────────────────────────


class TestDeprecationWarnings:
    """Verify that legacy flags emit deprecation warnings and still work correctly."""

    def test_trust_label_warns(self, profile_with_instance: Path) -> None:
        """--label on trust emits a deprecation warning to stderr."""
        home = Identity.generate("home")
        result = runner.invoke(
            app,
            [
                "trust",
                home.did,
                "--label",
                "home",
                "--profile",
                str(profile_with_instance),
            ],
        )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" in stderr
        assert "--peer" in stderr

    def test_trust_peer_no_warning(self, profile_with_instance: Path) -> None:
        """--peer on trust does NOT emit a deprecation warning."""
        home = Identity.generate("home")
        result = runner.invoke(
            app,
            [
                "trust",
                home.did,
                "--peer",
                "home",
                "--profile",
                str(profile_with_instance),
            ],
        )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" not in stderr

    def test_pair_label_warns(self, profile_with_instance: Path) -> None:
        """--label on pair emits a deprecation warning to stderr."""
        from unittest.mock import patch

        from aya.pair import TrustedKey as PairTrustedKey

        local_identity = Identity.generate("remote-host")
        p = Profile.load(profile_with_instance)
        p.instances["remote-host"] = local_identity
        p.save(profile_with_instance)

        peer_identity = Identity.generate("peer-host")
        mock_trusted = PairTrustedKey(
            did=peer_identity.did,
            label="peer-host",
            nostr_pubkey=peer_identity.nostr_public_hex,
        )

        with (
            patch("aya.cli.generate_code", return_value="ABCD1234"),
            patch("aya.cli.hash_code", return_value="deadbeef"),
            patch("aya.cli.publish_pair_request"),
            patch("aya.cli.poll_for_pair_response", return_value=mock_trusted),
        ):
            result = runner.invoke(
                app,
                [
                    "pair",
                    "--label",
                    "peer-host",
                    "--as",
                    "remote-host",
                    "--profile",
                    str(profile_with_instance),
                ],
            )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" in stderr
        assert "--peer" in stderr

    def test_pair_peer_no_warning(self, profile_with_instance: Path) -> None:
        """--peer on pair does NOT emit a deprecation warning."""
        from unittest.mock import patch

        from aya.pair import TrustedKey as PairTrustedKey

        local_identity = Identity.generate("remote-host")
        p = Profile.load(profile_with_instance)
        p.instances["remote-host"] = local_identity
        p.save(profile_with_instance)

        peer_identity = Identity.generate("peer-host")
        mock_trusted = PairTrustedKey(
            did=peer_identity.did,
            label="peer-host",
            nostr_pubkey=peer_identity.nostr_public_hex,
        )

        with (
            patch("aya.cli.generate_code", return_value="ABCD1234"),
            patch("aya.cli.hash_code", return_value="deadbeef"),
            patch("aya.cli.publish_pair_request"),
            patch("aya.cli.poll_for_pair_response", return_value=mock_trusted),
        ):
            result = runner.invoke(
                app,
                [
                    "pair",
                    "--peer",
                    "peer-host",
                    "--as",
                    "remote-host",
                    "--profile",
                    str(profile_with_instance),
                ],
            )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" not in stderr

    def test_pack_instance_warns(self, profile_with_trusted: Path, tmp_path: Path) -> None:
        """--instance on pack emits a deprecation warning to stderr."""
        out_file = tmp_path / "packet.json"
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "home",
                "--intent",
                "legacy flag test",
                "--out",
                str(out_file),
                "--instance",
                "default",  # uses the "default" instance which must exist in the fixture
                "--profile",
                str(profile_with_trusted),
            ],
            input="test content\n",
        )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" in stderr
        assert "--as" in stderr

    def test_pack_instance_and_as_together_errors(
        self, profile_with_trusted: Path, tmp_path: Path
    ) -> None:
        """Passing both --as and --instance is a usage error (exit 2)."""
        out_file = tmp_path / "packet.json"
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "home",
                "--intent",
                "conflict test",
                "--out",
                str(out_file),
                "--as",
                "work",
                "--instance",
                "home",
                "--profile",
                str(profile_with_trusted),
            ],
            input="test content\n",
        )
        assert result.exit_code == 2
        stderr = result.stderr or result.output
        assert "Cannot use" in stderr

    def test_pack_as_no_warning(self, profile_with_trusted: Path, tmp_path: Path) -> None:
        """--as on pack does NOT emit a deprecation warning."""
        out_file = tmp_path / "packet.json"
        result = runner.invoke(
            app,
            [
                "pack",
                "--to",
                "home",
                "--intent",
                "new flag test",
                "--out",
                str(out_file),
                "--as",
                "default",
                "--profile",
                str(profile_with_trusted),
            ],
            input="test content\n",
        )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" not in stderr

    def test_pair_instance_warns(self, profile_with_instance: Path) -> None:
        """--instance on pair emits a deprecation warning to stderr."""
        from unittest.mock import patch

        from aya.pair import TrustedKey as PairTrustedKey

        local_identity = Identity.generate("remote-host")
        p = Profile.load(profile_with_instance)
        p.instances["remote-host"] = local_identity
        p.save(profile_with_instance)

        peer_identity = Identity.generate("peer-host")
        mock_trusted = PairTrustedKey(
            did=peer_identity.did,
            label="peer-host",
            nostr_pubkey=peer_identity.nostr_public_hex,
        )

        with (
            patch("aya.cli.generate_code", return_value="ABCD1234"),
            patch("aya.cli.hash_code", return_value="deadbeef"),
            patch("aya.cli.publish_pair_request"),
            patch("aya.cli.poll_for_pair_response", return_value=mock_trusted),
        ):
            result = runner.invoke(
                app,
                [
                    "pair",
                    "--peer",
                    "peer-host",
                    "--instance",
                    "remote-host",
                    "--profile",
                    str(profile_with_instance),
                ],
            )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" in stderr
        assert "--as" in stderr

    def test_send_instance_warns(self, profile_with_trusted: Path, tmp_path: Path) -> None:
        """--instance on send emits a deprecation warning to stderr."""
        # Create a packet file to send
        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]
        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="deprecation test",
            content="test",
        )
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        mock_publish = AsyncMock(return_value="a" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "send",
                    str(packet_file),
                    "--instance",
                    "default",
                    "--profile",
                    str(profile_with_trusted),
                ],
            )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" in stderr
        assert "--as" in stderr

    def test_dispatch_instance_warns(self, profile_with_trusted: Path) -> None:
        """--instance on dispatch emits a deprecation warning to stderr."""
        mock_publish = AsyncMock(return_value="b" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "deprecation test",
                    "--instance",
                    "default",
                    "--profile",
                    str(profile_with_trusted),
                ],
                input="test content\n",
            )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" in stderr
        assert "--as" in stderr

    def test_receive_instance_warns(self, profile_with_instance: Path) -> None:
        """--instance on receive emits a deprecation warning to stderr."""

        async def mock_fetch(*args, **kwargs):
            if False:  # pragma: no cover
                yield

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                [
                    "receive",
                    "--quiet",
                    "--instance",
                    "default",
                    "--profile",
                    str(profile_with_instance),
                ],
            )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" in stderr
        assert "--as" in stderr

    def test_inbox_instance_warns(self, profile_with_instance: Path) -> None:
        """--instance on inbox emits a deprecation warning to stderr."""

        async def mock_fetch(*args, **kwargs):
            if False:  # pragma: no cover
                yield

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                [
                    "inbox",
                    "--instance",
                    "default",
                    "--profile",
                    str(profile_with_instance),
                ],
            )
        assert result.exit_code == 0, result.output
        stderr = result.stderr or ""
        assert "deprecated" in stderr
        assert "--as" in stderr


# ── ack ───────────────────────────────────────────────────────────────────────


class TestAck:
    """Tests for the `aya ack` command."""

    @pytest.fixture
    def profile_with_ingested(self, tmp_path: Path) -> tuple[Path, str, Identity]:
        """Profile with a 'default' instance, a trusted 'home' peer, and one ingested packet ID."""
        local = Identity.generate("default")
        home = Identity.generate("home")

        profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
        profile.instances["default"] = local
        profile.trusted_keys["home"] = TrustedKey(
            did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
        )

        # Add a fake ingested packet ID
        from datetime import UTC, datetime

        pkt = Packet(
            **{"from": home.did, "to": local.did},
            intent="seed from home",
        )
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        profile.ingested_ids.append({"id": pkt.id, "ingested_at": now_iso, "from_did": home.did})

        profile_path = tmp_path / "profile.json"
        profile.save(profile_path)
        return profile_path, pkt.id, home

    def test_ack_happy_path(self, profile_with_ingested: tuple) -> None:
        """ack sends an ACK packet and prints confirmation."""
        profile_path, packet_id, _home = profile_with_ingested
        mock_publish = AsyncMock(return_value="c" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "ack",
                    packet_id,
                    "looks good",
                    "--profile",
                    str(profile_path),
                    "--format",
                    "text",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "ACK sent" in result.output
        assert packet_id[:8] in result.output
        mock_publish.assert_awaited_once()

    def test_ack_prefix_match(self, profile_with_ingested: tuple) -> None:
        """ack resolves the full packet ID from a short prefix."""
        profile_path, packet_id, _home = profile_with_ingested
        prefix = packet_id[:8]
        mock_publish = AsyncMock(return_value="d" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                ["ack", prefix, "--profile", str(profile_path), "--format", "text"],
            )
        assert result.exit_code == 0, result.output
        assert "ACK sent" in result.output

    def test_ack_dismiss_flag(self, profile_with_ingested: tuple) -> None:
        """--dismiss sets the dismiss flag in the ACK content and uses default message."""
        profile_path, packet_id, _home = profile_with_ingested
        mock_publish = AsyncMock(return_value="e" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "ack",
                    packet_id,
                    "--dismiss",
                    "--profile",
                    str(profile_path),
                    "--format",
                    "text",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "ACK sent" in result.output
        # Verify ACK packet content has dismiss=True
        call_args = mock_publish.call_args
        ack_pkt: Packet = call_args[0][0]
        assert ack_pkt.intent == "ack"
        assert isinstance(ack_pkt.content, dict)
        assert ack_pkt.content["dismiss"] is True
        assert ack_pkt.content["message"] == "acknowledged"

    def test_ack_packet_has_correct_intent_and_reply_fields(
        self, profile_with_ingested: tuple
    ) -> None:
        """ACK packet must have intent='ack' and in_reply_to set to the original packet ID."""
        profile_path, packet_id, _home = profile_with_ingested
        mock_publish = AsyncMock(return_value="f" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            runner.invoke(
                app,
                ["ack", packet_id, "got it", "--profile", str(profile_path)],
            )
        ack_pkt: Packet = mock_publish.call_args[0][0]
        assert ack_pkt.intent == "ack"
        assert ack_pkt.in_reply_to == packet_id
        assert ack_pkt.content["in_reply_to"] == packet_id
        assert ack_pkt.content["message"] == "got it"

    def test_ack_unknown_packet_id_exits_nonzero(self, profile_with_ingested: tuple) -> None:
        """ack with an ID not in ingested_ids must exit non-zero."""
        profile_path, _packet_id, _home = profile_with_ingested
        result = runner.invoke(
            app,
            ["ack", "00000000000000000000000000", "--profile", str(profile_path)],
        )
        assert result.exit_code != 0

    def test_ack_no_trusted_peers_exits_nonzero(self, tmp_path: Path) -> None:
        """ack with no trusted peers (no Nostr pubkey) must exit non-zero."""
        local = Identity.generate("default")
        profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
        profile.instances["default"] = local
        # A trusted key without a Nostr pubkey
        other = Identity.generate("other")
        profile.trusted_keys["other"] = TrustedKey(did=other.did, label="other", nostr_pubkey=None)

        from datetime import UTC, datetime

        pkt = Packet(**{"from": other.did, "to": local.did}, intent="test")
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        profile.ingested_ids.append({"id": pkt.id, "ingested_at": now_iso})

        profile_path = tmp_path / "profile.json"
        profile.save(profile_path)

        result = runner.invoke(
            app,
            ["ack", pkt.id, "--profile", str(profile_path)],
        )
        assert result.exit_code != 0

    def test_ack_relay_error_exits_nonzero(self, profile_with_ingested: tuple) -> None:
        """ack must exit non-zero when the relay publish fails."""
        profile_path, packet_id, _home = profile_with_ingested
        mock_publish = AsyncMock(side_effect=Exception("relay down"))
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                ["ack", packet_id, "--profile", str(profile_path)],
            )
        assert result.exit_code != 0

    def test_ack_routes_to_correct_sender_with_multiple_peers(self, tmp_path: Path) -> None:
        """With two trusted peers, ack routes to the peer that sent the packet (via from_did)."""
        from datetime import UTC, datetime

        local = Identity.generate("default")
        peer_a = Identity.generate("peer_a")
        peer_b = Identity.generate("peer_b")

        profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
        profile.instances["default"] = local
        profile.trusted_keys["peer_a"] = TrustedKey(
            did=peer_a.did, label="peer_a", nostr_pubkey=peer_a.nostr_public_hex
        )
        profile.trusted_keys["peer_b"] = TrustedKey(
            did=peer_b.did, label="peer_b", nostr_pubkey=peer_b.nostr_public_hex
        )

        # Ingest a packet from peer_a
        pkt = Packet(**{"from": peer_a.did, "to": local.did}, intent="seed from A")
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        profile.ingested_ids.append({"id": pkt.id, "ingested_at": now_iso, "from_did": peer_a.did})

        profile_path = tmp_path / "profile.json"
        profile.save(profile_path)

        mock_publish = AsyncMock(return_value="a" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                ["ack", pkt.id, "thanks", "--profile", str(profile_path)],
            )
        assert result.exit_code == 0, result.output
        ack_pkt: Packet = mock_publish.call_args[0][0]
        assert ack_pkt.to_did == peer_a.did, "ACK must route to the original sender (peer_a)"

    def test_ack_falls_back_without_from_did(self, tmp_path: Path) -> None:
        """Old-style ingested entry (no from_did) falls back to sole trusted peer logic."""
        from datetime import UTC, datetime

        local = Identity.generate("default")
        peer = Identity.generate("peer")

        profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
        profile.instances["default"] = local
        profile.trusted_keys["peer"] = TrustedKey(
            did=peer.did, label="peer", nostr_pubkey=peer.nostr_public_hex
        )

        # Old-style entry without from_did
        pkt = Packet(**{"from": peer.did, "to": local.did}, intent="old seed")
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        profile.ingested_ids.append({"id": pkt.id, "ingested_at": now_iso})

        profile_path = tmp_path / "profile.json"
        profile.save(profile_path)

        mock_publish = AsyncMock(return_value="b" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                ["ack", pkt.id, "got it", "--profile", str(profile_path)],
            )
        assert result.exit_code == 0, result.output
        ack_pkt: Packet = mock_publish.call_args[0][0]
        assert ack_pkt.to_did == peer.did


# ── dry-run ─────────────────────────────────────────────────────────────────


class TestDryRun:
    """Tests for --dry-run flag across relay-publishing and state-mutating commands."""

    def test_send_dry_run(self, profile_with_trusted: Path, tmp_path: Path) -> None:
        """--dry-run prints packet JSON and does not call publish."""
        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]
        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="dry run test",
            content="hello",
        )
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_publish = AsyncMock(return_value="a" * 64)
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "send",
                    str(packet_file),
                    "--dry-run",
                    "--profile",
                    str(profile_with_trusted),
                ],
            )
        assert result.exit_code == 0, result.output
        output_data = json.loads(result.output)
        assert output_data["id"] == pkt.id
        assert output_data["intent"] == "dry run test"
        mock_publish.assert_not_awaited()

    def test_dispatch_dry_run(
        self, profile_with_trusted: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run prints signed packet JSON and does not call publish."""
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_publish = AsyncMock(return_value="a" * 64)
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "dry dispatch test",
                    "--dry-run",
                    "--profile",
                    str(profile_with_trusted),
                ],
                input="Some content for dispatch.\n",
            )
        assert result.exit_code == 0, result.output
        output_data = json.loads(result.output)
        assert output_data["intent"] == "dry dispatch test"
        assert "id" in output_data
        mock_publish.assert_not_awaited()

    def test_ack_dry_run(self, tmp_path: Path) -> None:
        """--dry-run prints ACK packet JSON and does not call publish."""
        from datetime import UTC, datetime

        local = Identity.generate("default")
        home = Identity.generate("home")

        profile = Profile(alias="Ace", ship_mind_name="", user_name="Shawn")
        profile.instances["default"] = local
        profile.trusted_keys["home"] = TrustedKey(
            did=home.did, label="home", nostr_pubkey=home.nostr_public_hex
        )

        pkt = Packet(**{"from": home.did, "to": local.did}, intent="seed from home")
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        profile.ingested_ids.append({"id": pkt.id, "ingested_at": now_iso, "from_did": home.did})

        profile_path = tmp_path / "profile.json"
        profile.save(profile_path)

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_publish = AsyncMock(return_value="c" * 64)
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                ["ack", pkt.id, "looks good", "--dry-run", "--profile", str(profile_path)],
            )
        assert result.exit_code == 0, result.output
        output_data = json.loads(result.output)
        assert output_data["intent"] == "ack"
        assert "id" in output_data
        mock_publish.assert_not_awaited()

    def test_schedule_remind_dry_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run prints reminder item and does not write to scheduler file."""
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
        scheduler_file.write_text(json.dumps({"items": []}))
        alerts_file.write_text(json.dumps({"alerts": []}))

        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        result = runner.invoke(
            app,
            [
                "schedule",
                "remind",
                "--message",
                "Stand up and stretch",
                "--due",
                "in 1 hour",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        output_data = json.loads(result.output)
        assert "item" in output_data
        output_data = output_data["item"]
        assert output_data["type"] == "reminder"
        assert output_data["message"] == "Stand up and stretch"
        # Scheduler file should still be empty
        data = json.loads(scheduler_file.read_text())
        assert len(data["items"]) == 0

    def test_schedule_watch_dry_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run prints watch preview and does not write to scheduler file."""
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        scheduler_file.parent.mkdir(parents=True)
        scheduler_file.write_text(json.dumps({"items": []}))
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)

        result = runner.invoke(
            app,
            [
                "schedule",
                "watch",
                "github-pr",
                "owner/repo#42",
                "--message",
                "PR ready",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        output_data = json.loads(result.output)
        assert "item" in output_data
        output_data = output_data["item"]
        assert output_data["type"] == "watch"
        assert output_data["provider"] == "github-pr"
        assert output_data["target"] == "owner/repo#42"
        assert output_data["condition"] == "approved_or_merged"
        assert output_data["message"] == "PR ready"
        assert output_data["status"] == "active"
        assert output_data["poll_interval_minutes"] == 30
        data = json.loads(scheduler_file.read_text())
        assert len(data["items"]) == 0

    def test_schedule_watch_dry_run_invalid_target(self) -> None:
        """--dry-run with invalid github-pr target exits 1."""
        result = runner.invoke(
            app,
            ["schedule", "watch", "github-pr", "bad-format", "-m", "test", "--dry-run"],
        )
        assert result.exit_code == 1

    def test_schedule_recurring_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run prints cron preview and does not write to scheduler file."""
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        scheduler_file.parent.mkdir(parents=True)
        scheduler_file.write_text(json.dumps({"items": []}))
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)

        result = runner.invoke(
            app,
            [
                "schedule",
                "recurring",
                "--message",
                "health check",
                "--cron",
                "*/15 * * * *",
                "--prompt",
                "Take a break",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        output_data = json.loads(result.output)
        assert "item" in output_data
        output_data = output_data["item"]
        assert output_data["type"] == "recurring"
        assert output_data["cron"] == "*/15 * * * *"
        assert output_data["prompt"] == "Take a break"
        assert output_data["message"] == "health check"
        assert output_data["status"] == "active"
        assert output_data["session_required"] is True
        data = json.loads(scheduler_file.read_text())
        assert len(data["items"]) == 0

    def test_pair_dry_run(self, profile_with_instance: Path) -> None:
        """--dry-run prints pairing intent JSON without relay interaction."""
        result = runner.invoke(
            app,
            [
                "pair",
                "--peer",
                "work",
                "--dry-run",
                "--profile",
                str(profile_with_instance),
            ],
        )
        assert result.exit_code == 0, result.output
        output_data = json.loads(result.output)
        assert output_data["action"] == "initiate_pairing"
        assert output_data["peer_label"] == "work"


# ── TestStructuredErrors ────────────────────────────────────────────────────


class TestStructuredErrors:
    """Structured JSON errors on stderr when not a TTY."""

    def test_profile_not_found_json_error(self, tmp_path: Path) -> None:
        """Non-TTY stderr emits JSON with PROFILE_NOT_FOUND code."""
        bad_path = tmp_path / "nonexistent.json"
        result = runner.invoke(app, ["inbox", "--profile", str(bad_path)])
        assert result.exit_code == 1
        # CliRunner captures stderr; parse JSON from output
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "PROFILE_NOT_FOUND"
        assert str(bad_path) in payload["error"]["message"]
        assert payload["error"]["context"]["path"] == str(bad_path)

    def test_profile_not_found_tty_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TTY stderr emits Rich-formatted text, not JSON."""
        import io

        from aya.cli import ErrorCode, _emit_error

        fake_stderr = io.StringIO()
        fake_stderr.isatty = lambda: True  # type: ignore[attr-defined]
        monkeypatch.setattr("aya.cli.sys.stderr", fake_stderr)

        # _emit_error writes to the module-level `err` Console, which
        # resolves sys.stderr lazily — so we also redirect the Console's
        # output to our fake stream for capture.
        monkeypatch.setattr("aya.cli.err", Console(file=fake_stderr))

        with pytest.raises(typer.Exit):
            _emit_error(
                ErrorCode.PROFILE_NOT_FOUND,
                "Profile not found at /tmp/x. Run 'aya init' first.",
                {"path": "/tmp/x"},
            )
        output = fake_stderr.getvalue()
        # Should NOT be valid JSON — Rich text instead
        with pytest.raises(json.JSONDecodeError):
            json.loads(output)
        assert "Profile not found" in output

    def test_instance_not_found_json_error(self, profile_with_multiple_instances: Path) -> None:
        """Non-TTY stderr emits JSON with INSTANCE_NOT_FOUND code."""
        result = runner.invoke(
            app,
            ["inbox", "--as", "nosuch", "--profile", str(profile_with_multiple_instances)],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "INSTANCE_NOT_FOUND"
        assert payload["error"]["context"]["instance"] == "nosuch"

    def test_packet_not_found_json_error(self, profile_with_instance: Path) -> None:
        """Non-TTY stderr emits JSON with PACKET_NOT_FOUND code for unknown ack ID."""
        fake_id = "01AAAAAA00000000000000ZZZZ"
        result = runner.invoke(
            app,
            ["ack", fake_id, "--profile", str(profile_with_instance)],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "PACKET_NOT_FOUND"
        assert payload["error"]["context"]["packet_id"] == fake_id

    def test_invalid_argument_json_error(self, profile_with_instance: Path) -> None:
        """Non-TTY stderr emits JSON with INVALID_ARGUMENT for --as/--instance conflict."""
        result = runner.invoke(
            app,
            [
                "inbox",
                "--as",
                "foo",
                "--instance",
                "bar",
                "--profile",
                str(profile_with_instance),
            ],
        )
        assert result.exit_code == 2
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "INVALID_ARGUMENT"
        assert "--as and --instance" in payload["error"]["message"]


# ── JSON format for mutating commands ────────────────────────────────────────


class TestJsonFormat:
    """Tests for --format json on mutating CLI commands (#137)."""

    def test_init_json_format(self, tmp_path: Path) -> None:
        """init --format json outputs JSON with profile_path and did."""
        path = tmp_path / "profile.json"
        result = runner.invoke(
            app, ["init", "--profile", str(path), "--label", "test", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["profile_path"] == str(path)
        assert data["did"].startswith("did:key:")
        assert data["instance"] == "test"

    def test_trust_json_format(self, profile_with_instance: Path) -> None:
        """trust --format json outputs JSON with did and label."""
        home = Identity.generate("home")
        result = runner.invoke(
            app,
            [
                "trust",
                home.did,
                "--peer",
                "home",
                "--profile",
                str(profile_with_instance),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["did"] == home.did
        assert data["label"] == "home"
        assert data["nostr_pubkey"] is None

    def test_send_json_format(self, profile_with_trusted: Path, tmp_path: Path) -> None:
        """send --format json outputs JSON with packet_id and event_id."""
        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]
        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="json format test",
            content="hello",
        )
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        mock_event_id = "e" * 64
        mock_publish = AsyncMock(return_value=mock_event_id)
        with patch("aya.cli.RelayClient") as mock_client_cls:
            mock_client_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "send",
                    str(packet_file),
                    "--profile",
                    str(profile_with_trusted),
                    "--format",
                    "json",
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["packet_id"] == pkt.id
        assert data["event_id"] == mock_event_id
        assert "relay" in data

    def test_schedule_remind_json_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """schedule remind --format json outputs {"item": ...} wrapper."""
        scheduler_file = tmp_path / "assistant" / "memory" / "scheduler.json"
        alerts_file = tmp_path / "assistant" / "memory" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
        scheduler_file.write_text(json.dumps({"items": []}))
        alerts_file.write_text(json.dumps({"alerts": []}))

        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        result = runner.invoke(
            app,
            [
                "schedule",
                "remind",
                "--message",
                "Test reminder",
                "--due",
                "in 1 hour",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "item" in data
        data = data["item"]
        assert data["message"] == "Test reminder"
        assert data["type"] == "reminder"
        assert "id" in data


# ── TestPacketPersistence ────────────────────────────────────────────────────


class TestPacketPersistence:
    """Tests for packet persistence, show, and packets commands."""

    @pytest.fixture
    def packets_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Set up a packets directory and patch PACKETS_DIR to point to it."""
        packets = tmp_path / "packets"
        packets.mkdir()
        import aya.paths

        monkeypatch.setattr(aya.paths, "PACKETS_DIR", packets)
        return packets

    @pytest.fixture
    def sample_packet(self) -> Packet:
        local = Identity.generate("default")
        home = Identity.generate("home")
        return Packet(
            **{"from": home.did, "to": local.did},
            intent="daily handoff",
            content="Here is today's summary.",
        )

    def test_ingest_persists_packet(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """After _ingest, a packet JSON file should exist in PACKETS_DIR."""
        packets = tmp_path / "packets"
        import aya.paths

        monkeypatch.setattr(aya.paths, "PACKETS_DIR", packets)

        local = Identity.generate("default")
        home = Identity.generate("home")
        pkt = Packet(
            **{"from": home.did, "to": local.did},
            intent="seed from home",
            content="test content",
        )

        from aya.cli import _ingest

        _ingest(pkt, quiet=True)

        assert packets.exists()
        packet_files = list(packets.glob("*.json"))
        assert len(packet_files) == 1
        assert packet_files[0].stem == pkt.id

    def test_show_displays_packet(self, packets_dir: Path, sample_packet: Packet) -> None:
        """show command displays packet content."""
        packet_file = packets_dir / f"{sample_packet.id}.json"
        packet_file.write_text(sample_packet.to_json())

        result = runner.invoke(app, ["show", sample_packet.id[:8], "--format", "text"])
        assert result.exit_code == 0, result.output
        assert "daily handoff" in result.output

    def test_show_json_format(self, packets_dir: Path, sample_packet: Packet) -> None:
        """show --format json returns valid JSON with expected fields."""
        packet_file = packets_dir / f"{sample_packet.id}.json"
        packet_file.write_text(sample_packet.to_json())

        result = runner.invoke(app, ["show", sample_packet.id[:8], "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["intent"] == "daily handoff"
        assert data["id"] == sample_packet.id

    def test_packets_list(
        self,
        packets_dir: Path,
    ) -> None:
        """packets command lists stored packets."""
        local = Identity.generate("default")
        home = Identity.generate("home")
        for i in range(3):
            pkt = Packet(
                **{"from": home.did, "to": local.did},
                intent=f"packet {i}",
                content=f"content {i}",
            )
            (packets_dir / f"{pkt.id}.json").write_text(pkt.to_json())

        result = runner.invoke(app, ["packets", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["packets"]) == 3

    def test_show_unknown_id(self, packets_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """show with an unknown ID exits nonzero."""
        monkeypatch.setenv("AYA_FORMAT", "json")
        result = runner.invoke(app, ["show", "00000000unknown"])
        assert result.exit_code != 0


# ── Idempotency ─────────────────────────────────────────────────────────────


class TestIdempotency:
    """Tests for --idempotency-key dedup on send, dispatch, and ack."""

    def test_send_idempotency_key_dedup(
        self, profile_with_trusted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second send with same key returns cached result without calling publish."""
        monkeypatch.setenv("AYA_HOME", str(tmp_path / "aya_home"))
        monkeypatch.setenv("AYA_FORMAT", "json")

        # Reload paths with patched env
        import importlib

        import aya.paths

        importlib.reload(aya.paths)

        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]

        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="idempotent test",
            content="hello",
        )
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        mock_publish = AsyncMock(return_value="e" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            # First send
            result1 = runner.invoke(
                app,
                [
                    "send",
                    str(packet_file),
                    "--idempotency-key",
                    "key-1",
                    "--profile",
                    str(profile_with_trusted),
                ],
            )
        assert result1.exit_code == 0, result1.output
        data1 = json.loads(result1.output)
        assert "cached" not in data1
        mock_publish.assert_awaited_once()

        # Second send with same key — should be cached
        mock_publish2 = AsyncMock(return_value="f" * 64)
        with patch("aya.cli.RelayClient") as mock_cls2:
            mock_cls2.return_value.publish = mock_publish2
            result2 = runner.invoke(
                app,
                [
                    "send",
                    str(packet_file),
                    "--idempotency-key",
                    "key-1",
                    "--profile",
                    str(profile_with_trusted),
                ],
            )
        assert result2.exit_code == 0, result2.output
        data2 = json.loads(result2.output)
        assert data2["cached"] is True
        assert data2["event_id"] == "e" * 64
        mock_publish2.assert_not_awaited()

    def test_send_different_key_sends(
        self, profile_with_trusted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Different idempotency keys both trigger publish."""
        monkeypatch.setenv("AYA_HOME", str(tmp_path / "aya_home"))
        monkeypatch.setenv("AYA_FORMAT", "json")

        import importlib

        import aya.paths

        importlib.reload(aya.paths)

        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]

        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="test",
            content="hello",
        )
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        for key_name in ("key-a", "key-b"):
            mock_publish = AsyncMock(return_value="a" * 64)
            with patch("aya.cli.RelayClient") as mock_cls:
                mock_cls.return_value.publish = mock_publish
                result = runner.invoke(
                    app,
                    [
                        "send",
                        str(packet_file),
                        "--idempotency-key",
                        key_name,
                        "--profile",
                        str(profile_with_trusted),
                    ],
                )
            assert result.exit_code == 0, result.output
            mock_publish.assert_awaited_once()

    def test_dispatch_idempotency_key(
        self, profile_with_trusted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dispatch with --idempotency-key dedup works the same as send."""
        monkeypatch.setenv("AYA_HOME", str(tmp_path / "aya_home"))
        monkeypatch.setenv("AYA_FORMAT", "json")

        import importlib

        import aya.paths

        importlib.reload(aya.paths)

        mock_publish = AsyncMock(return_value="d" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result1 = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "test dispatch",
                    "--idempotency-key",
                    "dispatch-key-1",
                    "--profile",
                    str(profile_with_trusted),
                ],
                input="dispatch content\n",
            )
        assert result1.exit_code == 0, result1.output
        data1 = json.loads(result1.output)
        assert "cached" not in data1
        mock_publish.assert_awaited_once()

        # Second dispatch with same key — cached
        mock_publish2 = AsyncMock(return_value="e" * 64)
        with patch("aya.cli.RelayClient") as mock_cls2:
            mock_cls2.return_value.publish = mock_publish2
            result2 = runner.invoke(
                app,
                [
                    "dispatch",
                    "--to",
                    "home",
                    "--intent",
                    "test dispatch",
                    "--idempotency-key",
                    "dispatch-key-1",
                    "--profile",
                    str(profile_with_trusted),
                ],
                input="dispatch content\n",
            )
        assert result2.exit_code == 0, result2.output
        data2 = json.loads(result2.output)
        assert data2["cached"] is True
        mock_publish2.assert_not_awaited()

    def test_idempotency_cache_expires(
        self, profile_with_trusted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache entries older than 24h are treated as expired — publish fires again."""
        aya_home = tmp_path / "aya_home"
        monkeypatch.setenv("AYA_HOME", str(aya_home))
        monkeypatch.setenv("AYA_FORMAT", "json")

        import importlib

        import aya.paths

        importlib.reload(aya.paths)

        # Write an expired cache entry manually
        aya_home.mkdir(parents=True, exist_ok=True)
        expired_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        cache = {
            "expired-key": {
                "packet_id": "old_packet_id",
                "event_id": "old_event_id",
                "sent_at": expired_time,
            }
        }
        (aya_home / "sent_cache.json").write_text(json.dumps(cache))

        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]

        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="after expiry",
            content="hello",
        )
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        mock_publish = AsyncMock(return_value="n" * 64)
        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = mock_publish
            result = runner.invoke(
                app,
                [
                    "send",
                    str(packet_file),
                    "--idempotency-key",
                    "expired-key",
                    "--profile",
                    str(profile_with_trusted),
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "cached" not in data  # should not be cached — expired
        mock_publish.assert_awaited_once()

    def test_send_without_key_always_sends(
        self, profile_with_trusted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --idempotency-key, every send calls publish."""
        monkeypatch.setenv("AYA_HOME", str(tmp_path / "aya_home"))
        monkeypatch.setenv("AYA_FORMAT", "json")

        import importlib

        import aya.paths

        importlib.reload(aya.paths)

        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]

        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="no key test",
            content="hello",
        )
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        for _ in range(2):
            mock_publish = AsyncMock(return_value="a" * 64)
            with patch("aya.cli.RelayClient") as mock_cls:
                mock_cls.return_value.publish = mock_publish
                result = runner.invoke(
                    app,
                    [
                        "send",
                        str(packet_file),
                        "--profile",
                        str(profile_with_trusted),
                    ],
                )
            assert result.exit_code == 0, result.output
            mock_publish.assert_awaited_once()


# ── TestRead ──────────────────────────────────────────────────────────────────


class TestRead:
    """Tests for the `aya read` command."""

    @pytest.fixture
    def packets_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        packets = tmp_path / "packets"
        packets.mkdir()
        import aya.paths

        monkeypatch.setattr(aya.paths, "PACKETS_DIR", packets)
        return packets

    @pytest.fixture
    def seed_packet(self) -> Packet:
        local = Identity.generate("default")
        home = Identity.generate("home")
        return Packet.as_seed(
            from_did=home.did,
            to_did=local.did,
            intent="seed test",
            opener="What's the plan for tomorrow?",
            context_summary="Wrapping up the relay project.",
            open_questions=["who reviews?", "merge target?"],
        )

    @pytest.fixture
    def content_packet(self) -> Packet:
        local = Identity.generate("default")
        home = Identity.generate("home")
        return Packet(
            **{"from": home.did, "to": local.did},
            intent="markdown body",
            content="# Notes\n\nA short markdown body.",
        )

    def test_extracts_seed_opener_and_context(self, packets_dir: Path, seed_packet: Packet) -> None:
        (packets_dir / f"{seed_packet.id}.json").write_text(seed_packet.to_json())
        result = runner.invoke(app, ["read", seed_packet.id, "--format", "text"])
        assert result.exit_code == 0, result.output
        assert "What's the plan for tomorrow?" in result.output
        assert "Wrapping up the relay project." in result.output
        assert "who reviews?" in result.output
        assert "merge target?" in result.output

    def test_extracts_content_string(self, packets_dir: Path, content_packet: Packet) -> None:
        (packets_dir / f"{content_packet.id}.json").write_text(content_packet.to_json())
        result = runner.invoke(app, ["read", content_packet.id, "--format", "text"])
        assert result.exit_code == 0, result.output
        assert "A short markdown body." in result.output

    def test_meta_flag_adds_header(self, packets_dir: Path, seed_packet: Packet) -> None:
        (packets_dir / f"{seed_packet.id}.json").write_text(seed_packet.to_json())
        result = runner.invoke(app, ["read", seed_packet.id, "--meta", "--format", "text"])
        assert result.exit_code == 0, result.output
        assert "seed test" in result.output  # intent
        assert seed_packet.id[:12] in result.output

    def test_json_format_returns_id_and_body(self, packets_dir: Path, seed_packet: Packet) -> None:
        (packets_dir / f"{seed_packet.id}.json").write_text(seed_packet.to_json())
        result = runner.invoke(app, ["read", seed_packet.id, "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["id"] == seed_packet.id
        assert "What's the plan" in data["body"]
        # No metadata fields without --meta
        assert "from" not in data

    def test_json_meta_includes_metadata_fields(
        self, packets_dir: Path, seed_packet: Packet
    ) -> None:
        (packets_dir / f"{seed_packet.id}.json").write_text(seed_packet.to_json())
        result = runner.invoke(app, ["read", seed_packet.id, "--meta", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["from"].startswith("did:key:")
        assert data["intent"] == "seed test"
        assert "sent_at" in data

    def test_prefix_match_resolves_full_id(self, packets_dir: Path, seed_packet: Packet) -> None:
        (packets_dir / f"{seed_packet.id}.json").write_text(seed_packet.to_json())
        prefix = seed_packet.id[:10]
        result = runner.invoke(app, ["read", prefix, "--format", "text"])
        assert result.exit_code == 0, result.output

    def test_packet_not_found_errors(self, packets_dir: Path) -> None:
        result = runner.invoke(app, ["read", "01XXXXXXXXXX", "--format", "text"])
        assert result.exit_code != 0

    def test_prefix_too_short_errors(self, packets_dir: Path) -> None:
        result = runner.invoke(app, ["read", "01XX", "--format", "text"])
        assert result.exit_code != 0

    def test_json_format_preserves_structured_body_for_json_content(
        self, packets_dir: Path
    ) -> None:
        """Non-seed dict content must pass through as a structured object
        in JSON output mode, not be stringified. Callers that pipe
        ``aya read --format json | jq`` should get a real object back."""
        from aya.packet import ContentType

        local = Identity.generate("default")
        home = Identity.generate("home")
        pkt = Packet(
            **{"from": home.did, "to": local.did},
            intent="structured payload",
            content_type=ContentType.JSON,
            content={
                "event": "deployed",
                "version": "1.2.3",
                "checks": ["lint", "test"],
            },
        )
        (packets_dir / f"{pkt.id}.json").write_text(pkt.to_json())

        result = runner.invoke(app, ["read", pkt.id, "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # body is a dict, not a string containing pretty-printed JSON
        assert isinstance(data["body"], dict)
        assert data["body"]["event"] == "deployed"
        assert data["body"]["version"] == "1.2.3"
        assert data["body"]["checks"] == ["lint", "test"]

    def test_text_format_still_stringifies_json_content(self, packets_dir: Path) -> None:
        """Text mode output hasn't regressed: non-seed dicts still render as
        pretty-printed JSON for human reading."""
        from aya.packet import ContentType

        local = Identity.generate("default")
        home = Identity.generate("home")
        pkt = Packet(
            **{"from": home.did, "to": local.did},
            intent="structured payload",
            content_type=ContentType.JSON,
            content={"event": "deployed", "version": "1.2.3"},
        )
        (packets_dir / f"{pkt.id}.json").write_text(pkt.to_json())

        result = runner.invoke(app, ["read", pkt.id, "--format", "text"])
        assert result.exit_code == 0, result.output
        # Text mode prints the pretty-printed JSON body
        assert '"event": "deployed"' in result.output
        assert '"version": "1.2.3"' in result.output


# ── TestDrop ──────────────────────────────────────────────────────────────────


class TestDrop:
    """Tests for the `aya drop` command and inbox filtering of dropped IDs."""

    @pytest.fixture
    def sender(self) -> Identity:
        return Identity.generate("work")

    @pytest.fixture
    def profile_with_sender(self, profile_with_instance: Path, sender: Identity) -> Path:
        p = Profile.load(profile_with_instance)
        p.trusted_keys["work"] = TrustedKey(
            did=sender.did, label="work", nostr_pubkey=sender.nostr_public_hex
        )
        p.save(profile_with_instance)
        return profile_with_instance

    def _signed_packet(self, sender: Identity, to_did: str, intent: str = "Test packet") -> Packet:
        pkt = Packet(
            **{"from": sender.did, "to": to_did},
            intent=intent,
            content="Test content.",
        )
        return pkt.sign(sender)

    def test_drop_full_id_persists_to_profile(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["drop", packet.id, "--profile", str(profile_with_sender), "--format", "json"],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["dropped"] == packet.id
        assert data["already_dropped"] is False

        reloaded = Profile.load(profile_with_sender)
        assert packet.id in reloaded.dropped_ids

    def test_drop_is_idempotent(self, profile_with_sender: Path, sender: Identity) -> None:
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did)
        p.dropped_ids.append(packet.id)
        p.save(profile_with_sender)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["drop", packet.id, "--profile", str(profile_with_sender), "--format", "json"],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["already_dropped"] is True

        reloaded = Profile.load(profile_with_sender)
        assert reloaded.dropped_ids.count(packet.id) == 1

    def test_drop_resolves_prefix_from_ingested_ids(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did)
        recent_ts = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        p.ingested_ids.append({"id": packet.id, "ingested_at": recent_ts})
        p.save(profile_with_sender)

        # No relay mock — should resolve from ingested_ids without hitting the network
        result = runner.invoke(
            app,
            ["drop", packet.id[:10], "--profile", str(profile_with_sender), "--format", "json"],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["dropped"] == packet.id

    def test_drop_resolves_prefix_from_relay_when_not_ingested(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                [
                    "drop",
                    packet.id[:10],
                    "--profile",
                    str(profile_with_sender),
                    "--format",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["dropped"] == packet.id

    def test_drop_packet_not_found_errors(self, profile_with_sender: Path) -> None:
        async def mock_fetch(*args, **kwargs):
            if False:  # pragma: no cover
                yield

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                [
                    "drop",
                    "01XXXXXXXXXX",
                    "--profile",
                    str(profile_with_sender),
                    "--format",
                    "json",
                ],
            )

        assert result.exit_code != 0

    def test_drop_prefix_too_short_errors(self, profile_with_sender: Path) -> None:
        result = runner.invoke(
            app,
            ["drop", "01XX", "--profile", str(profile_with_sender), "--format", "json"],
        )
        assert result.exit_code != 0

    def test_inbox_filters_dropped_packets(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="Stuck packet")
        p.dropped_ids.append(packet.id)
        p.save(profile_with_sender)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                ["inbox", "--format", "text", "--profile", str(profile_with_sender)],
            )

        assert result.exit_code == 0, result.output
        assert "Stuck packet" not in result.output
        assert "Inbox empty" in result.output

    def test_inbox_all_also_filters_dropped(
        self, profile_with_sender: Path, sender: Identity
    ) -> None:
        """--all should also exclude dropped packets — drop is permanent ignore."""
        p = Profile.load(profile_with_sender)
        packet = self._signed_packet(sender, p.instances["default"].did, intent="Dropped packet")
        p.dropped_ids.append(packet.id)
        p.save(profile_with_sender)

        async def mock_fetch(*args, **kwargs):
            yield packet

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = mock_fetch
            result = runner.invoke(
                app,
                [
                    "inbox",
                    "--all",
                    "--format",
                    "text",
                    "--profile",
                    str(profile_with_sender),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Dropped packet" not in result.output

    def test_drop_relay_fetch_times_out(
        self,
        profile_with_sender: Path,
        sender: Identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A slow/large relay should not wedge `aya drop` indefinitely.

        Mocks `fetch_pending` as an async generator that sleeps longer
        than the configured timeout. The command should exit non-zero
        with a RELAY_TIMEOUT error. Uses a tiny timeout (0.1s) patched
        onto the cli module so the test is fast.
        """
        import asyncio as _asyncio

        monkeypatch.setattr("aya.cli._RELAY_FETCH_TIMEOUT_SECONDS", 0.1)

        async def slow_fetch(*args, **kwargs):
            # Simulate a relay that keeps sending packets but each one
            # takes longer than the timeout window. In practice this
            # could be network latency, a large inbox, or a stalled
            # subscription.
            await _asyncio.sleep(2.0)
            # pragma: no cover — never reached because the timeout fires first
            if False:  # pragma: no cover
                yield

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.fetch_pending = slow_fetch
            result = runner.invoke(
                app,
                [
                    "drop",
                    "01ABCDEFGH",  # prefix not in ingested/dropped — forces relay
                    "--profile",
                    str(profile_with_sender),
                    "--format",
                    "json",
                ],
            )

        assert result.exit_code != 0
        # Error payload includes the RELAY_TIMEOUT code + timeout duration
        assert "RELAY_TIMEOUT" in result.output
        assert "timed out" in result.output


# ── TestSendSignatureValidation ───────────────────────────────────────────────


class TestSendSignatureValidation:
    """Tests for signature validation in `aya send`.

    Three paths:
      - Missing/invalid signature, from_did matches local → re-sign + send
      - Missing/invalid signature, from_did is external → reject
      - Valid signature → pass through unchanged
    """

    def test_resigns_when_signature_missing_and_local_is_sender(
        self, profile_with_trusted: Path, tmp_path: Path
    ) -> None:
        """Empty-sig packet authored by local instance is auto-signed before send."""
        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]

        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="hand-edited packet",
            content="hello",
        )
        # Note: no .sign() call — signature is None
        assert pkt.signature is None
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        captured: dict = {}

        async def fake_publish(packet, *args, **kwargs):
            captured["packet"] = packet
            return "e" * 64

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = fake_publish
            result = runner.invoke(
                app,
                ["send", str(packet_file), "--profile", str(profile_with_trusted)],
            )

        assert result.exit_code == 0, result.output
        assert captured["packet"].signature is not None
        # And the freshly applied signature is valid
        assert captured["packet"].verify_from_did()

    def test_resigns_when_signature_invalid_and_local_is_sender(
        self, profile_with_trusted: Path, tmp_path: Path
    ) -> None:
        """Garbage-sig packet authored by local instance is auto-resigned."""
        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]

        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="bad sig packet",
            content="hello",
        )
        # Inject a bogus base64 signature so verify_from_did() returns False
        pkt.signature = "A" * 100
        assert not pkt.verify_from_did()
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        captured: dict = {}

        async def fake_publish(packet, *args, **kwargs):
            captured["packet"] = packet
            return "e" * 64

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = fake_publish
            result = runner.invoke(
                app,
                ["send", str(packet_file), "--profile", str(profile_with_trusted)],
            )

        assert result.exit_code == 0, result.output
        # Signature replaced with a valid one
        assert captured["packet"].verify_from_did()

    def test_rejects_when_signature_missing_and_sender_is_external(
        self, profile_with_trusted: Path, tmp_path: Path
    ) -> None:
        """Empty-sig packet claiming to be from a different sender is refused."""
        p = Profile.load(profile_with_trusted)
        home_key = p.trusted_keys["home"]
        other_sender = Identity.generate("offline")

        pkt = Packet(
            **{"from": other_sender.did, "to": home_key.did},
            intent="forged-looking packet",
            content="hello",
        )
        assert pkt.signature is None
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        publish_calls = 0

        async def fake_publish(*args, **kwargs):
            nonlocal publish_calls
            publish_calls += 1
            return "e" * 64

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = fake_publish
            result = runner.invoke(
                app,
                [
                    "send",
                    str(packet_file),
                    "--profile",
                    str(profile_with_trusted),
                    "--format",
                    "json",
                ],
            )

        assert result.exit_code != 0
        assert publish_calls == 0  # never reached the relay

    def test_passes_through_valid_signature(
        self, profile_with_trusted: Path, tmp_path: Path
    ) -> None:
        """Properly-signed packet sends without modification."""
        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]

        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="properly signed",
            content="hello",
        ).sign(local)
        original_sig = pkt.signature
        assert pkt.verify_from_did()
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        captured: dict = {}

        async def fake_publish(packet, *args, **kwargs):
            captured["packet"] = packet
            return "e" * 64

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = fake_publish
            result = runner.invoke(
                app,
                ["send", str(packet_file), "--profile", str(profile_with_trusted)],
            )

        assert result.exit_code == 0, result.output
        # Signature unchanged — pass-through, not re-signed
        assert captured["packet"].signature == original_sig

    def test_resign_surfaces_console_notice_in_text_mode(
        self, profile_with_trusted: Path, tmp_path: Path
    ) -> None:
        """When aya send re-signs in interactive/text mode, the user
        should see a visible notice. Silent mutation is surprising."""
        p = Profile.load(profile_with_trusted)
        local = p.instances["default"]
        home_key = p.trusted_keys["home"]

        pkt = Packet(
            **{"from": local.did, "to": home_key.did},
            intent="silent resign",
            content="hello",
        )
        assert pkt.signature is None
        packet_file = tmp_path / "packet.json"
        packet_file.write_text(pkt.to_json())

        async def fake_publish(packet, *args, **kwargs):
            return "e" * 64

        with patch("aya.cli.RelayClient") as mock_cls:
            mock_cls.return_value.publish = fake_publish
            result = runner.invoke(
                app,
                [
                    "send",
                    str(packet_file),
                    "--profile",
                    str(profile_with_trusted),
                    "--format",
                    "text",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Re-signed packet" in result.output
