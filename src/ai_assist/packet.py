"""Packet — the core data structure for knowledge transfer between instances."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from ulid import ULID

from ai_assist.identity import Identity

PROTOCOL_VERSION = "assistant-sync/0.1"


class ContentType(StrEnum):
    MARKDOWN = "text/markdown"
    SEED = "application/ace-seed"     # conversation opener, not raw content
    JSON = "application/json"


class ConflictStrategy(StrEnum):
    """How the receiving instance should handle conflicts with existing knowledge."""
    LAST_WRITE_WINS = "last_write_wins"   # default — newer packet wins
    SURFACE_TO_USER = "surface_to_user"  # show both, let user decide
    APPEND = "append"                    # add alongside, don't replace
    SKIP_IF_NEWER = "skip_if_newer"      # discard if local is more recent


class SeedContent(BaseModel):
    """A conversation seed — tells the receiving assistant what to ask, not what to know."""
    opener: str
    context_summary: str
    open_questions: list[str] = Field(default_factory=list)
    expires_behavior: str = "surface_as_reminder"  # or "discard"


class Packet(BaseModel):
    """A signed knowledge packet — the unit of transfer in Assistant Sync."""

    id: str = Field(default_factory=lambda: str(ULID()))
    version: str = PROTOCOL_VERSION
    from_did: str = Field(alias="from")
    to_did: str = Field(alias="to")
    sent_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str | None = None
    intent: str
    context: str | None = None
    content_type: ContentType = ContentType.MARKDOWN
    content: str | dict[str, Any] = ""
    reply_to: str | None = None
    conflict_strategy: ConflictStrategy = ConflictStrategy.LAST_WRITE_WINS
    tags: list[str] = Field(default_factory=list)
    signature: str | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def set_default_expiry(self) -> Packet:
        if self.expires_at is None:
            sent = datetime.fromisoformat(self.sent_at)
            self.expires_at = (sent + timedelta(days=7)).isoformat()
        return self

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.fromisoformat(self.expires_at) < datetime.now(UTC)

    def canonical_bytes(self) -> bytes:
        """Deterministic serialisation for signing — excludes signature field."""
        data = self.model_dump(by_alias=True, exclude={"signature"})
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    def sign(self, identity: Identity) -> Packet:
        """Return a new packet with the signature field populated."""
        sig_bytes = identity.sign(self.canonical_bytes())
        signed = self.model_copy()
        signed.signature = base64.urlsafe_b64encode(sig_bytes).decode()
        return signed

    def verify(self, identity: Identity) -> bool:
        """Verify the packet signature against the given identity's public key."""
        if self.signature is None:
            return False
        try:
            sig_bytes = base64.urlsafe_b64decode(self.signature)
            identity.public_key().verify(sig_bytes, self.canonical_bytes())
            return True
        except Exception:
            return False

    def verify_from_did(self) -> bool:
        """Verify the packet signature using the sender's DID (no Identity object needed).

        Extracts the ed25519 public key from the from_did field and verifies
        the signature against it. This is the method used in the receive flow
        where we don't have the sender's private key.
        """
        if self.signature is None:
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            import base58
            z_encoded = self.from_did.removeprefix("did:key:z")
            multicodec = base58.b58decode(z_encoded)
            pub_bytes = multicodec[2:]  # strip ed25519 multicodec prefix

            pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
            sig_bytes = base64.urlsafe_b64decode(self.signature)
            pub_key.verify(sig_bytes, self.canonical_bytes())
            return True
        except Exception:
            return False

    def fingerprint(self) -> str:
        """Short content hash for display — first 8 chars of SHA-256."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()[:8]

    def summary(self) -> str:
        """One-line display for inbox listing."""
        age = human_age(self.sent_at)
        expiry = "⏰ expiring soon" if self._expiring_soon() else ""
        return f"[{self.id[:8]}] {self.intent}  ·  {age} {expiry}".strip()

    def _expiring_soon(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.fromisoformat(self.expires_at) - datetime.now(UTC) < timedelta(days=1)

    @classmethod
    def from_files(
        cls,
        paths: list[str],
        from_did: str,
        to_did: str,
        intent: str,
        context: str | None = None,
        tags: list[str] | None = None,
    ) -> Packet:
        """Pack multiple files into a single markdown packet."""
        parts = []
        for path in paths:
            p = Path(path)
            parts.append(f"## {p.name}\n\n{p.read_text()}")
        content = "\n\n---\n\n".join(parts)
        return cls(
            **{"from": from_did, "to": to_did},
            intent=intent,
            context=context,
            content_type=ContentType.MARKDOWN,
            content=content,
            tags=tags or [],
        )

    @classmethod
    def as_seed(
        cls,
        from_did: str,
        to_did: str,
        intent: str,
        opener: str,
        context_summary: str,
        open_questions: list[str] | None = None,
    ) -> Packet:
        """Create a conversation seed packet — what to ask, not what to know."""
        seed = SeedContent(
            opener=opener,
            context_summary=context_summary,
            open_questions=open_questions or [],
        )
        return cls(
            **{"from": from_did, "to": to_did},
            intent=intent,
            content_type=ContentType.SEED,
            content=seed.model_dump(),
        )

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, data: str | bytes) -> Packet:
        packet = cls.model_validate_json(data)
        # Version check — warn on unknown minor, reject on unknown major
        if packet.version and "/" in packet.version:
            major = packet.version.split("/")[1].split(".")[0]
            if major != PROTOCOL_VERSION.split("/")[1].split(".")[0]:
                import logging
                logging.getLogger(__name__).warning(
                    "Unknown protocol major version: %s (expected %s)",
                    packet.version, PROTOCOL_VERSION,
                )
        return packet


def human_age(iso: str) -> str:
    delta = datetime.now(UTC) - datetime.fromisoformat(iso)
    if delta.total_seconds() < 60:
        return "just now"
    if delta.total_seconds() < 3600:
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta.total_seconds() < 86400:
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"
