# SPDX-License-Identifier: GPL-3.0-or-later
#
# The demo's address book.
#
# BESIDE `tests/test_fixtures.py` AND NOT INSIDE IT, and the reason is the one
# `tests/test_calcli.py` established: the split falls where the MODULE split
# does, which is the answer whenever it is available. That file was at 590
# lines when this was written, so the 600-line rule was about to give the same
# answer for a worse reason.
#
# WHAT IS WORTH ASSERTING ABOUT A FIXTURE is not that it wrote rows. It is that
# the demo shows the STATES the interface has to draw — and for the address
# book there are four that a mailbox cannot produce by itself, so if this file
# stops writing them nothing else will notice:
#
#   a handle that is not an email address, which is the whole of PLAN.txt §2's
#   "every handle a person has: addresses, numbers, profiles";
#   a card with no email address at all;
#   two cards for one person;
#   and a handle that has bounced, which `store/fixtures.py` has carried since
#   stage 1 with a comment saying the contact card must render it.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import addressbook as book_repo
from cormani.store import contacts as contacts_repo
from cormani.store import fixtures


class DemoBook(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.report = fixtures.install(self.con)
        self.people = {c.label: c
                       for c in contacts_repo.list_contacts(self.con)}


class TestWhatItInstalled(DemoBook):
    def test_the_report_is_what_the_store_actually_holds(self):
        """COUNTED FROM THE TABLE AND NOT ADDED UP — `viewfixtures.py`'s rule
        taken one step further. A total assembled from `len(_HANDLES) + 2` is
        a claim about what the fixture MEANT to write, and the two diverge the
        moment a row names an address `fixtures._CONTACTS` no longer has, which
        is the case `contactfixtures.install` passes over in silence."""
        held = contacts_repo.counts(self.con)
        self.assertEqual(self.report["contacts"], held["contacts"])
        self.assertEqual(self.report["contact_handles"], held["handles"])

    def test_every_card_named_in_the_detail_table_was_actually_found(self):
        """The silent half of the loop. `contactfixtures` skips a row whose
        address is not in the book, which is right — but a table where every
        row was skipped would install nothing and report zero, and nothing
        else in the suite looks at that number."""
        from cormani.store import contactfixtures

        self.assertEqual(self.report["contacts_filled"],
                         len(contactfixtures._DETAIL))

    def test_two_installs_agree(self):
        """`tests/test_fixtures.py` asserts this of the demo as a whole. Said
        again here because `contactfixtures` reads the store to decide what to
        write, which is the shape that can differ between runs."""
        second = support.temp_store(self)
        again = fixtures.install(second)
        for key in ("contacts", "contact_handles", "contacts_filled",
                    "contacts_added"):
            self.assertEqual(self.report[key], again[key], key)


class TestTheStatesItExistsFor(DemoBook):
    def test_somebody_has_a_handle_that_is_not_an_email_address(self):
        """PLAN.txt §2 by name. Without this the address book demonstrates a
        list of names beside a panel with one line in it, which is the
        interface in the state it would be in if the card were not built."""
        kinds = book_repo.summary(self.con)["kinds"]
        self.assertIn("email", kinds)
        beyond = set(kinds) - {"email"}
        self.assertGreaterEqual(len(beyond), 4,
                                f"only {sorted(beyond)} beyond email")

    def test_one_person_carries_several_channels_at_once(self):
        """A card is the only surface in corMani where a telephone number and
        a LinkedIn profile are visible together, so at least one demo card has
        to have both — one handle each on four people would satisfy the test
        above and show nothing."""
        best = max(self.people.values(),
                   key=lambda c: len({h.kind for h in c.handles}))
        self.assertGreaterEqual(len({h.kind for h in best.handles}), 3,
                                f"{best.label} has {best.handles}")

    def test_somebody_has_no_email_address_at_all(self):
        """A card the composer can never offer, and `--check` counts it.
        Nothing in a mailbox can produce one: a contact made from a message
        always has the address it was made from."""
        self.assertGreaterEqual(book_repo.summary(self.con)["no_email"], 1)
        unreachable = [c for c in self.people.values() if not c.address]
        self.assertTrue(unreachable)
        for contact in unreachable:
            # And it is not merely a blank row — it has some other way of being
            # reached, or it is a card with nothing on it rather than a person
            # who does not use email.
            self.assertTrue(contact.handles, contact.label)

    def test_two_cards_are_one_person_and_the_pair_is_offered_correctly(self):
        """What Merge is for. The ORDER within the pair is the part worth
        asserting: `merge_contacts` fills the kept card's empty fields from the
        other, so the fuller card must be the one offered as `keep_id`."""
        pairs = book_repo.duplicates(self.con)
        self.assertEqual(len(pairs), 1, pairs)
        keep = contacts_repo.get_contact(self.con, pairs[0].keep_id)
        drop = contacts_repo.get_contact(self.con, pairs[0].drop_id)
        self.assertEqual(keep.name, drop.name)
        self.assertGreater(len(keep.handles) + len(keep.org) + len(keep.role),
                           len(drop.handles) + len(drop.org) + len(drop.role))

    def test_a_handle_has_bounced_and_keeps_the_server_words(self):
        """`store/fixtures.py` carried this since stage 1 for a card that did
        not exist yet. The NOTE is the half the card draws — "mailbox full" and
        "no such user" call for opposite decisions."""
        bounced = [h for c in self.people.values() for h in c.handles
                   if h.is_bounced]
        self.assertEqual(len(bounced), 1)
        self.assertTrue(bounced[0].note.strip())
        self.assertGreaterEqual(bounced[0].bounce_count, 1)

    def test_the_demo_offers_people_to_add_from_its_own_mail(self):
        """The Add from mail button has to demonstrate something. It would be
        empty if every sender in the demo were already a contact."""
        offered = book_repo.suggest(self.con)
        self.assertGreaterEqual(len(offered), 3)
        known = {h.value.lower() for c in self.people.values()
                 for h in c.handles}
        for stranger in offered:
            self.assertNotIn(stranger.address, known)

    def test_a_card_shows_mail_in_both_directions(self):
        """A demo whose every card said "no mail either way" would show the
        correspondence line in the one state that proves nothing."""
        both = [c for c in self.people.values()
                if book_repo.correspondence(self.con, c).sent
                and book_repo.correspondence(self.con, c).received]
        self.assertTrue(both, "no demo contact has mail in both directions")


class TestItStaysOffTheThreads(DemoBook):
    def test_no_filler_sender_was_put_on_a_tracked_thread(self):
        """`store/trackfixtures.py` carries the warning: Frances Baker and
        Meera Iyer are also filler senders, so putting either on a thread files
        a hundred and sixty generated messages onto it. `contactfixtures`
        writes only to `contact` and `handle`, and this is what says so if that
        ever stops being true."""
        from cormani.store import tracking

        for name in ("Frances Baker", "Meera Iyer"):
            contact = self.people.get(name)
            self.assertIsNotNone(contact, name)
            self.assertEqual(tracking.threads_for_contact(self.con, contact.id),
                             [], name)

    def test_the_two_people_it_adds_are_on_no_thread_either(self):
        from cormani.store import tracking

        for name in ("Ravi at the unit", "Tom Whitfield"):
            for contact in contacts_repo.list_contacts(self.con, query=name):
                if contact.name != name:
                    continue
                if contact.org:            # the real Tom, made by fixtures.py
                    continue
                self.assertEqual(
                    tracking.threads_for_contact(self.con, contact.id), [],
                    contact.label)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
