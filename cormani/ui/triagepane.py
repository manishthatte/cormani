# SPDX-License-Identifier: GPL-3.0-or-later
#
# What has arrived that is on no thread — the queue, with somewhere to put it.
#
# `store/triage.py` holds the question and the three narrowings that make it
# answerable. This is the surface, and without one the queue is a library
# nobody can reach: the whole claim of the tracking layer is that an unfiled
# reply is VISIBLE instead of silent, and a queue with no pane is neither.
#
# ── IT SITS BESIDE THE BOARD, WHICH IS WHAT MAKES FILING ONE GESTURE ───────
#
# The thread is selected on the left and the message on the right, so "file
# this onto that" is two clicks that are already made. A queue in a window of
# its own would need a thread PICKER — a second list of the same threads, in a
# dialog, with its own idea of the order — and the picker is where the wrong
# thread gets chosen.
#
# ── THE SCOPE CHOOSER SHOWS ALL THREE COUNTS AT ONCE ───────────────────────
#
# `known` is narrow on purpose and the prototype's measurement is why: without
# the narrowing the queue held 17,774 items where the answer was 40. But a
# narrow default that HID the rest would be a queue that lied, so the wider
# counts are on the chooser itself. Nothing is hidden; it is deferred, and the
# number says by how much.
#
# ── DISMISSING IS A DECISION AND IT IS REVERSIBLE ──────────────────────────
#
# "This needs no answer" is a judgement, keyed on the Message-ID so it survives
# a --resync and so all three of Gmail's copies go at once. The pane keeps the
# key of the last one so it can be put back: a queue you cannot undo is one
# people are afraid to work through, and a queue nobody works through is the
# failure this layer exists to prevent.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout,
                               QWidget)

from ..store import triage as triage_repo

_SCOPE_LABELS = ((triage_repo.SCOPE_KNOWN, "People I have written to"),
                 (triage_repo.SCOPE_HUMAN, "Anything a person sent"),
                 (triage_repo.SCOPE_ALL, "Everything, lists included"))


class TriagePane(QWidget):
    """The queue, and the two things a person does with a row of it."""

    file_requested = Signal(int)          # message id → onto the chosen thread
    message_activated = Signal(int)
    status_message = Signal(str)
    changed = Signal()

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._theme = None
        self._last_dismissed = ""
        self.setMinimumWidth(260)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 6)
        outer.setSpacing(4)

        self.heading = QLabel("Needs filing", self)
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        outer.addWidget(self.heading)

        self.scope = QComboBox(self)
        self.scope.currentIndexChanged.connect(lambda _i: self.reload())
        outer.addWidget(self.scope)

        self.list = QListWidget(self)
        self.list.itemActivated.connect(self._activated)
        outer.addWidget(self.list, 1)

        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 2, 0, 0)
        self.file_button = QPushButton("File onto thread", bar)
        self.file_button.clicked.connect(self._file)
        row.addWidget(self.file_button)
        self.dismiss_button = QPushButton("No answer needed", bar)
        self.dismiss_button.clicked.connect(self._dismiss)
        row.addWidget(self.dismiss_button)
        self.undo_button = QPushButton("Undo", bar)
        self.undo_button.clicked.connect(self._undo)
        self.undo_button.setEnabled(False)
        row.addWidget(self.undo_button)
        row.addStretch(1)
        outer.addWidget(bar)

        self.footer = QLabel("", self)
        self.footer.setWordWrap(True)
        outer.addWidget(self.footer)
        self.reload()

    # ------------------------------------------------------------- contents
    def current_scope(self) -> str:
        data = self.scope.currentData()
        return data or triage_repo.SCOPE_KNOWN

    def reload(self) -> None:
        counts = triage_repo.counts(self._con)
        self._fill_scopes(counts)
        wanted = self.current_scope()
        items = triage_repo.queue(self._con, scope=wanted)
        self.list.clear()
        for item in items:
            row = QListWidgetItem(self._label(item), self.list)
            row.setData(Qt.ItemDataRole.UserRole, item.message_id)
            row.setToolTip(self._tooltip(item))
            if self._theme is not None and item.is_bounce:
                row.setForeground(QColor(self._theme.error))
            self.list.addItem(row)
        self.footer.setText(self._footer(counts, items))
        self.file_button.setEnabled(bool(items))
        self.dismiss_button.setEnabled(bool(items))

    def _fill_scopes(self, counts: dict) -> None:
        """Rebuild the chooser with the numbers in it, keeping the choice.

        The counts change on every reload — filing something takes it out of
        all three — and a chooser whose labels went stale would be the one part
        of the pane that lied.
        """
        wanted = self.current_scope()
        self.scope.blockSignals(True)
        self.scope.clear()
        for name, label in _SCOPE_LABELS:
            self.scope.addItem(f"{label} — {counts.get(name, 0)}", name)
        index = [n for n, _ in _SCOPE_LABELS].index(wanted)
        self.scope.setCurrentIndex(index)
        self.scope.blockSignals(False)

    def _label(self, item) -> str:
        age = item.age_days()
        parts = [item.label, "—", item.title]
        if age is not None:
            parts.append(f"({age}d)")
        if item.is_bounce:
            parts.append("— DELIVERY FAILED")
        return " ".join(parts)

    def _tooltip(self, item) -> str:
        lines = [item.title, f"From {item.from_addr}",
                 f"To {item.account_address} · {item.folder_label}"]
        if item.copies > 1:
            # Gmail keeps one message in INBOX, All Mail and Important at once.
            lines.append(f"{item.copies} copies of this message")
        if item.preview:
            lines.append("")
            lines.append(item.preview[:300])
        return "\n".join(lines)

    def _footer(self, counts: dict, items: list) -> str:
        """What the pane says when it is empty, and it is three different
        empties — nothing arrived, everything is filed, or the horizon is in
        front of it. "Nothing to show" for all three reads as broken."""
        if items:
            return f"Since {counts['since']}"
        if counts.get(triage_repo.SCOPE_ALL):
            return (f"Nothing in this scope. {counts[triage_repo.SCOPE_ALL]} "
                    f"in the widest one — try it above.")
        return (f"Everything since {counts['since']} is either filed or "
                f"dismissed.")

    def set_theme(self, theme) -> None:
        self._theme = theme
        if theme is not None:
            self.footer.setStyleSheet(f"color: {theme.text_muted};")
        self.reload()

    def selected_message(self) -> int | None:
        item = self.list.currentItem()
        return None if item is None else int(item.data(Qt.ItemDataRole.UserRole))

    # -------------------------------------------------------------- actions
    def _file(self) -> None:
        message_id = self.selected_message()
        if message_id is None:
            self.status_message.emit("Choose a message to file.")
            return
        self.file_requested.emit(int(message_id))

    def _dismiss(self) -> None:
        message_id = self.selected_message()
        if message_id is None:
            self.status_message.emit("Choose a message to set aside.")
            return
        key = triage_repo.dismiss(self._con, int(message_id),
                                 reason="no answer needed")
        self._last_dismissed = key
        self.undo_button.setEnabled(bool(key))
        self.status_message.emit("Set aside. Undo puts it back.")
        self.reload()
        self.changed.emit()

    def _undo(self) -> None:
        if not self._last_dismissed:
            return
        triage_repo.restore(self._con, self._last_dismissed)
        self._last_dismissed = ""
        self.undo_button.setEnabled(False)
        self.status_message.emit("Put back.")
        self.reload()
        self.changed.emit()

    def _activated(self, item) -> None:
        self.message_activated.emit(int(item.data(Qt.ItemDataRole.UserRole)))
