# SPDX-License-Identifier: GPL-3.0-or-later
#
# The widget that draws a message body.
#
# WHY QTextBrowser AND NOT QWebEngineView. QtWebEngine is already a dependency
# — the site panels at stage 7 need a real browser — so reaching for it here
# would have cost nothing in packaging. It was rejected anyway, on two grounds.
#
# The first is the attack surface. QWebEngineView is Chromium: a JavaScript
# engine, a network stack, a process. Making it safe for a stranger's markup
# means disabling scripting, installing a request interceptor, pinning a
# content policy and trusting all three to stay correct through every Qt
# upgrade. QTextBrowser has NO script engine to disable and NO network stack to
# intercept. It cannot fetch anything except through `loadResource`, which is
# overridden below and is the only door. That is a smaller thing to be right
# about, and CONVENTIONS.txt §7 is the reason to prefer the smaller thing.
#
# The second is that the fidelity gap is narrower than it looks. HTML mail is
# tables, inline styles and `<font>` — not because senders like it, but because
# it has to survive Outlook, which renders with Word. Qt's rich text engine is
# roughly at that level, which is exactly the level mail is written for.
#
# THE COST IS REAL AND IS NOT HIDDEN. Modern CSS — flexbox, grid, media queries
# — does not render here, so a newsletter built for a browser will look plainer
# than it does in Thunderbird. If that proves unacceptable against real mail
# rather than against a guess, the upgrade is a QWebEngineView behind the same
# sanitiser, and the sanitiser is where the security lives either way.
#
# LINKS DO NOT NAVIGATE. `setOpenLinks(False)` stops a click replacing the
# message with a web page inside the reading pane, which is both disorienting
# and a way to be shown something that was never sanitised. The URL is emitted
# instead, and the window decides.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QFrame, QTextBrowser

from ..render import sanitise as sanitise_mod


class MessageView(QTextBrowser):
    """A message body, sanitised, with no way out to the network."""

    link_activated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._anchor)
        # The attachments this message carries, by absolute path. Nothing
        # outside this set is ever served — see `loadResource`.
        self._permitted: set = set()
        self._root: Path | None = None
        self.blocked_remote = 0

    # ------------------------------------------------------------- showing
    def show_body(self, *, text: str = "", html: str = "", attachments=(),
                  allow_remote: bool = False, palette: dict | None = None,
                  attachments_root: Path | str | None = None) -> int:
        """Draw one message. Returns how many remote resources were withheld.

        `text` is used when there is no HTML at all, which is a real and common
        case and deserves to be shown as what it is rather than converted into
        markup.
        """
        self._root = Path(attachments_root).resolve() if attachments_root else None
        cid_map, self._permitted = self._inline(attachments)

        if html:
            result = sanitise_mod.sanitise(html, allow_remote=allow_remote,
                                           cid_map=cid_map)
            self.blocked_remote = result.blocked_remote
            document = sanitise_mod.document(result, palette=palette,
                                             allow_remote=allow_remote)
        else:
            self.blocked_remote = 0
            document = sanitise_mod.plain_document(text, palette=palette)

        # setHtml, on a document whose every URL has already been through the
        # sanitiser and whose every resource must still pass `loadResource`.
        self.setHtml(document)
        self.verticalScrollBar().setValue(0)
        return self.blocked_remote

    def clear_body(self) -> None:
        self._permitted = set()
        self.blocked_remote = 0
        self.setHtml("")

    def _inline(self, attachments) -> tuple:
        """Map each inline part's Content-ID to the file holding its bytes."""
        cid_map: dict = {}
        permitted: set = set()
        for part in attachments or ():
            stored = (part["stored_path"] or "").strip()
            if not stored:
                continue
            path = Path(stored)
            if not self._inside_root(path):
                continue
            permitted.add(str(path.resolve()))
            content_id = (part["content_id"] or "").strip().strip("<>")
            if content_id:
                cid_map[content_id] = QUrl.fromLocalFile(str(path)).toString()
        return cid_map, permitted

    def _inside_root(self, path: Path) -> bool:
        """Containment, again. `store.ingest` already placed every attachment
        under the root; this is the check that the row still says so."""
        if self._root is None:
            return False
        try:
            resolved = path.resolve()
        except OSError:                                      # pragma: no cover
            return False
        return self._root == resolved or self._root in resolved.parents

    # ------------------------------------------------------------ the door
    def loadResource(self, kind: int, url: QUrl):            # noqa: N802 (Qt)
        """The ONLY way any byte reaches the rendered document.

        Qt calls this for every image the document references. Anything that is
        not one of THIS message's own inline attachments is refused by
        returning None, at which point Qt draws nothing and — the part that
        matters — makes no request.

        The set is rebuilt per message, so a message cannot reference another
        message's attachment, and the path is compared after resolution, so a
        symbolic link in the attachments directory cannot point out of it.
        """
        if not url.isLocalFile():
            return None
        try:
            path = Path(url.toLocalFile()).resolve()
        except OSError:                                      # pragma: no cover
            return None
        if str(path) not in self._permitted:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _anchor(self, url: QUrl) -> None:
        self.link_activated.emit(url.toString())

    # ------------------------------------------------------------- styling
    def palette_for(self, theme) -> dict:
        """The reading pane's colours, as the sanitiser's page wants them."""
        return {
            "fg": theme.text,
            "bg": theme.surface,
            "link": theme.accent,
            "quote": theme.text_muted,
            "font": self.font().family() or "system-ui, sans-serif",
            "size": max(8, self.font().pointSize() or 10),
        }
