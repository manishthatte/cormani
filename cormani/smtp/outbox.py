# SPDX-License-Identifier: GPL-3.0-or-later
#
# The outbox: what has been written and not yet sent.
#
# IT IS THE QUEUE, NOT A FOLDER. Everything an outbox needs — order, retries, a
# count for the status bar, a place to record why something did not go, and a
# guarantee that the user's own actions reach the server in the order they made
# them — is what `store/pending.py` already is. A `send` op points at the draft
# row; this file drains those ops and nothing else does.
#
# ONE CONNECTION FOR THE WHOLE RUN, AND ONLY IF THERE IS SOMETHING TO SEND. A
# submission connection costs a TLS handshake and an authentication, and opening
# one per message on a queue of five is four handshakes nobody needed. Opening
# one when the queue is empty is worse: it is a login attempt on every sync,
# which is what makes a provider start asking whether it was you.
#
# A PERMANENT FAILURE STOPS THAT MESSAGE; A TRANSIENT ONE STOPS THE RUN. A
# rejected recipient is about one message and the next one may be fine, so the
# queue moves on. A server that will not talk is about all of them, and
# hammering it in a loop is how an address gets rate-limited.
#
# WHO FILES THE SENT COPY IS A PROVIDER FACT. Google and Microsoft both put a
# copy in Sent when a message is submitted through their SMTP; appending another
# gives the user two of everything they send. An ordinary IMAP server files
# nothing and the client must APPEND. `auth/providers.files_sent` carries the
# difference, and it is the sort of thing that is invisible for a week and then
# obvious for a year.
#
# THE LOCAL COPY IS WRITTEN EITHER WAY, and it is the same row the draft was:
# moved to Sent, no longer a draft, still with no UID. When the server's own
# copy arrives on a later sync, `ingest.store_message` ADOPTS that row by
# Message-ID rather than making a second one.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..compose import build as build_mod
from ..store import drafts as drafts_repo
from ..store import edits as edits_repo
from ..store import folders as folders_repo
from ..store import pending as pending_repo
from ..store.database import utc_now
from .client import SendFailed, Sender


@dataclass
class OutboxReport:
    """What one run of the outbox did."""

    sent: int = 0
    stuck: int = 0
    notes: list = field(default_factory=list)

    @property
    def tried(self) -> int:
        return self.sent + self.stuck


def waiting(con: sqlite3.Connection, account_id: int | None = None) -> int:
    """How many messages are waiting to go out. The status bar's number."""
    sql = ("SELECT COUNT(*) FROM pending_op WHERE kind = ?"
           + (" AND account_id = ?" if account_id is not None else ""))
    params = [pending_repo.KIND_SEND]
    if account_id is not None:
        params.append(int(account_id))
    return int(con.execute(sql, params).fetchone()[0])


def queue(con: sqlite3.Connection, message_id: int) -> int:
    """Put a saved draft in the outbox. Returns the op id."""
    return pending_repo.enqueue_send(con, message_id)


def send_pending(con: sqlite3.Connection, account, credential, *,
                 connect=Sender.connect, append=None, files_sent: bool = False,
                 host: str = "", port: int = 0) -> OutboxReport:
    """Send everything queued for this account.

    `append` is how a copy is filed on the server, and is the IMAP connection's
    method: this module speaks SMTP and does not open an IMAP connection of its
    own — the engine already has one, and a second would be a second login.
    """
    report = OutboxReport()
    ops = pending_repo.unsent_of_kind(con, account.id, pending_repo.KIND_SEND)
    if not ops:
        return report

    sender = None
    try:
        for op in ops:
            draft = drafts_repo.load(con, op.message_id) if op.message_id else None
            if draft is None:
                # The draft was discarded after being queued. Nothing to send
                # and nothing wrong: the user changed their mind faster than the
                # sync ran.
                pending_repo.complete(con, [op.id])
                continue

            gone = drafts_repo.missing(draft)
            if gone:
                pending_repo.give_up(
                    con, op.id, f"cannot send: {', '.join(gone)} is no longer "
                                f"where the draft says it is")
                report.stuck += 1
                report.notes.append(
                    f"{draft.summary()}: {', '.join(gone)} could not be read")
                continue

            if sender is None:
                sender = connect(host or "", port or 0, credential)
            try:
                sent = _send_one(con, sender, draft, op, account,
                                 append=append, files_sent=files_sent)
            except SendFailed as exc:
                if exc.permanent:
                    pending_repo.give_up(con, op.id, str(exc))
                    report.stuck += 1
                    report.notes.append(f"{draft.summary()}: {exc}")
                    continue
                pending_repo.record_failure(con, op.id, str(exc))
                report.notes.append(f"{draft.summary()}: {exc}")
                break                    # the server, not the message
            report.sent += 1
            if sent.partial:
                report.notes.append(
                    f"{draft.summary()}: sent, but "
                    f"{', '.join(sorted(sent.refused))} would not take it")
    except SendFailed as exc:
        # Connecting failed. Every message is still queued; say so once rather
        # than once per message.
        pending_repo.record_failure(con, ops[0].id, str(exc))
        report.notes.append(str(exc))
    finally:
        if sender is not None:
            sender.close()
    return report


def _send_one(con, sender, draft, op, account, *, append, files_sent):
    """Build it, send it, file it, and take it out of the queue."""
    row = con.execute("SELECT message_id FROM message WHERE id = ?",
                      (op.message_id,)).fetchone()
    raw = build_mod.to_bytes(draft, message_id=(row["message_id"] if row else ""))
    sent = sender.send(draft.from_address or account.address,
                       draft.recipients(), raw)

    folder = folders_repo.by_role(con, account.id, folders_repo.ROLE_SENT)
    if append is not None and folder is not None and not files_sent:
        try:
            append(folder.path, raw)
        except Exception as exc:                             # noqa: BLE001
            # The message HAS gone. A failure to file a copy is worth saying and
            # is not worth undoing anything for — and it must never turn a sent
            # message back into a queued one, which would send it twice.
            _note(con, op.id, f"sent, but the copy could not be filed: {exc}")

    _file_locally(con, draft, op, account, folder)
    pending_repo.complete(con, [op.id])
    return sent


def _file_locally(con, draft, op, account, folder) -> None:
    """The draft row becomes the sent message.

    The same row rather than a new one: it already holds the body, the
    attachments, the Message-ID and the conversation it belongs to, and a copy
    would be a second row in the same thread saying the same thing.
    """
    target = folder.id if folder is not None else folders_repo.local_folder(
        con, account.id, folders_repo.ROLE_SENT, "Sent")
    con.execute(
        "UPDATE message SET folder_id = ?, draft = 0, seen = 1, uid = NULL, "
        "answered = 0, date_at = ?, received_at = ? WHERE id = ?",
        (target, utc_now(), utc_now(), op.message_id))
    con.commit()

    # The message this answers is now answered — which is what the Owed view
    # rests on, and what a correspondent seeing a reply would expect the client
    # to have noticed.
    if draft.in_reply_to:
        parent = con.execute(
            "SELECT id FROM message WHERE message_id = ? ORDER BY id LIMIT 1",
            (draft.in_reply_to,)).fetchone()
        if parent is not None:
            edits_repo.set_answered(con, [int(parent[0])], True)


def _note(con, op_id: int, text: str) -> None:
    con.execute("UPDATE pending_op SET last_error = ? WHERE id = ?",
                (text[:300], int(op_id)))
    con.commit()
