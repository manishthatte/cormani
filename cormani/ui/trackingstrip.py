# SPDX-License-Identifier: GPL-3.0-or-later
#
# The TRACKING strip under the reading pane.
#
# PLAN.txt §3 draws it: the thread this message is on, where that thread
# stands, and the three things a person does about it without leaving the
# message —
#
#     ── TRACKING ──────────────────────
#     Covalent Example · replied
#     Owed 4d · nudge 28 Aug
#     [Log call] [Set deadline] [Close]
#
# ── WHY IT IS HERE AND NOT IN THE TRACKING TAB ─────────────────────────────
#
# The moment a person knows what to do about a conversation is the moment they
# have just read a message on it. A strip that made them switch tabs to record
# a phone call is a strip that records no phone calls, and an unrecorded call
# is the failure the whole timeline exists to prevent: the board says a
# correspondent has been silent for three weeks when the matter was settled on
# the telephone in the first of them.
#
# ── IT OFFERS TO CREATE ONE WHEN THERE IS NONE, AND THAT IS THE COMMON CASE ─
#
# Most messages are on no thread and most never will be. So the strip is quiet
# — one line and one button — until there is something to say, and the button
# is how a thread gets made at all: from a message, with its correspondent and
# its conversation already on it. A tracking layer whose only entry point was a
# blank New Thread dialog would be one nobody filled in.
#
# ── IT ASKS BY ADDRESS AND NOT BY MESSAGE ──────────────────────────────────
#
# `tracking.threads_for_address` goes through `handle`, so a correspondent
# writing from their phone for the first time still finds their own thread —
# which is exactly the case that would otherwise look like the feature not
# working. A message already FILED names its thread directly, and that answer
# wins when both are available.
#
# THE STRIP PERFORMS NOTHING. Every button is a signal; `ui/actions.py` made
# the argument that one place decides what a command means, and it is also what
# lets the suite test this without a click.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from ..store import touches as touches_repo
from ..store import tracking as tracking_repo


class TrackingStrip(QWidget):
    """What the reading pane says about the conversation this message is on."""

    action = Signal(str)
    thread_chosen = Signal(int)

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._message_id: int | None = None
        self._thread_id: int | None = None
        self._theme = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 4, 10, 6)
        outer.setSpacing(2)

        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(line)

        self.heading = QLabel("TRACKING", self)
        outer.addWidget(self.heading)

        self.name = QLabel("", self)
        self.name.setWordWrap(True)
        outer.addWidget(self.name)

        self.standing = QLabel("", self)
        outer.addWidget(self.standing)

        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 2, 0, 0)
        self.buttons = {}
        for name, label in (("open", "Open thread"), ("log-call", "Log call"),
                            ("deadline", "Set deadline"), ("close", "Close"),
                            ("track", "Track this…")):
            button = QPushButton(label, bar)
            button.clicked.connect(lambda _checked=False, n=name:
                                   self.action.emit(n))
            row.addWidget(button)
            self.buttons[name] = button
        row.addStretch(1)
        outer.addWidget(bar)
        self.show_message(None)

    def set_theme(self, theme) -> None:
        self._theme = theme
        if theme is None:
            return
        self.heading.setStyleSheet(
            f"color: {theme.text_muted}; font-size: 10px; "
            f"letter-spacing: 1px;")
        self._repaint()

    # ------------------------------------------------------------- contents
    def thread_id(self) -> int | None:
        return self._thread_id

    def show_message(self, message_id: int | None, *,
                     today: dt.date | None = None) -> None:
        """Draw what is known about the message now in the reading pane."""
        self._message_id = int(message_id) if message_id else None
        self._thread_id = None
        if self._message_id is None:
            self.setVisible(False)
            return
        threads = self._threads_for(self._message_id)
        self.setVisible(True)
        if not threads:
            self._draw_untracked()
            return
        self._thread_id = threads[0].id
        self._draw(threads, today)

    def _threads_for(self, message_id: int) -> list:
        """This message's threads: the ones it is FILED on, then its sender's.

        Filed wins because it is an answer rather than an inference — somebody
        put this message there. The address lookup is the fallback and is what
        makes the strip useful for a message that has just arrived and has not
        been filed yet.
        """
        filed = [t.thread_id for t in touches_repo.for_message(self._con,
                                                               message_id)]
        found = [tracking_repo.get_thread(self._con, t) for t in filed]
        found = [t for t in found if t is not None]
        if found:
            return found
        row = self._con.execute("SELECT from_addr FROM message WHERE id = ?",
                                (int(message_id),)).fetchone()
        if row is None:
            return []
        return tracking_repo.threads_for_address(self._con,
                                                 row["from_addr"] or "")

    def _draw_untracked(self) -> None:
        """Quiet, because this is most messages. One line and one button."""
        self.name.setText("Not on a tracked thread")
        self.standing.setText("")
        for name, button in self.buttons.items():
            if name == "track":
                button.setVisible(True)
                button.setDefault(True)
                button.setAutoDefault(True)
            else:
                button.setVisible(False)

    def _draw(self, threads: list, today) -> None:
        thread = threads[0]
        others = (f"  (+{len(threads) - 1} more)" if len(threads) > 1 else "")
        self.name.setText(
            " · ".join(x for x in (thread.org, thread.title) if x) + others)
        self.standing.setText(self._standing(thread, today))
        for name, button in self.buttons.items():
            if name == "track":
                button.setDefault(False)
                button.setAutoDefault(False)
            button.setVisible(name != "track")
        self._repaint(thread, today)

    def _standing(self, thread, today) -> str:
        """The line PLAN.txt §3 draws: "Owed 4d · nudge 28 Aug".

        A DEADLINE REPLACES THE NUDGE RATHER THAN JOINING IT. There is one line
        and a statutory date is the thing that must be on it — putting both
        side by side is how a filing date reads as one more reminder.
        """
        parts = [thread.state]
        if thread.owed:
            parts.append(f"owed {thread.owed_days(today)}d")
        days = thread.days_to_deadline(today)
        if days is not None:
            parts.append(f"⏰ {thread.deadline_date}"
                         + (f" ({days}d)" if days >= 0 else " PASSED"))
        elif thread.overdue(today):
            parts.append("nudge overdue")
        elif thread.effective_due():
            parts.append(f"nudge {thread.effective_due().isoformat()}")
        return " · ".join(parts)

    def _repaint(self, thread=None, today=None) -> None:
        if self._theme is None:
            return
        if thread is None:
            self.standing.setStyleSheet(f"color: {self._theme.text_muted};")
            return
        days = thread.days_to_deadline(today)
        colour = (self._theme.deadline if days is not None
                  else self._theme.owed if thread.owed
                  else self._theme.text_muted)
        self.standing.setStyleSheet(f"color: {colour};")
