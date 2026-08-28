# SPDX-License-Identifier: GPL-3.0-or-later
#
# The month, as six weeks of seven days.
#
# SIX ROWS ALWAYS, never five. `store/times.month_grid` computes it and the
# reason is in that file: a grid whose height changes as the user pages through
# the year makes every cell jump, and the alternative — cells that grow taller
# in a five-week month — moves everything anyway. A fixed frame is what makes
# paging through twelve months readable.
#
# A CELL SHOWS WHAT FITS AND SAYS WHAT IT HID. "+3 more" is not a decoration:
# a month cell is forty pixels tall and a busy Tuesday has nine things in it,
# so something must be dropped, and the only unacceptable answer is dropping
# them silently. Clicking the line asks the pane for that day — CONVENTIONS.txt
# §8 in one label.
#
# THE DAYS OUTSIDE THIS MONTH ARE DRAWN, AND DRAWN QUIETLY. They are real days
# with real events, and a grid that blanked them would hide the meeting on the
# first of next month from somebody looking at the last week of this one. Muted
# text is the whole of the distinction.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..store import events as events_repo
from ..store import times
from .calendarbase import CHIP_GAP, CHIP_HEIGHT, DAY_HEADER, CalendarViewBase

WEEKS = 6
DAYS = 7

# Where a double-click on empty space puts a new event. Nine in the morning
# rather than midnight: a person clicking on a day means "something that day",
# and every calendar makes the same assumption because the alternative is an
# event at 00:00 that has to be moved every time.
NEW_EVENT_HOUR = 9


def weekday_names(first_weekday: int = times.FIRST_WEEKDAY) -> list:
    """Short day names in the desktop's own language, starting where it does."""
    try:
        from PySide6.QtCore import QLocale

        locale = QLocale.system()
        names = [locale.dayName((n % 7) + 1, QLocale.FormatType.ShortFormat)
                 for n in range(7)]
    except Exception:                                        # pragma: no cover
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return [names[(first_weekday + n) % 7] for n in range(7)]


class MonthView(CalendarViewBase):
    """A month grid. Chips in cells, and the truth about what did not fit."""

    def __init__(self, parent: QWidget | None = None, *,
                 first_weekday: int = times.FIRST_WEEKDAY) -> None:
        super().__init__(parent)
        self._first_weekday = first_weekday
        self._more: list = []               # (QRect, date) for the "+N more"
        self.setMinimumHeight(WEEKS * (DAY_HEADER + CHIP_HEIGHT * 2) + 24)

    def set_first_weekday(self, first: int) -> None:
        self._first_weekday = int(first) % 7
        self.update()

    # ------------------------------------------------------------- geometry
    def range(self) -> tuple:
        start, end = times.month_grid(self._anchor, first_weekday=self._first_weekday)
        first = dt.datetime.combine(start, dt.time(0, 0), self._tz)
        return first, dt.datetime.combine(end, dt.time(0, 0), self._tz)

    def days(self) -> list:
        start, _ = times.month_grid(self._anchor, first_weekday=self._first_weekday)
        return [start + dt.timedelta(days=n) for n in range(WEEKS * DAYS)]

    def _cell(self, index: int) -> QRect:
        top = self._header_height()
        width = self.width() / DAYS
        height = max((self.height() - top) / WEEKS, 1)
        row, column = divmod(index, DAYS)
        return QRect(int(column * width), int(top + row * height),
                     int(width) - 1, int(height) - 1)

    def _header_height(self) -> int:
        return self.fontMetrics().height() + 8

    def cell_at(self, point) -> dt.date | None:
        for index, day in enumerate(self.days()):
            if self._cell(index).contains(point):
                return day
        return None

    # ------------------------------------------------------------- painting
    def paintEvent(self, _moment) -> None:
        painter = QPainter(self)
        self._begin(painter)
        self._more.clear()
        buckets = events_repo.by_day(self._events, *self.range(), tz=self._tz)
        self._paint_header(painter)
        for index, day in enumerate(self.days()):
            self._paint_cell(painter, index, day, buckets.get(day, ()))
        painter.end()

    def _paint_header(self, painter: QPainter) -> None:
        height = self._header_height()
        painter.fillRect(QRect(0, 0, self.width(), height),
                         self._colour("surface_raised", "#eeeeee"))
        painter.setPen(self._pen("text_muted"))
        width = self.width() / DAYS
        for column, name in enumerate(weekday_names(self._first_weekday)):
            painter.drawText(QRect(int(column * width), 0, int(width), height),
                             int(Qt.AlignmentFlag.AlignCenter), name)

    def _paint_cell(self, painter: QPainter, index: int, day: dt.date,
                    events) -> None:
        rect = self._cell(index)
        this_month = day.month == self._anchor.month
        if not this_month:
            painter.fillRect(rect, self._colour("surface_sunken", "#f4f4f4"))
        painter.setPen(QPen(self._colour("border", "#cccccc")))
        painter.drawRect(rect)

        number = QRect(rect.left(), rect.top(), rect.width() - 4, DAY_HEADER)
        font = QFont(painter.font())
        font.setBold(day == self._today)
        painter.setFont(font)
        painter.setPen(self._pen("text_strong" if this_month else "text_muted"))
        if day == self._today:
            painter.setPen(QPen(self._colour("accent", "#268bd2")))
        painter.drawText(number, int(Qt.AlignmentFlag.AlignRight
                                     | Qt.AlignmentFlag.AlignVCenter),
                         str(day.day))
        font.setBold(False)
        painter.setFont(font)

        top = rect.top() + DAY_HEADER
        room = max((rect.bottom() - top) // (CHIP_HEIGHT + CHIP_GAP), 0)
        shown = list(events)
        # One line is given up to say how many were hidden, and only when
        # hiding actually happens: with exactly `room` events nothing is lost.
        if len(shown) > room and room:
            shown = shown[:max(room - 1, 0)]
        for offset, event in enumerate(shown):
            chip = QRect(rect.left() + 2, top + offset * (CHIP_HEIGHT + CHIP_GAP),
                         rect.width() - 4, CHIP_HEIGHT)
            self.draw_chip(painter, chip, event)
        hidden = len(events) - len(shown)
        if hidden > 0:
            line = QRect(rect.left() + 2,
                         top + len(shown) * (CHIP_HEIGHT + CHIP_GAP),
                         rect.width() - 4, CHIP_HEIGHT)
            painter.setPen(self._pen("text_muted"))
            painter.drawText(line, int(Qt.AlignmentFlag.AlignVCenter
                                       | Qt.AlignmentFlag.AlignLeft),
                             f"  +{hidden} more")
            self._more.append((QRect(line), day))
        # The whole cell is a place a new event can be made.
        self._slots.append((QRect(rect), dt.datetime.combine(
            day, dt.time(NEW_EVENT_HOUR, 0), self._tz)))

    # ------------------------------------------------------------ the mouse
    def mousePressEvent(self, moment) -> None:
        point = moment.position().toPoint()
        for rect, day in self._more:
            if rect.contains(point):
                self.range_requested.emit(day)
                return
        super().mousePressEvent(moment)

    def keyPressEvent(self, moment) -> None:
        """Paging is the month's own gesture; the base class owns the rest."""
        key = moment.key()
        if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
            months = 1 if key == Qt.Key.Key_PageDown else -1
            first = times.month_start(self._anchor)
            total = first.year * 12 + first.month - 1 + months
            self.range_requested.emit(dt.date(total // 12, total % 12 + 1, 1))
            return
        super().keyPressEvent(moment)
