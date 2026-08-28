# SPDX-License-Identifier: GPL-3.0-or-later
#
# Answering without leaving the message.
#
# THE COMMONEST REPLY IS ONE SENTENCE. "Thursday works", "yes, please send it",
# "that is the wrong invoice" — and a window opening for each of those is three
# actions where one would do. PLAN.txt §3 draws this box under the reading pane
# for exactly that reason, and every client that has one is faster than every
# client that does not.
#
# IT IS THE SAME REPLY THE COMPOSER WOULD MAKE. The text typed here goes ABOVE
# the quotation and the signature that `compose/quote.py` derives, so what
# arrives is indistinguishable from a reply written in the window — the
# recipients, the References chain and the subject are the same because they are
# derived by the same function. The box does not have its own idea of a reply.
#
# AND IT HANDS OVER RATHER THAN GROWING. Adding a recipient, an attachment or a
# second paragraph is what the composer is for, so "Open in composer" carries
# whatever has been typed and the box empties. A one-line box that slowly grew
# fields would end up as a worse composer under a smaller pane.
#
# CTRL+ENTER SENDS, which is what it does in the composer and in every other
# mail client. Enter alone is a newline: a reply sent by the key people press to
# start a second sentence is a reply nobody presses that key near again.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

# Three lines. Enough for the sentence this exists for, small enough that the
# message stays the thing on the screen.
LINES = 3


class InlineReply(QWidget):
    """A short answer, under the message it answers."""

    send_requested = Signal(str)        # the text typed
    expand_requested = Signal(str)      # the text, for a composer to take over

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._row = None

        self.frame = QFrame(self)
        self.frame.setFrameShape(QFrame.Shape.StyledPanel)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.frame)

        inner = QVBoxLayout(self.frame)
        inner.setContentsMargins(8, 6, 8, 6)
        inner.setSpacing(4)

        self.text = QPlainTextEdit(self.frame)
        self.text.setPlaceholderText("Reply inline…")
        self.text.setTabChangesFocus(True)
        metrics = self.text.fontMetrics()
        self.text.setFixedHeight(metrics.lineSpacing() * LINES + 12)
        inner.addWidget(self.text)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.send_button = QPushButton("Send reply", self.frame)
        self.send_button.clicked.connect(self.send)
        row.addWidget(self.send_button)
        self.expand_button = QPushButton("Open in composer", self.frame)
        self.expand_button.clicked.connect(self.expand)
        row.addWidget(self.expand_button)
        row.addStretch(1)
        inner.addLayout(row)

        send = QAction("Send reply", self)
        send.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
        send.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        send.triggered.connect(self.send)
        self.addAction(send)

        self.text.textChanged.connect(self._update_buttons)
        self.set_message(None)

    # ----------------------------------------------------------------- state
    def set_message(self, row) -> None:
        """Point the box at a message, or at nothing.

        The text is CLEARED when the message changes, and that is deliberate:
        half a sentence meant for one correspondent, left in the box while
        another message is opened, is how a reply goes to the wrong person.
        """
        changed = getattr(row, "id", None) != getattr(self._row, "id", None)
        self._row = row
        if changed:
            self.text.clear()
        self.setVisible(row is not None)
        if row is not None:
            who = row.correspondent or row.from_addr
            self.text.setPlaceholderText(f"Reply inline to {who}…")
        self._update_buttons()

    @property
    def message(self):
        return self._row

    def _update_buttons(self) -> None:
        has_text = bool(self.text.toPlainText().strip())
        self.send_button.setEnabled(has_text and self._row is not None)
        self.expand_button.setEnabled(self._row is not None)

    # --------------------------------------------------------------- actions
    def send(self) -> bool:
        text = self.text.toPlainText().strip()
        if not text or self._row is None:
            return False
        self.send_requested.emit(text)
        return True

    def expand(self) -> bool:
        if self._row is None:
            return False
        text = self.text.toPlainText().strip()
        self.text.clear()
        self.expand_requested.emit(text)
        return True

    def clear(self) -> None:
        self.text.clear()

    def apply_theme(self, theme) -> None:
        self.frame.setStyleSheet(
            f"QFrame {{ background: {theme.surface_raised}; "
            f"border: 1px solid {theme.border}; border-radius: 6px; }}")
        self.text.setStyleSheet(
            f"QPlainTextEdit {{ border: 1px solid {theme.border}; "
            f"background: {theme.surface}; border-radius: 4px; padding: 4px; }}")
