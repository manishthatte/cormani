# SPDX-License-Identifier: GPL-3.0-or-later
#
# Telling the provider what the user already did.
#
# `store/eventqueue.py` records the intention the moment the user acts. This
# drains it, and the split is `imap/queue.py`'s for the same two reasons: the
# store must never import the protocol, and the two halves fail differently —
# recording cannot fail and sending fails constantly.
#
# ORDER IS ABSOLUTE HERE, MORE THAN ON THE MAIL SIDE. A create and the edit
# behind it are not commutative and the second is addressed to an id the first
# has not been given yet. So there is no batching, no merging and no
# reordering: ops go up one at a time, in id order, and a create that succeeds
# writes the provider's id into every op still queued for that event before the
# next one is attempted.
#
# A CONFLICT IS REPORTED, NEVER RESOLVED. A 412 means the event changed between
# the user reading it and this running — from a phone, from the web interface,
# by the organiser. corMani cannot know which of the two versions the user
# wants, and the one thing it must not do is pick. The op is stuck, the reason
# is recorded, and the next sync brings down what the server actually holds so
# the two can be compared by the person who knows.
#
# AN EVENT THAT IS ALREADY GONE IS NOT A FAILURE. A delete of something already
# deleted, or an edit to a meeting the organiser cancelled, is an intention
# overtaken by events. The op is dropped and counted — the same judgement
# `imap/queue.py` makes about a message that is no longer there.
#
# A TRANSIENT FAILURE STOPS THE RUN. If the network is down for one op it is
# down for the next, and hammering fifteen accounts' queues against it is how
# the back-off in the database stops meaning anything.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..store import calendars as calendars_repo
from ..store import eventqueue as queue_repo
from ..store import events as events_repo
from ..store.eventqueue import (KIND_CREATE, KIND_DELETE, KIND_RESPOND,
                                KIND_UPDATE)
from . import errors


@dataclass
class DrainReport:
    sent: int = 0
    dropped: int = 0
    failed: int = 0
    stuck: int = 0
    conflicts: int = 0
    errors: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.errors and not self.failed


def drain(con: sqlite3.Connection, client, account_id: int) -> DrainReport:
    """Send one account's queued calendar changes, in order."""
    report = DrainReport()
    planned = [op.id for op in queue_repo.pending_for(con, account_id,
                                                      include_stuck=True)]
    for op_id in planned:
        # Read again, immediately before sending: the op ahead of this one may
        # have rewritten its remote id. `store/eventqueue.get` says why.
        op = queue_repo.get(con, op_id)
        if op is None:
            continue
        if op.stuck:
            report.stuck += 1
            continue
        calendar = calendars_repo.get_calendar(con, op.calendar_id)
        if calendar is None:
            # The calendar was deleted here. There is nothing to address.
            queue_repo.discard(con, [op.id])
            report.dropped += 1
            continue
        try:
            _perform(con, client, calendar, op)
        except errors.Conflict as exc:
            queue_repo.give_up(con, op.id, errors.describe(exc))
            report.conflicts += 1
            report.errors.append(
                f"{calendar.label}: the event changed elsewhere, so your "
                f"change was not sent. The provider's version has been kept.")
            continue
        except errors.NotFound:
            queue_repo.discard(con, [op.id])
            report.dropped += 1
            continue
        except errors.Transient as exc:
            queue_repo.record_failure(con, op.id, errors.describe(exc))
            report.failed += 1
            report.errors.append(errors.describe(exc))
            return report                       # the next op would fail too
        except errors.Permanent:
            # AuthFailed and NotAuthorised come up here and belong to the
            # account rather than to the op: the engine parks it.
            raise
        queue_repo.complete(con, [op.id])
        report.sent += 1
    return report


def _perform(con: sqlite3.Connection, client, calendar, op) -> None:
    if op.kind == KIND_CREATE:
        _create(con, client, calendar, op)
    elif op.kind == KIND_UPDATE:
        _update(con, client, calendar, op)
    elif op.kind == KIND_DELETE:
        client.delete(calendar.remote_id, op.remote_id, etag=op.etag)
    elif op.kind == KIND_RESPOND:
        _respond(con, client, calendar, op)


def _split(payload: dict) -> tuple:
    """An op's payload as (fields, attendees). Attendees are separate because
    both providers spell a guest list in their own way and `to_body` takes it
    as its own argument — and because None means "leave the list alone" while
    an empty list means "there is nobody"."""
    fields = {k: v for k, v in payload.items() if k != "attendees"}
    return fields, payload.get("attendees")


def _with_times(con: sqlite3.Connection, op, fields: dict) -> dict:
    """Make sure a time change carries `all_day` with it.

    Neither provider has a flag: an all-day event is a different KEY in the
    body — `date` against `dateTime` on Google, `isAllDay` beside a midnight
    on Graph — so a patch that moved a meeting without saying which kind it is
    could turn it into a day off. The row is the authority, and it is read here
    rather than trusted from the payload because the payload holds only what
    the user changed.
    """
    if "starts_at" not in fields and "ends_at" not in fields:
        return fields
    event = events_repo.get_event(con, op.event_id) if op.event_id else None
    out = dict(fields)
    if event is not None:
        out.setdefault("all_day", event.all_day)
        out.setdefault("starts_at", event.starts_at)
        out.setdefault("ends_at", event.ends_at)
    return out


def _create(con: sqlite3.Connection, client, calendar, op) -> None:
    fields, attendees = _split(op.payload)
    remote = client.create(calendar.remote_id, _with_times(con, op, fields),
                           attendees)
    if op.event_id is None:                                  # pragma: no cover
        return
    # The row keeps its identity and gains the provider's name for it, and
    # every op still queued behind this one is re-addressed in the same breath.
    events_repo.adopt_remote_id(con, op.event_id, remote.remote_id, commit=False)
    queue_repo.set_remote_id(con, op.event_id, remote.remote_id, commit=False)
    events_repo.upsert(con, calendar.id, remote.remote_id, remote.fields(),
                       attendees=remote.guests(), commit=False)
    con.commit()


def _update(con: sqlite3.Connection, client, calendar, op) -> None:
    fields, attendees = _split(op.payload)
    remote = client.update(calendar.remote_id, op.remote_id,
                           _with_times(con, op, fields), attendees,
                           etag=op.etag)
    events_repo.upsert(con, calendar.id, remote.remote_id, remote.fields(),
                       attendees=remote.guests())


def _respond(con: sqlite3.Connection, client, calendar, op) -> None:
    response = str(op.payload.get("response", ""))
    if not response:                                         # pragma: no cover
        return
    remote = client.respond(calendar.remote_id, op.remote_id, response,
                            comment=str(op.payload.get("comment", "")),
                            etag=op.etag)
    if remote is not None:
        events_repo.upsert(con, calendar.id, remote.remote_id, remote.fields(),
                           attendees=remote.guests())


def pending_summary(con: sqlite3.Connection) -> dict:
    return queue_repo.counts(con)
