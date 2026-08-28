# SPDX-License-Identifier: GPL-3.0-or-later
#
# Start-up: what happens, and in which order.
#
# The order is not arbitrary. Paths before anything, because the log file and
# the store both live under them. Configuration before the store, because it
# may move the store. The store before the window, because a window with no
# store to show is a window that has to handle a state that should not exist.
# Qt last, because everything above must work without it — and does, which is
# what lets the test suite run with no display.
#
# The user agent is derived here, once, and applied to the default web profile
# before any panel is built. Doing it per panel would mean a panel created
# before the first one had a different identity, and a site seeing two agents
# from one client is a fingerprint. See platform/runtime.py for why the version
# is read rather than written.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import APP_ID, APP_NAME, APP_ORG, __version__
from .config import settings as config
from .platform.paths import Paths
from .platform.runtime import MIN_COMFORTABLE_CHROME, engine_report
from .secrets import store as secrets
from .store import database, fixtures

log = logging.getLogger(APP_ID)

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR}


def setup_logging(paths: Paths, level: str = "info") -> None:
    """Log to the state directory and to stderr.

    The file is what a bug report can attach; stderr is what a developer sees.
    Both, because asking someone to reproduce a problem with a flag set is a
    request they will decline.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(paths.log_file, encoding="utf-8"))
    except OSError as exc:                                   # pragma: no cover
        print(f"cormani: cannot write {paths.log_file}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=_LEVELS.get(level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers, force=True)


def paths_for(cfg: config.Settings) -> Paths:
    """The four directories, with a `data_dir` override applied.

    CREATES NOTHING, and that is why it is separate from `build_paths` below.
    `--check` has to be able to say where the store and the panel sessions
    ARE without bringing them into existence as a side effect of asking; it
    used to answer from a bare `Paths()` instead, which meant that on a machine
    with an override it reported the store as "not created yet" while the
    application was using it perfectly well two directories away.
    """
    paths = Paths()
    if cfg.data_dir:
        paths.data = Path(cfg.data_dir).expanduser()
    return paths


def current_paths() -> Paths:
    """Where this installation keeps things, configuration and all.

    THE COMMAND LINE MUST NOT DISAGREE WITH THE WINDOW ABOUT WHERE THE STORE
    IS, and it did. `build_paths` applied `data_dir` for `app.run`, while
    `--sync`, `--resync`, `--reindex`, `--calendars` and `--add-account` each
    built a bare `Paths()` and did not. With an override set that is not an
    inconsistency, it is two stores: the window shows one, every command-line
    operation reads and writes the other, and an account added from the
    terminal never appears in the application.

    Worse than that, given what the setting is FOR. `config/settings.py` offers
    `data_dir` as "point it at an encrypted volume if you would rather" — so
    the effect of ignoring it is mail fetched onto the unencrypted default
    path, which is the one thing the person setting it was trying to prevent.

    Creates nothing. A caller that needs the directories to exist says
    `.ensure()`, as it did before.
    """
    return paths_for(config.load())


def build_paths(cfg: config.Settings) -> Paths:
    """`paths_for`, made real. Start-up's version, and the only one that
    creates directories."""
    paths = paths_for(cfg)
    if cfg.data_dir:
        log.info("store location overridden by configuration: %s", paths.data)
    return paths.ensure()


def open_store(paths: Paths, *, demo: bool = False):
    """Open the store — or, in demo mode, a disposable one under CACHE.

    Demo mode does not open the real store at all. It is not protected by a
    check inside the fixture installer; it is simply not the file being used.
    See platform/paths.demo_database.
    """
    path = paths.demo_database if demo else paths.database
    con = database.open_store(path)
    version = database.schema_version(con)
    log.info("store %s at schema version %d", path, version)
    if demo:
        con = _ensure_demo_data(con, path)
    return con


def _ensure_demo_data(con, path):
    """Install the demo data, or REBUILD it when it is an older edition.

    The rebuild is the part that had to be added. A demo store is cached, so
    the first version of this only ever installed into an empty one — and a
    store built before stage 5 then sat through the whole of stages 5 and 6
    holding 197 messages, no calendars and no tracked threads. It demonstrated
    precisely the emptiness those stages existed to fix, and the only way out
    was knowing to delete a file in the cache directory.

    DELETING AND STARTING AGAIN, rather than migrating or topping up. A demo
    store is disposable by construction — it is in the CACHE directory, it
    holds nothing of anybody's, and `app.open_store` never opens the real store
    in demo mode. Reconciling one edition of invented data with the next would
    be a migration runner for data that is regenerated in under a second.
    """
    if fixtures.is_current(con):
        return con
    stale = fixtures.is_demo(con)
    if stale:
        log.info("demo data is edition %d and this build makes %d — rebuilding",
                 fixtures.installed_version(con), fixtures.FIXTURE_VERSION)
        con.close()
        path.unlink(missing_ok=True)
        con = database.open_store(path)
    report = fixtures.install(con)
    log.info("demo data %s: %s", "rebuilt" if stale else "installed", report)
    return con


def _apply_engine_identity(cfg: config.Settings) -> dict:
    """Give the embedded browser an ordinary Chrome identity, once."""
    from PySide6.QtWebEngineCore import QWebEngineProfile

    profile = QWebEngineProfile.defaultProfile()
    report = engine_report(profile.httpUserAgent())
    profile.setHttpUserAgent(report["user_agent"])

    if report["chrome_major"] and report["chrome_major"] < MIN_COMFORTABLE_CHROME:
        # Not fatal, and not silent. When a site panel begins refusing to load,
        # the age of the engine is the first thing worth knowing, and it should
        # already be in the log rather than needing a rebuild to discover.
        log.warning("the embedded browser is Chromium %s, which is old enough "
                    "that some sites may refuse it", report["chrome_version"])
    else:
        log.info("embedded browser is Chromium %s", report["chrome_version"])
    return report


def run(argv: list[str] | None = None, *, demo: bool = False) -> int:
    argv = list(sys.argv if argv is None else argv)

    cfg = config.load()
    paths = build_paths(cfg)
    setup_logging(paths, cfg.log_level)

    log.info("%s %s starting", APP_NAME, __version__)
    log.info("config: %s", cfg.source or "defaults (no file)")
    log.info("paths: %s", paths)

    if not secrets.available():
        # Reported, not fatal. Reading mail already downloaded needs no
        # credential; only adding an account or syncing does, and those can
        # explain themselves when reached.
        log.warning("no system keyring is available (%s) — accounts cannot be "
                    "added until one is running", secrets.backend_name())
    else:
        log.info("keyring backend: %s", secrets.backend_name())

    con = open_store(paths, demo=demo)

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtWidgets import QApplication

    # QtWebEngine's requirement, and it must be set before the QApplication
    # exists rather than before a view does. It has been absent until stage 7
    # without any symptom, because nothing had ever constructed a
    # QWebEngineView — touching the default profile to set a user agent does
    # not trip it. The first site panel does, and Qt then prints a warning it
    # is too late to act on. docs/toolkit-verification.txt, finding 4.
    QCoreApplication.setAttribute(
        Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    # Set before QApplication so QSettings and the web profile paths derive
    # from them rather than from the executable's name.
    # THE ONE LINE THAT MAKES THE WINDOW PINNABLE. On Wayland the shell has no
    # WM_CLASS to match against — `StartupWMClass` in the .desktop file is an
    # X11 mechanism and is simply ignored. GNOME matches a window to its
    # launcher by the xdg-shell app_id, which Qt takes from here and nowhere
    # else. Without it the window appears as a second, anonymous entry that
    # cannot be pinned, and the launcher never shows as running.
    QGuiApplication.setDesktopFileName(APP_ID)
    QCoreApplication.setApplicationName(APP_ID)
    QCoreApplication.setApplicationVersion(__version__)
    QCoreApplication.setOrganizationName(APP_ORG)

    app = QApplication(argv)
    app.setApplicationDisplayName(APP_NAME)

    # Belt and braces beside the desktop file: this is what a window manager
    # draws before it has resolved the launcher, and what a screenshot shows.
    from .platform.runtime import resource
    icon_file = resource("cormani.svg")
    if icon_file is not None:
        app.setWindowIcon(QIcon(str(icon_file)))

    from .ui.theme import apply_to as apply_theme
    theme = apply_theme(app, cfg.theme)
    log.info("theme: %s", theme.name)

    engine = _apply_engine_identity(cfg)

    # WHERE PANEL SESSIONS GO, said rather than derived. A named profile takes
    # its directory from QStandardPaths, which reads the organisation name set
    # above — so left alone it would write a person's live logins into
    # ~/.local/share/Manish Jagdish Thatte/ instead of corMani's own tree.
    # `paths.web_profiles` has declared the right answer since stage 0 and
    # `paths.ensure` has been creating it, empty, ever since.
    from .panels import profiles as panel_profiles
    panel_profiles.use_storage(paths.web_profiles, paths.web_cache)

    from .ui.window import MainWindow
    window = MainWindow(con, settings=cfg, theme_key=cfg.theme, demo=demo,
                        attachments_root=paths.attachments,
                        attachment_cache=paths.attachment_cache)

    # The site panels, if any. The identity goes in from HERE rather than being
    # derived in the panel, so that the one place deciding what corMani claims
    # to be stays one place — `_apply_engine_identity` above took it from the
    # engine's own default and nothing writes a Chrome version down.
    window.attach_sites(cfg.site_keys(), user_agent=engine["user_agent"])

    if not demo:
        # Demo data has no server behind it, so there is nothing to sync and
        # the menu entry says so rather than offering a key that does nothing.
        from .imap.engine import options_from
        from .ui.syncing import SyncController
        window.attach_sync(SyncController(
            paths.database, options_from(cfg, paths), parent=window))
        # The calendar has its own controller and its own back-off:
        # `ui/calsyncing.py` records why the two are not one.
        from .calendar.engine import Options as CalendarOptions
        from .ui.calsyncing import CalendarSyncController
        window.attach_calendar_sync(CalendarSyncController(
            paths.database, CalendarOptions(), parent=window))
        window.start_reminders()
    # The tray is offered over demo data too: the unread count is real for the
    # fixture, and closing to the tray is the same question either way. Made
    # after the sync controller so the tray menu can offer F5 when there is one.
    window.attach_tray()
    counts = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
    window.set_store_summary(
        f"schema v{database.schema_version(con)} · {counts} accounts")
    window.set_engine_note(f"Embedded browser: Chromium {engine['chrome_version']}")
    window.show()

    try:
        return app.exec()
    finally:
        con.close()
        log.info("%s stopped", APP_NAME)
