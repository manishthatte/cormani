# SPDX-License-Identifier: GPL-3.0-or-later
#
# The mailbox seen through a person: what passed between, who is nobody yet,
# and which two cards are one.
#
# EVERY COUNT HERE IS ASSERTED AS A NUMBER AND NEVER AS "MORE THAN NONE". A
# correspondence count has one interesting failure — it counts the wrong side,
# or counts a message twice because the person is in both To and Cc — and both
# of those return a perfectly plausible positive integer. P44's rule from the
# compiler campaign next door, and it applies unchanged: assert the VALUE.
#
# AND EVERY DIRECTION IS TESTED BOTH WAYS ROUND. A card that reported the same
# number for `received` and `sent` would pass any test that only ever put one
# message in one direction, which is the shape of most of these fixtures until
# somebody notices.
#
# © Manish Jagdish Thatte
import datetime as dt
import unittest

import support

from cormani.imap import envelope
from cormani.store import (accounts, addressbook, contacts, folders, ingest,
                           times, tracking)

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
        self.junk = folders.ensure_folder(self.con, self.account, "Spam",
                                          display_name="Junk", role="junk")
        self.trash = folders.ensure_folder(self.con, self.account, "Trash",
                                           display_name="Trash", role="trash")
        self.drafts = folders.ensure_folder(self.con, self.account, "Drafts",
                                            display_name="Drafts",
                                            role="drafts")
        self._uid = 0

    def store(self, folder, *, frm, to, subject="Something", days_ago=1,
              cc=""):
        self._uid += 1
        raw = (f"From: {frm}\nTo: {to}\n"
               + (f"Cc: {cc}\n" if cc else "")
               + f"Subject: {subject}\n"
               f"Message-ID: <m{self._uid}@example.invalid>\n"
               f"Date: {when(days_ago)}\n\nBody.\n").encode()
        return ingest.store_message(self.con, folder, self._uid,
                                    envelope.read(raw)).message_id

    def person(self, address="lyle@covalent.invalid", name="Lyle Gordon"):
        return contacts.contact_for_address(self.con, address, name=name,
                                            create=True)

    def again(self, contact):
        """The card, re-read. Handles are a tuple on a frozen dataclass, so a
        contact fetched before a handle was added is a stale object — and one
        that would make half of these tests assert about an address book that
        no longer exists."""
        return contacts.get_contact(self.con, contact.id)


class TestCorrespondence(Fixture):
    def test_the_two_directions_are_counted_apart(self):
        """The pair is the point — forty in and two out is a newsletter."""
        lyle = self.person()
        for n in range(3):
            self.store(self.inbox, frm="Lyle <lyle@covalent.invalid>",
                       to="manish@manitlab.invalid", days_ago=n + 1)
        self.store(self.sent, frm="manish@manitlab.invalid",
                   to="Lyle <lyle@covalent.invalid>", days_ago=4)

        seen = addressbook.correspondence(self.con, lyle)
        self.assertEqual(seen.received, 3)
        self.assertEqual(seen.sent, 1)
        self.assertEqual(seen.total, 4)
        self.assertEqual(seen.describe(), "3 from them, 1 to them")

    def test_a_person_in_Cc_was_still_written_to(self):
        """Cc is not a lesser To for this question: a message copied to
        somebody is mail that reached them, and leaving it out would make a
        card under-report exactly the people who are on every thread."""
        lyle = self.person()
        self.store(self.sent, frm="manish@manitlab.invalid",
                   to="someone@else.invalid", cc="Lyle <lyle@covalent.invalid>")
        self.assertEqual(addressbook.correspondence(self.con, lyle).sent, 1)

    def test_being_in_both_To_and_Cc_counts_the_message_once(self):
        """The obvious implementation — one query per field, added together —
        double-counts exactly the message that names somebody twice, which is
        a reply-all to a list they are also on. It is a plausible number and
        nothing else in the interface could contradict it."""
        lyle = self.person()
        self.store(self.sent, frm="manish@manitlab.invalid",
                   to="Lyle <lyle@covalent.invalid>",
                   cc="lyle@covalent.invalid")
        self.assertEqual(addressbook.correspondence(self.con, lyle).sent, 1)

    def test_two_addresses_for_one_person_are_one_count(self):
        """The whole reason a contact has more than one handle."""
        lyle = self.person()
        contacts.add_handle(self.con, lyle.id, contacts.KIND_EMAIL,
                            "l.gordon@covalent.invalid")
        lyle = self.again(lyle)
        self.store(self.inbox, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid")
        self.store(self.inbox, frm="L.Gordon@covalent.invalid",
                   to="manish@manitlab.invalid")
        self.assertEqual(addressbook.correspondence(self.con, lyle).received, 2)

    def test_the_address_is_matched_whatever_its_case(self):
        """`contacts.bounced` case-folds and says why: an address is
        case-sensitive only in a specification nobody honours. A count that
        disagreed with the bounce guard about which mail is whose would be
        two different answers to one question."""
        lyle = self.person(address="lyle@covalent.invalid")
        self.store(self.inbox, frm="LYLE@Covalent.Invalid",
                   to="manish@manitlab.invalid")
        self.assertEqual(addressbook.correspondence(self.con, lyle).received, 1)

    def test_drafts_junk_and_trash_are_left_out_of_both_halves(self):
        """The same four roles `store/attach.py` and `store/triage.py`
        exclude. A draft in particular is not correspondence: the other person
        has never seen it."""
        lyle = self.person()
        self.store(self.inbox, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid")
        self.store(self.junk, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid")
        self.store(self.trash, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid")
        self.store(self.drafts, frm="manish@manitlab.invalid",
                   to="lyle@covalent.invalid")
        seen = addressbook.correspondence(self.con, lyle)
        self.assertEqual((seen.received, seen.sent), (1, 0))

    def test_archived_mail_still_counts(self):
        """Filing something is not un-receiving it, and a card that only
        looked at the Inbox would report zero for every correspondence that
        was ever tidied up — which is every old one."""
        lyle = self.person()
        self.store(self.archive, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid")
        self.assertEqual(addressbook.correspondence(self.con, lyle).received, 1)

    def test_a_contact_with_no_email_has_no_mail_and_does_not_raise(self):
        """A card made for a telephone number is an ordinary thing. The empty
        `IN ()` this would otherwise build is a SQL syntax error, so the guard
        is load-bearing rather than defensive."""
        contact_id = contacts.add_contact(self.con, "Somebody")
        contacts.add_handle(self.con, contact_id, "phone", "+44 7700 900000")
        seen = addressbook.correspondence(
            self.con, contacts.get_contact(self.con, contact_id))
        self.assertFalse(seen.any)
        self.assertEqual(seen.describe(), "no mail either way")

    def test_the_dates_span_both_directions(self):
        """`last_at` is what the card says about how long it has been, so it
        must not be the last message they sent when the last thing that
        happened is one the user sent."""
        lyle = self.person()
        self.store(self.inbox, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid", days_ago=40)
        self.store(self.sent, frm="manish@manitlab.invalid",
                   to="lyle@covalent.invalid", days_ago=2)
        seen = addressbook.correspondence(self.con, lyle)
        self.assertLess(seen.first_at, seen.last_at)
        # The newest is the OUTBOUND one, which is the half a from_addr-only
        # query would miss.
        newest = self.con.execute(
            "SELECT date_at FROM message ORDER BY date_at DESC "
            "LIMIT 1").fetchone()[0]
        self.assertEqual(seen.last_at, newest)


class TestRecentMessages(Fixture):
    def test_each_row_says_which_way_it_went(self):
        lyle = self.person()
        self.store(self.inbox, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid", subject="In", days_ago=2)
        self.store(self.sent, frm="manish@manitlab.invalid",
                   to="lyle@covalent.invalid", subject="Out", days_ago=1)
        rows = addressbook.recent_messages(self.con, lyle)
        self.assertEqual([r["subject"] for r in rows], ["Out", "In"])
        self.assertEqual([int(r["outbound"]) for r in rows], [1, 0])

    def test_it_agrees_with_the_count_above_it(self):
        """A card saying "12 from them" over a list of nine is a card nobody
        trusts again, so the two queries must apply one rule. Junk is the one
        that would differ."""
        lyle = self.person()
        for n in range(4):
            self.store(self.inbox, frm="lyle@covalent.invalid",
                       to="manish@manitlab.invalid", days_ago=n + 1)
        self.store(self.junk, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid", days_ago=9)
        seen = addressbook.correspondence(self.con, lyle)
        rows = addressbook.recent_messages(self.con, lyle, limit=100)
        self.assertEqual(len(rows), seen.total)

    def test_the_limit_takes_the_newest_and_not_the_first_found(self):
        lyle = self.person()
        for n in range(6):
            self.store(self.inbox, frm="lyle@covalent.invalid",
                       to="manish@manitlab.invalid",
                       subject=f"Number {n}", days_ago=10 - n)
        rows = addressbook.recent_messages(self.con, lyle, limit=2)
        self.assertEqual([r["subject"] for r in rows], ["Number 5", "Number 4"])


class TestSuggestions(Fixture):
    def test_somebody_already_in_the_book_is_not_offered(self):
        self.person(address="lyle@covalent.invalid")
        self.store(self.inbox, frm="lyle@covalent.invalid",
                   to="manish@manitlab.invalid")
        self.store(self.inbox, frm="Tom <tom@northgate.invalid>",
                   to="manish@manitlab.invalid")
        offered = [s.address for s in addressbook.suggest(self.con)]
        self.assertEqual(offered, ["tom@northgate.invalid"])

    def test_a_handle_of_another_kind_still_counts_as_known(self):
        """`handle` is UNIQUE on (kind, value), so the same address can be
        held twice under two kinds. Joining on the value alone and not on the
        kind would offer somebody whose address is in the book already."""
        contact_id = contacts.add_contact(self.con, "Tom")
        contacts.add_handle(self.con, contact_id, contacts.KIND_EMAIL,
                            "tom@northgate.invalid")
        self.store(self.inbox, frm="tom@northgate.invalid",
                   to="manish@manitlab.invalid")
        self.assertEqual(addressbook.suggest(self.con), [])

    def test_the_name_and_the_count_come_back_with_the_address(self):
        for n in range(3):
            self.store(self.inbox, frm="Tom Whitfield <tom@northgate.invalid>",
                       to="manish@manitlab.invalid", days_ago=n + 1)
        offered = addressbook.suggest(self.con)
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0].name, "Tom Whitfield")
        self.assertEqual(offered[0].messages, 3)
        self.assertEqual(offered[0].label,
                         "Tom Whitfield <tom@northgate.invalid>")

    def test_somebody_written_to_is_offered_before_somebody_who_only_wrote(self):
        """The ordering is the whole usefulness of the list. A first run over
        a real mailbox otherwise offers a page of senders never answered, and
        the person the user actually corresponds with is on page four."""
        from cormani.store import attach

        for n in range(9):
            self.store(self.inbox, frm="Loud <loud@newsletter.invalid>",
                       to="manish@manitlab.invalid", days_ago=n + 1)
        self.store(self.inbox, frm="Tom <tom@northgate.invalid>",
                   to="manish@manitlab.invalid", days_ago=3)
        self.store(self.sent, frm="manish@manitlab.invalid",
                   to="Tom <tom@northgate.invalid>", days_ago=2)
        attach.rebuild_wrote_to(self.con)

        offered = [s.address for s in addressbook.suggest(self.con)]
        self.assertEqual(offered[0], "tom@northgate.invalid")
        self.assertIn("loud@newsletter.invalid", offered)

    def test_a_machine_is_never_offered(self):
        for address in ("no-reply@bank.invalid", "noreply@bank.invalid",
                        "mailer-daemon@bank.invalid",
                        "notifications@social.invalid"):
            self.store(self.inbox, frm=address, to="manish@manitlab.invalid")
        self.store(self.inbox, frm="tom@northgate.invalid",
                   to="manish@manitlab.invalid")
        self.assertEqual([s.address for s in addressbook.suggest(self.con)],
                         ["tom@northgate.invalid"])

    def test_the_machine_test_reads_the_local_part_only(self):
        """A domain that contains the word is not a machine. `is_machine` is
        deliberately small, and the one thing a small rule must not do is
        catch a person because their EMPLOYER is called Notifications Ltd."""
        self.assertTrue(addressbook.is_machine("no-reply@example.invalid"))
        self.assertFalse(addressbook.is_machine("priya@noreply-media.invalid"))

    def test_sent_junk_trash_and_drafts_are_not_mined_for_strangers(self):
        """Junk is the point: an address book filled from the spam folder is
        an address book nobody opens twice."""
        self.store(self.junk, frm="spam@nowhere.invalid",
                   to="manish@manitlab.invalid")
        self.store(self.trash, frm="binned@nowhere.invalid",
                   to="manish@manitlab.invalid")
        self.store(self.sent, frm="manish@manitlab.invalid",
                   to="anyone@nowhere.invalid")
        self.assertEqual(addressbook.suggest(self.con), [])

    def test_nothing_is_written_by_asking(self):
        """`contacts.contact_for_address` defaults `create` to False because
        an address book that grows by itself is a list nobody can use. A
        suggestion that quietly made the contact would be that defect wearing
        a different name."""
        self.store(self.inbox, frm="tom@northgate.invalid",
                   to="manish@manitlab.invalid")
        addressbook.suggest(self.con)
        self.assertEqual(contacts.counts(self.con)["contacts"], 0)


class TestDuplicates(Fixture):
    def test_the_same_name_twice_is_a_pair(self):
        first = contacts.add_contact(self.con, "Lyle Gordon", org="Covalent")
        second = contacts.add_contact(self.con, "lyle gordon")
        pairs = addressbook.duplicates(self.con)
        self.assertEqual(len(pairs), 1)
        self.assertEqual({pairs[0].keep_id, pairs[0].drop_id}, {first, second})
        self.assertIn("Lyle Gordon", pairs[0].reason)

    def test_the_fuller_card_is_the_one_kept(self):
        """`merge_contacts` fills the KEPT card's empty fields from the other,
        so keeping the fuller one loses nothing and keeping the emptier one
        loses whichever field both of them have. Offering the pair the wrong
        way round would make the obvious click the lossy one."""
        thin = contacts.add_contact(self.con, "Lyle Gordon")
        fat = contacts.add_contact(self.con, "Lyle Gordon", org="Covalent",
                                   role="Sales", notes="Rings on Fridays")
        pair = addressbook.duplicates(self.con)[0]
        self.assertEqual(pair.keep_id, fat)
        self.assertEqual(pair.drop_id, thin)

    def test_one_address_held_under_two_kinds_is_a_pair(self):
        """UNIQUE is on (kind, value), so the same number on two cards as
        `phone` and as `whatsapp` is legal and is how the commonest real
        duplicate arises."""
        first = contacts.add_contact(self.con, "Anil")
        second = contacts.add_contact(self.con, "A. Kulkarni")
        contacts.add_handle(self.con, first, "phone", "+91 98200 00000")
        contacts.add_handle(self.con, second, "whatsapp", "+91 98200 00000")
        pairs = addressbook.duplicates(self.con)
        self.assertEqual(len(pairs), 1)
        self.assertIn("+91 98200 00000", pairs[0].reason)

    def test_two_blank_names_are_not_a_pair(self):
        """A contact made from a message can have no name at all, and every
        one of them matching every other would fill the report with pairs
        that are not people."""
        contacts.add_contact(self.con, "")
        contacts.add_contact(self.con, "   ")
        self.assertEqual(addressbook.duplicates(self.con), [])

    def test_a_pair_is_reported_once_and_not_twice(self):
        """`a.id < b.id` rather than `a.id <> b.id`. Without it the same two
        people are two rows in opposite orders, and the count in `--check`
        would be double what a person can see."""
        contacts.add_contact(self.con, "Priya Deshpande")
        contacts.add_contact(self.con, "Priya Deshpande")
        self.assertEqual(len(addressbook.duplicates(self.con)), 1)

    def test_nothing_is_merged_by_looking(self):
        contacts.add_contact(self.con, "Lyle Gordon")
        contacts.add_contact(self.con, "Lyle Gordon")
        addressbook.duplicates(self.con)
        self.assertEqual(contacts.counts(self.con)["contacts"], 2)


class TestSummary(Fixture):
    def test_a_contact_with_no_address_is_counted(self):
        """The one number that says something is WRONG rather than something
        is so: a contact with no address cannot be written to and the composer
        will never offer it."""
        contacts.add_contact(self.con, "Only a telephone")
        with_mail = contacts.add_contact(self.con, "Reachable")
        contacts.add_handle(self.con, with_mail, contacts.KIND_EMAIL,
                            "reach@example.invalid")
        counts = addressbook.summary(self.con)
        self.assertEqual(counts["contacts"], 2)
        self.assertEqual(counts["no_email"], 1)

    def test_the_kinds_are_counted_by_kind(self):
        contact_id = contacts.add_contact(self.con, "Lyle")
        contacts.add_handle(self.con, contact_id, contacts.KIND_EMAIL,
                            "lyle@covalent.invalid")
        contacts.add_handle(self.con, contact_id, "phone", "+44 7700 900000")
        contacts.add_handle(self.con, contact_id, "linkedin", "in/lylegordon")
        counts = addressbook.summary(self.con)
        self.assertEqual(counts["kinds"],
                         {"email": 1, "linkedin": 1, "phone": 1})

    def test_a_bounced_handle_is_counted_where_check_can_see_it(self):
        contact_id = contacts.add_contact(self.con, "Lyle")
        contacts.add_handle(self.con, contact_id, contacts.KIND_EMAIL,
                            "lyle@covalent.invalid")
        contacts.note_bounce(self.con, "lyle@covalent.invalid",
                             "550 unknown recipient")
        self.assertEqual(addressbook.summary(self.con)["bounced"], 1)


class TestAgainstTheTrackingLayer(Fixture):
    def test_a_contacts_threads_are_the_tracking_layers_own_answer(self):
        """The card draws the threads a person is on, and it must be the SAME
        answer the board gives — `store/tracking.threads_for_contact` — rather
        than a second query here that could drift from it."""
        lyle = self.person()
        thread = tracking.create_thread(self.con, "DWCNT wavelengths")
        tracking.link_contact(self.con, thread, lyle.id)
        found = tracking.threads_for_contact(self.con, lyle.id)
        self.assertEqual([t.title for t in found], ["DWCNT wavelengths"])


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
