# SPDX-License-Identifier: GPL-3.0-or-later
#
# The invitation, above the message that carried it.
#
# An invitation is a message whose point is a decision, and the whole of that
# decision is three buttons. Putting them here — between the header and the
# body, where the withheld-content bar is, and for the same reason — means the
# answer is one click from where the question is, and cannot be scrolled away
# from while the message is still on screen.
#
# IT SAYS WHICH ROUTE THE ANSWER WILL TAKE, BEFORE IT IS PRESSED. `calendar/
# invites.py` answers through the provider's API when the meeting is in a
# calendar corMani syncs and by an iTIP reply through the outbox when it is
# not. Those are different enough that the user should not have to find out
# afterwards which one happened: the line above the buttons says whether this
# will be filed in a calendar or sent as mail.
#
# AN INVITATION ALREADY ANSWERED STILL SHOWS ITS BUTTONS. Changing an answer
# is a thing people do — "yes" on Monday and "no" on Thursday — and a bar that
# collapsed to "Accepted" would send them to the web interface to change it.
# The current answer is marked and its button is the one that is disabled.
#
# A CANCELLATION IS NOT A QUESTION and gets no buttons at all: there is
# nothing to answer, and the useful action is taking the meeting out of the
# day, which is what the one button offers.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from ..store import times
from ..store.calendars import (RESPONSE_ACCEPTED, RESPONSE_DECLINED,
                               RESPONSE_LABELS, RESPONSE_TENTATIVE)

ANSWERS = ((RESPONSE_ACCEPTED, "Accept"), (RESPONSE_TENTATIVE, "Maybe"),
           (RESPONSE_DECLINED, "Decline"))


class InvitationBar(QWidget):
    """What the message is asking, and the three answers to it."""

    answered = Signal(str)                  # a response value
    dismissed = Signal()                    # a cancellation, acted on

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._found = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 6, 12, 6)
        outer.setSpacing(4)

        self.summary = QLabel("", self)
        self.summary.setWordWrap(True)
        font = self.summary.font()
        font.setBold(True)
        self.summary.setFont(font)
        outer.addWidget(self.summary)

        self.detail = QLabel("", self)
        self.detail.setWordWrap(True)
        outer.addWidget(self.detail)

        self.route = QLabel("", self)
        self.route.setWordWrap(True)
        outer.addWidget(self.route)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._buttons = {}
        for value, label in ANSWERS:
            button = QPushButton(label, self)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _c=False, v=value: self.answered.emit(v))
            row.addWidget(button)
            self._buttons[value] = button
        self.remove_button = QPushButton("Take it out of the calendar", self)
        self.remove_button.clicked.connect(lambda: self.dismissed.emit())
        row.addWidget(self.remove_button)
        row.addStretch(1)
        outer.addLayout(row)
        self.setVisible(False)

    def found(self):
        return self._found

    def set_invitation(self, found) -> None:
        """`found` is a `calendar.invites.Found`, or None for an ordinary
        message."""
        self._found = found
        if found is None:
            self.setVisible(False)
            return
        invitation = found.invitation
        self.summary.setText(invitation.summary or "(no subject)")
        self.detail.setText(self._when(invitation))
        cancelled = invitation.is_cancellation
        for value, button in self._buttons.items():
            button.setVisible(not cancelled)
            button.setEnabled(found.my_response != value)
        self.remove_button.setVisible(cancelled)
        self.route.setText(self._route(found, cancelled))
        self.setVisible(True)

    def _when(self, invitation) -> str:
        start = times.parse(invitation.starts_at)
        if start is None:
            return invitation.location or ""
        local = start.astimezone(times.local_zone())
        if invitation.all_day:
            when = local.strftime("%A, %d %B %Y") + " · all day"
        else:
            from .calendarbase import format_time

            when = f"{local.strftime('%A, %d %B %Y')} · {format_time(local)}"
        parts = [when]
        if invitation.recurring:
            parts.append("repeats")
        if invitation.location:
            parts.append(invitation.location)
        if invitation.zone_unknown:
            # Said rather than shown confidently. `calendar/itip.py` records
            # why a zone can fail to resolve at all.
            parts.append("the time zone in this invitation could not be "
                         "resolved, so the time may be wrong")
        return " · ".join(parts)

    def _route(self, found, cancelled: bool) -> str:
        if cancelled:
            return ("This meeting has been cancelled by the organiser."
                    if found.in_a_calendar else
                    "This meeting has been cancelled by the organiser. It is "
                    "not in a calendar corMani syncs.")
        answered = RESPONSE_LABELS.get(found.my_response, "")
        already = (f"You answered {answered.lower()}. "
                   if found.my_response and found.my_response != "needsAction"
                   else "")
        if found.in_a_calendar:
            return f"{already}Answering files this in your calendar."
        return (f"{already}This meeting is not in a calendar corMani syncs, "
                f"so answering sends a reply to the organiser by mail.")

    def apply_theme(self, theme) -> None:
        if theme is None or not theme.surface_raised:
            self.setStyleSheet("")
            return
        self.setStyleSheet(
            f"background: {theme.surface_raised}; color: {theme.text};")
        self.route.setStyleSheet(f"color: {theme.text_muted};")
