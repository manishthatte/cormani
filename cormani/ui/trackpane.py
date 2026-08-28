# SPDX-License-Identifier: GPL-3.0-or-later
#
# The tracking pane: what is being pursued, and what happened on it.
#
# Two halves, in the space the list and the reading pane occupy, because
# PLAN.txt §3 gives that space to whatever the rail is showing and this is the
# same swap the calendar makes. On the left, the board — every tracked thread,
# ordered by what is most likely to be missed. On the right, one thread's
# timeline across every channel.
#
# ── IT IS A TAB AND NOT A RAIL SECTION, WHICH WAS A DECISION ───────────────
#
# The rail already carries fifteen accounts, their folders, the calendars and
# stage 7's sites. Tracking opens as a tab instead, which keeps the rail short
# at the cost of the counts not being visible until somebody looks. The cost is
# real and is paid deliberately; what makes it bearable is that the tab's TITLE
# carries the numbers, so a tab left open is a badge.
#
# ── THE BOARD'S ORDER IS THE WHOLE OPINION ─────────────────────────────────
#
# A hard deadline first, whatever else is true, because it is the thing that
# cannot be recovered from. Then whichever date is soonest, then priority.
# `store/tracking._order_by` holds it, and it is there rather than here because
# it is a claim about the data and not about the drawing.
#
# ── EVERY MARK IS DERIVED AT DRAW TIME ─────────────────────────────────────
#
# "Owed 4d", "nudge overdue", "7 days to file" — none of them is stored, all of
# them are functions of today, and a pane that cached them would be wrong by
# one every midnight. `Thread` computes them and takes `today` so that a test
# can ask about a Tuesday in March.
#
# ── THE ACTIONS ARE SIGNALS AND THE PANE PERFORMS NONE OF THEM ─────────────
#
# `ui/actions.py` made the argument: one place decides what a command means.
# The buttons here say what was asked for and the host does it, which is also
# what makes them testable without a dialog — the suite has no QTest and
# cannot click anything.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from ..store import times
from ..store import touches as touches_repo
from ..store import tracking as tracking_repo
from ..store import triage as triage_repo
from .triagepane import TriagePane

# What the arrow in a timeline row means. A meeting and a note point neither
# way, which is the same distinction `store/touches.py` draws in `direction`
# and for the same reason: neither discharges what is owed.
_ARROWS = {touches_repo.DIRECTION_IN: "←", touches_repo.DIRECTION_OUT: "→",
           touches_repo.DIRECTION_NOTE: "·"}

_CHANNEL_MARKS = {touches_repo.CHANNEL_PHONE: "☎", touches_repo.CHANNEL_MEETING: "▣",
                  touches_repo.CHANNEL_NOTE: "✎", "whatsapp": "w", "linkedin": "in",
                  "x": "x", "facebook": "f"}

# A deadline this close is drawn in the deadline colour rather than as text.
URGENT_DAYS = 14


class ThreadBoard(QListWidget):
    """Every tracked thread, in the order it is most likely to be missed."""

    chosen = Signal(int)

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._theme = None
        self._state = "live"
        self._track = ""
        self.setMinimumWidth(230)
        self.currentItemChanged.connect(self._changed)

    def set_theme(self, theme) -> None:
        self._theme = theme
        self.reload()

    def set_filter(self, *, state: str = "", track: str = "") -> None:
        self._state = state if state else self._state
        self._track = track
        self.reload()

    def threads(self) -> list:
        return tracking_repo.list_threads(self._con, state=self._state,
                                          track=self._track, order="due")

    def reload(self, *, today: dt.date | None = None) -> None:
        """Draw the board. Keeps the selection on the same THREAD, not the same
        row: a reload after a state change re-orders, and a pane that kept the
        row index would move the user to a different thread for no reason."""
        wanted = self.current_thread_id()
        self.blockSignals(True)
        self.clear()
        for thread in self.threads():
            item = QListWidgetItem(self._label(thread, today), self)
            item.setData(Qt.ItemDataRole.UserRole, thread.id)
            item.setToolTip(self._tooltip(thread, today))
            self._paint(item, thread, today)
            self.addItem(item)
            if thread.id == wanted:
                self.setCurrentItem(item)
        self.blockSignals(False)
        if wanted and self.current_thread_id() != wanted:
            # The thread being shown has gone — closed, or filtered out. Say so
            # by emitting, so the right-hand half stops showing a thread the
            # board no longer lists.
            self.chosen.emit(self.current_thread_id() or 0)

    def _label(self, thread, today) -> str:
        marks = []
        days = thread.days_to_deadline(today)
        if days is not None:
            marks.append("⏰ overdue" if days < 0 else f"⏰ {days}d")
        elif thread.owed:
            marks.append(f"owed {thread.owed_days(today)}d")
        elif thread.overdue(today):
            marks.append("nudge")
        return f"{thread.title}    {' · '.join(marks)}" if marks else thread.title

    def _tooltip(self, thread, today) -> str:
        lines = [thread.title]
        if thread.org:
            lines.append(thread.org)
        lines.append(f"{thread.state} · {thread.track}")
        if thread.next_action:
            lines.append(f"Next: {thread.next_action}")
        silent = thread.silent_days(today)
        lines.append("Nothing has happened yet" if silent is None
                     else f"Silent {silent} day(s)")
        if thread.deadline_date:
            lines.append(f"Deadline {thread.deadline_date}"
                         + (f" — {thread.deadline_note}"
                            if thread.deadline_note else ""))
        return "\n".join(lines)

    def _paint(self, item: QListWidgetItem, thread, today) -> None:
        """Colour says which KIND of attention a row wants, and there are three.

        A deadline is not a nudge and neither is an unanswered reply; drawing
        all three the same is how a statutory date becomes one more red row
        among forty.
        """
        if self._theme is None:
            return
        days = thread.days_to_deadline(today)
        if days is not None and days <= URGENT_DAYS:
            item.setForeground(QColor(self._theme.deadline))
            font = QFont(item.font())
            font.setBold(True)
            item.setFont(font)
        elif thread.owed:
            item.setForeground(QColor(self._theme.owed))
        elif thread.overdue(today):
            item.setForeground(QColor(self._theme.text_strong))
        else:
            item.setForeground(QColor(self._theme.text))

    def current_thread_id(self) -> int | None:
        item = self.currentItem()
        return None if item is None else int(item.data(Qt.ItemDataRole.UserRole))

    def select(self, thread_id: int) -> bool:
        for row in range(self.count()):
            item = self.item(row)
            if int(item.data(Qt.ItemDataRole.UserRole)) == int(thread_id):
                self.setCurrentItem(item)
                return True
        return False

    def _changed(self, current, _previous) -> None:
        if current is not None:
            self.chosen.emit(int(current.data(Qt.ItemDataRole.UserRole)))


class ThreadView(QWidget):
    """One thread: what it is, what is owed on it, and everything that happened.

    THE HEADER IS FOUR LINES AND EACH ANSWERS A DIFFERENT QUESTION — what this
    is, where it stands, what to do next, and when it must be done. A single
    summary line would be shorter and would make the deadline optional.
    """

    action = Signal(str)
    touch_activated = Signal(int)

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._thread_id: int | None = None
        self._theme = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 8)
        outer.setSpacing(4)

        self.title = QLabel("", self)
        font = QFont(self.title.font())
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setWordWrap(True)
        outer.addWidget(self.title)

        self.subtitle = QLabel("", self)
        outer.addWidget(self.subtitle)
        self.standing = QLabel("", self)
        outer.addWidget(self.standing)
        self.next_action = QLabel("", self)
        self.next_action.setWordWrap(True)
        outer.addWidget(self.next_action)
        self.deadline = QLabel("", self)
        self.deadline.setWordWrap(True)
        outer.addWidget(self.deadline)

        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(line)

        self.timeline = QListWidget(self)
        self.timeline.itemActivated.connect(self._activated)
        outer.addWidget(self.timeline, 1)

        outer.addWidget(self._buttons())
        self.show_thread(None)

    def _buttons(self) -> QWidget:
        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 6, 0, 0)
        self.buttons = {}
        for name, label in (("log-call", "Log call"), ("note", "Note"),
                            ("deadline", "Set deadline"), ("edit", "Edit"),
                            ("close", "Close")):
            button = QPushButton(label, bar)
            button.clicked.connect(lambda _checked=False, n=name:
                                   self.action.emit(n))
            row.addWidget(button)
            self.buttons[name] = button
        row.addStretch(1)
        return bar

    def set_theme(self, theme) -> None:
        self._theme = theme
        if theme is None:
            return
        self.subtitle.setStyleSheet(f"color: {theme.text_muted};")
        self.reload()

    # ------------------------------------------------------------- contents
    def thread_id(self) -> int | None:
        return self._thread_id

    def show_thread(self, thread_id: int | None, *,
                    today: dt.date | None = None) -> None:
        self._thread_id = int(thread_id) if thread_id else None
        self.reload(today=today)

    def reload(self, *, today: dt.date | None = None) -> None:
        thread = (tracking_repo.get_thread(self._con, self._thread_id)
                  if self._thread_id else None)
        for button in self.buttons.values():
            button.setEnabled(thread is not None)
        if thread is None:
            self.title.setText("No thread selected")
            for label in (self.subtitle, self.standing, self.next_action,
                          self.deadline):
                label.setText("")
            self.timeline.clear()
            return

        self.title.setText(thread.title)
        self.subtitle.setText(" · ".join(
            x for x in (thread.org, thread.track,
                        ", ".join(thread.channels)) if x))
        self.standing.setText(self._standing(thread, today))
        self.next_action.setText(f"Next: {thread.next_action}"
                                 if thread.next_action else "")
        self.deadline.setText(self._deadline(thread, today))
        if self._theme is not None:
            self.deadline.setStyleSheet(
                f"color: {self._theme.deadline};"
                if thread.days_to_deadline(today) is not None else "")
        self._draw_timeline(thread)

    def _standing(self, thread, today) -> str:
        """Where it stands, in the words a person would use.

        The three facts are kept apart deliberately: what state somebody put it
        in, whether a reply is owed, and whether a nudge is due. They are
        different questions and a thread can answer them differently — awaiting
        a reply that is not yet late is the ordinary case.
        """
        parts = [thread.state]
        if thread.owed:
            parts.append(f"owed {thread.owed_days(today)} day(s)")
        silent = thread.silent_days(today)
        if silent is not None:
            parts.append(f"silent {silent} day(s)")
        if thread.overdue(today):
            parts.append("nudge overdue")
        elif thread.effective_due():
            parts.append(f"nudge {thread.effective_due().isoformat()}")
        return " · ".join(parts)

    def _deadline(self, thread, today) -> str:
        days = thread.days_to_deadline(today)
        if days is None:
            return ""
        when = thread.deadline_date
        note = f" — {thread.deadline_note}" if thread.deadline_note else ""
        if days < 0:
            return f"Deadline {when} PASSED {-days} day(s) ago{note}"
        return f"Deadline {when}, {days} day(s){note}"

    def _draw_timeline(self, thread) -> None:
        self.timeline.clear()
        tz = times.local_zone()
        for touch in touches_repo.timeline(self._con, thread.id):
            when = touch.when(tz)
            stamp = when.astimezone(tz).strftime("%d %b %Y %H:%M") if when \
                else "?"
            mark = _CHANNEL_MARKS.get(touch.channel,
                                      _ARROWS.get(touch.direction, "·"))
            who = touch.contact_name or ""
            text = f"{stamp}  {mark} {touch.title}"
            if who:
                text += f"  ({who})"
            if touch.status == touches_repo.STATUS_BOUNCED:
                text += "  — DELIVERY FAILED"
            item = QListWidgetItem(text, self.timeline)
            item.setData(Qt.ItemDataRole.UserRole, touch.id)
            if touch.body:
                item.setToolTip(touch.body[:400])
            if self._theme is not None and \
                    touch.status == touches_repo.STATUS_BOUNCED:
                item.setForeground(QColor(self._theme.error))
            self.timeline.addItem(item)
        self.timeline.scrollToBottom()

    def selected_touch(self) -> int | None:
        item = self.timeline.currentItem()
        return None if item is None else int(item.data(Qt.ItemDataRole.UserRole))

    def _activated(self, item) -> None:
        self.touch_activated.emit(int(item.data(Qt.ItemDataRole.UserRole)))


class TrackPane(QWidget):
    """The board and one thread, side by side."""

    action = Signal(str)
    thread_chosen = Signal(int)
    message_activated = Signal(int)
    file_requested = Signal(int, int)      # message id, thread id
    status_message = Signal(str)

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.board = ThreadBoard(con, self.splitter)
        self.view = ThreadView(con, self.splitter)
        # THE QUEUE SITS BESIDE THE BOARD, which is what makes filing one
        # gesture: the thread is chosen on the left and the message on the
        # right, so "file this onto that" is two clicks already made. A queue
        # in a window of its own would need a thread PICKER, and the picker is
        # where the wrong thread gets chosen.
        self.queue = TriagePane(con, self.splitter)
        self.queue.setVisible(False)
        self.splitter.addWidget(self.board)
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(self.queue)
        self.splitter.setStretchFactor(1, 1)
        outer.addWidget(self.splitter, 1)

        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 0, 10, 4)
        self.footer = QLabel("", bar)
        row.addWidget(self.footer, 1)
        self.queue_button = QPushButton("", bar)
        self.queue_button.setFlat(True)
        self.queue_button.clicked.connect(
            lambda: self.show_queue(not self.queue.isVisible()))
        row.addWidget(self.queue_button, 0)
        outer.addWidget(bar)

        self.board.chosen.connect(self._chosen)
        self.view.action.connect(self.action)
        self.view.touch_activated.connect(self._touch_activated)
        self.queue.status_message.connect(self.status_message)
        self.queue.message_activated.connect(self.message_activated)
        self.queue.changed.connect(lambda: self.reload())
        self.queue.file_requested.connect(self._file_requested)
        self.reload()

    def show_queue(self, on: bool) -> None:
        self.queue.setVisible(bool(on))
        if on:
            self.queue.reload()
        self._update_queue_button()

    def _update_queue_button(self) -> None:
        waiting = triage_repo.count(self._con)
        self.queue_button.setText(
            "Hide the queue" if self.queue.isVisible()
            else f"Needs filing ({waiting})")

    def _file_requested(self, message_id: int) -> None:
        """File the queued message onto the thread the BOARD has selected.

        Said rather than guessed when there is none: a button that silently
        does nothing reads as broken, and filing onto a thread the person did
        not choose is the one mistake this layer must never make.
        """
        thread_id = self.board.current_thread_id()
        if not thread_id:
            self.status_message.emit(
                "Choose a thread on the left to file it onto.")
            return
        self.file_requested.emit(int(message_id), int(thread_id))

    # ------------------------------------------------------------- contents
    def reload(self, *, today: dt.date | None = None) -> None:
        self.board.reload(today=today)
        self.view.reload(today=today)
        if self.queue.isVisible():
            self.queue.reload()
        self.footer.setText(self._footer(today))
        self._update_queue_button()

    def _footer(self, today) -> str:
        """The counts, and the three silences told apart.

        "Nothing to show" over an empty board is indistinguishable from a
        feature that is broken, so the pane says which of the three it is.
        """
        counts = tracking_repo.counts(self._con, today=today)
        if not counts["live"]:
            return ("Nothing is being tracked yet — file a message onto a new "
                    "thread from the reading pane"
                    if not counts["closed"]
                    else f"No live threads · {counts['closed']} closed")
        return (f"{counts['live']} live · {counts['owed']} owed · "
                f"{counts['overdue']} to nudge · "
                f"{counts['deadlines']} with a deadline within a month")

    def show_thread(self, thread_id: int | None) -> None:
        if thread_id and self.board.select(int(thread_id)):
            return
        self.view.show_thread(thread_id)

    def thread_id(self) -> int | None:
        return self.view.thread_id()

    def set_theme(self, theme) -> None:
        self.board.set_theme(theme)
        self.view.set_theme(theme)
        self.queue.set_theme(theme)
        if theme is not None:
            self.footer.setStyleSheet(
                f"color: {theme.text_muted}; padding: 4px 10px;")

    def title(self) -> str:
        """What the TAB says, which is where the counts live — see the header.

        A tab left open is the badge the rail does not carry.
        """
        counts = tracking_repo.counts(self._con)
        if not counts["live"]:
            return "Tracking"
        marks = []
        if counts["owed"]:
            marks.append(f"{counts['owed']} owed")
        if counts["deadlines"]:
            marks.append(f"{counts['deadlines']} ⏰")
        return f"Tracking ({' · '.join(marks)})" if marks else "Tracking"

    # -------------------------------------------------------------- events
    def _chosen(self, thread_id: int) -> None:
        self.view.show_thread(thread_id or None)
        self.thread_chosen.emit(int(thread_id or 0))

    def _touch_activated(self, touch_id: int) -> None:
        """Opening a timeline row opens the MESSAGE behind it, where there is
        one. A call has none, and saying so is better than doing nothing at
        all — a button that silently does nothing reads as broken."""
        touch = touches_repo.get_touch(self._con, touch_id)
        if touch is None:
            return
        if touch.message_id:
            self.message_activated.emit(int(touch.message_id))
        else:
            self.status_message.emit(
                f"{touch.title} — logged by hand; there is no message to open")
