# SPDX-License-Identifier: GPL-3.0-or-later
#
# What every filter menu item MEANS.
#
# The seam `ui/trackhost.py` and `ui/calendarhost.py` already are: the window
# owns the menu bar and the tab strip, and what a command DOES belongs beside
# the thing it does it to. `ui/window.py` stood at 566 of the 600 lines the
# packaging test allows when this arrived, so it was that or a fourteenth seam
# found by length instead of by subject.
#
# Three commands, and only the third does anything to mail.
#
# ── RUNNING THE RULES AGAIN IS A BULK CHANGE AND IS ASKED FOR ──────────────
#
# Filters run on arrival by themselves. This is the OTHER way they are used —
# after writing a rule, after fixing one, after an import — and it is a
# different kind of act: two thousand messages can move at once, every move is
# queued for the server, and NONE OF IT IS UNDOABLE. `store/undo.py` takes back
# one action a person took; a filter run is hundreds, taken by a rule.
#
# So it asks first, and the question says the three things that decide the
# answer: how many rules will run, how many messages they will see, and that it
# cannot be taken back. CONVENTIONS.txt §8 — the alternative is a person
# pressing a menu item to see what it does and losing the shape of their Inbox.
#
# ── IT RUNS OVER A FOLDER, AND SAYS SO WHEN THE VIEW IS NOT ONE ────────────
#
# A rule is about mail ARRIVING, so the honest target is a folder — one folder,
# or the several that one unified role stands for. A search result is not a
# folder and neither is "this account": running rules over an arbitrary set of
# messages the user happened to be looking at is how somebody's Sent mail ends
# up archived by a rule written for their Inbox. Where the view is not a
# folder, the answer is a sentence and not a guess.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from ..store import folders as folders_repo
from ..store import messages as messages_repo
from ..store import rulerun
from ..store import rules as rules_repo
from ..store import views as views_repo
from . import filtersdialog, ruleeditor

# How many messages one run looks at, per folder. `rulerun.run_over_folder`'s
# own default; named here because the confirmation quotes it, and a number in a
# question must be the number that will actually be used.
RUN_LIMIT = 2000


def _confirm(parent, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.StandardButton.Yes |
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def show_filters(window) -> None:
    """The rules, in the order they run."""
    filtersdialog.FiltersDialog(window._store, window).exec()


def filter_from_message(window, *, editor=None) -> bool:
    """Start a rule from the message being read.

    THE MESSAGE IS THE ONE IN THE READING PANE, not the selection, and the two
    differ when several rows are selected. A filter is written about ONE piece
    of mail — "more like this" — so the one on screen is the one meant; a
    selection of forty would leave the interface choosing which of them the
    rule is about, which is a choice it cannot make.
    """
    message_id = window.mail.reader.message_id()
    if not message_id:
        # A menu item that did nothing would look broken. `status_message` is
        # the window's own label; the mail pane has a SIGNAL of the same name,
        # and this side of the wire is the label.
        window.status_message.setText(
            "Open a message first — a filter is written about one.")
        return False
    row = messages_repo.get_row(window._store, message_id)
    if row is None:
        window.status_message.setText("That message is no longer in the store.")
        return False
    started = ruleeditor.from_message(row)
    dialog = (editor or ruleeditor.RuleEditor)(window._store, started, window)
    dialog.exec()
    if dialog.saved is None:
        return False
    window.status_message.setText(
        f"“{dialog.saved.name}” will run on the next sync. Tools ▸ Run "
        f"filters on this folder applies it to mail already here.")
    return True


def folders_in_view(window) -> list[int]:
    """Which folders the current view stands for, or nothing when it is not one.

    A UNIFIED VIEW STANDS FOR SEVERAL FOLDERS and that is the case worth
    supporting: the Inbox across fifteen accounts is exactly where somebody is
    standing when they decide their filters need running again. It resolves
    through `ids_by_role` and `visible_account_ids`, which is the same
    resolution `store/views.scope_where` uses, so what runs is what is on
    screen and not a second opinion about it.

    OWED IS REFUSED, and it is the one case where the honest answer is not the
    obvious one. Owed is a view OVER the Inbox — unanswered mail that is not
    the user's own — so resolving its role would silently run the rules over
    every message in every Inbox, which is not what the rail said was being
    looked at. `store/views.py` makes the same substitution deliberately for a
    WHERE clause and it is right there; here it would be a bulk change to mail
    nobody pointed at.
    """
    scope = window.mail.model.scope
    if window.mail.model.search.active:
        return []
    if scope.kind == "folder":
        return [int(scope.folder_id)] if scope.folder_id is not None else []
    if scope.role == views_repo.ROLE_OWED:
        return []
    accounts = ([scope.account_id] if scope.kind == "account"
                else views_repo.visible_account_ids(window._store))
    if scope.kind == "account" and scope.account_id is None:
        return []
    return list(folders_repo.ids_by_role(window._store, scope.role,
                                         account_ids=accounts))


def run_over_view(window, *, confirm=_confirm) -> object:
    """Run every enabled rule over the folder being looked at.

    Returns the `RunReport`, or None when nothing was run — which is the
    answer for a view that is not a folder, for a store with no rules, and for
    a question the user said no to. Three different reasons, three different
    sentences, and no silent no-op: a menu item that appears to do nothing is
    the failure this whole file is written against.
    """
    con = window._store
    active = [r for r in rules_repo.list_rules(con, enabled_only=True)
              if r.is_complete]
    if not active:
        window.status_message.setText(
            "There are no filter rules to run — Tools ▸ Message filters is "
            "where they are written.")
        return None
    folder_ids = folders_in_view(window)
    if not folder_ids:
        window.status_message.setText(
            "Choose a folder in the rail first — filters run over a folder, "
            "not over a search or an unanswered-mail view.")
        return None

    held = sum(_held(con, folder_id) for folder_id in folder_ids)
    where = (folders_repo.get_folder(con, folder_ids[0]).label
             if len(folder_ids) == 1 else f"{len(folder_ids)} folders")
    if not confirm(window, "Run the filters here?",
                   f"{len(active)} rule{'' if len(active) == 1 else 's'} will "
                   f"run over {held} message{'' if held == 1 else 's'} in "
                   f"{where}.\n\nMail may be moved, tagged or marked, and every "
                   f"change is sent to the server on the next sync. This cannot "
                   f"be undone."):
        return None

    total = rulerun.RunReport()
    for folder_id in folder_ids:
        report = rulerun.run_over_folder(con, folder_id, limit=RUN_LIMIT)
        total.considered += report.considered
        total.matched += report.matched
        total.outcomes.update(report.outcomes)
        total.problems.extend(report.problems)
    window.mail.reload()
    window.status_message.setText(_said(total))
    return total


def _held(con, folder_id: int) -> int:
    """How many messages are in one folder, asked through the store.

    `messages.count` and not a SELECT written here: `store/` owns the SQL in
    this tree, and this particular count has to agree with the number the list
    is showing — which it does by being the same query the list makes.
    """
    return messages_repo.count(
        con, views_repo.Scope(kind="folder", folder_id=int(folder_id)))


def _said(report) -> str:
    """What a run did, in one line, INCLUDING what it could not do.

    A run finishes rather than stopping on the first thing it cannot carry
    out — a missing Junk folder, a tag somebody deleted — so the count of
    problems has to reach the person who asked, or the rules look as though
    they all worked. `store/rulerun.py`'s header argues the same point from the
    other end.
    """
    said = report.describe()
    if report.problems:
        said += f" — {report.problems[0]}"
        if len(report.problems) > 1:
            said += f" (and {len(report.problems) - 1} more)"
    return said
