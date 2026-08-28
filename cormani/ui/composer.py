# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing a message.
#
# A WINDOW, NOT A PANE, and not modal. Mail is written while other mail is being
# read — the message being answered is the thing most often needed while
# answering it — and a composer that took the whole window over would make the
# user close it to look something up. Several may be open at once, and each is
# its own draft.
#
# SEND MEANS QUEUE. The message is saved as a draft, an op is put in the outbox
# and the window closes; `smtp/outbox.py` does the sending on the next sync,
# which may be a second later or an hour. That is what makes this client usable
# on a train: pressing Send on a carriage with no signal does exactly what
# pressing it anywhere else does, and the status bar says how many are waiting.
#
# THE BOUNCE GUARD ASKS, IT DOES NOT REFUSE. An address the store has seen bounce
# is named, with the server's own words, and the user decides — see
# store/contacts.py for why refusing would be worse.
#
# CLOSING ASKS ABOUT THE DRAFT ONCE. Anything typed is worth keeping; nothing
# typed is not worth a dialog. `Draft.is_empty` is the whole rule, and it is why
# opening a composer by accident costs nothing.
#
# THE DIALOGS ARE INJECTED — `ask_files`, `confirm` and `warn` — because Debian
# packages no QTest and a modal that a test cannot answer is a test that hangs.
# The same arrangement as ui/attachments.py and ui/tagsdialog.py.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QToolButton,
                               QVBoxLayout, QWidget)

from ..compose import quote as quote_mod
from ..compose.draft import Attachment, Draft, with_signature
from ..store import accounts as accounts_repo
from ..store import contacts as contacts_repo
from ..store import drafts as drafts_repo
from ..smtp import outbox as outbox_repo
from . import icons
from .flowlayout import FlowLayout


def _ask_files(parent) -> list:
    names, _filter = QFileDialog.getOpenFileNames(parent, "Attach files")
    return list(names)


def _confirm(parent, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.StandardButton.Yes |
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def _warn(parent, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


class Composer(QDialog):
    """One message being written."""

    queued = Signal(int)                # message id, now in the outbox
    saved = Signal(int)                 # message id, saved as a draft
    status_message = Signal(str)

    def __init__(self, con: sqlite3.Connection, draft: Draft, parent=None, *,
                 ask_files=_ask_files, confirm=_confirm, warn=_warn) -> None:
        super().__init__(parent)
        self._con = con
        self._draft = draft
        self._ask_files = ask_files
        self._confirm = confirm
        self._warn = warn
        self._attachments = list(draft.attachments)
        self._loading = False
        self.setModal(False)
        self.setMinimumSize(640, 480)
        self.setWindowTitle(draft.summary())

        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)

        self.identity = QComboBox(self)
        for identity in accounts_repo.list_identities(con, draft.account_id):
            # Shown plainly and stored as the address. `Identity.sender` is the
            # RFC form and belongs in the header, not in front of a person: a
            # display name with a bracket in it comes back from `formataddr`
            # wrapped in quotation marks, which is correct and unreadable.
            label = (f"{identity.display_name} <{identity.address}>"
                     if identity.display_name else identity.address)
            self.identity.addItem(label, identity.address)
        self.identity.currentIndexChanged.connect(lambda _: self._identity_chosen())
        form.addRow("&From", self.identity)

        self.to = QLineEdit(draft.to, self)
        form.addRow("&To", self.to)

        # Cc and Bcc are always shown. Thunderbird hides them behind a button
        # and the button is a thing people hunt for; two empty fields cost a
        # centimetre and are never in anybody's way.
        self.cc = QLineEdit(draft.cc, self)
        self.bcc = QLineEdit(draft.bcc, self)
        form.addRow("&Cc", self.cc)
        form.addRow("&Bcc", self.bcc)

        self.subject = QLineEdit(draft.subject, self)
        form.addRow("&Subject", self.subject)
        outer.addLayout(form)

        self.chips = QWidget(self)
        self._chip_layout = FlowLayout(self.chips, spacing=4)
        outer.addWidget(self.chips)

        self.body = QPlainTextEdit(draft.body, self)
        self.body.setTabChangesFocus(True)
        outer.addWidget(self.body, 1)

        row = QHBoxLayout()
        self.send_button = QPushButton("&Send", self)
        self.send_button.clicked.connect(self.send)
        row.addWidget(self.send_button)
        self.attach_button = QPushButton("&Attach…", self)
        self.attach_button.clicked.connect(self.attach)
        row.addWidget(self.attach_button)
        self.save_button = QPushButton("Save &draft", self)
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.save_button)
        self.preview_button = QPushButton("&Preview", self)
        self.preview_button.clicked.connect(self.preview)
        row.addWidget(self.preview_button)
        row.addStretch(1)
        self.note = QLabel("", self)
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        outer.addLayout(row)

        self._install_shortcuts()
        self._select_identity(draft.from_address)
        self._redraw_chips()

    def show(self) -> None:
        super().show()
        self.raise_()
        self.activateWindow()
        self.to.setFocus()

    # ------------------------------------------------------------- shortcuts
    def _install_shortcuts(self) -> None:
        send = QAction("Send", self)
        # Ctrl+Return, which is what every mail client in the world sends on.
        send.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
        send.triggered.connect(self.send)
        self.addAction(send)
        save = QAction("Save draft", self)
        save.setShortcut(QKeySequence.StandardKey.Save)
        save.triggered.connect(self.save)
        self.addAction(save)

    # ------------------------------------------------------------- the draft
    def draft(self) -> Draft:
        """What is on the screen, as data."""
        return self._draft.with_changes(
            from_address=self.identity.currentData() or self._draft.from_address,
            from_name=self._identity_name(),
            to=self.to.text(), cc=self.cc.text(), bcc=self.bcc.text(),
            subject=self.subject.text(), body=self.body.toPlainText(),
            attachments=tuple(self._attachments))

    def _identity_name(self) -> str:
        address = self.identity.currentData()
        identity = accounts_repo.identity_for(self._con, self._draft.account_id,
                                              address or "")
        return identity.display_name if identity else self._draft.from_name

    def _select_identity(self, address: str) -> None:
        self._loading = True
        try:
            index = self.identity.findData((address or "").lower())
            if index < 0:
                index = self.identity.findData(address)
            self.identity.setCurrentIndex(max(0, index))
        finally:
            self._loading = False

    def _identity_chosen(self) -> None:
        """Swap the signature when the sending address changes.

        `with_signature` strips the old one first, which is what stops a person
        who changes their mind twice from sending three signatures.
        """
        if self._loading:
            return
        identity = accounts_repo.identity_for(
            self._con, self._draft.account_id, self.identity.currentData() or "")
        self.body.setPlainText(
            with_signature(self.body.toPlainText(),
                           identity.signature if identity else ""))

    # ---------------------------------------------------------- attachments
    def attach(self, paths=None) -> int:
        """Add files. `paths` is for the tests and for a drop, later."""
        chosen = list(paths) if paths else self._ask_files(self)
        for path in chosen:
            if path and path not in [a.path for a in self._attachments]:
                self._attachments.append(Attachment(path=str(path)))
        self._redraw_chips()
        return len(self._attachments)

    def remove_attachment(self, path: str) -> None:
        self._attachments = [a for a in self._attachments if a.path != path]
        self._redraw_chips()

    def _redraw_chips(self) -> None:
        while self._chip_layout.count():
            item = self._chip_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
        for attachment in self._attachments:
            chip = QToolButton(self.chips)
            chip.setText(f"{attachment.name}  ✕")
            chip.setToolTip(f"{attachment.path}\nClick to remove")
            chip.setIcon(icons.icon("paperclip", "#657b83", 12))
            chip.clicked.connect(
                lambda _=False, p=attachment.path: self.remove_attachment(p))
            self._chip_layout.addWidget(chip)
        self.chips.setVisible(bool(self._attachments))

    # ---------------------------------------------------------------- saving
    def save(self) -> int | None:
        """Write the draft to the store. Returns the row id."""
        draft = self.draft()
        row_id, _rfc = drafts_repo.save(self._con, draft)
        self._draft = draft.with_changes(message_id=row_id)
        self.setWindowTitle(self._draft.summary())
        self._say(f"Saved as a draft — {self._draft.summary()}")
        self.saved.emit(row_id)
        return row_id

    def preview(self) -> None:
        draft = self.draft()
        identity = accounts_repo.identity_for(
            self._con, draft.account_id, self.identity.currentData() or "")
        body = with_signature(draft.body,
                              identity.signature if identity else "")
        lines = [f"To: {draft.to}"]
        if draft.cc.strip():
            lines.append(f"Cc: {draft.cc}")
        lines.extend([f"Subject: {draft.subject}", "", body])
        QMessageBox.information(self, "Outgoing message preview", "\n".join(lines))

    # --------------------------------------------------------------- sending
    def send(self) -> bool:
        """Queue it. The outbox does the rest — see the note at the top."""
        draft = self.draft()
        if not draft.is_addressed:
            self._say("Nobody to send it to yet")
            self.to.setFocus()
            return False

        gone = drafts_repo.missing(draft)
        if gone:
            self._warn(self, "Attachment missing",
                       f"{', '.join(gone)} is no longer where it was when it "
                       f"was attached. Remove it, or put the file back.")
            return False

        found = contacts_repo.bounced(self._con, draft.recipients())
        if found and not self._confirm(
                self, "Send anyway?",
                f"Mail to {contacts_repo.describe_bounces(found)} has come back "
                f"before.\n\nSend it anyway?"):
            return False

        if not draft.subject.strip() and not self._confirm(
                self, "Send without a subject?",
                "This message has no subject. Send it anyway?"):
            self.subject.setFocus()
            return False

        row_id, _rfc = drafts_repo.save(self._con, draft)
        self._draft = draft.with_changes(message_id=row_id)
        outbox_repo.queue(self._con, row_id)
        self.queued.emit(row_id)
        self.status_message.emit(
            f"Queued — {self._draft.summary()} will go out with the next sync")
        self._sent = True
        self.accept()
        return True

    # --------------------------------------------------------------- closing
    def closeEvent(self, event) -> None:                    # noqa: N802
        if getattr(self, "_sent", False):
            event.accept()
            return
        draft = self.draft()
        if draft.is_empty:
            event.accept()
            return
        if self._confirm(self, "Save draft?",
                         f"Keep “{draft.summary()}” as a draft?"):
            self.save()
        elif draft.message_id:
            # It was saved earlier and the user has now said no. The row goes,
            # because leaving it would be keeping something they declined.
            drafts_repo.discard(self._con, draft.message_id)
        event.accept()

    def _say(self, text: str) -> None:
        self.note.setText(text)
        self.status_message.emit(text)


def for_reply(con, row, body, *, all_recipients=False, parent=None, **kw) -> Composer:
    """A composer holding a reply to this message."""
    identities = accounts_repo.list_identities(con, row.account_id)
    mine = accounts_repo.list_identity_addresses(con)
    draft = quote_mod.reply(row, body, identities, all_recipients=all_recipients,
                            mine=mine, signature=quote_mod.signature_for_reply(
                                identities, row))
    return Composer(con, draft, parent, **kw)


def for_forward(con, row, body, *, attachments=(), parent=None, **kw) -> Composer:
    identities = accounts_repo.list_identities(con, row.account_id)
    draft = quote_mod.forward(row, body, identities, attachments=attachments,
                              signature=quote_mod.signature_for_reply(
                                  identities, row))
    return Composer(con, draft, parent, **kw)


def for_new(con, account_id: int, parent=None, *, to: str = "",
            **kw) -> Composer:
    identity = accounts_repo.default_identity(con, account_id)
    draft = quote_mod.blank(account_id, identity,
                            signature=identity.signature if identity else "",
                            to=to)
    return Composer(con, draft, parent, **kw)
