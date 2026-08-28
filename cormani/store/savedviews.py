# SPDX-License-Identifier: GPL-3.0-or-later
#
# A search somebody named and kept, and what it costs to open it again.
#
# `saved_view` — migration 9 — and the questions asked of it. The table was
# written a stage before this file was, and `store/rulesschema.py` names this
# module in its header as the reader that would arrive: the schema half argues
# for JSON, and this half is what spends it.
#
# ── A SAVED VIEW IS A VIEW, NOT A QUERY ────────────────────────────────────
#
# `store/views.py` §1 says a view is four things — a SCOPE, a FILTERS, a search
# QUERY and a SORT — each in its own module, and that a tab can be saved as the
# four rather than as a string somebody would have to parse back. That is what
# this holds, and it is why "the Inbox, unread, from Lyle, oldest first" is one
# saved view rather than a query language nobody has written.
#
# It holds a fifth field, `threaded`, and the schema's header says four. The
# reason is in `views.py`'s own sentence: it lists the parts as the scope, the
# filters, the order, the search AND `store/threads.py` — five modules, and the
# fifth was left out of the schema note by an oversight rather than a decision.
# A saved search that reopens flat when it was saved threaded is not the view
# that was saved. `store/rulesschema.py` now carries a dated notice saying so.
#
# ── THE READER NEVER TRUSTS THE JSON ───────────────────────────────────────
#
# Every field is read with `.get` and a default, and then COERCED — `int()`,
# `bool()`, `str()` — before it reaches a frozen dataclass. Two different
# reasons, and the second is the one that bites:
#
#   A definition written before a field existed must still open. That is the
#   tolerance the schema header promised, and `.get` alone gives it.
#
#   A definition written by a NEWER corMani may hold a field of a shape this
#   one does not expect — a scope kind that means nothing here, an account id
#   that is a string. `store/database.py` refuses a schema NEWER than it knows
#   (`SchemaTooNew`) precisely so this cannot happen through a migration; a
#   blob is the one thing that route does not cover, because the version is
#   inside the value rather than in the schema. So the coercion is the guard,
#   and an unreadable definition becomes the DEFAULT view rather than an
#   exception in the rail's build loop — a rail that will not draw because one
#   saved search is malformed is a client that will not start.
#
# ── WHETHER IT CAN STILL MEAN WHAT IT SAYS ─────────────────────────────────
#
# `store/rules.py` has `validate_here` for the mistake only the store can see;
# this has `unresolved`, and the mistake is not a mistake — it is TIME. A view
# scoped to one folder is a view whose folder can be deleted, an account can be
# removed, a tag can be dropped. `views.scope_where` answers all three the same
# way, with the literal `0`, which is the right WHERE clause and a terrible
# explanation: the rail would draw a virtual folder that is permanently empty
# and say nothing about why.
#
# So the reader can say. Nothing DELETES such a view — a folder can come back
# when an account is re-added, and a saved search silently disappearing is
# worse than one that says what is wrong with it.
#
# ── THE NAME IS UNIQUE AND SAVING OVER ONE IS THE CALLER'S QUESTION ────────
#
# The table declares `name TEXT NOT NULL UNIQUE`, so `save_view` raises rather
# than choosing. Overwriting a saved search the user forgot they had is a loss
# they cannot see, and silently making "Invoices (2)" is a rail that fills with
# near-duplicates. `ui/viewhost.py` asks.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace

from . import folders as folders_repo
from . import search as search_mod
from . import views as views_repo
from .database import utc_now

# The definition's own version, inside the JSON rather than in a column — see
# `store/rulesschema.py`. Bump it only when a field changes MEANING; adding one
# needs no bump, because the reader defaults what it does not find.
DEFINITION_VERSION = 1

# What `views.Scope.kind` may be. A definition naming anything else is a
# definition this corMani cannot honour, and the unified inbox is the safe
# reading of "I do not know what you meant".
_SCOPE_KINDS = ("unified", "account", "folder")

# Where the rail stops counting. NOT a tuning number: it is exactly the point
# `ui/rail.CountDelegate` starts drawing "999+" instead of a figure, so a
# capped count and an exact one put identical text on the screen. See
# `count_capped` for what it is worth and why the guess it replaced was
# backwards.
RAIL_COUNT_CAP = 1000


@dataclass(frozen=True)
class SavedView:
    """A named view: the four objects, and how it is drawn."""

    name: str
    scope: views_repo.Scope = field(default_factory=views_repo.Scope)
    filters: views_repo.Filters = field(default_factory=views_repo.Filters)
    search: search_mod.Query = field(default_factory=search_mod.Query)
    sort: views_repo.Sort = field(default_factory=views_repo.Sort)
    threaded: bool = True
    in_rail: bool = True
    sort_order: int = 0
    id: int | None = None

    @property
    def asks_anything(self) -> bool:
        """Whether this narrows the mailbox at all.

        A view with no search, no filters and the plain unified inbox as its
        scope is the Inbox row the rail already has, under a second name. Not
        refused here — the person named it, so they meant something by it — but
        the interface has to be able to tell, or "Save this search" would offer
        to save a view of everything.
        """
        return bool(self.search.active or self.filters.active
                    or self.scope.kind != "unified"
                    or self.scope.role != folders_repo.ROLE_INBOX)

    def describes(self, scope, filters, search, sort, threaded: bool) -> bool:
        """Whether a live pane is showing exactly this view.

        Asked by the interface for two things it would otherwise have to guess:
        whether a tab may be NAMED for this saved search, and whether "Save
        this search" means a new view or an update to this one. Comparing the
        four objects is the whole test, because they are frozen dataclasses and
        equality on them is the equality that matters.
        """
        return (self.scope == scope and self.filters == filters
                and self.search == search and self.sort == sort
                and self.threaded == bool(threaded))

    def with_changes(self, **changes) -> "SavedView":
        return replace(self, **changes)

    def describe(self) -> str:
        """What this view asks, in words: WHERE, then WHICH.

        For a caller with no connection — a menu's status tip, a confirmation.
        A caller that has already NAMED the scope wants `narrowing` instead;
        printing both gives "every inbox · every inbox, unread", which is what
        the first version of the read-out did.
        """
        said = _describe_scope(self.scope)
        rest = self.narrowing()
        return f"{said}, {rest}" if rest else said

    def narrowing(self) -> str:
        """What this view asks BEYOND its scope, or "" when it asks nothing.

        The half that pairs with a scope somebody else has already named —
        `describe_scope_here`, or the rail row's own label.
        """
        parts = []
        if self.search.active:
            parts.append(self.search.describe())
        parts.extend(_describe_filters(self.filters))
        said = ", ".join(p for p in parts if p)
        order = ""
        if self.sort.key == "date" and not self.sort.descending:
            order = "oldest first"
        elif self.sort.key != "date":
            order = f"by {self.sort.key}"
            if not self.sort.descending:
                order += ", reversed"
        if order:
            said = f"{said} — {order}" if said else order
        return said


# ── The definition, as JSON ────────────────────────────────────────────────


def to_definition(view: SavedView) -> str:
    """The four objects and `threaded`, as the text the column holds."""
    return json.dumps({
        "version": DEFINITION_VERSION,
        "scope": {"kind": view.scope.kind, "role": view.scope.role,
                  "account_id": view.scope.account_id,
                  "folder_id": view.scope.folder_id},
        "filters": {"unread": view.filters.unread,
                    "flagged": view.filters.flagged,
                    "attachment": view.filters.attachment,
                    "contact": view.filters.contact,
                    "tagged": view.filters.tagged,
                    "tag_id": view.filters.tag_id,
                    "text": view.filters.text},
        "search": {"text": view.search.text, "sender": view.search.sender,
                   "subject": view.search.subject,
                   "attachment": view.search.attachment,
                   "within": view.search.within,
                   "account_id": view.search.account_id,
                   "discarded": view.search.discarded},
        "sort": {"key": view.sort.key, "descending": view.sort.descending},
        "threaded": view.threaded,
    }, sort_keys=True)


def from_definition(text: str) -> tuple:
    """(scope, filters, search, sort, threaded) from stored text.

    Never raises. A definition that is not JSON at all, or is JSON that is not
    an object, reads as the default view — see the module header for why an
    exception here would be worse than a wrong-looking rail row.
    """
    try:
        raw = json.loads(text or "{}")
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return (_scope(raw.get("scope")), _filters(raw.get("filters")),
            _search(raw.get("search")), _sort(raw.get("sort")),
            _bool(raw.get("threaded"), True))


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def _bool(value, default: bool = False) -> bool:
    return bool(default if value is None else value)


def _id(value) -> int | None:
    """An optional row id. Anything that is not an integer is `None`, which is
    the meaning "every account" everywhere this appears — and is a great deal
    better than a string reaching a parameter slot."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _scope(raw) -> views_repo.Scope:
    raw = _obj(raw)
    kind = _text(raw.get("kind")) or "unified"
    if kind not in _SCOPE_KINDS:
        kind = "unified"
    return views_repo.Scope(
        kind=kind, role=_text(raw.get("role")) or folders_repo.ROLE_INBOX,
        account_id=_id(raw.get("account_id")),
        folder_id=_id(raw.get("folder_id")))


def _filters(raw) -> views_repo.Filters:
    raw = _obj(raw)
    return views_repo.Filters(
        unread=_bool(raw.get("unread")), flagged=_bool(raw.get("flagged")),
        attachment=_bool(raw.get("attachment")),
        contact=_bool(raw.get("contact")), tagged=_bool(raw.get("tagged")),
        tag_id=_id(raw.get("tag_id")), text=_text(raw.get("text")))


def _search(raw) -> search_mod.Query:
    raw = _obj(raw)
    within = _text(raw.get("within"))
    if within and within not in dict(search_mod.WITHIN):
        # A date range this corMani does not have. Any time, rather than a key
        # that reaches `search.where_sql` and quietly matches nothing.
        within = ""
    return search_mod.Query(
        text=_text(raw.get("text")), sender=_text(raw.get("sender")),
        subject=_text(raw.get("subject")),
        attachment=_bool(raw.get("attachment")), within=within,
        account_id=_id(raw.get("account_id")),
        discarded=_bool(raw.get("discarded")))


def _sort(raw) -> views_repo.Sort:
    raw = _obj(raw)
    key = _text(raw.get("key")) or "date"
    if key not in views_repo.SORT_KEYS and key != views_repo.SORT_RELEVANCE:
        key = "date"
    return views_repo.Sort(key=key, descending=_bool(raw.get("descending"), True))


# ── Reading ────────────────────────────────────────────────────────────────


def list_views(con: sqlite3.Connection, *,
               rail_only: bool = False) -> list[SavedView]:
    """Every saved view, in the order the user put them in.

    One query. The definition is parsed per row here rather than lazily,
    because every caller wants the objects — the rail counts them, the read-out
    describes them, and the interface opens them.
    """
    where = "WHERE in_rail = 1" if rail_only else ""
    rows = con.execute(
        f"SELECT * FROM saved_view {where} ORDER BY sort_order, id").fetchall()
    return [_view(row) for row in rows]


def _view(row) -> SavedView:
    scope, filters, search, sort, threaded = from_definition(row["definition"])
    return SavedView(
        id=int(row["id"]), name=row["name"], scope=scope, filters=filters,
        search=search, sort=sort, threaded=threaded,
        in_rail=bool(row["in_rail"]), sort_order=int(row["sort_order"]))


def get_view(con: sqlite3.Connection, view_id: int) -> SavedView | None:
    row = con.execute("SELECT * FROM saved_view WHERE id = ?",
                      (int(view_id),)).fetchone()
    return _view(row) if row is not None else None


def by_name(con: sqlite3.Connection, name: str) -> SavedView | None:
    """The view with this name, or None. `name` is UNIQUE, so this is the
    question `save_view` cannot answer for the caller — see the header."""
    row = con.execute("SELECT * FROM saved_view WHERE name = ?",
                      (name.strip(),)).fetchone()
    return _view(row) if row is not None else None


def count_in(con: sqlite3.Connection, view: SavedView) -> int:
    """How many messages this view holds, right now. EXACT, and not free.

    Through `store/messages.count`, which is the same query the list makes —
    so the number is the number of rows the view opens with, and not a second
    opinion about it. For the read-out, which runs once from a terminal. The
    rail wants `count_capped`; see it for the measurement.
    """
    from . import messages as messages_repo

    return messages_repo.count(con, view.scope, view.filters,
                               search=view.search)


def count_capped(con: sqlite3.Connection, view: SavedView,
                 *, cap: int = RAIL_COUNT_CAP) -> int:
    """The same count, abandoned once `cap` rows have been seen.

    ── WHY THIS EXISTS, MEASURED ──────────────────────────────────────────
    The rail rebuilds whole — on every drop, every sync and every account
    change — so anything it counts is counted again each time. Over a store
    scaled to 101,376 messages, one exact count per saved view costs:

        no search, no filters      107 ms      (75,776 rows)
        a quick-filter text        124 ms         (512 rows)
        unread only                 31 ms      (20,480 rows)
        a full-text search         1.5 ms       (1,024 rows)

    which is the OPPOSITE of the obvious guess. The full-text ones are the
    CHEAPEST: FTS5 narrows to a thousand rows and the join runs against those.
    The expensive saved view is the one that asks the LEAST, because counting
    three-quarters of a mailbox means visiting three-quarters of a mailbox.

    Capped at a thousand the same four are 1.37, 123, 1.36 and 1.50 ms. And
    the cap costs NOTHING VISIBLE, which is the part that decided the number
    rather than a benchmark: `ui/rail.CountDelegate` already draws `999+` for
    anything past 999, so a query that stops at 1,000 and a query that counts
    75,776 put the identical text on the screen. The display had a cap and the
    query did not.

    THE ONE CASE THE CAP DOES NOT HELP is in the table above and is left
    honestly: a quick-filter TEXT view matching 512 rows never reaches the
    limit, so it scans the mailbox anyway — five unindexed LIKE '%…%' clauses,
    `store/views.filter_where`. That is not a cost this introduces. It is what
    the Quick Filter bar already spends on every keystroke, against the same
    rows; a saved view carrying one spends it once per rebuild instead.

    For scale, the three queries the rail ALREADY runs cost 247 ms on that
    store. Saved views are not what would make it slow.
    """
    from . import messages as messages_repo

    return messages_repo.count_capped(con, view.scope, view.filters,
                                      search=view.search, cap=cap)


def unresolved(con: sqlite3.Connection, view: SavedView) -> str:
    """What this view names that is no longer there, or "".

    See the module header. Three things can go: the folder, the account and the
    tag. Each becomes `0` or a never-matching clause in `store/views.py`, which
    is correct and silent, so this is where the silence is broken.
    """
    scope = view.scope
    if scope.kind == "folder":
        if scope.folder_id is None:
            return "it names no folder"
        if folders_repo.get_folder(con, int(scope.folder_id)) is None:
            return "the folder it was saved in has gone"
    account_id = (scope.account_id if scope.kind == "account"
                  else view.search.account_id)
    if account_id is not None and not _account_exists(con, int(account_id)):
        return "the account it was saved for has gone"
    tag_id = view.filters.tag_id
    if tag_id is not None and not _tag_exists(con, int(tag_id)):
        return "the tag it filters on has been deleted"
    return ""


def _account_exists(con: sqlite3.Connection, account_id: int) -> bool:
    return con.execute("SELECT 1 FROM account WHERE id = ?",
                       (account_id,)).fetchone() is not None


def _tag_exists(con: sqlite3.Connection, tag_id: int) -> bool:
    return con.execute("SELECT 1 FROM tag WHERE id = ?",
                       (tag_id,)).fetchone() is not None


def counts(con: sqlite3.Connection) -> dict:
    """For `--check`: how many there are, and how many still mean anything."""
    views = list_views(con)
    return {
        "views": len(views),
        "in_rail": sum(1 for v in views if v.in_rail),
        "unresolved": sum(1 for v in views if unresolved(con, v)),
    }


# ── Writing ────────────────────────────────────────────────────────────────


def save_view(con: sqlite3.Connection, view: SavedView, *,
              commit: bool = True) -> SavedView:
    """Write a saved view whole. Raises ValueError with a sentence.

    WHOLE, like `store/rules.save_rule`: the definition is one value, so there
    is no half-written state the store can be in, and no reconciliation to get
    wrong.
    """
    name = view.name.strip()
    if not name:
        raise ValueError("a saved search needs a name")
    clash = by_name(con, name)
    if clash is not None and clash.id != view.id:
        raise ValueError(f"there is already a saved search called “{name}”")
    now = utc_now()
    definition = to_definition(view)
    if view.id is None:
        order = view.sort_order or _next_order(con)
        cur = con.execute(
            "INSERT INTO saved_view (name, definition, in_rail, sort_order, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, definition, int(view.in_rail), order, now, now))
        view_id = int(cur.lastrowid)
    else:
        view_id = int(view.id)
        con.execute(
            "UPDATE saved_view SET name = ?, definition = ?, in_rail = ?, "
            "sort_order = ?, updated_at = ? WHERE id = ?",
            (name, definition, int(view.in_rail), view.sort_order, now,
             view_id))
    if commit:
        con.commit()
    return get_view(con, view_id)


def _next_order(con: sqlite3.Connection) -> int:
    row = con.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM saved_view").fetchone()
    return int(row[0]) + 1


def delete_view(con: sqlite3.Connection, view_id: int, *,
                commit: bool = True) -> None:
    con.execute("DELETE FROM saved_view WHERE id = ?", (int(view_id),))
    if commit:
        con.commit()


def rename(con: sqlite3.Connection, view_id: int, name: str, *,
           commit: bool = True) -> SavedView | None:
    view = get_view(con, view_id)
    if view is None:
        return None
    return save_view(con, view.with_changes(name=name), commit=commit)


def set_in_rail(con: sqlite3.Connection, view_id: int, in_rail: bool, *,
                commit: bool = True) -> None:
    con.execute("UPDATE saved_view SET in_rail = ?, updated_at = ? WHERE id = ?",
                (int(bool(in_rail)), utc_now(), int(view_id)))
    if commit:
        con.commit()


def reorder(con: sqlite3.Connection, view_ids, *, commit: bool = True) -> None:
    """The order they are drawn in, which is the only order they have.

    Unlike `store/rules.reorder`, this IS presentation: nothing about a saved
    view depends on which came first. It is a write all the same, because where
    a person put their virtual folders is theirs and must survive a restart.
    """
    for order, view_id in enumerate(view_ids, start=1):
        con.execute("UPDATE saved_view SET sort_order = ? WHERE id = ?",
                    (order, int(view_id)))
    if commit:
        con.commit()


# ── Describing ─────────────────────────────────────────────────────────────


def _describe_scope(scope: views_repo.Scope) -> str:
    if scope.kind == "folder":
        return "one folder"
    role = "unanswered mail" if scope.role == views_repo.ROLE_OWED else scope.role
    return f"one account's {role}" if scope.kind == "account" else f"every {role}"


def _describe_filters(filters: views_repo.Filters) -> list[str]:
    said = []
    if filters.unread:
        said.append("unread")
    if filters.flagged:
        said.append("flagged")
    if filters.attachment:
        said.append("with an attachment")
    if filters.contact:
        said.append("from a known correspondent")
    if filters.tag_id is not None:
        said.append("carrying one tag")
    elif filters.tagged:
        said.append("tagged")
    if filters.text.strip():
        said.append(f"matching “{filters.text.strip()}”")
    return said


def describe_scope_here(con: sqlite3.Connection, view: SavedView) -> str:
    """`describe`, with the folder and account NAMED rather than counted.

    Needs a connection, which is why it is not on the dataclass — and why the
    dataclass can still say something useful without one. The read-out wants
    the name; a tooltip built while the rail is being rebuilt wants neither a
    query nor a blank.
    """
    scope = view.scope
    if scope.kind == "folder" and scope.folder_id is not None:
        folder = folders_repo.get_folder(con, int(scope.folder_id))
        if folder is not None:
            return folder.label
    if scope.kind == "account" and scope.account_id is not None:
        row = con.execute("SELECT address FROM account WHERE id = ?",
                          (int(scope.account_id),)).fetchone()
        if row is not None:
            role = ("unanswered mail" if scope.role == views_repo.ROLE_OWED
                    else scope.role)
            return f"{row['address']} · {role}"
    return _describe_scope(scope)
