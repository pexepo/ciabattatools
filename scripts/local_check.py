"""Local end-to-end check: python scripts/local_check.py

Exercises the real application objects -- the FastAPI app, the ORM, the initData
verifier -- against the real database, without needing a listening socket.

Why it exists: the sandbox this was developed in blocks both binding a port and
installing packages, so ``uvicorn`` cannot start and ``aiosqlite`` cannot be
installed. Neither limitation affects the application, and neither should stop
the code being verified before deploy. Two workarounds, both confined to this
file:

1. Requests are dispatched straight into the ASGI app -- which is what an HTTP
   server does once it has parsed a request. Every layer above the socket (auth,
   routing, serialisation) runs exactly as in production.
2. A minimal ``aiosqlite`` shim, registered before SQLAlchemy imports it. It
   wraps the stdlib ``sqlite3`` in a worker thread, which is what the real
   package does.

Not a substitute for the test suite, and never imported at runtime: this answers
one question, "would this boot and serve".
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# --- aiosqlite shim ----------------------------------------------------------


def _install_aiosqlite_shim() -> bool:
    """Provide a minimal ``aiosqlite`` when the real one is absent.

    Returns True if the shim was installed. SQLAlchemy's aiosqlite dialect needs
    a small surface: a connection with awaitable execute/commit/close, and a
    cursor. Everything runs in a thread, because sqlite3 blocks.
    """
    try:
        import aiosqlite  # noqa: F401

        return False
    except ModuleNotFoundError:
        pass

    import sqlite3
    import types

    class _Cursor:
        def __init__(self, cursor):
            self._c = cursor

        def __getattr__(self, name):
            return getattr(self._c, name)

        async def execute(self, sql, params=None):
            await asyncio.to_thread(self._c.execute, sql, params or [])
            return self

        async def executemany(self, sql, params):
            await asyncio.to_thread(self._c.executemany, sql, params)
            return self

        async def fetchone(self):
            return await asyncio.to_thread(self._c.fetchone)

        async def fetchall(self):
            return await asyncio.to_thread(self._c.fetchall)

        async def fetchmany(self, size=1):
            return await asyncio.to_thread(self._c.fetchmany, size)

        async def close(self):
            await asyncio.to_thread(self._c.close)

    class _Connection:
        def __init__(self, conn):
            self._conn = conn
            # The dialect sets `connection._thread.daemon = True`, because the
            # real aiosqlite drives sqlite3 from a dedicated thread. This shim
            # uses asyncio.to_thread instead, so there is no thread to mark --
            # but the attribute has to exist or connect() raises.
            self._thread = types.SimpleNamespace(daemon=False)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        # The dialect awaits the object returned by connect(), so this must
        # resolve to itself rather than to a coroutine result.
        def __await__(self):
            async def _self():
                return self

            return _self().__await__()

        async def cursor(self):
            return _Cursor(await asyncio.to_thread(self._conn.cursor))

        async def execute(self, sql, params=None):
            cur = await asyncio.to_thread(self._conn.execute, sql, params or [])
            return _Cursor(cur)

        async def commit(self):
            await asyncio.to_thread(self._conn.commit)

        async def rollback(self):
            await asyncio.to_thread(self._conn.rollback)

        async def close(self):
            await asyncio.to_thread(self._conn.close)

        # The dialect awaits these, so a plain passthrough returning None would
        # raise "NoneType can't be awaited". They are cheap and synchronous
        # underneath, but the signature has to be a coroutine.
        async def create_function(self, *args, **kwargs):
            # SQLAlchemy passes deterministic=True on newer Pythons; sqlite3
            # accepts it, so it is forwarded untouched.
            return self._conn.create_function(*args, **kwargs)

        async def executescript(self, sql):
            return _Cursor(await asyncio.to_thread(self._conn.executescript, sql))

        async def set_progress_handler(self, handler, n):
            return self._conn.set_progress_handler(handler, n)

    def connect(database, **kwargs):
        # check_same_thread=False because to_thread uses whichever worker is
        # free, not one fixed thread.
        kwargs.setdefault("check_same_thread", False)
        kwargs.pop("iter_chunk_size", None)
        return _Connection(sqlite3.connect(database, **kwargs))

    m = types.ModuleType("aiosqlite")
    m.connect = connect
    # Both spellings: the dialect's _init_dbapi_attributes reads
    # sqlite_version_info as well, and a missing one fails at engine creation.
    m.sqlite_version = sqlite3.sqlite_version
    m.sqlite_version_info = sqlite3.sqlite_version_info
    m.paramstyle = "qmark"
    for name in (
        "Error",
        "Warning",
        "DatabaseError",
        "IntegrityError",
        "OperationalError",
        "ProgrammingError",
        "InterfaceError",
        "NotSupportedError",
        "InternalError",
        "DataError",
        "Binary",
        "Row",
        "register_adapter",
        "register_converter",
        "PARSE_DECLTYPES",
        "PARSE_COLNAMES",
    ):
        setattr(m, name, getattr(sqlite3, name))
    m.Cursor = _Cursor
    m.Connection = _Connection

    class _DBAPI:
        sqlite_version = sqlite3.sqlite_version
        paramstyle = "qmark"

    m.dbapi = _DBAPI()
    sys.modules["aiosqlite"] = m
    return True


SHIMMED = _install_aiosqlite_shim()

from src.core import config, licenses  # noqa: E402
from src.db import models as db  # noqa: E402

PASS = "\033[32mOK\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label))
    print(f"  [{PASS if ok else FAIL}] {label}{f' -- {detail}' if detail else ''}")
    return bool(ok)


# --- ASGI transport ----------------------------------------------------------


async def call(app, method: str, path: str, *, headers=None, body=None, query=b""):
    """Dispatch one request into the ASGI app and collect the response."""
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body is not None:
        hdrs.append((b"content-type", b"application/json"))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query if isinstance(query, bytes) else query.encode(),
        "headers": hdrs,
        "root_path": "",
        "scheme": "http",
        "server": ("127.0.0.1", 8080),
        "client": ("127.0.0.1", 50000),
    }

    chunks: list[bytes] = []
    status: dict = {}
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)

    text = b"".join(chunks).decode("utf-8", "replace")
    payload = None
    if text[:1] in ("{", "["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    return status.get("code"), payload, text


def sign_init_data(tg_id: int, username: str, token: str) -> str:
    """Build a payload signed exactly the way Telegram signs one."""
    fields = {
        "user": json.dumps({"id": tg_id, "username": username}),
        "auth_date": str(int(time.time())),
        "query_id": "AAHtest",
    }
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


# --- checks ------------------------------------------------------------------


async def main() -> int:
    print("\n\033[1mCiabatta Tools -- локальная проверка\033[0m")
    if SHIMMED:
        print("  (aiosqlite не установлен -- используется встроенная замена)")

    print("\n\033[1m1. Конфигурация\033[0m")
    missing = [
        n
        for n in ("BOT_TOKEN", "TG_API_ID", "TG_API_HASH", "SECRET_KEY", "PUBLIC_URL")
        if not getattr(config, n, None)
    ]
    if not check(not missing, "переменные окружения", ", ".join(missing)):
        print("\n  Заполни .env по образцу .env.example и запусти снова.")
        return 1

    check(config.PUBLIC_URL.startswith("https://"), "PUBLIC_URL по https")
    check(bool(config.DATABASE_URL), "DATABASE_URL определён")
    check(
        config.DRY_RUN,
        "DRY_RUN включён",
        "" if config.DRY_RUN else "боевой режим -- траты реальные",
    )
    check(config.SUPPORT_CONTACT == "@awhoreable", "контакт поддержки")

    print("\n\033[1m2. База данных\033[0m")
    await db.init_models()
    check(True, "схема создана или уже существует")

    claimed, total = await db.licence_stats()
    if total == 0:
        await db.seed_licences(licenses.derive_keys(config.SECRET_KEY))
        claimed, total = await db.licence_stats()
    check(total == 101, "ключей 101 (100 + владелец)", f"найдено {total}")

    check(await db.find_licence(licenses.OWNER_KEY) is not None, "ключ PEXEPO зарегистрирован")
    check(
        await db.find_licence(licenses.normalize("pexepo")) is not None,
        "ключ распознаётся в нижнем регистре",
    )

    print("\n\033[1m3. Шифрование\033[0m")
    tg_id = config.OWNER_TG_ID or 1
    await db.put_secret(tg_id, "gift_satellite", "probe-value-123")
    check(
        await db.get_secret(tg_id, "gift_satellite") == "probe-value-123",
        "секрет шифруется и читается обратно",
    )
    check(
        await db.get_secret(tg_id + 1, "gift_satellite") is None,
        "секрет другого пользователя недоступен",
    )
    await db.drop_secret(tg_id, "gift_satellite")

    print("\n\033[1m4. API\033[0m")
    from src.api.app import app

    code, payload, _ = await call(app, "GET", "/healthz")
    check(code == 200 and bool(payload and payload.get("ok")), "/healthz отвечает")

    code, _, _ = await call(app, "GET", "/api/me")
    check(code == 401, "запрос без подписи отклонён", f"получено {code}")

    code, _, _ = await call(
        app,
        "GET",
        "/api/me",
        headers={"X-Init-Data": "user=%7B%22id%22%3A1%7D&auth_date=9999999999&hash=deadbeef"},
    )
    check(code == 401, "поддельная подпись отклонена", f"получено {code}")

    init = sign_init_data(tg_id, "pexepo", config.BOT_TOKEN)
    code, payload, text = await call(app, "GET", "/api/me", headers={"X-Init-Data": init})
    if not check(code == 200, "валидная подпись принята", f"получено {code}: {text[:140]}"):
        return 1

    check(payload.get("tg_id") == tg_id, "пользователь распознан")
    check(payload.get("support") == "@awhoreable", "контакт поддержки в ответе")

    if not payload.get("licensed"):
        await db.claim_licence(licenses.OWNER_KEY, tg_id)
        code, payload, _ = await call(app, "GET", "/api/me", headers={"X-Init-Data": init})
    check(bool(payload.get("licensed")), "лицензия активирована")
    check(payload.get("dry_run") is True, "API сообщает о режиме симуляции")

    print("\n\033[1m5. Ключ доступа через приложение\033[0m")
    # A user who has not claimed anything yet -- so the endpoint is exercised on
    # the path that matters, not on an already-licensed account.
    fresh_id = tg_id + 4242
    fresh_init = sign_init_data(fresh_id, "newcomer", config.BOT_TOKEN)

    code, payload, _ = await call(
        app, "POST", "/api/licence", headers={"X-Init-Data": fresh_init}, body={"key": "мусор"}
    )
    check(
        code == 200 and payload and payload.get("ok") is False,
        "мусорный ключ отклонён",
        f"получено {code}",
    )

    spare = licenses.derive_keys(config.SECRET_KEY)[7]
    code, payload, text = await call(
        app, "POST", "/api/licence", headers={"X-Init-Data": fresh_init}, body={"key": spare}
    )
    if check(
        code == 200 and bool(payload and payload.get("ok")),
        "настоящий ключ активирован",
        f"получено {code}: {text[:120]}",
    ):
        code, payload, _ = await call(app, "GET", "/api/me", headers={"X-Init-Data": fresh_init})
        check(bool(payload.get("licensed")), "лицензия видна в /api/me")

        # Lower case, since that is how the owner types their own key.
        code, payload, _ = await call(
            app,
            "POST",
            "/api/licence",
            headers={"X-Init-Data": fresh_init},
            body={"key": spare.lower()},
        )
        check(
            code == 200 and bool(payload and payload.get("ok")),
            "повторная активация тем же владельцем проходит",
        )

        # A third party must not be able to take a claimed key.
        thief = sign_init_data(tg_id + 9999, "thief", config.BOT_TOKEN)
        code, payload, _ = await call(
            app, "POST", "/api/licence", headers={"X-Init-Data": thief}, body={"key": spare}
        )
        check(
            code == 200 and payload and payload.get("ok") is False,
            "занятый ключ не достаётся другому",
            f"получено {code}",
        )

    code, _, _ = await call(app, "POST", "/api/licence", body={"key": spare})
    check(code == 401, "без подписи ключ активировать нельзя", f"получено {code}")

    print("\n\033[1m6. Чиабатты\033[0m")
    code, payload, text = await call(
        app,
        "POST",
        "/api/ciabattas",
        headers={"X-Init-Data": init},
        body={
            "kind": "sniping",
            "title": "Проверка",
            "max_price_ton": "2.50",
            "quantity": 3,
            "auto_buy": False,
        },
    )
    created = (payload or {}).get("ciabatta")
    if check(code == 201 and bool(created), "создание", f"получено {code}: {text[:140]}"):
        check(
            created["max_price"]["nano"] == 2500000000,
            "цена без потери точности",
            str(created["max_price"]),
        )
        check(created["active"] is False, "создаётся остановленной")

        cid = created["id"]
        code, payload, _ = await call(
            app,
            "PATCH",
            f"/api/ciabattas/{cid}",
            headers={"X-Init-Data": init},
            body={"active": True},
        )
        check(code == 200 and payload["ciabatta"]["active"], "запуск")

        # A different user must not be able to touch it.
        stranger = sign_init_data(tg_id + 777, "someone", config.BOT_TOKEN)
        code, _, _ = await call(
            app,
            "PATCH",
            f"/api/ciabattas/{cid}",
            headers={"X-Init-Data": stranger},
            body={"active": False},
        )
        check(code in (403, 404), "чужая чиабатта недоступна", f"получено {code}")

        code, _, _ = await call(
            app, "DELETE", f"/api/ciabattas/{cid}", headers={"X-Init-Data": init}
        )
        check(code == 204, "удаление")

    code, _, _ = await call(
        app,
        "POST",
        "/api/ciabattas",
        headers={"X-Init-Data": init},
        body={"kind": "sniping", "max_price_ton": "0.0000000001"},
    )
    check(code == 422, "цена мельче нанотона отклонена", f"получено {code}")

    print("\n\033[1m7. Мини-приложение\033[0m")
    for path, kind in (("/", "index.html"), ("/css/tokens.css", "CSS"), ("/js/app.js", "app.js")):
        code, _, text = await call(app, "GET", path)
        check(code == 200 and len(text) > 200, f"{kind} отдаётся", f"получено {code}")

    print("\n\033[1m8. Лента событий\033[0m")
    code, payload, _ = await call(app, "GET", "/api/events", headers={"X-Init-Data": init})
    check(code == 200 and "events" in (payload or {}), "лента доступна")

    failed = [label for ok, label in results if not ok]
    print(f"\n\033[1mИтог: {len(results) - len(failed)} из {len(results)}\033[0m")
    if failed:
        print("\n  Не прошло:")
        for label in failed:
            print(f"    - {label}")
        return 1

    print("\n  Всё сходится. Дальше -- запуск в Telegram:")
    print("    .venv/bin/python -m src.bot.main")
    print("    .venv/bin/python -m uvicorn src.api.app:app --port 8080")
    print("\n  Каталог и торговые вызовы здесь не проверялись -- им нужен")
    print("  живой MRKT и подключённая сессия Telegram.")
    return 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(main()))
