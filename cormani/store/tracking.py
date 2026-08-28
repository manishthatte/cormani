# SPDX-License-Identifier: GPL-3.0-or-later
#
# A conversation the user is PURSUING, and the questions asked about it.
#
# ── WHICH "THREAD" THIS IS ─────────────────────────────────────────────────
#
# This module means the TRACKED thread: authored by hand, carrying a state, a
# next action, a nudge cadence and possibly a statutory deadline, and holding a
# timeline of everything that happened on it across every channel.
#
# `store/threads.py` means the other thing — a mail conversation, a References
# chain, derived from headers and rebuilt by --reindex. The two never mix, and
# `store/trackschema.py` records why one word carries both.
#
# ── NOTHING DERIVABLE IS STORED, AND THAT IS THE WHOLE DESIGN ──────────────
#
# Silence, the effective due date, whether a nudge is overdue, whether a reply
# is owed: every one of them is a function of the timeline and TODAY, and a
# stored answer is a lie the moment the clock passes it. So they are methods on
# `Thread` rather than columns, they take `today` as a parameter so a test can
# ask about a Tuesday in March, and the aggregates they need — the last touch
# in each direction, the count, the channels — are computed by the SAME query
# that fetched the row. Five hundred threads render in one query and not in
# five hundred and one.
#
# OWED IS A FACT AND NOT A GUESS, which is the sentence PLAN.txt §2 uses to
# separate this from Outlook's Focused/Other. They answered last and this side
# has not: `last_in > last_out`. There is no scoring, no importance model, and
# nothing to be wrong about.
#
# A DEADLINE IS NOT A NUDGE. `due_date` is soft and defaults to the last touch
# plus the cadence; `deadline_date` is hard and no amount of polite reminding
# satisfies it. They are answered by different methods and drawn differently,
# and the day one of them is folded into the other is the day a VAT return is
# marked done by a follow-up email.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field

from . import times
from .database import utc_now

STATE_OPEN = "open"
STATE_AWAITING = "awaiting"
STATE_REPLIED = "replied"
STATE_BLOCKED = "blocked"
STATE_CLOSED = "closed"
STATE_DEAD = "dead"

STATES = (STATE_OPEN, STATE_AWAITING, STATE_REPLIED, STATE_BLOCKED,
          STATE_CLOSED, STATE_DEAD)

# A thread that is still work. `blocked` is IN it — something being blocked is
# a reason to look at it, not a reason to stop counting it — while closed and
# dead are finished, one well and one badly.
LIVE_STATES = (STATE_OPEN, STATE_AWAITING, STATE_REPLIED, STATE_BLOCKED)

# Never overdue for a NUDGE, whatever the dates say. A closed thread needs no
# follow-up, and a blocked one is waiting on something a reminder cannot move.
# A DEADLINE is not silenced by this: a statutory date on a blocked thread is
# exactly the thing that must still shout.
NO_NUDGE_STATES = (STATE_CLOSED, STATE_DEAD, STATE_BLOCKED)

# The user's own categories, seeded rather than enumerated: `track` is free
# text and a new one is data, not a migration.
DEFAULT_TRACK = "correspondence"
SEED_TRACKS = (DEFAULT_TRACK, "supplier", "customer", "statutory", "grant",
               "recruitment", "personal")

DEFAULT_CADENCE_DAYS = 7

_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def slugify(text: str, fallback: str = "thread") -> str:
    """A stable, readable handle for a thread.

    Readable because a thread is referred to from notes, from commit messages
    and from the user's own memory, and an integer id is none of those.
    """
    slug = _SLUG_BAD.sub("-", (text or "").lower()).strip("-")
    return slug[:60] or fallback


def unique_slug(con: sqlite3.Connection, base: str) -> str:
    """`base`, or `base-2`, or the first suffix nobody has taken.

    Slugs are never reused, so this counts up rather than filling gaps: a
    deleted thread's slug staying gone is what keeps an old note pointing at
    nothing rather than at something else.
    """
    slug, n = base, 1
    while con.execute("SELECT 1 FROM thread WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _date(value) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _local_date(stamp: str) -> dt.date | None:
    """A stored UTC instant as the LOCAL day it happened on.

    Silence is counted in days a person lived through, not in days UTC lived
    through: at UTC+05:30 a message that arrived at 03:00 local arrived
    yesterday by UTC's reckoning, and "silent for two days" would be wrong by
    one for every evening message.
    """
    when = times.to_local(stamp) if stamp else None
    return when.date() if when else None


@dataclass(frozen=True)
class Thread:
    """One tracked conversation, with the aggregates its own row was fetched
    with. The stored columns first, then what the query counted."""

    id: int
    slug: str
    title: str
    org: str
    track: str
    state: str
    priority: int
    cadence_days: int
    due_date: str
    deadline_date: str
    deadline_note: str
    next_action: str
    note: str
    created_at: str
    updated_at: str
    # Computed by the query that fetched this row, never stored.
    last_at: str = ""
    last_in: str = ""
    last_out: str = ""
    touches: int = 0
    bounced: int = 0
    channels: tuple = field(default_factory=tuple)

    # ------------------------------------------------------------- the facts
    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def owed(self) -> bool:
        """They answered last and this side has not.

        The whole of PLAN.txt §2's claim that Owed is a fact rather than a
        guess. A thread nobody has ever written IN to is not owed — there is
        nothing to answer — which is why the first test is `last_in` and not a
        comparison that would be true against two empty strings.
        """
        return bool(self.last_in) and (not self.last_out
                                       or self.last_in > self.last_out)

    def owed_days(self, today: dt.date | None = None) -> int | None:
        """How long they have been waiting. None when nothing is owed."""
        if not self.owed:
            return None
        day = _local_date(self.last_in)
        return ((today or times.now_local().date()) - day).days if day else None

    def silent_days(self, today: dt.date | None = None) -> int | None:
        """Days since anything at all happened. None when nothing ever has."""
        day = _local_date(self.last_at)
        return ((today or times.now_local().date()) - day).days if day else None

    def effective_due(self) -> dt.date | None:
        """When a nudge is due: the date set, or the last touch plus cadence.

        The cadence is what makes this useful without any dates being typed —
        a thread with a seven-day cadence and a fortnight of silence is asking
        to be looked at, and nobody had to remember to say so.
        """
        explicit = _date(self.due_date)
        if explicit:
            return explicit
        last = _local_date(self.last_at)
        if not last:
            return None
        return last + dt.timedelta(days=self.cadence_days
                                   or DEFAULT_CADENCE_DAYS)

    def overdue(self, today: dt.date | None = None) -> bool:
        """Whether a NUDGE is owed. Never true for a state a nudge cannot move."""
        if self.state in NO_NUDGE_STATES:
            return False
        due = self.effective_due()
        return bool(due and due <= (today or times.now_local().date()))

    def deadline(self) -> dt.date | None:
        return _date(self.deadline_date)

    def days_to_deadline(self, today: dt.date | None = None) -> int | None:
        """Negative when it has passed, which is the number that matters most.

        Deliberately not silenced by state: a statutory date on a blocked
        thread is exactly the one that must still shout.
        """
        day = self.deadline()
        return (day - (today or times.now_local().date())).days if day else None


def _thread(row: sqlite3.Row) -> Thread:
    channels = tuple(sorted(c for c in (row["channels"] or "").split(",") if c)) \
        if "channels" in row.keys() else ()
    extra = {name: row[name] for name in ("last_at", "last_in", "last_out")
             if name in row.keys()}
    return Thread(
        id=int(row["id"]), slug=row["slug"], title=row["title"],
        org=row["org"] or "", track=row["track"] or DEFAULT_TRACK,
        state=row["state"] or STATE_OPEN, priority=int(row["priority"] or 3),
        cadence_days=int(row["cadence_days"] or DEFAULT_CADENCE_DAYS),
        due_date=row["due_date"] or "", deadline_date=row["deadline_date"] or "",
        deadline_note=row["deadline_note"] or "",
        next_action=row["next_action"] or "", note=row["note"] or "",
        created_at=row["created_at"] or "", updated_at=row["updated_at"] or "",
        touches=int(row["touches"]) if "touches" in row.keys() else 0,
        bounced=int(row["bounced"]) if "bounced" in row.keys() else 0,
        channels=channels,
        **{k: (v or "") for k, v in extra.items()})


# The aggregates every board row needs, in the query that fetches the row. One
# query for five hundred threads; the alternative is five hundred and one.
_AGGREGATES = """
    (SELECT MAX(occurred_at) FROM touch x WHERE x.thread_id = t.id) AS last_at,
    (SELECT MAX(occurred_at) FROM touch x WHERE x.thread_id = t.id
       AND x.direction = 'in') AS last_in,
    (SELECT MAX(occurred_at) FROM touch x WHERE x.thread_id = t.id
       AND x.direction = 'out') AS last_out,
    (SELECT COUNT(*) FROM touch x WHERE x.thread_id = t.id) AS touches,
    (SELECT COUNT(*) FROM touch x WHERE x.thread_id = t.id
       AND x.status = 'bounced') AS bounced,
    (SELECT GROUP_CONCAT(DISTINCT x.channel) FROM touch x
       WHERE x.thread_id = t.id) AS channels
"""

_SELECT = f"SELECT t.*, {_AGGREGATES} FROM thread t"


# ------------------------------------------------------------------ reading
def list_threads(con: sqlite3.Connection, *, track: str = "",
                 state: str = "", channel: str = "", query: str = "",
                 order: str = "due", limit: int = 500,
                 offset: int = 0) -> list[Thread]:
    """The board. Every filter is optional and an empty one means "all".

    `state="live"` is a filter and not a state — the four that are still work,
    which is what a person means by "show me what is open" and is not
    expressible as one value.
    """
    where, params = ["1=1"], []
    if track:
        where.append("t.track = ?")
        params.append(track)
    if state == "live":
        marks = ",".join("?" * len(LIVE_STATES))
        where.append(f"t.state IN ({marks})")
        params.extend(LIVE_STATES)
    elif state:
        where.append("t.state = ?")
        params.append(state)
    if channel:
        where.append("EXISTS (SELECT 1 FROM touch x WHERE x.thread_id = t.id "
                     "AND x.channel = ?)")
        params.append(channel)
    if query:
        where.append("(t.title LIKE ? OR t.org LIKE ? OR t.slug LIKE ? "
                     "OR t.next_action LIKE ? OR t.note LIKE ?)")
        params.extend([f"%{query}%"] * 5)

    sql = (f"{_SELECT} WHERE {' AND '.join(where)} "
           f"ORDER BY {_order_by(order)} LIMIT ? OFFSET ?")
    rows = con.execute(sql, [*params, int(limit), int(offset)]).fetchall()
    return [_thread(r) for r in rows]


def _order_by(order: str) -> str:
    """The board's orders, and why the default is the one it is.

    A thread with a hard deadline comes first whatever else is true, because
    the deadline is the thing that cannot be recovered from. Then by whichever
    date is soonest, and a thread with no date at all sorts last rather than
    first — '9999' is the sentinel, and it is a string because these columns
    are ISO dates and compare lexically.
    """
    return {
        "due": ("CASE WHEN t.deadline_date IS NOT NULL AND t.deadline_date <> '' "
                "THEN 0 ELSE 1 END, "
                "COALESCE(NULLIF(t.deadline_date, ''), NULLIF(t.due_date, ''), "
                "'9999'), t.priority, t.title COLLATE NOCASE"),
        "activity": "last_at DESC, t.title COLLATE NOCASE",
        "title": "t.title COLLATE NOCASE",
        "org": "t.org COLLATE NOCASE, t.title COLLATE NOCASE",
        "priority": "t.priority, t.title COLLATE NOCASE",
    }.get(order, "COALESCE(NULLIF(t.due_date, ''), '9999')")


def get_thread(con: sqlite3.Connection, thread_id: int) -> Thread | None:
    row = con.execute(f"{_SELECT} WHERE t.id = ?", (int(thread_id),)).fetchone()
    return _thread(row) if row else None


def by_slug(con: sqlite3.Connection, slug: str) -> Thread | None:
    row = con.execute(f"{_SELECT} WHERE t.slug = ?", (slug,)).fetchone()
    return _thread(row) if row else None


def owed(con: sqlite3.Connection, **kwargs) -> list[Thread]:
    """Threads where they answered last and this side has not.

    Filtered in Python rather than in SQL, and that is a measured choice about
    where the complexity goes: the comparison is over two aggregates the board
    query already computes, and expressing it in SQL means repeating both
    correlated subqueries inside a HAVING. The board is hundreds of rows, not
    hundreds of thousands.
    """
    kwargs.setdefault("state", "live")
    return [t for t in list_threads(con, **kwargs) if t.owed]


def overdue(con: sqlite3.Connection, *, today: dt.date | None = None,
            **kwargs) -> list[Thread]:
    """Threads whose nudge is due. Same argument as `owed` about the filter."""
    kwargs.setdefault("state", "live")
    return [t for t in list_threads(con, **kwargs) if t.overdue(today)]


def deadlines(con: sqlite3.Connection, *, within_days: int = 60,
              today: dt.date | None = None, **kwargs) -> list[Thread]:
    """Hard dates, soonest first, including any that have already passed.

    A deadline that has gone by is the most important row on the board and the
    easiest to filter out by accident, so the lower bound is deliberately open.
    """
    kwargs.setdefault("state", "live")
    out = []
    for thread in list_threads(con, **kwargs):
        days = thread.days_to_deadline(today)
        if days is not None and days <= within_days:
            out.append(thread)
    return sorted(out, key=lambda t: (t.deadline_date, t.priority, t.id))


def counts(con: sqlite3.Connection, *, today: dt.date | None = None) -> dict:
    """What the rail and the tab header show. One pass over the live board."""
    live = list_threads(con, state="live")
    return {"live": len(live),
            "owed": sum(1 for t in live if t.owed),
            "overdue": sum(1 for t in live if t.overdue(today)),
            "deadlines": sum(1 for t in live
                             if (t.days_to_deadline(today) or 999) <= 30),
            "closed": con.execute(
                "SELECT COUNT(*) FROM thread WHERE state IN (?, ?)",
                (STATE_CLOSED, STATE_DEAD)).fetchone()[0]}


def tracks(con: sqlite3.Connection) -> list[str]:
    """The categories in use, plus the seeds, so a new store offers something."""
    used = {r[0] for r in con.execute(
        "SELECT DISTINCT track FROM thread WHERE track <> ''").fetchall()}
    return sorted(used | set(SEED_TRACKS))


# ------------------------------------------------------------------ writing
def create_thread(con: sqlite3.Connection, title: str, *, org: str = "",
                  track: str = DEFAULT_TRACK, state: str = STATE_OPEN,
                  priority: int = 3,
                  cadence_days: int = DEFAULT_CADENCE_DAYS,
                  due_date: str = "", deadline_date: str = "",
                  deadline_note: str = "", next_action: str = "",
                  note: str = "", slug: str = "", created_at: str = "",
                  commit: bool = True) -> int:
    """Make one. The slug is derived from the org and title unless given.

    `created_at` IS A PARAMETER BECAUSE IT IS LOAD-BEARING AND NOT DECORATION.
    `store/attach.py`'s second matcher bounds itself by the thread's first
    touch, and falls back to this when there is none — so a thread that claims
    to have begun today files none of the mail that led to it. An importer, and
    the demo fixtures, both need to say when a conversation actually started.
    """
    base = slug or slugify(f"{org}-{title}" if org else title)
    stamp = created_at or utc_now()
    cur = con.execute("""
        INSERT INTO thread (slug, title, org, track, state, priority,
            cadence_days, due_date, deadline_date, deadline_note, next_action,
            note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (unique_slug(con, base), title, org, track or DEFAULT_TRACK,
          state, int(priority), int(cadence_days), due_date, deadline_date,
          deadline_note, next_action, note, stamp, utc_now()))
    if commit:
        con.commit()
    return int(cur.lastrowid)


_WRITABLE = ("title", "org", "track", "state", "priority", "cadence_days",
             "due_date", "deadline_date", "deadline_note", "next_action",
             "note")


def update_thread(con: sqlite3.Connection, thread_id: int, *,
                  commit: bool = True, **fields) -> None:
    """Change what the user changed, and nothing else.

    An unknown field is an ERROR rather than a silent no-op: this is called
    from dialogs, and a typo that quietly changes nothing is a dialog that
    appears to work.
    """
    unknown = set(fields) - set(_WRITABLE)
    if unknown:
        raise ValueError(f"not a thread field: {', '.join(sorted(unknown))}")
    if not fields:
        return
    sets = ", ".join(f"{name} = ?" for name in fields)
    con.execute(f"UPDATE thread SET {sets}, updated_at = ? WHERE id = ?",
                [*fields.values(), utc_now(), int(thread_id)])
    if commit:
        con.commit()


def set_state(con: sqlite3.Connection, thread_id: int, state: str, *,
              commit: bool = True) -> None:
    if state not in STATES:
        raise ValueError(f"not a state: {state}")
    update_thread(con, thread_id, state=state, commit=commit)


def delete_thread(con: sqlite3.Connection, thread_id: int, *,
                  commit: bool = True) -> None:
    """Remove a thread and its timeline. Only at the user's request.

    The touches go with it — they are the thread's, not the mailbox's — and
    the MESSAGES they pointed at are untouched, which is the difference
    between deleting a thread and deleting mail.
    """
    con.execute("DELETE FROM thread WHERE id = ?", (int(thread_id),))
    if commit:
        con.commit()


def merge_threads(con: sqlite3.Connection, keep_id: int, drop_id: int, *,
                  commit: bool = True) -> int:
    """Fold one thread into another. Returns how many touches moved.

    Two threads for one conversation is what happens when a correspondent
    starts a fresh subject and the second matcher had nothing to match on yet.
    The kept thread's own fields win; the dropped one's timeline, contacts and
    note are carried over rather than lost, because the note is the part
    somebody typed.

    A COLLIDING ext_id IS DROPPED, NOT RENAMED. `UNIQUE(thread_id, ext_id)` is
    what makes filing idempotent, and two touches for one Message-ID on one
    thread is exactly what it exists to prevent — if both threads had filed the
    same message, the kept one's copy is the one that stays.
    """
    if int(keep_id) == int(drop_id):
        return 0
    moved = con.execute("""
        UPDATE OR IGNORE touch SET thread_id = ? WHERE thread_id = ?
    """, (int(keep_id), int(drop_id))).rowcount
    con.execute("INSERT OR IGNORE INTO thread_contact (thread_id, contact_id, "
                "role) SELECT ?, contact_id, role FROM thread_contact "
                "WHERE thread_id = ?", (int(keep_id), int(drop_id)))
    dropped = con.execute("SELECT title, note FROM thread WHERE id = ?",
                          (int(drop_id),)).fetchone()
    if dropped and (dropped["note"] or "").strip():
        keep = con.execute("SELECT note FROM thread WHERE id = ?",
                           (int(keep_id),)).fetchone()
        joined = "\n\n".join(x for x in ((keep["note"] if keep else ""),
                                         f"— merged from {dropped['title']} —",
                                         dropped["note"]) if x)
        con.execute("UPDATE thread SET note = ?, updated_at = ? WHERE id = ?",
                    (joined, utc_now(), int(keep_id)))
    con.execute("DELETE FROM thread WHERE id = ?", (int(drop_id),))
    if commit:
        con.commit()
    return moved


# ----------------------------------------------------------------- contacts
def link_contact(con: sqlite3.Connection, thread_id: int, contact_id: int, *,
                 role: str = "", commit: bool = True) -> None:
    """Put a person on a thread. Idempotent, and the role may be updated."""
    con.execute("""
        INSERT INTO thread_contact (thread_id, contact_id, role)
        VALUES (?, ?, ?)
        ON CONFLICT(thread_id, contact_id) DO UPDATE SET role = excluded.role
    """, (int(thread_id), int(contact_id), role))
    if commit:
        con.commit()


def unlink_contact(con: sqlite3.Connection, thread_id: int, contact_id: int, *,
                   commit: bool = True) -> None:
    con.execute("DELETE FROM thread_contact WHERE thread_id = ? "
                "AND contact_id = ?", (int(thread_id), int(contact_id)))
    if commit:
        con.commit()


def thread_contacts(con: sqlite3.Connection, thread_id: int) -> list:
    """The people on a thread, with the role each has on it."""
    from . import contacts as contacts_repo

    rows = con.execute("""
        SELECT c.id, tc.role FROM thread_contact tc
        JOIN contact c ON c.id = tc.contact_id
        WHERE tc.thread_id = ? ORDER BY c.name COLLATE NOCASE, c.id
    """, (int(thread_id),)).fetchall()
    out = []
    for row in rows:
        contact = contacts_repo.get_contact(con, int(row["id"]))
        if contact is not None:
            out.append((contact, row["role"] or ""))
    return out


def threads_for_contact(con: sqlite3.Connection, contact_id: int) -> list[Thread]:
    rows = con.execute(f"""
        {_SELECT} JOIN thread_contact tc ON tc.thread_id = t.id
        WHERE tc.contact_id = ? ORDER BY t.title COLLATE NOCASE
    """, (int(contact_id),)).fetchall()
    return [_thread(r) for r in rows]


def threads_for_address(con: sqlite3.Connection, address: str) -> list[Thread]:
    """Every thread a given email address is on. What the reader's strip asks.

    Through `handle` rather than by matching a string on a touch: the point of
    a contact is that one person has several addresses, and a strip that only
    recognised the address in front of it would fail exactly when somebody
    wrote from their phone.
    """
    rows = con.execute(f"""
        {_SELECT} JOIN thread_contact tc ON tc.thread_id = t.id
        JOIN handle h ON h.contact_id = tc.contact_id AND h.kind = 'email'
        WHERE LOWER(h.value) = ? ORDER BY t.title COLLATE NOCASE
    """, ((address or "").strip().lower(),)).fetchall()
    return [_thread(r) for r in rows]
