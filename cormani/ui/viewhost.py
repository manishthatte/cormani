# SPDX-License-Identifier: GPL-3.0-or-later
#
# What every saved-search menu item MEANS, and what opening one does to a pane.
#
# `ui/filterhost.py`'s shape and for its reason: the window owns the menu bar,
# and what a command DOES belongs beside the thing it does it to. Functions
# taking the window or the pane rather than a class, because — unlike
# `ui/calendarhost.py` and `ui/sitehost.py` — there is nothing to hold. A
# calendar host owns a pane and a site host owns a browser; a saved search is
# applied and then forgotten about, and the view it made is the pane's.
#
# ── OPENING ONE IS NOT CHOOSING A FOLDER, AND THE DIFFERENCE IS THE POINT ──
#
# `MailPane._scope_chosen` ENDS a search, deliberately: clicking a row in the
# rail is how a person says "show me this instead", and a search left running
# over the top of it would ignore what they just clicked. A saved search is the
# one row for which that is exactly wrong — its query is what it IS — so the
# rail gives it its own signal and this file applies all five parts at once.
#
# ── THE SAVED ORDER WINS, AND `set_search` WOULD OVERRULE IT ───────────────
#
# `MailPane.set_search` sorts a ranked query best-first, because relevance is
# what somebody typing into a search box asked for. But a saved search's order
# was CHOSEN and then kept, so it must survive being opened — which is why
# this sets the model directly and then tells the pane the choice was already
# made. Routing through `set_search` would silently re-sort by relevance every
# saved search that carries text, and the user's own "oldest first" would last
# until the next redraw.
#
# ── SAVING ASKS BEFORE IT REPLACES ─────────────────────────────────────────
#
# `saved_view.name` is UNIQUE, so `store/savedviews.save_view` raises rather
# than choosing, and the choosing is here. Overwriting a saved search somebody
# forgot they had is a loss they cannot see; inventing "Invoices (2)" is a rail
# that fills up with near-duplicates. So it asks, in the one place that has a
# window to ask from.
#
# ── AND IT REFUSES TO SAVE A VIEW OF EVERYTHING ────────────────────────────
#
# A saved search that asks nothing is the Inbox row under a second name.
# `SavedView.asks_anything` is the test and the refusal is a sentence, not a
# disabled menu item nobody can account for — `ui/filterhost.py`'s rule, that a
# menu item appearing to do nothing is the failure this kind of file exists to
# prevent.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtWidgets import QInputDialog, QMessageBox

from ..store import savedviews as savedviews_repo
from .models import rail as rail_model


def _confirm(parent, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.StandardButton.Yes |
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def _ask_name(parent, title: str, label: str, initial: str) -> str:
    """The name, or "" if the user cancelled.

    INJECTED at every call site for the reason `filterhost._confirm` is: a
    modal dialog in a test is a test that hangs, and the alternative — a
    QTimer that presses a button — tests Qt rather than corMani.
    """
    text, said_yes = QInputDialog.getText(parent, title, label, text=initial)
    return text.strip() if said_yes else ""


# ── Opening one ────────────────────────────────────────────────────────────


def apply_view(pane, view_id: int) -> bool:
    """Put a saved search into the pane. The rail's `view_chosen` verb.

    Returns whether it was applied — False when the view has been deleted from
    under the rail, which is a rebuild away from fixing itself and is not worth
    a sentence.
    """
    view = savedviews_repo.get_view(pane._con, int(view_id))
    if view is None:
        return False
    # The calendar, the tracking pane and a site panel all claim the space the
    # list occupies. Whoever was asked last owns it — `ui/sitehost.py`'s note.
    pane.tracking.show(False)
    pane.calendars.show(False)
    pane.sites.show("")

    # THE ORDER IS THE SAVED ONE, and the pane is told so. Without the second
    # line the next call to `set_search` would decide this query is ranked and
    # replace the order the user kept with relevance.
    pane._sort_before_search = view.sort
    pane._sort_chosen_in_search = True

    pane.quick_filter.set_filters(view.filters)
    pane.model.set_query(scope=view.scope, filters=view.filters,
                         sort=view.sort, search=view.search,
                         threaded=view.threaded)
    pane.list.set_show_location(view.search.active)
    pane.search_changed.emit(view.search)
    pane._update_empty_text()
    # Nothing is selected on arriving, for the reason a folder opens with
    # nothing selected: selecting a message MARKS IT READ, and a virtual folder
    # opened by accident would quietly read whatever was at the top of it.
    pane.clear_selection()
    if not pane._restoring:
        pane.view_changed.emit()
    return True


def showing(pane):
    """The saved view this pane is showing, or None.

    NOT "which one was clicked" — which one it still MATCHES. A person who
    opens a saved search and then types in the search box is no longer looking
    at it, and a tab that went on carrying its name would be making a claim
    about its contents that had stopped being true. `ui/panestate.py`'s rule.
    """
    view_id = rail_model.view_id_from_key(pane.rail.current_key())
    if view_id is None:
        return None
    view = savedviews_repo.get_view(pane._con, view_id)
    if view is None:
        return None
    return view if view.describes(pane.model.scope, pane.model.filters,
                                  pane.model.search, pane.model.sort,
                                  pane.model.threaded) else None


# ── Saving one ─────────────────────────────────────────────────────────────


def current_view(pane, name: str = "") -> savedviews_repo.SavedView:
    """What the pane is showing, as a saved view that has not been written."""
    return savedviews_repo.SavedView(
        name=name, scope=pane.model.scope, filters=pane.model.filters,
        search=pane.model.search, sort=pane.model.sort,
        threaded=pane.model.threaded)


def save_current(window, *, ask_name=_ask_name, confirm=_confirm):
    """`Save this search…`. Returns the SavedView written, or None.

    None has four causes and each says a different sentence: the pane is not
    showing mail at all, the view asks nothing, the user cancelled, or they
    declined to replace one of the same name. No silent no-op.
    """
    pane = window.mail
    # `showing_mail` and not three named hosts. This line asked for the
    # calendar, tracking and the sites, and the address book made it wrong
    # without changing a character of it — see `MailPane.showing_mail`.
    if not pane.showing_mail():
        window.status_message.setText(
            "Saved searches are about mail — open a mail view first.")
        return None
    proposed = current_view(pane)
    if not proposed.asks_anything:
        window.status_message.setText(
            "There is nothing to save yet: this is every inbox, unfiltered. "
            "Search for something, or narrow the list, and try again.")
        return None

    name = ask_name(window, "Save this search",
                    "A name for it, as it will appear in the rail:",
                    _suggest(pane))
    if not name:
        return None
    existing = savedviews_repo.by_name(window._store, name)
    if existing is not None:
        if not confirm(window, "Replace it?",
                       f"There is already a saved search called “{name}”.\n\n"
                       f"It asks for {existing.describe()}.\n\nReplace it with "
                       f"what is on screen?"):
            return None
        proposed = proposed.with_changes(id=existing.id,
                                         sort_order=existing.sort_order,
                                         in_rail=existing.in_rail)
    saved = savedviews_repo.save_view(window._store,
                                      proposed.with_changes(name=name))
    window.mail.rail.refresh_counts()
    if saved.in_rail:
        window.mail.rail.select_key(rail_model.saved_key(saved.id))
    window.status_message.setText(
        f"Saved “{saved.name}” — it is in the rail under Saved searches."
        if saved.in_rail else
        f"Saved “{saved.name}” — it is in the Saved searches menu.")
    return saved


def _suggest(pane) -> str:
    """A name to start from. What was typed, when anything was.

    A search box with `invoice` in it is a saved search called Invoice far more
    often than it is anything else, and a dialog opening on an empty field asks
    a question the screen has already answered.
    """
    search = pane.model.search
    for candidate in (search.text, search.sender, search.subject,
                      pane.model.filters.text):
        said = (candidate or "").strip()
        if said:
            return said[:60]
    return ""


# ── Managing them ──────────────────────────────────────────────────────────


def open_named(window, view_id: int) -> bool:
    """Open a saved search from the menu, including one kept OUT of the rail.

    Through the rail when it is drawn there, so that the selection follows —
    `MailPane.show_site` argues it and `ui/panestate.py` is the reason: a tab
    records the RAIL's key, so a view opened behind the rail's back comes back
    as the inbox next time the tab is restored. A view not IN the rail has no
    key to select and is applied directly; its tab restores to whatever row was
    highlighted, which is the honest consequence of keeping it out.
    """
    view = savedviews_repo.get_view(window._store, int(view_id))
    if view is None:
        window.status_message.setText("That saved search has been deleted.")
        return False
    if view.in_rail and window.mail.rail.select_key(
            rail_model.saved_key(view.id)):
        return True
    return apply_view(window.mail, view.id)


def show_manager(window, *, dialog=None) -> None:
    """`Manage saved searches…` — rename, reorder, hide from the rail, delete."""
    from . import savedviewsdialog

    (dialog or savedviewsdialog.SavedViewsDialog)(window._store, window).exec()
    window.mail.rail.refresh_counts()


def delete_view(window, view_id: int, *, confirm=_confirm) -> bool:
    """Delete one, having asked. Returns whether it went.

    A saved search is not undoable — `store/undo.py` takes back one action on
    one message — and it can be the only record of a query somebody worked out
    once and has relied on since. So it asks, and the question says what the
    view was FOR rather than only its name.
    """
    view = savedviews_repo.get_view(window._store, int(view_id))
    if view is None:
        return False
    if not confirm(window, "Delete this saved search?",
                   f"“{view.name}” asks for {view.describe()}.\n\nDeleting it "
                   f"removes the search, never the mail. It cannot be undone."):
        return False
    savedviews_repo.delete_view(window._store, view.id)
    window.mail.rail.refresh_counts()
    window.status_message.setText(f"Deleted “{view.name}”.")
    return True


def build_menu(window, menu) -> None:
    """The Saved searches submenu, rebuilt each time it opens.

    REBUILT, for the reason the Tags menu is: a view can be saved, renamed or
    deleted while the window is open, and a menu built once is a menu that is
    wrong from then on.
    """
    from PySide6.QtGui import QAction

    menu.clear()
    views = savedviews_repo.list_views(window._store)
    if not views:
        empty = QAction("none saved yet", window)
        empty.setEnabled(False)
        menu.addAction(empty)
        return
    for view in views:
        action = QAction(view.name, window)
        wrong = savedviews_repo.unresolved(window._store, view)
        action.setStatusTip(f"{view.name} — {wrong}" if wrong else view.describe())
        action.triggered.connect(
            lambda _=False, i=view.id: open_named(window, i))
        menu.addAction(action)
