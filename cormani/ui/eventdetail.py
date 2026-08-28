# SPDX-License-Identifier: GPL-3.0-or-later
#
# One event, in full, beside the view that selected it.
#
# It sits where the reading pane sits when the window is showing mail, and it
# answers the same question: what IS this, and what can I do about it. The
# three answer buttons are the reason it exists — PLAN.txt asks for
# invitations answerable in place, and a calendar that made a person open
# their browser to say yes has not replaced anything.
#
# THE BUTTONS SAY WHAT THEY WILL DO, AND THEY DIFFER. Answering an invitation
# is not editing an event: it is allowed on a calendar shared read-only, and
# it is the only write that is. Editing and deleting are offered only where
# they would work, because `store/eventedits.py` refuses them for a read-only
# calendar and an interface that offered a button which always failed would be
# worse than one that offered nothing.
#
# WHAT IS NOT ANSWERED IS SAID PLAINLY. An event whose time zone could not be
# resolved, one whose change is still queued, one the user has declined —
# each of those is a sentence in the panel rather than a shade of grey.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..store import times
from ..store.calendars import (RESPONSE_ACCEPTED, RESPONSE_DECLINED,
                               RESPONSE_LABELS, RESPONSE_TENTATIVE)
from .calendarbase import span_label

# The three a person can send. `needsAction` is not among them: there is no
# way to un-answer an invitation on either provider, and offering it would be
# offering something that cannot be done.
ANSWERS = ((RESPONSE_ACCEPTED, "Accept"), (RESPONSE_TENTATIVE, "Maybe"),
           (RESPONSE_DECLINED, "Decline"))


class EventDetail(QWidget):
    """What is selected, and the buttons that act on it."""

    respond = Signal(str)                   # a response value
    command = Signal(str)                   # edit | delete | open

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._event = None
        self._calendar = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(self.scroll)
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(12, 12, 12, 12)
        self.body.setSpacing(8)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)

        self.title = _label("", bold=True, size=2)
        self.when = _label("")
        self.where = _label("")
        self.who = _label("")
        self.guests = _label("")
        self.note = _label("")
        self.description = _label("")
        for widget in (self.title, self.when, self.where, self.who,
                       self.guests, self.note, self.description):
            self.body.addWidget(widget)
        self.body.addStretch(1)

        self.answers = QWidget(self)
        row = QHBoxLayout(self.answers)
        row.setContentsMargins(12, 0, 12, 0)
        self._buttons = {}
        for value, label in ANSWERS:
            button = QPushButton(label, self.answers)
            button.clicked.connect(lambda _c=False, v=value: self.respond.emit(v))
            row.addWidget(button)
            self._buttons[value] = button
        row.addStretch(1)
        outer.addWidget(self.answers)

        self.actions = QWidget(self)
        row = QHBoxLayout(self.actions)
        row.setContentsMargins(12, 0, 12, 12)
        self.edit_button = QPushButton("Edit", self.actions)
        self.move_button = QPushButton("Move to…", self.actions)
        self.delete_button = QPushButton("Delete", self.actions)
        self.open_button = QPushButton("Open on the web", self.actions)
        for button, name in ((self.edit_button, "edit"),
                             (self.move_button, "move"),
                             (self.delete_button, "delete"),
                             (self.open_button, "open")):
            button.clicked.connect(lambda _c=False, n=name: self.command.emit(n))
            row.addWidget(button)
        row.addStretch(1)
        outer.addWidget(self.actions)
        self.show_event(None, None)

    # ------------------------------------------------------------- contents
    def current_event(self):
        """NOT `event()`. That name belongs to `QWidget`'s event handler, and
        overriding it with a getter breaks every widget in the tree — Qt calls
        it with an event object and gets a TypeError, silently, in a slot."""
        return self._event

    def current_calendar(self):
        return self._calendar

    def show_event(self, event, calendar, *, tz=None) -> None:
        self._event, self._calendar = event, calendar
        if event is None:
            self.title.setText("")
            for widget in (self.when, self.where, self.who, self.guests,
                           self.note, self.description):
                widget.setText("")
            self.note.setText("Nothing selected.")
            self.answers.setVisible(False)
            self.actions.setVisible(False)
            return

        tz = tz or times.local_zone()
        self.title.setText(event.title)
        start = event.start(tz)
        day = start.strftime("%A, %d %B %Y") if start else ""
        self.when.setText(f"{day} · {span_label(event, tz)}"
                          if day else span_label(event, tz))
        self.where.setText(f"Where: {event.location}" if event.location else "")
        organiser = (event.organiser_name or event.organiser_addr)
        where_from = calendar.label if calendar else ""
        self.who.setText(" · ".join(part for part in (
            f"Organised by {organiser}" if organiser else "", where_from)
            if part))
        self.guests.setText(self._guest_lines(event))
        self.note.setText(self._notes(event))
        self.description.setText(event.description)

        self.answers.setVisible(event.is_invitation)
        for value, button in self._buttons.items():
            button.setEnabled(event.my_response != value)
        self.actions.setVisible(True)
        writable = bool(calendar and calendar.writable)
        if event.is_series_master:
            self.edit_button.setEnabled(False)
            self.edit_button.setToolTip(
                "This is the recurring series master. corMani stores individual "
                "occurrences only — open one occurrence to edit it.")
        else:
            self.edit_button.setEnabled(writable)
            self.edit_button.setToolTip("")
        self.delete_button.setEnabled(writable)
        self.move_button.setEnabled(writable and not event.is_series_master)
        self.move_button.setToolTip(
            "" if writable else "This calendar cannot be written to")
        self.open_button.setVisible(bool(event.web_link))

    def _guest_lines(self, event) -> str:
        if not event.attendees:
            return ""
        lines = ["Guests:"]
        for guest in event.attendees:
            answer = RESPONSE_LABELS.get(guest.response, guest.response)
            mark = " (optional)" if guest.optional else ""
            lines.append(f"    {guest.label}{mark} — {answer}")
        return "\n".join(lines)

    def _notes(self, event) -> str:
        """The sentences about the event that are not the event.

        Each is something the user would otherwise have to infer from a colour
        or from nothing at all.
        """
        notes = []
        if event.needs_reply:
            notes.append("You have not answered this invitation.")
        elif event.is_invitation:
            notes.append(f"You answered "
                         f"{RESPONSE_LABELS.get(event.my_response, '—').lower()}.")
        if event.pending:
            notes.append("Your change has not reached the provider yet; it "
                         "will go on the next sync.")
        if event.recurring and not event.is_series_master:
            notes.append("This is one occurrence of a repeating event. "
                         "corMani changes the occurrence, not the series.")
        elif event.is_series_master:
            notes.append("This is the recurring series master. corMani cannot "
                         "edit the whole series — choose one occurrence instead.")
        if self._calendar is not None and not self._calendar.writable:
            notes.append(f"{self._calendar.label} is shared read-only, so this "
                         f"event cannot be changed here.")
        return "\n".join(notes)


def _label(text: str, *, bold: bool = False, size: int = 0) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    if bold or size:
        font = label.font()
        font.setBold(bold)
        if size:
            font.setPointSize(font.pointSize() + size)
        label.setFont(font)
    # An empty label still takes a line of layout, which is what makes the
    # panel jump as the selection moves between events with and without a
    # location. Hiding it when there is nothing to say is the fix.
    label.setVisible(bool(text))
    _watch(label)
    return label


def _watch(label: QLabel) -> None:
    original = label.setText

    def setText(text):                                       # noqa: N802 (Qt)
        original(text)
        label.setVisible(bool(text))

    label.setText = setText
