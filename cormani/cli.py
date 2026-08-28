# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the command line actually does.
#
# `__main__.py` parses arguments and dispatches; everything with a body lives
# here. Split when account setup arrived and the two concerns stopped being
# one file's worth — CONVENTIONS.txt §4.
#
# EVERY COMMAND HERE WORKS WITH NO DISPLAY. That is not a testing convenience:
# a first import runs for hours and should survive the interface being closed,
# a scheduled run has no display at all, and when something is wrong the
# terminal is where the engine's own words are. The window is one way to drive
# corMani, not the only one.
#
# ASKING IS INJECTED, NEVER CALLED DIRECTLY. `ask` and `ask_secret` are
# parameters so that the whole of account setup can be tested without a
# terminal — and so that the Qt dialog, when it arrives, calls the SAME
# functions with its own prompts rather than reimplementing them.
#
# THEY DEFAULT TO None AND ARE RESOLVED INSIDE THE FUNCTION. Writing
# `ask_secret=_ask_secret` in the signature binds the default once, at import,
# so patching the module attribute has NO effect — and a test that believes it
# has replaced the prompt silently blocks on the real terminal instead. That
# happened; hence this.
#
# A PASSWORD IS VERIFIED BEFORE ANYTHING IS WRITTEN. It is held in memory,
# used to connect, and only stored once the server has accepted it. A failed
# setup therefore leaves nothing behind: no half-made account row, and no
# credential in the keyring for an address that does not work.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import getpass
import sys
from pathlib import Path

from . import APP_NAME, __version__

# ------------------------------------------------------------------- check
def check() -> int:
    """Report what is present and what is not. Never guesses; never raises."""
    from . import app as app_mod
    from . import calcli as calcli_mod
    from . import contactcli as contactcli_mod
    from . import rulecli as rulecli_mod
    from . import viewcli as viewcli_mod
    from .config import settings as config
    from .secrets import store as secrets
    from .store import database, schema

    ok = True
    print(f"{APP_NAME} {__version__}")
    print(f"  python           {sys.version.split()[0]}")

    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion
        print(f"  PySide6          {pyside_version} (Qt {qVersion()})")
    except ImportError as exc:
        print(f"  PySide6          MISSING — {exc}")
        ok = False

    print(f"  keyring          {secrets.backend_name()}"
          f"{'' if secrets.available() else '  (UNUSABLE)'}")
    if not secrets.available():
        ok = False

    cfg = config.load()
    print(f"  config           {cfg.source or 'defaults (no file)'}")

    paths = app_mod.paths_for(cfg)
    print(f"  config dir       {paths.config}")
    print(f"  data dir         {paths.data}")
    print(f"  state dir        {paths.state}")

    if paths.database.exists():
        con = database.connect(paths.database, read_only=True)
        have = database.schema_version(con)
        accounts = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
        note = ("" if have == schema.LATEST_VERSION
                else f"  (build expects {schema.LATEST_VERSION})")
        print(f"  store            schema v{have}, {accounts} accounts{note}")
        if have >= 2:
            _report_index(con)
        if have >= 5:
            _report_threads(con)
        if have >= 4:
            _report_outbox(con)
        if have >= 4:
            _report_engine(con)
        if have >= calcli_mod.CALENDAR_SCHEMA:
            calcli_mod.report(con)
        if have >= TRACKING_SCHEMA:
            _report_tracking(con)
        if have >= rulecli_mod.RULES_SCHEMA:
            rulecli_mod.report(con)
        if have >= viewcli_mod.VIEWS_SCHEMA:
            viewcli_mod.report(con)
        if have >= contactcli_mod.ADDRESS_BOOK_SCHEMA:
            contactcli_mod.report(con)
        con.close()
    else:
        print(f"  store            not created yet ({paths.database})")

    ok = _report_panels(cfg, paths) and ok

    print("  verdict          " + ("ready" if ok else "NOT READY — see above"))
    return 0 if ok else 1


def _report_panels(cfg, paths) -> bool:
    """The site panels: what the engine is, which sites are on, and where each
    session lives. Read-only; never raises.

    THE ENGINE'S AGE IS THE FIRST QUESTION when a panel starts refusing to
    load, which is docs/toolkit-verification.txt finding 2, and it is asked
    here rather than found by rebuilding. Both versions are printed because
    they are a long way apart and mean different things: the Chromium version
    is what a panel claims to be and what a site accepts or declines, the
    security-patch version is how current the fixes in it are.

    WHERE A SESSION IS, is the other half. Signing a site out is deleting that
    directory, so a person has to be able to be told which one — and `--check`
    has no QApplication, so it asks `panels/profiles.storage_for`, which
    derives the path and constructs nothing.

    A MISSING ENGINE IS NOT "NOT READY" UNLESS A PANEL WAS ASKED FOR. The
    panels are optional by design — finding 2 says the embedded Chromium will
    age out and that mail and calendar must be unaffected when it does — so a
    machine without the package is a supported machine, and the verdict only
    turns when the configuration asked for something that cannot be given.
    """
    from .panels import profiles as profiles_mod
    from .panels import sites as sites_mod
    from .platform import runtime

    keys = cfg.site_keys()
    try:
        engine = runtime.engine_versions()
    except ImportError:
        print("  web engine       MISSING — QtWebEngine is not installed")
        if keys:
            names = ", ".join(sites_mod.get(k).name for k in keys)
            print(f"                   {len(keys)} panel(s) configured and "
                  f"unavailable: {names}")
            print("                   mail and calendars are unaffected; "
                  "set sites = [\"none\"] to stop asking")
            return False
        print("                   no panels are configured, so nothing "
              "depends on it")
        return True

    line = f"QtWebEngine {engine['qt']}, Chromium {engine['chromium']}"
    print(f"  web engine       {line}")
    if engine["security"] and engine["security"] != engine["chromium"]:
        print(f"                   security patches to {engine['security']}")
    major = engine["chromium_major"]
    if major and major < runtime.MIN_COMFORTABLE_CHROME:
        print(f"                   OLD — Chromium {major} is old enough that "
              "some sites may refuse it")

    if not keys:
        print("  site panels      none configured")
        return True

    print("  site panels      "
          + ", ".join(sites_mod.get(k).name for k in keys))
    off = [s.name for s in sites_mod.SITES if s.key not in keys]
    if off:
        print(f"                   off: {', '.join(off)}")

    print(f"  panel sessions   {paths.web_profiles}")
    for key in keys:
        site = sites_mod.get(key)
        where = profiles_mod.storage_for(site)
        print(f"    {site.name[:20]:<20} {_session_size(where)}")
    return True


def _session_size(where) -> str:
    """How much of a session is on disk, or that there is none.

    A directory that exists but is empty is what a panel leaves before anybody
    has signed in, and it is worth distinguishing from one that was never made:
    the first means the panel has been opened, the second that it has not.
    """
    if not where.exists():
        return "no session"
    total = sum(f.stat().st_size
                for f in where.rglob("*") if f.is_file())
    if not total:
        return "opened, not signed in"
    for unit in ("bytes", "kB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            amount = f"{total:.0f}" if unit == "bytes" else f"{total:.1f}"
            return f"signed in ({amount} {unit})"
        total /= 1024.0
    return "signed in"                                       # pragma: no cover


# The schema version at which a tracked thread exists at all. `--check` has to
# survive a store older than stage 6 — and, more to the point, one whose
# migration is what is broken.
TRACKING_SCHEMA = 8


def _report_tracking(con) -> None:
    """The tracking layer's lines. Read-only; never raises.

    Three numbers, and they are the three questions the layer exists to
    answer: what is being pursued, what is owed a reply, and what has arrived
    that nobody has filed. The deadlines are named individually rather than
    counted, because a statutory date is the one thing on this page that
    cannot be recovered from and a bare "2" is not something a person acts on.
    """
    from .store import tracking, triage

    counts = tracking.counts(con)
    if not counts["live"] and not counts["closed"]:
        print("  tracking         nothing tracked yet")
    else:
        print(f"  tracking         {counts['live']} live thread(s), "
              f"{counts['owed']} owed, {counts['overdue']} to nudge"
              + (f", {counts['closed']} closed" if counts["closed"] else ""))
        for thread in tracking.deadlines(con, within_days=30):
            days = thread.days_to_deadline()
            when = (f"PASSED {-days}d ago" if days is not None and days < 0
                    else f"in {days}d")
            print(f"    {thread.title[:32]:<32} "
                  f"deadline {thread.deadline_date} — {when}")

    waiting = triage.counts(con)
    if waiting.get(triage.SCOPE_ALL):
        print(f"  needs filing     {waiting[triage.SCOPE_KNOWN]} from people "
              f"you have written to, {waiting[triage.SCOPE_ALL]} in all, "
              f"since {waiting['since']}")


def _report_index(con) -> None:
    """Whether the search box can see every message the store holds.

    A message that was written but not indexed is invisible to search and
    nothing else about it looks wrong — the row is in the list, the reader
    opens it, and only the search is silently short. So the number is reported
    beside the store's own, and the two must agree.

    COUNT over a contentless index is legal although reading a COLUMN of one is
    not: the rowids are all this has to count.
    """
    stored = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    indexed = con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]
    short = stored - indexed
    print(f"  search index     {indexed} of {stored} messages"
          + (f"  ({short} NOT SEARCHABLE — run --reindex)" if short > 0 else ""))


def _report_outbox(con) -> None:
    """What is written and not yet gone.

    The first question when a message has not arrived, and the one --check is
    for: it is either waiting here or it is not, and each answer sends the user
    somewhere different.
    """
    from .smtp import outbox

    waiting = outbox.waiting(con)
    stuck = con.execute(
        "SELECT COUNT(*) FROM pending_op WHERE kind = 'send' AND attempts >= ?",
        (10,)).fetchone()[0]
    if not waiting:
        return
    print(f"  outbox           {waiting} waiting to send"
          + (f"  ({stuck} no longer being retried)" if stuck else ""))


def _report_threads(con) -> None:
    """Whether every message is in a conversation.

    `thread_key` is derived from the References chains exactly as the search
    index is derived from the text, and it fails the same way: a row nothing
    ever threaded looks perfectly healthy and is simply on its own.
    """
    stored = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    placed = con.execute(
        "SELECT COUNT(*) FROM message WHERE thread_key IS NOT NULL "
        "AND thread_key <> ''").fetchone()[0]
    threads_n = con.execute(
        "SELECT COUNT(DISTINCT thread_key) FROM message "
        "WHERE thread_key IS NOT NULL AND thread_key <> ''").fetchone()[0]
    short = stored - placed
    print(f"  conversations    {threads_n} over {placed} messages"
          + (f"  ({short} UNPLACED — run --reindex)" if short > 0 else ""))


def reindex() -> int:
    """Build the derived data again: the search index, and the conversations.

    Both are derived from the messages and neither can be repaired in place —
    an external-content index holds no copy of the text to check itself
    against, and a thread is an opinion about a chain of headers. The honest
    answer for derived data is to derive it again, and doing both under one
    command means there is one thing to remember rather than two.
    """
    from .app import current_paths
    from .store import database, ingest, threads

    paths = current_paths()
    if not paths.database.exists():
        print(f"no store yet ({paths.database})")
        return 1
    con = database.open_store(paths.database)
    try:
        written = ingest.rebuild_search_index(con)
        threaded = threads.rethread(con)
    finally:
        con.close()
    print(f"indexed {written} messages")
    print(f"threaded {threaded} messages")
    return 0


def _report_engine(con) -> None:
    """What the sync engine would do, without doing any of it.

    Every line is a question a person asks when mail is not arriving: can this
    account authenticate at all, is it being held back, and is something the
    user did still waiting to be sent. Read-only and OFFLINE — `--check` has to
    run when the network is the problem, so it reports whether a credential
    EXISTS and never whether it works.
    """
    from .auth import credentials
    from .store import pending

    rows = con.execute(
        "SELECT id, address, provider, enabled, last_sync_at, last_error, "
        "next_attempt_at FROM account ORDER BY id").fetchall()
    if not rows:
        print("  accounts         none yet — see --add-account")
        return

    queued = pending.counts(con)
    total = sum(v["pending"] for v in queued.values())
    stuck = sum(v["stuck"] for v in queued.values())
    print(f"  offline queue    {total} waiting"
          f"{f', {stuck} no longer retried' if stuck else ''}")

    for row in rows:
        marks = []
        if not row["enabled"]:
            marks.append("disabled")
        if not credentials.configured(row["address"], row["provider"]):
            marks.append("no credential stored")
        if row["next_attempt_at"]:
            marks.append(f"waiting until {row['next_attempt_at']}")
        if row["last_error"]:
            marks.append(row["last_error"][:60])
        state = "; ".join(marks) or f"last synced {row['last_sync_at'] or 'never'}"
        print(f"    {row['address']:<32} {state}")


# -------------------------------------------------------------------- sync
def sync(verbose: bool = True, what: str = "all") -> int:
    """Fetch once and report. The headless half of the F5 key — BOTH halves.

    `ui/window.sync_now` says F5 means "everything, now" and drives the mail
    and calendar controllers together; this did mail alone until stage 5's
    command-line work, which meant the two ways of driving corMani disagreed
    about what a sync IS — and the one that disagreed was the one that runs for
    hours in a terminal and gets scheduled.

    `what` narrows it, and earns its place on a first import: mail takes hours,
    the calendar takes seconds, and watching the second scroll past behind the
    first is not watching it.

    ONE EXIT CODE ANSWERS FOR BOTH HALVES, which is why each returns a count of
    failed accounts rather than a code of its own. A scheduled run asks one
    question — did anything fail — and gets one answer.
    """
    from . import calcli
    from .app import current_paths
    from .store import database

    paths = current_paths().ensure()
    if not paths.database.exists():
        print("no store yet — run --add-account, or start corMani once")
        return 1
    database.open_store(paths.database).close()   # migrations belong to start-up

    failed = 0
    if what in ("all", "mail"):
        failed += _sync_mail(paths, verbose)
    if what in ("all", "calendars"):
        failed += calcli.sync(paths, verbose=verbose)
    return 1 if failed else 0


def _sync_mail(paths, verbose: bool) -> int:
    """Every due account's mail. Returns how many accounts FAILED."""
    from .config import settings as config
    from .imap import engine
    from .store import database

    options = engine.options_from(config.load(paths), paths)

    def progress(name: str, detail: dict) -> None:
        if not verbose:
            return
        if name == "account:start":
            print(f"  {detail['address']} …", flush=True)
        elif name == "folder:done":
            report = detail["report"]
            if report.changed or report.remaining:
                print(f"    {report.folder}: {report.new} new, "
                      f"{report.flags_changed} flags, {report.vanished} gone"
                      f"{f', {report.remaining} to come' if report.remaining else ''}")
        elif name == "filed":
            filed = detail["filed"]
            print(f"  filed onto threads: {filed.threaded} by threading, "
                  f"{filed.by_address} by address, {filed.bounces} bounce(s)")

    results = engine.sync_once(paths.database, options=options, progress=progress)
    if not results:
        # Two very different situations, and saying the wrong one sends someone
        # looking for a disabled account that was never there.
        con = database.connect(paths.database, read_only=True)
        total = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
        con.close()
        print("no accounts are configured — see --add-account" if total == 0
              else "no account is due — every one is disabled or waiting")
        return 0

    failed = 0
    for result in results:
        if result.ok:
            print(f"  {result.address:<32} {result.new} new, "
                  f"{result.flags_changed} flags, {result.vanished} gone, "
                  f"{result.sent} sent"
                  f"{f', {result.posted} posted' if result.posted else ''}"
                  f"{f', {result.remaining} to come' if result.remaining else ''}")
        else:
            failed += 1
            print(f"  {result.address:<32} FAILED — {result.error}")
            if result.retry_at:
                print(f"  {'':<32} next attempt {result.retry_at}")
        for note in result.notes:
            print(f"  {'':<32} note: {note}")
    return failed


# --------------------------------------------------- the OAuth registration


def resync(address: str) -> int:
    """Discard one account's cached messages so the next sync fetches them again.

    Needed because a sync is INCREMENTAL by design: once a folder's UIDNEXT has
    moved past a message, nothing revisits it. That is right almost always and
    wrong in one case — a parser fixed after the message was stored, which
    leaves a body in the store that the current code would read differently.

    `store.folders.discard_contents` is the same machinery a changed
    UIDVALIDITY uses, and it is safe for the same reason: the server holds the
    mail and the local rows are a cache. What IS lost is anything local that
    the server does not know about, which is why the offline queue is reported
    and the account is refused while it has one outstanding.
    """
    from .app import current_paths
    from .store import accounts as accounts_repo
    from .store import database
    from .store import folders as folders_repo
    from .store import pending

    paths = current_paths().ensure()
    if not paths.database.exists():
        print("no store yet")
        return 1
    con = database.open_store(paths.database)
    try:
        account = accounts_repo.find_by_address(con, address)
        if account is None:
            print(f"{address} is not configured")
            return 1

        outstanding = pending.counts(con).get(account.id, {})
        waiting = outstanding.get("pending", 0) + outstanding.get("stuck", 0)
        if waiting:
            # Discarding now would throw away the rows those ops point at
            # before the server has been told. One sync first costs nothing.
            print(f"{address} has {waiting} change(s) not yet sent to the "
                  f"server. Run --sync first, then --resync")
            return 1

        removed = 0
        folders = folders_repo.list_folders(con, account.id, subscribed_only=False)
        for folder in folders:
            removed += folders_repo.discard_contents(con, folder.id)
        # Clearing `last_sync_at` as well, so the next sync is a FIRST sync and
        # the date window applies again. Without this, --resync silently turns
        # "fetch the last ninety days again" into "fetch ten years", because
        # `engine._since` only windows an account that has never finished a
        # sync. Observed: a re-fetch of 77 messages became 1,339 and counting.
        con.execute("UPDATE account SET last_sync_at = NULL WHERE id = ?",
                    (account.id,))
        con.commit()
        print(f"discarded {removed} cached message(s) across {len(folders)} "
              f"folders for {address}")
        print("the next sync starts fresh, so the initial_sync_days window "
              "applies again")
        print("now run:  python3 -m cormani --sync")
        return 0
    finally:
        con.close()
