# SPDX-License-Identifier: GPL-3.0-or-later
#
# The timeline: one thing that happened on a tracked thread.
#
# A message sent or received, a call logged, a meeting held, a note written.
# `store/trackschema.py` says why the row is called a touch rather than an
# event — stage 5 spent that word on a calendar instance, and a meeting IS one
# of the things that goes on a timeline, so two tables called `event` would
# have meant every join saying which one it meant, for ever.
#
# ── THE CHANNEL IS THE POINT ───────────────────────────────────────────────
#
# PLAN.txt §2: "Channel on every row — email, WhatsApp, LinkedIn, phone, in
# person. One timeline per correspondent regardless of how they reached you."
# A touch from a message and a touch from a telephone call differ in `channel`
# and in nothing else, and that is deliberate: the moment a phone call is a
# second kind of row, the timeline becomes two lists that have to be merged and
# the merge becomes the place the ordering is wrong.
#
# ── A TOUCH POINTS AT ITS SOURCE AND NEVER COPIES IT ───────────────────────
#
# `message_id` and `cal_event_id` are the two things this store already holds.
# Both are ON DELETE SET NULL: a message deleted from the server does not
# delete the fact that it arrived, and the timeline keeps enough of its own
# — who, when, what it was about — to stay readable when the source is gone.
# What it does NOT keep is the body of a message that is still in the store,
# because a second copy is a second thing to be wrong.
#
# ── FILING IS IDEMPOTENT, AND `ext_id` IS WHAT MAKES IT SO ─────────────────
#
# The matchers in `store/attach.py` run on every sync and re-examine mail they
# have already seen. UNIQUE(thread_id, ext_id) is what turns a second pass into
# a no-op instead of a duplicate, and `ext_id` is the correspondent's own
# identifier — a Message-ID, a call reference — carried and never parsed.
#
# A LOGGED TOUCH IS NEVER REMOVED BY ANYTHING DERIVED. `source` separates the
# two: `attached` means a matcher filed it and a re-file may replace it,
# `logged` means a person typed it. Nothing automatic may delete a `logged`
# row, because a phone call has no other record anywhere.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

from . import times
from .database import utc_now

DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTION_NOTE = "note"

CHANNEL_EMAIL = "email"
CHANNEL_PHONE = "phone"
CHANNEL_MEETING = "meeting"
CHANNEL_NOTE = "note"
# Seeded, not enumerated — `touch.channel` is free text and stage 7's panels
# bring their own.
SEED_CHANNELS = (CHANNEL_EMAIL, CHANNEL_PHONE, "whatsapp", "linkedin", "x",
                 "facebook", CHANNEL_MEETING, CHANNEL_NOTE)

SOURCE_ATTACHED = "attached"
SOURCE_LOGGED = "logged"

STATUS_SENT = "sent"
STATUS_RECEIVED = "received"
STATUS_BOUNCED = "bounced"


@dataclass(frozen=True)
class Touch:
    id: int
    thread_id: int
    contact_id: int | None
    channel: str
    direction: str
    occurred_at: str
    subject: str
    body: str
    source: str
    status: str
    message_id: int | None
    cal_event_id: int | None
    ext_id: str
    to_repr: str
    created_at: str
    # Filled from the join where the row was fetched with its people.
    contact_name: str = ""
    contact_org: str = ""

    @property
    def is_logged(self) -> bool:
        """Typed by a person, and therefore never removed by anything derived."""
        return self.source == SOURCE_LOGGED

    @property
    def inbound(self) -> bool:
        return self.direction == DIRECTION_IN

    @property
    def title(self) -> str:
        """Never blank. A logged call usually has no subject, and a timeline
        row with an empty line in it is a defect to look at."""
        if self.subject:
            return self.subject
        if self.direction == DIRECTION_NOTE:
            return "Note"
        return f"{self.channel.capitalize()} {self.direction}"

    def when(self, tz: dt.tzinfo | None = None) -> dt.datetime | None:
        return times.parse(self.occurred_at, tz)


def _touch(row: sqlite3.Row) -> Touch:
    keys = row.keys()
    return Touch(
        id=int(row["id"]), thread_id=int(row["thread_id"]),
        contact_id=None if row["contact_id"] is None else int(row["contact_id"]),
        channel=row["channel"], direction=row["direction"],
        occurred_at=row["occurred_at"], subject=row["subject"] or "",
        body=row["body"] or "", source=row["source"] or SOURCE_LOGGED,
        status=row["status"] or "",
        message_id=None if row["message_id"] is None else int(row["message_id"]),
        cal_event_id=(None if row["cal_event_id"] is None
                      else int(row["cal_event_id"])),
        ext_id=row["ext_id"] or "", to_repr=row["to_repr"] or "",
        created_at=row["created_at"] or "",
        contact_name=(row["contact_name"] or "") if "contact_name" in keys else "",
        contact_org=(row["contact_org"] or "") if "contact_org" in keys else "")


_SELECT = """
    SELECT x.*, c.name AS contact_name, c.org AS contact_org
    FROM touch x LEFT JOIN contact c ON c.id = x.contact_id
"""


# ------------------------------------------------------------------ reading
def timeline(con: sqlite3.Connection, thread_id: int, *,
             limit: int = 1000) -> list[Touch]:
    """Everything on one thread, oldest first.

    Oldest first because a timeline is read as a story, and because the last
    row — the one that says what is owed — is then at the bottom where the eye
    already is after reading it.
    """
    rows = con.execute(
        f"{_SELECT} WHERE x.thread_id = ? ORDER BY x.occurred_at, x.id LIMIT ?",
        (int(thread_id), int(limit))).fetchall()
    return [_touch(r) for r in rows]


def get_touch(con: sqlite3.Connection, touch_id: int) -> Touch | None:
    row = con.execute(f"{_SELECT} WHERE x.id = ?", (int(touch_id),)).fetchone()
    return _touch(row) if row else None


def for_message(con: sqlite3.Connection, message_id: int) -> list[Touch]:
    """Every thread this message has been filed on.

    A list and not one row: the same message can legitimately belong to two
    threads — an email that answers one question and raises another — and the
    reading pane's strip shows all of them rather than picking.
    """
    rows = con.execute(f"{_SELECT} WHERE x.message_id = ? ORDER BY x.id",
                       (int(message_id),)).fetchall()
    return [_touch(r) for r in rows]


def filed_message_ids(con: sqlite3.Connection) -> set:
    """Which message rows are on some thread. What triage subtracts."""
    return {int(r[0]) for r in con.execute(
        "SELECT DISTINCT message_id FROM touch WHERE message_id IS NOT NULL"
    ).fetchall()}


def recent(con: sqlite3.Connection, *, days: int = 14, limit: int = 200,
           now: dt.datetime | None = None) -> list[Touch]:
    """Everything across every thread, newest first. The activity feed."""
    since = times.to_utc_text((now or times.now_local())
                              - dt.timedelta(days=days))
    rows = con.execute(
        f"{_SELECT} WHERE x.occurred_at >= ? ORDER BY x.occurred_at DESC, "
        f"x.id DESC LIMIT ?", (since, int(limit))).fetchall()
    return [_touch(r) for r in rows]


def counts_by_channel(con: sqlite3.Connection, thread_id: int | None = None) -> dict:
    sql = "SELECT channel, COUNT(*) FROM touch"
    params: list = []
    if thread_id is not None:
        sql += " WHERE thread_id = ?"
        params.append(int(thread_id))
    sql += " GROUP BY channel"
    return {r[0]: int(r[1]) for r in con.execute(sql, params).fetchall()}


# ------------------------------------------------------------------ writing
def add_touch(con: sqlite3.Connection, thread_id: int, *, channel: str,
              direction: str, occurred_at: str, subject: str = "",
              body: str = "", contact_id: int | None = None,
              source: str = SOURCE_LOGGED, status: str = "",
              message_id: int | None = None, cal_event_id: int | None = None,
              ext_id: str = "", to_repr: str = "",
              commit: bool = True) -> int:
    """Put one thing on a timeline. Returns its id, or 0 if it was already there.

    ZERO RATHER THAN AN EXCEPTION for a duplicate, because the matchers call
    this on every sync over mail they have already filed, and "already there"
    is the expected answer rather than an error. `ext_id` is what makes the
    question answerable; a touch with none can be added twice, which is right —
    two phone calls to the same person on the same day are two calls.
    """
    if direction not in (DIRECTION_IN, DIRECTION_OUT, DIRECTION_NOTE):
        raise ValueError(f"not a direction: {direction}")
    if ext_id:
        existing = con.execute(
            "SELECT id FROM touch WHERE thread_id = ? AND ext_id = ?",
            (int(thread_id), ext_id)).fetchone()
        if existing is not None:
            return 0
    cur = con.execute("""
        INSERT INTO touch (thread_id, contact_id, channel, direction,
            occurred_at, subject, body, source, status, message_id,
            cal_event_id, ext_id, to_repr, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (int(thread_id), None if contact_id is None else int(contact_id),
          channel, direction, occurred_at, subject, body, source, status,
          None if message_id is None else int(message_id),
          None if cal_event_id is None else int(cal_event_id),
          ext_id or None, to_repr, utc_now()))
    if commit:
        con.commit()
    return int(cur.lastrowid)


def log_call(con: sqlite3.Connection, thread_id: int, *, summary: str,
             occurred_at: str = "", direction: str = DIRECTION_OUT,
             contact_id: int | None = None, body: str = "",
             channel: str = CHANNEL_PHONE, commit: bool = True) -> int:
    """PLAN.txt §2's log-a-call, and the reason the timeline is not the mailbox.

    A telephone call leaves no trace anywhere. Without this the timeline says a
    correspondent went silent for three weeks when in fact the matter was
    settled on the phone in the first of them — and "silent for three weeks" is
    the number the whole board is sorted by.
    """
    return add_touch(con, thread_id, channel=channel, direction=direction,
                     occurred_at=occurred_at or utc_now(), subject=summary,
                     body=body, contact_id=contact_id, source=SOURCE_LOGGED,
                     commit=commit)


def add_note(con: sqlite3.Connection, thread_id: int, text: str, *,
             occurred_at: str = "", commit: bool = True) -> int:
    """A note is a touch with a direction of its own.

    Not `in` and not `out`: a note came from nobody and went to nobody, and
    counting it as either would move `last_in` or `last_out` and so change what
    the board says is owed. Writing a note to yourself must not make a
    correspondent look answered.
    """
    return add_touch(con, thread_id, channel=CHANNEL_NOTE,
                     direction=DIRECTION_NOTE,
                     occurred_at=occurred_at or utc_now(), subject="",
                     body=text, source=SOURCE_LOGGED, commit=commit)


def from_message(con: sqlite3.Connection, thread_id: int, message_id: int, *,
                 source: str = SOURCE_ATTACHED, commit: bool = True) -> int:
    """File one message onto a thread. Returns the touch id, or 0 if already filed.

    THE DIRECTION IS DECIDED BY THE FOLDER'S ROLE AND NOT BY THE ADDRESS, which
    is the one thing about this that is easy to get wrong. A message in a Sent
    folder is outbound even when it is addressed to the user themselves, and a
    message whose From is one of the user's own addresses may still be inbound
    — a mailing list posts it back, and Gmail files that copy in All Mail.
    Asking the folder is asking the mailbox what it did.
    """
    from . import accounts as accounts_repo
    from . import folders as folders_repo

    row = con.execute("""
        SELECT m.id, m.message_id, m.date_at, m.received_at, m.subject,
               m.preview, m.from_addr, m.from_name, m.to_addrs, m.is_bounce,
               m.bounce_status, f.role AS role
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE m.id = ?""", (int(message_id),)).fetchone()
    if row is None:
        return 0

    outgoing = row["role"] in (folders_repo.ROLE_SENT, folders_repo.ROLE_DRAFTS)
    direction = DIRECTION_OUT if outgoing else DIRECTION_IN
    if not outgoing and (row["from_addr"] or "").strip().lower() in \
            accounts_repo.list_identity_addresses(con):
        # From the user, but not in a Sent folder: a list posting the message
        # back, or Gmail's own copy. Outbound all the same — it is something
        # this side said, and counting it inbound would make a thread look owed
        # by the person who wrote it.
        direction = DIRECTION_OUT

    from . import contacts as contacts_repo

    correspondent = row["from_addr"] if not outgoing else \
        next(iter(contacts_repo.addresses_in(row["to_addrs"] or "")), "")
    contact = contacts_repo.contact_for_address(con, correspondent, commit=False)
    status = STATUS_BOUNCED if row["is_bounce"] else (
        STATUS_SENT if outgoing else STATUS_RECEIVED)
    return add_touch(
        con, thread_id, channel=CHANNEL_EMAIL, direction=direction,
        occurred_at=row["date_at"] or row["received_at"] or utc_now(),
        subject=row["subject"] or "", body=row["preview"] or "",
        contact_id=contact.id if contact else None, source=source,
        status=status, message_id=int(row["id"]),
        ext_id=row["message_id"] or f"row:{int(row['id'])}",
        to_repr=row["to_addrs"] or "", commit=commit)


def from_event(con: sqlite3.Connection, thread_id: int, event_id: int, *,
               commit: bool = True) -> int:
    """File a calendar event onto a thread — a meeting that happened.

    The channel is `meeting` and the direction is `note`, which is a judgement
    rather than an omission: a meeting is not something one side said to the
    other, and counting it as either would change what the board says is owed.
    Two people met; neither now owes the other a reply because of it.
    """
    row = con.execute(
        "SELECT id, summary, starts_at, location, remote_id FROM event "
        "WHERE id = ?", (int(event_id),)).fetchone()
    if row is None:
        return 0
    return add_touch(
        con, thread_id, channel=CHANNEL_MEETING, direction=DIRECTION_NOTE,
        occurred_at=row["starts_at"] or utc_now(),
        subject=row["summary"] or "Meeting", body=row["location"] or "",
        source=SOURCE_ATTACHED, cal_event_id=int(row["id"]),
        ext_id=f"cal:{row['remote_id']}", commit=commit)


def remove_touch(con: sqlite3.Connection, touch_id: int, *,
                 commit: bool = True) -> None:
    """Take one off a timeline. Only at the user's request — see the header."""
    con.execute("DELETE FROM touch WHERE id = ?", (int(touch_id),))
    if commit:
        con.commit()


def move_touch(con: sqlite3.Connection, touch_id: int, thread_id: int, *,
               commit: bool = True) -> bool:
    """Re-file one touch onto another thread. False if it is already there.

    False rather than an exception for the UNIQUE collision, for the same
    reason `add_touch` returns 0: the target thread already holding this
    message is an answer, not a fault.
    """
    try:
        con.execute("UPDATE touch SET thread_id = ? WHERE id = ?",
                    (int(thread_id), int(touch_id)))
    except sqlite3.IntegrityError:
        return False
    if commit:
        con.commit()
    return True
