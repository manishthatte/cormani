# SPDX-License-Identifier: GPL-3.0-or-later
#
# The filter rules, in the order they run.
#
# `ui/ruleeditor.py` is one rule; this is the list of them, and the list is
# where the two things a rule set has that a single rule does not both live:
# the ORDER, and the evidence that any of it is working.
#
# ── ORDER IS MEANING, SO IT IS EDITABLE AND IT IS NUMBERED ─────────────────
#
# `stop_after` lets a specific rule above a general one claim a message
# outright, which makes the order part of what the rules SAY rather than how
# they are displayed. So Up and Down write to the store — `rules.reorder` — and
# the position is drawn beside each rule. A list that could be sorted by name
# would be a list that quietly rewrote the rules.
#
# ── EVERY ROW CARRIES ITS MATCH COUNT ──────────────────────────────────────
#
# A filter is invisible when it works and invisible when it does not: mail a
# rule moved and mail no rule looked at are both just mail in a folder. The two
# integers `filter_rule` keeps are the only evidence there is, so they are on
# the row rather than a page away — and "matched nothing yet" beside a rule
# written six months ago is the single most useful sentence this dialog can
# print. `store/rulesschema.py` argues for the counters instead of an audit
# log; this is the other place that spends them, beside `--filters`.
#
# ── THE TICK BOX IS THE RULE'S OWN `enabled` ───────────────────────────────
#
# Not a view state. Turning a rule off is the answer to "is this what is moving
# my mail?", it must survive closing the dialog, and it must be visible from
# `--filters` in a terminal — which is where somebody looks when the interface
# is not the thing they can reach.
#
# ── DELETING ASKS, AND SAYS WHAT IS LOST ───────────────────────────────────
#
# The mail is never at risk: the schema cascades the conditions and the actions
# and touches no message. What is lost is the rule itself and the count of what
# it did, and there is no undo for it — `store/undo.py` is about actions on
# mail, and a deleted rule is not one. So the question names the rule.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QVBoxLayout)

from ..store import accounts as accounts_repo
from ..store import rules as rules_repo
from . import ruleeditor


def _confirm(parent, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.StandardButton.Yes |
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


class FiltersDialog(QDialog):
    """Every rule, in the order they run, with what each has actually done."""

    def __init__(self, con: sqlite3.Connection, parent=None, *,
                 confirm=_confirm, editor=None) -> None:
        super().__init__(parent)
        self._con = con
        self._confirm = confirm
        # The editor is injected for the reason the dialogs are: a test has no
        # QTest to press Save with, so it passes something that saves.
        self._editor = editor or self._open_editor
        self._loading = False
        self.setWindowTitle("Message filters")
        self.setMinimumSize(680, 420)

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(
            "Rules run in this order on mail as it arrives, in the Inbox of "
            "the account they name.", self))

        body = QHBoxLayout()
        self.list = QListWidget(self)
        self.list.itemChanged.connect(self._toggled)
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        self.list.currentRowChanged.connect(lambda _: self._enable_buttons())
        body.addWidget(self.list, 1)

        side = QVBoxLayout()
        for label, slot in (("&New…", self.new), ("&Edit…", self.edit),
                            ("&Delete", self.delete), ("Move &up", self.up),
                            ("Move &down", self.down)):
            button = QPushButton(label, self)
            # `*_` and not a bare slot: QPushButton.clicked carries a `checked`
            # bool, and `new` takes an optional rule — connected directly, a
            # click would pass False as the rule to edit.
            button.clicked.connect(lambda *_, s=slot: s())
            side.addWidget(button)
            setattr(self, f"button_{slot.__name__}", button)
        side.addStretch(1)
        body.addLayout(side)
        outer.addLayout(body, 1)

        self.note = QLabel("", self)
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
            accounts = {a.id: a.address
                        for a in accounts_repo.list_accounts(self._con)}
            for position, rule in enumerate(rules_repo.list_rules(self._con),
                                            start=1):
                item = QListWidgetItem(self._label(rule, position, accounts))
                item.setData(Qt.ItemDataRole.UserRole, rule.id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if rule.enabled
                                   else Qt.CheckState.Unchecked)
                self.list.addItem(item)
                if rule.id == keep:
                    self.list.setCurrentItem(item)
            if self.list.currentRow() < 0 and self.list.count():
                self.list.setCurrentRow(0)
        finally:
            self._loading = False
        self._enable_buttons()

    def _label(self, rule, position: int, accounts: dict) -> str:
        scope = ("every account" if rule.account_id is None
                 else accounts.get(rule.account_id, "an account that is gone"))
        head = f"{position}.  {rule.name}    [{scope}]"
        if rule.stop_after:
            head += "    stops the run"
        if not rule.is_complete:
            head += f"    HALF-WRITTEN — {rule.incomplete_reason}"
        did = ("matched nothing yet" if not rule.match_count
               else f"matched {rule.match_count} time"
                    f"{'' if rule.match_count == 1 else 's'}"
                    f"{', last on ' + rule.last_matched_at[:10] if rule.last_matched_at else ''}")
        return f"{head}\n     {rule.describe()}\n     {did}"

    def current_id(self) -> int | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def current_rule(self):
        rule_id = self.current_id()
        return (rules_repo.get_rule(self._con, rule_id)
                if rule_id is not None else None)

    def _enable_buttons(self) -> None:
        row, last = self.list.currentRow(), self.list.count() - 1
        chosen = row >= 0
        self.button_edit.setEnabled(chosen)
        self.button_delete.setEnabled(chosen)
        self.button_up.setEnabled(chosen and row > 0)
        self.button_down.setEnabled(chosen and row < last)

    # ------------------------------------------------------------- the edits
    def _toggled(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        wanted = item.checkState() == Qt.CheckState.Checked
        rules_repo.set_enabled(self._con, rule_id, wanted)
        self.note.setText(
            f"“{rules_repo.get_rule(self._con, rule_id).name}” "
            f"{'will run' if wanted else 'will not run'} on the next sync.")
        self.reload(keep=rule_id)

    def _open_editor(self, rule):
        dialog = ruleeditor.RuleEditor(self._con, rule, self)
        dialog.exec()
        return dialog.saved

    def new(self, rule=None) -> None:
        saved = self._editor(rule)
        if saved is not None:
            self.reload(keep=saved.id)

    def edit(self) -> None:
        rule = self.current_rule()
        if rule is None:
            return
        saved = self._editor(rule)
        self.reload(keep=saved.id if saved is not None else rule.id)

    def delete(self) -> None:
        rule = self.current_rule()
        if rule is None:
            return
        did = ("It has never matched anything." if not rule.match_count
               else f"It has matched {rule.match_count} time"
                    f"{'' if rule.match_count == 1 else 's'}, and that count "
                    f"goes with it.")
        if not self._confirm(self, "Delete this rule?",
                             f"“{rule.name}” will be deleted. {did}\n\n"
                             f"No mail is touched, and this cannot be undone."):
            return
        rules_repo.delete_rule(self._con, rule.id)
        self.note.setText(f"“{rule.name}” is gone.")
        self.reload(keep=None)

    def up(self) -> None:
        self._move(-1)

    def down(self) -> None:
        self._move(+1)

    def _move(self, step: int) -> None:
        """Swap this rule with its neighbour, and write the whole order down.

        `rules.reorder` takes the WHOLE list rather than a rule and a position,
        because `sort_order` is only meaningful as a sequence: writing one row's
        number leaves the others to be guessed at, and two rules with the same
        number run in an order nobody chose.
        """
        rules = rules_repo.list_rules(self._con)
        ids = [r.id for r in rules]
        rule_id = self.current_id()
        if rule_id is None:
            return
        index = ids.index(rule_id)
        target = index + step
        if not 0 <= target < len(ids):
            return
        ids[index], ids[target] = ids[target], ids[index]
        rules_repo.reorder(self._con, ids)
        self.reload(keep=rule_id)
