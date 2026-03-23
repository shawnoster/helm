"""Tests for identity generation, DID derivation, and profile persistence."""

from __future__ import annotations

import json
from pathlib import Path

from aya.identity import Identity, Profile, TrustedKey


class TestIdentityGeneration:
    def test_generates_unique_keypairs(self) -> None:
        a = Identity.generate("work")
        b = Identity.generate("home")
        assert a.did != b.did
        assert a.private_key_hex != b.private_key_hex

    def test_did_format(self) -> None:
        identity = Identity.generate("test")
        assert identity.did.startswith("did:key:z6Mk")

    def test_sign_produces_bytes(self) -> None:
        identity = Identity.generate("test")
        sig = identity.sign(b"hello world")
        assert isinstance(sig, bytes)
        assert len(sig) == 64  # ed25519 signature length

    def test_private_key_roundtrip(self) -> None:
        identity = Identity.generate("test")
        # Reconstruct private key from hex and verify it produces same public key
        reconstructed = identity.private_key()
        pub = reconstructed.public_key().public_bytes_raw().hex()
        assert pub == identity.public_key_hex

    def test_nostr_pubkey_is_hex(self) -> None:
        identity = Identity.generate("test")
        pubkey = identity.nostr_pubkey()
        assert len(pubkey) == 64  # 32 bytes hex-encoded
        assert all(c in "0123456789abcdef" for c in pubkey)


class TestProfilePersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(json.dumps({"alias": "Ace", "user_name": "Shawn"}))

        p = Profile.load(profile_path)
        p.instances["work"] = Identity.generate("work")
        p.trusted_keys["home"] = TrustedKey(
            did="did:key:z6MkFakeHome", label="home", nostr_pubkey="abc123"
        )
        p.save(profile_path)

        restored = Profile.load(profile_path)
        assert "work" in restored.instances
        assert restored.instances["work"].did == p.instances["work"].did
        assert "home" in restored.trusted_keys

    def test_save_does_not_clobber_other_keys(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(
            json.dumps(
                {
                    "alias": "Cipher",
                    "ship_mind_name": "Dramatically Unbothered",
                    "user_name": "Shawn",
                    "movement_reminders": True,
                }
            )
        )

        p = Profile.load(profile_path)
        p.instances["work"] = Identity.generate("work")
        p.save(profile_path)

        data = json.loads(profile_path.read_text())
        # Existing keys must survive the save
        assert data["alias"] == "Cipher"
        assert data["ship_mind_name"] == "Dramatically Unbothered"
        assert data["movement_reminders"] is True

    def test_is_trusted(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text("{}")

        p = Profile.load(profile_path)
        home = Identity.generate("home")
        p.trusted_keys["home"] = TrustedKey(did=home.did, label="home")
        p.save(profile_path)

        restored = Profile.load(profile_path)
        assert restored.is_trusted(home.did)
        assert not restored.is_trusted("did:key:z6MkStranger")

    def test_active_instance_fallback(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text("{}")

        p = Profile.load(profile_path)
        assert p.active_instance() is None

        p.instances["work"] = Identity.generate("work")
        p.save(profile_path)

        restored = Profile.load(profile_path)
        # "default" label doesn't exist but should fall back to first instance
        assert restored.active_instance("default") is not None


# ── Multi-relay profile ───────────────────────────────────────────────────────


class TestProfileMultiRelay:
    def test_default_relays_saved_as_list(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text("{}")

        p = Profile.load(profile_path)
        p.default_relays = ["wss://relay1.example.com", "wss://relay2.example.com"]
        p.save(profile_path)

        data = json.loads(profile_path.read_text())
        assert data["aya"]["default_relays"] == [
            "wss://relay1.example.com",
            "wss://relay2.example.com",
        ]
        assert "default_relay" not in data["aya"]

    def test_load_legacy_default_relay_string(self, tmp_path: Path) -> None:
        """Profiles with the old scalar default_relay key are migrated transparently."""
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(json.dumps({"aya": {"default_relay": "wss://legacy.example.com"}}))

        p = Profile.load(profile_path)
        assert p.default_relays == ["wss://legacy.example.com"]
        assert p.default_relay == "wss://legacy.example.com"

    def test_load_new_default_relays_list(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(
            json.dumps(
                {
                    "aya": {
                        "default_relays": [
                            "wss://relay1.example.com",
                            "wss://relay2.example.com",
                        ]
                    }
                }
            )
        )

        p = Profile.load(profile_path)
        assert p.default_relays == ["wss://relay1.example.com", "wss://relay2.example.com"]
        assert p.default_relay == "wss://relay1.example.com"

    def test_default_relay_setter_updates_list(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text("{}")

        p = Profile.load(profile_path)
        p.default_relay = "wss://single.example.com"
        assert p.default_relays == ["wss://single.example.com"]
        assert p.default_relay == "wss://single.example.com"

    def test_legacy_key_dropped_on_save(self, tmp_path: Path) -> None:
        """After loading a legacy profile and saving, default_relay key is removed."""
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(json.dumps({"aya": {"default_relay": "wss://legacy.example.com"}}))

        p = Profile.load(profile_path)
        p.save(profile_path)

        data = json.loads(profile_path.read_text())
        assert "default_relay" not in data["aya"]
        assert data["aya"]["default_relays"] == ["wss://legacy.example.com"]
