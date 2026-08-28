# SPDX-License-Identifier: GPL-3.0-or-later
#
# Running the sync engine without stopping the interface.
#
# The engine itself knows nothing about Qt — see `imap/engine.py` — and this is
# the whole of the glue. Three things it has to get right:
#
# THE WORKER OPENS ITS OWN DATABASE CONNECTION. An sqlite3 connection belongs
# to the thread that made it, and sharing the interface's handle with a sync
# is how a mail client corrupts its own store. `engine.sync_once` opens one and
# closes it; WAL is what lets the list redraw while it writes.
#
# NOTHING ON THIS SIDE TOUCHES A WIDGET. The engine's progress callback runs on
# the worker thread, so it does one thing: emit a signal. Qt queues a
# cross-thread connection by itself, and the slots at the other end run where
# the widgets are.
#
# ONE SYNC AT A TIME, AND F5 DURING ONE IS NOT AN ERROR. Two engines against
# one store would fight over the offline queue and re-fetch each other's work.
# `start` returns False when one is already running, and the caller says so in
# the status bar rather than raising.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..imap.engine import Options, sync_once


class _Worker(QObject):
    """Lives on the sync thread. Owns nothing the interface can see."""

    progressed = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, database_path: Path, options: Options) -> None:
        super().__init__()
        self._path = database_path
        self._options = options

    def run(self) -> None:
        try:
            results = sync_once(self._path, options=self._options,
                                progress=self._progress)
        except Exception as exc:
            # A crash in the engine must reach the interface as a message, not
            # as a dead thread and a spinner that never stops.
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")
            return
        self.completed.emit(results)

    def _progress(self, name: str, detail: dict) -> None:
        if name == "account:start":
            self.progressed.emit(f"Checking {detail['address']}…")
        elif name == "folder:done":
            report = detail["report"]
            if not report.changed:
                return
            where = f"{detail['address']} — {report.folder}"
            self.progressed.emit(f"{where}: {report.new} new" if report.new
                                 else where)


class SyncController(QObject):
    """Starts a sync, reports on it, and says when the store has changed."""

    started = Signal()
    progressed = Signal(str)
    # The AccountResult list, before the summary. The notifier needs the ids;
    # the status bar needs the sentence. Both come from the same completion.
    results_ready = Signal(object)
    finished = Signal(str, bool)               # a summary, and whether it went

    def __init__(self, database_path: Path, options: Options,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = Path(database_path)
        self._options = options
        self._thread: QThread | None = None
        self._worker: _Worker | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> bool:
        """Begin a sync. False when one is already under way."""
        if self.running:
            return False
        thread = QThread()
        worker = _Worker(self._path, self._options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progressed.connect(self.progressed)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        self._thread, self._worker = thread, worker
        thread.start()
        self.started.emit()
        return True

    def stop(self) -> None:
        """Wait for the current sync to end. Called when the window closes.

        There is no cancellation: a sync interrupted mid-folder is exactly the
        case the store is built to survive — the state is written after the
        messages — so waiting is both simpler and safer than a flag that every
        loop would have to check.
        """
        thread = self._thread
        if thread is None:
            return
        thread.quit()
        thread.wait(30000)
        self._retire()

    def _completed(self, results) -> None:
        self._retire()
        self.results_ready.emit(results)
        self.finished.emit(summarise(results), all(r.ok for r in results))

    def _failed(self, message: str) -> None:
        self._retire()
        self.finished.emit(f"Sync failed: {message}", False)

    def _retire(self) -> None:
        thread, self._thread, self._worker = self._thread, None, None
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(5000)


def summarise(results) -> str:
    """One line for the status bar. Says what failed, not only what worked.

    CONVENTIONS.txt §8: a report that mentions only the successes is a report
    that hides the account which has stopped receiving mail.
    """
    if not results:
        return "No account is due — every one is disabled or waiting"
    new = sum(r.new for r in results)
    sent = sum(r.sent for r in results)
    remaining = sum(r.remaining for r in results)
    failed = [r for r in results if not r.ok]

    parts = [f"{new} new message{'s' if new != 1 else ''}" if new
             else "No new mail"]
    if sent:
        parts.append(f"{sent} change{'s' if sent != 1 else ''} sent")
    posted = sum(r.posted for r in results)
    if posted:
        parts.append(f"{posted} message{'s' if posted != 1 else ''} posted")
    if remaining:
        parts.append(f"{remaining} still to fetch")
    if failed:
        if len(failed) == 1:
            parts.append(f"{failed[0].address} failed: {failed[0].error}")
        else:
            parts.append(f"{len(failed)} accounts failed")
    return ". ".join(parts) + "."
