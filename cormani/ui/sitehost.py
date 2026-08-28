# SPDX-License-Identifier: GPL-3.0-or-later
#
# Which site panel is showing, and the panels themselves.
#
# `ui/calendarhost.py`'s shape and `ui/trackhost.py`'s, for the reason both
# have it: holding panes is not the same job as arranging widgets. This one
# owns whatever site panels have been opened and the swapping between them and
# the mail.
#
# ── PANELS ARE MADE ON FIRST USE AND THEN KEPT ─────────────────────────────
#
# A QWebEngineView is a browser: a render process, a network stack, a
# JavaScript engine. Making six at start-up would cost six of those to show a
# window somebody opened to read mail — so a site's panel is built the first
# time it is selected, and kept afterwards, because a panel rebuilt on every
# visit would reload the page and lose the scroll position each time.
#
# THE PROFILES OUTLIVE EVEN THIS. `panels/profiles.py` keeps them at module
# level, so a panel closed and made again is the same session — and, more to
# the point, releasing a profile while a page still holds it is finding 3's
# "Expect troubles!".
#
# ── THREE PANES NOW CLAIM THE SAME SPACE ───────────────────────────────────
#
# The calendar, the tracking board, a site panel and the address book all go
# where the list and the reading pane are. Whoever was asked last owns it, and
# the others are told to stand down — a pane left visible under another is a
# window with two things drawn on top of each other, which reads as a rendering
# fault rather than as a mistake. `ui/panespace.py` is the one place that knows
# who the claimants are; this file used to name two of them and was made wrong
# by the fourth arriving.
#
# ── THE UNREAD COUNT ONLY EXISTS WHILE A PANEL DOES ────────────────────────
#
# There is no background polling and no hidden page. A site nobody has opened
# has no count — not zero, UNKNOWN — and the rail draws no badge for it. The
# alternative is six browsers running behind a mail window, which is both a
# resource cost and a claim about what corMani is doing that would have to be
# made honestly somewhere.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import logging

from ..panels import sites as sites_mod
from . import panespace
from .sitepanel import SitePanel

log = logging.getLogger("cormani")


class SiteHost:
    """The site panels, and the switching between them and the mail."""

    def __init__(self, pane) -> None:
        self.pane = pane
        self._panels: dict = {}
        self._user_agent = ""
        self.showing = ""            # the site key, or "" for none

    def set_user_agent(self, agent: str) -> None:
        """The derived Chrome identity, from `app._apply_engine_identity`.

        Passed in rather than read here, so that the one place that decides
        what corMani claims to be stays one place — `platform/runtime` derives
        it from the engine's own default and nothing writes a version down.
        """
        self._user_agent = agent or ""

    # ------------------------------------------------------------- swapping
    def panel(self, key: str):
        """The panel for a site, made on first use. None if it is not a site."""
        site = sites_mod.get(key)
        if site is None:
            return None
        existing = self._panels.get(site.key)
        if existing is not None:
            return existing
        panel = SitePanel(site, user_agent=self._user_agent, parent=self.pane)
        panel.setVisible(False)
        panel.status_message.connect(self.pane.status_message)
        panel.open_externally.connect(self.pane.open_link)
        panel.unread_changed.connect(self._unread)
        self.pane.splitter.addWidget(panel)
        self._panels[site.key] = panel
        log.info("site panel created: %s", site.key)
        return panel

    def show(self, key: str) -> bool:
        """Swap the middle and the reader for one site's panel.

        `key` of "" puts the mail back. Returns whether a panel is showing.
        """
        wanted = sites_mod.get(key) if key else None
        if wanted is None:
            self._hide_all()
            self.showing = ""
            for widget in (self.pane.middle, self.pane.reader):
                widget.setVisible(True)
            return False

        # The other panes want this space as well — `ui/panespace.py` knows
        # which, because a list written here was wrong within one stage.
        panespace.claim(self.pane, "sites")

        panel = self.panel(wanted.key)
        self._hide_all()
        for widget in (self.pane.middle, self.pane.reader):
            widget.setVisible(False)
        panel.setVisible(True)
        panel.load()
        self.showing = wanted.key
        return True

    def _hide_all(self) -> None:
        for panel in self._panels.values():
            panel.setVisible(False)

    def chosen(self, key: str) -> None:
        """The rail selected a site row."""
        self.show(key)

    def title(self) -> str:
        """What the tab says while a panel is showing."""
        site = sites_mod.get(self.showing) if self.showing else None
        return site.name if site else "Mail"

    # -------------------------------------------------------------- unread
    def _unread(self, key: str, count) -> None:
        """A panel reported. The rail redraws; nothing else is told.

        The count is NOT written to the store and never has been. It is a
        property of a page that is open right now, it is meaningless when the
        panel closes, and a decade of correspondence is not the place to keep
        a number that expires in six seconds.
        """
        rail = getattr(self.pane, "rail", None)
        model = getattr(rail, "model_obj", None) if rail is not None else None
        if model is not None and hasattr(model, "set_site_unread"):
            model.set_site_unread(key, count)

    def unread(self) -> dict:
        """What each open panel last reported, for the window's status bar."""
        return {key: panel.counter.count
                for key, panel in self._panels.items()
                if panel.counter.count}

    # ------------------------------------------------------------- sessions
    def sign_out(self, key: str) -> bool:
        """Throw one site's session away. Returns whether there was one to throw.

        THE PROFILE SURVIVES AND ONLY ITS DATA GOES. Finding 3: a page may
        still reference the profile, and releasing one that is referenced is
        the "Expect troubles!" case — so the cookies, the cache and the
        visited links are cleared and the object stays.

        The panel is then RELOADED, because a panel still showing a signed-in
        page whose session has just been destroyed is a panel telling a person
        something untrue. What they should see is the sign-in screen, which is
        what signing out means.
        """
        from ..panels import profiles as profiles_mod

        had = profiles_mod.forget(key)
        panel = self._panels.get(key)
        if panel is not None:
            panel.reload()
        log.info("signed out of %s (session present: %s)", key, had)
        return had

    # ------------------------------------------------------------- lifetime
    def reload(self) -> None:
        panel = self._panels.get(self.showing)
        if panel is not None:
            panel.reload()

    def shutdown(self) -> None:
        """Stop every panel's timer. Called when the window closes.

        The panels and their profiles are NOT destroyed here. Qt owns the
        widgets and `panels/profiles.py` owns the profiles for the life of the
        process; tearing either down in the right order at exit is a problem
        this does not need to have.
        """
        for panel in self._panels.values():
            panel.shutdown()

    def apply_theme(self, theme) -> None:
        """Nothing. A site draws itself, and a panel that restyled somebody
        else's page would be DOM automation — which PLAN.txt §7 rules out, and
        which would look wrong the first time the site changed."""
