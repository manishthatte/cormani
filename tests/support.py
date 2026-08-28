# SPDX-License-Identifier: GPL-3.0-or-later
#
# What every test in this suite needs, in one place.
#
# Three obligations, from CONVENTIONS.txt §9 and the packaging rules:
#
# NO DISPLAY. The offscreen platform is selected here, before Qt is imported by
# anything, rather than left to the caller. A suite that only passes when
# someone remembers an environment variable does not meet the requirement.
#
# NO NETWORK. Nothing here opens a socket, and nothing under test does either at
# this stage.
#
# NOTHING WRITTEN OUTSIDE A TEMPORARY DIRECTORY. QSettings is pointed at a
# temporary path as well as given a test-only organisation, because the window
# stores its geometry through it and a test suite that moves the user's window
# is a test suite people stop running. The store likewise: every connection here
# is under a directory that is deleted afterwards.
#
# python3-pyside6.qttest is NOT packaged in Debian, so there is no QTest and no
# way to synthesise a real key press or a real click. Tests therefore drive the
# widgets through their own methods and signals. Where that leaves something
# unverified — Qt's own ShortcutOverride handling, for one — it is said so
# rather than asserted around.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6.QtWidgets  # noqa: F401
    HAVE_QT = True
except ImportError:                                          # pragma: no cover
    HAVE_QT = False

requires_qt = unittest.skipUnless(HAVE_QT, "PySide6 is not installed")

try:
    import PySide6.QtWebEngineWidgets  # noqa: F401
    HAVE_WEBENGINE = True
except ImportError:                                          # pragma: no cover
    HAVE_WEBENGINE = False

# The site panels need a real browser. It IS installed here and it DOES work
# with QT_QPA_PLATFORM=offscreen — docs/toolkit-verification.txt finding 5 —
# so this guard is for a machine without the package rather than for a machine
# without a display. A panel test that quietly did not run would be worse than
# one that says it was skipped.
requires_webengine = unittest.skipUnless(
    HAVE_QT and HAVE_WEBENGINE, "QtWebEngine is not installed")

_APP = None
_SETTINGS_DIR = None


def qt_app():
    """The one QApplication, with its settings redirected somewhere disposable."""
    global _APP, _SETTINGS_DIR
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    if _APP is None:
        _SETTINGS_DIR = tempfile.mkdtemp(prefix="cormani-test-settings-")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat,
                          QSettings.Scope.UserScope, _SETTINGS_DIR)
        QCoreApplication.setApplicationName("cormani-test")
        QCoreApplication.setOrganizationName("cormani-test")
        # QtWebEngine's, and it must be set before the QApplication exists
        # rather than before a view does — docs/toolkit-verification.txt
        # finding 4. Set here as well as in `app.run` because the suite makes
        # its own QApplication and the site panel tests construct views on it.
        from PySide6.QtCore import Qt
        QCoreApplication.setAttribute(
            Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        _APP = QApplication.instance() or QApplication([])
        _redirect_web_profiles()
    return _APP


def _redirect_web_profiles() -> None:
    """Panel sessions into the temporary directory, with the settings.

    A QWebEngineProfile writes its directory the moment it is CONSTRUCTED, so
    this has to happen before the first panel test and not inside it. Until it
    did, the suite created ~/.local/share/cormani-test/cormani-test/QtWebEngine
    with a directory per site — six real directories outside the temporary one,
    which is exactly what this module's third obligation forbids. Nothing
    secret was in them, because no test signs into anything; the rule is worth
    keeping anyway, since the day a test DOES sign in is not the day to notice.
    """
    from cormani.panels import profiles

    root = Path(_SETTINGS_DIR)
    profiles.use_storage(root / "web", root / "webcache")


def dispose(widget) -> None:
    """Take a widget out of service NOW, rather than when an event loop runs.

    `deleteLater` only schedules a deletion, and these tests never spin an event
    loop, so a view registered that way outlives the test that made it — and
    then Qt calls `fetchMore` on its model, whose database connection the same
    test already closed. Detaching the model is what actually stops that: a
    model with no view attached is never called again.
    """
    from PySide6.QtWidgets import QAbstractItemView

    views = widget.findChildren(QAbstractItemView)
    if isinstance(widget, QAbstractItemView):
        views = [widget, *views]
    for view in views:
        try:
            view.setModel(None)
        except RuntimeError:                                 # already gone
            pass
        except TypeError:
            # A convenience view — QListWidget, QTableWidget — owns a model of
            # its own and makes setModel private. There is nothing to detach:
            # its model holds no database connection, which is the whole reason
            # this loop exists.
            pass
    _release_web_pages(widget)
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


def _release_web_pages(widget) -> None:
    """Give up any QWebEnginePage before its profile can outlive it.

    THE SAME PROBLEM `dispose` ALREADY EXISTS FOR, one layer down. A page is
    released when its view is DELETED, and `deleteLater` never runs here — so a
    site panel's page survives the test that made it and is still alive at
    interpreter exit, when the profile it belongs to is freed in whatever order
    CPython and Qt happen to choose. That prints "Release of profile requested
    but WebEnginePage still not deleted. Expect troubles!" — docs/toolkit-
    verification.txt finding 3 — ten times per suite run.

    IT IS A PROPERTY OF THIS SUITE AND NOT OF THE APPLICATION, which was worth
    establishing rather than assuming: with a real event loop the same panel
    tears down silently. But ten lines of warning on every run is how people
    learn to ignore output, so the page is handed back here instead.
    """
    if not HAVE_WEBENGINE:
        return
    from PySide6.QtWebEngineWidgets import QWebEngineView

    views = widget.findChildren(QWebEngineView)
    if isinstance(widget, QWebEngineView):
        views = [widget, *views]
    for view in views:
        try:
            page = view.page()
            if page is not None:
                page.setParent(None)
                page.deleteLater()
        except RuntimeError:                                 # already gone
            pass
    _drain_deferred_deletes()


def _drain_deferred_deletes() -> None:
    """Make `deleteLater` actually happen, here, with no event loop.

    THIS IS WHAT WAS MISSING, and it is one call. `dispose` and
    `_release_web_pages` both end in `deleteLater`, which POSTS a DeferredDelete
    event and returns; the event is delivered by an event loop, and this suite
    never runs one. So every page disposed of during a run was still alive at
    interpreter exit, when Qt frees the profiles it belongs to — and Qt said so,
    ten times a run: "Release of profile requested but WebEnginePage still not
    deleted. Expect troubles!", docs/toolkit-verification.txt finding 3.

    `sendPostedEvents` with that one event type delivers exactly the pending
    deletions and nothing else, which is Qt's own answer to this and is why it
    takes an event type at all. Measured: ten warnings a run before, none after.

    IT IS NOISE AND NOT A DEFECT — with a real event loop the same panel tears
    down silently, which was checked rather than assumed. It is fixed anyway,
    because ten lines of warning on every green run is how people learn to skim
    output, and the day one of those lines means something is the day it will
    be missed.
    """
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def own(case: unittest.TestCase, widget):
    """Hand a widget to the test, to be disposed of before its store closes."""
    case.addCleanup(dispose, widget)
    return widget


class FakeKeyring:
    """Just enough of the keyring API, in memory.

    Nothing in this suite may touch the real keyring: it is protected on this
    machine, and a test run that stores secrets in a developer's login keyring
    is a test run nobody should perform twice.
    """

    def __init__(self, broken: bool = False):
        self.data: dict = {}
        self.broken = broken

    def set_password(self, service, key, value):
        if self.broken:
            raise RuntimeError("no backend")
        self.data[(service, key)] = value

    def get_password(self, service, key):
        if self.broken:
            raise RuntimeError("no backend")
        return self.data.get((service, key))

    def delete_password(self, service, key):
        if self.broken:
            raise RuntimeError("no backend")
        del self.data[(service, key)]

    def get_keyring(self):
        return self


def fake_keyring(case: unittest.TestCase, *, broken: bool = False) -> FakeKeyring:
    """Redirect the secret store into memory for the duration of one test."""
    from unittest import mock

    from cormani.secrets import store as secrets

    keyring = FakeKeyring(broken=broken)
    patcher = mock.patch.object(secrets, "_backend", lambda: keyring)
    patcher.start()
    case.addCleanup(patcher.stop)
    return keyring


def temp_store(case: unittest.TestCase) -> sqlite3.Connection:
    """An empty store at the current schema, cleaned up with the test.

    The cleanup is registered FIRST, so that it runs LAST: unittest unwinds
    cleanups in reverse, and every widget handed to `own` must be detached
    before the connection under it goes away.
    """
    from cormani.store import database

    directory = tempfile.mkdtemp(prefix="cormani-test-store-")
    con = database.open_store(Path(directory) / "test.sqlite3")

    def close_and_remove():
        try:
            con.close()
        except sqlite3.Error:                                # pragma: no cover
            pass
        shutil.rmtree(directory, ignore_errors=True)

    case.addCleanup(close_and_remove)
    return con


def demo_store(case: unittest.TestCase) -> sqlite3.Connection:
    """A store with the demo fixtures in it."""
    from cormani.store import fixtures

    con = temp_store(case)
    fixtures.install(con)
    return con


def store_path(con: sqlite3.Connection) -> str:
    """The file behind a connection, for tests that reopen it to prove a write
    reached the disk rather than only the connection's cache."""
    return con.execute("PRAGMA database_list").fetchone()[2]


def reopened(con: sqlite3.Connection) -> sqlite3.Connection:
    from cormani.store import database
    return database.connect(store_path(con), read_only=True)
