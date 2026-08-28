# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the command line does about filters.
#
# The fourth file of the command line: `cli.py` is mail, `calcli.py` is
# calendars, `configure.py` is account setup, and a new command with a body
# goes beside them rather than into `__main__.py`, which parses and dispatches
# and has nothing with a body in it. CONVENTIONS.txt §4 — and `cli.py` stood at
# 504 lines when this was written, so the seam was going to be found either
# way; it is better found by what the code is ABOUT than by its length.
#
# ── WHY A FILTER NEEDS A READ-OUT MORE THAN MOST THINGS DO ─────────────────
#
# Every other subsystem announces itself. Mail arrives, a calendar draws, a
# tracked thread sits on a board. A filter is invisible when it works and
# invisible when it does not: mail that was moved is in the folder the rule
# named, mail that was NOT moved is in the Inbox, and those are also what the
# two states look like when no rule ran at all. The only difference a person
# can see from outside is one they have to be told.
#
# So this prints the two numbers `filter_rule` keeps — `match_count` and
# `last_matched_at` — and says plainly which rules have never matched anything.
# `store/rulesschema.py` argues for those columns instead of an audit log; this
# is the file that spends them. A rule that stopped matching when a
# correspondent changed their address is invisible from every other direction,
# including from the dialog that wrote it.
#
# ── READ-ONLY AND OFFLINE, LIKE `--check` AND `--calendars` ────────────────
#
# It opens the store read-only and speaks to nothing. That is not caution for
# its own sake: `store/triage.py` had a READING path that wrote, and `--check`
# is what found it, because a report has to work when the disk is what is
# wrong. A rule is also the kind of thing a person goes looking at precisely
# when they suspect it of having done something, and a report that could itself
# change the store is a report that cannot answer that question.
#
# Running the rules over mail already here is a WRITE and is not here. It
# belongs where the result can be seen and taken back — `store/rulerun.py`'s
# `run_over_folder`, driven from the interface.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

# The schema version at which a filter rule exists at all. Everything here is
# guarded by it, for the reason `calcli.CALENDAR_SCHEMA` is: `--check` must
# survive a store older than this stage, and above all a store that has not
# been migrated because the migration is what is broken.
RULES_SCHEMA = 9


# ------------------------------------------------------------------- report
def report(con) -> None:
    """The filter lines of `--check`. Read-only; never raises.

    One line, and a second only when something is wrong with a rule rather
    than with the mail. `--check` is a page read in a hurry; `--filters` is the
    table it points at.
    """
    from .store import rules as rules_repo

    counts = rules_repo.counts(con)
    if not counts["rules"]:
        return
    off = counts["rules"] - counts["enabled"]
    line = f"  filters          {counts['rules']} rule(s)"
    if off:
        line += f", {off} switched off"
    print(line)
    if counts["incomplete"]:
        print(f"    {counts['incomplete']} half-written — kept, never run "
              f"(see --filters)")
    if counts["never_matched"]:
        # The question the counters exist for. Not an error: a rule written
        # this morning has matched nothing and is perfectly healthy.
        idle = counts["never_matched"]
        print(f"    {idle} enabled rule{'' if idle == 1 else 's'} "
              f"{'has' if idle == 1 else 'have'} never matched anything")


# ------------------------------------------------------------------ filters
def filters(address: str = "") -> int:
    """`--filters`: every rule, in the order they run, with what it has done.

    The order is numbered because ORDER IS MEANING here — a rule with
    `stop_after` claims a message outright and the rules below it never see
    it — so a list that did not say which came first would be missing the one
    thing that decides what a set of rules does.
    """
    from .app import current_paths
    from .store import accounts as accounts_repo
    from .store import database
    from .store import rules as rules_repo

    paths = current_paths()
    if not paths.database.exists():
        print(f"no store yet ({paths.database})")
        return 1
    con = database.connect(paths.database, read_only=True)
    try:
        if database.schema_version(con) < RULES_SCHEMA:
            print("this store predates filters — start corMani once to "
                  "migrate it")
            return 1
        by_id = {a.id: a for a in accounts_repo.list_accounts(con)}
        if address and not any(a.address == address for a in by_id.values()):
            print(f"{address} is not configured")
            return 1

        every = rules_repo.list_rules(con)
        wanted = [r for r in every if _applies(r, by_id, address)]
        if not every:
            print("no filter rules — nothing arriving is being moved, tagged "
                  "or marked by a rule")
            return 0
        if not wanted:
            print(f"no filter rules run against {address}")
            return 0

        for position, rule in enumerate(every, start=1):
            if rule in wanted:
                _print_rule(con, rule, position, by_id)
        _print_footer(every, hidden=len(every) - len(wanted))
        return 0
    finally:
        con.close()


def _applies(rule, by_id, address: str) -> bool:
    """Whether this rule would ever see mail arriving at `address`.

    A rule with no account is every account's, which is the useful default and
    is also the answer people forget when they ask why a rule fired.
    """
    if not address:
        return True
    if rule.account_id is None:
        return True
    account = by_id.get(rule.account_id)
    return account is not None and account.address == address


def _print_rule(con, rule, position: int, by_id) -> None:
    from .store import rules as rules_repo

    scope = "every account"
    if rule.account_id is not None:
        account = by_id.get(rule.account_id)
        scope = account.address if account else "an account that is gone"
    marks = []
    if not rule.enabled:
        marks.append("SWITCHED OFF")
    if rule.stop_after:
        marks.append("stops the run")
    if not rule.is_complete:
        # Short here and explained below: the body lines already say WHICH
        # half is missing, and printing the same sentence twice reads as a
        # report that is padding itself.
        marks.append("HALF-WRITTEN")
    tail = ("  — " + ", ".join(marks)) if marks else ""

    print(f"\n{position}. {rule.name}   [{scope}]{tail}")
    joiner = "and" if rule.match_all else "or"
    for index, condition in enumerate(rule.conditions):
        lead = "   when" if index == 0 else f"    {joiner}"
        print(f"{lead} {condition.describe()}")
    if not rule.conditions:
        print("   when  — nothing, so it matches nothing")
    for index, action in enumerate(rule.actions):
        lead = "   then" if index == 0 else "    and"
        print(f"{lead} {rules_repo.describe_action(con, action)}")
    if not rule.actions:
        print("   then  — nothing, so it would do nothing")
    print(f"        matched {_times(rule.match_count)}"
          + (f", last on {rule.last_matched_at[:10]}"
             if rule.last_matched_at else ""))


def _times(count: int) -> str:
    if not count:
        return "nothing yet"
    return "once" if count == 1 else f"{count} times"


def _print_footer(every, *, hidden: int = 0) -> None:
    """What the list above does not show.

    THE NUMBERS ARE GLOBAL, SO A NARROWED LIST HAS GAPS IN IT — 1, 3, 4 — and
    a gap with no explanation reads as a missing rule rather than as a rule
    that does not run here. Numbering the narrowed list 1, 2, 3 instead would
    be worse: the number would then mean nothing, and the order is the one
    thing about a set of rules that cannot be worked out by reading them.
    """
    if hidden:
        print("\nNumbered in the order every rule runs, so the "
              + (f"gap is the one rule that does not" if hidden == 1
                 else f"gaps are the {hidden} rules that do not")
              + " run against this account.")
    if any(r.stop_after for r in every):
        print("\nA rule that stops the run claims the message: the rules "
              "numbered below it never see it.")
