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

import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from src.api.auth import InitData, InitDataError, verify_init_data
from src.core import config
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
        # Debug, not warning: a public endpoint attracts noise, and a single
        # failed signature is not an incident.
        log.debug("initData rejected: %s", exc)
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
    return {
        "id": c.id,
        "kind": c.kind,
        "title": c.title,
        "active": c.active,
        "filters": c.filters_json or {},
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
        "payload": e.payload_json or {},
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
    max_price_ton: str | None = None
    quantity: int | None = Field(default=None, ge=1, le=1000)
    stop_pct_of_floor: int | None = Field(default=None, ge=1, le=500)
    outbid_step_ton: str | None = None
    auto_buy: bool = False
    auto_offer: bool = False
    offer_pct: int | None = Field(default=None, ge=1, le=100)

    @field_validator("max_price_ton", "outbid_step_ton")
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

    def outbid_step_nano(self) -> int | None:
        return (
            Nano.from_ton(self.outbid_step_ton).value if self.outbid_step_ton else None
        )


class CiabattaPatch(BaseModel):
    """A partial update -- only what the user changed."""

    title: str | None = Field(default=None, max_length=128)
    active: bool | None = None
    max_price_ton: str | None = None
    quantity: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("max_price_ton")
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
            from src.markets.mrkt.auth import fetch_init_data

            return await fetch_init_data(client)
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
        "licensed": bool(row and row.is_licensed),
        "dry_run": bool(row.dry_run) if row else True,
        "sessions": {"main": has_main, "writer": has_writer},
        "gift_satellite": await db.get_secret(user.tg_id, "gift_satellite") is not None,
        "support": config.SUPPORT_CONTACT,
        "is_owner": user.tg_id == config.OWNER_TG_ID,
    }


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
                "floor": money(c.floor),
                "floor_change_pct": c.floor_change_pct,
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


@app.get("/api/orders/top")
async def top_order(
    user: LicensedUser, collection: str, model: str | None = None
) -> dict[str, Any]:
    """The highest standing order on a facet.

    MRKT publishes no order book, so only the top order is reported: it is the one
    figure needed to outbid, and fabricating the rest of the book would mean
    fabricating numbers.
    """
    client = await market_for(user.tg_id)
    try:
        order = await client.orders_for(collection, model)
    finally:
        await client.close()

    if order is None:
        return {"order": None, "note": "ордеров не найдено"}
    return {
        "order": {
            "collection": order.collection,
            "model": order.model,
            "price": money(order.price),
            "count": order.count,
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
            filters_json=body.filters,
            max_price_nano=body.max_price_nano(),
            quantity=body.quantity,
            stop_pct_of_floor=body.stop_pct_of_floor,
            outbid_step_nano=body.outbid_step_nano(),
            auto_buy=body.auto_buy,
            auto_offer=body.auto_offer,
            offer_pct=body.offer_pct,
            # Created stopped. A job that starts spending the moment it is saved
            # leaves no chance to review what was just configured.
            active=False,
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

        if body.title is not None:
            row.title = body.title
        if body.active is not None:
            row.active = body.active
        if body.quantity is not None:
            row.quantity = body.quantity
        if body.max_price_ton is not None:
            row.max_price_nano = Nano.from_ton(body.max_price_ton).value
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
