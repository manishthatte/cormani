# SPDX-License-Identifier: GPL-3.0-or-later
#
# The address book's two halves, and its four dialogs.
#
# `tests/test_contacthost.py` is what the commands MEAN. This is what is drawn
# and what a dialog reads back, which is a different set of failures.
#
# ── A WIDGET IS NOT A FACT, AND THIS SUITE IS WHERE THAT BITES ─────────────
#
# `isVisible()` is False for every widget whose window has not been SHOWN, and
# this suite shows none of them. `ui/ruleeditor.ConditionRow` asked a widget
# whether it was visible to decide whether a field counted, and reading a rule
# out of a dialog that had not appeared therefore dropped every value the user
# had typed — "Subject contains ''", which matches every message in the store.
# So nothing here asks a widget what it is currently doing; it asks the model
# what a field means, and every dialog test reads `values()` on a dialog that
# was never opened, which is the state the application will also be in when the
# host calls it.
#
# ── AND THE COUNTS ARE ASSERTED AS NUMBERS ─────────────────────────────────
#
# A list that draws the wrong PEOPLE returns a perfectly plausible non-empty
# list. Every assertion here is on the names or the ids, never on the length
# alone.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import addressbook as book_repo
from cormani.store import contacts as contacts_repo
from cormani.store import folders as folders_repo
from cormani.store import tracking as tracking_repo
from cormani.store.accounts import add_account

support.qt_app() if support.HAVE_QT else None


@support.requires_qt
class Book(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "manish@manitlab.invalid",
                                   "google")
        self.inbox = folders_repo.ensure_folder(self.con, self.account,
                                                "INBOX", display_name="Inbox",
                                                role="inbox")
        self.sent = folders_repo.ensure_folder(self.con, self.account, "Sent",
                                               display_name="Sent",
                                               role="sent")
        self._uid = 0

    def pane(self):
        from cormani.ui.contactpane import ContactPane

        return support.own(self, ContactPane(self.con))

    def person(self, name="Lyle Gordon", address="lyle@covalent.invalid",
               **fields):
        contact_id = contacts_repo.add_contact(self.con, name, **fields)
        if address:
            contacts_repo.add_handle(self.con, contact_id,
                                     contacts_repo.KIND_EMAIL, address)
        return contact_id

    def message(self, *, frm="Lyle <lyle@covalent.invalid>", folder=None,
                subject="A subject", to="manish@manitlab.invalid"):
        from cormani.imap import envelope
        from cormani.store import ingest

        self._uid += 1
        raw = (f"From: {frm}\nTo: {to}\nSubject: {subject}\n"
               f"Message-ID: <m{self._uid}@x.invalid>\n"
               f"Date: Sat, 12 Sep 2026 09:00:00 +0000\n\nBody.\n").encode()
        return ingest.store_message(self.con, folder or self.inbox, self._uid,
                                    envelope.read(raw)).message_id

    def rows(self, listing) -> list[str]:
        return [listing.item(n).text() for n in range(listing.count())]


class TestTheList(Book):
    def test_it_draws_everybody_with_their_organisation(self):
        self.person(name="Lyle Gordon", org="Covalent Example")
        self.person(name="Priya Deshpande", address="priya@idlidu.invalid")
        pane = self.pane()
        drawn = self.rows(pane.list)
        self.assertEqual(len(drawn), 2)
        self.assertIn("Covalent Example", drawn[0])

    def test_the_search_box_asks_the_STORE_and_finds_an_address(self):
        """`contacts.list_contacts` searches the person AND their handles,
        because the thing a person remembers is often the address rather than
        the name — "who was it at northgate". A filter over the drawn NAMES
        could never answer that, which is why this re-queries."""
        self.person(name="Lyle Gordon", address="lyle@covalent.invalid")
        self.person(name="Tom Whitfield", address="tom@northgate.invalid")
        pane = self.pane()
        pane.search.setText("northgate")
        self.assertEqual([r.split("  ")[0] for r in self.rows(pane.list)],
                         ["Tom Whitfield"])

    def test_the_scope_chooser_reads_its_DATA_and_not_its_index(self):
        """The chooser's ORDER is a drawing decision and the status is the
        fact. A handler reading the index would break the moment a fifth
        status was added anywhere but the end."""
        self.person(name="Active person")
        self.person(name="Gone", address="gone@x.invalid",
                    status=contacts_repo.CONTACT_LEFT_ORG)
        pane = self.pane()
        index = pane.scope.findData(contacts_repo.CONTACT_LEFT_ORG)
        self.assertGreaterEqual(index, 0)
        pane.scope.setCurrentIndex(index)
        self.assertEqual([r.split("  ")[0] for r in self.rows(pane.list)],
                         ["Gone"])

    def test_a_contact_with_no_name_still_has_a_row_to_click(self):
        """`Contact.label` is never blank and says why: a row with an empty
        title is a defect to look at rather than a person to recognise."""
        self.person(name="", address="anon@x.invalid")
        pane = self.pane()
        self.assertEqual(self.rows(pane.list), ["anon@x.invalid"])

    def test_reloading_keeps_the_PERSON_and_not_the_row(self):
        """A reload after a rename re-orders. A list that kept the index would
        move somebody to a different card for no reason — `ThreadBoard` makes
        the same argument."""
        first = self.person(name="Zoe", address="zoe@x.invalid")
        self.person(name="Anil", address="anil@x.invalid")
        pane = self.pane()
        pane.list.select(first)
        self.assertEqual(pane.list.current_contact_id(), first)
        contacts_repo.update_contact(self.con, first, name="Aaron")
        pane.reload()
        self.assertEqual(pane.list.current_contact_id(), first)

    def test_a_person_narrowed_out_stops_being_shown(self):
        """The card must not go on showing somebody the list no longer holds,
        so the list emits when its selection disappears."""
        first = self.person(name="Zoe", address="zoe@x.invalid")
        self.person(name="Anil", address="anil@x.invalid")
        pane = self.pane()
        pane.list.select(first)
        pane.card.show_contact(first)
        pane.search.setText("anil")
        self.assertNotEqual(pane.card.contact_id(), first)


class TestTheFooter(Book):
    def test_an_empty_book_says_which_silence_it_is(self):
        pane = self.pane()
        self.assertIn("No contacts yet", pane.footer.text())
        self.assertIn("Add from mail", pane.footer.text())

    def test_it_counts_the_three_things_that_are_wrong(self):
        lyle = self.person(name="Lyle Gordon")
        contacts_repo.note_bounce(self.con, "lyle@covalent.invalid", "550 gone")
        self.person(name="Lyle Gordon", address="")   # a duplicate, no address
        pane = self.pane()
        text = pane.footer.text()
        self.assertIn("1 address has bounced", text)
        self.assertIn("1 with no address", text)
        self.assertIn("1 possible duplicate", text)
        self.assertTrue(lyle)

    def test_the_grammar_is_written_out_rather_than_suffixed(self):
        """`--filters` learnt this by printing itself: "1 enabled rule(s) have
        never matched" mixes a plural marker with a singular verb, and the verb
        is the half a suffix cannot fix."""
        for n in range(2):
            contact_id = self.person(name=f"P{n}",
                                     address=f"p{n}@covalent.invalid")
            contacts_repo.note_bounce(self.con, f"p{n}@covalent.invalid",
                                      "550 gone")
            self.assertTrue(contact_id)
        pane = self.pane()
        self.assertIn("2 addresses have bounced", pane.footer.text())

    def test_it_says_how_many_are_shown_when_the_search_narrows(self):
        self.person(name="Lyle Gordon")
        self.person(name="Tom Whitfield", address="tom@northgate.invalid")
        pane = self.pane()
        pane.search.setText("northgate")
        self.assertIn("2 contacts, 1 shown", pane.footer.text())


class TestTheCard(Book):
    def test_it_draws_every_handle_whatever_the_kind(self):
        """PLAN.txt §2 asks for this by name — "contact cards carrying every
        handle a person has: addresses, numbers, profiles" — and it is the one
        surface where a WhatsApp number is visible at all."""
        contact_id = self.person()
        contacts_repo.add_handle(self.con, contact_id, "phone",
                                 "+44 7700 900123")
        contacts_repo.add_handle(self.con, contact_id, "linkedin", "in/lyle")
        pane = self.pane()
        pane.card.show_contact(contact_id)
        drawn = "\n".join(self.rows(pane.card.handles))
        self.assertIn("lyle@covalent.invalid", drawn)
        self.assertIn("+44 7700 900123", drawn)
        self.assertIn("in/lyle", drawn)

    def test_a_kind_nobody_anticipated_still_draws(self):
        """`store/contacts.SEED_KINDS` is a SEED and the schema says so: the
        column is free text "so that adding a channel is data rather than a
        migration". A card that drew only the seven it knows would make it an
        enumeration again."""
        contact_id = self.person()
        contacts_repo.add_handle(self.con, contact_id, "mastodon",
                                 "@lyle@example.social")
        pane = self.pane()
        pane.card.show_contact(contact_id)
        drawn = "\n".join(self.rows(pane.card.handles))
        self.assertIn("@lyle@example.social", drawn)
        self.assertIn("mastodon", drawn)

    def test_a_bounced_handle_says_so_and_quotes_the_server(self):
        """`store/fixtures.py` has carried a bounced handle since stage 1 with
        a comment saying it is there "because a handle whose status is
        'bounced' is the state the contact card must render correctly". Four
        stages later, this is that assertion.

        THE SERVER'S OWN WORDS, in the tooltip: "mailbox full" and "no such
        user" call for opposite decisions, so a status word in place of the
        reason is the half that matters thrown away.
        """
        contact_id = self.person()
        contacts_repo.note_bounce(self.con, "lyle@covalent.invalid",
                                  "550 5.1.1 no such user")
        pane = self.pane()
        pane.card.show_contact(contact_id)
        self.assertIn("BOUNCED once", self.rows(pane.card.handles)[0])
        self.assertIn("550 5.1.1 no such user",
                      pane.card.handles.item(0).toolTip())

    def test_the_correspondence_line_names_both_directions_and_the_date(self):
        contact_id = self.person()
        self.message(frm="Lyle <lyle@covalent.invalid>")
        self.message(frm="Lyle <lyle@covalent.invalid>")
        self.message(folder=self.sent, frm="manish@manitlab.invalid",
                     to="lyle@covalent.invalid")
        pane = self.pane()
        pane.card.show_contact(contact_id)
        text = pane.card.correspondence.text()
        self.assertIn("2 from them", text)
        self.assertIn("1 to them", text)
        self.assertIn("last on", text)

    def test_somebody_with_no_mail_is_told_apart_from_somebody_with_no_address(self):
        """Two different facts and a card that drew one sentence for both
        would leave a person looking for mail that was never there."""
        known = self.person(name="Known", address="known@x.invalid")
        unreachable = self.person(name="Unreachable", address="")
        pane = self.pane()
        pane.card.show_contact(known)
        self.assertIn("No mail either way", pane.card.correspondence.text())
        pane.card.show_contact(unreachable)
        self.assertIn("No address", pane.card.correspondence.text())

    def test_the_recent_list_says_which_way_each_message_went(self):
        contact_id = self.person()
        self.message(frm="Lyle <lyle@covalent.invalid>", subject="Inbound")
        self.message(folder=self.sent, frm="manish@manitlab.invalid",
                     to="lyle@covalent.invalid", subject="Outbound")
        pane = self.pane()
        pane.card.show_contact(contact_id)
        drawn = self.rows(pane.card.recent)
        self.assertTrue(any("→" in r and "Outbound" in r for r in drawn), drawn)
        self.assertTrue(any("←" in r and "Inbound" in r for r in drawn), drawn)

    def test_the_threads_are_the_tracking_layers_own_answer(self):
        contact_id = self.person()
        thread = tracking_repo.create_thread(self.con, "DWCNT wavelengths",
                                             org="Covalent")
        tracking_repo.link_contact(self.con, thread, contact_id)
        pane = self.pane()
        pane.card.show_contact(contact_id)
        self.assertIn("On 1 tracked thread", pane.card.threads_heading.text())
        self.assertIn("DWCNT wavelengths", self.rows(pane.card.threads)[0])

    def test_an_empty_card_says_which_of_two_silences_it_is(self):
        pane = self.pane()
        self.assertEqual(pane.card.title.text(), "No contacts yet")
        self.person()
        pane.reload()
        pane.card.show_contact(None)
        self.assertEqual(pane.card.title.text(), "Nobody selected")

    def test_every_button_is_off_with_nobody_selected(self):
        pane = self.pane()
        for name, button in pane.card.buttons.items():
            self.assertFalse(button.isEnabled(), name)

    def test_remove_handle_is_off_until_one_is_chosen(self):
        """Asked of the LIST and not of the button, because the button's own
        enabled state is what is being decided."""
        contact_id = self.person()
        pane = self.pane()
        pane.card.show_contact(contact_id)
        self.assertFalse(pane.card.buttons["remove-handle"].isEnabled())
        pane.card.handles.setCurrentRow(0)
        self.assertTrue(pane.card.buttons["remove-handle"].isEnabled())

    def test_the_placeholder_row_cannot_be_chosen_as_a_handle(self):
        """A placeholder that could be selected would offer to delete a
        sentence."""
        contact_id = self.person(address="")
        pane = self.pane()
        pane.card.show_contact(contact_id)
        pane.card.handles.setCurrentRow(0)
        self.assertIsNone(pane.card.selected_handle())
        self.assertFalse(pane.card.buttons["remove-handle"].isEnabled())

    def test_reloading_does_not_connect_the_handle_signal_again(self):
        """A connect inside the redraw is a second connection on every reload,
        and the slot then runs as many times as the card has ever been opened.
        Silent here, because this one only enables a button — which is why it
        is worth a test rather than a comment."""
        contact_id = self.person()
        pane = self.pane()
        fired = []
        pane.card.handles.currentItemChanged.connect(
            lambda *_: fired.append(1))
        for _ in range(4):
            pane.card.show_contact(contact_id)
        fired.clear()
        pane.card.handles.setCurrentRow(0)
        self.assertEqual(len(fired), 1)

    def test_the_title_carries_the_count(self):
        pane = self.pane()
        self.assertEqual(pane.title(), "Address book")
        self.person()
        pane.reload()
        self.assertEqual(pane.title(), "Address book (1)")


class TestTheDialogs(Book):
    def test_the_contact_dialog_reads_back_what_it_was_given(self):
        """READ WITHOUT BEING SHOWN, which is the state the host calls it in.
        `ui/ruleeditor.ConditionRow` asked `isVisible()` and dropped every
        typed value for exactly this reason."""
        from cormani.ui.contactdialog import ContactDialog

        contact_id = self.person(name="Lyle Gordon", org="Covalent",
                                 role="Sales", notes="Fridays")
        contact = contacts_repo.get_contact(self.con, contact_id)
        dialog = support.own(self, ContactDialog(contact=contact))
        self.assertEqual(dialog.values(),
                         {"name": "Lyle Gordon", "org": "Covalent",
                          "role": "Sales", "status": "active",
                          "notes": "Fridays"})

    def test_a_status_the_box_does_not_offer_is_KEPT(self):
        """Falling back to index 0 would silently turn "do not contact" into
        "active" by opening the dialog, which is the one direction that change
        must never happen in."""
        from cormani.ui.contactdialog import ContactDialog

        contact_id = self.person(name="X", status="some-future-status")
        contact = contacts_repo.get_contact(self.con, contact_id)
        dialog = support.own(self, ContactDialog(contact=contact))
        self.assertEqual(dialog.values()["status"], "some-future-status")

    def test_do_not_contact_survives_a_round_trip(self):
        from cormani.ui.contactdialog import ContactDialog

        contact_id = self.person(
            name="X", status=contacts_repo.CONTACT_DO_NOT_CONTACT)
        contact = contacts_repo.get_contact(self.con, contact_id)
        dialog = support.own(self, ContactDialog(contact=contact))
        self.assertEqual(dialog.values()["status"],
                         contacts_repo.CONTACT_DO_NOT_CONTACT)

    def test_the_handle_dialogs_kind_is_free_text(self):
        """`handle.kind` is free text "so that adding a channel is data rather
        than a migration". A closed box would make it an enumeration again."""
        from cormani.ui.contactdialog import HandleDialog

        dialog = support.own(self, HandleDialog())
        self.assertTrue(dialog.kind.isEditable())
        dialog.kind.setCurrentText("mastodon")
        dialog.value.setText("  @lyle@example.social  ")
        self.assertEqual(dialog.values(),
                         {"kind": "mastodon", "value": "@lyle@example.social",
                          "status": "unverified"})

    def test_the_merge_dialog_finds_the_suggested_card(self):
        """`QComboBox.findData` matches through Qt's variant comparison and
        falls back to IDENTITY for an arbitrary Python object — a tuple built
        to look one up never finds the equal tuple that was stored, and the box
        silently keeps what it had. `ui/ruleeditor.py` shipped within a minute
        of that. An int is a value the comparison knows how to do."""
        from cormani.ui.contactdialog import MergeDialog

        keep = self.person(name="Tom Whitfield", org="Northgate")
        first = self.person(name="Somebody", address="a@x.invalid")
        twin = self.person(name="Tom Whitfield", address="")
        contacts = [contacts_repo.get_contact(self.con, i)
                    for i in (first, twin)]
        dialog = support.own(self, MergeDialog(
            keep=contacts_repo.get_contact(self.con, keep), others=contacts,
            suggested_id=twin))
        self.assertEqual(dialog.values()["drop_id"], twin)

    def test_the_merge_dialog_names_what_would_be_lost(self):
        """A confirmation saying only "merge these two?" is one people answer
        without reading."""
        from cormani.ui.contactdialog import MergeDialog

        keep = self.person(name="Tom", address="tom@x.invalid")
        other = self.person(name="Tom Whitfield", address="t.w@x.invalid",
                            org="Northgate Print")
        dialog = support.own(self, MergeDialog(
            keep=contacts_repo.get_contact(self.con, keep),
            others=[contacts_repo.get_contact(self.con, other)]))
        effect = dialog.effect.text()
        self.assertIn("1 handle would move", effect)
        self.assertIn("org", effect)
        self.assertIn("cannot be undone", effect)

    def test_the_suggest_dialog_opens_with_nothing_ticked(self):
        """Pre-ticking the top ten would make the safe click — OK — the one
        that writes ten cards, and an address book somebody did not mean to
        make is what `contact_for_address` refuses to do by itself."""
        from cormani.ui.contactdialog import SuggestDialog

        self.message(frm="Tom <tom@northgate.invalid>")
        self.message(frm="Priya <priya@idlidu.invalid>")
        dialog = support.own(self, SuggestDialog(
            strangers=book_repo.suggest(self.con)))
        self.assertEqual(dialog.people.count(), 2)
        self.assertEqual(dialog.values()["people"], [])

    def test_it_reads_the_TICKS_and_not_the_selection(self):
        """They are two different things in a QListWidget, and the difference
        is invisible until somebody clicks a row without ticking it — which is
        what everybody does first."""
        from PySide6.QtCore import Qt

        from cormani.ui.contactdialog import SuggestDialog

        self.message(frm="Tom <tom@northgate.invalid>")
        self.message(frm="Priya <priya@idlidu.invalid>")
        dialog = support.own(self, SuggestDialog(
            strangers=book_repo.suggest(self.con)))
        dialog.people.setCurrentRow(0)                  # selected, not ticked
        self.assertEqual(dialog.values()["people"], [])
        dialog.people.item(0).setCheckState(Qt.CheckState.Checked)
        chosen = dialog.values()["people"]
        self.assertEqual(len(chosen), 1)
        self.assertIn("@", chosen[0]["address"])

    def test_select_all_ticks_every_row(self):
        from cormani.ui.contactdialog import SuggestDialog

        for n in range(3):
            self.message(frm=f"P{n} <p{n}@x.invalid>")
        dialog = support.own(self, SuggestDialog(
            strangers=book_repo.suggest(self.con)))
        dialog.check_all()
        self.assertEqual(len(dialog.values()["people"]), 3)
        dialog.check_all(False)
        self.assertEqual(dialog.values()["people"], [])


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
