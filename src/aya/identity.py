"""Identity management — did:key generation, keypair storage, trusted key registry."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import base58
from coincurve import PrivateKey as Secp256k1PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Multicodec prefix for ed25519 public keys: 0xed 0x01
_ED25519_MULTICODEC = bytes([0xED, 0x01])


@dataclass
class Identity:
    """A local assistant instance identity.

    Two keypairs:
      - ed25519: for did:key identity and packet signing (W3C standard)
      - secp256k1: for Nostr transport (BIP-340 Schnorr signatures)
    """

    did: str
    label: str  # "work", "home", "laptop", etc.
    private_key_hex: str  # ed25519 — identity / packet signing
    public_key_hex: str  # ed25519
    nostr_private_hex: str  # secp256k1 — Nostr transport
    nostr_public_hex: str  # secp256k1 x-only (32 bytes)

    @classmethod
    def generate(cls, label: str) -> Identity:
        """Generate ed25519 (did:key) + secp256k1 (Nostr) keypairs."""
        # ed25519 for did:key
        ed_private = Ed25519PrivateKey.generate()
        ed_pub_bytes = ed_private.public_key().public_bytes_raw()
        ed_priv_bytes = ed_private.private_bytes_raw()

        multicodec = _ED25519_MULTICODEC + ed_pub_bytes
        did = "did:key:z" + base58.b58encode(multicodec).decode()

        # secp256k1 for Nostr
        nostr_secret = secrets.token_bytes(32)
        nostr_key = Secp256k1PrivateKey(nostr_secret)
        # x-only public key (BIP-340): drop the 0x02/0x03 prefix byte
        nostr_pub_full = nostr_key.public_key.format(compressed=True)
        nostr_pub_xonly = nostr_pub_full[1:]  # 32 bytes

        return cls(
            did=did,
            label=label,
            private_key_hex=ed_priv_bytes.hex(),
            public_key_hex=ed_pub_bytes.hex(),
            nostr_private_hex=nostr_secret.hex(),
            nostr_public_hex=nostr_pub_xonly.hex(),
        )

    def private_key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_key_hex))

    def public_key(self) -> Ed25519PublicKey:
        return self.private_key().public_key()

    def sign(self, data: bytes) -> bytes:
        """Sign with ed25519 (for packet signatures)."""
        return self.private_key().sign(data)

    def nostr_sign(self, message_bytes: bytes) -> bytes:
        """Sign with secp256k1 Schnorr (BIP-340, for Nostr events)."""
        key = Secp256k1PrivateKey(bytes.fromhex(self.nostr_private_hex))
        return key.sign_schnorr(message_bytes)

    def nostr_pubkey(self) -> str:
        """Hex-encoded x-only secp256k1 public key for Nostr."""
        return self.nostr_public_hex


@dataclass
class TrustedKey:
    did: str
    label: str  # "home", "friend:alice", etc.
    nostr_pubkey: str | None = None


@dataclass
class Profile:
    """
    Persistent assistant profile — personality + identity.
    Stored at ~/.copilot/assistant_profile.json (or configured path).
    """

    alias: str
    ship_mind_name: str
    user_name: str
    instances: dict[str, Identity] = field(default_factory=dict)
    trusted_keys: dict[str, TrustedKey] = field(default_factory=dict)
    default_relay: str = "wss://relay.damus.io"
    last_checked: dict[str, str] = field(default_factory=dict)  # relay → ISO timestamp
    ingested_ids: list[str] = field(default_factory=list)  # packet IDs already ingested (dedup)

    @classmethod
    def load(cls, path: Path) -> Profile:
        """Load from assistant_profile.json, merging assistant-sync fields if present."""
        data = json.loads(path.read_text())
        instances = {}
        for k, v in data.get("assistant_sync", {}).get("instances", {}).items():
            # Migrate old profiles missing Nostr keys
            if "nostr_private_hex" not in v:
                nostr_secret = secrets.token_bytes(32)
                nostr_key = Secp256k1PrivateKey(nostr_secret)
                nostr_pub_xonly = nostr_key.public_key.format(compressed=True)[1:]
                v["nostr_private_hex"] = nostr_secret.hex()
                v["nostr_public_hex"] = nostr_pub_xonly.hex()
            instances[k] = Identity(**v)
        trusted = {
            k: TrustedKey(**v)
            for k, v in data.get("assistant_sync", {}).get("trusted_keys", {}).items()
        }
        return cls(
            alias=data.get("alias", "Ace"),
            ship_mind_name=data.get("ship_mind_name", ""),
            user_name=data.get("user_name", ""),
            instances=instances,
            trusted_keys=trusted,
            default_relay=data.get("assistant_sync", {}).get(
                "default_relay", "wss://relay.damus.io"
            ),
            last_checked=data.get("assistant_sync", {}).get("last_checked", {}),
            ingested_ids=data.get("assistant_sync", {}).get("ingested_ids", []),
        )

    def save(self, path: Path) -> None:
        """Write assistant-sync fields back into the profile without clobbering other keys."""
        data = json.loads(path.read_text()) if path.exists() else {}
        data.setdefault("assistant_sync", {})
        data["assistant_sync"]["instances"] = {
            k: {
                "did": v.did,
                "label": v.label,
                "private_key_hex": v.private_key_hex,
                "public_key_hex": v.public_key_hex,
                "nostr_private_hex": v.nostr_private_hex,
                "nostr_public_hex": v.nostr_public_hex,
            }
            for k, v in self.instances.items()
        }
        data["assistant_sync"]["trusted_keys"] = {
            k: {"did": v.did, "label": v.label, "nostr_pubkey": v.nostr_pubkey}
            for k, v in self.trusted_keys.items()
        }
        data["assistant_sync"]["default_relay"] = self.default_relay
        data["assistant_sync"]["last_checked"] = self.last_checked
        data["assistant_sync"]["ingested_ids"] = self.ingested_ids[-100:]  # keep last 100
        path.write_text(json.dumps(data, indent=2))

    def active_instance(self, label: str = "default") -> Identity | None:
        return self.instances.get(label) or next(iter(self.instances.values()), None)

    def is_trusted(self, did: str) -> bool:
        return did in {k.did for k in self.trusted_keys.values()}
