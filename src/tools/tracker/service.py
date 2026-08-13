"""Production wiring for gift tracking.

The watcher deliberately knows nothing about SQLAlchemy or aiogram. This module
connects it to saved Ciabattas, user MTProto sessions, the event feed, and bot
messages.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.bot.notifications import buttons_for, render_toast, render_tracker
from src.core import config
from src.core.money import Nano
from src.db import models as db
from src.markets.mrkt.client import MrktClient
from src.mtproto.gifts import GiftReader, UniqueGift
from src.mtproto.sessions import MAIN, SessionStore
from src.tools.tracker.watcher import ALL_STATES, Filters, Found, Watcher, matches

log = logging.getLogger(__name__)


def _load_filters(raw: str | None, max_price_nano: int | None) -> Filters:
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    def names(key: str) -> set[str]:
        value = data.get(key)
        if isinstance(value, str):
            return {value} if value else set()
        if isinstance(value, list):
            return {item for item in value if isinstance(item, str) and item}
        return set()

    states = names("states")
    return Filters(
        collections=names("collection"),
        collections_all=bool(data.get("collection_all")),
        models=names("model"),
        backdrops=names("backdrop"),
        states=states or set(ALL_STATES),
        price_from=Nano.parse(data.get("price_from_nano")),
        price_to=Nano.parse(max_price_nano),
        owner_gifts_min=_optional_int(data.get("owner_gifts_min")),
        owner_gifts_max=_optional_int(data.get("owner_gifts_max")),
        reputation_min=_optional_int(data.get("reputation_min")),
        reputation_max=_optional_int(data.get("reputation_max")),
    )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _price_payload(value: Nano | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"nano": value.value, "ton": value.format_ton(2)}


def _payload(found: Found) -> dict[str, Any]:
    gift = found.gift
    return {
        "collection": gift.collection,
        "number": gift.number,
        "state": gift.state,
        "model": gift.model.name if gift.model else None,
        "backdrop": gift.backdrop.name if gift.backdrop else None,
        "symbol": gift.pattern.name if gift.pattern else None,
        "owner": gift.owner.label,
        "owner_username": gift.owner.username,
        "owner_reputation": gift.owner.reputation_level,
        "owner_gifts": gift.owner.gift_count,
        "telegram_url": gift.telegram_url,
        "chat_url": gift.owner.chat_url,
        "floor_model": _price_payload(found.floor_model),
        "floor_collection": _price_payload(found.floor_collection),
    }


def _keyboard(gift: UniqueGift) -> InlineKeyboardMarkup | None:
    return _keyboard_from_urls(gift.telegram_url, gift.owner.chat_url)


def _keyboard_from_urls(
    telegram_url: str | None, chat_url: str | None
) -> InlineKeyboardMarkup | None:
    rows = buttons_for(telegram_url=telegram_url, chat_url=chat_url)
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button.text, url=button.url) for button in row]
            for row in rows
        ]
    )


@dataclass(slots=True)
class Rule:
    id: int
    tg_id: int
    filters: Filters


class TrackerService:
    """Run one watcher per connected user and deliver matching events."""

    def __init__(self, bot: Bot, session_store: SessionStore):
        self.bot = bot
        self.session_store = session_store
        self._watchers: dict[int, Watcher] = {}
        self._clients: dict[int, Any] = {}
        self._rules: dict[int, list[Rule]] = {}
        self._catalogue: dict[int, list[tuple[str, str]]] = {}
        self._last_sync = 0.0
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()
        for watcher in self._watchers.values():
            watcher.stop()
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        self._watchers.clear()
        self._clients.clear()

    async def run(self) -> None:
        """Synchronise rules and poll until the bot process shuts down."""
        while not self._stop.is_set():
            try:
                if asyncio.get_running_loop().time() - self._last_sync >= 5:
                    await self.sync()

                for tg_id, watcher in tuple(self._watchers.items()):
                    for found in await watcher.poll_once():
                        await self._dispatch(tg_id, found)
            except Exception as exc:  # noqa: BLE001 - the service must stay alive
                log.warning("tracker cycle failed: %s", exc)

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=config.TRACKER_POLL_INTERVAL
                )
            except asyncio.TimeoutError:
                continue

    async def sync(self) -> None:
        """Reload active tracker rules and update per-user catalogues."""
        self._last_sync = asyncio.get_running_loop().time()
        async with db.session_scope() as session:
            rows = (
                await session.scalars(
                    select(db.Ciabatta).where(
                        db.Ciabatta.kind == "tracker", db.Ciabatta.active.is_(True)
                    )
                )
            ).all()

        grouped: dict[int, list[Rule]] = {}
        for row in rows:
            grouped.setdefault(row.tg_id, []).append(
                Rule(row.id, row.tg_id, _load_filters(row.filters_json, row.max_price_nano))
            )
        self._rules = grouped
        await self._retry_pending()

        for tg_id in tuple(self._watchers):
            if tg_id not in grouped:
                await self._drop_watcher(tg_id)
        for tg_id, rules in grouped.items():
            try:
                await self._ensure_watcher(tg_id, rules)
            except Exception as exc:  # noqa: BLE001 - one user must not stop others
                log.warning("could not sync tracker for tg_id=%s: %s", tg_id, exc)

    async def _ensure_watcher(self, tg_id: int, rules: list[Rule]) -> None:
        watcher = self._watchers.get(tg_id)
        if watcher is None:
            client = await self.session_store.client(tg_id, MAIN)
            if client is None:
                log.warning("tracker has no usable Telegram session for tg_id=%s", tg_id)
                return
            reader = GiftReader(client)
            catalogue = await reader.upgradeable_collections()
            watcher = Watcher(
                reader,
                on_found=lambda _: asyncio.sleep(0),
                floors=lambda gift: mrkt_floors(self.session_store, tg_id, gift),
            )
            self._watchers[tg_id] = watcher
            self._clients[tg_id] = client
            self._catalogue[tg_id] = catalogue
            log.info(
                "tracker started for tg_id=%s (%d collections)", tg_id, len(catalogue)
            )

        watch_all = any(
            rule.filters.collections_all or not rule.filters.collections
            for rule in rules
        )
        selected = {
            _fold(name)
            for rule in rules
            for name in rule.filters.collections
        }
        catalogue = self._catalogue[tg_id]
        scope = catalogue if watch_all else [
            item for item in catalogue if _fold(item[0]) in selected
        ]
        watcher.replace_collections(scope)
        await watcher.prime_missing()

    async def _drop_watcher(self, tg_id: int) -> None:
        watcher = self._watchers.pop(tg_id, None)
        if watcher is not None:
            watcher.stop()
        client = self._clients.pop(tg_id, None)
        if client is not None:
            await client.disconnect()
        self._catalogue.pop(tg_id, None)
        log.info("tracker stopped for tg_id=%s", tg_id)

    async def _dispatch(self, tg_id: int, found: Found) -> None:
        for rule in self._rules.get(tg_id, []):
            if not matches(found.gift, rule.filters, price=found.floor_model):
                continue
            await self._deliver(rule, found)

    async def _retry_pending(self) -> None:
        """Retry chat delivery after a transient Telegram/Bot API failure."""
        async with db.session_scope() as session:
            rows = (
                await session.scalars(
                    select(db.Event)
                    .where(
                        db.Event.delivered.is_(False),
                        db.Event.kind.in_(
                            ("gift_upgraded", "gift_crafted", "gift_burned")
                        ),
                    )
                    .order_by(db.Event.created_at)
                    .limit(50)
                )
            ).all()

        for event in rows:
            try:
                payload = json.loads(event.payload_json or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            message = payload.get("notification_html")
            if not isinstance(message, str) or not message:
                continue
            try:
                await self.bot.send_message(
                    event.tg_id,
                    message,
                    reply_markup=_keyboard_from_urls(
                        payload.get("telegram_url"), payload.get("chat_url")
                    ),
                    link_preview_options=LinkPreviewOptions(is_disabled=False),
                )
            except Exception as exc:  # noqa: BLE001 - retry on the next sync
                log.warning(
                    "tracker notification retry failed for tg_id=%s: %s",
                    event.tg_id,
                    exc,
                )
                continue

            async with db.session_scope() as session:
                stored = await session.get(db.Event, event.id)
                if stored is not None:
                    stored.delivered = True

    async def _deliver(self, rule: Rule, found: Found) -> None:
        gift = found.gift
        message = render_tracker(
            collection=gift.collection,
            number=gift.number,
            model=gift.model.name if gift.model else None,
            backdrop=gift.backdrop.name if gift.backdrop else None,
            symbol=gift.pattern.name if gift.pattern else None,
            state=gift.state,
            owner_label=gift.owner.label,
            owner_reputation=gift.owner.reputation_level,
            floor_model=found.floor_model,
            floor_collection=found.floor_collection,
        )
        linked_message = (
            f'{message}\n\n<a href="{html.escape(gift.telegram_url, quote=True)}">'
            "Открыть подарок</a>"
        )
        payload = _payload(found)
        payload["notification_html"] = linked_message
        dedupe_key = f"tracker:{rule.id}:{gift.slug}:{gift.state}"

        try:
            async with db.session_scope() as session:
                event = db.Event(
                    id=time.time_ns(),
                    tg_id=rule.tg_id,
                    ciabatta_id=rule.id,
                    kind=f"gift_{gift.state}",
                    dedupe_key=dedupe_key,
                    gift_slug=gift.slug,
                    price_nano=found.floor_model.value if found.floor_model else None,
                    body=render_toast(
                        collection=gift.collection,
                        number=gift.number,
                        state=gift.state,
                        price=found.floor_model,
                    ),
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    delivered=False,
                )
                session.add(event)
                await session.flush()
                event_id = event.id
        except IntegrityError:
            return

        try:
            await self.bot.send_message(
                rule.tg_id,
                linked_message,
                reply_markup=_keyboard(gift),
                link_preview_options=LinkPreviewOptions(is_disabled=False),
            )
        except Exception as exc:  # noqa: BLE001 - feed still records the event
            log.warning(
                "tracker notification delivery failed for tg_id=%s: %s",
                rule.tg_id,
                exc,
            )
            return

        async with db.session_scope() as session:
            event = await session.get(db.Event, event_id)
            if event is not None:
                event.delivered = True


def _fold(value: str) -> str:
    punctuation = str.maketrans({"’": "'", "‘": "'", "–": "-", "—": "-"})
    return " ".join(value.translate(punctuation).split()).casefold()


async def mrkt_floors(
    session_store: SessionStore, tg_id: int, gift: UniqueGift
) -> tuple[Nano | None, Nano | None]:
    """Read model and collection floors for a tracker notification."""

    async def init_data_source() -> str:
        from src.markets.mrkt.auth import init_data_via_telethon

        client = await session_store.client(tg_id, MAIN)
        if client is None:
            raise RuntimeError("Telegram session is unavailable")
        try:
            return await init_data_via_telethon(client)
        finally:
            await client.disconnect()

    market = MrktClient(init_data_source=init_data_source)
    try:
        model = await market.facet_floor(
            collection=gift.collection,
            model=gift.model.name if gift.model else None,
        )
        collection = await market.facet_floor(collection=gift.collection)
        return model, collection
    finally:
        await market.close()
