# SPDX-License-Identifier: GPL-3.0-or-later
#
# The reading pane.
#
# THE BODY IS A MessageView (stage 3), which was a QPlainTextEdit until the
# sanitiser existed. Stage 1 chose a widget that CANNOT render HTML on purpose,
# so that the requirement could not be quietly lost by rendering harmless
# fixtures and never coming back. It has been come back to: `render/sanitise.py`
# is the allowlist, `ui/messageview.py` is a widget with no script engine and no
# network stack, and `loadResource` is the only door any byte reaches the page
# through. CONVENTIONS.txt §7.
#
# REMOTE CONTENT IS WITHHELD AND THE BAR SAYS SO. A tracking pixel tells the
# sender the message was opened, when, and at which address. The bar appears
# only when something was actually withheld, names the number, and the choice
# it offers applies to THIS message — a per-sender memory is a decision the
# user should make deliberately, not one made for them by clicking once.
#
# The header block is Outlook's: subject in weight, then the correspondent, then
# the date and the account the message arrived on, with that account's colour
# beside it. At fifteen accounts, "which of my addresses was this sent to" is a
# question the reading pane has to answer without being asked.
#
# THE ATTACHMENT STRIP IS A WIDGET, NOT A LINE OF TEXT (stage 3, item 15). It
# was a QLabel naming the files, which is the honest placeholder for a strip
# that could not yet do anything with them. `ui/attachments.py` owns it now,
# and the reading pane's only remaining business with attachments is to hand
# it the parts and forward what it says to the status bar.
#
# THE TRACKING STRIP AT THE FOOT IS STAGE 6'S AND IS NOW REAL. It was a
# sentence saying what would go there, because an empty frame in a reading pane
# reads as something that failed to load; `ui/trackingstrip.py` replaced it.
# The reading pane HOLDS it and never drives it — the strip's buttons are
# signals and `ui/mailpane.py` decides what they mean, which is the same
# arrangement the command bar and the inline reply already have.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from .attachments import AttachmentStrip
from .commandbar import CommandBar
from .invitationbar import InvitationBar
from .inlinereply import InlineReply
from .messagelist import to_local
from .messageview import MessageView
from .trackingstrip import TrackingStrip


def format_full_date(iso: str) -> str:
    """The reading pane's date, in the reader's timezone. See messagelist.to_local."""
    when = to_local(iso)
    if when is None:
        return iso
    return when.strftime("%-d %b %Y %H:%M")


class Reader(QWidget):
    command = Signal(str)
    link_activated = Signal(str)          # a link in the body; the window opens it
    status_message = Signal(str)          # what an open or a save did
    invitation_answered = Signal(str)     # a response to the invitation shown
    invitation_dismissed = Signal()       # a cancellation, acted on

    def __init__(self, parent=None, *, con=None) -> None:
        super().__init__(parent)
        # The connection is optional so that a test may build a reading pane
        # with no store behind it, which several already do. Without one the
        # tracking strip is a blank label — the pane draws, and the half that
        # needs a database is simply absent.
        self._con = con
        self._theme = None
        self._row = None
        self._bodies: tuple = ("", "")
        self._attachments: list = []
        self._attachments_root = None
        self._attachment_cache = None
        self._allow_remote = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        self.subject = QLabel("")
        self.subject.setWordWrap(True)
        # A long subject must not widen the pane; it wraps inside whatever width
        # the splitter gives it.
        self.subject.setMinimumWidth(120)
        self.subject.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.subject)

        who = QHBoxLayout()
        who.setSpacing(8)
        self.correspondent = QLabel("")
        self.correspondent.setMinimumWidth(100)
        self.correspondent.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        who.addWidget(self.correspondent)
        who.addStretch(1)
        self.account_swatch = QLabel("")
        self.account_swatch.setFixedSize(10, 10)
        who.addWidget(self.account_swatch)
        self.account = QLabel("")
        who.addWidget(self.account)
        self.when = QLabel("")
        who.addWidget(self.when)
        outer.addLayout(who)

        self.commands = CommandBar(self)
        self.commands.command.connect(self.command)
        outer.addWidget(self.commands)

        divider = QFrame(self)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        outer.addWidget(divider)

        # The withheld-content bar sits between the header and the body, so
        # that it cannot be scrolled away from while the message it describes
        # is still on screen.
        self.remote_bar = QWidget(self)
        bar = QHBoxLayout(self.remote_bar)
        bar.setContentsMargins(12, 6, 12, 6)
        bar.setSpacing(10)
        self.remote_note = QLabel("", self.remote_bar)
        self.remote_note.setWordWrap(True)
        bar.addWidget(self.remote_note, 1)
        self.remote_load = QPushButton("Load images", self.remote_bar)
        self.remote_load.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remote_load.clicked.connect(self._load_remote)
        bar.addWidget(self.remote_load, 0)
        self.remote_bar.setVisible(False)
        outer.addWidget(self.remote_bar)

        # Above the body, beside the withheld-content bar and for the same
        # reason: an invitation is a message whose point is a decision, and
        # the decision must not scroll away from the question.
        self.invitation = InvitationBar(self)
        self.invitation.answered.connect(self.invitation_answered)
        self.invitation.dismissed.connect(self.invitation_dismissed)
        outer.addWidget(self.invitation)

        self.body = MessageView(self)
        self.body.link_activated.connect(self.link_activated)
        outer.addWidget(self.body, 1)

        self.attachments = AttachmentStrip(self)
        self.attachments.status.connect(self.status_message)
        outer.addWidget(self.attachments)

        self.tracking = TrackingStrip(con, self) if con is not None \
            else QLabel("", self)
        outer.addWidget(self.tracking)

        # Under the message it answers. ui/inlinereply.py argues why the
        # commonest reply deserves not to open a window.
        self.inline = InlineReply(self)
        outer.addWidget(self.inline)

        self.clear()

    # ---------------------------------------------------------------- content
    def clear(self) -> None:
        self._row = None
        self.invitation.set_invitation(None)
        self.subject.setText("No message selected")
        self.correspondent.setText("")
        self.account.setText("")
        self.when.setText("")
        self.account_swatch.setVisible(False)
        self.body.clear_body()
        self._bodies = ("", "")
        self._attachments = []
        self._allow_remote = False
        self.remote_bar.setVisible(False)
        self.attachments.clear()
        if hasattr(self.tracking, "show_message"):
            self.tracking.show_message(None)
        self.commands.set_message(None)
        self.inline.set_message(None)
        self._restyle()

    def show_message(self, row, bodies, attachments, *,
                     attachments_root=None, attachment_cache=None,
                     invitation=None) -> None:
        self._row = row
        self.subject.setText(row.subject_label)
        name = row.from_name or row.from_addr
        address = row.from_addr
        self.correspondent.setText(
            f"{name} · {address}" if name and name != address else address)
        self.account.setText(row.account_label)
        self.account_swatch.setVisible(bool(row.account_colour))
        self.when.setText(format_full_date(row.date_at))
        # Kept so that "Load images" can redraw the same message without
        # asking the store for it again.
        self._bodies = bodies if isinstance(bodies, tuple) else (bodies or "", "")
        self._attachments = list(attachments or ())
        self._attachments_root = attachments_root
        self._attachment_cache = attachment_cache
        self._allow_remote = False
        self._render()

        # The strip decides for itself which parts a person would call an
        # attachment; it is given all of them because the inline ones are what
        # the body draws with.
        self.attachments.set_attachments(self._attachments,
                                         root=attachments_root,
                                         cache=attachment_cache)

        # The strip asks the store what it needs; the reading pane hands it
        # only the message id. A pane that passed the whole row would be a
        # second place deciding what a thread is.
        if hasattr(self.tracking, "show_message"):
            self.tracking.show_message(row.id)
        self.invitation.set_invitation(invitation)
        self.commands.set_message(row)
        # Not on a message of the user's own: replying to yourself in Sent is a
        # gesture with no meaning, and the box would be an invitation to it.
        self.inline.set_message(None if row.outgoing else row)
        self._restyle()

    # --------------------------------------------------------------- theming
    def _render(self) -> None:
        """Draw the body at the current remote-content setting."""
        text, html = self._bodies
        withheld = self.body.show_body(
            text=text, html=html, attachments=self._attachments,
            allow_remote=self._allow_remote,
            palette=self.body.palette_for(self._theme) if self._theme else None,
            attachments_root=self._attachments_root)
        self._show_remote_bar(withheld)

    def _show_remote_bar(self, withheld: int) -> None:
        if self._allow_remote or not withheld:
            self.remote_bar.setVisible(False)
            return
        thing = "resource" if withheld == 1 else "resources"
        self.remote_note.setText(
            f"{withheld} remote {thing} withheld. Loading them tells the "
            f"sender you opened this message.")
        self.remote_bar.setVisible(True)

    def _load_remote(self) -> None:
        """For THIS message only. A per-sender memory is a decision the user
        should make deliberately, not one made by clicking once."""
        self._allow_remote = True
        self._render()

    def message_id(self) -> int | None:
        """Which message is being read, for anything that needs to ask the
        store about it. The row itself stays private: a caller given the row
        would be a second place deciding what its fields mean."""
        return None if self._row is None else int(self._row.id)

    def apply_theme(self, theme) -> None:
        self._theme = theme
        if hasattr(self.tracking, "set_theme"):
            self.tracking.set_theme(theme)
        self.commands.apply_theme(theme)
        self.attachments.apply_theme(theme)
        if self._row is not None:
            # The body is an HTML document with its colours baked in, so a
            # theme change has to redraw it rather than restyle it.
            self._render()
        self._restyle()

    def _restyle(self) -> None:
        t = self._theme
        if t is None:
            return
        self.subject.setStyleSheet(
            f"font-size: 15pt; font-weight: 600; color: {t.text_strong};")
        self.correspondent.setStyleSheet(f"color: {t.text}; font-weight: 500;")
        self.account.setStyleSheet(f"color: {t.text_muted};")
        self.when.setStyleSheet(f"color: {t.text_muted};")
        self.tracking.setStyleSheet(f"color: {t.owed}; font-style: italic;")
        colour = self._row.account_colour if self._row else ""
        if colour:
            self.account_swatch.setStyleSheet(
                f"background: {colour}; border-radius: 5px;")
        self.body.setStyleSheet(
            f"background: {t.surface}; color: {t.text}; border: 0;")
