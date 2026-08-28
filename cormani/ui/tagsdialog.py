# SPDX-License-Identifier: GPL-3.0-or-later
#
# Managing tags: the names, the colours, and the keys 1-9.
#
# Five tags ship with the store and have since migration 3, which is why tagging
# worked from stage 1 without any of this. What was missing is everything after
# the first week: renaming one, giving it a key, choosing a colour that is not
# one of the five, and deleting the one that turned out to be a duplicate.
#
# IT WRITES AS YOU GO, and there is no OK button to make the changes real. A tag
# is a name, a colour and a key; there is nothing to validate across fields and
# nothing that only makes sense once the whole form is filled in. A dialog that
# collected all that and then applied it would need a cancel path that undid
# three tables' worth of nothing. Close is close.
#
# DELETING A TAG SAYS HOW MANY MESSAGES CARRY IT, and asks. The mail is not at
# risk — the schema cascades the message_tag rows and nothing else — but "Later"
# on four hundred messages is a decision the user made four hundred times, and
# an undo for it is not offered.
#
# THE DIALOGS ARE INJECTED, as everywhere else in this tree: Debian ships no
# QTest, so a test cannot click a colour out of QColorDialog. `ask_colour` and
# `confirm` are parameters, and the test passes functions.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from ..store import tags as tags_repo
from . import icons

# The keys a tag may claim. Nine, because that is what the keyboard offers
# without a modifier and what Thunderbird trained everyone to expect.
KEYS = (None, 1, 2, 3, 4, 5, 6, 7, 8, 9)


def _ask_colour(parent, current: str) -> str:
    from PySide6.QtWidgets import QColorDialog

    chosen = QColorDialog.getColor(QColor(current or "#888888"), parent,
                                   "Tag colour")
    return chosen.name() if chosen.isValid() else ""


def _confirm(parent, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.StandardButton.Yes |
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


class TagsDialog(QDialog):
    """The tag list, and one tag's fields beside it."""

    changed = Signal()                  # the store's tags are different now

    def __init__(self, con: sqlite3.Connection, parent=None, *,
                 ask_colour=_ask_colour, confirm=_confirm) -> None:
        super().__init__(parent)
        self._con = con
        self._ask_colour = ask_colour
        self._confirm = confirm
        self._loading = False
        self.setWindowTitle("Tags")
        self.setMinimumWidth(460)

        outer = QVBoxLayout(self)
        body = QHBoxLayout()
        outer.addLayout(body, 1)

        self.list = QListWidget(self)
        self.list.currentRowChanged.connect(lambda _: self._show_current())
        body.addWidget(self.list, 1)

        side = QWidget(self)
        form = QFormLayout(side)
        self.name = QLineEdit(side)
        self.name.editingFinished.connect(self._rename)
        form.addRow("&Name", self.name)

        self.colour = QPushButton("Colour…", side)
        self.colour.clicked.connect(self._pick_colour)
        form.addRow("&Colour", self.colour)

        self.key = QComboBox(side)
        for value in KEYS:
            self.key.addItem("None" if value is None else str(value), value)
        self.key.currentIndexChanged.connect(lambda _: self._set_key())
        form.addRow("&Key", self.key)

        self.used = QLabel("", side)
        form.addRow("", self.used)
        body.addWidget(side, 1)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("&Add", self)
        self.add_button.clicked.connect(self.add)
        buttons.addWidget(self.add_button)
        self.delete_button = QPushButton("&Delete", self)
        self.delete_button.clicked.connect(self.delete)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        self.note = QLabel("", self)
        buttons.addWidget(self.note)
        outer.addLayout(buttons)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        box.rejected.connect(self.accept)
        box.accepted.connect(self.accept)
        outer.addWidget(box)

        self.reload()

    # -------------------------------------------------------------- the list
    def reload(self, *, keep: int | None = None) -> None:
        self._loading = True
        try:
            keep = keep if keep is not None else self.current_id()
            self.list.clear()
            self._counts = tags_repo.message_counts(self._con)
            for tag in tags_repo.list_tags(self._con):
                item = QListWidgetItem(
                    f"{tag.name}    {tag.shortcut}" if tag.shortcut else tag.name)
                item.setData(Qt.ItemDataRole.UserRole, tag.id)
                item.setIcon(icons.icon("tag", tag.colour or "#888888", 14,
                                        filled=True))
                self.list.addItem(item)
                if tag.id == keep:
                    self.list.setCurrentItem(item)
            if self.list.currentRow() < 0 and self.list.count():
                self.list.setCurrentRow(0)
        finally:
            self._loading = False
        self._show_current()

    def current_id(self) -> int | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def current_tag(self):
        tag_id = self.current_id()
        return tags_repo.get_tag(self._con, tag_id) if tag_id is not None else None

    def _show_current(self) -> None:
        tag = self.current_tag()
        self._loading = True
        try:
            self.name.setText(tag.name if tag else "")
            self.key.setCurrentIndex(
                KEYS.index(tag.shortcut) if tag and tag.shortcut in KEYS else 0)
            self._paint_colour_button(tag.colour if tag else "")
            count = self._counts.get(tag.id, 0) if tag else 0
            self.used.setText(
                "" if tag is None else
                f"{count} message{'' if count == 1 else 's'} carry this tag")
            for widget in (self.name, self.colour, self.key, self.delete_button):
                widget.setEnabled(tag is not None)
        finally:
            self._loading = False

    def _paint_colour_button(self, colour: str) -> None:
        self.colour.setIcon(icons.icon("tag", colour or "#888888", 14, filled=True))
        self.colour.setText(colour or "Colour…")

    # ------------------------------------------------------------- the edits
    def _rename(self) -> None:
        tag = self.current_tag()
        if self._loading or tag is None or self.name.text().strip() == tag.name:
            return
        wanted = self.name.text().strip()
        taken = {t.name for t in tags_repo.list_tags(self._con) if t.id != tag.id}
        if wanted in taken:
            # The column is UNIQUE. Said rather than raised: a duplicate name is
            # a thing a person does, not an error condition.
            self._say(f"There is already a tag called {wanted}")
            self.name.setText(tag.name)
            return
        tags_repo.update_tag(self._con, tag.id, name=wanted)
        self._done(tag.id)

    def _pick_colour(self) -> None:
        tag = self.current_tag()
        if tag is None:
            return
        chosen = self._ask_colour(self, tag.colour)
        if not chosen:
            return
        tags_repo.update_tag(self._con, tag.id, colour=chosen)
        self._done(tag.id)

    def _set_key(self) -> None:
        tag = self.current_tag()
        if self._loading or tag is None:
            return
        wanted = self.key.currentData()
        if wanted == tag.shortcut:
            return
        displaced = tags_repo.update_tag(
            self._con, tag.id, shortcut=wanted, clear_shortcut=wanted is None)
        self._say(f"{displaced} no longer has a key" if displaced else "")
        self._done(tag.id)

    def add(self) -> int:
        name = tags_repo.unused_name(self._con)
        tag_id = tags_repo.add_tag(self._con, name, "#93a1a1")
        self._done(tag_id)
        self.name.setFocus()
        self.name.selectAll()
        return tag_id

    def delete(self) -> bool:
        tag = self.current_tag()
        if tag is None:
            return False
        count = self._counts.get(tag.id, 0)
        if not self._confirm(
                self, "Delete tag",
                f"Delete the tag {tag.name}?\n\n"
                f"{count} message{'' if count == 1 else 's'} carry it. The "
                f"messages are not touched — they stop carrying this tag, and "
                f"that cannot be undone."):
            return False
        tags_repo.delete_tag(self._con, tag.id)
        self._done(None)
        return True

    # --------------------------------------------------------------- telling
    def _done(self, keep: int | None) -> None:
        self.reload(keep=keep)
        self.changed.emit()

    def _say(self, text: str) -> None:
        self.note.setText(text)
