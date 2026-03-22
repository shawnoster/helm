"""Tests for packet creation, signing, and verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_assist.identity import Identity
from ai_assist.packet import ConflictStrategy, ContentType, Packet


@pytest.fixture
def work_identity() -> Identity:
    return Identity.generate("work")


@pytest.fixture
def home_identity() -> Identity:
    return Identity.generate("home")


@pytest.fixture
def basic_packet(work_identity: Identity, home_identity: Identity) -> Packet:
    return Packet(
        **{"from": work_identity.did, "to": home_identity.did},
        intent="Pack for home — dinner party notes",
        content="Guest list: 12 people. Decision: open seating.",
    )


class TestPacketCreation:
    def test_id_is_generated(self, basic_packet: Packet) -> None:
        assert basic_packet.id
        assert len(basic_packet.id) == 26  # ULID length

    def test_default_expiry_is_seven_days(self, basic_packet: Packet) -> None:
        sent = datetime.fromisoformat(basic_packet.sent_at)
        expires = datetime.fromisoformat(basic_packet.expires_at)
        assert timedelta(days=6) < (expires - sent) <= timedelta(days=7)

    def test_default_content_type(self, basic_packet: Packet) -> None:
        assert basic_packet.content_type == ContentType.MARKDOWN

    def test_default_conflict_strategy(self, basic_packet: Packet) -> None:
        assert basic_packet.conflict_strategy == ConflictStrategy.LAST_WRITE_WINS

    def test_not_expired(self, basic_packet: Packet) -> None:
        assert not basic_packet.is_expired()

    def test_expired_packet(self, work_identity: Identity, home_identity: Identity) -> None:
        past = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        packet = Packet(
            **{"from": work_identity.did, "to": home_identity.did},
            intent="Old news",
            content="...",
            expires_at=past,
        )
        assert packet.is_expired()

    def test_fingerprint_is_deterministic(self, basic_packet: Packet) -> None:
        assert basic_packet.fingerprint() == basic_packet.fingerprint()
        assert len(basic_packet.fingerprint()) == 8


class TestPacketSigning:
    def test_sign_and_verify(
        self, basic_packet: Packet, work_identity: Identity
    ) -> None:
        signed = basic_packet.sign(work_identity)
        assert signed.signature is not None
        assert signed.verify(work_identity)

    def test_wrong_key_fails_verification(
        self, basic_packet: Packet, work_identity: Identity, home_identity: Identity
    ) -> None:
        signed = basic_packet.sign(work_identity)
        assert not signed.verify(home_identity)

    def test_unsigned_fails_verification(
        self, basic_packet: Packet, work_identity: Identity
    ) -> None:
        assert not basic_packet.verify(work_identity)

    def test_tampered_content_fails_verification(
        self, basic_packet: Packet, work_identity: Identity
    ) -> None:
        signed = basic_packet.sign(work_identity)
        tampered = signed.model_copy(update={"content": "TAMPERED"})
        assert not tampered.verify(work_identity)

    def test_canonical_bytes_excludes_signature(
        self, basic_packet: Packet, work_identity: Identity
    ) -> None:
        signed = basic_packet.sign(work_identity)
        # canonical bytes should be identical before and after signing
        assert basic_packet.canonical_bytes() == signed.model_copy(
            update={"signature": None}
        ).canonical_bytes()

    def test_verify_from_did(
        self, basic_packet: Packet, work_identity: Identity
    ) -> None:
        signed = basic_packet.sign(work_identity)
        assert signed.verify_from_did()

    def test_verify_from_did_unsigned(self, basic_packet: Packet) -> None:
        assert not basic_packet.verify_from_did()

    def test_verify_from_did_tampered(
        self, basic_packet: Packet, work_identity: Identity
    ) -> None:
        signed = basic_packet.sign(work_identity)
        tampered = signed.model_copy(update={"content": "TAMPERED"})
        assert not tampered.verify_from_did()

    def test_verify_from_did_wrong_sender(
        self, basic_packet: Packet, work_identity: Identity, home_identity: Identity
    ) -> None:
        # Sign with work key but claim to be from home
        signed = basic_packet.sign(work_identity)
        forged = signed.model_copy(update={"from_did": home_identity.did})
        assert not forged.verify_from_did()


class TestPacketSerialisation:
    def test_round_trip_json(self, basic_packet: Packet, work_identity: Identity) -> None:
        signed = basic_packet.sign(work_identity)
        restored = Packet.from_json(signed.to_json())
        assert restored.id == signed.id
        assert restored.intent == signed.intent
        assert restored.signature == signed.signature

    def test_from_files(
        self, tmp_path: pytest.TempPathFactory, work_identity: Identity, home_identity: Identity
    ) -> None:
        f = tmp_path / "notes.md"
        f.write_text("# Notes\n\nSome content.")
        packet = Packet.from_files(
            paths=[str(f)],
            from_did=work_identity.did,
            to_did=home_identity.did,
            intent="Test pack",
        )
        assert "notes.md" in packet.content
        assert "Some content." in packet.content

    def test_seed_packet(self, work_identity: Identity, home_identity: Identity) -> None:
        packet = Packet.as_seed(
            from_did=work_identity.did,
            to_did=home_identity.did,
            intent="Resume dinner party planning",
            opener="Have you decided on 8 or 12 guests?",
            context_summary="Discussed last Tuesday, leaning toward 12.",
            open_questions=["Guest count", "Venue"],
        )
        assert packet.content_type == ContentType.SEED
        assert isinstance(packet.content, dict)
        assert packet.content["opener"] == "Have you decided on 8 or 12 guests?"


class TestPacketSummary:
    def test_summary_contains_intent(self, basic_packet: Packet) -> None:
        assert "dinner party" in basic_packet.summary()

    def test_summary_contains_short_id(self, basic_packet: Packet) -> None:
        assert basic_packet.id[:8] in basic_packet.summary()
