"""Reading Telegram unique gifts over MTProto.

Layer 227 is required and not negotiable: the ``burned`` and ``crafted`` flags on
``starGiftUnique`` do not exist below it, and telling a gift that was upgraded
from one consumed in a craft is the whole point of the tracker. Telethon 1.44.0
ships layer 227; GramJS 2.26.22 is on 198 and cannot even parse the response.

There is no push channel for gifts that are not yours. The complete set of
gift-related updates on layer 227 is auction state, auction user state, craft
failure, stars balance and stars revenue -- none of which fires for a stranger's
gift. So discovery is polling, and the cheap primitive is ``availability_issued``:
the current high-water mark of minted numbers for a collection, so one read tells
you how many are new and only the gap needs fetching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.core import config

log = logging.getLogger(__name__)

# Telegram's rarity bands for crafted attributes, lowest to highest.
RARITY_ORDER = ("uncommon", "rare", "epic", "legendary")


def _rarity_from_tl(rarity: Any) -> str | None:
    """Map a StarGiftAttributeRarity constructor onto a band name.

    The TL types are parameterless marker classes, so the class name is the only
    carrier of meaning. Matched by suffix rather than by import, so a band added
    upstream degrades to None instead of raising.
    """
    if rarity is None:
        return None
    name = type(rarity).__name__.lower()
    for band in RARITY_ORDER:
        if name.endswith(band):
            return band
    return None


def _rarity_from_per_mille(per_mille: int | float | None) -> str | None:
    """Band a per-mille rarity. Cutoffs: <=5 legendary, <=15 epic, <=50 rare."""
    if per_mille is None:
        return None
    try:
        value = float(per_mille)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        # 0 marks a crafted attribute, whose band arrives as its own field.
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
class Attribute:
    """One model, backdrop or pattern on a gift."""

    kind: str  # "model" | "backdrop" | "pattern"
    name: str
    rarity: str | None = None
    rarity_per_mille: float | None = None
    crafted: bool = False
    # Backdrops only, packed RGB24. Drives the preview gradient.
    center_color: int | None = None
    edge_color: int | None = None
    # Sticker document id, for fetching the preview asset.
    document_id: int | None = None


@dataclass(slots=True)
class OwnerInfo:
    """Who holds the gift, and how much that tells us.

    The three upstream fields are independent flags, so they are read in order: a
    resolvable peer, then a bare display name, then a TON address. A gift with an
    address and no peer has left Telegram, meaning no chat button and no
    reputation -- calling that "unknown owner" would be wrong, the owner is known
    but unreachable.
    """

    user_id: int | None = None
    username: str | None = None
    display_name: str | None = None
    ton_address: str | None = None
    reputation_level: int | None = None
    gift_count: int | None = None

    @property
    def is_on_blockchain(self) -> bool:
        return self.ton_address is not None and self.user_id is None

    @property
    def is_reachable(self) -> bool:
        """Whether a "message the owner" button can work."""
        return self.user_id is not None or bool(self.username)

    @property
    def label(self) -> str:
        """Best human label available, in Russian for the UI."""
        if self.username:
            return f"@{self.username}"
        if self.display_name:
            return self.display_name
        if self.ton_address:
            return f"TON {self.ton_address[:4]}…{self.ton_address[-4:]}"
        return "скрыт"

    @property
    def chat_url(self) -> str | None:
        if self.username:
            return f"https://t.me/{self.username}"
        if self.user_id is not None:
            return f"tg://user?id={self.user_id}"
        return None


@dataclass(slots=True)
class UniqueGift:
    """A minted unique gift, flattened for display and filtering."""

    slug: str
    title: str
    number: int | None
    gift_id: int | None
    # The two flags the tracker exists to read.
    burned: bool = False
    crafted: bool = False
    craft_chance_per_mille: int | None = None
    model: Attribute | None = None
    backdrop: Attribute | None = None
    pattern: Attribute | None = None
    owner: OwnerInfo = field(default_factory=OwnerInfo)
    availability_issued: int | None = None
    availability_total: int | None = None
    # Telegram's own valuation, when present.
    value_amount: int | None = None
    value_currency: str | None = None
    raw: Any = field(default=None, repr=False)

    @property
    def state(self) -> str:
        """One of "burned", "crafted", "upgraded".

        Burned wins over crafted: a gift that was crafted and later consumed is
        gone, and showing it as a fresh craft would invite a bid on nothing.
        """
        if self.burned:
            return "burned"
        if self.crafted:
            return "crafted"
        return "upgraded"

    @property
    def state_label(self) -> str:
        return {
            "burned": "сгорел в крафте",
            "crafted": "получен крафтом",
            "upgraded": "улучшен",
        }[self.state]

    @property
    def telegram_url(self) -> str:
        return config.GIFT_PAGE_TPL.format(slug=self.slug)

    @property
    def collection(self) -> str:
        """Collection name: the title, which excludes the mint number.

        ``title`` is the human name of the base gift; the number lives in its own
        field, and the slug joins them with a hyphen.
        """
        return self.title


def parse_unique_gift(gift: Any, *, users: dict | None = None) -> UniqueGift:
    """Flatten a ``starGiftUnique`` TL object.

    Attributes are read by class-name suffix rather than isinstance against
    imported TL types, so this module can be imported and unit-tested without
    Telethon present, and an upstream rename degrades to a missing attribute
    instead of an exception mid-poll.
    """
    out = UniqueGift(
        slug=getattr(gift, "slug", "") or "",
        title=getattr(gift, "title", "") or "",
        number=getattr(gift, "num", None),
        gift_id=getattr(gift, "gift_id", None) or getattr(gift, "id", None),
        burned=bool(getattr(gift, "burned", False)),
        crafted=bool(getattr(gift, "crafted", False)),
        craft_chance_per_mille=getattr(gift, "craft_chance_permille", None),
        availability_issued=getattr(gift, "availability_issued", None),
        availability_total=getattr(gift, "availability_total", None),
        value_amount=getattr(gift, "value_amount", None),
        value_currency=getattr(gift, "value_currency", None),
        raw=gift,
    )

    for attr in getattr(gift, "attributes", None) or []:
        cls = type(attr).__name__.lower()
        per_mille = getattr(attr, "rarity_permille", None)
        parsed = Attribute(
            kind="",
            name=getattr(attr, "name", "") or "",
            rarity=_rarity_from_tl(getattr(attr, "rarity", None))
            or _rarity_from_per_mille(per_mille),
            rarity_per_mille=per_mille,
            crafted=bool(getattr(attr, "crafted", False)),
            document_id=getattr(getattr(attr, "document", None), "id", None),
        )
        if "model" in cls:
            parsed.kind = "model"
            out.model = parsed
        elif "backdrop" in cls:
            parsed.kind = "backdrop"
            parsed.center_color = getattr(attr, "center_color", None)
            parsed.edge_color = getattr(attr, "edge_color", None)
            out.backdrop = parsed
        elif "pattern" in cls:
            parsed.kind = "pattern"
            out.pattern = parsed

    out.owner = _parse_owner(gift, users or {})
    return out


def _parse_owner(gift: Any, users: dict) -> OwnerInfo:
    """Resolve the owner from whichever of the three fields is set.

    ``payments.uniqueStarGift`` returns ``users`` and ``chats`` in the same
    response, so a username is usually already in hand and needs no extra request.
    """
    info = OwnerInfo()

    peer = getattr(gift, "owner_id", None)
    if peer is not None:
        uid = (
            getattr(peer, "user_id", None)
            or getattr(peer, "channel_id", None)
            or (peer if isinstance(peer, int) else None)
        )
        if uid is not None:
            info.user_id = int(uid)
            entity = users.get(int(uid))
            if entity is not None:
                info.username = getattr(entity, "username", None)
                first = getattr(entity, "first_name", "") or ""
                last = getattr(entity, "last_name", "") or ""
                info.display_name = (
                    f"{first} {last}".strip() or getattr(entity, "title", None) or None
                )

    if not info.username and not info.display_name:
        info.display_name = getattr(gift, "owner_name", None)
    info.ton_address = getattr(gift, "owner_address", None)
    return info


def slug_for(base_name: str, number: int) -> str:
    """Build a gift slug.

    The separator is a hyphen and the base name keeps its capitalisation with
    spaces removed -- verified against a working implementation rather than
    guessed. ``t.me/nft/<slug>`` resolves to a real preview card.
    """
    compact = "".join((base_name or "").split())
    return config.GIFT_SLUG_TPL.format(base_name=compact, number=number)


class GiftReader:
    """Gift reads against one logged-in Telethon client.

    Wraps the raw requests so callers deal in ``UniqueGift`` rather than TL, and
    so the "no push updates, poll the watermark" strategy lives in one place.
    """

    def __init__(self, client: Any):
        self.client = client

    async def get(self, slug: str) -> UniqueGift | None:
        """Read one gift by slug. ``None`` when it does not exist.

        A missing slug is expected during polling -- the watermark promises a
        number was minted, not that it is fetchable this second -- so the error is
        reported as absence. The literal error name is logged at debug level so it
        can be learned from a live run instead of guessed here.
        """
        from telethon.tl.functions.payments import GetUniqueStarGiftRequest

        try:
            result = await self.client(GetUniqueStarGiftRequest(slug=slug))
        except Exception as exc:  # noqa: BLE001 - the RPC error name is unverified
            log.debug(
                "getUniqueStarGift(%s) failed: %s: %s", slug, type(exc).__name__, exc
            )
            return None

        users = {u.id: u for u in (getattr(result, "users", None) or [])}
        for chat in getattr(result, "chats", None) or []:
            users[chat.id] = chat
        return parse_unique_gift(result.gift, users=users)

    async def watermark(self, slug: str) -> int | None:
        """Current mint high-water mark for this gift's collection.

        One request answers "how many exist now", which is what makes polling
        affordable: without it the only option is blind slug probing.
        """
        gift = await self.get(slug)
        return gift.availability_issued if gift else None

    async def enrich_owner(self, gift: UniqueGift) -> UniqueGift:
        """Add reputation level and gift count to the owner.

        Both come from ``UserFull`` in a single request: ``stars_rating.level`` and
        ``stargifts_count``. The level is a Stars-spending reliability rating, not
        a collection score, and it can be negative -- which is exactly why it is
        worth seeing before messaging someone.

        Failure is non-fatal: a notification without a reputation figure is still
        useful, so the gift comes back unchanged.
        """
        if gift.owner.user_id is None:
            return gift
        try:
            from telethon.tl.functions.users import GetFullUserRequest

            full = await self.client(GetFullUserRequest(id=gift.owner.user_id))
            info = full.full_user
            rating = getattr(info, "stars_rating", None)
            gift.owner.reputation_level = getattr(rating, "level", None)
            gift.owner.gift_count = getattr(info, "stargifts_count", None)
            if not gift.owner.username:
                for user in getattr(full, "users", None) or []:
                    if user.id == gift.owner.user_id:
                        gift.owner.username = getattr(user, "username", None)
                        break
        except Exception as exc:  # noqa: BLE001 - privacy and flood both land here
            log.debug("owner enrich failed for %s: %s", gift.slug, exc)
        return gift
