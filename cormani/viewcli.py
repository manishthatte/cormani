# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the command line does about saved searches.
#
# The fifth file of the command line — `cli.py` is mail, `calcli.py` is
# calendars, `configure.py` is account setup, `rulecli.py` is filters — and it
# is beside `rulecli.py` rather than inside it although both spend migration 9.
# The seam falls where the MODULE split does: `store/rules.py` and
# `store/savedviews.py` are two subjects, so these are two files. That is the
# answer whenever it is available, and length is the answer only when it is not.
#
# ── WHY A SAVED SEARCH NEEDS A READ-OUT AT ALL, WHICH IS NOT OBVIOUS ───────
#
# `rulecli.py` opens by arguing that a filter is invisible in both its states,
# so the only evidence is a report. A saved search is the opposite: it is a row
# in the rail with a count beside it, which is about as visible as this program
# gets. So the case has to be made differently, and it is made by ONE of the
# three numbers below.
#
# It is `unresolved`. A saved search whose folder was deleted, whose account
# was removed or whose tag was dropped still draws — `store/views.scope_where`
# turns all three into the literal `0`, which is the correct WHERE clause and
# says nothing — so what the user sees is a virtual folder that is empty today
# and was full last week. The rail marks it and the manager dialog explains it,
# and both of those are inside the window. This is the one that works when the
# window is what is wrong.
#
# The other two numbers are ordinary bookkeeping and would not have justified a
# switch by themselves.
#
# ── READ-ONLY AND OFFLINE, LIKE `--check`, `--calendars` AND `--filters` ───
#
# It opens the store read-only and speaks to nothing. `store/triage.py` had a
# READING path that wrote and `--check` is what found it; a report has to work
# when the disk is what is wrong.
#
# ── THE COUNT IS EXACT HERE ────────────────────────────────────────────────
#
# `store/savedviews.count_capped` exists because the rail redraws and cannot
# afford an exact count. This runs once, from a terminal, and a report that
# said "999+" would be a report refusing to answer the question it was asked.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

# The schema version at which a saved search exists at all. Guarded for the
# reason `rulecli.RULES_SCHEMA` and `calcli.CALENDAR_SCHEMA` are: `--check` must
# survive a store older than this stage, and above all one that has not been
# migrated because the migration is what is broken.
VIEWS_SCHEMA = 9


# ------------------------------------------------------------------- report
def report(con) -> None:
    """The saved-search lines of `--check`. Read-only; never raises.

    One line, and a second only when a view can no longer mean what it says —
    which is the state nothing else outside the window can tell anybody about.
    """
    from .store import savedviews as savedviews_repo

    counts = savedviews_repo.counts(con)
    if not counts["views"]:
        return
    total = counts["views"]
    line = f"  saved searches   {total} saved"
    hidden = total - counts["in_rail"]
    if hidden:
        line += f", {hidden} not drawn in the rail"
    print(line)
    if counts["unresolved"]:
        broken = counts["unresolved"]
        print(f"    {broken} {'names' if broken == 1 else 'name'} a folder, "
              f"account or tag that has gone — {'it draws' if broken == 1 else 'they draw'} "
              f"as empty (see --searches)")


# ----------------------------------------------------------------- searches
def searches() -> int:
    """`--searches`: every saved search, what it asks for, and what it holds."""
    from .app import current_paths
    from .store import database
    from .store import savedviews as savedviews_repo

    paths = current_paths()
    if not paths.database.exists():
        print(f"no store yet ({paths.database})")
        return 1
    con = database.connect(paths.database, read_only=True)
    try:
        if database.schema_version(con) < VIEWS_SCHEMA:
            print("this store predates saved searches — start corMani once "
                  "to migrate it")
            return 1
        views = savedviews_repo.list_views(con)
        if not views:
            print("no saved searches — Edit ▸ Save this search keeps the one "
                  "on screen as a virtual folder in the rail")
            return 0
        for view in views:
            _print_view(con, view)
        _print_footer(con, views)
        return 0
    finally:
        con.close()


def _print_view(con, view) -> None:
    from .store import savedviews as savedviews_repo

    where = "in the rail" if view.in_rail else "Search menu only"
    print(f"\n{view.name}   [{where}]")
    # `narrowing` and not `describe`: the scope is named on this line already,
    # and `describe` opens with it — "every inbox · every inbox, unread".
    narrowing = view.narrowing()
    print(f"   asks   {savedviews_repo.describe_scope_here(con, view)}"
          + (f" · {narrowing}" if narrowing else ""))
    wrong = savedviews_repo.unresolved(con, view)
    if wrong:
        # NOT counted as well as explained. A number beside a broken view would
        # be 0, and a 0 that means "this cannot run" beside a 0 that means "no
        # mail matches" is two different facts wearing one digit.
        print(f"   holds  nothing — THIS CANNOT RUN: {wrong}")
        return
    held = savedviews_repo.count_in(con, view)
    print(f"   holds  {held} message{'' if held == 1 else 's'} right now")


def _print_footer(con, views) -> None:
    """What the list above does not say for itself."""
    from .store import savedviews as savedviews_repo

    broken = [v for v in views if savedviews_repo.unresolved(con, v)]
    if broken:
        print("\nA search that cannot run is kept, never deleted: the folder "
              "comes back when the account is re-added. Edit ▸ Manage saved "
              "searches is where one is removed for good.")
    if any(not v.in_rail for v in views):
        print("\nA search not drawn in the rail still runs — it is in the "
              "Saved searches menu. Three virtual folders are a section and "
              "thirty are a wall, which is what the setting is for.")
