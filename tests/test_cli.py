"""Tests for cli.py — smoke tests using typer.testing.CliRunner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aya.cli import app
from aya.identity import Identity, Profile, TrustedKey
from aya.scheduler import add_reminder

runner = CliRunner()


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


# ── bootstrap ─────────────────────────────────────────────────────────────────


class TestBootstrap:
    def test_bootstrap_creates_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "myworkspace"
        root.mkdir()
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()

        monkeypatch.setattr("aya.workspace.Path.home", lambda: fake_home)

        result = runner.invoke(
            app,
            [
                "bootstrap",
                "--root",
                str(root),
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (root / "CLAUDE.md").exists()
        assert (root / "assistant" / "memory" / "scheduler.json").exists()

    def test_bootstrap_noninteractive_with_yes_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "workspace2"
        root.mkdir()
        fake_home = tmp_path / "fakehome2"
        fake_home.mkdir()

        monkeypatch.setattr("aya.workspace.Path.home", lambda: fake_home)

        result = runner.invoke(app, ["bootstrap", "--root", str(root), "--yes"])
        # Should not prompt and should succeed
        assert result.exit_code == 0
