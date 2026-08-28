# SPDX-License-Identifier: GPL-3.0-or-later
#
# Microsoft Graph, v1.0.
#
# The same three jobs as `google.py` and almost none of the same spellings.
# Four things here are decisions rather than transcription:
#
# EVERY REQUEST ASKS FOR UTC, AND THE ALTERNATIVE IS A DATA FILE. Graph returns
# each time as a naive string beside a WINDOWS time zone name — "India
# Standard Time", not "Asia/Kolkata" — and turning one of those into an offset
# needs the CLDR mapping table, which is a vendored dependency and
# CONVENTIONS.txt §3 forbids it. `Prefer: outlook.timezone="UTC"` makes the
# question not arise. It is the single most load-bearing header in this file.
#
# AND THAT COSTS AN ALL-DAY EVENT ITS DATE, WHICH IS PUT BACK BY ROUNDING.
# Graph stores an all-day event as midnight-to-midnight in some zone; asked for
# UTC it converts, so an all-day event created at midnight in Bombay comes back
# as 18:30 on the PREVIOUS day. The date is recovered by rounding to the
# nearest midnight — add twelve hours and take the date — which is exact for
# every zone inside ±12 and is why `_all_day_date` exists rather than a slice
# of the string. `google.py` needs none of this because Google sends a plain
# date and says so.
#
# A DELTA LINK IS A URL, NOT A TOKEN, and it is followed verbatim. Graph
# returns `@odata.nextLink` and `@odata.deltaLink` with the whole query
# embedded, so continuing a pass means fetching the URL it gave and adding
# nothing: appending a parameter to one is how a client gets a 400 that looks
# like an expired bookmark. The store's `sync_token` column is documented as
# opaque for exactly this reason — one provider puts a token in it and the
# other puts a URL.
#
# ANSWERING AN INVITATION IS AN ACTION, NOT A PATCH. `/accept`, `/decline` and
# `/tentativelyAccept` are POSTs that also send the organiser the reply mail;
# patching `responseStatus` is refused by the API. This is the one place where
# Graph is the simpler of the two — Google needs a read and a write to answer
# the same question.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from ..store.calendars import (RESPONSE_ACCEPTED, RESPONSE_DECLINED,
                               RESPONSE_NEEDS_ACTION, RESPONSE_TENTATIVE)
from ..store.events import STATUS_CANCELLED, STATUS_CONFIRMED
from .model import Page, RemoteAttendee, RemoteCalendar, RemoteEvent

API = "https://graph.microsoft.com/v1.0"

PAGE_SIZE = 250

# What every request asks for. `outlook.timezone` is the important one; see the
# module header. `odata.maxpagesize` is a Prefer rather than a query parameter
# on the delta endpoint, which is why it is here and not in `params`.
_PREFER = f'outlook.timezone="UTC", odata.maxpagesize={PAGE_SIZE}'
_PREFER_DELTA = f'{_PREFER}, odata.track-changes'

# Graph's five words for a response, and this store's four. `organizer` is not
# a response at all — it is the answer to a different question — and it maps to
# accepted because somebody's own meeting is one they are going to.
_RESPONSES = {
    "none": RESPONSE_NEEDS_ACTION,
    "notresponded": RESPONSE_NEEDS_ACTION,
    "organizer": RESPONSE_ACCEPTED,
    "accepted": RESPONSE_ACCEPTED,
    "tentativelyaccepted": RESPONSE_TENTATIVE,
    "declined": RESPONSE_DECLINED,
}

_ACTIONS = {RESPONSE_ACCEPTED: "accept",
            RESPONSE_DECLINED: "decline",
            RESPONSE_TENTATIVE: "tentativelyAccept"}


def _response(value) -> str:
    return _RESPONSES.get(str(value or "").strip().lower(), RESPONSE_NEEDS_ACTION)


def _instant(part) -> dt.datetime | None:
    """One of Graph's {dateTime, timeZone} objects as an aware datetime.

    The zone name is deliberately ignored: every request carries
    `Prefer: outlook.timezone="UTC"`, so the string is UTC, and reading the
    name would mean owning the Windows-to-IANA table this file exists to avoid.
    """
    if not isinstance(part, dict):
        return None
    text = str(part.get("dateTime", "") or "").strip()
    if not text:
        return None
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:                                       # pragma: no cover
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def _all_day_date(when: dt.datetime) -> str:
    """The date an all-day event is on, from the instant Graph converted it to.

    Rounding, not truncation. See the module header: the value has been moved
    across midnight by the UTC conversion, and the offset that moved it is
    always less than twelve hours in practice.
    """
    return (when.astimezone(dt.timezone.utc) + dt.timedelta(hours=12)).date(
        ).isoformat()


def _text_of(payload: dict) -> str:
    """An event's description as characters.

    `bodyPreview` is plain and TRUNCATED at 255 characters, which is fine for a
    list and wrong for the pane that shows the whole thing, so the body is
    taken and flattened when it is HTML. `text_from_html` is not a sanitiser
    and is not being used as one — nothing here is rendered.
    """
    body = payload.get("body")
    if isinstance(body, dict) and body.get("content"):
        content = str(body["content"])
        if str(body.get("contentType", "")).lower() == "html":
            from ..imap.envelope import text_from_html
            return text_from_html(content)
        return content
    return str(payload.get("bodyPreview", "") or "")


def _attendees(payload: dict) -> tuple:
    organiser = ((payload.get("organizer") or {}).get("emailAddress") or {}) \
        if isinstance(payload.get("organizer"), dict) else {}
    organiser_addr = str(organiser.get("address", "") or "").lower()
    out = []
    for guest in payload.get("attendees") or ():
        if not isinstance(guest, dict) or guest.get("type") == "resource":
            continue
        mail = guest.get("emailAddress") or {}
        address = str(mail.get("address", "") or "")
        status = guest.get("status") or {}
        out.append(RemoteAttendee(
            address=address, name=str(mail.get("name", "") or ""),
            response=_response(status.get("response")),
            is_organiser=bool(address and address.lower() == organiser_addr),
            # Graph does not mark the signed-in user in the attendee list at
            # all; `responseStatus` on the event is where that answer is, and
            # `to_event` fills this in from the address it was built with.
            is_self=False,
            optional=str(guest.get("type", "")).lower() == "optional"))
    return tuple(out)


def to_event(payload: dict, *, address: str = "") -> RemoteEvent:
    """One of Graph's events as one of ours."""
    if payload.get("@removed"):
        return RemoteEvent(remote_id=str(payload.get("id", "") or ""),
                           status=STATUS_CANCELLED, deleted=True)
    all_day = bool(payload.get("isAllDay"))
    start, end = _instant(payload.get("start")), _instant(payload.get("end"))
    if all_day:
        starts_at = _all_day_date(start) if start else ""
        ends_at = _all_day_date(end) if end else ""
    else:
        starts_at = start.astimezone(dt.timezone.utc).replace(
            microsecond=0).isoformat() if start else ""
        ends_at = end.astimezone(dt.timezone.utc).replace(
            microsecond=0).isoformat() if end else ""

    organiser = ((payload.get("organizer") or {}).get("emailAddress") or {}) \
        if isinstance(payload.get("organizer"), dict) else {}
    guests = _attendees(payload)
    mine = (address or "").lower()
    if mine:
        guests = tuple(RemoteAttendee(
            address=g.address, name=g.name, response=g.response,
            is_organiser=g.is_organiser, optional=g.optional,
            is_self=g.address.lower() == mine) for g in guests)
    cancelled = bool(payload.get("isCancelled"))
    return RemoteEvent(
        remote_id=str(payload.get("id", "") or ""),
        series_id=str(payload.get("seriesMasterId", "") or ""),
        ical_uid=str(payload.get("iCalUId", "") or ""),
        etag=str(payload.get("@odata.etag", "") or ""),
        summary=str(payload.get("subject", "") or ""),
        description=_text_of(payload),
        location=str(((payload.get("location") or {}).get("displayName", "")
                      if isinstance(payload.get("location"), dict) else "") or ""),
        starts_at=starts_at, ends_at=ends_at, all_day=all_day,
        status=STATUS_CANCELLED if cancelled else STATUS_CONFIRMED,
        busy=str(payload.get("showAs", "busy")).lower() != "free",
        organiser_name=str(organiser.get("name", "") or ""),
        organiser_addr=str(organiser.get("address", "") or ""),
        my_response=_response(
            (payload.get("responseStatus") or {}).get("response")
            if isinstance(payload.get("responseStatus"), dict) else None),
        web_link=str(payload.get("webLink", "") or ""),
        recurring=str(payload.get("type", "")) in ("occurrence", "exception"),
        reminder=(int(payload["reminderMinutesBeforeStart"])
                  if payload.get("isReminderOn")
                  and str(payload.get("reminderMinutesBeforeStart", "")).isdigit()
                  else None),
        updated_at=str(payload.get("lastModifiedDateTime", "") or ""),
        attendees=guests, deleted=cancelled)


def to_calendar(payload: dict) -> RemoteCalendar:
    return RemoteCalendar(
        remote_id=str(payload.get("id", "") or ""),
        name=str(payload.get("name", "") or ""),
        colour=str(payload.get("hexColor", "") or ""),
        # Graph carries no per-calendar zone; the mailbox has one, and an
        # empty string is the honest answer rather than a guess at the user's.
        timezone="",
        is_primary=bool(payload.get("isDefaultCalendar")),
        writable=bool(payload.get("canEdit", True)))


def to_body(fields: dict, attendees=None) -> dict:
    """corMani's field names as one of Graph's event bodies."""
    body: dict = {}
    if "summary" in fields:
        body["subject"] = fields["summary"] or ""
    if "description" in fields:
        body["body"] = {"contentType": "text",
                        "content": fields["description"] or ""}
    if "location" in fields:
        body["location"] = {"displayName": fields["location"] or ""}
    if "busy" in fields:
        body["showAs"] = "busy" if fields["busy"] else "free"
    if "reminder" in fields:
        minutes = fields["reminder"]
        body["isReminderOn"] = minutes is not None
        if minutes is not None:
            body["reminderMinutesBeforeStart"] = int(minutes)
    if "all_day" in fields and fields.get("starts_at"):
        body["isAllDay"] = bool(fields["all_day"])
        if fields["all_day"]:
            # Midnight UTC, which is what Graph requires of an all-day event
            # and what `_all_day_date` reads back unchanged.
            body["start"] = {"dateTime": f"{str(fields['starts_at'])[:10]}T00:00:00",
                             "timeZone": "UTC"}
            body["end"] = {"dateTime": f"{str(fields.get('ends_at', ''))[:10]}T00:00:00",
                           "timeZone": "UTC"}
        else:
            body["start"] = _graph_time(fields["starts_at"])
            body["end"] = _graph_time(fields.get("ends_at", ""))
    if attendees is not None:
        body["attendees"] = [
            {"emailAddress": {"address": guest["address"],
                              **({"name": guest["name"]} if guest.get("name") else {})},
             "type": "optional" if guest.get("optional") else "required"}
            for guest in attendees if guest.get("address")]
    return body


def _graph_time(value: str) -> dict:
    text = (value or "").strip()
    if not text:
        return {}
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:                                  # pragma: no cover
        when = when.replace(tzinfo=dt.timezone.utc)
    when = when.astimezone(dt.timezone.utc).replace(microsecond=0, tzinfo=None)
    # No offset in the string: Graph reads the zone from the object beside it
    # and rejects a value that carries both.
    return {"dateTime": when.isoformat(), "timeZone": "UTC"}


class GraphCalendar:
    """One account's calendars on Microsoft Graph."""

    name = "microsoft"

    def __init__(self, http, *, address: str = "") -> None:
        self.http = http
        self.address = address

    # ------------------------------------------------------------ reading
    def calendars(self) -> list:
        out, url = [], f"{API}/me/calendars"
        while url:
            answer = self.http.get(url, headers={"Prefer": _PREFER})
            for item in answer.data.get("value") or ():
                if isinstance(item, dict) and item.get("id"):
                    # Graph has no per-calendar default reminder, so there is
                    # nothing to carry: an event that wants one says so.
                    out.append((to_calendar(item), None))
            url = str(answer.data.get("@odata.nextLink", "") or "")
        return out

    def events(self, calendar_id: str, *, start: str = "", end: str = "",
               sync_token: str = "", page_token: str = "") -> Page:
        """One page of the calendar view, or of the delta that continues it.

        A continuation URL is followed exactly as given. Everything the request
        needs — the window, the page, the state — is already inside it.
        """
        link = page_token or sync_token
        if link.startswith("http"):
            answer = self.http.get(link, headers={"Prefer": _PREFER_DELTA})
        else:
            answer = self.http.get(
                f"{API}/me/calendars/{_quote(calendar_id)}/calendarView/delta",
                {"startDateTime": _naive_utc(start),
                 "endDateTime": _naive_utc(end)},
                headers={"Prefer": _PREFER_DELTA})
        events = tuple(to_event(item, address=self.address)
                       for item in answer.data.get("value") or ()
                       if isinstance(item, dict) and item.get("id"))
        return Page(events=events,
                    next_token=str(answer.data.get("@odata.nextLink", "") or ""),
                    sync_token=str(answer.data.get("@odata.deltaLink", "") or ""))

    def event(self, calendar_id: str, event_id: str) -> RemoteEvent:
        answer = self.http.get(f"{API}/me/events/{_quote(event_id)}",
                               headers={"Prefer": _PREFER})
        return to_event(answer.data, address=self.address)

    # ------------------------------------------------------------ writing
    def create(self, calendar_id: str, fields: dict,
               attendees=None) -> RemoteEvent:
        answer = self.http.post(
            f"{API}/me/calendars/{_quote(calendar_id)}/events",
            to_body(fields, attendees), headers={"Prefer": _PREFER})
        return to_event(answer.data, address=self.address)

    def update(self, calendar_id: str, event_id: str, fields: dict,
               attendees=None, *, etag: str = "") -> RemoteEvent:
        answer = self.http.patch(f"{API}/me/events/{_quote(event_id)}",
                                 to_body(fields, attendees), if_match=etag)
        return to_event(answer.data, address=self.address)

    def delete(self, calendar_id: str, event_id: str, *, etag: str = "",
               notify: bool = True) -> None:
        """Graph posts the cancellation itself when the organiser deletes a
        meeting, so `notify` has nothing to switch and is accepted to keep the
        two clients interchangeable."""
        self.http.delete(f"{API}/me/events/{_quote(event_id)}", if_match=etag)

    def respond(self, calendar_id: str, event_id: str, response: str, *,
                comment: str = "", etag: str = "") -> RemoteEvent | None:
        action = _ACTIONS.get(response)
        if action is None:
            # needsAction is not something a person can send: there is no way
            # to un-answer an invitation on either provider.
            raise ValueError(f"{response!r} cannot be sent to Graph")
        body = {"sendResponse": True}
        if comment:
            body["comment"] = comment
        self.http.post(f"{API}/me/events/{_quote(event_id)}/{action}", body)
        # The action returns 202 with no body. The event is re-read so that the
        # caller has the same thing Google's patch hands back, rather than two
        # shapes of answer for one operation.
        return self.event(calendar_id, event_id)


def _naive_utc(value: str) -> str:
    """The calendarView window, in the form Graph takes: no offset, UTC."""
    text = (value or "").strip()
    if not text:
        return ""
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:                                  # pragma: no cover
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).replace(
        microsecond=0, tzinfo=None).isoformat()


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")
