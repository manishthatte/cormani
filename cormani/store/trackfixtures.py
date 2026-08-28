# SPDX-License-Identifier: GPL-3.0-or-later
#
# The demo data's tracked threads and their timelines.
#
# Beside `fixtures.py` and `calendarfixtures.py`, for the reason both of those
# are beside each other: one subject, and a 600-line rule that has been right
# every time it fired. `fixtures.install` calls it and owns the transaction.
#
# ── WHAT AN EMPTY TRACKING TAB WOULD DEMONSTRATE ───────────────────────────
#
# Nothing, and the calendar taught that lesson at a cost: stage 5 shipped with
# no demo events and the month grid was correct and useless. Every judgement
# the tracking pane asks to have made — does the board's order put the right
# thing first, do three kinds of attention read differently at a glance, is a
# cross-channel timeline legible — is a claim about a board with something in
# it.
#
# ── THE TIMELINES ARE BUILT FROM THE DEMO'S OWN MAIL ───────────────────────
#
# `store/attach.py`'s matchers are run over the messages `fixtures.py` already
# wrote, rather than touches being invented here. Three things fall out of that
# and all three are the reason: the demo exercises the REAL filing code and not
# a second implementation of it; a timeline row opens the message behind it,
# because there is one; and if a matcher breaks, the demo board goes visibly
# thin, which is a test nobody had to write.
#
# What IS invented is everything a mailbox cannot contain: the phone calls, the
# notes, the meetings. That is the whole point of the layer — PLAN.txt §2 asks
# for log-a-call by name, because the channels that leave no trace are the ones
# that make a correspondent look silent when the matter was settled.
#
# ── THE SEVEN THREADS ARE CHOSEN TO BE SEVEN DIFFERENT SHAPES ──────────────
#
# One owed for days. One with a statutory deadline coming. One with a deadline
# already PASSED, because a missed date is the most important row on the board
# and the easiest to filter out by accident. One blocked, to show that being
# blocked silences a nudge and not a deadline. One that crossed from email to
# telephone, so the board says nothing is owed although the mail says it is.
# One closed, so the board is not only live rows. And one with nothing on it at
# all, because a thread made this morning is a real state and it must not read
# as overdue.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3

from . import attach as attach_repo
from . import contacts as contacts_repo
from . import times
from . import touches as touches_repo
from . import tracking as tracking_repo

# (key, title, org, track, state, priority, cadence, days since the thread
#  began, next action)
#
# THE DAY EACH BEGAN IS DATA AND NOT A DETAIL. `store/attach.py`'s second
# matcher bounds itself by the thread's first touch and falls back to when the
# thread was made, so a fixture whose threads all began this morning would file
# none of the mail that led to them — and the demo would be a board of empty
# timelines, which is the defect the calendar's fixtures already taught.
_THREADS = (
    ("dwcnt", "DWCNT wavelengths and pricing", "Covalent Example",
     "supplier", tracking_repo.STATE_AWAITING, 2, 7, 30,
     "Chase the quotation for 1064 and 785"),
    ("vat", "VAT return, period ending 31 July", "idlidu",
     "statutory", tracking_repo.STATE_OPEN, 1, 7, 20,
     "File online before the deadline"),
    ("confirmation", "Confirmation statement", "idlidu Ltd",
     "statutory", tracking_repo.STATE_OPEN, 1, 14, 25,
     "File at Companies House"),
    ("diwali", "Diwali programme", "Saptarang Trust",
     "correspondence", tracking_repo.STATE_BLOCKED, 2, 7, 14,
     "Waiting on the hall's insurance certificate"),
    # A seven-day cadence against eleven days of silence, so the board has a
    # NUDGE row — the third kind of attention, and the one that is invisible in
    # a demo where every thread is either owed or has a deadline.
    ("reading", "Ternary logic reading group", "IIT Bombay",
     "correspondence", tracking_repo.STATE_REPLIED, 3, 7, 40,
     "Send the September date"),
    ("datasheet", "nanoMani datasheet print run", "Northgate Print",
     "supplier", tracking_repo.STATE_CLOSED, 4, 14, 60, ""),
    ("stand", "TechExpo stand", "idlidu Ltd", "customer",
     tracking_repo.STATE_OPEN, 3, 7, 21, "Confirm the floor plan"),
    # NOBODY IS ON THIS ONE AND NOTHING HAS HAPPENED FOR THREE WEEKS, which is
    # the third kind of attention: not owed, no deadline, simply gone quiet. A
    # seven-day cadence against twenty days of silence is what makes the board
    # say so without anybody having typed a date — and a demo with no such row
    # never draws the nudge at all.
    ("supply", "Nanotube supply — revised lead time", "Bharat Nano",
     "supplier", tracking_repo.STATE_AWAITING, 3, 7, 90,
     "Chase the six-week lead time in writing"),
)

# (thread key, contact name, address, role on the thread)
#
# FRANCES BAKER IS DELIBERATELY NOT HERE although the DWCNT exchange mentions
# her. `fixtures._FILLER_SENDERS` uses her address for the hundred and sixty
# generated messages that make the list long enough to scroll, so putting her
# on a thread makes matcher 2 file every one of them — a twenty-one-row
# timeline of "Weekly digest #37" where the story is four rows. Which is the
# matcher working exactly as designed, and a demonstration of nothing.
_PEOPLE = (
    ("dwcnt", "Lyle Gordon", "lyle.gordon@covalent.example", "primary"),
    ("vat", "Priya Deshpande", "priya@idlidu.example", "primary"),
    ("confirmation", "Registrar", "enquiries@companieshouse.gov.uk", ""),
    ("diwali", "Anil Kulkarni", "anil@saptarang-trust.org", "primary"),
    ("reading", "Dr. Sarita Rane", "s.rane@iitb.ac.in", "primary"),
    ("datasheet", "Tom Whitfield", "tom@northgateprint.co.uk", "primary"),
    ("stand", "Priya Deshpande", "priya@idlidu.example", "primary"),
)

# (thread key, days before the base, deadline note)
_DEADLINES = (
    ("vat", -12, "Statutory. A penalty applies from the following day."),
    ("confirmation", -19, "Companies House. Late filing is an offence."),
    # Already gone, and still on the board. This is the row the whole ordering
    # exists for: a missed statutory date must not be filtered out by a query
    # that only looks forward.
    ("stand", 4, "The exhibitor deadline has passed — ring them."),
)

# What a mailbox cannot contain. (thread key, days ago, channel, direction,
# summary, body)
_CALLS = (
    ("dwcnt", 2, "phone", "out", "Rang Lyle; he will send pricing on Friday",
     "Both wavelengths quoted against volume. He asked for the quantity "
     "bands again."),
    ("vat", 6, "phone", "in", "Priya rang; the bookkeeping is finished",
     ""),
    ("diwali", 3, "whatsapp", "in", "Anil: the hall wants a certificate",
     "Blocked until the insurer answers."),
    ("reading", 11, "meeting", "note", "Reading group, September session",
     "Agreed to speak on balanced ternary in October."),
    ("datasheet", 30, "phone", "out", "Approved the proof over the telephone",
     ""),
    ("supply", 20, "phone", "out", "Rang Meera; she will confirm in writing",
     "Six weeks quoted verbally. Nothing since."),
)

# (thread key, days ago, text). A note answers nobody and must never discharge
# what is owed — `store/touches.add_note` is where that is enforced.
_NOTES = (
    ("dwcnt", 1, "If Friday passes with nothing, try Frances instead."),
    ("confirmation", 2, "The filing code is in the safe, not in the keyring."),
    ("stand", 5, "The floor plan puts us next to the entrance this year."),
)


def _iso(base: dt.datetime, days_ago: int, hour: int = 11) -> str:
    when = (base - dt.timedelta(days=days_ago)).replace(
        hour=hour, minute=(hour * 11) % 60, second=0, microsecond=0)
    return times.to_utc_text(when)


def install(con: sqlite3.Connection, base: dt.datetime) -> dict:
    """Threads, people, timelines. Does not commit — `fixtures.install` owns
    the transaction, for the reason it owns the calendar's."""
    local_base = times.aware(base).astimezone(times.local_zone())
    ids = {}
    for key, title, org, track, state, priority, cadence, began, action \
            in _THREADS:
        ids[key] = tracking_repo.create_thread(
            con, title, org=org, track=track, state=state, priority=priority,
            cadence_days=cadence, next_action=action,
            created_at=_iso(local_base, began, hour=9), commit=False)

    for key, name, address, role in _PEOPLE:
        contact = contacts_repo.contact_for_address(con, address, name=name,
                                                    create=True, commit=False)
        if contact is not None:
            tracking_repo.link_contact(con, ids[key], contact.id, role=role,
                                       commit=False)

    for key, days, note in _DEADLINES:
        tracking_repo.update_thread(
            con, ids[key], commit=False,
            deadline_date=(local_base.date()
                           + dt.timedelta(days=-days)).isoformat(),
            deadline_note=note)

    # The mail first, through the REAL matchers, so that the demo's timelines
    # are the ones the application would have built. `wrote_to` has to exist
    # before they run — the address matcher is scoped by it.
    attach_repo.rebuild_wrote_to(con, commit=False)
    filed = attach_repo.run(con, commit=False)

    written = 0
    for key, days, channel, direction, summary, body in _CALLS:
        touches_repo.add_touch(
            con, ids[key], channel=channel, direction=direction,
            occurred_at=_iso(local_base, days), subject=summary, body=body,
            source=touches_repo.SOURCE_LOGGED, commit=False)
        written += 1
    for key, days, text in _NOTES:
        touches_repo.add_note(con, ids[key], text,
                              occurred_at=_iso(local_base, days, hour=17),
                              commit=False)
        written += 1

    # Counted, not forgotten: `fixtures.install` reports what it installed and
    # a report that is one short is a report nobody can check against.
    written += _make_one_owed(con, ids, local_base)
    return {"threads": len(ids), "filed": filed.total, "logged": written}


def _make_one_owed(con: sqlite3.Connection, ids: dict,
                   base: dt.datetime) -> int:
    """Guarantee the board has an owed row, and one that is genuinely owed.

    The demo's mail is fixed and the matchers may file it in any order, so
    whether anything ends up owed is otherwise an accident of the fixtures. One
    inbound touch with nothing after it is the smallest honest way to make the
    state exist — and it is a real touch on a real thread rather than a flag,
    because `owed` is DERIVED and there is no flag to set.
    """
    touches_repo.add_touch(
        con, ids["dwcnt"], channel=touches_repo.CHANNEL_EMAIL, direction="in",
        occurred_at=_iso(base, 1, hour=9),
        subject="Re: DWCNT wavelengths — one more question",
        body="Before I quote: do you need the 785 at the same quantity?",
        source=touches_repo.SOURCE_LOGGED, ext_id="<demo-owed@cormani>",
        status=touches_repo.STATUS_RECEIVED, commit=False)
    return 1
