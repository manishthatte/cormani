# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the three calendar views share.
#
# Month, week and agenda are three drawings of one question — what is in this
# range — and everything they have in common is here: the events they hold, the
# selection, the colours, and the arithmetic that turns a time into a place on
# the screen. The alternative is three copies of the same eighty lines, which
# is how two views end up disagreeing about which day an event is on.
#
# THEY ARE DRAWN, NOT LAID OUT. The same decision `ui/messagerow.py` records: a
# week of a busy calendar is sixty overlapping blocks and a month grid is
# forty-two cells of chips, and a widget per event would be several hundred
# objects rebuilt on every navigation. One `paintEvent` costs nothing and the
# hit test is arithmetic.
#
# A CHIP'S COLOUR IS THE CALENDAR'S AND ITS TEXT COLOUR IS DERIVED FROM IT.
# `ui/theme.py` says nothing in the interface may name a colour, and this is
# the honest exception rather than a violation: a calendar's colour is DATA —
# the provider's, or the user's own choice — so what the theme cannot supply is
# whether black or white can be read on top of it. That is computed from the
# luminance, once, here.
#
# THE HIT TEST AND THE PAINTING USE THE SAME RECTANGLES. `_hits` is filled
# during painting and read by the mouse handlers, which is `messagerow.py`'s
# `action_rects` rule in another shape: two copies of that arithmetic is how a
# chip highlights under the cursor and opens its neighbour.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..store import times
from ..store.events import Event
from . import theme as theme_mod

# A chip's own metrics. Not in `density.py` because that file is about the
# three-line message row and its numbers mean something else; these are the
# calendar's and they are read the same way.
CHIP_HEIGHT = 18
CHIP_GAP = 2
CHIP_RADIUS = 3
DAY_HEADER = 22

# Minutes per row of the week grid's ruler, and how tall one hour is drawn.
HOUR_HEIGHT = 44
MINIMUM_BLOCK = 16


def luminance(colour: str) -> float:
    """Perceived brightness of a colour, 0 to 1.

    The sRGB coefficients rather than a mean: the eye is roughly twice as
    sensitive to green as to red and five times as sensitive to green as to
    blue, and a mean makes white text on a saturated blue chip unreadable while
    calling it a light background.
    """
    value = QColor(colour)
    if not value.isValid():
        return 1.0
    r, g, b = value.redF(), value.greenF(), value.blueF()
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def text_on(colour: str) -> QColor:
    """Black or white, whichever can be read on that fill."""
    return QColor("#101010") if luminance(colour) > 0.55 else QColor("#fdfdfd")


def format_time(when: dt.datetime | None, *, ampm: bool | None = None) -> str:
    """A time, in the form this desktop writes them.

    The system locale decides between 09:30 and 9:30 am, because a calendar
    that insists on one is a calendar half the world reads twice. `ampm` is
    the override, and exists so a test can assert a string rather than assert
    the machine it runs on.
    """
    if when is None:
        return ""
    if ampm is None:
        ampm = _locale_is_ampm()
    if not ampm:
        return f"{when.hour:02d}:{when.minute:02d}"
    hour = when.hour % 12 or 12
    suffix = "am" if when.hour < 12 else "pm"
    return (f"{hour}:{when.minute:02d} {suffix}" if when.minute
            else f"{hour} {suffix}")


def _locale_is_ampm() -> bool:
    try:
        from PySide6.QtCore import QLocale

        return "AP" in QLocale.system().timeFormat(QLocale.FormatType.ShortFormat)
    except Exception:                                        # pragma: no cover
        return False


def span_label(event: Event, tz: dt.tzinfo | None = None, *,
               ampm: bool | None = None) -> str:
    """"09:30 – 10:30", or "All day". What every view writes above a title."""
    if event.all_day:
        return "All day"
    start, end = event.start(tz), event.end(tz)
    if start is None:
        return ""
    if end is None or end <= start:
        return format_time(start, ampm=ampm)
    return (f"{format_time(start, ampm=ampm)} – "
            f"{format_time(end, ampm=ampm)}")


class CalendarViewBase(QWidget):
    """Events, a theme, a selection, and the range being shown."""

    event_selected = Signal(int)            # event id, or 0 for none
    event_activated = Signal(int)           # double click, or Return
    slot_activated = Signal(object)         # a datetime: "make one here"
    range_requested = Signal(object)        # a date: "show me this instead"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._events: list = []
        self._calendars: dict = {}
        self._theme = theme_mod.get(None)
        self._anchor = times.now_local().date()
        self._today = self._anchor
        self._selected = 0
        self._tz = times.local_zone()
        self._hits: list = []               # (QRect, event id) filled by paint
        self._slots: list = []              # (QRect, datetime) filled by paint
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------- contents
    def set_events(self, events, calendars: dict) -> None:
        """What to draw. `calendars` maps id to a `store.calendars.Calendar`."""
        self._events = list(events)
        self._calendars = dict(calendars)
        if self._selected and not any(e.id == self._selected
                                      for e in self._events):
            self._selected = 0
        self.update()

    def set_anchor(self, day: dt.date, *, today: dt.date | None = None) -> None:
        self._anchor = day
        self._today = today or times.now_local().date()
        self.update()

    def anchor(self) -> dt.date:
        return self._anchor

    def set_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def set_timezone(self, tz) -> None:
        self._tz = tz
        self.update()

    def selected_id(self) -> int:
        return self._selected

    def select_event(self, event_id: int) -> None:
        if event_id != self._selected:
            self._selected = int(event_id or 0)
            self.update()
            self.event_selected.emit(self._selected)

    def selected_event(self) -> Event | None:
        for event in self._events:
            if event.id == self._selected:
                return event
        return None

    def range(self) -> tuple:
        """The window this view draws, as two aware datetimes. Subclasses say."""
        raise NotImplementedError                            # pragma: no cover

    # -------------------------------------------------------------- colours
    def colour_for(self, event: Event) -> str:
        calendar = self._calendars.get(event.calendar_id)
        colour = calendar.display_colour if calendar else ""
        return colour or self._theme.accent or "#268bd2"

    def _pen(self, role: str) -> QPen:
        return QPen(QColor(getattr(self._theme, role) or "#000000"))

    def _colour(self, role: str, fallback: str = "#000000") -> QColor:
        return QColor(getattr(self._theme, role, "") or fallback)

    # ---------------------------------------------------------- chip drawing
    def draw_chip(self, painter: QPainter, rect: QRect, event: Event, *,
                  show_time: bool = True) -> None:
        """One event, as a filled chip with its title.

        An event the user has DECLINED is drawn hollow: it is still on the
        calendar — the organiser's, and the user's own record of having said no
        — and drawing it solid would make a day look busy with meetings nobody
        is attending.
        """
        colour = self.colour_for(event)
        declined = event.my_response == "declined"
        pending = bool(event.pending)
        painter.save()
        if declined:
            painter.setPen(QPen(QColor(colour), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1),
                                    CHIP_RADIUS, CHIP_RADIUS)
            painter.setPen(QPen(QColor(colour)))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawRoundedRect(rect, CHIP_RADIUS, CHIP_RADIUS)
            painter.setPen(QPen(text_on(colour)))
        if event.id == self._selected:
            painter.setPen(QPen(self._colour("text_strong"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1),
                                    CHIP_RADIUS, CHIP_RADIUS)
            painter.setPen(QPen(text_on(colour) if not declined
                                else QColor(colour)))

        font = QFont(painter.font())
        font.setItalic(pending)
        painter.setFont(font)
        text = event.title
        if show_time and not event.all_day:
            when = event.start(self._tz)
            if when is not None:
                text = f"{format_time(when)}  {text}"
        if event.needs_reply:
            # An invitation nobody has answered is the one thing on a calendar
            # that is a QUESTION rather than a fact, and it is marked as one.
            text = f"? {text}"
        painter.drawText(rect.adjusted(5, 0, -4, 0),
                         int(Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft),
                         painter.fontMetrics().elidedText(
                             text, Qt.TextElideMode.ElideRight,
                             max(rect.width() - 9, 10)))
        painter.restore()
        self._hits.append((QRect(rect), event.id))

    # ------------------------------------------------------------- the mouse
    def _hit(self, point) -> int:
        for rect, event_id in reversed(self._hits):
            if rect.contains(point):
                return event_id
        return 0

    def _slot(self, point):
        for rect, when in reversed(self._slots):
            if rect.contains(point):
                return when
        return None

    def mousePressEvent(self, moment) -> None:
        self.select_event(self._hit(moment.position().toPoint()))
        super().mousePressEvent(moment)

    def mouseDoubleClickEvent(self, moment) -> None:
        point = moment.position().toPoint()
        found = self._hit(point)
        if found:
            self.event_activated.emit(found)
            return
        when = self._slot(point)
        if when is not None:
            self.slot_activated.emit(when)

    def keyPressEvent(self, moment) -> None:
        key = moment.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._selected:
            self.event_activated.emit(self._selected)
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self._step(1 if key == Qt.Key.Key_Down else -1)
            return
        super().keyPressEvent(moment)

    def _step(self, delta: int) -> None:
        """Move the selection through the events in drawing order.

        Down and up rather than left and right: every one of the three views
        reads top to bottom, and a month grid's "next" is the next chip rather
        than the next day — which is what somebody pressing the key is looking
        at.
        """
        if not self._events:
            return
        order = [e.id for e in self._events]
        if self._selected in order:
            index = min(max(order.index(self._selected) + delta, 0),
                        len(order) - 1)
        else:
            index = 0 if delta > 0 else len(order) - 1
        self.select_event(order[index])

    # ------------------------------------------------------------- painting
    def _begin(self, painter: QPainter) -> None:
        self._hits.clear()
        self._slots.clear()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), self._colour("surface", "#ffffff"))
