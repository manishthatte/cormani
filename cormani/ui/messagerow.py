# SPDX-License-Identifier: GPL-3.0-or-later
#
# A row of the message list, as it is drawn.
#
# The row is DRAWN. Sender in weight, subject beneath it, preview in grey, the
# account's colour down the left edge, the date and the status icons on the
# right, tag chips on the subject line, and — in a conversation or a set of
# search results — how many messages are behind it and where it lives. That is
# eight pieces of information with three type treatments, and no arrangement of
# widgets in a list of two hundred thousand rows is going to be fast enough. A
# delegate paints it in one pass and Qt creates nothing per row.
#
# THE ACTION RECTANGLES ARE COMPUTED IN ONE PLACE, `action_rects`, and used by
# both the painting and the hit test. Two copies of that arithmetic is the
# classic way to get a button that highlights under the cursor and does nothing
# when clicked, or worse, does its neighbour's job.
#
# SEARCH RESULTS SAY WHERE THEY ARE, AND ORDINARY ROWS DO NOT. In a folder,
# "which folder is this in" is answered by the folder you are looking at, and
# printing it on all two hundred rows is noise. In a result set drawn from every
# folder of fifteen accounts, or under a conversation whose other half is in
# Sent, it is what makes a row readable — so the third line ends with `Inbox ·
# manitlab` and the snippet is elided to make room for it.
#
# THE FILE THIS CAME OUT OF IS ui/messagelist.py, which owns the VIEW: hover,
# hit testing, selection and the cursor. Drawing a row and behaving as a list
# are different jobs and the 600-line rule found the seam.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from . import density as density_mod
from . import icons
from . import theme as theme_mod
from .models import messages as message_model

# (id, glyph, tooltip). Order is left to right, and is deliberate: the
# destructive one is furthest from where the cursor arrives.
HOVER_ACTIONS = (
    ("mark_read", "envelope", "Mark read or unread"),
    ("flag", "flag", "Flag"),
    ("archive", "archive", "Archive"),
    ("delete", "trash", "Delete"),
)


def to_local(iso: str) -> dt.datetime | None:
    """Parse a stored timestamp and move it into the reader's own timezone.

    The store keeps UTC — schema.py says so, and it is the only sane choice for
    a client syncing fifteen mailboxes. The interface must not. "Today" and
    "09:30" mean the reader's day and the reader's clock, and this machine is at
    UTC+05:30, where the two dates disagree for five and a half hours out of
    every twenty-four. Formatting the stored value directly would put yesterday's
    date on this morning's mail for that whole window.
    """
    if not iso:
        return None
    try:
        when = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return when.astimezone() if when.tzinfo is not None else when


def format_date(iso: str, now: dt.datetime | None = None) -> str:
    """A date the width of the space available, not the width of a date.

    Today's mail wants a time and last year's wants a year, and showing both
    always costs the column six characters it does not have. A pure function so
    the rule can be tested without a display or a clock.
    """
    when = to_local(iso)
    if when is None:
        return iso[:10] if iso else ""
    now = now or dt.datetime.now()
    if now.tzinfo is not None:
        now = now.astimezone()
    if when.tzinfo is not None and now.tzinfo is None:
        when = when.replace(tzinfo=None)
    if when.date() == now.date():
        return when.strftime("%H:%M")
    if when.year == now.year:
        return when.strftime("%-d %b")
    return when.strftime("%-d %b %y")


def action_rects(rect: QRect, d: density_mod.Density) -> dict[str, QRect]:
    """Where the hover actions sit in a row. The single source of that geometry."""
    button = d.icon + 8
    gap = 2
    total = len(HOVER_ACTIONS) * button + (len(HOVER_ACTIONS) - 1) * gap
    top = rect.top() + (rect.height() - button) // 2
    x = rect.right() - d.pad_h - total
    out: dict[str, QRect] = {}
    for action_id, _glyph, _tip in HOVER_ACTIONS:
        out[action_id] = QRect(x, top, button, button)
        x += button + gap
    return out


def action_strip_left(rect: QRect, d: density_mod.Density) -> int:
    rects = action_rects(rect, d)
    return min(r.left() for r in rects.values()) - 8


class MessageDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme_mod.SOLARIZED_LIGHT
        self.density = density_mod.NORMAL
        self.hover_row = -1
        self.hover_action = ""
        # Set while a search is showing; see the note at the top of this file.
        # A row that came from another folder to complete a conversation says
        # where it is whatever this is set to — that is the whole reason it is
        # legible beneath a message from somewhere else.
        self.show_location = False
        self._font_cache: dict[tuple, tuple[QFont, QFont, QFont]] = {}

    # ------------------------------------------------------------- measuring
    def fonts(self, base: QFont) -> tuple[QFont, QFont, QFont]:
        d = self.density
        key = (base.family(), base.pointSizeF(), base.bold(), d.key)
        cached = self._font_cache.get(key)
        if cached is None:
            def derive(delta: int, bold: bool = False) -> QFont:
                font = QFont(base)
                font.setPointSizeF(max(6.0, base.pointSizeF() + delta))
                font.setBold(bold)
                return font
            cached = (derive(d.sender_pt), derive(d.subject_pt), derive(d.preview_pt))
            self._font_cache[key] = cached
        return cached

    def set_density(self, density) -> None:
        self.density = density
        self._font_cache.clear()

    def sizeHint(self, option, index) -> QSize:
        from PySide6.QtGui import QFontMetrics
        sender, subject, preview = self.fonts(option.font)
        heights = [QFontMetrics(sender).height(), QFontMetrics(subject).height(),
                   QFontMetrics(preview).height()]
        return QSize(option.rect.width(),
                     density_mod.row_height(self.density, heights))

    # -------------------------------------------------------------- painting
    def paint(self, painter: QPainter, option, index) -> None:
        row = index.data(message_model.RowRole)
        if row is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setClipRect(option.rect)
        t, d = self.theme, self.density
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = index.row() == self.hover_row

        if selected:
            painter.fillRect(rect, QColor(t.accent))
            strong = QColor(t.text_inverse)
            normal = QColor(t.text_inverse)
            muted = QColor(t.text_inverse)
            muted.setAlpha(185)
        else:
            if hovered:
                painter.fillRect(rect, QColor(t.accent_muted))
            strong = QColor(t.text_strong)
            normal = QColor(t.text)
            muted = QColor(t.text_muted)

        painter.setPen(QPen(QColor(t.border)))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        # The account's colour, the same value the rail shows. This is what makes
        # a unified inbox readable at fifteen accounts.
        if row.account_colour:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(row.account_colour))
            painter.drawRect(QRectF(rect.left(), rect.top() + 1.0,
                                    float(d.swatch), rect.height() - 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)

        sender_font, subject_font, preview_font = self.fonts(option.font)
        if not row.seen:
            sender_font = QFont(sender_font)
            sender_font.setBold(True)

        left = rect.left() + d.swatch + d.gutter
        dot = max(6, int(d.icon * 0.45))
        right = rect.right() - d.pad_h

        from PySide6.QtGui import QFontMetrics
        m_sender = QFontMetrics(sender_font)
        m_subject = QFontMetrics(subject_font)
        m_preview = QFontMetrics(preview_font)

        y = rect.top() + d.pad_v
        line1 = QRect(left, y, right - left, m_sender.height())
        y += m_sender.height() + d.line_gap
        line2 = QRect(left, y, right - left, m_subject.height())
        y += m_subject.height() + d.line_gap
        line3 = QRect(left, y, right - left, m_preview.height())

        # The unread marker keeps its space when the message is read, so that
        # every sender in the column starts at the same x.
        if not row.seen:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.text_inverse if selected else t.unread))
            painter.drawEllipse(QRectF(left, line1.center().y() - dot / 2.0,
                                       float(dot), float(dot)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        text_left = left + dot + 6

        # --- line 1: sender, then the status column and the date -------------
        status_right = right
        if hovered:
            status_right = action_strip_left(rect, d)
            self._paint_actions(painter, rect, selected, index)
        else:
            status_right = self._paint_thread_count(
                painter, index, rect, status_right, line1, selected, muted)
            date = format_date(row.date_at)
            if date:
                width = m_preview.horizontalAdvance(date) + 4
                painter.setFont(preview_font)
                painter.setPen(QPen(muted))
                painter.drawText(QRect(status_right - width, line1.top(), width,
                                       line1.height()),
                                 int(Qt.AlignmentFlag.AlignRight |
                                     Qt.AlignmentFlag.AlignVCenter), date)
                status_right -= width + 6
            status_right = self._paint_status(painter, row, rect, status_right,
                                              line1, selected, muted)

        painter.setFont(sender_font)
        painter.setPen(QPen(strong))
        sender_rect = QRect(text_left, line1.top(),
                            max(0, status_right - text_left - 6), line1.height())
        painter.drawText(sender_rect,
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         m_sender.elidedText(row.correspondent or row.from_addr,
                                             Qt.TextElideMode.ElideRight,
                                             sender_rect.width()))

        # --- line 2: subject, and the tag chips ------------------------------
        # While the actions are showing, every line ends where they begin, so
        # the text is ELIDED rather than merely hidden behind the mask. An
        # ellipsis says the line continues; a clean cut says it ended there.
        right = min(right, action_strip_left(rect, d)) if hovered else right
        subject_right = right
        if row.tags:
            subject_right = self._paint_tags(painter, row, line2, right, selected)
        painter.setFont(subject_font)
        painter.setPen(QPen(normal if row.seen else strong))
        subject_rect = QRect(text_left, line2.top(),
                             max(0, subject_right - text_left - 6), line2.height())
        painter.drawText(subject_rect,
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         m_subject.elidedText(row.subject_label,
                                              Qt.TextElideMode.ElideRight,
                                              subject_rect.width()))

        # --- line 3: the preview, or the line the search matched in ----------
        if d.shows_preview:
            painter.setFont(preview_font)
            painter.setPen(QPen(muted))
            preview_right = right
            if (self.show_location or not row.in_scope) and not hovered:
                # The folder and account this hit came from, right-aligned, and
                # never elided: a location cut in half names nothing. The
                # snippet gives up the width instead, because a snippet is
                # already an excerpt and losing three more words costs less.
                location = m_preview.elidedText(
                    row.location, Qt.TextElideMode.ElideLeft,
                    max(60, (right - text_left) // 2))
                width = m_preview.horizontalAdvance(location)
                painter.drawText(QRect(right - width, line3.top(), width,
                                       line3.height()),
                                 int(Qt.AlignmentFlag.AlignRight |
                                     Qt.AlignmentFlag.AlignVCenter), location)
                preview_right = right - width - 10
            preview_rect = QRect(text_left, line3.top(),
                                 max(0, preview_right - text_left), line3.height())
            painter.drawText(preview_rect,
                             int(Qt.AlignmentFlag.AlignLeft |
                                 Qt.AlignmentFlag.AlignVCenter),
                             m_preview.elidedText(row.snippet or row.preview,
                                                  Qt.TextElideMode.ElideRight,
                                                  preview_rect.width()))
        painter.restore()

    def _paint_thread_count(self, painter, index, rect, right, line, selected,
                            muted) -> int:
        """How many messages the conversation holds, when there is more than one.

        A pill rather than a bare number, because a bare number beside a date
        beside a size is three numbers nobody can tell apart. Filled when the
        conversation has something unread in it: that is the difference between
        "there is more here" and "there is more here that you have not read".
        """
        total = index.data(message_model.ThreadCountRole) or 0
        if total < 2:
            return right
        unread = index.data(message_model.ThreadUnreadRole) or 0
        _sender, _subject, preview_font = self.fonts(painter.font())
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(preview_font)
        label = str(total)
        width = max(metrics.horizontalAdvance(label) + 10, line.height() - 4)
        height = line.height() - 4
        box = QRectF(right - width, line.top() + 2.0, float(width), float(height))
        theme = self.theme
        fill = QColor(theme.unread if unread else theme.border)
        if selected:
            fill = QColor(theme.text_inverse)
            fill.setAlpha(70 if not unread else 160)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(box, height / 2.0, height / 2.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setFont(preview_font)
        painter.setPen(QPen(QColor(theme.text_inverse if unread and not selected
                                   else theme.text)))
        painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), label)
        return right - width - 6

    def _paint_status(self, painter, row, rect, right, line, selected, muted) -> int:
        density = self.density
        for name, active, colour in (
                ("paperclip", row.has_attachment, muted),
                ("reply", row.answered, muted),
                ("flag", row.flagged, QColor(self.theme.flagged))):
            if not active:
                continue
            box = QRectF(right - density.icon, line.center().y() - density.icon / 2.0,
                         float(density.icon), float(density.icon))
            icons.paint(painter, name, box,
                        muted if selected else colour, filled=(name == "flag"))
            right -= density.icon + density.icon_gap
        return right

    def _paint_tags(self, painter, row, line, right, selected) -> int:
        d = self.density
        _sender, _subject, preview_font = self.fonts(painter.font())
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(preview_font)
        painter.setFont(preview_font)
        # At most three. A message with nine tags is a message whose subject
        # would otherwise disappear behind them.
        for tag in list(row.tags)[:3][::-1]:
            width = metrics.horizontalAdvance(tag.name) + 12
            chip = QRectF(right - width, line.center().y() - d.tag_height / 2.0,
                          float(width), float(d.tag_height))
            colour = QColor(tag.colour or self.theme.accent)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(chip, d.tag_height / 2.0, d.tag_height / 2.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Black or white on the chip, whichever the colour can carry. A tag
            # the user coloured pale yellow must not have white text on it.
            ink = "#ffffff" if colour.lightness() < 150 else "#202020"
            painter.setPen(QPen(QColor(ink)))
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, tag.name)
            right -= width + 4
        return right

    def _paint_actions(self, painter, rect, selected, index) -> None:
        t, d = self.theme, self.density
        rects = action_rects(rect, d)
        strip = QRect(action_strip_left(rect, d), rect.top() + 1,
                      rect.right() - action_strip_left(rect, d) - 1,
                      rect.height() - 3)
        # Painted in the row's OWN background colour. Its job is to mask a
        # subject or preview long enough to run underneath the buttons, not to
        # be a panel — a strip in a different colour reads as a second widget
        # that has appeared on the row.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t.accent if selected else t.accent_muted))
        painter.drawRect(strip)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        row = index.data(message_model.RowRole)
        for action_id, glyph, _tip in HOVER_ACTIONS:
            box = rects[action_id]
            active = self.hover_action == action_id
            if active:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(t.accent if not selected else t.accent_muted))
                painter.drawRoundedRect(QRectF(box), 4.0, 4.0)
                painter.setBrush(Qt.BrushStyle.NoBrush)
            if action_id == "mark_read" and row is not None:
                glyph = "envelope" if row.seen else "envelope-open"
            colour = t.text_inverse if (active and not selected) else (
                t.text_inverse if selected else t.text_strong)
            if action_id == "flag" and row is not None and row.flagged and not active:
                colour = t.flagged
            inner = QRectF(box).adjusted(4, 4, -4, -4)
            icons.paint(painter, glyph, inner, colour,
                        filled=(action_id == "flag" and row is not None
                                and row.flagged))
