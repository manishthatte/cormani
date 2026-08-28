# SPDX-License-Identifier: GPL-3.0-or-later
#
# The search box across the top of the window, and its chips.
#
# The other half of the pair `ui/quickfilter.py` describes: that bar narrows
# what is already in front of you, this one finds a message you cannot see, in
# any folder of any account. The two are labelled so nobody has to guess which
# is which, and this one is deliberately the whole width of the window rather
# than the width of the list — it is not about the list.
#
# IT SEARCHES ON ENTER, NOT ON EVERY KEYSTROKE, AND THAT IS THE INDEX'S DOING.
# FTS5 matches whole terms unless a prefix is asked for, so a search-as-you-type
# box over this index shows nothing for `i`, `in`, `inv`, `invo`, `invoi` and
# `invoic` and then everything for `invoice` — six frames of "no results" while
# the user types a word that is in the store. Thunderbird's global search gets
# away with typing because it prefix-matches every term; doing that here would
# mean `-word` and `word*` no longer mean what the language says they mean, and
# a person who types two words would wait for the prefix expansion of both. So:
# Enter searches, and the placeholder says so.
#
# EMPTYING THE BOX LEAVES THE SEARCH IMMEDIATELY, without Enter. Nobody clears
# the box in order to search for nothing, and the clear button that Qt draws
# inside it has to do something — a ✕ that appears to work and does not is the
# defect this whole file is careful about elsewhere.
#
# THE CHIPS ARE FIELDS, NOT TEXT WRITTEN INTO THE BOX. Writing `from:lyle` into
# the user's own query would mean a chip could only be turned off by editing a
# string, and restoring a tab would restore a query that merely LOOKED like the
# chips it came from. `store/search.Query` therefore holds them separately and
# this bar is a view of that object.
#
# THE FIFTH CHIP IS NOT IN THE LAYOUT SKETCH AND IS HERE ON PURPOSE. Trash and
# Junk are excluded from a search by default — see store/search.py — and an
# exclusion the user cannot lift is a search box that cannot find a message
# they deleted. The list's footer names the number it is holding back, and this
# is the control that lets them have it.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QMenu, QToolButton,
                               QWidget, QWidgetAction)

from ..store import accounts as accounts_repo
from ..store import search as search_mod
from . import icons

# Wide enough for an address or a subject, narrow enough not to be a dialog.
_POPUP_WIDTH = 260


class _SearchEdit(QLineEdit):
    """The box itself. Escape gives up the search; Enter runs it."""

    escaped = Signal()

    def keyPressEvent(self, event) -> None:                     # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _TextChip(QToolButton):
    """A chip holding one line of text — From, and Subject.

    The value is edited in a QLineEdit inside the chip's own menu rather than in
    a dialog, because a dialog for one word is three clicks and a modal state
    for something the user is going to change again in four seconds.
    """

    changed = Signal()

    def __init__(self, label: str, placeholder: str, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setText(label)

        menu = QMenu(self)
        self.editor = QLineEdit(menu)
        self.editor.setPlaceholderText(placeholder)
        self.editor.setClearButtonEnabled(True)
        self.editor.setMinimumWidth(_POPUP_WIDTH)
        holder = QWidgetAction(menu)
        holder.setDefaultWidget(self.editor)
        menu.addAction(holder)
        self.setMenu(menu)

        self.editor.returnPressed.connect(self._commit)
        # The LABEL follows the editor, not the commit. Typing into the popup
        # and then clicking away leaves the text there, and `query` reads it
        # live — so a chip that only relabelled on Enter would sit there saying
        # "From" while the next search ran with a sender in it.
        self.editor.textChanged.connect(self.relabel)
        menu.aboutToShow.connect(self._focus_editor)

    def _focus_editor(self) -> None:
        self.editor.setFocus(Qt.FocusReason.PopupFocusReason)
        self.editor.selectAll()

    def _commit(self) -> None:
        menu = self.menu()
        if menu is not None:
            menu.close()
        self.relabel()
        self.changed.emit()

    # ------------------------------------------------------------------ value
    def value(self) -> str:
        return self.editor.text().strip()

    def set_value(self, text: str) -> None:
        """Without emitting — for restoring a tab, where the value came from the
        tab and not from the user."""
        self.editor.setText(text or "")
        self.relabel()

    def relabel(self) -> None:
        value = self.value()
        self.setText(f"{self._label}: {value}" if value else self._label)
        font = QFont(self.font())
        font.setBold(bool(value))
        self.setFont(font)


class _MenuChip(QToolButton):
    """A chip that picks one of a fixed set — Date, and Account.

    Fixed ranges rather than a date picker, and a menu rather than a combo box:
    a combo box that says "Any time" is a control shouting a default, and five
    entries fit in a menu that opens where the cursor already is.
    """

    changed = Signal()

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        self._value = None
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setText(label)

    def set_options(self, options) -> None:
        """(value, label) pairs, the first being the "no choice" one."""
        self._options = list(options)
        self._menu.clear()
        for value, label in self._options:
            entry = self._menu.addAction(label)
            entry.setCheckable(True)
            entry.setChecked(value == self._value)
            entry.triggered.connect(lambda _=False, v=value: self._choose(v))
        self.relabel()

    def _choose(self, value) -> None:
        self._value = value
        self.relabel()
        self.changed.emit()

    # ------------------------------------------------------------------ value
    def value(self):
        return self._value

    def set_value(self, value) -> None:
        self._value = value
        for entry, (option, _label) in zip(self._menu.actions(),
                                           getattr(self, "_options", ())):
            entry.setChecked(option == value)
        self.relabel()

    def relabel(self) -> None:
        chosen = ""
        for value, label in getattr(self, "_options", ()):
            if value == self._value and self._is_set(value):
                chosen = label
        self.setText(chosen or self._label)
        font = QFont(self.font())
        font.setBold(bool(chosen))
        self.setFont(font)

    @staticmethod
    def _is_set(value) -> bool:
        return value not in ("", None)


class SearchBar(QWidget):
    """The box, the chips, and one signal carrying the whole query."""

    changed = Signal(object)                    # search.Query

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._loading = False
        self._icon_action = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.text = _SearchEdit(self)
        self.text.setPlaceholderText(
            "Search all accounts…   Enter to search, Escape to come back")
        self.text.setClearButtonEnabled(True)
        self.text.setToolTip(
            "Searches every account's subjects, bodies and addresses.\n"
            "  two words        both, anywhere in the message\n"
            "  \"a phrase\"       those words, in that order\n"
            "  word*            anything starting with it\n"
            "  -word            not this\n"
            "  from: to: subject: body:   one part of the message\n"
            "To narrow what is already in view, use the Quick Filter "
            "above the list.")
        self.text.returnPressed.connect(self._emit)
        self.text.escaped.connect(self.clear)
        self.text.textChanged.connect(self._text_changed)
        layout.addWidget(self.text, 1)

        self.sender_chip = _TextChip("From", "Sender's name or address", self)
        self.sender_chip.setToolTip("Only messages from this person")
        self.subject_chip = _TextChip("Subject", "Words in the subject", self)
        self.subject_chip.setToolTip("Only messages whose subject says this")

        self.attachment_chip = QToolButton(self)
        self.attachment_chip.setText("Attachment")
        self.attachment_chip.setCheckable(True)
        self.attachment_chip.setToolTip("Only messages carrying an attachment")
        self.attachment_chip.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        self.date_chip = _MenuChip("Date", self)
        self.date_chip.setToolTip("Only messages from this period")
        self.date_chip.set_options(search_mod.WITHIN)

        self.account_chip = _MenuChip("Account", self)
        self.account_chip.setToolTip("Only this account")

        self.discarded_chip = QToolButton(self)
        self.discarded_chip.setText("Trash && Junk")
        self.discarded_chip.setCheckable(True)
        self.discarded_chip.setToolTip(
            "Also look in Trash and Junk, which a search leaves out")
        self.discarded_chip.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        self.chips = (self.sender_chip, self.subject_chip, self.attachment_chip,
                      self.date_chip, self.account_chip, self.discarded_chip)
        for chip in self.chips:
            layout.addWidget(chip)
            if isinstance(chip, (_TextChip, _MenuChip)):
                chip.changed.connect(self._emit)
            else:
                chip.toggled.connect(self._emit)

        self.clear_button = QToolButton(self)
        self.clear_button.setText("Clear")
        self.clear_button.setEnabled(False)
        self.clear_button.setToolTip("Stop searching and go back to the mail")
        self.clear_button.clicked.connect(self.clear)
        layout.addWidget(self.clear_button)

        self.reload_accounts()

    # ---------------------------------------------------------------- the query
    @property
    def query(self) -> search_mod.Query:
        """What the controls currently say. Read live rather than stored, so
        that pressing a chip searches the text already typed into the box —
        which is what the person looking at both of them expects."""
        return search_mod.Query(
            text=self.text.text().strip(),
            sender=self.sender_chip.value(),
            subject=self.subject_chip.value(),
            attachment=self.attachment_chip.isChecked(),
            within=self.date_chip.value() or "",
            account_id=self.account_chip.value(),
            discarded=self.discarded_chip.isChecked())

    def set_query(self, query: search_mod.Query) -> None:
        """Put the bar into a given state WITHOUT emitting — for restoring a
        tab, whose search came from the tab and not from this bar."""
        self._loading = True
        try:
            if self.text.text() != query.text:
                self.text.setText(query.text)
            self.sender_chip.set_value(query.sender)
            self.subject_chip.set_value(query.subject)
            self.attachment_chip.setChecked(query.attachment)
            self.date_chip.set_value(query.within or "")
            self.account_chip.set_value(query.account_id)
            self.discarded_chip.setChecked(query.discarded)
            self.clear_button.setEnabled(query.active)
        finally:
            self._loading = False

    def clear(self) -> None:
        """Give up the search. One key, because coming back to your mail should
        not be a tidying-up exercise."""
        was_active = self.query.active
        self.set_query(search_mod.Query())
        if was_active:
            self.changed.emit(search_mod.Query())

    def focus_text(self) -> None:
        self.text.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.text.selectAll()

    def _text_changed(self, value: str) -> None:
        # See the module header: emptying the box acts at once, while typing
        # into it waits for Enter. It does not always END the search — with a
        # chip still pressed, what is left is a search by that chip, and going
        # back to the whole mailbox would be a second thing the user did not
        # ask for. `Query.active` decides, and it decides in one place.
        if self._loading or value.strip():
            return
        self._emit()

    def _emit(self) -> None:
        if self._loading:
            return
        query = self.query
        self.clear_button.setEnabled(query.active)
        self.changed.emit(query)

    # -------------------------------------------------------------- accounts
    def reload_accounts(self) -> None:
        """The Account chip's menu. Rebuilt when an account is added, hidden or
        renamed — the same event the rail redraws on."""
        options = [(None, "All accounts")]
        options.extend(
            (account.id, account.label)
            for account in accounts_repo.list_accounts(
                self._con, include_hidden=False, include_disabled=False))
        self.account_chip.set_options(options)

    # --------------------------------------------------------------- theming
    def apply_theme(self, theme) -> None:
        """Recolour the drawn icons. See ui/icons.py — they are painted, so a
        theme change is a repaint rather than a second set of assets."""
        icon = icons.icon("search", theme.text_muted, 14)
        if self._icon_action is None:
            self._icon_action = self.text.addAction(
                icon, QLineEdit.ActionPosition.LeadingPosition)
        else:
            self._icon_action.setIcon(icon)
        self.attachment_chip.setIcon(
            icons.icon("paperclip", theme.text_strong, 14))
        self.discarded_chip.setIcon(icons.icon("trash", theme.text_strong, 14))
