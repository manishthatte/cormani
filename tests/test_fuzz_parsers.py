# SPDX-License-Identifier: GPL-3.0-or-later
#
# Parsers that eat untrusted bytes must not fall over.
#
# PLAN.txt Stage 9: fuzz MIME/headers, iCalendar, DSNs. This is not a
# coverage-guided fuzzer — there is no AFL in the Debian dependency set, and
# CONVENTIONS.txt §3 forbids vendoring one. It is a deterministic battery of
# hostile and degenerate inputs that MUST return or raise a typed error, never
# take the process down. Expanding the lists is cheap; a panic in `envelope.read`
# on live mail is not.
#
# © Manish Jagdish Thatte
import unittest

from cormani.calendar import itip
from cormani.imap import envelope


# Bytes that have historically broken mail parsers: truncated MIME, bogus
# encodings, NULs, huge lines, nested multipart with no end.
_MIME_BLOBS = [
    b"",
    b"\x00\x01\x02",
    b"From: \xff\xfe\n\n",
    b"Content-Type: multipart/mixed; boundary=\n\n--\n",
    b"Subject: =" + b"?UTF-8?B?%%%%?" + b"=\n\n",
    b"Content-Type: text/plain; charset=totally-unknown\n\nhello",
    b"Content-Type: message/rfc822\n\n" + b"A" * 200_000,
    b"MIME-Version: 1.0\nContent-Type: multipart/report; "
    b"report-type=delivery-status; boundary=b\n\n--b\n"
    b"Content-Type: message/delivery-status\n\n"
    b"Final-Recipient: rfc822; a@b.c\nAction: failed\nStatus: 5.1.1\n\n--b--\n",
    bytes(range(256)) * 40,
    b"From: " + (b"A" * 10_000) + b"\n\nbody",
    b"Content-Disposition: attachment; filename=\"" + (b"../" * 50) + b"x\"\n\n",
]

_ICAL_TEXTS = [
    "",
    "BEGIN:VCALENDAR\nEND:VCALENDAR\n",
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\n" + ("X-PAD:x\n" * 5000) + "END:VEVENT\nEND:VCALENDAR\n",
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:notadate\nSUMMARY:x\n"
    "END:VEVENT\nEND:VCALENDAR\n",
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:" + ("\\n" * 1000) + "\n"
    "DTSTART:20260828T100000Z\nEND:VEVENT\nEND:VCALENDAR\n",
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\nATTACH;VALUE=URI:file:///etc/passwd\n"
    "DTSTART:20260828T100000Z\nSUMMARY:x\nEND:VEVENT\nEND:VCALENDAR\n",
    "\x00BEGIN:VCALENDAR\n",
    "BEGIN:VCALENDAR\n" + "A" * 100_000,
]


class TestEnvelopeNeverTakesTheProcessDown(unittest.TestCase):
    def test_degenerate_blobs_return_an_envelope(self):
        for i, raw in enumerate(_MIME_BLOBS):
            with self.subTest(i=i, size=len(raw)):
                env = envelope.read(raw)
                self.assertIsInstance(env, envelope.Envelope)
                self.assertIsInstance(env.subject, str)
                self.assertIsInstance(env.body_text, str)

    def test_random_seeded_noise_is_survivable(self):
        # Deterministic "fuzz": same seed every run, so a failure is reproducible.
        rng = _LCG(0xC0FFEE)
        for i in range(40):
            raw = bytes(rng.byte() for _ in range(rng.between(0, 4000)))
            with self.subTest(i=i):
                env = envelope.read(raw)
                self.assertIsInstance(env.message_id, str)


class TestItipNeverTakesTheProcessDown(unittest.TestCase):
    def test_degenerate_calendars_return_none_or_an_invitation(self):
        for i, text in enumerate(_ICAL_TEXTS):
            with self.subTest(i=i):
                # May return None for unparseable; must not raise.
                got = itip.parse(text)
                self.assertTrue(got is None or hasattr(got, "summary"))

    def test_random_seeded_text_is_survivable(self):
        rng = _LCG(0xBADC0DE)
        for i in range(40):
            text = "".join(chr(rng.between(0, 255)) for _ in range(rng.between(0, 2000)))
            with self.subTest(i=i):
                itip.parse(text)


class TestDeliveryClassificationSurvivesJunk(unittest.TestCase):
    def test_classify_on_broken_envelopes(self):
        for i, raw in enumerate(_MIME_BLOBS[:12]):
            with self.subTest(i=i):
                env = envelope.read(raw)
                # delivery is already on the Envelope from read(); poking the
                # helpers with junk headers must not raise either.
                self.assertTrue(hasattr(env, "delivery"))


class _LCG:
    """Tiny deterministic generator. No stdlib random — same answer everywhere."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFF

    def byte(self) -> int:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state & 0xFF

    def between(self, lo: int, hi: int) -> int:
        if hi <= lo:
            return lo
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return lo + (self.state % (hi - lo + 1))


if __name__ == "__main__":
    unittest.main()
