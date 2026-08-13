"""Mini-app HTTP API.

Every endpoint is authenticated by Telegram's ``initData`` signature -- there are
no sessions, cookies or tokens of our own. The mini-app resends the launch payload
with each request and ``src.api.auth`` verifies it.

Money crosses this boundary twice: as ``nano`` integers, which are authoritative,
and as ``ton`` decimal strings for display. Never as a JSON number -- ``2.5``
survives a round trip but ``0.1`` does not, and a price that drifts in the last
decimal place is a wrong bid.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from src.api.auth import InitData, InitDataError, verify_init_data
from src.core import config, licenses
from src.core.money import MoneyError, Nano
from src.db import models as db

log = logging.getLogger(__name__)

app = FastAPI(
    title="Ciabatta Tools",
    # Nothing here is for public browsing, and an exposed schema is a map of the
    # trading endpoints.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# The mini-app is served from PUBLIC_URL, so production is same-origin. CORS is
# opened only to Telegram's own web clients, which host the iframe themselves.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://web.telegram.org",
        "https://webk.telegram.org",
        "https://webz.telegram.org",
    ],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


# --- auth --------------------------------------------------------------------


async def current_user(
    x_init_data: Annotated[str | None, Header(alias="X-Init-Data")] = None,
) -> InitData:
    """Verify the caller and ensure a user row exists.

    Every failure returns a bare 401: telling "expired" apart from "bad signature"
    tells someone probing which half to keep working on.
    """
    try:
        data = verify_init_data(x_init_data or "")
    except InitDataError as exc:
        # Warning, not debug. The client is given a bare 401 -- distinguishing
        # "expired" from "bad signature" would tell someone probing which half to
        # keep working on -- but the operator needs the reason, because a 401 here
        # has several unrelated causes: a wrong BOT_TOKEN, a stale payload, or an
        # app opened outside Telegram. Without this line all three look the same.
        #
        # The payload itself is never logged: it contains a valid signature for a
        # real user, so a log file holding one is a replayable credential. Only
        # its length, which separates "header absent" from "header rejected".
        log.warning(
            "initData rejected: %s (header %s)",
            exc,
            f"{len(x_init_data)} chars" if x_init_data else "absent",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        ) from None

    await db.ensure_user(data.tg_id, data.username)
    return data


async def licensed_user(user: Annotated[InitData, Depends(current_user)]) -> InitData:
    """Authenticated *and* holding a licence.

    Separate from ``current_user`` because ``/api/me`` must answer for an
    unlicensed caller -- that is how the app knows to show the key prompt instead
    of an error screen.
    """
    row = await db.get_user(user.tg_id)
    if row is None or not row.is_licensed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "no_licence",
                "message": "Нужен ключ доступа",
                "contact": config.SUPPORT_CONTACT,
            },
        )
    return user


CurrentUser = Annotated[InitData, Depends(current_user)]
LicensedUser = Annotated[InitData, Depends(licensed_user)]


# --- serialisation -----------------------------------------------------------


def money(amount: Nano | None) -> dict[str, Any] | None:
    """Render an amount for JSON.

    Both forms travel together: ``nano`` is what the client echoes back to act on
    this exact price, ``ton`` is what it shows. The client never converts between
    them.
    """
    if amount is None:
        return None
    return {"nano": amount.value, "ton": amount.format_ton(2)}


def _nano_or_none(value: int | None) -> dict[str, Any] | None:
    return money(Nano(value)) if value else None


def _load_json(raw: str | None) -> dict[str, Any]:
    """Decode a JSON text column.

    ``filters_json`` and ``payload_json`` are ``Text``, not a JSON type, so that
    the schema works identically on SQLite and Postgres -- SQLite is the default
    and has no JSONB. That means encoding and decoding happen here rather than in
    the driver.

    A row that fails to parse returns {} instead of raising: one malformed blob
    should cost that Ciabatta its filters, not take down the whole listing.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("could not decode stored JSON: %r", raw[:80])
        return {}
    # A stored scalar or list would break every caller that expects a mapping.
    return value if isinstance(value, dict) else {}


def listing_json(g) -> dict[str, Any]:
    return {
        "id": g.id,
        "sale_id": g.sale_id,
        "collection": g.collection,
        "model": g.model,
        "backdrop": g.backdrop,
        "symbol": g.symbol,
        "number": g.number,
        "price": money(g.price),
        "floor_collection": money(g.floor_collection),
        "floor_backdrop_model": money(g.floor_backdrop_model),
        "rarity": g.rarity,
        "rarity_per_mille": g.model_rarity_per_mille,
        "thumb": g.thumb_key,
        # Two colours, not a CSS string: the client owns its gradient rendering,
        # and a server-built `linear-gradient(...)` would freeze it.
        "backdrop_colors": g.backdrop_pair,
        "tg_url": f"https://t.me/nft/{g.collection.replace(' ', '')}-{g.number}"
        if g.number
        else None,
    }


def ciabatta_json(c) -> dict[str, Any]:
    filters = _load_json(c.filters_json)
    return {
        "id": c.id,
        "kind": c.kind,
        "title": c.title,
        "active": c.active,
        "filters": filters,
        "min_price": _nano_or_none(filters.get("price_from_nano")),
        "max_price": _nano_or_none(c.max_price_nano),
        "quantity": c.quantity,
        "filled": c.filled,
        "stop_pct_of_floor": c.stop_pct_of_floor,
        "outbid_step": _nano_or_none(c.outbid_step_nano),
        "auto_buy": c.auto_buy,
        "auto_offer": c.auto_offer,
        "offer_pct": c.offer_pct,
        "paused_until": c.paused_until.isoformat() if c.paused_until else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def event_json(e) -> dict[str, Any]:
    return {
        "id": e.id,
        "kind": e.kind,
        "ciabatta_id": e.ciabatta_id,
        "gift_slug": e.gift_slug,
        "price": _nano_or_none(e.price_nano),
        "body": e.body,
        "payload": _load_json(e.payload_json),
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


# --- request bodies ----------------------------------------------------------


class CiabattaIn(BaseModel):
    """A new automation job.

    Prices arrive as decimal strings and are parsed through ``Nano``, so a value
    JSON cannot hold exactly is rejected here rather than rounded inside a bid.
    """

    kind: str = Field(pattern="^(tracker|ordering|sniping|automessage)$")
    title: str = Field(default="", max_length=128)
    filters: dict[str, Any] = Field(default_factory=dict)
    min_price_ton: str | None = None
    max_price_ton: str | None = None
    quantity: int | None = Field(default=None, ge=1, le=1000)
    stop_pct_of_floor: int | None = Field(default=None, ge=1, le=500)
    outbid_step_ton: str | None = None
    auto_buy: bool = False
    auto_offer: bool = False
    offer_pct: int | None = Field(default=None, ge=1, le=100)

    @field_validator("min_price_ton", "max_price_ton", "outbid_step_ton")
    @classmethod
    def _parseable(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            Nano.from_ton(v)
        except (MoneyError, ValueError) as exc:
            raise ValueError(f"invalid amount: {exc}") from None
        return v

    def max_price_nano(self) -> int | None:
        return Nano.from_ton(self.max_price_ton).value if self.max_price_ton else None

    def filters_for_storage(self) -> dict[str, Any]:
        filters = dict(self.filters)
        if self.min_price_ton:
            filters["price_from_nano"] = Nano.from_ton(self.min_price_ton).value
        else:
            filters.pop("price_from_nano", None)
        return filters

    def outbid_step_nano(self) -> int | None:
        return (
            Nano.from_ton(self.outbid_step_ton).value if self.outbid_step_ton else None
        )


class CiabattaPatch(BaseModel):
    """A partial update -- only what the user changed."""

    title: str | None = Field(default=None, max_length=128)
    active: bool | None = None
    filters: dict[str, Any] | None = None
    min_price_ton: str | None = None
    max_price_ton: str | None = None
    quantity: int | None = Field(default=None, ge=1, le=1000)
    stop_pct_of_floor: int | None = Field(default=None, ge=1, le=500)
    outbid_step_ton: str | None = None
    auto_buy: bool | None = None
    auto_offer: bool | None = None
    offer_pct: int | None = Field(default=None, ge=1, le=100)

    @field_validator("min_price_ton", "max_price_ton", "outbid_step_ton")
    @classmethod
    def _parseable(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            Nano.from_ton(v)
        except (MoneyError, ValueError) as exc:
            raise ValueError(f"invalid amount: {exc}") from None
        return v


# --- market client -----------------------------------------------------------


async def market_for(tg_id: int):
    """An MRKT client bound to one user's Telegram session.

    Per-user rather than shared: MRKT authenticates as the account, so a shared
    client would place one user's orders against another's balance.
    """
    from src.markets.mrkt.client import MrktClient
    from src.mtproto.sessions import MAIN, SessionStore

    store = SessionStore(
        load=db.session_load, save=db.session_save, delete=db.session_delete
    )

    async def init_data_source() -> str:
        client = await store.client(tg_id, MAIN)
        if client is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "no_session",
                    "message": "Telegram-аккаунт не подключён",
                },
            )
        try:
            # Opens the mini-app as the user over MTProto and returns the signed
            # blob MRKT accepts. Imported here rather than at module scope so the
            # API can start without Telethon present.
            from src.markets.mrkt.auth import init_data_via_telethon

            return await init_data_via_telethon(client)
        finally:
            await client.disconnect()

    return MrktClient(init_data_source=init_data_source)


# --- endpoints ---------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness probe.

    Deliberately does not touch the database: a host that restarts the container
    because Postgres blipped turns a recoverable stall into an outage.
    """
    return {"ok": True, "dry_run": config.DRY_RUN}


@app.get("/api/me")
async def me(user: CurrentUser) -> dict[str, Any]:
    """Who the caller is and what they still need.

    Drives the first screen, so it answers for unlicensed users too.
    """
    row = await db.get_user(user.tg_id)
    has_main = await db.session_load(user.tg_id, "main") is not None
    has_writer = await db.session_load(user.tg_id, "writer") is not None
    return {
        "tg_id": user.tg_id,
        "username": user.username,
        "bot_username": config.BOT_USERNAME,
        "licensed": bool(row and row.is_licensed),
        # When the key was activated. Keys are permanent, so there is no expiry
        # date to report -- the client labels the term as lifetime. Null until a
        # licence is claimed.
        "licensed_at": row.licensed_at.isoformat() if row and row.licensed_at else None,
        "dry_run": bool(row.dry_run) if row else True,
        "sessions": {"main": has_main, "writer": has_writer},
        "gift_satellite": await db.get_secret(user.tg_id, "gift_satellite") is not None,
        "support": config.SUPPORT_CONTACT,
        "is_owner": user.tg_id == config.OWNER_TG_ID,
    }


class LicenceIn(BaseModel):
    """A key typed into the mini-app."""

    # Generous bound: the field accepts pasted text with spacing and dashes, and
    # normalisation happens after. Rejecting on length would refuse keys that are
    # merely untidy.
    key: str = Field(min_length=1, max_length=128)


@app.post("/api/licence")
async def claim_licence(body: LicenceIn, user: CurrentUser) -> dict[str, Any]:
    """Activate a subscription key from inside the mini-app.

    Deliberately on ``CurrentUser`` and not ``LicensedUser``: this is the endpoint
    an unlicensed user needs, so requiring a licence to reach it would be circular.

    Mirrors the bot's dialogue rather than replacing it -- someone who opened the
    app first should not have to go back to the chat to type a key.
    """
    key = licenses.normalize(body.key)

    # Shape first: it costs nothing and keeps obvious typos out of the query path.
    # The same reply is used for a malformed key and an unknown one, so probing
    # cannot distinguish "wrong shape" from "not issued".
    rejected = {
        "ok": False,
        "message": "Ключ не подошёл. Проверь, что скопировал его целиком.",
        "contact": config.SUPPORT_CONTACT,
    }
    if not licenses.is_valid_format(key):
        return rejected

    record = await db.find_licence(key)
    if record is None:
        log.info("rejected licence attempt fp=%s", licenses.fingerprint(key))
        return rejected

    if record.is_claimed and record.claimed_by != user.tg_id:
        return {
            "ok": False,
            "message": "Этот ключ уже активирован на другом аккаунте.",
            "contact": config.SUPPORT_CONTACT,
        }

    claimed = await db.claim_licence(key, user.tg_id)
    if not claimed:
        # Lost a race against another request for the same key.
        return {
            "ok": False,
            "message": "Этот ключ уже активирован на другом аккаунте.",
            "contact": config.SUPPORT_CONTACT,
        }

    # Fingerprint, never the key: a log file holding keys is a list of working
    # credentials.
    log.info(
        "licence claimed via app fp=%s tg_id=%s",
        licenses.fingerprint(key),
        user.tg_id,
    )
    return {"ok": True, "message": "Доступ открыт"}


@app.get("/api/collections")
async def collections(user: LicensedUser) -> dict[str, Any]:
    """Collections with floors -- the catalogue's first level."""
    client = await market_for(user.tg_id)
    try:
        rows = await client.collections()
    finally:
        await client.close()

    return {
        "collections": [
            {
                "name": c.name,
                "title": c.title or c.name,
                "floor": money(c.floor),
                "floor_change_pct": c.floor_change_pct,
                "thumb": c.thumb_key,
                "created_at": c.created_at,
                "is_new": c.is_new,
            }
            for c in rows
        ]
    }


@app.get("/api/catalog")
async def catalog(
    user: LicensedUser,
    collection: Annotated[list[str] | None, Query()] = None,
    model: Annotated[list[str] | None, Query()] = None,
    backdrop: Annotated[list[str] | None, Query()] = None,
    max_price_ton: str | None = None,
    min_price_ton: str | None = None,
    cheapest_first: bool = True,
    count: int = Query(default=30, ge=1, le=100),
    cursor: str = "",
) -> dict[str, Any]:
    """Search listings.

    Facets are repeated query parameters (``?model=A&model=B``), so several models
    or backdrops can be selected at once, up to all of them.
    """
    try:
        lo = Nano.from_ton(min_price_ton) if min_price_ton else None
        hi = Nano.from_ton(max_price_ton) if max_price_ton else None
    except (MoneyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid price: {exc}") from None

    client = await market_for(user.tg_id)
    try:
        items, next_cursor = await client.search(
            collections=collection or None,
            models=model or None,
            backdrops=backdrop or None,
            cheapest_first=cheapest_first,
            count=count,
            cursor=cursor,
            min_price=lo,
            max_price=hi,
        )
    finally:
        await client.close()

    return {"items": [listing_json(g) for g in items], "cursor": next_cursor}


@app.get("/api/facets")
async def facets(
    user: LicensedUser,
    collection: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    """Complete model and backdrop lists for the selected collections."""
    scope = [name for name in (collection or []) if name]
    if not scope:
        return {"models": [], "backdrops": [], "symbols": []}

    client = await market_for(user.tg_id)
    try:
        model_rows = await client.models(scope)
        backdrop_rows = await client.backdrops(scope)
    finally:
        await client.close()

    models: dict[str, dict[str, Any]] = {}
    for row in model_rows:
        name = row.get("modelName") or row.get("modelTitle")
        if not name:
            continue
        floor_nano = Nano.parse(row.get("floorPriceNanoTons"))
        models[name] = {
            "name": name,
            "collection": row.get("collectionName"),
            "thumb": row.get("modelStickerThumbnailKey"),
            "floor": money(floor_nano),
            "rarity_per_mille": row.get("rarityPerMille"),
            "created_at": row.get("createdAt"),
        }

    backdrops: dict[str, dict[str, Any]] = {}
    for row in backdrop_rows:
        name = row.get("backdropName")
        if not name:
            continue
        center, edge = row.get("colorsCenterColor"), row.get("colorsEdgeColor")
        colors = (
            [center, edge]
            if all(isinstance(value, int) and 0 <= value <= 0xFFFFFF for value in (center, edge))
            else None
        )
        floor_nano = Nano.parse(row.get("floorNanoTons"))
        backdrops[name] = {
            "name": name,
            "collection": row.get("collectionName"),
            "floor": money(floor_nano),
            "rarity_per_mille": row.get("rarityPerMille"),
            "backdrop_colors": colors,
        }

    return {
        "models": [models[k] for k in sorted(models)],
        "backdrops": [backdrops[k] for k in sorted(backdrops)],
        "symbols": [],
    }


def _facet_scope(collection, model, backdrop) -> str:
    """Name the facet a floor describes.

    Reported back because collection floor, model floor and backdrop+model floor
    are three different claims, and a client that cannot tell them apart will
    label one as another.
    """
    if model and backdrop:
        return "backdrop_model"
    if model:
        return "model"
    if backdrop:
        return "backdrop"
    return "collection"


@app.get("/api/floor")
async def floor(
    user: LicensedUser,
    collection: str | None = None,
    model: str | None = None,
    backdrop: str | None = None,
) -> dict[str, Any]:
    """The floor for one facet.

    Feeds the price box in the sniping screen, where the +/-5% buttons need a
    reference matching whichever facet is being hunted.
    """
    if not any((collection, model, backdrop)):
        raise HTTPException(status_code=400, detail="specify at least one facet")

    client = await market_for(user.tg_id)
    try:
        value = await client.facet_floor(
            collection=collection, model=model, backdrop=backdrop
        )
    finally:
        await client.close()

    return {"scope": _facet_scope(collection, model, backdrop), "floor": money(value)}


@app.get("/api/floors/summary")
async def floor_summary(
    user: LicensedUser,
    collection: str,
    model: str | None = None,
    backdrop: str | None = None,
) -> dict[str, Any]:
    """Collection, model and backdrop floors for the ordering reference strip."""
    client = await market_for(user.tg_id)
    try:
        collection_floor = await client.facet_floor(collection=collection)
        model_floor = (
            await client.facet_floor(collection=collection, model=model)
            if model
            else None
        )
        backdrop_floor = (
            await client.facet_floor(
                collection=collection, model=model, backdrop=backdrop
            )
            if backdrop
            else None
        )
    finally:
        await client.close()
    return {
        "collection": money(collection_floor),
        "model": money(model_floor),
        "backdrop": money(backdrop_floor),
    }


@app.get("/api/orders/top")
async def top_order(
    user: LicensedUser,
    collection: str,
    model: str | None = None,
    backdrop: str | None = None,
) -> dict[str, Any]:
    """The highest standing order on a facet.

    MRKT publishes no order book, so only the top order is reported: it is the one
    figure needed to outbid, and fabricating the rest of the book would mean
    fabricating numbers.
    """
    client = await market_for(user.tg_id)
    try:
        order = await client.orders_for(collection, model, backdrop)
    finally:
        await client.close()

    if order is None:
        return {"order": None, "note": "ордеров не найдено"}
    return {
        "order": {
            "id": order.id,
            "collection": order.collection,
            "model": order.model,
            "backdrop": order.backdrop,
            # A range, not a single bid: an MRKT order says "buy N units between
            # min and max". price is an alias for price_max, which is the figure a
            # competitor has to beat.
            "price": money(order.price_max),
            "price_min": money(order.price_min),
            "price_max": money(order.price_max),
            "total_quantity": order.total_quantity,
            "completed_quantity": order.completed_quantity,
            # Units still unfilled. An order with none left does not compete for
            # the next sale, so the UI should not present it as a target.
            "remaining": order.remaining,
            # The tool never outbids this one -- raising the price you pay while
            # competing with nobody -- so the UI labels it rather than hiding it.
            "is_mine": order.is_mine,
            "whole_collection": order.targets_whole_collection,
            "end_at": order.end_at,
            # Exposed so the UI can mark an untrusted figure: a price whose
            # provenance is unknown must not look authoritative.
            "source": order.source,
            "trustworthy": order.is_trustworthy,
        }
    }


@app.get("/api/ciabattas")
async def list_ciabattas(user: LicensedUser) -> dict[str, Any]:
    async with db.session_scope() as s:
        rows = (
            await s.scalars(
                select(db.Ciabatta)
                .where(db.Ciabatta.tg_id == user.tg_id)
                .order_by(db.Ciabatta.created_at.desc())
            )
        ).all()
    return {"ciabattas": [ciabatta_json(c) for c in rows]}


@app.post("/api/ciabattas", status_code=201)
async def create_ciabatta(body: CiabattaIn, user: LicensedUser) -> dict[str, Any]:
    async with db.session_scope() as s:
        row = db.Ciabatta(
            tg_id=user.tg_id,
            kind=body.kind,
            title=body.title or "",
            # Serialised here because the column is Text: SQLite is the default
            # database and has no JSON type, so passing a dict binds nothing.
            filters_json=json.dumps(body.filters_for_storage(), ensure_ascii=False),
            max_price_nano=body.max_price_nano(),
            quantity=body.quantity,
            stop_pct_of_floor=body.stop_pct_of_floor,
            outbid_step_nano=body.outbid_step_nano(),
            auto_buy=body.auto_buy,
            auto_offer=body.auto_offer,
            offer_pct=body.offer_pct,
            # Tracking is read-only and should work as soon as it is saved.
            # Spending and auto-message jobs still require an explicit start.
            active=body.kind == "tracker",
        )
        s.add(row)
        await s.flush()
        return {"ciabatta": ciabatta_json(row)}


@app.patch("/api/ciabattas/{cid}")
async def patch_ciabatta(
    cid: int, body: CiabattaPatch, user: LicensedUser
) -> dict[str, Any]:
    async with db.session_scope() as s:
        row = await s.get(db.Ciabatta, cid)
        # Ownership checked before existence is revealed, so probing ids cannot
        # enumerate other users' jobs.
        if row is None or row.tg_id != user.tg_id:
            raise HTTPException(status_code=404, detail="not found")

        fields = body.model_fields_set
        if "title" in fields:
            row.title = body.title or ""
        if "active" in fields and body.active is not None:
            row.active = body.active
        filters = _load_json(row.filters_json)
        if "filters" in fields:
            filters = dict(body.filters or {})
        if "min_price_ton" in fields:
            if body.min_price_ton:
                filters["price_from_nano"] = Nano.from_ton(body.min_price_ton).value
            else:
                filters.pop("price_from_nano", None)
        if "filters" in fields or "min_price_ton" in fields:
            row.filters_json = json.dumps(filters, ensure_ascii=False)
        if "quantity" in fields:
            row.quantity = body.quantity
        if "max_price_ton" in fields:
            row.max_price_nano = (
                Nano.from_ton(body.max_price_ton).value if body.max_price_ton else None
            )
        if "stop_pct_of_floor" in fields:
            row.stop_pct_of_floor = body.stop_pct_of_floor
        if "outbid_step_ton" in fields:
            row.outbid_step_nano = (
                Nano.from_ton(body.outbid_step_ton).value
                if body.outbid_step_ton
                else None
            )
        if "auto_buy" in fields and body.auto_buy is not None:
            row.auto_buy = body.auto_buy
        if "auto_offer" in fields and body.auto_offer is not None:
            row.auto_offer = body.auto_offer
        if "offer_pct" in fields:
            row.offer_pct = body.offer_pct
        await s.flush()
        return {"ciabatta": ciabatta_json(row)}


@app.delete("/api/ciabattas/{cid}", status_code=204)
async def delete_ciabatta(cid: int, user: LicensedUser) -> None:
    async with db.session_scope() as s:
        row = await s.get(db.Ciabatta, cid)
        if row is None or row.tg_id != user.tg_id:
            raise HTTPException(status_code=404, detail="not found")
        await s.delete(row)


@app.get("/api/events")
async def events(
    user: LicensedUser,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = None,
) -> dict[str, Any]:
    """The notification feed, newest first.

    Keyset pagination on id rather than offset: the feed grows while it is being
    read, and offsets would skip or repeat rows as it does.
    """
    async with db.session_scope() as s:
        q = select(db.Event).where(db.Event.tg_id == user.tg_id)
        if before_id:
            q = q.where(db.Event.id < before_id)
        rows = (await s.scalars(q.order_by(db.Event.id.desc()).limit(limit))).all()
    return {"events": [event_json(e) for e in rows]}


@app.post("/api/settings/dry-run")
async def set_dry_run(user: LicensedUser, enabled: bool) -> dict[str, Any]:
    """Arm or disarm real spending.

    Its own endpoint rather than a field in a settings blob: this is the switch
    that turns simulation into money, and a partial update must not flip it by
    omission.
    """
    await db.set_dry_run(user.tg_id, enabled)
    return {"dry_run": enabled}


@app.exception_handler(MoneyError)
async def money_error_handler(request, exc: MoneyError) -> JSONResponse:
    """A price that could not be represented exactly.

    400 rather than 500: the input was wrong, and the message says which value.
    """
    return JSONResponse(status_code=400, content={"detail": f"invalid amount: {exc}"})


# --- static mini-app ---------------------------------------------------------
# Mounted last, because a mount at "/" claims every unmatched path. Declared
# above the API routes it would shadow them and every /api call would return
# index.html with a 200 -- which reads as "the API returns HTML" rather than as a
# routing mistake.
#
# Served from this process rather than a separate web server: BotHost gives one
# container and one port, so the bot, the API and the app share it.

_WEBAPP = config.ROOT / "webapp"

if _WEBAPP.is_dir():
    # html=True makes the mount serve index.html for "/", which is what Telegram
    # opens. Without it the root path 404s and the mini-app button appears broken.
    app.mount("/", StaticFiles(directory=_WEBAPP, html=True), name="webapp")
else:
    # A missing webapp directory means a broken image or a bad working directory.
    # Logged as an error rather than raising: the bot's own API endpoints still
    # work, and a running bot that says why is more useful than a dead container.
    log.error("webapp directory not found at %s -- mini-app will not load", _WEBAPP)
