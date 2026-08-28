# SPDX-License-Identifier: GPL-3.0-or-later
#
# Undo: putting back what an action changed, on both sides of the queue.
#
# EVERY DESTRUCTIVE ACTION IN AN OFFLINE-FIRST CLIENT HAS TWO HALVES, and an
# undo that reverses one of them is worse than no undo at all. Archiving a
# message moves the row locally AND records that the server is to be told; put
# the row back without touching the queue and the next sync moves it away again,
# in front of a user who watched it come back.
#
# SO THE RULE IS: UNSAY WHAT HAS NOT BEEN SAID, AND SAY THE OPPOSITE OF WHAT
# HAS. An op still in `pending_op` has not been accepted by the server — the
# queue deletes an op when the server takes it — so undoing an action whose op
# is still queued means deleting the op and restoring the row exactly, UID and
# all. An action whose op is gone was accepted, and the honest reversal is a new
# op saying the opposite, which is what `store/edits.py` writes anyway.
#
# THAT DISTINCTION IS NOT AN OPTIMISATION. A move whose op is still queued
# cannot be reversed by moving back: `move_to_folder` clears the UID, and
# `enqueue_move` skips a message with no UID because there is nothing on the
# server to move. The reversal would therefore queue NOTHING while the original
# move stayed queued, and the next sync would carry out an action the user had
# already taken back. The op has to be deleted and the UID restored.
#
# WHAT IS CAPTURED IS THE STATE BEFORE, NOT THE ACTION. "It was in the inbox
# with UID 4132, and unread" survives anything the action did afterwards,
# including the parts that were skipped — an account with no Archive folder
# archives nothing, and an undo built from the action's INTENT would try to
# reverse a move that never happened.
#
# THERE IS NO REDO, and that is a decision. Redo is another stack, another set
# of states to capture, and a second way for the queue to disagree with the
# store; the gesture it competes with is doing the thing again, which in a mail
# client is one key press.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import edits as edits_repo
from . import pending as pending_repo
from . import tags as tags_repo

KIND_FLAG = "flag"
KIND_MOVE = "move"
KIND_TAG = "tag"

# The flag columns an action may change, and the writer for each. A table
# rather than getattr(): the set of columns that may be written is a decision,
# and a lookup that accepted any name would let a caller write any column.
_FLAG_WRITERS = {
    "seen": edits_repo.set_seen,
    "flagged": edits_repo.set_flagged,
    "answered": edits_repo.set_answered,
}


@dataclass(frozen=True)
class Step:
    """One action, and everything needed to take it back.

    Frozen, and holding values rather than a connection or a callable: a step
    sits on a stack while the user does other things, and anything it held on to
    would be a lie by the time it was used.
    """

    label: str                          # what the status line said
    kind: str
    column: str = ""                    # KIND_FLAG: which column
    tag_id: int | None = None           # KIND_TAG: which tag
    before: tuple = field(default=())   # ((message_id, previous), ...)

    @property
    def count(self) -> int:
        return len(self.before)


# ------------------------------------------------------------------ capturing
def capture_flag(con: sqlite3.Connection, message_ids, column: str,
                 label: str) -> Step:
    """What these messages' flag was, before it is changed."""
    assert column in _FLAG_WRITERS
    rows = _select(con, message_ids, column)
    return Step(label=label, kind=KIND_FLAG, column=column,
                before=tuple((mid, bool(value)) for mid, value in rows))


def capture_move(con: sqlite3.Connection, message_ids, label: str) -> Step:
    """Where these messages are, and under which UID.

    The UID is the half that matters and the half that is destroyed: a move
    clears it, and without it a message that was never sent to the server cannot
    be put back the way it was.
    """
    marks = ",".join("?" * len(list(message_ids))) or "NULL"
    ids = [int(m) for m in message_ids]
    if not ids:
        return Step(label=label, kind=KIND_MOVE)
    rows = con.execute(
        f"SELECT id, folder_id, uid FROM message WHERE id IN ({marks})",
        ids).fetchall()
    return Step(label=label, kind=KIND_MOVE,
                before=tuple((int(r["id"]), (int(r["folder_id"]), r["uid"]))
                             for r in rows))


def capture_tag(con: sqlite3.Connection, message_ids, tag_id: int,
                label: str) -> Step:
    """Which of these carried the tag already."""
    ids = [int(m) for m in message_ids]
    if not ids:
        return Step(label=label, kind=KIND_TAG, tag_id=tag_id)
    marks = ",".join("?" * len(ids))
    carrying = {int(r[0]) for r in con.execute(
        f"SELECT message_id FROM message_tag WHERE tag_id = ? "
        f"AND message_id IN ({marks})", [tag_id, *ids]).fetchall()}
    return Step(label=label, kind=KIND_TAG, tag_id=tag_id,
                before=tuple((mid, mid in carrying) for mid in ids))


def _select(con: sqlite3.Connection, message_ids, column: str):
    ids = [int(m) for m in message_ids]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return [(int(r["id"]), r[column]) for r in con.execute(
        f"SELECT id, {column} FROM message WHERE id IN ({marks})", ids).fetchall()]


# ------------------------------------------------------------------ reversing
def reverse(con: sqlite3.Connection, step: Step) -> int:
    """Put it back. Returns how many messages were touched."""
    if not step.before:
        return 0
    if step.kind == KIND_FLAG:
        return _reverse_flag(con, step)
    if step.kind == KIND_MOVE:
        return _reverse_move(con, step)
    if step.kind == KIND_TAG:
        return _reverse_tag(con, step)
    return 0


def _reverse_flag(con: sqlite3.Connection, step: Step) -> int:
    """Set the column back through the ordinary writer, so the queue coalesces.

    Coalescing is what makes this correct rather than merely convenient: the op
    the action wrote is the message's most recent, and writing the opposite
    merges into it instead of queueing a second instruction. What is left says
    what the row now IS — "remove \\Seen" after a mark-read is taken back —
    which is what the queue exists to make true, and one round trip rather than
    two. When the merge cancels to nothing at all, `drop_empty_flags` clears it:
    an instruction to change no flags is not an instruction.
    """
    writer = _FLAG_WRITERS[step.column]
    touched = 0
    for value in (True, False):
        ids = [mid for mid, previous in step.before if previous is value]
        if ids:
            writer(con, ids, value)
            touched += len(ids)
    pending_repo.drop_empty_flags(con, [mid for mid, _ in step.before])
    return touched


def _reverse_move(con: sqlite3.Connection, step: Step) -> int:
    """Back to the folder it was in — and to the UID, when nothing was sent."""
    touched = 0
    for message_id, (folder_id, uid) in step.before:
        where = con.execute("SELECT folder_id FROM message WHERE id = ?",
                            (message_id,)).fetchone()
        if where is None or int(where[0]) == folder_id:
            continue                    # it never moved; nothing to take back
        queued = pending_repo.queued_move(con, message_id)
        if queued is not None:
            # The server was never told. Unsay it, and put the row back exactly
            # as it was — the UID included, because the message is still sitting
            # in that folder on the server under it.
            pending_repo.discard(con, [queued.id], commit=False)
            con.execute("UPDATE message SET folder_id = ?, uid = ? WHERE id = ?",
                        (folder_id, uid, message_id))
            con.commit()
        else:
            edits_repo.move_to_folder(con, [message_id], folder_id)
        touched += 1
    return touched


def _reverse_tag(con: sqlite3.Connection, step: Step) -> int:
    """Tags are local; there is no queue to reconcile.

    Thunderbird keeps them in an IMAP keyword and corMani does not, deliberately
    — a tag is the user's mark on their own copy, and not every server accepts
    keywords. If that changes, this is where the other half goes.
    """
    for value in (True, False):
        ids = [mid for mid, previous in step.before if previous is value]
        if ids:
            tags_repo.set_on_messages(con, ids, step.tag_id, value)
    return len(step.before)
