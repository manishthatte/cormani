# SPDX-License-Identifier: GPL-3.0-or-later
#
# Threading: which messages are one conversation.
#
# By REFERENCES, and by nothing else. RFC 5322 gives every message an identity
# and every reply a chain of the identities it answers, and that chain is the
# only evidence in a mail message that two of them belong together. Stage 1 put
# the normalised subject in `thread_key` as a placeholder and said so; this
# replaces it.
#
# SUBJECT THREADING IS DELIBERATELY NOT DONE. It is what the placeholder was,
# and over fifteen accounts it is actively wrong: every message called "Re:
# Invoice", "Meeting" or "Hi" from any correspondent in a decade collapses into
# one conversation. Thunderbird offers it as an option and Gmail bounds it with
# a time window and the participants; both are guesses. A chain is a fact. A
# message with no chain threads alone, which is the honest answer, and the one
# real cost — a reply sent by a client that stripped the headers — shows as its
# own row rather than as somebody else's conversation.
#
# THE KEY IS A MESSAGE-ID, NOT A NUMBER. It is the id of the message the thread
# is rooted at, as claimed by whichever member of it spoke first — References is
# chronological, so its first entry is the root. That makes the key derivable
# from a message ALONE, which is what lets a reply that arrives before the
# message it answers still land in the right thread, and lets the whole column
# be rebuilt from the messages at any time.
#
# ARRIVAL ORDER IS NOT DELIVERY ORDER, so the key has to be able to CHANGE. Two
# messages can each be rooted at an id the store has never seen, and then the
# message that names both arrives and they are one conversation. `assign` looks
# both ways — for messages this one names, and for messages that name this one —
# and when it finds two threads it MERGES them, rewriting the losing key across
# every row that carried it. That is a union-find union, done in SQL, and it is
# why the key must not be used as anything but a grouping value.
#
# THE INDEX ON `message_id` IS NOT OPTIONAL. `assign` runs once per message
# stored, and a first import is a hundred thousand of them; without the index
# each one is a full scan of everything imported so far, which is quadratic and
# turns a long import into an impossible one. Migration 5 creates it.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

# An id longer than this is not a Message-ID, it is a mangled header. Kept as a
# bound rather than a validation: anything is allowed to be a key, but a
# megabyte of junk must not become one.
MAX_ID = 512


def parse_ids(value: str | None) -> tuple[str, ...]:
    """The message-ids in a References or In-Reply-To header, in order.

    Split on whitespace rather than parsed: the header is a list of msg-ids and
    the angle brackets are part of the syntax, not of the identity. Anything
    that is not bracketed is kept anyway — a client that omits the brackets
    still means the same string, and consistency with itself is all threading
    needs from it.
    """
    if not value:
        return ()
    out: list[str] = []
    for piece in value.replace(",", " ").split():
        piece = piece.strip()
        if piece and len(piece) <= MAX_ID and piece not in out:
            out.append(piece)
    return tuple(out)


def candidates(message_id: str, in_reply_to: str, references: str) -> tuple[str, ...]:
    """Every id that would put this message in a thread, root first.

    The message's OWN id is included, and that is the half that is easy to
    forget: a root arriving after its replies is recognised only because the
    replies already name it.
    """
    ids = list(parse_ids(references))
    for one in parse_ids(in_reply_to):
        if one not in ids:
            ids.append(one)
    own = (message_id or "").strip()
    if own and own not in ids:
        ids.append(own)
    return tuple(ids)


def claimed_root(message_id: str, in_reply_to: str, references: str,
                 *, fallback: str = "") -> str:
    """What this message says its thread is, knowing nothing else.

    The first reference; failing that the message it replies to; failing that
    itself. A message with none of the three has no identity to offer and gets
    the fallback, which is its row id — unique, and never colliding with a real
    message-id because no message-id is a bare number.
    """
    for value in (references, in_reply_to, message_id):
        ids = parse_ids(value)
        if ids:
            return ids[0]
    return fallback


def _keys_for(con: sqlite3.Connection, ids: Sequence[str]) -> set[str]:
    """The threads these ids are already part of.

    Two queries rather than one with an OR, so that each uses its own index:
    `ix_message_msgid` for the first and `ix_message_thread` for the second. An
    OR across two columns is exactly the shape SQLite will answer with a scan.
    """
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    found = set()
    for column in ("message_id", "thread_key"):
        rows = con.execute(
            f"SELECT DISTINCT thread_key FROM message WHERE {column} IN ({marks})",
            list(ids)).fetchall()
        found.update(r[0] for r in rows if r[0])
    return found


def assign(con: sqlite3.Connection, *, row_id: int, message_id: str,
           in_reply_to: str, references: str, commit: bool = False) -> str:
    """Put this message in a thread, merging two if it joins them.

    Called once per message stored. Returns the key, and writes it — the caller
    does not have to, and must not decide differently.
    """
    ids = candidates(message_id, in_reply_to, references)
    keys = _keys_for(con, ids)
    mine = claimed_root(message_id, in_reply_to, references,
                        fallback=f"id:{int(row_id)}")
    keys.add(mine)

    # The smallest, and it does not matter which — every other key is rewritten
    # to it in the same transaction, so the choice only has to be the SAME one
    # whichever member of the thread arrives first.
    key = min(keys)
    if len(keys) > 1:
        losing = sorted(keys - {key})
        marks = ",".join("?" * len(losing))
        con.execute(
            f"UPDATE message SET thread_key = ? WHERE thread_key IN ({marks})",
            [key, *losing])
    con.execute("UPDATE message SET thread_key = ? WHERE id = ?", (key, row_id))
    if commit:
        con.commit()
    return key


def rethread(con: sqlite3.Connection) -> int:
    """Build every thread again from the messages. Returns the count.

    OLDEST FIRST, which is what makes one pass enough: a reply is assigned after
    the message it answers whenever the store has both, so the merge in `assign`
    has something to find rather than something to fix later. The store is the
    only input — like the search index, this column is derived, and the honest
    repair for derived data is to derive it again.
    """
    rows = con.execute(
        "SELECT id, message_id, in_reply_to, references_ FROM message "
        "ORDER BY COALESCE(date_at, received_at), id").fetchall()
    for row in rows:
        assign(con, row_id=int(row["id"]), message_id=row["message_id"] or "",
               in_reply_to=row["in_reply_to"] or "",
               references=row["references_"] or "")
    con.commit()
    return len(rows)


def members(con: sqlite3.Connection, thread_key: str) -> list[int]:
    """Every message in a thread, oldest first, across folders and accounts.

    Not filtered by scope on purpose: this answers "what is the conversation",
    and the list's own query answers "what is in view".
    """
    if not thread_key:
        return []
    return [int(r[0]) for r in con.execute(
        "SELECT id FROM message WHERE thread_key = ? "
        "ORDER BY COALESCE(date_at, received_at), id", (thread_key,)).fetchall()]


def key_of(con: sqlite3.Connection, message_id: int) -> str:
    row = con.execute("SELECT thread_key FROM message WHERE id = ?",
                      (message_id,)).fetchone()
    return (row[0] or "") if row else ""


def counts(con: sqlite3.Connection, keys: Iterable[str]) -> dict[str, tuple[int, int]]:
    """(total, unread) per thread, counting the WHOLE conversation.

    The list shows a thread's members that are in view; the number on the row
    is about the conversation. A reply filed in Archive is still part of it, and
    a count that changed when a message was archived would be reporting on the
    folder while claiming to report on the thread.
    """
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = con.execute(
        f"SELECT thread_key, COUNT(*) AS n, SUM(CASE WHEN seen = 0 THEN 1 ELSE 0 END) "
        f"AS unread FROM message WHERE thread_key IN ({marks}) AND deleted = 0 "
        f"GROUP BY thread_key", keys).fetchall()
    return {r["thread_key"]: (int(r["n"]), int(r["unread"] or 0)) for r in rows}


# ------------------------------------------------------------ what the list asks
# The newest message of each thread, computed over the rows the view's own WHERE
# clause already chose. A window function rather than a join on a grouped
# subquery: the grouping is over exactly the same rows, and asking SQLite for
# them twice is the slower way to get the same answer.
THREAD_AT = "MAX(m.date_at) OVER (PARTITION BY m.thread_key) AS thread_at"

# Threads first by their latest activity, then INSIDE a thread newest first.
# Newest first inside matters: the list builds a conversation from the first row
# of its run, so that row has to be the one the conversation is currently about.
ORDER_BY = "ORDER BY thread_at DESC, m.date_at DESC, m.id DESC"


def context_where(keys: Sequence[str],
                  exclude_ids: Sequence[int]) -> tuple[str, list]:
    """The rest of these conversations, wherever they live.

    A conversation is not a folder, and the reply the user sent is the half that
    explains the half they received. So the list shows a thread's other messages
    beneath the one that put it in view — each labelled with where it actually
    is, which is what `Row.location` was written for.

    Trash and Junk are left out for the reason store/search.py gives, and a
    message the user deleted does not come back as context.
    """
    if not keys:
        return "0", []
    marks = ",".join("?" * len(keys))
    sql = (f"m.thread_key IN ({marks}) AND m.deleted = 0 "
           f"AND f.role NOT IN ('trash', 'junk')")
    params: list = list(keys)
    if exclude_ids:
        holes = ",".join("?" * len(exclude_ids))
        sql += f" AND m.id NOT IN ({holes})"
        params.extend(int(i) for i in exclude_ids)
    return sql, params
