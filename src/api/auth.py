"""Telegram Mini App ``initData`` verification.

This is the only thing between the API and the open internet: the mini-app runs
in the user's browser, so anything it sends can be forged. Telegram signs the
launch payload with a key derived from the bot token, and that signature is what
proves a request came from a real Telegram client acting for a real user.

Distinct from ``markets/mrkt/auth.py``, which *obtains* an initData blob in order
to authenticate against MRKT. This module *verifies* one that arrived from our own
mini-app. Opposite directions -- conflating them would mean trusting a blob nobody
checked.

Reference: core.telegram.org/bots/webapps, "Validating data received via the Mini
App".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from src.core import config

log = logging.getLogger(__name__)


class InitDataError(Exception):
    """initData was missing, malformed, expired, or not signed by Telegram."""


@dataclass(slots=True)
class InitData:
    """The verified contents of a launch payload.

    Only the fields the application uses are named. ``raw`` is kept for
    diagnostics, but nothing downstream should reach into it: a value that has not
    been named here has not been considered.
    """

    tg_id: int
    username: str | None
    first_name: str | None
    auth_date: int
    query_id: str | None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.auth_date


def _secret_key(bot_token: str) -> bytes:
    """Derive Telegram's signing key.

    Note the argument order: the literal ``"WebAppData"`` is the *key* and the bot
    token is the *message*. Reversing them produces a plausible digest that never
    matches, which is the most common way this check is written wrong.
    """
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def _data_check_string(fields: dict[str, str]) -> str:
    """Build the string Telegram signed.

    Every field except ``hash`` participates, sorted by key and joined with
    newlines. ``signature`` is excluded too: it belongs to the newer Ed25519
    third-party flow and is not part of the HMAC payload.
    """
    return "\n".join(
        f"{k}={fields[k]}" for k in sorted(fields) if k not in ("hash", "signature")
    )


def verify_init_data(
    init_data: str,
    *,
    bot_token: str | None = None,
    max_age_seconds: int | None = None,
) -> InitData:
    """Verify a raw initData query string and return its contents.

    Raises ``InitDataError`` on any failure. Callers must map every failure to the
    same response -- a bare 401 -- because telling "bad signature" apart from
    "expired" tells an attacker which half to keep working on.
    """
    if not init_data:
        raise InitDataError("initData is empty")

    token = bot_token or config.BOT_TOKEN
    if not token:
        # Without a token every signature fails to match, which reads in the logs
        # as "nobody can log in" rather than as a configuration mistake.
        raise InitDataError("BOT_TOKEN is not configured")

    # keep_blank_values: Telegram sends empty fields, and dropping them changes
    # the data-check string and therefore the digest.
    fields = dict(parse_qsl(init_data, keep_blank_values=True))

    received = fields.get("hash")
    if not received:
        raise InitDataError("initData has no hash")

    expected = hmac.new(
        _secret_key(token), _data_check_string(fields).encode(), hashlib.sha256
    ).hexdigest()

    # Constant-time: a plain == leaks how much of a forged digest was right, which
    # is enough to derive the rest.
    if not hmac.compare_digest(expected, received):
        raise InitDataError("signature mismatch")

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("auth_date is not an integer") from None
    if auth_date <= 0:
        raise InitDataError("auth_date is missing")

    max_age = (
        config.INITDATA_MAX_AGE_SEC if max_age_seconds is None else max_age_seconds
    )
    age = time.time() - auth_date
    # Expiry is what stops a captured payload being replayed forever: signatures
    # do not expire by themselves, so a blob copied out of a browser once would
    # otherwise work indefinitely.
    if max_age > 0 and age > max_age:
        raise InitDataError(f"initData expired ({int(age)}s old)")
    # Small clock skew is normal; a payload dated well into the future is not, and
    # would sail past the expiry check.
    if age < -300:
        raise InitDataError("auth_date is in the future")

    user_raw = fields.get("user")
    if not user_raw:
        # Absent when the app is launched from an inline context. Every endpoint
        # here is per-user, so there is nothing to serve without it.
        raise InitDataError("initData has no user")

    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        raise InitDataError("user is not valid JSON") from None

    tg_id = user.get("id")
    # isinstance(True, int) is True in Python, so bool is excluded explicitly --
    # a payload with "id": true must not become user 1.
    if not isinstance(tg_id, int) or isinstance(tg_id, bool):
        raise InitDataError("user.id is missing or not an integer")

    return InitData(
        tg_id=tg_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
        auth_date=auth_date,
        query_id=fields.get("query_id"),
        raw=fields,
    )
