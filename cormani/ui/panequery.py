# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the list is showing, and the six things that change it.
#
# `ui/mailpane.py` had an orphaned `# --- the query` section header with
# nothing under it: the methods it named had drifted down past the calendar,
# the tracking pane and the sites, and the header stayed where they had been.
# The 600-line rule fired on the file when the address book made a fourth host,
# and the seam it wanted was the one the file had already named and lost — the
# sixteenth the rule has found, and the second to be a claim the file made
# about itself and did not keep (`store/rules.py` was the first).
#
# ── FUNCTIONS TAKING THE PANE, AS `ui/panestate.py` DOES ───────────────────
#
# And they are siblings: that file is what a tab REMEMBERS about the query,
# this is what CHANGES it. There is no state here — every one of these reads
# and writes the pane's own model and flags — so a class would be a second name
# for each function and nothing else.
#
# ── ALL SIX END THE SAME WAY, AND THAT IS THE FAMILY RESEMBLANCE ───────────
#
# `if not pane._restoring: pane.view_changed.emit()`. `_restoring` is
# `ui/panestate.py`'s flag and its header explains the hazard at length:
# every setter here fires a change signal, and the window's handler for that
# signal writes the pane's state back onto the CURRENT tab, so restoring tab
# two would overwrite tab one on the way past.
#
# ── AND `_sort_chosen_in_search` LIVES HERE, WHICH IS WHY IT IS WORTH ONE ──
#
# The flag means "the user chose this order during this search, and it wins
# from here". Two of these six write it and one reads it, and while they were
# scattered through six hundred lines a saved-search test was hollow TWICE
# without anybody being able to see why — see SESSION_STATE, and note that
# `ui/window._sync_sort_menu` still round-trips into `set_sort` by accident.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from ..store import search as search_mod
from ..store import views as views_repo


def scope_chosen(pane, scope, key: str) -> None:
    pane.calendars.show(False)
    searching = pane.model.search.active
    if scope == pane.model.scope and not searching:
        # The rail re-selected the node it was already on — a rebuild, not a
        # choice. Doing the work anyway would clear the selection and drop
        # the message being read.
        return
    # Choosing a folder ENDS a search, and does it in the same refresh: the
    # rail is how a person says "show me this instead", and a search left
    # running over the top of it would ignore what they just clicked.
    sort = None
    if searching:
        sort = pane._sort_before_search or views_repo.Sort()
        pane._sort_before_search = None
    pane.model.set_query(scope=scope, sort=sort, search=search_mod.Query())
    if searching:
        pane.list.set_show_location(False)
        pane.search_changed.emit(search_mod.Query())
    pane._update_empty_text()
    # Nothing is selected on arriving in a folder. Selecting the first
    # message would MARK IT READ — every folder click would read a message
    # nobody looked at, and in a unified inbox across fifteen accounts that
    # is unread counts quietly going down on their own. Both Thunderbird and
    # Outlook open a folder with no selection for the same reason.
    pane.clear_selection()
    if not pane._restoring:
        pane.view_changed.emit()


def filters_changed(pane, filters) -> None:
    keep = pane.current_row()
    pane.model.set_query(filters=filters)
    pane._update_empty_text()
    # A filter narrows what is in view; if what was being read survived it,
    # carry on reading it.
    if keep is None or not pane.select_message(keep.id):
        pane.clear_selection()
    if not pane._restoring:
        pane.view_changed.emit()


def set_sort(pane, sort) -> None:
    if pane.model.search.active:
        pane._sort_chosen_in_search = True
    pane.model.set_query(sort=sort)
    if not pane._restoring:
        pane.view_changed.emit()


def set_threaded(pane, on: bool) -> None:
    """Group the list into conversations, or stop. The model decides
    whether it can — see `MessageModel.grouping`."""
    keep = pane.current_row()
    pane.model.set_query(threaded=bool(on))
    if keep is None or not pane.select_message(keep.id):
        pane.clear_selection()
    if not pane._restoring:
        pane.view_changed.emit()


def set_search(pane, query) -> None:
    """Run a search, change one, or leave it — the bar hands over the whole
    query and this decides what that means for the view.

    THE ORDER FOLLOWS THE SEARCH. A ranked query is shown best-first,
    because "the most relevant" is what someone typing into a search box
    asked for; the order they were using is remembered and restored when
    the search ends. If they choose an order DURING the search, that wins
    and keeps winning — `set_sort` records it.
    """
    if query == pane.model.search:
        return
    was = pane.model.search.active
    sort = None
    if query.active:
        if not was:
            pane._sort_before_search = pane.model.sort
            pane._sort_chosen_in_search = False
        if not pane._sort_chosen_in_search:
            sort = (views_repo.Sort(key=views_repo.SORT_RELEVANCE)
                    if search_mod.has_rank(query)
                    # Chips alone match nothing against the index, so there
                    # is no score. Back to the order they came in with.
                    else pane._sort_before_search or views_repo.Sort())
    elif was:
        sort = pane._sort_before_search or views_repo.Sort()
        pane._sort_before_search = None
        pane._sort_chosen_in_search = False
    pane.model.set_query(search=query, sort=sort)
    pane.list.set_show_location(query.active)
    pane._update_empty_text()
    # Nothing is selected in a fresh set of results, for the reason a folder
    # opens with nothing selected: selecting marks read.
    pane.clear_selection()
    if not pane._restoring:
        pane.view_changed.emit()


def accounts_changed(pane) -> None:
    # Hiding an account changes what a unified view covers, so the list has
    # to be re-asked rather than merely repainted.
    pane.model.refresh()
    pane._update_empty_text()
