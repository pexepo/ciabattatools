"""Bot-side login dialogue: licence key, Telegram session, Gift Satellite key.

Three rules shape this module.

**Secrets do not persist in the chat.** A phone number, a login code and a 2FA
password all arrive as ordinary messages, and Telegram keeps chat history
forever. Each is deleted immediately after it is read, and none is ever logged --
not even at debug level.

**Login state lives in memory, not in FSM storage.** Telethon ties the code
Telegram sends to the connection that requested it, so the live client has to
survive between steps. Putting it in FSM storage would mean serialising a socket;
it is held in ``_flows`` instead, keyed by user and slot.

**Failures are explained, not echoed.** ``LoginProgress.message`` is already
written for the user, so handlers relay it rather than composing their own
wording out of exception text.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from src.core import licenses
from src.mtproto.sessions import MAIN, LoginFlow, LoginState

log = logging.getLogger(__name__)

router = Router(name="auth")

# Live logins, keyed by (tg_id, kind). A user may hold a main session and a
# writer session, and the two must not collide mid-flow.
_flows: dict[tuple[int, str], LoginFlow] = {}

# One login at a time per user. Without this, a double-tap on "подключить"
# starts two Telethon clients and Telegram invalidates the first code.
_locks: dict[int, asyncio.Lock] = {}

_REJECTED = (
    "🥖 <b>Ключ не подошёл</b>\n\n"
    "Проверь, что скопировал его целиком.\n"
    "За покупкой доступа — @awhoreable"
)

_LOST = "Сессия входа потерялась — начнём заново.\nНажми <b>Подключить аккаунт</b>."


def _lock(tg_id: int) -> asyncio.Lock:
    return _locks.setdefault(tg_id, asyncio.Lock())


class KeyGate(StatesGroup):
    waiting_key = State()


class LoginSG(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class GsKeySG(StatesGroup):
    waiting_key = State()


async def _scrub(message: Message) -> None:
    """Remove a message carrying a secret from the chat.

    Deletion can fail -- the message may be too old, or the bot may lack rights
    in a group -- and that must not abort a login in progress. Only the exception
    type is logged; logging the content would defeat the purpose.
    """
    try:
        await message.delete()
    except Exception as exc:
        log.warning("could not delete secret-bearing message: %s", type(exc).__name__)


def _relay(progress) -> str:
    """Render a LoginProgress for the chat."""
    if progress.hint:
        return f"{progress.message}\n\n<i>{progress.hint}</i>"
    return progress.message


# --- licence gate -------------------------------------------------------------


@router.message(KeyGate.waiting_key)
async def process_licence_key(message: Message, state: FSMContext, **kw) -> None:
    """Check a typed licence key.

    Normalised first, so a key pasted lowercase or without dashes is not reported
    as invalid.
    """
    repo = kw["repo"]
    raw = (message.text or "").strip()
    await _scrub(message)

    key = licenses.normalize(raw)

    # Shape is checked before touching the database: it costs nothing and keeps
    # obvious typos out of the query path.
    if not licenses.is_valid_format(key):
        await message.answer(_REJECTED)
        return

    record = await repo.find_licence(key)
    if record is None:
        # Deliberately identical to the malformed-key reply: someone probing for
        # valid shapes learns nothing from the difference.
        await message.answer(_REJECTED)
        log.info("rejected licence attempt fp=%s", licenses.fingerprint(key))
        return

    if record.is_claimed and record.claimed_by != message.from_user.id:
        await message.answer(
            "🥖 <b>Этот ключ уже активирован</b>\n\n"
            "Один ключ — один аккаунт.\n"
            "За покупкой доступа — @awhoreable"
        )
        return

    await repo.claim_licence(key, message.from_user.id)
    await state.clear()
    log.info(
        "licence claimed fp=%s tg_id=%s",
        licenses.fingerprint(key),
        message.from_user.id,
    )

    await message.answer(
        "✅ <b>Доступ открыт</b>\n\n"
        "Теперь подключим твой Telegram-аккаунт — без него инструменты "
        "не смогут читать подарки.\n\n"
        "Нажми <b>Подключить аккаунт</b>."
    )


# --- Telegram login -----------------------------------------------------------


async def begin_login(message: Message, state: FSMContext, kind: str = MAIN) -> None:
    """Start (or restart) a login for one account slot."""
    tg_id = message.from_user.id

    async with _lock(tg_id):
        # Drop any earlier attempt so a stale client cannot answer for the new one.
        old = _flows.pop((tg_id, kind), None)
        if old is not None:
            await old._close()

        flow = LoginFlow(tg_id=tg_id, kind=kind)
        _flows[(tg_id, kind)] = flow
        progress = await flow.start()

    await state.update_data(login_kind=kind)
    await state.set_state(LoginSG.waiting_phone)
    await message.answer(_relay(progress))


@router.message(LoginSG.waiting_phone)
async def process_phone(message: Message, state: FSMContext, **kw) -> None:
    tg_id = message.from_user.id
    kind = (await state.get_data()).get("login_kind", MAIN)
    phone = (message.text or "").strip()

    # A phone number is a secret in a group and clutter in a DM; removed either
    # way, and before the network call, so a slow Telegram response cannot leave
    # it sitting in the chat.
    await _scrub(message)

    flow = _flows.get((tg_id, kind))
    if flow is None:
        await state.clear()
        await message.answer(_LOST)
        return

    notice = await message.answer("⏳ Отправляю код…")
    async with _lock(tg_id):
        progress = await flow.submit_phone(phone)

    try:
        await notice.delete()
    except Exception:
        # Cosmetic only -- a leftover "sending code" line is not worth an error.
        pass

    await _advance(message, state, flow, progress, kind, kw["session_store"])


@router.message(LoginSG.waiting_code)
async def process_code(message: Message, state: FSMContext, **kw) -> None:
    tg_id = message.from_user.id
    kind = (await state.get_data()).get("login_kind", MAIN)
    code = (message.text or "").strip()
    await _scrub(message)

    flow = _flows.get((tg_id, kind))
    if flow is None:
        await state.clear()
        await message.answer(_LOST)
        return

    async with _lock(tg_id):
        progress = await flow.submit_code(code)
    await _advance(message, state, flow, progress, kind, kw["session_store"])


@router.message(LoginSG.waiting_password)
async def process_password(message: Message, state: FSMContext, **kw) -> None:
    tg_id = message.from_user.id
    kind = (await state.get_data()).get("login_kind", MAIN)
    password = (message.text or "").strip()

    # The most sensitive value in the flow: the account's cloud password, not a
    # one-time code.
    await _scrub(message)

    flow = _flows.get((tg_id, kind))
    if flow is None:
        await state.clear()
        await message.answer(_LOST)
        return

    async with _lock(tg_id):
        progress = await flow.submit_password(password)
    await _advance(message, state, flow, progress, kind, kw["session_store"])


async def _advance(
    message: Message, state: FSMContext, flow: LoginFlow, progress, kind: str, store
) -> None:
    """Move the dialogue to whichever step the flow reports.

    The flow owns the decision; this maps its state onto an FSM state and, on
    success, persists the session. Duplicating the branching in each handler is
    how the two drift apart.
    """
    text = _relay(progress)

    if progress.state is LoginState.WAIT_CODE:
        await state.set_state(LoginSG.waiting_code)
        await message.answer(text)
        return

    if progress.state is LoginState.WAIT_PASSWORD:
        await state.set_state(LoginSG.waiting_password)
        await message.answer(text)
        return

    if progress.state is LoginState.WAIT_PHONE:
        # A restart: wrong number, or an expired code.
        await state.set_state(LoginSG.waiting_phone)
        await message.answer(text)
        return

    if progress.done:
        await _persist(message, state, flow, kind, store)
        return

    # Failed. Close the client and forget the flow so the next attempt is clean.
    _flows.pop((flow.tg_id, kind), None)
    await flow._close()
    await state.clear()
    await message.answer(text)


async def _persist(
    message: Message, state: FSMContext, flow: LoginFlow, kind: str, store
) -> None:
    """Store a completed session, then drop every secret from memory."""
    try:
        await store.put(flow.tg_id, kind, flow.session_string())
    except Exception as exc:
        # The account is authorised but unsaveable. Reporting success here would
        # be a lie the user discovers on the next restart.
        log.error("failed to store %s session: %s", kind, type(exc).__name__)
        await message.answer(
            "⚠️ Вход прошёл, но сохранить сессию не удалось.\n"
            "Попробуй ещё раз — если повторится, напиши @awhoreable."
        )
    else:
        who = "основной аккаунт" if kind == MAIN else "аккаунт для авто-сообщений"
        await message.answer(
            f"✅ <b>Готово — {who} подключён</b>\n\n"
            "Открывай приложение и выбирай инструмент."
        )
    finally:
        # Runs on both paths: a stored session must not leave the phone number and
        # code hash sitting in memory either.
        flow.forget_secrets()
        _flows.pop((flow.tg_id, kind), None)
        await flow._close()
        await state.clear()


# --- Gift Satellite key -------------------------------------------------------


# Registered before the catch-all below, because aiogram matches in registration
# order and an unfiltered handler on the same state would swallow /skip.
@router.message(GsKeySG.waiting_key, F.text == "/skip")
async def skip_gs_key(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Хорошо, обойдёмся без него.\n"
        "Флор будет считаться по Telegram и MRKT — этого хватает для работы.\n"
        "Добавить ключ можно позже в настройках."
    )


@router.message(GsKeySG.waiting_key)
async def process_gs_key(message: Message, state: FSMContext, **kw) -> None:
    """Accept a Gift Satellite API key.

    The key is optional: floors also come from Telegram and from MRKT listings,
    so a user without one is told what they lose rather than being blocked.
    """
    repo = kw["repo"]
    key = (message.text or "").strip()
    await _scrub(message)

    if len(key) < 8:
        await message.answer(
            "Это не похоже на ключ. Пришли его целиком, "
            "или напиши /skip — цены будут считаться без Gift Satellite."
        )
        return

    await repo.put_secret(message.from_user.id, "gift_satellite", key)
    await state.clear()
    log.info("stored gift satellite key for tg_id=%s", message.from_user.id)
    await message.answer(
        "✅ <b>Ключ сохранён</b>\n\nЦены теперь считаются по всем маркетам."
    )
