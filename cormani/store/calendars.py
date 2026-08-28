# SPDX-License-Identifier: GPL-3.0-or-later
#
# Calendars: the ones the provider has, and what the user has decided about them.
#
# The same shape as `folders.py`, for the same reason: a remote list arrives on
# every sync, the local row carries state the remote list does not know about,
# and reconciling the two must never lose the second. Everything here is
# idempotent on (account_id, remote_id).
#
# TWO COLOURS AND TWO VISIBILITIES, AND EACH PAIR IS ONE PROVIDER'S FACT BESIDE
# ONE USER'S CHOICE. `colour` is what the provider draws the calendar in and is
# overwritten by every sync; `user_colour` is what the person sitting here
# chose and is never touched by one. `present` says the provider still lists
# it; `shown` says the user wants to see it. Merging either pair would mean a
# sync silently undoing a decision, which is the failure that makes people stop
# trusting a setting.
#
# A CALENDAR THAT LEAVES THE PROVIDER IS MARKED ABSENT, NOT DELETED. Deleting
# cascades to every event it holds, and the local store is the user's record of
# where they were last March. The same instinct as `folders.py`'s unsubscribe,
# and the same one as delete-means-Trash.
#
# THE WINDOW IS PART OF THE STATE, NOT AN IMPLEMENTATION DETAIL. Migration 6
# says why the store holds instances rather than rules: the provider expands
# recurrence and corMani does not implement RRULE. The consequence is that
# `synced_from` and `synced_to` are a promise about what a range query can be
# trusted to answer, and `covers` is how a view asks before it draws.
#
# THE WINDOW AND THE SYNC TOKEN MOVE TOGETHER, ALWAYS. A provider's bookmark
# remembers the parameters of the request that produced it — ask Google with a
# syncToken and you are asking about THAT window — so a store whose window said
# more than its token covers would report an empty answer as "nothing changed".
# `calendar/sync.py` writes the pair in one call for that reason, and there is
# deliberately no function here that widens one alone.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence

from .database import utc_now

# A response, as this store spells it. The providers spell them differently and
# each provider module translates; nothing above this line ever sees theirs.
RESPONSE_NEEDS_ACTION = "needsAction"
RESPONSE_ACCEPTED = "accepted"
RESPONSE_TENTATIVE = "tentative"
RESPONSE_DECLINED = "declined"
RESPONSES = (RESPONSE_NEEDS_ACTION, RESPONSE_ACCEPTED, RESPONSE_TENTATIVE,
             RESPONSE_DECLINED)

RESPONSE_LABELS = {
    RESPONSE_NEEDS_ACTION: "No reply",
    RESPONSE_ACCEPTED: "Accepted",
    RESPONSE_TENTATIVE: "Tentative",
    RESPONSE_DECLINED: "Declined",
}


@dataclass(frozen=True)
class Calendar:
    id: int
    account_id: int
    remote_id: str
    name: str
    description: str
    colour: str
    user_colour: str
    timezone: str
    is_primary: bool
    writable: bool
    shown: bool
    present: bool
    sync_token: str
    synced_from: str
    synced_to: str
    last_synced_at: str
    last_error: str
    default_reminder: int | None
    sync_failures: int = 0
    next_attempt_at: str = ""

    @property
    def label(self) -> str:
        """What a person reads. Never blank: a calendar with no name is drawn
        as its remote id, which for Google is the address it belongs to and is
        therefore better than an empty chip."""
        return self.name or self.remote_id

    @property
    def display_colour(self) -> str:
        """The user's choice if they made one, otherwise the provider's."""
        return self.user_colour or self.colour

    def covers(self, start: str, end: str) -> bool:
        """Whether a range query over this calendar can be trusted.

        False is not an error — it is the honest answer that the store holds a
        window and this view is outside it, which is what makes the interface
        able to say "fetching" rather than draw an empty week that is a lie.
        """
        if not (self.synced_from and self.synced_to):
            return False
        return self.synced_from <= start and end <= self.synced_to


def _calendar(row: sqlite3.Row) -> Calendar:
    return Calendar(
        id=int(row["id"]), account_id=int(row["account_id"]),
        remote_id=row["remote_id"], name=row["name"] or "",
        description=row["description"] or "", colour=row["colour"] or "",
        user_colour=row["user_colour"] or "", timezone=row["timezone"] or "",
        is_primary=bool(row["is_primary"]), writable=bool(row["writable"]),
        shown=bool(row["shown"]), present=bool(row["present"]),
        sync_token=row["sync_token"] or "", synced_from=row["synced_from"] or "",
        synced_to=row["synced_to"] or "",
        last_synced_at=row["last_synced_at"] or "",
        last_error=row["last_error"] or "",
        default_reminder=row["default_reminder"],
        sync_failures=int(row["sync_failures"] or 0),
        next_attempt_at=row["next_attempt_at"] or "")


def _sort_key(calendar: Calendar) -> tuple:
    """The primary calendar first, then by name.

    A judgement, like `folders.py`'s role order: the calendar an account's own
    invitations land in is the one asked for most, and alphabetical order puts
    "Birthdays" above it.
    """
    return (0 if calendar.is_primary else 1, calendar.label.lower(), calendar.id)


# ------------------------------------------------------------------ reading
def list_calendars(con: sqlite3.Connection, account_id: int | None = None, *,
                   shown_only: bool = False,
                   include_absent: bool = False) -> list[Calendar]:
    """Calendars, in the order the rail draws them.

    `account_id` of None means every account, which is what the calendar view
    wants: unlike a mail folder, a calendar is read across accounts by default
    — the whole point of the week grid is that Monday holds everything.
    """
    where, params = [], []
    if account_id is not None:
        where.append("account_id = ?")
        params.append(int(account_id))
    if shown_only:
        where.append("shown = 1")
    if not include_absent:
        where.append("present = 1")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = con.execute(f"SELECT * FROM calendar {clause}", params).fetchall()
    return sorted((_calendar(r) for r in rows), key=_sort_key)


def get_calendar(con: sqlite3.Connection, calendar_id: int) -> Calendar | None:
    row = con.execute("SELECT * FROM calendar WHERE id = ?",
                      (int(calendar_id),)).fetchone()
    return _calendar(row) if row else None


def by_remote(con: sqlite3.Connection, account_id: int,
              remote_id: str) -> Calendar | None:
    row = con.execute(
        "SELECT * FROM calendar WHERE account_id = ? AND remote_id = ?",
        (int(account_id), remote_id)).fetchone()
    return _calendar(row) if row else None


def primary(con: sqlite3.Connection, account_id: int) -> Calendar | None:
    """The calendar a new event goes in when the user has not said.

    Falls back to the first writable one: an account whose provider declares no
    primary calendar still has to be able to accept an event, and refusing to
    create one because a flag was missing is the wrong kind of correctness.
    """
    for calendar in list_calendars(con, account_id):
        if calendar.is_primary and calendar.writable:
            return calendar
    for calendar in list_calendars(con, account_id):
        if calendar.writable:
            return calendar
    return None


def shown_ids(con: sqlite3.Connection, *,
              account_ids: Sequence[int] | None = None) -> list[int]:
    """The calendars a view should draw. The range query's input."""
    sql = "SELECT id FROM calendar WHERE shown = 1 AND present = 1"
    params: list = []
    if account_ids is not None:
        if not account_ids:
            return []
        sql += f" AND account_id IN ({','.join('?' * len(account_ids))})"
        params.extend(int(a) for a in account_ids)
    return [int(r[0]) for r in con.execute(sql, params).fetchall()]


# ------------------------------------------------------- the sync's writers
def ensure_calendar(con: sqlite3.Connection, account_id: int, remote_id: str, *,
                    name: str = "", description: str | None = None,
                    colour: str | None = None, timezone: str | None = None,
                    is_primary: bool | None = None,
                    writable: bool | None = None,
                    default_reminder: int | None = None,
                    commit: bool = True) -> int:
    """Create the calendar if this account has not seen it, and return its id.

    Idempotent on the schema's unique key. A calendar that was marked absent
    and has come back is marked present again here rather than duplicated —
    which is what happens when a shared calendar is unshared and re-shared, and
    the events under it are still the right ones.
    """
    row = con.execute(
        "SELECT id FROM calendar WHERE account_id = ? AND remote_id = ?",
        (int(account_id), remote_id)).fetchone()
    if row is None:
        cur = con.execute("""
            INSERT INTO calendar (account_id, remote_id, name, description,
                colour, timezone, is_primary, writable, default_reminder)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (int(account_id), remote_id, name, description or "", colour or "",
              timezone or "", 1 if is_primary else 0,
              0 if writable is False else 1,
              None if default_reminder is None else int(default_reminder)))
        if commit:
            con.commit()
        return int(cur.lastrowid)

    calendar_id = int(row[0])
    update_calendar(con, calendar_id, name=name or None, description=description,
                    colour=colour, timezone=timezone, is_primary=is_primary,
                    writable=writable, default_reminder=default_reminder,
                    present=True, commit=commit)
    return calendar_id


def update_calendar(con: sqlite3.Connection, calendar_id: int, *,
                    name: str | None = None, description: str | None = None,
                    colour: str | None = None, timezone: str | None = None,
                    is_primary: bool | None = None, writable: bool | None = None,
                    present: bool | None = None,
                    default_reminder: int | None = None,
                    commit: bool = True) -> bool:
    """What the provider says about a calendar. None means "leave it alone".

    `user_colour` and `shown` are deliberately not among these: they are the
    user's, and this is the writer the sync uses.
    """
    sets, params = [], []
    for column, value in (("name", name), ("description", description),
                          ("colour", colour), ("timezone", timezone)):
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    for column, flag in (("is_primary", is_primary), ("writable", writable),
                         ("present", present)):
        if flag is not None:
            sets.append(f"{column} = ?")
            params.append(1 if flag else 0)
    if default_reminder is not None:
        sets.append("default_reminder = ?")
        params.append(int(default_reminder))
    if not sets:
        return False
    params.append(int(calendar_id))
    cur = con.execute(f"UPDATE calendar SET {', '.join(sets)} WHERE id = ?", params)
    if commit:
        con.commit()
    return cur.rowcount > 0


def mark_absent(con: sqlite3.Connection, account_id: int,
                keep_remote_ids: Sequence[str], *, commit: bool = True) -> list[str]:
    """Calendars this account no longer lists. Returns what was marked.

    Marked, not deleted — see the module header. Returned rather than only
    counted because the interface says which ones, and a number alone reads as
    data loss.
    """
    keep = {str(r) for r in keep_remote_ids}
    gone = []
    for row in con.execute(
            "SELECT id, remote_id FROM calendar WHERE account_id = ? AND present = 1",
            (int(account_id),)).fetchall():
        if row["remote_id"] not in keep:
            con.execute("UPDATE calendar SET present = 0 WHERE id = ?", (row["id"],))
            gone.append(row["remote_id"])
    if commit:
        con.commit()
    return gone


def record_sync_state(con: sqlite3.Connection, calendar_id: int, *,
                      sync_token: str | None = None,
                      synced_from: str | None = None,
                      synced_to: str | None = None,
                      last_synced_at: str | None = None,
                      last_error: str | None = None,
                      commit: bool = True) -> None:
    """Where the sync got to. Written AFTER the events, never before.

    The same order as `folders.record_sync_state` and for the same reason: a
    calendar whose bookmark says less than it holds re-fetches a few instances
    the store already has, which the upsert makes harmless. The other order
    skips them permanently.

    `sync_token` accepts the empty string as a MEANING — the provider has
    expired the token and the next pass must be a full one — which is why it is
    checked against None rather than falsiness.
    """
    sets, params = [], []
    for column, value in (("sync_token", sync_token),
                          ("synced_from", synced_from),
                          ("synced_to", synced_to),
                          ("last_synced_at", last_synced_at),
                          ("last_error", last_error)):
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    if not sets:
        return
    params.append(int(calendar_id))
    con.execute(f"UPDATE calendar SET {', '.join(sets)} WHERE id = ?", params)
    if commit:
        con.commit()


# ------------------------------------------------------------ the user's own
def set_shown(con: sqlite3.Connection, calendar_id: int, shown: bool, *,
              commit: bool = True) -> None:
    con.execute("UPDATE calendar SET shown = ? WHERE id = ?",
                (1 if shown else 0, int(calendar_id)))
    if commit:
        con.commit()


def set_user_colour(con: sqlite3.Connection, calendar_id: int, colour: str, *,
                    commit: bool = True) -> None:
    """The user's own colour for a calendar. Empty gives the provider's back."""
    con.execute("UPDATE calendar SET user_colour = ? WHERE id = ?",
                (colour or "", int(calendar_id)))
    if commit:
        con.commit()


def due(con: sqlite3.Connection, account_id: int, *,
        stamp: str = "") -> list[Calendar]:
    """This account's calendars that may be contacted now.

    `shown` is deliberately not consulted, for the same reason the mail
    engine ignores `hidden`: a calendar the user has un-ticked is out of the
    VIEW and still in the store, and one that stopped syncing while un-ticked
    would be a month stale the moment they ticked it again.
    """
    stamp = stamp or utc_now()
    return [c for c in list_calendars(con, account_id)
            if not c.next_attempt_at or c.next_attempt_at <= stamp]


def record_failure(con: sqlite3.Connection, calendar_id: int, error: str,
                   retry_at: str, *, commit: bool = True) -> None:
    """Park one calendar, and say why. The counter is the back-off's exponent."""
    con.execute(
        "UPDATE calendar SET last_error = ?, sync_failures = sync_failures + 1, "
        "next_attempt_at = ? WHERE id = ?", (error[:300], retry_at,
                                             int(calendar_id)))
    if commit:
        con.commit()


def record_success(con: sqlite3.Connection, calendar_id: int, *,
                   commit: bool = True) -> None:
    con.execute(
        "UPDATE calendar SET last_error = '', sync_failures = 0, "
        "next_attempt_at = NULL, last_synced_at = ? WHERE id = ?",
        (utc_now(), int(calendar_id)))
    if commit:
        con.commit()


def clear_backoff(con: sqlite3.Connection, account_id: int, *,
                  commit: bool = True) -> int:
    """Try this account's calendars again now. What a sign-in calls."""
    cur = con.execute(
        "UPDATE calendar SET sync_failures = 0, next_attempt_at = NULL, "
        "last_error = '' WHERE account_id = ?", (int(account_id),))
    if commit:
        con.commit()
    return cur.rowcount


def failures(con: sqlite3.Connection) -> dict:
    """Why each parked calendar is parked — for the interface to show."""
    return {int(r["id"]): {"until": r["next_attempt_at"],
                           "error": r["last_error"],
                           "failures": int(r["sync_failures"] or 0)}
            for r in con.execute(
                "SELECT id, next_attempt_at, last_error, sync_failures "
                "FROM calendar WHERE last_error <> '' "
                "OR next_attempt_at IS NOT NULL").fetchall()}


def forget_calendar(con: sqlite3.Connection, calendar_id: int, *,
                    commit: bool = True) -> int:
    """Delete a calendar and everything under it. Only at the user's request.

    The one place calendar data is destroyed here, and it is deliberately not
    reachable from a sync: `mark_absent` is what a sync does. The cascade takes
    the events and their attendees with it, which is why the count returned is
    of events and not of calendars — that is the number worth confirming.
    """
    events = int(con.execute("SELECT COUNT(*) FROM event WHERE calendar_id = ?",
                             (int(calendar_id),)).fetchone()[0])
    con.execute("DELETE FROM calendar WHERE id = ?", (int(calendar_id),))
    if commit:
        con.commit()
    return events


def touch(con: sqlite3.Connection, calendar_id: int, *,
          error: str = "", commit: bool = True) -> None:
    """Record that a sync ran, and whether it complained."""
    record_sync_state(con, calendar_id, last_synced_at=utc_now(),
                      last_error=error, commit=commit)
