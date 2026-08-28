# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing one filter rule down.
#
# `ui/filtersdialog.py` is the list of rules and their order; this is one rule,
# opened from it. The store's side is `store/rules.py`, what a rule MEANS is
# `store/rulematch.py`, and what a match DOES is `store/rulerun.py`.
#
# ── THE SECOND BOX IS BUILT FROM THE FIRST ─────────────────────────────────
#
# `rulematch.ops_for` answers "which comparisons does this field offer", and
# the operator combo is rebuilt from it every time the field changes. So "has
# an attachment starts with" is not a rule somebody can write badly and then be
# told about: it is not a rule the interface can express at all. The value box
# disappears for a field that takes no value, for the same reason — a box that
# accepts text nothing reads is a box that has already misled somebody.
#
# ── A RULE IS EDITED IN MEMORY AND SAVED ONCE ──────────────────────────────
#
# Which is `save_rule`'s bargain from this end: the rows here are widgets and
# nothing reaches the store until Save. A half-written rule is therefore never
# a state the store can be in, and a rule being edited cannot fire in the
# middle of the edit — the sync runs the rules the store holds, not the ones on
# the screen.
#
# ── VALIDATION IS `store/rules.validate_here` AND NOT A SECOND OPINION ─────
#
# The same function the store calls, and it returns a sentence rather than a
# boolean. Writing the checks here instead would mean a rule refused by the
# dialog and accepted by an import, or the reverse — and the reverse is worse,
# because it is a rule sitting in the table that nothing will ever run.
#
# ── PREVIEW IS THE HONEST PART ─────────────────────────────────────────────
#
# A rule is a GUESS about a pattern in mail nobody has read. The only way to
# check one is against mail that already arrived, so the dialog will run the
# conditions — never the actions — over what the store holds and say how many
# it would have caught. A rule matching eleven thousand messages is a rule to
# think again about, and there is no other moment at which a person finds that
# out cheaply: after Save, they find out when their Inbox empties.
#
# ── THE DIALOGS ARE INJECTED ───────────────────────────────────────────────
#
# Debian ships no QTest, so a test cannot click a button in a QMessageBox.
# `confirm` is a parameter, as it is in `ui/tagsdialog.py` and
# `configure.add_account`, and every widget below is drivable from its own
# methods so that a test can drive the dialog rather than the store beneath it.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
                               QWidget)

from ..store import accounts as accounts_repo
from ..store import folders as folders_repo
from ..store import rulematch
from ..store import rulerun
from ..store import rules as rules_repo
from ..store import tags as tags_repo

# How far back a preview looks. `rulerun.preview` reads more rows than it
# returns, because the conditions are applied in Python; this is the number of
# MATCHES it stops at, and it is a number a person can act on — "more than 200"
# already says "think again".
PREVIEW_LIMIT = 200


def _warn(parent, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


class ConditionRow(QWidget):
    """One condition: what to look at, how to compare it, and to what."""

    def __init__(self, parent=None, *, on_remove=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)

        self.field = QComboBox(self)
        for name, label in rulematch.FIELDS.items():
            self.field.addItem(label, name)
        self.field.currentIndexChanged.connect(lambda _: self._field_changed())
        row.addWidget(self.field, 2)

        self.op = QComboBox(self)
        row.addWidget(self.op, 2)

        self.value = QLineEdit(self)
        row.addWidget(self.value, 3)

        self.remove = QPushButton("−", self)
        self.remove.setFixedWidth(28)
        self.remove.setToolTip("Take this condition out")
        if on_remove is not None:
            self.remove.clicked.connect(lambda: on_remove(self))
        row.addWidget(self.remove)
        self._field_changed()

    def _field_changed(self) -> None:
        """Rebuild the operator box, and show the value box only if it is read.

        The current operator is KEPT when the new field still offers it, so
        changing From to Subject does not silently reset "does not contain" to
        "contains" — which is a rule that then means the opposite of what it
        said a moment ago.
        """
        name = self.field.currentData()
        was = self.op.currentData()
        self.op.clear()
        for op in rulematch.ops_for(name):
            self.op.addItem(rulematch.OPS[op], op)
        if was is not None:
            index = self.op.findData(was)
            if index >= 0:
                self.op.setCurrentIndex(index)
        self.value.setVisible(rulematch.takes_value(name))

    def condition(self) -> rulematch.Condition:
        """What this row says, as a Condition.

        WHETHER THE VALUE COUNTS IS ASKED OF THE FIELD, NOT OF THE WIDGET.
        `self.value.isVisible()` looks like the same question and is not: a
        widget reports False whenever its window has not been SHOWN, so reading
        the rule out of a dialog that had not yet appeared — which is every
        test of it, and any caller that builds one to save it — silently
        dropped every value the user had typed. `rulematch.takes_value` is a
        fact about the field and is true whether or not anything is on screen.
        """
        name = self.field.currentData()
        return rulematch.Condition(
            field=name, op=self.op.currentData(),
            value=self.value.text() if rulematch.takes_value(name) else "")

    def set_condition(self, condition: rulematch.Condition) -> None:
        index = self.field.findData(condition.field)
        if index >= 0:
            self.field.setCurrentIndex(index)
        self._field_changed()
        index = self.op.findData(condition.op)
        if index >= 0:
            self.op.setCurrentIndex(index)
        self.value.setText(condition.value)


class ActionRow(QWidget):
    """One action, and the target it needs — which depends on which it is.

    THE TARGET BOX SWAPS RATHER THAN SITTING THERE GREYED. A move needs a
    folder, a tag needs a tag, putting something on the board needs a title,
    and the other six need nothing at all. A box that stayed visible and
    ignored would be a value somebody typed and expected to matter.
    """

    def __init__(self, con: sqlite3.Connection, parent=None, *,
                 on_remove=None) -> None:
        super().__init__(parent)
        self._con = con
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)

        self.kind = QComboBox(self)
        for name, label in rulematch.ACTIONS.items():
            self.kind.addItem(label, name)
        self.kind.currentIndexChanged.connect(lambda _: self._kind_changed())
        row.addWidget(self.kind, 2)

        self.folder = QComboBox(self)
        self._fill_folders()
        row.addWidget(self.folder, 3)

        self.tag = QComboBox(self)
        for tag in tags_repo.list_tags(con):
            self.tag.addItem(tag.name, tag.id)
        row.addWidget(self.tag, 3)

        self.title = QLineEdit(self)
        self.title.setPlaceholderText("what to call the thread")
        row.addWidget(self.title, 3)

        self.remove = QPushButton("−", self)
        self.remove.setFixedWidth(28)
        self.remove.setToolTip("Take this action out")
        if on_remove is not None:
            self.remove.clicked.connect(lambda: on_remove(self))
        row.addWidget(self.remove)
        self._kind_changed()

    def _fill_folders(self) -> None:
        """The roles first, then every real folder, each naming its account.

        THE ROLES ARE NOT A CONVENIENCE. A rule that runs against fifteen
        accounts and says "file this in Archive" means fifteen different
        folders, and the only way to write that once is to name the role and
        resolve it when the rule fires — `store/rulesschema.py` argues it at
        length. So they are at the top, where a person choosing a target for a
        cross-account rule meets them first.
        """
        for role in (folders_repo.ROLE_ARCHIVE, folders_repo.ROLE_TRASH,
                     folders_repo.ROLE_JUNK, folders_repo.ROLE_INBOX):
            label = folders_repo.ROLE_LABELS[role]
            self.folder.addItem(f"each account's {label}", f"role:{role}")
        for account in accounts_repo.list_accounts(self._con):
            for folder in folders_repo.list_folders(self._con, account.id):
                if folders_repo.is_local(folder.path):
                    continue        # a local Drafts or Sent is not a target
                self.folder.addItem(f"{account.address} — {folder.path}",
                                    f"folder:{folder.id}")

    def _kind_changed(self) -> None:
        kind = self.kind.currentData()
        self.folder.setVisible(kind == "move")
        self.tag.setVisible(kind == "tag")
        self.title.setVisible(kind == "track")

    def action(self) -> rulematch.Action:
        kind = self.kind.currentData()
        if kind == "move":
            what, _, which = (self.folder.currentData() or "role:").partition(":")
            if what == "folder" and which:
                return rulematch.Action(kind="move", folder_id=int(which))
            return rulematch.Action(kind="move", value=which)
        if kind == "tag":
            return rulematch.Action(kind="tag", tag_id=self.tag.currentData())
        if kind == "track":
            return rulematch.Action(kind="track", value=self.title.text().strip())
        return rulematch.Action(kind=kind)

    def set_action(self, action: rulematch.Action) -> None:
        index = self.kind.findData(action.kind)
        if index >= 0:
            self.kind.setCurrentIndex(index)
        if action.kind == "move":
            key = (f"folder:{int(action.folder_id)}"
                   if action.folder_id is not None
                   else f"role:{action.value.strip()}")
            index = self.folder.findData(key)
            if index >= 0:
                self.folder.setCurrentIndex(index)
        elif action.kind == "tag" and action.tag_id is not None:
            index = self.tag.findData(int(action.tag_id))
            if index >= 0:
                self.tag.setCurrentIndex(index)
        elif action.kind == "track":
            self.title.setText(action.value)
        self._kind_changed()


class RuleEditor(QDialog):
    """One rule: what it looks at, what it does, and what it would have caught."""

    def __init__(self, con: sqlite3.Connection, rule: rulematch.Rule | None = None,
                 parent=None, *, warn=_warn) -> None:
        super().__init__(parent)
        self._con = con
        self._warn = warn
        self._rule = rule or rulematch.Rule()
        self._conditions: list[ConditionRow] = []
        self._actions: list[ActionRow] = []
        self.saved: rulematch.Rule | None = None
        self.setWindowTitle("Filter rule" if rule is None else f"Rule: {rule.name}")
        self.setMinimumWidth(720)

        outer = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(self)
        form.addRow("&Name", self.name)

        self.account = QComboBox(self)
        # Every account is the DEFAULT and is first, because this is a client
        # built around a unified inbox: a rule about invoices is about
        # invoices, not about which of fifteen addresses they arrived at.
        self.account.addItem("every account", None)
        for account in accounts_repo.list_accounts(con):
            self.account.addItem(account.address, account.id)
        form.addRow("&Runs against", self.account)

        self.match_all = QComboBox(self)
        self.match_all.addItem("every condition holds", True)
        self.match_all.addItem("any condition holds", False)
        form.addRow("&Matches when", self.match_all)
        outer.addLayout(form)

        self._conditions_box = self._group("When", outer, self._add_condition,
                                           "Add a &condition")
        self._actions_box = self._group("Then", outer, self._add_action,
                                        "Add an &action")

        self.stop_after = QCheckBox("Stop here — rules below this one do not "
                                    "see a message this rule matched", self)
        outer.addWidget(self.stop_after)
        self.enabled = QCheckBox("Run this rule", self)
        self.enabled.setChecked(True)
        outer.addWidget(self.enabled)

        bar = QHBoxLayout()
        self.preview_button = QPushButton("&How many would this catch?", self)
        self.preview_button.setToolTip(
            "Try the conditions against the mail already here. Nothing is "
            "moved, tagged or marked.")
        self.preview_button.clicked.connect(lambda *_: self.preview())
        bar.addWidget(self.preview_button)
        self.preview_label = QLabel("", self)
        self.preview_label.setWordWrap(True)
        bar.addWidget(self.preview_label, 1)
        outer.addLayout(bar)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                               QDialogButtonBox.StandardButton.Cancel, self)
        box.accepted.connect(lambda *_: self.save())
        box.rejected.connect(self.reject)
        outer.addWidget(box)

        self.load(self._rule)

    def _group(self, title: str, outer: QVBoxLayout, adder, label: str) -> QVBoxLayout:
        group = QGroupBox(title, self)
        inner = QVBoxLayout(group)
        rows = QVBoxLayout()
        inner.addLayout(rows)
        button = QPushButton(label, group)
        button.clicked.connect(lambda *_: adder())
        line = QHBoxLayout()
        line.addWidget(button)
        line.addStretch(1)
        inner.addLayout(line)
        outer.addWidget(group)
        return rows

    # ------------------------------------------------------------- the rows
    def _add_condition(self, condition: rulematch.Condition | None = None) -> ConditionRow:
        row = ConditionRow(self, on_remove=self._drop_condition)
        if condition is not None:
            row.set_condition(condition)
        self._conditions.append(row)
        self._conditions_box.addWidget(row)
        return row

    def _drop_condition(self, row: ConditionRow) -> None:
        # THE LAST ONE MAY GO. A rule with no conditions matches nothing —
        # `store/rulematch.py` is explicit that it is never `all([])` — so an
        # empty rule is safe. It is KEPT rather than refused, because half a
        # rule is a rule somebody is still writing, and `Rule.is_complete` is
        # what stops it ever running; the list marks it HALF-WRITTEN and says
        # which half. Refusing the removal instead would trap somebody who
        # wants to start the conditions again from nothing.
        self._conditions.remove(row)
        row.setParent(None)
        row.deleteLater()

    def _add_action(self, action: rulematch.Action | None = None) -> ActionRow:
        row = ActionRow(self._con, self, on_remove=self._drop_action)
        if action is not None:
            row.set_action(action)
        self._actions.append(row)
        self._actions_box.addWidget(row)
        return row

    def _drop_action(self, row: ActionRow) -> None:
        self._actions.remove(row)
        row.setParent(None)
        row.deleteLater()

    # ------------------------------------------------------- the rule itself
    def load(self, rule: rulematch.Rule) -> None:
        self.name.setText(rule.name)
        index = self.account.findData(rule.account_id)
        self.account.setCurrentIndex(max(0, index))
        self.match_all.setCurrentIndex(0 if rule.match_all else 1)
        self.stop_after.setChecked(bool(rule.stop_after))
        self.enabled.setChecked(bool(rule.enabled))
        for row in list(self._conditions):
            self._drop_condition(row)
        for row in list(self._actions):
            self._drop_action(row)
        for condition in rule.conditions:
            self._add_condition(condition)
        for action in rule.actions:
            self._add_action(action)
        # A NEW RULE OPENS WITH ONE OF EACH rather than empty. Empty is a
        # correct state and a discouraging one: the first thing a person sees
        # would be two headings and two buttons.
        if not rule.conditions:
            self._add_condition()
        if not rule.actions:
            self._add_action()

    def rule(self) -> rulematch.Rule:
        """What the widgets currently say, as a Rule. Reads nothing."""
        return self._rule.with_changes(
            name=self.name.text().strip(),
            account_id=self.account.currentData(),
            enabled=self.enabled.isChecked(),
            match_all=bool(self.match_all.currentData()),
            stop_after=self.stop_after.isChecked(),
            conditions=tuple(row.condition() for row in self._conditions),
            actions=tuple(row.action() for row in self._actions))

    def save(self) -> None:
        rule = self.rule()
        problem = rules_repo.validate_here(self._con, rule)
        if problem:
            # A sentence, in the dialog, naming the field. The alternative —
            # saving it disabled and letting `--filters` report it — is how a
            # rule ends up in the table with nobody ever told why it does
            # nothing.
            self._warn(self, "This rule cannot be saved", problem)
            return
        self.saved = rules_repo.save_rule(self._con, rule)
        self.accept()

    def preview(self) -> int:
        """How many stored messages the CONDITIONS would catch. Changes nothing.

        The actions are not performed and are not consulted: a rule being
        written usually has none yet, and `rulerun.preview` supplies a harmless
        one so that a rule can be tried before it is finished.
        """
        rule = self.rule()
        if not rule.conditions:
            self.preview_label.setText("Add a condition first — a rule with "
                                       "none matches nothing.")
            return 0
        hits = rulerun.preview(self._con, rule, limit=PREVIEW_LIMIT)
        if not hits:
            self.preview_label.setText(
                "Nothing here matches it. That is not a mistake if the mail "
                "it is for has not arrived yet.")
        elif len(hits) >= PREVIEW_LIMIT:
            self.preview_label.setText(
                f"At least {PREVIEW_LIMIT} messages here match it — worth "
                f"reading again before you save it.")
        else:
            self.preview_label.setText(
                f"{len(hits)} message{'' if len(hits) == 1 else 's'} here "
                f"would have matched.")
        return len(hits)


def from_message(row) -> rulematch.Rule:
    """A rule started from a message somebody is looking at.

    Takes a `store/messages.Row` — what the list and the reading pane already
    hold — so that nothing here queries anything.

    THE SENDER, NOT THE SUBJECT. Every mail client offers "filter on this
    message" and the useful answer is almost always the address: a subject
    repeats across a conversation and then never again, while an address is
    what a person means by "these". The subject is offered as a second
    condition only when it survives having its Re:/Fwd: taken off, because
    `store/subject.py` already knows what a subject is once the prefixes are
    gone and matching on “Re: Re: Fwd: figures” is a rule that matches one
    message.
    """
    sender = (row.from_addr or "").strip()
    base = (row.subject_base or "").strip()
    conditions = []
    if sender:
        conditions.append(rulematch.Condition("from", "contains", sender))
    if not conditions and base:
        conditions.append(rulematch.Condition("subject", "contains", base))
    name = sender or base or "New rule"
    return rulematch.Rule(name=name, conditions=tuple(conditions))
