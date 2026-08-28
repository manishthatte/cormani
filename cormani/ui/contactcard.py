# SPDX-License-Identifier: GPL-3.0-or-later
#
# One person, drawn: who they are, every way of reaching them, and what has
# passed between.
#
# PLAN.txt §2 asks for this by name — "contact cards carrying every handle a
# person has: addresses, numbers, profiles" — and it is the last of the six
# things §2 lists under "from corMani itself". The other five have had a
# surface since stage 6; this one has had a STORE since stage 6 and nothing
# that could see it.
#
# ── SPLIT FROM `ui/contactpane.py` BEFORE THE 600-LINE RULE FIRED ──────────
#
# `cormani/calcli.py` and `store/calendarfixtures.py` were both split at 344
# and 462 lines because the seam was obvious from what the code was ABOUT
# rather than from its length, and this is the same: a LIST of people and a
# CARD of one person are two subjects that share a splitter and nothing else.
# Together they are about seven hundred lines, so the rule would have fired
# anyway and named a worse seam.
#
# ── EVERY HANDLE, AND THE BOUNCED ONE DRAWN AS SUCH ────────────────────────
#
# `store/fixtures.py` has carried a bounced handle since stage 1 with a comment
# saying it exists "because a handle whose status is 'bounced' is the state the
# contact card must render correctly". This is that card, four stages later.
# The bounce is not a warning here — the composer's guard is where a warning
# belongs, at the moment it can change what somebody does — it is a FACT about
# the address, and what the card owes is the server's own words, because
# "mailbox full" and "no such user" call for opposite decisions.
#
# ── THE CORRESPONDENCE COUNTS ARE TAKEN HERE AND NOT IN THE LIST ───────────
#
# `store/addressbook.correspondence` is a scan, and its header explains that
# the cost is affordable exactly once — when a card is opened. So this widget
# asks and the list beside it does not. A "messages" column beside every name
# would be that query per row, per redraw.
#
# ── IT PERFORMS NOTHING ────────────────────────────────────────────────────
#
# `ui/trackpane.py`'s rule, and the same reason: the suite has no QTest and
# cannot click anything, so every button emits a NAME and `ui/contacthost.py`
# decides what it means. It is also what keeps one implementation of "merge"
# rather than one here and one in the menu.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout,
                               QWidget)

from ..store import addressbook as book_repo
from ..store import contacts as contacts_repo
from ..store import times
from ..store import tracking as tracking_repo

# What a handle's kind is drawn as. The mark is a hint and never the only
# label — `store/contacts.SEED_KINDS` is a SEED and not an enumeration, so a
# kind nobody anticipated must still draw, which is why the kind's own word
# follows it.
_KIND_MARKS = {"email": "@", "phone": "☎", "whatsapp": "w", "linkedin": "in",
               "x": "x", "facebook": "f", "signal": "s", "web": "⌘"}

_STATUS_WORDS = {
    contacts_repo.CONTACT_ACTIVE: "",
    contacts_repo.CONTACT_LEFT_ORG: "has left the organisation",
    contacts_repo.CONTACT_DO_NOT_CONTACT: "DO NOT CONTACT",
}

# What the buttons are, in the order they are drawn. Names, because the host
# takes names — see the header.
_BUTTONS = (("edit", "Edit"), ("add-handle", "Add a handle"),
            ("remove-handle", "Remove handle"), ("write", "Write to"),
            ("mail", "Their mail"), ("merge", "Merge…"), ("delete", "Delete"))


class ContactCard(QWidget):
    """One contact: identity, handles, threads, and the mail either way."""

    action = Signal(str)
    message_activated = Signal(int)
    thread_activated = Signal(int)

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._contact_id: int | None = None
        self._theme = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 8)
        outer.setSpacing(4)

        self.title = QLabel("", self)
        font = QFont(self.title.font())
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setWordWrap(True)
        outer.addWidget(self.title)

        self.subtitle = QLabel("", self)
        self.subtitle.setWordWrap(True)
        outer.addWidget(self.subtitle)

        self.standing = QLabel("", self)
        self.standing.setWordWrap(True)
        outer.addWidget(self.standing)

        self.correspondence = QLabel("", self)
        self.correspondence.setWordWrap(True)
        outer.addWidget(self.correspondence)

        outer.addWidget(self._rule())

        self.handles_heading = QLabel("Ways of reaching them", self)
        outer.addWidget(self.handles_heading)
        self.handles = QListWidget(self)
        self.handles.setMaximumHeight(140)
        # CONNECTED ONCE, HERE, and not where the list is filled. A connect
        # inside the redraw is a second connection on every reload, and the
        # slot then runs as many times as the card has ever been opened —
        # silent, because this one only enables a button.
        self.handles.currentItemChanged.connect(self._handle_chosen)
        outer.addWidget(self.handles)

        self.threads_heading = QLabel("", self)
        outer.addWidget(self.threads_heading)
        self.threads = QListWidget(self)
        self.threads.setMaximumHeight(90)
        self.threads.itemActivated.connect(self._thread_activated)
        outer.addWidget(self.threads)

        self.recent_heading = QLabel("", self)
        outer.addWidget(self.recent_heading)
        self.recent = QListWidget(self)
        self.recent.itemActivated.connect(self._message_activated)
        outer.addWidget(self.recent, 1)

        self.notes = QLabel("", self)
        self.notes.setWordWrap(True)
        outer.addWidget(self.notes)

        outer.addWidget(self._buttons())
        self.show_contact(None)

    def _rule(self) -> QFrame:
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        return line

    def _buttons(self) -> QWidget:
        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 6, 0, 0)
        self.buttons = {}
        for name, label in _BUTTONS:
            button = QPushButton(label, bar)
            # `_checked=False` and not a bare lambda: QPushButton.clicked
            # carries the checked state and PySide6 hands it to any slot whose
            # signature will take it — see SESSION_STATE's note.
            button.clicked.connect(lambda _checked=False, n=name:
                                   self.action.emit(n))
            row.addWidget(button)
            self.buttons[name] = button
        row.addStretch(1)
        return bar

    # ----------------------------------------------------------------- theme
    def set_theme(self, theme) -> None:
        self._theme = theme
        if theme is None:
            return
        for label in (self.subtitle, self.correspondence, self.notes,
                      self.handles_heading, self.threads_heading,
                      self.recent_heading):
            label.setStyleSheet(f"color: {theme.text_muted};")
        self.reload()

    # -------------------------------------------------------------- contents
    def contact_id(self) -> int | None:
        return self._contact_id

    def show_contact(self, contact_id: int | None) -> None:
        self._contact_id = int(contact_id) if contact_id else None
        self.reload()

    def contact(self):
        """The contact this card is showing, read afresh. None when there is
        none, or when the one it was showing has been deleted from under it."""
        if not self._contact_id:
            return None
        return contacts_repo.get_contact(self._con, self._contact_id)

    def selected_handle(self) -> int | None:
        """Which handle the Remove button would take, or None.

        `UserRole` on the item rather than the row index, because the list is
        rebuilt on every reload and an index would name a different handle
        after one.

        NONE FOR THE PLACEHOLDER TOO, and not the 0 it carries. Qt makes an
        unselectable item CURRENT even though it cannot be selected, so the
        "no way of reaching them yet" row arrives here like any other; 0 is
        falsy and the Remove command therefore behaved correctly by accident,
        while this method was answering a different question from the one its
        own docstring asks.
        """
        item = self.handles.currentItem()
        if item is None:
            return None
        return int(item.data(Qt.ItemDataRole.UserRole)) or None

    def reload(self) -> None:
        contact = self.contact()
        for name, button in self.buttons.items():
            button.setEnabled(contact is not None)
        if contact is None:
            self._draw_nobody()
            return

        self.title.setText(contact.label)
        self.subtitle.setText(" · ".join(x for x in (contact.org, contact.role)
                                         if x))
        self.standing.setText(_STATUS_WORDS.get(contact.status, contact.status))
        if self._theme is not None:
            self.standing.setStyleSheet(
                f"color: {self._theme.error};"
                if contact.status == contacts_repo.CONTACT_DO_NOT_CONTACT
                else f"color: {self._theme.text_muted};")
        self.correspondence.setText(self._correspondence(contact))
        self._draw_handles(contact)
        self._draw_threads(contact)
        self._draw_recent(contact)
        self.notes.setText(contact.notes)
        # Removing a handle needs one CHOSEN, and there is nothing chosen the
        # instant a card opens. Asked of the list rather than of the button,
        # because the button's own enabled state is what is being decided.
        self._sync_remove_button()

    def _draw_nobody(self) -> None:
        """What an empty card says, and it says which of two silences it is.

        "Nobody selected" over an address book with two hundred people in it
        and over one with none are different facts, and a card that drew the
        same sentence for both would leave a person looking for the list.
        """
        held = contacts_repo.counts(self._con)["contacts"]
        self.title.setText("Nobody selected" if held else "No contacts yet")
        self.subtitle.setText(
            "Choose somebody on the left." if held else
            "New makes one. Add from mail offers the people you already "
            "correspond with, most mail first.")
        for label in (self.standing, self.correspondence, self.notes,
                      self.threads_heading, self.recent_heading):
            label.setText("")
        for listing in (self.handles, self.threads, self.recent):
            listing.clear()

    def _correspondence(self, contact) -> str:
        """The two counts and how long it has been.

        THE LAST DATE IS THE HALF PEOPLE ACT ON. "23 from them, 8 to them" is
        a description of a relationship; "last 14 months ago" is a reason to
        write. Both, or the card is a filing cabinet.
        """
        seen = book_repo.correspondence(self._con, contact)
        words = book_repo.describe_mail(contact, seen)
        if not seen.any:
            return words
        when = times.to_local(seen.last_at)
        stamp = when.strftime("%d %b %Y") if when else "an unknown date"
        return f"{words} · last on {stamp}"

    def _draw_handles(self, contact) -> None:
        """Rebuilt with the signals blocked, so that clearing the list does not
        report a selection change the user did not make — which would turn the
        Remove button off in the middle of somebody using it."""
        self.handles.blockSignals(True)
        self.handles.clear()
        for handle in contact.handles:
            item = QListWidgetItem(self._handle_text(handle), self.handles)
            item.setData(Qt.ItemDataRole.UserRole, handle.id)
            item.setToolTip(self._handle_tooltip(handle))
            if self._theme is not None and handle.is_bounced:
                item.setForeground(QColor(self._theme.error))
            self.handles.addItem(item)
        if not contact.handles:
            item = QListWidgetItem("No way of reaching them yet — "
                                   "Add a handle", self.handles)
            # Not selectable, so `selected_handle` reports None and Remove
            # stays off. A placeholder row that could be chosen would offer to
            # delete a sentence.
            item.setData(Qt.ItemDataRole.UserRole, 0)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.handles.addItem(item)
        self.handles.blockSignals(False)

    def _handle_text(self, handle) -> str:
        mark = _KIND_MARKS.get(handle.kind, "·")
        text = f"{mark} {handle.value}"
        if handle.kind != contacts_repo.KIND_EMAIL:
            text += f"   ({handle.kind})"
        if handle.is_bounced:
            times_ = handle.bounce_count
            text += ("   — BOUNCED once" if times_ == 1
                     else f"   — BOUNCED {times_} times")
        elif handle.status == contacts_repo.STATUS_VERIFIED:
            text += "   ✓"
        return text

    def _handle_tooltip(self, handle) -> str:
        """The server's own words, quoted rather than summarised.

        `contacts.describe_bounces` makes the same argument for the composer's
        warning: "mailbox full" and "no such user" call for opposite decisions,
        so a status word in place of the reason is the half that matters
        thrown away.
        """
        lines = [f"{handle.kind}: {handle.value}", f"status: {handle.status}"]
        if handle.is_bounced and handle.last_bounce_at:
            when = times.to_local(handle.last_bounce_at)
            lines.append("last refused "
                         + (when.strftime("%d %b %Y") if when
                            else handle.last_bounce_at))
        if handle.note:
            lines.append(f"the server said: {handle.note}")
        return "\n".join(lines)

    def _draw_threads(self, contact) -> None:
        """The threads this person is on — `store/tracking.threads_for_contact`
        and never a query of this file's own. Two answers to "what is this
        person involved in" is one of them being wrong on a Tuesday."""
        self.threads.clear()
        threads = tracking_repo.threads_for_contact(self._con, contact.id)
        if not threads:
            self.threads_heading.setText("On no tracked thread")
            self.threads.setVisible(False)
            return
        self.threads_heading.setText(
            f"On {len(threads)} tracked thread"
            f"{'' if len(threads) == 1 else 's'}")
        self.threads.setVisible(True)
        for thread in threads:
            label = thread.title
            if thread.org:
                label += f"  ·  {thread.org}"
            label += f"  ·  {thread.state}"
            item = QListWidgetItem(label, self.threads)
            item.setData(Qt.ItemDataRole.UserRole, thread.id)
            self.threads.addItem(item)

    def _draw_recent(self, contact) -> None:
        self.recent.clear()
        rows = book_repo.recent_messages(self._con, contact)
        if not rows:
            self.recent_heading.setText("")
            return
        self.recent_heading.setText("Recent mail")
        for row in rows:
            when = times.to_local(row["date_at"])
            stamp = when.strftime("%d %b %Y") if when else "?"
            arrow = "→" if int(row["outbound"]) else "←"
            subject = row["subject"] or "(no subject)"
            item = QListWidgetItem(f"{stamp}  {arrow}  {subject}", self.recent)
            item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            if self._theme is not None and not int(row["seen"]) \
                    and not int(row["outbound"]):
                item.setForeground(QColor(self._theme.unread))
            self.recent.addItem(item)

    # --------------------------------------------------------------- events
    def _handle_chosen(self, _current, _previous) -> None:
        self._sync_remove_button()

    def _sync_remove_button(self) -> None:
        """ONE QUESTION, ASKED ONCE. This used to test `current is not None`
        while the command tested `selected_handle()`, which are different
        questions about the placeholder row — the button would have offered to
        remove the sentence "No way of reaching them yet" and the command would
        have declined. Two places deciding whether a handle is chosen is how
        they come to disagree."""
        self.buttons["remove-handle"].setEnabled(
            self.selected_handle() is not None)

    def _message_activated(self, item) -> None:
        self.message_activated.emit(int(item.data(Qt.ItemDataRole.UserRole)))

    def _thread_activated(self, item) -> None:
        self.thread_activated.emit(int(item.data(Qt.ItemDataRole.UserRole)))
