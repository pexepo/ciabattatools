"""Notification rendering.

One template family, three densities: a line in the bot chat, a collapsed toast in
the mini app, and an expanded card on tap. Nothing is ever attached -- the
``t.me/nft/<slug>`` link renders its own preview card, so an upload would only
duplicate it and slow the message down.

Two rules shape every function here:

* An unknown value is written as "н/д", never as 0 and never silently omitted. A
  missing floor rendered as "0 TON" reads as "free" at a glance, and these
  messages get read fast, during a purchase decision.
* HTML is escaped at every interpolation. Collection and model names come from an
  API and owner display names come from strangers; one unescaped "<" turns a
  notification into a parse error and the alert is simply lost.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Iterable

from src.core.money import Nano

UNKNOWN = "н/д"

# Premium emoji degrade to ordinary emoji for users without Premium, so they are
# safe unconditionally. One per line at most: a wall of them reads as spam, which
# is the opposite of premium.
ICON = {
    "upgraded": "✨",
    "burned": "🔥",
    "crafted": "⚗️",
    "found": "👁",
    "bought": "✅",
    "offered": "🤝",
    "order": "📌",
}


def esc(value: object) -> str:
    """Escape for Telegram HTML, rendering absence as the unknown mark."""
    if value is None or value == "":
        return UNKNOWN
    return html.escape(str(value), quote=False)


def ton(amount: Nano | None) -> str:
    """Format a price, or the unknown mark. Zero is a real price and is shown."""
    return f"{amount.format_ton()} TON" if amount is not None else UNKNOWN


def _line(label: str, value: str) -> str:
    return f"{label}: <b>{value}</b>"


def reputation_label(level: int | None) -> str:
    """Describe a Stars reliability level.

    The level can be negative, and Telegram documents that as a reason for
    concern -- so a negative value is called out in words rather than left as a
    bare number the reader has to interpret mid-trade.
    """
    if level is None:
        return UNKNOWN
    if level < 0:
        return f"{level} ⚠️ отрицательная"
    if level == 0:
        return "0 — нет истории"
    return str(level)


@dataclass(slots=True)
class Button:
    """One inline button. URL only -- notifications carry no callback buttons."""

    text: str
    url: str


def _attributes_block(
    model: str | None, backdrop: str | None, symbol: str | None
) -> list[str]:
    return [
        _line("Модель", esc(model)),
        _line("Фон", esc(backdrop)),
        _line("Узор", esc(symbol)),
    ]


def render_tracker(
    *,
    collection: str,
    number: int | None,
    model: str | None,
    backdrop: str | None,
    symbol: str | None,
    state: str,
    owner_label: str | None,
    owner_reputation: int | None,
    floor_model: Nano | None,
    floor_collection: Nano | None,
) -> str:
    """Tool 1: a newly seen gift.

    The status line comes first because it is the tool's whole point: upgraded
    versus consumed-in-a-craft changes what the reader should do, and burying it
    under attributes hides the one fact they came for.
    """
    icon = ICON.get(state, ICON["upgraded"])
    state_text = {
        "burned": "сгорел в крафте",
        "crafted": "получен крафтом",
        "upgraded": "улучшен",
    }.get(state, state)

    head = f"{icon} <b>{esc(collection)}</b>"
    if number is not None:
        head += f" <code>#{number}</code>"

    return join_nonempty([
        head,
        _line("Статус", state_text),
        "",
        *_attributes_block(model, backdrop, symbol),
        "",
        _line("Владелец", esc(owner_label)),
        _line("Репутация", reputation_label(owner_reputation)),
        "",
        _line("Флор модели", ton(floor_model)),
        _line("Флор коллекции", ton(floor_collection)),
    ])


def render_order_catch(
    *,
    collection: str,
    number: int | None,
    model: str | None,
    backdrop: str | None,
    symbol: str | None,
    price: Nano | None,
    owner_gift_count: int | None,
    estimate: Nano | None,
    telegram_url: str,
) -> str:
    """Tool 2: an order filled.

    Per the spec this drops the owner line and instead reports how many NFT gifts
    the owner holds counting this one, plus an estimate. The Telegram link sits in
    the body as well as on a button, because the body link is what produces the
    preview card.
    """
    title = f"<b>{esc(collection)}</b>"
    if number is not None:
        title += f" <code>#{number}</code>"

    return join_nonempty([
        f"{ICON['order']} <b>Ордер сработал</b>",
        "",
        f'<a href="{html.escape(telegram_url, quote=True)}">{title}</a>',
        _line("Куплено за", ton(price)),
        "",
        *_attributes_block(model, backdrop, symbol),
        "",
        _line(
            "Подарков у владельца",
            str(owner_gift_count) if owner_gift_count is not None else UNKNOWN,
        ),
        _line("Примерная цена", ton(estimate)),
    ])


def render_snipe(
    *,
    kind: str,
    collection: str,
    number: int | None,
    model: str | None,
    backdrop: str | None,
    symbol: str | None,
    price: Nano | None,
    telegram_url: str,
    dry_run: bool = False,
) -> str:
    """Tool 3, three forms, wording exactly as specified.

    ``bought``  -> "Вы успешно купили X за Y"
    ``found``   -> "Найден X за Y"            (no auto-buy, notify only)
    ``offered`` -> "Вы предложили X за Y"

    In dry-run the message is marked as a simulation. A simulated catch that reads
    identically to a real purchase is worse than no simulation at all.
    """
    label = f"<b>{esc(collection)}</b>"
    if number is not None:
        label += f" <code>#{number}</code>"
    linked = f'<a href="{html.escape(telegram_url, quote=True)}">{label}</a>'
    amount = ton(price)

    if kind == "bought":
        head = f"{ICON['bought']} Вы успешно купили {linked} за <b>{amount}</b>"
    elif kind == "offered":
        head = f"{ICON['offered']} Вы предложили {linked} за <b>{amount}</b>"
    elif kind == "found":
        head = f"{ICON['found']} Найден {linked} за <b>{amount}</b>"
    else:
        raise ValueError(f"unknown snipe notification kind: {kind!r}")

    if dry_run:
        head = f"🧪 <i>Симуляция</i>\n{head}"

    return join_nonempty([head, "", *_attributes_block(model, backdrop, symbol)])


def buttons_for(
    *,
    telegram_url: str | None = None,
    chat_url: str | None = None,
    deal_url: str | None = None,
    offer_url: str | None = None,
) -> list[list[Button]]:
    """Inline keyboard rows, skipping anything unavailable.

    A button that cannot work is worse than a missing one: "message the owner" on
    a gift held by a TON address leads nowhere, so it is omitted rather than shown
    dead.
    """
    rows: list[list[Button]] = []

    first: list[Button] = []
    if telegram_url:
        first.append(Button("Открыть подарок", telegram_url))
    if chat_url:
        first.append(Button("Написать владельцу", chat_url))
    if first:
        rows.append(first)

    second: list[Button] = []
    if deal_url:
        second.append(Button("Посмотреть сделку", deal_url))
    if offer_url:
        second.append(Button("Поставить оффер", offer_url))
    if second:
        rows.append(second)
    return rows


def render_toast(
    *,
    collection: str,
    number: int | None,
    state: str,
    price: Nano | None = None,
) -> str:
    """The collapsed one-line form used in the mini app feed.

    Same facts, fewer of them: the expanded card carries the rest, so this stays
    scannable in a fast-moving list.
    """
    icon = ICON.get(state, ICON["upgraded"])
    num = f" #{number}" if number is not None else ""
    tail = f" · {ton(price)}" if price is not None else ""
    return f"{icon} {collection}{num}{tail}"


def join_nonempty(lines: Iterable[str]) -> str:
    """Join lines, collapsing blank runs left behind by omitted sections."""
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)
