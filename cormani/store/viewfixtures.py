# SPDX-License-Identifier: GPL-3.0-or-later
#
# Demo saved searches, and the mail they actually find.
#
# Beside `fixtures.py` for the reason `calendarfixtures.py`, `trackfixtures.py`
# and `rulefixtures.py` are: one subject rather than an addition to another one.
#
# ── WHY THE DEMO NEEDS SAVED SEARCHES ──────────────────────────────────────
#
# `rulefixtures.py` argues this for filters and the argument is different here.
# A filter is invisible, so a demo without one shows the interface in the state
# somebody would be in if the feature were broken. A saved search is perfectly
# visible — but it is visible only if one EXISTS, and an empty Saved searches
# section reads as a feature that has not been built rather than as a section
# nobody has put anything in. The rail's own note makes the same point about
# the site panels: a rail that hides what is coming reads as a rail that will
# never have it.
#
# ── THEY ARE CHECKED AGAINST THE DEMO'S MAIL, NOT INVENTED ─────────────────
#
# `rulefixtures.py` RUNS its rules so that the counters are what actually
# happened. The equivalent here is weaker and is worth stating rather than
# skipping: a saved view has no stored count to fake, since `count_capped` asks
# the question afresh every time it is drawn. What CAN be invented is a search
# that matches nothing — three virtual folders all reading zero, which is what
# a broken query looks like — so each of these is counted once as it is
# written, and `install` reports the totals for `tests/test_fixtures.py` to
# assert on. A demo whose saved searches all found nothing would be worse than
# none at all.
#
# ── ONE OF THEM IS DELIBERATELY OUT OF THE RAIL ────────────────────────────
#
# `in_rail` is the one thing about a saved search that cannot be shown by a
# list of searches that are all the same. Three virtual folders are a section
# and thirty are a wall, so the column exists; a demo in which every view is
# ticked never shows what the tick is for, and the Saved searches menu would
# look like a duplicate of the rail rather than the place the unticked ones
# live.
#
# ── AND NONE OF THEM NAMES A FOLDER ────────────────────────────────────────
#
# Every scope here is `unified`, so none can be broken by the demo being built
# in a different order or by an account being hidden. A demo fixture that could
# come out `unresolved` would put "THIS CANNOT RUN" in a screenshot.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from . import savedviews as savedviews_repo
from . import search as search_mod
from . import views as views_repo


def install(con: sqlite3.Connection) -> dict:
    """Write the demo's saved searches and report what each one finds."""
    wanted = [
        # The commonest virtual folder there is, and the one that shows the
        # difference between a saved search and a Quick Filter: the toggle is
        # forgotten when the folder changes, this one is not.
        savedviews_repo.SavedView(
            name="Unread, flagged",
            filters=views_repo.Filters(unread=True, flagged=True)),
        # A FULL-TEXT one, because that is the half a Quick Filter cannot do at
        # all — it searches every folder of every account rather than narrowing
        # what is on screen.
        savedviews_repo.SavedView(
            name="Wavelength question",
            search=search_mod.Query(text="wavelength"),
            sort=views_repo.Sort(key="date", descending=False)),
        # And one kept OUT of the rail. See the header.
        savedviews_repo.SavedView(
            name="Anything with an attachment", in_rail=False,
            filters=views_repo.Filters(attachment=True)),
    ]
    found = {}
    for view in wanted:
        saved = savedviews_repo.save_view(con, view, commit=False)
        found[saved.name] = savedviews_repo.count_in(con, saved)
    return {"views": len(wanted), "in_rail": sum(1 for v in wanted if v.in_rail),
            "found": found}
