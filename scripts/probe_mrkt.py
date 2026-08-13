"""Discover what MRKT actually exposes: python scripts/probe_mrkt.py

MRKT publishes no API documentation, so every integration detail in this project
was found by observation. This script does the observing in one pass, instead of
learning it from production 400s one at a time.

It answers three open questions:

1. **What does `/gifts/saling` want?** A recent 400 reported a required `req`
   field that no prior version of this client sent. The probe posts several body
   shapes and reports which ones the server accepts.
2. **Is there an order book?** Auto-ordering needs the highest standing order, and
   no documented endpoint exists -- so candidates are tried and the reachable ones
   reported.
3. **Can models and backdrops be listed?** The filter screen needs them for
   multi-select. Failing that, they must be accumulated from listings.

Read-only: every request is a GET or a search-shaped POST. Nothing here buys,
bids, or cancels. It authenticates as you, so run it with a live session.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import config  # noqa: E402
from src.db import models as db  # noqa: E402
from src.markets.mrkt.auth import init_data_via_telethon  # noqa: E402
from src.markets.mrkt.client import MrktClient  # noqa: E402
from src.mtproto.sessions import MAIN, SessionStore  # noqa: E402

OK = "\033[32m"
BAD = "\033[31m"
DIM = "\033[2m"
OFF = "\033[0m"

# Body shapes for /gifts/saling, most likely first. The first is what the client
# sends today; the rest test the `req` field the server asked for, whose shape is
# unknown -- a wrapper, a nested object, or a scalar.
SEARCH_SHAPES: list[tuple[str, dict]] = [
    ("тело как сейчас", {}),
    ("req как пустой объект", {"req": {}}),
    ("req обёртывает фильтры", {"__wrap__": "req"}),
    ("req как строка", {"req": ""}),
    ("req как номер страницы", {"req": 0}),
]

FACET_CANDIDATES: list[tuple[str, str]] = [
    ("GET", "/gifts/attributes"),
    ("GET", "/gifts/filters"),
    ("GET", "/gifts/models"),
    ("GET", "/gifts/backdrops"),
    ("GET", "/gifts/collections/filters"),
    ("POST", "/gifts/attributes"),
    ("GET", "/attributes"),
    ("GET", "/filters"),
]

# Bodies to try once a path is known to exist but rejects an empty one. Ordered
# by how MRKT shapes its other endpoints: plural name arrays, a count, a cursor.
BODY_SHAPES: list[tuple[str, dict]] = [
    ("пустое", {}),
    ("collectionName", {"collectionName": "Plush Pepe"}),
    ("collectionNames", {"collectionNames": ["Plush Pepe"]}),
    (
        "как поиск",
        {
            "collectionNames": [],
            "modelNames": [],
            "backdropNames": [],
            "count": 5,
            "cursor": "",
        },
    ),
    ("count+cursor", {"count": 5, "cursor": ""}),
    ("ordering как в поиске", {"ordering": "Price", "lowToHigh": True, "count": 5}),
    # /orders answered to the search-shaped body, so the facet endpoints are worth
    # trying with the same vocabulary plus a collection, which is the one thing a
    # model list would plausibly be scoped by.
    (
        "как поиск + collectionNames",
        {
            "collectionNames": ["Plush Pepe"],
            "modelNames": [],
            "backdropNames": [],
            "count": 50,
            "cursor": "",
        },
    ),
    ("collectionTitle", {"collectionTitle": "Plush Pepe"}),
    ("collectionTitles", {"collectionTitles": ["Plush Pepe"]}),
]


def _summarise(data) -> str:
    """One line describing a successful response.

    Shape matters more than content here: whether it is a list or an object, and
    what the keys are called, is what determines how it gets parsed.
    """
    if isinstance(data, list):
        head = data[0] if data else None
        if isinstance(head, dict):
            return f"список из {len(data)}, поля: {sorted(head)[:8]}"
        if head is not None:
            return f"список из {len(data)}, например {head!r}"
        return "пустой список"
    if isinstance(data, dict):
        keys = [k for k in sorted(data) if k != "__status__"]
        for key in keys:
            value = data[key]
            if isinstance(value, list) and value:
                inner = value[0]
                shape = sorted(inner)[:8] if isinstance(inner, dict) else repr(inner)[:40]
                return f"объект, в {key!r} список из {len(value)}: {shape}"
        return f"объект, поля: {keys[:10]}"
    return repr(data)[:80]


async def try_path(client: MrktClient, method: str, path: str, body: dict | None = None):
    """One request, returning (status, data) and never raising.

    Discovery has to survive its own failures: a path that times out must not stop
    the rest of the sweep.
    """
    try:
        data = await client._request(
            method, path, json=body if method == "POST" else None, raise_for_status=False
        )
        return _status_of(data), data
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {str(exc)[:90]}"


async def explore(client: MrktClient, method: str, path: str) -> None:
    """Report one path, following up on what the status implies.

    405 and 400 are not failures -- they are the server confirming the route
    exists. 405 means the verb is wrong, so the other one is tried; 400 means the
    body is wrong, so several shapes are tried. Treating both as "no" is how a
    working endpoint gets written off as missing.
    """
    status, data = await try_path(client, method, path, {} if method == "POST" else None)

    if status == 200:
        print(f"  {OK}   200{OFF} {method} {path} — {_summarise(data)}")
        return

    if status == 404:
        print(f"  {DIM}   404 {method} {path}{OFF}")
        return

    if status == -1:
        print(f"  {BAD}ошибка{OFF} {method} {path}: {data}")
        return

    if status == 405:
        other = "POST" if method == "GET" else "GET"
        print(f"  {DIM}   405 {method} {path} — путь есть, пробую {other}{OFF}")
        alt_status, alt_data = await try_path(
            client, other, path, {} if other == "POST" else None
        )
        if alt_status == 200:
            print(f"  {OK}   200{OFF} {other} {path} — {_summarise(alt_data)}")
            return
        if alt_status == 400 and other == "POST":
            await probe_bodies(client, path)
            return
        print(f"  {BAD}{alt_status:>6}{OFF} {other} {path}")
        return

    if status == 400 and method == "POST":
        print(f"  {DIM}   400 POST {path} — путь есть, подбираю тело{OFF}")
        await probe_bodies(client, path)
        return

    print(f"  {BAD}{status:>6}{OFF} {method} {path}")


async def probe_bodies(client: MrktClient, path: str) -> None:
    """Try body shapes against a path that exists but rejected an empty one."""
    for label, body in BODY_SHAPES:
        status, data = await try_path(client, "POST", path, body)
        if status == 200:
            print(f"    {OK}200{OFF} тело «{label}» — {_summarise(data)}")
            # Keep going: a later shape may return more, and knowing which are
            # accepted is more useful than stopping at the first.
            continue
        if status == -1:
            print(f"    {BAD}ошибка{OFF} тело «{label}»: {data}")
            continue
        detail = ""
        if isinstance(data, dict):
            # The validation message names the missing or wrong field, which is
            # the specification MRKT does not publish. __text__ is the fallback
            # for a non-JSON error body.
            text = str(
                data.get("errors") or data.get("title") or data.get("__text__") or ""
            )[:200]
            detail = f"\n         {DIM}{text}{OFF}" if text else ""
        print(f"    {BAD}{status}{OFF} тело «{label}»{detail}")



def base_body() -> dict:
    """The search body the client sends today, trimmed to one row."""
    from src.markets.mrkt.client import SEARCH_DEFAULTS

    return {**SEARCH_DEFAULTS, "count": 1}


async def client_for(tg_id: int) -> MrktClient:
    store = SessionStore(
        load=db.session_load, save=db.session_save, delete=db.session_delete
    )

    probe = await store.client(tg_id, MAIN)
    if probe is None:
        raise SystemExit(
            f"Нет сессии Telegram для tg_id={tg_id}.\n"
            "Открой бота, нажми «Подключить аккаунт», потом запусти снова."
        )
    await probe.disconnect()

    async def init_data_source() -> str:
        # Reconnected per call and closed after: a long-lived MTProto connection
        # held open by a probe script is a session Telegram may decide to drop.
        c = await store.client(tg_id, MAIN)
        if c is None:
            raise SystemExit("сессия исчезла на середине проверки")
        try:
            return await init_data_via_telethon(c)
        finally:
            await c.disconnect()

    return MrktClient(init_data_source=init_data_source)


def _status_of(data) -> int:
    """Extract the status from a raise_for_status=False response."""
    return data.get("__status__", 200) if isinstance(data, dict) else 200


async def probe_search(client: MrktClient) -> None:
    print("\n\033[1m1. Что принимает /gifts/saling\033[0m")
    for label, extra in SEARCH_SHAPES:
        body = base_body()
        if extra.get("__wrap__") == "req":
            body = {"req": body}
        else:
            body.update(extra)

        try:
            data = await client._request(
                "POST", "/gifts/saling", json=body, raise_for_status=False
            )
        except Exception as exc:
            print(f"  {BAD}ошибка{OFF} {label}: {type(exc).__name__}: {str(exc)[:100]}")
            continue

        status = _status_of(data)
        if status != 200:
            print(f"  {BAD}{status:>6}{OFF} {label}")
            continue

        rows = data.get("gifts") if isinstance(data, dict) else None
        count = len(rows) if isinstance(rows, list) else "?"
        print(f"  {OK}   200{OFF} {label} — подарков: {count}")
        if isinstance(rows, list) and rows:
            # The field names are the real prize: Listing.parse reads them, and a
            # rename upstream becomes a silent None downstream.
            print(f"         {DIM}поля: {sorted(rows[0])[:12]}{OFF}")


async def probe_collections(client: MrktClient) -> None:
    print("\n\033[1m2. Коллекции\033[0m")
    try:
        rows = await client.collections()
    except Exception as exc:
        print(f"  {BAD}ошибка{OFF}: {type(exc).__name__}: {str(exc)[:120]}")
        return
    print(f"  коллекций: {len(rows)}")
    for c in rows[:5]:
        floor = c.floor.format_ton(2) if c.floor else "—"
        print(f"    {c.name[:34]:36} флор {floor}")
    if len(rows) > 5:
        print(f"    {DIM}… и ещё {len(rows) - 5}{OFF}")


async def probe_facets(client: MrktClient) -> None:
    print("\n\033[1m3. Списки моделей и фонов\033[0m")
    print(f"{DIM}  405 = путь есть, метод не тот. 400 = путь есть, тело не то.{OFF}")
    for method, path in FACET_CANDIDATES:
        await explore(client, method, path)


async def probe_orders(client: MrktClient) -> None:
    """Order-book candidates, with the same follow-up treatment.

    The earlier sweep reported ``POST /orders`` as 400 and ``GET /orders`` as 405,
    which together say the route exists and takes a POST -- only the body was
    wrong. That is a lead, not a dead end.
    """
    print("\n\033[1m4. Стакан ордеров\033[0m")
    for method, path in (
        ("POST", "/orders"),
        ("GET", "/orders/search"),
        ("POST", "/orders/search"),
        ("GET", "/orders/market"),
        ("POST", "/gifts/orders"),
        ("GET", "/offers"),
        ("POST", "/offers/search"),
    ):
        await explore(client, method, path)

    # The order book is what auto-ordering is built on, so its full row shape is
    # dumped rather than summarised: every field is a decision the tool may need to
    # make, and a truncated list means guessing at the rest later.
    print("\n\033[1m5. Поля одной заявки\033[0m")
    status, data = await try_path(
        client,
        "POST",
        "/orders",
        {"collectionNames": [], "modelNames": [], "backdropNames": [], "count": 3, "cursor": ""},
    )
    if status != 200 or not isinstance(data, dict):
        print(f"  {BAD}не удалось получить заявки{OFF} (статус {status})")
        return

    orders = data.get("orders")
    if not isinstance(orders, list) or not orders:
        print(f"  {DIM}список пуст — на маркете сейчас нет активных ордеров{OFF}")
        print(f"  {DIM}ответ: {sorted(k for k in data if k != '__status__')}{OFF}")
        return

    print(f"  ключи ответа: {sorted(k for k in data if k != '__status__')}")
    row = orders[0]
    for key in sorted(row):
        value = row[key]
        shown = repr(value)
        if len(shown) > 60:
            shown = shown[:60] + "…"
        print(f"    {key:28} {shown}")


async def main() -> int:
    tg_id = config.OWNER_TG_ID
    if not tg_id:
        raise SystemExit("Задай OWNER_TG_ID в .env — запросы идут от его сессии.")

    print("\n\033[1mMRKT — что доступно\033[0m")
    print(f"{DIM}Только чтение: ничего не покупается и не отменяется.{OFF}")

    client = await client_for(tg_id)
    try:
        # Search first: if the body shape is wrong nothing else matters, and the
        # report should say so before the rest of the output.
        await probe_search(client)
        await probe_collections(client)
        await probe_facets(client)
        await probe_orders(client)
    finally:
        await client.close()

    print(
        f"\n{DIM}Пришли этот вывод — по нему станет ясно, какое тело запроса\n"
        f"ждёт MRKT и что можно использовать для фильтров.{OFF}"
    )
    return 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    raise SystemExit(asyncio.run(main()))
