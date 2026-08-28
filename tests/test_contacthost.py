# SPDX-License-Identifier: GPL-3.0-or-later
#
# What every address-book command MEANS, driven from a pane.
#
# EVERY TEST STARTS AT A SIGNAL OR A COMMAND NAME AND ENDS AT THE STORE, which
# is the rule stage 3 and stage 4 each paid a feature to learn: an exception in
# a Qt slot does not reach the caller — it is printed and the program carries
# on — so a signal handler that raises looks exactly like a button that does
# nothing. Asserting that a method returned would not have caught either of the
# two features that were lost that way.
#
# THE DIALOGS AND THE CONFIRMATION ARE INJECTED. Debian ships no QTest and the
# real ones are modal, so a test that opened one would hang. `ui/contacthost.py`
# takes them as parameters for exactly this reason.
#
# WHAT IS WORTH ASSERTING HERE IS THE THREE WAYS THIS LAYER CAN BE WRONG
# WITHOUT LOOKING WRONG:
#
#   - a command that quietly does nothing, because nobody is selected or the
#     card has no address. Each of those has to SAY so — `ui/filterhost.py`'s
#     rule, that a menu item appearing to do nothing is the failure this kind
#     of file exists to prevent.
#   - a change carried out without asking. Deleting a contact and merging two
#     are both undoable by nothing at all.
#   - the wrong person. A merge in the wrong direction loses the fuller card's
#     fields, and it cannot be taken back.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import contacts as contacts_repo
from cormani.store import folders as folders_repo
from cormani.store import ingest
from cormani.store.accounts import add_account

support.qt_app() if support.HAVE_QT else None


class Values:
    """A dialog that was never opened, answering with what a person typed."""

    def __init__(self, **values):
        self._values = values

    def values(self) -> dict:
        return dict(self._values)


@support.requires_qt
class Host(unittest.TestCase):
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
        self.asked = []
        self.answer = True
        self._uid = 0

    def pane(self, **dialogs):
        from cormani.ui.mailpane import MailPane

        dialogs.setdefault("run", lambda dialog: True)
        dialogs.setdefault("confirm", self.confirm)
        pane = support.own(self, MailPane(self.con, dialogs=dialogs))
        self.said = []
        pane.status_message.connect(self.said.append)
        return pane

    def confirm(self, title, text):
        self.asked.append(text)
        return self.answer

    def person(self, name="Lyle Gordon", address="lyle@covalent.invalid",
               **fields):
        contact_id = contacts_repo.add_contact(self.con, name, **fields)
        if address:
            contacts_repo.add_handle(self.con, contact_id,
                                     contacts_repo.KIND_EMAIL, address)
        return contact_id

    def message(self, *, frm="Tom Whitfield <tom@northgate.invalid>",
                folder=None, subject="A subject"):
        from cormani.imap import envelope

        self._uid += 1
        raw = (f"From: {frm}\nTo: manish@manitlab.invalid\n"
               f"Subject: {subject}\nMessage-ID: <m{self._uid}@x.invalid>\n"
               f"Date: Sat, 12 Sep 2026 09:00:00 +0000\n\nBody.\n").encode()
        return ingest.store_message(self.con, folder or self.inbox, self._uid,
                                    envelope.read(raw)).message_id


class TestMaking(Host):
    def test_new_writes_a_contact_and_selects_it(self):
        pane = self.pane(contact=lambda **kw: Values(
            name="Priya Deshpande", org="idlidu Ltd", role="Bookkeeper",
            status="active", notes=""))
        pane.contacts.open()
        self.assertTrue(pane.contacts.action("new"))
        found = contacts_repo.list_contacts(self.con)
        self.assertEqual([c.name for c in found], ["Priya Deshpande"])
        self.assertEqual(pane.contacts.contacts.contact_id(), found[0].id)

    def test_an_entirely_empty_card_is_refused_and_said(self):
        """A card with nothing on it is not a person; it is a row somebody
        finds later and does not recognise. Refused with a sentence rather
        than written, because a button that appears to do nothing is the
        failure this file exists to prevent."""
        pane = self.pane(contact=lambda **kw: Values(
            name="  ", org="", role="", status="active", notes=""))
        pane.contacts.open()
        self.assertFalse(pane.contacts.action("new"))
        self.assertEqual(contacts_repo.counts(self.con)["contacts"], 0)
        self.assertTrue(any("needs at least a name" in s for s in self.said))

    def test_new_works_with_nobody_selected(self):
        """`new` and `suggest` are handled BEFORE the "is anybody selected"
        guard, and this is why: an address book with nobody in it is exactly
        when they are wanted, so a guard demanding a selection would make an
        empty book impossible to fill."""
        pane = self.pane(contact=lambda **kw: Values(
            name="Somebody", org="", role="", status="active", notes=""))
        pane.contacts.open()
        self.assertIsNone(pane.contacts.contacts.contact_id())
        self.assertTrue(pane.contacts.action("new"))

    def test_editing_changes_the_row_and_not_a_copy(self):
        contact_id = self.person(name="Lyle Gordon")
        pane = self.pane(contact=lambda **kw: Values(
            name="Lyle Gordon", org="Covalent Example",
            role="Sales engineer", status="active", notes="Rings on Fridays"))
        pane.contacts.open()
        self.assertTrue(pane.contacts.action("edit", contact_id=contact_id))
        again = contacts_repo.get_contact(self.con, contact_id)
        self.assertEqual(again.org, "Covalent Example")
        self.assertEqual(again.role, "Sales engineer")
        self.assertEqual(contacts_repo.counts(self.con)["contacts"], 1)


class TestHandles(Host):
    def test_adding_one_reaches_the_store(self):
        contact_id = self.person(address="")
        pane = self.pane(handle=lambda **kw: Values(
            kind="phone", value="+44 7700 900123", status="verified"))
        pane.contacts.open()
        self.assertTrue(pane.contacts.action("add-handle",
                                             contact_id=contact_id))
        contact = contacts_repo.get_contact(self.con, contact_id)
        self.assertEqual([(h.kind, h.value) for h in contact.handles],
                         [("phone", "+44 7700 900123")])

    def test_taking_an_address_off_somebody_else_is_SAID(self):
        """`contacts.add_handle` MOVES a handle that belongs to another card —
        deliberately, because an address has one owner. A move nobody was told
        about is a card that quietly lost its address, and the person who
        notices is the one who later cannot find it."""
        first = self.person(name="Lyle Gordon", address="shared@x.invalid")
        second = self.person(name="Frances Baker", address="")
        pane = self.pane(handle=lambda **kw: Values(
            kind="email", value="shared@x.invalid", status="unverified"))
        pane.contacts.open()
        self.assertTrue(pane.contacts.action("add-handle", contact_id=second))
        self.assertTrue(any("moved here from" in s and "Lyle Gordon" in s
                            for s in self.said), self.said)
        self.assertEqual(
            [h.value for h in
             contacts_repo.get_contact(self.con, first).handles], [])

    def test_an_ordinary_add_does_not_claim_a_move(self):
        """The negative half of the test above. A message saying an address
        "moved here from" nobody would be worse than none — and a check that
        only ever ran on the moving case would not notice it appearing on the
        other."""
        contact_id = self.person(address="")
        pane = self.pane(handle=lambda **kw: Values(
            kind="email", value="fresh@x.invalid", status="unverified"))
        pane.contacts.open()
        pane.contacts.action("add-handle", contact_id=contact_id)
        self.assertFalse(any("moved here from" in s for s in self.said),
                         self.said)

    def test_a_handle_with_no_value_is_refused_and_said(self):
        contact_id = self.person()
        pane = self.pane(handle=lambda **kw: Values(kind="phone", value="  ",
                                                    status="unverified"))
        pane.contacts.open()
        self.assertFalse(pane.contacts.action("add-handle",
                                              contact_id=contact_id))
        self.assertIn("A handle needs a value.", self.said)

    def test_removing_one_needs_a_handle_chosen_and_says_so(self):
        contact_id = self.person()
        pane = self.pane()
        pane.contacts.open()
        pane.contacts.contacts.show_contact(contact_id)
        pane.contacts.contacts.card.handles.setCurrentRow(-1)
        self.assertFalse(pane.contacts.action("remove-handle",
                                              contact_id=contact_id))
        self.assertTrue(any("Choose a handle" in s for s in self.said))

    def test_removing_one_asks_first(self):
        contact_id = self.person()
        pane = self.pane()
        pane.contacts.open()
        pane.contacts.contacts.show_contact(contact_id)
        pane.contacts.contacts.card.handles.setCurrentRow(0)
        self.answer = False
        self.assertFalse(pane.contacts.action("remove-handle",
                                              contact_id=contact_id))
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(contacts_repo.counts(self.con)["handles"], 1)

        self.answer = True
        self.assertTrue(pane.contacts.action("remove-handle",
                                             contact_id=contact_id))
        self.assertEqual(contacts_repo.counts(self.con)["handles"], 0)


class TestDeleting(Host):
    def test_it_asks_and_a_no_writes_nothing(self):
        contact_id = self.person()
        pane = self.pane()
        pane.contacts.open()
        self.answer = False
        self.assertFalse(pane.contacts.action("delete", contact_id=contact_id))
        self.assertEqual(len(self.asked), 1)
        self.assertIsNotNone(contacts_repo.get_contact(self.con, contact_id))

    def test_the_question_names_what_survives(self):
        """The half people fear. A thread keeps its timeline and the mail is
        not touched at all, and a confirmation that did not say so would be
        answered No by somebody who wanted Yes."""
        contact_id = self.person()
        pane = self.pane()
        pane.contacts.open()
        self.answer = False
        pane.contacts.action("delete", contact_id=contact_id)
        self.assertIn("The mail stays", self.asked[0])
        self.assertIn("cannot be undone", self.asked[0])

    def test_a_yes_removes_the_card_and_leaves_the_mail(self):
        self.message(frm="Lyle <lyle@covalent.invalid>")
        contact_id = self.person()
        pane = self.pane()
        pane.contacts.open()
        self.assertTrue(pane.contacts.action("delete", contact_id=contact_id))
        self.assertIsNone(contacts_repo.get_contact(self.con, contact_id))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 1)


class TestMerging(Host):
    def test_it_merges_into_the_card_the_command_names(self):
        keep = self.person(name="Lyle Gordon", address="lyle@covalent.invalid",
                           org="Covalent Example")
        drop = self.person(name="Lyle Gordon", address="l.g@covalent.invalid")
        pane = self.pane(merge=lambda **kw: Values(drop_id=drop))
        pane.contacts.open()
        self.assertTrue(pane.contacts.action("merge", contact_id=keep))
        self.assertIsNone(contacts_repo.get_contact(self.con, drop))
        survivor = contacts_repo.get_contact(self.con, keep)
        self.assertEqual(sorted(h.value for h in survivor.handles),
                         ["l.g@covalent.invalid", "lyle@covalent.invalid"])
        self.assertEqual(survivor.org, "Covalent Example")

    def test_the_dialog_is_offered_the_pair_duplicates_found(self):
        """`_suggested_pair` is the whole reason `duplicates` is wired in
        here: it starts the box on the card most likely to be right, and never
        acts on it, because a wrong merge cannot be taken back."""
        keep = self.person(name="Tom Whitfield", address="tom@x.invalid",
                           org="Northgate")
        twin = self.person(name="Tom Whitfield", address="")
        seen = {}

        def merge(**kw):
            seen.update(kw)
            return Values(drop_id=None)

        pane = self.pane(merge=merge)
        pane.contacts.open()
        pane.contacts.action("merge", contact_id=keep)
        self.assertEqual(seen["suggested_id"], twin)

    def test_with_nobody_else_it_says_so_rather_than_opening_a_dialog(self):
        contact_id = self.person()
        opened = []
        pane = self.pane(merge=lambda **kw: opened.append(kw) or Values())
        pane.contacts.open()
        self.assertFalse(pane.contacts.action("merge", contact_id=contact_id))
        self.assertEqual(opened, [])
        self.assertIn("There is nobody else to merge in.", self.said)

    def test_merging_a_card_into_itself_writes_nothing(self):
        """`merge_contacts` returns 0 for it, but the card would still be
        reported as merged and the list would redraw around a deletion that
        did not happen."""
        contact_id = self.person()
        self.person(name="Somebody else", address="else@x.invalid")
        pane = self.pane(merge=lambda **kw: Values(drop_id=contact_id))
        pane.contacts.open()
        self.assertFalse(pane.contacts.action("merge", contact_id=contact_id))
        self.assertEqual(contacts_repo.counts(self.con)["contacts"], 2)


class TestWritingAndSearching(Host):
    def test_write_to_opens_a_composer_addressed_to_them(self):
        contact_id = self.person(address="lyle@covalent.invalid")
        pane = self.pane()
        pane.contacts.open()
        pane.contacts.action("write", contact_id=contact_id)
        composers = pane.actions.composers
        self.assertEqual(len(composers), 1)
        self.assertEqual(composers[0].to.text(), "lyle@covalent.invalid")

    def test_write_to_somebody_with_no_address_says_so(self):
        contact_id = self.person(address="")
        pane = self.pane()
        pane.contacts.open()
        pane.contacts.action("write", contact_id=contact_id)
        self.assertEqual(pane.actions.composers, [])
        self.assertTrue(any("no email address" in s for s in self.said))

    def test_do_not_contact_is_said_and_not_refused(self):
        """`store/contacts.py`'s own rule about the bounce guard: a client that
        will not send is one the user works around by pasting the address
        somewhere else. So it warns and opens the composer anyway."""
        contact_id = self.person(address="lyle@covalent.invalid",
                                 status=contacts_repo.CONTACT_DO_NOT_CONTACT)
        pane = self.pane()
        pane.contacts.open()
        pane.contacts.action("write", contact_id=contact_id)
        self.assertEqual(len(pane.actions.composers), 1)
        self.assertTrue(any("DO NOT CONTACT" in s for s in self.said))

    def test_their_mail_puts_a_search_in_the_pane_and_leaves_the_book(self):
        contact_id = self.person(address="lyle@covalent.invalid")
        pane = self.pane()
        pane.contacts.open()
        pane.contacts.action("mail", contact_id=contact_id)
        self.assertFalse(pane.contacts.showing)
        self.assertEqual(pane.model.search.sender, "lyle@covalent.invalid")

    def test_a_second_address_is_named_rather_than_silently_dropped(self):
        """`search.Query` has ONE sender field, so a person with three
        addresses cannot be asked for in one query. Saying which was used is
        honest; silently searching a third of their mail is not."""
        contact_id = self.person(address="lyle@covalent.invalid")
        contacts_repo.add_handle(self.con, contact_id,
                                 contacts_repo.KIND_EMAIL, "l.g@x.invalid")
        pane = self.pane()
        pane.contacts.open()
        pane.contacts.action("mail", contact_id=contact_id)
        self.assertTrue(any("1 other address" in s for s in self.said),
                        self.said)


class TestFromAMessage(Host):
    def test_it_makes_a_card_with_the_senders_address_on_it(self):
        message_id = self.message(frm="Tom Whitfield <tom@northgate.invalid>")
        pane = self.pane(contact=lambda **kw: Values(
            name="Tom Whitfield", org="Northgate Print", role="",
            status="active", notes=""))
        made = pane.contacts.add_from_message(message_id)
        self.assertTrue(made)
        contact = contacts_repo.get_contact(self.con, made)
        self.assertEqual(contact.name, "Tom Whitfield")
        self.assertEqual([h.value for h in contact.handles],
                         ["tom@northgate.invalid"])

    def test_the_dialog_is_filled_in_from_the_sender(self):
        """One keystroke at the moment somebody decides a correspondent
        matters, which is while they are reading them — `ui/threaddialog.py`
        makes the same argument about a blank New Thread dialog."""
        from cormani.ui.contactdialog import ContactDialog

        message_id = self.message(frm="Tom Whitfield <tom@northgate.invalid>")
        row = self.con.execute(
            "SELECT from_name, from_addr FROM message WHERE id = ?",
            (message_id,)).fetchone()
        dialog = support.own(self, ContactDialog.from_message(None, row))
        self.assertEqual(dialog.values()["name"], "Tom Whitfield")

    def test_somebody_already_in_the_book_is_SHOWN_not_added_twice(self):
        """`handle` is UNIQUE on (kind, value) so a duplicate is impossible
        anyway — but adding one silently and landing on an unchanged book
        would look like a command that did nothing."""
        contact_id = self.person(name="Tom", address="tom@northgate.invalid")
        message_id = self.message(frm="Tom <tom@northgate.invalid>")
        opened = []
        pane = self.pane(contact=lambda **kw: opened.append(kw) or Values())
        self.assertEqual(pane.contacts.add_from_message(message_id),
                         contact_id)
        self.assertEqual(opened, [])
        self.assertEqual(contacts_repo.counts(self.con)["contacts"], 1)
        self.assertTrue(pane.contacts.showing)
        self.assertTrue(any("already" in s for s in self.said))

    def test_a_message_with_no_sender_says_so(self):
        message_id = self.message(frm="Nobody <>")
        pane = self.pane()
        self.assertEqual(pane.contacts.add_from_message(message_id), 0)
        self.assertTrue(any("no sender" in s for s in self.said), self.said)

    def test_the_card_and_its_handle_go_in_together(self):
        """One transaction. A card written without its address is a contact
        the composer can never offer, and nothing in the interface would say
        which half failed."""
        message_id = self.message(frm="Tom <tom@northgate.invalid>")
        pane = self.pane(contact=lambda **kw: Values(
            name="Tom", org="", role="", status="active", notes=""))
        pane.contacts.add_from_message(message_id)
        fresh = support.reopened(self.con)
        self.assertEqual(
            fresh.execute("SELECT COUNT(*) FROM handle").fetchone()[0], 1)


class TestSuggestions(Host):
    def test_ticked_people_become_contacts(self):
        self.message(frm="Tom <tom@northgate.invalid>")
        self.message(frm="Priya <priya@idlidu.invalid>")
        offered = {}

        def suggest(**kw):
            offered.update(kw)
            return Values(people=[{"address": "tom@northgate.invalid",
                                   "name": "Tom"}])

        pane = self.pane(suggest=suggest)
        pane.contacts.open()
        self.assertTrue(pane.contacts.action("suggest"))
        self.assertEqual(len(offered["strangers"]), 2)
        self.assertEqual([c.name for c in contacts_repo.list_contacts(self.con)],
                         ["Tom"])

    def test_ticking_nobody_writes_nothing_and_says_so(self):
        self.message()
        pane = self.pane(suggest=lambda **kw: Values(people=[]))
        pane.contacts.open()
        self.assertFalse(pane.contacts.action("suggest"))
        self.assertEqual(contacts_repo.counts(self.con)["contacts"], 0)
        self.assertIn("Nobody was ticked.", self.said)

    def test_with_no_mail_at_all_it_says_which_silence_this_is(self):
        """"No suggestions" over an empty mailbox and over a complete address
        book are different facts, and a dialog whose only content is "nothing
        to show" should have been a sentence."""
        opened = []
        pane = self.pane(suggest=lambda **kw: opened.append(kw) or Values())
        pane.contacts.open()
        self.assertFalse(pane.contacts.action("suggest"))
        self.assertEqual(opened, [])
        self.assertTrue(any("no mail to take names from" in s
                            for s in self.said), self.said)

    def test_with_everybody_already_known_it_says_the_other_thing(self):
        self.person(name="Tom", address="tom@northgate.invalid")
        self.message(frm="Tom <tom@northgate.invalid>")
        pane = self.pane(suggest=lambda **kw: Values())
        pane.contacts.open()
        self.assertFalse(pane.contacts.action("suggest"))
        self.assertTrue(any("already in the address book" in s
                            for s in self.said), self.said)

    def test_they_are_written_in_one_transaction(self):
        for n in range(3):
            self.message(frm=f"P{n} <p{n}@x.invalid>")
        pane = self.pane(suggest=lambda **kw: Values(people=[
            {"address": f"p{n}@x.invalid", "name": f"P{n}"} for n in range(3)]))
        pane.contacts.open()
        pane.contacts.action("suggest")
        fresh = support.reopened(self.con)
        self.assertEqual(
            fresh.execute("SELECT COUNT(*) FROM contact").fetchone()[0], 3)
        self.assertEqual(
            fresh.execute("SELECT COUNT(*) FROM handle").fetchone()[0], 3)


class TestNobodySelected(Host):
    def test_every_command_that_needs_one_says_so_rather_than_doing_nothing(self):
        """`ui/filterhost.py`'s rule, and the reason this is a loop rather than
        one case: a guard added for `delete` and forgotten for `merge` is a
        button that silently does nothing, and only the one that was tested
        would be right."""
        pane = self.pane()
        pane.contacts.open()
        for name in ("edit", "add-handle", "remove-handle", "merge", "delete",
                     "write", "mail"):
            self.said.clear()
            self.assertFalse(pane.contacts.action(name), name)
            self.assertEqual(self.said, ["Nobody is selected."], name)


class TestTabState(Host):
    def test_a_tab_remembers_the_address_book_and_who_was_open(self):
        contact_id = self.person()
        pane = self.pane()
        pane.contacts.open(contact_id)
        state = pane.view_state("x")
        self.assertTrue(state.is_contacts)
        self.assertEqual(state.contact_id, contact_id)

    def test_a_mail_tab_is_not_the_address_book(self):
        """None means "this tab is not the address book" and 0 means "it is,
        and nobody is chosen" — the convention `calendar_id` established. A
        state that reported 0 for a mail tab would restore every tab into the
        address book."""
        pane = self.pane()
        self.assertIsNone(pane.view_state("x").contact_id)
        pane.contacts.open()
        self.assertEqual(pane.view_state("x").contact_id, 0)

    def test_restoring_puts_the_pane_back_without_emitting(self):
        """`_restoring` is what stops the window writing this tab's state onto
        the CURRENT tab on the way past — `ui/panestate.py`'s header."""
        contact_id = self.person()
        pane = self.pane()
        pane.contacts.open(contact_id)
        state = pane.view_state("x")
        pane.contacts.show(False)
        emitted = []
        pane.view_changed.connect(lambda: emitted.append(1))
        pane.restore(state)
        self.assertTrue(pane.contacts.showing)
        self.assertEqual(pane.contacts.contacts.contact_id(), contact_id)
        self.assertEqual(emitted, [])

    def test_the_tab_is_named_for_the_address_book(self):
        self.person()
        pane = self.pane()
        pane.contacts.open()
        self.assertEqual(pane.title_for_scope(), "Address book (1)")


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
