# SPDX-License-Identifier: GPL-3.0-or-later
#
# Bringing one calendar up to date.
#
# `imap/sync.py`'s job, over a different protocol and with one difference that
# decides the whole design: a mail folder is a set the client can hold ALL of,
# and a calendar is not. A calendar is a function of time — the provider will
# expand a rule into instances for any window it is asked about, forever in
# both directions — so a sync of one has to choose a window, and every
# consequence below follows from that choice.
#
# THE WINDOW IS QUANTISED TO MONTHS, AND THAT IS WHAT MAKES INCREMENTAL SYNC
# POSSIBLE AT ALL. A provider's bookmark remembers the parameters of the
# request that issued it: Google's syncToken answers "what changed" only within
# the timeMin/timeMax it was born with, and Graph's deltaLink has the window
# baked into the URL. A window computed as "ninety days either side of now"
# moves every time the clock ticks, so the bookmark would be wrong the moment
# it was written and a client that kept using it would silently miss
# everything created outside yesterday's edges. Anchored to the first of the
# month, the window is stable for weeks at a time, and when it does move the
# answer is a full pass — once a month, not once a sync.
#
# A FULL PASS LEARNS ABOUT DELETIONS BY SUBTRACTION AND AN INCREMENTAL ONE IS
# TOLD. `events.prune_window` is the subtraction and it runs after a full pass
# ONLY: running it after an incremental pass would delete the entire window,
# because an incremental answer is a handful of changes rather than the whole
# truth. Getting this backwards empties a calendar, which is why the two paths
# below never share the line.
#
# AN EXPIRED BOOKMARK IS NOT AN ERROR AND IS NEVER REPORTED. Both providers
# invalidate one after some weeks; `errors.TokenExpired` is caught here, the
# token is thrown away, and the same call falls through to a full pass. The
# user sees a sync that took longer, which is the honest cost.
#
# THE STATE IS WRITTEN AFTER THE EVENTS. Same rule as the mail side: a bookmark
# that claims more than the store holds skips instances permanently, while one
# that claims less re-fetches a few, and the upsert makes that harmless.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field

from ..store import calendars as calendars_repo
from ..store import events as events_repo
from ..store import times
from ..store.calendars import Calendar
from ..store.database import utc_now
from . import errors

# How much of the past and the future the store holds, in whole months. Three
# back answers "what did I do last quarter" and twelve forward covers an annual
# fixture booked a year out, which is the longest lead time that turns up in
# practice. Both are counted from the first of the CURRENT month — see the
# module header for why they are not counted from today.
MONTHS_BACK = 3
MONTHS_AHEAD = 12

# A guard, not a policy: a provider answering a window with a million
# instances is a provider that has misunderstood the question, and a client
# that keeps asking for the next page is one that never returns.
MAX_PAGES = 200


@dataclass
class CalendarReport:
    calendar: str = ""
    calendar_id: int = 0
    full: bool = False
    changed: int = 0
    removed: int = 0
    pages: int = 0
    error: str = ""
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


def _add_months(day: dt.date, months: int) -> dt.date:
    total = day.year * 12 + (day.month - 1) + months
    return dt.date(total // 12, total % 12 + 1, 1)


def window_for(now: dt.datetime | None = None, *, back: int = MONTHS_BACK,
               ahead: int = MONTHS_AHEAD) -> tuple:
    """The half-open window this store keeps, as two UTC instants.

    Anchored to the first of the month in the LOCAL zone, because the window a
    person means by "this year" is a local one, and because anchoring to UTC
    would make the window move for half the world on the last day of every
    month.
    """
    now = now or times.now_local()
    first = times.month_start(times.aware(now).date())
    start = dt.datetime.combine(_add_months(first, -back), dt.time(0, 0),
                                times.aware(now).tzinfo)
    end = dt.datetime.combine(_add_months(first, ahead), dt.time(0, 0),
                              times.aware(now).tzinfo)
    return times.to_utc_text(start), times.to_utc_text(end)


# ------------------------------------------------------------- calendars
def sync_calendar_list(con: sqlite3.Connection, client,
                       account_id: int) -> tuple:
    """Reconcile the account's calendars with what the provider lists.

    Returns (the local ids in the provider's order, the ones that have gone).
    A calendar that has gone is MARKED, never deleted — `store/calendars.py`
    says why, and it is the same instinct as an unsubscribed folder.
    """
    ids, seen = [], []
    for remote, default_reminder in client.calendars():
        if not remote.remote_id:
            continue                                         # pragma: no cover
        calendar_id = calendars_repo.ensure_calendar(
            con, account_id, remote.remote_id, name=remote.name,
            description=remote.description, colour=remote.colour,
            timezone=remote.timezone, is_primary=remote.is_primary,
            writable=remote.writable, default_reminder=default_reminder,
            commit=False)
        ids.append(calendar_id)
        seen.append(remote.remote_id)
    gone = calendars_repo.mark_absent(con, account_id, seen, commit=False)
    con.commit()
    return ids, gone


# ---------------------------------------------------------------- events
def sync_calendar(con: sqlite3.Connection, client, calendar: Calendar, *,
                  now: dt.datetime | None = None,
                  window: tuple | None = None) -> CalendarReport:
    """One calendar, over the window this store keeps."""
    report = CalendarReport(calendar=calendar.label, calendar_id=calendar.id)
    start, end = window or window_for(now)
    incremental = bool(calendar.sync_token
                       and calendar.synced_from == start
                       and calendar.synced_to == end)
    try:
        if incremental:
            try:
                _incremental(con, client, calendar, report)
            except errors.TokenExpired:
                # Not an error, and not reported. The bookmark is stale; the
                # window is still the right question to ask.
                report.notes.append(
                    f"{calendar.label}: the provider's sync bookmark expired, "
                    f"so the window was fetched again")
                calendars_repo.record_sync_state(con, calendar.id,
                                                 sync_token="", commit=False)
                _full(con, client, calendar, start, end, report)
        else:
            _full(con, client, calendar, start, end, report)
    except errors.CalendarError as exc:
        report.error = errors.describe(exc)
        calendars_repo.touch(con, calendar.id, error=report.error)
        raise
    calendars_repo.touch(con, calendar.id)
    return report


def _full(con: sqlite3.Connection, client, calendar: Calendar, start: str,
          end: str, report: CalendarReport) -> None:
    """Everything in the window, and then everything else in it deleted."""
    report.full = True
    seen, token, sync_token = [], "", ""
    for _ in range(MAX_PAGES):
        page = client.events(calendar.remote_id, start=start, end=end,
                             page_token=token)
        report.pages += 1
        for remote in page.events:
            if remote.deleted:
                continue           # a full pass asks what IS; prune does the rest
            seen.append(remote.remote_id)
            _write(con, calendar, remote)
            report.changed += 1
        sync_token = page.sync_token or sync_token
        token = page.next_token
        if not token:
            break
    else:                                                    # pragma: no cover
        report.notes.append(
            f"{calendar.label}: stopped after {MAX_PAGES} pages; the window "
            f"holds more instances than corMani will fetch at once")

    report.removed = events_repo.prune_window(con, calendar.id, start, end,
                                              keep=seen, commit=False)
    # The window and the bookmark, together and after the events.
    calendars_repo.record_sync_state(
        con, calendar.id, sync_token=sync_token, synced_from=start,
        synced_to=end, last_synced_at=utc_now(), commit=False)
    con.commit()


def _incremental(con: sqlite3.Connection, client, calendar: Calendar,
                 report: CalendarReport) -> None:
    """What changed since the bookmark. Deletions arrive as deletions."""
    token, sync_token = "", ""
    for _ in range(MAX_PAGES):
        # BOTH, on every page after the first. Google requires that a
        # continuation repeat every parameter of the request it continues, and
        # the bookmark is one of them; Graph ignores the second because its
        # page link already carries the state.
        page = client.events(calendar.remote_id,
                             sync_token=calendar.sync_token, page_token=token)
        report.pages += 1
        for remote in page.events:
            if remote.deleted:
                report.removed += events_repo.forget_remote(
                    con, calendar.id, [remote.remote_id], commit=False)
            else:
                _write(con, calendar, remote)
                report.changed += 1
        sync_token = page.sync_token or sync_token
        token = page.next_token
        if not token:
            break
    else:                                                    # pragma: no cover
        report.notes.append(f"{calendar.label}: stopped after {MAX_PAGES} pages")
    calendars_repo.record_sync_state(con, calendar.id, sync_token=sync_token,
                                     last_synced_at=utc_now(), commit=False)
    con.commit()


def _write(con: sqlite3.Connection, calendar: Calendar, remote) -> int:
    """One instance into the store. The only place a synced event is written.

    A LOCAL EVENT IS NEVER OVERWRITTEN BY ITS OWN CREATE COMING BACK: the
    provider's copy arrives under the provider's id, and the local row still
    carries `local:`, so this would make a SECOND row for the same event. The
    queue is what reconciles them — it renames the local row the moment the
    create is accepted — and the only case left here is a create that has not
    been sent yet, which the provider cannot possibly be reporting.
    """
    return events_repo.upsert(con, calendar.id, remote.remote_id,
                              remote.fields(), attendees=remote.guests(),
                              commit=False)


def fetch_range(con: sqlite3.Connection, client, calendar: Calendar,
                start: str, end: str) -> CalendarReport:
    """One range outside the window, fetched for a view that asked to see it.

    Deliberately does NOT touch `sync_token`, `synced_from` or `synced_to`.
    The store now holds these instances, and it does not hold them
    INCREMENTALLY — nothing here can promise to notice when one of them
    changes, and a window that claimed otherwise would be the quiet kind of
    wrong. `Calendar.covers` goes on saying no, and the view goes on fetching
    when the user pages back into 2019.
    """
    report = CalendarReport(calendar=calendar.label, calendar_id=calendar.id,
                            full=True)
    token = ""
    for _ in range(MAX_PAGES):
        page = client.events(calendar.remote_id, start=start, end=end,
                             page_token=token)
        report.pages += 1
        for remote in page.events:
            if not remote.deleted:
                _write(con, calendar, remote)
                report.changed += 1
        token = page.next_token
        if not token:
            break
    con.commit()
    return report
