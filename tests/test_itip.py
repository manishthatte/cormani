# SPDX-License-Identifier: GPL-3.0-or-later
#
# iCalendar in and out, and answering an invitation that arrived as mail.
#
# The parser tests are written against documents in the shapes the three
# senders actually use — Google's UTC stamps, Microsoft's Windows zone name
# beside a VTIMEZONE, and a hand-written one with folded and escaped values —
# because every one of those cost somebody a day once.
#
# `AnsweringByMail` is the half that has no API behind it, and its assertion is
# a row in the OUTBOX rather than a returned object: the lesson from stage 3's
# command bar and stage 4's inline reply is that only a test which ends at the
# store proves the wiring.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from cormani.calendar import invites, itip
from cormani.imap import envelope
from cormani.store import calendars as calendars_repo
from cormani.store import events as events_repo
from cormani.store import folders as folders_repo
from cormani.store import ingest, pending
from cormani.store.accounts import add_account
import support

UTC = dt.timezone.utc

GOOGLE_ICS = """BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
CALSCALE:GREGORIAN
METHOD:REQUEST
BEGIN:VEVENT
DTSTART:20260912T093000Z
DTEND:20260912T103000Z
DTSTAMP:20260901T120000Z
ORGANIZER;CN=Frances Baker:mailto:frances@example.com
UID:abc123@google.com
ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;CN=Man
 ish;X-NUM-GUESTS=0:mailto:someone@gmail.com
ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=OPT-PARTICIPANT;PARTSTAT=ACCEPTED;CN=Frances
 :mailto:frances@example.com
SUMMARY:DWCNT wavelengths\\, and the rest
DESCRIPTION:Bring the spectra.\\nAll of them.
LOCATION:Room 2\\; second floor
SEQUENCE:1
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""

OUTLOOK_ICS = """BEGIN:VCALENDAR
METHOD:REQUEST
VERSION:2.0
BEGIN:VTIMEZONE
TZID:India Standard Time
BEGIN:STANDARD
DTSTART:16010101T000000
TZOFFSETFROM:+0530
TZOFFSETTO:+0530
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:040000008200E00074C5B7101A82E008
SUMMARY:Quarterly review
DTSTART;TZID=India Standard Time:20260912T150000
DTEND;TZID=India Standard Time:20260912T160000
ORGANIZER;CN=IT Services:mailto:it@example.org
ATTENDEE;PARTSTAT=NEEDS-ACTION;CN=Manish:mailto:someone@gmail.com
END:VEVENT
END:VCALENDAR
"""


def invitation_mail(ics: str, *, subject: str = "Invitation: DWCNT") -> bytes:
    return (
        "From: Frances Baker <frances@example.com>\r\n"
        "To: someone@gmail.com\r\n"
        f"Subject: {subject}\r\n"
        "Message-ID: <invite-1@example.com>\r\n"
        'Content-Type: multipart/alternative; boundary="B"\r\n'
        "\r\n"
        "--B\r\n"
        "Content-Type: text/plain; charset=UTF-8\r\n"
        "\r\n"
        "You have been invited to a meeting.\r\n"
        "--B\r\n"
        'Content-Type: text/calendar; charset=UTF-8; method=REQUEST\r\n'
        "\r\n"
        f"{ics}"
        "--B--\r\n").encode("utf-8")


class Parsing(unittest.TestCase):
    def test_a_google_request_is_read_whole(self):
        found = itip.parse(GOOGLE_ICS)
        self.assertEqual(found.method, "REQUEST")
        self.assertEqual(found.uid, "abc123@google.com")
        self.assertEqual(found.sequence, 1)
        self.assertEqual(found.starts_at, "2026-09-12T09:30:00+00:00")
        self.assertEqual(found.ends_at, "2026-09-12T10:30:00+00:00")
        self.assertFalse(found.all_day)
        self.assertEqual(found.organiser_addr, "frances@example.com")
        self.assertEqual(found.organiser_name, "Frances Baker")

    def test_folded_lines_and_escaped_values_come_back_whole(self):
        found = itip.parse(GOOGLE_ICS)
        # The SUMMARY was folded across two lines AND carries an escaped comma.
        self.assertEqual(found.summary, "DWCNT wavelengths, and the rest")
        self.assertEqual(found.description, "Bring the spectra.\nAll of them.")
        self.assertEqual(found.location, "Room 2; second floor")
        # And the folded ATTENDEE is one attendee, not two.
        self.assertEqual([(a.address, a.response, a.optional)
                          for a in found.attendees],
                         [("someone@gmail.com", "needsAction", False),
                          ("frances@example.com", "accepted", True)])

    def test_a_windows_zone_falls_back_to_the_offset_in_the_file(self):
        """`zoneinfo` cannot name "India Standard Time"; the VTIMEZONE can."""
        found = itip.parse(OUTLOOK_ICS)
        self.assertEqual(found.starts_at, "2026-09-12T09:30:00+00:00")
        self.assertFalse(found.zone_unknown)

    def test_an_unresolvable_zone_says_so_rather_than_guessing(self):
        text = OUTLOOK_ICS.replace("TZOFFSETTO:+0530", "TZOFFSETTO:nonsense")
        found = itip.parse(text)
        self.assertTrue(found.zone_unknown)
        self.assertEqual(found.starts_at, "2026-09-12T15:00:00+00:00")

    def test_an_iana_zone_resolves_against_the_system(self):
        text = OUTLOOK_ICS.replace("India Standard Time", "Asia/Kolkata")
        found = itip.parse(text)
        self.assertEqual(found.starts_at, "2026-09-12T09:30:00+00:00")
        self.assertFalse(found.zone_unknown)

    def test_an_all_day_invitation_is_a_date(self):
        text = GOOGLE_ICS.replace("DTSTART:20260912T093000Z",
                                  "DTSTART;VALUE=DATE:20261108").replace(
            "DTEND:20260912T103000Z", "DTEND;VALUE=DATE:20261109")
        found = itip.parse(text)
        self.assertTrue(found.all_day)
        self.assertEqual((found.starts_at, found.ends_at),
                         ("2026-11-08", "2026-11-09"))

    def test_a_duration_stands_in_for_a_missing_end(self):
        text = GOOGLE_ICS.replace("DTEND:20260912T103000Z", "DURATION:PT45M")
        self.assertEqual(itip.parse(text).ends_at, "2026-09-12T10:15:00+00:00")

    def test_a_date_with_no_end_at_all_lasts_one_day(self):
        text = GOOGLE_ICS.replace("DTSTART:20260912T093000Z",
                                  "DTSTART;VALUE=DATE:20261108").replace(
            "DTEND:20260912T103000Z", "")
        self.assertEqual(itip.parse(text).ends_at, "2026-11-09")

    def test_a_cancellation_is_recognised(self):
        text = GOOGLE_ICS.replace("METHOD:REQUEST", "METHOD:CANCEL")
        self.assertTrue(itip.parse(text).is_cancellation)

    def test_a_recurring_invitation_is_marked_and_not_expanded(self):
        text = GOOGLE_ICS.replace("SEQUENCE:1", "RRULE:FREQ=WEEKLY;COUNT=8")
        self.assertTrue(itip.parse(text).recurring)

    def test_rubbish_is_not_an_invitation(self):
        for text in ("", "hello", "BEGIN:VCALENDAR\nEND:VCALENDAR\n"):
            self.assertIsNone(itip.parse(text))


class Replying(unittest.TestCase):
    def setUp(self):
        self.invitation = itip.parse(GOOGLE_ICS)

    def test_a_reply_carries_one_attendee_and_the_method(self):
        text = itip.build_reply(self.invitation, "someone@gmail.com",
                                "accepted", name="Manish",
                                now=dt.datetime(2026, 9, 1, 12, tzinfo=UTC))
        self.assertIn("METHOD:REPLY", text)
        self.assertIn("UID:abc123@google.com", text)
        self.assertIn("SEQUENCE:1", text)
        self.assertIn("DTSTAMP:20260901T120000Z", text)
        self.assertIn('ATTENDEE;PARTSTAT=ACCEPTED;CN="Manish":'
                      'mailto:someone@gmail.com', text)
        self.assertEqual(text.count("ATTENDEE"), 1)

    def test_a_reply_is_valid_input_to_the_parser(self):
        text = itip.build_reply(self.invitation, "someone@gmail.com",
                                "declined", name="Manish")
        again = itip.parse(text)
        self.assertEqual(again.method, "REPLY")
        self.assertEqual(again.uid, self.invitation.uid)
        self.assertEqual(again.response_of("someone@gmail.com"), "declined")
        self.assertEqual(again.summary, self.invitation.summary)

    def test_long_values_are_folded_at_seventy_five_octets(self):
        long_one = itip.parse(GOOGLE_ICS.replace(
            "SUMMARY:DWCNT wavelengths\\, and the rest",
            "SUMMARY:" + "wavelength " * 20))
        text = itip.build_reply(long_one, "someone@gmail.com", "accepted")
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)
        self.assertEqual(itip.parse(text).summary, long_one.summary)

    def test_an_unanswerable_response_is_refused(self):
        with self.assertRaises(ValueError):
            itip.build_reply(self.invitation, "someone@gmail.com", "needsAction")


class InMail(unittest.TestCase):
    """From the bytes on the wire to an invitation the interface can draw."""

    def setUp(self):
        self.con = support.temp_store(self)
        self.root = Path(tempfile.mkdtemp(prefix="cormani-test-att-"))
        self.addCleanup(_rmtree, self.root)
        self.account = add_account(self.con, address="someone@gmail.com",
                                   provider="google")
        self.folder = folders_repo.ensure_folder(self.con, self.account,
                                                 "INBOX", role="inbox")

    def deliver(self, ics: str = GOOGLE_ICS) -> int:
        env = envelope.read(invitation_mail(ics))
        stored = ingest.store_message(self.con, self.folder, 101, env,
                                      attachments_root=self.root,
                                      account_id=self.account)
        return stored.message_id

    def test_the_calendar_part_is_not_the_body_of_the_message(self):
        """Google puts the invitation in a bare text/calendar alternative.

        Read as a body — which is what a maintype test does — every invitation
        from Google previews as BEGIN:VCALENDAR.
        """
        message_id = self.deliver()
        row = self.con.execute("SELECT body_text, preview FROM message "
                               "WHERE id = ?", (message_id,)).fetchone()
        self.assertEqual(row["body_text"].strip(),
                         "You have been invited to a meeting.")
        self.assertNotIn("VCALENDAR", row["preview"])

    def test_an_invitation_is_found_and_parsed(self):
        found = invites.find(self.con, self.deliver(), self.root)
        self.assertIsNotNone(found)
        self.assertEqual(found.invitation.uid, "abc123@google.com")
        self.assertEqual(found.address, "someone@gmail.com")
        self.assertEqual(found.my_response, "needsAction")
        self.assertFalse(found.in_a_calendar)

    def test_a_message_with_no_invitation_returns_nothing(self):
        env = envelope.read(b"From: a@b.c\r\nSubject: Hello\r\n\r\nNo calendar.\r\n")
        stored = ingest.store_message(self.con, self.folder, 202, env,
                                      attachments_root=self.root,
                                      account_id=self.account)
        self.assertIsNone(invites.find(self.con, stored.message_id, self.root))

    def test_the_invitation_is_joined_to_the_calendar_by_its_uid(self):
        calendar = calendars_repo.ensure_calendar(self.con, self.account,
                                                  "primary")
        event_id = events_repo.upsert(
            self.con, calendar, "srv-1",
            {"ical_uid": "abc123@google.com", "summary": "DWCNT",
             "organiser_addr": "frances@example.com",
             "starts_at": "2026-09-12T09:30:00+00:00",
             "ends_at": "2026-09-12T10:30:00+00:00"})
        found = invites.find(self.con, self.deliver(), self.root)
        self.assertTrue(found.in_a_calendar)
        self.assertEqual(found.event_id, event_id)


class Answering(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.root = Path(tempfile.mkdtemp(prefix="cormani-test-att-"))
        self.addCleanup(_rmtree, self.root)
        self.account = add_account(self.con, address="someone@gmail.com",
                                   provider="google", display_name="Manish")
        self.folder = folders_repo.ensure_folder(self.con, self.account,
                                                 "INBOX", role="inbox")
        env = envelope.read(invitation_mail(GOOGLE_ICS))
        self.message = ingest.store_message(
            self.con, self.folder, 101, env, attachments_root=self.root,
            account_id=self.account).message_id

    def test_answering_a_meeting_in_a_calendar_goes_to_the_provider(self):
        calendar = calendars_repo.ensure_calendar(self.con, self.account,
                                                  "primary")
        events_repo.upsert(self.con, calendar, "srv-1",
                           {"ical_uid": "abc123@google.com",
                            "organiser_addr": "frances@example.com",
                            "starts_at": "2026-09-12T09:30:00+00:00",
                            "ends_at": "2026-09-12T10:30:00+00:00"})
        found = invites.find(self.con, self.message, self.root)
        answer = invites.answer(self.con, found, "accepted", root=self.root)
        self.assertEqual(answer.kind, invites.ANSWER_CALENDAR)
        self.assertEqual(
            events_repo.get_event(self.con, found.event_id).my_response,
            "accepted")
        self.assertEqual(pending.counts(self.con), {})     # nothing to MAIL

    def test_answering_a_meeting_that_is_in_no_calendar_goes_by_mail(self):
        found = invites.find(self.con, self.message, self.root)
        answer = invites.answer(self.con, found, "tentative",
                                comment="I may be late", root=self.root)
        self.assertEqual(answer.kind, invites.ANSWER_MAIL)
        self.assertIsNotNone(answer.draft_id)
        row = self.con.execute(
            "SELECT subject, to_addrs, draft FROM message WHERE id = ?",
            (answer.draft_id,)).fetchone()
        self.assertEqual(row["subject"],
                         "Tentative: DWCNT wavelengths, and the rest")
        self.assertEqual(row["to_addrs"], "frances@example.com")
        self.assertEqual(row["draft"], 1)
        # And it is IN THE OUTBOX, which is the half a widget test would miss.
        ops = self.con.execute(
            "SELECT kind, message_id FROM pending_op").fetchall()
        self.assertEqual([(o["kind"], o["message_id"]) for o in ops],
                         [("send", answer.draft_id)])

    def test_the_queued_reply_is_a_calendar_part_carrying_the_method(self):
        from cormani.compose import build
        from cormani.store import drafts as drafts_repo

        found = invites.find(self.con, self.message, self.root)
        answer = invites.answer(self.con, found, "accepted", root=self.root)
        draft = drafts_repo.load(self.con, answer.draft_id)
        self.assertEqual(drafts_repo.missing(draft), [])
        message = build.build(draft)
        parts = [p for p in message.walk()
                 if p.get_content_type() == "text/calendar"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_param("method"), "REPLY")
        text = parts[0].get_payload(decode=True).decode("utf-8")
        self.assertEqual(itip.parse(text).response_of("someone@gmail.com"),
                         "accepted")

    def test_a_cancellation_takes_the_meeting_out_of_the_day(self):
        calendar = calendars_repo.ensure_calendar(self.con, self.account,
                                                  "primary")
        events_repo.upsert(self.con, calendar, "srv-1",
                           {"ical_uid": "abc123@google.com",
                            "starts_at": "2026-09-12T09:30:00+00:00",
                            "ends_at": "2026-09-12T10:30:00+00:00"})
        env = envelope.read(invitation_mail(
            GOOGLE_ICS.replace("METHOD:REQUEST", "METHOD:CANCEL"),
            subject="Cancelled: DWCNT"))
        cancelled = ingest.store_message(self.con, self.folder, 102, env,
                                         attachments_root=self.root,
                                         account_id=self.account).message_id
        found = invites.find(self.con, cancelled, self.root)
        self.assertTrue(found.invitation.is_cancellation)
        self.assertEqual(invites.cancelled_events(self.con, found), 1)
        self.assertEqual(events_repo.counts_by_calendar(self.con).get(calendar, 0),
                         0)


def _rmtree(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
