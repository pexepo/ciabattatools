"""MRKT authentication.

MRKT authenticates a Telegram mini-app ``initData`` blob, which only a *user*
account can mint -- a bot token cannot. So the flow is:

    Telethon user session -> RequestAppWebView(bot='mrkt', app='app')
        -> tgWebAppData -> POST /auth -> token

The resulting token is account-level credential material, equivalent to a
password. It is never logged, never echoed into a bot message, and never
committed; ``repr`` deliberately omits it.

Telethon is used rather than Pyrogram because the gift tools already require
layer 227, and carrying two MTProto stacks doubles the session-file and
API-drift surface for no gain.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Awaitable, Callable

from src.core import config

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Raised when a token cannot be obtained. Never carries the token."""


def extract_init_data(url: str) -> str:
    """Pull ``tgWebAppData`` out of a mini-app URL.

    The payload normally lives in the fragment, but it has been observed in the
    query string too, so both are checked before giving up. The value is itself
    percent-encoded and is passed on verbatim -- re-encoding it breaks the HMAC
    that MRKT verifies.
    """
    parsed = urllib.parse.urlparse(url)
    for part in (parsed.fragment, parsed.query):
        if not part:
            continue
        values = urllib.parse.parse_qs(part).get("tgWebAppData")
        if values and values[0]:
            return values[0]
    raise AuthError("mini-app URL carried no tgWebAppData")


class MrktAuth:
    """Owns the MRKT token and its refresh.

    A single in-flight refresh is shared by all callers: a burst of 401s from
    concurrent requests must not produce a burst of login attempts, because each
    login drives a real MTProto call on the user's account.
    """

    def __init__(
        self,
        session,
        init_data_source: Callable[[], Awaitable[str]] | None = None,
        static_token: str | None = None,
    ):
        """
        Args:
            session: an HTTP session exposing ``post`` (curl_cffi AsyncSession).
            init_data_source: coroutine returning a fresh initData blob. Injected
                so the MTProto dependency stays out of the transport layer, and
                so tests can drive this without a Telegram account.
            static_token: hand-pasted token. Cannot be refreshed, so an expiry is
                reported plainly instead of looping on 401.
        """
        self._session = session
        self._init_data_source = init_data_source
        token = (
            static_token if static_token is not None else config.MRKT_STATIC_TOKEN
        ) or ""
        self._token: str | None = token or None
        self._static = bool(token)
        self._refresh_lock = asyncio.Lock()

    def __repr__(self) -> str:
        state = "set" if self._token else "unset"
        return f"<MrktAuth token={state} static={self._static}>"

    async def token(self, force_refresh: bool = False) -> str:
        if self._token and not force_refresh:
            return self._token
        async with self._refresh_lock:
            # Another caller may have refreshed while we waited on the lock.
            if self._token and not force_refresh:
                return self._token
            if force_refresh and self._static:
                raise AuthError(
                    "MRKT_TOKEN from the environment expired. Paste a fresh one, "
                    "or connect a Telegram session for automatic refresh. "
                    "Also make sure the MRKT account has enough TON balance "
                    "for the operation and fee."
                )
            self._token = await self._login()
            return self._token

    def invalidate(self) -> None:
        if not self._static:
            self._token = None

    async def _login(self) -> str:
        if self._init_data_source is None:
            raise AuthError(
                "no Telegram session and no MRKT_TOKEN: cannot authenticate. "
                "Log in through the bot first and make sure the MRKT account "
                "has enough TON balance for the operation and fee."
            )
        init_data = await self._init_data_source()
        response = await self._session.post(
            f"{config.MRKT_API}/auth",
            json={"data": init_data},
            headers={"Referer": config.MRKT_CDN},
            timeout=config.REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            # The body may echo the blob, so it is not logged.
            raise AuthError(f"MRKT /auth returned HTTP {response.status_code}")
        try:
            payload = response.json() or {}
        except Exception as exc:  # noqa: BLE001 - json() raises several types
            raise AuthError("MRKT /auth response was not JSON") from exc
        token = payload.get("token")
        if not token or not isinstance(token, str):
            raise AuthError("MRKT /auth returned no token")
        log.info("MRKT token obtained")
        return token


async def init_data_via_telethon(client) -> str:
    """Mint a fresh initData blob for MRKT from a logged-in Telethon client.

    Passed to ``MrktAuth`` as ``init_data_source``. Telethon is imported lazily
    so that loading the transport layer does not require an MTProto stack -- the
    API process and the tests both do exactly that.
    """
    from telethon.tl.functions.messages import RequestAppWebViewRequest
    from telethon.tl.types import InputBotAppShortName

    bot = await client.get_input_entity(config.MRKT_BOT)
    result = await client(
        RequestAppWebViewRequest(
            peer=bot,
            app=InputBotAppShortName(
                bot_id=bot, short_name=config.MRKT_APP_SHORT_NAME
            ),
            platform="android",
        )
    )
    return extract_init_data(result.url)
