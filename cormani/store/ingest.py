# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing what the sync engine fetched.
#
# `messages.py` reads the list and changes flags; this is the other half — the
# only place a message ROW is created, and the only place an attachment reaches
# the disk. Kept apart from `messages.py` because the risks are different: that
# module's mistakes show a wrong list, this one's lose mail or write a file
# where a message told it to.
#
# Five decisions worth the words:
#
# THE FILENAME IN A MESSAGE IS HOSTILE INPUT. It arrived from a stranger over
# the internet and it is used to name a file. `../../.ssh/authorized_keys` is
# the obvious attempt, `.` and `..` and a 4,000-character name and a NUL are
# the ones that get forgotten. Every name goes through `safe_filename`, the
# result is placed under a directory derived from ids and never from content,
# and the resolved path is checked to be inside the attachments root before
# anything is opened. Two independent barriers, because one is a typo away from
# none. CONVENTIONS.txt §7.
#
# received_at IS THE SERVER'S ARRIVAL TIME, NOT THE IMPORT TIME. Migration 1
# calls it "when this store first saw it", and read literally that would stamp
# every message of a first import with today — making "what arrived while I was
# away" useless on the one day it matters most. INTERNALDATE is the server's
# own record of arrival, it survives a re-import, and it is what the fixtures
# already put there. The literal reading is the one that is wrong.
#
# THE FTS INDEX IS MAINTAINED BY HAND, AND DELETING FROM IT NEEDS THE OLD ROW.
# Migration 2 declares `content=''`, so there are no triggers — deliberately,
# because a trigger firing per row turns a first import from a minute into an
# hour. The cost is that FTS5 cannot delete a row it does not store: the old
# column values must be handed back to it. Every path that removes or replaces
# a message therefore reads the row FIRST. Forgetting leaves a phantom in the
# index that search will return and the reader cannot open.
#
# ONE MESSAGE IS ONE TRANSACTION, UNLESS THE CALLER SAYS OTHERWISE. A first
# import of a busy Gmail account is tens of thousands of messages, and a
# transaction each is the difference between minutes and an evening. The
# `commit` flag lets the sync engine batch; the default is the safe one.
#
# THE SAME MESSAGE IN TWO FOLDERS IS TWO ROWS, ON PURPOSE. Gmail presents
# labels as folders and one message appears under several. De-duplicating by
# Message-ID would make archiving from the unified inbox remove it from a label
# the user never touched. The folder is the unit; the schema's UNIQUE
# (folder_id, uid) says the same.
#
# THE STORE DOES NOT IMPORT THE PROTOCOL. `Envelope` and `Part` appear here
# only as annotations, so they are imported under TYPE_CHECKING and the
# dependency runs one way at run time: `imap` knows about `store`, and `store`
# knows nothing about IMAP. That is what keeps a second source of messages —
# stage 4's own sent mail, the Thunderbird importer — from having to pretend to
# be an IMAP fetch to get written.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from . import contacts
from . import threads
from .database import utc_now

if TYPE_CHECKING:                    # annotations only — see the note below
    from ..imap.envelope import Envelope, Part

# IMAP's system flags, and the columns they are kept in. Keywords a server
# invents are ignored here rather than guessed at; tags are the user's.
FLAG_COLUMNS = {
    "\\seen": "seen",
    "\\flagged": "flagged",
    "\\answered": "answered",
    "\\draft": "draft",
    "\\deleted": "deleted",
}
FLAG_NAMES = {column: flag for flag, column in FLAG_COLUMNS.items()}

# Long enough for any real name, short enough that the path stays inside the
# limit on every filesystem corMani is meant to run on — including eCryptfs,
# whose per-component ceiling is 143 bytes rather than 255.
_MAX_FILENAME = 120
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]")


@dataclass(frozen=True)
class Ingested:
    message_id: int
    created: bool
    attachments: int


# --------------------------------------------------------------- filenames
def safe_filename(name: str, *, fallback: str = "part") -> str:
    """A name from a message, made safe to place in a directory.

    Not a sanitiser for display — the stored name is what the file is CALLED on
    disk, and the original stays in `attachment.filename` for the interface to
    show. Everything structural is removed: separators of both kinds, control
    characters, leading dots, and the two reserved names.
    """
    name = unicodedata.normalize("NFKC", name or "")
    # Both separators, whichever platform wrote the message and whichever is
    # reading it. A Windows path in a message must not become a path here.
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = _UNSAFE.sub("_", name).strip(" .")
    # `..` cannot survive as a whole component; a name that reduces to nothing
    # or to a dot run gets the fallback rather than an empty path segment.
    if not name or set(name) <= {".", "_", " "}:
        name = fallback
    if len(name) > _MAX_FILENAME:
        stem, dot, suffix = name.rpartition(".")
        if dot and 0 < len(suffix) <= 10:
            name = stem[:_MAX_FILENAME - len(suffix) - 1] + "." + suffix
        else:
            name = name[:_MAX_FILENAME]
    return name


def attachment_path(root: Path, account_id: int, message_id: int,
                    index: int, filename: str) -> Path:
    """Where one attachment's bytes go, checked to be inside `root`.

    The directory comes from identifiers only. Nothing derived from the message
    contributes to a path component except the final name, and that has been
    through `safe_filename` — after which the result is resolved and compared
    against the root anyway. The second check is not redundant: it is what
    catches the next mistake in the first.
    """
    root = Path(root).resolve()
    target = (root / str(int(account_id)) / str(int(message_id)) /
              f"{int(index)}-{safe_filename(filename)}")
    resolved = Path(target).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(
            f"attachment path escapes the store: {resolved} is not under {root}")
    return resolved


# --------------------------------------------------------------------- FTS
def _fts_values(row: Mapping) -> tuple[str, str, str, str]:
    from_repr = " ".join(x for x in (row["from_name"], row["from_addr"]) if x)
    to_repr = " ".join(x for x in (row["to_addrs"], row["cc_addrs"]) if x)
    return (row["subject"] or "", row["body_text"] or "", from_repr, to_repr)


def index_message(con: sqlite3.Connection, message_id: int) -> bool:
    """Put an existing message row into the search index.

    Public because `store.fixtures` needs it: a writer that makes rows without
    indexing them leaves an index that cannot safely be deleted from. See
    `_fts_forget`.
    """
    row = con.execute(
        "SELECT id, subject, body_text, from_name, from_addr, to_addrs, cc_addrs "
        "FROM message WHERE id = ?", (message_id,)).fetchone()
    if row is None or indexed_rowids(con, [message_id]):
        return False
    _fts_insert(con, int(row["id"]), row)
    return True


def _fts_insert(con: sqlite3.Connection, message_id: int, row: Mapping) -> None:
    subject, body, from_repr, to_repr = _fts_values(row)
    con.execute(
        "INSERT INTO message_fts (rowid, subject, body, from_repr, to_repr) "
        "VALUES (?, ?, ?, ?, ?)", (message_id, subject, body, from_repr, to_repr))


def indexed_rowids(con: sqlite3.Connection, message_ids: Sequence[int]) -> set:
    """Which of these the index actually holds.

    A contentless FTS5 table will not tell you what is in it — reading a column
    is an error — but the ROWID is readable, and that is all this needs.
    """
    if not message_ids:
        return set()
    marks = ",".join("?" * len(message_ids))
    return {int(r[0]) for r in con.execute(
        f"SELECT rowid FROM message_fts WHERE rowid IN ({marks})",
        list(message_ids)).fetchall()}


def _fts_forget(con: sqlite3.Connection, message_ids: Sequence[int]) -> None:
    """Remove rows from the index, reading their old values first.

    FTS5 with `content=''` stores nothing, so it cannot look up what it is
    being asked to delete — the previous column values are the delete command's
    arguments. A path that skips this leaves a phantom that search returns and
    the reader cannot open.

    AND IT MUST NOT BE ASKED TO DELETE A ROW IT NEVER HAD. That is not a no-op
    and it is not an error either: FTS5 subtracts the terms anyway, the
    posting lists go negative, and the next read fails with `database disk
    image is malformed` — an index that reports corruption for a message that
    was merely never indexed. Any writer that creates a `message` row without
    indexing it leaves that landmine, and there are such writers: the demo
    fixtures, and anything a future stage adds. Filtering here is what makes
    the index's integrity independent of every one of them.
    """
    if not message_ids:
        return
    present = indexed_rowids(con, message_ids)
    if not present:
        return
    marks = ",".join("?" * len(present))
    rows = con.execute(
        f"SELECT id, subject, body_text, from_name, from_addr, to_addrs, cc_addrs "
        f"FROM message WHERE id IN ({marks})", sorted(present)).fetchall()
    for row in rows:
        subject, body, from_repr, to_repr = _fts_values(row)
        con.execute(
            "INSERT INTO message_fts (message_fts, rowid, subject, body, "
            "from_repr, to_repr) VALUES ('delete', ?, ?, ?, ?, ?)",
            (row["id"], subject, body, from_repr, to_repr))


# ---------------------------------------------------------------- writing
_INSERT = """
INSERT INTO message (folder_id, uid, message_id, in_reply_to, references_,
    thread_key, date_at, received_at, from_name, from_addr, to_addrs, cc_addrs,
    bcc_addrs, reply_to, subject, subject_base, body_text, body_html, preview,
    size_bytes, has_attachment, seen, flagged, answered, draft, deleted,
    is_bulk, is_bounce, bounce_rcpt, bounce_status, bounce_diag,
    pending_flags)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?, ?, '')
ON CONFLICT (folder_id, uid) DO UPDATE SET
    message_id = excluded.message_id, in_reply_to = excluded.in_reply_to,
    references_ = excluded.references_, thread_key = excluded.thread_key,
    date_at = excluded.date_at, received_at = excluded.received_at,
    from_name = excluded.from_name, from_addr = excluded.from_addr,
    to_addrs = excluded.to_addrs, cc_addrs = excluded.cc_addrs,
    bcc_addrs = excluded.bcc_addrs, reply_to = excluded.reply_to,
    subject = excluded.subject, subject_base = excluded.subject_base,
    body_text = excluded.body_text, body_html = excluded.body_html,
    preview = excluded.preview, size_bytes = excluded.size_bytes,
    has_attachment = excluded.has_attachment, seen = excluded.seen,
    flagged = excluded.flagged, answered = excluded.answered,
    draft = excluded.draft, deleted = excluded.deleted,
    is_bulk = excluded.is_bulk, is_bounce = excluded.is_bounce,
    bounce_rcpt = excluded.bounce_rcpt, bounce_status = excluded.bounce_status,
    bounce_diag = excluded.bounce_diag
"""


def flags_to_columns(flags: Iterable[str]) -> dict[str, int]:
    """IMAP flags to the five columns. Unknown keywords are ignored, not lost —
    a server's own keywords are its business, and tags are the user's."""
    lowered = {str(f).strip().lower() for f in flags}
    return {column: (1 if flag in lowered else 0)
            for flag, column in FLAG_COLUMNS.items()}


def store_message(con: sqlite3.Connection, folder_id: int, uid: int,
                  env: Envelope, *, flags: Iterable[str] = (),
                  internaldate: str | None = None,
                  attachments_root: Path | str | None = None,
                  account_id: int | None = None,
                  commit: bool = True) -> Ingested:
    """Write one fetched message. Idempotent on (folder_id, uid).

    Re-running a sync that was interrupted must not duplicate anything, which
    is what the conflict clause is for: the second write of the same UID
    updates the row it already made rather than failing or doubling it.
    """
    columns = flags_to_columns(flags)
    existing = con.execute(
        "SELECT id FROM message WHERE folder_id = ? AND uid = ?",
        (folder_id, uid)).fetchone()
    if existing is None and env.message_id:
        # A COPY THIS CLIENT MADE, meeting the server's copy of itself. Sending
        # files the message locally with no UID — see smtp/outbox.py — and the
        # server's own copy arrives on a later sync with a UID and the same
        # Message-ID. Adopting the row rather than inserting is what stops every
        # sent message appearing twice; the conflict clause below then updates
        # it with what the server actually holds.
        existing = con.execute(
            "SELECT id FROM message WHERE folder_id = ? AND uid IS NULL "
            "AND message_id = ? ORDER BY id LIMIT 1",
            (folder_id, env.message_id)).fetchone()
        if existing is not None:
            con.execute("UPDATE message SET uid = ? WHERE id = ?",
                        (uid, existing["id"]))

    if existing is not None:
        _fts_forget(con, [existing["id"]])
        _remove_attachments(con, existing["id"])

    con.execute(_INSERT, (
        folder_id, uid, env.message_id, env.in_reply_to, env.references,
        # Provisional: what the message claims, with nothing else consulted.
        # `threads.assign` below replaces it with the thread it actually joins,
        # and does so inside this same call so no row is ever visible unthreaded.
        threads.claimed_root(env.message_id or "", env.in_reply_to or "",
                             env.references or ""), env.date_at, internaldate or env.date_at or utc_now(),
        env.from_name, env.from_addr, env.to_addrs, env.cc_addrs, env.bcc_addrs,
        env.reply_to, env.subject, env.subject_base, env.body_text,
        env.body_html, env.preview, env.size_bytes,
        1 if env.has_attachment else 0,
        columns["seen"], columns["flagged"], columns["answered"],
        columns["draft"], columns["deleted"],
        # What the message says about its own nature. `imap/delivery.py`
        # derived it; this is the only place it is written, because this is the
        # only place a message row is made.
        1 if env.delivery.is_bulk else 0,
        1 if env.delivery.is_bounce else 0,
        env.delivery.recipient, env.delivery.status,
        env.delivery.diagnostic[:500]))

    row = con.execute("SELECT * FROM message WHERE folder_id = ? AND uid = ?",
                      (folder_id, uid)).fetchone()
    message_id = int(row["id"])
    _fts_insert(con, message_id, row)
    # THE THREAD IS DECIDED HERE AND NOT IN THE ENVELOPE, because it is not a
    # property of the message: it depends on what the store already holds. A
    # reply can arrive before the message it answers, and the two are one
    # conversation only once both are in front of the same query.
    threads.assign(con, row_id=message_id, message_id=env.message_id or "",
                   in_reply_to=env.in_reply_to or "", references=env.references or "")

    # THE BOUNCE GUARD IS TOLD HERE, and until stage 6 nothing told it at all:
    # `contacts.note_bounce` was written for stage 4's composer and had no
    # caller, so the guard only ever knew what a person typed. Inside this
    # transaction, so a store can never hold a delivery failure whose guard
    # entry was never made.
    #
    # ONLY A PERMANENT FAILURE COUNTS. A 4.x.x is the sending server saying it
    # will try again, and marking an address dead over a full mailbox on
    # Tuesday is how a working correspondent becomes unreachable.
    if env.delivery.is_bounce and env.delivery.recipient \
            and env.delivery.permanent:
        contacts.note_bounce(
            con, env.delivery.recipient,
            env.delivery.diagnostic or env.delivery.status or "delivery failed",
            when=env.date_at or utc_now(),
            # The DSN's OWN Message-ID, so a re-fetch of the same failure is
            # the same failure. Falling back to the recipient and the folder
            # keeps a report that carries no Message-ID from counting twice on
            # every sync — it costs one missed count if two genuinely different
            # failures for one address land in one folder unidentified, which
            # is the better way round.
            key=env.message_id or f"rcpt:{env.delivery.recipient}@{folder_id}",
            commit=False)

    written = 0
    if env.parts:
        if account_id is None:
            account_id = con.execute(
                "SELECT account_id FROM folder WHERE id = ?",
                (folder_id,)).fetchone()["account_id"]
        written = _write_attachments(con, message_id, int(account_id), env.parts,
                                     attachments_root)

    if commit:
        con.commit()
    return Ingested(message_id=message_id, created=existing is None,
                    attachments=written)


def _write_attachments(con: sqlite3.Connection, message_id: int, account_id: int,
                       parts: Sequence[Part],
                       attachments_root: Path | str | None) -> int:
    written = 0
    for index, part in enumerate(parts, start=1):
        stored = ""
        if attachments_root is not None and part.payload:
            path = attachment_path(Path(attachments_root), account_id,
                                   message_id, index, part.filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(part.payload)
            stored = str(path)
            written += 1
        con.execute("""
            INSERT INTO attachment (message_id, filename, content_type,
                content_id, size_bytes, part_number, stored_path, is_inline)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (message_id, part.filename, part.content_type, part.content_id,
              part.size_bytes, part.part_number, stored,
              1 if part.is_inline else 0))
    return written


def _remove_attachments(con: sqlite3.Connection, message_id: int) -> None:
    """Drop a message's attachment rows and the files they named.

    Only files this store recorded in `stored_path` are unlinked, and a missing
    one is not an error: the directory is disposable in a way the database is
    not, and refusing to proceed because a file was already gone would stop a
    sync over nothing.
    """
    rows = con.execute(
        "SELECT stored_path FROM attachment WHERE message_id = ?",
        (message_id,)).fetchall()
    for row in rows:
        stored = row["stored_path"]
        if not stored:
            continue
        try:
            Path(stored).unlink()
        except OSError:
            pass
    con.execute("DELETE FROM attachment WHERE message_id = ?", (message_id,))


# ---------------------------------------------------------------- updating
def update_flags(con: sqlite3.Connection, folder_id: int,
                 by_uid: Mapping[int, Sequence[str]], *,
                 commit: bool = True) -> int:
    """Apply the server's flags to messages already stored. Returns rows changed.

    Only the five system flags are touched, and only for UIDs this store has:
    a flag arriving for a UID that was never fetched is a message the sync has
    not reached yet, not a row to invent.
    """
    changed = 0
    for uid, flags in by_uid.items():
        columns = flags_to_columns(flags)
        cur = con.execute(
            "UPDATE message SET seen = ?, flagged = ?, answered = ?, draft = ?, "
            "deleted = ? WHERE folder_id = ? AND uid = ? AND ("
            "seen <> ? OR flagged <> ? OR answered <> ? OR draft <> ? OR deleted <> ?)",
            (columns["seen"], columns["flagged"], columns["answered"],
             columns["draft"], columns["deleted"], folder_id, uid,
             columns["seen"], columns["flagged"], columns["answered"],
             columns["draft"], columns["deleted"]))
        changed += cur.rowcount
    if commit:
        con.commit()
    return changed


def forget_uids(con: sqlite3.Connection, folder_id: int, uids: Iterable[int], *,
                commit: bool = True) -> int:
    """Remove messages the server no longer has. Index and files go with them."""
    uids = [int(u) for u in uids]
    if not uids:
        return 0
    removed = 0
    for chunk in _chunked(uids, 500):
        marks = ",".join("?" * len(chunk))
        ids = [r[0] for r in con.execute(
            f"SELECT id FROM message WHERE folder_id = ? AND uid IN ({marks})",
            [folder_id, *chunk]).fetchall()]
        if not ids:
            continue
        _fts_forget(con, ids)
        for message_id in ids:
            _remove_attachments(con, message_id)
        id_marks = ",".join("?" * len(ids))
        cur = con.execute(f"DELETE FROM message WHERE id IN ({id_marks})", ids)
        removed += cur.rowcount
    if commit:
        con.commit()
    return removed


def _chunked(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


# ------------------------------------------------------------------ asking
def uids_in(con: sqlite3.Connection, folder_id: int) -> set[int]:
    """Every UID this store holds for a folder — the input to expunge detection."""
    return {int(r[0]) for r in con.execute(
        "SELECT uid FROM message WHERE folder_id = ? AND uid IS NOT NULL",
        (folder_id,)).fetchall()}


def local_flags(con: sqlite3.Connection, folder_id: int) -> dict[int, frozenset[str]]:
    """This store's flags per UID, in the server's vocabulary, for comparison."""
    out: dict[int, frozenset[str]] = {}
    for row in con.execute(
            "SELECT uid, seen, flagged, answered, draft, deleted FROM message "
            "WHERE folder_id = ? AND uid IS NOT NULL", (folder_id,)).fetchall():
        out[int(row["uid"])] = frozenset(
            FLAG_NAMES[column] for column in FLAG_COLUMNS.values() if row[column])
    return out


def rebuild_search_index(con: sqlite3.Connection) -> int:
    """Throw the index away and build it from the messages. Returns the count.

    Kept because an external-content index cannot be repaired — it has no copy
    of the text to check itself against — so the only honest answer to a
    suspect index is to make it again.
    """
    # NOT `DELETE FROM message_fts`, which SQLite refuses on a contentless
    # table — there is nothing there to delete rows FROM. `delete-all` is the
    # command that empties one, and it is the only way to start again.
    con.execute("INSERT INTO message_fts (message_fts) VALUES ('delete-all')")
    written = 0
    for row in con.execute(
            "SELECT id, subject, body_text, from_name, from_addr, to_addrs, "
            "cc_addrs FROM message").fetchall():
        _fts_insert(con, int(row["id"]), row)
        written += 1
    con.commit()
    return written
