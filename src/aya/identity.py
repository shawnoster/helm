"""Identity management — did:key generation, keypair storage, trusted key registry."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import base58
from coincurve import PrivateKey as Secp256k1PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from ulid import ULID

logger = logging.getLogger(__name__)

# ── schema version ────────────────────────────────────────────────────────────
PROFILE_SCHEMA_VERSION = 1

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


_DEFAULT_RELAYS = ["wss://relay.damus.io", "wss://nos.lol"]


def _is_valid_ulid(value: str) -> bool:
    """Check if a string is a valid ULID."""
    try:
        ULID.from_str(value)
        return True
    except (ValueError, TypeError):
        return False


def _validate_instance(key: str, data: dict[str, Any]) -> Identity:
    """Validate and create an Instance from a dict, with helpful error messages.

    Raises ValueError with context if the dict is malformed.
    """
    try:
        return Identity(**data)
    except TypeError as e:
        raise ValueError(f"Instance '{key}' is malformed: {e}") from e
    except Exception as e:
        raise ValueError(f"Instance '{key}' could not be loaded: {e}") from e


def _validate_trusted_key(key: str, data: dict[str, Any]) -> TrustedKey:
    """Validate and create a TrustedKey from a dict, with helpful error messages.

    Raises ValueError with context if the dict is malformed.
    """
    try:
        return TrustedKey(**data)
    except TypeError as e:
        raise ValueError(
            f"Trusted key '{key}' is malformed: missing or invalid required field. {e}"
        ) from e
    except Exception as e:
        raise ValueError(f"Trusted key '{key}' could not be loaded: {e}") from e


def _assert_valid_ulid(id_: str) -> None:
    """Raise ``ValueError`` if *id_* is not a valid 26-character ULID.

    Call this before appending to ``ingested_ids`` so that truncated display
    prefixes or other malformed values are rejected at write time.
    """
    if not _is_valid_ulid(id_):
        raise ValueError(
            f"Refusing to store invalid ULID in ingested_ids (len={len(id_)}): {id_!r}"
        )


def _normalize_ingested_ids(raw: object) -> list[dict[str, str]]:
    """Coerce legacy string entries to the ``{id, ingested_at, from_did?}`` dict format.

    Older profiles stored bare packet-ID strings in ``ingested_ids``.  On
    first load after the migration, those strings are converted to dicts with
    ``ingested_at`` set to the current time so they survive the next TTL prune
    and don't cause an immediate false-re-ingestion.

    Entries whose ``id`` field is not a valid 26-character ULID are dropped,
    with a warning logged.  This serves as a one-time migration that removes any
    truncated 8-character display prefixes that were erroneously stored by older
    versions.
    """
    if not isinstance(raw, list):
        return []
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    result: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            if not _is_valid_ulid(entry):
                logger.warning(
                    "Dropping ingested_id entry with invalid ULID (len=%d): %r", len(entry), entry
                )
                continue
            result.append({"id": entry, "ingested_at": now_iso})
        elif isinstance(entry, dict) and "id" in entry:
            entry_id = entry.get("id", "")
            if not _is_valid_ulid(entry_id):
                entry_len = len(entry_id) if isinstance(entry_id, str) else 0
                logger.warning(
                    "Dropping ingested_id entry with invalid ULID (len=%d): %r",
                    entry_len,
                    entry_id,
                )
                continue
            result.append(entry)
    return result


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
    default_relays: list[str] = field(default_factory=lambda: list(_DEFAULT_RELAYS))
    last_checked: dict[str, str] = field(default_factory=dict)  # relay → ISO timestamp
    # {id, ingested_at, from_did?} — dedup
    ingested_ids: list[dict[str, str]] = field(default_factory=list)

    @property
    def default_relay(self) -> str:
        """Return the primary relay URL (first in the list). Backward-compat alias."""
        return self.default_relays[0] if self.default_relays else _DEFAULT_RELAYS[0]

    @default_relay.setter
    def default_relay(self, value: str) -> None:
        """Set a single relay, replacing the list. Backward-compat alias."""
        self.default_relays = [value]

    @classmethod
    def load(cls, path: Path) -> Profile:
        """Load from assistant_profile.json.

        Reads from 'aya' key; migrates 'assistant_sync' if present.
        Accepts both legacy ``default_relay`` (string) and ``default_relays`` (list).

        Validates profile structure and logs warnings for deprecated keys or malformed data.
        """
        data = json.loads(path.read_text())
        # Migrate profiles written by older versions (assistant_sync → aya)
        aya_data = data.get("aya") or data.get("assistant_sync", {})

        # Forward compatibility: warn if schema is newer than expected
        if isinstance(aya_data, dict):
            file_version = aya_data.get("schema_version", 0)
            if file_version > PROFILE_SCHEMA_VERSION:
                logger.warning(
                    "profile schema_version %d > expected %d",
                    file_version,
                    PROFILE_SCHEMA_VERSION,
                )

        # Validate aya_data is a dict before calling methods on it
        if not isinstance(aya_data, dict):
            raise ValueError(
                f"Profile 'aya' section must be a dictionary, got {type(aya_data).__name__}. "
                "Profile may be corrupted."
            )

        # Load and validate instances
        instances = {}
        instances_data = aya_data.get("instances", {})
        if not isinstance(instances_data, dict):
            raise ValueError(
                f"Profile 'instances' must be a dictionary, got {type(instances_data).__name__}"
            )
        for k, v in instances_data.items():
            if not isinstance(v, dict):
                raise ValueError(f"Instance '{k}' must be a dictionary, got {type(v).__name__}")
            # Migrate old profiles missing Nostr keys
            if "nostr_private_hex" not in v:
                nostr_secret = secrets.token_bytes(32)
                nostr_key = Secp256k1PrivateKey(nostr_secret)
                nostr_pub_xonly = nostr_key.public_key.format(compressed=True)[1:]
                v["nostr_private_hex"] = nostr_secret.hex()
                v["nostr_public_hex"] = nostr_pub_xonly.hex()
            try:
                instances[k] = _validate_instance(k, v)
            except ValueError as e:
                logger.error("Profile validation error: %s", e)
                raise

        # Load and validate trusted keys
        trusted = {}
        trusted_keys_data = aya_data.get("trusted_keys", {})
        if not isinstance(trusted_keys_data, dict):
            raise ValueError(
                f"Profile 'trusted_keys' must be a dict, got {type(trusted_keys_data).__name__}"
            )
        for k, v in trusted_keys_data.items():
            if not isinstance(v, dict):
                raise ValueError(f"Trusted key '{k}' must be a dictionary, got {type(v).__name__}")
            try:
                trusted[k] = _validate_trusted_key(k, v)
            except ValueError as e:
                logger.error("Profile validation error: %s", e)
                raise

        # Support both default_relays (list) and legacy default_relay (string).
        # Coerce a bare string to a list, strip non-string entries, fall back to
        # _DEFAULT_RELAYS if the result is empty or the key is missing entirely.
        if "default_relays" in aya_data:
            raw = aya_data["default_relays"]
            if isinstance(raw, str):
                relays: list[str] = [raw]
            else:
                relays = [u for u in raw if isinstance(u, str) and u.strip()]
            if not relays:
                relays = list(_DEFAULT_RELAYS)
        elif "default_relay" in aya_data:
            # Warn about deprecated key
            logger.warning(
                "Profile uses deprecated 'default_relay' key; prefer 'default_relays' list. "
                "This key will be removed on next save."
            )
            relays = [aya_data["default_relay"]]
        else:
            relays = list(_DEFAULT_RELAYS)

        return cls(
            alias=data.get("alias", "Ace"),
            ship_mind_name=data.get("ship_mind_name", ""),
            user_name=data.get("user_name", ""),
            instances=instances,
            trusted_keys=trusted,
            default_relays=relays,
            last_checked=aya_data.get("last_checked", {}),
            ingested_ids=_normalize_ingested_ids(aya_data.get("ingested_ids", [])),
        )

    def save(self, path: Path) -> None:
        """Write aya fields back into the profile without clobbering other keys."""
        data = json.loads(path.read_text()) if path.exists() else {}
        # Drop legacy key on first save with new format
        data.pop("assistant_sync", None)
        data.setdefault("aya", {})
        data["aya"]["schema_version"] = PROFILE_SCHEMA_VERSION
        data["aya"]["instances"] = {
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
        data["aya"]["trusted_keys"] = {
            k: {"did": v.did, "label": v.label, "nostr_pubkey": v.nostr_pubkey}
            for k, v in self.trusted_keys.items()
        }
        # Always write the list form and remove the legacy scalar key so that
        # profiles migrated from older versions don't keep a stale default_relay entry.
        data["aya"].pop("default_relay", None)
        data["aya"]["default_relays"] = self.default_relays
        data["aya"]["last_checked"] = self.last_checked
        cutoff = datetime.now(UTC) - timedelta(days=7)
        pruned: list[dict[str, str]] = []
        for entry in self.ingested_ids:
            raw_ts = entry.get("ingested_at", "")
            try:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts >= cutoff:
                    pruned.append(entry)
            except (ValueError, AttributeError):
                pass  # unparseable timestamp — treat as expired
        data["aya"]["ingested_ids"] = pruned
        path.write_text(json.dumps(data, indent=2))
        path.chmod(0o600)  # private keys live here — owner-read only

    def active_instance(self, label: str = "default") -> Identity | None:
        return self.instances.get(label) or next(iter(self.instances.values()), None)

    def is_trusted(self, did: str) -> bool:
        return did in {k.did for k in self.trusted_keys.values()}
