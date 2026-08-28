# SPDX-License-Identifier: GPL-3.0-or-later
#
# The message list: how it behaves.
#
# HOVER IS TRACKED BY THE VIEW, NOT THE DELEGATE. QAbstractItemView sends mouse
# press, release and double-click to a delegate's editorEvent, but not mouse
# MOVE — so a delegate cannot know which of its four buttons the cursor is over.
# The view therefore owns the tracking and tells the delegate what to highlight.
# The view also swallows a press that lands on an action, because otherwise
# clicking "archive" would first select the row it is about to remove.
#
# THE CURSOR IS THE VIEW'S, and that is why `select`, `index_near` and
# `selected_ids` live here rather than in the pane. Where the cursor goes after
# a row is removed, what a collapsed conversation stands for, whether a message
# inside one can be reached at all — every one of those is a question about the
# widget's own state, and answering them from outside meant reaching through it
# for its selection model and its expanded rows.
#
# A TREE, AND THREADED SINCE STAGE 3. `ui/messagerow.py` draws a row; this file
# is the list those rows are in.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QItemSelectionModel, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QTreeView

from . import theme as theme_mod
from .messagerow import (HOVER_ACTIONS, MessageDelegate, action_rects,
                         action_strip_left, format_date, to_local)
from .models import messages as message_model

__all__ = ["MessageList", "MessageDelegate", "HOVER_ACTIONS", "action_rects",
           "action_strip_left", "format_date", "to_local"]


class MessageList(QTreeView):
    """The list pane. A tree, flat today, because stage 3 threads it."""

    # The IDS a hover action applies to, not one id: the button is on a row,
    # and a collapsed conversation is a row that stands for several messages.
    # The keyboard and the reading pane already meant the conversation; a hover
    # click that meant one message of it would be the same command doing two
    # different things depending on where it was pressed from.
    action_requested = Signal(str, list)       # action id, message ids
    open_requested = Signal(int)               # message id, for a new tab

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("messageList")
        self.setHeaderHidden(True)
        # The twisty. Only ever drawn beside a row that has children, so an
        # unthreaded list looks exactly as it did — Qt indents the tree by the
        # same amount either way, which is why `setIndentation` follows the
        # density rather than being left at the style's default.
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setMouseTracking(True)
        self.setAlternatingRowColors(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._delegate = MessageDelegate(self)
        self.setItemDelegate(self._delegate)
        self._pressed_action = ""
        self._empty_text = "No messages"
        self.doubleClicked.connect(self._double_clicked)

    # ------------------------------------------------------------ appearance
    def set_theme(self, theme) -> None:
        self._delegate.theme = theme
        self.viewport().update()

    def set_density(self, density) -> None:
        self._delegate.set_density(density)
        self.scheduleDelayedItemsLayout()

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self.viewport().update()

    def set_show_location(self, show: bool) -> None:
        """Whether each row names the folder and account it is in. On while a
        search is showing, off in a folder."""
        if show == self._delegate.show_location:
            return
        self._delegate.show_location = show
        self.viewport().update()

    @property
    def delegate(self) -> MessageDelegate:
        return self._delegate

    # ------------------------------------------------------------- selection
    def select(self, index) -> bool:
        """Put the cursor on a row and make it visible.

        Expands the conversation a row is inside first: a message inside a
        collapsed thread cannot hold the cursor while it is not drawn, so `n`
        from the row above would appear to do nothing at all.
        """
        if not isinstance(index, QModelIndex) or not index.isValid():
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())
            return False
        parent = index.parent()
        if parent.isValid():
            self.expand(parent)
        self.setCurrentIndex(index)
        selection = self.selectionModel()
        if selection is not None:
            selection.select(
                index, QItemSelectionModel.SelectionFlag.ClearAndSelect |
                QItemSelectionModel.SelectionFlag.Rows)
        self.scrollTo(index)
        return True

    def clear(self) -> None:
        selection = self.selectionModel()
        if selection is not None:
            selection.clearSelection()
        self.setCurrentIndex(QModelIndex())

    def select_message(self, message_id: int) -> bool:
        model = self.model()
        return bool(model is not None
                    and self.select(model.index_of(message_id)))

    def select_row(self, position: int) -> bool:
        """The nth row at the top level. What "the first message" means."""
        model = self.model()
        if model is None or not 0 <= position < model.rowCount():
            return False
        return self.select(model.index(position, 0))

    def index_near(self, parent_id: int | None, position: int) -> QModelIndex:
        """Where the cursor goes after the row under it left.

        The same PLACE rather than the same message: archiving a run of mail is
        one key press each only if the next one is already under the cursor.
        Falls back to the conversation the row was in, and then to nothing.
        """
        model = self.model()
        if model is None:
            return QModelIndex()
        parent = (model.index_of(parent_id) if parent_id is not None
                  else QModelIndex())
        count = model.rowCount(parent)
        if count == 0:
            return parent if parent.isValid() else QModelIndex()
        return model.index(max(0, min(position, count - 1)), 0, parent)

    def cursor_place(self) -> tuple:
        """(conversation, position) — enough to find the cursor's place again
        after the row it was on has gone."""
        model = self.model()
        current = self.currentIndex()
        if model is None or not current.isValid():
            return (None, 0)
        parent_row = model.row_at(current.parent())
        return (parent_row.id if parent_row is not None else None, current.row())

    def current_row(self):
        model = self.model()
        return model.row_at(self.currentIndex()) if model is not None else None

    def ids_at(self, index) -> list[int]:
        """What one row stands for when it is acted on.

        A COLLAPSED conversation stands for the messages inside it. Acting on
        the visible row alone would archive one message of five and leave the
        row in place showing the next — which reads as a command that did not
        work. Expanded, a row is itself and nothing more.
        """
        model = self.model()
        if model is None or not index.isValid():
            return []
        if model.rowCount(index) and not self.isExpanded(index):
            return model.thread_ids(index)
        return model.ids_for([index])

    def selected_ids(self) -> list[int]:
        """What the next action applies to: every selected row, resolved."""
        model, selection = self.model(), self.selectionModel()
        if model is None or selection is None:
            return []
        out: list[int] = []
        for index in selection.selectedRows():
            for one in self.ids_at(index):
                if one not in out:
                    out.append(one)
        return out

    # ----------------------------------------------------------------- hover
    def _action_at(self, point: QPoint) -> tuple[QModelIndex, str]:
        index = self.indexAt(point)
        if not index.isValid():
            return index, ""
        rect = self.visualRect(index)
        for action_id, box in action_rects(rect, self._delegate.density).items():
            if box.contains(point):
                return index, action_id
        return index, ""

    def mouseMoveEvent(self, event) -> None:                    # noqa: N802
        index, action = self._action_at(event.position().toPoint())
        row = index.row() if index.isValid() else -1
        if row != self._delegate.hover_row or action != self._delegate.hover_action:
            previous = self._delegate.hover_row
            self._delegate.hover_row = row
            self._delegate.hover_action = action
            for repaint in {previous, row}:
                if repaint >= 0:
                    self.viewport().update(self.visualRect(self.model().index(repaint, 0)))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:                # noqa: N802
        if self._delegate.hover_row >= 0:
            stale = self._delegate.hover_row
            self._delegate.hover_row = -1
            self._delegate.hover_action = ""
            self.viewport().update(self.visualRect(self.model().index(stale, 0)))
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:                   # noqa: N802
        index, action = self._action_at(event.position().toPoint())
        if action and event.button() == Qt.MouseButton.LeftButton:
            # Swallowed: clicking Archive must not first select the row it is
            # about to take away.
            self._pressed_action = action
            event.accept()
            return
        self._pressed_action = ""
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:                 # noqa: N802
        if self._pressed_action:
            index, action = self._action_at(event.position().toPoint())
            pressed, self._pressed_action = self._pressed_action, ""
            if action == pressed and index.isValid():
                ids = self.ids_at(index)
                if ids:
                    self.action_requested.emit(pressed, ids)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _double_clicked(self, index: QModelIndex) -> None:
        message_id = index.data(message_model.MessageIdRole)
        if message_id is not None:
            self.open_requested.emit(int(message_id))

    # ----------------------------------------------------------- empty state
    def paintEvent(self, event) -> None:                        # noqa: N802
        super().paintEvent(event)
        model = self.model()
        if model is not None and model.rowCount() > 0:
            return
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor(self._delegate.theme.text_muted)))
        painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                         self._empty_text)
        painter.end()
