# SPDX-License-Identifier: GPL-3.0-or-later
#
# Telling somebody a meeting is about to start.
#
# WHOSE NUMBER IS IT. An event's own reminder is set far less often than the
# calendar's: both providers let an event say "the default" and most events do,
# so the lead time is the event's if it has one and the calendar's otherwise.
# `store/calendars.default_reminder` exists for exactly this, and a client that
# read only the event would silently remind nobody about almost anything.
#
# ONCE EACH, AND ONLY FOR SOMETHING STILL AHEAD. A reminder that repeated every
# minute would be worse than none; one that fired for this morning's meetings
# when a laptop is opened in the afternoon is noise a person learns to ignore.
# So a fired id is remembered for the session, and nothing fires more than
# `GRACE` after the start.
#
# IN MEMORY RATHER THAN IN THE STORE, and that is a decision rather than
# laziness: after a restart, a meeting that has not happened yet is one the
# user still wants to be told about, and a table of "already reminded" would
# turn a crash at nine into a missed meeting at ten.
#
# A DECLINED MEETING DOES NOT REMIND. The user said they were not going. It is
# the one filter here that is about intent rather than time.
#
# NOTHING IS CLAIMED THAT CANNOT BE KNOWN. `platform/notify.py` reports whether
# it could SEND, never whether anybody saw it, so this emits the same words for
# the status bar when there is no notification service — CONVENTIONS.txt §8.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3

from PySide6.QtCore import QObject, QTimer, Signal

from ..platform import notify as notify_mod
from ..store import calendars as calendars_repo
from ..store import events as events_repo
from ..store import times
from ..store.calendars import RESPONSE_DECLINED
from .calendarbase import format_time

# How often to look. A minute is the resolution a reminder is set in, so
# checking more often buys nothing and checking less can miss a five-minute one.
INTERVAL = 60 * 1000

# How far ahead to look at all. A day plus an hour, because the longest lead
# time offered is one day and an event exactly a day out must be seen before it
# is due rather than after.
HORIZON_HOURS = 25

# How late a reminder may still be worth showing. Five minutes: a meeting that
# started ten minutes ago is one the person is already in or has already missed.
GRACE = dt.timedelta(minutes=5)


class Reminders(QObject):
    """A timer, a memory of what has fired, and one notification per event."""

    fired = Signal(int, str, str, bool)     # event id, title, body, sent

    def __init__(self, con: sqlite3.Connection, *, interval: int = INTERVAL,
                 notifier=None, clock=None, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._notify = notifier or notify_mod.notify
        self._clock = clock or times.now_local
        self._seen: set = set()
        self._timer = QTimer(self)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self.check)

    def start(self) -> None:
        self._timer.start()
        self.check()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def due(self, now: dt.datetime | None = None) -> list:
        """The events whose reminder has come and has not been shown."""
        now = now or self._clock()
        horizon = now + dt.timedelta(hours=HORIZON_HOURS)
        calendars = {c.id: c for c in calendars_repo.list_calendars(self._con)}
        out = []
        for event in events_repo.events_between(
                self._con, now - GRACE, horizon,
                calendar_ids=calendars_repo.shown_ids(self._con)):
            if event.id in self._seen or event.my_response == RESPONSE_DECLINED:
                continue
            lead = self._lead(event, calendars.get(event.calendar_id))
            if lead is None:
                continue
            start = event.start()
            if start is None:                                # pragma: no cover
                continue
            if start - dt.timedelta(minutes=lead) <= now <= start + GRACE:
                out.append(event)
        return out

    def check(self) -> int:
        """Show whatever is due. Returns how many were shown."""
        shown = 0
        for event in self.due():
            self._seen.add(event.id)
            title, body = describe(event)
            sent = bool(self._notify(title, body))
            self.fired.emit(event.id, title, body, sent)
            shown += 1
        return shown

    def forget(self, event_id: int) -> None:
        """Let an event remind again — after its time has been changed."""
        self._seen.discard(int(event_id))

    def _lead(self, event, calendar) -> int | None:
        """Minutes before the start, or None when nobody asked for one.

        An ALL-DAY event is not reminded about by its own minutes: "ten minutes
        before" a day that begins at midnight is a notification at ten to
        twelve at night. Both providers treat an all-day reminder as a lead
        from midnight and so does this, but only when the event itself asks —
        a calendar default of ten minutes is about meetings.
        """
        if event.reminder is not None:
            return int(event.reminder)
        if event.all_day:
            return None
        return (int(calendar.default_reminder)
                if calendar is not None and calendar.default_reminder is not None
                else None)


def describe(event) -> tuple:
    """The two lines a notification shows.

    The title is the meeting and the body is when and where, in that order,
    because a notification is read in the corner of an eye and the first line
    is the only one guaranteed to be.
    """
    start = event.start()
    local = start.astimezone(times.local_zone()) if start else None
    when = "today" if event.all_day else (format_time(local) if local else "")
    minutes = _minutes_until(local) if local and not event.all_day else None
    if minutes is not None and minutes > 0:
        when = f"{when} — in {minutes} minute{'s' if minutes != 1 else ''}"
    elif minutes is not None:
        when = f"{when} — now"
    detail = " · ".join(part for part in (when, event.location) if part)
    return event.title, detail


def _minutes_until(when: dt.datetime) -> int:
    return int((when - times.now_local()).total_seconds() // 60)
