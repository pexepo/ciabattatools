"""Subscription keys.

100 permanent keys, plus one reserved for the owner. Keys are generated once and
seeded into the database; there is no self-service purchase flow, because the
product is sold by hand -- the bot points buyers at a contact.

Two design choices worth stating:

* Keys are checked against the database, never against a pattern. A
  pattern-checkable key would let anyone mint their own by reading this file, so
  ``is_valid_format`` is a cheap pre-filter and nothing more.
* The alphabet excludes look-alike characters (0/O, 1/I/L, U/V). These keys get
  typed by hand into a chat, and a key that fails because of a misread glyph
  reads as "I was scammed" to the person who paid. Excluding them at generation
  is the fix; guessing what a user meant afterwards is not, so no character
  substitution happens on input.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

# Crockford-style: no 0, O, 1, I, L, U.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
GROUPS = 3
GROUP_LEN = 4
PREFIX = "CIAB"

# The owner's key, as specified. Seeded like any other key, never regenerated.
OWNER_KEY = "PEXEPO"

TOTAL_KEYS = 100

_SHAPE = re.compile(rf"^{PREFIX}(?:-[{ALPHABET}]{{{GROUP_LEN}}}){{{GROUPS}}}$")


def normalize(raw: str) -> str:
    """Fold a user-typed key into canonical form.

    Users paste keys with stray spaces, lowercase, and missing or extra dashes.
    Rejecting those is a support burden with no security benefit, so case and
    separators are normalised and the result is looked up.

    Characters outside the alphabet are left alone rather than "corrected": since
    they are never generated, their presence is a genuine typo, and silently
    rewriting one would turn a clear failure into a confusing one.
    """
    text = re.sub(r"[\s\-_]+", "", (raw or "").strip().upper())
    if text == OWNER_KEY:
        return OWNER_KEY
    body = text[len(PREFIX) :] if text.startswith(PREFIX) else text
    if len(body) != GROUPS * GROUP_LEN:
        # Returned cleaned anyway; the caller reports "not found" rather than
        # "malformed", so a wrong key and a typo look identical to anyone
        # probing for valid shapes.
        return f"{PREFIX}-{body}" if body else text
    chunks = [body[i : i + GROUP_LEN] for i in range(0, len(body), GROUP_LEN)]
    return "-".join([PREFIX, *chunks])


def is_valid_format(key: str) -> bool:
    """Whether a key could exist. Says nothing about whether it does."""
    return key == OWNER_KEY or bool(_SHAPE.match(key))


def generate_keys(count: int = TOTAL_KEYS, *, include_owner: bool = True) -> list[str]:
    """Mint keys with a CSPRNG.

    ``secrets`` rather than ``random``: these are bearer credentials, and a
    predictable sequence would let one buyer derive the other 99.

    Non-deterministic by design, so each call returns a different set. That makes
    it the wrong function for seeding a database that already holds sold keys --
    use :func:`derive_keys` for that. This one is for minting a fresh batch and
    recording the result somewhere.
    """
    keys: list[str] = [OWNER_KEY] if include_owner else []
    seen = set(keys)
    target = count + (1 if include_owner else 0)
    while len(keys) < target:
        body = "".join(secrets.choice(ALPHABET) for _ in range(GROUPS * GROUP_LEN))
        chunks = [body[i : i + GROUP_LEN] for i in range(0, len(body), GROUP_LEN)]
        key = "-".join([PREFIX, *chunks])
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def derive_keys(
    secret: str, count: int = TOTAL_KEYS, *, include_owner: bool = True
) -> list[str]:
    """Derive the same key set every time, from a secret.

    This is what the bot seeds with. ``generate_keys`` produces a fresh random set
    on every call, so seeding with it meant a restart minted 100 *new* keys and
    left the old ones in the table: a key sold yesterday still validated, but the
    total grew by 100 each boot, and a wiped database invalidated every key ever
    sold. Deriving from ``CIABATTA_SECRET_KEY`` fixes both -- the same secret
    always yields the same 100 keys, and rebuilding the database restores them.

    Unpredictable without the secret: each key is HMAC-SHA256 over the secret and
    an index, so knowing 99 keys does not reveal the hundredth. The secret must
    therefore be treated as the master credential it is -- rotating it silently
    invalidates every key in circulation.
    """
    if not secret:
        raise ValueError("cannot derive licence keys without a secret")

    keys: list[str] = [OWNER_KEY] if include_owner else []
    seen = set(keys)
    index = 0
    while len(keys) < count + (1 if include_owner else 0):
        digest = hmac.new(
            secret.encode("utf-8"), f"licence:{index}".encode(), hashlib.sha256
        ).digest()
        # Rejection-free mapping: each byte is reduced modulo the alphabet, which
        # skews the distribution by under 1% for a 30-character alphabet -- far
        # too little to help an attacker who does not have the secret.
        body = "".join(ALPHABET[b % len(ALPHABET)] for b in digest[: GROUPS * GROUP_LEN])
        chunks = [body[i : i + GROUP_LEN] for i in range(0, len(body), GROUP_LEN)]
        key = "-".join([PREFIX, *chunks])
        index += 1
        # A collision would silently shorten the set below 100.
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def fingerprint(key: str) -> str:
    """Short non-reversible tag for logs.

    Lets an operator correlate "key X activated" across log lines without the log
    becoming a list of valid keys.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def keys_match(a: str, b: str) -> bool:
    """Constant-time comparison.

    Overkill for 100 keys, but the cost is nil and a timing side channel on a
    bearer credential is an awkward thing to explain.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
