# SPDX-License-Identifier: GPL-3.0-or-later
#
# The calendar's offline queue, from the store's side.
#
# `pending.py` is this module's twin and the discipline is copied from it
# deliberately: the user's action wins the interface immediately, the intention
# is recorded here, and `calendar/queue.py` tells the provider afterwards. A
# refusal is recorded rather than dropped, an op is deleted only when the
# server has accepted it, and the queue drains in id order per account because
# a create and the update that follows it must arrive in that order.
#
# WHY A SECOND TABLE. Migration 7 argues it: everything about the rule is
# shared and none of the coordinates are. `pending_op` is written in a folder
# and a UID; there is no folder and no UID here.
#
# AN OP CARRIES THE ETAG THE USER ACTED ON, and that is what makes a
# conditional write possible — the provider refuses rather than overwriting a
# change made from a phone while this laptop was shut. Recorded at the moment
# of the action, because by the time the queue drains the row may have been
# re-synced and the etag it holds is the server's, not the one the user saw.
#
# THE PAYLOAD OF AN UPDATE IS THE FIELDS THAT CHANGED, NEVER THE WHOLE EVENT.
# corMani models about twenty of the sixty fields a Google event has. A write
# that sent the whole of what it holds would silently drop conferencing
# details, colour, visibility, attachments and extended properties — data the
# user can see in their provider's own interface and would watch disappear.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from .database import utc_now
from .pending import MAX_ATTEMPTS

KIND_CREATE = "create"
KIND_UPDATE = "update"
KIND_DELETE = "delete"
KIND_RESPOND = "respond"
KINDS = (KIND_CREATE, KIND_UPDATE, KIND_DELETE, KIND_RESPOND)


@dataclass(frozen=True)
class EventOp:
    id: int
    account_id: int
    calendar_id: int
    event_id: int | None
    kind: str
    remote_id: str
    etag: str
    payload: dict
    attempts: int
    last_error: str

    @property
    def stuck(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS


def _op(row: sqlite3.Row) -> EventOp:
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except ValueError:                                       # pragma: no cover
        payload = {}
    return EventOp(
        id=int(row["id"]), account_id=int(row["account_id"]),
        calendar_id=int(row["calendar_id"]),
        event_id=row["event_id"], kind=row["kind"],
        remote_id=row["remote_id"] or "", etag=row["etag"] or "",
        payload=payload if isinstance(payload, dict) else {},
        attempts=int(row["attempts"]), last_error=row["last_error"] or "")


# --------------------------------------------------------------- enqueueing
def enqueue(con: sqlite3.Connection, calendar_id: int, kind: str, *,
            event_id: int | None = None, remote_id: str = "", etag: str = "",
            payload: dict | None = None, commit: bool = True) -> int:
    """Record one intention. Returns the op id, or 0 if the calendar is gone."""
    row = con.execute("SELECT account_id FROM calendar WHERE id = ?",
                      (int(calendar_id),)).fetchone()
    if row is None:
        return 0
    cur = con.execute("""
        INSERT INTO event_op (account_id, calendar_id, event_id, kind,
            remote_id, etag, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (int(row["account_id"]), int(calendar_id),
          None if event_id is None else int(event_id), kind, remote_id, etag,
          json.dumps(payload or {}), utc_now()))
    if commit:
        con.commit()
    return int(cur.lastrowid)


def last_op(con: sqlite3.Connection, event_id: int) -> EventOp | None:
    """The most recent op for an event, whatever kind it is.

    "Most recent" and not "most recent of this kind", because what a new op
    must not do is overtake an older one. Merging into anything but the last is
    a reordering.
    """
    row = con.execute(
        "SELECT * FROM event_op WHERE event_id = ? ORDER BY id DESC LIMIT 1",
        (int(event_id),)).fetchone()
    return _op(row) if row is not None else None


def merge_payload(con: sqlite3.Connection, op_id: int, fields: dict, *,
                  commit: bool = True) -> None:
    """Fold more changes into an op that has not been attempted.

    Two edits before a sync are one write, and — more importantly — an edit to
    an event whose CREATE is still queued belongs in the create. Queueing an
    update against a `local:` id would ask the provider to change something it
    has never heard of.
    """
    row = con.execute("SELECT payload FROM event_op WHERE id = ?",
                      (int(op_id),)).fetchone()
    if row is None:
        return
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except ValueError:                                       # pragma: no cover
        payload = {}
    if not isinstance(payload, dict):                        # pragma: no cover
        payload = {}
    payload.update(fields)
    con.execute("UPDATE event_op SET payload = ? WHERE id = ?",
                (json.dumps(payload), int(op_id)))
    if commit:
        con.commit()


def set_remote_id(con: sqlite3.Connection, event_id: int, remote_id: str, *,
                  commit: bool = True) -> None:
    """Give every queued op for an event the id the provider has just issued.

    A create that succeeds turns a `local:` id into a real one, and any op
    queued behind it was written against the local one. Without this the update
    the user made two minutes later is sent to an event that does not exist.
    """
    con.execute("UPDATE event_op SET remote_id = ? WHERE event_id = ?",
                (remote_id, int(event_id)))
    if commit:
        con.commit()


# ------------------------------------------------------------------ reading
def get(con: sqlite3.Connection, op_id: int) -> EventOp | None:
    """One op, as it stands NOW.

    The drain re-reads each op immediately before sending it rather than
    working from the list it started with, and that is not tidiness: a create
    that succeeds rewrites the remote id of every op queued behind it, and a
    stale copy would address the edit to an id the provider has never seen.
    """
    row = con.execute("SELECT * FROM event_op WHERE id = ?",
                      (int(op_id),)).fetchone()
    return _op(row) if row is not None else None


def pending_for(con: sqlite3.Connection, account_id: int, *,
                include_stuck: bool = False) -> list[EventOp]:
    """One account's queue, in the order the user made the changes."""
    rows = con.execute("SELECT * FROM event_op WHERE account_id = ? ORDER BY id",
                       (int(account_id),)).fetchall()
    ops = [_op(r) for r in rows]
    return ops if include_stuck else [op for op in ops if not op.stuck]


def for_event(con: sqlite3.Connection, event_id: int) -> list[EventOp]:
    return [_op(r) for r in con.execute(
        "SELECT * FROM event_op WHERE event_id = ? ORDER BY id",
        (int(event_id),)).fetchall()]


def counts(con: sqlite3.Connection) -> dict:
    """Outstanding and stuck, per account, for the status bar."""
    out: dict = {}
    for row in con.execute("SELECT account_id, attempts FROM event_op").fetchall():
        entry = out.setdefault(int(row["account_id"]), {"pending": 0, "stuck": 0})
        entry["stuck" if row["attempts"] >= MAX_ATTEMPTS else "pending"] += 1
    return out


# --------------------------------------------------------------- retiring
def _touch_events(con: sqlite3.Connection, op_ids: Sequence[int]) -> list[int]:
    marks = ",".join("?" * len(op_ids))
    return [int(r[0]) for r in con.execute(
        f"SELECT DISTINCT event_id FROM event_op WHERE id IN ({marks})",
        list(op_ids)).fetchall() if r[0] is not None]


def _finish(con: sqlite3.Connection, op_ids: Sequence[int],
            commit: bool) -> int:
    from . import events as events_repo

    op_ids = [int(o) for o in op_ids]
    if not op_ids:
        return 0
    touched = _touch_events(con, op_ids)
    marks = ",".join("?" * len(op_ids))
    cur = con.execute(f"DELETE FROM event_op WHERE id IN ({marks})", op_ids)
    events_repo.set_pending(con, touched, commit=False)
    if commit:
        con.commit()
    return cur.rowcount


def complete(con: sqlite3.Connection, op_ids: Sequence[int], *,
             commit: bool = True) -> int:
    """The provider accepted these. Remove them and refresh the markers."""
    return _finish(con, op_ids, commit)


def discard(con: sqlite3.Connection, op_ids: Sequence[int], *,
            commit: bool = True) -> int:
    """Take these back before the provider ever hears them.

    The same statement as `complete` and a different meaning, which is why it
    is a different name: complete means it happened, discard means it will not.
    """
    return _finish(con, op_ids, commit)


def record_failure(con: sqlite3.Connection, op_id: int, error: str, *,
                   commit: bool = True) -> None:
    con.execute(
        "UPDATE event_op SET attempts = attempts + 1, last_attempt_at = ?, "
        "last_error = ? WHERE id = ?", (utc_now(), error[:300], int(op_id)))
    if commit:
        con.commit()


def give_up(con: sqlite3.Connection, op_id: int, error: str, *,
            commit: bool = True) -> None:
    """Stop retrying this one, and say why. Kept, never deleted."""
    con.execute(
        "UPDATE event_op SET attempts = ?, last_attempt_at = ?, "
        "last_error = ? WHERE id = ?",
        (MAX_ATTEMPTS, utc_now(), error[:300], int(op_id)))
    if commit:
        con.commit()


def clear_for_account(con: sqlite3.Connection, account_id: int, *,
                      commit: bool = True) -> int:
    """Abandon an account's calendar queue. Only ever at the user's request."""
    from . import events as events_repo

    touched = [int(r[0]) for r in con.execute(
        "SELECT DISTINCT event_id FROM event_op WHERE account_id = ?",
        (int(account_id),)).fetchall() if r[0] is not None]
    cur = con.execute("DELETE FROM event_op WHERE account_id = ?",
                      (int(account_id),))
    events_repo.set_pending(con, touched, commit=False)
    if commit:
        con.commit()
    return cur.rowcount
