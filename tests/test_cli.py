"""Tests for cli.py — smoke tests using typer.testing.CliRunner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
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

        result = runner.invoke(app, ["schedule", "dismiss", prefix])
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
    def test_no_crons_exits_silently(self):
        result = runner.invoke(app, ["hook", "crons"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_outputs_valid_json_with_crons(self, tmp_path, monkeypatch):
        scheduler_file = tmp_path / "sched" / "scheduler.json"
        alerts_file = tmp_path / "sched" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
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
        alerts_file.write_text(json.dumps({"alerts": []}))
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        result = runner.invoke(app, ["hook", "crons"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "hookSpecificOutput" in data
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "CronCreate" in ctx
        assert "test-cron" in ctx

    def test_multiple_crons_emit_separate_lines(self, tmp_path, monkeypatch):
        """Each session cron must produce its own JSON line so Claude Code
        creates a separate system reminder per cron — prevents truncation
        when multiple crons are bundled into a single hookSpecificOutput."""
        scheduler_file = tmp_path / "sched" / "scheduler.json"
        alerts_file = tmp_path / "sched" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
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
        alerts_file.write_text(json.dumps({"alerts": []}))
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

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

    def test_escapes_double_quotes_in_prompt(self, tmp_path, monkeypatch):
        """Prompts with double quotes must be escaped to avoid malformed output."""
        scheduler_file = tmp_path / "sched" / "scheduler.json"
        alerts_file = tmp_path / "sched" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
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
        alerts_file.write_text(json.dumps({"alerts": []}))
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        result = runner.invoke(app, ["hook", "crons"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        # Quotes in the prompt must be escaped
        assert r"\"hello\"" in ctx
        # Must not contain unescaped quotes that would break parsing
        assert 'prompt="Say \\"hello\\" to the user."' in ctx

    def test_does_not_claim_alerts(self, tmp_path, monkeypatch):
        """hook crons must not consume alerts — they belong to schedule pending."""
        scheduler_file = tmp_path / "sched" / "scheduler.json"
        alerts_file = tmp_path / "sched" / "alerts.json"
        scheduler_file.parent.mkdir(parents=True)
        scheduler_file.write_text(json.dumps({"items": []}))
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
        monkeypatch.setattr("aya.scheduler.SCHEDULER_FILE", scheduler_file)
        monkeypatch.setattr("aya.scheduler.ALERTS_FILE", alerts_file)

        # Run hook crons
        runner.invoke(app, ["hook", "crons"])

        # Alerts must still be unseen
        alerts = json.loads(alerts_file.read_text())["alerts"]
        assert len(alerts) == 1
        assert alerts[0]["seen"] is False
        assert "delivered_at" not in alerts[0]


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

        # Record a previous check time on one relay
        relay_url = p.default_relays[0]
        last_check_time = datetime(2026, 3, 31, 12, 0, 0, tzinfo=UTC)
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
        assert len(data) == 1
        assert data[0]["ingested"] is True

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
        assert len(data) == 1
        assert "ingested" not in data[0]


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
        profile.ingested_ids.append({"id": pkt.id, "ingested_at": now_iso})

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
                ["ack", packet_id, "looks good", "--profile", str(profile_path)],
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
                ["ack", prefix, "--profile", str(profile_path)],
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
                ["ack", packet_id, "--dismiss", "--profile", str(profile_path)],
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
