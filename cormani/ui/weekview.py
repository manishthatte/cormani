# SPDX-License-Identifier: GPL-3.0-or-later
#
# The week, with a time gutter and blocks in it.
#
# THREE PIECES, NOT ONE WIDGET. A week is a fixed header — the day names and
# the all-day strip — above a grid twenty-four hours tall that must scroll, and
# a single painted widget would either scroll the headers away or reimplement a
# scroll area. So the header and the grid are two painted widgets that share
# one selection, and `WeekView` is the container that keeps them agreeing.
#
# ALL-DAY EVENTS ARE NOT DRAWN IN THE GRID AND NEVER WILL BE. They have no
# hour to be at; drawing one as a block from midnight to midnight fills a
# column that the day's actual meetings need, and drawing it at the top of the
# hour grid means it scrolls away. The strip is the answer every calendar has
# arrived at, and `store/times.py` explains why the two kinds are different in
# the first place.
#
# OVERLAPPING MEETINGS SHARE THE WIDTH, BY CLUSTER. `lay_out` groups events
# that overlap into a cluster and gives each a column within it, so two
# meetings at ten o'clock are each half a day wide and a third at eleven that
# overlaps neither is full width. The alternative — dividing the day by the
# most crowded hour — makes every event in a busy day narrow.
#
# THE NOW LINE IS DRAWN ONLY WHEN TODAY IS IN VIEW, and it is the one thing on
# this grid that is not data from the store. It is worth it: a week view
# without one is a picture rather than a place, and finding "now" by reading
# the ruler is what people do instead.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from ..store import events as events_repo
from ..store import times
from .calendarbase import (CHIP_GAP, CHIP_HEIGHT, HOUR_HEIGHT, MINIMUM_BLOCK,
                           CalendarViewBase, format_time)

GUTTER = 58
SLOT_MINUTES = 30
# Where the grid is scrolled to when a week is opened. Not midnight: eight
# hours of empty night at the top is a view that always has to be scrolled
# before it is useful.
OPENS_AT_HOUR = 8


def lay_out(events) -> list:
    """(event, column, columns) for a day's timed events. See the header."""
    ordered = sorted(events, key=lambda e: (e.starts_at, e.ends_at, e.id))
    out: list = []
    cluster: list = []
    cluster_end = None
    for event in ordered:
        start, end = event.start(), event.end()
        if start is None:
            continue
        end = end if (end and end > start) else start + dt.timedelta(minutes=1)
        if cluster_end is not None and start >= cluster_end:
            out.extend(_columns(cluster))
            cluster, cluster_end = [], None
        cluster.append((event, start, end))
        cluster_end = end if cluster_end is None else max(cluster_end, end)
    out.extend(_columns(cluster))
    return out


def _columns(cluster: list) -> list:
    if not cluster:
        return []
    ends: list = []
    placed: list = []
    for event, start, end in cluster:
        for index, when in enumerate(ends):
            if when <= start:
                ends[index] = end
                placed.append((event, index))
                break
        else:
            ends.append(end)
            placed.append((event, len(ends) - 1))
    total = len(ends)
    return [(event, column, total) for event, column in placed]


class _Header(CalendarViewBase):
    """Day names, day numbers, and the all-day strip."""

    def __init__(self, days: int, parent=None) -> None:
        super().__init__(parent)
        self.days = days
        self._rows = 1

    def range(self) -> tuple:
        return _span(self._anchor, self.days, self._tz)

    def sizeHint(self):
        from PySide6.QtCore import QSize

        return QSize(400, self._wanted())

    def _wanted(self) -> int:
        return (self.fontMetrics().height() + 12
                + self._rows * (CHIP_HEIGHT + CHIP_GAP) + 4)

    def paintEvent(self, _moment) -> None:
        painter = QPainter(self)
        self._begin(painter)
        start, end = self.range()
        buckets = events_repo.by_day(
            [e for e in self._events if e.all_day], start, end, tz=self._tz)
        self._rows = max(1, max((len(v) for v in buckets.values()), default=1))
        if self.height() != self._wanted():
            self.setFixedHeight(self._wanted())
        width = (self.width() - GUTTER) / max(self.days, 1)
        title = self.fontMetrics().height() + 10
        painter.fillRect(QRect(0, 0, self.width(), title),
                         self._colour("surface_raised", "#eeeeee"))
        for column in range(self.days):
            day = start.date() + dt.timedelta(days=column)
            box = QRect(int(GUTTER + column * width), 0, int(width), title)
            font = QFont(painter.font())
            font.setBold(day == self._today)
            painter.setFont(font)
            painter.setPen(QPen(self._colour("accent", "#268bd2"))
                           if day == self._today else self._pen("text_strong"))
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter),
                             _day_label(day))
            for row, event in enumerate(buckets.get(day, ())):
                chip = QRect(int(GUTTER + column * width) + 1,
                             title + 2 + row * (CHIP_HEIGHT + CHIP_GAP),
                             int(width) - 2, CHIP_HEIGHT)
                self.draw_chip(painter, chip, event, show_time=False)
        painter.setPen(QPen(self._colour("border", "#cccccc")))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()


class _Grid(CalendarViewBase):
    """Twenty-four hours, and the blocks in them."""

    def __init__(self, days: int, parent=None) -> None:
        super().__init__(parent)
        self.days = days
        self.setFixedHeight(24 * HOUR_HEIGHT + 1)

    def range(self) -> tuple:
        return _span(self._anchor, self.days, self._tz)

    def _y(self, when: dt.datetime) -> int:
        local = when.astimezone(self._tz)
        return int((local.hour * 60 + local.minute) * HOUR_HEIGHT / 60)

    def paintEvent(self, _moment) -> None:
        painter = QPainter(self)
        self._begin(painter)
        start, end = self.range()
        width = (self.width() - GUTTER) / max(self.days, 1)
        self._paint_ruler(painter, width)
        buckets = events_repo.by_day(
            [e for e in self._events if not e.all_day], start, end, tz=self._tz)
        for column in range(self.days):
            day = start.date() + dt.timedelta(days=column)
            left = int(GUTTER + column * width)
            self._paint_slots(day, left, int(width))
            for event, index, total in lay_out(buckets.get(day, ())):
                self._paint_block(painter, event, day, left, int(width),
                                  index, total)
        self._paint_now(painter, start, width)
        painter.end()

    def _paint_ruler(self, painter: QPainter, width: float) -> None:
        painter.setPen(QPen(self._colour("border", "#dddddd")))
        for hour in range(25):
            y = hour * HOUR_HEIGHT
            painter.drawLine(GUTTER, y, self.width(), y)
            if hour < 24:
                painter.setPen(self._pen("text_muted"))
                painter.drawText(QRect(0, y + 2, GUTTER - 6, 16),
                                 int(Qt.AlignmentFlag.AlignRight),
                                 format_time(dt.datetime(2000, 1, 1, hour, 0)))
                painter.setPen(QPen(self._colour("border", "#dddddd")))
        for column in range(self.days + 1):
            x = int(GUTTER + column * width)
            painter.drawLine(x, 0, x, self.height())

    def _paint_slots(self, day: dt.date, left: int, width: int) -> None:
        for minute in range(0, 24 * 60, SLOT_MINUTES):
            when = dt.datetime.combine(day, dt.time(minute // 60, minute % 60),
                                       self._tz)
            top = int(minute * HOUR_HEIGHT / 60)
            self._slots.append((QRect(left, top, width,
                                      int(SLOT_MINUTES * HOUR_HEIGHT / 60)),
                                when))

    def _paint_block(self, painter: QPainter, event, day: dt.date, left: int,
                     width: int, index: int, total: int) -> None:
        start, end = event.start(self._tz), event.end(self._tz)
        if start is None:
            return
        midnight = dt.datetime.combine(day, dt.time(0, 0), self._tz)
        top = self._y(start) if start >= midnight else 0
        bottom = (self._y(end) if end and end.astimezone(self._tz).date() == day
                  else 24 * HOUR_HEIGHT)
        if end is not None and end <= start:
            bottom = top
        height = max(bottom - top, MINIMUM_BLOCK)
        column_width = max(int(width / max(total, 1)) - 2, 24)
        self.draw_chip(painter,
                       QRect(left + 1 + index * (column_width + 1), top,
                             column_width, height), event)

    def _paint_now(self, painter: QPainter, start: dt.datetime,
                   width: float) -> None:
        now = times.now_local().astimezone(self._tz)
        offset = (now.date() - start.date()).days
        if not 0 <= offset < self.days:
            return
        y = self._y(now)
        painter.setPen(QPen(self._colour("error", "#dc322f"), 2))
        painter.drawLine(int(GUTTER + offset * width), y,
                         int(GUTTER + (offset + 1) * width), y)


def _span(anchor: dt.date, days: int, tz) -> tuple:
    first = (times.week_start(anchor) if days > 1 else anchor)
    start = dt.datetime.combine(first, dt.time(0, 0), tz)
    return start, start + dt.timedelta(days=days)


def _day_label(day: dt.date) -> str:
    try:
        from PySide6.QtCore import QLocale

        name = QLocale.system().dayName(day.isoweekday(),
                                        QLocale.FormatType.ShortFormat)
    except Exception:                                        # pragma: no cover
        name = day.strftime("%a")
    return f"{name} {day.day}"


class WeekView(QWidget):
    """The header and the grid, kept agreeing. The pane sees one widget."""

    event_selected = Signal(int)
    event_activated = Signal(int)
    slot_activated = Signal(object)
    range_requested = Signal(object)

    def __init__(self, parent=None, *, days: int = 7) -> None:
        super().__init__(parent)
        self.days = days
        self.header = _Header(days, self)
        self.grid = _Grid(days, self)
        self.scroll = QScrollArea(self)
        self.scroll.setWidget(self.grid)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.scroll, 1)
        self._opened = False
        self._propagating = False

        for child in (self.header, self.grid):
            child.event_selected.connect(self._selected)
            child.event_activated.connect(self.event_activated)
            child.slot_activated.connect(self.slot_activated)
            child.range_requested.connect(self.range_requested)

    # The pane drives all three views through the same names.
    def set_events(self, events, calendars: dict) -> None:
        self.header.set_events(events, calendars)
        self.grid.set_events(events, calendars)

    def set_anchor(self, day, *, today=None) -> None:
        self.header.set_anchor(day, today=today)
        self.grid.set_anchor(day, today=today)
        self._scroll_to_the_working_day()

    def set_theme(self, theme) -> None:
        self.header.set_theme(theme)
        self.grid.set_theme(theme)

    def set_timezone(self, tz) -> None:
        self.header.set_timezone(tz)
        self.grid.set_timezone(tz)

    def anchor(self):
        return self.grid.anchor()

    def range(self) -> tuple:
        return self.grid.range()

    def selected_id(self) -> int:
        return self.grid.selected_id()

    def selected_event(self):
        return self.grid.selected_event() or self.header.selected_event()

    def select_event(self, event_id: int) -> None:
        self.header.select_event(event_id)
        self.grid.select_event(event_id)

    def _selected(self, event_id: int) -> None:
        """One selection across two widgets, and ONE signal out of them.

        The guard is not belt and braces. `select_event` already refuses to
        emit when the value has not changed, which stops the two children
        calling each other for ever — but the second child DOES change, and
        its own signal re-enters this slot, so the composite emitted twice for
        one selection. A listener that counted, or that reloaded a pane, did
        the work twice.
        """
        if self._propagating:
            return
        self._propagating = True
        try:
            self.header.select_event(event_id)
            self.grid.select_event(event_id)
        finally:
            self._propagating = False
        self.event_selected.emit(event_id)

    def _scroll_to_the_working_day(self) -> None:
        if self._opened:
            return
        self._opened = True
        self.scroll.verticalScrollBar().setValue(OPENS_AT_HOUR * HOUR_HEIGHT)
