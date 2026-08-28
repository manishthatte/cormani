# SPDX-License-Identifier: GPL-3.0-or-later
#
# The line under the list, and the sentence in the middle of an empty one.
#
# Two strings, and both of them are CONVENTIONS.txt §8 in one line each: a view
# showing nothing has to say what it asked for, and a set of results has to say
# what it left out. That is why this is a module and not two f-strings inside
# the pane — the sentences are the feature, and they are worth testing without
# a widget.
#
# THE TWO COUNTS ARE DIFFERENT QUESTIONS AND BOTH ARE PRINTED. When the list is
# grouped, the ROWS are conversations and the number a person compares against
# the rail is messages; a footer that gave one of them would be right half the
# time and unexplainable the other half.
#
# THE HELD-BACK NUMBER NAMES THE CHIP THAT LIFTS IT. A search excludes Trash
# and Junk — SESSION_STATE.txt records the decision and why — and a number with
# no way to act on it is only half an answer.
#
# There is no Qt here on purpose. The pane sets the label; this decides what it
# says.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from ..store import views as views_repo


def empty_text(*, search, filters, scope) -> str:
    """What an empty list says about itself."""
    if search.active:
        return f"Nothing matches {search.describe()}"
    if filters.active:
        return "No message matches these filters"
    if scope.role == views_repo.ROLE_OWED:
        return "Nothing is owed a reply"
    return "No messages"


def footer_text(*, loaded: int, total: int, search, grouping: bool,
                discarded: int = 0, conversations: int = 0) -> str:
    """The line under the list, for either kind of view."""
    if search.active:
        parts = [f"{total} found" if loaded >= total
                 else f"{loaded} of {total} found"]
        if discarded:
            parts.append(f"{discarded} more in Trash or Junk — "
                         f"press “Trash & Junk” to include them")
        return " · ".join(parts)
    if total == 0:
        return ""
    counted = (f"{total} messages" if loaded >= total
               else f"{loaded} of {total} messages")
    if grouping:
        counted += f" in {conversations} conversations"
    return counted


class Counts:
    """The two numbers a footer needs beyond what the model emits, cached.

    Both are a query over the store, and the footer is redrawn on every page
    the list fetches — cheap once, not cheap per scroll. The key is the whole
    query, so any change to it recomputes and nothing else does.
    """

    def __init__(self, con) -> None:
        self._con = con
        self._key = None
        self.discarded = 0
        self.conversations = 0

    def forget(self) -> None:
        """The store changed under the same query. Deleting a result moves it
        into the very folders one of these numbers is about."""
        self._key = None

    def of(self, model) -> tuple:
        from ..store import messages as messages_repo

        key = (model.scope, model.filters, model.search, model.grouping)
        if key != self._key:
            self._key = key
            self.discarded = messages_repo.count_discarded(
                self._con, model.scope, model.filters, model.search)
            self.conversations = (
                messages_repo.count_threads(self._con, model.scope,
                                            model.filters, search=model.search)
                if model.grouping else 0)
        return (self.discarded, self.conversations)
