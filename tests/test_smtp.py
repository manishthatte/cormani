# SPDX-License-Identifier: GPL-3.0-or-later
#
# Sending: the submission client, and the outbox that drives it.
#
# A REAL `smtplib.SMTP` against a server in this process — tests/fakesmtp.py
# says what that buys and what it does not. The failures are the point: half of
# these tests are about a message that did NOT go, because that is the half a
# person finds out about days later if the client gets it wrong.
#
# THE DISTINCTION UNDER TEST IS PERMANENT AGAINST TRANSIENT. A refused password
# retried on a schedule is how fifteen accounts get an address blocked; a
# message abandoned because a server hiccuped is a message the user thinks they
# sent. Every failure below asserts which one it is.
#
# AND ONE THAT IS NEITHER: a partial send. Some recipients accepted and some
# refused means the message HAS gone, and retrying it would deliver a second
# copy to everyone it reached.
#
# © Manish Jagdish Thatte
import unittest

import support                                              # noqa: F401
import fakesmtp
from test_threads import Store

from cormani.auth.credentials import Credential
from cormani.compose.draft import Draft
from cormani.smtp import outbox
from cormani.smtp.client import Sender, SendFailed
from cormani.store import accounts, drafts, edits, folders, messages, pending

PASSWORD = Credential(method="password", user="owner@manitlab.example",
                      secret="correct-horse")
TOKEN = Credential(method="oauth2", user="owner@manitlab.example",
                   secret="ya29.fake-token")
RAW = (b"From: owner@manitlab.example\r\nTo: lyle@covalent.example\r\n"
       b"Subject: Wavelengths\r\n\r\nThe body.\r\n.dotted line\r\n")


def connect(server, credential=PASSWORD, *, port: int = 587) -> Sender:
    return Sender.connect("smtp.fake.invalid", port, credential,
                          factory=fakesmtp.factory_for(server))


class TestTheClient(unittest.TestCase):
    def test_it_starts_tls_authenticates_and_delivers(self):
        server = fakesmtp.Server()
        with connect(server) as sender:
            sent = sender.send("owner@manitlab.example", ["lyle@covalent.example"], RAW)
        self.assertTrue(server.tls)
        self.assertTrue(server.authenticated)
        self.assertEqual(sent.recipients, ("lyle@covalent.example",))
        self.assertFalse(sent.partial)

    def test_the_message_arrives_byte_for_byte(self):
        # Dot-stuffing is smtplib's and un-stuffing is the server's; a body
        # whose line begins with a full stop is where that shows.
        server = fakesmtp.Server()
        with connect(server) as sender:
            sender.send("owner@manitlab.example", ["lyle@covalent.example"], RAW)
        _sender, _rcpts, raw = server.delivered[0]
        self.assertIn(b".dotted line", raw)
        self.assertIn(b"Subject: Wavelengths", raw)

    def test_a_token_authenticates_the_same_way_imap_does(self):
        server = fakesmtp.Server()
        with connect(server, TOKEN) as sender:
            sender.send("owner@manitlab.example", ["lyle@covalent.example"], RAW)
        self.assertTrue(server.authenticated)
        self.assertEqual(server.user, "owner@manitlab.example")

    def test_a_refused_credential_is_permanent(self):
        server = fakesmtp.Server()
        wrong = Credential(method="password", user="owner@manitlab.example",
                           secret="hunter2")
        with self.assertRaises(SendFailed) as caught:
            connect(server, wrong)
        self.assertTrue(caught.exception.permanent)
        self.assertNotIn("hunter2", str(caught.exception))

    def test_a_stale_token_is_permanent_too(self):
        # The auth layer already refreshed it once before this connection was
        # made; by the time a refusal arrives here it needs a person.
        server = fakesmtp.Server()
        stale = Credential(method="oauth2", user="owner@manitlab.example",
                           secret="expired")
        with self.assertRaises(SendFailed) as caught:
            connect(server, stale)
        self.assertTrue(caught.exception.permanent)

    def test_a_server_that_will_not_encrypt_is_refused(self):
        server = fakesmtp.Server(offer_starttls=False, require_auth=False)
        with self.assertRaises(SendFailed) as caught:
            connect(server)
        self.assertTrue(caught.exception.permanent)
        self.assertIn("STARTTLS", str(caught.exception))
        self.assertEqual(server.delivered, [])

    def test_every_recipient_refused_is_permanent(self):
        server = fakesmtp.Server(refuse=("lyle@covalent.example",))
        with connect(server) as sender:
            with self.assertRaises(SendFailed) as caught:
                sender.send("owner@manitlab.example", ["lyle@covalent.example"], RAW)
        self.assertTrue(caught.exception.permanent)

    def test_some_recipients_refused_is_a_send_that_happened(self):
        server = fakesmtp.Server(refuse=("nobody@example.invalid",))
        with connect(server) as sender:
            sent = sender.send("owner@manitlab.example",
                               ["lyle@covalent.example", "nobody@example.invalid"],
                               RAW)
        self.assertTrue(sent.partial)
        self.assertIn("nobody@example.invalid", sent.refused)
        self.assertEqual(len(server.delivered), 1)

    def test_a_four_hundred_reply_is_worth_trying_again_and_a_five_is_not(self):
        for reply, permanent in (("452 4.3.1 Insufficient storage", False),
                                 ("552 5.3.4 Message too big", True)):
            server = fakesmtp.Server(data_reply=reply)
            with connect(server) as sender:
                with self.assertRaises(SendFailed) as caught:
                    sender.send("owner@manitlab.example", ["lyle@covalent.example"], RAW)
            self.assertEqual(caught.exception.permanent, permanent, reply)

    def test_no_recipients_and_no_host_are_refused_without_a_connection(self):
        server = fakesmtp.Server()
        with connect(server) as sender:
            with self.assertRaises(SendFailed):
                sender.send("owner@manitlab.example", [], RAW)
        with self.assertRaises(SendFailed) as caught:
            Sender.connect("", 587, PASSWORD)
        self.assertTrue(caught.exception.permanent)


class OutboxCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Store(self)
        self.con = self.fixture.con
        self.account = accounts.get_account(self.con, self.fixture.account)
        self.sent_folder = folders.ensure_folder(
            self.con, self.fixture.account, "[Gmail]/Sent Mail",
            display_name="Sent", role=folders.ROLE_SENT)
        self.server = fakesmtp.Server()
        self.appended = []

    def queue(self, **changes) -> int:
        fields = dict(account_id=self.fixture.account,
                      from_address="owner@manitlab.example", from_name="Manish",
                      to="lyle@covalent.example", subject="Wavelengths",
                      body="1064 and 785.")
        fields.update(changes)
        row_id, _rfc = drafts.save(self.con, Draft(**fields))
        outbox.queue(self.con, row_id)
        return row_id

    def drain(self, *, files_sent=False, append=True):
        return outbox.send_pending(
            self.con, self.account, PASSWORD,
            connect=lambda host, port, cred: connect(self.server, cred),
            append=(lambda path, raw: self.appended.append((path, raw)))
            if append else None,
            files_sent=files_sent, host="smtp.fake.invalid", port=587)

    def row(self, message_id):
        return messages.get_row(self.con, message_id)


class TestTheOutbox(OutboxCase):
    def test_a_queued_message_goes_and_the_row_becomes_a_sent_one(self):
        row_id = self.queue()
        self.assertEqual(outbox.waiting(self.con), 1)

        report = self.drain()
        self.assertEqual((report.sent, report.stuck), (1, 0))
        self.assertEqual(outbox.waiting(self.con), 0)
        self.assertEqual(len(self.server.delivered), 1)

        row = self.row(row_id)
        self.assertFalse(row.draft)
        self.assertEqual(row.folder_label, "Sent")
        self.assertTrue(row.seen)

    def test_nothing_queued_opens_no_connection(self):
        # A login attempt on every sync is what makes a provider start asking
        # whether it was really you.
        report = outbox.send_pending(
            self.con, self.account, PASSWORD,
            connect=lambda *a, **k: self.fail("connected with nothing to send"),
            host="smtp.fake.invalid", port=587)
        self.assertEqual(report.tried, 0)

    def test_the_copy_is_filed_when_the_provider_does_not_do_it(self):
        self.queue()
        self.drain(files_sent=False)
        self.assertEqual(len(self.appended), 1)
        self.assertEqual(self.appended[0][0], "[Gmail]/Sent Mail")

    def test_and_is_not_when_the_provider_does(self):
        # Google and Microsoft both file a copy of anything submitted through
        # their SMTP. Appending another gives the user two of everything.
        self.queue()
        self.drain(files_sent=True)
        self.assertEqual(self.appended, [])

    def test_a_reply_marks_the_message_it_answers_as_answered(self):
        original = self.fixture.store(subject="Wavelengths",
                                      message_id="<root@x>")
        self.queue(subject="Re: Wavelengths", in_reply_to="<root@x>",
                   references="<root@x>")
        self.drain()
        self.assertTrue(self.row(original).answered)

    def test_the_sent_copy_is_in_the_same_conversation(self):
        original = self.fixture.store(subject="Wavelengths",
                                      message_id="<root@x>")
        row_id = self.queue(subject="Re: Wavelengths", in_reply_to="<root@x>",
                            references="<root@x>")
        self.drain()
        self.assertEqual(self.row(row_id).thread_key,
                         self.row(original).thread_key)

    def test_a_draft_discarded_after_being_queued_is_simply_forgotten(self):
        row_id = self.queue()
        drafts.discard(self.con, row_id)
        report = self.drain()
        self.assertEqual((report.sent, report.stuck), (0, 0))
        self.assertEqual(outbox.waiting(self.con), 0)
        self.assertEqual(self.server.delivered, [])

    def test_an_attachment_that_moved_stops_that_message_and_says_which(self):
        import tempfile
        from pathlib import Path

        from cormani.compose.draft import Attachment

        directory = Path(tempfile.mkdtemp())
        path = directory / "figures.pdf"
        path.write_bytes(b"%PDF")
        row_id = self.queue(attachments=(Attachment(path=str(path)),))
        path.unlink()

        report = self.drain()
        self.assertEqual((report.sent, report.stuck), (0, 1))
        self.assertIn("figures.pdf", " ".join(report.notes))
        self.assertTrue(self.row(row_id).draft)      # still a draft, still there

    def test_a_permanent_refusal_stops_that_message_and_not_the_next(self):
        self.server.refuse = {"nobody@example.invalid"}
        first = self.queue(to="nobody@example.invalid")
        second = self.queue(to="lyle@covalent.example", subject="Second")
        report = self.drain()
        self.assertEqual((report.sent, report.stuck), (1, 1))
        self.assertTrue(self.row(first).draft)
        self.assertFalse(self.row(second).draft)

    def test_a_transient_failure_stops_the_run_and_keeps_the_queue(self):
        self.server.data_reply = "451 4.3.0 Try again"
        self.queue()
        self.queue(subject="Second")
        report = self.drain()
        self.assertEqual((report.sent, report.stuck), (0, 0))
        self.assertEqual(outbox.waiting(self.con), 2)
        self.assertTrue(report.notes)
        # And it is not marked as given up on: it will be tried again.
        ops = pending.unsent_of_kind(self.con, self.account.id,
                                     pending.KIND_SEND, include_stuck=True)
        self.assertFalse(any(op.stuck for op in ops))

    def test_a_failure_to_file_the_copy_does_not_unsend_the_message(self):
        def refuse(path, raw):
            raise RuntimeError("mailbox is full")

        row_id = self.queue()
        report = outbox.send_pending(
            self.con, self.account, PASSWORD,
            connect=lambda host, port, cred: connect(self.server, cred),
            append=refuse, files_sent=False, host="smtp.fake.invalid", port=587)
        self.assertEqual(report.sent, 1)
        self.assertEqual(outbox.waiting(self.con), 0)
        self.assertFalse(self.row(row_id).draft)

    def test_the_partial_send_is_reported_rather_than_retried(self):
        self.server.refuse = {"nobody@example.invalid"}
        self.queue(to="lyle@covalent.example, nobody@example.invalid")
        report = self.drain()
        self.assertEqual(report.sent, 1)
        self.assertIn("nobody@example.invalid", " ".join(report.notes))
        self.assertEqual(outbox.waiting(self.con), 0)

    def test_the_server_gets_the_bcc_and_the_message_does_not(self):
        self.queue(bcc="quiet@example.org")
        self.drain()
        _sender, rcpts, raw = self.server.delivered[0]
        self.assertIn("quiet@example.org", rcpts)
        self.assertNotIn(b"quiet@example.org", raw)


class TestTheSentCopyComingBack(OutboxCase):
    """The server's own copy of a sent message, meeting the local one."""

    def test_it_adopts_the_local_row_rather_than_making_a_second(self):
        from cormani.imap import envelope
        from cormani.store import ingest

        row_id = self.queue()
        self.drain()
        raw = self.server.delivered[0][2]
        before = self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0]

        # What the next sync does with the copy the server filed.
        ingest.store_message(self.con, self.sent_folder, 4242,
                             envelope.read(raw), flags=["\\Seen"])
        after = self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(
            self.con.execute("SELECT uid FROM message WHERE id = ?",
                             (row_id,)).fetchone()[0], 4242)

    def test_a_message_that_is_not_ours_is_still_a_new_row(self):
        from cormani.imap import envelope
        from cormani.store import ingest

        before = self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        ingest.store_message(
            self.con, self.sent_folder, 99,
            envelope.read(b"From: a@x\r\nSubject: Someone else's\r\n"
                          b"Message-ID: <other@x>\r\n\r\nbody\r\n"))
        after = self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        self.assertEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main()
