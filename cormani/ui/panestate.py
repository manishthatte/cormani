# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a tab remembers, and putting the pane back into it.
#
# The eleventh seam the 600-line rule has found in `ui/mailpane.py`'s
# neighbourhood, and it falls here rather than at the action dispatch — which
# the previous edition of SESSION_STATE.txt predicted — because two better
# seams appeared first: `ui/actions.py` took what a command MEANS and
# `ui/calendarhost.py` took the calendar half. What is left over is arranging
# widgets, and this is the one part of that which is not about widgets at all:
# it is the CLAIM that a saved `ViewState` and a live pane describe the same
# thing.
#
# ── RESTORING MUST NOT EMIT ────────────────────────────────────────────────
#
# `_restoring` is the whole of why this is delicate. Every setter here fires a
# change signal, and the window's handler for that signal writes the pane's
# state back onto the CURRENT tab — so restoring tab two would overwrite tab
# one with tab two's state on the way past. The flag is the pane's and is set
# here because this is the only place that drives every setter in one go.
#
# ── THE ORDER OF THE FOUR KINDS IS NOT ARBITRARY ───────────────────────────
#
# Sites, tracking, the address book, then calendar, then mail. Each of the
# first four RETURNS once it has claimed the pane, and each is asked to stand
# down before the next is tried, because all of them want the space the list
# and the reading pane occupy. A pane left visible under another is a window
# with two things drawn on top of one another, and it looks like a rendering
# fault rather than a mistake.
#
# THE ORDER ITSELF IS ARBITRARY AND THE EXHAUSTIVENESS IS NOT. Any order works
# so long as every claimant is either restored or stood down; what would break
# is a fifth kind added to `ViewState` and not to this chain, which would
# restore as mail with the fifth pane still visible underneath. That is why
# `view_state` below sets all four in one expression — the two halves are read
# together or one of them is forgotten.
#
# ── A TAB'S NAME IS A CLAIM ABOUT WHAT IS IN IT ────────────────────────────
#
# Which is why a searching tab is named for the search and not for the rail row
# underneath it, and why the tracking tab carries its counts: naming a tab of
# results "Inbox" is a lie a person acts on.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt

from ..store import accounts as accounts_repo
from .models.rail import ScopeRole
from .tabs import ViewState


def _mode_prefix(mode: str, label: str) -> str:
    """Tab title with wayfinding prefix — ui.md §3.2."""
    return f"{mode} · {label}" if label else mode


def show_mail(pane) -> None:
    """Return to the message list from any alternate pane."""
    from . import panespace

    for name, nothing in panespace.CLAIMANTS:
        host = getattr(pane, name, None)
        if host is not None and getattr(host, "showing", False):
            host.show(nothing)
    pane.middle.setVisible(True)
    pane.reader.setVisible(True)
    if pane.rail.current_key() != "unified:inbox":
        pane.rail.select_key("unified:inbox")
    pane.view_changed.emit()


def showing_mail(pane) -> bool:
    from . import panespace

    return not panespace.showing(pane)


def view_state(pane, title: str) -> ViewState:
    """Everything needed to put this pane back exactly as it is now."""
    row = pane.current_row()
    calendar_id, mode, anchor = pane.calendars.state()
    return ViewState(
        title=title, rail_key=pane.rail.current_key(),
        scope=pane.model.scope, filters=pane.model.filters,
        sort=pane.model.sort, search=pane.model.search,
        threaded=pane.model.threaded,
        selected_id=row.id if row else None,
        calendar_id=calendar_id, calendar_mode=mode,
        calendar_anchor=anchor, thread_id=pane.tracking.state(),
        site_key=pane.sites.showing,
        contact_id=pane.contacts.state())


def restore(pane, state: ViewState) -> None:
    """Put the pane into a saved state without emitting change signals."""
    pane._restoring = True
    try:
        pane.rail.select_key(state.rail_key)
        if state.is_site:
            pane.sites.show(state.site_key)
            return
        pane.sites.show("")
        if state.is_tracking:
            pane.tracking.restore(state)
            return
        pane.tracking.show(False)
        if state.is_contacts:
            pane.contacts.restore(state)
            return
        pane.contacts.show(False)
        if state.is_calendar:
            pane.calendars.restore(state)
            return
        pane.calendars.show(False)
        pane.quick_filter.set_filters(state.filters)
        pane.model.set_query(scope=state.scope, filters=state.filters,
                             sort=state.sort, search=state.search,
                             threaded=state.threaded)
        pane.list.set_show_location(state.search.active)
        pane.search_changed.emit(state.search)
        pane._update_empty_text()
        restored = (state.selected_id is not None
                    and pane.select_message(state.selected_id))
        if not restored:
            # The message the tab was left on has been archived or deleted
            # since. Show the view rather than nothing.
            pane.clear_selection()
    finally:
        pane._restoring = False


def title_for_scope(pane) -> str:
    """A tab's name: what the rail row says, and the account when that would
    otherwise be ambiguous — four tabs called Inbox are four tabs called
    nothing."""
    if pane.showing_site():
        return _mode_prefix("Sites", pane.sites.title())
    if pane.showing_tracking():
        return _mode_prefix("Tracking", pane.tracking.title())
    if pane.showing_contacts():
        return _mode_prefix("Contacts", pane.contacts.title())
    if pane.showing_calendar():
        index = pane.rail.currentIndex()
        label = (index.data(Qt.ItemDataRole.DisplayRole)
                 if index.isValid() else "") or "Calendar"
        return _mode_prefix("Calendar", label)
    # A SAVED SEARCH IS NAMED, AND ITS NAME BEATS ITS QUERY. "Invoices" says
    # more than "Search: invoice", and the person chose the first. This is
    # above the `search.active` branch for that reason and BELOW nothing else,
    # because it is still a claim about what is in the tab — `viewhost.showing`
    # returns the view only while the pane still MATCHES it, so a tab whose
    # search has since been edited falls through to the line below and is named
    # for what it is now showing instead.
    from . import viewhost

    view = viewhost.showing(pane)
    if view is not None:
        return view.name[:40]
    search = pane.model.search
    if search.active:
        # The search, not the rail row underneath it: the tab is showing
        # results, and naming it "Inbox" would be a lie about what is in it.
        asked = search.text.strip() or search.describe()
        return _mode_prefix("Mail", f"Search: {asked}"[:40])
    index = pane.rail.currentIndex()
    if not index.isValid():
        return _mode_prefix("Mail", "Inbox")
    label = index.data(Qt.ItemDataRole.DisplayRole) or "Mail"
    scope = index.data(ScopeRole)
    if scope is not None and scope.account_id is not None:
        account = accounts_repo.get_account(pane._con, scope.account_id)
        if account is not None and account.label != label:
            return _mode_prefix("Mail", f"{account.label} · {label}")
    return _mode_prefix("Mail", label)
