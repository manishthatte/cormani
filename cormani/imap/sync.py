# SPDX-License-Identifier: GPL-3.0-or-later
#
# Bringing one folder up to date.
#
# The unit is a folder, not an account, because everything that decides how to
# proceed — UIDVALIDITY, UIDNEXT, HIGHESTMODSEQ — is per folder, and because a
# sync that must be abandoned halfway should leave every folder it finished in
# a consistent state rather than the account in an ambiguous one.
#
# Five decisions, each of which has a plausible wrong version:
#
# THE STATE IS WRITTEN AFTER THE MESSAGES, NEVER BEFORE. If the process dies in
# between, a folder whose recorded UIDNEXT is behind what it actually holds
# re-fetches a handful of messages the store already has — and `ingest` is
# idempotent on (folder, uid), so that costs bandwidth and nothing else. The
# other order skips those messages permanently, and nothing ever notices.
#
# NEW MESSAGES ARE FOUND BY UID, NOT BY COUNT. `UID SEARCH UID <next>:*` asks
# the server what has arrived since; comparing EXISTS against a local count
# cannot distinguish two arrivals from three arrivals and one deletion.
#
# A FIRST SYNC IS WINDOWED AND CHUNKED, BECAUSE OF A REAL LIMIT. docs/accounts.txt
# measured it: Gmail's ceiling of fifteen simultaneous connections is per
# account and is not the constraint — the DAILY DOWNLOAD CAP is, and a full
# history across eight Google accounts trips it. So the first pass takes a date
# window, and every pass takes at most `max_new` messages and says whether more
# remain. The engine comes back rather than pushing through.
#
# EXPUNGE DETECTION IS CONDITIONAL ON A CHEAP CHECK. Asking for the whole UID
# list every five minutes, for fifteen accounts and ten folders each, is a lot
# of traffic to learn that nothing was deleted. EXISTS is already in hand from
# SELECT: when it matches the local count, nothing has gone, and when it does
# not, the full list is worth asking for.
#
# A REFUSED BATCH MUST NOT STALL THE FOLDER. Observed against Gmail: a FETCH
# of fifty bodies came back `System Error (Failure)`. The first version let
# that abort the whole folder BEFORE the watermark was written, so the next
# pass asked for the identical range, got the identical refusal, and the
# folder never advanced again — a permanent stall dressed as a transient
# error. Now a refused batch is halved and retried, down to a single message;
# a single message the server will not send is counted, skipped, and the
# watermark moves past it. Terminating, and it keeps the progress already made.
#
# CONDSTORE IS AN OPTIMISATION AND NOTHING RESTS ON IT. With it, flags are
# fetched by MODSEQ delta; without it, every flag in the folder is fetched and
# compared. The second is correct and slow, the first is correct and fast, and
# a server that advertises CONDSTORE and then lies about a MODSEQ costs a
# missed flag until the next full pass rather than a broken sync.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..store import folders as folders_repo
from ..store import ingest
from ..store.database import utc_now
from ..store.folders import Folder
from . import envelope, parse
from .client import BODY_ITEMS, Connection
from .errors import Transient, describe
from .folders import check_uid_validity

# How many message bodies one pass will take from one folder. Not a
# performance number — it is what makes a first import resumable, and what
# stops one enormous folder starving the other fourteen accounts.
DEFAULT_MAX_NEW = 500

# Bodies per FETCH. Small enough that an interruption loses little, large
# enough that the round trips do not dominate.
DEFAULT_BATCH = 50


@dataclass
class SyncReport:
    folder: str = ""
    # The folder's ROLE, carried so a caller can tell arriving mail from a
    # re-fetch of the Sent folder without going back to the folder table.
    # `imap/engine.py` needs exactly that to decide what the filters see.
    role: str = ""
    new: int = 0
    flags_changed: int = 0
    vanished: int = 0
    discarded: int = 0
    remaining: int = 0
    unreadable: int = 0
    notes: list = field(default_factory=list)
    # The rows this pass CREATED, in the order they were written. Not a count:
    # the filters need the identities, and deriving them afterwards from
    # `received_at` would be a guess that two passes a second apart can break.
    new_ids: list = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.new + self.flags_changed + self.vanished + self.discarded

    @property
    def complete(self) -> bool:
        return self.remaining == 0


def sync_folder(con: sqlite3.Connection, connection: Connection, folder: Folder,
                *, attachments_root: Path | str | None = None,
                since: dt.date | None = None,
                max_new: int = DEFAULT_MAX_NEW,
                batch: int = DEFAULT_BATCH) -> SyncReport:
    """One folder, one pass. Returns what changed and whether more is waiting."""
    report = SyncReport(folder=folder.path, role=folder.role)
    state = connection.select(folder.path)

    report.discarded = check_uid_validity(con, folder.id, state.uid_validity)
    stored = folders_repo.sync_state(con, folder.id)

    wanted = _new_uids(connection, stored.get("uid_next"), since=since)
    report.remaining = max(0, len(wanted) - max_new)
    if report.remaining:
        report.notes.append(
            f"{len(wanted)} new; taking {max_new} this pass, "
            f"{report.remaining} to come")
        wanted = wanted[:max_new]

    reached = None
    if wanted:
        (report.new, report.unreadable, reached, refusal,
         report.new_ids) = _fetch_new(
            con, connection, folder, wanted, attachments_root, batch)
        if report.unreadable:
            report.notes.append(
                f"{report.unreadable} message(s) the server did not send; "
                f"most often expunged between the search and the fetch")
        if refusal:
            report.notes.append(f"the server refused a fetch: {refusal}")
        # Anything above where this pass actually got to is still outstanding,
        # whether it was cut short by `max_new` or by a refusal.
        outstanding = [u for u in wanted if reached is None or u > reached]
        report.remaining = max(report.remaining, len(outstanding))

    report.flags_changed = _sync_flags(con, connection, folder, stored,
                                       state.highest_modseq)
    report.vanished = _forget_gone(con, connection, folder, state.exists)

    # Written last, and the value differs by whether the pass finished.
    #
    # A COMPLETE pass takes the server's UIDNEXT: nothing above it exists yet.
    #
    # An INCOMPLETE one takes ONE PAST THE HIGHEST UID IT ASKED FOR. Recording
    # the server's UIDNEXT there would skip the outstanding messages for good;
    # recording nothing at all — which is the obvious cautious answer, and is
    # wrong — means the next pass searches from the beginning, takes the same
    # chunk again, and a chunked import never advances at all.
    #
    # The watermark passes over anything the server did not send. That is a
    # deliberate trade: the alternative stalls the whole folder on one message,
    # and the count is reported above rather than swallowed. The usual cause is
    # a message expunged between the search and the fetch, where skipping is
    # not merely acceptable but correct.
    if report.complete:
        checkpoint, modseq = state.uid_next, state.highest_modseq
    else:
        checkpoint, modseq = (reached + 1 if reached is not None else None), None
    folders_repo.record_sync_state(
        con, folder.id, uid_next=checkpoint, highest_modseq=modseq,
        last_synced_at=utc_now())
    return report


def _new_uids(connection: Connection, uid_next: int | None, *,
              since: dt.date | None) -> list[int]:
    """The UIDs this store has not seen.

    `<next>:*` rather than a count: it asks the server what has arrived since a
    known point, which is the only question with an unambiguous answer.
    """
    if uid_next:
        found = connection.search_uids("UID", f"{int(uid_next)}:*")
        # `<n>:*` matches the highest existing UID even when it is BELOW n,
        # which is what the RFC says and is surprising every time. Filtered
        # here rather than trusted.
        return [uid for uid in found if uid >= int(uid_next)]
    if since is not None:
        return connection.search_uids("SINCE", parse.search_date(since))
    return connection.search_uids("ALL")


def _fetch_new(con: sqlite3.Connection, connection: Connection, folder: Folder,
               uids: list, attachments_root, batch: int) -> tuple:
    """Download and store bodies, one batch at a time.

    Returns (written, unreadable, reached, refusal, created). `reached` is the
    highest UID this pass is entitled to checkpoint past — everything at or
    below it has been stored or deliberately skipped — and it is what makes a
    refusal survivable rather than fatal. `created` is the ids of the rows this
    pass MADE, which is not the same as the ones it wrote: a sync resumed after
    an interruption re-stores messages it already holds, and a filter must not
    run on those a second time.

    Committing per batch is not only about interruption: it is what makes the
    work already done survive the batch that fails.
    """
    ordered = sorted({int(u) for u in uids})
    written = unreadable = 0
    created: list = []
    reached = None
    refusal = ""
    index = 0
    size = max(1, batch)

    while index < len(ordered):
        chunk = ordered[index:index + size]
        try:
            fetched = connection.fetch(chunk, BODY_ITEMS)
        except Transient as exc:
            if size > 1:
                # Halve and try again from the same place. A server refusing
                # fifty will usually accept twenty-five, and where it will not,
                # this converges on the single message that is the problem.
                size = max(1, size // 2)
                continue
            # One message the server will not send. Counted, skipped, and the
            # watermark moves past it — because the alternative is asking for
            # the same UID on every sync until someone notices.
            unreadable += 1
            refusal = describe(exc)
            reached = chunk[0]
            index += 1
            size = max(1, batch)
            continue

        for item in fetched:
            if item.uid is None or item.body is None:
                # Answered without the body it was asked for; most often the
                # message was expunged between the search and the fetch.
                unreadable += 1
                continue
            stored = ingest.store_message(
                con, folder.id, item.uid, envelope.read(item.body),
                flags=item.flags, internaldate=item.internaldate,
                attachments_root=attachments_root, account_id=folder.account_id,
                commit=False)
            if stored.created:
                created.append(stored.message_id)
            written += 1
        con.commit()
        reached = chunk[-1]
        index += len(chunk)
        size = max(1, batch)          # recover the full batch after a success
    return written, unreadable, reached, refusal, created


def _sync_flags(con: sqlite3.Connection, connection: Connection, folder: Folder,
                stored: dict, highest_modseq: int | None) -> int:
    """Apply the server's flags. By MODSEQ delta when the server offers one."""
    previous = stored.get("highest_modseq")
    if connection.has("CONDSTORE") and previous and highest_modseq:
        if int(highest_modseq) <= int(previous):
            return 0                       # nothing has changed in this folder
        fetched = connection.fetch_flags(changed_since=int(previous))
    else:
        fetched = connection.fetch_flags("1:*")

    by_uid = {f.uid: f.flags for f in fetched if f.uid is not None}
    if not by_uid:
        return 0
    return ingest.update_flags(con, folder.id, by_uid)


def _forget_gone(con: sqlite3.Connection, connection: Connection,
                 folder: Folder, exists: int) -> int:
    """Remove what the server no longer has.

    The count comparison is the cheap part: EXISTS came free with SELECT, and
    when it matches the local count nothing has been deleted. Only when they
    disagree is the whole UID list worth asking for.

    A local count HIGHER than EXISTS is the ordinary deletion case. A count
    LOWER means the store is behind, which the fetch above handles — asking for
    the UID list then would cost a round trip to learn nothing.
    """
    local = ingest.uids_in(con, folder.id)
    if len(local) <= int(exists or 0):
        return 0
    on_server = set(connection.search_uids("ALL"))
    gone = local - on_server
    if not gone:
        return 0
    return ingest.forget_uids(con, folder.id, gone)


def sync_account_folders(con: sqlite3.Connection, connection: Connection,
                         account_id: int, *, roles_first: bool = True,
                         **kwargs) -> list:
    """Every subscribed folder of one account, in a useful order.

    Inbox first, then the other roles, then everything else — because a sync
    interrupted after two folders should have done the two the user is looking
    at. `list_folders` already sorts that way; this only says so out loud.
    """
    reports = []
    # A LOCAL folder is not the server's and must not be selected on it: an
    # account whose server offers no Drafts folder has one made here, and
    # asking for it over IMAP is a NONEXISTENT that fails the whole account's
    # sync. store/folders.LOCAL_PREFIX is what makes them recognisable.
    folders = [f for f in folders_repo.list_folders(con, account_id,
                                                    subscribed_only=True)
               if not folders_repo.is_local(f.path)]
    if not roles_first:                                      # pragma: no cover
        folders = sorted(folders, key=lambda f: f.path)
    for folder in folders:
        reports.append(sync_folder(con, connection, folder, **kwargs))
    return reports
