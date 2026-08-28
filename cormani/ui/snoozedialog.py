# SPDX-License-Identifier: GPL-3.0-or-later
#
# Choosing when a snoozed message comes back.
#
# PRESETS FIRST, CUSTOM LAST. Later today, tomorrow and next week are what
# people reach for; a datetime picker is there for the rest and is one dialog
# rather than a calendar on the main window.
#
# THE DIALOGS ARE INJECTED for the same reason as ui/tagsdialog.py: Debian
# ships no QTest, and a modal a test cannot answer is a test that hangs.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (QDateTimeEdit, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from ..store import times


def _later_today(now: dt.datetime) -> dt.datetime:
    local = times.aware(now)
    hour = local.hour
    if hour < 9:
        target = local.replace(hour=9, minute=0, second=0, microsecond=0)
    elif hour < 13:
        target = local.replace(hour=13, minute=0, second=0, microsecond=0)
    elif hour < 17:
        target = local.replace(hour=17, minute=0, second=0, microsecond=0)
    else:
        target = (local + dt.timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0)
    if target <= local:
        target = local + dt.timedelta(hours=1)
    return target


def _tomorrow_morning(now: dt.datetime) -> dt.datetime:
    local = times.aware(now)
    day = (local + dt.timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    return day


def _next_week(now: dt.datetime) -> dt.datetime:
    local = times.aware(now)
    days_ahead = (7 - local.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    day = (local + dt.timedelta(days=days_ahead)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    return day


class SnoozeDialog(QDialog):
    """Pick when snoozed mail returns."""

    def __init__(self, parent=None, *, now: dt.datetime | None = None) -> None:
        super().__init__(parent)
        self._now = now or dt.datetime.now(dt.timezone.utc)
        self._until = ""
        self.setWindowTitle("Snooze until")
        self.setMinimumWidth(360)

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("Take it off the list and bring it back:", self))

        presets = QHBoxLayout()
        for label, maker in (
                ("Later today", _later_today),
                ("Tomorrow", _tomorrow_morning),
                ("Next week", _next_week)):
            button = QPushButton(label, self)
            button.clicked.connect(lambda _=False, m=maker: self._choose(m(self._now)))
            presets.addWidget(button)
        outer.addLayout(presets)

        form = QFormLayout()
        self.custom = QDateTimeEdit(self)
        self.custom.setCalendarPopup(True)
        self.custom.setDisplayFormat("ddd d MMM yyyy, HH:mm")
        local_now = times.aware(self._now)
        self.custom.setDateTime(QDateTime(local_now))
        self.custom.setMinimumDateTime(QDateTime(local_now))
        form.addRow("Custom:", self.custom)
        outer.addLayout(form)

        custom_row = QHBoxLayout()
        custom_row.addStretch(1)
        pick = QPushButton("Use custom time", self)
        pick.clicked.connect(self._choose_custom)
        custom_row.addWidget(pick)
        outer.addLayout(custom_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel, parent=self)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _choose(self, when: dt.datetime) -> None:
        self._until = times.to_utc_text(when)
        self.accept()

    def _choose_custom(self) -> None:
        qdt = self.custom.dateTime()
        when = qdt.toPython()
        if when.tzinfo is None:
            when = when.replace(tzinfo=times.local_zone())
        self._choose(when)

    @property
    def until(self) -> str:
        return self._until


def ask(parent, *, now: dt.datetime | None = None) -> str:
    """The UTC ISO time chosen, or '' if the dialog was cancelled."""
    dialog = SnoozeDialog(parent, now=now)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return ""
    return dialog.until
