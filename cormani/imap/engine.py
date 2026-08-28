# SPDX-License-Identifier: GPL-3.0-or-later
#
# Driving the whole thing: which account, when, and what to do when it fails.
#
# Everything below this is about one connection or one folder. This is where
# fifteen accounts become a schedule, and it exists because the interesting
# constraints are not per-account at all.
#
# SEQUENTIAL, NOT FANNED OUT, AND THE REASON IS NOT THE CONNECTION LIMIT.
# docs/accounts.txt measured it: Gmail's ceiling of fifteen simultaneous IMAP
# connections is per account, so eight Google accounts in parallel is nowhere
# near it. The DAILY DOWNLOAD CAP is the real limit, and a full first import
# fanned out across eight accounts trips it — after which every one of them is
# refused for the rest of the day. One at a time, with each pass bounded, is
# what makes a first import take an evening instead of a week.
#
# THE BACK-OFF LIVES IN THE DATABASE, NOT IN MEMORY. A provider counting
# refusals does not forget them because corMani restarted, and a back-off that
# resets on relaunch turns a rate limit into a ban. `account.sync_failures` is
# the exponent and `account.next_attempt_at` is the gate.
#
# A REFUSED CREDENTIAL IS NOT RETRIED AT ALL. The one refresh that might have
# fixed it has already happened in `auth/credentials.py`, which is what makes
# `AuthFailed` permanent. The account is parked for a day, the reason is
# recorded where the interface can show it, and a person signs in again. Any
# other behaviour is fifteen accounts hammering a provider that has already
# said no.
#
# THE QUEUE DRAINS BEFORE THE FETCH, ALWAYS. A message the user archived on a
# train should reach the server before the sync asks what is in the Inbox —
# otherwise the fetch brings it straight back and the archive appears not to
# have worked.
#
# NOTHING HERE IMPORTS QT. The engine is driven from a worker thread and
# reports through a plain callable, so all of it is testable with no display.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..auth import credentials as auth
from ..auth.credentials import NotConfigured
from ..store import accounts as accounts_repo
from ..store import folders as folders_repo
from ..store import rulerun
from ..store import rules as rules_repo
from ..store.accounts import Account
from ..store.database import utc_now
from . import folders as folder_sync
from . import queue as queue_sync
from . import sync as message_sync
from .client import Connection
from .errors import AuthFailed, ImapError, Permanent, RateLimited, describe

# The back-off ladder, in seconds: a minute, then doubling, capped at an hour.
# Long enough that a provider stops counting, short enough that a laptop coming
# back onto a network does not sit idle.
_BACKOFF_BASE = 60
_BACKOFF_CAP = 3600

# A quota is not a fault, and minutes are the wrong unit for one. Gmail's daily
# cap resets on a daily boundary; half an hour is the shortest wait that is
# ever worth making, and six hours the longest before checking again is free.
_RATE_LIMIT_MIN = 30 * 60
_RATE_LIMIT_CAP = 6 * 3600

# A credential the user must fix. Parked, not retried.
_NEEDS_ATTENTION = 24 * 3600


@dataclass
class Options:
    attachments_root: Path | str | None = None
    # How far back a FIRST sync reaches. None means everything, which is
    # correct and is not the default: see the module header.
    initial_days: int | None = 90
    max_new: int = message_sync.DEFAULT_MAX_NEW
    batch: int = message_sync.DEFAULT_BATCH
    timeout: float = 60.0


@dataclass
class AccountResult:
    account_id: int
    address: str
    ok: bool = False
    new: int = 0
    flags_changed: int = 0
    vanished: int = 0
    sent: int = 0            # queued changes the server accepted
    posted: int = 0          # messages sent out of the outbox
    stuck: int = 0
    folders: int = 0
    remaining: int = 0
    error: str = ""
    retry_at: str = ""
    notes: list = field(default_factory=list)
    # Inbox rows THIS PASS created, in the order they were written. The
    # notifier announces these minus `filtered.quiet_ids()` — see
    # `ui/mailnotify.py`. Carried rather than re-derived from `received_at`,
    # which two passes a second apart can make wrong.
    arrived: list = field(default_factory=list)
    # What the filter rules did to this account's arriving mail, or None when
    # there are no rules. Carried on the result rather than applied and
    # forgotten because two callers need it afterwards: `--sync` prints it, and
    # the window's notifier reads `quiet_ids` off it to decide what NOT to
    # announce. `store/rulerun.RunReport`.
    filtered: object = None

    @property
    def changed(self) -> int:
        return (self.new + self.flags_changed + self.vanished + self.sent
                + self.posted)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Engine:
    """One account at a time, with the state that decides which one."""

    def __init__(self, con: sqlite3.Connection, *, options: Options | None = None,
                 connect: Callable | None = None,
                 resolve: Callable = auth.resolve,
                 submit: Callable | None = None,
                 clock: Callable = _now) -> None:
        self.con = con
        self.options = options or Options()
        self._connect = connect or self._open
        self._resolve = resolve
        # How a submission connection is made. Injected for the same reason
        # `connect` is: the suite talks to a server in this process, and the
        # default is the real one.
        self._submit = submit
        self._clock = clock

    # ------------------------------------------------------------ scheduling
    def due(self, *, now: dt.datetime | None = None) -> list:
        """Accounts that may be contacted. Disabled and backed-off ones are not.

        `hidden` is deliberately NOT consulted: hiding takes an account out of
        the rail and leaves its mail in the store and in search, which means it
        must keep syncing. `enabled` is the switch that stops it.
        """
        stamp = (now or self._clock()).isoformat()
        rows = self.con.execute(
            "SELECT id FROM account WHERE enabled = 1 AND "
            "(next_attempt_at IS NULL OR next_attempt_at <= ?)", (stamp,)).fetchall()
        allowed = {int(r[0]) for r in rows}
        return [a for a in accounts_repo.list_accounts(self.con) if a.id in allowed]

    def backed_off(self) -> dict:
        """Why each parked account is parked — for the interface to show."""
        return {int(r["id"]): {"until": r["next_attempt_at"],
                               "error": r["last_error"],
                               "failures": r["sync_failures"]}
                for r in self.con.execute(
                    "SELECT id, next_attempt_at, last_error, sync_failures "
                    "FROM account WHERE next_attempt_at IS NOT NULL").fetchall()}

    def clear_backoff(self, account_id: int) -> None:
        """Try this account again now. What the interface calls after a sign-in."""
        self.con.execute(
            "UPDATE account SET sync_failures = 0, next_attempt_at = NULL, "
            "last_error = '' WHERE id = ?", (account_id,))
        self.con.commit()

    # ---------------------------------------------------------------- running
    def sync_all(self, *, progress: Callable | None = None,
                 accounts: list | None = None) -> list:
        """Every account that is due, one after another.

        A failure stops that account and nothing else. Fifteen accounts where
        one has a rejected password must still sync the other fourteen.
        """
        results = []
        for account in (accounts if accounts is not None else self.due()):
            if progress:
                progress("account:start", {"address": account.address,
                                           "account_id": account.id})
            result = self.sync_account(account, progress=progress)
            results.append(result)
            if progress:
                progress("account:done", {"address": account.address,
                                          "result": result})
        self._file_what_arrived(results, progress)
        return results

    def _filter(self, arrived: list, progress: Callable | None,
                account) -> object:
        r"""Put this account's ARRIVING mail through the user's filter rules.

        HERE, IN `sync_account`, AND NOT IN `sync_all` — which is the opposite
        of where `_file_what_arrived` sits, and for the opposite reason. Filing
        onto tracked threads is a pass over the whole store and only makes
        sense once, at the end; a filter is scoped to one account's rules and
        acts on the ids that were created two lines above. Putting it here also
        means every caller gets it: `sync_all` goes through `sync_account`, and
        so does every test that syncs one account.

        ONLY WHAT ARRIVED IN THE INBOX. A sync fetches Sent, Archive, Drafts
        and Trash as well, and a rule saying "subject contains invoice: move to
        Archive" must not run over the Sent folder, nor fire a second time on
        Gmail's `\All` copy of a message it already filed. A filter is about
        incoming mail; running the rules over anything else is
        `store/rulerun.run_over_folder`, which the user asks for by name.

        ONLY ROWS THIS PASS CREATED. `store/ingest.store_message` is idempotent
        and a resumed sync re-writes messages it already holds; filtering those
        again would move mail the user had deliberately moved back.
        """
        if not arrived:
            return None
        # NO RULES IS NOT AN EMPTY RUN. `AccountResult.filtered` is None when
        # the user has written no filters, and a `RunReport` saying nothing
        # happened is a different claim: it means the rules were asked and
        # declined. `--sync` prints one and says nothing about the other, and a
        # report read as "0 of 0 matched" on every account of every sync is the
        # line people stop reading.
        active = [rule for rule in rules_repo.list_rules(self.con, enabled_only=True)
                  if rule.is_complete]
        if not active:
            return None
        # Read once and handed down: fifteen accounts must not be fifteen
        # passes over the rule table, and `run` takes them for that reason.
        report = rulerun.run(self.con, arrived, rules=active)
        if report.matched and progress:
            progress("filtered", {"address": account.address, "report": report})
        return report

    def _file_what_arrived(self, results: list,
                           progress: Callable | None) -> None:
        """Put the new mail onto the tracked threads it belongs to.

        HERE AND NOT IN A CALLER, because there are two callers — the terminal
        and the window — and a step either of them can forget is a step that
        works differently depending on how corMani was started. `store/attach`
        is idempotent, so running it after every sync is correct rather than
        merely cheap: a message that arrived before the thread it belongs to is
        filed the moment somebody makes that thread.

        SKIPPED ENTIRELY WHEN NOTHING NEW ARRIVED. `rebuild_wrote_to` is a
        pass over every message in every Sent folder, which is fifteen years of
        mail on this installation, and running it to discover that nothing has
        changed is the kind of cost that makes a quiet sync feel slow.
        """
        if not any(r.new for r in results):
            return
        from ..store import attach

        addresses = attach.rebuild_wrote_to(self.con, commit=False)
        filed = attach.run(self.con)
        if progress and filed.total:
            progress("filed", {"filed": filed, "addresses": addresses})

    def sync_account(self, account: Account, *,
                     progress: Callable | None = None) -> AccountResult:
        """One account, end to end. Never raises — the result carries the news."""
        result = AccountResult(account_id=account.id, address=account.address)
        connection = None
        try:
            credential = self._resolve(account.address, account.provider,
                                       method=self._method(account))
            connection = self._connect(account)
            auth.authenticate(connection, credential)

            report = folder_sync.sync_folders(self.con, connection, account.id)
            result.folders = report.total
            if report.vanished:
                result.notes.append(
                    f"{len(report.vanished)} folder(s) are no longer on the "
                    f"server and have been unsubscribed, not deleted")

            # Before the fetch. Always. See the module header.
            drained = queue_sync.drain(self.con, connection, account.id)
            result.sent, result.stuck = drained.sent, drained.stuck
            if drained.stuck:
                result.notes.append(
                    f"{drained.stuck} change(s) could not be sent and are "
                    f"no longer being retried")
            result.notes.extend(drained.errors)

            # And the outbox, before the fetch as well. A message the user
            # asked to send should leave on the same connection that went out
            # to look for new mail, rather than after it.
            posted = self._post(account, credential, connection)
            result.posted = posted.sent
            result.stuck += posted.stuck
            result.notes.extend(posted.notes)

            arrived: list = []
            for report in message_sync.sync_account_folders(
                    self.con, connection, account.id,
                    attachments_root=self.options.attachments_root,
                    since=self._since(account),
                    max_new=self.options.max_new, batch=self.options.batch):
                result.new += report.new
                if report.role == folders_repo.ROLE_INBOX:
                    arrived.extend(report.new_ids)
                result.flags_changed += report.flags_changed
                result.vanished += report.vanished
                result.remaining += report.remaining
                result.notes.extend(report.notes)
                if progress:
                    progress("folder:done", {"address": account.address,
                                             "folder": report.folder,
                                             "report": report})
            result.arrived = list(arrived)
            result.filtered = self._filter(arrived, progress, account)
            result.ok = True
            self._succeeded(account)
        except NotConfigured as exc:
            result.error = describe(exc)
            self._parked(account, result, _NEEDS_ATTENTION)
        except AuthFailed as exc:
            result.error = describe(exc)
            self._parked(account, result, _NEEDS_ATTENTION)
        except RateLimited as exc:
            result.error = describe(exc)
            self._parked(account, result,
                         self._rate_limit_wait(account, exc.retry_after))
        except Permanent as exc:
            result.error = describe(exc)
            self._parked(account, result, _NEEDS_ATTENTION)
        except (ImapError, sqlite3.Error, OSError) as exc:
            result.error = describe(exc)
            self._parked(account, result, self._backoff_wait(account))
        finally:
            if connection is not None:
                connection.logout()
        return result

    # -------------------------------------------------------------- watching
    def watch_once(self, account: Account, *, seconds: float = 29 * 60) -> list:
        """Hold a connection open on the Inbox and report what the server says.

        One wait, not a loop: RFC 2177 lets a server drop an idle connection
        after thirty minutes and Gmail does, so the renewal has to be somebody's
        decision. It is the caller's, because only the caller knows whether the
        window is still open.
        """
        inbox = folders_repo.by_role(self.con, account.id, folders_repo.ROLE_INBOX)
        if inbox is None:
            return []
        credential = self._resolve(account.address, account.provider,
                                   method=self._method(account))
        connection = self._connect(account)
        try:
            auth.authenticate(connection, credential)
            connection.select(inbox.path)
            return connection.idle(seconds=seconds)
        finally:
            connection.logout()

    # ---------------------------------------------------------------- helpers
    def _open(self, account: Account) -> Connection:
        """The stored host, falling back to the provider's default.

        The stored one wins because the schema says so: a provider's hostname
        is a fact about today, and an account whose host moves must be fixable
        without a new release. The provider default is what a newly added
        account is given, not what an existing one is held to.
        """
        from ..auth import providers

        row = self.con.execute(
            "SELECT imap_host, imap_port FROM account WHERE id = ?",
            (account.id,)).fetchone()
        provider = providers.get(account.provider)
        host = (row["imap_host"] if row else "") or provider.imap_host
        port = int((row["imap_port"] if row else 0) or provider.imap_port or 993)
        if not host:
            raise NotConfigured(f"no IMAP host is recorded for {account.address}")
        return Connection.connect(host, port, address=account.address,
                                  timeout=self.options.timeout)

    def _post(self, account: Account, credential, connection):
        """Drain this account's outbox over SMTP, filing the copy over IMAP.

        Two protocols in one method because the two halves of sending are on
        two protocols: the message goes out through the submission server, and
        the copy is filed through the connection this engine already holds.
        Opening a second IMAP connection to file it would be a second login.
        """
        from ..auth import providers
        from ..smtp import outbox

        provider = providers.get(account.provider)
        row = self.con.execute(
            "SELECT smtp_host, smtp_port FROM account WHERE id = ?",
            (account.id,)).fetchone()
        host = (row["smtp_host"] if row else "") or provider.smtp_host
        port = int((row["smtp_port"] if row else 0) or provider.smtp_port or 587)
        extra = {"connect": self._submit} if self._submit is not None else {}
        return outbox.send_pending(
            self.con, account, credential, append=connection.append,
            files_sent=provider.files_sent, host=host, port=port, **extra)

    def _method(self, account: Account) -> str:
        row = self.con.execute("SELECT auth_method FROM account WHERE id = ?",
                               (account.id,)).fetchone()
        return (row["auth_method"] if row else "") or ""

    def _since(self, account: Account) -> dt.date | None:
        """The date window for a FIRST sync of this account, or None.

        Only for an account that has never finished one: once a folder has a
        UIDNEXT the window is irrelevant, and applying it anyway would hide
        older mail that has already been fetched.
        """
        if self.options.initial_days is None:
            return None
        row = self.con.execute(
            "SELECT last_sync_at FROM account WHERE id = ?", (account.id,)).fetchone()
        if row and row["last_sync_at"]:
            return None
        return (self._clock() - dt.timedelta(days=self.options.initial_days)).date()

    # ------------------------------------------------------------- outcomes
    def _succeeded(self, account: Account) -> None:
        self.con.execute(
            "UPDATE account SET last_sync_at = ?, last_error = '', "
            "sync_failures = 0, next_attempt_at = NULL WHERE id = ?",
            (utc_now(), account.id))
        self.con.commit()

    def _parked(self, account: Account, result: AccountResult,
                seconds: float) -> None:
        when = (self._clock() + dt.timedelta(seconds=seconds)).replace(
            microsecond=0).isoformat()
        result.retry_at = when
        self.con.execute(
            "UPDATE account SET last_error = ?, sync_failures = sync_failures + 1, "
            "next_attempt_at = ? WHERE id = ?", (result.error, when, account.id))
        self.con.commit()

    def _failures(self, account: Account) -> int:
        row = self.con.execute("SELECT sync_failures FROM account WHERE id = ?",
                               (account.id,)).fetchone()
        return int(row["sync_failures"]) if row else 0

    def _backoff_wait(self, account: Account) -> float:
        return min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** self._failures(account)))

    def _rate_limit_wait(self, account: Account,
                         retry_after: float | None) -> float:
        if retry_after:
            return max(float(retry_after), _RATE_LIMIT_MIN)
        return min(_RATE_LIMIT_CAP,
                   max(_RATE_LIMIT_MIN,
                       _RATE_LIMIT_MIN * (2 ** self._failures(account))))


def options_from(settings, paths) -> Options:
    """The engine's options from the configuration file and the XDG paths."""
    days = int(getattr(settings, "initial_sync_days", 90) or 0)
    return Options(
        attachments_root=paths.attachments,
        initial_days=days if days > 0 else None,
        max_new=int(getattr(settings, "sync_max_new", None)
                    or message_sync.DEFAULT_MAX_NEW))


def sync_once(database_path, *, options: Options | None = None,
              progress: Callable | None = None) -> list:
    """Sync every due account over a connection of this call's own.

    The connection is opened and closed here rather than passed in, and that is
    the point: a worker thread must not share the interface's sqlite handle.
    WAL is what makes the two coexist — the list redraws while this writes —
    and it is why `database.connect` turns it on.

    `connect`, not `open_store`: opening a database and changing its shape are
    different acts, and a background thread must never find itself running DDL.
    The migration happened at start-up.
    """
    from ..store.database import connect

    con = connect(database_path)
    try:
        return Engine(con, options=options).sync_all(progress=progress)
    finally:
        con.close()
