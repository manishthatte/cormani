# SPDX-License-Identifier: GPL-3.0-or-later
#
# Changing a message: the flags, the moves, and the queue entry each one owes
# the server.
#
# THE OTHER HALF OF `store/ingest.py`. Two things write to a message row and
# they must never be confused: the SYNC applies what the server said, and the
# USER changes what the server has yet to be told. Ingest is the first and this
# is the second, and the difference is the queue — every function here writes a
# `pending_op` beside the row it changes, and ingest deliberately writes none.
# A flag the server reported, queued straight back at it, is a loop that never
# settles; that warning used to be a comment in the module these came out of,
# and it is now the boundary between two files.
#
# QUEUED BEFORE THE CHANGE WHERE THE CHANGE DESTROYS WHAT THE SERVER NEEDS.
# Archiving is the case: the queue has to name the folder and the UID the
# message had, and the move is what clears them. `store/pending.py` holds the
# format and drains into `imap/queue.py`.
#
# A MOVE THAT CANNOT BE PERFORMED IS REPORTED, NOT SWALLOWED. An account whose
# server has no Archive folder is a real configuration, and `move_to_role`
# returns the messages it could not move so that the interface can say which
# account it was rather than appearing to work. CONVENTIONS.txt §8.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from typing import Sequence

from . import folders as folders_repo
from . import pending as pending_repo


def set_seen(con: sqlite3.Connection, message_ids: Sequence[int],
             seen: bool) -> int:
    return _set_flag(con, message_ids, "seen", seen)


def set_flagged(con: sqlite3.Connection, message_ids: Sequence[int],
                flagged: bool) -> int:
    return _set_flag(con, message_ids, "flagged", flagged)


def set_answered(con: sqlite3.Connection, message_ids: Sequence[int],
                 answered: bool) -> int:
    return _set_flag(con, message_ids, "answered", answered)


# The IMAP flag each column stands for, so the queue speaks the server's
# vocabulary rather than the store's column names.
_FLAG_OF = {"seen": "\\Seen", "flagged": "\\Flagged",
            "answered": "\\Answered", "deleted": "\\Deleted"}


def _set_flag(con: sqlite3.Connection, message_ids: Sequence[int],
              column: str, value: bool) -> int:
    if not message_ids:
        return 0
    assert column in _FLAG_OF
    marks = ",".join("?" * len(message_ids))
    with con:
        cur = con.execute(
            f"UPDATE message SET {column} = ? WHERE id IN ({marks})",
            [1 if value else 0, *message_ids])
        flag = _FLAG_OF[column]
        pending_repo.enqueue_flag(
            con, message_ids, add=[flag] if value else [],
            remove=[] if value else [flag], commit=False)
    return cur.rowcount


def move_to_folder(con: sqlite3.Connection, message_ids: Sequence[int],
                   folder_id: int) -> int:
    if not message_ids:
        return 0
    marks = ",".join("?" * len(message_ids))
    with con:
        # QUEUED FIRST, and that order is the whole point: the server needs the
        # folder and the UID the message had, and the next statement is what
        # destroys them.
        pending_repo.enqueue_move(
            con, {int(m): folder_id for m in message_ids}, commit=False)
        # The UID belongs to the folder that issued it. Carrying it across would
        # make it collide with a real message in the destination, which is the
        # UNIQUE (folder_id, uid) the schema declares. Cleared here, and the
        # reconciler writes the new one back from the server's COPYUID.
        cur = con.execute(
            f"UPDATE message SET folder_id = ?, uid = NULL WHERE id IN ({marks})",
            [folder_id, *message_ids])
    return cur.rowcount


def move_to_role(con: sqlite3.Connection, message_ids: Sequence[int],
                 role: str) -> tuple[int, list[int]]:
    """Move each message to its own account's folder for this role.

    Returns (moved, skipped) — skipped being messages whose account has no such
    folder. Reported rather than swallowed: an account with no Archive folder is
    a real configuration, and the interface should say so instead of appearing
    to archive and doing nothing. CONVENTIONS.txt §8.
    """
    if not message_ids:
        return 0, []
    marks = ",".join("?" * len(message_ids))
    rows = con.execute(f"""
        SELECT m.id AS id, f.account_id AS account_id FROM message m
        JOIN folder f ON f.id = m.folder_id WHERE m.id IN ({marks})
    """, list(message_ids)).fetchall()

    targets: dict[int, int | None] = {}
    by_target: dict[int, list[int]] = {}
    skipped: list[int] = []
    for r in rows:
        account_id = r["account_id"]
        if account_id not in targets:
            folder = folders_repo.by_role(con, account_id, role)
            targets[account_id] = folder.id if folder else None
        target = targets[account_id]
        if target is None:
            skipped.append(r["id"])
        else:
            by_target.setdefault(target, []).append(r["id"])

    moved = 0
    for folder_id, ids in by_target.items():
        moved += move_to_folder(con, ids, folder_id)
    return moved, skipped


def archive(con: sqlite3.Connection,
            message_ids: Sequence[int]) -> tuple[int, list[int]]:
    return move_to_role(con, message_ids, folders_repo.ROLE_ARCHIVE)


def trash(con: sqlite3.Connection,
          message_ids: Sequence[int]) -> tuple[int, list[int]]:
    """Delete means move to Trash, not erase.

    Every mail client that has ever erased on delete has had to grow an undo,
    and the folder IS the undo. Purging the Trash is a separate act the user
    performs deliberately.
    """
    return move_to_role(con, message_ids, folders_repo.ROLE_TRASH)
