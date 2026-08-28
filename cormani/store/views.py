# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a view IS, and the WHERE clause it becomes.
#
# A view is four things and each says its own part in its own module: the SCOPE,
# the FILTERS and the ORDER are here, a SEARCH is store/search.py, and a
# conversation is store/threads.py. store/messages.py composes them and returns
# rows. That division is why none of the four had to be threaded through the
# other three when search and threading arrived — and why a tab can be saved as
# the four objects rather than as a query somebody would have to parse back.
#
# EVERY FRAGMENT RETURNS (sql, params) AND NEVER AN INTERPOLATED VALUE. The one
# exception is a list of integer ids the caller already holds, and even those go
# in as placeholders. A store that holds a decade of correspondence and speaks
# to fifteen servers has no business building SQL out of strings that came from
# a header — CONVENTIONS.txt §7.
#
# "0" IS THE EMPTY ANSWER. A scope that selects nothing at all returns the
# literal false rather than an empty IN (), which is a syntax error, and rather
# than raising, which would make an account with no folders a crash instead of
# an empty list.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from . import folders as folders_repo
from . import search as search_mod
from .accounts import list_accounts, list_identity_addresses

# The pseudo-role. Not a folder anywhere, and not written to the folder table.
ROLE_OWED = "owed"

SORT_KEYS = ("date", "sender", "subject")

# The fourth order, and it is not in SORT_KEYS because it is not always
# available: bm25 needs something matched against the index, so relevance means
# nothing outside a search and falls back to date rather than failing.
SORT_RELEVANCE = "relevance"

@dataclass(frozen=True)
class Scope:
    """Which messages this view is about.

    kind is one of:
      unified  — every visible account's folder with this role, or Owed
      account  — one account's folder with this role
      folder   — exactly this folder
    """

    kind: str = "unified"
    role: str = folders_repo.ROLE_INBOX
    account_id: int | None = None
    folder_id: int | None = None

    @property
    def is_outgoing(self) -> bool:
        """Sent and Drafts show who the message is TO. A Sent folder listing
        the sender is fifteen hundred rows with the user's own name on them."""
        return self.role in (folders_repo.ROLE_SENT, folders_repo.ROLE_DRAFTS)


@dataclass(frozen=True)
class Filters:
    """The Quick Filter bar, as data. Every field is 'off' by default, and off
    means 'do not constrain' rather than 'require false' — a toggle that filtered
    to read messages when unpressed would be a filter nobody could turn off."""

    unread: bool = False
    flagged: bool = False
    attachment: bool = False
    contact: bool = False           # from an address in the address book
    tagged: bool = False            # carries any tag
    tag_id: int | None = None       # carries this tag
    text: str = ""                  # substring of sender, subject or preview

    @property
    def active(self) -> bool:
        return bool(self.unread or self.flagged or self.attachment or
                    self.contact or self.tagged or self.tag_id or self.text.strip())

    def cleared(self) -> "Filters":
        return Filters()


@dataclass(frozen=True)
class Sort:
    key: str = "date"
    descending: bool = True

    def toggled(self, key: str) -> "Sort":
        """Clicking the current key reverses it; a new key starts descending for
        dates and ascending for text, which is what each one is useful as."""
        if key == self.key:
            return replace(self, descending=not self.descending)
        return Sort(key=key, descending=(key == "date"))


_SORT_SQL = {
    "date": "m.date_at",
    "sender": "LOWER(COALESCE(NULLIF(m.from_name, \'\'), m.from_addr))",
    "subject": "LOWER(COALESCE(NULLIF(m.subject_base, \'\'), m.subject))",
}


def visible_account_ids(con: sqlite3.Connection) -> list[int]:
    """The accounts a unified view covers: not hidden, not disabled.

    Hidden means "not in the rail", and a unified inbox that still counted a
    hidden account would make hiding useless — the count the user was trying to
    get away from would follow them to the top of the tree.
    """
    return [a.id for a in list_accounts(con, include_hidden=False,
                                        include_disabled=False)]


def scope_where(con: sqlite3.Connection, scope: Scope) -> tuple[str, list]:
    """The scope, as a WHERE fragment. '0' when it selects nothing at all —
    an empty IN () is a syntax error and an empty result is the right answer."""
    if scope.kind == "folder":
        if scope.folder_id is None:
            return "0", []
        return "m.folder_id = ?", [scope.folder_id]

    if scope.kind == "account":
        if scope.account_id is None:
            return "0", []
        account_ids = [scope.account_id]
    else:
        account_ids = visible_account_ids(con)

    if not account_ids:
        return "0", []

    role = folders_repo.ROLE_INBOX if scope.role == ROLE_OWED else scope.role
    folder_ids = folders_repo.ids_by_role(con, role, account_ids=account_ids)
    if not folder_ids:
        return "0", []
    marks = ",".join("?" * len(folder_ids))
    sql = f"m.folder_id IN ({marks})"
    params: list = list(folder_ids)

    if scope.role == ROLE_OWED:
        identities = sorted(list_identity_addresses(con))
        sql += " AND m.answered = 0 AND m.draft = 0"
        if identities:
            imarks = ",".join("?" * len(identities))
            sql += f" AND LOWER(m.from_addr) NOT IN ({imarks})"
            params.extend(identities)
        sql += _ANSWERED_ELSEWHERE
    return sql, params


# STAGE 6 MADE OWED THE REAL QUESTION, and the note at the top of
# `store/messages.py` predicted exactly this: the simple version answers
# "inbound, unanswered, not from me", which is a fact about the MAILBOX. What a
# person means by owed is a fact about the CORRESPONDENCE — and a matter
# settled on the telephone is settled, however the \Answered flag reads.
#
# So a message drops out of Owed when its tracked thread has heard something
# go OUT since it arrived. `out` and not `note`: a note written to oneself and
# a meeting attended are both direction `note` precisely so that neither can
# mark a correspondent answered.
#
# A MESSAGE ON NO THREAD IS STILL OWED BY THE SIMPLE RULE, which is what keeps
# the view useful before any thread exists — the clause subtracts, and has
# nothing to subtract until somebody is tracking something.
_ANSWERED_ELSEWHERE = """
    AND NOT EXISTS (
        SELECT 1 FROM touch mine
        JOIN touch later ON later.thread_id = mine.thread_id
        WHERE mine.message_id = m.id
          AND later.direction = 'out'
          AND later.occurred_at > mine.occurred_at)
"""


def searching(search) -> bool:
    """Whether a search is what decides this view. A Query that asks nothing is
    not a search, and must not turn the inbox into every folder of every
    account — see `search.Query.active`."""
    return search is not None and search.active


def search_scope_where(con: sqlite3.Connection) -> tuple[str, list]:
    """Every folder of every visible account. What the box at the top means.

    The rail's selection is deliberately NOT consulted. Narrowing what is in
    front of you is the Quick Filter's job; this box is the other act, and a
    search that silently stayed inside the folder the user happened to be
    looking at would answer a question nobody asked. Which accounts count is
    still the rail's rule — a hidden account is hidden from search too, or
    hiding one would not do what it says.

    The Account chip narrows further, and does it in `search.where_sql`, so
    that the chips are all in one place.
    """
    account_ids = visible_account_ids(con)
    if not account_ids:
        return "0", []
    marks = ",".join("?" * len(account_ids))
    return f"f.account_id IN ({marks})", list(account_ids)


def filter_where(filters: Filters) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if filters.unread:
        clauses.append("m.seen = 0")
    if filters.flagged:
        clauses.append("m.flagged = 1")
    if filters.attachment:
        clauses.append("m.has_attachment = 1")
    if filters.contact:
        # A known correspondent: any handle in the address book matching the
        # sender. Case-folded on both sides because addresses are compared, and
        # the local part is case-sensitive only in a specification nobody honours.
        clauses.append(
            "EXISTS (SELECT 1 FROM handle h WHERE h.kind = 'email' "
            "AND LOWER(h.value) = LOWER(m.from_addr))")
    if filters.tag_id is not None:
        clauses.append("EXISTS (SELECT 1 FROM message_tag mt "
                       "WHERE mt.message_id = m.id AND mt.tag_id = ?)")
        params.append(filters.tag_id)
    elif filters.tagged:
        clauses.append("EXISTS (SELECT 1 FROM message_tag mt WHERE mt.message_id = m.id)")
    text = filters.text.strip()
    if text:
        # ESCAPE, because a search for "50%" or "a_b" is a search for those
        # characters and not two wildcards.
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append(
            "(m.subject LIKE ? ESCAPE '\\' OR m.from_name LIKE ? ESCAPE '\\' "
            "OR m.from_addr LIKE ? ESCAPE '\\' OR m.preview LIKE ? ESCAPE '\\' "
            "OR m.to_addrs LIKE ? ESCAPE '\\')")
        params.extend([pattern] * 5)
    return (" AND ".join(clauses), params)


def order_by(sort: Sort, *, ranked: bool = False) -> str:
    if sort.key == SORT_RELEVANCE and not ranked:
        # Nothing was matched against the index — chips alone, or no search at
        # all — so there is no score to order by. Date rather than an error:
        # the view still has to draw, and the most recent is the best answer an
        # unranked query has.
        return order_by(Sort())
    if sort.key == SORT_RELEVANCE:
        # bm25 returns a NEGATIVE score and the better match is the more
        # negative one, so best-first is ASC. `descending` still means "the
        # useful end first", which is what it means on a date.
        column = "hit.score"
        direction = "ASC" if sort.descending else "DESC"
    else:
        column = _SORT_SQL.get(sort.key, _SORT_SQL["date"])
        direction = "DESC" if sort.descending else "ASC"
    # A stable tiebreak on every sort. Without it two messages with the same
    # subject swap places between fetches and the list appears to shuffle while
    # the user scrolls.
    return f"ORDER BY {column} {direction}, m.date_at DESC, m.id DESC"


def clause(con: sqlite3.Connection, scope: Scope, filters: Filters,
           search=None) -> tuple[str, list]:
    if searching(search):
        # The search replaces the scope and brings its own exclusions with it:
        # which folders it will not look in, and the mail marked deleted.
        sql, params = search_scope_where(con)
        extra, extra_params = search_mod.where_sql(search)
        if extra:
            sql += f" AND {extra}"
            params = params + extra_params
    else:
        sql, params = scope_where(con, scope)
        # Deleted messages are hidden everywhere except the folder they were
        # moved to, which is the only place they are not a surprise.
        if scope.role != folders_repo.ROLE_TRASH and scope.kind != "folder":
            sql += " AND m.deleted = 0"
    filter_sql, filter_params = filter_where(filters)
    if filter_sql:
        sql += f" AND {filter_sql}"
        params = params + filter_params
    return sql, params
