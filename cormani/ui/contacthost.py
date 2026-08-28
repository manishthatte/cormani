# SPDX-License-Identifier: GPL-3.0-or-later
#
# The address book pane, and what every one of its commands MEANS.
#
# `ui/trackhost.py`'s shape and its reasons: holding a pane is not the same job
# as arranging widgets, and deciding what a command means is a third job again.
# The pane emits names; this is the only place that turns a name into a write.
#
# ── THE DIALOGS ARE INJECTED ───────────────────────────────────────────────
#
# Debian ships no QTest, so a modal dialog cannot be driven from a test. The
# host takes its dialogs as parameters, exactly as `ui/trackhost.py` does, and
# the suite hands it objects that answer `values()` with what a person would
# have typed. Every test then STARTS at a signal and ENDS at the store.
#
# ── DELETING ASKS AND MERGING ASKS HARDER ──────────────────────────────────
#
# Neither is undoable — `store/undo.py` takes back one action on one message —
# and they lose different things. Deleting a contact loses the card and leaves
# the mail; merging loses a card AND moves handles, and `contacts.merge_contacts`
# fills empty fields from the other, which means the direction matters. So the
# delete question names what goes, and the merge dialog names the numbers.
#
# ── "THEIR MAIL" IS A SEARCH AND NOT A FOLDER ──────────────────────────────
#
# The useful answer to "show me everything from this person" is the mail pane
# running `from:` on their address — the same object `ui/searchbar.py` builds,
# so the chips appear, the tab is named for the search and Save this search
# works over it. A pane of its own would be a second message list with its own
# idea of sorting, threading and what has been read.
#
# THE SEARCH USES ONE ADDRESS AND SAYS SO WHEN THERE ARE MORE. `search.Query`
# has one `sender` field, and a person with three addresses cannot be asked for
# in one query without inventing an OR the language does not have. Saying which
# address was used is honest; silently searching for a third of their mail is
# not.
#
# ── AND A SUGGESTION IS ACCEPTED IN ONE TRANSACTION ────────────────────────
#
# Twenty ticked people are twenty contacts and twenty handles, and a failure
# halfway through would leave an address book somebody cannot tell is half
# written. `contact_for_address` takes `commit`, so they go in together.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from ..store import addressbook as book_repo
from ..store import contacts as contacts_repo
from ..store import search as search_mod
from . import panespace
from .contactpane import ContactPane


class ContactHost:
    """The address book pane, and every command it can ask for."""

    def __init__(self, pane, *, dialogs=None) -> None:
        self.pane = pane
        self._dialogs = dialogs or {}
        con = pane._con

        self.contacts = ContactPane(con, pane)
        self.contacts.setVisible(False)
        self.contacts.status_message.connect(pane.status_message)
        self.contacts.action.connect(self.action)
        self.contacts.message_activated.connect(self._open_message)
        self.contacts.thread_activated.connect(self._open_thread)
        self.contacts.contact_chosen.connect(
            lambda _id: pane.view_changed.emit())
        pane.splitter.addWidget(self.contacts)

        self.showing = False

    # ------------------------------------------------------------- swapping
    def show(self, on: bool) -> None:
        """Swap the middle and the reader for the address book, or back.

        The other claimants are asked to stand down first, for the reason
        `ui/trackhost.show` gives: two panes both claiming the space the list
        and the reader occupy is a window with one drawn over the other, and it
        reads as a rendering fault rather than as a mistake. WHICH others is
        `ui/panespace.py`'s to know — this method knew all three when it was
        written, which is exactly what the other three hosts could each have
        said about their own lists.
        """
        on = bool(on)
        if on:
            panespace.claim(self.pane, "contacts")
        self.showing = on
        for widget in (self.pane.middle, self.pane.reader):
            widget.setVisible(not on)
        self.contacts.setVisible(on)
        if on:
            self.contacts.reload()

    def open(self, contact_id: int | None = None) -> None:
        """Show the address book, at somebody if one was named."""
        self.show(True)
        if contact_id:
            self.contacts.show_contact(int(contact_id))

    def open_by_address(self, address: str) -> str | None:
        """Open a contact from an address. Returns a status message if absent."""
        contact = contacts_repo.contact_for_address(self.pane._con, address)
        if contact is None:
            return f"{address} is not in the address book."
        self.open(contact.id)
        return None

    def title(self) -> str:
        return self.contacts.title()

    # ------------------------------------------------------------- commands
    def action(self, name: str, *, contact_id: int | None = None) -> bool:
        """What a button means. Returns whether anything was written.

        `new` and `suggest` come FIRST because they are the two that do not
        need a contact — an address book with nobody in it is exactly when they
        are wanted, and a guard demanding a selection would make an empty book
        impossible to fill.
        """
        if name == "open":
            self.open(contact_id)
            return False
        if name == "new":
            return self._new()
        if name == "suggest":
            return self._suggest()
        if name == "import-vcard":
            return self._import_vcard()
        if name == "export-vcard":
            return self._export_vcard()
        if name == "merge-suggested":
            return self._merge_suggested()

        contact_id = contact_id or self.contacts.contact_id()
        if not contact_id:
            self.pane.status_message.emit("Nobody is selected.")
            return False
        handler = {"edit": self._edit, "add-handle": self._add_handle,
                   "remove-handle": self._remove_handle, "merge": self._merge,
                   "delete": self._delete, "write": self._write,
                   "mail": self._their_mail}.get(name)
        if handler is None:
            return False
        wrote = handler(int(contact_id))
        if wrote:
            self.refresh()
        return wrote

    def _new(self) -> bool:
        dialog = self._dialog("contact")
        if dialog is None or not self._run(dialog):
            return False
        values = dialog.values()
        if not any(v.strip() for v in (values["name"], values["org"],
                                       values["role"], values["notes"])):
            # A card with nothing on it is not a person; it is a row somebody
            # will find later and not recognise. Said rather than written.
            self.pane.status_message.emit(
                "A contact needs at least a name — nothing was added.")
            return False
        contact_id = contacts_repo.add_contact(self.pane._con, **values)
        self.refresh()
        self.contacts.show_contact(contact_id)
        self.pane.status_message.emit(
            f"Added {values['name'] or 'a contact'} — Add a handle gives them "
            f"an address.")
        return True

    def _edit(self, contact_id: int) -> bool:
        contact = contacts_repo.get_contact(self.pane._con, contact_id)
        if contact is None:
            return False
        dialog = self._dialog("contact", contact=contact)
        if dialog is None or not self._run(dialog):
            return False
        contacts_repo.update_contact(self.pane._con, contact_id,
                                     **dialog.values())
        return True

    def _add_handle(self, contact_id: int) -> bool:
        dialog = self._dialog("handle")
        if dialog is None or not self._run(dialog):
            return False
        values = dialog.values()
        # STRIPPED HERE AND NOT TRUSTED TO THE DIALOG. `contacts.add_handle`
        # RAISES on an empty value, and an exception in a Qt slot does not
        # reach the caller — it is printed and the program carries on — so a
        # dialog that handed over "  " would be a button that silently did
        # nothing. The host is the boundary; the real dialog stripping as well
        # is belt and braces rather than the guarantee.
        value = (values.get("value") or "").strip()
        if not value:
            self.pane.status_message.emit("A handle needs a value.")
            return False
        # WHOSE IT WAS, asked BEFORE it moves. `contacts.add_handle` MOVES a
        # handle that belongs to somebody else — deliberately, because an
        # address has one owner — and a move nobody was told about is a card
        # that quietly lost its address.
        owner = self._owner_of(values["kind"], value)
        contacts_repo.add_handle(self.pane._con, contact_id, values["kind"],
                                 value, status=values["status"])
        if owner is not None and owner.id != contact_id:
            self.pane.status_message.emit(
                f"{value} moved here from “{owner.label}” — one address "
                f"belongs to one person.")
        else:
            self.pane.status_message.emit(f"Added {value}.")
        return True

    def _owner_of(self, kind: str, value: str):
        row = self.pane._con.execute(
            "SELECT contact_id FROM handle WHERE kind = ? AND LOWER(value) = ?",
            (kind, (value or "").strip().lower())).fetchone()
        if row is None:
            return None
        return contacts_repo.get_contact(self.pane._con, int(row["contact_id"]))

    def _remove_handle(self, contact_id: int) -> bool:
        handle_id = self.contacts.selected_handle()
        if not handle_id:
            self.pane.status_message.emit(
                "Choose a handle in the card first.")
            return False
        contact = contacts_repo.get_contact(self.pane._con, contact_id)
        gone = next((h for h in (contact.handles if contact else ())
                     if h.id == handle_id), None)
        if gone is None:
            return False
        if not self._confirm("Remove this handle?",
                             f"“{gone.value}” would no longer be one of "
                             f"{contact.label}'s. The mail stays where it is; "
                             f"only the card changes."):
            return False
        contacts_repo.remove_handle(self.pane._con, handle_id)
        self.pane.status_message.emit(f"Removed {gone.value}.")
        return True

    def _merge(self, contact_id: int) -> bool:
        con = self.pane._con
        keep = contacts_repo.get_contact(con, contact_id)
        if keep is None:
            return False
        others = [c for c in contacts_repo.list_contacts(con)
                  if c.id != contact_id]
        if not others:
            self.pane.status_message.emit(
                "There is nobody else to merge in.")
            return False
        dialog = self._dialog("merge", keep=keep, others=others,
                              suggested_id=self._suggested_pair(contact_id))
        if dialog is None or not self._run(dialog):
            return False
        drop_id = dialog.values().get("drop_id")
        if not drop_id or int(drop_id) == contact_id:
            return False
        drop = contacts_repo.get_contact(con, int(drop_id))
        moved = contacts_repo.merge_contacts(con, contact_id, int(drop_id))
        self.pane.status_message.emit(
            f"Merged “{drop.label if drop else drop_id}” into “{keep.label}” — "
            f"{moved} handle{'' if moved == 1 else 's'} moved.")
        # The kept card, explicitly: the merged-away one has gone and a list
        # that reloaded onto whatever took its row would show somebody else.
        self.contacts.show_contact(contact_id)
        return True

    def _suggested_pair(self, contact_id: int) -> int | None:
        """Which card `store/addressbook.duplicates` thinks this one is.

        Offered as the dialog's starting choice and never acted on: the whole
        design of `duplicates` is that it OFFERS pairs, because a wrong merge
        cannot be taken back.
        """
        for pair in book_repo.duplicates(self.pane._con):
            if pair.keep_id == contact_id:
                return pair.drop_id
            if pair.drop_id == contact_id:
                return pair.keep_id
        return None

    def _delete(self, contact_id: int) -> bool:
        contact = contacts_repo.get_contact(self.pane._con, contact_id)
        if contact is None:
            return False
        # WHAT SURVIVES IS NAMED, because it is the half people fear. A thread
        # keeps its timeline — `touch.contact_id` is SET NULL — and the mail is
        # not touched at all.
        if not self._confirm(
                "Delete this contact?",
                f"“{contact.label}” and their "
                f"{len(contact.handles)} handle"
                f"{'' if len(contact.handles) == 1 else 's'} would go.\n\n"
                f"The mail stays, and any tracked thread keeps its timeline. "
                f"This cannot be undone."):
            return False
        contacts_repo.delete_contact(self.pane._con, contact_id)
        self.contacts.show_contact(None)
        self.pane.status_message.emit(f"Deleted “{contact.label}”.")
        return True

    def _write(self, contact_id: int) -> bool:
        """Write to them. Returns False because nothing was WRITTEN to the
        store — the composer is a window, and `action`'s return value means
        "the address book changed" rather than "something happened"."""
        contact = contacts_repo.get_contact(self.pane._con, contact_id)
        if contact is None:
            return False
        if not contact.address:
            self.pane.status_message.emit(
                f"{contact.label} has no email address — Add a handle first.")
            return False
        if contact.status == contacts_repo.CONTACT_DO_NOT_CONTACT:
            # SAID AND NOT REFUSED, which is `contacts.py`'s own rule about the
            # bounce guard: a client that will not send is one the user works
            # around by pasting the address somewhere else.
            self.pane.status_message.emit(
                f"{contact.label} is marked DO NOT CONTACT — opening a message "
                f"anyway.")
        self.pane.compose("new", to=contact.address)
        return False

    def _their_mail(self, contact_id: int) -> bool:
        """Everything from this person, in the mail pane, as a search."""
        contact = contacts_repo.get_contact(self.pane._con, contact_id)
        if contact is None:
            return False
        address = contact.address
        if not address:
            self.pane.status_message.emit(
                f"{contact.label} has no email address to search for.")
            return False
        self.show(False)
        self.pane.set_search(search_mod.Query(sender=address))
        spare = [h.value for h in contact.handles
                 if h.kind == contacts_repo.KIND_EMAIL and h.value != address]
        self.pane.status_message.emit(
            f"Mail from {address}."
            + (f" They have {len(spare)} other address"
               f"{'' if len(spare) == 1 else 'es'}; search names one at a time."
               if spare else ""))
        return False

    def _suggest(self) -> bool:
        """Take a page of people out of the mailbox, having ticked them."""
        con = self.pane._con
        strangers = book_repo.suggest(con)
        if not strangers:
            # A DIALOG WHOSE ONLY CONTENT IS "NOTHING TO SHOW" SHOULD HAVE BEEN
            # A SENTENCE. The three reasons are told apart, because "no
            # suggestions" over an empty mailbox and over a complete address
            # book are different facts.
            held = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            self.pane.status_message.emit(
                "There is no mail to take names from yet."
                if not held else
                "Everybody you have mail from is already in the address book.")
            return False
        dialog = self._dialog("suggest", strangers=strangers)
        if dialog is None or not self._run(dialog):
            return False
        people = dialog.values().get("people") or []
        if not people:
            self.pane.status_message.emit("Nobody was ticked.")
            return False
        made = 0
        last = None
        for person in people:
            contact = contacts_repo.contact_for_address(
                con, person["address"], name=person.get("name", ""),
                create=True, commit=False)
            if contact is not None:
                made += 1
                last = contact.id
        con.commit()
        self.refresh()
        if made == 1 and last:
            self.contacts.show_contact(last)
        self.pane.status_message.emit(
            f"Added {made} contact{'' if made == 1 else 's'} from your mail.")
        return True

    def _import_vcard(self) -> bool:
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from ..importer import vcard as vcard_mod

        path, _ = QFileDialog.getOpenFileName(
            self.pane, "Import vCard", "",
            "vCard files (*.vcf *.vcard);;All files (*)")
        if not path:
            return False
        report = vcard_mod.import_file(self.pane._con, Path(path))
        self.refresh()
        self.pane.status_message.emit(
            f"Imported {report.imported} contact"
            f"{'' if report.imported == 1 else 's'} from {Path(path).name}.")
        return bool(report.imported)

    def _export_vcard(self) -> bool:
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from ..importer import vcard as vcard_mod

        path, _ = QFileDialog.getSaveFileName(
            self.pane, "Export vCard", "contacts.vcf",
            "vCard files (*.vcf);;All files (*)")
        if not path:
            return False
        dest = Path(path)
        if dest.suffix.lower() not in (".vcf", ".vcard"):
            dest = dest.with_suffix(".vcf")
        count = vcard_mod.export_file(self.pane._con, dest)
        self.pane.status_message.emit(
            f"Exported {count} contact{'' if count == 1 else 's'} to "
            f"{dest.name}.")
        return bool(count)

    def _merge_suggested(self) -> bool:
        pairs = book_repo.duplicates(self.pane._con)
        if not pairs:
            self.pane.status_message.emit("No possible duplicates right now.")
            return False
        pair = pairs[0]
        keep = contacts_repo.get_contact(self.pane._con, pair.keep_id)
        others = [contacts_repo.get_contact(self.pane._con, pair.drop_id)]
        if keep is None or others[0] is None:
            return False
        dialog = self._dialog("merge", keep=keep, others=others,
                              suggested_id=pair.drop_id)
        if dialog is None or not self._run(dialog):
            return False
        target = int(dialog.values().get("keep_id") or keep.id)
        drop = int(dialog.values().get("drop_id") or pair.drop_id)
        contacts_repo.merge_contacts(self.pane._con, target, drop)
        self.refresh()
        self.contacts.show_contact(target)
        self.pane.status_message.emit("Merged the two cards.")
        return True

    # -------------------------------------------------------- from elsewhere
    def add_from_message(self, message_id: int | None) -> int:
        """Make a card from the message being read. Returns the id, or 0.

        The commonest way a contact gets made in any client, and it is the one
        that has to need a single keystroke: the moment somebody decides a
        correspondent matters is while they are reading them.

        SOMEBODY ALREADY IN THE BOOK IS SHOWN, NOT DUPLICATED. `handle` is
        UNIQUE on (kind, value), so a second card for the same address is
        impossible anyway — but adding one silently and landing on an unchanged
        book would look like a command that did nothing.
        """
        if not message_id:
            return 0
        row = self.pane._con.execute(
            "SELECT id, from_name, from_addr FROM message WHERE id = ?",
            (int(message_id),)).fetchone()
        if row is None or not (row["from_addr"] or "").strip():
            self.pane.status_message.emit(
                "That message has no sender to add.")
            return 0
        address = row["from_addr"].strip()
        existing = contacts_repo.contact_for_address(self.pane._con, address)
        if existing is not None:
            self.open(existing.id)
            self.pane.status_message.emit(
                f"{address} is already {existing.label}'s.")
            return existing.id

        dialog = self._dialog("contact", row=row)
        if dialog is None or not self._run(dialog):
            return 0
        values = dialog.values()
        contact_id = contacts_repo.add_contact(self.pane._con, commit=False,
                                               **values)
        contacts_repo.add_handle(self.pane._con, contact_id,
                                 contacts_repo.KIND_EMAIL, address,
                                 commit=False)
        self.pane._con.commit()
        self.open(contact_id)
        self.pane.status_message.emit(
            f"Added {values['name'] or address} with {address}.")
        return contact_id

    def _open_message(self, message_id: int) -> None:
        """A row of the card's recent mail opens the message, in a tab of its
        own — `ui/trackhost._open_message`'s reason: the point of clicking is
        to READ the thing, and an address book is not a reading pane."""
        self.pane.open_in_tab.emit(int(message_id))

    def _open_thread(self, thread_id: int) -> None:
        """A tracked thread on the card opens the tracking pane at it. The two
        panes claim the same space, so this is a swap and not a second view."""
        self.pane.tracking.open(int(thread_id))
        self.pane.view_changed.emit()

    # ------------------------------------------------------------- redrawing
    def refresh(self) -> None:
        if self.showing:
            self.contacts.reload()

    def apply_theme(self, theme) -> None:
        self.contacts.set_theme(theme)

    # ------------------------------------------------------------ view state
    def state(self) -> int | None:
        """The contact id for the tab to remember, or None when not showing.

        Zero rather than None while showing with nobody selected — the
        convention `ViewState.calendar_id` established and `thread_id` copied:
        None means "this tab is not the address book", 0 means "it is, and
        nobody is chosen".
        """
        if not self.showing:
            return None
        return self.contacts.contact_id() or 0

    def restore(self, state) -> None:
        self.open(state.contact_id or None)

    # --------------------------------------------------------------- dialogs
    def _dialog(self, name: str, **kwargs):
        maker = self._dialogs.get(name)
        if maker is not None:
            return maker(**kwargs)
        return self._default_dialog(name, **kwargs)

    def _default_dialog(self, name: str, **kwargs):
        from .contactdialog import (ContactDialog, HandleDialog, MergeDialog,
                                    SuggestDialog)

        if name == "contact":
            row = kwargs.get("row")
            if row is not None:
                return ContactDialog.from_message(self.pane, row)
            return ContactDialog(self.pane, contact=kwargs.get("contact"))
        if name == "handle":
            return HandleDialog(self.pane)
        if name == "merge":
            return MergeDialog(self.pane, keep=kwargs.get("keep"),
                               others=kwargs.get("others", ()),
                               suggested_id=kwargs.get("suggested_id"))
        if name == "suggest":
            return SuggestDialog(self.pane,
                                 strangers=kwargs.get("strangers", ()))
        return None                                          # pragma: no cover

    def _run(self, dialog) -> bool:
        runner = self._dialogs.get("run")
        if runner is not None:
            return bool(runner(dialog))
        return bool(dialog.exec())                           # pragma: no cover

    def _confirm(self, title: str, text: str) -> bool:
        """Ask, through the injected asker where there is one.

        Same shape as `_run` and for the same reason: a QMessageBox in a test
        is a test that hangs. The default is the real thing.
        """
        asker = self._dialogs.get("confirm")
        if asker is not None:
            return bool(asker(title, text))
        from PySide6.QtWidgets import QMessageBox               # pragma: no cover

        answer = QMessageBox.question(                         # pragma: no cover
            self.pane, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes        # pragma: no cover
