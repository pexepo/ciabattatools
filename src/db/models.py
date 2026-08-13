"""Database layer: schema, engine, and the adapters other modules plug into.

Two decisions shape this file.

Money is ``BigInteger`` nanoTON -- never ``Float``, never ``Numeric``. A float
column would reintroduce the representation error :mod:`src.core.money` exists
to prevent, at the storage boundary where it is hardest to notice. Values cross
this layer as ints and become :class:`~src.core.money.Nano` above it.

Timestamps are ``TIMESTAMPTZ`` and always timezone-aware UTC. Comparing a naive
timestamp against an aware one raises at runtime, and deciding "is this new?" is
the tracker's entire job -- so the ambiguity is designed out rather than handled.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core import config, licenses
from src.core.crypto import secret_box

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Timezone-aware current time.

    ``datetime.utcnow()`` returns a naive value that merely happens to be UTC;
    mixed with an aware column it raises on comparison. This is the only clock
    this module uses.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- identity and access -----------------------------------------------------


class User(Base):
    """One Telegram user of the bot."""

    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))

    # True by default, matching config.DRY_RUN. A user who has never opened the
    # setting must not be spending real money, so the safe value is the default
    # at every layer -- not only in config.
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    license_key_fp: Mapped[str | None] = mapped_column(String(64), index=True)
    licensed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_licensed(self) -> bool:
        return self.license_key_fp is not None


class License(Base):
    """One of the permanent subscription keys.

    Stores a fingerprint, not the key. A leaked database should not yield a
    working set of keys, and activation only ever compares fingerprints --
    see :func:`src.core.licenses.fingerprint`.
    """

    __tablename__ = "licenses"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(64))

    claimed_by: Mapped[int | None] = mapped_column(BigInteger, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def is_claimed(self) -> bool:
        return self.claimed_by is not None


# --- secrets at rest ---------------------------------------------------------


class SessionBlob(Base):
    """An encrypted MTProto session string.

    ``kind`` is ``main`` or ``writer``: the writer slot is the separate account
    used for auto-messaging, so a ban there cannot cost the user their main
    account. Ciphertext is bound to ``(tg_id, kind)`` by
    :class:`~src.mtproto.sessions.SessionStore`, so a row copied between users or
    slots fails to decrypt instead of quietly working.
    """

    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("tg_id", "kind", name="uq_session_owner_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    blob: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        # Never render `blob`. A default repr would put a live session string --
        # a full account credential -- into any log line or traceback.
        return f"<SessionBlob tg_id={self.tg_id} kind={self.kind}>"


class Secret(Base):
    """An encrypted third-party credential (Gift Satellite key, MRKT token)."""

    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("tg_id", "name", name="uq_secret_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    blob: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Secret tg_id={self.tg_id} name={self.name}>"


# --- observed gifts ----------------------------------------------------------


class Gift(Base):
    """A unique gift the tracker has seen.

    Doubles as the dedupe table: ``slug`` is the primary key, so re-observing a
    gift is an upsert rather than a second notification.
    """

    __tablename__ = "gifts"
    __table_args__ = (
        # The tracker's hot query is "newest first within a collection".
        Index("ix_gift_collection_seen", "collection", "seen_at"),
        Index("ix_gift_burned", "burned"),
    )

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    collection: Mapped[str] = mapped_column(String(128), nullable=False)
    num: Mapped[int | None] = mapped_column(Integer)

    model: Mapped[str | None] = mapped_column(String(128))
    backdrop: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(128))
    model_rarity_per_mille: Mapped[int | None] = mapped_column(Integer)

    # Owner may be genuinely unknown: hidden by privacy settings, or held at a
    # TON address rather than a Telegram account. Nullable columns say so
    # honestly instead of storing a placeholder that reads as a real owner.
    owner_tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    owner_username: Mapped[str | None] = mapped_column(String(64))
    owner_name: Mapped[str | None] = mapped_column(String(128))
    owner_address: Mapped[str | None] = mapped_column(String(80))

    # The tool's core distinction: upgraded (tradeable) versus burned in a craft
    # (gone). Three states, hence nullable -- True, False, and "not yet
    # determined". Defaulting to False would report every unchecked gift as alive.
    burned: Mapped[bool | None] = mapped_column(Boolean)
    crafted: Mapped[bool | None] = mapped_column(Boolean)

    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def tg_link(self) -> str:
        """Public gift page. Renders a preview card inside Telegram."""
        return f"https://t.me/nft/{self.slug}"


# --- user configuration ------------------------------------------------------


class Ciabatta(Base):
    """One configured automation: a tracker filter, an order, or a snipe.

    Filters live in a JSON text column rather than columns of their own. They are
    user-authored, multi-select, and will gain fields; a migration per new filter
    would be friction with no benefit, since the database never queries inside
    them -- the tools do.
    """

    __tablename__ = "ciabattas"
    __table_args__ = (Index("ix_ciabatta_owner_kind", "tg_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)

    # "tracker" | "ordering" | "sniping" | "automessage"
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    filters_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    # nanoTON. Nullable because "no cap set" differs from "a cap of zero", and
    # conflating them either blocks every trade or authorises any price.
    max_price_nano: Mapped[int | None] = mapped_column(BigInteger)
    quantity: Mapped[int | None] = mapped_column(Integer)

    # Ordering: how far below floor to stop, and the outbid step.
    stop_pct_of_floor: Mapped[int | None] = mapped_column(Integer)
    outbid_step_nano: Mapped[int | None] = mapped_column(BigInteger)

    # Sniping: auto-buy versus auto-offer, and the offer's discount.
    auto_buy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_offer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    offer_pct: Mapped[int | None] = mapped_column(Integer)

    # Set when the bot-detection rule trips, so a paused job resumes on its own
    # instead of waiting for the user to come back and restart it.
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    filled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Event(Base):
    """A notification, for both the chat feed and the mini-app feed.

    One row serves both surfaces so they cannot disagree about what happened.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_event_owner_time", "tg_id", "created_at"),
        # The dedupe key includes price upstream, so a relist at a new price is a
        # new event rather than a suppressed duplicate.
        UniqueConstraint("tg_id", "dedupe_key", name="uq_event_owner_dedupe"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ciabatta_id: Mapped[int | None] = mapped_column(
        ForeignKey("ciabattas.id", ondelete="SET NULL")
    )

    # "tracker" | "order_filled" | "snipe_found" | "snipe_bought" | "offer_made"
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)

    gift_slug: Mapped[str | None] = mapped_column(String(128))
    price_nano: Mapped[int | None] = mapped_column(BigInteger)

    # Rendered text, stored so the mini-app shows exactly what the chat showed.
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Floor(Base):
    """A cached floor price for one facet.

    ``scope`` records what the number describes, because collection floor, model
    floor and backdrop+model floor are three different claims and swapping one
    for another misprices every decision downstream. Storing them in one table
    with an explicit scope keeps that distinction visible instead of encoding it
    in a column name.

    Cached rather than fetched per decision: a snipe check that hit the market for
    a floor on every candidate would spend its whole rate-limit budget on
    valuation and have none left to buy.
    """

    __tablename__ = "floors"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_floor_scope_key"),
        Index("ix_floor_fetched", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # "collection" | "model" | "backdrop_model"
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    # Normalised through markets.mrkt.models.norm, so casing and stray spacing
    # cannot split one facet across two rows.
    key: Mapped[str] = mapped_column(String(256), nullable=False)

    # Nullable on purpose: "we asked and nothing is listed" is a real answer, and
    # storing 0 for it would read as a free gift.
    price_nano: Mapped[int | None] = mapped_column(BigInteger)

    # Which market or aggregator said so. A single-market floor is a weaker claim
    # than a cross-market one, and the UI labels them differently.
    source: Mapped[str] = mapped_column(String(32), default="mrkt", nullable=False)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def age_seconds(self) -> float:
        """How stale this figure is.

        Callers decide their own tolerance: a tracker notification can show a
        minute-old floor, while a buy decision should not.
        """
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds()


class SpendLog(Base):
    """Every spend attempt, successful or not.

    Failures are recorded too: the daily cap must count money that was committed,
    and a buy that errored after submission may still have settled. Counting only
    confirmed successes would let a retry loop walk past the cap.
    """

    __tablename__ = "spend_log"
    __table_args__ = (Index("ix_spend_owner_time", "tg_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ciabatta_id: Mapped[int | None] = mapped_column(
        ForeignKey("ciabattas.id", ondelete="SET NULL")
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # buy|offer|order
    amount_nano: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gift_slug: Mapped[str | None] = mapped_column(String(128))

    # True only on a confirmed success. Anything ambiguous stays False, matching
    # the market client's rule that only an explicit success counts.
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- engine ------------------------------------------------------------------

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine():
    """The lazily-created async engine.

    Lazy because importing this module must not require a reachable database:
    tests and ``--help`` would fail at import time otherwise.
    """
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        if not url:
            raise RuntimeError("DATABASE_URL is not set; see .env.example")
        if url.startswith("sqlite"):
            # Hosts commonly point DATABASE_URL at a path whose parent directory
            # does not exist (or is not writable); both end as the same opaque
            # "unable to open database file". Creating the directory here turns
            # that into either a working database or a readable error.
            path = make_url(url).database
            if path and path != ":memory:":
                parent = os.path.dirname(path)
                if parent:
                    try:
                        os.makedirs(parent, exist_ok=True)
                    except OSError:
                        log.error(
                            "cannot create database directory %s -- check that "
                            "the volume mounted there is writable", parent
                        )
                        raise
                log.info("sqlite database file %s", os.path.abspath(path))
        _engine = create_async_engine(
            url,
            # Verifies a pooled connection before handing it out. Without it a
            # long-idle tracker gets a stale socket and fails its first query
            # after every quiet period.
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=5,
            echo=False,
        )
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            engine(), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A transactional session: commits on success, rolls back on failure."""
    async with sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """Create tables if absent.

    For first run and tests. Alembic owns schema changes once the database holds
    real data.
    """
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database schema ready")


# --- adapters for SessionStore ----------------------------------------------
# SessionStore takes load/save/delete callables so src.mtproto stays free of ORM
# imports (see src/mtproto/sessions.py). These are the production wiring; tests
# pass dictionaries instead.


async def session_load(tg_id: int, kind: str) -> str | None:
    async with session_scope() as db:
        row = await db.scalar(
            select(SessionBlob).where(
                SessionBlob.tg_id == tg_id, SessionBlob.kind == kind
            )
        )
        return row.blob if row else None


async def session_save(tg_id: int, kind: str, blob: str) -> None:
    async with session_scope() as db:
        row = await db.scalar(
            select(SessionBlob).where(
                SessionBlob.tg_id == tg_id, SessionBlob.kind == kind
            )
        )
        if row:
            row.blob = blob
            row.updated_at = utcnow()
        else:
            db.add(SessionBlob(tg_id=tg_id, kind=kind, blob=blob))


async def session_delete(tg_id: int, kind: str) -> None:
    async with session_scope() as db:
        row = await db.scalar(
            select(SessionBlob).where(
                SessionBlob.tg_id == tg_id, SessionBlob.kind == kind
            )
        )
        if row:
            await db.delete(row)


async def secret_load(tg_id: int, name: str) -> str | None:
    async with session_scope() as db:
        row = await db.scalar(
            select(Secret).where(Secret.tg_id == tg_id, Secret.name == name)
        )
        return row.blob if row else None


async def secret_save(tg_id: int, name: str, blob: str) -> None:
    async with session_scope() as db:
        row = await db.scalar(
            select(Secret).where(Secret.tg_id == tg_id, Secret.name == name)
        )
        if row:
            row.blob = blob
            row.updated_at = utcnow()
        else:
            db.add(Secret(tg_id=tg_id, name=name, blob=blob))


# --- users -------------------------------------------------------------------


async def ensure_user(tg_id: int, username: str | None = None) -> User:
    """Fetch or create a user row.

    Called on every /start, so it also refreshes the username: people rename
    themselves, and a stale @handle in a notification is a dead link.
    """
    async with session_scope() as db:
        user = await db.get(User, tg_id)
        if user is None:
            user = User(tg_id=tg_id, username=username)
            db.add(user)
        else:
            if username and user.username != username:
                user.username = username
            user.last_seen_at = utcnow()
        # Flushed inside the scope so the caller receives a populated row rather
        # than one whose defaults are still unresolved.
        await db.flush()
        return user


async def get_user(tg_id: int) -> User | None:
    """Fetch a user without creating one.

    Distinct from ``ensure_user``: read paths must not have the side effect of
    registering a user, or a probe against the API would populate the table.
    """
    async with session_scope() as db:
        return await db.get(User, tg_id)


async def set_dry_run(tg_id: int, dry_run: bool) -> None:
    """Arm or disarm real spending for one user.

    Logged at info deliberately: turning this off is what lets the tools spend
    money, and anyone reconstructing "when did it start buying" needs the moment
    recorded.
    """
    async with session_scope() as db:
        await db.execute(update(User).where(User.tg_id == tg_id).values(dry_run=dry_run))
    log.info("dry_run=%s for tg_id=%s", dry_run, tg_id)


# --- licences ----------------------------------------------------------------


async def seed_licences(keys: list[str]) -> int:
    """Register keys by fingerprint, skipping any already present.

    The keys themselves are not stored -- only ``fingerprint(key)`` -- so this is
    the one place they exist in memory, and the caller is expected to have them
    from ``licenses.generate_keys()`` with a fixed seed or from its own record.

    Idempotent so it can run on every boot: re-seeding must never register a
    second row for a key someone already paid for.
    """
    added = 0
    async with session_scope() as db:
        existing = set((await db.scalars(select(License.fingerprint))).all())
        for key in keys:
            fp = licenses.fingerprint(key)
            if fp in existing:
                continue
            label = "owner" if key == licenses.OWNER_KEY else None
            db.add(License(fingerprint=fp, label=label))
            existing.add(fp)
            added += 1
    if added:
        log.info("registered %d licence fingerprints", added)
    return added


async def find_licence(key: str) -> License | None:
    """Look up a licence by the fingerprint of an already-normalised key.

    Only the fingerprint is compared, because that is all the database holds. The
    lookup is therefore already independent of the key's length and content.
    """
    async with session_scope() as db:
        return await db.get(License, licenses.fingerprint(key))


async def claim_licence(key: str, tg_id: int) -> bool:
    """Bind a licence to a user.

    The UPDATE is guarded on the row still being unclaimed, so two users racing
    on one key produce a single winner instead of both being told yes. A re-claim
    by the same user succeeds: that is someone who lost their session, not a
    second buyer.
    """
    fp = licenses.fingerprint(key)
    now = utcnow()
    async with session_scope() as db:
        result = await db.execute(
            update(License)
            .where(
                License.fingerprint == fp,
                (License.claimed_by.is_(None)) | (License.claimed_by == tg_id),
            )
            .values(claimed_by=tg_id, claimed_at=now)
        )
        if result.rowcount == 0:
            return False

        user = await db.get(User, tg_id)
        if user is None:
            db.add(User(tg_id=tg_id, license_key_fp=fp, licensed_at=now))
        else:
            user.license_key_fp = fp
            user.licensed_at = now
        return True


async def licence_stats() -> tuple[int, int]:
    """(claimed, total), for the owner's overview."""
    async with session_scope() as db:
        total = await db.scalar(select(func.count()).select_from(License))
        claimed = await db.scalar(
            select(func.count())
            .select_from(License)
            .where(License.claimed_by.is_not(None))
        )
    return int(claimed or 0), int(total or 0)


# --- third-party secrets -----------------------------------------------------
# secret_load/secret_save above move opaque blobs. These two wrap them with
# encryption, and are what callers should use: a plaintext key must never reach
# the database, and binding the ciphertext to its owner means a row copied
# between users fails to decrypt instead of being sent to the wrong API.


def _secret_context(tg_id: int, name: str) -> str:
    return f"secret:{tg_id}:{name}"


async def put_secret(tg_id: int, name: str, value: str) -> None:
    blob = secret_box().encrypt(value, context=_secret_context(tg_id, name))
    await secret_save(tg_id, name, blob)


async def get_secret(tg_id: int, name: str) -> str | None:
    blob = await secret_load(tg_id, name)
    if not blob:
        return None
    try:
        return secret_box().decrypt(blob, context=_secret_context(tg_id, name))
    except Exception:
        # Undecryptable means the encryption key changed and the value is
        # unrecoverable. Reported as missing, because asking the user for it
        # again is the only way forward.
        log.error("could not decrypt secret %r for tg_id=%s", name, tg_id)
        return None


async def drop_secret(tg_id: int, name: str) -> None:
    """Forget a stored credential.

    Deletes rather than blanks the row: a user revoking a leaked API key expects
    it gone, and an empty ciphertext would still read as "a secret exists" to
    every caller that checks for presence.
    """
    async with session_scope() as db:
        row = await db.scalar(
            select(Secret).where(Secret.tg_id == tg_id, Secret.name == name)
        )
        if row is not None:
            await db.delete(row)
