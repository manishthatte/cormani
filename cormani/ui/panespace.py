# SPDX-License-Identifier: GPL-3.0-or-later
#
# Who owns the space the list and the reading pane occupy.
#
# PLAN.txt §3 gives that space to whatever the rail is showing, and by stage 8
# there are FOUR things that can be showing there: the calendar, the tracking
# board, a site panel and the address book. Only one may be visible at a time —
# a pane left visible under another is a window with two things drawn on top of
# one another, and it reads as a rendering fault rather than as a mistake.
#
# ── WHY THIS FILE EXISTS, WHICH IS A DEFECT AND NOT A TIDINESS ARGUMENT ────
#
# Each host used to carry its own hand-written list of the others. Four lists,
# and when the address book became the fourth claimant THREE OF THEM WERE
# WRONG:
#
#   `ui/trackhost.py`   stood down calendars and sites, not the address book
#   `ui/sitehost.py`    stood down calendars and tracking, not the address book
#   `ui/calendarhost.py` stood down sites only — tracking was stood down by
#                       `MailPane._calendar_chosen` instead, so a direct
#                       `calendars.show(True)` left the board underneath
#
# Nothing was wrong with any of those lists when it was written. They were made
# wrong by a change somewhere else, silently, and a test could only catch the
# combination it happened to try — which is why the one that caught it asserted
# BOTH orderings of one pair and would still have missed the other five.
#
# An n-way "everybody must know about everybody" relationship is a defect
# generator with n² places to forget. This is the one place, and adding a fifth
# claimant is one line here rather than four edits in four files that each look
# complete on their own.
#
# ── THE STAND-DOWN ARGUMENT IS DATA, BECAUSE THE HOSTS DISAGREE ────────────
#
# `SiteHost.show` takes a KEY and "" means none; the other three take a bool.
# The alternative — a `stand_down()` method on each host — is four more edits
# and a fifth thing for a new claimant to forget. A pair per row says it once.
#
# FUNCTIONS TAKING THE PANE, as `ui/panestate.py` and `ui/panequery.py` do, and
# for the same reason: there is no state here. The state is each host's own
# `showing`, and duplicating it would be a fifth place for it to be wrong.
#
# © Manish Jagdish Thatte
from __future__ import annotations

# (the pane's attribute, what its `show` is given to stand down). ONE LINE PER
# CLAIMANT, and this tuple is the whole registry — `tests/test_contacthost.py`
# asserts that every pair of them stands the other down, over this tuple rather
# than over a list of its own, so a fifth arrives already covered.
CLAIMANTS: tuple[tuple[str, object], ...] = (
    ("calendars", False),
    ("tracking", False),
    ("sites", ""),
    ("contacts", False),
)


def claim(pane, owner: str) -> None:
    """Ask every other claimant to stand down, before `owner` shows itself.

    Called by each host at the top of its own `show`, so that whoever was
    asked last owns the space — which is the rule the interface has followed
    since stage 5 and the one nobody could see written down anywhere.
    """
    for name, nothing in CLAIMANTS:
        if name == owner:
            continue
        host = getattr(pane, name, None)
        # `getattr(host, "showing", False)` and not `host.showing`: the sites
        # host is made on first use and a pane under construction has not
        # finished attaching all four, so this runs while some of them do not
        # exist yet.
        if host is not None and getattr(host, "showing", False):
            host.show(nothing)


def showing(pane) -> str:
    """Which claimant owns the space, or "" for the list and the reader.

    The positive question. `ui/viewhost.save_current` asked the negative one —
    "is it not the calendar, not tracking, not a site" — and the address book
    made that line wrong without changing a character of it: Save this search
    from the address book would have saved a search of a mail view nobody was
    looking at, under a name that described it.
    """
    for name, _nothing in CLAIMANTS:
        host = getattr(pane, name, None)
        if host is not None and getattr(host, "showing", False):
            return name
    return ""
