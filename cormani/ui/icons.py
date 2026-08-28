# SPDX-License-Identifier: GPL-3.0-or-later
#
# Icons, drawn rather than shipped.
#
# Two options were rejected before this one. A bundled icon set is a vendored
# dependency, which CONVENTIONS.txt §3 forbids and Debian would reject. Unicode
# glyphs — the envelopes and paperclips a mock-up uses — depend on which emoji
# font is installed, render at the wrong weight beside text, and are coloured by
# the font rather than by the theme; on a machine without the font they are
# empty boxes.
#
# So they are paths, drawn at the size and in the colour asked for. That is what
# makes them themeable at all: an icon in a hover action must be the theme's
# muted text colour until the row is hovered and its accent colour after, and a
# bitmap cannot do that without shipping two of everything.
#
# Every glyph is defined in a 24x24 box and scaled to the rectangle given. The
# pen width is in box units, so it thins as the icon shrinks, which is what
# keeps a 13px compact-density icon from looking like a blob.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap)

BOX = 24.0
_STROKE = 1.9


def _stadium(painter: QPainter, cx: float, cy: float, half: float,
             radius: float, angle: float) -> None:
    """A rounded bar through (cx, cy) at an angle. The paperclip is two of them."""
    painter.save()
    painter.translate(cx, cy)
    painter.rotate(angle)
    painter.drawRoundedRect(QRectF(-radius, -half, radius * 2, half * 2),
                            radius, radius)
    painter.restore()


def _envelope(painter: QPainter, filled: bool) -> None:
    body = QRectF(2.5, 5.0, 19.0, 14.0)
    painter.drawRoundedRect(body, 2.0, 2.0)
    path = QPainterPath(QPointF(2.5, 6.5))
    path.lineTo(12.0, 13.5)
    path.lineTo(21.5, 6.5)
    painter.drawPath(path)


def _envelope_open(painter: QPainter, filled: bool) -> None:
    path = QPainterPath(QPointF(2.5, 20.0))
    path.lineTo(2.5, 10.5)
    path.lineTo(12.0, 4.0)
    path.lineTo(21.5, 10.5)
    path.lineTo(21.5, 20.0)
    path.closeSubpath()
    painter.drawPath(path)
    flap = QPainterPath(QPointF(2.5, 10.5))
    flap.lineTo(12.0, 17.0)
    flap.lineTo(21.5, 10.5)
    painter.drawPath(flap)


def _archive(painter: QPainter, filled: bool) -> None:
    painter.drawRoundedRect(QRectF(2.5, 4.0, 19.0, 5.0), 1.2, 1.2)
    painter.drawRoundedRect(QRectF(4.0, 9.5, 16.0, 10.5), 1.2, 1.2)
    painter.drawLine(QPointF(9.5, 13.5), QPointF(14.5, 13.5))


def _trash(painter: QPainter, filled: bool) -> None:
    painter.drawLine(QPointF(3.5, 6.5), QPointF(20.5, 6.5))
    lid = QPainterPath(QPointF(9.0, 6.5))
    lid.lineTo(9.0, 4.0)
    lid.lineTo(15.0, 4.0)
    lid.lineTo(15.0, 6.5)
    painter.drawPath(lid)
    body = QPainterPath(QPointF(5.5, 6.5))
    body.lineTo(6.8, 20.5)
    body.lineTo(17.2, 20.5)
    body.lineTo(18.5, 6.5)
    painter.drawPath(body)
    painter.drawLine(QPointF(10.0, 10.0), QPointF(10.4, 17.0))
    painter.drawLine(QPointF(14.0, 10.0), QPointF(13.6, 17.0))


def _flag(painter: QPainter, filled: bool) -> None:
    painter.drawLine(QPointF(5.5, 3.0), QPointF(5.5, 21.0))
    banner = QPainterPath(QPointF(5.5, 4.0))
    banner.lineTo(18.5, 7.5)
    banner.lineTo(5.5, 12.5)
    banner.closeSubpath()
    if filled:
        painter.fillPath(banner, painter.pen().color())
    painter.drawPath(banner)


def _paperclip(painter: QPainter, filled: bool) -> None:
    _stadium(painter, 12.0, 11.0, 9.0, 4.5, 40.0)
    _stadium(painter, 13.3, 10.0, 6.0, 2.2, 40.0)


def _arrow_head(path: QPainterPath, tip_x: float, tip_y: float,
                back: float) -> None:
    path.moveTo(tip_x + back, tip_y - 5.0)
    path.lineTo(tip_x, tip_y)
    path.lineTo(tip_x + back, tip_y + 5.0)


def _reply(painter: QPainter, filled: bool) -> None:
    path = QPainterPath()
    _arrow_head(path, 3.5, 9.5, 5.0)
    path.moveTo(3.5, 9.5)
    path.lineTo(13.0, 9.5)
    path.cubicTo(19.5, 9.5, 20.5, 14.0, 20.5, 20.0)
    painter.drawPath(path)


def _reply_all(painter: QPainter, filled: bool) -> None:
    path = QPainterPath()
    _arrow_head(path, 2.5, 9.5, 4.5)
    _arrow_head(path, 8.0, 9.5, 4.5)
    path.moveTo(8.0, 9.5)
    path.lineTo(14.0, 9.5)
    path.cubicTo(19.5, 9.5, 20.5, 14.0, 20.5, 20.0)
    painter.drawPath(path)


def _forward(painter: QPainter, filled: bool) -> None:
    path = QPainterPath()
    _arrow_head(path, 20.5, 9.5, -5.0)
    path.moveTo(20.5, 9.5)
    path.lineTo(11.0, 9.5)
    path.cubicTo(4.5, 9.5, 3.5, 14.0, 3.5, 20.0)
    painter.drawPath(path)


def _snooze(painter: QPainter, filled: bool) -> None:
    painter.drawEllipse(QRectF(3.5, 3.5, 17.0, 17.0))
    painter.drawLine(QPointF(12.0, 7.5), QPointF(12.0, 12.0))
    painter.drawLine(QPointF(12.0, 12.0), QPointF(15.5, 14.0))


def _tag(painter: QPainter, filled: bool) -> None:
    path = QPainterPath(QPointF(2.5, 11.5))
    path.lineTo(11.5, 2.5)
    path.lineTo(21.5, 2.5)
    path.lineTo(21.5, 12.5)
    path.lineTo(12.5, 21.5)
    path.closeSubpath()
    if filled:
        painter.fillPath(path, painter.pen().color())
    painter.drawPath(path)
    if not filled:
        painter.drawEllipse(QRectF(15.6, 5.6, 3.4, 3.4))


def _dot(painter: QPainter, filled: bool) -> None:
    rect = QRectF(7.0, 7.0, 10.0, 10.0)
    if filled:
        painter.setBrush(painter.pen().color())
    painter.drawEllipse(rect)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _chevron_right(painter: QPainter, filled: bool) -> None:
    path = QPainterPath(QPointF(9.5, 5.5))
    path.lineTo(16.0, 12.0)
    path.lineTo(9.5, 18.5)
    painter.drawPath(path)


def _chevron_down(painter: QPainter, filled: bool) -> None:
    path = QPainterPath(QPointF(5.5, 9.5))
    path.lineTo(12.0, 16.0)
    path.lineTo(18.5, 9.5)
    painter.drawPath(path)


def _search(painter: QPainter, filled: bool) -> None:
    painter.drawEllipse(QRectF(3.5, 3.5, 13.0, 13.0))
    painter.drawLine(QPointF(16.0, 16.0), QPointF(21.0, 21.0))


def _plus(painter: QPainter, filled: bool) -> None:
    painter.drawLine(QPointF(12.0, 5.0), QPointF(12.0, 19.0))
    painter.drawLine(QPointF(5.0, 12.0), QPointF(19.0, 12.0))


def _hidden(painter: QPainter, filled: bool) -> None:
    painter.drawEllipse(QRectF(6.0, 6.0, 12.0, 12.0))
    painter.drawLine(QPointF(4.0, 20.0), QPointF(20.0, 4.0))


def _file(painter: QPainter, filled: bool) -> None:
    """A page with its corner turned. The generic attachment."""
    body = QPainterPath(QPointF(5.0, 2.5))
    body.lineTo(14.0, 2.5)
    body.lineTo(19.0, 7.5)
    body.lineTo(19.0, 21.5)
    body.lineTo(5.0, 21.5)
    body.closeSubpath()
    painter.drawPath(body)
    fold = QPainterPath(QPointF(14.0, 2.5))
    fold.lineTo(14.0, 7.5)
    fold.lineTo(19.0, 7.5)
    painter.drawPath(fold)


def _image(painter: QPainter, filled: bool) -> None:
    """A frame with a hill and a sun in it, which is the only drawing of a
    picture small enough to read at 13 px."""
    painter.drawRoundedRect(QRectF(3.0, 5.0, 18.0, 14.0), 1.6, 1.6)
    painter.drawEllipse(QRectF(7.0, 8.0, 3.4, 3.4))
    hill = QPainterPath(QPointF(4.0, 18.0))
    hill.lineTo(10.0, 12.5)
    hill.lineTo(14.0, 16.0)
    hill.lineTo(16.5, 13.5)
    hill.lineTo(20.0, 18.0)
    painter.drawPath(hill)


def _save(painter: QPainter, filled: bool) -> None:
    """An arrow into a tray. Save, as distinct from Archive's box."""
    painter.drawLine(QPointF(12.0, 3.5), QPointF(12.0, 14.5))
    head = QPainterPath(QPointF(7.5, 10.0))
    head.lineTo(12.0, 14.5)
    head.lineTo(16.5, 10.0)
    painter.drawPath(head)
    tray = QPainterPath(QPointF(4.0, 15.0))
    tray.lineTo(4.0, 20.0)
    tray.lineTo(20.0, 20.0)
    tray.lineTo(20.0, 15.0)
    painter.drawPath(tray)


def _person(painter: QPainter, filled: bool) -> None:
    painter.drawEllipse(QRectF(8.0, 3.5, 8.0, 8.0))
    path = QPainterPath(QPointF(3.5, 21.0))
    path.cubicTo(3.5, 14.0, 20.5, 14.0, 20.5, 21.0)
    painter.drawPath(path)


GLYPHS = {
    "envelope": _envelope,
    "envelope-open": _envelope_open,
    "archive": _archive,
    "trash": _trash,
    "flag": _flag,
    "paperclip": _paperclip,
    "reply": _reply,
    "reply-all": _reply_all,
    "forward": _forward,
    "snooze": _snooze,
    "tag": _tag,
    "dot": _dot,
    "chevron-right": _chevron_right,
    "chevron-down": _chevron_down,
    "search": _search,
    "plus": _plus,
    "hidden": _hidden,
    "person": _person,
    "file": _file,
    "image": _image,
    "save": _save,
}


def paint(painter: QPainter, name: str, rect: QRectF, colour: str | QColor, *,
          filled: bool = False, stroke: float = _STROKE) -> None:
    """Draw one glyph into a rectangle, in a colour.

    An unknown name draws nothing rather than raising. A missing icon is a blank
    space; an exception here would be raised inside a paint event, several
    hundred times a second, on a list that is scrolling.
    """
    glyph = GLYPHS.get(name)
    if glyph is None:
        return
    rect = QRectF(rect)
    side = min(rect.width(), rect.height())
    if side <= 0:
        return

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(rect.x() + (rect.width() - side) / 2.0,
                      rect.y() + (rect.height() - side) / 2.0)
    painter.scale(side / BOX, side / BOX)

    pen = QPen(QColor(colour))
    pen.setWidthF(stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    glyph(painter, filled)
    painter.restore()


def pixmap(name: str, colour: str | QColor, size: int = 16, *,
           filled: bool = False, ratio: float = 1.0) -> QPixmap:
    """A glyph as a pixmap, for the places Qt wants one — a button, a tab.

    `ratio` is the device pixel ratio: drawn at the real pixel count and then
    told what it is, which is what stops icons being soft on a HiDPI screen.
    """
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    paint(painter, name, QRectF(0, 0, size, size), colour, filled=filled)
    painter.end()
    return pm


def icon(name: str, colour: str | QColor, size: int = 16, *,
         filled: bool = False) -> QIcon:
    return QIcon(pixmap(name, colour, size, filled=filled))
