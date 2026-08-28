# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a message says about itself: bulk, and delivery failure.
#
# THE COST OF BEING WRONG IS ASYMMETRIC IN BOTH DIRECTIONS, and that asymmetry
# is what most of these tests are about. A message wrongly called bulk is
# hidden from the triage queue, which is the one place an unfiled reply is
# meant to become visible. An address wrongly called dead is one the composer
# warns about for ever, and the user works around a client they cannot trust by
# pasting the address somewhere else.
#
# So the tests here are mostly about what must NOT be classified: a bounce is
# not bulk although it declares itself automatic; a delayed notice is not a
# failure; a 4.x.x is not permanent; a human message with an Auto-Submitted
# header saying "no" is a human message.
#
# The messages are written out in full rather than built by a helper. A DSN is
# a structure with three parts and a per-recipient block, and a helper that
# assembled one would be a second implementation of the thing under test.
#
# © Manish Jagdish Thatte
import unittest
from email.parser import BytesParser
from email.policy import compat32

from cormani.imap import delivery


def parse(raw: bytes):
    return BytesParser(policy=compat32).parsebytes(raw)


def read(raw: bytes) -> delivery.Delivery:
    return delivery.read(parse(raw))


GOOGLE_DSN = b"""From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: manish@manitlab.invalid
Subject: Delivery Status Notification (Failure)
Auto-Submitted: auto-replied
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain; charset=UTF-8

Your message could not be delivered.

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com

Final-Recipient: rfc822; j.harrington@covalent.example
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to reach does
 not exist. Please try double-checking the recipient's email address.

--B
Content-Type: text/rfc822-headers

Message-ID: <original-42@manitlab.invalid>
Subject: DWCNT wavelength question

--B--
"""


class TestDeliveryReports(unittest.TestCase):
    def test_it_names_the_address_that_failed(self):
        # Which is not the From (a mailer daemon), not the To (the user), and
        # not in the subject. RFC 3464 puts it in the report part and that is
        # the only place it is reliably machine-readable.
        found = read(GOOGLE_DSN)
        self.assertTrue(found.is_bounce)
        self.assertEqual(found.recipient, "j.harrington@covalent.example")
        self.assertEqual(found.status, "5.1.1")
        self.assertTrue(found.permanent)

    def test_a_bounce_is_never_bulk_however_it_declares_itself(self):
        """Every DSN carries Auto-Submitted and most carry Precedence: bulk. A
        delivery failure hidden by the bulk filter is the single most useful
        message in the mailbox thrown away."""
        self.assertFalse(read(GOOGLE_DSN).is_bulk)
        noisy = GOOGLE_DSN.replace(b"Auto-Submitted: auto-replied",
                                   b"Auto-Submitted: auto-replied\n"
                                   b"Precedence: bulk\n"
                                   b"List-Unsubscribe: <https://x.example/u>")
        found = read(noisy)
        self.assertTrue(found.is_bounce)
        self.assertFalse(found.is_bulk)

    def test_the_diagnostic_keeps_its_continuation_lines(self):
        # Reading only the first line gives a sentence that stops before it
        # says anything.
        found = read(GOOGLE_DSN)
        self.assertIn("does not exist", found.diagnostic)
        self.assertNotIn("\n", found.diagnostic)

    def test_the_transport_label_is_stripped_and_the_words_are_not(self):
        found = read(GOOGLE_DSN)
        self.assertFalse(found.diagnostic.lower().startswith("smtp;"))
        self.assertTrue(found.diagnostic.startswith("550"))

    def test_it_finds_the_message_that_bounced(self):
        """Without this a bounce is a fact about an address; with it, it is a
        fact about a conversation, and the tracking layer can file it."""
        self.assertEqual(read(GOOGLE_DSN).original_message_id,
                         "<original-42@manitlab.invalid>")

    def test_it_finds_it_in_a_wrapped_message_rather_than_bare_headers(self):
        # Servers send either; the same answer must come out of both.
        wrapped = GOOGLE_DSN.replace(
            b"Content-Type: text/rfc822-headers",
            b"Content-Type: message/rfc822")
        self.assertEqual(read(wrapped).original_message_id,
                         "<original-42@manitlab.invalid>")

    def test_a_delayed_notice_is_not_a_failure(self):
        """"Not delivered yet, still trying" arrives as a well-formed DSN.
        Treating it as a bounce blacklists an address whose mail is about to
        arrive."""
        delayed = GOOGLE_DSN.replace(b"Action: failed", b"Action: delayed")
        found = read(delayed)
        self.assertFalse(found.is_bounce)
        self.assertEqual(found.recipient, "")

    def test_a_transient_failure_is_reported_but_is_not_permanent(self):
        # A full mailbox on Tuesday is not a dead address on Wednesday.
        soft = GOOGLE_DSN.replace(b"Status: 5.1.1", b"Status: 4.2.2")
        found = read(soft)
        self.assertTrue(found.is_bounce)
        self.assertFalse(found.permanent)

    def test_the_first_FAILED_recipient_wins_and_not_the_first_block(self):
        """A DSN can name several recipients, and the one that succeeded is
        listed too. Taking the first block would mark a working address dead."""
        two = GOOGLE_DSN.replace(
            b"Final-Recipient: rfc822; j.harrington@covalent.example\n"
            b"Action: failed",
            b"Final-Recipient: rfc822; works@covalent.example.com\n"
            b"Action: delivered\n"
            b"Status: 2.0.0\n"
            b"\n"
            b"Final-Recipient: rfc822; j.harrington@covalent.example\n"
            b"Action: failed")
        self.assertEqual(read(two).recipient,
                         "j.harrington@covalent.example")

    def test_a_report_without_the_rfc822_type_label_still_yields_an_address(self):
        bare = GOOGLE_DSN.replace(b"Final-Recipient: rfc822; ",
                                  b"Final-Recipient: ")
        self.assertEqual(read(bare).recipient,
                         "j.harrington@covalent.example")


PLAIN_BOUNCE = b"""From: Mail Delivery System <MAILER-DAEMON@mx.example.org>
To: manish@manitlab.invalid
Subject: Undelivered Mail Returned to Sender

This is the mail system at host mx.example.org.

<someone@example.org>: host example.org said: 550 5.1.1 unknown user
"""


class TestTheHeuristic(unittest.TestCase):
    """The fallback, for a server that sends prose rather than a report."""

    def test_a_daemon_saying_undelivered_is_a_bounce(self):
        self.assertTrue(read(PLAIN_BOUNCE).is_bounce)

    def test_it_reports_no_recipient_rather_than_guessing_one(self):
        """The address is right there in the body, and reading it out would be
        a guess about prose. "This came back and I cannot tell you for whom" is
        the honest answer; the row is still shown for what it is."""
        self.assertEqual(read(PLAIN_BOUNCE).recipient, "")

    def test_a_daemon_with_an_ordinary_subject_is_not_a_bounce(self):
        # Postmaster writes about other things.
        ordinary = PLAIN_BOUNCE.replace(b"Subject: Undelivered Mail Returned "
                                        b"to Sender",
                                        b"Subject: Scheduled maintenance")
        self.assertFalse(read(ordinary).is_bounce)

    def test_a_person_with_a_bounce_like_subject_is_not_a_bounce(self):
        """Both conditions are required. Somebody forwarding a failure notice,
        or asking about one, must not mark anything dead."""
        person = PLAIN_BOUNCE.replace(
            b"From: Mail Delivery System <MAILER-DAEMON@mx.example.org>",
            b"From: Lyle Gordon <lyle@covalent.example>")
        self.assertFalse(read(person).is_bounce)


class TestBulk(unittest.TestCase):
    def test_the_list_headers_are_enough_on_their_own(self):
        for header in (b"List-Id: <weekly.example>",
                       b"List-Unsubscribe: <https://weekly.example/u>",
                       b"List-Post: <mailto:list@weekly.example>",
                       b"Precedence: bulk",
                       b"Auto-Submitted: auto-generated",
                       b"X-Auto-Response-Suppress: OOF"):
            raw = (b"From: Newsletter <news@weekly.example>\n"
                   b"Subject: Weekly digest\n" + header + b"\n\nHello.\n")
            self.assertTrue(read(raw).is_bulk, header)

    def test_an_ordinary_message_is_not_bulk(self):
        raw = (b"From: Lyle Gordon <lyle@covalent.example>\n"
               b"To: manish@manitlab.invalid\n"
               b"Subject: Re: DWCNT wavelength question\n\n"
               b"Happy to quote both wavelengths.\n")
        found = read(raw)
        self.assertFalse(found.is_bulk)
        self.assertFalse(found.is_bounce)

    def test_auto_submitted_no_is_the_statement_that_a_person_wrote_it(self):
        # RFC 3834's own value for "this is not automatic". Reading any
        # Auto-Submitted header as bulk would hide every message from a client
        # that sets it honestly.
        raw = (b"From: Lyle Gordon <lyle@covalent.example>\n"
               b"Subject: Re: DWCNT\n"
               b"Auto-Submitted: no\n\nHello.\n")
        self.assertFalse(read(raw).is_bulk)

    def test_nothing_in_a_body_makes_a_message_bulk(self):
        """The test is the sender's own declaration. A message that talks about
        unsubscribing is a message somebody may be waiting on an answer to."""
        raw = (b"From: Lyle Gordon <lyle@covalent.example>\n"
               b"Subject: How do I unsubscribe from your list?\n\n"
               b"Your newsletter has no unsubscribe link. Precedence: bulk?\n")
        self.assertFalse(read(raw).is_bulk)


class TestIngest(unittest.TestCase):
    """The chain end to end: a DSN reaching the bounce guard the composer reads.

    `contacts.note_bounce` was written for stage 4 and had NO CALLER until this
    — so the guard only ever knew what a person typed into it. The test that
    matters is the one that starts at bytes and ends at the guard.
    """

    def setUp(self):
        import support
        from cormani.store import accounts, contacts, folders

        self.con = support.temp_store(self)
        account = accounts.add_account(self.con, "manish@manitlab.invalid",
                                       "google")
        self.folder = folders.ensure_folder(self.con, account, "INBOX",
                                            display_name="Inbox", role="inbox")
        contact = contacts.add_contact(self.con, "Jane Harrington")
        contacts.add_handle(self.con, contact, "email",
                            "j.harrington@covalent.example")

    def store(self, raw: bytes, uid: int = 1):
        from cormani.imap import envelope
        from cormani.store import ingest
        return ingest.store_message(self.con, self.folder, uid,
                                    envelope.read(raw))

    def test_a_dsn_reaches_the_guard_the_composer_reads(self):
        from cormani.store import contacts

        self.store(GOOGLE_DSN)
        found = contacts.bounced(self.con,
                                 ["j.harrington@covalent.example"])
        self.assertIn("j.harrington@covalent.example", found)
        self.assertIn("does not exist",
                      found["j.harrington@covalent.example"]["note"])

    def test_a_transient_failure_does_not_mark_the_address_dead(self):
        from cormani.store import contacts

        self.store(GOOGLE_DSN.replace(b"Status: 5.1.1", b"Status: 4.2.2"))
        self.assertEqual(
            contacts.bounced(self.con,
                             ["j.harrington@covalent.example"]), {})

    def test_the_facts_are_written_onto_the_message_row(self):
        message_id = self.store(GOOGLE_DSN).message_id
        row = self.con.execute(
            "SELECT is_bulk, is_bounce, bounce_rcpt, bounce_status "
            "FROM message WHERE id = ?", (message_id,)).fetchone()
        self.assertEqual((row["is_bulk"], row["is_bounce"]), (0, 1))
        self.assertEqual(row["bounce_rcpt"],
                         "j.harrington@covalent.example")
        self.assertEqual(row["bounce_status"], "5.1.1")

    def test_re_ingesting_the_same_uid_does_not_double_the_bounce_count(self):
        """A sync that was interrupted re-fetches. The row is idempotent on
        (folder, uid) and the guard must not treat the second write as a second
        failure — an address that bounced once should not read as bounced four
        times because the import was restarted."""
        from cormani.store import contacts

        self.store(GOOGLE_DSN)
        before = contacts.bounced(
            self.con, ["j.harrington@covalent.example"])
        self.store(GOOGLE_DSN)
        after = contacts.bounced(
            self.con, ["j.harrington@covalent.example"])
        self.assertEqual(
            after["j.harrington@covalent.example"]["bounces"],
            before["j.harrington@covalent.example"]["bounces"])


if __name__ == "__main__":
    unittest.main()
