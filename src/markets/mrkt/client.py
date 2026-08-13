"""MRKT HTTP client.

Three transport facts are load-bearing and none of them are optional:

* Requests go through ``curl_cffi`` impersonating Chrome. MRKT filters on TLS
  fingerprint; a plain HTTP client is rejected whatever headers it sends.
* The token goes in ``Authorization`` *without* a ``Bearer`` prefix **and** in a
  ``Cookie``. Sending only one of the two fails.
* Requests are spaced by ``MIN_REQUEST_INTERVAL`` process-wide. There is no
  documented rate limit, no test environment, and the credential at risk is a
  personal Telegram account, so the spacing is not a knob to trade away.

Retry policy is deliberately shallow: one attempt after a 401 refresh, one after
a 429 cooldown. Longer backoff belongs to the polling loops, which know whether
falling behind matters.

Order books: MRKT publishes no order-book endpoint, and none was found in two
independent unofficial sources or in a working third-party client. Rather than
guess a path, ``probe_endpoints`` asks the live server which candidates exist and
``orders_for`` reports provenance, so a number of unknown origin can never
authorise a bid.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from src.core import config
from src.core.money import Nano
from src.markets.mrkt.auth import AuthError, MrktAuth
from src.markets.mrkt.models import Collection, Listing, Order, top_order

log = logging.getLogger(__name__)

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Origin": config.MRKT_CDN.rstrip("/"),
    "Referer": config.MRKT_CDN,
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36"
    ),
}

# Every /gifts/saling call sends the full shape; the server rejects partials.
SEARCH_DEFAULTS: dict[str, Any] = {
    "collectionNames": [],
    "modelNames": [],
    "backdropNames": [],
    "symbolNames": [],
    # "Price", never None. MRKT rejects a null outright:
    #   Cannot convert null value to GiftsService+GiftOrdering
    # It is a non-nullable enum server-side, so every request has to name an
    # ordering even when the caller has no preference.
    "ordering": "Price",
    "lowToHigh": False,
    "maxPrice": None,
    "minPrice": None,
    "mintable": None,
    "number": None,
    "count": config.MRKT_PAGE_SIZE,
    "cursor": "",
    "query": None,
    "promotedFirst": False,
}

ORDERING_VALUES = ("Price", "ModelRarity", "BackgroundRarity", "SymbolRarity")

# MRKT refuses facet requests containing too many collections. Keep requests
# comfortably below its current limit of 10; _facet_rows also splits a rejected
# batch so a server-side limit change cannot break "select all" again.
FACET_COLLECTION_BATCH_SIZE = 10

# Candidate order-book paths, in descending order of plausibility. Probed, never
# assumed: an invented endpoint that 404s silently would look like "no orders",
# which reads as "safe to bid low" -- the expensive direction to be wrong in.
ORDER_BOOK_CANDIDATES = (
    ("GET", "/orders"),
    ("POST", "/orders"),
    ("POST", "/orders/search"),
    ("GET", "/offers"),
    ("POST", "/offers/search"),
    ("POST", "/gifts/orders"),
    ("GET", "/gifts/orders"),
    ("POST", "/offers/saling"),
)


class MrktError(RuntimeError):
    pass


class MrktClient:
    def __init__(
        self,
        init_data_source: Callable[[], Awaitable[str]] | None = None,
        static_token: str | None = None,
        session: Any | None = None,
    ):
        if session is not None:
            self.session = session
        else:
            # Imported here so the module loads without curl_cffi present: the
            # API process imports the models but never opens a socket.
            from curl_cffi.requests import AsyncSession

            self.session = AsyncSession(impersonate=config.IMPERSONATE)
        self.auth = MrktAuth(
            self.session,
            init_data_source=init_data_source,
            static_token=static_token,
        )
        self._gate = asyncio.Lock()
        self._last_request = 0.0
        # Two sources disagree on whether by-id lookup exists; the first attempt
        # settles it and the answer is remembered for the process lifetime.
        self._by_id_supported: bool | None = None
        # Set once probing has run, so a caller can tell "no book" from
        # "never looked".
        self.order_book_path: tuple[str, str] | None = None
        self.order_book_probed = False

    async def close(self) -> None:
        close = getattr(self.session, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def _throttle(self) -> None:
        async with self._gate:
            loop = asyncio.get_running_loop()
            wait = config.MIN_REQUEST_INTERVAL - (loop.time() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = loop.time()

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        *,
        _retry: bool = True,
        raise_for_status: bool = True,
    ) -> Any:
        await self._throttle()
        token = await self.auth.token()
        headers = {
            **BASE_HEADERS,
            "Authorization": token,
            "Cookie": f"access_token={token}",
        }
        url = config.MRKT_API + path
        if method == "GET":
            r = await self.session.get(
                url, headers=headers, timeout=config.REQUEST_TIMEOUT
            )
        else:
            r = await self.session.post(
                url, headers=headers, json=json, timeout=config.REQUEST_TIMEOUT
            )

        if r.status_code == 401 and _retry:
            self.auth.invalidate()
            await self.auth.token(force_refresh=True)
            return await self._request(
                method, path, json=json,
                _retry=False, raise_for_status=raise_for_status,
            )
        if r.status_code == 429 and _retry:
            delay = _retry_after(r) or config.RATE_LIMIT_COOLDOWN
            log.warning("MRKT rate limited on %s, waiting %.0fs", path, delay)
            await asyncio.sleep(delay)
            return await self._request(
                method, path, json=json,
                _retry=False, raise_for_status=raise_for_status,
            )
        if r.status_code != 200:
            if not raise_for_status:
                # The body is returned alongside the status, not discarded. MRKT
                # publishes no API, so its validation message naming the missing
                # or wrong field is the only specification there is -- throwing it
                # away leaves a caller with a bare number and nothing to act on.
                payload: dict[str, Any] = {"__status__": r.status_code}
                if r.text:
                    try:
                        body = r.json()
                    except Exception:
                        payload["__text__"] = r.text[:400]
                    else:
                        if isinstance(body, dict):
                            payload.update(body)
                        else:
                            payload["__body__"] = body
                return payload
            # A 400 means MRKT understood the request and refused its shape --
            # a field renamed, a new one required, an enum that stopped accepting
            # null. Since MRKT publishes no API, its own error text is the only
            # specification available, so the field names we sent are included
            # alongside it: seeing "we sent X, it wants Y" is the whole diagnosis.
            #
            # One reading trap, learned the hard way. A complaint about a field
            # called "req" does NOT mean a field named req is missing: "req" is
            # the parameter name in MRKT's own controller signature, so
            # "The req field is required" means the whole body failed to bind.
            # Look at the *other* errors in the same response for the real cause
            # -- in our case an `ordering: null` that could not convert.
            #
            # Keys only, never values: a payload can carry prices and a cursor,
            # and the surrounding log lines are not the place for them.
            if r.status_code == 400 and isinstance(json, dict):
                raise MrktError(
                    f"MRKT {path}: HTTP 400 {r.text[:300]} "
                    f"(sent fields: {sorted(json)})"
                )
            raise MrktError(f"MRKT {path}: HTTP {r.status_code} {r.text[:200]}")
        if not r.text:
            return {}
        try:
            return r.json()
        except Exception as exc:  # noqa: BLE001 - json() raises several types
            raise MrktError(f"MRKT {path}: response was not JSON") from exc

    # --- reads -----------------------------------------------------------

    async def search(
        self,
        *,
        collections: list[str] | None = None,
        models: list[str] | None = None,
        backdrops: list[str] | None = None,
        symbols: list[str] | None = None,
        ordering: str | None = None,
        cheapest_first: bool = False,
        count: int | None = None,
        cursor: str = "",
        min_price: Nano | None = None,
        max_price: Nano | None = None,
    ) -> tuple[list[Listing], str | None]:
        """One page of listings. Returns (listings, next_cursor).

        Backdrop colours and both floor figures arrive on every row, so nothing
        downstream needs a second lookup to render a card or judge a discount.
        """
        if ordering is not None and ordering not in ORDERING_VALUES:
            raise MrktError(
                f"unknown ordering {ordering!r}; expected one of {ORDERING_VALUES}"
            )

        payload = {
            **SEARCH_DEFAULTS,
            "collectionNames": list(collections or []),
            "modelNames": list(models or []),
            "backdropNames": list(backdrops or []),
            "symbolNames": list(symbols or []),
            # The server caps this; asking for more silently returns fewer.
            "count": min(count or config.MRKT_PAGE_SIZE, config.MRKT_PAGE_SIZE),
            "cursor": cursor or "",
        }
        if cheapest_first:
            payload["ordering"] = "Price"
            payload["lowToHigh"] = True
        elif ordering:
            payload["ordering"] = ordering
        if min_price is not None:
            payload["minPrice"] = min_price.value
        if max_price is not None:
            payload["maxPrice"] = max_price.value

        data = await self._request("POST", "/gifts/saling", json=payload)
        if not isinstance(data, dict):
            return [], None
        rows = data.get("gifts") or []
        listings = [Listing.parse(g) for g in rows if g.get("isOnSale", True)]
        return listings, data.get("cursor") or None

    async def newest(self, count: int | None = None) -> list[Listing]:
        """Most recently listed lots. Default ordering is by listing time."""
        listings, _ = await self.search(count=count)
        return listings

    async def collections(self) -> list[Collection]:
        data = await self._request("GET", "/gifts/collections")
        rows = data if isinstance(data, list) else []
        return [
            c
            for c in (Collection.parse(r) for r in rows)
            if c.name
            and not c.name.isdigit()
            and not c.is_hidden
            and c.raw.get("craftable", True)
        ]

    async def models(self, collections: list[str]) -> list[dict[str, Any]]:
        """All models published for the selected collections.

        This endpoint is case-sensitive: it expects ``Collections`` rather than
        the ``collectionNames`` used by listing search. Sending the latter yields
        a misleading "Collections is required" validation error.
        """
        return await self._facet_rows("/gifts/models", collections)

    async def backdrops(self, collections: list[str]) -> list[dict[str, Any]]:
        """All backdrops published for the selected collections."""
        return await self._facet_rows("/gifts/backdrops", collections)

    async def _facet_rows(
        self, path: str, collections: list[str]
    ) -> list[dict[str, Any]]:
        names = list(dict.fromkeys(name for name in collections if name))
        rows: list[dict[str, Any]] = []
        for start in range(0, len(names), FACET_COLLECTION_BATCH_SIZE):
            batch = names[start : start + FACET_COLLECTION_BATCH_SIZE]
            rows.extend(await self._facet_batch(path, batch))
        return rows

    async def _facet_batch(
        self, path: str, collections: list[str]
    ) -> list[dict[str, Any]]:
        if not collections:
            return []
        try:
            data = await self._request(
                "POST", path, json={"Collections": collections}
            )
        except MrktError as exc:
            too_many = "Too many gifts collections" in str(exc)
            if not too_many or len(collections) == 1:
                raise
            middle = len(collections) // 2
            left = await self._facet_batch(path, collections[:middle])
            right = await self._facet_batch(path, collections[middle:])
            return left + right
        return (
            [row for row in data if isinstance(row, dict)]
            if isinstance(data, list)
            else []
        )

    async def model_page(
        self,
        collection: str,
        model: str,
        *,
        count: int | None = None,
        cursor: str = "",
        max_price: Nano | None = None,
    ) -> tuple[list[Listing], str | None]:
        """Cheapest-first page for one model. The comparables source."""
        return await self.search(
            collections=[collection] if collection else None,
            models=[model] if model else None,
            cheapest_first=True,
            count=count,
            cursor=cursor,
            max_price=max_price,
        )

    async def facet_floor(
        self,
        *,
        collection: str | None = None,
        model: str | None = None,
        backdrop: str | None = None,
    ) -> Nano | None:
        """Cheapest asking price for a facet combination.

        MRKT has no per-model or per-backdrop floor endpoint, so the floor is the
        first row of a cheapest-first page: one request, one row.
        """
        rows, _ = await self.search(
            collections=[collection] if collection else None,
            models=[model] if model else None,
            backdrops=[backdrop] if backdrop else None,
            cheapest_first=True,
            count=1,
        )
        return rows[0].price if rows else None

    async def get_listing(self, listing: Listing) -> Listing | None:
        """Re-read one lot straight from the market.

        Two sources disagree on whether ``/gifts/gift/{id}`` exists: a published
        client calls it, a working implementation states it does not. So the cheap
        path is tried once and the outcome remembered; on failure this falls back
        to scanning the lot's own model page.

        ``None`` means "gone, or unprovable" and callers must treat it as a
        refusal to spend -- never as "probably still fine".
        """
        if self._by_id_supported is not False:
            data = await self._request(
                "GET", f"/gifts/gift/{listing.id}", raise_for_status=False
            )
            ok = isinstance(data, dict) and not data.get("__status__") and data.get("id")
            if ok:
                if self._by_id_supported is None:
                    log.info("MRKT by-id lookup works; using it")
                self._by_id_supported = True
                return Listing.parse(data)
            if self._by_id_supported is None:
                status = data.get("__status__") if isinstance(data, dict) else "?"
                log.info(
                    "MRKT by-id lookup unavailable (status %s); "
                    "falling back to model-page scan",
                    status,
                )
            self._by_id_supported = False

        rows, _ = await self.model_page(listing.collection, listing.model, count=20)
        for row in rows:
            if row.id == listing.id:
                return row
        return None

    async def orders_page(
        self,
        *,
        collections: list[str] | None = None,
        models: list[str] | None = None,
        backdrops: list[str] | None = None,
        count: int | None = None,
        cursor: str = "",
    ) -> tuple[list[Order], str | None, int]:
        """One page of the live order book. Returns (orders, next_cursor, total).

        ``POST /orders`` takes the same body shape as the search endpoint --
        confirmed against the live server, which rejects a bare ``{}`` and accepts
        the name-array form. Response is ``{orders, cursor, total}``.

        Note the quantity effect: a body with ``collectionNames`` set returned 20
        orders where an unfiltered one returned 5, so filtering server-side is not
        merely a convenience -- it is how the useful rows are reached at all.
        """
        body: dict[str, Any] = {
            "collectionNames": list(collections or []),
            "modelNames": list(models or []),
            "backdropNames": list(backdrops or []),
            "count": min(count or config.MRKT_PAGE_SIZE, config.MRKT_PAGE_SIZE),
            "cursor": cursor or "",
        }
        data = await self._request("POST", "/orders", json=body)
        if not isinstance(data, dict):
            return [], None, 0

        rows = data.get("orders")
        if not isinstance(rows, list):
            return [], None, 0

        orders = [Order.parse(r) for r in rows if isinstance(r, dict)]
        total = data.get("total")
        return orders, data.get("cursor") or None, int(total or 0)

    async def orders_for(
        self,
        collection: str,
        model: str | None = None,
        backdrop: str | None = None,
    ) -> Order | None:
        """The highest competing order on a collection, model or backdrop.

        Only the top order matters: to be first in the queue you beat one number,
        not the whole book.

        Own orders are excluded -- outbidding yourself raises the price you pay
        while competing with nobody, and since the tool re-reads the book after
        each placement, including them would make it bid against itself.
        """
        orders, _, _ = await self.orders_page(
            collections=[collection],
            models=[model] if model else None,
            backdrops=[backdrop] if backdrop else None,
        )
        return top_order(orders)

    async def probe_endpoints(self) -> dict[str, int]:
        """Ask the live server which order-book candidates exist.

        Discovery, not guesswork: the report says what actually answered, so a
        path is only used once it has proven to exist. Safe against a live
        account -- every candidate is a read.
        """
        report: dict[str, int] = {}
        for method, path in ORDER_BOOK_CANDIDATES:
            try:
                data = await self._request(
                    method, path,
                    json={} if method == "POST" else None,
                    raise_for_status=False,
                )
            except (MrktError, AuthError) as exc:
                log.debug("probe %s %s failed: %s", method, path, exc)
                report[f"{method} {path}"] = -1
                continue
            status = data.get("__status__", 200) if isinstance(data, dict) else 200
            report[f"{method} {path}"] = status
            if status == 200 and self.order_book_path is None:
                self.order_book_path = (method, path)
                log.info("MRKT order book found at %s %s", method, path)
        self.order_book_probed = True
        if self.order_book_path is None:
            log.info("MRKT exposes no reachable order-book endpoint")
        return report

    # --- writes ----------------------------------------------------------

    async def buy(self, gift_id: str) -> dict:
        """Buy one lot. Spends real money -- callers must gate on DRY_RUN."""
        data = await self._request("POST", "/gifts/buy", json={"Ids": [gift_id]})
        return data if isinstance(data, dict) else {"result": data}

    async def make_offer(self, sale_id: str, price: Nano) -> dict:
        """Offer on one lot. ``sale_id`` must be the SALE id, not the gift id."""
        data = await self._request(
            "POST", "/offers/create",
            json={"price": price.value, "giftSaleId": sale_id},
        )
        return data if isinstance(data, dict) else {"result": data}

    async def cancel_offer(self, offer_id: str) -> dict:
        data = await self._request("POST", "/offers/cancel", json={"ids": [offer_id]})
        return data if isinstance(data, dict) else {"result": data}

    async def balance(self) -> dict:
        data = await self._request("GET", "/balance")
        return data if isinstance(data, dict) else {}


def _retry_after(response) -> float | None:
    raw = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def buy_succeeded(payload: object) -> bool:
    """Whether a /gifts/buy response actually means "you own it now".

    The response shape has varied, so several positive signals are accepted --
    but anything unrecognised counts as failure, because a false positive here
    books a purchase that never happened and then prices an exit against a gift
    the user does not have.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is True:
        return True
    if str(payload.get("status", "")).lower() in {"purchased", "success", "ok"}:
        return True
    gift = payload.get("userGift")
    return bool(isinstance(gift, dict) and gift.get("id"))
