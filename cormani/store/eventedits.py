# SPDX-License-Identifier: GPL-3.0-or-later
#
# Changing an event, and the queue entry it owes.
#
# The seam is `store/edits.py`'s, for the same reason: reading is one concern
# and writing is another, and every write here owes the provider a queue entry
# while the sync's own writes — `events.upsert` — must owe nothing. Two
# writers, two modules.
#
# THE LOCAL HALF HAPPENS FIRST AND ALWAYS. Offline-first is not a feature of
# the mail half alone: moving a meeting on a train must move it on the screen,
# and `calendar/queue.py` tells the provider when there is a network. Nothing
# in this module opens a socket or knows that one exists.
#
# AN EDIT BEHIND AN UNSENT CREATE IS FOLDED INTO THE CREATE. An event made
# offline has a `local:` id, which is an id no provider will recognise; an
# update queued against it would be a request to change something that has
# never existed. `eventqueue.merge_payload` is where that happens and the two
# cases — nothing sent yet, something sent already — are the whole of the
# reasoning here.
#
# DELETING AN EVENT WHOSE CREATE IS STILL QUEUED TELLS THE SERVER NOTHING.
# `store/undo.py` reached the same conclusion for mail and the sentence is the
# same: undo unsays what the server has not been told, and says the opposite of
# what it has.
#
# WHAT THIS MODULE CANNOT DO, AND SAYS SO RATHER THAN PRETENDING: it cannot
# move an event to another calendar (both providers implement that as a delete
# and a create, so it is two intentions and not one), and it cannot edit a
# SERIES. Migration 6 explains the second: the store holds instances, so
# "change every Tuesday" is a rule this client never parsed. `series_id` is
# carried precisely so that the day corMani can send "this and all following"
# it does not need a migration to do it.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from typing import Mapping, Sequence

from . import calendars as calendars_repo
from . import eventqueue
from . import events as events_repo
from .calendars import RESPONSE_ACCEPTED, RESPONSES
from .database import utc_now
from .events import Attendee, Event

# What a user may change about an event. Everything else on the row is the
# provider's — an etag, a web link, whether it recurs — and a writer that
# accepted those would be offering to change something the provider owns.
EDITABLE = ("summary", "description", "location", "starts_at", "ends_at",
            "all_day", "busy", "reminder")


class NotWritable(RuntimeError):
    """The calendar is read-only. Refused here rather than by the provider, so
    the interface can say so before the user types anything."""


class NotSupported(RuntimeError):
    """Something corMani deliberately does not do. See the module header."""


def _account_of(con: sqlite3.Connection, calendar_id: int) -> tuple:
    row = con.execute("""
        SELECT a.id AS id, a.address AS address, a.display_name AS display_name
        FROM calendar c JOIN account a ON a.id = c.account_id WHERE c.id = ?
    """, (int(calendar_id),)).fetchone()
    return (int(row["id"]), row["address"], row["display_name"] or "") if row \
        else (0, "", "")


def _require_writable(con: sqlite3.Connection, calendar_id: int):
    calendar = calendars_repo.get_calendar(con, calendar_id)
    if calendar is None:
        raise NotWritable("that calendar is no longer in the store")
    if not calendar.writable:
        raise NotWritable(f"{calendar.label} is shared read-only; corMani "
                          f"cannot write to it")
    return calendar


# ------------------------------------------------------------------ making
def create_event(con: sqlite3.Connection, calendar_id: int, *, summary: str,
                 starts_at: str, ends_at: str, all_day: bool = False,
                 location: str = "", description: str = "", busy: bool = True,
                 reminder: int | None = None,
                 attendees: Sequence = (), commit: bool = True) -> int:
    """Put an event in a calendar. Returns the local row id.

    The row is written with an id no provider can produce, so that everything
    which addresses an event — the views, the queue, a later edit — works
    identically whether or not the provider has heard of it yet.
    """
    _require_writable(con, calendar_id)
    _, address, display_name = _account_of(con, calendar_id)
    remote_id = events_repo.new_local_id()
    fields = {"summary": summary, "description": description,
              "location": location, "starts_at": starts_at, "ends_at": ends_at,
              "all_day": all_day, "busy": busy, "reminder": reminder,
              "status": events_repo.STATUS_CONFIRMED,
              "organiser_name": display_name, "organiser_addr": address,
              # The user made it, so the user is going. Anything else would
              # show their own event as an invitation awaiting a reply.
              "my_response": RESPONSE_ACCEPTED, "updated_at": utc_now()}
    guests = _guest_list(attendees, self_address=address,
                         self_name=display_name)
    event_id = events_repo.upsert(con, calendar_id, remote_id, fields,
                                  attendees=guests, commit=False)
    payload = {name: fields[name] for name in EDITABLE if name in fields}
    if guests:
        payload["attendees"] = [_guest_dict(g) for g in guests]
    eventqueue.enqueue(con, calendar_id, eventqueue.KIND_CREATE,
                       event_id=event_id, remote_id=remote_id, payload=payload,
                       commit=False)
    events_repo.set_pending(con, [event_id], commit=False)
    if commit:
        con.commit()
    return event_id


def _guest_list(attendees: Sequence, *, self_address: str,
                self_name: str) -> list:
    """The organiser goes in the list, marked, and is not repeated.

    Both providers return the organiser as an attendee and corMani's own writes
    have to match, or an event created here and re-read from the server would
    have one more guest than the one the user typed.
    """
    out, seen = [], set()
    if self_address:
        out.append(Attendee(name=self_name, address=self_address,
                            response=RESPONSE_ACCEPTED, is_organiser=True,
                            is_self=True))
        seen.add(self_address.lower())
    for guest in attendees:
        if isinstance(guest, Mapping):
            guest = Attendee(name=str(guest.get("name", "")),
                             address=str(guest.get("address", "")),
                             optional=bool(guest.get("optional")))
        if not guest.address or guest.address.lower() in seen:
            continue
        seen.add(guest.address.lower())
        out.append(guest)
    return out


def _guest_dict(guest: Attendee) -> dict:
    return {"name": guest.name, "address": guest.address,
            "optional": guest.optional, "is_organiser": guest.is_organiser}


# ---------------------------------------------------------------- changing
def update_event(con: sqlite3.Connection, event_id: int, *,
                 attendees: Sequence | None = None, commit: bool = True,
                 **changes) -> bool:
    """Change an event. Only the fields named, and only ones the user owns.

    Returns whether anything moved. The queue entry carries the SAME fields —
    a patch, never a replace — because corMani models perhaps twenty of the
    sixty a provider event has and a full write would drop the rest.
    """
    event = events_repo.get_event(con, event_id)
    if event is None:
        return False
    _require_writable(con, event.calendar_id)
    # A recurring event needs no special case, and that is a consequence of
    # migration 6 rather than an oversight: what this store holds is one
    # INSTANCE, its remote id is that instance's, and a write to it changes
    # that occurrence alone on both providers. "This and all following" would
    # need the series, which corMani has never parsed.
    fields = {name: value for name, value in changes.items()
              if name in EDITABLE}
    if not fields and attendees is None:
        return False
    if fields:
        events_repo.upsert(con, event.calendar_id, event.remote_id, fields,
                           commit=False)
    guests = None
    if attendees is not None:
        _, address, display_name = _account_of(con, event.calendar_id)
        guests = _guest_list(attendees, self_address=address,
                             self_name=display_name)
        events_repo.set_attendees(con, event_id, guests, commit=False)

    payload = dict(fields)
    if guests is not None:
        payload["attendees"] = [_guest_dict(g) for g in guests]
    previous = eventqueue.last_op(con, event_id)
    if previous is not None and previous.attempts == 0 and previous.kind in (
            eventqueue.KIND_CREATE, eventqueue.KIND_UPDATE):
        eventqueue.merge_payload(con, previous.id, payload, commit=False)
    else:
        eventqueue.enqueue(con, event.calendar_id, eventqueue.KIND_UPDATE,
                           event_id=event_id, remote_id=event.remote_id,
                           etag=event.etag, payload=payload, commit=False)
    events_repo.set_pending(con, [event_id], commit=False)
    if commit:
        con.commit()
    return True


def set_response(con: sqlite3.Connection, event_id: int, response: str, *,
                 comment: str = "", commit: bool = True) -> bool:
    """Answer an invitation. The one write that a read-only calendar allows.

    Answering is not editing: a meeting somebody else owns, on a calendar
    shared into this account, is still a meeting this user is expected at, and
    both providers accept a response to it. `_require_writable` is deliberately
    not called here.
    """
    if response not in RESPONSES:
        raise ValueError(f"{response!r} is not a response")
    event = events_repo.get_event(con, event_id)
    if event is None:
        return False
    events_repo.upsert(con, event.calendar_id, event.remote_id,
                       {"my_response": response}, commit=False)
    guests = [g if not g.is_self else Attendee(
        name=g.name, address=g.address, response=response,
        is_organiser=g.is_organiser, is_self=True, optional=g.optional)
        for g in event.attendees]
    if guests:
        events_repo.set_attendees(con, event_id, guests, commit=False)

    payload = {"response": response}
    if comment:
        payload["comment"] = comment
    previous = eventqueue.last_op(con, event_id)
    if (previous is not None and previous.attempts == 0
            and previous.kind == eventqueue.KIND_RESPOND):
        eventqueue.merge_payload(con, previous.id, payload, commit=False)
    else:
        eventqueue.enqueue(con, event.calendar_id, eventqueue.KIND_RESPOND,
                           event_id=event_id, remote_id=event.remote_id,
                           etag=event.etag, payload=payload, commit=False)
    events_repo.set_pending(con, [event_id], commit=False)
    if commit:
        con.commit()
    return True


# ---------------------------------------------------------------- deleting
def delete_event(con: sqlite3.Connection, event_id: int, *,
                 commit: bool = True) -> Event | None:
    """Remove an event here, and tell the provider unless it never knew.

    Returns what was removed, so the interface can name it. The row goes now:
    the op carries the calendar, the remote id and the etag, which is
    everything the delete needs, and an event that stayed on the screen until
    the next sync would read as a command that did not work.
    """
    event = events_repo.get_event(con, event_id)
    if event is None:
        return None
    _require_writable(con, event.calendar_id)

    queued = eventqueue.for_event(con, event_id)
    unsent_create = any(op.kind == eventqueue.KIND_CREATE and op.attempts == 0
                        for op in queued)
    if unsent_create:
        # The provider has never heard of this event. Saying "delete it" would
        # be the first it ever heard, and it would be an error.
        eventqueue.discard(con, [op.id for op in queued], commit=False)
    else:
        eventqueue.discard(con, [op.id for op in queued
                                 if op.kind != eventqueue.KIND_DELETE],
                           commit=False)
        eventqueue.enqueue(con, event.calendar_id, eventqueue.KIND_DELETE,
                           event_id=event_id, remote_id=event.remote_id,
                           etag=event.etag, commit=False)
    con.execute("DELETE FROM event WHERE id = ?", (int(event_id),))
    if commit:
        con.commit()
    return event


def move_to_calendar(con: sqlite3.Connection, event_id: int,
                     calendar_id: int, *, commit: bool = True) -> int:
    """Move an event by deleting it on the source calendar and creating it on
    the target. Both providers implement a move as delete+create; this exposes
    that as one user-facing action."""
    event = events_repo.get_event(con, event_id)
    if event is None:
        raise NotWritable("that event is no longer in the store")
    target = int(calendar_id)
    if event.calendar_id == target:
        return event_id
    if event.is_series_master:
        raise NotSupported(
            "moving a recurring series master is not supported")
    _require_writable(con, event.calendar_id)
    _require_writable(con, target)
    guests = list(event.attendees)
    fields = {
        "summary": event.summary,
        "description": event.description,
        "location": event.location,
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "all_day": event.all_day,
        "busy": event.busy,
        "reminder": event.reminder,
    }
    delete_event(con, event_id, commit=False)
    new_id = create_event(con, target, attendees=guests, commit=False, **fields)
    if commit:
        con.commit()
    return new_id
