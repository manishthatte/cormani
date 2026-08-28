# SPDX-License-Identifier: GPL-3.0-or-later
#
# Telling the server what the user already did.
#
# `store/pending.py` records the intention the moment the user acts, in the
# server's coordinates and in order. This drains it. The two halves are
# separate because the store must never import the protocol, and because they
# fail differently: recording cannot fail, and sending fails constantly.
#
# Four things decide whether this is right or merely plausible:
#
# ORDER IS PRESERVED, BATCHING NOTWITHSTANDING. Ops on ONE message must reach
# the server in the order the user made them — marking read then archiving is
# not the same as archiving then marking read, because after the move the UID
# is gone. So the queue is walked in id order and only CONSECUTIVE compatible
# ops are merged into one command. Selecting eighty messages and pressing A
# still produces one UID MOVE; nothing is ever reordered to achieve it.
#
# A FLAG OP RESOLVES AGAINST THE LIVE ROW, A MOVE AGAINST THE RECORDED ONE.
# They differ deliberately. A move must go to where the server still thinks the
# message is, which only the recorded coordinates know. A flag change queued
# BEHIND a move must go to wherever the message ended up — and by the time it
# runs, the move ahead of it has written the new UID back, so the live row is
# the correct and current answer.
#
# A MESSAGE THAT IS NO LONGER THERE IS NOT A FAILURE. Another client archived
# it, or the user did on their phone. The op is dropped and counted, not
# retried: the intention has been overtaken by events, and the next sync will
# reflect what actually happened.
#
# THE QUEUE IS NEVER SILENTLY EMPTIED. An op that keeps failing is retried
# until `pending.MAX_ATTEMPTS`, then STUCK — skipped, kept, and reported. The
# interface can say "three changes could not be sent"; it cannot say that about
# a row that was deleted to make the number look better. CONVENTIONS.txt §8.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..store import folders as folders_repo
from ..store import pending as pending_repo
from .client import Connection
from .errors import ImapError, MailboxGone, Permanent, Transient, describe

# After this many failures in a row the connection is presumed bad and the
# drain stops, leaving the rest queued. Three rather than one because a single
# refused op is usually about that op; three in a row is about the connection.
_CONSECUTIVE_FAILURES = 3


@dataclass
class DrainReport:
    sent: int = 0
    dropped: int = 0
    failed: int = 0
    stuck: int = 0
    errors: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.errors and not self.failed


def _compatible(a: pending_repo.PendingOp, b: pending_repo.PendingOp) -> bool:
    """Whether two consecutive ops can go in one command."""
    if a.kind != b.kind or a.source_folder_id != b.source_folder_id:
        return False
    if a.kind == pending_repo.KIND_MOVE:
        return a.target_folder_id == b.target_folder_id
    if a.kind == pending_repo.KIND_FLAG:
        return (sorted(a.payload.get("add", ())) == sorted(b.payload.get("add", ()))
                and sorted(a.payload.get("remove", ()))
                == sorted(b.payload.get("remove", ())))
    return True


def _runs(ops: list) -> list:
    """Consecutive compatible ops, grouped. Never reordered — see the header."""
    out: list = []
    for op in ops:
        if out and _compatible(out[-1][0], op):
            out[-1].append(op)
        else:
            out.append([op])
    return out


def _uid_for(con: sqlite3.Connection, op: pending_repo.PendingOp) -> tuple:
    """(folder_id, uid) to act on, or (None, None) when there is nothing to do.

    A move uses what was recorded; a flag uses the live row when there is one.
    See the module header for why those differ.
    """
    if op.kind != pending_repo.KIND_FLAG:
        return op.source_folder_id, op.source_uid
    if op.message_id is not None:
        row = con.execute("SELECT folder_id, uid FROM message WHERE id = ?",
                          (op.message_id,)).fetchone()
        if row is not None and row["uid"] is not None:
            return int(row["folder_id"]), int(row["uid"])
    return op.source_folder_id, op.source_uid


def _path(con: sqlite3.Connection, folder_id: int | None) -> str | None:
    folder = folders_repo.get_folder(con, folder_id) if folder_id else None
    return folder.path if folder else None


# What this module can carry out. `pending_repo.KINDS` is longer than this on
# purpose: the queue is one table and more than one protocol drains it.
_IMAP_KINDS = (pending_repo.KIND_FLAG, pending_repo.KIND_MOVE,
               pending_repo.KIND_EXPUNGE)


def drain(con: sqlite3.Connection, connection: Connection,
          account_id: int) -> DrainReport:
    """Send one account's queued changes. Returns what happened to each.

    Called BEFORE the fetch, always. A message the user archived offline should
    reach the server before the sync asks the server what is in the Inbox —
    otherwise the fetch brings it straight back and the archive looks like it
    did not work.
    """
    report = DrainReport()
    # THE KINDS THIS PROTOCOL OWNS, and no others. A `send` op sits in the same
    # table and is not IMAP's — smtp/outbox.py drains those — and it has no UID
    # by definition, because a draft has never been on the server. Without this
    # filter the loop below reads that as "nothing on the server answers to
    # this any more" and DELETES the message the user asked to send.
    everything = [op for op in pending_repo.pending_for(con, account_id,
                                                        include_stuck=True)
                  if op.kind in _IMAP_KINDS]
    report.stuck = sum(1 for op in everything if op.stuck)
    ops = [op for op in everything if not op.stuck]
    if not ops:
        return report

    consecutive = 0
    for run in _runs(ops):
        resolved = [(op, *_uid_for(con, op)) for op in run]
        actionable = [(op, folder_id, uid) for op, folder_id, uid in resolved
                      if uid is not None and folder_id is not None]
        gone = [op for op, folder_id, uid in resolved
                if uid is None or folder_id is None]
        if gone:
            # Nothing on the server answers to these any more.
            report.dropped += pending_repo.complete(con, [op.id for op in gone])
        if not actionable:
            continue

        try:
            _perform(con, connection, actionable)
        except MailboxGone as exc:
            # The folder itself is gone. These ops can never succeed, and
            # retrying them nine more times helps nobody.
            report.dropped += pending_repo.complete(
                con, [op.id for op, _, _ in actionable])
            report.errors.append(describe(exc))
            continue
        except Permanent as exc:
            for op, _, _ in actionable:
                pending_repo.record_failure(con, op.id, describe(exc))
            report.failed += len(actionable)
            report.errors.append(describe(exc))
            consecutive += 1
        except (ImapError, sqlite3.Error) as exc:
            for op, _, _ in actionable:
                pending_repo.record_failure(con, op.id, describe(exc))
            report.failed += len(actionable)
            report.errors.append(describe(exc))
            consecutive += 1
        else:
            report.sent += pending_repo.complete(
                con, [op.id for op, _, _ in actionable])
            consecutive = 0

        if consecutive >= _CONSECUTIVE_FAILURES:
            report.errors.append(
                "stopped after three failures in a row; the rest stay queued")
            break
    return report


def _perform(con: sqlite3.Connection, connection: Connection,
             actionable: list) -> None:
    """Carry out one run of compatible ops as a single command."""
    op = actionable[0][0]
    folder_id = actionable[0][1]
    uids = [uid for _, _, uid in actionable]
    source = _path(con, folder_id)
    if source is None:
        raise MailboxGone(f"folder {folder_id} is no longer in the store")
    connection.select(source)

    if op.kind == pending_repo.KIND_FLAG:
        connection.store_flags(uids, add=op.payload.get("add", ()),
                               remove=op.payload.get("remove", ()))
        return

    if op.kind == pending_repo.KIND_EXPUNGE:
        connection.store_flags(uids, add=["\\Deleted"])
        connection.expunge_uids(uids)
        return

    target = _path(con, op.target_folder_id)
    if target is None:
        raise MailboxGone(f"folder {op.target_folder_id} is no longer in the store")
    mapping = connection.move(uids, target)
    _adopt_new_uids(con, actionable, mapping)


def _adopt_new_uids(con: sqlite3.Connection, actionable: list,
                    mapping: dict) -> None:
    """Write the destination UIDs the server issued back onto the local rows.

    Without this a moved message sits in its new folder with no UID, the next
    sync of that folder sees it as new, and the store holds it twice — once as
    the row the user moved and once as the row the server sent.

    A server with no UIDPLUS reports nothing, and there the honest answer is to
    remove the local row: the message is safely on the server, and the next
    sync fetches it correctly. It blinks out of the list until then, which is
    the visible cost of a server that will not say what it did.
    """
    for op, _folder_id, uid in actionable:
        if op.message_id is None:
            continue
        new_uid = mapping.get(uid)
        if new_uid is None:
            con.execute("DELETE FROM message WHERE id = ?", (op.message_id,))
            continue
        try:
            con.execute("UPDATE message SET uid = ? WHERE id = ?",
                        (int(new_uid), op.message_id))
        except sqlite3.IntegrityError:
            # The destination already holds that UID: a sync fetched the moved
            # message before the queue drained. The row the user moved is the
            # duplicate, so it is the one that goes.
            con.execute("DELETE FROM message WHERE id = ?", (op.message_id,))
    con.commit()


def pending_summary(con: sqlite3.Connection) -> dict:
    """What the status bar shows: outstanding and stuck, per account."""
    return pending_repo.counts(con)
