"""NIP-44 v2 packet encryption — secp256k1 ECDH + ChaCha20 + HMAC-SHA256.

Spec: https://github.com/nostr-protocol/nips/blob/master/44.md
"""

from __future__ import annotations

import base64
import hmac as _hmac
import logging
import os

from coincurve import PrivateKey as Secp256k1PrivateKey
from coincurve import PublicKey as Secp256k1PublicKey
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

_NIP44_VERSION = 2
_NONCE_LEN = 32
_MAC_LEN = 32
_HKDF_LEN = 76
_HKDF_INFO = b"nip44-v2"


def _get_conversation_key(priv_hex: str, pub_xonly_hex: str) -> bytes:
    """Derive shared secret via secp256k1 ECDH — x-coordinate of shared point."""
    if len(priv_hex) != 64 or len(pub_xonly_hex) != 64:
        raise ValueError(
            f"Expected 32-byte hex keys; "
            f"got priv={len(priv_hex) // 2}B pub={len(pub_xonly_hex) // 2}B"
        )
    priv = Secp256k1PrivateKey(bytes.fromhex(priv_hex))
    # Nostr pubkeys are 32-byte x-only; prefix 0x02 (even y) to form a valid compressed key
    pub = Secp256k1PublicKey(b"\x02" + bytes.fromhex(pub_xonly_hex))
    shared = pub.multiply(priv.secret)
    return bytes(shared.format(compressed=True))[1:]  # x-coordinate only (32 bytes)


def _derive_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    """HKDF-SHA256(conversation_key, salt=nonce, info="nip44-v2", len=76).

    Returns (chacha_key, chacha_nonce, hmac_key).
    """
    keys = HKDF(
        algorithm=hashes.SHA256(),
        length=_HKDF_LEN,
        salt=nonce,
        info=_HKDF_INFO,
    ).derive(conversation_key)
    return keys[:32], keys[32:44], keys[44:76]


def _calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (unpadded_len - 1).bit_length()
    chunk = next_power // 8
    if chunk <= 1:
        return 32
    return chunk * (1 + (unpadded_len - 1) // chunk)


def _pad(plaintext: bytes) -> bytes:
    """Prepend 2-byte BE message length, then zero-pad to the next NIP-44 boundary."""
    unpadded_len = len(plaintext)
    if unpadded_len > 0xFFFF:
        raise ValueError(f"NIP-44 plaintext too long: {unpadded_len} bytes (max 65535)")
    padded_len = _calc_padded_len(unpadded_len)
    return unpadded_len.to_bytes(2, "big") + plaintext + b"\x00" * (padded_len - unpadded_len)


def _unpad(padded: bytes) -> bytes:
    if len(padded) < 2:
        raise ValueError("NIP-44 padded block too short to contain length prefix")
    msg_len = int.from_bytes(padded[:2], "big")
    if 2 + msg_len > len(padded):
        raise ValueError(
            f"NIP-44 length prefix ({msg_len}) exceeds available data ({len(padded) - 2} bytes)"
        )
    return padded[2 : 2 + msg_len]


def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    """ChaCha20 stream cipher (counter=0).

    cryptography's ChaCha20 expects a 16-byte nonce: 4-byte LE counter || 12-byte nonce.
    """
    nonce16 = (0).to_bytes(4, "little") + nonce12
    cipher = Cipher(algorithms.ChaCha20(key, nonce16), mode=None)
    ctx = cipher.encryptor()
    return ctx.update(data) + ctx.finalize()


def nip44_encrypt(plaintext: str, sender_priv_hex: str, recipient_pub_xonly_hex: str) -> str:
    """Encrypt *plaintext* for *recipient_pub_xonly_hex* using NIP-44 v2.

    Returns a base64-encoded payload: version(1) || nonce(32) || ciphertext || mac(32).
    """
    conversation_key = _get_conversation_key(sender_priv_hex, recipient_pub_xonly_hex)
    nonce = os.urandom(_NONCE_LEN)
    chacha_key, chacha_nonce, hmac_key = _derive_keys(conversation_key, nonce)

    padded = _pad(plaintext.encode("utf-8"))
    ciphertext = _chacha20(chacha_key, chacha_nonce, padded)
    mac = _hmac.digest(hmac_key, nonce + ciphertext, "sha256")

    payload = bytes([_NIP44_VERSION]) + nonce + ciphertext + mac
    return base64.b64encode(payload).decode("ascii")


def nip44_decrypt(payload: str, recipient_priv_hex: str, sender_pub_xonly_hex: str) -> str:
    """Decrypt a NIP-44 v2 *payload* (base64-encoded).

    Raises *ValueError* on version mismatch, MAC failure, or malformed payload.
    """
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid NIP-44 payload (base64 decode failed): {exc}") from exc

    min_len = 1 + _NONCE_LEN + _MAC_LEN
    if len(raw) < min_len:
        raise ValueError(f"NIP-44 payload too short: {len(raw)} < {min_len}")
    if raw[0] != _NIP44_VERSION:
        raise ValueError(f"Unsupported NIP-44 version: {raw[0]}")

    nonce = raw[1 : 1 + _NONCE_LEN]
    ciphertext = raw[1 + _NONCE_LEN : -_MAC_LEN]
    mac = raw[-_MAC_LEN:]

    conversation_key = _get_conversation_key(recipient_priv_hex, sender_pub_xonly_hex)
    chacha_key, chacha_nonce, hmac_key = _derive_keys(conversation_key, nonce)

    expected_mac = _hmac.digest(hmac_key, nonce + ciphertext, "sha256")
    if not _hmac.compare_digest(mac, expected_mac):
        raise ValueError("NIP-44 MAC verification failed")

    padded = _chacha20(chacha_key, chacha_nonce, ciphertext)
    return _unpad(padded).decode("utf-8")
