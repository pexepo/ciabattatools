"""Subscription keys.

These are what buyers pay for, so the properties that matter here are commercial
as much as cryptographic: a key sold today must still work after a restart, and a
customer typing one by hand must not be told it is invalid.

The first class covers a real failure. Seeding used ``generate_keys``, which draws
fresh random keys on every call -- so each boot added 100 more rows, and rebuilding
the database would have invalidated every key already sold.
"""

from __future__ import annotations

import unittest

from src.core import licenses

SECRET = "test-secret-0123456789abcdef0123456789abcdef0123456789abcdef01234567"
OTHER_SECRET = "test-secret-ffffffffffffffffffffffffffffffffffffffffffffffffffff"


class TestDerivation(unittest.TestCase):
    """The property the product depends on: same secret, same keys, always."""

    def test_same_secret_yields_the_same_keys(self):
        self.assertEqual(licenses.derive_keys(SECRET), licenses.derive_keys(SECRET))

    def test_a_restart_does_not_mint_a_second_batch(self):
        """Two boots: the second must add nothing new."""
        first = set(licenses.derive_keys(SECRET))
        second = set(licenses.derive_keys(SECRET))
        self.assertEqual(first - second, set(), "a restart invented new keys")
        self.assertEqual(len(first | second), 101)

    def test_different_secrets_yield_different_keys(self):
        """Two deployments must not share a key space."""
        a = licenses.derive_keys(SECRET)
        b = licenses.derive_keys(OTHER_SECRET)
        # The owner key is fixed by specification, so it is excluded.
        self.assertEqual(set(a[1:]) & set(b[1:]), set())

    def test_count_and_uniqueness(self):
        keys = licenses.derive_keys(SECRET)
        self.assertEqual(len(keys), 101, "100 keys plus the owner's")
        self.assertEqual(len(set(keys)), 101, "a collision would shorten the set")

    def test_owner_key_is_first_and_fixed(self):
        self.assertEqual(licenses.derive_keys(SECRET)[0], licenses.OWNER_KEY)
        self.assertEqual(licenses.OWNER_KEY, "PEXEPO")

    def test_every_derived_key_passes_its_own_format_check(self):
        """A key failing is_valid_format would be rejected before any lookup."""
        for key in licenses.derive_keys(SECRET):
            self.assertTrue(licenses.is_valid_format(key), key)

    def test_no_secret_is_refused(self):
        """Deriving from "" would give every deployment identical keys."""
        with self.assertRaises(ValueError):
            licenses.derive_keys("")

    def test_derivation_does_not_look_sequential(self):
        """Knowing 100 keys must not suggest the 101st.

        Not a proof -- HMAC provides that -- but it catches a rewrite that makes
        the sequence structural, such as an index leaking into the visible part.
        """
        keys = licenses.derive_keys(SECRET, count=100)
        bodies = [k.removeprefix("CIAB-").replace("-", "") for k in keys[1:]]
        for a, b in zip(bodies, bodies[1:]):
            shared = 0
            for ca, cb in zip(a, b):
                if ca != cb:
                    break
                shared += 1
            self.assertLess(shared, 6, f"keys look sequential: {a} / {b}")


class TestGenerateKeys(unittest.TestCase):
    """The random minter, kept for issuing a fresh batch deliberately."""

    def test_generated_sets_differ(self):
        """The distinction from derive_keys, stated as a test.

        If this ever fails, the two functions have been merged and seeding is
        random again -- which is the bug this file was written for.
        """
        self.assertNotEqual(licenses.generate_keys(), licenses.generate_keys())

    def test_generated_keys_are_well_formed(self):
        for key in licenses.generate_keys(10):
            self.assertTrue(licenses.is_valid_format(key), key)


class TestNormalize(unittest.TestCase):
    """Keys get typed into a chat by hand, so input is forgiving."""

    def test_owner_key_lowercase(self):
        """The owner types "pexepo"; it has to work."""
        self.assertEqual(licenses.normalize("pexepo"), licenses.OWNER_KEY)

    def test_surrounding_whitespace(self):
        self.assertEqual(licenses.normalize("  pexepo\n"), licenses.OWNER_KEY)

    def test_missing_dashes_are_restored(self):
        key = licenses.derive_keys(SECRET)[1]
        self.assertEqual(licenses.normalize(key.replace("-", "")), key)

    def test_lowercase_full_key(self):
        key = licenses.derive_keys(SECRET)[1]
        self.assertEqual(licenses.normalize(key.lower()), key)

    def test_internal_spaces(self):
        """Copy-paste out of a chat often carries stray spaces."""
        key = licenses.derive_keys(SECRET)[1]
        self.assertEqual(licenses.normalize(key.replace("-", " ")), key)

    def test_empty_and_none(self):
        self.assertEqual(licenses.normalize(""), "")
        self.assertEqual(licenses.normalize(None), "")

    def test_lookalike_characters_are_not_substituted(self):
        """A misread glyph must fail rather than resolve to another key.

        The alphabet excludes O/0 and I/1 so they never appear in a real key;
        "correcting" them on input could map a typo onto someone else's key.
        """
        self.assertFalse(
            licenses.is_valid_format(licenses.normalize("CIAB-O0O0-1I1I-LLLL"))
        )


class TestFormatCheck(unittest.TestCase):
    def test_rejects_garbage(self):
        for bad in ("", "hello", "CIAB", "CIAB-", "CIAB-ABC", "1234-5678-9012"):
            self.assertFalse(licenses.is_valid_format(bad), bad)

    def test_accepts_owner_and_derived(self):
        self.assertTrue(licenses.is_valid_format(licenses.OWNER_KEY))
        self.assertTrue(licenses.is_valid_format(licenses.derive_keys(SECRET)[1]))


class TestFingerprint(unittest.TestCase):
    """Only fingerprints are stored, so this is the database's real key."""

    def test_stable(self):
        key = licenses.derive_keys(SECRET)[1]
        self.assertEqual(licenses.fingerprint(key), licenses.fingerprint(key))

    def test_distinct_per_key(self):
        keys = licenses.derive_keys(SECRET)
        self.assertEqual(
            len({licenses.fingerprint(k) for k in keys}),
            len(keys),
            "fingerprint collision",
        )

    def test_does_not_contain_the_key(self):
        """A log line carrying a fingerprint must not leak the credential."""
        key = licenses.derive_keys(SECRET)[1]
        fp = licenses.fingerprint(key)
        self.assertNotIn(key, fp)
        self.assertNotIn(key.replace("-", ""), fp)

    def test_short_enough_to_log(self):
        self.assertLessEqual(len(licenses.fingerprint(licenses.OWNER_KEY)), 16)


class TestComparison(unittest.TestCase):
    def test_match_and_mismatch(self):
        keys = licenses.derive_keys(SECRET)
        self.assertTrue(licenses.keys_match(keys[1], keys[1]))
        self.assertFalse(licenses.keys_match(keys[1], keys[2]))

    def test_compares_exactly(self):
        """keys_match does not normalise; callers normalise first."""
        self.assertFalse(licenses.keys_match("PEXEPO", "pexepo"))


if __name__ == "__main__":
    unittest.main()
