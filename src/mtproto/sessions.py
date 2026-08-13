"""Telegram user sessions: interactive login and encrypted storage.

The login runs inside a chat, so this is an explicit state machine rather than
Telethon's interactive prompt -- there is no stdin to read, and each step arrives
as a separate message, sometimes minutes apart.

Everything here handles credential material. Three rules follow, enforced in code
rather than left to callers:

* A phone number, login code and 2FA password live only as long as the request
  using them. They are never stored, never logged, and the bot deletes the
  messages carrying them.
* The session string is encrypted before it reaches the database, bound to the
  owning user so a row lifted into another account fails to decrypt.
* Two session kinds stay apart: ``main`` reads and trades, ``writer`` sends
  automessages. Mixing them is what gets a main account limited for spam, so the
  split is structural, not advisory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from src.core import config
from src.core.crypto import secret_box

log = logging.getLogger(__name__)

MAIN = "main"
WRITER = "writer"


class SessionError(RuntimeError):
    """Login failed in a way the user must act on."""


class LoginState(str, Enum):
    """Where a login has got to.

    Persisted so a login survives a bot restart mid-flow: a user who sent their
    phone number and then waited should not have to start over.
    """

    IDLE = "idle"
    WAIT_PHONE = "wait_phone"
    WAIT_CODE = "wait_code"
    WAIT_PASSWORD = "wait_password"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class LoginProgress:
    """What the bot should say next, and whether it may proceed."""

    state: LoginState
    message: str
    hint: str = ""
    done: bool = False
    failed: bool = False


@dataclass
class LoginFlow:
    """One in-progress login for one Telegram user.

    Holds a live Telethon client between steps, because the code Telegram sends is
    tied to the connection that requested it: reconnecting invalidates it and the
    user gets "code expired" on a code they just received.
    """

    tg_id: int
    kind: str = MAIN
    state: LoginState = LoginState.IDLE
    client: Any = field(default=None, repr=False)
    phone: str | None = field(default=None, repr=False)
    phone_code_hash: str | None = field(default=None, repr=False)
    password_hint: str | None = None
    error: str | None = None

    def __repr__(self) -> str:
        # A default repr would print the phone number into any traceback.
        return (
            f"<LoginFlow tg_id={self.tg_id} kind={self.kind} state={self.state.value}>"
        )

    async def start(self) -> LoginProgress:
        self.state = LoginState.WAIT_PHONE
        return LoginProgress(
            state=self.state,
            message="Отправьте номер телефона в международном формате.",
            hint="Например: +79001234567. Сообщение удалится сразу после обработки.",
        )

    async def submit_phone(self, phone: str) -> LoginProgress:
        """Request a login code, keeping the client connected afterwards."""
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        cleaned = _clean_phone(phone)
        if not cleaned:
            return LoginProgress(
                state=LoginState.WAIT_PHONE,
                message="Это не похоже на номер телефона.",
                hint="Нужен международный формат, например +79001234567.",
            )

        self.client = TelegramClient(
            StringSession(), config.TG_API_ID, config.TG_API_HASH
        )
        await self.client.connect()
        try:
            sent = await self.client.send_code_request(cleaned)
        except Exception as exc:  # noqa: BLE001 - many RPC errors land here
            await self._close()
            self.state = LoginState.FAILED
            self.error = _explain(exc)
            return LoginProgress(
                state=self.state,
                message=self.error,
                failed=True,
                hint="Проверьте номер и попробуйте снова: /login",
            )

        self.phone = cleaned
        self.phone_code_hash = sent.phone_code_hash
        self.state = LoginState.WAIT_CODE
        return LoginProgress(
            state=self.state,
            message="Код отправлен. Введите его.",
            hint="Код придёт в Telegram. Сообщение с кодом будет удалено.",
        )

    async def submit_code(self, code: str) -> LoginProgress:
        """Sign in with the code, or ask for the 2FA password."""
        from telethon.errors import SessionPasswordNeededError

        if self.client is None or not self.phone_code_hash:
            return await self._restart("Сессия входа истекла.")

        digits = "".join(ch for ch in (code or "") if ch.isdigit())
        if not digits:
            return LoginProgress(
                state=LoginState.WAIT_CODE,
                message="Код состоит из цифр.",
                hint="Пришлите только цифры из сообщения Telegram.",
            )

        try:
            await self.client.sign_in(
                phone=self.phone, code=digits, phone_code_hash=self.phone_code_hash
            )
        except SessionPasswordNeededError:
            self.state = LoginState.WAIT_PASSWORD
            try:
                password = await self.client(_password_request())
                self.password_hint = getattr(password, "hint", None) or None
            except Exception:  # noqa: BLE001 - the hint is a nicety, not a requirement
                self.password_hint = None
            hint = "Пароль двухфакторной защиты. Сообщение будет удалено."
            if self.password_hint:
                hint = f"Подсказка: {self.password_hint}. " + hint
            return LoginProgress(
                state=self.state,
                message="Нужен облачный пароль.",
                hint=hint,
            )
        except Exception as exc:  # noqa: BLE001
            self.state = LoginState.FAILED
            self.error = _explain(exc)
            await self._close()
            return LoginProgress(
                state=self.state,
                message=self.error,
                failed=True,
                hint="Начните заново: /login",
            )

        return await self._finish()

    async def submit_password(self, password: str) -> LoginProgress:
        if self.client is None:
            return await self._restart("Сессия входа истекла.")
        try:
            await self.client.sign_in(password=password)
        except Exception as exc:  # noqa: BLE001
            # A wrong password is recoverable, so the flow stays on this step
            # instead of forcing the whole login to be repeated.
            self.error = _explain(exc)
            return LoginProgress(
                state=LoginState.WAIT_PASSWORD,
                message=self.error,
                hint="Попробуйте ещё раз или начните заново: /login",
            )
        return await self._finish()

    async def _finish(self) -> LoginProgress:
        me = await self.client.get_me()
        self.state = LoginState.DONE
        who = f"@{me.username}" if getattr(me, "username", None) else str(me.id)
        log.info("session established for tg_id=%s kind=%s", self.tg_id, self.kind)
        return LoginProgress(
            state=self.state, message=f"Готово. Вошли как {who}.", done=True
        )

    def session_string(self) -> str:
        """The session, for encryption by the store. Never logged."""
        if self.client is None or self.state is not LoginState.DONE:
            raise SessionError("login is not complete")
        return self.client.session.save()

    async def _restart(self, why: str) -> LoginProgress:
        await self._close()
        self.state = LoginState.FAILED
        return LoginProgress(
            state=self.state, message=why, failed=True, hint="Начните заново: /login"
        )

    async def _close(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001 - teardown must not mask the real error
                pass

    def forget_secrets(self) -> None:
        """Drop the phone and code hash once they are no longer needed."""
        self.phone = None
        self.phone_code_hash = None
        self.password_hint = None


def _password_request():
    from telethon.tl.functions.account import GetPasswordRequest

    return GetPasswordRequest()


def _clean_phone(raw: str) -> str | None:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) < 8 or len(digits) > 15:
        return None
    return "+" + digits


def _explain(exc: Exception) -> str:
    """Turn an RPC error into something a user can act on, in Russian.

    Matched on class name rather than imported error types: the error lists vary
    across Telethon versions, and an unmatched error must still produce a sentence
    rather than a stack trace.
    """
    name = type(exc).__name__
    table = {
        "PhoneNumberInvalidError": "Неверный номер телефона.",
        "PhoneNumberBannedError": "Этот номер заблокирован в Telegram.",
        "PhoneCodeInvalidError": "Неверный код.",
        "PhoneCodeExpiredError": "Код истёк.",
        "PasswordHashInvalidError": "Неверный пароль.",
        "FloodWaitError": "Слишком много попыток. Подождите и повторите.",
        "SessionPasswordNeededError": "Нужен облачный пароль.",
        "ApiIdInvalidError": "Неверные TG_API_ID / TG_API_HASH в настройках сервера.",
    }
    if name in table:
        return table[name]
    seconds = getattr(exc, "seconds", None)
    if seconds:
        return f"Telegram просит подождать {seconds} с."
    log.warning("unmapped login error %s: %s", name, exc)
    return "Не удалось войти. Попробуйте позже."


class SessionStore:
    """Encrypted session persistence.

    The database layer is injected as callables so this module stays free of ORM
    imports and can be driven by dictionaries in tests.
    """

    def __init__(
        self,
        load: Callable[[int, str], Awaitable[str | None]],
        save: Callable[[int, str, str], Awaitable[None]],
        delete: Callable[[int, str], Awaitable[None]] | None = None,
    ):
        self._load = load
        self._save = save
        self._delete = delete

    @staticmethod
    def _context(tg_id: int, kind: str) -> str:
        """Bind ciphertext to its owner and role.

        A session row copied to another user, or a writer session pasted into the
        main slot, fails to decrypt instead of quietly working.
        """
        return f"session:{tg_id}:{kind}"

    async def put(self, tg_id: int, kind: str, session_string: str) -> None:
        blob = secret_box().encrypt(session_string, context=self._context(tg_id, kind))
        await self._save(tg_id, kind, blob)
        log.info("stored %s session for tg_id=%s", kind, tg_id)

    async def get(self, tg_id: int, kind: str = MAIN) -> str | None:
        blob = await self._load(tg_id, kind)
        if not blob:
            return None
        return secret_box().decrypt(blob, context=self._context(tg_id, kind))

    async def drop(self, tg_id: int, kind: str = MAIN) -> None:
        if self._delete is not None:
            await self._delete(tg_id, kind)

    async def client(self, tg_id: int, kind: str = MAIN):
        """A connected Telethon client for a stored session, or None.

        Returns None rather than raising when there is no session: "not logged in"
        is a normal state the caller handles, not an exception.
        """
        session_string = await self.get(tg_id, kind)
        if not session_string:
            return None

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(session_string), config.TG_API_ID, config.TG_API_HASH
        )
        await client.connect()
        if not await client.is_user_authorized():
            # Revoked upstream. Saying so plainly beats letting every later call
            # fail with an opaque auth error.
            await client.disconnect()
            log.warning(
                "stored %s session for tg_id=%s is no longer valid", kind, tg_id
            )
            return None
        return client
