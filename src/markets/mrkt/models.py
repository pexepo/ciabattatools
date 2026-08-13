"""Typed views over MRKT JSON payloads.

MRKT publishes no API. Every field name here was observed on live responses, so
``raw`` is always retained: a field we do not model yet stays reachable, and a
field that disappears surfaces as ``None`` rather than a KeyError deep inside a
trading decision.

Prices are ``Nano`` throughout. The market speaks integer nanoTON and a float
TON value cannot represent 0.1 exactly, so no float ever holds a price here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.money import Nano

RARITY_BANDS = ("uncommon", "rare", "epic", "legendary")


def norm(name: str | None) -> str:
    """Fold a collection/model/backdrop name for comparison.

    Names arrive with inconsistent case and internal spacing, and they are the
    only join key available -- MRKT filters take human-readable names, not ids.
    """
    return " ".join((name or "").split()).casefold()


def rarity_band(per_mille: float | int | None) -> str | None:
    """Map a per-mille rarity onto a band name.

    Telegram exposes an explicit band only for crafted attributes; everything
    else carries a per-mille figure. Bucketing here keeps one vocabulary in the
    UI instead of two. Cutoffs: <=5 legendary, <=15 epic, <=50 rare, else
    uncommon.
    """
    if per_mille is None:
        return None
    try:
        value = float(per_mille)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        # 0 means "crafted" upstream, where the band arrives as its own field.
        # Reporting "legendary" here would invent a rarity.
        return None
    if value <= 5:
        return "legendary"
    if value <= 15:
        return "epic"
    if value <= 50:
        return "rare"
    return "uncommon"


@dataclass(slots=True)
class Listing:
    """One lot currently on sale."""

    id: str
    # Offers address the *sale*, not the gift. Posting a gift id to
    # /offers/create targets the wrong object without erroring, so the two stay
    # apart even when they coincide.
    sale_id: str
    collection: str
    model: str
    backdrop: str
    symbol: str
    number: int | None
    price: Nano
    floor_collection: Nano | None
    # Floor among lots sharing this backdrop+model. Circular for valuation --
    # it is the floor of the very trait being priced -- so it is displayed but
    # never used as the denominator when judging an overpay.
    floor_backdrop_model: Nano | None
    model_rarity_per_mille: float | None
    backdrop_center: int | None
    backdrop_edge: int | None
    thumb_key: str | None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, g: dict) -> "Listing":
        gid = str(g.get("id") or "")
        sale_id = ""
        for key in ("giftSaleId", "saleId", "gift_sale_id"):
            if g.get(key):
                sale_id = str(g[key])
                break

        number = g.get("number")
        try:
            number = int(number) if number is not None else None
        except (TypeError, ValueError):
            number = None

        # A lot with no readable price is unusable, but raising would kill a
        # whole page over one bad row. Zero is recorded and the spend guards
        # downstream refuse to act on it.
        price = Nano.parse(g.get("salePrice")) or Nano(0)

        return cls(
            id=gid,
            sale_id=sale_id or gid,
            collection=g.get("collectionName") or g.get("name") or "",
            model=g.get("modelName") or "",
            backdrop=g.get("backdropName") or "",
            symbol=g.get("symbolName") or "",
            number=number,
            price=price,
            floor_collection=Nano.parse(g.get("floorPriceNanoTONsByCollection")),
            floor_backdrop_model=Nano.parse(
                g.get("floorPriceNanoTONsByBackdropModel")
            ),
            model_rarity_per_mille=g.get("modelRarityPerMille"),
            backdrop_center=g.get("backdropColorsCenterColor"),
            backdrop_edge=g.get("backdropColorsEdgeColor"),
            thumb_key=g.get("modelStickerThumbnailKey"),
            raw=g,
        )

    @property
    def dedupe_key(self) -> str:
        """Identity for "have we alerted on this already".

        Price is part of the key on purpose: a relist at a new price is a new
        opportunity, not a duplicate.
        """
        return f"{self.id}:{self.price.value}"

    @property
    def model_key(self) -> tuple[str, str]:
        return (norm(self.collection), norm(self.model))

    @property
    def rarity(self) -> str | None:
        return rarity_band(self.model_rarity_per_mille)

    @property
    def backdrop_pair(self) -> tuple[int, int] | None:
        """Center/edge as packed RGB24, or None when unusable.

        Double-zero is a sentinel rather than pure black: a genuinely black
        backdrop renders as 0x0A0A0A or similar, so treating 0,0 as a colour
        would fabricate a gradient.
        """
        c, e = self.backdrop_center, self.backdrop_edge
        if not isinstance(c, int) or not isinstance(e, int):
            return None
        if not (0 <= c <= 0xFFFFFF and 0 <= e <= 0xFFFFFF):
            return None
        if c == 0 and e == 0:
            return None
        return (c, e)

    def discount_vs_floor(self) -> Nano | None:
        """How far under the collection floor this lot sits. None if unknown."""
        if self.floor_collection is None or self.floor_collection.value == 0:
            return None
        return self.floor_collection - self.price


@dataclass(slots=True)
class Collection:
    """Market-level figures for one collection."""

    name: str
    floor: Nano | None
    prev_day_floor: Nano | None
    # Cumulative all-time turnover counter, NOT a 24h figure. Verified
    # monotonically increasing. Only ever used as a difference between two
    # snapshots, never displayed raw as "turnover".
    volume: Nano | None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, c: dict) -> "Collection":
        return cls(
            name=c.get("name") or "",
            floor=Nano.parse(c.get("floorPriceNanoTons")),
            prev_day_floor=Nano.parse(c.get("previousDayFloorPriceNanoTons")),
            volume=Nano.parse(c.get("volume")),
            raw=c,
        )

    @property
    def key(self) -> str:
        return norm(self.name)

    @property
    def floor_change_pct(self) -> float | None:
        """Floor movement over a day. Any movement means trades happened."""
        if not self.floor or not self.prev_day_floor:
            return None
        if self.prev_day_floor.value == 0:
            return None
        pct = (self.floor - self.prev_day_floor).pct_of(self.prev_day_floor)
        return float(pct) if pct is not None else None


@dataclass(slots=True)
class Order:
    """A standing bid on a model or collection, placed below floor.

    This is what the auto-ordering tool competes on: you outbid the top order to
    be first in line when someone sells into the book.

    MRKT exposes no documented order-book endpoint, so ``source`` records how
    the figure was obtained. A number whose provenance is unknown must not
    authorise spending, and ``source`` is what lets a caller tell the difference.
    """

    collection: str
    model: str | None
    price: Nano
    # Orders at or above this price, when the source reports it.
    count: int | None = None
    # "book"    - read from a real order-book endpoint
    # "probe"   - found by endpoint discovery, shape not yet trusted
    # "unknown" - provenance unclear; display only, never spend
    source: str = "unknown"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (norm(self.collection), norm(self.model))

    @property
    def is_trustworthy(self) -> bool:
        """Whether this price may drive a real bid."""
        return self.source == "book" and self.price.value > 0

    def outbid_by(self, step: Nano) -> Nano:
        """The next bid that beats this order."""
        return self.price + step


def top_order(orders: list[Order]) -> Order | None:
    """The highest standing order -- the only one needed to compete.

    The full book is useful for display but not required: to win the queue you
    only have to beat the top. So when the book is unavailable, this is the one
    figure the tool asks for.
    """
    real = [o for o in orders if o.price.value > 0]
    return max(real, key=lambda o: o.price.value) if real else None
