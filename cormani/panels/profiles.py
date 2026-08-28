# SPDX-License-Identifier: GPL-3.0-or-later
#
# One web profile per site, owned by the application.
#
# ── PROFILES MUST OUTLIVE THEIR PAGES ──────────────────────────────────────
#
# docs/toolkit-verification.txt, finding 3: releasing a QWebEngineProfile while
# a QWebEnginePage still references it prints "Release of profile requested but
# WebEnginePage still not deleted. Expect troubles!" and means it. So a profile
# belongs to a long-lived registry — this one — and never to the widget that
# happens to be showing it. A panel closed and reopened gets the SAME profile,
# which is also what keeps it signed in.
#
# ── ISOLATION IS BY CONSTRUCTION, NOT BY ENFORCEMENT ───────────────────────
#
# CONVENTIONS.txt §7 requires one profile per site, no shared cookie jar, and
# no bridge from a panel into the application. The first two are free: a
# profile constructed with a storage NAME is persistent and derives its storage
# and cache directories from that name, so WhatsApp's cookies are in
# `QtWebEngine/site-whatsapp` and LinkedIn's are in `site-linkedin`, and
# neither can see the other. Nothing to configure and nothing to get wrong —
# finding 5.
#
# THE DEFAULT PROFILE IS NEVER USED FOR A PANEL. It is off-the-record, so a
# panel on it would lose its session at every restart; and it is shared, so
# every site on it would share one cookie jar. `app._apply_engine_identity`
# touches it to set the user agent and that is all it is for.
#
# ── WHERE A SESSION LIVES IS CORMANI'S DECISION, NOT QT'S ─────────────────────
#
# A named profile derives its storage from QStandardPaths, and that derivation
# reads `QCoreApplication::organizationName`. `app.run` sets one — "Manish
# Jagdish Thatte" — so the derived location is
#
#     ~/.local/share/Manish Jagdish Thatte/cormani/QtWebEngine/site-whatsapp
#
# which is a second tree, outside corMani's own XDG directory, named after a
# person. Finding 5 records the path WITHOUT an organisation name because it
# was measured in a bare probe, and no panel had ever run inside the real
# application to contradict it.
#
# So the paths are SET here rather than derived. `platform/paths.py` has
# declared `web_profiles` ("cookies and logins, so DATA") and `web_cache`
# ("disposable, so CACHE") since stage 0 and creates both at every start-up;
# until now nothing read them. A location that four unrelated Qt settings can
# move is not a location a person can be told to delete, and `--check` has to
# be able to name it without constructing a browser to ask.
#
# ── THE USER AGENT IS DERIVED AND NOT WRITTEN DOWN ─────────────────────────
#
# `platform/runtime.derive_user_agent` takes the engine's own default and
# removes the QtWebEngine token, keeping the Chrome version the engine actually
# is. Finding 1 records why: WhatsApp Web refuses an unusual user agent, and
# "QtWebEngine/6.8.2" is one. A version written as a constant here would be a
# lie the day Debian ships a newer Qt, and a lie that contradicts the client
# hints Qt sends alongside it — which cannot be overridden and would then
# disagree with the header.
#
# ── WHAT IS TURNED OFF, AND WHY EACH ────────────────────────────────────────
#
# A panel is a signed-in session on somebody else's site, which is the most
# valuable thing in this application after the mail store. The settings below
# are the ones that are off because a messaging site does not need them and
# each is a way out: screen capture, WebRTC's real interface addresses,
# hyperlink auditing, and reading back from a canvas. JavaScript stays ON —
# there is no site here without it — and that is the honest cost of the
# feature, which stage 9 audits rather than removes.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import logging
from pathlib import Path

from .sites import Site

log = logging.getLogger("cormani")

# Kept for the lifetime of the process. A module-level registry rather than a
# member of a window, because a profile must outlive every page ever made on it
# and a window is not the longest-lived thing here.
_PROFILES: dict = {}

# The two roots, settable once at start-up. Module level for the same reason
# the registry is: they must outlive every window. `Paths()` by default, so a
# caller that forgets still gets corMani's own directory rather than whatever
# Qt would have derived — the failure mode this exists to prevent is a silent
# relocation, so the fallback must be the right answer and not a null.
_ROOTS: dict = {}


def use_storage(profiles_root, cache_root) -> None:
    """Where panel sessions and their caches go. Called once, at start-up.

    The suite calls it too, and must: a QWebEngineProfile writes its directory
    the moment it is constructed, so a test that does not redirect this writes
    into the real profile — which is what `tests/support.py`'s "nothing written
    outside a temporary directory" exists to forbid.
    """
    _ROOTS["profiles"] = Path(profiles_root)
    _ROOTS["cache"] = Path(cache_root)


def _roots() -> tuple:
    if "profiles" not in _ROOTS:
        from ..platform.paths import Paths
        paths = Paths()
        use_storage(paths.web_profiles, paths.web_cache)
    return _ROOTS["profiles"], _ROOTS["cache"]


def storage_for(site: Site) -> Path:
    """Where one site's session is, WITHOUT constructing anything.

    `--check` runs with no QApplication and must still be able to say where a
    session is; and a person told to delete a directory needs the name of one
    that exists whether or not the application is running. `storage_paths`
    below answers the same question from live profiles, and the two must agree
    — a test holds them to it.
    """
    return _roots()[0] / site.profile_name


def cache_for(site: Site) -> Path:
    return _roots()[1] / site.profile_name


def _configure(profile, site: Site, *, user_agent: str = "") -> None:
    """Everything a panel profile needs, in one place so no site is different."""
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings

    if user_agent:
        profile.setHttpUserAgent(user_agent)

    # On disk, so a panel stays signed in across restarts. This is the whole
    # difference between a panel and a browser tab somebody has to log into
    # every morning, and it is why the default off-the-record profile is not
    # usable here.
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)

    # ASKED EVERY TIME, and never remembered. A site that has been granted the
    # microphone once has it for ever, and "for ever" on a profile that is
    # signed into somebody's WhatsApp is not a decision to make by clicking
    # once. `ui/sitepanel.py` refuses them all by default in any case; this is
    # the second lock.
    try:
        profile.setPersistentPermissionsPolicy(
            QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime)
    except AttributeError:                                   # pragma: no cover
        # Qt older than 6.8. The per-request refusal in the panel still holds.
        pass

    settings = profile.settings()
    attribute = QWebEngineSettings.WebAttribute
    for name, value in (
            # A messaging site has no reason to record the screen.
            ("ScreenCaptureEnabled", False),
            # Without this, WebRTC discloses every local interface address —
            # which is a fingerprint and a network map, offered to a page.
            ("WebRTCPublicInterfacesOnly", True),
            # A ping-back on every link click, to whoever asked for it.
            ("HyperlinkAuditingEnabled", False),
            # Reading pixels back out of a canvas is the classic fingerprint.
            # It costs some sites a rendering feature and none of these four.
            ("ReadingFromCanvasEnabled", False),
            # Mixed content on a signed-in session is not a trade worth making.
            ("AllowRunningInsecureContent", False),
            # A local file is corMani's store or the user's documents. A page
            # from the network has no business reaching either.
            ("LocalContentCanAccessFileUrls", False),
            ("LocalContentCanAccessRemoteUrls", False),
            # Media that starts by itself in a panel nobody is looking at.
            ("PlaybackRequiresUserGesture", True),
            # Sign-in flows genuinely need a second window; the panel decides
            # what to do with the request rather than the page deciding for it.
            ("JavascriptCanOpenWindows", site.allows_popups),
            # Pasting INTO a page is a person's own doing; a page reading the
            # clipboard unasked is not.
            ("JavascriptCanAccessClipboard", False),
            ("JavascriptCanPaste", False)):
        try:
            settings.setAttribute(getattr(attribute, name), value)
        except AttributeError:                               # pragma: no cover
            # A Qt without this attribute. Named in the log rather than
            # ignored: the setting is a security decision and its absence
            # should be visible, not assumed.
            log.warning("QtWebEngine has no setting %s; panel %s is without it",
                        name, site.key)


def for_site(site: Site, *, user_agent: str = ""):
    """The persistent profile for one site, made once and then kept.

    The registry is what stops a second panel on the same site making a second
    profile — which would be a second cookie jar, and a sign-in that the other
    panel could not see.
    """
    from PySide6.QtWebEngineCore import QWebEngineProfile

    from PySide6.QtCore import QCoreApplication

    existing = _PROFILES.get(site.key)
    if existing is not None:
        return existing
    # PARENTED TO THE APPLICATION, and that is finding 3 being obeyed rather
    # than merely recorded. A module-level dict keeps the profile alive against
    # Python's collector; it does NOT decide the order in which Qt and CPython
    # tear things down at exit, and getting that order wrong prints "Release of
    # profile requested but WebEnginePage still not deleted. Expect troubles!".
    # Giving the profile a Qt parent that outlives every widget makes the
    # ownership explicit to the toolkit that actually does the freeing. The
    # warning appeared in this suite before the parent was added.
    profile = QWebEngineProfile(site.profile_name,
                                QCoreApplication.instance())
    # BEFORE `_configure`, and before anything can touch a cookie: the profile
    # has already chosen a derived directory by now, and these two calls move
    # it to corMani's own. See the header — the derived one is named after the
    # organisation and sits outside the XDG tree everything else here uses.
    storage, cache = storage_for(site), cache_for(site)
    storage.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    profile.setPersistentStoragePath(str(storage))
    profile.setCachePath(str(cache))
    _configure(profile, site, user_agent=user_agent)
    _PROFILES[site.key] = profile
    log.info("web profile %s at %s", site.profile_name,
             profile.persistentStoragePath())
    return profile


def existing(site_key: str):
    """The profile for a site, or None if no panel has ever opened it."""
    return _PROFILES.get(site_key)


def known() -> list:
    return sorted(_PROFILES)


def storage_paths() -> dict:
    """Where each LIVE profile's session is, as the profile itself reports it.

    Only sites a panel has opened appear here, so this is the running
    application's answer. `--check` has no QApplication and wants
    `storage_for` instead, which answers for every site and constructs
    nothing. The two agree by construction and a test says so.
    """
    return {key: profile.persistentStoragePath()
            for key, profile in _PROFILES.items()}


def forget(site_key: str) -> bool:
    """Sign a site out by throwing its session away. Returns whether there was
    one.

    THE PROFILE OBJECT IS KEPT AND ONLY ITS DATA IS CLEARED, because pages may
    still reference it — finding 3 again. Dropping it from the registry to let
    it be collected is exactly the "Expect troubles!" case.
    """
    profile = _PROFILES.get(site_key)
    if profile is None:
        return False
    profile.cookieStore().deleteAllCookies()
    profile.clearHttpCache()
    profile.clearAllVisitedLinks()
    return True
