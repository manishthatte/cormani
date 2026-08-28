# SPDX-License-Identifier: GPL-3.0-or-later
#
# Which account's calendars, when, and what to do when they refuse.
#
# `imap/engine.py`'s shape, and everything it says about back-off, sequencing
# and a refused credential holds here. Four things are this engine's own:
#
# THE BACK-OFF IS PER CALENDAR AND NEVER TOUCHES THE ACCOUNT'S. Migration 7
# gives the reason and it is a real configuration rather than a hypothetical:
# a Google account added with an app password syncs mail perfectly and cannot
# read a calendar at all, because Google issues those for one and refuses them
# for the other. An engine that parked the ACCOUNT over that would stop the
# mail of an account whose mail works.
#
# A PROVIDER WITHOUT A CALENDAR API IS NOT A FAILURE. A plain IMAP account has
# no calendar to sync; it is reported as such, once, and never retried. Writing
# it into an error field would put a permanent red mark against an account that
# is behaving exactly as expected.
#
# THE QUEUE DRAINS BEFORE THE FETCH, ALWAYS. Same rule and same reason as the
# mail side: a meeting the user moved on a train must reach the provider before
# the fetch asks what the provider holds, or the fetch brings the old time back
# and the change appears not to have worked.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from ..auth import credentials as auth
from ..auth.credentials import NotConfigured
from ..store import accounts as accounts_repo
from ..store import calendars as calendars_repo
from ..store.accounts import Account
from . import errors
from . import queue as queue_sync
from . import sync as calendar_sync
from .google import GoogleCalendar
from .graph import GraphCalendar
from .http import Http, bearer_for

# One line per provider, which is CONVENTIONS.txt §4's promise about adding
# one. A provider absent from here has no calendar API as far as corMani is
# concerned, and says so rather than failing.
CLIENTS: dict = {GoogleCalendar.name: GoogleCalendar,
                 GraphCalendar.name: GraphCalendar}

_BACKOFF_BASE = 60
_BACKOFF_CAP = 3600
_RATE_LIMIT_MIN = 15 * 60
_RATE_LIMIT_CAP = 6 * 3600
_NEEDS_ATTENTION = 24 * 3600


@dataclass
class Options:
    months_back: int = calendar_sync.MONTHS_BACK
    months_ahead: int = calendar_sync.MONTHS_AHEAD
    timeout: float = 30.0


@dataclass
class AccountResult:
    account_id: int
    address: str
    ok: bool = False
    calendars: int = 0
    changed: int = 0
    removed: int = 0
    sent: int = 0
    dropped: int = 0
    stuck: int = 0
    conflicts: int = 0
    error: str = ""
    retry_at: str = ""
    unsupported: bool = False
    notes: list = field(default_factory=list)

    @property
    def moved(self) -> int:
        return self.changed + self.removed + self.sent


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Engine:
    """One account at a time, with the state that decides which one."""

    def __init__(self, con: sqlite3.Connection, *, options: Options | None = None,
                 client: Callable | None = None,
                 resolve: Callable = auth.resolve,
                 clock: Callable = _now) -> None:
        self.con = con
        self.options = options or Options()
        self._make_client = client or self._open
        self._resolve = resolve
        self._clock = clock

    # ------------------------------------------------------------ scheduling
    def due(self, *, now: dt.datetime | None = None) -> list:
        """Accounts whose calendars may be contacted.

        An account is due when ANY of its calendars is: the alternative is an
        account that stops being visited because one shared holiday feed is
        parked for a day.
        """
        stamp = (now or self._clock()).isoformat()
        parked = accounts_repo.calendar_state(self.con)
        out = []
        for account in accounts_repo.list_accounts(self.con):
            if not account.enabled or account.provider not in CLIENTS:
                continue
            until = (parked.get(account.id) or {}).get("until")
            if until and until > stamp:
                continue
            known = calendars_repo.list_calendars(self.con, account.id)
            if not known or calendars_repo.due(self.con, account.id, stamp=stamp):
                out.append(account)
        return out

    # ---------------------------------------------------------------- running
    def sync_all(self, *, progress: Callable | None = None,
                 accounts: list | None = None) -> list:
        results = []
        for account in (accounts if accounts is not None else self.due()):
            if progress:
                progress("account:start", {"address": account.address,
                                           "account_id": account.id})
            result = self.sync_account(account, progress=progress)
            results.append(result)
            if progress:
                progress("account:done", {"address": account.address,
                                          "result": result})
        return results

    def sync_account(self, account: Account, *,
                     progress: Callable | None = None) -> AccountResult:
        """One account's calendars, end to end. Never raises."""
        result = AccountResult(account_id=account.id, address=account.address)
        if account.provider not in CLIENTS:
            result.unsupported = True
            result.ok = True
            result.notes.append(
                f"{account.address}: this account's provider has no calendar "
                f"API, so there is nothing to sync")
            return result
        try:
            client = self._make_client(account)
            _, gone = calendar_sync.sync_calendar_list(self.con, client,
                                                       account.id)
            if gone:
                result.notes.append(
                    f"{len(gone)} calendar(s) are no longer listed and have "
                    f"been hidden, not deleted")

            drained = queue_sync.drain(self.con, client, account.id)
            result.sent, result.dropped = drained.sent, drained.dropped
            result.stuck, result.conflicts = drained.stuck, drained.conflicts
            result.notes.extend(drained.errors)

            window = calendar_sync.window_for(
                self._clock().astimezone(), back=self.options.months_back,
                ahead=self.options.months_ahead)
            for calendar in calendars_repo.due(self.con, account.id):
                self._one(client, calendar, window, result, progress)
            result.calendars = len(calendars_repo.list_calendars(self.con,
                                                                 account.id))
            result.ok = not result.error
            accounts_repo.record_calendar_success(self.con, account.id)
        except NotConfigured as exc:
            self._park(account, result, errors.describe(exc), _NEEDS_ATTENTION)
        except errors.AuthFailed as exc:
            self._park(account, result, errors.describe(exc), _NEEDS_ATTENTION)
        except errors.NotAuthorised as exc:
            self._park(account, result, errors.describe(exc), _NEEDS_ATTENTION)
        except errors.RateLimited as exc:
            self._park(account, result, errors.describe(exc),
                       self._rate_wait(account, exc.retry_after))
        except errors.Permanent as exc:
            self._park(account, result, errors.describe(exc), _NEEDS_ATTENTION)
        except (errors.CalendarError, sqlite3.Error, OSError) as exc:
            self._park(account, result, errors.describe(exc),
                       self._backoff_wait(account))
        return result

    def _one(self, client, calendar, window, result: AccountResult,
             progress: Callable | None) -> None:
        """One calendar, with its own failure kept to itself.

        A holiday feed that 404s must not stop the calendar the user actually
        works from, which is the same judgement `imap/sync.py` makes about a
        folder that has gone.
        """
        try:
            report = calendar_sync.sync_calendar(self.con, client, calendar,
                                                 window=window)
        except (errors.AuthFailed, errors.NotAuthorised, errors.RateLimited):
            raise                                # the account's, not this one's
        except errors.CalendarError as exc:
            message = errors.describe(exc)
            wait = (_NEEDS_ATTENTION if isinstance(exc, errors.Permanent)
                    else self._calendar_backoff(calendar))
            calendars_repo.record_failure(self.con, calendar.id, message,
                                          self._when(wait))
            result.notes.append(f"{calendar.label}: {message}")
            return
        calendars_repo.record_success(self.con, calendar.id)
        result.changed += report.changed
        result.removed += report.removed
        result.notes.extend(report.notes)
        if progress:
            progress("calendar:done", {"calendar": calendar.label,
                                       "report": report})

    # ---------------------------------------------------------------- helpers
    def _open(self, account: Account):
        """The real client for an account. Injected in tests, never mocked."""
        client = CLIENTS[account.provider]
        http = Http(bearer_for(account.address, account.provider,
                               resolve=self._resolve),
                    timeout=self.options.timeout)
        return client(http, address=account.address)

    def _when(self, seconds: float) -> str:
        return (self._clock() + dt.timedelta(seconds=seconds)).replace(
            microsecond=0).isoformat()

    def _park(self, account: Account, result: AccountResult, message: str,
              seconds: float) -> None:
        """An account-level refusal, on the account's own calendar columns.

        NOT on `account.next_attempt_at`, which is the mail engine's, and not
        spread across the calendar rows either: the first thing a sync does is
        ask for the list of calendars, so the account that most needs parking
        is the one with no calendar rows to park.
        """
        result.error = message
        result.retry_at = self._when(seconds)
        accounts_repo.record_calendar_failure(self.con, account.id, message,
                                              result.retry_at)

    def _failures(self, account: Account) -> int:
        state = accounts_repo.calendar_state(self.con).get(account.id) or {}
        return int(state.get("failures") or 0)

    def _backoff_wait(self, account: Account) -> float:
        return min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** self._failures(account)))

    def _calendar_backoff(self, calendar) -> float:
        return min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** calendar.sync_failures))

    def _rate_wait(self, account: Account, retry_after: float | None) -> float:
        if retry_after:
            return max(float(retry_after), 60.0)
        return min(_RATE_LIMIT_CAP,
                   max(_RATE_LIMIT_MIN,
                       _RATE_LIMIT_MIN * (2 ** self._failures(account))))


def sync_once(database_path, *, options: Options | None = None,
              progress: Callable | None = None) -> list:
    """Every due account's calendars, over a connection of this call's own.

    The connection is opened here for the reason `imap/engine.sync_once` gives:
    a worker thread must not share the interface's sqlite handle, and WAL is
    what lets the two coexist.
    """
    from ..store.database import connect

    con = connect(database_path)
    try:
        return Engine(con, options=options).sync_all(progress=progress)
    finally:
        con.close()
