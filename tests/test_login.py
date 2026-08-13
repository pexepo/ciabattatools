"""Login dialogue: a flow must be keyed to the human, not to the bot.

These tests exist because of a real failure. ``begin_login`` read
``message.from_user.id``, and when called from a callback handler that
``message`` is the *bot's* own message -- so the flow was filed under the bot's
id and the user's next message could not find it. Every login died at the phone
number with "сессия входа потерялась".

The bug was invisible to type checking (both objects are Messages) and to the API
tests (they never touch the bot). Only a test that keeps the two ids distinct
catches it.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from src.bot import handlers_auth as ha
from src.mtproto.sessions import LoginProgress, LoginState

USER_ID = 111
BOT_ID = 999

WAIT_PHONE = LoginProgress(state=LoginState.WAIT_PHONE, message="Пришли номер")
WAIT_CODE = LoginProgress(state=LoginState.WAIT_CODE, message="Пришли код")


class FakeUser:
    def __init__(self, uid: int):
        self.id = uid
        self.username = "pexepo" if uid == USER_ID else "ciabatta_bot"


class FakeMessage:
    """Stands in for a Message.

    ``from_user`` is the sender -- and for a bot's own message that is the bot,
    which is the exact confusion these tests guard against.
    """

    def __init__(self, sender_id: int, text: str = ""):
        self.from_user = FakeUser(sender_id)
        self.text = text
        self.answers: list[str] = []
        self.deleted = False

    async def answer(self, text: str, **kwargs):
        self.answers.append(text)
        return FakeMessage(BOT_ID)

    async def delete(self):
        self.deleted = True


class FakeState:
    def __init__(self, data: dict | None = None):
        self.state = None
        self.data: dict = dict(data or {})
        self.cleared = False

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.state = None
        self.data = {}


class LoginTestCase(unittest.TestCase):
    """Shared setup: the module keeps flows in a dict at module scope."""

    def setUp(self):
        # A leftover flow from another test would let these pass for the wrong
        # reason.
        ha._flows.clear()
        ha._locks.clear()
        self._start = ha.LoginFlow.start
        self._submit_phone = ha.LoginFlow.submit_phone
        # Patched because the real calls open a Telethon connection to Telegram.
        ha.LoginFlow.start = AsyncMock(return_value=WAIT_PHONE)
        ha.LoginFlow.submit_phone = AsyncMock(return_value=WAIT_CODE)

    def tearDown(self):
        ha.LoginFlow.start = self._start
        ha.LoginFlow.submit_phone = self._submit_phone
        ha._flows.clear()
        ha._locks.clear()


class TestFlowOwnership(LoginTestCase):
    def test_flow_is_keyed_to_the_user_not_the_sender(self):
        """A login begun from a callback belongs to whoever tapped the button."""
        asyncio.run(
            ha.begin_login(FakeMessage(BOT_ID), FakeState(), "main", USER_ID)
        )

        self.assertIn((USER_ID, "main"), ha._flows, "must be filed under the user")
        self.assertNotIn((BOT_ID, "main"), ha._flows, "must not be filed under the bot")
        self.assertEqual(ha._flows[(USER_ID, "main")].tg_id, USER_ID)

    def test_phone_step_finds_a_flow_started_from_a_callback(self):
        """The regression itself: tap the button, then send a phone number."""
        state = FakeState()

        async def scenario():
            # The keyboard message is the bot's; the tap is the user's.
            await ha.begin_login(FakeMessage(BOT_ID), state, "main", USER_ID)
            # The reply is the user's own message.
            phone = FakeMessage(USER_ID, "+70000000000")
            await ha.process_phone(phone, state, session_store=AsyncMock())
            return phone

        phone = asyncio.run(scenario())

        self.assertNotIn(
            "потерялась",
            " ".join(phone.answers),
            "flow not found -- begin_login and process_phone disagree on the id",
        )
        self.assertIs(state.state, ha.LoginSG.waiting_code)

    def test_main_and_writer_slots_are_independent(self):
        """Two account slots for one user must not overwrite each other."""

        async def scenario():
            state = FakeState()
            await ha.begin_login(FakeMessage(BOT_ID), state, "main", USER_ID)
            await ha.begin_login(FakeMessage(BOT_ID), state, "writer", USER_ID)

        asyncio.run(scenario())

        self.assertIn((USER_ID, "main"), ha._flows)
        self.assertIn((USER_ID, "writer"), ha._flows)
        self.assertIsNot(ha._flows[(USER_ID, "main")], ha._flows[(USER_ID, "writer")])


class TestSecrets(LoginTestCase):
    def test_phone_number_is_deleted_from_the_chat(self):
        """A phone number must not remain in chat history."""

        async def scenario():
            state = FakeState()
            await ha.begin_login(FakeMessage(BOT_ID), state, "main", USER_ID)
            phone = FakeMessage(USER_ID, "+70000000000")
            await ha.process_phone(phone, state, session_store=AsyncMock())
            return phone

        self.assertTrue(asyncio.run(scenario()).deleted)

    def test_deletion_failure_does_not_abort_the_login(self):
        """Telegram refuses to delete some messages; the login must continue.

        A message older than 48 hours, or one in a group where the bot lacks
        rights, cannot be deleted -- and losing the login over that would be
        worse than the leftover message.
        """

        class Undeletable(FakeMessage):
            async def delete(self):
                raise RuntimeError("message can't be deleted")

        async def scenario():
            state = FakeState()
            await ha.begin_login(FakeMessage(BOT_ID), state, "main", USER_ID)
            phone = Undeletable(USER_ID, "+70000000000")
            await ha.process_phone(phone, state, session_store=AsyncMock())
            return state

        state = asyncio.run(scenario())
        self.assertIs(state.state, ha.LoginSG.waiting_code)


class TestMissingFlow(LoginTestCase):
    def test_absent_flow_reports_clearly(self):
        """With nothing in progress, the "lost" message is the honest answer.

        Its wording is only wrong when a flow *should* have been found, so it has
        to survive as the genuine empty case.
        """
        state = FakeState({"login_kind": "main"})
        message = FakeMessage(USER_ID, "+70000000000")

        asyncio.run(ha.process_phone(message, state, session_store=AsyncMock()))

        self.assertIn("потерялась", " ".join(message.answers))
        self.assertTrue(state.cleared)


if __name__ == "__main__":
    unittest.main()
