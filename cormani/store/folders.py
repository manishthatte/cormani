# SPDX-License-Identifier: GPL-3.0-or-later
#
# Folders: the ones the server has, and the roles they play.
#
# Two names for every folder, kept apart deliberately. `path` is the server's,
# byte for byte including its hierarchy delimiter, and is never normalised —
# it is the key used to talk to the server again, and a folder whose stored name
# has been tidied is a folder that can no longer be selected. `display_name` is
# what a person reads, and may be anything.
#
# `role` is the RFC 6154 special-use attribute when the server declares one:
# inbox, sent, drafts, trash, junk, archive, all. It is what makes "archive
# this" work across fifteen accounts whose archive folder is variously called
# Archive, All Mail, [Gmail]/All Mail and Arkiv. Code asks for a role; only the
# sync engine asks for a path.
#
# The listing order here is Thunderbird's, and is a judgement rather than a
# fact: the six roles in the order people use them, then everything else
# alphabetically. Sorting purely alphabetically puts Archive above Inbox, which
# is correct and useless.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence

# RFC 6154's attributes, minus \All which is Gmail-specific in practice and is
# mapped onto `archive` by the sync engine rather than carried separately.
ROLE_INBOX = "inbox"
ROLE_DRAFTS = "drafts"
ROLE_SENT = "sent"
ROLE_ARCHIVE = "archive"
ROLE_JUNK = "junk"
ROLE_TRASH = "trash"

ROLES = (ROLE_INBOX, ROLE_DRAFTS, ROLE_SENT, ROLE_ARCHIVE, ROLE_JUNK, ROLE_TRASH)

# A folder that exists HERE and not on the server. The prefix is a path no IMAP
# server can produce — a backslash is not a delimiter any of them use, and a
# folder called `\Local\...` would have to have been created deliberately.
#
# There is exactly one reason for these: a draft has to be somewhere, and an
# account whose server offers no Drafts folder must still be able to hold one.
# `imap/folders.sync_folders` skips them when it unsubscribes the folders the
# server stopped mentioning, because the server was never going to mention
# these.
LOCAL_PREFIX = "\\Local\\"


def is_local(path: str) -> bool:
    return (path or "").startswith(LOCAL_PREFIX)

_ROLE_RANK = {role: n for n, role in enumerate(ROLES)}

ROLE_LABELS = {
    ROLE_INBOX: "Inbox",
    ROLE_DRAFTS: "Drafts",
    ROLE_SENT: "Sent",
    ROLE_ARCHIVE: "Archive",
    ROLE_JUNK: "Junk",
    ROLE_TRASH: "Trash",
}


@dataclass(frozen=True)
class Folder:
    id: int
    account_id: int
    path: str
    display_name: str
    role: str
    parent_id: int | None
    subscribed: bool

    @property
    def label(self) -> str:
        return label_for(self.display_name, self.path)


def label_for(display_name: str, path: str) -> str:
    """The last path segment when nothing better was given.

    Both delimiters are tried because IMAP servers use either, and a folder
    shown as "INBOX/Lists/debian" instead of "debian" is a rail that gets wider
    with every level of nesting.

    A FUNCTION as well as a property, because the message list needs the same
    answer from a joined row rather than from a Folder — search results name
    the folder each hit is in, and building a Folder per row to read one string
    off it is a query per row.
    """
    if display_name:
        return display_name
    tail = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail or path


def _folder(row: sqlite3.Row) -> Folder:
    return Folder(id=row["id"], account_id=row["account_id"], path=row["path"],
                  display_name=row["display_name"], role=row["role"],
                  parent_id=row["parent_id"], subscribed=bool(row["subscribed"]))


def _sort_key(f: Folder) -> tuple:
    return (_ROLE_RANK.get(f.role, len(ROLES)), f.label.lower(), f.id)


def list_folders(con: sqlite3.Connection, account_id: int, *,
                 subscribed_only: bool = True) -> list[Folder]:
    where = "WHERE account_id = ?" + (" AND subscribed = 1" if subscribed_only else "")
    rows = con.execute(f"SELECT * FROM folder {where}", (account_id,)).fetchall()
    return sorted((_folder(r) for r in rows), key=_sort_key)


def get_folder(con: sqlite3.Connection, folder_id: int) -> Folder | None:
    row = con.execute("SELECT * FROM folder WHERE id = ?", (folder_id,)).fetchone()
    return _folder(row) if row else None


def local_folder(con: sqlite3.Connection, account_id: int, role: str,
                 display_name: str) -> int:
    """This account's local folder for a role, made if it is not there.

    The one place a folder is invented, and it is invented HERE rather than on
    the server: `by_role` returns None for a server with no Drafts folder
    precisely so that nothing creates one behind the user's back, and this
    creates a local one instead — visible, subscribed, and never confused for
    the server's because of its path.
    """
    return ensure_folder(con, account_id, f"{LOCAL_PREFIX}{role}",
                         display_name=display_name, role=role)


def by_role(con: sqlite3.Connection, account_id: int, role: str) -> Folder | None:
    """One account's folder for a role, or None when the server has no such
    folder. None rather than an invented one: a client that silently creates an
    Archive folder on a server that has none has made a change nobody asked for.
    """
    row = con.execute(
        "SELECT * FROM folder WHERE account_id = ? AND role = ? "
        "ORDER BY id LIMIT 1", (account_id, role)).fetchone()
    return _folder(row) if row else None


def ids_by_role(con: sqlite3.Connection, role: str, *,
                account_ids: Sequence[int] | None = None) -> list[int]:
    """Every folder with this role, across accounts. The unified views' input."""
    sql = "SELECT id FROM folder WHERE role = ?"
    params: list = [role]
    if account_ids is not None:
        if not account_ids:
            return []
        sql += f" AND account_id IN ({','.join('?' * len(account_ids))})"
        params.extend(account_ids)
    return [r[0] for r in con.execute(sql, params).fetchall()]


def ensure_folder(con: sqlite3.Connection, account_id: int, path: str, *,
                  display_name: str = "", role: str = "",
                  parent_id: int | None = None) -> int:
    """Create the folder if this account has not seen it, and return its id.

    Idempotent on (account_id, path), which is the schema's unique key: the sync
    engine re-lists folders on every connection and must not accumulate
    duplicates, nor lose the sync state stored against the existing row.
    """
    row = con.execute("SELECT id FROM folder WHERE account_id = ? AND path = ?",
                      (account_id, path)).fetchone()
    if row:
        if role:
            con.execute("UPDATE folder SET role = ? WHERE id = ?", (role, row[0]))
            con.commit()
        return int(row[0])
    cur = con.execute("""
        INSERT INTO folder (account_id, path, display_name, role, parent_id, subscribed)
        VALUES (?, ?, ?, ?, ?, 1)
    """, (account_id, path, display_name, role, parent_id))
    con.commit()
    return int(cur.lastrowid)


# ------------------------------------------------------- the sync's writers
def update_folder(con: sqlite3.Connection, folder_id: int, *,
                  display_name: str | None = None, role: str | None = None,
                  subscribed: bool | None = None, commit: bool = True) -> bool:
    """Change what the server told us about a folder. Returns whether it moved.

    Every field is optional and None means "leave it alone", because the folder
    sync learns different things from different responses — LIST gives the
    attributes, LSUB gives the subscription — and a writer that took all of
    them at once would have to invent the ones it did not know. The parent is
    NOT among them — see `set_parent`, where None has a meaning of its own.
    """
    sets, params = [], []
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(display_name)
    if role is not None:
        sets.append("role = ?")
        params.append(role)
    if subscribed is not None:
        sets.append("subscribed = ?")
        params.append(1 if subscribed else 0)
    if not sets:
        return False
    params.append(folder_id)
    cur = con.execute(f"UPDATE folder SET {', '.join(sets)} WHERE id = ?", params)
    if commit:
        con.commit()
    return cur.rowcount > 0


def set_parent(con: sqlite3.Connection, folder_id: int, parent_id: int | None,
               *, commit: bool = True) -> None:
    """Separate from `update_folder` because None is a MEANING here — the
    folder is at the top level — rather than "unchanged"."""
    con.execute("UPDATE folder SET parent_id = ? WHERE id = ?",
                (parent_id, folder_id))
    if commit:
        con.commit()


def record_sync_state(con: sqlite3.Connection, folder_id: int, *,
                      uid_validity: int | None = None,
                      uid_next: int | None = None,
                      highest_modseq: int | None = None,
                      last_synced_at: str | None = None,
                      commit: bool = True) -> None:
    """Where the sync got to. None leaves a value alone.

    Written AFTER the messages, never before. If the process dies in between,
    a folder whose state says less than it holds re-fetches a few messages the
    store already has — which the ingest path makes harmless. The other order
    would skip messages permanently.
    """
    sets, params = [], []
    for column, value in (("uid_validity", uid_validity), ("uid_next", uid_next),
                          ("highest_modseq", highest_modseq),
                          ("last_synced_at", last_synced_at)):
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    if not sets:
        return
    params.append(folder_id)
    con.execute(f"UPDATE folder SET {', '.join(sets)} WHERE id = ?", params)
    if commit:
        con.commit()


def sync_state(con: sqlite3.Connection, folder_id: int) -> dict:
    row = con.execute(
        "SELECT uid_validity, uid_next, highest_modseq, last_synced_at "
        "FROM folder WHERE id = ?", (folder_id,)).fetchone()
    return dict(row) if row else {}


def discard_contents(con: sqlite3.Connection, folder_id: int, *,
                     commit: bool = True) -> int:
    """Throw away everything cached for a folder. Returns the messages removed.

    The one place corMani destroys local mail without the user asking, and it
    is correct: the server changes UIDVALIDITY exactly to say "every UID you
    hold for this mailbox is meaningless". Keeping the rows would leave
    messages that can never be matched to the server again, can never be
    flagged or moved, and would be duplicated by the re-download that follows.

    Deliberately NOT used when a folder merely disappears from LIST — see
    `imap/folders.py`. A vanished folder might be a rename or a server having a
    bad morning; an invalidated UIDVALIDITY is the server being explicit.
    """
    from . import ingest                      # local: store must not cycle
    uids = list(ingest.uids_in(con, folder_id))
    removed = ingest.forget_uids(con, folder_id, uids, commit=False)
    con.execute("UPDATE folder SET uid_next = NULL, highest_modseq = NULL "
                "WHERE id = ?", (folder_id,))
    if commit:
        con.commit()
    return removed
