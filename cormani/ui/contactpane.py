# SPDX-License-Identifier: GPL-3.0-or-later
#
# The address book: everybody on the left, one person on the right.
#
# The last of PLAN.txt §5's stage 8 list to have no surface. `store/contacts.py`
# has held the whole model since stage 6 — contacts, handles across channels,
# merging, the bounce guard — and until now the only things that reached it
# were the composer's guard and the tracking layer's own use. Open item 11.
#
# ── IT IS A TAB, LIKE TRACKING, AND FOR THE SAME REASON ────────────────────
#
# `ui/trackpane.py` argued it and nothing has changed: the rail already carries
# fifteen accounts, their folders, the calendars, stage 7's sites and stage 8's
# saved searches. The address book opens where the list and the reading pane
# are, and the cost — that it is not visible until somebody looks — is smaller
# here than it was for tracking, because an address book has no counts anybody
# needs to watch. There is nothing to badge.
#
# ── THE SEARCH BOX ASKS THE STORE, NOT THE LIST ────────────────────────────
#
# `contacts.list_contacts` searches the person AND their handles, because the
# thing a person remembers is often the address rather than the name — "who was
# it at northgate" is the question, and a filter over the drawn NAMES could
# never answer it. So typing re-queries. It is bounded by `limit` and by the
# fact that an address book is a number a person accumulates rather than one a
# server sends.
#
# ── THE LIST DRAWS NO COUNTS, DELIBERATELY ─────────────────────────────────
#
# `store/addressbook.py`'s header has the measurement: correspondence is an
# unindexed scan and is affordable once, when a card opens. A "messages" column
# beside every name is that scan per row and per redraw. The saved-search work
# made exactly this mistake's cheaper cousin visible — the rail counting three
# queries it did not need to — and the answer there was a cap. Here the answer
# is not to ask.
#
# ── NOTHING HERE WRITES ────────────────────────────────────────────────────
#
# Every button emits a name; `ui/contacthost.py` decides what it means. The
# suite has no QTest and cannot click, so a test starts at a signal and ends at
# the store — the rule stage 3 and stage 4 each paid a feature to learn.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton,
                               QSplitter, QVBoxLayout, QWidget)

from ..store import addressbook as book_repo
from ..store import contacts as contacts_repo
from .contactcard import ContactCard

# The chooser above the list. `status` is free text in the schema and these
# three are what `store/contacts.py` names; "everybody" is first because it is
# what an address book is for.
_SCOPES = (("", "Everybody"),
           (contacts_repo.CONTACT_ACTIVE, "Active"),
           (contacts_repo.CONTACT_LEFT_ORG, "Left their organisation"),
           (contacts_repo.CONTACT_DO_NOT_CONTACT, "Do not contact"))


class ContactList(QListWidget):
    """Everybody, narrowed by what has been typed."""

    chosen = Signal(int)

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._theme = None
        self._query = ""
        self._status = ""
        self.setMinimumWidth(230)
        self.currentItemChanged.connect(self._changed)

    def set_theme(self, theme) -> None:
        self._theme = theme
        self.reload()

    def set_query(self, text: str) -> None:
        self._query = (text or "").strip()
        self.reload()

    def set_status(self, status: str) -> None:
        self._status = status or ""
        self.reload()

    def contacts(self) -> list:
        return contacts_repo.list_contacts(self._con, query=self._query,
                                           status=self._status)

    def reload(self) -> None:
        """Draw the list. Keeps the selection on the same PERSON and not the
        same row — `ui/trackpane.ThreadBoard` argues it: a reload after a
        rename re-orders, and a list that kept the index would move somebody
        to a different card for no reason."""
        wanted = self.current_contact_id()
        self.blockSignals(True)
        self.clear()
        for contact in self.contacts():
            item = QListWidgetItem(self._label(contact), self)
            item.setData(Qt.ItemDataRole.UserRole, contact.id)
            item.setToolTip(self._tooltip(contact))
            self._paint(item, contact)
            self.addItem(item)
            if contact.id == wanted:
                self.setCurrentItem(item)
        self.blockSignals(False)
        if wanted and self.current_contact_id() != wanted:
            # The person being shown has gone — deleted, merged away, or
            # narrowed out by the search box. Say so, so the card stops
            # showing somebody the list no longer holds.
            self.chosen.emit(self.current_contact_id() or 0)

    def _label(self, contact) -> str:
        """The name, and the organisation when there is one.

        `Contact.label` is never blank and its docstring says why: a card made
        from a message may have no name at all, and a row with an empty title
        is a defect to look at rather than a person to recognise.
        """
        return f"{contact.label}    {contact.org}" if contact.org \
            else contact.label

    def _tooltip(self, contact) -> str:
        lines = [contact.label]
        for field in (contact.org, contact.role):
            if field:
                lines.append(field)
        for handle in contact.handles:
            lines.append(handle.label
                         + ("  — has bounced" if handle.is_bounced else ""))
        if not contact.handles:
            lines.append("no way of reaching them yet")
        return "\n".join(lines)

    def _paint(self, item: QListWidgetItem, contact) -> None:
        """Colour says which rows cannot be written to, and there are two
        kinds: somebody the user has said not to contact, and somebody whose
        only address has refused mail. Both are drawn in the error colour
        because both end the same way — a message that does not arrive."""
        if self._theme is None:
            return
        if contact.status == contacts_repo.CONTACT_DO_NOT_CONTACT:
            item.setForeground(QColor(self._theme.error))
        elif not contact.reachable:
            item.setForeground(QColor(self._theme.text_muted))
        elif any(h.is_bounced for h in contact.handles):
            item.setForeground(QColor(self._theme.error))
        else:
            item.setForeground(QColor(self._theme.text))

    def current_contact_id(self) -> int | None:
        item = self.currentItem()
        return None if item is None else int(item.data(Qt.ItemDataRole.UserRole))

    def select(self, contact_id: int) -> bool:
        for row in range(self.count()):
            item = self.item(row)
            if int(item.data(Qt.ItemDataRole.UserRole)) == int(contact_id):
                self.setCurrentItem(item)
                return True
        return False

    def _changed(self, current, _previous) -> None:
        if current is not None:
            self.chosen.emit(int(current.data(Qt.ItemDataRole.UserRole)))


class ContactPane(QWidget):
    """The list and one card, side by side, over a footer that counts."""

    action = Signal(str)
    contact_chosen = Signal(int)
    message_activated = Signal(int)
    thread_activated = Signal(int)
    status_message = Signal(str)

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)

        left = QWidget(self.splitter)
        column = QVBoxLayout(left)
        column.setContentsMargins(8, 8, 4, 0)
        column.setSpacing(4)

        self.search = QLineEdit(left)
        self.search.setPlaceholderText("Find a name, an organisation or an "
                                       "address")
        self.search.setClearButtonEnabled(True)
        # RE-QUERIED ON EVERY KEYSTROKE, unlike the message search box, and the
        # difference is the size of what is being asked. `ui/searchbar.py`
        # defers because a full-text query over fifteen accounts on every
        # letter is a client that stutters; this is a LIKE over a table with
        # hundreds of rows in it, and deferring would only make it feel slow.
        self.search.textChanged.connect(self._search_changed)
        column.addWidget(self.search)

        self.scope = QComboBox(left)
        for status, label in _SCOPES:
            self.scope.addItem(label, status)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        column.addWidget(self.scope)

        self.list = ContactList(con, left)
        column.addWidget(self.list, 1)

        buttons = QWidget(left)
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 4, 0, 4)
        self.new_button = QPushButton("New", buttons)
        self.new_button.clicked.connect(lambda *_: self.action.emit("new"))
        row.addWidget(self.new_button)
        self.import_button = QPushButton("Add from mail…", buttons)
        self.import_button.setToolTip(
            "The people you correspond with who are not in the book yet, "
            "most mail first")
        self.import_button.clicked.connect(lambda *_: self.action.emit("suggest"))
        row.addWidget(self.import_button)
        row.addStretch(1)
        column.addWidget(buttons)

        self.card = ContactCard(con, self.splitter)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.card)
        self.splitter.setStretchFactor(1, 1)
        outer.addWidget(self.splitter, 1)

        self.footer = QLabel("", self)
        outer.addWidget(self.footer)

        self.list.chosen.connect(self._chosen)
        self.card.action.connect(self.action)
        self.card.message_activated.connect(self.message_activated)
        self.card.thread_activated.connect(self.thread_activated)
        self.reload()

    # -------------------------------------------------------------- contents
    def reload(self) -> None:
        self.list.reload()
        self.card.reload()
        self.footer.setText(self._footer())

    def _footer(self) -> str:
        """The counts, and the two things that are WRONG rather than so.

        "Nothing to show" over an empty book is indistinguishable from a
        feature that is broken, so the footer says which of the silences it is
        — `ui/trackpane.TrackPane._footer`'s rule.
        """
        counts = book_repo.summary(self._con)
        if not counts["contacts"]:
            return ("No contacts yet — New makes one, and Add from mail "
                    "offers the people you already write to")
        drawn = self.list.count()
        line = f"{counts['contacts']} contact" \
               f"{'' if counts['contacts'] == 1 else 's'}"
        if drawn != counts["contacts"]:
            line += f", {drawn} shown"
        line += f" · {counts['handles']} handle" \
                f"{'' if counts['handles'] == 1 else 's'}"
        # The grammar is written out rather than assembled from a plural
        # marker, which is the lesson `--filters` learnt by printing itself:
        # "1 enabled rule(s) have never matched" mixes a plural marker with a
        # singular verb, and the verb is the half a suffix cannot fix.
        marks = []
        bounced = counts["bounced"]
        if bounced:
            marks.append(f"{bounced} address has bounced" if bounced == 1
                         else f"{bounced} addresses have bounced")
        if counts["no_email"]:
            marks.append(f"{counts['no_email']} with no address")
        if counts["duplicates"]:
            marks.append(f"{counts['duplicates']} possible duplicate"
                         f"{'' if counts['duplicates'] == 1 else 's'}")
        return line + (" · " + " · ".join(marks) if marks else "")

    def contact_id(self) -> int | None:
        return self.card.contact_id()

    def show_contact(self, contact_id: int | None) -> None:
        if contact_id and self.list.select(int(contact_id)):
            return
        self.card.show_contact(contact_id)

    def selected_handle(self) -> int | None:
        return self.card.selected_handle()

    def set_theme(self, theme) -> None:
        self.list.set_theme(theme)
        self.card.set_theme(theme)
        if theme is not None:
            self.footer.setStyleSheet(
                f"color: {theme.text_muted}; padding: 4px 10px;")

    def title(self) -> str:
        """What the TAB says. A number and not a badge: unlike tracking, there
        is nothing here that becomes urgent by being ignored, so the count is
        context rather than a warning."""
        held = contacts_repo.counts(self._con)["contacts"]
        return f"Address book ({held})" if held else "Address book"

    # --------------------------------------------------------------- events
    def _chosen(self, contact_id: int) -> None:
        self.card.show_contact(contact_id or None)
        self.contact_chosen.emit(int(contact_id or 0))

    def _search_changed(self, text: str) -> None:
        self.list.set_query(text)
        self.footer.setText(self._footer())

    def _scope_changed(self, _index: int) -> None:
        # `currentData` and not the index: the chooser's ORDER is a drawing
        # decision and the status is the fact, and a handler that read the
        # index would break the moment a fifth status was added anywhere but
        # the end.
        self.list.set_status(self.scope.currentData() or "")
        self.footer.setText(self._footer())
