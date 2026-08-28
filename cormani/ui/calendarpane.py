# SPDX-License-Identifier: GPL-3.0-or-later
#
# The calendar, where the list and the reading pane are.
#
# PLAN.txt §3 says the calendar REPLACES the list and the reader rather than
# opening a window of its own, and the rail stays where it is. That is the
# whole shape of this file: a toolbar, one of three views, and a detail panel
# beside them where the reading pane would be.
#
# THE THREE VIEWS SHOW THE SAME RANGE AND ARE DRIVEN THROUGH THE SAME FOUR
# NAMES — `set_events`, `set_anchor`, `range`, `select_event`. Switching view
# must not move the user in time, and a pane that knew which of the three it
# was holding would grow a branch per operation.
#
# A RANGE THE STORE HAS NOT FETCHED IS SAID, NOT DRAWN AS EMPTY. `Calendar.
# covers` is the honest answer to "can this window be trusted", and paging back
# to 2019 gives a footer that says so and a request to go and get it. An empty
# week that is empty because nobody asked is exactly the quiet wrong of
# CONVENTIONS.txt §8.
#
# EVERY ACTION GOES THROUGH `store/eventedits.py` AND THEREFORE THROUGH THE
# QUEUE. Nothing here opens a socket. Making an event on a train puts it on
# the screen and in the queue, and `calendar/queue.py` tells the provider when
# there is a network — the same rule as pressing Send.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from ..store import calendars as calendars_repo
from ..store import eventedits
from ..store import events as events_repo
from ..store import times
from .agendaview import AgendaView
from .eventdetail import EventDetail
from .eventdialog import EventDialog
from .monthview import MonthView
from .weekview import WeekView

MONTH, WEEK, DAY, AGENDA = "month", "week", "day", "agenda"
VIEWS = (MONTH, WEEK, DAY, AGENDA)
VIEW_LABELS = {MONTH: "Month", WEEK: "Week", DAY: "Day", AGENDA: "Agenda"}


class CalendarPane(QWidget):
    """The toolbar, the view, and the panel beside it."""

    status_message = Signal(str)
    view_changed = Signal()
    fetch_requested = Signal(object)        # (calendar_ids, start, end)

    def __init__(self, con: sqlite3.Connection, parent=None, *,
                 dialogs=None) -> None:
        super().__init__(parent)
        self._con = con
        self._theme = None
        self._tz = times.local_zone()
        self._anchor = times.now_local().date()
        self._mode = MONTH
        self._account_id = None
        self._calendar_ids = None           # None means "every shown calendar"
        self._label = ""
        # Injected because Debian ships no QTest and a modal dialog cannot be
        # driven from a test. The same seam as the attachment strip's four.
        self._dialogs = dialogs or {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._toolbar())

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.stack = QStackedWidget(self)
        self.views = {MONTH: MonthView(self), WEEK: WeekView(self, days=7),
                      DAY: WeekView(self, days=1), AGENDA: AgendaView(self)}
        for key in VIEWS:
            view = self.views[key]
            self.stack.addWidget(view)
            view.event_selected.connect(self._selected)
            view.event_activated.connect(self._activated)
            view.slot_activated.connect(self.new_event)
            view.range_requested.connect(self._range_requested)
        self.splitter.addWidget(self.stack)

        self.detail = EventDetail(self)
        self.detail.respond.connect(self._respond)
        self.detail.command.connect(self._command)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([760, 320])
        layout.addWidget(self.splitter, 1)

        self.footer = QLabel("", self)
        self.footer.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.footer)
        self.set_mode(MONTH)

    def _toolbar(self) -> QWidget:
        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 0)
        row.setSpacing(6)
        self.today_button = QPushButton("Today", bar)
        self.today_button.clicked.connect(self.go_today)
        row.addWidget(self.today_button)
        for text, step in (("‹", -1), ("›", 1)):
            button = QPushButton(text, bar)
            button.setFixedWidth(30)
            button.clicked.connect(lambda _c=False, s=step: self.step(s))
            row.addWidget(button)
        self.range_label = QLabel("", bar)
        font = self.range_label.font()
        font.setBold(True)
        self.range_label.setFont(font)
        row.addWidget(self.range_label)
        row.addStretch(1)
        self._mode_buttons = {}
        for key in VIEWS:
            button = QPushButton(VIEW_LABELS[key], bar)
            button.setCheckable(True)
            button.clicked.connect(lambda _c=False, k=key: self.set_mode(k))
            row.addWidget(button)
            self._mode_buttons[key] = button
        self.new_button = QPushButton("New event", bar)
        self.new_button.clicked.connect(lambda: self.new_event(None))
        row.addWidget(self.new_button)
        return bar

    # ---------------------------------------------------------------- state
    def view(self):
        return self.views[self._mode]

    def mode(self) -> str:
        return self._mode

    def anchor(self) -> dt.date:
        return self._anchor

    def set_mode(self, mode: str) -> None:
        if mode not in self.views:
            return                                           # pragma: no cover
        self._mode = mode
        for key, button in self._mode_buttons.items():
            button.setChecked(key == mode)
        self.stack.setCurrentWidget(self.views[mode])
        self.reload()
        self.view_changed.emit()

    def set_anchor(self, day: dt.date) -> None:
        self._anchor = day
        self.reload()
        self.view_changed.emit()

    def go_today(self) -> None:
        self.set_anchor(times.now_local().date())

    def step(self, direction: int) -> None:
        """Forward or back by the unit the current view is drawn in.

        A month view steps a month and a week view a week, which is what the
        arrows mean to the person looking at them. Stepping by a fixed number
        of days in all three would make the month view drift off its edges.
        """
        if self._mode == MONTH or self._mode == AGENDA:
            first = times.month_start(self._anchor)
            total = first.year * 12 + first.month - 1 + direction
            self.set_anchor(dt.date(total // 12, total % 12 + 1, 1))
        elif self._mode == WEEK:
            self.set_anchor(self._anchor + dt.timedelta(days=7 * direction))
        else:
            self.set_anchor(self._anchor + dt.timedelta(days=direction))

    def set_filter(self, *, account_id: int | None = None,
                   calendar_ids=None) -> None:
        """Which calendars are drawn. None for both means every shown one."""
        self._account_id = account_id
        self._calendar_ids = list(calendar_ids) if calendar_ids else None
        self.reload()

    def chosen_id(self) -> int:
        """The calendar this pane is pinned to, or 0 for every ticked one.

        What a tab remembers. Not the resolved LIST, which changes when the
        user ticks a calendar in the rail — a tab that had recorded the list
        would be restored to yesterday's ticks.
        """
        return int(self._calendar_ids[0]) if self._calendar_ids else 0

    def title(self) -> str:
        return self._label or "Calendar"

    # --------------------------------------------------------------- drawing
    def calendar_ids(self) -> list:
        if self._calendar_ids is not None:
            return list(self._calendar_ids)
        accounts = [self._account_id] if self._account_id else None
        return calendars_repo.shown_ids(self._con, account_ids=accounts)

    def reload(self) -> None:
        view = self.view()
        view.set_anchor(self._anchor)
        view.set_timezone(self._tz)
        start, end = view.range()
        ids = self.calendar_ids()
        known = {c.id: c for c in calendars_repo.list_calendars(self._con)}
        events = events_repo.events_between(self._con, start, end,
                                            calendar_ids=ids,
                                            with_attendees=True)
        view.set_events(events, known)
        self._label = _range_label(self._mode, self._anchor, start, end)
        self.range_label.setText(self._label)
        self._refresh_detail()
        self._footer(events, ids, known, start, end)

    def _footer(self, events, ids, known, start, end) -> None:
        if not ids:
            self.footer.setText(
                "No calendar is being shown. Tick one in the rail, or add an "
                "account whose provider has a calendar.")
            return
        window = (times.to_utc_text(start), times.to_utc_text(end))
        missing = [known[i].label for i in ids
                   if i in known and not known[i].covers(*window)]
        counted = f"{len(events)} event{'' if len(events) == 1 else 's'}"
        if missing:
            # Said, not drawn as empty. See the module header.
            self.footer.setText(
                f"{counted}. This range has not been fetched for "
                f"{', '.join(missing[:3])}"
                f"{' and others' if len(missing) > 3 else ''} — press F5.")
            self.fetch_requested.emit((ids, window[0], window[1]))
        else:
            self.footer.setText(counted)

    def _refresh_detail(self) -> None:
        event = events_repo.get_event(self._con, self.view().selected_id()) \
            if self.view().selected_id() else None
        calendar = (calendars_repo.get_calendar(self._con, event.calendar_id)
                    if event else None)
        self.detail.show_event(event, calendar, tz=self._tz)

    # -------------------------------------------------------------- actions
    def _selected(self, _event_id: int) -> None:
        self._refresh_detail()

    def _activated(self, event_id: int) -> None:
        self.view().select_event(event_id)
        self._refresh_detail()
        self.edit_event()

    def _range_requested(self, day) -> None:
        """A view asking to be shown something else — "+3 more", or a page key.

        The month view's answer is the DAY, because that is what somebody
        clicking "+3 more" wants to see; a page key asks for another month and
        stays in the month view.
        """
        if isinstance(day, dt.date) and self._mode == MONTH and day.day != 1:
            self._anchor = day
            self.set_mode(DAY)
            return
        self.set_anchor(day)

    def new_event(self, when) -> None:
        """The dialog, and then the store. Never the provider directly."""
        calendar_id = (self._calendar_ids[0] if self._calendar_ids else None)
        dialog = self._dialog(event=None, calendar_id=calendar_id, when=when)
        if dialog is None or not self._run(dialog):
            return
        values = dialog.values()
        target = values.pop("calendar_id")
        if target is None:
            self.status_message.emit("There is no calendar that can be written "
                                     "to. Nothing was created.")
            return
        try:
            eventedits.create_event(self._con, target, **values)
        except eventedits.NotWritable as exc:
            self.status_message.emit(str(exc))
            return
        self.status_message.emit(
            f"{values['summary']} added — it will reach the provider on the "
            f"next sync")
        self.reload()

    def edit_event(self) -> None:
        event = self.detail.current_event()
        if event is None:
            return
        dialog = self._dialog(event=event)
        if dialog is None or not self._run(dialog):
            return
        values = dialog.values()
        values.pop("calendar_id", None)
        attendees = values.pop("attendees", None)
        try:
            eventedits.update_event(self._con, event.id, attendees=attendees,
                                    **values)
        except eventedits.NotWritable as exc:
            self.status_message.emit(str(exc))
            return
        self.status_message.emit(f"{event.title} changed")
        self.reload()

    def delete_event(self) -> None:
        event = self.detail.current_event()
        if event is None:
            return
        confirm = self._dialogs.get("confirm")
        if confirm is not None and not confirm(
                f"Delete “{event.title}”?",
                "The event will be removed here and on the provider."):
            return
        try:
            removed = eventedits.delete_event(self._con, event.id)
        except eventedits.NotWritable as exc:
            self.status_message.emit(str(exc))
            return
        if removed is not None:
            self.status_message.emit(f"{removed.title} deleted")
        self.view().select_event(0)
        self.reload()

    def _respond(self, response: str) -> None:
        event = self.detail.current_event()
        if event is None:
            return
        eventedits.set_response(self._con, event.id, response)
        self.status_message.emit(
            f"Answered — the organiser will be told on the next sync")
        self.reload()

    def _command(self, name: str) -> None:
        if name == "edit":
            self.edit_event()
        elif name == "delete":
            self.delete_event()
        elif name == "open":
            event = self.detail.current_event()
            opener = self._dialogs.get("open_url")
            if event is not None and event.web_link and opener is not None:
                opener(event.web_link)

    # --------------------------------------------------------------- dialogs
    def _dialog(self, **kwargs):
        """The event dialog, or whatever a test injected in its place."""
        maker = self._dialogs.get("event")
        if maker is not None:
            return maker(**kwargs)
        return EventDialog(self._con, account_id=self._account_id,
                           parent=self, tz=self._tz, **kwargs)

    def _run(self, dialog) -> bool:
        runner = self._dialogs.get("run")
        if runner is not None:
            return bool(runner(dialog))
        return dialog.exec() == dialog.DialogCode.Accepted

    # ---------------------------------------------------------------- chrome
    def apply_theme(self, theme) -> None:
        self._theme = theme
        for view in self.views.values():
            view.set_theme(theme)

    def set_timezone(self, tz) -> None:
        self._tz = tz
        self.reload()


def _range_label(mode: str, anchor: dt.date, start, end) -> str:
    """What the toolbar says the user is looking at."""
    if mode in (MONTH, AGENDA):
        return anchor.strftime("%B %Y")
    if mode == DAY:
        return anchor.strftime("%A, %d %B %Y")
    first = start.date()
    last = (end - dt.timedelta(days=1)).date()
    if first.month == last.month:
        return f"{first.day} – {last.day} {first.strftime('%B %Y')}"
    if first.year == last.year:
        return (f"{first.day} {first.strftime('%b')} – "
                f"{last.day} {last.strftime('%b %Y')}")
    return (f"{first.day} {first.strftime('%b %Y')} – "
            f"{last.day} {last.strftime('%b %Y')}")
