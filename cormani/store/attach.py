# SPDX-License-Identifier: GPL-3.0-or-later
#
# Which message belongs to which tracked thread.
#
# The one interesting piece of logic in the tracking layer, and the prototype's
# four years of use is what shaped it. TWO MATCHERS, because either alone loses
# mail:
#
#   1. THREADING. A message whose References or In-Reply-To names something
#      already on a thread. Exact, and the normal case.
#
#   2. THE ADDRESS. A message from somebody on the thread, dated after the
#      thread's first touch. This is the one that is easy to leave out and
#      expensive to be without: correspondents answer by starting a FRESH
#      message with a new subject, and no amount of header threading sees that.
#
# And a third that is not a matcher so much as a repair:
#
#   3. THE BOUNCE. A delivery failure naming an address that belongs to a
#      thread. It has no References to the original — a DSN is a new message
#      from a mailer daemon — so neither matcher above can see it, and it is
#      the single most important thing to file: it is the reason a
#      correspondent has gone quiet.
#
# ── WHATEVER NEITHER CLAIMS GOES TO TRIAGE RATHER THAN BEING DROPPED ───────
#
# `store/triage.py` is that queue and it is the whole point. An unfiled reply
# is VISIBLE instead of silent, which is the failure this layer exists to
# prevent. A matcher that guessed harder would file things onto the wrong
# thread, and a wrongly-filed message is worse than an unfiled one: it looks
# answered.
#
# ── FILING IS IDEMPOTENT AND RUNS AGAIN AFTER EVERY SYNC ───────────────────
#
# UNIQUE(thread_id, ext_id) is what makes a second pass a no-op; `touches.
# add_touch` returns 0 rather than raising. So this can be called after every
# sync without remembering what it did, which is what makes it correct rather
# than merely cheap: a message that arrives before the thread it belongs to is
# filed the moment the thread exists.
#
# THE DATE BOUND ON MATCHER 2 IS LOAD-BEARING. Without "after the thread's
# first touch", starting a thread with a supplier files a decade of their
# earlier mail onto it — which is not history, it is a timeline that says the
# conversation has been going since 2016 and a silence figure computed from the
# wrong end.
#
# AND A THREAD WITH NO TOUCHES YET FALLS BACK TO WHEN IT WAS MADE, which is the
# case the first version got wrong: an empty thread has no first touch, the
# bound collapsed to the beginning of time, and putting a person on a new
# thread swept in their entire history — 34 touches on a thread whose story was
# four. The thread began when somebody made it. That is what `created_at` says,
# and it is the honest bound when there is nothing else to ask.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import touches as touches_repo


@dataclass(frozen=True)
class Filed:
    """What one pass did. Every number is a message newly put on a timeline."""

    threaded: int = 0
    by_address: int = 0
    bounces: int = 0

    @property
    def total(self) -> int:
        return self.threaded + self.by_address + self.bounces


def run(con: sqlite3.Connection, *, limit: int = 5000,
        commit: bool = True) -> Filed:
    """File what can be filed. Safe to call after every sync."""
    filed = Filed(threaded=_by_threading(con, limit),
                  by_address=_by_address(con, limit),
                  bounces=_by_bounce(con, limit))
    if commit:
        con.commit()
    return filed


def _file(con: sqlite3.Connection, rows) -> int:
    written = 0
    for row in rows:
        if touches_repo.from_message(con, int(row["thread_id"]),
                                     int(row["id"]), commit=False):
            written += 1
    return written


# Messages that are not candidates for filing at ALL, whichever matcher is
# asking. A draft is not correspondence — it has not happened — and a bulk
# message is not a conversation with a person. The bounce matcher overrides the
# bulk half deliberately: a DSN declares itself automatic and is the most
# useful message there is.
_NOT_CORRESPONDENCE = "m.draft = 0 AND m.deleted = 0"


def _by_threading(con: sqlite3.Connection, limit: int) -> int:
    """Matcher 1: this message answers something already on a thread.

    THE JOIN IS ON `thread_key` AND NOT ON THE RAW References STRING, and that
    is what makes it exact rather than approximate: `store/threads.py` has
    already resolved every message in the store to the conversation it belongs
    to, including the case where a reply arrived before the message it answers.
    Re-deriving that here would be a second, worse implementation of threading.
    """
    rows = con.execute(f"""
        SELECT DISTINCT m.id, x.thread_id
        FROM message m
        JOIN message parent ON parent.thread_key = m.thread_key
                           AND parent.thread_key IS NOT NULL
                           AND parent.thread_key <> ''
        JOIN touch x ON x.message_id = parent.id
        LEFT JOIN touch mine ON mine.message_id = m.id
                            AND mine.thread_id = x.thread_id
        WHERE mine.id IS NULL AND m.is_bulk = 0 AND {_NOT_CORRESPONDENCE}
        LIMIT ?""", (int(limit),)).fetchall()
    return _file(con, rows)


def _by_address(con: sqlite3.Connection, limit: int) -> int:
    """Matcher 2: from somebody on the thread, after the thread began.

    The one that catches a correspondent who replies by starting a fresh
    message with a new subject — which threading cannot see at all, because
    there is nothing in the headers to see.

    INBOUND ONLY. An outbound message is filed by matcher 1 when it answers
    something on the thread, and by hand otherwise. Filing every message the
    user ever SENT to a contact would sweep in mail about entirely different
    matters, and it would do it silently.
    """
    rows = con.execute(f"""
        SELECT DISTINCT m.id, tc.thread_id
        FROM message m
        JOIN folder f ON f.id = m.folder_id
        JOIN handle h ON h.kind = 'email' AND LOWER(h.value) = LOWER(m.from_addr)
        JOIN thread_contact tc ON tc.contact_id = h.contact_id
        JOIN thread t ON t.id = tc.thread_id
        LEFT JOIN touch mine ON mine.message_id = m.id
                            AND mine.thread_id = tc.thread_id
        WHERE mine.id IS NULL AND m.is_bulk = 0 AND {_NOT_CORRESPONDENCE}
          AND f.role NOT IN ('sent', 'drafts', 'junk', 'trash')
          AND m.date_at >= COALESCE((SELECT MIN(occurred_at) FROM touch x
                                     WHERE x.thread_id = tc.thread_id),
                                    t.created_at)
        LIMIT ?""", (int(limit),)).fetchall()
    return _file(con, rows)


def _by_bounce(con: sqlite3.Connection, limit: int) -> int:
    """Matcher 3: a delivery failure for an address that belongs to a thread.

    Two ways in, and both are needed. The first is the address that failed — a
    DSN names it in its report part and `imap/delivery.py` put it on the row —
    which files the bounce onto every thread that person is on. The second is
    the ORIGINAL message's id where the report carried one, which is exact and
    files it onto the thread the failed message was actually on.

    `is_bulk` is NOT consulted here and that is the point: every bounce
    declares itself automatic, so the ordinary filter would discard exactly the
    message that explains a silence.
    """
    rows = con.execute("""
        SELECT DISTINCT m.id, tc.thread_id
        FROM message m
        JOIN handle h ON h.kind = 'email'
                     AND LOWER(h.value) = LOWER(m.bounce_rcpt)
        JOIN thread_contact tc ON tc.contact_id = h.contact_id
        LEFT JOIN touch mine ON mine.message_id = m.id
                            AND mine.thread_id = tc.thread_id
        WHERE mine.id IS NULL AND m.is_bounce = 1 AND m.bounce_rcpt <> ''
              AND m.deleted = 0
        LIMIT ?""", (int(limit),)).fetchall()
    return _file(con, rows)


def attach_message(con: sqlite3.Connection, thread_id: int, message_id: int, *,
                   with_conversation: bool = True, commit: bool = True) -> int:
    """File one message by hand, and by default its whole conversation with it.

    `with_conversation` is the useful default rather than a convenience: a
    person filing a message means "this exchange belongs here", and filing the
    one message they happened to have open leaves the other nine looking
    unfiled in the triage queue they were trying to empty.
    """
    written = 0
    if touches_repo.from_message(con, thread_id, message_id,
                                 source=touches_repo.SOURCE_LOGGED,
                                 commit=False):
        written += 1
    if with_conversation:
        rows = con.execute("""
            SELECT m.id FROM message m
            WHERE m.thread_key IS NOT NULL AND m.thread_key <> ''
              AND m.thread_key = (SELECT thread_key FROM message WHERE id = ?)
              AND m.id <> ? AND m.deleted = 0
            ORDER BY m.date_at""", (int(message_id), int(message_id))).fetchall()
        for row in rows:
            if touches_repo.from_message(con, thread_id, int(row["id"]),
                                         commit=False):
                written += 1
    _link_correspondents(con, thread_id, message_id)
    if commit:
        con.commit()
    return written


def _link_correspondents(con: sqlite3.Connection, thread_id: int,
                         message_id: int) -> None:
    """Put the other person on the thread, so matcher 2 works from now on.

    Filing by hand once and then watching the next four replies land in triage
    is the behaviour this prevents — and it is why a contact IS created here
    although `contacts.contact_for_address` refuses to invent one in general:
    somebody has just said this conversation matters.
    """
    from . import accounts as accounts_repo
    from . import contacts as contacts_repo
    from . import tracking

    row = con.execute("""
        SELECT m.from_addr, m.from_name, m.to_addrs, f.role AS role
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE m.id = ?""", (int(message_id),)).fetchone()
    if row is None:
        return
    mine = accounts_repo.list_identity_addresses(con)
    candidates = [(row["from_name"] or "", row["from_addr"] or "")]
    candidates += [("", a) for a in
                   contacts_repo.addresses_in(row["to_addrs"] or "")]
    for name, address in candidates:
        address = (address or "").strip()
        if not address or address.lower() in mine:
            continue
        contact = contacts_repo.contact_for_address(con, address, name=name,
                                                    create=True, commit=False)
        if contact is not None:
            tracking.link_contact(con, thread_id, contact.id, commit=False)
        return


def detach_message(con: sqlite3.Connection, thread_id: int, message_id: int, *,
                   commit: bool = True) -> int:
    """Take one message off a thread. Returns how many touches went.

    A LOGGED touch goes too, and that is right: a person filed it by hand and a
    person is unfiling it by hand. What survives is anything with no message
    behind it — a call, a note — because those exist nowhere else.
    """
    cur = con.execute("DELETE FROM touch WHERE thread_id = ? AND message_id = ?",
                      (int(thread_id), int(message_id)))
    if commit:
        con.commit()
    return cur.rowcount


# ------------------------------------------------------------- wrote_to
def rebuild_wrote_to(con: sqlite3.Connection, *, commit: bool = True) -> int:
    """Every address this user has ever written to. Returns how many there are.

    Derived from the SENT folders, because that is what "wrote to" means, and
    rebuilt wholesale rather than maintained: it is a cache, the query that
    builds it is one pass, and an incremental version would be a second place
    for the set to be wrong.

    IT IS WHAT MAKES TRIAGE USABLE. The default scope is "mail from somebody I
    have written to, or a reply to something I sent", and in a real mailbox
    that is a few hundred messages out of tens of thousands. Without this
    table, that question has no cheap answer and the queue becomes a landfill.
    """
    rows = con.execute("""
        SELECT m.to_addrs, m.cc_addrs, m.date_at FROM message m
        JOIN folder f ON f.id = m.folder_id
        WHERE f.role = 'sent' AND m.date_at IS NOT NULL
    """).fetchall()

    from . import contacts as contacts_repo

    seen: dict = {}
    for row in rows:
        when = row["date_at"]
        for field in ("to_addrs", "cc_addrs"):
            for address in contacts_repo.addresses_in(row[field] or ""):
                address = address.lower()
                entry = seen.setdefault(address, {"first": when, "last": when,
                                                  "n": 0})
                entry["n"] += 1
                entry["first"] = min(entry["first"], when)
                entry["last"] = max(entry["last"], when)
    con.execute("DELETE FROM wrote_to")
    con.executemany(
        "INSERT INTO wrote_to (address, first_at, last_at, n) "
        "VALUES (?, ?, ?, ?)",
        [(a, e["first"], e["last"], e["n"]) for a, e in seen.items()])
    if commit:
        con.commit()
    return len(seen)
