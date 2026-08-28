# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the command line does about calendars.
#
# `cli.py` is the mail half and `configure.py` is account setup. This is the
# third, and it is a separate file for the reason the second one was —
# CONVENTIONS.txt §4, and a 600-line rule that has found eleven seams in this
# repository and been right at every one of them.
#
# THE COMMAND LINE HAD TO LEARN THIS BECAUSE THE WINDOW ALREADY KNEW IT. F5
# syncs mail AND calendars — `ui/window.sync_now` says in as many words that
# F5 means "everything, now", and drives both controllers — while `--sync`
# fetched mail alone. The two ways of driving corMani disagreed about what a
# sync IS, and the one without a display is the one that runs for hours,
# survives the window being closed, and gets scheduled. So `--sync` is the
# headless half of F5 and does both; `--sync mail` and `--sync calendars` are
# there because a first mail import takes hours and watching a calendar behind
# it is not watching anything.
#
# THE REPORTS ARE OFFLINE AND THAT IS THE POINT. `--check` and `--calendars`
# open the store read-only and ask no provider anything, for the reason
# `cli.check` gives: the report has to work when the network is the problem.
# What they can report is what the store HOLDS — which calendars are known,
# what window of instances is in them, whether a token is held — and every one
# of those is a question somebody asks when a calendar looks wrong.
#
# A SYNC TOKEN IS THE ONE FACT WORTH PRINTING PLAINLY. It is the difference
# between a second pass that asks "what changed since" and one that fetches the
# whole window again, and it is invisible in the interface by design. OPEN ITEM
# 9 in SESSION_STATE.txt names "is the second pass incremental" as the first
# thing to look at when this finally meets a real server, and this is where it
# is answered. The token's VALUE is never printed: it is a bearer bookmark, and
# CONVENTIONS.txt §7 covers what happens to those.
#
# THE WINDOW IS PRINTED IN LOCAL DATES, NOT THE UTC IT IS STORED IN. At
# UTC+05:30 a window anchored to the first of May is stored as 30 April, 18:30
# UTC; printing the stored date would report a window a day short of the one
# the store actually keeps, and the person reading it is trying to find out
# whether a particular day is in it.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

# The schema version at which a calendar exists at all. Everything here is
# guarded by it, because `--check` must survive a store older than stage 5 —
# and, more to the point, a store that has not been migrated yet because the
# migration is what is broken.
CALENDAR_SCHEMA = 6


# ------------------------------------------------------------------- report
def report(con) -> None:
    """The calendar lines of `--check`. Read-only; never raises.

    Deliberately short. `--check` is a page a person reads in a hurry when
    something is wrong, and it earns its place by being one line per subsystem
    with the exceptions indented under it — so this prints the totals, and then
    names only what is actually parked or unfetched. `--calendars` is where the
    full table lives, for when the summary says something is wrong and the next
    question is which one.
    """
    from .store import calendars as calendars_repo
    from .store import events as events_repo
    from .store import eventqueue

    known = calendars_repo.list_calendars(con)
    if not known:
        print("  calendars        none known — none has been synced yet")
        return

    accounts = {c.account_id for c in known}
    total_events = con.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    unfetched = [c for c in known if not (c.synced_from and c.synced_to)]
    print(f"  calendars        {len(known)} across {len(accounts)} account(s), "
          f"{total_events} events held"
          + (f"  ({len(unfetched)} never fetched)" if unfetched else ""))

    queued = eventqueue.counts(con)
    waiting = sum(v["pending"] for v in queued.values())
    stuck = sum(v["stuck"] for v in queued.values())
    if waiting or stuck:
        print(f"  calendar queue   {waiting} waiting"
              f"{f', {stuck} no longer retried' if stuck else ''}")

    _report_parked(con, known)
    replies = len(events_repo.needing_reply(con))
    if replies:
        # An unanswered invitation is a thing somebody is waiting on, and it is
        # the one calendar fact that is about a person rather than a protocol.
        print(f"  invitations      {replies} not yet answered")


def _report_parked(con, known) -> None:
    """Only what is wrong, and named so it can be found.

    Two levels, because the engine has two: an account can be parked with no
    calendar rows to park — the first thing a sync does is ASK for the list —
    and a single shared feed can be parked while the calendar the user works
    from is perfectly healthy. Reporting only one of them hides the other.
    """
    from .store import accounts as accounts_repo
    from .store import calendars as calendars_repo

    addresses = {a.id: a.address for a in accounts_repo.list_accounts(con)}
    for account_id, state in sorted(accounts_repo.calendar_state(con).items()):
        if not (state["error"] or state["until"]):
            continue
        detail = state["error"] or "waiting"
        until = f" until {state['until']}" if state["until"] else ""
        print(f"    {addresses.get(account_id, account_id):<32} "
              f"calendars parked{until} — {detail[:60]}")

    by_id = {c.id: c for c in known}
    for calendar_id, state in sorted(calendars_repo.failures(con).items()):
        calendar = by_id.get(calendar_id)
        if calendar is None:
            continue
        detail = state["error"] or "waiting"
        print(f"    {calendar.label[:32]:<32} {detail[:60]}")


# --------------------------------------------------------------------- sync
def sync(paths, *, verbose: bool = True) -> int:
    """Every due account's calendars. Returns how many accounts FAILED.

    A count rather than an exit code, because `--sync` runs this beside the
    mail half and one exit code has to answer for both. The caller adds them.

    The store is not checked for existence here: `cli.sync` has already done
    it, and doing it twice would print the same refusal twice for a person who
    typed one command.
    """
    from .calendar import engine

    def progress(name: str, detail: dict) -> None:
        if not verbose:
            return
        if name == "account:start":
            print(f"  {detail['address']} …", flush=True)
        elif name == "calendar:done":
            report_ = detail["report"]
            if report_.changed or report_.removed:
                print(f"    {detail['calendar']}: {report_.changed} changed, "
                      f"{report_.removed} gone")

    print("calendars")
    # A bare `Options()`, which is what `app.py` hands the window's controller:
    # there is no calendar setting in `config/settings.py` to read, and a
    # second source of defaults would be the command line and the window
    # fetching different windows of the same calendar.
    results = engine.sync_once(paths.database, options=engine.Options(),
                               progress=progress)
    if not results:
        print(f"  {_nothing_due(paths.database)}")
        return 0

    failed = 0
    for result in results:
        if result.unsupported:
            # Not a failure and never an error field: a plain IMAP account has
            # no calendar to sync and is behaving exactly as expected. See the
            # engine's header. The note already names the account, so it is
            # printed whole rather than split back apart into a column.
            for note in result.notes:
                print(f"  {note}")
            continue
        if result.ok:
            print(f"  {result.address:<32} {result.calendars} calendar(s), "
                  f"{result.changed} changed, {result.removed} gone, "
                  f"{result.sent} sent"
                  f"{f', {result.dropped} dropped' if result.dropped else ''}"
                  f"{f', {result.conflicts} conflicts' if result.conflicts else ''}")
        else:
            failed += 1
            print(f"  {result.address:<32} FAILED — {result.error}")
            if result.retry_at:
                print(f"  {'':<32} next attempt {result.retry_at}")
        for note in result.notes:
            print(f"  {'':<32} note: {note}")
    return failed


def _nothing_due(database_path) -> str:
    """Why there is nothing to do, in the words that send someone the right way.

    Three situations that look identical from an empty result list and are not:
    no accounts at all, accounts whose providers have no calendar API, and
    accounts that are simply parked. Saying the wrong one sends a person
    looking for a disabled account that was never there — `cli.sync` makes the
    same distinction for mail and for the same reason.
    """
    from .calendar.engine import CLIENTS
    from .store import database

    con = database.connect(database_path, read_only=True)
    try:
        rows = con.execute("SELECT provider FROM account").fetchall()
    finally:
        con.close()
    if not rows:
        return "no accounts are configured — see --add-account"
    if not any(row["provider"] in CLIENTS for row in rows):
        return ("no account's provider has a calendar API — nothing to sync, "
                "and nothing wrong")
    return "no calendar is due — every one is disabled or waiting"


# --------------------------------------------------------------- --calendars
def calendars(address: str = "") -> int:
    """The full table: every calendar, what is in it, and where its sync got to.

    The command that did not exist while stage 5 was being built, and the gap
    was felt immediately: a calendar can be un-ticked in the rail, parked by a
    provider, or holding a window that does not reach the month being looked
    at, and NONE of those is visible without a display. This is the read-out.
    """
    from .app import current_paths
    from .store import accounts as accounts_repo
    from .store import calendars as calendars_repo
    from .store import database

    paths = current_paths()
    if not paths.database.exists():
        print(f"no store yet ({paths.database})")
        return 1
    con = database.connect(paths.database, read_only=True)
    try:
        if database.schema_version(con) < CALENDAR_SCHEMA:
            print("this store predates calendars — start corMani once to "
                  "migrate it")
            return 1
        wanted = [a for a in accounts_repo.list_accounts(con)
                  if not address or a.address == address]
        if address and not wanted:
            print(f"{address} is not configured")
            return 1
        if not wanted:
            print("no accounts are configured — see --add-account")
            return 0
        _print_accounts(con, wanted)
        _print_upcoming(con)
        return 0
    finally:
        con.close()


def _print_accounts(con, wanted) -> None:
    from .calendar.engine import CLIENTS
    from .store import calendars as calendars_repo
    from .store import events as events_repo

    counts = events_repo.counts_by_calendar(con)
    for account in wanted:
        print(f"{account.address} ({account.provider})"
              f"{'' if account.enabled else '  — account disabled'}")
        if account.provider not in CLIENTS:
            print("  this provider has no calendar API")
            continue
        known = calendars_repo.list_calendars(con, account.id,
                                              include_absent=True)
        if not known:
            print("  no calendars known — this account has not synced yet")
            continue
        for calendar in known:
            _print_calendar(calendar, counts.get(calendar.id, 0))


def _print_calendar(calendar, held: int) -> None:
    """One calendar, in three lines: what it is, what is in it, where it got to."""
    marks = [("primary" if calendar.is_primary else ""),
             ("writable" if calendar.writable else "read-only"),
             ("shown" if calendar.shown else "hidden"),
             ("" if calendar.present else "NO LONGER LISTED")]
    print(f"  {calendar.label[:38]:<38} "
          f"{', '.join(m for m in marks if m)}")
    print(f"      {held} event(s), window {_window(calendar)}")
    print(f"      {_sync_state(calendar)}")
    if calendar.last_error:
        print(f"      last error: {calendar.last_error[:70]}")


def _window(calendar) -> str:
    """The range of instances this calendar holds, as local dates.

    "not fetched" rather than a blank pair, because the two are read very
    differently: a calendar with no window is one nothing has ever asked the
    provider about, and a view over it draws an empty month that is a lie
    rather than an answer — which is exactly what `ui/calendarpane._footer`
    refuses to do.
    """
    from .store import times

    if not (calendar.synced_from and calendar.synced_to):
        return "not fetched"
    first = times.to_local(calendar.synced_from)
    last = times.to_local(calendar.synced_to)
    if not (first and last):
        return f"{calendar.synced_from} → {calendar.synced_to}"
    return f"{first.date().isoformat()} → {last.date().isoformat()} (local)"


def _sync_state(calendar) -> str:
    """Where the sync got to, and whether the next pass will be incremental."""
    when = calendar.last_synced_at or "never"
    if calendar.sync_token:
        how = "a token is held, so the next pass asks only what changed"
    else:
        how = "no token, so the next pass fetches the whole window"
    parked = (f"; parked until {calendar.next_attempt_at} after "
              f"{calendar.sync_failures} failure(s)"
              if calendar.next_attempt_at else "")
    return f"synced {when} — {how}{parked}"


def _print_upcoming(con, *, limit: int = 12, days: int = 14) -> None:
    """The next fortnight, one line an event, with an all-day one marked.

    Here because of the second question OPEN ITEM 9 asks of a first real run —
    "does an all-day event land on the right day" — which no summary of tokens
    and windows can answer. An appointment is an instant and an all-day event
    is a date; `store/times.py` is the whole argument, and this is the one
    place in the output where getting it wrong is visible.

    BUCKETED BY DAY RATHER THAN LISTED BY START, and `events.by_day` does it
    rather than a sort written here. Two things fall out of that and both are
    the reason: an event running from yesterday into today appears under TODAY
    rather than under a date outside the range this heading claims, and a
    three-day conference appears on each of its three days, which is what an
    agenda is. `events.upcoming` is deliberately NOT used — its order is the
    one a day column wants, all-day first, so taking the first twelve of a
    fortnight would return twelve all-day events and no appointments at all.
    """
    from .store import events as events_repo
    from .store import times

    tz = times.local_zone()
    start, _ = times.day_bounds(times.now_local().date(), tz)
    end = start + dt.timedelta(days=days)
    buckets = events_repo.by_day(events_repo.events_between(con, start, end),
                                 start, end, tz=tz)
    # `needs_reply` is a question about the ATTENDEE LIST, and neither of the
    # two calls above loads one — asking an event without its guests answers
    # "yes" for everything with an organiser, the user's own events included.
    # So the set comes from the store's own answer to the question.
    unanswered = {e.id for e in events_repo.needing_reply(con)}

    print(f"next {days} days")
    listing = [(day, event) for day in sorted(buckets)
               for event in buckets[day]]
    if not listing:
        print("  nothing")
        return
    for day, event in listing[:limit]:
        when = "ALL DAY"
        if not event.all_day:
            begins = event.start(tz)
            when = begins.astimezone(tz).strftime("%H:%M") if begins else "?"
        print(f"  {day.isoformat()}  {when:<8} {event.title[:44]}"
              f"{'  (no reply sent)' if event.id in unanswered else ''}")
    if len(listing) > limit:
        # A day-appearance rather than an event: a multi-day event is in this
        # count once per day it covers, because that is what was listed.
        print(f"  … and {len(listing) - limit} more")
