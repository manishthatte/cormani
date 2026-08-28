# SPDX-License-Identifier: GPL-3.0-or-later
#
# The server's folders, and which of them play a role.
#
# `store/folders.py` explains why a role exists: it is what makes "archive
# this" work across fifteen accounts whose archive folder is variously called
# Archive, All Mail, [Gmail]/All Mail and Arkiv. This module is where a role is
# decided, and it decides in three tiers, in this order:
#
#   1. INBOX, by name. RFC 3501 says the name is case-insensitive and always
#      means the inbox. Not a heuristic — a rule.
#   2. The RFC 6154 special-use attribute, when the server declares one.
#   3. The folder's name against a table, ONLY for servers that declare
#      nothing. Most do declare now; the tier exists for the ones that do not,
#      and it is last because a folder genuinely called "Archive of 2019" must
#      not become the account's Archive when the server has said which one is.
#
# \All IS NOT \Archive, EXCEPT WHEN IT IS. Gmail declares \All on All Mail and
# has no \Archive at all, so archiving on a Gmail account has to mean All Mail.
# A server offering both — Fastmail does — must use the real \Archive, so \All
# is only taken as archive once every mailbox has been seen and none claimed
# it. That is why the roles are decided over the whole listing rather than one
# line at a time.
#
# A FOLDER THAT VANISHES IS NOT DELETED. Removing it would cascade to every
# message in it, and the local store is the user's archive: a rename, or a
# server having a bad morning, would silently destroy mail that no longer
# exists anywhere else. Vanished folders are unsubscribed and REPORTED, and
# removing them stays a deliberate act. `store.folders.discard_contents` is the
# one place that does destroy, and only on the server's explicit say-so.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..store import folders as repo
from ..store.database import utc_now
from . import parse
from .client import Connection

# RFC 6154's attributes to the roles the store keeps.
_SPECIAL_USE = {
    "\\drafts": repo.ROLE_DRAFTS,
    "\\sent": repo.ROLE_SENT,
    "\\trash": repo.ROLE_TRASH,
    "\\junk": repo.ROLE_JUNK,
    "\\spam": repo.ROLE_JUNK,           # not RFC 6154, but servers send it
    "\\archive": repo.ROLE_ARCHIVE,
    "\\inbox": repo.ROLE_INBOX,
}

# The last resort, for servers declaring nothing. Matched against the final
# path segment, decoded and lower-cased. English and the two other languages
# the correspondence is in; anything else is left as an ordinary folder, which
# is honest — a wrong role is worse than none, because "archive this" would
# quietly file mail somewhere the user does not look.
_BY_NAME = {
    repo.ROLE_SENT: ("sent", "sent items", "sent mail", "sent messages",
                     "gesendet", "envoyés"),
    repo.ROLE_DRAFTS: ("drafts", "draft", "entwürfe", "brouillons"),
    repo.ROLE_TRASH: ("trash", "deleted items", "deleted messages", "bin",
                      "papierkorb", "corbeille"),
    repo.ROLE_JUNK: ("junk", "spam", "junk e-mail", "junk email", "bulk mail"),
    repo.ROLE_ARCHIVE: ("archive", "archives", "all mail", "archiv"),
}


@dataclass(frozen=True)
class FolderReport:
    """What one folder listing changed. Reported rather than logged, because
    the interface has to be able to say why a folder appeared or left."""

    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    vanished: tuple[str, ...] = ()
    roles: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.updated) + len(self.vanished)


def _tail(box: parse.Mailbox) -> str:
    """The last path segment, decoded — what a person calls the folder."""
    name = box.display_name or box.path
    delimiter = box.delimiter or "/"
    return name.rstrip(delimiter).rsplit(delimiter, 1)[-1] or name


def assign_roles(boxes: list) -> dict:
    """Decide every folder's role over the WHOLE listing.

    One pass per tier, and one role per account: the first claimant wins, so a
    declared \\Sent is never displaced by a folder that happens to be called
    "Sent Items". The listing order is the server's, which is stable.
    """
    roles: dict = {}
    taken: set = set()

    def claim(path: str, role: str) -> None:
        if role in taken or path in roles:
            return
        roles[path] = role
        taken.add(role)

    # 1 — INBOX by name, which is a rule and not a guess.
    for box in boxes:
        if box.path.upper() == "INBOX":
            claim(box.path, repo.ROLE_INBOX)

    # 2 — what the server declared.
    for box in boxes:
        for attribute in box.attributes:
            role = _SPECIAL_USE.get(attribute.lower())
            if role:
                claim(box.path, role)

    # 2b — \All, only if nothing claimed \Archive. Gmail has no \Archive and
    # All Mail is where archived mail goes; Fastmail has both, and there the
    # real Archive must win.
    if repo.ROLE_ARCHIVE not in taken:
        for box in boxes:
            if any(a.lower() == "\\all" for a in box.attributes):
                claim(box.path, repo.ROLE_ARCHIVE)

    # 3 — by name, only for what is still unclaimed.
    for role, names in _BY_NAME.items():
        if role in taken:
            continue
        for box in boxes:
            if box.path in roles or not box.selectable:
                continue
            if _tail(box).strip().lower() in names:
                claim(box.path, role)
                break
    return roles


def _parent_of(path: str, delimiter: str, known: dict) -> int | None:
    """The id of the folder above this one, when this account has it.

    Nesting is by the server's delimiter, which differs per server and is why
    it is carried on every LIST line rather than assumed.
    """
    if not delimiter or delimiter not in path:
        return None
    parent_path = path.rsplit(delimiter, 1)[0]
    return known.get(parent_path)


def sync_folders(con: sqlite3.Connection, connection: Connection,
                 account_id: int) -> FolderReport:
    """Bring one account's folder table in line with its server.

    Runs on every connection, so it must be cheap and idempotent — the store's
    `ensure_folder` is keyed on (account_id, path) for exactly that reason, and
    the sync state held against an existing row is never disturbed.
    """
    boxes = [b for b in connection.list_mailboxes() if b.selectable]
    roles = assign_roles(boxes)
    subscribed = connection.subscribed_paths()

    before = {f.path: f for f in repo.list_folders(con, account_id,
                                                   subscribed_only=False)}
    added: list = []
    updated: list = []
    ids: dict = {}

    # Shortest path first, so a parent always exists before its children ask
    # for its id.
    for box in sorted(boxes, key=lambda b: (b.path.count(b.delimiter or "/"), b.path)):
        role = roles.get(box.path, "")
        existing = before.get(box.path)
        display = _tail(box)
        is_subscribed = (box.path in subscribed) if subscribed else True

        if existing is None:
            folder_id = repo.ensure_folder(con, account_id, box.path,
                                           display_name=display, role=role)
            if not is_subscribed:
                # `ensure_folder` creates every folder subscribed, which is the
                # right default for a folder nobody has said anything about.
                # Here the server HAS said, and a new folder must arrive in the
                # state it reported rather than the state it would have had.
                repo.update_folder(con, folder_id, subscribed=False, commit=False)
            added.append(box.path)
        else:
            folder_id = existing.id
            changes = {}
            if existing.display_name != display:
                changes["display_name"] = display
            if existing.role != role:
                changes["role"] = role
            if existing.subscribed != is_subscribed:
                changes["subscribed"] = is_subscribed
            if changes:
                repo.update_folder(con, folder_id, commit=False, **changes)
                updated.append(box.path)
        ids[box.path] = folder_id
        repo.set_parent(con, folder_id,
                        _parent_of(box.path, box.delimiter, ids), commit=False)

    # Anything the store has and the server did not mention. Unsubscribed and
    # reported — never deleted; see the module header.
    vanished = []
    for path, folder in before.items():
        if path in ids or repo.is_local(path):
            # A local folder was never the server's to mention. It holds the
            # drafts of an account whose server offers nowhere to put them, and
            # unsubscribing it would take them out of the rail.
            continue
        vanished.append(path)
        if folder.subscribed:
            repo.update_folder(con, folder.id, subscribed=False, commit=False)

    con.commit()
    return FolderReport(added=tuple(added), updated=tuple(updated),
                        vanished=tuple(vanished), roles=roles)


def check_uid_validity(con: sqlite3.Connection, folder_id: int,
                       uid_validity: int | None) -> int:
    """Compare the server's UIDVALIDITY with the stored one; act if it changed.

    Returns the number of messages discarded, which is zero in every ordinary
    case. A change means the server has declared every UID this store holds for
    the folder meaningless, and there is no way to match the two sets up again
    — the local rows can never be flagged, moved or expunged on the server, and
    the re-download that follows would duplicate every one of them.

    A server that sends NO uid_validity is not treated as a change. Absence is
    not a new value, and discarding a folder's mail because a response was
    short would be the worst possible reading of missing information.
    """
    if uid_validity is None:
        return 0
    state = repo.sync_state(con, folder_id)
    stored = state.get("uid_validity")
    if stored is None:
        repo.record_sync_state(con, folder_id, uid_validity=uid_validity)
        return 0
    if int(stored) == int(uid_validity):
        return 0
    discarded = repo.discard_contents(con, folder_id, commit=False)
    repo.record_sync_state(con, folder_id, uid_validity=uid_validity,
                           last_synced_at=utc_now(), commit=False)
    con.commit()
    return discarded
