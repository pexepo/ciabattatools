"""Money tests.

Written against stdlib ``unittest`` rather than pytest so they run with no
installed dependencies -- pytest also collects unittest classes, so nothing is
lost when the environment does have it.

The point of these is not coverage, it is the scale error: a price wrong by a
factor of a billion looks plausible in a log line and empties a wallet. So the
round-trip and double-scaling cases are asserted explicitly, and every guard
that refuses a float is asserted to actually refuse.

    python3 -m unittest discover -s tests -v
"""

import unittest
from decimal import Decimal

from src.core.money import NANO_PER_TON, MoneyError, Nano, ZERO


class TestConstruction(unittest.TestCase):
    def test_raw_int_is_nanoton(self):
        self.assertEqual(Nano(1_500_000_000).value, 1_500_000_000)

    def test_from_ton_string(self):
        self.assertEqual(Nano.from_ton("1.5").value, 1_500_000_000)

    def test_from_ton_int(self):
        self.assertEqual(Nano.from_ton(2).value, 2_000_000_000)

    def test_from_ton_decimal(self):
        self.assertEqual(Nano.from_ton(Decimal("0.1")).value, 100_000_000)

    def test_one_nanoton_is_representable(self):
        self.assertEqual(Nano.from_ton("0.000000001").value, 1)

    def test_float_ton_is_refused(self):
        # 0.1 has no exact binary form; accepting it reintroduces drift.
        with self.assertRaisesRegex(MoneyError, "refuses float"):
            Nano.from_ton(0.1)

    def test_float_nano_is_refused(self):
        with self.assertRaisesRegex(MoneyError, "int of nanoTON"):
            Nano(1.5)  # type: ignore[arg-type]

    def test_bool_is_refused(self):
        # bool is an int subclass; True would silently become 1 nanoTON.
        with self.assertRaises(MoneyError):
            Nano(True)  # type: ignore[arg-type]

    def test_sub_nanoton_precision_is_refused(self):
        with self.assertRaisesRegex(MoneyError, "finer than one nanoTON"):
            Nano.from_ton("0.0000000005")

    def test_garbage_string_is_refused(self):
        with self.assertRaises(MoneyError):
            Nano.from_ton("бесплатно")


class TestScaleError(unittest.TestCase):
    """The failure this module exists to prevent."""

    def test_round_trip_preserves_value(self):
        for ton in ("0.000000001", "0.1", "1", "2.5", "1234.567890123"):
            with self.subTest(ton=ton):
                n = Nano.from_ton(ton)
                self.assertEqual(Nano.from_ton(n.to_ton()), n)

    def test_to_ton_does_not_multiply(self):
        # A double-scale bug shows up here as 1e9 instead of 1.
        self.assertEqual(Nano(NANO_PER_TON).to_ton(), Decimal(1))

    def test_from_ton_scales_exactly_once(self):
        self.assertEqual(Nano.from_ton("1").value, NANO_PER_TON)

    def test_double_scaling_is_detectable(self):
        once = Nano.from_ton("1")
        twice = Nano.from_ton(Decimal(once.value))
        self.assertEqual(twice.value, NANO_PER_TON**2)
        self.assertNotEqual(twice, once)

    def test_to_ton_is_never_float(self):
        self.assertIsInstance(Nano(1).to_ton(), Decimal)


class TestParse(unittest.TestCase):
    """Reading API payloads, where absent must not become zero."""

    def test_int_payload(self):
        self.assertEqual(Nano.parse(2_000_000_000), Nano.from_ton("2"))

    def test_numeric_string_payload(self):
        self.assertEqual(Nano.parse("2000000000"), Nano.from_ton("2"))

    def test_integral_float_payload(self):
        self.assertEqual(Nano.parse(2e9), Nano.from_ton("2"))

    def test_none_stays_none(self):
        self.assertIsNone(Nano.parse(None))

    def test_zero_is_a_real_price_not_unknown(self):
        parsed = Nano.parse(0)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed, ZERO)

    def test_empty_string_is_unknown(self):
        self.assertIsNone(Nano.parse(""))

    def test_malformed_is_unknown_not_zero(self):
        # A price we cannot prove is a refusal to spend, never a default.
        self.assertIsNone(Nano.parse("about 2 TON"))
        self.assertIsNone(Nano.parse(1.5))
        self.assertIsNone(Nano.parse({"amount": 1}))

    def test_bool_is_not_a_price(self):
        self.assertIsNone(Nano.parse(True))


class TestArithmetic(unittest.TestCase):
    def test_add_and_subtract(self):
        self.assertEqual(Nano.from_ton("1") + Nano.from_ton("0.5"), Nano.from_ton("1.5"))
        self.assertEqual(Nano.from_ton("1") - Nano.from_ton("0.5"), Nano.from_ton("0.5"))

    def test_mixing_units_is_refused(self):
        with self.assertRaises(TypeError):
            Nano.from_ton("1") + 5  # type: ignore[operator]

    def test_scale_rounds_down_by_default(self):
        # A bid must never drift above the cap the user set.
        self.assertEqual(Nano(10).scale("0.95").value, 9)

    def test_scale_rounds_up_for_costs(self):
        self.assertEqual(Nano(10).scale("0.95", round_up=True).value, 10)

    def test_scale_refuses_float_ratio(self):
        with self.assertRaisesRegex(MoneyError, "refuses float"):
            Nano(10).scale(0.95)

    def test_scale_by_percent_of_floor(self):
        floor = Nano.from_ton("12.4")
        self.assertEqual(floor.scale("0.90"), Nano.from_ton("11.16"))

    def test_pct_of(self):
        self.assertEqual(Nano.from_ton("5").pct_of(Nano.from_ton("10")), Decimal(50))

    def test_pct_of_zero_is_none(self):
        self.assertIsNone(Nano.from_ton("5").pct_of(ZERO))

    def test_ordering(self):
        self.assertLess(Nano.from_ton("1"), Nano.from_ton("2"))
        self.assertEqual(
            max(Nano.from_ton("1"), Nano.from_ton("2")), Nano.from_ton("2")
        )


class TestDisplay(unittest.TestCase):
    def test_format_two_places(self):
        self.assertEqual(Nano.from_ton("12.456").format_ton(), "12.46")

    def test_format_pads_places(self):
        self.assertEqual(Nano.from_ton("12.4").format_ton(), "12.40")

    def test_format_does_not_use_scientific_notation(self):
        # Decimal defaults to exponent form for small values; users read prices.
        self.assertNotIn("E", Nano(1).format_ton(9))
        self.assertEqual(Nano(1).format_ton(9), "0.000000001")

    def test_str_carries_the_unit(self):
        self.assertEqual(str(Nano.from_ton("3")), "3.00 TON")

    def test_repr_shows_both_units(self):
        # A log line must make a scale error visible at a glance.
        text = repr(Nano.from_ton("1"))
        self.assertIn("1000000000n", text)
        self.assertIn("1.00 TON", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
