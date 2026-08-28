# SPDX-License-Identifier: GPL-3.0-or-later
#
# Google Calendar, API v3.
#
# THE SERVER EXPANDS RECURRENCE AND THIS CLIENT NEVER PARSES A RULE.
# `singleEvents=true` with a `timeMin` and a `timeMax` is the whole of that
# decision; migration 6 argues it, and the cost — a window rather than the
# whole calendar — is recorded there.
#
# A SYNC TOKEN BELONGS TO THE WINDOW IT WAS ISSUED FOR. This is the fact about
# this API most likely to be got wrong, because nothing in the response says
# so: `nextSyncToken` remembers the parameters of the request that produced it,
# so asking with a token returns changes WITHIN THAT WINDOW ONLY, and passing
# `timeMin` beside a token is an error. The consequence for corMani is in
# `sync.py`: widening the window throws the token away, because a token for
# September cannot answer a question about October and would silently claim to.
#
# `showDeleted` IS SET FOR AN INCREMENTAL PASS AND NOT FOR A FULL ONE, and each
# is deliberate. Incrementally, a deletion IS the news — an event the user
# cancelled from a phone arrives as `status: cancelled` and nothing else says
# it is gone. In a full pass the answer is what exists, and cancelled instances
# of a recurring series would arrive by the dozen to be thrown away;
# `events.prune_window` is what learns about deletions there.
#
# A RESPONSE IS A GET AND THEN A PATCH, AND THE EXTRA REQUEST IS THE POINT.
# Google patches an ARRAY by replacing it, so answering an invitation means
# sending the whole attendee list back with one `responseStatus` changed —
# and sending the copy this store happens to hold would silently drop anybody
# added since the last sync, from their own meeting.
#
# GOOGLE'S RESPONSE WORDS ARE THIS STORE'S. `needsAction`, `declined`,
# `tentative`, `accepted`: `store/calendars.py` uses those spellings because
# one of the two providers had to win and inventing a third vocabulary would
# have meant two translations instead of one. Graph is where the translating
# happens.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from ..store.calendars import RESPONSE_ACCEPTED, RESPONSE_NEEDS_ACTION
from ..store.events import STATUS_CANCELLED, STATUS_CONFIRMED
from .model import Page, RemoteAttendee, RemoteCalendar, RemoteEvent

API = "https://www.googleapis.com/calendar/v3"

# 2500 is the API's ceiling and a page nobody wants; 250 is a month of a busy
# calendar in one request and a response small enough to parse without a pause.
PAGE_SIZE = 250

# Which reminder to keep, of the several an event may carry. The soonest, and
# only the ones that reach a person here: an `email` reminder is Google's to
# send and corMani showing a notification for it as well would be two.
_REMINDER_METHODS = ("popup",)


def _rfc3339(value: str) -> str:
    """The API's timeMin/timeMax want an RFC 3339 instant with an offset.

    The store's own format already is one. This exists to refuse anything else
    loudly rather than have the API refuse it obscurely.
    """
    text = (value or "").strip()
    if not text:
        return ""
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _when(part: dict) -> tuple:
    """One of Google's start/end objects as (text, all_day).

    `date` and `dateTime` are the two kinds, and which key is present is the
    only thing that says which kind an event is — there is no flag.
    """
    if not isinstance(part, dict):
        return "", False
    if part.get("date"):
        return str(part["date"]), True
    stamp = str(part.get("dateTime", "") or "")
    if not stamp:
        return "", False
    try:
        when = dt.datetime.fromisoformat(stamp)
    except ValueError:                                       # pragma: no cover
        return "", False
    if when.tzinfo is None:
        # A dateTime without an offset is legal only with a timeZone beside it,
        # and Google always sends one of the two. Reading it as UTC is the
        # least wrong of the available guesses and is an hour out at worst.
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat(), False


def _reminder(payload: dict) -> int | None:
    """The soonest reminder this client will act on, in minutes, or None.

    None means "the calendar's default", which is what `useDefault` says and
    what the calendar row carries. It does not mean "no reminder".
    """
    reminders = payload.get("reminders")
    if not isinstance(reminders, dict) or reminders.get("useDefault"):
        return None
    overrides = reminders.get("overrides")
    if not isinstance(overrides, list):
        return None
    minutes = [int(o["minutes"]) for o in overrides
               if isinstance(o, dict) and o.get("method") in _REMINDER_METHODS
               and str(o.get("minutes", "")).lstrip("-").isdigit()]
    return min(minutes) if minutes else None


def _attendees(payload: dict) -> tuple:
    organiser = payload.get("organizer") if isinstance(
        payload.get("organizer"), dict) else {}
    out = []
    for guest in payload.get("attendees") or ():
        if not isinstance(guest, dict) or guest.get("resource"):
            continue                     # a room is not a person
        address = str(guest.get("email", "") or "")
        out.append(RemoteAttendee(
            address=address, name=str(guest.get("displayName", "") or ""),
            response=str(guest.get("responseStatus", "") or RESPONSE_NEEDS_ACTION),
            is_organiser=bool(guest.get("organizer")) or (
                bool(address) and address == organiser.get("email")),
            is_self=bool(guest.get("self")), optional=bool(guest.get("optional"))))
    return tuple(out)


def _my_response(payload: dict, guests: tuple) -> str:
    for guest in guests:
        if guest.is_self:
            return guest.response
    organiser = payload.get("organizer") if isinstance(
        payload.get("organizer"), dict) else {}
    if organiser.get("self"):
        return RESPONSE_ACCEPTED
    # No attendees at all: an appointment the user wrote for themselves. They
    # are going, and calling that "no reply yet" would put a chip on every
    # entry in their own diary.
    return RESPONSE_ACCEPTED if not guests else RESPONSE_NEEDS_ACTION


def to_event(payload: dict) -> RemoteEvent:
    """One of Google's events as one of ours."""
    guests = _attendees(payload)
    organiser = payload.get("organizer") if isinstance(
        payload.get("organizer"), dict) else {}
    starts_at, all_day = _when(payload.get("start") or {})
    ends_at, _ = _when(payload.get("end") or {})
    status = str(payload.get("status", "") or STATUS_CONFIRMED)
    return RemoteEvent(
        remote_id=str(payload.get("id", "") or ""),
        series_id=str(payload.get("recurringEventId", "") or ""),
        ical_uid=str(payload.get("iCalUID", "") or ""),
        etag=str(payload.get("etag", "") or ""),
        summary=str(payload.get("summary", "") or ""),
        description=str(payload.get("description", "") or ""),
        location=str(payload.get("location", "") or ""),
        starts_at=starts_at, ends_at=ends_at, all_day=all_day, status=status,
        busy=str(payload.get("transparency", "opaque")) != "transparent",
        organiser_name=str(organiser.get("displayName", "") or ""),
        organiser_addr=str(organiser.get("email", "") or ""),
        my_response=_my_response(payload, guests),
        web_link=str(payload.get("htmlLink", "") or ""),
        recurring=bool(payload.get("recurringEventId")),
        reminder=_reminder(payload),
        updated_at=str(payload.get("updated", "") or ""),
        attendees=guests, deleted=(status == STATUS_CANCELLED))


def to_calendar(payload: dict) -> RemoteCalendar:
    access = str(payload.get("accessRole", "") or "")
    return RemoteCalendar(
        remote_id=str(payload.get("id", "") or ""),
        # `summaryOverride` is the name the user gave a calendar somebody else
        # shared with them, and it wins: it is the name they will look for.
        name=str(payload.get("summaryOverride") or payload.get("summary") or ""),
        description=str(payload.get("description", "") or ""),
        colour=str(payload.get("backgroundColor", "") or ""),
        timezone=str(payload.get("timeZone", "") or ""),
        is_primary=bool(payload.get("primary")),
        writable=access in ("owner", "writer"))


def default_reminder(payload: dict) -> int | None:
    minutes = [int(r["minutes"]) for r in payload.get("defaultReminders") or ()
               if isinstance(r, dict) and r.get("method") in _REMINDER_METHODS
               and str(r.get("minutes", "")).lstrip("-").isdigit()]
    return min(minutes) if minutes else None


def to_body(fields: dict, attendees=None) -> dict:
    """corMani's field names as one of Google's event bodies.

    A TIME IS NEVER WRITTEN WITHOUT `all_day` BESIDE IT, because the two kinds
    are different keys rather than a flag — `{"date": ...}` against
    `{"dateTime": ...}` — and a patch that guessed would turn a meeting into a
    day off. `queue.py` guarantees the pair; this asserts nothing and simply
    does not write a time it cannot spell.
    """
    body: dict = {}
    for ours, theirs in (("summary", "summary"), ("description", "description"),
                         ("location", "location")):
        if ours in fields:
            body[theirs] = fields[ours] or ""
    if "busy" in fields:
        body["transparency"] = "opaque" if fields["busy"] else "transparent"
    if "reminder" in fields:
        minutes = fields["reminder"]
        body["reminders"] = ({"useDefault": True} if minutes is None else
                             {"useDefault": False,
                              "overrides": [{"method": "popup",
                                             "minutes": int(minutes)}]})
    if "all_day" in fields and fields.get("starts_at"):
        if fields["all_day"]:
            body["start"] = {"date": str(fields["starts_at"])[:10]}
            body["end"] = {"date": str(fields.get("ends_at", ""))[:10]}
        else:
            body["start"] = {"dateTime": _rfc3339(fields["starts_at"]),
                             "timeZone": "UTC"}
            body["end"] = {"dateTime": _rfc3339(fields.get("ends_at", "")),
                           "timeZone": "UTC"}
    if attendees is not None:
        body["attendees"] = [
            {"email": guest["address"],
             **({"displayName": guest["name"]} if guest.get("name") else {}),
             **({"optional": True} if guest.get("optional") else {})}
            for guest in attendees if guest.get("address")]
    return body


class GoogleCalendar:
    """One account's calendars on Google. Holds no state but its connection."""

    name = "google"

    def __init__(self, http, *, address: str = "") -> None:
        self.http = http
        self.address = address

    # ------------------------------------------------------------ reading
    def calendars(self) -> list:
        """Every calendar this account can see, followed as many pages as
        there are. `minAccessRole` is not set: a read-only holiday feed is
        still something the user wants drawn."""
        out, token = [], ""
        while True:
            params = {"maxResults": PAGE_SIZE, "showHidden": "true"}
            if token:
                params["pageToken"] = token
            answer = self.http.get(f"{API}/users/me/calendarList", params)
            for item in answer.data.get("items") or ():
                if not isinstance(item, dict) or item.get("deleted"):
                    continue
                out.append((to_calendar(item), default_reminder(item)))
            token = str(answer.data.get("nextPageToken", "") or "")
            if not token:
                return out

    def events(self, calendar_id: str, *, start: str = "", end: str = "",
               sync_token: str = "", page_token: str = "") -> Page:
        """One page of instances, either over a window or since a token.

        The two are exclusive by the API's rules, not by choice: a request
        carrying both is refused, and the token already remembers the window.
        """
        params: dict = {"maxResults": PAGE_SIZE, "singleEvents": "true"}
        if sync_token:
            params["syncToken"] = sync_token
            params["showDeleted"] = "true"
        else:
            params["timeMin"] = _rfc3339(start)
            params["timeMax"] = _rfc3339(end)
            # Only legal beside singleEvents, and worth asking for: it makes a
            # partial page a usable prefix rather than an arbitrary sample.
            params["orderBy"] = "startTime"
        if page_token:
            # Every other parameter must be repeated unchanged; the API says so
            # and a page token alone returns the first page again.
            params["pageToken"] = page_token
        answer = self.http.get(f"{API}/calendars/{_quote(calendar_id)}/events",
                               params)
        events = tuple(to_event(item)
                       for item in answer.data.get("items") or ()
                       if isinstance(item, dict) and item.get("id"))
        return Page(events=events,
                    next_token=str(answer.data.get("nextPageToken", "") or ""),
                    sync_token=str(answer.data.get("nextSyncToken", "") or ""))

    def event(self, calendar_id: str, event_id: str) -> RemoteEvent:
        answer = self.http.get(
            f"{API}/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}")
        return to_event(answer.data)

    # ------------------------------------------------------------ writing
    def create(self, calendar_id: str, fields: dict,
               attendees=None) -> RemoteEvent:
        body = to_body(fields, attendees)
        answer = self.http.post(
            f"{API}/calendars/{_quote(calendar_id)}/events", body,
            params={"sendUpdates": _send_updates(attendees)})
        return to_event(answer.data)

    def update(self, calendar_id: str, event_id: str, fields: dict,
               attendees=None, *, etag: str = "") -> RemoteEvent:
        body = to_body(fields, attendees)
        answer = self.http.patch(
            f"{API}/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}",
            body, params={"sendUpdates": _send_updates(attendees)},
            if_match=etag)
        return to_event(answer.data)

    def delete(self, calendar_id: str, event_id: str, *, etag: str = "",
               notify: bool = True) -> None:
        self.http.delete(
            f"{API}/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}",
            params={"sendUpdates": "all" if notify else "none"}, if_match=etag)

    def respond(self, calendar_id: str, event_id: str, response: str, *,
                comment: str = "", etag: str = "") -> RemoteEvent:
        """Answer an invitation, by reading the guest list back first.

        See the module header: a patch replaces the array, so the list that
        goes up must be the list that is there — not the one this store last
        heard about.
        """
        current = self.event(calendar_id, event_id)
        mine = self.address.lower()
        guests = []
        for guest in current.attendees:
            entry: dict = {"email": guest.address}
            if guest.optional:
                entry["optional"] = True
            if guest.is_self or (mine and guest.address.lower() == mine):
                entry["responseStatus"] = response
                if comment:
                    entry["comment"] = comment
            else:
                entry["responseStatus"] = guest.response
            guests.append(entry)
        if not any(g.get("responseStatus") == response for g in guests):
            # Invited through a group address, so this user is not in the list
            # by name. Google accepts the addition and it is what its own
            # interface does.
            entry = {"email": self.address, "responseStatus": response}
            if comment:
                entry["comment"] = comment
            guests.append(entry)
        answer = self.http.patch(
            f"{API}/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}",
            {"attendees": guests}, params={"sendUpdates": "all"},
            if_match=etag or current.etag)
        return to_event(answer.data)


def _send_updates(attendees) -> str:
    """Whether the provider should post invitations for this write.

    "all" when there are guests, and that is not a preference: a meeting
    created with attendees that sends no invitation is a meeting nobody knows
    about. "none" when there are none, so that editing a private appointment
    does not ask Google to mail anybody.
    """
    return "all" if attendees else "none"


def _quote(value: str) -> str:
    """A calendar id is an address and an event id is opaque; both go in a path.

    `safe=''` on purpose: the default leaves `/` alone, and a value containing
    one would silently become two path segments and address something else.
    """
    from urllib.parse import quote

    return quote(str(value), safe="")
