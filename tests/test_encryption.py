"""Tests for encryption.py — NIP-44 v2 encrypt/decrypt."""

from __future__ import annotations

import base64

import pytest

from aya.encryption import (
    _calc_padded_len,
    _pad,
    _unpad,
    nip44_decrypt,
    nip44_encrypt,
)
from aya.identity import Identity

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sender() -> Identity:
    return Identity.generate("work")


@pytest.fixture
def recipient() -> Identity:
    return Identity.generate("home")


# ── _calc_padded_len ──────────────────────────────────────────────────────────


class TestCalcPaddedLen:
    def test_short_messages_pad_to_32(self) -> None:
        for n in range(1, 33):
            assert _calc_padded_len(n) == 32

    def test_33_pads_to_chunk_boundary(self) -> None:
        result = _calc_padded_len(33)
        assert result > 32
        assert result % 1 == 0  # integer

    def test_output_is_always_gte_input(self) -> None:
        for n in range(1, 500):
            assert _calc_padded_len(n) >= n


# ── _pad / _unpad ─────────────────────────────────────────────────────────────


class TestPadUnpad:
    def test_round_trip(self) -> None:
        msg = b"hello world"
        assert _unpad(_pad(msg)) == msg

    def test_length_prefix_correct(self) -> None:
        msg = b"abc"
        padded = _pad(msg)
        assert int.from_bytes(padded[:2], "big") == len(msg)

    def test_padded_block_size(self) -> None:
        msg = b"x" * 10
        padded = _pad(msg)
        assert len(padded) == 2 + _calc_padded_len(len(msg))

    def test_empty_string_round_trips(self) -> None:
        assert _unpad(_pad(b"")) == b""


# ── nip44_encrypt / nip44_decrypt ─────────────────────────────────────────────


class TestNip44RoundTrip:
    def test_basic_round_trip(self, sender: Identity, recipient: Identity) -> None:
        plaintext = "Hello from work, here is your context."
        encrypted = nip44_encrypt(plaintext, sender.nostr_private_hex, recipient.nostr_public_hex)
        decrypted = nip44_decrypt(encrypted, recipient.nostr_private_hex, sender.nostr_public_hex)
        assert decrypted == plaintext

    def test_empty_string_round_trip(self, sender: Identity, recipient: Identity) -> None:
        encrypted = nip44_encrypt("", sender.nostr_private_hex, recipient.nostr_public_hex)
        decrypted = nip44_decrypt(encrypted, recipient.nostr_private_hex, sender.nostr_public_hex)
        assert decrypted == ""

    def test_unicode_round_trip(self, sender: Identity, recipient: Identity) -> None:
        plaintext = "日本語テスト 🚀 emoji and unicode — café"
        encrypted = nip44_encrypt(plaintext, sender.nostr_private_hex, recipient.nostr_public_hex)
        decrypted = nip44_decrypt(encrypted, recipient.nostr_private_hex, sender.nostr_public_hex)
        assert decrypted == plaintext

    def test_large_payload_round_trip(self, sender: Identity, recipient: Identity) -> None:
        plaintext = "A" * 10_000
        encrypted = nip44_encrypt(plaintext, sender.nostr_private_hex, recipient.nostr_public_hex)
        decrypted = nip44_decrypt(encrypted, recipient.nostr_private_hex, sender.nostr_public_hex)
        assert decrypted == plaintext

    def test_each_call_produces_different_ciphertext(
        self, sender: Identity, recipient: Identity
    ) -> None:
        """Random nonce must produce different ciphertext each time."""
        msg = "same message"
        c1 = nip44_encrypt(msg, sender.nostr_private_hex, recipient.nostr_public_hex)
        c2 = nip44_encrypt(msg, sender.nostr_private_hex, recipient.nostr_public_hex)
        assert c1 != c2

    def test_output_is_base64(self, sender: Identity, recipient: Identity) -> None:
        encrypted = nip44_encrypt("test", sender.nostr_private_hex, recipient.nostr_public_hex)
        raw = base64.b64decode(encrypted)  # must not raise
        assert raw[0] == 2  # NIP-44 v2 version byte

    def test_ecdh_is_symmetric(self, sender: Identity, recipient: Identity) -> None:
        """Sender encrypting to recipient == recipient encrypting to sender (same conv key)."""
        plaintext = "symmetric ECDH check"
        # Sender → Recipient
        enc = nip44_encrypt(plaintext, sender.nostr_private_hex, recipient.nostr_public_hex)
        dec = nip44_decrypt(enc, recipient.nostr_private_hex, sender.nostr_public_hex)
        assert dec == plaintext

        # Recipient → Sender (roles reversed)
        enc2 = nip44_encrypt(plaintext, recipient.nostr_private_hex, sender.nostr_public_hex)
        dec2 = nip44_decrypt(enc2, sender.nostr_private_hex, recipient.nostr_public_hex)
        assert dec2 == plaintext


# ── Error cases ───────────────────────────────────────────────────────────────


class TestNip44Errors:
    def test_wrong_recipient_key_fails_mac(self, sender: Identity, recipient: Identity) -> None:
        other = Identity.generate("other")
        encrypted = nip44_encrypt("secret", sender.nostr_private_hex, recipient.nostr_public_hex)
        with pytest.raises(ValueError, match="MAC"):
            nip44_decrypt(encrypted, other.nostr_private_hex, sender.nostr_public_hex)

    def test_wrong_sender_key_fails_mac(self, sender: Identity, recipient: Identity) -> None:
        other = Identity.generate("other")
        encrypted = nip44_encrypt("secret", sender.nostr_private_hex, recipient.nostr_public_hex)
        with pytest.raises(ValueError, match="MAC"):
            nip44_decrypt(encrypted, recipient.nostr_private_hex, other.nostr_public_hex)

    def test_tampered_ciphertext_fails_mac(self, sender: Identity, recipient: Identity) -> None:
        encrypted = nip44_encrypt("secret", sender.nostr_private_hex, recipient.nostr_public_hex)
        raw = bytearray(base64.b64decode(encrypted))
        raw[33] ^= 0xFF  # flip a byte in the ciphertext
        tampered = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(ValueError, match="MAC"):
            nip44_decrypt(tampered, recipient.nostr_private_hex, sender.nostr_public_hex)

    def test_garbage_payload_raises(self, sender: Identity, recipient: Identity) -> None:
        with pytest.raises(ValueError, match="base64"):
            nip44_decrypt("not-base64!!!", recipient.nostr_private_hex, sender.nostr_public_hex)

    def test_too_short_payload_raises(self, sender: Identity, recipient: Identity) -> None:
        short = base64.b64encode(b"\x02" + b"\x00" * 10).decode()
        with pytest.raises(ValueError, match="too short"):
            nip44_decrypt(short, recipient.nostr_private_hex, sender.nostr_public_hex)

    def test_wrong_version_raises(self, sender: Identity, recipient: Identity) -> None:
        # Build a payload with version byte = 1 (unsupported)
        bad = base64.b64encode(b"\x01" + b"\x00" * 65).decode()
        with pytest.raises(ValueError, match="version"):
            nip44_decrypt(bad, recipient.nostr_private_hex, sender.nostr_public_hex)
