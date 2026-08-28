# SPDX-License-Identifier: GPL-3.0-or-later
#
# iCalendar, in and out — enough of RFC 5545 to answer an invitation.
#
# An invitation arrives as mail. It is a `text/calendar` part carrying
# `METHOD:REQUEST`, and the reply the organiser's client expects is another
# one carrying `METHOD:REPLY`. That is iTIP (RFC 5546), and it is the only
# thing that works when the invitation came from a server corMani has no API
# for — which is most of them.
#
# WHAT THIS PARSES, AND WHAT IT DELIBERATELY DOES NOT. It reads one VEVENT:
# who, what, when, and who has answered. It does NOT implement recurrence.
# migration 6 explains why at length and nothing here changes it — an RRULE is
# recorded as "this repeats" and never expanded, so an invitation to a weekly
# meeting is answered as a whole, which is what accepting one means anyway.
#
# TIME ZONES COME FROM `zoneinfo`, WHICH IS THE STANDARD LIBRARY AND NOT A
# VENDORED DEPENDENCY. A TZID naming an IANA zone resolves against the system's
# own tzdata. Microsoft sometimes writes a WINDOWS zone name there instead —
# "India Standard Time" — which zoneinfo cannot know, so the VTIMEZONE
# component that accompanies it is read for its TZOFFSETTO and used as the
# fallback. Only if both fail is the time read as UTC, and that is recorded on
# the invitation rather than hidden, because an hour's error in a meeting time
# is the kind of quiet wrong CONVENTIONS.txt §8 is about.
#
# EVERY VALUE IS UNESCAPED AND EVERY LINE IS UNFOLDED FIRST. RFC 5545 folds at
# 75 octets by inserting CRLF and a space, and escapes commas, semicolons,
# backslashes and newlines inside a value. A parser that splits on ':' before
# unfolding gets a SUMMARY cut in half; one that does not unescape shows
# `Lunch\, then the talk`.
#
# THE REPLY IS BUILT BY HAND AND THAT IS FOUR LINES OF FOLDING. The alternative
# is a library, and CONVENTIONS.txt §3 forbids one for something this size.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .. import APP_NAME, __version__
from ..store.calendars import (RESPONSE_ACCEPTED, RESPONSE_DECLINED,
                               RESPONSE_NEEDS_ACTION, RESPONSE_TENTATIVE)

METHOD_REQUEST = "REQUEST"
METHOD_REPLY = "REPLY"
METHOD_CANCEL = "CANCEL"
METHOD_PUBLISH = "PUBLISH"

CONTENT_TYPE = "text/calendar"

# iCalendar's PARTSTAT words and this store's. `DELEGATED` and `IN-PROCESS`
# have no corMani equivalent and are read as "no answer yet", which is what
# they mean to the person being asked.
_PARTSTAT_IN = {"NEEDS-ACTION": RESPONSE_NEEDS_ACTION,
                "ACCEPTED": RESPONSE_ACCEPTED,
                "TENTATIVE": RESPONSE_TENTATIVE,
                "DECLINED": RESPONSE_DECLINED}
_PARTSTAT_OUT = {RESPONSE_ACCEPTED: "ACCEPTED",
                 RESPONSE_TENTATIVE: "TENTATIVE",
                 RESPONSE_DECLINED: "DECLINED"}

_UTC = dt.timezone.utc
_FOLD = re.compile(r"\r?\n[ \t]")
_DURATION = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$")


@dataclass(frozen=True)
class InviteAttendee:
    address: str
    name: str = ""
    response: str = RESPONSE_NEEDS_ACTION
    optional: bool = False


@dataclass(frozen=True)
class Invitation:
    method: str = ""
    uid: str = ""
    sequence: int = 0
    summary: str = ""
    description: str = ""
    location: str = ""
    starts_at: str = ""
    ends_at: str = ""
    all_day: bool = False
    organiser_name: str = ""
    organiser_addr: str = ""
    recurring: bool = False
    status: str = ""
    attendees: tuple = ()
    # True when a time carried a zone this build could not resolve and was
    # read as UTC. The interface says so rather than showing a confident hour.
    zone_unknown: bool = False
    raw: str = field(default="", repr=False)

    @property
    def is_cancellation(self) -> bool:
        return self.method == METHOD_CANCEL or self.status == "CANCELLED"

    @property
    def is_request(self) -> bool:
        return self.method in (METHOD_REQUEST, "") and bool(self.uid)

    def response_of(self, address: str) -> str:
        wanted = (address or "").strip().lower()
        for guest in self.attendees:
            if guest.address.lower() == wanted:
                return guest.response
        return RESPONSE_NEEDS_ACTION

    def invites(self, address: str) -> bool:
        wanted = (address or "").strip().lower()
        return any(g.address.lower() == wanted for g in self.attendees)


# ------------------------------------------------------------------ reading
def _unescape(value: str) -> str:
    out, escaped = [], False
    for char in value:
        if escaped:
            out.append({"n": "\n", "N": "\n"}.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    return "".join(out)


def _split_params(head: str) -> tuple:
    """A content line's name and parameters, respecting quoted values.

    Quoted because a CN may contain a semicolon and a URI parameter contains a
    colon, and splitting naively is how "Baker; Frances" becomes two parameters.
    """
    parts, current, quoted = [], [], False
    for char in head:
        if char == '"':
            quoted = not quoted
            continue
        if char == ";" and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    name = parts[0].strip().upper()
    params = {}
    for chunk in parts[1:]:
        key, _, value = chunk.partition("=")
        params[key.strip().upper()] = value.strip()
    return name, params


def _lines(text: str) -> list:
    """Unfolded content lines as (name, params, value)."""
    out = []
    for line in _FOLD.sub("", text or "").splitlines():
        if not line.strip():
            continue
        head, sep, value = _partition_unquoted(line)
        if not sep:
            continue
        name, params = _split_params(head)
        out.append((name, params, value))
    return out


def _partition_unquoted(line: str) -> tuple:
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            return line[:index], ":", line[index + 1:]
    return line, "", ""


def _zones(lines: list) -> dict:
    """TZID to a fixed offset, read from the VTIMEZONE blocks in the file.

    The fallback for a zone `zoneinfo` cannot name — see the module header.
    The STANDARD offset is taken rather than the DAYLIGHT one because it is
    the one that is right for most of the year, and this path is already the
    second-best answer.
    """
    out, current, in_standard = {}, "", False
    depth = []
    for name, _params, value in lines:
        if name == "BEGIN":
            depth.append(value.upper())
            if value.upper() == "STANDARD":
                in_standard = True
        elif name == "END":
            if depth and depth[-1] == "VTIMEZONE":
                current = ""
            if value.upper() == "STANDARD":
                in_standard = False
            if depth:
                depth.pop()
        elif name == "TZID" and depth and depth[-1] == "VTIMEZONE":
            current = value.strip()
        elif name == "TZOFFSETTO" and current and (in_standard or current not in out):
            offset = _offset(value)
            if offset is not None:
                out[current] = offset
    return out


def _offset(value: str) -> dt.timezone | None:
    text = (value or "").strip()
    match = re.match(r"^([+-])(\d{2})(\d{2})(\d{2})?$", text)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    delta = dt.timedelta(hours=int(match.group(2)), minutes=int(match.group(3)),
                         seconds=int(match.group(4) or 0))
    return dt.timezone(sign * delta)


def _zone_for(tzid: str, zones: dict) -> tuple:
    """(tzinfo, resolved). Never raises; UTC is the last resort."""
    if not tzid:
        return _UTC, True
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tzid), True
    except Exception:
        pass
    zone = zones.get(tzid)
    return (zone, True) if zone is not None else (_UTC, False)


def _time(value: str, params: dict, zones: dict) -> tuple:
    """One DTSTART/DTEND as (stored text, all_day, zone_resolved)."""
    text = (value or "").strip()
    if not text:
        return "", False, True
    if params.get("VALUE", "").upper() == "DATE" or (
            len(text) == 8 and text.isdigit()):
        try:
            return dt.date(int(text[:4]), int(text[4:6]),
                           int(text[6:8])).isoformat(), True, True
        except ValueError:                                   # pragma: no cover
            return "", True, True
    stamp = text.rstrip("Z")
    try:
        when = dt.datetime.strptime(stamp[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return "", False, True
    if text.endswith("Z"):
        when = when.replace(tzinfo=_UTC)
        resolved = True
    else:
        zone, resolved = _zone_for(params.get("TZID", ""), zones)
        when = when.replace(tzinfo=zone)
    return when.astimezone(_UTC).replace(microsecond=0).isoformat(), False, resolved


def _duration(value: str) -> dt.timedelta | None:
    match = _DURATION.match((value or "").strip())
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()
             if k != "sign" and v}
    delta = dt.timedelta(weeks=parts.get("weeks", 0), days=parts.get("days", 0),
                         hours=parts.get("hours", 0),
                         minutes=parts.get("minutes", 0),
                         seconds=parts.get("seconds", 0))
    return -delta if match.group("sign") == "-" else delta


def _address(value: str) -> str:
    text = (value or "").strip()
    if text.lower().startswith("mailto:"):
        text = text[7:]
    return text.strip()


def parse(text: str) -> Invitation | None:
    """One iCalendar document as an Invitation, or None if it holds no event."""
    lines = _lines(text)
    if not lines:
        return None
    zones = _zones(lines)
    method = ""
    event: list = []
    depth: list = []
    for name, params, value in lines:
        if name == "BEGIN":
            depth.append(value.upper())
            continue
        if name == "END":
            if depth and depth[-1] == "VEVENT" and event:
                break               # the FIRST VEVENT; see the module header
            if depth:
                depth.pop()
            continue
        if name == "METHOD" and depth and depth[-1] == "VCALENDAR":
            method = value.strip().upper()
        elif depth and depth[-1] == "VEVENT":
            event.append((name, params, value))
    if not event:
        return None

    fields: dict = {}
    attendees: list = []
    resolved = True
    duration = None
    for name, params, value in event:
        if name == "ATTENDEE":
            attendees.append(InviteAttendee(
                address=_address(value),
                name=_unescape(params.get("CN", "")),
                response=_PARTSTAT_IN.get(params.get("PARTSTAT", "").upper(),
                                          RESPONSE_NEEDS_ACTION),
                optional=params.get("ROLE", "").upper() == "OPT-PARTICIPANT"))
        elif name == "ORGANIZER":
            fields["organiser_addr"] = _address(value)
            fields["organiser_name"] = _unescape(params.get("CN", ""))
        elif name in ("DTSTART", "DTEND"):
            stored, all_day, ok = _time(value, params, zones)
            resolved = resolved and ok
            fields["starts_at" if name == "DTSTART" else "ends_at"] = stored
            if name == "DTSTART":
                fields["all_day"] = all_day
        elif name == "DURATION":
            duration = _duration(value)
        elif name in ("SUMMARY", "DESCRIPTION", "LOCATION"):
            fields[name.lower()] = _unescape(value)
        elif name == "UID":
            fields["uid"] = value.strip()
        elif name == "SEQUENCE":
            fields["sequence"] = int(value) if value.strip().isdigit() else 0
        elif name == "STATUS":
            fields["status"] = value.strip().upper()
        elif name == "RRULE":
            fields["recurring"] = True

    starts = fields.get("starts_at", "")
    if not fields.get("ends_at") and starts:
        fields["ends_at"] = _end_from(starts, fields.get("all_day", False),
                                      duration)
    return Invitation(method=method or METHOD_REQUEST,
                      attendees=tuple(attendees), zone_unknown=not resolved,
                      raw=text or "", **fields)


def _end_from(starts_at: str, all_day: bool, duration) -> str:
    """The end RFC 5545 implies when none was given.

    A DATE start with no end lasts one day; a DATE-TIME start with no end and
    no duration is instantaneous. Both are the specification's answers rather
    than a guess, and an event of zero length is drawn as a marker rather than
    a block.
    """
    if all_day:
        day = dt.date.fromisoformat(starts_at[:10])
        return (day + (duration or dt.timedelta(days=1))).isoformat()
    when = dt.datetime.fromisoformat(starts_at)
    return (when + (duration or dt.timedelta(0))).replace(
        microsecond=0).isoformat()


# ------------------------------------------------------------------ writing
def _escape(value: str) -> str:
    return (str(value or "").replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _fold(line: str) -> str:
    """RFC 5545's 75-OCTET limit, which is not 75 characters.

    Folded on the encoded bytes, because a line of accented text is longer than
    it looks and a fold in the middle of a UTF-8 sequence produces mojibake in
    somebody else's client.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], b""
    for char in line:
        encoded = char.encode("utf-8")
        limit = 75 if not out else 74            # the continuation's leading space
        if len(chunk) + len(encoded) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += encoded
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def _stamp(when: dt.datetime | None = None) -> str:
    when = (when or dt.datetime.now(_UTC)).astimezone(_UTC)
    return when.strftime("%Y%m%dT%H%M%SZ")


def build_reply(invitation: Invitation, address: str, response: str, *,
                name: str = "", comment: str = "",
                now: dt.datetime | None = None) -> str:
    """An iTIP REPLY: this attendee's answer, and nothing else.

    ONE ATTENDEE LINE, WHICH IS THE SPECIFICATION AND ALSO THE SAFE THING. A
    reply carrying the whole guest list is a client asserting other people's
    answers, and organisers' clients have been known to believe it.
    """
    partstat = _PARTSTAT_OUT.get(response)
    if partstat is None:
        raise ValueError(f"{response!r} cannot be replied with")
    attendee = f"ATTENDEE;PARTSTAT={partstat}"
    if name:
        attendee += f';CN="{name}"'
    attendee += f":mailto:{address}"

    lines = [
        "BEGIN:VCALENDAR",
        f"PRODID:-//{APP_NAME}//{APP_NAME} {__version__}//EN",
        "VERSION:2.0",
        f"METHOD:{METHOD_REPLY}",
        "BEGIN:VEVENT",
        f"UID:{invitation.uid}",
        f"SEQUENCE:{invitation.sequence}",
        f"DTSTAMP:{_stamp(now)}",
    ]
    if invitation.organiser_addr:
        lines.append(f"ORGANIZER:mailto:{invitation.organiser_addr}")
    lines.append(attendee)
    if invitation.summary:
        lines.append(f"SUMMARY:{_escape(invitation.summary)}")
    if comment:
        lines.append(f"COMMENT:{_escape(comment)}")
    if invitation.starts_at:
        lines.append(_dt_line("DTSTART", invitation.starts_at,
                              invitation.all_day))
    if invitation.ends_at:
        lines.append(_dt_line("DTEND", invitation.ends_at, invitation.all_day))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _dt_line(name: str, value: str, all_day: bool) -> str:
    if all_day:
        return f"{name};VALUE=DATE:{value[:10].replace('-', '')}"
    return f"{name}:{_stamp(dt.datetime.fromisoformat(value))}"


def reply_subject(invitation: Invitation, response: str) -> str:
    """What the organiser's inbox shows. The words every client uses."""
    word = {RESPONSE_ACCEPTED: "Accepted", RESPONSE_TENTATIVE: "Tentative",
            RESPONSE_DECLINED: "Declined"}.get(response, "Reply")
    return f"{word}: {invitation.summary or '(no subject)'}"
