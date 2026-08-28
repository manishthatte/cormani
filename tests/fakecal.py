# SPDX-License-Identifier: GPL-3.0-or-later
#
# Google Calendar and Microsoft Graph, in the test process.
#
# `tests/fakeimap.py` is the model: a server faithful enough that the client
# under test is the real one and only the transport is a double. These two are
# written from the APIs' own rules rather than from what corMani happens to
# send, and the rules they ENFORCE are the useful part —
#
#   * a request with no bearer token is 401, not an empty answer;
#   * a syncToken beside timeMin is 400, because Google refuses the pair and a
#     client that sent both would otherwise be silently forgiven here;
#   * a syncToken this server never issued is 410 fullSyncRequired, which is
#     the one error the sync is supposed to handle by itself;
#   * an If-Match that does not match is 412, so the conditional write in
#     `calendar/queue.py` is exercised rather than asserted about;
#   * and Graph answers a request WITHOUT `Prefer: outlook.timezone="UTC"` in
#     the mailbox's own zone, naming it the Windows way. That is not
#     decoration: it is the failure `calendar/graph.py`'s header calls its most
#     load-bearing line, and a double that always returned UTC would let the
#     header be deleted with every test still green.
#
# Both keep a version counter and hand out bookmarks against it, so an
# incremental pass here really is incremental — a test can change one event and
# assert that one event came back.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from tests.calwire import Reply, Transport, fail

TOKEN = "test-access-token"

_UTC = dt.timezone.utc


def _err(reason: str, message: str, code: int) -> dict:
    return {"error": {"code": code, "message": message,
                      "errors": [{"reason": reason, "message": message}]}}


class _Server:
    """What the two have in common: a bearer token and a version counter."""

    def __init__(self, address: str, token: str = TOKEN) -> None:
        self.address = address
        self.token = token
        self.version = 0
        self.page_size = 250
        self.calendars: list = []
        self.events: dict = {}

    def _tick(self) -> int:
        self.version += 1
        return self.version

    def _authorised(self, call) -> bool:
        return call.token == self.token

    def transport(self) -> Transport:
        return Transport(self)


# --------------------------------------------------------------- Google
class FakeGoogle(_Server):
    """Calendar API v3, over the parts corMani uses."""

    def __init__(self, address: str = "someone@gmail.com", **kwargs) -> None:
        super().__init__(address, **kwargs)
        self.tokens: dict = {}

    # ------------------------------------------------------- fixture side
    def add_calendar(self, remote_id: str, *, summary: str = "",
                     primary: bool = False, access: str = "owner",
                     colour: str = "#268bd2", reminder: int | None = None) -> dict:
        entry = {"id": remote_id, "summary": summary or remote_id,
                 "backgroundColor": colour, "timeZone": "Asia/Kolkata",
                 "accessRole": access, "primary": primary,
                 "defaultReminders": ([{"method": "popup", "minutes": reminder}]
                                      if reminder is not None else [])}
        self.calendars.append(entry)
        self.events.setdefault(remote_id, {})
        return entry

    def add_event(self, calendar_id: str, event_id: str, *, summary: str = "",
                  start: str = "", end: str = "", all_day: bool = False,
                  attendees=(), organiser: str = "", recurring: str = "",
                  ical_uid: str = "", reminder: int | None = None,
                  busy: bool = True, description: str = "",
                  location: str = "") -> dict:
        organiser = organiser or self.address
        event = {
            "id": event_id, "status": "confirmed", "summary": summary,
            "description": description, "location": location,
            "etag": f'W/"{self._tick()}"', "iCalUID": ical_uid or f"{event_id}@x",
            "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
            "updated": dt.datetime.now(_UTC).replace(microsecond=0).isoformat(),
            "organizer": {"email": organiser,
                          "self": organiser == self.address},
            "start": {"date": start} if all_day else {"dateTime": start},
            "end": {"date": end} if all_day else {"dateTime": end},
            "transparency": "opaque" if busy else "transparent",
            "reminders": ({"useDefault": True} if reminder is None else
                          {"useDefault": False,
                           "overrides": [{"method": "popup", "minutes": reminder}]}),
            "_version": self.version,
        }
        if recurring:
            event["recurringEventId"] = recurring
        if attendees:
            event["attendees"] = [
                {"email": a, "self": a == self.address,
                 "organizer": a == organiser,
                 "responseStatus": "accepted" if a == organiser else "needsAction"}
                if isinstance(a, str) else dict(a) for a in attendees]
        self.events.setdefault(calendar_id, {})[event_id] = event
        return event

    def cancel_event(self, calendar_id: str, event_id: str) -> None:
        event = self.events.get(calendar_id, {}).get(event_id)
        if event is not None:
            event.update(status="cancelled", _version=self._tick())

    def touch(self, calendar_id: str, event_id: str, **fields) -> None:
        event = self.events.get(calendar_id, {}).get(event_id)
        if event is not None:
            event.update(fields)
            event["_version"] = self._tick()
            event["etag"] = f'W/"{self.version}"'

    # ------------------------------------------------------------ serving
    def handle(self, call) -> Reply:
        if not self._authorised(call):
            raise fail(401, _err("authError", "Invalid Credentials", 401))
        parts = call.segments()
        # calendar/v3/users/me/calendarList | calendar/v3/calendars/{id}/events
        if parts[-1] == "calendarList":
            return self._calendar_list(call)
        if "calendars" in parts:
            index = parts.index("calendars")
            calendar_id = parts[index + 1]
            rest = parts[index + 2:]
            if rest[:1] == ["events"]:
                if len(rest) == 1:
                    if call.method == "POST":
                        return self._create(call, calendar_id)
                    return self._list(call, calendar_id)
                return self._one(call, calendar_id, rest[1])
        raise fail(404, _err("notFound", f"no such path: {call.path}", 404))

    def _calendar_list(self, call) -> Reply:
        return Reply(200, {"items": list(self.calendars)})

    def _list(self, call, calendar_id: str) -> Reply:
        held = self.events.get(calendar_id)
        if held is None:
            raise fail(404, _err("notFound", "Not Found", 404))
        sync_token = call.query.get("syncToken", "")
        time_min = call.query.get("timeMin", "")
        time_max = call.query.get("timeMax", "")
        if sync_token and (time_min or time_max or call.query.get("orderBy")):
            raise fail(400, _err(
                "invalidParameter",
                "Sync token cannot be used with other query parameters", 400))
        if call.query.get("orderBy") and call.query.get("singleEvents") != "true":
            raise fail(400, _err("invalidParameter",
                                 "orderBy needs singleEvents", 400))

        since = -1
        if sync_token:
            if sync_token not in self.tokens:
                raise fail(410, _err("fullSyncRequired", "Sync token is no "
                                     "longer valid, a full sync is required.", 410))
            since, time_min, time_max = self.tokens[sync_token]

        rows = []
        for event in held.values():
            if event["_version"] <= since:
                continue
            cancelled = event["status"] == "cancelled"
            if cancelled and not (sync_token
                                  or call.query.get("showDeleted") == "true"):
                continue
            if not sync_token and not _overlaps(event, time_min, time_max):
                continue
            rows.append(event)
        rows.sort(key=lambda e: (_start_of(e), e["id"]))

        offset = int(call.query.get("pageToken", "0") or 0)
        page = rows[offset:offset + self.page_size]
        answer: dict = {"items": [_strip(e) for e in page]}
        if offset + self.page_size < len(rows):
            answer["nextPageToken"] = str(offset + self.page_size)
        else:
            token = f"sync-{self.version}-{len(self.tokens)}"
            self.tokens[token] = (self.version, time_min, time_max)
            answer["nextSyncToken"] = token
        return Reply(200, answer)

    def _one(self, call, calendar_id: str, event_id: str) -> Reply:
        held = self.events.get(calendar_id, {})
        event = held.get(event_id)
        if event is None:
            raise fail(404, _err("notFound", "Not Found", 404))
        if call.if_match and call.if_match != event["etag"]:
            raise fail(412, _err("conditionNotMet", "Precondition Failed", 412))
        if call.method == "GET":
            return Reply(200, _strip(event), {"ETag": event["etag"]})
        if call.method == "DELETE":
            event.update(status="cancelled", _version=self._tick())
            return Reply(204, None)
        if call.method == "PATCH":
            _apply(event, call.body)
            event["_version"] = self._tick()
            event["etag"] = f'W/"{self.version}"'
            return Reply(200, _strip(event), {"ETag": event["etag"]})
        raise fail(405, _err("notAllowed", "Method Not Allowed", 405))

    def _create(self, call, calendar_id: str) -> Reply:
        if calendar_id not in self.events:
            raise fail(404, _err("notFound", "Not Found", 404))
        event_id = f"srv{len(self.events[calendar_id]) + 1}"
        event = self.add_event(calendar_id, event_id)
        _apply(event, call.body)
        event["_version"] = self._tick()
        event["etag"] = f'W/"{self.version}"'
        return Reply(200, _strip(event), {"ETag": event["etag"]})


def _strip(event: dict) -> dict:
    return {k: v for k, v in event.items() if not k.startswith("_")}


def _start_of(event: dict) -> str:
    part = event.get("start") or {}
    return str(part.get("dateTime") or part.get("date") or "")


def _end_of(event: dict) -> str:
    part = event.get("end") or {}
    return str(part.get("dateTime") or part.get("date") or "")


def _overlaps(event: dict, time_min: str, time_max: str) -> bool:
    """The server's own window test, in the server's own terms.

    Deliberately naive — a string comparison over RFC 3339, which is what a
    date and a timestamp share a prefix of. It is the API's behaviour that
    matters here and not this line's elegance.
    """
    start, end = _start_of(event), _end_of(event)
    if time_min and end and end <= time_min[:len(end)]:
        return False
    if time_max and start and start >= time_max[:len(start)]:
        return False
    return True


def _apply(event: dict, body: dict) -> None:
    """A PATCH: the fields given, and only those. Arrays replace."""
    for key, value in (body or {}).items():
        event[key] = value


# ---------------------------------------------------------------- Graph
class FakeGraph(_Server):
    """Graph v1.0, over /me/calendars and the calendar view."""

    def __init__(self, address: str = "someone@outlook.com", **kwargs) -> None:
        super().__init__(address, **kwargs)
        self.deltas: dict = {}
        # The mailbox's own zone, as Graph would name it. Only ever seen by a
        # request that forgot to ask for UTC.
        self.zone_name = "India Standard Time"
        self.zone = dt.timezone(dt.timedelta(hours=5, minutes=30))

    # ------------------------------------------------------- fixture side
    def add_calendar(self, remote_id: str, *, name: str = "",
                     default: bool = False, can_edit: bool = True,
                     colour: str = "#268bd2") -> dict:
        entry = {"id": remote_id, "name": name or remote_id,
                 "hexColor": colour, "isDefaultCalendar": default,
                 "canEdit": can_edit,
                 "owner": {"name": "", "address": self.address}}
        self.calendars.append(entry)
        self.events.setdefault(remote_id, {})
        return entry

    def add_event(self, calendar_id: str, event_id: str, *, subject: str = "",
                  start: str = "", end: str = "", all_day: bool = False,
                  attendees=(), organiser: str = "", series: str = "",
                  ical_uid: str = "", reminder: int | None = None,
                  busy: bool = True, body: str = "", location: str = "",
                  response: str = "none") -> dict:
        organiser = organiser or self.address
        event = {
            "id": event_id, "subject": subject, "isAllDay": all_day,
            "isCancelled": False, "@odata.etag": f'W/"{self._tick()}"',
            "iCalUId": ical_uid or f"{event_id}@x",
            "webLink": f"https://outlook.office.com/calendar/item/{event_id}",
            "lastModifiedDateTime": dt.datetime.now(_UTC).replace(
                microsecond=0).isoformat(),
            "organizer": {"emailAddress": {"address": organiser, "name": ""}},
            "_start": start, "_end": end,
            "showAs": "busy" if busy else "free",
            "body": {"contentType": "text", "content": body},
            "bodyPreview": body[:255],
            "location": {"displayName": location},
            "isReminderOn": reminder is not None,
            "reminderMinutesBeforeStart": reminder or 0,
            "responseStatus": {"response": response},
            "type": "occurrence" if series else "singleInstance",
            "_version": self.version,
        }
        if series:
            event["seriesMasterId"] = series
        if attendees:
            event["attendees"] = [
                {"emailAddress": {"address": a, "name": ""},
                 "type": "required",
                 "status": {"response": "organizer" if a == organiser else "none"}}
                if isinstance(a, str) else dict(a) for a in attendees]
        self.events.setdefault(calendar_id, {})[event_id] = event
        return event

    def remove_event(self, calendar_id: str, event_id: str) -> None:
        event = self.events.get(calendar_id, {}).get(event_id)
        if event is not None:
            event["_removed"] = True
            event["_version"] = self._tick()

    def touch(self, calendar_id: str, event_id: str, **fields) -> None:
        event = self.events.get(calendar_id, {}).get(event_id)
        if event is not None:
            event.update(fields)
            event["_version"] = self._tick()
            event["@odata.etag"] = f'W/"{self.version}"'

    # ------------------------------------------------------------ serving
    def handle(self, call) -> Reply:
        if not self._authorised(call):
            raise fail(401, {"error": {"code": "InvalidAuthenticationToken",
                                       "message": "Access token is empty."}})
        parts = call.segments()
        if parts[-1] == "calendars" and call.method == "GET":
            return Reply(200, {"value": list(self.calendars)})
        if "calendarView" in parts or "delta" in parts:
            index = parts.index("calendars")
            return self._view(call, parts[index + 1])
        if "events" in parts:
            index = parts.index("events")
            rest = parts[index + 1:]
            if not rest:
                return self._create(call, parts[index - 1])
            if len(rest) == 2:
                return self._respond(call, rest[0], rest[1])
            return self._one(call, rest[0])
        raise fail(404, {"error": {"code": "itemNotFound",
                                   "message": f"no such path: {call.path}"}})

    def _time(self, call, value: str, all_day: bool) -> dict:
        """UTC when asked for it, and the mailbox's zone when not.

        The whole point of the double. See the module header.
        """
        if not value:
            return {}
        when = dt.datetime.fromisoformat(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_UTC)
        if call.prefers('outlook.timezone="utc"'):
            return {"dateTime": when.astimezone(_UTC).replace(
                tzinfo=None, microsecond=0).isoformat() + ".0000000",
                "timeZone": "UTC"}
        return {"dateTime": when.astimezone(self.zone).replace(
            tzinfo=None, microsecond=0).isoformat() + ".0000000",
            "timeZone": self.zone_name}

    def _shaped(self, call, event: dict) -> dict:
        if event.get("_removed"):
            return {"id": event["id"], "@removed": {"reason": "deleted"}}
        out = {k: v for k, v in event.items() if not k.startswith("_")}
        out["start"] = self._time(call, event["_start"], event["isAllDay"])
        out["end"] = self._time(call, event["_end"], event["isAllDay"])
        return out

    def _view(self, call, calendar_id: str) -> Reply:
        held = self.events.get(calendar_id)
        if held is None:
            raise fail(404, {"error": {"code": "ErrorItemNotFound",
                                       "message": "calendar not found"}})
        token = call.query.get("$deltatoken", "")
        skip = int(call.query.get("$skiptoken", "0") or 0)
        since = -1
        start, end = call.query.get("startDateTime", ""), call.query.get(
            "endDateTime", "")
        if token:
            if token not in self.deltas:
                raise fail(410, {"error": {"code": "resyncRequired",
                                           "message": "delta token expired"}})
            since, start, end = self.deltas[token]
        elif not (start and end):
            raise fail(400, {"error": {
                "code": "ErrorInvalidUrlQueryFilter",
                "message": "startDateTime and endDateTime are required"}})

        rows = [e for e in held.values() if e["_version"] > since
                and (e.get("_removed") or _graph_overlaps(e, start, end))]
        rows.sort(key=lambda e: (e["_start"], e["id"]))
        page = rows[skip:skip + self.page_size]
        answer: dict = {"value": [self._shaped(call, e) for e in page]}
        base = (f"https://graph.microsoft.com/v1.0/me/calendars/{calendar_id}"
                f"/calendarView/delta")
        if skip + self.page_size < len(rows):
            answer["@odata.nextLink"] = (
                f"{base}?$skiptoken={skip + self.page_size}"
                f"&startDateTime={start}&endDateTime={end}")
        elif call.prefers("odata.track-changes") or token:
            # Without the Prefer header there is no bookmark, exactly as the
            # real service behaves — the client simply gets a collection.
            name = f"delta-{self.version}-{len(self.deltas)}"
            self.deltas[name] = (self.version, start, end)
            answer["@odata.deltaLink"] = f"{base}?$deltatoken={name}"
        return Reply(200, answer)

    def _one(self, call, event_id: str) -> Reply:
        calendar_id, event = self._find(event_id)
        if call.if_match and call.if_match != event["@odata.etag"]:
            raise fail(412, {"error": {"code": "ErrorIrresolvableConflict",
                                       "message": "precondition failed"}})
        if call.method == "GET":
            return Reply(200, self._shaped(call, event))
        if call.method == "DELETE":
            event["_removed"] = True
            event["_version"] = self._tick()
            return Reply(204, None)
        if call.method == "PATCH":
            _apply_graph(event, call.body)
            event["_version"] = self._tick()
            event["@odata.etag"] = f'W/"{self.version}"'
            return Reply(200, self._shaped(call, event))
        raise fail(405, {"error": {"code": "notAllowed"}})   # pragma: no cover

    def _respond(self, call, event_id: str, action: str) -> Reply:
        _, event = self._find(event_id)
        mapping = {"accept": "accepted", "decline": "declined",
                   "tentativelyAccept": "tentativelyAccepted"}
        if action not in mapping or call.method != "POST":
            raise fail(400, {"error": {"code": "ErrorInvalidRequest",
                                       "message": f"cannot {action}"}})
        event["responseStatus"] = {"response": mapping[action]}
        for guest in event.get("attendees") or ():
            if guest.get("emailAddress", {}).get("address") == self.address:
                guest["status"] = {"response": mapping[action]}
        event["_version"] = self._tick()
        return Reply(202, None)

    def _create(self, call, calendar_id: str) -> Reply:
        if calendar_id not in self.events:
            raise fail(404, {"error": {"code": "ErrorItemNotFound"}})
        event_id = f"srv{len(self.events[calendar_id]) + 1}"
        event = self.add_event(calendar_id, event_id)
        _apply_graph(event, call.body)
        event["_version"] = self._tick()
        return Reply(201, self._shaped(call, event))

    def _find(self, event_id: str) -> tuple:
        for calendar_id, held in self.events.items():
            if event_id in held:
                return calendar_id, held[event_id]
        raise fail(404, {"error": {"code": "ErrorItemNotFound",
                                   "message": "event not found"}})


def _apply_graph(event: dict, body: dict) -> None:
    for key, value in (body or {}).items():
        if key in ("start", "end") and isinstance(value, dict):
            # The server keeps one canonical instant and renders it per
            # request, so a write is stored in the same normal form.
            stamp = str(value.get("dateTime", ""))
            when = dt.datetime.fromisoformat(stamp) if stamp else None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_UTC)
                event[f"_{key}"] = when.astimezone(_UTC).replace(
                    microsecond=0).isoformat()
        else:
            event[key] = value


def _graph_overlaps(event: dict, start: str, end: str) -> bool:
    first, last = event.get("_start", ""), event.get("_end", "")
    if start and last and last <= start:
        return False
    if end and first and first >= end:
        return False
    return True
