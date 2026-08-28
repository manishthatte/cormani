# SPDX-License-Identifier: GPL-3.0-or-later
#
# The Quick Filter bar.
#
# Thunderbird's, and it is the single fastest way to cut a thousand messages to
# six. Five toggles and a text box, above the list, always visible.
#
# THE TEXT BOX IS NOT THE SEARCH BOX, and the two are labelled so that nobody
# has to guess which is which. This one filters the messages already in view, by
# substring, and is instant. The one across the top of the window searches every
# account's full text and arrives with stage 3. Thunderbird ships both for the
# same reason: narrowing what is in front of you and finding something you
# cannot see are different acts, and a single control that tries to be both is
# slower at the first and worse at the second.
#
# THE TAG TOGGLE HAS A MENU. Pressed on its own it means "carries any tag";
# choosing a tag from its menu means that tag. One control, because two would
# be a toggle and a combo box that can contradict each other.
#
# Escape is handled in the text box itself rather than as a shortcut. A key that
# clears a filter has to work while the cursor is in the box, which is exactly
# where a window-level shortcut is least reliable — see ui/shortcuts.py.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QMenu, QToolButton,
                               QWidget)

from ..store import messages as messages_repo
from ..store import tags as tags_repo
from ..store import views as views_repo
from . import icons

# (attribute, label, glyph, tooltip)
# The width a toggle needs with its label off. Also the floor the layout is
# allowed to squeeze one to.
_ICON_ONLY_WIDTH = 34

TOGGLES = (
    ("unread", "Unread", "envelope-open", "Only messages you have not read"),
    ("flagged", "Flagged", "flag", "Only flagged messages"),
    ("attachment", "Attachment", "paperclip", "Only messages with an attachment"),
    ("contact", "Contact", "person", "Only messages from someone in the address book"),
)


class _FilterEdit(QLineEdit):
    """A line edit that gives Escape back to the bar."""

    escaped = Signal()

    def keyPressEvent(self, event) -> None:                     # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class QuickFilterBar(QWidget):
    changed = Signal(object)                   # Filters

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._filters = views_repo.Filters()
        self._buttons: dict[str, QToolButton] = {}
        self._emitting = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        for name, label, glyph, tip in TOGGLES:
            button = QToolButton(self)
            button.setText(label)
            button.setToolTip(tip)
            button.setCheckable(True)
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            # An EXPLICIT minimum, which overrides the size hint. Without it a
            # QToolButton's minimum is its labelled width, five of them put a
            # 600-pixel floor under this bar, and the splitter has nowhere to
            # take that from except the rail — which ends up 80 pixels wide with
            # every account name elided to nothing. The labels come off below
            # instead; they do not get to dictate the width of the window.
            button.setMinimumWidth(_ICON_ONLY_WIDTH)
            button.toggled.connect(
                lambda on, n=name: self._set(**{n: on}))
            self._buttons[name] = button
            layout.addWidget(button)
            self._glyphs = getattr(self, "_glyphs", {})
            self._glyphs[name] = glyph

        self.tag_button = QToolButton(self)
        self.tag_button.setText("Tag")
        self.tag_button.setCheckable(True)
        self.tag_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.tag_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.tag_button.setToolTip("Only tagged messages. Its menu picks one tag.")
        self.tag_button.setMinimumWidth(_ICON_ONLY_WIDTH + 16)   # it has a menu arrow
        self._tag_menu = QMenu(self.tag_button)
        self.tag_button.setMenu(self._tag_menu)
        self.tag_button.toggled.connect(self._tag_toggled)
        self._glyphs["tag"] = "tag"
        layout.addWidget(self.tag_button)

        self.text = _FilterEdit(self)
        self.text.setPlaceholderText("Filter these messages…")
        self.text.setClearButtonEnabled(True)
        self.text.setToolTip(
            "Filters the messages in view by sender, subject or preview.\n"
            "To search every account, use the box at the top of the window.")
        self.text.textChanged.connect(lambda value: self._set(text=value))
        self.text.escaped.connect(self.clear)
        self.text.setMinimumWidth(70)
        layout.addWidget(self.text, 1)

        self.clear_button = QToolButton(self)
        self.clear_button.setText("Clear")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(self.clear)
        layout.addWidget(self.clear_button)

        self.reload_tags()
        self._compact = None
        self._labelled_width = self._measure_labelled()
        self._apply_button_style(self.width())

    # ------------------------------------------------------------- narrowing
    def _measure_labelled(self) -> int:
        """How wide the bar wants to be with every label showing.

        The style is forced on FIRST. Measuring while the buttons happen to be
        in icon-only mode returns the icon-only width, the bar concludes its
        labels fit in a space half what they need, and Qt elides every one of
        them to "Atta...nt" — which is worse than either honest answer.
        """
        buttons = [*self._buttons.values(), self.tag_button, self.clear_button]
        for button in buttons:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        return sum(b.sizeHint().width() for b in buttons) + 130

    def _apply_button_style(self, width: int) -> None:
        """Labels when there is room for them, icons when there is not.

        The alternative to dropping the labels is a bar that overflows its pane,
        and the alternative to that is a rail with no room in it. Tooltips carry
        the words either way, which is what makes this an acceptable trade
        rather than a loss of meaning.
        """
        compact = width < self._labelled_width
        if compact == self._compact:
            return
        self._compact = compact
        style = (Qt.ToolButtonStyle.ToolButtonIconOnly if compact
                 else Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for button in self._buttons.values():
            button.setToolButtonStyle(style)
        self.tag_button.setToolButtonStyle(style)
        self.clear_button.setVisible(not compact or self._filters.active)

    def resizeEvent(self, event) -> None:                       # noqa: N802
        super().resizeEvent(event)
        self._apply_button_style(event.size().width())

    # ---------------------------------------------------------------- state
    @property
    def filters(self) -> views_repo.Filters:
        return self._filters

    def _set(self, **changes) -> None:
        if self._emitting:
            return
        from dataclasses import replace
        updated = replace(self._filters, **changes)
        if updated == self._filters:
            return
        self._filters = updated
        self.clear_button.setEnabled(updated.active)
        if self._compact:
            self.clear_button.setVisible(updated.active)
        self.changed.emit(updated)

    def set_filters(self, filters: views_repo.Filters) -> None:
        """Put the bar into a given state without emitting — for restoring a
        tab, where the filters came from the tab rather than from a click."""
        self._emitting = True
        try:
            self._filters = filters
            for name, button in self._buttons.items():
                button.setChecked(bool(getattr(filters, name)))
            self.tag_button.setChecked(bool(filters.tagged or filters.tag_id))
            if self.text.text() != filters.text:
                self.text.setText(filters.text)
            self.clear_button.setEnabled(filters.active)
            self._label_tag_button()
        finally:
            self._emitting = False

    def clear(self) -> None:
        self.set_filters(views_repo.Filters())
        self.changed.emit(self._filters)

    def focus_text(self) -> None:
        self.text.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.text.selectAll()

    # ------------------------------------------------------------------ tags
    def reload_tags(self) -> None:
        self._tag_menu.clear()
        any_tag = self._tag_menu.addAction("Any tag")
        any_tag.triggered.connect(lambda: self._choose_tag(None))
        self._tag_menu.addSeparator()
        for tag in tags_repo.list_tags(self._con):
            entry = self._tag_menu.addAction(
                f"{tag.name}    {tag.shortcut}" if tag.shortcut else tag.name)
            entry.setIcon(icons.icon("tag", tag.colour or "#888888", 14, filled=True))
            entry.triggered.connect(lambda _=False, t=tag.id: self._choose_tag(t))
        self._label_tag_button()

    def _tag_toggled(self, on: bool) -> None:
        if self._emitting:
            return
        self._set(tagged=on, tag_id=None)
        self._label_tag_button()

    def _choose_tag(self, tag_id: int | None) -> None:
        self._emitting = True
        try:
            self.tag_button.setChecked(True)
        finally:
            self._emitting = False
        self._set(tagged=(tag_id is None), tag_id=tag_id)
        self._label_tag_button()

    def _label_tag_button(self) -> None:
        tag_id = self._filters.tag_id
        if tag_id is None:
            self.tag_button.setText("Tag")
            return
        tag = tags_repo.get_tag(self._con, tag_id)
        self.tag_button.setText(tag.name if tag else "Tag")

    # --------------------------------------------------------------- theming
    def apply_theme(self, theme) -> None:
        """Recolour the icons. They are drawn, so a theme change is a repaint
        rather than a second set of assets — see ui/icons.py."""
        for name, button in self._buttons.items():
            button.setIcon(icons.icon(self._glyphs[name], theme.text_strong, 14))
        self.tag_button.setIcon(icons.icon("tag", theme.text_strong, 14))
        # See the matching note in ui/commandbar.py: a button measured before it
        # has an icon is measured too narrow.
        self._compact = None
        self._labelled_width = self._measure_labelled()
        self._apply_button_style(self.width())
