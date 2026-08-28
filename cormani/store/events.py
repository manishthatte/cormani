# SPDX-License-Identifier: GPL-3.0-or-later
#
# Events: what the provider says is in a calendar, and what the user has done
# to it since.
#
# A row here is one INSTANCE, never a rule. Migration 6 argues that at length:
# the providers expand recurrence for a window and corMani does not implement
# RRULE, so "the reading group, every second Tuesday" is not in this table and
# "the reading group, on 12 September" is.
#
# AN ALL-DAY EVENT IS NOT AN INSTANT AND MUST NOT BE FILTERED AS ONE. This is
# the defect this module exists to make impossible. `starts_at` holds a UTC
# timestamp for a timed event and a plain YYYY-MM-DD for an all-day one, and
# because ISO-8601 sorts lexically the two comparisons look interchangeable —
# '2026-09-12' really is less than '2026-09-12T10:00:00+00:00'. They are not
# interchangeable. This machine is at UTC+05:30, so the local day of the 12th
# runs from 18:30Z on the 11th to 18:30Z on the 12th; an all-day event on the
# ELEVENTH ends at '2026-09-12', which is greater than '2026-09-11T18:30:00Z',
# so a single-comparison query draws Diwali on both days. Every range query
# below therefore carries four bounds — two instants for timed rows and two
# plain dates for all-day ones — and `store/times.window` is the one place they
# are derived.
#
# A CANCELLED INSTANCE IS DELETED, NOT KEPT GREY. It is what an incremental
# sync means by it: Google returns status `cancelled` for an instance that no
# longer exists, and Graph reports it as a removal. Keeping the row would mean
# a week grid that fills up with meetings that are not happening. The user is
# told a meeting was cancelled by the mail that says so — the organiser's
# iTIP CANCEL, which `calendar/itip.py` reads — and that is a message with
# words in it rather than a grey box.
#
# THE STORE NEVER IMPORTS THE PROTOCOL. `upsert` takes a mapping of plain
# values, not a provider's object and not `calendar/model.py`'s: the two
# provider modules translate into it, and this module could not name Google if
# it wanted to.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from . import times
from .calendars import RESPONSE_NEEDS_ACTION

STATUS_CONFIRMED = "confirmed"
STATUS_TENTATIVE = "tentative"
STATUS_CANCELLED = "cancelled"

# An event created here and not yet accepted by a provider. The prefix is one
# no provider can produce, exactly as `folders.LOCAL_PREFIX` is a path no IMAP
# server can produce, and for the same reason: the row has to be addressable
# and unique before the server has said anything about it.
LOCAL_PREFIX = "local:"

# The columns `upsert` will take from a mapping. Anything else in it is
# ignored rather than refused — a provider module that learns a new field
# should not have to wait for this list, and a typo here would be silent if
# unknown keys were an error only in the other direction.
_REMOTE_FIELDS = (
    "series_id", "ical_uid", "etag", "summary", "description", "location",
    "starts_at", "ends_at", "all_day", "status", "busy", "organiser_name",
    "organiser_addr", "my_response", "web_link", "recurring", "reminder",
    "updated_at",
)
_FLAGS = ("all_day", "busy", "recurring")


def is_local(remote_id: str) -> bool:
    return (remote_id or "").startswith(LOCAL_PREFIX)


def new_local_id() -> str:
    return f"{LOCAL_PREFIX}{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Attendee:
    name: str
    address: str
    response: str = RESPONSE_NEEDS_ACTION
    is_organiser: bool = False
    is_self: bool = False
    optional: bool = False

    @property
    def label(self) -> str:
        return self.name or self.address


@dataclass(frozen=True)
class Event:
    id: int
    calendar_id: int
    account_id: int
    remote_id: str
    series_id: str
    ical_uid: str
    etag: str
    summary: str
    description: str
    location: str
    starts_at: str
    ends_at: str
    all_day: bool
    status: str
    busy: bool
    organiser_name: str
    organiser_addr: str
    my_response: str
    web_link: str
    recurring: bool
    reminder: int | None
    updated_at: str
    pending: str
    attendees: tuple = field(default_factory=tuple)

    @property
    def title(self) -> str:
        """Never blank. Google and Graph both allow an event with no summary,
        and a week grid with an empty box in it is a defect to look at."""
        return self.summary or "(no subject)"

    @property
    def is_local(self) -> bool:
        return is_local(self.remote_id)

    @property
    def is_invitation(self) -> bool:
        """Somebody else's event that this account was asked to. The test is
        the organiser rather than the attendee list: an event the user made
        with guests also has attendees, and it is not an invitation."""
        return bool(self.organiser_addr) and not self.organiser_is_self

    @property
    def organiser_is_self(self) -> bool:
        return any(a.is_organiser and a.is_self for a in self.attendees)

    @property
    def needs_reply(self) -> bool:
        return (self.is_invitation
                and self.my_response in ("", RESPONSE_NEEDS_ACTION))

    @property
    def is_series_master(self) -> bool:
        """The recurring rule's own row, when the provider stores one."""
        return bool(self.series_id and self.remote_id == self.series_id)

    def start(self, tz: dt.tzinfo | None = None) -> dt.datetime | None:
        return times.parse(self.starts_at, tz)

    def end(self, tz: dt.tzinfo | None = None) -> dt.datetime | None:
        return times.parse(self.ends_at, tz)

    def start_date(self) -> dt.date | None:
        when = self.start()
        return when.date() if when else None

    def duration(self) -> dt.timedelta:
        first, last = self.start(), self.end()
        return (last - first) if (first and last) else dt.timedelta(0)

    def spans_days(self) -> bool:
        """Whether this needs drawing across more than one column.

        An all-day event over one day does not: its stored end is the NEXT
        date, because both providers make an all-day end exclusive, and a naive
        comparison of the two dates would call every one of them multi-day.
        """
        first, last = self.start(), self.end()
        if not (first and last):
            return False
        if self.all_day:
            return (last.date() - first.date()).days > 1
        return last.date() > first.date()


def _attendee(row: sqlite3.Row) -> Attendee:
    return Attendee(name=row["name"] or "", address=row["address"] or "",
                    response=row["response"] or RESPONSE_NEEDS_ACTION,
                    is_organiser=bool(row["is_organiser"]),
                    is_self=bool(row["is_self"]), optional=bool(row["optional"]))


def _event(row: sqlite3.Row, guests: tuple = ()) -> Event:
    return Event(
        id=int(row["id"]), calendar_id=int(row["calendar_id"]),
        account_id=int(row["account_id"]) if "account_id" in row.keys() else 0,
        remote_id=row["remote_id"], series_id=row["series_id"] or "",
        ical_uid=row["ical_uid"] or "", etag=row["etag"] or "",
        summary=row["summary"] or "", description=row["description"] or "",
        location=row["location"] or "", starts_at=row["starts_at"] or "",
        ends_at=row["ends_at"] or "", all_day=bool(row["all_day"]),
        status=row["status"] or STATUS_CONFIRMED, busy=bool(row["busy"]),
        organiser_name=row["organiser_name"] or "",
        organiser_addr=row["organiser_addr"] or "",
        my_response=row["my_response"] or "", web_link=row["web_link"] or "",
        recurring=bool(row["recurring"]), reminder=row["reminder"],
        updated_at=row["updated_at"] or "", pending=row["pending"] or "",
        attendees=guests)


def _sort_key(event: Event) -> tuple:
    """The order every view draws: all-day first, then by start, then by name.

    All-day first because it is what a day looks like — the whole-day facts at
    the top and then the hours — and because an all-day event has no time to
    sort by that would not be a fiction.
    """
    return (0 if event.all_day else 1, event.starts_at, event.title.lower(),
            event.id)


# ------------------------------------------------------------------ reading
def _guests(con: sqlite3.Connection, event_ids: Sequence[int]) -> dict:
    """Attendees for a set of events, in one query rather than one per row.

    A week of a busy calendar is sixty events; sixty queries to draw one screen
    is what makes a view feel slow, and the join is trivial.
    """
    if not event_ids:
        return {}
    marks = ",".join("?" * len(event_ids))
    out: dict = {}
    for row in con.execute(
            f"SELECT * FROM attendee WHERE event_id IN ({marks}) ORDER BY id",
            [int(e) for e in event_ids]).fetchall():
        out.setdefault(int(row["event_id"]), []).append(_attendee(row))
    return {k: tuple(v) for k, v in out.items()}


_SELECT = """
    SELECT e.*, c.account_id AS account_id
    FROM event e JOIN calendar c ON c.id = e.calendar_id
"""


def _collect(con: sqlite3.Connection, rows: Sequence[sqlite3.Row], *,
             with_attendees: bool) -> list[Event]:
    guests = _guests(con, [int(r["id"]) for r in rows]) if with_attendees else {}
    return sorted((_event(r, guests.get(int(r["id"]), ())) for r in rows),
                  key=_sort_key)


def events_between(con: sqlite3.Connection, start: dt.datetime,
                   end: dt.datetime, *, calendar_ids: Sequence[int] | None = None,
                   with_attendees: bool = False,
                   include_cancelled: bool = False) -> list[Event]:
    """Everything overlapping a window. The query every view makes.

    `start` and `end` are local, aware or not, and the window is half-open:
    an event ending exactly when the window begins is not in it, which is what
    stops yesterday's last meeting appearing at the top of today.

    The four bounds are the point — see the module header.
    """
    from_utc, to_utc, from_date, to_date = times.window(start, end)
    sql = _SELECT + """
        WHERE ((e.all_day = 0 AND e.starts_at < ? AND e.ends_at > ?)
            OR (e.all_day = 1 AND e.starts_at < ? AND e.ends_at > ?))
    """
    params: list = [to_utc, from_utc, to_date, from_date]
    if not include_cancelled:
        sql += " AND e.status <> ?"
        params.append(STATUS_CANCELLED)
    if calendar_ids is not None:
        if not calendar_ids:
            return []
        sql += f" AND e.calendar_id IN ({','.join('?' * len(calendar_ids))})"
        params.extend(int(c) for c in calendar_ids)
    return _collect(con, con.execute(sql, params).fetchall(),
                    with_attendees=with_attendees)


def events_on(con: sqlite3.Connection, day: dt.date, **kwargs) -> list[Event]:
    return events_between(con, *times.day_bounds(day), **kwargs)


def by_day(events: Sequence[Event], start: dt.datetime, end: dt.datetime, *,
           tz: dt.tzinfo | None = None) -> dict:
    """Events bucketed into the local days they touch, empty days included.

    Here rather than in a view because all three views need it and each would
    otherwise get the multi-day case subtly differently. An event appears in
    every day it covers, which is what a month grid draws; a view that wants it
    once takes the first bucket it is in.
    """
    days = times.days_between(start, end)
    out = {day: [] for day in days}
    tz = tz or times.aware(start).tzinfo
    for event in events:
        first, last = event.start(tz), event.end(tz)
        if not (first and last):
            continue
        first, last = first.astimezone(tz), last.astimezone(tz)
        first_day, last_day = first.date(), last.date()
        if last <= first or (event.all_day and last.time() == dt.time(0, 0)):
            # An exclusive end at midnight belongs to the previous day. Without
            # this a one-day all-day event is drawn on two.
            last_day -= dt.timedelta(days=1)
        elif last.time() == dt.time(0, 0):
            last_day -= dt.timedelta(days=1)
        for day in days:
            if first_day <= day <= max(first_day, last_day):
                out[day].append(event)
    return out


def get_event(con: sqlite3.Connection, event_id: int) -> Event | None:
    row = con.execute(_SELECT + " WHERE e.id = ?", (int(event_id),)).fetchone()
    if row is None:
        return None
    return _event(row, _guests(con, [int(row["id"])]).get(int(row["id"]), ()))


def by_remote(con: sqlite3.Connection, calendar_id: int,
              remote_id: str) -> Event | None:
    row = con.execute(_SELECT + " WHERE e.calendar_id = ? AND e.remote_id = ?",
                      (int(calendar_id), remote_id)).fetchone()
    return _event(row) if row else None


def by_ical_uid(con: sqlite3.Connection, ical_uid: str, *,
                account_id: int | None = None) -> list[Event]:
    """Every instance carrying an iCalendar UID. What an invitation joins on.

    A list rather than one row, and not only because of recurrence: the same
    meeting reaches two of this user's fifteen accounts often enough, and each
    account holds its own copy in its own calendar. An answer sent from one is
    not an answer from the other.
    """
    sql = _SELECT + " WHERE e.ical_uid = ? AND e.ical_uid <> ''"
    params: list = [ical_uid]
    if account_id is not None:
        sql += " AND c.account_id = ?"
        params.append(int(account_id))
    return _collect(con, con.execute(sql, params).fetchall(), with_attendees=True)


def upcoming(con: sqlite3.Connection, *, limit: int = 20,
             days: int = 14, calendar_ids: Sequence[int] | None = None,
             now: dt.datetime | None = None) -> list[Event]:
    """The next few, for the agenda pane. Anything still running counts.

    A meeting that started ten minutes ago is the one most likely to be looked
    up, so the window begins at the start of the current LOCAL DAY rather than
    at this instant, and the pane greys what has finished.
    """
    now = now or times.now_local()
    start, _ = times.day_bounds(now.date(), now.tzinfo)
    events = events_between(con, start, start + dt.timedelta(days=days),
                            calendar_ids=calendar_ids)
    return events[:limit]


def needing_reply(con: sqlite3.Connection, *, days: int = 60,
                  now: dt.datetime | None = None) -> list[Event]:
    """Invitations this user has not answered, in the near future.

    Past invitations are not asked about: an unanswered meeting from March is
    not a decision anybody is waiting for.
    """
    now = now or times.now_local()
    events = events_between(con, now, now + dt.timedelta(days=days),
                            with_attendees=True)
    return [e for e in events if e.needs_reply]


def counts_by_calendar(con: sqlite3.Connection) -> dict:
    return {int(r[0]): int(r[1]) for r in con.execute(
        "SELECT calendar_id, COUNT(*) FROM event GROUP BY calendar_id").fetchall()}


# ------------------------------------------------------- the sync's writers
def _values(fields: Mapping) -> dict:
    out = {}
    for name in _REMOTE_FIELDS:
        if name not in fields:
            continue
        value = fields[name]
        if name in _FLAGS:
            out[name] = 1 if value else 0
        elif name == "reminder":
            out[name] = None if value is None else int(value)
        else:
            out[name] = "" if value is None else str(value)
    return out


def upsert(con: sqlite3.Connection, calendar_id: int, remote_id: str,
           fields: Mapping, *, attendees: Sequence | None = None,
           commit: bool = True) -> int:
    """Write what the provider said about one instance. Returns its row id.

    Idempotent on (calendar_id, remote_id), which is what makes a re-fetch of
    a range the store already holds harmless — and that in turn is what lets
    `calendars.record_sync_state` be written after the events rather than
    before.
    """
    values = _values(fields)
    values.setdefault("starts_at", "")
    values.setdefault("ends_at", "")
    row = con.execute(
        "SELECT id FROM event WHERE calendar_id = ? AND remote_id = ?",
        (int(calendar_id), remote_id)).fetchone()
    if row is None:
        columns = ["calendar_id", "remote_id", *values]
        marks = ",".join("?" * len(columns))
        cur = con.execute(
            f"INSERT INTO event ({','.join(columns)}) VALUES ({marks})",
            [int(calendar_id), remote_id, *values.values()])
        event_id = int(cur.lastrowid)
    else:
        event_id = int(row[0])
        if values:
            sets = ",".join(f"{name} = ?" for name in values)
            con.execute(f"UPDATE event SET {sets} WHERE id = ?",
                        [*values.values(), event_id])
    if attendees is not None:
        set_attendees(con, event_id, attendees, commit=False)
    if commit:
        con.commit()
    return event_id


def set_attendees(con: sqlite3.Connection, event_id: int, attendees: Sequence,
                  *, commit: bool = True) -> int:
    """Replace an event's guest list. Wholesale, and deliberately.

    An attendee has no identity of its own worth preserving — a response
    changes, an address does not — and reconciling a list of five people by
    address costs more code than rewriting five rows.
    """
    con.execute("DELETE FROM attendee WHERE event_id = ?", (int(event_id),))
    written = 0
    for guest in attendees:
        if isinstance(guest, Mapping):
            guest = Attendee(name=str(guest.get("name", "")),
                             address=str(guest.get("address", "")),
                             response=str(guest.get("response",
                                                    RESPONSE_NEEDS_ACTION)),
                             is_organiser=bool(guest.get("is_organiser")),
                             is_self=bool(guest.get("is_self")),
                             optional=bool(guest.get("optional")))
        con.execute("""
            INSERT INTO attendee (event_id, name, address, response,
                is_organiser, is_self, optional)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (int(event_id), guest.name, guest.address, guest.response,
              1 if guest.is_organiser else 0, 1 if guest.is_self else 0,
              1 if guest.optional else 0))
        written += 1
    if commit:
        con.commit()
    return written


def forget_remote(con: sqlite3.Connection, calendar_id: int,
                  remote_ids: Sequence[str], *, commit: bool = True) -> int:
    """Remove instances the provider says are gone. Attendees cascade."""
    remote_ids = [str(r) for r in remote_ids]
    if not remote_ids:
        return 0
    marks = ",".join("?" * len(remote_ids))
    cur = con.execute(
        f"DELETE FROM event WHERE calendar_id = ? AND remote_id IN ({marks})",
        [int(calendar_id), *remote_ids])
    if commit:
        con.commit()
    return cur.rowcount


def prune_window(con: sqlite3.Connection, calendar_id: int, start: str,
                 end: str, keep: Sequence[str], *, commit: bool = True) -> int:
    """Delete instances inside a fetched window that the fetch did not mention.

    The only way a FULL pass learns about a deletion: a provider asked for a
    range answers with what is there, and says nothing about what is not. An
    incremental pass gets told, which is why this runs for the full one alone —
    running it after an incremental pass would delete the whole window.
    """
    keep_set = {str(k) for k in keep}
    gone = [row["remote_id"] for row in con.execute("""
        SELECT remote_id FROM event
        WHERE calendar_id = ?
          AND ((all_day = 0 AND starts_at >= ? AND starts_at < ?)
            OR (all_day = 1 AND starts_at >= ? AND starts_at < ?))
    """, (int(calendar_id), start, end, start[:10], end[:10])).fetchall()
        if row["remote_id"] not in keep_set and not is_local(row["remote_id"])]
    return forget_remote(con, calendar_id, gone, commit=commit)


def adopt_remote_id(con: sqlite3.Connection, event_id: int, remote_id: str,
                    *, commit: bool = True) -> None:
    """Give a locally created event the id the provider has just issued.

    The row keeps its identity — every view, every queue entry and every
    selection still points at the same `event.id` — and only the provider's
    name for it changes. Doing this as a delete and an insert would lose the
    selection the user is looking at and any op queued behind the create.
    """
    con.execute("UPDATE event SET remote_id = ? WHERE id = ?",
                (remote_id, int(event_id)))
    if commit:
        con.commit()


def set_pending(con: sqlite3.Connection, event_ids: Sequence[int], *,
                commit: bool = True) -> None:
    """Refresh `event.pending` for these rows from the queue.

    The marker, exactly as `pending.py._mark` is for a message: the queue is
    the truth and this is what a view can read without joining to it.
    """
    for event_id in {int(e) for e in event_ids}:
        kinds = sorted({r[0] for r in con.execute(
            "SELECT DISTINCT kind FROM event_op WHERE event_id = ?",
            (event_id,)).fetchall()})
        con.execute("UPDATE event SET pending = ? WHERE id = ?",
                    (",".join(kinds), event_id))
    if commit:
        con.commit()
