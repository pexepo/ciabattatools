"""Tool 1: tracking newly minted gifts.

Discovery is polling, because layer 227 has no update for gifts that are not
yours. The naive approach -- probe slug after slug until one fails -- costs a
request per guess and earns a FLOOD_WAIT. Instead the mint counter does the work:

    read one known gift  ->  availability_issued = highest number minted
    counter moved by N   ->  fetch exactly those N slugs

A quiet collection costs one request per cycle; a busy one costs one plus the
number of gifts that actually appeared. Nothing is guessed.

The watermark promises a number was minted, not that it is fetchable this instant,
so a miss is retried next pass rather than treated as the end of the collection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from src.core import config
from src.core.money import Nano
from src.mtproto.gifts import GiftReader, UniqueGift, slug_for

log = logging.getLogger(__name__)
_NO_WATERMARK = object()

# Every state a gift can be seen in. Filters default to all three: a tracker that
# silently hid burns would defeat its own purpose.
ALL_STATES = ("upgraded", "crafted", "burned")


def _fold(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


@dataclass(slots=True)
class Filters:
    """What a user wants to hear about.

    An empty set means "all", not "none". That asymmetry is deliberate: a fresh
    filter set should deliver everything, so the user sees the firehose and
    narrows it, rather than configuring filters and wondering why nothing arrives.
    """

    collections: set[str] = field(default_factory=set)
    # Explicit marker from the collection picker's "select all" action. The
    # market catalogue can lag Telegram's upgradeable-gift catalogue, so a list
    # containing every currently visible market collection is not necessarily
    # identical to Telegram's list.
    collections_all: bool = False
    models: set[str] = field(default_factory=set)
    backdrops: set[str] = field(default_factory=set)
    states: set[str] = field(default_factory=lambda: set(ALL_STATES))
    price_from: Nano | None = None
    price_to: Nano | None = None
    owner_gifts_min: int | None = None
    owner_gifts_max: int | None = None
    reputation_min: int | None = None
    reputation_max: int | None = None
    # Notifications without a working owner chat are not actionable. Tracking
    # is intentionally limited to Telegram owners the user can contact.
    reachable_owner_only: bool = True
    # When a price bound is set but no floor is known, drop the gift rather than
    # letting it through: a price filter that ignores unpriced gifts is not a
    # price filter.
    require_price_when_bounded: bool = True

    @staticmethod
    def _fold_all(values: Iterable[str]) -> set[str]:
        return {_fold(v) for v in values if v}

    def normalized(self) -> "Filters":
        return Filters(
            collections=self._fold_all(self.collections),
            collections_all=self.collections_all,
            models=self._fold_all(self.models),
            backdrops=self._fold_all(self.backdrops),
            states=set(self.states or ALL_STATES),
            price_from=self.price_from,
            price_to=self.price_to,
            owner_gifts_min=self.owner_gifts_min,
            owner_gifts_max=self.owner_gifts_max,
            reputation_min=self.reputation_min,
            reputation_max=self.reputation_max,
            reachable_owner_only=self.reachable_owner_only,
            require_price_when_bounded=self.require_price_when_bounded,
        )


def matches(gift: UniqueGift, filters: Filters, *, price: Nano | None = None) -> bool:
    """Whether a gift passes a filter set.

    ``price`` is the figure the bounds apply to -- normally the model floor, since
    that is what a buyer compares against. Passed in rather than read off the gift
    so the caller decides which floor is authoritative.
    """
    f = filters.normalized()

    if gift.state not in f.states:
        return False
    if f.collections and not f.collections_all and _fold(gift.collection) not in f.collections:
        return False
    if f.models and _fold(gift.model.name if gift.model else None) not in f.models:
        return False
    if f.backdrops:
        name = _fold(gift.backdrop.name if gift.backdrop else None)
        if name not in f.backdrops:
            return False

    if f.reachable_owner_only and not gift.owner.is_reachable:
        return False

    if f.price_from is not None or f.price_to is not None:
        if price is None:
            return not f.require_price_when_bounded
        if f.price_from is not None and price < f.price_from:
            return False
        if f.price_to is not None and price > f.price_to:
            return False

    count = gift.owner.gift_count
    if f.owner_gifts_min is not None and (count is None or count < f.owner_gifts_min):
        return False
    if f.owner_gifts_max is not None and (count is None or count > f.owner_gifts_max):
        return False

    if f.reputation_min is not None:
        level = gift.owner.reputation_level
        # Unknown reputation fails a reputation floor: avoiding unrated accounts
        # is precisely what the filter is for.
        if level is None or level < f.reputation_min:
            return False
    if f.reputation_max is not None:
        level = gift.owner.reputation_level
        if level is None or level > f.reputation_max:
            return False

    return True


@dataclass(slots=True)
class Collection:
    """One tracked collection and where its counter last stood."""

    base_name: str
    # Any known slug of this collection, used to read the counter.
    probe_slug: str
    last_issued: int | None = None
    # Consecutive failures, for backing off one collection without stalling the
    # others.
    misses: int = 0


@dataclass(slots=True)
class Found:
    """A gift the tracker decided is worth reporting."""

    gift: UniqueGift
    floor_model: Nano | None
    floor_collection: Nano | None
    at: float


class Watcher:
    """Polls tracked collections and emits new gifts.

    Dependencies arrive as callables so this runs against fakes: no database
    import, no Telegram import, no market import.
    """

    def __init__(
        self,
        reader: GiftReader,
        *,
        on_found: Callable[[Found], Awaitable[None]],
        floors: Callable[
            [UniqueGift], Awaitable[tuple[Nano | None, Nano | None]]
        ] | None = None,
        seen: Callable[[str], Awaitable[bool]] | None = None,
        mark_seen: Callable[[str], Awaitable[None]] | None = None,
        # Cap on new gifts fetched per pass. A collection that jumps by thousands
        # (a mass upgrade event) must not monopolise the loop or trigger a flood
        # wait; the remainder is picked up next pass.
        max_catch_up: int = 25,
    ):
        self.reader = reader
        self.on_found = on_found
        self.floors = floors
        self._seen = seen
        self._mark_seen = mark_seen
        self.max_catch_up = max_catch_up
        self.collections: dict[str, Collection] = {}
        self._stop = asyncio.Event()
        # Dedupe within the process even without a database.
        self._local_seen: set[str] = set()

    def replace_collections(self, collections: Iterable[tuple[str, str]]) -> None:
        """Synchronise the watched catalogue without resetting known counters."""
        wanted: dict[str, tuple[str, str]] = {}
        for base_name, probe_slug in collections:
            if base_name and probe_slug:
                wanted[_fold(base_name)] = (base_name, probe_slug)

        for key in tuple(self.collections):
            if key not in wanted:
                self.collections.pop(key)
        for key, (base_name, probe_slug) in wanted.items():
            current = self.collections.get(key)
            if current is None:
                self.collections[key] = Collection(base_name, probe_slug)
            else:
                current.base_name = base_name
                current.probe_slug = probe_slug

    def track(self, base_name: str, probe_slug: str) -> None:
        key = _fold(base_name)
        if key not in self.collections:
            self.collections[key] = Collection(base_name=base_name, probe_slug=probe_slug)

    def untrack(self, base_name: str) -> None:
        self.collections.pop(_fold(base_name), None)

    def stop(self) -> None:
        self._stop.set()

    async def _already_seen(self, slug: str) -> bool:
        if slug in self._local_seen:
            return True
        if self._seen is not None:
            return await self._seen(slug)
        return False

    async def _remember(self, slug: str) -> None:
        self._local_seen.add(slug)
        if self._mark_seen is not None:
            await self._mark_seen(slug)

    async def prime(self) -> None:
        """Record current counters without emitting anything.

        Called once at startup so a fresh install does not deliver the entire back
        catalogue as "new" -- thousands of notifications and an instant flood wait.
        """
        for entry in self.collections.values():
            issued = await self.reader.watermark(entry.probe_slug)
            if issued is not None:
                entry.last_issued = issued
                log.info("primed %s at #%s", entry.base_name, issued)

    async def prime_missing(self) -> None:
        """Prime only collections added after the watcher started."""
        missing = [
            entry for entry in self.collections.values() if entry.last_issued is None
        ]
        batch_reader = getattr(self.reader, "watermarks", None)
        if missing and callable(batch_reader):
            try:
                watermarks = await batch_reader(
                    (entry.base_name, entry.probe_slug) for entry in missing
                )
            except Exception as exc:  # noqa: BLE001 - fall back to isolated reads
                log.warning("could not batch-prime collections: %s", exc)
            else:
                for entry in missing:
                    issued = watermarks.get(entry.base_name)
                    if issued is not None:
                        entry.last_issued = issued
                        log.info("primed %s at #%s", entry.base_name, issued)
                missing = [entry for entry in missing if entry.last_issued is None]

        for entry in missing:
            if entry.last_issued is not None:
                continue
            try:
                issued = await self.reader.watermark(entry.probe_slug)
            except Exception as exc:  # noqa: BLE001 - one collection is isolated
                log.warning("could not prime %s: %s", entry.base_name, exc)
                continue
            if issued is not None:
                entry.last_issued = issued
                log.info("primed %s at #%s", entry.base_name, issued)

    async def poll_once(self) -> list[Found]:
        """One pass over every tracked collection."""
        found: list[Found] = []
        entries = list(self.collections.values())
        watermarks: dict[str, int] | None = None
        batch_reader = getattr(self.reader, "watermarks", None)
        if entries and callable(batch_reader):
            try:
                watermarks = await batch_reader(
                    (entry.base_name, entry.probe_slug) for entry in entries
                )
            except Exception as exc:  # noqa: BLE001 - sequential fallback below
                log.warning("could not batch-read collection counters: %s", exc)

        for entry in entries:
            try:
                issued = (
                    watermarks.get(entry.base_name)
                    if watermarks is not None
                    else _NO_WATERMARK
                )
                found.extend(await self._poll_collection(entry, issued=issued))
            except Exception as exc:  # noqa: BLE001 - one bad collection must not
                # stop the others; this loop is the product's heartbeat.
                entry.misses += 1
                log.warning(
                    "poll failed for %s (%s misses): %s",
                    entry.base_name, entry.misses, exc,
                )
        return found

    async def _poll_collection(
        self, entry: Collection, *, issued: int | None | object = _NO_WATERMARK
    ) -> list[Found]:
        if issued is _NO_WATERMARK:
            issued = await self.reader.watermark(entry.probe_slug)
        if issued is None:
            entry.misses += 1
            return []
        entry.misses = 0

        if entry.last_issued is None:
            # First sight of this collection: adopt the counter, emit nothing.
            entry.last_issued = issued
            return []
        if issued <= entry.last_issued:
            return []

        first = entry.last_issued + 1
        last = min(issued, entry.last_issued + self.max_catch_up)
        if last < issued:
            log.info(
                "%s jumped %s->%s; taking %s this pass",
                entry.base_name, entry.last_issued, issued, self.max_catch_up,
            )

        out: list[Found] = []
        for number in range(first, last + 1):
            slug = slug_for(entry.base_name, number)
            if await self._already_seen(slug):
                entry.last_issued = number
                continue
            gift = await self.reader.get(slug)
            if gift is None:
                # Minted but not readable yet. Leave the counter behind so the
                # next pass retries instead of skipping the gift forever.
                log.debug("%s not readable yet", slug)
                break
            await self._remember(slug)
            gift = await self.reader.enrich_owner(gift)

            floor_model = floor_collection = None
            if self.floors is not None:
                try:
                    floor_model, floor_collection = await self.floors(gift)
                except Exception as exc:  # noqa: BLE001 - a failing price source
                    # must not drop the notification; "н/д" is honest.
                    log.debug("floor lookup failed for %s: %s", slug, exc)

            out.append(
                Found(
                    gift=gift,
                    floor_model=floor_model,
                    floor_collection=floor_collection,
                    at=time.time(),
                )
            )
            entry.last_issued = number

        return out

    async def run(self, interval: float | None = None) -> None:
        """Poll until stopped, backing off on repeated failure.

        Backoff is per-loop rather than per-collection: when Telegram is unhappy it
        is unhappy with the account, so slowing one collection while hammering the
        rest would not help.
        """
        delay = interval or config.TRACKER_POLL_INTERVAL
        backoff = delay
        while not self._stop.is_set():
            try:
                for item in await self.poll_once():
                    await self.on_found(item)
                backoff = delay
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                backoff = min(backoff * 2, config.TRACKER_POLL_BACKOFF_MAX)
                log.warning("tracker cycle failed, backing off %.0fs: %s", backoff, exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                continue
