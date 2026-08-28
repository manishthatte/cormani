# SPDX-License-Identifier: GPL-3.0-or-later
#
# Automatic mail checks and optional IMAP IDLE while the window is open.
#
# `config/settings.py` defines `sync_interval_minutes`; this is where that
# number becomes a timer. Zero means manual only — F5 and the File menu — and
# is honoured rather than treated as "soon".
#
# IDLE is optional and complementary: `imap/engine.watch_once` holds one
# connection on the Inbox and reports what the server says. It runs on its own
# thread with its own database connection, for the same reason `ui/syncing.py`
# does — an sqlite3 handle belongs to the thread that opened it.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..imap.engine import Engine, Options
from ..store.database import open_store


class _IdleWorker(QObject):
    progressed = Signal(str)
    mail_seen = Signal()
    finished = Signal()

    def __init__(self, database_path: Path, options: Options) -> None:
        super().__init__()
        self._path = Path(database_path)
        self._options = options

    def run(self) -> None:
        try:
            con = open_store(self._path)
            try:
                engine = Engine(con, options=self._options)
                due = engine.due()
                if not due:
                    return
                account = due[0]
                self.progressed.emit(f"Watching {account.address}…")
                lines = engine.watch_once(account, seconds=90)
                if any("EXISTS" in line or "RECENT" in line for line in lines):
                    self.mail_seen.emit()
            finally:
                con.close()
        except Exception as exc:                               # pragma: no cover
            self.progressed.emit(f"Idle watch ended: {exc.__class__.__name__}")
        finally:
            self.finished.emit()


def attach_to_window(window, *, database_path, options,
                     interval_minutes: int) -> None:
    """Wire periodic sync and status-bar feedback on an open window."""
    if getattr(window, "_demo", False) or interval_minutes < 0:
        return
    controller = AutoSync(
        window, interval_minutes=interval_minutes,
        database_path=database_path, options=options, parent=window)
    window._autosync = controller
    controller.last_sync_changed.connect(
        lambda when: window.status_last_checked.setText(
            format_last_checked(when)))
    controller.syncing_changed.connect(window._set_sync_indicator)
    window.status_last_checked.setText(format_last_checked(None))
    controller.start()


class AutoSync(QObject):
    """Periodic sync from settings, plus an optional idle watcher."""

    last_sync_changed = Signal(object)          # datetime or None
    syncing_changed = Signal(bool)

    def __init__(self, window, *, interval_minutes: int,
                 database_path: Path, options: Options,
                 idle: bool = True, parent=None) -> None:
        super().__init__(parent)
        self._window = window
        self._path = Path(database_path)
        self._options = options
        self._interval = max(0, int(interval_minutes))
        self._idle_enabled = idle
        self._last_sync: dt.datetime | None = None
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._tick)
        self._idle_timer = QTimer(self)
        self._idle_timer.timeout.connect(self._idle_tick)
        self._idle_thread: QThread | None = None
        self._idle_worker: _IdleWorker | None = None
        self._idle_busy = False

        sync = getattr(window, "_sync", None)
        if sync is not None:
            sync.started.connect(lambda: self.syncing_changed.emit(True))
            sync.finished.connect(self._sync_finished)

    @property
    def last_sync(self) -> dt.datetime | None:
        return self._last_sync

    def start(self) -> None:
        if self._interval > 0:
            self._sync_timer.start(self._interval * 60_000)
        if self._idle_enabled and self._interval > 0:
            # Between scheduled syncs, a light wait on the first due account.
            self._idle_timer.start(max(120_000, self._interval * 30_000))

    def stop(self) -> None:
        self._sync_timer.stop()
        self._idle_timer.stop()
        thread = self._idle_thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(5000)

    def note_manual_sync(self) -> None:
        """Called when the user presses F5 — the clock resets either way."""
        self._mark_sync()

    def _tick(self) -> None:
        sync = getattr(self._window, "_sync", None)
        if sync is None or sync.running:
            return
        if sync.start():
            self.syncing_changed.emit(True)

    def _sync_finished(self, _summary: str, _ok: bool) -> None:
        self.syncing_changed.emit(False)
        self._mark_sync()

    def _mark_sync(self) -> None:
        self._last_sync = dt.datetime.now().astimezone()
        self.last_sync_changed.emit(self._last_sync)

    def _idle_tick(self) -> None:
        sync = getattr(self._window, "_sync", None)
        if sync is not None and sync.running:
            return
        if self._idle_busy:
            return
        thread = QThread()
        worker = _IdleWorker(self._path, self._options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.mail_seen.connect(self._mail_seen)
        worker.finished.connect(thread.quit)
        worker.finished.connect(lambda: self._idle_done(thread))
        self._idle_thread, self._idle_worker = thread, worker
        self._idle_busy = True
        thread.start()

    def _mail_seen(self) -> None:
        sync = getattr(self._window, "_sync", None)
        if sync is not None and not sync.running:
            sync.start()

    def _idle_done(self, thread: QThread) -> None:
        self._idle_busy = False
        if self._idle_thread is thread:
            self._idle_thread = None
            self._idle_worker = None


def format_last_checked(when: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    """One line for the status bar's permanent widget."""
    if when is None:
        return "Not checked yet"
    now = now or dt.datetime.now().astimezone()
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        return "Checked just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"Checked {minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"Checked {hours} h ago"
    return when.strftime("Checked %-d %b %H:%M")
