"""Money as integer nanoTON.

Every price on MRKT is an integer count of nanoTON (1 TON = 1e9 nanoTON).
Floats are banned in this module and in every caller that touches a price,
because a float TON value cannot represent 0.1 TON exactly and the error
compounds across a fee-plus-gas calculation.

The failure mode this type exists to prevent is scale error: multiplying by
1e9 twice, or not at all. Both produce a number that looks plausible in a log
line and is wrong by a factor of a billion. So the only way to build a Nano is
from an explicit unit -- ``Nano.from_ton("1.5")`` or ``Nano(1_500_000_000)`` --
and the only way out is an explicit conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

NANO_PER_TON = 1_000_000_000
_TON = Decimal(NANO_PER_TON)


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as an exact nanoTON amount."""


@dataclass(frozen=True, slots=True, order=True)
class Nano:
    """An exact amount of nanoTON.

    Immutable and ordered, so amounts can be compared and sorted directly.
    Arithmetic is closed over Nano: adding two amounts yields an amount, and
    scaling by a ratio rounds in an explicitly chosen direction.
    """

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise MoneyError(
                f"Nano takes an int of nanoTON, got {type(self.value).__name__}. "
                "Use Nano.from_ton() for a TON figure."
            )

    # --- construction ----------------------------------------------------

    @classmethod
    def from_ton(cls, ton: str | int | Decimal) -> "Nano":
        """Build from a TON figure.

        A float is refused on purpose: 0.1 has no exact binary form, so
        accepting one would reintroduce the error this type exists to prevent.
        Pass a string ("0.1"), an int, or a Decimal.
        """
        if isinstance(ton, float):
            raise MoneyError(
                "from_ton() refuses float: 0.1 TON is not exactly representable. "
                'Pass a string instead, e.g. Nano.from_ton("0.1").'
            )
        try:
            amount = Decimal(ton)
        except Exception as exc:  # noqa: BLE001 - Decimal raises several types
            raise MoneyError(f"not a TON amount: {ton!r}") from exc
        if not amount.is_finite():
            raise MoneyError(f"not a finite TON amount: {ton!r}")
        scaled = amount * _TON
        if scaled != scaled.to_integral_value():
            raise MoneyError(
                f"{ton} TON is finer than one nanoTON and would be truncated"
            )
        return cls(int(scaled))

    @classmethod
    def parse(cls, raw: object) -> "Nano | None":
        """Read a nanoTON field off an API payload.

        Returns None when the field is absent, because zero is a real price and
        must never stand in for "unknown". A malformed value is None too: a
        price we cannot prove is a refusal to spend, not a default.
        """
        if raw is None or isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return cls(raw)
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            try:
                return cls(int(text))
            except ValueError:
                return None
        if isinstance(raw, float):
            # Large integers sometimes arrive as JSON numbers that decode to
            # float. Accept only when the value is exactly integral.
            if raw.is_integer():
                return cls(int(raw))
            return None
        return None

    # --- conversion ------------------------------------------------------

    def to_ton(self) -> Decimal:
        """Exact TON value. Decimal, never float."""
        return Decimal(self.value) / _TON

    def format_ton(self, places: int = 2) -> str:
        """Human-readable TON, rounded half-up for display only.

        Display rounding never feeds back into arithmetic: callers needing a
        number use to_ton() or value.
        """
        quantum = Decimal(1).scaleb(-places)
        return f"{self.to_ton().quantize(quantum, rounding=ROUND_HALF_UP):f}"

    # --- arithmetic ------------------------------------------------------

    def __add__(self, other: "Nano") -> "Nano":
        if not isinstance(other, Nano):
            return NotImplemented
        return Nano(self.value + other.value)

    def __sub__(self, other: "Nano") -> "Nano":
        if not isinstance(other, Nano):
            return NotImplemented
        return Nano(self.value - other.value)

    def scale(self, ratio: str | int | Decimal, *, round_up: bool = False) -> "Nano":
        """Multiply by a ratio, rounding to whole nanoTON.

        Rounds down by default so a computed bid never drifts above the cap the
        user set. ``round_up=True`` is for costs, where understating is the
        dangerous direction.
        """
        if isinstance(ratio, float):
            raise MoneyError("scale() refuses float; pass a string such as '0.95'.")
        exact = Decimal(self.value) * Decimal(ratio)
        rounded = exact.to_integral_value(
            rounding=ROUND_HALF_UP if round_up else ROUND_DOWN
        )
        return Nano(int(rounded))

    def pct_of(self, other: "Nano") -> Decimal | None:
        """This amount as a percentage of another. None when other is zero."""
        if not isinstance(other, Nano):
            raise MoneyError("pct_of() takes a Nano")
        if other.value == 0:
            return None
        return Decimal(self.value) / Decimal(other.value) * Decimal(100)

    # --- presentation ----------------------------------------------------

    def __str__(self) -> str:
        return f"{self.format_ton()} TON"

    def __repr__(self) -> str:
        return f"Nano({self.value}n = {self.format_ton()} TON)"


ZERO = Nano(0)
