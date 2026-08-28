# SPDX-License-Identifier: GPL-3.0-or-later
#
# Demo filter rules, and the mail they actually caught.
#
# Beside `fixtures.py` for the reason `calendarfixtures.py` and
# `trackfixtures.py` are: one subject rather than an addition to another one.
#
# ── WHY THE DEMO NEEDS RULES AT ALL ────────────────────────────────────────
#
# A filter is invisible when it works and invisible when it does not, so a demo
# with no rules in it does not merely omit the feature — it shows the interface
# in exactly the state somebody would be in if the feature were broken. The
# Tools ▸ Message filters dialog would be an empty list, which is what a person
# who had written three rules and lost them would also see.
#
# ── THE RULES ARE RUN, NOT INVENTED ────────────────────────────────────────
#
# `filter_rule` keeps `match_count` and `last_matched_at`, and those two
# integers are the only evidence a filter ever offers. Writing plausible
# numbers into them would be inventing evidence: the demo would show a rule
# that had matched forty-one messages and a mailbox in which nothing had been
# tagged. So the rules are RUN, through `store/rulerun.py`, over the demo's own
# inboxes — the same code path a sync takes — and the counts are whatever
# actually happened. It is `trackfixtures.py`'s argument for filing demo
# timelines with the real matchers, one stage later: a fixture that invents
# what the code would have done cannot notice the code no longer doing it.
#
# ── AND WHY THEY RUN AFTER THE STORE IS COMMITTED ──────────────────────────
#
# Every other fixture writes inside `fixtures.install`'s single transaction.
# This one cannot: the actions go through `store/edits.py` and `store/tags.py`,
# each of which opens its own `with con:` because each is one thing a person
# could have done by hand. Rather than weaken that — a filter that wrote
# without committing would be a filter unlike every other write in the
# store — the rules run last, over a demo store that is already complete and
# already marked. A failure here therefore leaves a usable demo whose rules
# have caught nothing, which is a state the interface can describe; a failure
# inside the transaction would leave accounts written and `is_demo` unset,
# which `install` then refuses to build over.
#
# ── ONLY TAGS, AND ONE MOVE THAT IS SWITCHED OFF ───────────────────────────
#
# The enabled rules TAG. A tag is local by design — `store/tags.py` says why —
# so the demo shows filters at work without rearranging the folders the rest of
# the fixtures carefully arranged, and without the tracking layer's timelines
# suddenly finding their mail somewhere else.
#
# The third rule MOVES, and is switched off. It is there because a move is what
# people mean by a filter, and because the two states a rule can be in are
# worth showing side by side. Note it would in fact be harmless if it ran: a
# demo message has `uid = NULL`, so `pending.enqueue_move` skips it and nothing
# is queued for a server that does not exist. It is off for the sake of the
# demo's shape, not for safety.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from . import folders as folders_repo
from . import rulematch
from . import rulerun
from . import rules as rules_repo
from . import tags as tags_repo


def install(con: sqlite3.Connection) -> dict:
    """Write the demo rules, run the enabled ones, and report what they caught."""
    by_name = {tag.name: tag.id for tag in tags_repo.list_tags(con)}

    # A SPECIFIC RULE ABOVE A GENERAL ONE, with `stop_after`, because that is
    # the idiom the order exists for and it cannot be shown by a list of rules
    # that never interact.
    rules_repo.save_rule(con, rulematch.Rule(
        name="Covalent Example", stop_after=True,
        conditions=(rulematch.Condition("from", "contains",
                                        "covalent.example"),),
        actions=(rulematch.Action(kind="tag", tag_id=by_name["Work"]),)))

    rules_repo.save_rule(con, rulematch.Rule(
        name="Reading, not doing", match_all=False,
        conditions=(rulematch.Condition("subject", "contains", "digest"),
                    rulematch.Condition("subject", "contains", "summary")),
        actions=(rulematch.Action(kind="tag", tag_id=by_name["Later"]),
                 rulematch.Action(kind="silence"))))

    rules_repo.save_rule(con, rulematch.Rule(
        name="File the receipts", enabled=False,
        conditions=(rulematch.Condition("subject", "contains", "receipt"),),
        actions=(rulematch.Action(kind="move",
                                  value=folders_repo.ROLE_ARCHIVE),)))

    # OVER THE INBOXES AND NOT OVER EVERYTHING. A filter is about mail
    # ARRIVING, and the demo's Sent folders hold the user's own half of these
    # conversations; running arrival rules over those is the mistake
    # `ui/filterhost.py` refuses to make on a real store.
    arrived = [int(r[0]) for r in con.execute(
        "SELECT m.id FROM message m JOIN folder f ON f.id = m.folder_id "
        "WHERE f.role = ? AND m.deleted = 0 ORDER BY m.id",
        (folders_repo.ROLE_INBOX,))]
    report = rulerun.run(con, arrived)
    return {"rules": len(rules_repo.list_rules(con)),
            "considered": report.considered, "matched": report.matched}
