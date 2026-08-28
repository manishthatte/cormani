# SPDX-License-Identifier: GPL-3.0-or-later
#
# Today, beside the mail.
#
# Outlook's right-hand agenda, which PLAN.txt §2 asks for by name, and the
# reason to have it is the reason to have the whole application: the question
# "am I free at four" arrives in the middle of reading a message about it, and
# a client that answers it without leaving the message has removed a context
# switch that happens twenty times a day.
#
# IT DRAWS THE SAME ROWS AS THE AGENDA VIEW. `ui/agendaview._List` already
# knows how to draw a day heading and an event row; giving it a range instead
# of a month is the whole of the difference. Two widgets that drew the same
# thing would eventually disagree about what an all-day event looks like.
#
# IT IS OFF BY DEFAULT AND THAT IS DELIBERATE. It costs three hundred pixels of
# a three-pane window, which is the reading pane's width on a laptop. A person
# who wants it turns it on and the window remembers; a person who does not
# should never have had it imposed.
#
# CLICKING SOMETHING OPENS THE CALENDAR RATHER THAN A DIALOG. The pane is a
# glance, not a place to work: the useful next action is the week the meeting
# is in, and that is where the button goes.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..store import calendars as calendars_repo
from ..store import events as events_repo
from ..store import times
from .agendaview import _List

# How far ahead the pane looks. Long enough that "nothing today" is followed by
# what IS coming, short enough that it stays a glance.
DAYS = 7


class AgendaPane(QWidget):
    """A narrow list of what is coming, beside the message list."""

    event_activated = Signal(int)
    open_calendar = Signal()

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._tz = times.local_zone()
        self.setMinimumWidth(220)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QWidget(self)
        row = QHBoxLayout(head)
        row.setContentsMargins(10, 6, 6, 6)
        self.title = QLabel("Agenda", head)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        row.addWidget(self.title, 1)
        self.open_button = QPushButton("Open", head)
        self.open_button.setFlat(True)
        self.open_button.clicked.connect(lambda: self.open_calendar.emit())
        row.addWidget(self.open_button, 0)
        outer.addWidget(head)

        self.list = _List(self)
        self.list.event_activated.connect(self.event_activated)
        self.scroll = QScrollArea(self)
        self.scroll.setWidget(self.list)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll, 1)

        self.footer = QLabel("", self)
        self.footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.footer)
        self.reload()

    # ------------------------------------------------------------- contents
    def reload(self, *, now: dt.datetime | None = None) -> None:
        now = now or times.now_local()
        start, _ = times.day_bounds(now.date(), self._tz)
        end = start + dt.timedelta(days=DAYS)
        ids = calendars_repo.shown_ids(self._con)
        known = {c.id: c for c in calendars_repo.list_calendars(self._con)}
        events = (events_repo.events_between(self._con, start, end,
                                             calendar_ids=ids) if ids else [])
        self.list.set_anchor(now.date(), today=now.date())
        self.list.set_timezone(self._tz)
        self.list.set_span(start, end)
        self.list.set_events(events, known)
        self.title.setText(now.strftime("%A, %d %B"))
        self.footer.setText(self._footer(events, ids, now))

    def _footer(self, events, ids, now: dt.datetime) -> str:
        """What the pane says when there is nothing to draw.

        Three different silences, and they are not the same thing: no calendar
        at all, no calendar ticked, and a genuinely free week. Saying "nothing"
        to all three is how a person concludes the feature is broken.
        """
        if not calendars_repo.list_calendars(self._con):
            return "No calendar yet"
        if not ids:
            return "No calendar is ticked"
        if not events:
            return f"Nothing in the next {DAYS} days"
        today = [e for e in events if (e.start(self._tz) or now).date()
                 == now.date()]
        return (f"{len(today)} today · {len(events)} this week" if today
                else f"Nothing today · {len(events)} this week")

    def set_theme(self, theme) -> None:
        self.list.set_theme(theme)
        if theme is not None and theme.text_muted:
            self.footer.setStyleSheet(f"color: {theme.text_muted}; padding: 4px;")

    def set_timezone(self, tz) -> None:
        self._tz = tz
        self.reload()
