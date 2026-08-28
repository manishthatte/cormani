# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a finished sync should say out loud.
#
# THE ENGINE ALREADY KNOWS WHAT NOT TO ANNOUNCE. `AccountResult.arrived` is the
# Inbox rows this pass created, and `filtered.quiet_ids()` is the subset a rule
# filed, marked read or asked to silence — written for this caller before this
# caller existed. What remains is the mail somebody has not already dealt with
# by writing a filter, and that is what a notification is for.
#
# ONE NOTIFICATION PER SYNC, NEVER ONE PER MESSAGE. Fifteen accounts can bring
# in dozens of messages at once, and a cascade of balloons is worse than none:
# people turn them off. The body lists a few subjects and then a count.
#
# A SYNC THAT IS STILL FETCHING DOES NOT ANNOUNCE. `remaining > 0` means the
# next F5 will bring more of the same mailbox, and announcing mid-import turns
# a first sync into a parade. The status bar already says how many are left.
#
# AND THE WINDOW BEING LOOKED AT IS ENOUGH. A reminder that fires while the
# person is reading the list is noise about something they can already see.
# `platform/notify.py` reports whether it could SEND; this reports whether it
# should have tried.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from typing import Sequence

from .. import APP_NAME
from ..platform import notify as notify_mod
from ..store import messages as messages_repo

# How many subjects to name before the body becomes a count. Three is what fits
# a notification without scrolling on every desktop that has been measured.
PREVIEW = 3


def announceable(results: Sequence) -> list[int]:
    """Message ids that arrived and were not silenced or filed by a filter."""
    ids: list[int] = []
    for result in results or ():
        if not getattr(result, "ok", False):
            continue
        arrived = list(getattr(result, "arrived", ()) or ())
        quiet = set()
        filtered = getattr(result, "filtered", None)
        if filtered is not None:
            quiet = set(filtered.quiet_ids())
        ids.extend(mid for mid in arrived if mid not in quiet)
    return ids


def still_fetching(results: Sequence) -> bool:
    """True when any account has more mail waiting on the server."""
    return any(getattr(r, "remaining", 0) for r in (results or ()))


def describe(con: sqlite3.Connection, message_ids: Sequence[int]) -> tuple:
    """(title, body) for one notification about these messages."""
    ids = [int(m) for m in message_ids]
    if not ids:
        return ("", "")
    if len(ids) == 1:
        row = messages_repo.get_row(con, ids[0])
        if row is None:
            return (f"1 new message — {APP_NAME}", "")
        who = row.correspondent or row.from_addr or "somebody"
        return (who, row.subject_label)
    title = f"{len(ids)} new messages"
    lines = []
    for mid in ids[:PREVIEW]:
        row = messages_repo.get_row(con, mid)
        if row is None:
            continue
        who = row.correspondent or row.from_addr or "somebody"
        lines.append(f"{who} — {row.subject_label}")
    rest = len(ids) - len(lines)
    if rest > 0 and lines:
        lines.append(f"and {rest} more")
    return (title, "\n".join(lines))


def announce(results: Sequence, con: sqlite3.Connection, *,
             notifier=None, window_active: bool = False) -> str | None:
    """Tell the desktop about new mail. Returns the status-bar fallback, or None.

    None means nothing to say — no mail, still fetching, or the window is the
    thing being looked at. A string is the same words that were (or would have
    been) put in a notification, for the status bar when `notify` cannot send.
    """
    if window_active or still_fetching(results):
        return None
    ids = announceable(results)
    if not ids:
        return None
    title, body = describe(con, ids)
    if not title:
        return None
    send = notifier or notify_mod.notify
    sent = bool(send(title, body))
    words = f"{title} — {body}" if body else title
    return None if sent else words
