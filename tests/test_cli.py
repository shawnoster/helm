"""Tests for cli.py — smoke tests using typer.testing.CliRunner."""

from __future__ import annotations

import json
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
        result = runner.invoke(app, ["version"])
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
                "--label",
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
                "--label",
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
                "--label",
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
                "--label",
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


# ── pack ──────────────────────────────────────────────────────────────────────


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
                "--instance",
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
        """When only one instance exists and its name differs from --instance, use it anyway."""
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
                "--instance",
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
                "--instance",
                "default",
                "--profile",
                str(profile_with_multiple_instances),
            ],
            input="data\n",
        )
        assert result.exit_code != 0
        # Error should mention available instance names
        combined = result.stdout + (result.stderr or "")
        assert "work" in combined and "laptop" in combined


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

    def test_dispatch_missing_instance_fails(self, profile_with_trusted: Path) -> None:
        result = runner.invoke(
            app,
            [
                "dispatch",
                "--to",
                "home",
                "--intent",
                "fail",
                "--instance",
                "nonexistent",
                "--profile",
                str(profile_with_trusted),
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
        assert "Could not reach relay" in result.output


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
        result = runner.invoke(app, ["schedule", "status", "--json"])
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
        p.ingested_ids.append(packet.id)
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
        assert packet.id in saved.ingested_ids

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
        assert packet.id in saved.ingested_ids

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
        assert packet.id in saved.ingested_ids
