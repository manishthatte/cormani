# SPDX-License-Identifier: GPL-3.0-or-later
#
# Drafts: a message being written, in the store.
#
# A DRAFT IS AN ORDINARY MESSAGE ROW with `draft = 1` and no UID, in the
# account's Drafts folder. It could have been a table of its own, and then the
# list would not have shown it, the search would not have found it, and the
# conversation it belongs to would not have contained it — three features
# rewritten to avoid one column. The row it makes is the row the reader already
# knows how to draw.
#
# NO UID MEANS NOTHING ON THE SERVER, and every part of the sync already
# understands that: `enqueue_flag` skips a message with no UID, `enqueue_move`
# skips it too, and the reconciler resolves against the live row. A draft is
# therefore invisible to the queue until it is sent, which is exactly right.
#
# THE MESSAGE-ID IS MADE AT THE FIRST SAVE AND NEVER AGAIN. It is what the
# correspondent's reply will cite, what this store threads by, and what the copy
# filed in Sent has to match. Regenerating it on each save would give a
# conversation as many roots as the user pressed Ctrl+S.
#
# AN ATTACHMENT'S PATH IS THE USER'S OWN and is not copied into the store. The
# file is read when the message is built — see compose/draft.py — so a draft
# saved on Monday and sent on Tuesday sends Tuesday's version. The cost is
# honest and is reported rather than hidden: a file moved in between cannot be
# sent, and `missing` is how the composer finds that out before the user
# presses Send.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import folders as folders_repo
from . import ingest
from . import subject as subject_mod
from . import threads as threads_mod
from .accounts import get_account
from .database import utc_now

# How much of the body the list shows. The same rule the sync uses for incoming
# mail, so a draft's row reads like every other row.
PREVIEW = 200


def folder_for(con: sqlite3.Connection, account_id: int) -> int:
    """Where this account's drafts go.

    The server's Drafts folder when it has one, and a local folder when it does
    not — see store/folders.local_folder. Never an invented folder ON the
    server: creating one there is a change to the user's mailbox that saving a
    draft did not ask for.
    """
    folder = folders_repo.by_role(con, account_id, folders_repo.ROLE_DRAFTS)
    if folder is not None:
        return folder.id
    return folders_repo.local_folder(con, account_id, folders_repo.ROLE_DRAFTS,
                                     "Drafts")


def save(con: sqlite3.Connection, draft, *, message_id: str = "") -> tuple:
    """Write a draft, new or existing. Returns (row id, RFC Message-ID).

    `message_id` is only consulted when the draft has never been saved; after
    that the row's own is kept. The caller does not have to remember which case
    it is in, which is the point.
    """
    from ..compose import build as build_mod

    row_id = draft.message_id
    existing = _row(con, row_id) if row_id else None
    rfc_id = (existing["message_id"] if existing else "") or message_id or \
        build_mod.new_message_id(draft.from_address)

    body = draft.body or ""
    values = {
        "folder_id": folder_for(con, draft.account_id),
        "message_id": rfc_id,
        "in_reply_to": draft.in_reply_to or "",
        "references_": draft.references or "",
        "date_at": utc_now(),
        "from_name": draft.from_name or "",
        "from_addr": draft.from_address or "",
        "to_addrs": draft.to or "",
        "cc_addrs": draft.cc or "",
        "bcc_addrs": draft.bcc or "",
        "subject": draft.subject or "",
        "subject_base": subject_mod.strip_subject(draft.subject or ""),
        "body_text": body,
        "preview": " ".join(body.split())[:PREVIEW],
        "size_bytes": len(body.encode("utf-8")),
        "has_attachment": 1 if draft.attachments else 0,
    }

    if existing is None:
        columns = ", ".join(values)
        marks = ",".join("?" * len(values))
        cur = con.execute(
            f"INSERT INTO message ({columns}, received_at, uid, seen, draft, "
            f"deleted, pending_flags) VALUES ({marks}, ?, NULL, 1, 1, 0, '')",
            [*values.values(), utc_now()])
        row_id = int(cur.lastrowid)
    else:
        assignments = ", ".join(f"{name} = ?" for name in values)
        con.execute(f"UPDATE message SET {assignments} WHERE id = ?",
                    [*values.values(), row_id])
        ingest._fts_forget(con, [row_id])

    _write_attachments(con, row_id, draft.attachments)
    ingest.index_message(con, row_id)
    threads_mod.assign(con, row_id=row_id, message_id=rfc_id,
                       in_reply_to=draft.in_reply_to or "",
                       references=draft.references or "")
    con.commit()
    return row_id, rfc_id


def load(con: sqlite3.Connection, row_id: int):
    """The draft in this row, ready to be edited again."""
    from ..compose.draft import Attachment, Draft

    row = _row(con, row_id)
    if row is None:
        return None
    account_id = con.execute(
        "SELECT account_id FROM folder WHERE id = ?",
        (row["folder_id"],)).fetchone()[0]
    attachments = tuple(
        Attachment(path=a["stored_path"], filename=a["filename"],
                   content_type=a["content_type"])
        for a in con.execute(
            "SELECT filename, content_type, stored_path FROM attachment "
            "WHERE message_id = ? AND is_inline = 0 ORDER BY id",
            (row_id,)).fetchall())
    return Draft(account_id=int(account_id), from_address=row["from_addr"] or "",
                 from_name=row["from_name"] or "", to=row["to_addrs"] or "",
                 cc=row["cc_addrs"] or "", bcc=row["bcc_addrs"] or "",
                 subject=row["subject"] or "", body=row["body_text"] or "",
                 in_reply_to=row["in_reply_to"] or "",
                 references=row["references_"] or "",
                 attachments=attachments, message_id=row_id)


def discard(con: sqlite3.Connection, row_id: int) -> bool:
    """Throw a draft away. Nothing was ever on the server, so nothing is queued.

    The row is DELETED rather than moved to Trash, and that is the one place in
    this client where delete means erase: a draft the user abandoned is not
    correspondence, and a Trash folder full of half-written mail is a Trash
    folder nobody empties.
    """
    row = _row(con, row_id)
    if row is None or not row["draft"] or row["uid"] is not None:
        return False
    ingest._fts_forget(con, [row_id])
    con.execute("DELETE FROM attachment WHERE message_id = ?", (row_id,))
    con.execute("DELETE FROM message WHERE id = ?", (row_id,))
    con.commit()
    return True


def missing(draft) -> list[str]:
    """The attachments whose files are no longer where the draft says.

    Asked before sending. A file that moved between saving and sending is the
    price of not copying it into the store, and the honest response is to name
    it rather than to send a message with an attachment silently absent.
    """
    return [a.name for a in draft.attachments if not Path(a.path).is_file()]


def identity_of(con: sqlite3.Connection, draft):
    """The account this draft is sent from, as a row. None if it has gone."""
    return get_account(con, draft.account_id)


def _row(con: sqlite3.Connection, row_id) -> sqlite3.Row | None:
    if not row_id:
        return None
    return con.execute("SELECT * FROM message WHERE id = ?", (row_id,)).fetchone()


def _write_attachments(con: sqlite3.Connection, row_id: int, attachments) -> None:
    con.execute("DELETE FROM attachment WHERE message_id = ?", (row_id,))
    for position, attachment in enumerate(attachments, start=1):
        path = Path(attachment.path)
        size = path.stat().st_size if path.is_file() else 0
        con.execute("""
            INSERT INTO attachment (message_id, filename, content_type,
                content_id, size_bytes, part_number, stored_path, is_inline)
            VALUES (?, ?, ?, '', ?, ?, ?, 0)
        """, (row_id, attachment.name, attachment.content_type or "",
              size, str(position), str(attachment.path)))
