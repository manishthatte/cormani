# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing an event down.
#
# THE INCLUSIVE END IS THE WHOLE OF THIS DIALOG'S DIFFICULTY. A person asked
# when a three-day trip ends says the third day; both providers, and this
# store, record the day AFTER — an all-day end is exclusive, which is what
# makes a one-day event start on the 8th and end on the 9th. So the dialog
# shows the inclusive date and converts on the way in and on the way out, in
# one place, named. Every calendar that has ever shipped an off-by-one has
# shipped it here.
#
# A TIMED EVENT IS ENTERED IN LOCAL TIME AND STORED IN UTC. `store/times.py`
# owns the conversion; nothing in this file does arithmetic on a zone.
#
# THE CALENDAR CHOICE IS FIXED ONCE THE EVENT EXISTS. Moving an event between
# calendars is a delete and a create on both providers — `store/eventedits.py`
# refuses it as one action and says why — so the combo is disabled when
# editing rather than offering something that would silently not happen.
#
# READ-ONLY CALENDARS ARE NOT OFFERED AT ALL. A shared holiday feed cannot
# take an event, and finding that out after typing one is the interface
# failing at the last possible moment.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import email.utils

from PySide6.QtCore import QDate, QTime, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog,
                               QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QTimeEdit, QVBoxLayout,
                               QWidget)

from ..store import calendars as calendars_repo
from ..store import times

# The reminders offered, in minutes. None is "the calendar's own default",
# which is a real answer and not an absence: both providers have one, and
# `calendar.default_reminder` carries it.
REMINDERS = ((None, "The calendar's default"), (0, "At the time of the event"),
             (5, "5 minutes before"), (10, "10 minutes before"),
             (15, "15 minutes before"), (30, "30 minutes before"),
             (60, "1 hour before"), (120, "2 hours before"),
             (24 * 60, "1 day before"))

DEFAULT_MINUTES = 60


def inclusive_end(stored: str) -> dt.date | None:
    """The last day of an all-day event, from the exclusive date stored."""
    day = times.parse_date(stored)
    return (day - dt.timedelta(days=1)) if day else None


def exclusive_end(shown: dt.date) -> str:
    """The stored end, from the last day a person named."""
    return (shown + dt.timedelta(days=1)).isoformat()


class EventDialog(QDialog):
    """New event, or an existing one. `values()` is what the store takes."""

    def __init__(self, con, *, account_id: int | None = None, event=None,
                 calendar_id: int | None = None,
                 when: dt.datetime | None = None, parent: QWidget | None = None,
                 tz=None) -> None:
        super().__init__(parent)
        self._con = con
        self._event = event
        self._tz = tz or times.local_zone()
        self.setWindowTitle("Edit event" if event else "New event")
        self.setModal(True)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addLayout(form)

        self.summary = QLineEdit(self)
        self.summary.setPlaceholderText("What is it?")
        form.addRow("Title", self.summary)

        self.calendar = QComboBox(self)
        for calendar in calendars_repo.list_calendars(con, account_id):
            if calendar.writable:
                self.calendar.addItem(_calendar_label(con, calendar),
                                      calendar.id)
        form.addRow("Calendar", self.calendar)

        self.all_day = QCheckBox("All day", self)
        form.addRow("", self.all_day)

        self.start_date = QDateEdit(self)
        self.start_time = QTimeEdit(self)
        self.end_date = QDateEdit(self)
        self.end_time = QTimeEdit(self)
        for widget in (self.start_date, self.end_date):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("ddd d MMM yyyy")
        form.addRow("Starts", _pair(self.start_date, self.start_time))
        form.addRow("Ends", _pair(self.end_date, self.end_time))

        self.location = QLineEdit(self)
        form.addRow("Where", self.location)

        self.guests = QLineEdit(self)
        self.guests.setPlaceholderText("addresses, separated by commas")
        form.addRow("Guests", self.guests)

        self.reminder = QComboBox(self)
        for minutes, label in REMINDERS:
            self.reminder.addItem(label, minutes)
        form.addRow("Remind", self.reminder)

        self.busy = QCheckBox("Shows me as busy", self)
        self.busy.setChecked(True)
        form.addRow("", self.busy)

        self.description = QPlainTextEdit(self)
        self.description.setPlaceholderText("Notes")
        self.description.setMinimumHeight(90)
        form.addRow("Notes", self.description)

        self.warning = QLabel("", self)
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.all_day.toggled.connect(self._all_day_toggled)
        self.start_date.dateChanged.connect(self._keep_the_end_after_the_start)
        self.start_time.timeChanged.connect(self._keep_the_end_after_the_start)

        if event is not None:
            self._load(event)
        else:
            self._blank(calendar_id, when)
        self._all_day_toggled(self.all_day.isChecked())

    # ------------------------------------------------------------- filling
    def _blank(self, calendar_id: int | None, when: dt.datetime | None) -> None:
        when = (when or times.now_local()).astimezone(self._tz)
        # To the next half hour. An event proposed at 14:23 is one the user has
        # to correct every time.
        when = when.replace(second=0, microsecond=0)
        when += dt.timedelta(minutes=(30 - when.minute % 30) % 30)
        self._set_start(when)
        self._set_end(when + dt.timedelta(hours=1))
        self.reminder.setCurrentIndex(
            [m for m, _ in REMINDERS].index(DEFAULT_MINUTES))
        if calendar_id is not None:
            index = self.calendar.findData(int(calendar_id))
            if index >= 0:
                self.calendar.setCurrentIndex(index)

    def _load(self, event) -> None:
        self.summary.setText(event.summary)
        self.location.setText(event.location)
        self.description.setPlainText(event.description)
        self.busy.setChecked(event.busy)
        self.all_day.setChecked(event.all_day)
        index = self.calendar.findData(event.calendar_id)
        if index >= 0:
            self.calendar.setCurrentIndex(index)
        # See the module header: the calendar an event is in cannot change.
        self.calendar.setEnabled(False)
        start = event.start(self._tz)
        if start:
            self._set_start(start.astimezone(self._tz))
        if event.all_day:
            last = inclusive_end(event.ends_at)
            if last:
                self.end_date.setDate(QDate(last.year, last.month, last.day))
        else:
            end = event.end(self._tz)
            if end:
                self._set_end(end.astimezone(self._tz))
        self.guests.setText(", ".join(
            email.utils.formataddr((g.name, g.address))
            for g in event.attendees if not g.is_self))
        minutes = [m for m, _ in REMINDERS]
        self.reminder.setCurrentIndex(
            minutes.index(event.reminder) if event.reminder in minutes else 0)

    def _set_start(self, when: dt.datetime) -> None:
        self.start_date.setDate(QDate(when.year, when.month, when.day))
        self.start_time.setTime(QTime(when.hour, when.minute))

    def _set_end(self, when: dt.datetime) -> None:
        self.end_date.setDate(QDate(when.year, when.month, when.day))
        self.end_time.setTime(QTime(when.hour, when.minute))

    # ------------------------------------------------------------ behaviour
    def _all_day_toggled(self, on: bool) -> None:
        self.start_time.setVisible(not on)
        self.end_time.setVisible(not on)
        self.reminder.setEnabled(True)

    def _keep_the_end_after_the_start(self, *_args) -> None:
        """Drag the end along with the start, as every calendar does.

        Only when it would otherwise be before: a user who has deliberately
        made a two-hour meeting must not have it snapped back to one because
        they then moved the start.
        """
        start, end = self._starts(), self._ends()
        if end <= start:
            moved = start + dt.timedelta(days=1 if self.all_day.isChecked()
                                         else 0, hours=0 if
                                         self.all_day.isChecked() else 1)
            self._set_end(moved)

    def _starts(self) -> dt.datetime:
        day = self.start_date.date()
        time = QTime(0, 0) if self.all_day.isChecked() else self.start_time.time()
        return dt.datetime(day.year(), day.month(), day.day(),
                           time.hour(), time.minute(), tzinfo=self._tz)

    def _ends(self) -> dt.datetime:
        day = self.end_date.date()
        time = QTime(0, 0) if self.all_day.isChecked() else self.end_time.time()
        return dt.datetime(day.year(), day.month(), day.day(),
                           time.hour(), time.minute(), tzinfo=self._tz)

    # --------------------------------------------------------------- answer
    def values(self) -> dict:
        """The dialog as the arguments `store/eventedits` takes."""
        all_day = self.all_day.isChecked()
        starts = self._starts()
        ends = self._ends()
        return {
            "calendar_id": self.calendar.currentData(),
            "summary": self.summary.text().strip(),
            "location": self.location.text().strip(),
            "description": self.description.toPlainText().strip(),
            "all_day": all_day,
            "busy": self.busy.isChecked(),
            "reminder": self.reminder.currentData(),
            "starts_at": (starts.date().isoformat() if all_day
                          else times.to_utc_text(starts)),
            "ends_at": (exclusive_end(ends.date()) if all_day
                        else times.to_utc_text(ends)),
            "attendees": guests_from(self.guests.text()),
        }

    def accept(self) -> None:
        """Refuse the two things that cannot be saved, and say which."""
        if not self.summary.text().strip():
            self._refuse("An event needs a title.")
            return
        if self.calendar.currentData() is None:
            self._refuse("There is no calendar here that can be written to. "
                         "A calendar shared read-only cannot take an event.")
            return
        if self._ends() < self._starts():
            self._refuse("The end is before the start.")
            return
        unusable = invalid_guests(self.guests.text())
        if unusable:
            self._refuse(f"{', '.join(unusable)} is not an address. Put a "
                         f"display name containing a comma in quotes.")
            return
        super().accept()

    def _refuse(self, message: str) -> None:
        self.warning.setText(message)
        if self.isVisible():                                 # pragma: no cover
            QMessageBox.warning(self, "corMani", message)


def _addresses(text: str) -> list:
    """`email.utils.getaddresses`, which is the only correct parser for this.

    It handles the case a comma-split gets wrong — a QUOTED display name with
    a comma in it, `"Baker, Frances" <f@x.com>` — and it treats an UNQUOTED
    comma as a separator, which is what the standard says and what the
    recipient's own client would do. That second case is why `invalid_guests`
    exists: `Baker, Frances <f@x.com>` parses into a guest called `Baker` with
    no address, and the user has to be told rather than have it dropped.
    """
    return [(name.strip(), address.strip())
            for name, address in email.utils.getaddresses([text or ""])
            if address.strip() or name.strip()]


def guests_from(text: str) -> list:
    """The addresses in a line of typing, as attendee dictionaries."""
    return [{"name": name, "address": address}
            for name, address in _addresses(text) if "@" in address]


def invalid_guests(text: str) -> list:
    """What was typed in the guest field and is not an address.

    Reported rather than skipped. A meeting sent to four of the five people
    the user named is worse than one that would not save, because nobody finds
    out until the fifth person does not arrive.
    """
    return [address or name for name, address in _addresses(text)
            if "@" not in address]


def _pair(first: QWidget, second: QWidget) -> QWidget:
    from PySide6.QtWidgets import QHBoxLayout

    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(first)
    layout.addWidget(second)
    layout.addStretch(1)
    return box


def _calendar_label(con, calendar) -> str:
    """The calendar's name, and whose it is when there is more than one
    account — fifteen accounts each with a "Calendar" is a combo of
    indistinguishable entries."""
    row = con.execute("SELECT address FROM account WHERE id = ?",
                      (calendar.account_id,)).fetchone()
    return f"{calendar.label} — {row['address']}" if row else calendar.label
