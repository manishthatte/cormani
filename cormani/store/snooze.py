# SPDX-License-Identifier: GPL-3.0-or-later
#
# Snooze: hide a message until a time, then bring it back.
#
# THE OUTLOOK MODEL. A snoozed message stays in its folder and stays indexed;
# it simply does not appear in any view until `snooze_until` passes. That is
# different from the tracking layer's deadlines, which are about correspondence
# the user is pursuing, not mail they want out of the inbox for an hour.
#
# TIMES ARE UTC ISO STRINGS, like everywhere else in the store. Comparison is
# lexical and therefore chronological because every value is written in one
# shape — seconds, `+00:00` — by `store/times.to_utc_text`.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Sequence

from . import times


def active_snooze_clause(*, now: dt.datetime | None = None) -> tuple[str, list]:
    """A WHERE fragment excluding messages still snoozed.

    Empty `snooze_until` means not snoozed; a time in the past means the snooze
    has expired and the row is visible again even before `clear_expired` runs.
    """
    now_iso = times.to_utc_text(now or dt.datetime.now(dt.timezone.utc))
    return ("(m.snooze_until = '' OR m.snooze_until <= ?)", [now_iso])


def snooze(con: sqlite3.Connection, message_ids: Sequence[int],
           until: str) -> int:
    """Snooze these messages until `until`, an ISO-8601 UTC timestamp."""
    if not message_ids:
        return 0
    marks = ",".join("?" * len(message_ids))
    with con:
        cur = con.execute(
            f"UPDATE message SET snooze_until = ? WHERE id IN ({marks})",
            [until, *message_ids])
    return cur.rowcount


def clear_expired(con: sqlite3.Connection,
                  *, now: dt.datetime | None = None) -> int:
    """Clear snooze_until on messages whose time has passed."""
    now_iso = times.to_utc_text(now or dt.datetime.now(dt.timezone.utc))
    with con:
        cur = con.execute(
            "UPDATE message SET snooze_until = '' "
            "WHERE snooze_until <> '' AND snooze_until <= ?",
            [now_iso])
    return cur.rowcount
