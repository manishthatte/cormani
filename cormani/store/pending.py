# SPDX-License-Identifier: GPL-3.0-or-later
#
# The offline queue, from the store's side.
#
# Offline-first means the user's action wins the interface IMMEDIATELY and
# reconciles with the server later. `messages.py` performs the local half — the
# row's flag changes, the row moves folder — and this is where the intention is
# recorded so that the server can be told afterwards. The other half, actually
# telling it, is `imap/queue.py`; the split is the same one everywhere in this
# tree, that the store never imports the protocol.
#
# THE QUEUE IS WRITTEN IN THE SERVER'S COORDINATES. Migration 4 says why: by
# the time the reconciler runs, an archived message's row is already in the
# Archive folder with no UID, and the server still has it in the Inbox under
# the UID it had when the user pressed the key. `source_folder_id` and
# `source_uid` are recorded at that moment because they cannot be recovered
# afterwards.
#
# COALESCING IS NOT AN OPTIMISATION, IT IS CORRECTNESS OF A KIND. Marking
# eighty messages read is eighty ops; marking them read, then unread, then read
# again should still be eighty, not two hundred and forty. Flag ops merge into
# the message's most recent unattempted flag op — and ONLY the most recent,
# because an op behind a queued MOVE must not overtake it. Order is what makes
# a queue a queue.
#
# `message.pending_flags` IS A MARKER, NOT THE QUEUE. It holds the kinds
# outstanding for a row so the interface can show that a change has not landed
# yet, and migration 1's partial index exists for exactly that query. The truth
# is the table.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from .database import utc_now

KIND_FLAG = "flag"
KIND_MOVE = "move"
KIND_EXPUNGE = "expunge"
# A message the user has written and asked to send. It is the OUTBOX: a queue
# entry rather than a folder, because everything an outbox needs — order, retry,
# a count for the status bar, and a place to record why it did not go — is what
# this table already does. `smtp/outbox.py` drains these; `imap/queue.py` drains
# the other three and deliberately ignores this one, because it is not IMAP.
KIND_SEND = "send"
KINDS = (KIND_FLAG, KIND_MOVE, KIND_EXPUNGE, KIND_SEND)

# After this many refusals an op stops being retried. It stays in the table and
# is REPORTED rather than deleted: silently dropping something the user did is
# worse than telling them it could not be done.
MAX_ATTEMPTS = 10


@dataclass(frozen=True)
class PendingOp:
    id: int
    account_id: int
    message_id: int | None
    kind: str
    source_folder_id: int | None
    source_uid: int | None
    target_folder_id: int | None
    payload: dict
    attempts: int
    last_error: str

    @property
    def stuck(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS


def _op(row: sqlite3.Row) -> PendingOp:
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except ValueError:                                       # pragma: no cover
        payload = {}
    return PendingOp(
        id=row["id"], account_id=row["account_id"], message_id=row["message_id"],
        kind=row["kind"], source_folder_id=row["source_folder_id"],
        source_uid=row["source_uid"], target_folder_id=row["target_folder_id"],
        payload=payload if isinstance(payload, dict) else {},
        attempts=row["attempts"], last_error=row["last_error"])


# ------------------------------------------------------------ where a row is
def _locations(con: sqlite3.Connection, message_ids: Sequence[int]) -> dict:
    """Each message's account, folder and UID as they stand right now."""
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    rows = con.execute(f"""
        SELECT m.id AS id, m.uid AS uid, m.folder_id AS folder_id,
               f.account_id AS account_id
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE m.id IN ({marks})
    """, list(message_ids)).fetchall()
    return {int(r["id"]): r for r in rows}


# --------------------------------------------------------------- enqueueing
def enqueue_flag(con: sqlite3.Connection, message_ids: Sequence[int],
                 *, add: Sequence[str] = (), remove: Sequence[str] = (),
                 commit: bool = True) -> int:
    """Record that these messages' flags changed locally. Returns ops written.

    A message with no UID is skipped: there is nothing on the server to change
    yet. That is not a loss — the sync has not sent it, or a queued move ahead
    of this one will give it a new UID, and the reconciler resolves a flag op
    against the live row rather than the recorded one for that reason.
    """
    if not (add or remove):
        return 0
    written = 0
    for message_id, row in _locations(con, message_ids).items():
        if row["uid"] is None and not _has_queued_move(con, message_id):
            continue
        merged = _merge_flag(con, message_id, add, remove)
        if not merged:
            con.execute("""
                INSERT INTO pending_op (account_id, message_id, kind,
                    source_folder_id, source_uid, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (row["account_id"], message_id, KIND_FLAG, row["folder_id"],
                  row["uid"], json.dumps({"add": list(add), "remove": list(remove)}),
                  utc_now()))
        written += 1
    _mark(con, message_ids)
    if commit:
        con.commit()
    return written


def _has_queued_move(con: sqlite3.Connection, message_id: int) -> bool:
    return con.execute(
        "SELECT 1 FROM pending_op WHERE message_id = ? AND kind = ? LIMIT 1",
        (message_id, KIND_MOVE)).fetchone() is not None


def _merge_flag(con: sqlite3.Connection, message_id: int,
                add: Sequence[str], remove: Sequence[str]) -> bool:
    """Fold a flag change into the message's LAST op, if that op is a flag one.

    Only the last: a flag op sitting behind a queued move must not overtake it,
    and the queue drains in id order precisely so that the user's sequence of
    actions reaches the server as the sequence they performed.
    """
    row = con.execute(
        "SELECT * FROM pending_op WHERE message_id = ? ORDER BY id DESC LIMIT 1",
        (message_id,)).fetchone()
    if row is None or row["kind"] != KIND_FLAG or row["attempts"] > 0:
        return False
    op = _op(row)
    adding = set(op.payload.get("add", ())) - set(remove) | set(add)
    removing = set(op.payload.get("remove", ())) - set(add) | set(remove)
    con.execute("UPDATE pending_op SET payload = ? WHERE id = ?",
                (json.dumps({"add": sorted(adding), "remove": sorted(removing)}),
                 op.id))
    return True


def enqueue_move(con: sqlite3.Connection, moves: dict, *,
                 commit: bool = True) -> int:
    """Record local moves. `moves` maps message id to its TARGET folder id.

    Called BEFORE the row is moved, because the source folder and UID are what
    the server needs and the move is what destroys them.
    """
    if not moves:
        return 0
    written = 0
    for message_id, row in _locations(con, list(moves)).items():
        if row["uid"] is None:
            continue                 # never on the server; nothing to move there
        con.execute("""
            INSERT INTO pending_op (account_id, message_id, kind,
                source_folder_id, source_uid, target_folder_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (row["account_id"], message_id, KIND_MOVE, row["folder_id"],
              row["uid"], moves[message_id], utc_now()))
        written += 1
    _mark(con, list(moves))
    if commit:
        con.commit()
    return written


def enqueue_send(con: sqlite3.Connection, message_id: int, *,
                 commit: bool = True) -> int:
    """Ask for this draft to be sent. Returns the op id.

    NOT skipped for a message with no UID — every other kind is, and this is the
    one that must not be: a draft has never been on the server by definition,
    and that is what makes it something to send rather than something to change.
    """
    row = con.execute(
        "SELECT m.id, f.account_id AS account_id, m.folder_id AS folder_id "
        "FROM message m JOIN folder f ON f.id = m.folder_id WHERE m.id = ?",
        (int(message_id),)).fetchone()
    if row is None:
        return 0
    cur = con.execute("""
        INSERT INTO pending_op (account_id, message_id, kind,
            source_folder_id, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (row["account_id"], int(message_id), KIND_SEND, row["folder_id"],
          utc_now()))
    _mark(con, [message_id])
    if commit:
        con.commit()
    return int(cur.lastrowid)


def unsent_of_kind(con: sqlite3.Connection, account_id: int,
                   kind: str, *, include_stuck: bool = False) -> list[PendingOp]:
    """One account's ops of one kind, in the order they were made."""
    return [op for op in pending_for(con, account_id, include_stuck=include_stuck)
            if op.kind == kind]


def give_up(con: sqlite3.Connection, op_id: int, error: str, *,
            commit: bool = True) -> None:
    """Stop retrying this one, and say why.

    Kept rather than deleted, which is the rule the whole table follows: the
    user asked for something and it did not happen, and the only worse answer
    than "it failed" is silence.
    """
    con.execute(
        "UPDATE pending_op SET attempts = ?, last_attempt_at = ?, "
        "last_error = ? WHERE id = ?",
        (MAX_ATTEMPTS, utc_now(), error[:300], int(op_id)))
    if commit:
        con.commit()


def enqueue_expunge(con: sqlite3.Connection, message_ids: Sequence[int], *,
                    commit: bool = True) -> int:
    """Record that these are to be erased on the server, not merely moved."""
    written = 0
    for message_id, row in _locations(con, message_ids).items():
        if row["uid"] is None:
            continue
        con.execute("""
            INSERT INTO pending_op (account_id, message_id, kind,
                source_folder_id, source_uid, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (row["account_id"], message_id, KIND_EXPUNGE, row["folder_id"],
              row["uid"], utc_now()))
        written += 1
    _mark(con, message_ids)
    if commit:
        con.commit()
    return written


# ------------------------------------------------------------- the marker
def _mark(con: sqlite3.Connection, message_ids: Sequence[int]) -> None:
    """Refresh `message.pending_flags` for these rows from the queue."""
    for message_id in {int(m) for m in message_ids}:
        kinds = sorted({r[0] for r in con.execute(
            "SELECT DISTINCT kind FROM pending_op WHERE message_id = ?",
            (message_id,)).fetchall()})
        con.execute("UPDATE message SET pending_flags = ? WHERE id = ?",
                    (",".join(kinds), message_id))


def unsent_message_ids(con: sqlite3.Connection) -> set:
    """Rows with something outstanding. The interface's "not landed yet" mark."""
    return {int(r[0]) for r in con.execute(
        "SELECT id FROM message WHERE pending_flags <> ''").fetchall()}


# ---------------------------------------------------------------- reading
def pending_for(con: sqlite3.Connection, account_id: int, *,
                include_stuck: bool = False) -> list[PendingOp]:
    """One account's queue, in the order the user made the changes."""
    rows = con.execute(
        "SELECT * FROM pending_op WHERE account_id = ? ORDER BY id",
        (account_id,)).fetchall()
    ops = [_op(r) for r in rows]
    return ops if include_stuck else [op for op in ops if not op.stuck]


def counts(con: sqlite3.Connection) -> dict:
    """Outstanding and stuck, per account, for the status bar."""
    out: dict = {}
    for row in con.execute(
            "SELECT account_id, attempts FROM pending_op").fetchall():
        entry = out.setdefault(int(row["account_id"]), {"pending": 0, "stuck": 0})
        if row["attempts"] >= MAX_ATTEMPTS:
            entry["stuck"] += 1
        else:
            entry["pending"] += 1
    return out


# ------------------------------------------------------------------- undoing
def queued_move(con: sqlite3.Connection, message_id: int) -> PendingOp | None:
    """The move op waiting to be sent for this message, if there is one.

    "Waiting" means present: an op is deleted when the server accepts it, so a
    row here has not been carried out. That is what lets undo tell the two cases
    apart — see store/undo.py, which is the only caller and explains why the
    distinction is not an optimisation.
    """
    row = con.execute(
        "SELECT * FROM pending_op WHERE message_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1", (message_id, KIND_MOVE)).fetchone()
    return _op(row) if row is not None else None


def discard(con: sqlite3.Connection, op_ids: Sequence[int], *,
            commit: bool = True) -> int:
    """Take these back before the server ever hears them.

    The same statement as `complete` and a different meaning, which is why it is
    a different function: complete means the server has it, discard means it
    never will. The marker on the row is refreshed either way.
    """
    op_ids = [int(o) for o in op_ids]
    if not op_ids:
        return 0
    marks = ",".join("?" * len(op_ids))
    touched = [r[0] for r in con.execute(
        f"SELECT DISTINCT message_id FROM pending_op WHERE id IN ({marks})",
        op_ids).fetchall() if r[0] is not None]
    cur = con.execute(f"DELETE FROM pending_op WHERE id IN ({marks})", op_ids)
    _mark(con, touched)
    if commit:
        con.commit()
    return cur.rowcount


def drop_empty_flags(con: sqlite3.Connection, message_ids: Sequence[int], *,
                     commit: bool = True) -> int:
    """Remove flag ops that no longer ask for anything.

    Undoing a flag change merges the opposite into the op the change wrote, and
    the two cancel. What is left is an instruction to add no flags and remove
    none, which would be a round trip to the server to say nothing.
    """
    ids = [int(m) for m in message_ids]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    empty = []
    for row in con.execute(
            f"SELECT * FROM pending_op WHERE kind = ? AND message_id IN ({marks})",
            [KIND_FLAG, *ids]).fetchall():
        op = _op(row)
        if not op.payload.get("add") and not op.payload.get("remove"):
            empty.append(op.id)
    return discard(con, empty, commit=commit) if empty else 0


# --------------------------------------------------------------- retiring
def complete(con: sqlite3.Connection, op_ids: Sequence[int], *,
             commit: bool = True) -> int:
    """The server accepted these. Remove them and refresh the markers."""
    op_ids = [int(o) for o in op_ids]
    if not op_ids:
        return 0
    marks = ",".join("?" * len(op_ids))
    touched = [r[0] for r in con.execute(
        f"SELECT DISTINCT message_id FROM pending_op WHERE id IN ({marks})",
        op_ids).fetchall() if r[0] is not None]
    cur = con.execute(f"DELETE FROM pending_op WHERE id IN ({marks})", op_ids)
    _mark(con, touched)
    if commit:
        con.commit()
    return cur.rowcount


def record_failure(con: sqlite3.Connection, op_id: int, error: str, *,
                   commit: bool = True) -> None:
    con.execute(
        "UPDATE pending_op SET attempts = attempts + 1, last_attempt_at = ?, "
        "last_error = ? WHERE id = ?", (utc_now(), error[:300], op_id))
    if commit:
        con.commit()


def clear_for_account(con: sqlite3.Connection, account_id: int, *,
                      commit: bool = True) -> int:
    """Abandon an account's queue. Only ever at the user's request."""
    touched = [r[0] for r in con.execute(
        "SELECT DISTINCT message_id FROM pending_op WHERE account_id = ?",
        (account_id,)).fetchall() if r[0] is not None]
    cur = con.execute("DELETE FROM pending_op WHERE account_id = ?", (account_id,))
    _mark(con, touched)
    if commit:
        con.commit()
    return cur.rowcount
