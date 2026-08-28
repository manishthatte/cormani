# SPDX-License-Identifier: GPL-3.0-or-later
#
# Which half of the window is showing, and the two panes that are the calendar.
#
# `ui/actions.py` made this argument first and it holds here: deciding what a
# command means is not the same job as arranging four widgets, and the file
# that did both was the one the 600-line rule caught. This is the same shape —
# it HOLDS the pane rather than being part of it — and it owns the two things
# the calendar adds to the window:
#
#   the CALENDAR PANE, which goes where the list and the reading pane are,
#   because PLAN.txt §3 says the calendar replaces them and the rail stays; and
#
#   the AGENDA PANE, which goes BESIDE the mail, because the question it
#   answers arrives in the middle of reading a message about it.
#
# THE TWO ARE NEVER SHOWN TOGETHER. An agenda beside a calendar is the same
# events drawn twice, once in a narrower column.
#
# THE SPLITTER'S SIZES ARE LEFT ALONE ON EVERY SWAP. A person who has widened
# the reading pane finds it the same width when they come back from the
# calendar; a host that set sizes would undo a drag every time.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from . import panespace
from .agendapane import AgendaPane
from .calendarpane import CalendarPane


class CalendarHost:
    """The calendar pane, the agenda pane, and the switching between them."""

    def __init__(self, pane) -> None:
        self.pane = pane
        con = pane._con

        # Beside the mail rather than instead of it.
        self.agenda = AgendaPane(con, pane)
        self.agenda.setVisible(False)
        self.agenda.open_calendar.connect(self.open)
        self.agenda.event_activated.connect(self._agenda_event)
        pane.splitter.addWidget(self.agenda)

        self.calendar = CalendarPane(con, pane)
        self.calendar.setVisible(False)
        self.calendar.status_message.connect(pane.status_message)
        self.calendar.view_changed.connect(pane._calendar_view_changed)
        pane.splitter.addWidget(self.calendar)

        self.showing = False
        self.agenda_wanted = False

    # ------------------------------------------------------------- swapping
    def show(self, on: bool) -> None:
        """Swap the middle and the reader for the calendar, or back.

        FOUR PANES CLAIM THIS SPACE since stage 8 — the calendar, the tracking
        board, a site panel and the address book — and whoever was asked last
        owns it. A pane left visible under another is a window with two things
        drawn on top of each other, which reads as a rendering fault.

        THIS METHOD USED TO STAND DOWN THE SITES ALONE, and the tracking board
        was stood down by `MailPane._calendar_chosen` instead — so a direct
        `calendars.show(True)`, which is what restoring a tab does, left the
        board underneath. `ui/panespace.py` is where that list lives now.
        """
        if on:
            panespace.claim(self.pane, "calendars")
        self.showing = bool(on)
        for widget in (self.pane.middle, self.pane.reader):
            widget.setVisible(not on)
        self.calendar.setVisible(bool(on))
        self.agenda.setVisible(self.agenda_wanted and not on)

    def chosen(self, calendar_id: int) -> None:
        """The rail selected a calendar — one of them, or every ticked one."""
        self.show(True)
        self.calendar.set_filter(
            calendar_ids=[calendar_id] if calendar_id else None)

    def open(self) -> None:
        """Show the calendar from a menu or a key rather than from the rail.

        THROUGH THE RAIL even so: the rail is what says which calendar is
        being shown, and a window that swapped the panes without moving the
        selection would leave the two disagreeing about it.
        """
        if not self.pane.rail.select_key("calendar:all"):
            self.pane.status_message.emit(
                "There is no calendar yet. Add a Google or Microsoft account — "
                "a plain IMAP account has no calendar.")

    def mode(self, mode: str) -> None:
        if not self.showing:
            self.open()
        if self.showing:
            self.calendar.set_mode(mode)

    def action(self, name: str) -> None:
        """Today, forward, back, and new — from the menu or a key."""
        if not self.showing:
            self.open()
        if not self.showing:
            return
        if name == "new":
            self.calendar.new_event(None)
        elif name == "today":
            self.calendar.go_today()
        elif name == "next":
            self.calendar.step(1)
        elif name == "previous":
            self.calendar.step(-1)

    # --------------------------------------------------------------- agenda
    def set_agenda_visible(self, on: bool) -> None:
        self.agenda_wanted = bool(on)
        self.agenda.setVisible(self.agenda_wanted and not self.showing)
        if on:
            self.agenda.reload()

    def _agenda_event(self, event_id: int) -> None:
        """A row in the pane opens the calendar at the day it is on.

        The pane is a glance; the useful next action is the week the meeting
        is in, not a dialog about it.
        """
        from ..store import events as events_repo

        event = events_repo.get_event(self.pane._con, event_id)
        self.open()
        if event is not None and self.showing:
            start = event.start()
            if start is not None:
                self.calendar.set_anchor(start.astimezone().date())
            self.calendar.view().select_event(event_id)

    # -------------------------------------------------------------- redrawing
    def refresh(self) -> None:
        """Something changed under both panes — a tick in the rail, a sync."""
        if self.showing:
            self.calendar.reload()
        if self.agenda_wanted:
            self.agenda.reload()

    def apply_theme(self, theme) -> None:
        self.calendar.apply_theme(theme)
        self.agenda.set_theme(theme)

    # ------------------------------------------------------------ view state
    def state(self) -> tuple:
        """(calendar id or None, mode, anchor) for the tab to remember."""
        if not self.showing:
            return (None, self.calendar.mode(),
                    self.calendar.anchor().isoformat())
        return (self.calendar.chosen_id(), self.calendar.mode(),
                self.calendar.anchor().isoformat())

    def restore(self, state) -> None:
        self.show(True)
        self.calendar.set_filter(
            calendar_ids=[state.calendar_id] if state.calendar_id else None)
        if state.calendar_anchor:
            try:
                self.calendar.set_anchor(
                    dt.date.fromisoformat(state.calendar_anchor))
            except ValueError:                               # pragma: no cover
                pass
        self.calendar.set_mode(state.calendar_mode or "month")
