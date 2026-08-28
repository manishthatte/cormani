# SPDX-License-Identifier: GPL-3.0-or-later
#
# The demo data's calendars and events.
#
# Beside `fixtures.py` rather than in it, for the reason `calendarschema.py`
# sits beside `schema.py`: the 600-line rule, and the fact that this is one
# subject rather than an addition to another one. `fixtures.install` calls it
# and owns the transaction.
#
# WHY THIS EXISTS AT ALL. Stage 5 shipped with the calendar half of the demo
# store empty, so `--demo` opened a month grid that was correct and useless —
# and every judgement the interface asks to have made about it (do the
# per-calendar colours read at a glance, does a clash draw sensibly, is the
# "+3 more" overflow legible, does an all-day event land on the right day) is a
# claim about a calendar with something IN it. An empty grid tests nothing.
#
# DETERMINISTIC, IN THE SAME SENSE `fixtures.py` IS. No randomness, and every
# date is arithmetic on the base rather than on the clock. What it is NOT is
# zone-independent, and that is deliberate rather than an oversight: "the lab
# meeting, Tuesdays at ten" means ten o'clock where the person is, so the
# instant stored depends on the machine's zone, exactly as a real provider's
# would. All-day events are plain dates and do not move. Assert on what a view
# RENDERS, never on the stored text.
#
# THE WINDOW IS THE SYNC'S OWN, NOT A GENEROUS GUESS. `calendar.sync.window_for`
# is imported and used, so the demo calendars claim exactly the window a real
# first sync would have claimed. A second definition of "the window" here would
# be a second answer to drift from the first — and the number matters: a
# calendar whose recorded window does not reach the month being looked at makes
# `ui/calendarpane._footer` say "this range has not been fetched, press F5",
# which over demo data is advice that cannot be taken.
#
# RECURRENCE IS EXPANDED, BECAUSE THAT IS WHAT THE STORE HOLDS. Stage 5's first
# decision was that the server expands a rule and a row is one INSTANCE; this
# writes instances for the same reason, and the instance ids are shaped the way
# Google shapes them. It is also what keeps the demo useful for longer than a
# week: a fortnight of one-off events makes any view outside that fortnight
# empty again, whereas a weekly meeting fills every week of the window.
#
# THE GUESTS ARE THE ADDRESS BOOK'S PEOPLE. The addresses here are the ones in
# `fixtures._CONTACTS`, on purpose — an attendee chip that resolves to a
# contact is a different rendering from one that does not, and both need to be
# on screen. They are spelled again rather than imported because `fixtures`
# imports THIS module, and the other direction would be a cycle.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
import zlib

from . import calendars as calendars_repo
from . import events as events_repo
from . import eventqueue
from . import times
from .calendars import RESPONSE_ACCEPTED, RESPONSE_DECLINED
from .calendars import RESPONSE_NEEDS_ACTION, RESPONSE_TENTATIVE
from .database import utc_now
from .events import STATUS_CANCELLED, STATUS_CONFIRMED, STATUS_TENTATIVE

# (account index into fixtures._ACCOUNTS, key, remote id or "" to derive it,
#  name, colour, primary, writable, shown)
#
# `shown` is not all-on. Fifteen accounts with a calendar each, every one
# ticked, is a month grid nobody can read — and the rail's tick is a control
# that has to be exercised in both positions. The five shown are the five a
# person would actually keep open together.
_CALENDARS = (
    (0, "work", "", "manitLab — Manish", "#3377BB", 1, 1, 1),
    (0, "lab", "lab-bookings@manitlab.example", "Lab bookings", "#93A1A1", 0, 0, 1),
    (1, "nano", "", "nanoMani", "#AF6C00", 1, 1, 0),
    (2, "sapt", "", "Saptarang", "#CC1111", 1, 1, 1),
    (2, "programme", "saptarang-programme", "Programme", "#D33682", 0, 1, 1),
    (3, "photo", "", "Saptarang Photography", "#2AA198", 1, 1, 0),
    (4, "hitech", "", "Saptarang Hi-Tech", "#859900", 1, 1, 0),
    (5, "idlidu", "", "idlidu", "#268BD2", 1, 1, 1),
    (6, "idliltd", "", "idlidu Ltd", "#6C71C4", 1, 1, 0),
    (7, "personal", "", "Manish Thatte", "#D4A017", 1, 1, 1),
    (7, "holidays", "en.uk#holiday@group.v.calendar.google.com",
     "Holidays in the United Kingdom", "#586E75", 0, 0, 1),
    (7, "family", "family@group.v.calendar.example", "Family",
     "#DC322F", 0, 1, 0),
    (8, "hotmail", "", "Manish (hotmail)", "#CB4B16", 1, 1, 0),
    (9, "krishna", "", "Krishna", "#B58900", 1, 1, 0),
    (10, "krishnah", "", "Krishna (hotmail)", "#657B83", 1, 1, 0),
)

# The same people as `fixtures._CONTACTS`. See the header for why they are
# written out again rather than imported.
_LYLE = ("Lyle Gordon", "lyle.gordon@covalent.example")
_FRANCES = ("Frances Baker", "f.baker@m2016labs.co.uk")
_SARITA = ("Dr. Sarita Rane", "s.rane@iitb.ac.in")
_ANIL = ("Anil Kulkarni", "anil@saptarang-trust.org")
_PRIYA = ("Priya Deshpande", "priya@idlidu.example")
_MEERA = ("Meera Iyer", "meera.iyer@bharatnano.in")
_TOM = ("Tom Whitfield", "tom@northgateprint.co.uk")

# (calendar key, weekday 0=Monday, hour, minute, minutes, summary, location,
#  busy, reminder minutes or None)
#
# The spine of the demo calendar. Every week of the window has these in it,
# which is what stops a view outside the base fortnight being empty.
_WEEKLY = (
    ("work", 1, 10, 0, 60, "Lab meeting", "manitLab, room 2", 1, 10),
    ("work", 3, 16, 30, 45, "Ternary logic reading group", "Online", 1, 15),
    ("lab", 2, 9, 0, 240, "Raman bay booked", "manitLab, bay 3", 1, None),
    ("sapt", 4, 18, 0, 120, "Rehearsal", "The Hexagon", 1, 30),
    ("idlidu", 0, 9, 30, 30, "Stand-up", "Online", 1, 5),
    ("personal", 5, 7, 30, 60, "Swim", "Sports Club", 0, None),
    ("nano", 2, 15, 0, 30, "Production call", "Online", 1, None),
    ("hotmail", 4, 12, 0, 30, "Lunch", "", 0, None),
)

# (calendar key, day of the month, hour, minute, minutes, summary, location,
#  reminder minutes or None)
_MONTHLY = (
    ("work", 15, 14, 0, 90, "Grant report", "", 60),
    ("idliltd", 25, 11, 0, 60, "Payroll approval", "", 30),
    ("idlidu", 7, 10, 0, 45, "VAT and bookkeeping", "", 60),
    ("photo", 20, 17, 0, 60, "Print run review", "Northgate Print", None),
    ("krishna", 3, 19, 0, 60, "Family call", "", None),
)

# (calendar key, days from the base, length in days, summary). All-day, so a
# plain date at each end and no zone anywhere near it.
#
# Spread across the window rather than clustered at the base, so that paging
# forward a month finds something. The dates are offsets and not real
# anniversaries: this is demonstration data and a wrong Diwali would be a
# distraction rather than a defect.
_ALL_DAY = (
    ("holidays", -94, 1, "May Day"),
    ("holidays", -66, 1, "Spring bank holiday"),
    ("holidays", 6, 1, "Summer bank holiday"),
    ("holidays", 122, 1, "Christmas Day"),
    ("holidays", 123, 1, "Boxing Day"),
    ("holidays", 129, 1, "New Year's Day"),
    ("holidays", 234, 1, "Good Friday"),
    ("holidays", 293, 1, "May Day"),
    ("personal", 48, 8, "Bombay — IIT visit"),
    ("personal", -21, 2, "Long weekend"),
    ("hitech", 75, 4, "TechExpo, Mumbai"),
    ("sapt", 58, 2, "Diwali programme"),
    ("programme", 57, 1, "Get-in and technical rehearsal"),
    ("family", 33, 1, "Krishna's birthday"),
    ("family", 165, 1, "Anniversary"),
    ("photo", 96, 3, "Exhibition hang"),
    ("work", 201, 5, "Conference — Grenoble"),
)

# The one-off appointments, which is where the detail lives. A dict rather
# than another tuple because there are twelve properties and a tuple of twelve
# is a puzzle to read and a worse one to edit; the tables above are tuples
# because four of five columns are a number.
#
# `organiser` is None for the user's own event and a (name, address) pair for
# somebody else's — which is what makes it an INVITATION, and `store/events.py`
# says why the test is the organiser rather than the guest list.
_ONE_OFF = (
    {"calendar": "work", "days": 2, "at": (11, 0), "minutes": 45,
     "summary": "DWCNT wavelengths — call with Covalent",
     "location": "Online", "organiser": _LYLE, "response": RESPONSE_NEEDS_ACTION,
     "guests": (_LYLE, _FRANCES),
     "description": "Quantities and the two wavelengths, 1064 and 785. Lyle "
                    "will bring pricing against volume.",
     "reminder": 10},
    {"calendar": "work", "days": 5, "at": (15, 0), "minutes": 60,
     "summary": "Draft paper — sections 3 and 4",
     "location": "", "organiser": None, "response": "",
     "guests": (_FRANCES,),
     "description": "Frances has marked up sections 3 and 4. Nothing "
                    "structural.",
     "reminder": None},
    {"calendar": "work", "days": 9, "at": (9, 30), "minutes": 30,
     "summary": "Reading group — speaker slot",
     "location": "IIT Bombay, online", "organiser": _SARITA,
     "response": RESPONSE_TENTATIVE, "guests": (_SARITA,),
     "description": "September restart. Balanced ternary, forty minutes and "
                    "questions.",
     "reminder": 30},
    {"calendar": "work", "days": 3, "at": (11, 0), "minutes": 60,
     "summary": "Bharat Nano — supply review",
     "location": "Online", "organiser": _MEERA, "response": RESPONSE_ACCEPTED,
     "guests": (_MEERA,),
     "description": "Revised quotation, six-week lead time.",
     "reminder": None},
    {"calendar": "work", "days": 12, "at": (14, 0), "minutes": 30,
     "summary": "Cancelled — equipment demo", "location": "",
     "organiser": _LYLE, "response": RESPONSE_ACCEPTED, "guests": (_LYLE,),
     "description": "Called off by the supplier.", "reminder": None,
     "status": STATUS_CANCELLED},
    {"calendar": "sapt", "days": 4, "at": (19, 0), "minutes": 90,
     "summary": "Venue walk-through", "location": "The Hexagon",
     "organiser": _ANIL, "response": RESPONSE_ACCEPTED, "guests": (_ANIL,),
     "description": "Both evenings confirmed. Running order needed by the end "
                    "of the month.",
     "reminder": 60},
    {"calendar": "programme", "days": 1, "at": (17, 30), "minutes": 30,
     "summary": "Sound engineer — second rig?", "location": "",
     "organiser": _ANIL, "response": RESPONSE_NEEDS_ACTION, "guests": (_ANIL,),
     "description": "Decide whether the second rig is needed.",
     "reminder": None},
    {"calendar": "idlidu", "days": 3, "at": (9, 30), "minutes": 30,
     "summary": "Payroll — August", "location": "",
     "organiser": _PRIYA, "response": RESPONSE_NEEDS_ACTION,
     "guests": (_PRIYA,),
     "description": "August payroll ready for approval.", "reminder": 15},
    {"calendar": "idlidu", "days": 6, "at": (10, 0), "minutes": 60,
     "summary": "Supplier invoices — week 35", "location": "",
     "organiser": None, "response": "", "guests": (_PRIYA,),
     "description": "", "reminder": None},
    {"calendar": "idliltd", "days": 8, "at": (14, 0), "minutes": 30,
     "summary": "Companies House — accounts", "location": "",
     "organiser": None, "response": "", "guests": (), "description": "",
     "reminder": 1440},
    {"calendar": "personal", "days": 1, "at": (20, 0), "minutes": 120,
     "summary": "Dinner with Krishna", "location": "Ambrette",
     "organiser": None, "response": "", "guests": (), "description": "",
     "reminder": 60},
    {"calendar": "personal", "days": 7, "at": (8, 30), "minutes": 45,
     "summary": "Dentist", "location": "High Street", "organiser": None,
     "response": "", "guests": (), "description": "", "reminder": 120},
    {"calendar": "personal", "days": 14, "at": (18, 30), "minutes": 60,
     "summary": "Declined — supplier drinks", "location": "",
     "organiser": _TOM, "response": RESPONSE_DECLINED, "guests": (_TOM,),
     "description": "", "reminder": None},
    {"calendar": "photo", "days": 10, "at": (11, 0), "minutes": 90,
     "summary": "Print sizes for the Diwali set", "location": "Photocircle",
     "organiser": None, "response": "", "guests": (), "description": "",
     "reminder": None},
    {"calendar": "hitech", "days": 11, "at": (13, 0), "minutes": 60,
     "summary": "Stand floor plan", "location": "Online", "organiser": None,
     "response": "", "guests": (), "description": "", "reminder": None},
    {"calendar": "work", "days": 3, "at": (9, 0), "minutes": 30,
     "summary": "Funder report — weekly", "location": "", "organiser": None,
     "response": "", "guests": (), "description": "", "reminder": None},
    {"calendar": "work", "days": 3, "at": (13, 0), "minutes": 45,
     "summary": "Interview — postdoc", "location": "manitLab, room 2",
     "organiser": None, "response": "", "guests": (_SARITA, _FRANCES),
     "description": "Second of three. Panel of two.", "reminder": 15},
    {"calendar": "lab", "days": 3, "at": (14, 0), "minutes": 120,
     "summary": "Bay 3 — Bharat Nano samples", "location": "manitLab, bay 3",
     "organiser": _MEERA, "response": RESPONSE_ACCEPTED, "guests": (_MEERA,),
     "description": "", "reminder": None},
    {"calendar": "sapt", "days": 3, "at": (12, 0), "minutes": 60,
     "summary": "Programme printing — quote", "location": "Northgate Print",
     "organiser": _TOM, "response": RESPONSE_NEEDS_ACTION, "guests": (_TOM,),
     "description": "Bring the running order.", "reminder": 30},
    {"calendar": "personal", "days": 3, "at": (16, 0), "minutes": 30,
     "summary": "Bank", "location": "High Street", "organiser": None,
     "response": "", "guests": (), "description": "", "reminder": None},
    {"calendar": "nano", "days": 2, "at": (16, 0), "minutes": 45,
     "summary": "Datasheet proof", "location": "", "organiser": _TOM,
     "response": RESPONSE_ACCEPTED, "guests": (_TOM,),
     "description": "The bleed on page 2.", "reminder": None,
     "status": STATUS_TENTATIVE},
)

# One event the user made while offline, so that the "not yet sent" marker on a
# row and the queue behind it are both on screen. Sync is disabled over demo
# data, so it stays that way, which is exactly the state being demonstrated.
_LOCAL_EVENT = {"calendar": "personal", "days": 4, "at": (17, 0),
                "minutes": 30, "summary": "Collect prints",
                "location": "Northgate Print"}

# A clash, deliberately: two events at the same hour on the same day in two
# DIFFERENT calendars, which is the case a week grid draws worst and the one
# per-calendar colour exists for.
_CLASH = {"calendar": "personal", "days": 2, "at": (11, 0), "minutes": 60,
          "summary": "Plumber", "location": "Home"}


def _opaque(seed: str) -> str:
    """A Microsoft-shaped calendar id: opaque, and never parsed.

    crc32 and not hash(), for the reason `fixtures._insert` gives — Python
    salts string hashing per process, and an id that changed on every install
    would quietly break the determinism this module promises.
    """
    return f"AAMkA{zlib.crc32(seed.encode('utf-8')):08x}GgAAAA=="


def _remote_id(spec_id: str, address: str, provider: str) -> str:
    """What the provider would have called this calendar.

    Google's primary calendar id IS the address and Graph's is opaque, which
    the schema records and nothing downstream parses. Reproduced here for the
    reason `fixtures._FOLDERS` reproduces Gmail's bracketed folder paths: a
    rail that looks right against tidy names and wrong against real ones has
    tested nothing.
    """
    if spec_id:
        return spec_id
    return address if provider == "google" else _opaque(address)


def _instance_id(series: str, when: dt.datetime, *, all_day: bool = False) -> str:
    """Google's own instance-id shape: the series, then the start.

    Written out because a recurring event's remote id is not the series' id,
    and the store's UNIQUE(calendar_id, remote_id) is what makes that matter.
    """
    if all_day:
        return f"{series}_{when.strftime('%Y%m%d')}"
    return f"{series}_{when.astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _series_id(key: str, summary: str) -> str:
    return f"s{zlib.crc32(f'{key}|{summary}'.encode('utf-8')):08x}"


def _guest_fields(guests, organiser, self_address: str) -> list:
    """The attendee rows for one event, the user included.

    The user is always on the list when there is a list at all, and is marked
    `is_self` — which is what `Event.organiser_is_self` and therefore
    `is_invitation` turn on. An event with guests that does not include the
    person whose calendar it is would be a shape no provider produces.
    """
    if not guests and not organiser:
        return []
    rows = [{"address": self_address, "name": "", "is_self": True,
             "is_organiser": organiser is None,
             "response": RESPONSE_ACCEPTED if organiser is None else ""}]
    for name, address in guests:
        rows.append({"name": name, "address": address,
                     "is_organiser": bool(organiser and organiser[1] == address),
                     "response": RESPONSE_ACCEPTED})
    return rows


def _timed(con: sqlite3.Connection, calendar_id: int, remote_id: str,
           start: dt.datetime, minutes: int, *, summary: str,
           self_address: str, series: str = "", location: str = "",
           description: str = "", organiser=None, response: str = "",
           guests=(), busy: bool = True, reminder: int | None = None,
           status: str = STATUS_CONFIRMED, updated_at: str = "") -> int:
    """One appointment. An INSTANT, so UTC text at both ends."""
    end = start + dt.timedelta(minutes=minutes)
    fields = {
        "series_id": series,
        "ical_uid": f"{series or remote_id}@cormani.demo",
        "etag": f'W/"{zlib.crc32(remote_id.encode("utf-8")):08x}"',
        "summary": summary, "description": description, "location": location,
        "starts_at": times.to_utc_text(start), "ends_at": times.to_utc_text(end),
        "all_day": False, "status": status, "busy": busy,
        "organiser_name": organiser[0] if organiser else "",
        "organiser_addr": organiser[1] if organiser else "",
        "my_response": response, "recurring": bool(series),
        "web_link": f"https://calendar.example/event/{remote_id}",
        "reminder": reminder, "updated_at": updated_at or utc_now()}
    return events_repo.upsert(
        con, calendar_id, remote_id, fields, commit=False,
        attendees=_guest_fields(guests, organiser, self_address))


def _all_day(con: sqlite3.Connection, calendar_id: int, remote_id: str,
             first: dt.date, length: int, summary: str) -> int:
    """One all-day event. A DATE, and the end is EXCLUSIVE.

    Both providers make it exclusive and so does the store; `events.by_day`
    carries the note about what happens when it is not, which is a one-day
    event drawn across two.
    """
    fields = {"summary": summary, "starts_at": first.isoformat(),
              "ends_at": (first + dt.timedelta(days=length)).isoformat(),
              "all_day": True, "status": STATUS_CONFIRMED, "busy": False,
              "ical_uid": f"{remote_id}@cormani.demo",
              "updated_at": utc_now()}
    return events_repo.upsert(con, calendar_id, remote_id, fields, commit=False)


def _months(first: dt.date, last: dt.date):
    """Every (year, month) from one date to another, inclusive."""
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def install(con: sqlite3.Connection, account_ids: list, accounts_spec,
            base: dt.datetime) -> dict:
    """Calendars and events for the demo store. Does not commit.

    `fixtures.install` owns the transaction, so nothing here commits: a demo
    store half-written because the calendar half raised would be worse than no
    demo store, and the caller already refuses to run over real data.
    """
    from ..calendar.sync import window_for      # the window's one definition

    local_base = times.aware(base).astimezone(times.local_zone())
    from_utc, to_utc = window_for(local_base)
    first = times.to_local(from_utc).date()
    last = times.to_local(to_utc).date() - dt.timedelta(days=1)

    calendar_ids, addresses = {}, {}
    for idx, key, spec_id, name, colour, primary, writable, shown in _CALENDARS:
        address, provider = accounts_spec[idx][0], accounts_spec[idx][1]
        calendar_id = calendars_repo.ensure_calendar(
            con, account_ids[idx], _remote_id(spec_id, address, provider),
            name=name, colour=colour, timezone="Asia/Kolkata",
            is_primary=bool(primary), writable=bool(writable),
            default_reminder=10 if primary else None, commit=False)
        if not shown:
            calendars_repo.set_shown(con, calendar_id, False, commit=False)
        calendar_ids[key] = calendar_id
        addresses[key] = address

    written = _write_events(con, calendar_ids, addresses, local_base,
                            first, last)

    for key, calendar_id in calendar_ids.items():
        # The window a real first sync would have recorded, and a token, so
        # that no view over demo data says "this range has not been fetched,
        # press F5" — which over demo data is advice that cannot be taken.
        calendars_repo.record_sync_state(
            con, calendar_id, sync_token=f"demo-{_series_id(key, 'token')}",
            synced_from=from_utc, synced_to=to_utc,
            last_synced_at=times.to_utc_text(local_base), commit=False)

    return {"calendars": len(calendar_ids), "events": written,
            "from": first.isoformat(), "to": last.isoformat()}


def _write_events(con: sqlite3.Connection, calendar_ids: dict,
                  addresses: dict, base: dt.datetime, first: dt.date,
                  last: dt.date) -> int:
    written = 0
    written += _write_weekly(con, calendar_ids, addresses, base, first, last)
    written += _write_monthly(con, calendar_ids, addresses, base, first, last)
    written += _write_all_day(con, calendar_ids, base)
    written += _write_one_off(con, calendar_ids, addresses, base)
    written += _write_local(con, calendar_ids, addresses, base)
    return written


def _write_weekly(con, calendar_ids, addresses, base, first, last) -> int:
    """Every instance of every weekly meeting, across the whole window."""
    written = 0
    for key, weekday, hour, minute, minutes, summary, where, busy, remind \
            in _WEEKLY:
        series = _series_id(key, summary)
        day = first + dt.timedelta(days=(weekday - first.weekday()) % 7)
        while day <= last:
            start = dt.datetime.combine(day, dt.time(hour, minute),
                                        base.tzinfo)
            _timed(con, calendar_ids[key], _instance_id(series, start), start,
                   minutes, summary=summary, self_address=addresses[key],
                   series=series, location=where, busy=bool(busy),
                   reminder=remind)
            written += 1
            day += dt.timedelta(days=7)
    return written


def _write_monthly(con, calendar_ids, addresses, base, first, last) -> int:
    """The monthly ones, skipping a month that is too short for the day."""
    written = 0
    for key, dom, hour, minute, minutes, summary, where, remind in _MONTHLY:
        series = _series_id(key, summary)
        for year, month in _months(first, last):
            try:
                day = dt.date(year, month, dom)
            except ValueError:                   # the 31st of a 30-day month
                continue
            if not first <= day <= last:
                continue
            start = dt.datetime.combine(day, dt.time(hour, minute), base.tzinfo)
            _timed(con, calendar_ids[key], _instance_id(series, start), start,
                   minutes, summary=summary, self_address=addresses[key],
                   series=series, location=where, reminder=remind)
            written += 1
    return written


def _write_all_day(con, calendar_ids, base) -> int:
    written = 0
    for key, days, length, summary in _ALL_DAY:
        day = base.date() + dt.timedelta(days=days)
        _all_day(con, calendar_ids[key],
                 f"d{_series_id(key, f'{summary}|{days}')}", day, length,
                 summary)
        written += 1
    return written


def _write_one_off(con, calendar_ids, addresses, base) -> int:
    written = 0
    for spec in _ONE_OFF:
        key = spec["calendar"]
        # The offset is in the key as well as the summary: two entries may
        # legitimately share a name, and an id that did not tell them apart
        # would make the second silently replace the first through the
        # upsert's UNIQUE(calendar_id, remote_id).
        summary_key = f"{spec['summary']}|{spec['days']}"
        hour, minute = spec["at"]
        start = dt.datetime.combine(
            base.date() + dt.timedelta(days=spec["days"]),
            dt.time(hour, minute), base.tzinfo)
        _timed(con, calendar_ids[key],
               f"o{_series_id(key, summary_key)}", start, spec["minutes"],
               summary=spec["summary"], self_address=addresses[key],
               location=spec["location"], description=spec["description"],
               organiser=spec["organiser"], response=spec["response"],
               guests=spec["guests"], reminder=spec["reminder"],
               status=spec.get("status", STATUS_CONFIRMED))
        written += 1

    key = _CLASH["calendar"]
    hour, minute = _CLASH["at"]
    start = dt.datetime.combine(base.date() + dt.timedelta(days=_CLASH["days"]),
                                dt.time(hour, minute), base.tzinfo)
    _timed(con, calendar_ids[key], f"c{_series_id(key, _CLASH['summary'])}",
           start, _CLASH["minutes"], summary=_CLASH["summary"],
           self_address=addresses[key], location=_CLASH["location"])
    return written + 1


def _write_local(con, calendar_ids, addresses, base) -> int:
    """The event made offline: a local id, a queue entry, and the row marked.

    All three, because any one of them alone is a state the application cannot
    reach on its own — a local id with no op is an event that will never be
    sent, and an op with no marker is a row the list draws as though it had
    been.
    """
    key = _LOCAL_EVENT["calendar"]
    hour, minute = _LOCAL_EVENT["at"]
    start = dt.datetime.combine(
        base.date() + dt.timedelta(days=_LOCAL_EVENT["days"]),
        dt.time(hour, minute), base.tzinfo)
    # `events.new_local_id` is a uuid4 and is right for the application and
    # wrong here: an id that changed on every install would break the one
    # promise this module makes. The PREFIX is what carries the meaning — it is
    # the mark of an id no provider can produce — so the prefix is taken from
    # the store and only the tail is made deterministic.
    remote_id = f"{events_repo.LOCAL_PREFIX}{_series_id(key, 'offline')}"
    event_id = _timed(con, calendar_ids[key], remote_id, start,
                      _LOCAL_EVENT["minutes"], summary=_LOCAL_EVENT["summary"],
                      self_address=addresses[key],
                      location=_LOCAL_EVENT["location"])
    eventqueue.enqueue(con, calendar_ids[key], "create", event_id=event_id,
                       remote_id=remote_id, commit=False)
    events_repo.set_pending(con, [event_id], commit=False)
    return 1
