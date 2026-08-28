# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing a person down, giving them another way of being reached, choosing
# which of two cards to keep, and taking a page of people out of the mailbox.
#
# `ui/threaddialog.py`'s shape and for its reasons: four dialogs in one file
# because they are four views of one object, each returns `values()` — a plain
# dict of exactly what the store takes — and none of them writes anything.
# `ui/contacthost.py` decides what a command means, which is also what lets the
# suite drive these without a click, since Debian ships no QTest.
#
# ── A HANDLE'S KIND IS EDITABLE FREE TEXT, NOT A CHOICE ────────────────────
#
# `store/contacts.SEED_KINDS` is a SEED and the schema says so: `handle.kind`
# is free text "so that adding a channel is data rather than a migration". A
# closed combo box here would quietly make it an enumeration again, and the
# first person wanting to record a Mastodon address would find the application
# had an opinion about it. So the box is editable and the seeds are offered.
#
# ── THE ITEM DATA IS A STRING, ALWAYS ──────────────────────────────────────
#
# `QComboBox.findData` matches through Qt's variant comparison and falls back
# to IDENTITY for an arbitrary Python object, so a tuple built to look one up
# never finds the equal tuple that was stored — `setCurrentIndex(-1)`, and the
# box silently keeps what it had. `ui/ruleeditor.py` shipped within a minute of
# that defect and SESSION_STATE carries the note. Every `setItemData` here is a
# string or an int.
#
# ── THE MERGE DIALOG NAMES WHAT WOULD BE LOST ──────────────────────────────
#
# `contacts.merge_contacts` moves handles, fills the kept card's EMPTY fields
# from the other, joins the notes and DELETES a row. It is not undoable —
# `store/undo.py` takes back one action on one message — so the question says
# which card survives, how many handles would move, and which fields differ.
# A confirmation that only asks "merge these two?" is one people answer without
# reading.
#
# ── AND "ADD FROM MAIL" IS A LIST OF TICKS, NOT A WIZARD ───────────────────
#
# `store/addressbook.suggest` ranks the strangers by how much mail there is and
# puts the people already written to first. The dialog's whole job is to let
# somebody run down that list saying yes; anything that asked a question per
# person would be a dialog nobody reaches the end of.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QFormLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QVBoxLayout)

from ..store import addressbook as book_repo
from ..store import contacts as contacts_repo
from ..store import times

# What the status box offers, and the words rather than the stored keys. The
# schema's values are `active`, `left-org` and `do-not-contact`, and a box
# reading "left-org" is a box that shows its own database.
_STATUSES = ((contacts_repo.CONTACT_ACTIVE, "Active"),
             (contacts_repo.CONTACT_LEFT_ORG, "Has left their organisation"),
             (contacts_repo.CONTACT_DO_NOT_CONTACT, "Do not contact"))


class ContactDialog(QDialog):
    """New contact, or edit one."""

    def __init__(self, parent=None, *, contact=None, name: str = "",
                 org: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit contact" if contact else "New contact")
        self._contact = contact

        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)

        self.name = QLineEdit(contact.name if contact else name, self)
        self.name.setPlaceholderText("How you would refer to them")
        form.addRow("Name", self.name)

        self.org = QLineEdit(contact.org if contact else org, self)
        form.addRow("Organisation", self.org)

        self.role = QLineEdit(contact.role if contact else "", self)
        self.role.setPlaceholderText("What they do there")
        form.addRow("Role", self.role)

        self.status = QComboBox(self)
        for value, label in _STATUSES:
            self.status.addItem(label, value)
        self._select(self.status, contact.status if contact
                     else contacts_repo.CONTACT_ACTIVE)
        form.addRow("Standing", self.status)

        self.notes = QPlainTextEdit(contact.notes if contact else "", self)
        self.notes.setPlaceholderText("Anything worth remembering")
        form.addRow("Notes", self.notes)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.name.setFocus()

    @staticmethod
    def _select(box: QComboBox, value: str) -> None:
        """Choose by DATA, and keep a value the box does not offer.

        `status` is a text column with no constraint, so a store written by a
        future version — or by hand — can hold one of these boxes has never
        heard of. Falling back to index 0 would silently turn "do not contact"
        into "active" by opening the dialog, which is the one direction that
        change must never happen in.
        """
        index = box.findData(str(value))
        if index < 0:
            box.addItem(str(value), str(value))
            index = box.count() - 1
        box.setCurrentIndex(index)

    @classmethod
    def from_message(cls, parent, row) -> "ContactDialog":
        """A card filled in from the message being read.

        The NAME is what the sender called themselves, which is the one field
        a mailbox actually knows. `store/contacts.contact_for_address` falls
        back to the local part when there is none, and the same fallback is
        used here so that the dialog and the store agree about who this is.
        """
        address = (row["from_addr"] or "").strip()
        return cls(parent, name=(row["from_name"] or "").strip()
                   or address.split("@")[0])

    def values(self) -> dict:
        return {"name": self.name.text().strip(),
                "org": self.org.text().strip(),
                "role": self.role.text().strip(),
                "status": str(self.status.currentData()),
                "notes": self.notes.toPlainText().strip()}


class HandleDialog(QDialog):
    """One more way of reaching somebody."""

    def __init__(self, parent=None, *, kind: str = contacts_repo.KIND_EMAIL,
                 value: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a handle")

        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)

        self.kind = QComboBox(self)
        self.kind.setEditable(True)           # free text over a seed list
        for seed in contacts_repo.SEED_KINDS:
            self.kind.addItem(seed)
        self.kind.setCurrentText(kind)
        form.addRow("Kind", self.kind)

        self.value = QLineEdit(value, self)
        self.value.setPlaceholderText("An address, a number, a profile")
        form.addRow("Value", self.value)

        self.verified = QCheckBox("I know this one works", self)
        self.verified.setToolTip(
            "Marks it verified rather than unverified. A handle that has "
            "bounced is marked by the delivery report and not from here")
        form.addRow("", self.verified)

        note = QLabel("One address belongs to one person. Typing an address "
                      "that is on somebody else's card MOVES it here.", self)
        note.setWordWrap(True)
        outer.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.value.setFocus()

    def values(self) -> dict:
        return {"kind": self.kind.currentText().strip()
                or contacts_repo.KIND_EMAIL,
                "value": self.value.text().strip(),
                "status": contacts_repo.STATUS_VERIFIED
                if self.verified.isChecked()
                else contacts_repo.STATUS_UNVERIFIED}


class MergeDialog(QDialog):
    """Which of two cards to keep, and what merging them would do.

    The other card is CHOSEN here rather than being the second thing selected
    in the list, because a list can hold one selection at a time and a merge
    needs two — and a mode where the next click means something different is
    the shape people mis-click.
    """

    def __init__(self, parent=None, *, keep=None, others=(),
                 suggested_id: int | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Merge contacts")
        self._keep = keep

        outer = QVBoxLayout(self)
        heading = QLabel(
            f"“{keep.label}” will be KEPT. Choose the card to merge into it "
            f"— that one is deleted, and every handle on it moves across."
            if keep is not None else "Choose a card to merge in.", self)
        heading.setWordWrap(True)
        outer.addWidget(heading)

        form = QFormLayout()
        outer.addLayout(form)
        self.other = QComboBox(self)
        for contact in others:
            # An int, not the Contact. `findData` compares through Qt's own
            # variant comparison — see the header — and an int is a value it
            # knows how to compare.
            self.other.addItem(self._describe(contact), int(contact.id))
        if suggested_id is not None:
            index = self.other.findData(int(suggested_id))
            if index >= 0:
                self.other.setCurrentIndex(index)
        form.addRow("Merge in", self.other)

        self.effect = QLabel("", self)
        self.effect.setWordWrap(True)
        outer.addWidget(self.effect)
        self._others = {int(c.id): c for c in others}
        self.other.currentIndexChanged.connect(lambda _i: self._describe_effect())
        self._describe_effect()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _describe(contact) -> str:
        parts = [contact.label]
        if contact.org:
            parts.append(contact.org)
        if contact.handles:
            parts.append(f"{len(contact.handles)} handle"
                         f"{'' if len(contact.handles) == 1 else 's'}")
        return " · ".join(parts)

    def _describe_effect(self) -> None:
        """What this particular merge would do, in the numbers a person can
        check afterwards. A confirmation that says only "merge these two?" is
        one people answer without reading."""
        other = self._others.get(self.other.currentData())
        if other is None or self._keep is None:
            self.effect.setText("")
            return
        moving = len(other.handles)
        filled = [name for name in ("name", "org", "role")
                  if not getattr(self._keep, name).strip()
                  and getattr(other, name).strip()]
        lines = [f"{moving} handle{'' if moving == 1 else 's'} would move to "
                 f"“{self._keep.label}”."]
        if filled:
            lines.append("It would also take their "
                         + ", ".join(filled) + ".")
        if other.notes.strip():
            lines.append("Their notes would be added below yours.")
        lines.append("This cannot be undone.")
        self.effect.setText(" ".join(lines))

    def values(self) -> dict:
        return {"drop_id": self.other.currentData()}


class SuggestDialog(QDialog):
    """The people in the mailbox who are not in the book yet, to tick.

    NOTHING IS TICKED WHEN IT OPENS, and that is the decision. Pre-ticking the
    top ten would make the safe click — OK — the one that writes ten cards, and
    an address book somebody did not mean to make is exactly what
    `contacts.contact_for_address` refuses to do by itself.
    """

    def __init__(self, parent=None, *, strangers=()) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add people from your mail")

        outer = QVBoxLayout(self)
        heading = QLabel(
            "People you have mail from who are not in the address book. Those "
            "you have written to come first; automated senders are left out.",
            self)
        heading.setWordWrap(True)
        outer.addWidget(heading)

        self.people = QListWidget(self)
        self.people.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        for stranger in strangers:
            item = QListWidgetItem(self._describe(stranger), self.people)
            item.setData(Qt.ItemDataRole.UserRole, stranger.address)
            item.setData(Qt.ItemDataRole.UserRole + 1, stranger.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.people.addItem(item)
        outer.addWidget(self.people, 1)

        if not strangers:
            empty = QLabel("Everybody you have mail from is already in the "
                           "book.", self)
            empty.setWordWrap(True)
            outer.addWidget(empty)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _describe(stranger) -> str:
        when = times.to_local(stranger.last_at)
        stamp = when.strftime("%d %b %Y") if when else "?"
        return (f"{stranger.label}   —   {stranger.messages} message"
                f"{'' if stranger.messages == 1 else 's'}, last {stamp}")

    def check_all(self, on: bool = True) -> None:
        """Tick or clear every row. Public because the host's test needs it and
        because a list of twenty-five with no Select all is a dialog people
        close."""
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for row in range(self.people.count()):
            self.people.item(row).setCheckState(state)

    def values(self) -> dict:
        """The ticked ones, as the store takes them.

        THE CHECK STATE AND NOT THE SELECTION. They are two different things in
        a QListWidget and the difference is invisible until somebody clicks a
        row without ticking it — which is what everybody does first.
        """
        chosen = []
        for row in range(self.people.count()):
            item = self.people.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                chosen.append({
                    "address": str(item.data(Qt.ItemDataRole.UserRole)),
                    "name": str(item.data(Qt.ItemDataRole.UserRole + 1) or "")})
        return {"people": chosen}


def strangers_for(con, *, limit: int = book_repo.SUGGEST_LIMIT) -> list:
    """What `SuggestDialog` is built from. Here rather than in the dialog so
    that the host can count them before deciding whether to open one at all —
    a dialog whose only content is "there is nothing to show" is a dialog that
    should have been a sentence in the status bar."""
    return book_repo.suggest(con, limit=limit)
