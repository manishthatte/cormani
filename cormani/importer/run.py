# SPDX-License-Identifier: GPL-3.0-or-later
#
# Putting Thunderbird's mail into corMani's store.
#
# THE PROFILE IS NEVER WRITTEN. Every mbox is opened read-only; resume state
# lives in corMani's own `import_folder` table. That is CONVENTIONS.txt §7 and
# the prototype's first rule, and it is why Thunderbird can stay open during
# an import.
#
# THE BYTES BECOME AN Envelope THROUGH `imap/envelope.read`. The importer does
# not have a second parser: a message that lands here must look the same as one
# that arrived over IMAP, or search, threading and the reader disagree about
# the same mail depending on how it got in. `store/ingest.store_message` is
# the only writer either path uses.
#
# THE SYNTHETIC UID IS THE BYTE OFFSET. An imported message has no IMAP UID,
# and UNIQUE (folder_id, uid) still needs a stable key across resumes. The
# offset of the `From ` separator is that key — unchanged for every message
# already in the file, and assigned to new ones as the file grows.
#
# FLAGS COME FROM X-Mozilla-Status WHEN PRESENT. Thunderbird records \Seen and
# friends there; without them every imported message would look unread, and a
# person who had already read fifteen years of mail would face a rail full of
# badges. Missing or unreadable status is treated as seen, because an archive
# that lights up as new is worse than one that does not.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..imap import envelope as envelope_mod
from ..store import database
from ..store import folders as folders_repo
from ..store import ingest
from . import discover
from . import mbox

# How often to commit during a long folder. Matches the prototype: holding the
# write lock across a 1.8 GB folder makes the window unreadable for minutes.
_BATCH = 500

# Thunderbird's status word: bit 0x0001 = read, 0x0002 = replied, 0x0004 = flagged.
_MOZ_STATUS = re.compile(br"^X-Mozilla-Status:\s*([0-9A-Fa-f]+)", re.M)
_SEEN = 0x0001
_ANSWERED = 0x0002
_FLAGGED = 0x0004


@dataclass
class Report:
    folders: int = 0
    new: int = 0
    skipped: int = 0          # unchanged since last pass
    unreadable: int = 0
    roots: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def describe(self) -> str:
        if not self.roots and not self.folders:
            return "no Thunderbird mail folders found"
        parts = [f"{self.new} message{'s' if self.new != 1 else ''} imported"]
        if self.folders:
            parts.append(f"across {self.folders} folder{'s' if self.folders != 1 else ''}")
        if self.skipped:
            parts.append(f"{self.skipped} unchanged")
        if self.unreadable:
            parts.append(f"{self.unreadable} unreadable")
        return ", ".join(parts)


def run(con: sqlite3.Connection, account_id: int, *,
        path: str | Path | None = None,
        include_junk: bool = False,
        attachments_root: Path | str | None = None,
        progress: Callable | None = None,
        home: Path | None = None) -> Report:
    """Import mbox files into one corMani account. Read-only on the sources."""
    report = Report()
    roots = discover.resolve_roots(path, home=home)
    report.roots = [str(r) for r in roots]
    if not roots:
        report.notes.append("nothing to import — name a profile, an ImapMail "
                            "directory, or a single mbox file")
        return report

    for source, account_key, label in discover.folder_files(
            roots, include_junk=include_junk):
        try:
            st = source.stat()
        except OSError as exc:
            report.notes.append(f"{source}: {exc}")
            continue
        n = _one_folder(con, account_id, source, account_key, label, st,
                        attachments_root=attachments_root, progress=progress,
                        report=report)
        if n is None:
            report.skipped += 1
        else:
            report.folders += 1
            report.new += n
    return report


def _one_folder(con, account_id, source: Path, account_key: str, label: str,
                st, *, attachments_root, progress, report: Report) -> int | None:
    """Import one mbox. None when unchanged; else the count of messages seen."""
    source_path = str(source.resolve())
    row = con.execute(
        "SELECT * FROM import_folder WHERE account_id = ? AND source_path = ?",
        (account_id, source_path)).fetchone()

    start = 0
    folder_id = None
    if row is not None:
        folder_id = int(row["folder_id"])
        if (row["size_bytes"] == st.st_size
                and abs((row["mtime"] or 0) - st.st_mtime) < 1):
            return None
        if (st.st_size > row["size_bytes"]
                and mbox.is_separator_at(source, int(row["resume_offset"]))):
            start = int(row["resume_offset"])
        else:
            # Compacted or rewritten: drop what we had and read again.
            _forget_folder_messages(con, folder_id)
            start = 0

    if folder_id is None:
        folder_id = folders_repo.ensure_folder(
            con, account_id, discover.store_path(account_key, label),
            display_name=label.rsplit("/", 1)[-1],
            role=discover.role_for(label))

    n, last_off = 0, start
    for uid, end, raw in mbox.iter_mbox(source, start):
        last_off = end
        try:
            env = envelope_mod.read(raw)
        except Exception:                                    # pragma: no cover
            report.unreadable += 1
            continue
        if not env.message_id and not env.subject and not env.from_addr:
            # Empty or separator-only residue. Not worth a row.
            report.unreadable += 1
            continue
        flags = _flags_from(raw)
        ingest.store_message(
            con, folder_id, uid, env, flags=flags,
            internaldate=env.date_at,
            attachments_root=attachments_root,
            account_id=account_id, commit=False)
        n += 1
        if n % _BATCH == 0:
            _record(con, account_id, folder_id, source_path, st, last_off)
            con.commit()
            if progress:
                progress(f"{account_key}/{label}: {n}…")

    _record(con, account_id, folder_id, source_path, st, last_off)
    con.commit()
    if progress and n:
        progress(f"{account_key}/{label}: +{n}")
    return n


def _record(con, account_id, folder_id, source_path, st, resume_offset) -> None:
    con.execute("""
        INSERT INTO import_folder
            (account_id, folder_id, source_path, size_bytes, mtime,
             resume_offset, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_id, source_path) DO UPDATE SET
            folder_id = excluded.folder_id,
            size_bytes = excluded.size_bytes,
            mtime = excluded.mtime,
            resume_offset = excluded.resume_offset,
            updated_at = excluded.updated_at
    """, (account_id, folder_id, source_path, st.st_size, st.st_mtime,
          resume_offset, database.utc_now()))


def _forget_folder_messages(con, folder_id: int) -> None:
    uids = ingest.uids_in(con, folder_id)
    if uids:
        ingest.forget_uids(con, folder_id, uids, commit=False)


def _flags_from(raw: bytes) -> list[str]:
    m = _MOZ_STATUS.search(raw[:2048])
    if not m:
        return ["\\Seen"]
    try:
        status = int(m.group(1), 16)
    except ValueError:
        return ["\\Seen"]
    flags = []
    if status & _SEEN:
        flags.append("\\Seen")
    if status & _ANSWERED:
        flags.append("\\Answered")
    if status & _FLAGGED:
        flags.append("\\Flagged")
    return flags
