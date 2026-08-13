"""Mini App initData verification.

This is the only thing standing between the API and the open internet, so the
tests are adversarial: forge the payload, replay it, backdate it, and confirm each
attempt is refused.

Several of them cover a real production failure. Every launch was rejected with
"signature mismatch" because ``_data_check_string`` excluded the ``signature``
field -- the reasoning being that an Ed25519 signature does not belong inside an
HMAC payload. Telegram computes ``hash`` over the whole payload including it.
Worse, only clients new enough to send the field were affected, so older ones
authenticated fine and it looked like a token problem.

Some tests therefore cross-check against aiogram's own
``check_webapp_signature``: when our verdict and theirs disagree, one of us is
wrong, and that is worth failing over.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from aiogram.utils.web_app import check_webapp_signature

from src.api.auth import InitDataError, verify_init_data

TOKEN = "123456:TEST-TOKEN-not-a-real-bot"
USER_ID = 111


def sign(fields: dict[str, str], token: str = TOKEN) -> str:
    """Build a payload signed the way Telegram signs one.

    Everything except ``hash`` goes into the signed string, sorted by key. This
    mirrors the documented algorithm rather than calling our own code, so an
    implementation bug cannot make its own tests pass.
    """
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signed = dict(fields)
    signed["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(signed)


def payload(**extra) -> dict[str, str]:
    fields = {
        "user": json.dumps({"id": USER_ID, "username": "pexepo"}),
        "auth_date": str(int(time.time())),
    }
    fields.update(extra)
    return fields


def accepts(blob: str, **kwargs) -> bool:
    try:
        verify_init_data(blob, bot_token=TOKEN, **kwargs)
        return True
    except InitDataError:
        return False


class TestSignatureField(unittest.TestCase):
    """The regression: payloads carrying a ``signature`` field.

    Newer Telegram clients include it, and excluding it from the check string is
    what broke every login.
    """

    def test_payload_with_signature_is_accepted(self):
        blob = sign(payload(signature="ed25519_placeholder", chat_type="sender"))
        self.assertTrue(accepts(blob), "a modern client's payload was rejected")

    def test_payload_without_signature_is_accepted(self):
        """Older clients omit it; both shapes have to work."""
        self.assertTrue(accepts(sign(payload())))

    def test_signature_field_is_covered_by_the_hash(self):
        """Editing ``signature`` must invalidate the payload.

        While the field was excluded from the check string this went unnoticed: it
        could be rewritten freely and the hash still matched.
        """
        blob = sign(payload(signature="original"))
        tampered = blob.replace("signature=original", "signature=tampered")
        self.assertNotEqual(tampered, blob, "the test modified nothing")
        self.assertFalse(accepts(tampered))

    def test_agrees_with_aiogram_on_real_shapes(self):
        for extra in (
            {},
            {"signature": "sig"},
            {"signature": "sig", "chat_type": "sender"},
            {"query_id": "AAHtest", "signature": "sig"},
            {"query_id": "AAHtest", "chat_instance": "-123", "signature": "sig"},
        ):
            blob = sign(payload(**extra))
            with self.subTest(extra=sorted(extra)):
                self.assertEqual(
                    accepts(blob),
                    check_webapp_signature(TOKEN, blob),
                    "our verdict differs from aiogram's",
                )


class TestForgery(unittest.TestCase):
    def test_honest_payload_is_accepted(self):
        self.assertTrue(accepts(sign(payload())))

    def test_tampered_user_is_rejected(self):
        """Impersonating someone by editing the id."""
        blob = sign(payload())
        tampered = blob.replace(
            urlencode({"user": json.dumps({"id": USER_ID, "username": "pexepo"})}),
            urlencode({"user": json.dumps({"id": 999, "username": "pexepo"})}),
        )
        self.assertNotEqual(tampered, blob, "the test modified nothing")
        self.assertFalse(accepts(tampered))

    def test_payload_signed_with_another_token_is_rejected(self):
        """Another bot must not be able to vouch for a user of ours."""
        self.assertFalse(accepts(sign(payload(), token="999999:SOMEONE-ELSE")))

    def test_blanked_hash_is_rejected(self):
        blob = sign(payload())
        self.assertFalse(accepts(blob[: blob.rfind("hash=") + 5] + "0" * 64))

    def test_missing_hash_is_rejected(self):
        self.assertFalse(accepts(urlencode(payload())))

    def test_empty_payload_is_rejected(self):
        self.assertFalse(accepts(""))

    def test_field_appended_after_signing_is_rejected(self):
        self.assertFalse(accepts(sign(payload()) + "&injected=1"))


class TestReplayWindow(unittest.TestCase):
    """A signature never expires by itself, so age is checked separately."""

    def test_stale_payload_is_rejected(self):
        old = sign(payload(auth_date=str(int(time.time()) - 7200)))
        self.assertFalse(accepts(old, max_age_seconds=3600))

    def test_fresh_payload_is_accepted(self):
        recent = sign(payload(auth_date=str(int(time.time()) - 60)))
        self.assertTrue(accepts(recent, max_age_seconds=3600))

    def test_future_dated_payload_is_rejected(self):
        """A payload dated well ahead would otherwise never expire."""
        future = sign(payload(auth_date=str(int(time.time()) + 86400)))
        self.assertFalse(accepts(future))

    def test_small_clock_skew_is_tolerated(self):
        """Phone clocks drift; a minute ahead is not an attack."""
        skewed = sign(payload(auth_date=str(int(time.time()) + 60)))
        self.assertTrue(accepts(skewed))

    def test_zero_max_age_disables_the_check(self):
        old = sign(payload(auth_date=str(int(time.time()) - 999999)))
        self.assertTrue(accepts(old, max_age_seconds=0))


class TestMalformedUser(unittest.TestCase):
    """A correct signature over nonsense is still nonsense."""

    def test_missing_user_is_rejected(self):
        self.assertFalse(accepts(sign({"auth_date": str(int(time.time()))})))

    def test_user_that_is_not_json_is_rejected(self):
        self.assertFalse(accepts(sign(payload(user="not json at all"))))

    def test_boolean_id_is_rejected(self):
        """isinstance(True, int) is True in Python, so this needs its own guard.

        Without it, {"id": true} would be accepted and become user 1.
        """
        blob = sign({"user": json.dumps({"id": True}), "auth_date": str(int(time.time()))})
        self.assertFalse(accepts(blob))

    def test_string_id_is_rejected(self):
        blob = sign({"user": json.dumps({"id": "111"}), "auth_date": str(int(time.time()))})
        self.assertFalse(accepts(blob))

    def test_missing_auth_date_is_rejected(self):
        self.assertFalse(accepts(sign({"user": json.dumps({"id": USER_ID})})))

    def test_non_numeric_auth_date_is_rejected(self):
        self.assertFalse(accepts(sign(payload(auth_date="yesterday"))))


class TestExtractedFields(unittest.TestCase):
    def test_user_details_are_returned(self):
        data = verify_init_data(sign(payload(query_id="AAHtest")), bot_token=TOKEN)
        self.assertEqual(data.tg_id, USER_ID)
        self.assertEqual(data.username, "pexepo")
        self.assertEqual(data.query_id, "AAHtest")

    def test_absent_username_is_none(self):
        """Users without a @handle exist and must still authenticate."""
        blob = sign({"user": json.dumps({"id": USER_ID}), "auth_date": str(int(time.time()))})
        self.assertIsNone(verify_init_data(blob, bot_token=TOKEN).username)

    def test_missing_bot_token_is_refused(self):
        """Without a token every signature fails; say so rather than reject all."""
        with self.assertRaises(InitDataError):
            verify_init_data(sign(payload()), bot_token="")


if __name__ == "__main__":
    unittest.main()
