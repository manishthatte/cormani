# SPDX-License-Identifier: GPL-3.0-or-later
#
# One site, in the space the list and the reading pane occupy.
#
# PLAN.txt §3: "Selecting a site in the rail replaces the list and reader with
# that site's live web panel. Same window, same rail, no tab switching." This
# is that panel — a QWebEngineView on the site's own persistent profile, a
# strip above it saying what is happening, and nothing else.
#
# ── THIS IS THE ONLY PLACE IN corMani WHERE A STRANGER'S JAVASCRIPT RUNS ───
#
# `ui/messageview.py` chose QTextBrowser over QWebEngineView precisely to avoid
# it, and its header says why: a script engine and a network stack are a large
# thing to be right about. A site panel cannot avoid it — the sites do not work
# without JavaScript — so what is left is to make the blast radius small and to
# write down where its edges are.
#
#   NO BRIDGE. There is no QWebChannel, no registered object, no injected
#   script that can call back. The page can reach nothing in this process; the
#   only thing corMani takes OUT of it is a number, and `panels/unread.py` is
#   the whole of that.
#
#   NO STORE. This widget is constructed without a database connection and has
#   no attribute holding one. It cannot read the mail because there is no path
#   from here to it — CONVENTIONS.txt §7 asks for that and this is how it is
#   guaranteed rather than promised.
#
#   PERMISSIONS ARE REFUSED, not asked about. A messaging panel does not need
#   the camera, the microphone, the screen or the position of the machine, and
#   a dialog offering them is a dialog somebody eventually says yes to. The
#   refusal is silent and the site degrades, which is what these sites do
#   anyway when a permission is denied in a browser.
#
#   DOWNLOADS ARE REFUSED IN THE PANEL AND OFFERED TO THE DESKTOP. A file
#   arriving from a signed-in session is a real thing a person wants — an
#   attachment somebody sent on WhatsApp — but a panel writing to disk by
#   itself is not. The request is cancelled and the URL is handed out through
#   `platform/desktop.open_url`, which is the same door the reading pane's
#   links use and checks the scheme a second time.
#
# ── A POPUP IS A SIGN-IN, NOT AN ADVERTISEMENT ─────────────────────────────
#
# Every one of these sites opens a window during sign-in — an OAuth hop, a
# second-factor prompt. A panel that swallowed them would be a panel nobody
# can sign into. They are opened in the DESKTOP browser rather than in a second
# panel: the sign-in then happens in a browser the person already trusts, the
# cookie comes back to the panel because the flow redirects to the site, and
# corMani never has to be a window manager.
#
# ── THE STRIP SAYS WHICH OF FOUR STATES IT IS IN ───────────────────────────
#
# Loading, loaded, failed, and "the engine is old" — because when a site refuses
# to work the first question is which of those it is, and a blank white panel
# answers none of them. docs/toolkit-verification.txt finding 2 is why the
# fourth exists: Debian pins the embedded Chromium, and WhatsApp Web will
# eventually refuse it outright. That must read as a known cost, not a fault.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import logging

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from ..panels import profiles as profiles_mod
from ..panels import unread as unread_mod

log = logging.getLogger("cormani")


class SitePanel(QWidget):
    """A live web panel for one site. Holds no database connection."""

    unread_changed = Signal(str, object)      # site key, count or None
    status_message = Signal(str)
    open_externally = Signal(str)             # a URL for the desktop browser

    def __init__(self, site, *, user_agent: str = "", parent=None) -> None:
        super().__init__(parent)
        # DELIBERATELY NO `self._con`. See the header: the guarantee that a
        # panel cannot reach the store is that there is no path from here to
        # it, rather than a rule somebody has to keep.
        self.site = site
        self._user_agent = user_agent
        self._loaded = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._strip())
        self.view = self._make_view()
        outer.addWidget(self.view, 1)
        self.counter = unread_mod.Counter(site, self.view.page(),
                                          self._counted)

    # ------------------------------------------------------------ building
    def _strip(self) -> QWidget:
        bar = QWidget(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 5, 8, 5)
        self.status = QLabel("", bar)
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.reload_button = QPushButton("Reload", bar)
        self.reload_button.setFlat(True)
        self.reload_button.clicked.connect(self.reload)
        row.addWidget(self.reload_button)
        self.browser_button = QPushButton("Open in browser", bar)
        self.browser_button.setFlat(True)
        self.browser_button.clicked.connect(
            lambda: self.open_externally.emit(self.site.url))
        row.addWidget(self.browser_button)
        return bar

    def _make_view(self):
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView

        profile = profiles_mod.for_site(self.site,
                                        user_agent=self._user_agent)
        view = QWebEngineView(self)
        # The page is constructed on the SITE'S profile and given to the view,
        # rather than letting the view make one on the default profile. That
        # default is off-the-record and shared: a panel on it would lose its
        # sign-in at every restart and share a cookie jar with every other
        # site.
        page = QWebEnginePage(profile, view)
        page.loadStarted.connect(self._load_started)
        page.loadFinished.connect(self._load_finished)
        page.newWindowRequested.connect(self._new_window)
        page.certificateError.connect(self._certificate_error)
        self._connect_permissions(page)
        profile.downloadRequested.connect(self._download)
        view.setPage(page)
        return view

    def _connect_permissions(self, page) -> None:
        """Refuse everything, on whichever of the two APIs this Qt has.

        Qt 6.8 replaced `featurePermissionRequested` with `permissionRequested`
        and a `QWebEnginePermission` object. Both are wired where they exist,
        because a panel that silently granted the microphone on the Qt it was
        NOT tested against is the kind of difference nobody notices.
        """
        if hasattr(page, "permissionRequested"):
            page.permissionRequested.connect(self._permission)
        if hasattr(page, "featurePermissionRequested"):
            page.featurePermissionRequested.connect(self._old_permission)

    # -------------------------------------------------------------- loading
    def load(self) -> None:
        """Go to the site. Idempotent — a panel shown twice does not reload."""
        if self.view.url().isEmpty():
            self.view.setUrl(QUrl(self.site.url))
            self.counter.start()

    def reload(self) -> None:
        self.view.setUrl(QUrl(self.site.url))
        self.status_message.emit(f"Reloading {self.site.name}…")

    def shutdown(self) -> None:
        """Stop asking the page anything. The PROFILE is left alone — it
        outlives every page made on it, and dropping it is finding 3's
        "Expect troubles!"."""
        self.counter.stop()

    def _load_started(self) -> None:
        self.status.setText(f"Loading {self.site.name}…")

    def _load_finished(self, ok: bool) -> None:
        self._loaded = bool(ok)
        if ok:
            self.status.setText(self.site.hint)
            self.counter.poll()
            return
        # A panel that has failed says the two things a person can act on:
        # that it is the site and not corMani, and that the engine has an age.
        self.status.setText(
            f"{self.site.name} did not load. The network, the site, or the "
            f"embedded browser — which is {self._engine_note()}. Mail and the "
            f"calendar are unaffected.")
        self.unread_changed.emit(self.site.key, None)

    def _engine_note(self) -> str:
        """The engine's age, in the sentence where it matters.

        docs/toolkit-verification.txt finding 2: Debian pins the embedded
        Chromium and it will eventually be refused outright by these sites.
        That is a known price of the no-vendoring rule, and it should read as
        one rather than as a defect.
        """
        from ..platform.runtime import chrome_version

        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile
            version = chrome_version(
                QWebEngineProfile.defaultProfile().httpUserAgent())
        except Exception:                                    # pragma: no cover
            version = ""
        return f"Chromium {version}" if version else "older than a browser's"

    def _counted(self, key: str, number) -> None:
        self.unread_changed.emit(key, number)

    # ------------------------------------------------------------- refusals
    def _permission(self, permission) -> None:
        """Qt 6.8's permission request. Denied, and said in the log."""
        try:
            name = permission.permissionType().name
        except AttributeError:                               # pragma: no cover
            name = "unknown"
        log.info("panel %s asked for %s; denied", self.site.key, name)
        try:
            permission.deny()
        except AttributeError:                               # pragma: no cover
            pass

    def _old_permission(self, origin, feature) -> None:
        """The pre-6.8 spelling of the same refusal."""
        from PySide6.QtWebEngineCore import QWebEnginePage

        log.info("panel %s asked for %s; denied", self.site.key, feature)
        self.view.page().setFeaturePermission(
            origin, feature,
            QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)

    def _certificate_error(self, error) -> None:
        """Never accepted, and never offered as a choice.

        A panel holds a signed-in session on somebody else's site. "Continue
        anyway" on a certificate error there is the one click that turns a
        network somebody else controls into that session.
        """
        log.warning("panel %s certificate error: %s", self.site.key,
                    error.description())
        error.rejectCertificate()
        self.status_message.emit(
            f"{self.site.name}: the connection could not be verified and was "
            f"refused.")

    def _new_window(self, request) -> None:
        """A popup. Sent to the desktop browser rather than opened here.

        The sign-in flows need one and a panel that swallowed them would be a
        panel nobody can sign into — but corMani is not a window manager, and
        a second panel with its own lifetime is a second thing to get wrong.
        The browser the person already trusts does it, the flow redirects back
        to the site, and the cookie lands in this profile.
        """
        url = request.requestedUrl().toString()
        log.info("panel %s asked for a window: %s", self.site.key, url)
        self.open_externally.emit(url)
        self.status_message.emit(
            f"{self.site.name} opened a window; it has gone to your browser.")

    def _download(self, item) -> None:
        """Refused here and offered to the desktop.

        A file arriving from a signed-in session is a real thing somebody
        wants; a panel writing to disk by itself is not. The URL goes out
        through the same door the reading pane's links use, which checks the
        scheme a second time.
        """
        url = item.url().toString()
        item.cancel()
        log.info("panel %s tried to download %s; refused and handed out",
                 self.site.key, url)
        self.open_externally.emit(url)
        self.status_message.emit(
            "A download was refused in the panel and handed to your browser.")
