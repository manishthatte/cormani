# SPDX-License-Identifier: GPL-3.0-or-later
#
# Running the calendar engine without stopping the interface.
#
# `ui/syncing.py` for the other half, and the three rules there hold unchanged:
# the worker opens its own database connection, nothing on this side touches a
# widget, and one run at a time.
#
# A SECOND CONTROLLER RATHER THAN A SECOND MODE OF THE FIRST. Two reasons, and
# neither is tidiness. Mail and calendar are different services with different
# back-offs, so an account whose mail is being fetched from Gmail can have its
# calendar refused by Google Calendar and neither should wait for the other.
# And a view that pages back to 2019 needs a calendar fetch and must not
# trigger a mail sync — which a single controller would either do or have to
# grow a mode for.
#
# THE TWO CAN RUN AT ONCE, AND WAL IS WHY THAT IS SAFE. SQLite allows one
# writer at a time; `store/database.py` sets `busy_timeout` to thirty seconds,
# so the second writer waits rather than failing. What must never happen is two
# threads sharing one CONNECTION, which is exactly what both controllers avoid
# by opening their own.
#
# A RANGE FETCH IS NOT A SYNC. `fetch` gets one window for one set of calendars
# and deliberately does not touch a sync token or the stored window —
# `calendar/sync.fetch_range` explains why — so paging into 2019 leaves the
# incremental sync of this year exactly as it was.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..calendar.engine import Options


class _Worker(QObject):
    progressed = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, database_path: Path, options: Options,
                 request=None) -> None:
        super().__init__()
        self._path = database_path
        self._options = options
        self._request = request         # (calendar ids, start, end) or None

    def run(self) -> None:
        try:
            results = (self._fetch() if self._request
                       else self._sync())
        except Exception as exc:
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")
            return
        self.completed.emit(results)

    def _sync(self) -> list:
        from ..calendar.engine import sync_once

        return sync_once(self._path, options=self._options,
                         progress=self._progress)

    def _fetch(self) -> list:
        """One window, for the calendars a view asked about."""
        from ..calendar import sync as calendar_sync
        from ..calendar.engine import CLIENTS, Engine
        from ..store import calendars as calendars_repo
        from ..store.accounts import list_accounts
        from ..store.database import connect

        ids, start, end = self._request
        con = connect(self._path)
        try:
            engine = Engine(con, options=self._options)
            accounts = {a.id: a for a in list_accounts(con)}
            reports = []
            for calendar_id in ids:
                calendar = calendars_repo.get_calendar(con, calendar_id)
                account = accounts.get(calendar.account_id) if calendar else None
                if account is None or account.provider not in CLIENTS:
                    continue
                self.progressed.emit(f"Fetching {calendar.label}…")
                client = engine._make_client(account)
                reports.append(calendar_sync.fetch_range(con, client, calendar,
                                                         start, end))
            return reports
        finally:
            con.close()

    def _progress(self, name: str, detail: dict) -> None:
        if name == "account:start":
            self.progressed.emit(f"Checking {detail['address']}’s calendars…")
        elif name == "calendar:done":
            report = detail["report"]
            if report.changed or report.removed:
                self.progressed.emit(
                    f"{detail['calendar']}: {report.changed} changed")


class CalendarSyncController(QObject):
    """Starts a calendar sync or a range fetch, and says what happened."""

    started = Signal()
    progressed = Signal(str)
    finished = Signal(str, bool)

    def __init__(self, database_path: Path, options: Options | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = Path(database_path)
        self._options = options or Options()
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._fetching = False

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, request=None) -> bool:
        if self.running:
            return False
        thread = QThread()
        worker = _Worker(self._path, self._options, request)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progressed.connect(self.progressed)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        self._thread, self._worker = thread, worker
        self._fetching = request is not None
        thread.start()
        self.started.emit()
        return True

    def fetch(self, request) -> bool:
        """Get one range for one set of calendars. Quiet when busy.

        A view emits this every time it draws a range it does not hold, which
        is once per navigation — so a fetch while one is already running is
        ordinary rather than an error, and returning False is the whole of the
        handling.
        """
        return self.start(request)

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        thread.quit()
        thread.wait(30000)
        self._retire()

    def _completed(self, results) -> None:
        fetching = self._fetching
        self._retire()
        if fetching:
            changed = sum(getattr(r, "changed", 0) for r in results)
            self.finished.emit(
                f"Fetched {changed} event{'' if changed == 1 else 's'}." if changed
                else "Nothing in that range.", True)
            return
        self.finished.emit(summarise(results),
                           all(r.ok for r in results) if results else True)

    def _failed(self, message: str) -> None:
        self._retire()
        self.finished.emit(f"Calendar sync failed: {message}", False)

    def _retire(self) -> None:
        thread, self._thread, self._worker = self._thread, None, None
        self._fetching = False
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(5000)


def summarise(results) -> str:
    """One line for the status bar. Names what failed as well as what worked."""
    if not results:
        return "No calendar is due"
    usable = [r for r in results if not r.unsupported]
    if not usable:
        return "No account here has a calendar"
    changed = sum(r.changed for r in usable)
    removed = sum(r.removed for r in usable)
    sent = sum(r.sent for r in usable)
    failed = [r for r in usable if not r.ok]
    conflicts = sum(r.conflicts for r in usable)

    parts = [f"{changed} event{'s' if changed != 1 else ''} updated" if changed
             else "No calendar changes"]
    if removed:
        parts.append(f"{removed} removed")
    if sent:
        parts.append(f"{sent} of your change{'s' if sent != 1 else ''} sent")
    if conflicts:
        parts.append(f"{conflicts} could not be sent — the event had changed "
                     f"elsewhere")
    if failed:
        parts.append(f"{failed[0].address} failed: {failed[0].error}"
                     if len(failed) == 1 else f"{len(failed)} accounts failed")
    return ". ".join(parts) + "."
