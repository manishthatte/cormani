# SPDX-License-Identifier: GPL-3.0-or-later
#
# The address-book half of the command line.
#
# THE FIXTURE IS test_cli's, IMPORTED, for the reason `test_rulecli.py`,
# `test_calcli.py` and `test_viewcli.py` give: it redirects all four XDG
# variables and the keyring into a temporary directory.
#
# THAT `--contacts` WRITES NOTHING IS ASSERTED BY THE MECHANISM. It opens the
# store READ-ONLY, so a reading path that wrote would raise `attempt to write a
# readonly database` and every test below would fail at once. `store/triage.py`
# had exactly that defect and `--check` is what found it.
#
# WHAT IS ASSERTED IS THE WORDS, and the three that matter are the three states
# the address book gets into WITHOUT ANYBODY OPENING IT: a handle marked
# bounced by a delivery report during a sync, a card created by the tracking
# layer when somebody was put on a thread, and two cards for one person. Those
# are `cormani/contactcli.py`'s reason for existing, so each has a test that
# would go red if the sentence were dropped.
#
# © Manish Jagdish Thatte
import unittest

from test_cli import Fixture

from cormani.store import contacts as contacts_repo


class BookFixture(Fixture):
    def account(self, address="work@manitlab.invalid", provider="google"):
        from cormani.store.accounts import add_account

        con = self.store()
        return con, add_account(con, address, provider, display_name="Work")

    def people_last(self, con) -> int:
        """The id of the card most recently written. For a test that needs to
        hang a second handle on one it just made."""
        return int(con.execute(
            "SELECT id FROM contact ORDER BY id DESC LIMIT 1").fetchone()[0])

    def person(self, con, name="Lyle Gordon", address="lyle@covalent.invalid",
               **fields):
        contact_id = contacts_repo.add_contact(con, name, **fields)
        if address:
            contacts_repo.add_handle(con, contact_id,
                                     contacts_repo.KIND_EMAIL, address)
        return contact_id


class TestContactsCommand(BookFixture):
    def test_with_no_store_it_says_so_and_fails(self):
        code, text = self.run_cli("--contacts")
        self.assertEqual(code, 1)
        self.assertIn("no store yet", text)

    def test_nobody_yet_is_not_a_failure_and_says_where_they_come_from(self):
        """"None" has to answer the question the person had, not merely be
        true — `test_viewcli.py`'s rule."""
        self.store()
        code, text = self.run_cli("--contacts")
        self.assertEqual(code, 0)
        self.assertIn("no contacts yet", text)
        self.assertIn("Add from mail", text)

    def test_a_bare_switch_means_everybody(self):
        """`nargs="?"` with `const=""`, and the dispatch tests `is not None`.
        Testing it for truth would make the bare form fall through to opening
        a window, which is the failure `cormani/__main__.py`'s header is
        about."""
        con, _ = self.account()
        self.person(con, name="Lyle Gordon")
        self.person(con, name="Priya Deshpande",
                    address="priya@idlidu.invalid")
        code, text = self.run_cli("--contacts")
        self.assertEqual(code, 0)
        self.assertIn("Lyle Gordon", text)
        self.assertIn("Priya Deshpande", text)

    def test_a_query_narrows_it(self):
        con, _ = self.account()
        self.person(con, name="Lyle Gordon")
        self.person(con, name="Priya Deshpande",
                    address="priya@idlidu.invalid")
        code, text = self.run_cli("--contacts", "priya")
        self.assertEqual(code, 0)
        self.assertIn("Priya Deshpande", text)
        self.assertNotIn("Lyle Gordon", text)

    def test_a_query_matching_nobody_says_so_rather_than_printing_nothing(self):
        con, _ = self.account()
        self.person(con)
        code, text = self.run_cli("--contacts", "nobody")
        self.assertEqual(code, 0)
        self.assertIn("nobody matches", text)

    def test_every_handle_is_printed_with_its_kind(self):
        con, _ = self.account()
        contact_id = self.person(con)
        contacts_repo.add_handle(con, contact_id, "phone", "+44 7700 900123")
        contacts_repo.add_handle(con, contact_id, "linkedin", "in/lyle")
        code, text = self.run_cli("--contacts")
        self.assertIn("lyle@covalent.invalid", text)
        self.assertIn("+44 7700 900123", text)
        self.assertIn("in/lyle", text)
        self.assertIn("linkedin", text)

    def test_a_bounce_quotes_the_server_and_not_a_status_word(self):
        """"mailbox full" and "no such user" call for opposite decisions, so
        the note is quoted rather than summarised — `contacts.describe_bounces`
        argues it for the composer and it is the same argument here."""
        con, _ = self.account()
        self.person(con)
        contacts_repo.note_bounce(con, "lyle@covalent.invalid",
                                  "550 5.1.1 no such user")
        code, text = self.run_cli("--contacts")
        self.assertIn("BOUNCED once", text)
        self.assertIn("550 5.1.1 no such user", text)

    def test_a_card_with_no_handle_at_all_says_so(self):
        """An empty gap under a name reads as a report that stopped early."""
        con, _ = self.account()
        self.person(con, name="Nobody's contact", address="")
        code, text = self.run_cli("--contacts")
        self.assertIn("no way of reaching them", text)

    def test_a_narrowed_list_carries_the_mail_counts(self):
        con, account = self.account()
        from cormani.store.folders import ensure_folder

        inbox = ensure_folder(con, account, "INBOX", role="inbox")
        con.execute(
            "INSERT INTO message (folder_id, uid, message_id, subject, "
            "subject_base, from_addr, from_name, to_addrs, body_text, "
            "date_at, received_at, size_bytes) VALUES "
            "(?, 1, '<a@x>', 'Hello', 'Hello', 'lyle@covalent.invalid', "
            "'Lyle', '', 'body', '2026-08-25T10:00:00+00:00', "
            "'2026-08-25T10:00:00+00:00', 10)", (inbox,))
        con.commit()
        self.person(con)
        code, text = self.run_cli("--contacts", "lyle")
        self.assertIn("1 from them", text)

    def test_duplicates_are_NAMED_and_the_remedy_is_given(self):
        """A pair is only actionable if you know which two. `--check` counts
        them; this names them, because the merge is in the pane and this
        report is read-only."""
        con, _ = self.account()
        self.person(con, name="Tom Whitfield", address="tom@northgate.invalid")
        self.person(con, name="Tom Whitfield", address="t.w@northgate.invalid")
        code, text = self.run_cli("--contacts")
        self.assertIn("Possibly the same person", text)
        self.assertIn("both are called Tom Whitfield", text)
        self.assertIn("Address book", text)

    def test_the_advice_says_which_card_to_keep(self):
        """`merge_contacts` fills the KEPT card's empty fields from the other,
        so keeping the emptier one loses whichever field both of them have.
        Advice that did not say which way round would be advice that halves
        the time it is followed."""
        con, _ = self.account()
        self.person(con, name="Tom Whitfield", address="tom@northgate.invalid",
                    org="Northgate Print")
        self.person(con, name="Tom Whitfield", address="")
        code, text = self.run_cli("--contacts")
        self.assertIn("fuller card is the one to keep", text)

    def test_a_narrowed_list_does_not_print_the_duplicate_section(self):
        """It is a report about the WHOLE book, and printing it under a query
        that shows one person would say those two are the ones matching."""
        con, _ = self.account()
        self.person(con, name="Tom Whitfield", address="tom@northgate.invalid")
        self.person(con, name="Tom Whitfield", address="t.w@northgate.invalid")
        code, text = self.run_cli("--contacts", "tom@northgate.invalid")
        self.assertNotIn("Possibly the same person", text)

    def test_somebody_with_no_address_is_not_told_they_have_no_mail(self):
        """FOUND BY PRINTING THE REPORT AND READING IT, which is the five
        minutes `--filters` spent and found three faults in.

        "no mail either way" is true of a card with only a telephone number
        and it is useless — the fact is that nothing CAN be sent. The card two
        files away already said the other thing about the same person, so this
        was one question answered two ways on two surfaces.
        """
        con, _ = self.account()
        self.person(con, name="Ravi at the unit", address="")
        contacts_repo.add_handle(con, self.people_last(con), "phone",
                                 "+44 7700 900789")
        code, text = self.run_cli("--contacts")
        self.assertIn("No address, so nothing can be sent", text)
        self.assertNotIn("no mail either way", text)

    def test_somebody_with_an_address_and_no_mail_says_the_other_thing(self):
        """The negative half. A single sentence for both silences would pass
        the test above by saying the wrong thing everywhere instead of
        somewhere."""
        con, _ = self.account()
        self.person(con, name="Written down by hand")
        code, text = self.run_cli("--contacts")
        self.assertIn("No mail either way", text)

    def test_a_duplicate_pair_can_be_told_apart(self):
        """The first rule `duplicates` applies is "these two have the same
        name", so printing the name twice reads "Tom Whitfield and Tom
        Whitfield" — a pair nobody can act on, under a heading claiming it is
        actionable. The organisation and the first handle are what differ."""
        con, _ = self.account()
        self.person(con, name="Tom Whitfield", address="tom@northgate.invalid",
                    org="Northgate Print")
        self.person(con, name="Tom Whitfield", address="t.w@elsewhere.invalid")
        code, text = self.run_cli("--contacts")
        pair = [line for line in text.splitlines()
                if " and " in line and "Tom Whitfield" in line]
        self.assertEqual(len(pair), 1, text)
        self.assertIn("Northgate Print", pair[0])
        self.assertIn("t.w@elsewhere.invalid", pair[0])

    def test_a_card_with_nothing_to_tell_it_apart_by_is_named_by_its_id(self):
        """Rather than by nothing at all, which would print "X and X" again in
        the one case the fix above does not cover."""
        con, _ = self.account()
        self.person(con, name="Anon", address="")
        self.person(con, name="Anon", address="")
        code, text = self.run_cli("--contacts")
        self.assertIn("nothing else on the card", text)

    def test_a_status_that_is_not_active_is_drawn(self):
        con, _ = self.account()
        self.person(con, name="Gone",
                    status=contacts_repo.CONTACT_DO_NOT_CONTACT)
        code, text = self.run_cli("--contacts")
        self.assertIn("do-not-contact", text)


class TestTheCheckLines(BookFixture):
    def test_an_empty_book_prints_no_address_book_line_at_all(self):
        """`--check` is read top to bottom when something is wrong, and a line
        reading "0 contacts" is a line between the reader and the fault."""
        self.store()
        code, text = self.run_cli("--check")
        self.assertNotIn("address book", text)

    def test_it_counts_the_people_the_handles_and_the_channels(self):
        con, _ = self.account()
        contact_id = self.person(con)
        contacts_repo.add_handle(con, contact_id, "phone", "+44 7700 900123")
        code, text = self.run_cli("--check")
        self.assertIn("address book", text)
        self.assertIn("1 contacts, 2 handles", text)
        self.assertIn("1 email", text)
        self.assertIn("1 phone", text)

    def test_a_bounce_is_reported_with_what_it_means(self):
        """Marked during a SYNC, by `store/ingest.py` reading a delivery
        report — which is why the address book needs a read-out at all. The
        consequence is named rather than the state, because "1 bounced" does
        not tell anybody what will happen next time they write."""
        con, _ = self.account()
        self.person(con)
        contacts_repo.note_bounce(con, "lyle@covalent.invalid", "550 gone")
        code, text = self.run_cli("--check")
        self.assertIn("1 address has bounced", text)
        self.assertIn("composer will warn", text)

    def test_a_card_with_no_address_is_a_separate_line_from_a_bounce(self):
        """Not the same fault: a bounced address refused, and no address at
        all is a card the composer can never offer. The remedy differs."""
        con, _ = self.account()
        self.person(con, name="Telephone only", address="")
        code, text = self.run_cli("--check")
        self.assertIn("no email address", text)
        self.assertNotIn("has bounced", text)

    def test_duplicates_are_counted_and_pointed_at_the_long_form(self):
        con, _ = self.account()
        self.person(con, name="Tom Whitfield", address="tom@northgate.invalid")
        self.person(con, name="Tom Whitfield", address="t.w@northgate.invalid")
        code, text = self.run_cli("--check")
        self.assertIn("1 possible duplicate", text)
        self.assertIn("--contacts", text)

    def test_the_grammar_is_singular_and_plural_in_the_right_places(self):
        """`--filters` learnt this by printing itself: "1 enabled rule(s) have
        never matched" mixes a plural marker with a singular verb."""
        con, _ = self.account()
        for n in range(2):
            contact_id = self.person(con, name=f"P{n}",
                                     address=f"p{n}@covalent.invalid")
            contacts_repo.note_bounce(con, f"p{n}@covalent.invalid", "550 gone")
            self.assertTrue(contact_id)
        code, text = self.run_cli("--check")
        self.assertIn("2 addresses have bounced", text)
        self.assertNotIn("2 address has", text)

    def test_check_still_succeeds_over_a_book_with_faults_in_it(self):
        """A bounced address is a fact about the world, not a broken
        installation. `--check`'s verdict is about whether corMani can run."""
        con, _ = self.account()
        self.person(con)
        contacts_repo.note_bounce(con, "lyle@covalent.invalid", "550 gone")
        code, text = self.run_cli("--check")
        self.assertIn("ready", text)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
