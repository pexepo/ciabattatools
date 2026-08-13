"""Bot entry point: python -m src.bot.main

Wiring only. The dialogue lives in ``handlers_auth``, the queries in
``db.models``; this module builds the objects, injects them, and starts polling.

Polling rather than webhooks, deliberately: a webhook needs a public HTTPS
endpoint reachable by Telegram, and the mini-app already needs one for its own
reasons. Coupling bot delivery to that certificate means a lapsed cert silently
stops the bot instead of only breaking the web UI.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from src.bot import handlers_auth
from src.bot.handlers_auth import GsKeySG, KeyGate, begin_login
from src.core import config, licenses
from src.db import models as db
from src.mtproto.sessions import MAIN, WRITER, SessionStore

log = logging.getLogger(__name__)

router = Router(name="start")


def _open_app_kb() -> InlineKeyboardMarkup:
    """Keyboard with the mini-app button.

    Telegram rejects a WebApp button whose URL is not HTTPS, and rejects it at
    send time rather than at startup -- so a missing PUBLIC_URL would surface as
    "the bot ignores /start". The button is omitted instead, and the caller
    explains why.
    """
    if not config.PUBLIC_URL.startswith("https://"):
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🥖 Открыть Ciabatta Tools",
                    web_app=WebAppInfo(url=config.PUBLIC_URL),
                )
            ]
        ]
    )


def _connect_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Подключить аккаунт", callback_data="login:main"
                )
            ]
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, **kw) -> None:
    """Greet, then route by whatever the user is still missing.

    Three gates in order -- licence, session, app -- because each is useless
    without the previous one. Offering all three at once would let someone open
    the app before it can read anything.
    """
    store: SessionStore = kw["session_store"]
    await state.clear()

    user = await db.ensure_user(message.from_user.id, message.from_user.username)

    if not user.is_licensed:
        await state.set_state(KeyGate.waiting_key)
        await message.answer(
            "🥖 <b>Ciabatta Tools</b>\n\n"
            "Инструменты для заработка на NFT-подарках: трекинг новых "
            "подарков, авто-ордеринг и авто-снайпинг.\n\n"
            "Доступ по ключу. Пришли его сообщением.\n"
            f"За покупкой — {config.SUPPORT_CONTACT}"
        )
        return

    if await store.get(message.from_user.id, MAIN) is None:
        await message.answer(
            "🥖 <b>С возвращением</b>\n\n"
            "Осталось подключить Telegram-аккаунт — без него инструменты "
            "не смогут читать подарки.",
            reply_markup=_connect_kb(),
        )
        return

    kb = _open_app_kb()
    if not kb.inline_keyboard:
        await message.answer(
            "⚠️ <b>Приложение не настроено</b>\n\n"
            "В переменных окружения не задан <code>PUBLIC_URL</code> "
            "с https-адресом. Смотри DEPLOY.txt."
        )
        return

    await message.answer(
        "🥖 <b>Ciabatta Tools</b>\n\nВсё готово. Открывай приложение.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "login:main")
async def cb_login_main(callback, state: FSMContext) -> None:
    await callback.answer()
    await begin_login(callback.message, state, MAIN)


@router.callback_query(F.data == "login:writer")
async def cb_login_writer(callback, state: FSMContext) -> None:
    """Connect the second account used for auto-messages.

    Separate from the main login because the risk differs: unsolicited DMs are
    what gets an account limited, and the point of a writer slot is that the
    account holding the gifts is not the one taking that risk.
    """
    await callback.answer()
    await begin_login(callback.message, state, WRITER)


@router.message(Command("gskey"))
async def cmd_gskey(message: Message, state: FSMContext) -> None:
    await state.set_state(GsKeySG.waiting_key)
    await message.answer(
        "Пришли ключ Gift Satellite сообщением.\n\n"
        "Он нужен только для цен по всем маркетам. "
        "Без него флор считается по Telegram и MRKT — "
        "напиши /skip, если ключа нет."
    )


@router.message(Command("logout"))
async def cmd_logout(message: Message, **kw) -> None:
    """Drop stored sessions.

    Deletes rather than invalidates: someone asking to log out wants the session
    string gone, not flagged.
    """
    store: SessionStore = kw["session_store"]
    for kind in (MAIN, WRITER):
        await store.drop(message.from_user.id, kind)
    await message.answer(
        "Сессии удалены. Чтобы вернуться — /start.\nКлюч доступа остаётся за тобой."
    )


@router.message(Command("keys"))
async def cmd_keys(message: Message) -> None:
    """Licence overview, owner only.

    Silent for everyone else: an ignored command reveals less than "нет доступа".
    """
    if message.from_user.id != config.OWNER_TG_ID:
        return
    claimed, total = await db.licence_stats()
    await message.answer(
        f"🔑 Ключи: <b>{claimed}</b> из <b>{total}</b> активировано.\n"
        f"Твой ключ: <code>{licenses.OWNER_KEY}</code>"
    )


async def _startup() -> tuple[Bot, Dispatcher, SessionStore]:
    """Build everything and check the environment before polling.

    Config problems are raised here rather than on first use: a bot that starts
    and then fails on every message is harder to diagnose than one that refuses
    to start and says what is missing.
    """
    missing = [
        name
        for name in ("BOT_TOKEN", "TG_API_ID", "TG_API_HASH", "SECRET_KEY")
        if not getattr(config, name, None)
    ]
    if missing:
        raise SystemExit(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
            + "\nСмотри .env.example и DEPLOY.txt."
        )

    await db.init_models()

    # Idempotent: mints only what is absent, so a restart never issues a second
    # copy of a key someone already bought.
    added = await db.seed_licences(licenses.generate_keys())
    log.info("licences ready (%d new)", added)

    store = SessionStore(
        load=db.session_load, save=db.session_save, delete=db.session_delete
    )

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Injected into every handler as a keyword argument, so handlers take these
    # rather than importing them and tests can drive the dialogue with fakes.
    dp["session_store"] = store
    dp["repo"] = db

    dp.include_router(router)
    dp.include_router(handlers_auth.router)

    return bot, dp, store


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Telethon logs every MTProto call at INFO, which buries our own lines.
    logging.getLogger("telethon").setLevel(logging.WARNING)

    bot, dp, _ = await _startup()

    if config.DRY_RUN:
        log.warning("DRY_RUN is on -- no real purchases will be made")

    try:
        # Drops updates queued while the bot was down. Acting on a snipe found
        # ten minutes ago is worse than missing it, because the price has moved.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
