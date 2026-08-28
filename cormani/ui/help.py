# SPDX-License-Identifier: GPL-3.0-or-later
#
# The two dialogs that describe the application to itself.
#
# The shortcut list is GENERATED from ui/shortcuts.py rather than written out.
# A hand-written key list is wrong within two releases, and a wrong key list is
# worse than none — it teaches a key that does nothing.
#
# Both dialogs say plainly which parts are not built yet. A first-time reader of
# the shortcut list should not have to press eight keys to discover which four
# work. CONVENTIONS.txt §8.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QTextBrowser,
                               QVBoxLayout)

from .. import APP_NAME, __version__
from . import shortcuts as shortcuts_mod

_SCOPE_TITLES = {
    shortcuts_mod.SCOPE_WINDOW: "Anywhere in the window",
    shortcuts_mod.SCOPE_LIST: "While the message list has focus",
}


def shortcut_lines() -> list[tuple[str, str, str, bool]]:
    """(scope title, key, description, ready). Plain data, so it is testable
    without opening a dialog."""
    out = []
    for scope in (shortcuts_mod.SCOPE_WINDOW, shortcuts_mod.SCOPE_LIST):
        for shortcut in shortcuts_mod.in_scope(scope):
            out.append((_SCOPE_TITLES[scope], shortcut.key,
                        shortcut.description, shortcut.ready))
    return out


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(560, 620)
        layout = QVBoxLayout(self)

        view = QTextBrowser(self)
        # Built as rich text from values this module produced. Nothing here
        # comes from a message or a server; the sanitising rule in
        # CONVENTIONS.txt §7 is about content, and this is chrome.
        rows = []
        current = ""
        for scope, key, description, ready in shortcut_lines():
            if scope != current:
                current = scope
                rows.append(f"<tr><td colspan='2' style='padding-top:14px'>"
                            f"<b>{scope}</b></td></tr>")
            note = "" if ready else "  <i>(arrives with a later stage)</i>"
            rows.append(
                f"<tr><td style='padding:2px 18px 2px 0'><code>{key}</code></td>"
                f"<td>{description}{note}</td></tr>")
        view.setHtml("<table>" + "".join(rows) + "</table>")
        layout.addWidget(view)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class AboutDialog(QDialog):
    def __init__(self, parent=None, *, engine: str = "", store: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        layout = QVBoxLayout(self)

        title = QLabel(f"<b>{APP_NAME}</b> {__version__}")
        title.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "Mail, calendar and correspondence tracking in one window."))
        layout.addWidget(QLabel(
            "GPL-3.0-or-later, with a commercial licence available."))
        layout.addWidget(QLabel("© Manish Jagdish Thatte"))
        if store:
            layout.addWidget(QLabel(store))
        if engine:
            layout.addWidget(QLabel(engine))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
