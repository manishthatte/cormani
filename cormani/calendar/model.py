# SPDX-License-Identifier: GPL-3.0-or-later
#
# One shape for an event, whichever provider it came from.
#
# The two APIs disagree about almost everything: Google nests a date or a
# dateTime under `start`, Graph carries a naive string beside a timezone name;
# Google says `responseStatus: needsAction`, Graph says `response: notResponded`
# and has a value for "I am the organiser" that is not a response at all;
# Google's all-day end is exclusive and so is Graph's, but Graph writes it as a
# midnight timestamp rather than as a date. None of that may reach the store,
# the views or the tests — so it stops here, and each provider module's only
# job is to produce one of these.
#
# THE SHAPE IS THE STORE'S, NOT A COMPROMISE BETWEEN THE TWO APIS. `fields()`
# returns exactly what `store/events.upsert` takes, because a second vocabulary
# between the wire and the table would be a second place to be wrong about what
# `all_day` means. Where the store has no opinion — a page token, whether this
# item is a deletion — it is on the dataclass and not in `fields()`.
#
# `deleted` IS A KIND OF EVENT, NOT AN ERROR. An incremental pass reports
# removals in the same list as changes: Google gives status `cancelled`, Graph
# an `@removed` annotation. Both become this, and `sync.py` acts on it.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from dataclasses import dataclass, field

from ..store.calendars import RESPONSE_NEEDS_ACTION
from ..store.events import STATUS_CONFIRMED


@dataclass(frozen=True)
class RemoteCalendar:
    remote_id: str
    name: str = ""
    description: str = ""
    colour: str = ""
    timezone: str = ""
    is_primary: bool = False
    writable: bool = True

    def fields(self) -> dict:
        return {"name": self.name, "description": self.description,
                "colour": self.colour, "timezone": self.timezone,
                "is_primary": self.is_primary, "writable": self.writable}


@dataclass(frozen=True)
class RemoteAttendee:
    address: str
    name: str = ""
    response: str = RESPONSE_NEEDS_ACTION
    is_organiser: bool = False
    is_self: bool = False
    optional: bool = False

    def fields(self) -> dict:
        return {"address": self.address, "name": self.name,
                "response": self.response, "is_organiser": self.is_organiser,
                "is_self": self.is_self, "optional": self.optional}


@dataclass(frozen=True)
class RemoteEvent:
    remote_id: str
    series_id: str = ""
    ical_uid: str = ""
    etag: str = ""
    summary: str = ""
    description: str = ""
    location: str = ""
    # UTC for a timed event, a plain YYYY-MM-DD for an all-day one. The
    # provider modules do that conversion; nothing downstream repeats it.
    starts_at: str = ""
    ends_at: str = ""
    all_day: bool = False
    status: str = STATUS_CONFIRMED
    busy: bool = True
    organiser_name: str = ""
    organiser_addr: str = ""
    my_response: str = ""
    web_link: str = ""
    recurring: bool = False
    reminder: int | None = None
    updated_at: str = ""
    attendees: tuple = ()
    deleted: bool = False

    def fields(self) -> dict:
        """Exactly what `store/events.upsert` takes."""
        return {"series_id": self.series_id, "ical_uid": self.ical_uid,
                "etag": self.etag, "summary": self.summary,
                "description": self.description, "location": self.location,
                "starts_at": self.starts_at, "ends_at": self.ends_at,
                "all_day": self.all_day, "status": self.status,
                "busy": self.busy, "organiser_name": self.organiser_name,
                "organiser_addr": self.organiser_addr,
                "my_response": self.my_response, "web_link": self.web_link,
                "recurring": self.recurring, "reminder": self.reminder,
                "updated_at": self.updated_at}

    def guests(self) -> list:
        return [a.fields() for a in self.attendees]


@dataclass(frozen=True)
class Page:
    """One answer from a listing call, and how to ask for the next.

    `sync_token` is only ever set on the LAST page of a pass — both providers
    do it that way, and taking one from the middle would bookmark a position
    the store has not reached.
    """
    events: tuple = ()
    next_token: str = ""
    sync_token: str = ""
    calendars: tuple = field(default_factory=tuple)
