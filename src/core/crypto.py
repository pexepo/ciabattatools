"""Symmetric encryption for credential material.

What lands here is a Telegram session string and third-party API keys. A session
string is not "sensitive data" in the abstract -- it is full control of a real
person's Telegram account, so the threat model is a stolen database dump.

AES-256-GCM: authenticated, so a tampered ciphertext fails loudly instead of
decrypting to garbage that then gets handed to Telegram as a session. A random
96-bit nonce is generated per encryption and stored alongside the ciphertext,
because reusing a nonce with GCM leaks the key stream.

``cryptography`` is imported lazily and a hand-rolled fallback is refused: there
is no safe way to improvise AES here, so a missing dependency is an error at
startup rather than a silent downgrade to something weaker.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets

NONCE_BYTES = 12  # 96 bits, the GCM standard
KEY_BYTES = 32  # AES-256


class CryptoError(RuntimeError):
    """Raised when a value cannot be encrypted or verified."""


def _load_aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CryptoError(
            "the 'cryptography' package is required to encrypt sessions. "
            "Install it (pip install cryptography) -- there is no fallback, "
            "because storing sessions unencrypted is not an acceptable default."
        ) from exc
    return AESGCM


def derive_key(material: str) -> bytes:
    """Turn the configured secret into exactly 32 bytes.

    Accepts base64, hex, or an arbitrary passphrase. A passphrase is hashed
    rather than truncated so a short secret still fills the key space -- though a
    short secret is still a weak secret, which is why generate_key() exists and
    the deploy guide tells operators to use it.
    """
    text = (material or "").strip()
    if not text:
        raise CryptoError(
            "CIABATTA_SECRET_KEY is empty. Generate one with:\n"
            "  python -c \"import secrets,base64;"
            'print(base64.b64encode(secrets.token_bytes(32)).decode())"'
        )

    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            raw = decoder(text)
        except Exception:  # noqa: BLE001 - both raise several types
            continue
        if len(raw) == KEY_BYTES:
            return raw

    # Not a 32-byte encoded key, so treat it as a passphrase.
    return hashlib.sha256(text.encode("utf-8")).digest()


def generate_key() -> str:
    """A fresh base64 key, for the deploy guide and first-run setup."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


class SecretBox:
    """Encrypts and decrypts strings with one key.

    Instances hold key material, so ``repr`` is overridden: a default repr in a
    traceback or a log line would print the key.
    """

    __slots__ = ("_key", "_aesgcm")

    def __init__(self, key_material: str):
        self._key = derive_key(key_material)
        self._aesgcm = _load_aesgcm()(self._key)

    def __repr__(self) -> str:
        return "<SecretBox key=hidden>"

    def encrypt(self, plaintext: str, *, context: str = "") -> str:
        """Encrypt to base64 ``nonce || ciphertext``.

        ``context`` is bound as additional authenticated data, so a session blob
        cannot be lifted out of one row and replayed in another: moving a
        ciphertext between users makes decryption fail rather than succeed with
        someone else's account.
        """
        if not isinstance(plaintext, str):
            raise CryptoError("encrypt() takes a string")
        nonce = os.urandom(NONCE_BYTES)
        aad = context.encode("utf-8") if context else None
        ct = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, blob: str, *, context: str = "") -> str:
        """Decrypt a value produced by ``encrypt``.

        Raises on tampering, on the wrong key, and on a context mismatch. It
        never returns partial or best-effort output: a corrupted session must not
        reach Telegram.
        """
        if not isinstance(blob, str) or not blob:
            raise CryptoError("decrypt() takes a non-empty string")
        try:
            raw = base64.b64decode(blob)
        except Exception as exc:  # noqa: BLE001
            raise CryptoError("stored value is not valid base64") from exc
        if len(raw) <= NONCE_BYTES:
            raise CryptoError("stored value is too short to contain a nonce")
        nonce, ct = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        aad = context.encode("utf-8") if context else None
        try:
            return self._aesgcm.decrypt(nonce, ct, aad).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - InvalidTag and friends
            raise CryptoError(
                "could not decrypt: wrong CIABATTA_SECRET_KEY, wrong context, "
                "or the value was tampered with"
            ) from exc


_box: SecretBox | None = None


def secret_box() -> SecretBox:
    """Process-wide box built from config, on first use rather than at import.

    Deferred so that importing this module -- which the tests and tooling do --
    does not require a configured secret.
    """
    global _box
    if _box is None:
        from src.core import config

        _box = SecretBox(config.SECRET_KEY)
    return _box


def mask(value: str, *, keep: int = 4) -> str:
    """Render a secret for human eyes without disclosing it.

    Used in the bot and the mini app so a user can confirm *which* key is stored
    without the key itself appearing in a chat log or a screenshot.
    """
    text = value or ""
    if len(text) <= keep:
        return "•" * len(text)
    return "•" * (len(text) - keep) + text[-keep:]
