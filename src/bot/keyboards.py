"""Inline keypad for entering the Telegram login code.

Not a nicety. Telegram scans message text for its own login codes and can refuse
to deliver them -- a user who types "12345" into a chat may find the message
blocked, and the ones that do arrive sit in history as a working credential.

Tapping digits keeps the code out of message text entirely: it travels in callback
data, accumulates in FSM state, and is submitted as one action. There is nothing
to delete afterwards, because nothing was ever posted.

The keypad is edited in place on every tap, so the chat holds one message rather
than a column of them.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Callback prefix, kept short: Telegram caps callback_data at 64 bytes and the
# digit has to fit alongside it.
CODE_CB = "c"

# Telegram login codes are five digits.
CODE_LENGTH = 5

# Filled and empty slots. Circles rather than the digits themselves: the keyboard
# sits on screen next to the chat, and someone glancing over a shoulder should not
# be able to read the code off it. The user knows what they typed.
DOT_FILLED = "●"
DOT_EMPTY = "○"

_BLANK = " "


def _digit_rows() -> list[list[InlineKeyboardButton]]:
    """1-9 in a 3x3 grid.

    Phone-dial order rather than calculator order, because this is a phone.
    """
    return [
        [
            InlineKeyboardButton(text=str(d), callback_data=f"{CODE_CB}:d:{d}")
            for d in range(start, start + 3)
        ]
        for start in (1, 4, 7)
    ]


def code_keyboard(entered: str = "") -> InlineKeyboardMarkup:
    """The keypad, reflecting how much has been entered so far.

    ``entered`` is display state only. The authoritative copy lives in FSM
    storage, because callback data is client-supplied and a crafted callback could
    otherwise inject digits of its own.
    """
    filled = len(entered)
    progress = DOT_FILLED * filled + DOT_EMPTY * max(0, CODE_LENGTH - filled)

    rows = [
        # Inert display row. Telegram has no read-only button, so this carries a
        # no-op callback that gets answered silently.
        [InlineKeyboardButton(text=progress, callback_data=f"{CODE_CB}:noop")],
        *_digit_rows(),
    ]

    # Backspace | 0 | submit. All three slots are always present so the 0 does not
    # shift position as digits are entered -- a key that moves under the thumb
    # causes mistypes, and this one is pressed while reading a code off another
    # screen.
    bottom = [
        InlineKeyboardButton(
            text="⌫" if filled else _BLANK,
            callback_data=f"{CODE_CB}:del" if filled else f"{CODE_CB}:noop",
        ),
        InlineKeyboardButton(text="0", callback_data=f"{CODE_CB}:d:0"),
        # Offered from four digits: five is standard, but a shorter code must not
        # be impossible to submit.
        InlineKeyboardButton(
            text="✓" if filled >= CODE_LENGTH - 1 else _BLANK,
            callback_data=f"{CODE_CB}:ok" if filled >= CODE_LENGTH - 1 else f"{CODE_CB}:noop",
        ),
    ]
    rows.append(bottom)

    # An escape hatch. If the keypad misbehaves on some client, the user is not
    # locked out of logging in.
    rows.append(
        [
            InlineKeyboardButton(
                text="Ввести код сообщением", callback_data=f"{CODE_CB}:manual"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_action(data: str) -> tuple[str, str]:
    """Split callback data into ``(action, argument)``.

    Returns ``("noop", "")`` for anything unrecognised. Callback data comes from
    the client and can be forged, so an unknown payload is ignored rather than
    trusted -- and a digit is validated as exactly one digit, not merely as
    non-empty.
    """
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != CODE_CB:
        return ("noop", "")

    action = parts[1]
    if action == "d":
        digit = parts[2] if len(parts) > 2 else ""
        if len(digit) != 1 or not digit.isdigit():
            return ("noop", "")
        return ("digit", digit)
    if action in ("del", "ok", "manual"):
        return (action, "")
    return ("noop", "")
