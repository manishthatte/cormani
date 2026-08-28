# SPDX-License-Identifier: GPL-3.0-or-later
#
# The saved searches: what each one asks, where it is drawn, and what order.
#
# `ui/filtersdialog.py`'s shape, and deliberately NOT its twin, because a saved
# search and a filter rule differ in the one thing that dialog is built around.
#
# ── THE ORDER HERE IS PRESENTATION, AND IT SAYS SO ─────────────────────────
#
# A filter's order is MEANING — `stop_after` lets a rule above another claim a
# message outright — so `ui/filtersdialog.py` numbers its list and calls the
# number part of what the rules say. Nothing about a saved search depends on
# which came first: they do not interact, and the order is only where the user
# put their virtual folders in the rail. It is still written to the store,
# because where somebody put them is theirs and must survive a restart, and it
# is still Up and Down rather than a sortable column, for the same reason as
# there: a list that re-sorted itself would move the rail under them.
#
# ── THERE IS NO EDITOR, AND THAT IS THE DESIGN ─────────────────────────────
#
# `ui/ruleeditor.py` exists because a rule cannot be demonstrated — you write
# conditions in a form and hope. A saved search CAN be: it is a view, so the
# way to change one is to open it, adjust the search box and the Quick Filter
# until the list is right, and save it again over the same name. The screen is
# the editor. So this dialog renames, reorders, hides and deletes, and the one
# thing it does not offer is a second, worse way to express a query — which is
# also why `saved_view` holds JSON and not columns.
#
# The consequence is stated where somebody will read it: the note under the
# list says how to change what one asks for. A dialog with no Edit button and
# no explanation reads as a dialog missing its Edit button.
#
# ── A VIEW THAT CANNOT MEAN WHAT IT SAYS IS SHOWN, NOT SWEPT UP ────────────
#
# `savedviews.unresolved` names the folder, account or tag that has gone. The
# row says so, and nothing here deletes it on the user's behalf: the folder
# comes back when the account is re-added, and a saved search that vanished
# because a server was offline once would be the client discarding something a
# person made.
#
# ── THE TICK BOX IS `in_rail`, WHICH IS NOT `enabled` ──────────────────────
#
# A filter switched off does not run. A saved search taken out of the rail runs
# whenever it is opened — it is still in the Search menu — and the box means
# "draw it in the rail", nothing more. Three virtual folders are a section and
# thirty are a wall, which is the whole reason the column exists.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton,
                               QVBoxLayout)

from ..store import savedviews as savedviews_repo


def _confirm(parent, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.StandardButton.Yes |
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def _ask_name(parent, title: str, label: str, initial: str) -> str:
    text, said_yes = QInputDialog.getText(parent, title, label, text=initial)
    return text.strip() if said_yes else ""


class SavedViewsDialog(QDialog):
    """Every saved search: what it asks, whether it is in the rail, and where."""

    def __init__(self, con: sqlite3.Connection, parent=None, *,
                 confirm=_confirm, ask_name=_ask_name) -> None:
        super().__init__(parent)
        self._con = con
        self._confirm = confirm
        self._ask_name = ask_name
        self._loading = False
        self.setWindowTitle("Saved searches")
        self.setMinimumSize(640, 400)

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(
            "Searches you have named. A ticked one is drawn in the rail as a "
            "virtual folder; the rest are in the Search menu.", self))

        body = QHBoxLayout()
        self.list = QListWidget(self)
        self.list.itemChanged.connect(self._toggled)
        self.list.itemDoubleClicked.connect(lambda _: self.rename())
        self.list.currentRowChanged.connect(lambda _: self._enable_buttons())
        body.addWidget(self.list, 1)

        side = QVBoxLayout()
        for label, slot in (("Re&name…", self.rename), ("&Delete", self.delete),
                            ("Move &up", self.up), ("Move &down", self.down)):
            button = QPushButton(label, self)
            # `*_` and not a bare slot — QPushButton.clicked carries a `checked`
            # bool and PySide6 hands it to any slot that will take it.
            button.clicked.connect(lambda *_, s=slot: s())
            side.addWidget(button)
            setattr(self, f"button_{slot.__name__}", button)
        side.addStretch(1)
        body.addLayout(side)
        outer.addLayout(body, 1)

        self.note = QLabel(
            "To change what one of these asks for, open it, adjust the search "
            "and the Quick Filter until the list is right, and save it again "
            "under the same name.", self)
        self.note.setWordWrap(True)
        outer.addWidget(self.note)

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
            for view in savedviews_repo.list_views(self._con):
                item = QListWidgetItem(self._label(view))
                item.setData(Qt.ItemDataRole.UserRole, view.id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if view.in_rail
                                   else Qt.CheckState.Unchecked)
                self.list.addItem(item)
                if view.id == keep:
                    self.list.setCurrentItem(item)
            if self.list.currentRow() < 0 and self.list.count():
                self.list.setCurrentRow(0)
        finally:
            self._loading = False
        self._enable_buttons()

    def _label(self, view) -> str:
        """Three lines: the name, what it asks, and how many it holds now.

        THE COUNT IS EXACT HERE and capped in the rail, and the difference is
        deliberate: this dialog is opened by hand and shows a dozen rows, so it
        can afford the query the rail cannot afford on every rebuild.
        `store/savedviews.count_capped` has the measurement.
        """
        wrong = savedviews_repo.unresolved(self._con, view)
        if wrong:
            return (f"{view.name}\n     {view.describe()}\n"
                    f"     THIS CANNOT RUN — {wrong}")
        held = savedviews_repo.count_in(self._con, view)
        where = savedviews_repo.describe_scope_here(self._con, view)
        narrowing = view.narrowing()
        asks = f"{where} · {narrowing}" if narrowing else where
        return (f"{view.name}\n     {asks}\n"
                f"     {held} message{'' if held == 1 else 's'} right now")

    def current_id(self) -> int | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def current_view(self):
        view_id = self.current_id()
        return (savedviews_repo.get_view(self._con, view_id)
                if view_id is not None else None)

    def _enable_buttons(self) -> None:
        row, last = self.list.currentRow(), self.list.count() - 1
        chosen = row >= 0
        self.button_rename.setEnabled(chosen)
        self.button_delete.setEnabled(chosen)
        self.button_up.setEnabled(chosen and row > 0)
        self.button_down.setEnabled(chosen and row < last)

    # ------------------------------------------------------------- the edits
    def _toggled(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        view_id = item.data(Qt.ItemDataRole.UserRole)
        wanted = item.checkState() == Qt.CheckState.Checked
        savedviews_repo.set_in_rail(self._con, view_id, wanted)
        view = savedviews_repo.get_view(self._con, view_id)
        where = "in the rail" if wanted else "in the Search menu only"
        self.note.setText(f"“{view.name}” is {where}.")
        self.reload(keep=view_id)

    def rename(self) -> None:
        view = self.current_view()
        if view is None:
            return
        name = self._ask_name(self, "Rename this saved search",
                              "A new name for it:", view.name)
        if not name or name == view.name:
            return
        try:
            savedviews_repo.rename(self._con, view.id, name)
        except ValueError as problem:
            # The name is UNIQUE and the store refuses rather than choosing —
            # `store/savedviews.py`'s header. Said here, beside the list, and
            # not raised past a dialog the user is standing in.
            self.note.setText(str(problem))
            return
        self.note.setText(f"Renamed to “{name}”.")
        self.reload(keep=view.id)

    def delete(self) -> None:
        view = self.current_view()
        if view is None:
            return
        if not self._confirm(
                self, "Delete this saved search?",
                f"“{view.name}” asks for {view.describe()}.\n\nDeleting it "
                f"removes the search, never the mail. It cannot be undone."):
            return
        savedviews_repo.delete_view(self._con, view.id)
        self.note.setText(f"“{view.name}” is gone. No mail was touched.")
        self.reload(keep=None)

    def up(self) -> None:
        self._move(-1)

    def down(self) -> None:
        self._move(+1)

    def _move(self, step: int) -> None:
        """Swap with the neighbour and write the WHOLE order down.

        `savedviews.reorder` takes the whole list rather than one row and a
        position, for `rules.reorder`'s reason: `sort_order` is only meaningful
        as a sequence, and two rows carrying the same number are drawn in an
        order nobody chose.
        """
        ids = [v.id for v in savedviews_repo.list_views(self._con)]
        view_id = self.current_id()
        if view_id is None or view_id not in ids:
            return
        index = ids.index(view_id)
        target = index + step
        if not 0 <= target < len(ids):
            return
        ids[index], ids[target] = ids[target], ids[index]
        savedviews_repo.reorder(self._con, ids)
        self.reload(keep=view_id)
