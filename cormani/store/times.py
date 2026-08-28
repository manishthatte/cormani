# SPDX-License-Identifier: GPL-3.0-or-later
#
# Instants, dates, and the difference — which is the whole of calendar work.
#
# The store keeps UTC, as everywhere else. A calendar is the one part of this
# application where that is not enough, because a calendar holds two kinds of
# thing that look alike and are not:
#
# AN APPOINTMENT IS AN INSTANT. "The call, at 15:00" is one moment; it is 09:30
# in London and 15:00 here, and moving the laptop does not move the call.
#
# AN ALL-DAY EVENT IS A DATE. "Diwali, the 8th" is the 8th in Bombay and the
# 8th in Berlin. Storing it as an instant — midnight of the 8th, UTC — makes it
# 05:30 on the 8th here and 19:00 on the 7th in Los Angeles, so half the world
# sees it on the wrong day. Both providers spell this distinction the same way,
# with a plain YYYY-MM-DD where a timed event has a timestamp, and this module
# is where corMani keeps it.
#
# EVERY RANGE QUESTION THEREFORE HAS TWO ANSWERS. `window` returns both: the
# pair of UTC instants a timed row is compared against, and the pair of plain
# dates an all-day row is. `store/events.py` uses both in one query, and the
# module header there records what happens when only the first is used.
#
# The local zone is read at the moment it is needed rather than captured at
# import: a laptop crosses zones, and a value frozen when the process started
# would draw the wrong week until it was restarted.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

UTC = dt.timezone.utc

# Monday. `calendar.setfirstweekday` is the standard library's answer and it is
# a process-wide global, which is the wrong shape for a setting; the views take
# this as a parameter and it is only the default.
FIRST_WEEKDAY = 0


def local_zone() -> dt.tzinfo:
    """The machine's own zone, as a fixed offset for the moment it is asked.

    `astimezone()` with no argument is the standard library's way of getting
    the system zone without a database name, which matters on Windows where
    there is no /etc/localtime. It resolves to a fixed offset, so the value is
    correct for now and is not asked to answer questions about next March —
    which is why nothing here holds one for long.
    """
    return dt.datetime.now().astimezone().tzinfo or UTC


def now_local() -> dt.datetime:
    return dt.datetime.now(local_zone())


def aware(when: dt.datetime, tz: dt.tzinfo | None = None) -> dt.datetime:
    """A datetime that is definitely aware. Naive is read as local."""
    return when if when.tzinfo is not None else when.replace(
        tzinfo=tz or local_zone())


def to_utc_text(when: dt.datetime) -> str:
    """The store's format: ISO-8601 UTC, to the second."""
    return aware(when).astimezone(UTC).replace(microsecond=0).isoformat()


def parse(value: str, tz: dt.tzinfo | None = None) -> dt.datetime | None:
    """A stored value as an aware datetime, whichever of the two kinds it is.

    An all-day value is midnight LOCAL of that date, because that is where a
    week grid must draw it. Callers that need to know which kind they had ask
    the row's `all_day`, not this.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(text[:10]),
                                       dt.time(0, 0), tz or local_zone())
        except ValueError:
            return None
    if when.tzinfo is None:
        # A date parsed by fromisoformat — "2026-09-12" — arrives naive at
        # midnight, and it is a DATE, so it is local midnight and not UTC.
        return when.replace(tzinfo=tz or local_zone())
    return when


def parse_date(value: str) -> dt.date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def to_local(value: str, tz: dt.tzinfo | None = None) -> dt.datetime | None:
    when = parse(value, tz)
    return when.astimezone(tz or local_zone()) if when else None


# ----------------------------------------------------------------- windows
def window(start: dt.datetime, end: dt.datetime) -> tuple[str, str, str, str]:
    """The four bounds a range query needs: two instants, then two dates.

    The dates are taken from the LOCAL ends of the window, not from the UTC
    ones, and that is the entire point of this function. At UTC+05:30 a local
    day begins at 18:30 UTC the day before, so the UTC bounds of "Saturday"
    name Friday — and an all-day event compared against them lands on the wrong
    day for every user east of Greenwich and, in the other direction, for every
    user west of it.
    """
    start, end = aware(start), aware(end)
    return (to_utc_text(start), to_utc_text(end),
            start.date().isoformat(), end.date().isoformat())


def day_bounds(day: dt.date, tz: dt.tzinfo | None = None) -> tuple:
    tz = tz or local_zone()
    start = dt.datetime.combine(day, dt.time(0, 0), tz)
    return start, start + dt.timedelta(days=1)


def week_start(day: dt.date, first_weekday: int = FIRST_WEEKDAY) -> dt.date:
    return day - dt.timedelta(days=(day.weekday() - first_weekday) % 7)


def week_bounds(day: dt.date, *, first_weekday: int = FIRST_WEEKDAY,
                tz: dt.tzinfo | None = None) -> tuple:
    tz = tz or local_zone()
    start = dt.datetime.combine(week_start(day, first_weekday), dt.time(0, 0), tz)
    return start, start + dt.timedelta(days=7)


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def month_end(day: dt.date) -> dt.date:
    """The first day of the NEXT month. Exclusive, as every end here is."""
    return (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def month_bounds(day: dt.date, tz: dt.tzinfo | None = None) -> tuple:
    tz = tz or local_zone()
    return (dt.datetime.combine(month_start(day), dt.time(0, 0), tz),
            dt.datetime.combine(month_end(day), dt.time(0, 0), tz))


def month_grid(day: dt.date, *, first_weekday: int = FIRST_WEEKDAY) -> tuple:
    """The bounds a MONTH VIEW draws, which are not the month's.

    A month grid shows whole weeks, so it begins in the previous month and ends
    in the next one. Six rows always, rather than five or six: a grid whose
    height changes as the user pages through the year makes every row jump, and
    the alternative — cells that get shorter in a five-week month — is worse.
    """
    start = week_start(month_start(day), first_weekday)
    return start, start + dt.timedelta(days=42)


def days_between(start: dt.datetime, end: dt.datetime) -> list:
    """Every local date the window touches, first to last."""
    first, last = aware(start).date(), aware(end).date()
    span = (last - first).days
    if aware(end).time() == dt.time(0, 0):
        span -= 1                    # an exclusive end at midnight is not a day
    return [first + dt.timedelta(days=n) for n in range(max(span + 1, 1))]
