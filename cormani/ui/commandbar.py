# SPDX-License-Identifier: GPL-3.0-or-later
#
# The command bar above the reading pane.
#
# Outlook's, and the reason to take it rather than Thunderbird's menu is that
# Reply, Reply all and Forward are the three things done most often in a mail
# client and putting them behind a menu costs a click every time.
#
# WHETHER A BUTTON WORKS comes from `ui/commands.py`, not from this file.
# Disabled controls name what is missing in their tooltip — CONVENTIONS.txt §8.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton, QWidget

from . import commands as commands_mod
from . import icons

# The width a command needs with its label off, and the floor the layout may
# squeeze one to. Same reasoning as ui/quickfilter.py: a row of labelled buttons
# that cannot shrink sets a minimum width for the pane holding it, and the
# splitter then takes that width from whichever pane CAN shrink — which is the
# rail, and which is the wrong answer.
_ICON_ONLY_WIDTH = 32

# (id, label, glyph, primary)
#
# PRIMARY commands keep their labels. PLAN.txt §2 asks for Reply, Reply all and
# Forward "as buttons, not a menu", and a button whose label has been dropped to
# save room is halfway back to a menu. The rest are icons with tooltips: they
# are recognisable shapes, and reserving label space for eight commands is what
# made this bar wide enough to squeeze the rail flat.
COMMANDS = (
    ("compose", "New", "plus", True),
    ("reply", "Reply", "reply", True),
    ("reply_all", "Reply all", "reply-all", True),
    ("forward", "Forward", "forward", True),
    (None, None, None, False),                            # separator
    ("archive", "Archive", "archive", False),
    ("flag", "Flag", "flag", False),
    ("mark_read", "Mark read", "envelope", False),
    ("delete", "Delete", "trash", False),
    ("snooze", "Snooze", "snooze", False),
)


class CommandBar(QWidget):
    command = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._buttons: dict[str, QToolButton] = {}
        self._glyphs: dict[str, str] = {}
        self._primary: dict[str, bool] = {}
        self._has_message = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        for command_id, label, glyph, primary in COMMANDS:
            if command_id is None:
                line = QFrame(self)
                line.setFrameShape(QFrame.Shape.VLine)
                line.setFrameShadow(QFrame.Shadow.Plain)
                layout.addWidget(line)
                continue
            button = QToolButton(self)
            button.setText(label)
            tip = commands_mod.command_tooltip(command_id)
            if tip:
                button.setToolTip(tip)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setAutoRaise(True)
            button.setMinimumWidth(_ICON_ONLY_WIDTH)
            button.clicked.connect(lambda _=False, c=command_id: self.command.emit(c))
            self._buttons[command_id] = button
            self._glyphs[command_id] = glyph
            self._primary[command_id] = primary
            layout.addWidget(button)
        layout.addStretch(1)
        self._compact = None
        self._labelled_width = self._measure_labelled()
        self._apply_button_style(self.width())
        self.set_message(None)

    def _ready(self, command_id: str) -> bool:
        if command_id == "compose":
            return commands_mod.command_ready(command_id)
        return commands_mod.command_ready(command_id)

    def _measure_labelled(self) -> int:
        """How wide the bar needs to be with the primary labels showing.

        The style is forced on first: measuring while the buttons happen to be
        in icon-only mode returns the icon-only width, the bar concludes its
        labels fit in half the space they need, and Qt elides every one to
        "Fo...rd" — worse than either honest answer.
        """
        self._set_style(labelled=True)
        return sum(b.sizeHint().width() for b in self._buttons.values()) + 30

    def _set_style(self, *, labelled: bool) -> None:
        for command_id, button in self._buttons.items():
            wants_label = labelled and self._primary[command_id]
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon if wants_label
                else Qt.ToolButtonStyle.ToolButtonIconOnly)

    def _apply_button_style(self, width: int) -> None:
        compact = width < self._labelled_width
        if compact == self._compact:
            return
        self._compact = compact
        self._set_style(labelled=not compact)

    def resizeEvent(self, event) -> None:                       # noqa: N802
        super().resizeEvent(event)
        self._apply_button_style(event.size().width())

    def set_message(self, row) -> None:
        """Enable what applies to the message now shown, and relabel the two
        buttons whose meaning depends on it."""
        self._has_message = row is not None
        for command_id, button in self._buttons.items():
            needs_message = command_id != "compose"
            ready = self._ready(command_id)
            button.setEnabled(ready and (self._has_message or not needs_message))
        for command_id, when_set, when_clear in (
                ("mark_read", "Mark unread", "Mark read"),
                ("flag", "Unflag", "Flag")):
            button = self._buttons.get(command_id)
            if button is None:
                continue
            state = row is not None and (row.seen if command_id == "mark_read"
                                         else row.flagged)
            label = when_set if state else when_clear
            button.setText(label)
            # The tooltip carries it as well: these are icon-only most of the
            # time, and an icon cannot say whether it will set or clear.
            button.setToolTip(label)

    def apply_theme(self, theme) -> None:
        for command_id, button in self._buttons.items():
            colour = (theme.text_strong if self._ready(command_id)
                      else theme.text_muted)
            button.setIcon(icons.icon(self._glyphs[command_id], colour, 15))
        # Re-measured HERE, not at construction. The icons are applied with the
        # theme, and a button measured before it has one is measured too narrow —
        # the bar then believes its labels fit, never switches to icons, and Qt
        # elides every label to "Fo...rd" instead.
        self._compact = None
        self._labelled_width = self._measure_labelled()
        self._apply_button_style(self.width())
