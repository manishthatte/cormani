# SPDX-License-Identifier: GPL-3.0-or-later
#
# The attachment strip, and the two things it can do with a part.
#
# Until now this was a line of text under the body — a QLabel that named the
# files and could do nothing with them. It is a row of chips now, one per part,
# and clicking one opens it.
#
# ONE CLICK OPENS, AND THE CONTEXT MENU SAVES. That is Outlook's arrangement
# and Thunderbird's, and it is right for the reason that opening is what people
# do with an attachment nine times in ten. Save is a menu away rather than a
# second button on every chip, because twelve chips with two buttons each is a
# worse strip than twelve chips.
#
# THE STRIP ASKS BEFORE HANDING THE DESKTOP SOMETHING IT WOULD RUN. A `.desktop`
# file looks inert and is a program; `xdg-open` on one executes its Exec line.
# `store/attachments.is_risky` names the suffixes, and the confirmation names
# the file and says what will happen, because a dialog that says "are you sure"
# and nothing else trains people to click through it. Save is never gated: a
# file on disk is not a file that ran.
#
# NOTHING HERE OPENS THE STORE'S OWN COPY. `copy_for_opening` puts a copy under
# CACHE and that is what the desktop is given, so an editor cannot write back
# into the archive and a re-sync cannot delete the file out from under an open
# window. The reasoning is in `store/attachments.py`, which owns it.
#
# EVERY DIALOG AND THE LAUNCH ITSELF ARE INJECTED. Debian ships no QTest, so
# this suite cannot synthesise a click; widgets are driven through their own
# methods instead, and a method that opened a real QFileDialog or spawned a
# real xdg-open could not be called from a test at all. The four hooks below
# are the seam — the same reason `cli.py` takes its prompts as parameters.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QLabel, QMenu, QMessageBox,
                               QToolButton, QWidget)

from ..platform import desktop
from ..store import attachments as att
from . import icons
from .flowlayout import FlowLayout, height_for_width_policy

# The chip's icon, by what the part claims to be. Everything else is a page:
# a strip that guesses a spreadsheet from `application/vnd.ms-excel` and gets
# `application/octet-stream` wrong beside it looks broken rather than helpful.
_IMAGE_TYPES = ("image/",)


def chip_colour(theme, *, missing: bool, risky: bool) -> str:
    """What colour a chip's icon is drawn in, as a rule rather than a branch.

    A file the desktop would RUN is marked before it is clicked, not only in
    the dialog afterwards, so that nobody meets that dialog by surprise.
    `error` rather than `flagged` or `deadline`, both of which already mean
    particular things in this palette; red is the one role here that reads as
    caution and nothing else. Missing wins over risky: a part with no bytes
    cannot be opened at all, so that is the more useful thing to say.
    """
    if missing:
        return theme.text_muted
    if risky:
        return theme.error
    return theme.accent


class AttachmentStrip(QWidget):
    """The parts of the message a person would call attachments."""

    status = Signal(str)                  # for the window's status bar

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._theme = None
        self._root: Path | None = None
        self._cache: Path | None = None
        # (row, index) for each VISIBLE chip. `index` is the part's position
        # among ALL parts, inline ones included, because that is what names an
        # unnamed part and it must not shift when the strip hides one.
        self._parts: list = []
        self._buttons: list = []

        # Injected by the tests; None means the real thing. See the header.
        self.choose_file = None           # (suggested_name) -> path | None
        self.choose_directory = None      # () -> path | None
        self.confirm_open = None          # (name) -> bool
        self.open_file = None             # (path) -> None

        height_for_width_policy(self)
        self._layout = FlowLayout(self, margin=0, spacing=6)

        self.caption = QLabel("", self)
        self._layout.addWidget(self.caption)

        self.save_all_button = QToolButton(self)
        self.save_all_button.setText("Save all")
        self.save_all_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.save_all_button.setAutoRaise(True)
        self.save_all_button.clicked.connect(lambda _=False: self.save_all())
        self.save_all_button.setVisible(False)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setVisible(False)

    # ---------------------------------------------------------------- content
    def set_attachments(self, rows, *, root=None, cache=None) -> None:
        """Show the non-inline parts of one message, and forget the last one.

        An inline part is not listed for the same reason `attachments_of`
        returns it anyway: the reading pane needs a signature logo to draw the
        message, and a person does not need it offered as a file.
        """
        self._root = Path(root) if root else None
        self._cache = Path(cache) if cache else None
        self._parts = [(row, index)
                       for index, row in enumerate(rows or (), start=1)
                       if not row["is_inline"]]
        self._rebuild()

    def clear(self) -> None:
        self.set_attachments(())

    def _rebuild(self) -> None:
        for button in self._buttons:
            self._layout.removeWidget(button)
            button.setParent(None)
            button.deleteLater()
        self._buttons = []
        self._layout.removeWidget(self.save_all_button)

        if not self._parts:
            self.setVisible(False)
            self.save_all_button.setVisible(False)
            return

        count = len(self._parts)
        self.caption.setText("Attachment:" if count == 1 else f"{count} attachments:")
        for position, (row, index) in enumerate(self._parts):
            self._buttons.append(self._chip(row, index, position))
        self.save_all_button.setVisible(count > 1)
        if count > 1:
            self._layout.addWidget(self.save_all_button)
        self.setVisible(True)
        self._restyle()

    def _chip(self, row, index: int, position: int) -> QToolButton:
        name = att.display_name(row, index=index)
        button = QToolButton(self)
        button.setText(f"{name}  ·  {att.human_size(row['size_bytes'])}")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(self._tooltip(row, name))
        button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        button.clicked.connect(
            lambda _=False, p=position: self.open_attachment(p))
        button.customContextMenuRequested.connect(
            lambda point, p=position, b=button: self._chip_menu(p, b, point))
        self._layout.addWidget(button)
        return button

    def _tooltip(self, row, name: str) -> str:
        kind = (row["content_type"] or "unknown type").split(";")[0].strip()
        lines = [name, f"{kind} · {att.human_size(row['size_bytes'])}"]
        if not (row["stored_path"] or "").strip():
            lines.append("Not downloaded — re-sync the account to fetch it.")
        elif att.is_risky(name):
            lines.append("corMani will ask before opening this: the desktop "
                         "would run it.")
        else:
            lines.append("Click to open · right-click to save")
        return "\n".join(lines)

    # ---------------------------------------------------------------- actions
    def open_attachment(self, position: int) -> bool:
        """Open one part in whatever the desktop uses for it. True if handed over."""
        part = self._part(position)
        if part is None:
            return False
        row, index = part
        name = att.display_name(row, index=index)

        # In this order on purpose. Whether the bytes are there is the thing
        # the user can act on, so it is asked first; the cache is an internal
        # condition; and the question about a file the desktop would RUN comes
        # last, so that it is never asked about something that then fails to
        # open anyway.
        try:
            att.stored_file(row, self._root)
        except (att.AttachmentMissing, att.AttachmentEscapes) as exc:
            self.status.emit(f"{name}: {exc}")
            return False
        if self._cache is None:
            self.status.emit(f"{name}: nowhere to put a copy to open")
            return False
        if att.is_risky(name) and not self._ask_about(name):
            self.status.emit(f"{name}: not opened")
            return False

        try:
            copy = att.copy_for_opening(row, self._root, self._cache, index=index)
            (self.open_file or desktop.open_path)(copy)
        except (att.AttachmentMissing, att.AttachmentEscapes,
                desktop.OpenFailed) as exc:
            self.status.emit(f"{name}: {exc}")
            return False
        except OSError as exc:
            self.status.emit(f"{name}: could not be copied — {exc}")
            return False
        self.status.emit(f"Opened {name}")
        return True

    def save_attachment(self, position: int, target=None) -> bool:
        """Save one part where the user says. `target` is the test's answer to
        the dialog; None asks for one."""
        part = self._part(position)
        if part is None:
            return False
        row, index = part
        name = att.display_name(row, index=index)

        chosen = target if target is not None else self._ask_where(name)
        if not chosen:
            return False
        try:
            written = att.save_as(row, self._root, chosen)
        except (att.AttachmentMissing, att.AttachmentEscapes) as exc:
            self.status.emit(f"{name}: {exc}")
            return False
        except OSError as exc:
            self.status.emit(f"{name}: could not be saved — {exc}")
            return False
        self.status.emit(f"Saved {written.name} to {written.parent}")
        return True

    def save_all(self, directory=None) -> int:
        """Save every part into one directory. Returns how many were written."""
        if not self._parts:
            return 0
        chosen = directory if directory is not None else self._ask_directory()
        if not chosen:
            return 0
        rows = [row for row, _ in self._parts]
        try:
            written = att.save_all(rows, self._root, chosen)
        except (att.AttachmentEscapes, OSError) as exc:
            self.status.emit(f"could not save to {chosen} — {exc}")
            return 0

        missing = len(rows) - len(written)
        # CONVENTIONS.txt §8: what was NOT written is the half worth saying.
        note = f"Saved {len(written)} of {len(rows)} to {chosen}" if missing \
            else f"Saved {len(written)} attachments to {chosen}"
        if missing:
            note += f" — {missing} not downloaded"
        self.status.emit(note)
        return len(written)

    def _part(self, position: int):
        if 0 <= position < len(self._parts):
            return self._parts[position]
        return None

    # ------------------------------------------------------------- the asking
    def _ask_about(self, name: str) -> bool:
        if self.confirm_open is not None:
            return bool(self.confirm_open(name))
        answer = QMessageBox.warning(          # pragma: no cover — needs a display
            self, "Open this attachment?",
            f"{name} is a kind of file your desktop will RUN rather than "
            f"display.\n\nOpen it only if you know who sent it and were "
            f"expecting it. Saving it instead does not run it.",
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Open

    def _ask_where(self, name: str):
        if self.choose_file is not None:
            return self.choose_file(name)
        path, _ = QFileDialog.getSaveFileName(  # pragma: no cover — needs a display
            self, "Save attachment", str(Path.home() / name))
        return path or None

    def _ask_directory(self):
        if self.choose_directory is not None:
            return self.choose_directory()
        path = QFileDialog.getExistingDirectory(  # pragma: no cover
            self, "Save all attachments into", str(Path.home()))
        return path or None

    # ---------------------------------------------------------------- the menu
    def _chip_menu(self, position: int, button: QToolButton, point) -> None:
        menu = self.build_menu(position)
        menu.exec(button.mapToGlobal(point))

    def build_menu(self, position: int) -> QMenu:
        """The chip's context menu. Public so a test can read what it offers
        without an event loop to open it in."""
        menu = QMenu(self)
        colour = self._theme.text_strong if self._theme else "#000000"
        part = self._part(position)
        stored = part is not None and bool((part[0]["stored_path"] or "").strip())

        opener = menu.addAction(icons.icon("envelope-open", colour, 15), "Open")
        opener.setEnabled(stored and desktop.opener_available())
        opener.triggered.connect(lambda _=False: self.open_attachment(position))

        saver = menu.addAction(icons.icon("save", colour, 15), "Save as…")
        saver.setEnabled(stored)
        saver.triggered.connect(lambda _=False: self.save_attachment(position))

        if len(self._parts) > 1:
            menu.addSeparator()
            every = menu.addAction("Save all…")
            every.triggered.connect(lambda _=False: self.save_all())
        return menu

    # ---------------------------------------------------------------- theming
    def apply_theme(self, theme) -> None:
        self._theme = theme
        self._restyle()

    def _restyle(self) -> None:
        t = self._theme
        if t is None:
            return
        self.caption.setStyleSheet(f"color: {t.text_muted}; padding-right: 2px;")
        self.save_all_button.setIcon(icons.icon("save", t.text_strong, 14))
        self.save_all_button.setStyleSheet(f"color: {t.text_muted};")
        for button, (row, index) in zip(self._buttons, self._parts):
            name = att.display_name(row, index=index)
            kind = (row["content_type"] or "").lower()
            glyph = "image" if kind.startswith(_IMAGE_TYPES) else "file"
            missing = not (row["stored_path"] or "").strip()
            colour = chip_colour(t, missing=missing, risky=att.is_risky(name))
            button.setIcon(icons.icon(glyph, colour, 14))
            button.setEnabled(not missing)
            button.setStyleSheet(
                f"QToolButton {{ color: {t.text_muted if missing else t.text}; "
                f"background: {t.surface_raised}; border: 1px solid {t.border}; "
                f"border-radius: 4px; padding: 3px 8px; }}"
                f"QToolButton:hover {{ border-color: {t.accent}; "
                f"color: {t.text_strong}; }}")
            button.setToolTip(self._tooltip(row, name))
