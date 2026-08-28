# SPDX-License-Identifier: GPL-3.0-or-later
#
# The agenda: everything in the range, as a list, densest first.
#
# IT SHOWS THE SAME RANGE AS THE MONTH VIEW, and that is a navigation decision
# rather than an arbitrary span. If the agenda covered "the next thirty days"
# then switching from month to agenda would move the user in time without their
# asking, and the arrows above would step by a different unit in each view. One
# range, three drawings of it.
#
# EMPTY DAYS ARE NOT LISTED. A month view must draw the gaps — that is what
# makes it a shape — and a list must not: thirty rows saying "nothing" is the
# useless half of a calendar, and the whole reason to look at an agenda is to
# see what is actually there.
#
# A ROW IS TWO LINES AND THE SECOND ONE IS OPTIONAL. The time and the title,
# then the place and the calendar it came from — the same three-line-row
# thinking as `ui/messagerow.py`, one line shorter because an event has less to
# say than a message.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from ..store import events as events_repo
from ..store import times
from .calendarbase import CalendarViewBase, span_label

HEADING = 26
ROW = 38
DOT = 8
TIME_WIDTH = 118


class _List(CalendarViewBase):
    """The rows themselves. Its height is what it needs, and no more."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout: list = []             # (kind, date|event, QRect)
        self._span = None

    def set_span(self, start, end) -> None:
        """Draw this range instead of the anchor's month.

        What the agenda PANE beside the mail uses: "today and the next few
        days" is not a month, and giving the same rows two containers is
        cheaper than a second widget that draws them the same way.
        """
        self._span = (start, end)
        self.updateGeometry()
        self.setMinimumHeight(self.sizeHint().height())
        self.update()

    def range(self) -> tuple:
        if self._span is not None:
            return self._span
        start, end = times.month_bounds(self._anchor, self._tz)
        return start, end

    def rows(self) -> list:
        """The day headings and event rows, in order. Computed, not painted.

        Separate from the painting so that the height can be known before a
        paint — a scrolled widget that measured itself while drawing would be
        one frame late for ever.
        """
        start, end = self.range()
        buckets = events_repo.by_day(self._events, start, end, tz=self._tz)
        out: list = []
        for day in sorted(buckets):
            events = buckets[day]
            if not events:
                continue
            out.append(("day", day))
            out.extend(("event", event) for event in events)
        return out

    def sizeHint(self) -> QSize:
        rows = self.rows()
        height = sum(HEADING if kind == "day" else ROW for kind, _ in rows)
        return QSize(320, max(height, 40))

    def set_events(self, events, calendars: dict) -> None:
        super().set_events(events, calendars)
        self.updateGeometry()
        self.setMinimumHeight(self.sizeHint().height())

    def set_anchor(self, day, *, today=None) -> None:
        super().set_anchor(day, today=today)
        self.updateGeometry()
        self.setMinimumHeight(self.sizeHint().height())

    def paintEvent(self, _moment) -> None:
        painter = QPainter(self)
        self._begin(painter)
        rows = self.rows()
        if not rows:
            painter.setPen(self._pen("text_muted"))
            painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter),
                             "Nothing in this range")
            painter.end()
            return
        y = 0
        for kind, item in rows:
            if kind == "day":
                self._paint_day(painter, item, y)
                y += HEADING
            else:
                self._paint_event(painter, item, y)
                y += ROW
        painter.end()

    def _paint_day(self, painter: QPainter, day: dt.date, y: int) -> None:
        rect = QRect(0, y, self.width(), HEADING)
        painter.fillRect(rect, self._colour("surface_raised", "#eeeeee"))
        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(self._colour("accent", "#268bd2"))
                       if day == self._today else self._pen("text_strong"))
        painter.drawText(rect.adjusted(10, 0, -10, 0),
                         int(Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft), _long_day(day))
        font.setBold(False)
        painter.setFont(font)
        self._slots.append((QRect(rect), dt.datetime.combine(
            day, dt.time(9, 0), self._tz)))

    def _paint_event(self, painter: QPainter, event, y: int) -> None:
        rect = QRect(0, y, self.width(), ROW)
        if event.id == self._selected:
            painter.fillRect(rect, self._colour("accent_muted", "#dddddd"))
        colour = self.colour_for(event)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._colour("text_muted") if
                         event.my_response == "declined" else _brush(colour))
        painter.drawEllipse(10, y + (ROW - DOT) // 2, DOT, DOT)

        painter.setPen(self._pen("text_muted"))
        painter.drawText(QRect(26, y, TIME_WIDTH, ROW),
                         int(Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft),
                         span_label(event, self._tz))

        left = 26 + TIME_WIDTH
        width = max(self.width() - left - 12, 40)
        metrics = painter.fontMetrics()
        start = event.start(self._tz)
        on_deadline = (start is not None
                       and start.date().isoformat() in self._deadline_dates)
        painter.setPen(self._pen("deadline" if on_deadline else "text_strong"))
        font = QFont(painter.font())
        font.setItalic(bool(event.pending))
        painter.setFont(font)
        title = event.title + (" ?" if event.needs_reply else "")
        painter.drawText(QRect(left, y + 3, width, metrics.height()),
                         int(Qt.AlignmentFlag.AlignLeft),
                         metrics.elidedText(title, Qt.TextElideMode.ElideRight,
                                            width))
        font.setItalic(False)
        painter.setFont(font)
        detail = " · ".join(part for part in (
            event.location, self._calendar_label(event)) if part)
        if detail:
            painter.setPen(self._pen("text_muted"))
            painter.drawText(
                QRect(left, y + 3 + metrics.height(), width, metrics.height()),
                int(Qt.AlignmentFlag.AlignLeft),
                metrics.elidedText(detail, Qt.TextElideMode.ElideRight, width))
        self._hits.append((QRect(rect), event.id))

    def _calendar_label(self, event) -> str:
        calendar = self._calendars.get(event.calendar_id)
        return calendar.label if calendar else ""


def _brush(colour: str):
    from PySide6.QtGui import QColor

    return QColor(colour)


def _long_day(day: dt.date) -> str:
    try:
        from PySide6.QtCore import QLocale

        locale = QLocale.system()
        return (f"{locale.dayName(day.isoweekday(), QLocale.FormatType.LongFormat)}"
                f", {day.day} "
                f"{locale.monthName(day.month, QLocale.FormatType.LongFormat)}")
    except Exception:                                        # pragma: no cover
        return day.strftime("%A, %d %B")


class AgendaView(QWidget):
    """The list in a scroll area. The pane sees the same names as the others."""

    event_selected = Signal(int)
    event_activated = Signal(int)
    slot_activated = Signal(object)
    range_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.list = _List(self)
        self.scroll = QScrollArea(self)
        self.scroll.setWidget(self.list)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)
        self.list.event_selected.connect(self.event_selected)
        self.list.event_activated.connect(self.event_activated)
        self.list.slot_activated.connect(self.slot_activated)
        self.list.range_requested.connect(self.range_requested)

    def set_events(self, events, calendars: dict) -> None:
        self.list.set_events(events, calendars)

    def set_anchor(self, day, *, today=None) -> None:
        self.list.set_anchor(day, today=today)

    def set_theme(self, theme) -> None:
        self.list.set_theme(theme)

    def set_timezone(self, tz) -> None:
        self.list.set_timezone(tz)

    def anchor(self):
        return self.list.anchor()

    def range(self) -> tuple:
        return self.list.range()

    def selected_id(self) -> int:
        return self.list.selected_id()

    def selected_event(self):
        return self.list.selected_event()

    def select_event(self, event_id: int) -> None:
        self.list.select_event(event_id)
