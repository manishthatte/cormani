# SPDX-License-Identifier: GPL-3.0-or-later
#
# What has arrived that is on no thread — the anti-drift queue.
#
# `store/attach.py`'s two matchers file what they can recognise. This is
# everything they could not, and it is the whole point of the tracking layer:
# an unfiled reply is VISIBLE instead of silent.
#
# ── SCOPING MATTERS MORE THAN IT LOOKS, AND THE NUMBERS ARE MEASURED ───────
#
# Showing every unfiled inbound message turns a queue somebody works through
# into a landfill they ignore, and an ignored queue is exactly the failure this
# exists to prevent. The prototype measured it on a real mailbox: without a
# horizon, a relevance scope and deduplication, the queue held 17,774 items.
# With all three, 40.
#
# THE THREE ARE INDEPENDENT AND ALL THREE ARE NEEDED:
#
#   A HORIZON. Syncing a real mailbox pulls in years, and history is context —
#   searchable, linkable, worth having — but it is not a to-do list. Older mail
#   is fully present; it is simply not presented as outstanding.
#
#   A RELEVANCE SCOPE. `known` is mail from somebody the user has written to,
#   or a reply to something they sent. In a real mailbox that is a few hundred
#   messages out of tens of thousands, and it is the set that can actually need
#   an answer. The wider scopes exist and their counts are shown BESIDE the
#   narrow one, so nothing is hidden — only deferred.
#
#   DEDUPLICATION. Gmail keeps one message in INBOX, All Mail and Important at
#   once, so a raw count trebles every conversation. Counted and grouped by
#   Message-ID, falling back to the row id for the mail that has none.
#
# ── EVERY QUESTION HERE IS ANSWERABLE OVER A READ-ONLY CONNECTION ──────────
#
# `--check` reports the queue's counts and opens the store read-only, because a
# report has to work when the network is the problem and when the disk is. So
# nothing on a reading path writes — not even the horizon, which an earlier
# version persisted on first read and which raised `attempt to write a readonly
# database` from a function nobody would think to look at.
#
# ── DISMISSAL IS A DECISION AND SURVIVES A RESYNC ──────────────────────────
#
# "This needs no answer" is something a person decided, and `--resync` throws
# every message row away and fetches them again. So the dismissal is keyed on
# the RFC 5322 Message-ID rather than on a row id — see `store/trackschema.py`
# — and dismissing one copy dismisses all three of Gmail's.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

from . import times
from .database import get_meta, set_meta, utc_now

# How far back the queue reaches, when nothing else has been said.
DEFAULT_HORIZON_DAYS = 60
HORIZON_KEY = "triage_since"

SCOPE_KNOWN = "known"
SCOPE_HUMAN = "human"
SCOPE_ALL = "all"

# The relevance scopes, as SQL. Each is a WHERE fragment over `m`.
SCOPES = {
    # The default, and deliberately narrow. Mail from somebody this user has
    # written to, or a reply to something they sent.
    SCOPE_KNOWN: """(
        LOWER(m.from_addr) IN (SELECT address FROM wrote_to)
        OR EXISTS (SELECT 1 FROM message sent
                   JOIN folder sf ON sf.id = sent.folder_id
                   WHERE sf.role = 'sent'
                     AND sent.thread_key = m.thread_key
                     AND m.thread_key IS NOT NULL AND m.thread_key <> '')
    )""",
    # Anything a person wrote, whether or not this user started it. The scope
    # for catching a first approach from somebody new.
    SCOPE_HUMAN: "1=1",
    # Everything unfiled, newsletters and receipts included.
    SCOPE_ALL: "1=1",
}


@dataclass(frozen=True)
class Item:
    """One row of the queue. Not a `Message` — this is a different question.

    `store/messages.py` answers "what is in this folder"; this answers "what
    has arrived that nobody has filed", and the fields differ: a queue row
    needs the account it came to and whether it is a bounce, and does not need
    flags, tags or an attachment count.
    """

    message_id: int
    key: str
    date_at: str
    from_name: str
    from_addr: str
    subject: str
    preview: str
    account_address: str
    folder_label: str
    is_bounce: bool
    bounce_status: str
    copies: int = 1

    @property
    def label(self) -> str:
        return self.from_name or self.from_addr or "(no sender)"

    @property
    def title(self) -> str:
        return self.subject or "(no subject)"

    def age_days(self, today: dt.date | None = None) -> int | None:
        when = times.to_local(self.date_at) if self.date_at else None
        if not when:
            return None
        return ((today or times.now_local().date()) - when.date()).days


def horizon(con: sqlite3.Connection) -> str:
    """The date the queue starts from. READ-ONLY, and that is load-bearing.

    An earlier version wrote the default on first read, so that the window
    would not move. It was wrong twice over. `--check` opens the store
    READ-ONLY — it has to work when the network is the problem and when the
    disk is — and asking it for a count raised `attempt to write a readonly
    database`, from a function nobody would look at for a write. And a lazy
    write on a read path is a write whose transaction belongs to whoever
    happened to ask first.

    So the window ROLLS unless somebody pins it, which is also the more honest
    reading of "the last sixty days". `set_horizon` is the pin: a person who
    has worked the queue down moves it forward, and one who wants a look at
    last year moves it back, and either decision is then kept.
    """
    stored = get_meta(con, HORIZON_KEY)
    if stored:
        return stored
    return (times.now_local().date()
            - dt.timedelta(days=DEFAULT_HORIZON_DAYS)).isoformat()


def set_horizon(con: sqlite3.Connection, value: str, *,
                commit: bool = True) -> None:
    set_meta(con, HORIZON_KEY, value)
    if commit:
        con.commit()


def _where(con: sqlite3.Connection, scope: str, query: str) -> tuple:
    """The clause every queue question shares. One definition, three callers.

    The alternative — the listing and the count each spelling out "unfiled,
    undismissed, inbound, within the horizon" — is how a queue comes to show
    twelve rows under a badge that says nine.
    """
    where = [
        "f.role NOT IN ('sent', 'drafts', 'junk', 'trash')",
        "m.draft = 0", "m.deleted = 0",
        "x.id IS NULL",                     # on no thread
        "d.message_key IS NULL",            # not dismissed
        "m.date_at >= ?",
        SCOPES.get(scope, SCOPES[SCOPE_KNOWN]),
    ]
    params: list = [horizon(con)]
    if scope != SCOPE_ALL:
        # A bounce is never bulk, so this does not hide one. `imap/delivery.py`
        # says why that matters.
        where.append("m.is_bulk = 0")
    if query:
        where.append("(m.subject LIKE ? OR m.from_addr LIKE ? "
                     "OR m.from_name LIKE ?)")
        params.extend([f"%{query}%"] * 3)
    return " AND ".join(where), params


# The joins the clause above needs. `x` is the touch that would mean "already
# filed" and `d` the dismissal — both LEFT, because the queue is what is NOT
# there.
_FROM = """
    FROM message m
    JOIN folder f ON f.id = m.folder_id
    JOIN account a ON a.id = f.account_id
    LEFT JOIN touch x ON x.message_id = m.id
    LEFT JOIN triage_dismissed d
           ON d.message_key = COALESCE(NULLIF(m.message_id, ''), 'row:' || m.id)
"""


def queue(con: sqlite3.Connection, *, scope: str = SCOPE_KNOWN,
          query: str = "", limit: int = 200, offset: int = 0) -> list[Item]:
    """The queue itself, newest first.

    DELIBERATELY NOT `SELECT m.*`. A body column holds tens of kilobytes and a
    page of two hundred would move megabytes to render a preview line. The
    preview is already derived and is what the row shows.
    """
    clause, params = _where(con, scope, query)
    rows = con.execute(f"""
        SELECT MIN(m.id) AS message_id,
               COALESCE(NULLIF(m.message_id, ''), 'row:' || m.id) AS key,
               COUNT(*) AS copies,
               m.date_at, m.from_name, m.from_addr, m.subject, m.preview,
               m.is_bounce, m.bounce_status,
               a.address AS account_address, f.display_name AS folder_label
        {_FROM}
        WHERE {clause}
        GROUP BY key
        ORDER BY m.date_at DESC, m.id DESC
        LIMIT ? OFFSET ?""", [*params, int(limit), int(offset)]).fetchall()
    return [Item(message_id=int(r["message_id"]), key=r["key"],
                 date_at=r["date_at"] or "", from_name=r["from_name"] or "",
                 from_addr=r["from_addr"] or "", subject=r["subject"] or "",
                 preview=r["preview"] or "",
                 account_address=r["account_address"] or "",
                 folder_label=r["folder_label"] or "",
                 is_bounce=bool(r["is_bounce"]),
                 bounce_status=r["bounce_status"] or "",
                 copies=int(r["copies"])) for r in rows]


def counts(con: sqlite3.Connection, *, query: str = "") -> dict:
    """How many are waiting in each scope, plus the horizon they are measured
    from. Shown together so that a narrow default never hides work — it only
    defers it, and the wider numbers are how a person knows that."""
    out = {"since": horizon(con)}
    for name in SCOPES:
        clause, params = _where(con, name, query)
        out[name] = con.execute(
            f"SELECT COUNT(DISTINCT COALESCE(NULLIF(m.message_id, ''), "
            f"'row:' || m.id)) {_FROM} WHERE {clause}", params).fetchone()[0]
    return out


def count(con: sqlite3.Connection, *, scope: str = SCOPE_KNOWN) -> int:
    """One scope's count. What the rail's badge shows."""
    clause, params = _where(con, scope, "")
    return con.execute(
        f"SELECT COUNT(DISTINCT COALESCE(NULLIF(m.message_id, ''), "
        f"'row:' || m.id)) {_FROM} WHERE {clause}", params).fetchone()[0]


def dismiss(con: sqlite3.Connection, message_id: int, *, reason: str = "",
            commit: bool = True) -> str:
    """Take one out of the queue. Returns the key that was dismissed.

    Keyed on the Message-ID, so every copy goes at once — the same mail exists
    three times over in a Gmail store — and so the decision survives a
    --resync, which discards every row and fetches them again.
    """
    row = con.execute("SELECT message_id FROM message WHERE id = ?",
                      (int(message_id),)).fetchone()
    if row is None:
        return ""
    key = (row["message_id"] or "").strip() or f"row:{int(message_id)}"
    con.execute("INSERT OR REPLACE INTO triage_dismissed (message_key, reason, "
                "created_at) VALUES (?, ?, ?)", (key, reason, utc_now()))
    if commit:
        con.commit()
    return key


def restore(con: sqlite3.Connection, key: str, *, commit: bool = True) -> bool:
    """Undo a dismissal. What the undo stack calls."""
    cur = con.execute("DELETE FROM triage_dismissed WHERE message_key = ?",
                      (key,))
    if commit:
        con.commit()
    return cur.rowcount > 0


def dismissed(con: sqlite3.Connection, *, limit: int = 200) -> list:
    """What has been set aside, newest first. Visible, because a dismissal made
    by mistake is otherwise a message that vanished."""
    return [dict(r) for r in con.execute(
        "SELECT message_key, reason, created_at FROM triage_dismissed "
        "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()]
