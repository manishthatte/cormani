# SPDX-License-Identifier: GPL-3.0-or-later
#
# Filing mail onto tracked threads, and the queue of what could not be filed.
#
# THE TWO MATCHERS ARE THE SUBJECT HERE, and the second one is why this file
# exists at all: correspondents answer by starting a FRESH message with a new
# subject, which header threading cannot see, and a client that only threads
# loses the reply silently. Most of what follows is about what each matcher
# must NOT claim — a wrongly-filed message is worse than an unfiled one,
# because it looks answered.
#
# THE QUEUE'S THREE NARROWINGS ARE TESTED SEPARATELY, because the prototype's
# measurement says all three are load-bearing: without a horizon, a relevance
# scope and deduplication the queue held 17,774 items where the answer was 40,
# and any one of them missing brings back most of that.
#
# The messages are built from bytes through `imap/envelope.py` and
# `store/ingest.py` rather than inserted as rows. Slower, and the only way the
# tests are about the thing the application does: `is_bulk`, `thread_key` and
# the bounce fields are all DERIVED on the way in, and a hand-written row would
# be testing a fixture's opinion of them.
#
# © Manish Jagdish Thatte
import datetime as dt
import unittest

import support

from cormani.imap import envelope
from cormani.store import (accounts, attach, contacts, folders, ingest, times,
                           touches, tracking, triage, views)
from cormani.store import messages as messages_repo

NOW = times.now_local()


def when(days_ago: int) -> str:
    return (NOW - dt.timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S %z")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = accounts.add_account(self.con, "manish@manitlab.invalid",
                                            "google")
        self.inbox = folders.ensure_folder(self.con, self.account, "INBOX",
                                           display_name="Inbox", role="inbox")
        self.sent = folders.ensure_folder(self.con, self.account, "Sent",
                                          display_name="Sent", role="sent")
        self.archive = folders.ensure_folder(self.con, self.account, "Archive",
                                             display_name="Archive",
                                             role="archive")
        self._uid = 0

    def store(self, folder, *, frm, to, subject, key, days_ago,
              headers=b"", body=b"Body text.\n", uid=None):
        self._uid += 1
        raw = (f"From: {frm}\nTo: {to}\nSubject: {subject}\n"
               f"Message-ID: {key}\nDate: {when(days_ago)}\n").encode()
        raw += headers + b"\n" + body
        return ingest.store_message(self.con, folder,
                                    self._uid if uid is None else uid,
                                    envelope.read(raw)).message_id

    def inbound(self, **kwargs):
        kwargs.setdefault("frm", "Lyle Gordon <lyle@covalent.example>")
        kwargs.setdefault("to", "manish@manitlab.invalid")
        return self.store(self.inbox, **kwargs)

    def outbound(self, **kwargs):
        kwargs.setdefault("frm", "manish@manitlab.invalid")
        kwargs.setdefault("to", "Lyle Gordon <lyle@covalent.example>")
        return self.store(self.sent, **kwargs)

    def tracked(self, title="DWCNT wavelengths", *, address=None):
        thread = tracking.create_thread(self.con, title,
                                        org="Covalent Example")
        if address is not False:
            contact = contacts.contact_for_address(
                self.con, address or "lyle@covalent.example",
                name="Lyle Gordon", create=True)
            tracking.link_contact(self.con, thread, contact.id)
        return thread


class TestTheMatchers(Fixture):
    def test_a_reply_with_a_fresh_subject_is_still_filed(self):
        """Matcher 2, and the reason it exists. There is nothing in this
        message's headers connecting it to the thread — no References, a new
        subject — and threading alone loses it entirely."""
        thread = self.tracked()
        first = self.outbound(subject="DWCNT wavelength question",
                              key="<out1@manitlab.invalid>", days_ago=9)
        touches.from_message(self.con, thread, first)
        self.inbound(subject="Pricing, as promised",
                     key="<in1@covalent.example>", days_ago=4)
        filed = attach.run(self.con)
        self.assertEqual(filed.by_address, 1)
        self.assertEqual([t.direction for t in touches.timeline(self.con, thread)],
                         ["out", "in"])

    def test_a_reply_in_the_chain_is_filed_by_threading(self):
        thread = self.tracked(address=False)
        first = self.outbound(subject="DWCNT wavelength question",
                              key="<out1@manitlab.invalid>", days_ago=9)
        touches.from_message(self.con, thread, first)
        self.inbound(subject="Re: DWCNT wavelength question",
                     key="<in1@covalent.example>", days_ago=4,
                     headers=b"In-Reply-To: <out1@manitlab.invalid>\n"
                             b"References: <out1@manitlab.invalid>\n")
        filed = attach.run(self.con)
        self.assertEqual(filed.threaded, 1)

    def test_mail_from_before_the_thread_began_is_NOT_swept_in(self):
        """The date bound on matcher 2, and it is load-bearing. Without it,
        starting a thread with a supplier files a decade of their earlier mail
        onto it — which is not history, it is a silence figure computed from
        the wrong end."""
        thread = self.tracked()
        opening = self.outbound(subject="First contact",
                                key="<out1@manitlab.invalid>", days_ago=10)
        touches.from_message(self.con, thread, opening)
        self.inbound(subject="An old matter entirely",
                     key="<old@covalent.example>", days_ago=400)
        attach.run(self.con)
        self.assertEqual(len(touches.timeline(self.con, thread)), 1)

    def test_bulk_mail_is_never_filed_by_either_matcher(self):
        thread = self.tracked(address="news@weekly.example")
        opening = self.outbound(subject="Opening", key="<o@manitlab.invalid>",
                                days_ago=10)
        touches.from_message(self.con, thread, opening)
        self.inbound(frm="News <news@weekly.example>", subject="Weekly digest",
                     key="<n1@weekly.example>", days_ago=1,
                     headers=b"List-Id: <weekly.example>\n")
        attach.run(self.con)
        self.assertEqual(len(touches.timeline(self.con, thread)), 1)

    def test_a_bounce_IS_filed_although_it_declares_itself_automatic(self):
        """Matcher 3. A DSN has no References to the message that failed and
        every one of them is bulk by declaration, so it is invisible to the
        other two — and it is the message that EXPLAINS a silence."""
        thread = self.tracked(address="j.harrington@covalent.example")
        opening = self.outbound(subject="Opening", key="<o@manitlab.invalid>",
                                days_ago=10)
        touches.from_message(self.con, thread, opening)
        self.store(self.inbox,
                   frm="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                   to="manish@manitlab.invalid",
                   subject="Delivery Status Notification (Failure)",
                   key="<dsn1@mx.google.com>", days_ago=9,
                   headers=b'Auto-Submitted: auto-replied\n'
                           b'Content-Type: multipart/report; '
                           b'report-type=delivery-status; boundary="B"\n',
                   body=b"--B\nContent-Type: text/plain\n\nFailed.\n\n"
                        b"--B\nContent-Type: message/delivery-status\n\n"
                        b"Final-Recipient: rfc822; j.harrington@covalent.example\n"
                        b"Action: failed\nStatus: 5.1.1\n"
                        b"Diagnostic-Code: smtp; 550 no such user\n\n--B--\n")
        filed = attach.run(self.con)
        self.assertEqual(filed.bounces, 1)
        bounced = [t for t in touches.timeline(self.con, thread)
                   if t.status == touches.STATUS_BOUNCED]
        self.assertEqual(len(bounced), 1)

    def test_running_it_again_files_nothing_twice(self):
        # It runs after every sync and re-examines mail it has already seen.
        thread = self.tracked()
        opening = self.outbound(subject="Opening", key="<o@manitlab.invalid>",
                                days_ago=10)
        touches.from_message(self.con, thread, opening)
        self.inbound(subject="A reply", key="<r@covalent.example>", days_ago=2)
        self.assertEqual(attach.run(self.con).total, 1)
        self.assertEqual(attach.run(self.con).total, 0)
        self.assertEqual(len(touches.timeline(self.con, thread)), 2)

    def test_a_message_the_user_sent_is_filed_outbound_from_the_folder(self):
        """The direction comes from the FOLDER'S ROLE and not from the address.
        A message in Sent is outbound even when addressed to the user."""
        thread = self.tracked(address=False)
        note_to_self = self.store(self.sent, frm="manish@manitlab.invalid",
                                  to="manish@manitlab.invalid",
                                  subject="A note to myself",
                                  key="<self@manitlab.invalid>", days_ago=1)
        touches.from_message(self.con, thread, note_to_self)
        self.assertEqual(touches.timeline(self.con, thread)[0].direction, "out")

    def test_the_users_own_message_outside_sent_is_still_outbound(self):
        """A mailing list posts it back, and Gmail files that copy in All Mail.
        Counted inbound it would make a thread look owed by the person who
        wrote it."""
        thread = self.tracked(address=False)
        posted_back = self.store(self.archive, frm="manish@manitlab.invalid",
                                 to="list@discuss.example",
                                 subject="My own post, returned",
                                 key="<mine@manitlab.invalid>", days_ago=1)
        touches.from_message(self.con, thread, posted_back)
        self.assertEqual(touches.timeline(self.con, thread)[0].direction, "out")


class TestFilingByHand(Fixture):
    def test_filing_one_message_files_its_conversation(self):
        """A person filing a message means "this exchange belongs here". Filing
        only the message they had open leaves the other nine in the queue they
        were trying to empty."""
        thread = self.tracked(address=False)
        first = self.outbound(subject="DWCNT wavelength question",
                              key="<out1@manitlab.invalid>", days_ago=9)
        reply = self.inbound(subject="Re: DWCNT wavelength question",
                             key="<in1@covalent.example>", days_ago=4,
                             headers=b"References: <out1@manitlab.invalid>\n")
        attach.attach_message(self.con, thread, reply)
        self.assertEqual(len(touches.timeline(self.con, thread)), 2)
        self.assertIn(first, [t.message_id
                              for t in touches.timeline(self.con, thread)])

    def test_filing_by_hand_puts_the_correspondent_on_the_thread(self):
        """So that the NEXT four replies are filed by matcher 2 instead of
        landing in the queue one at a time."""
        thread = self.tracked(address=False)
        reply = self.inbound(subject="Out of the blue",
                             key="<in1@covalent.example>", days_ago=4)
        attach.attach_message(self.con, thread, reply)
        people = [c.address for c, _role in tracking.thread_contacts(self.con,
                                                                     thread)]
        self.assertEqual(people, ["lyle@covalent.example"])

    def test_it_never_puts_the_user_themselves_on_a_thread(self):
        thread = self.tracked(address=False)
        mine = self.outbound(subject="Opening", key="<o@manitlab.invalid>",
                             days_ago=1)
        attach.attach_message(self.con, thread, mine)
        people = [c.address for c, _role in tracking.thread_contacts(self.con,
                                                                     thread)]
        self.assertEqual(people, ["lyle@covalent.example"])

    def test_detaching_removes_the_message_and_leaves_the_calls(self):
        thread = self.tracked(address=False)
        message = self.inbound(subject="A reply", key="<r@covalent.example>",
                               days_ago=2)
        touches.from_message(self.con, thread, message)
        touches.log_call(self.con, thread, summary="Rang about it")
        self.assertEqual(attach.detach_message(self.con, thread, message), 1)
        left = touches.timeline(self.con, thread)
        self.assertEqual([t.channel for t in left], ["phone"])


class TestTheQueue(Fixture):
    def test_only_unfiled_inbound_mail_is_in_it(self):
        self.inbound(subject="A reply", key="<r@covalent.example>", days_ago=2)
        self.outbound(subject="Mine", key="<m@manitlab.invalid>", days_ago=2)
        attach.rebuild_wrote_to(self.con)
        self.assertEqual([i.key for i in triage.queue(self.con)],
                         ["<r@covalent.example>"])

    def test_filing_a_message_takes_it_out_of_the_queue(self):
        # Written to first, so the default scope can see the reply at all:
        # `known` is mail from somebody this user has written to.
        self.outbound(subject="Mine", key="<m@manitlab.invalid>", days_ago=9)
        message = self.inbound(subject="A reply", key="<r@covalent.example>",
                               days_ago=2)
        attach.rebuild_wrote_to(self.con)
        self.assertEqual(triage.count(self.con), 1)
        touches.from_message(self.con, self.tracked(), message)
        self.assertEqual(triage.count(self.con), 0)

    def test_the_horizon_keeps_history_out_of_the_to_do_list(self):
        """Older mail is fully present and searchable; it is simply not
        presented as outstanding. Without this the queue is every message ever
        received from anyone ever written to."""
        self.outbound(subject="Long ago", key="<o@manitlab.invalid>",
                      days_ago=400)
        self.inbound(subject="Also long ago", key="<old@covalent.example>",
                     days_ago=399)
        attach.rebuild_wrote_to(self.con)
        self.assertEqual(triage.count(self.con), 0)
        triage.set_horizon(self.con, "2000-01-01")
        self.assertEqual(triage.count(self.con), 1)

    def test_the_scopes_widen_and_their_counts_are_all_shown(self):
        """A narrow default must never HIDE work — it only defers it, and the
        wider numbers beside it are how a person knows that."""
        self.outbound(subject="Mine", key="<m@manitlab.invalid>", days_ago=9)
        self.inbound(subject="From someone I wrote to",
                     key="<known@covalent.example>", days_ago=2)
        self.inbound(frm="Stranger <who@nowhere.example>", subject="Hello",
                     key="<s@nowhere.example>", days_ago=2)
        self.inbound(frm="News <news@weekly.example>", subject="Digest",
                     key="<n@weekly.example>", days_ago=2,
                     headers=b"List-Id: <weekly.example>\n")
        attach.rebuild_wrote_to(self.con)
        counts = triage.counts(self.con)
        self.assertEqual((counts["known"], counts["human"], counts["all"]),
                         (1, 2, 3))

    def test_gmails_three_copies_of_one_message_count_once(self):
        """INBOX, All Mail and Important hold the same mail at once. A raw
        count trebles every conversation."""
        for folder in (self.inbox, self.archive):
            self.store(folder, frm="Lyle Gordon <lyle@covalent.example>",
                       to="manish@manitlab.invalid", subject="One message",
                       key="<one@covalent.example>", days_ago=2)
        attach.rebuild_wrote_to(self.con)
        self.assertEqual(triage.count(self.con, scope=triage.SCOPE_HUMAN), 1)
        self.assertEqual(len(triage.queue(self.con,
                                          scope=triage.SCOPE_HUMAN)), 1)

    def test_dismissing_one_copy_dismisses_all_of_them(self):
        ids = [self.store(folder, frm="Lyle Gordon <lyle@covalent.example>",
                          to="manish@manitlab.invalid", subject="One message",
                          key="<one@covalent.example>", days_ago=2)
               for folder in (self.inbox, self.archive)]
        triage.dismiss(self.con, ids[0], reason="no answer needed")
        self.assertEqual(triage.count(self.con, scope=triage.SCOPE_HUMAN), 0)

    def test_a_dismissal_survives_the_rows_being_thrown_away(self):
        """--resync discards every message row and fetches them again. A
        decision keyed on a row id would be undone by it, and the queue a
        person had worked down would refill."""
        message = self.inbound(subject="A reply", key="<r@covalent.example>",
                               days_ago=2)
        triage.dismiss(self.con, message)
        folders.discard_contents(self.con, self.inbox)
        again = self.inbound(subject="A reply", key="<r@covalent.example>",
                             days_ago=2, uid=999)
        self.assertTrue(again)
        self.assertEqual(triage.count(self.con, scope=triage.SCOPE_HUMAN), 0)

    def test_a_dismissal_can_be_taken_back(self):
        message = self.inbound(subject="A reply", key="<r@covalent.example>",
                               days_ago=2)
        key = triage.dismiss(self.con, message)
        self.assertTrue(triage.restore(self.con, key))
        self.assertEqual(triage.count(self.con, scope=triage.SCOPE_HUMAN), 1)

    def test_junk_and_trash_are_never_in_the_queue(self):
        for role in ("junk", "trash"):
            folder = folders.ensure_folder(self.con, self.account, role.title(),
                                           display_name=role.title(), role=role)
            self.store(folder, frm="Lyle Gordon <lyle@covalent.example>",
                       to="manish@manitlab.invalid", subject=f"In {role}",
                       key=f"<{role}@covalent.example>", days_ago=1)
        self.assertEqual(triage.count(self.con, scope=triage.SCOPE_ALL), 0)

    def test_a_bounce_is_in_the_queue_although_it_is_automatic(self):
        # The narrow scopes exclude bulk, and a bounce is never marked bulk.
        self.store(self.inbox,
                   frm="Mail Delivery System <MAILER-DAEMON@mx.example.org>",
                   to="manish@manitlab.invalid",
                   subject="Undelivered Mail Returned to Sender",
                   key="<b@mx.example.org>", days_ago=1)
        found = triage.queue(self.con, scope=triage.SCOPE_HUMAN)
        self.assertEqual([i.is_bounce for i in found], [True])


class TestWroteTo(Fixture):
    def test_it_is_built_from_the_sent_folders(self):
        self.outbound(to="Lyle <lyle@covalent.example>, cc@covalent.example",
                      subject="Mine", key="<m@manitlab.invalid>", days_ago=2)
        self.inbound(subject="Theirs", key="<t@covalent.example>", days_ago=1)
        self.assertEqual(attach.rebuild_wrote_to(self.con), 2)
        rows = {r[0] for r in self.con.execute(
            "SELECT address FROM wrote_to").fetchall()}
        self.assertEqual(rows, {"lyle@covalent.example", "cc@covalent.example"})

    def test_rebuilding_replaces_rather_than_accumulates(self):
        self.outbound(subject="Mine", key="<m@manitlab.invalid>", days_ago=2)
        attach.rebuild_wrote_to(self.con)
        self.assertEqual(attach.rebuild_wrote_to(self.con), 1)


class TestOwedIsNowTheRealQuestion(Fixture):
    """The note at the top of `store/messages.py` predicted this: the simple
    version is a fact about the MAILBOX, and what a person means by owed is a
    fact about the CORRESPONDENCE."""

    def owed_count(self):
        return messages_repo.count(
            self.con, views.Scope(kind="unified", role=views.ROLE_OWED))

    def test_an_unanswered_inbound_message_is_owed(self):
        self.inbound(subject="A question", key="<q@covalent.example>",
                     days_ago=2)
        self.assertEqual(self.owed_count(), 1)

    def test_a_message_on_no_thread_is_still_owed(self):
        # The clause subtracts, and has nothing to subtract until somebody is
        # tracking something. Owed must be useful before any thread exists.
        self.inbound(subject="A question", key="<q@covalent.example>",
                     days_ago=2)
        self.tracked()
        self.assertEqual(self.owed_count(), 1)

    def test_a_logged_call_answers_it(self):
        """A matter settled on the telephone is settled, however the \\Answered
        flag reads. This is the whole upgrade."""
        message = self.inbound(subject="A question", key="<q@covalent.example>",
                               days_ago=2)
        thread = self.tracked()
        touches.from_message(self.con, thread, message)
        self.assertEqual(self.owed_count(), 1)
        touches.log_call(self.con, thread, summary="Rang and settled it")
        self.assertEqual(self.owed_count(), 0)

    def test_a_note_to_yourself_does_not_answer_it(self):
        message = self.inbound(subject="A question", key="<q@covalent.example>",
                               days_ago=2)
        thread = self.tracked()
        touches.from_message(self.con, thread, message)
        touches.add_note(self.con, thread, "Must reply to this")
        self.assertEqual(self.owed_count(), 1)

    def test_a_call_made_BEFORE_the_message_does_not_answer_it(self):
        # Only something later discharges it, or every thread with any outbound
        # history would read as answered for ever.
        thread = self.tracked()
        touches.log_call(self.con, thread, summary="Rang first",
                         occurred_at=times.to_utc_text(NOW - dt.timedelta(days=5)))
        message = self.inbound(subject="A question", key="<q@covalent.example>",
                               days_ago=2)
        touches.from_message(self.con, thread, message)
        self.assertEqual(self.owed_count(), 1)


if __name__ == "__main__":
    unittest.main()


class TestTheQueueIsAnswerableReadOnly(Fixture):
    """`--check` reports the counts and opens the store READ-ONLY.

    An earlier `horizon` persisted its default on first read, so asking a fresh
    store for a count raised `attempt to write a readonly database` — from a
    function nobody would think to look at for a write. The class of the bug is
    a READING path that writes, so the test asks every reading question over a
    connection that cannot.
    """

    def read_only(self):
        from cormani.store import database
        con = database.connect(support.store_path(self.con), read_only=True)
        self.addCleanup(con.close)
        return con

    def test_every_reading_question_works_over_a_read_only_connection(self):
        self.inbound(subject="A reply", key="<r@covalent.example>", days_ago=2)
        self.con.commit()
        con = self.read_only()
        self.assertTrue(triage.horizon(con))
        self.assertIsInstance(triage.counts(con), dict)
        self.assertIsInstance(triage.count(con), int)
        self.assertIsInstance(triage.queue(con), list)

    def test_the_window_rolls_until_somebody_pins_it(self):
        # Which is the honest reading of "the last sixty days", and is what
        # makes the read path pure.
        first = triage.horizon(self.con)
        triage.set_horizon(self.con, "2020-01-01")
        self.assertEqual(triage.horizon(self.con), "2020-01-01")
        self.assertNotEqual(first, "2020-01-01")
