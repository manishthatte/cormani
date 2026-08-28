# SPDX-License-Identifier: GPL-3.0-or-later
#
# The message list, as a model.
#
# AN ITEM MODEL, NOT A LIST MODEL, AND NOW IT EARNS IT. Stage 1 said threading
# would give a row children and that starting from a QAbstractListModel would
# mean rewriting the model, the view and the delegate together at the point when
# there is real mail to break. That is what arrived: a conversation is a root
# with its other messages beneath it, and every row is still a `Row` from the
# store.
#
# THE ROOT OF A CONVERSATION IS ITS NEWEST MESSAGE IN THIS VIEW, not the one
# that started it and not one from somewhere else. A mail list is a list of
# things that need attention and what needs attention is the latest turn; the
# count and the twisty say there is more behind it. IN THIS VIEW is the half
# that had to be got right: a thread's place in the list is decided by its
# newest in-scope message, so drawing a different one at the top would be an
# order the rows contradict — and every top-level row would no longer be a
# message the folder actually holds, which is what a filter, an action and a
# count all assume.
#
# A CONVERSATION IS NOT A FOLDER. The children include the thread's messages
# from OTHER folders — the replies the user sent, which are the half that
# explains the half they received — and each of those draws its own location.
# They are marked `in_scope = False`, because they are in the LIST and not in
# the VIEW: no filter chose them, and an action on the conversation does not
# reach them. See store/threads.context_where.
#
# MUTATIONS DO NOT RESET THE MODEL. Marking a message read repaints one row;
# archiving it removes one row. Both go through `apply_change`, which asks the
# store which of the touched messages still belong in this view and acts on the
# difference. When a ROOT leaves, its newest surviving child is PROMOTED into
# its place rather than the whole subtree being torn down and rebuilt — that is
# what keeps the thread expanded, the scroll where it was, and the cursor on the
# conversation the user is working through.
#
# THE SEARCH AND THE CONVERSATION ARE PARTS OF THE QUERY, beside the scope, the
# filters and the order. Everything below re-asks the store rather than
# reasoning about rows, so each of them costs the same three calls a folder
# change does.
#
# PAGING IS BY canFetchMore. `list_page_size` from the configuration bounds the
# first query; scrolling asks for the next page. The unified inbox across
# fifteen accounts with full history is hundreds of thousands of rows, and the
# cost of drawing a list is dominated by how many rows were fetched, not by how
# many exist. A page is counted in MESSAGES IN SCOPE — the conversation members
# pulled in beside them are context and must not make the list think it has
# reached the end.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import dataclasses
import sqlite3

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal

from ...store import messages as messages_repo
from ...store import search as search_mod
from ...store import threads as threads_repo
from ...store import views as views_repo

_ROLE = Qt.ItemDataRole.UserRole
RowRole = _ROLE + 1          # the store's Row dataclass, for the delegate
MessageIdRole = _ROLE + 2
UnreadRole = _ROLE + 3
ColourRole = _ROLE + 4
FlaggedRole = _ROLE + 5
ThreadCountRole = _ROLE + 6  # messages in the whole conversation, or 0
ThreadUnreadRole = _ROLE + 7
InScopeRole = _ROLE + 8


class Node:
    """One row of the tree: a message, and the conversation beneath it.

    A plain object rather than a dataclass because a QModelIndex holds a raw
    pointer to it and the model creates one per visible row; `__slots__` is the
    difference between two hundred thousand small objects and two hundred
    thousand dictionaries.
    """

    __slots__ = ("row", "children", "parent", "position", "count", "unread")

    def __init__(self, row, parent=None, position: int = 0) -> None:
        self.row = row
        self.children: list["Node"] = []
        self.parent = parent
        self.position = position
        self.count = 0                  # the whole conversation, from the store
        self.unread = 0

    @property
    def key(self) -> str:
        return getattr(self.row, "thread_key", "") or ""


class MessageModel(QAbstractItemModel):
    """Rows for one scope, one set of filters, one order and one search."""

    # Emitted whenever the number of rows the query would return changes, so the
    # status bar can say "40 of 876" without asking.
    counts_changed = Signal(int, int)          # loaded, total

    def __init__(self, con: sqlite3.Connection, *, page_size: int = 200,
                 parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._page_size = max(20, int(page_size))
        self._scope = views_repo.Scope()
        self._filters = views_repo.Filters()
        self._sort = views_repo.Sort()
        self._search = search_mod.Query()
        self._threaded = True
        self._roots: list[Node] = []
        self._by_id: dict[int, Node] = {}
        self._loaded = 0                # messages IN SCOPE, for paging
        self._total = 0
        self.refresh()

    # ----------------------------------------------------------- the query
    @property
    def scope(self) -> views_repo.Scope:
        return self._scope

    @property
    def filters(self) -> views_repo.Filters:
        return self._filters

    @property
    def sort(self) -> views_repo.Sort:
        return self._sort

    @property
    def search(self) -> search_mod.Query:
        return self._search

    @property
    def threaded(self) -> bool:
        """Whether the user asked for conversations. `grouping` is whether they
        are actually being drawn, which is the one the view should ask."""
        return self._threaded

    @property
    def grouping(self) -> bool:
        """Threading needs date order and no search.

        A conversation is a run of messages ordered by when they happened; sort
        it by sender and the run is not a run any more. A search returns HITS,
        and grouping them under a message that did not match would be answering
        a different question. Both are refusals rather than silent failures —
        the View menu disables the entry and says which it is.
        """
        return bool(self._threaded and self._sort.key == "date"
                    and not self._search.active)

    @property
    def total(self) -> int:
        return self._total

    def set_query(self, scope=None, filters=None, sort=None, search=None,
                  threaded=None) -> None:
        changed = False
        for name, value in (("_scope", scope), ("_filters", filters),
                            ("_sort", sort), ("_search", search),
                            ("_threaded", threaded)):
            if value is not None and value != getattr(self, name):
                setattr(self, name, value)
                changed = True
        if changed:
            self.refresh()

    def refresh(self) -> None:
        """Re-run the query from the top. The one place a reset is right: the
        query itself changed, so no row's identity carries over."""
        self.beginResetModel()
        try:
            self._total = messages_repo.count(self._con, self._scope,
                                              self._filters, search=self._search)
            rows = messages_repo.fetch(
                self._con, self._scope, self._filters, self._sort,
                search=self._search, threaded=self.grouping,
                limit=self._page_size)
            self._loaded = len(rows)
            self._roots = []
            self._by_id = {}
            self._graft(rows, signal=False)
        finally:
            self.endResetModel()
        self.counts_changed.emit(self._loaded, self._total)

    # ------------------------------------------------------------ the tree
    def _graft(self, rows, *, signal: bool) -> None:
        """Add a page of rows to the tree, in the order the store returned them.

        Without `signal` the caller is inside a reset and Qt must not be told;
        with it, the insertions are announced so the view keeps its place.
        """
        if not self.grouping:
            self._append_roots([Node(r) for r in rows], signal=signal)
            return

        keys, groups = [], {}
        for row in rows:
            key = row.thread_key or f"id:{row.id}"
            if key not in groups:
                keys.append(key)
                groups[key] = []
            groups[key].append(row)

        known = self._key_index()
        # The conversation's other messages, for the keys this page introduced.
        fresh = [k for k in keys if k not in known]
        context = messages_repo.thread_context(
            self._con, fresh, [r.id for r in rows]) if fresh else []
        extra: dict[str, list] = {}
        for row in context:
            extra.setdefault(row.thread_key or "", []).append(row)

        counts = threads_repo.counts(self._con, keys)
        pending: list[Node] = []
        for key in keys:
            existing = known.get(key)
            if existing is not None:
                # A thread the previous page had already started. Its remaining
                # messages are older, and the store returned them in that order.
                self._append_children(existing, groups[key], signal=signal)
                continue
            # The store returned the in-scope rows newest first; the head of
            # that run is the row, and everything else — including the thread's
            # messages from other folders — goes underneath in date order.
            head, *rest = groups[key]
            children = sorted(rest + extra.get(key, []),
                              key=lambda r: (r.date_at or "", r.id), reverse=True)
            node = Node(head)
            node.count, node.unread = counts.get(key, (len(children) + 1, 0))
            for position, row in enumerate(children):
                node.children.append(Node(row, parent=node, position=position))
            pending.append(node)
        self._append_roots(pending, signal=signal)

    def _key_index(self) -> dict[str, Node]:
        return {node.key: node for node in self._roots if node.key}

    def _append_roots(self, nodes: list[Node], *, signal: bool) -> None:
        if not nodes:
            return
        first = len(self._roots)
        if signal:
            self.beginInsertRows(QModelIndex(), first, first + len(nodes) - 1)
        for offset, node in enumerate(nodes):
            node.position = first + offset
            self._roots.append(node)
            self._register(node)
        if signal:
            self.endInsertRows()

    def _append_children(self, parent: Node, rows, *, signal: bool) -> None:
        if not rows:
            return
        first = len(parent.children)
        if signal:
            self.beginInsertRows(self._index_of_node(parent),
                                 first, first + len(rows) - 1)
        for offset, row in enumerate(rows):
            child = Node(row, parent=parent, position=first + offset)
            parent.children.append(child)
            self._by_id[row.id] = child
        if signal:
            self.endInsertRows()

    def _register(self, node: Node) -> None:
        self._by_id[node.row.id] = node
        for child in node.children:
            self._by_id[child.row.id] = child

    def _reposition(self, parent: Node | None) -> None:
        """Row numbers after a removal. Qt asks for them by position, so they
        have to be true the moment `endRemoveRows` returns."""
        nodes = parent.children if parent is not None else self._roots
        for position, node in enumerate(nodes):
            node.position = position

    def _index_of_node(self, node) -> QModelIndex:
        if node is None:
            return QModelIndex()
        return self.createIndex(node.position, 0, node)

    # ------------------------------------------------------------- Qt basics
    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if parent.isValid():
            node = parent.internalPointer()
            if node is None or row >= len(node.children):
                return QModelIndex()
            return self.createIndex(row, column, node.children[row])
        return self.createIndex(row, column, self._roots[row])

    def parent(self, index=QModelIndex()):
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        if node is None or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.parent.position, 0, node.parent)

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return len(self._roots)
        node = parent.internalPointer()
        if node is None or node.parent is not None:
            return 0                    # one level; see the note at the top
        return len(node.children)

    def columnCount(self, parent=QModelIndex()):
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        node = index.internalPointer() if index.isValid() else None
        if node is None:
            return None
        row = node.row
        if role == Qt.ItemDataRole.DisplayRole:
            # Type-to-select in the view uses this, so it is the subject rather
            # than a formatted whole row.
            return row.subject_label
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{row.correspondent}\n{row.subject_label}\n{row.location}"
        if role == RowRole:
            return row
        if role == MessageIdRole:
            return row.id
        if role == UnreadRole:
            return not row.seen
        if role == ColourRole:
            return row.account_colour
        if role == FlaggedRole:
            return row.flagged
        if role == ThreadCountRole:
            return node.count if node.children else 0
        if role == ThreadUnreadRole:
            return node.unread if node.children else 0
        if role == InScopeRole:
            return row.in_scope
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        node = index.internalPointer()
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if node is not None and not node.children:
            base |= Qt.ItemFlag.ItemNeverHasChildren
        return base

    # ---------------------------------------------------------------- paging
    def canFetchMore(self, parent=QModelIndex()):
        return not parent.isValid() and self._loaded < self._total

    def fetchMore(self, parent=QModelIndex()):
        if parent.isValid():
            return
        more = messages_repo.fetch(
            self._con, self._scope, self._filters, self._sort,
            search=self._search, threaded=self.grouping,
            limit=self._page_size, offset=self._loaded)
        if not more:
            # The count and the page disagree — something changed underneath.
            # Believe the page: a total that cannot be reached would make
            # canFetchMore true forever and the view would ask on every scroll.
            self._total = self._loaded
            return
        self._loaded += len(more)
        self._graft(more, signal=True)
        self.counts_changed.emit(self._loaded, self._total)

    def fetch_all(self, *, limit: int = 20000) -> None:
        """Load every remaining page. For the keyboard's next-unread, which has
        to be able to reach a message the user has not scrolled to."""
        while self.canFetchMore() and self._loaded < limit:
            before = self._loaded
            self.fetchMore()
            if self._loaded == before:
                break

    # ------------------------------------------------------------- accessors
    def row_at(self, index) -> messages_repo.Row | None:
        if isinstance(index, QModelIndex):
            node = index.internalPointer() if index.isValid() else None
            return node.row if node is not None else None
        if 0 <= index < len(self._roots):
            return self._roots[index].row
        return None

    def rows(self) -> list[messages_repo.Row]:
        """Every row in the tree, in the order it is drawn."""
        return [node.row for node in self.walk()]

    def walk(self):
        """Nodes in visual order: a root, then its children, then the next."""
        for root in self._roots:
            yield root
            yield from root.children

    def index_of(self, message_id: int) -> QModelIndex:
        node = self._by_id.get(message_id)
        return self._index_of_node(node) if node is not None else QModelIndex()

    def ids_for(self, indexes) -> list[int]:
        out = []
        for index in indexes:
            row = self.row_at(index)
            if row is not None and row.id not in out:
                out.append(row.id)
        return out

    def thread_ids(self, index) -> list[int]:
        """The message ids a row stands for when it is acted on as a whole.

        A root with its conversation beneath it stands for the messages of that
        conversation that are IN VIEW — the ones a filter chose. The replies
        pulled in from other folders are context and are deliberately not
        included: "archive this conversation" must not move the user's Sent
        copies, and the status line reports the number it did move.
        """
        node = index.internalPointer() if index.isValid() else None
        if node is None:
            return []
        rows = [node.row, *(c.row for c in node.children)]
        return [r.id for r in rows if r.in_scope]

    def next_unread(self, from_index, *, forward: bool = True) -> QModelIndex:
        """The next unread row in the direction asked, or an invalid index.

        Walks the tree in the order it is drawn, and loads the rest of the list
        if it has to: someone pressing `n` at the bottom of a page means "the
        next unread message", not "the next among the two hundred fetched".
        """
        while True:
            order = list(self.walk())
            start = -1
            current = (from_index.internalPointer()
                       if isinstance(from_index, QModelIndex) and from_index.isValid()
                       else None)
            if current is not None:
                for position, node in enumerate(order):
                    if node is current:
                        start = position
                        break
            step = 1 if forward else -1
            position = start + step
            while 0 <= position < len(order):
                if not order[position].row.seen:
                    return self._index_of_node(order[position])
                position += step
            if forward and self.canFetchMore():
                before = self._loaded
                last = order[-1] if order else None
                self.fetchMore()
                if self._loaded == before:
                    return QModelIndex()
                from_index = self._index_of_node(last)
                continue
            return QModelIndex()

    # ------------------------------------------------------------- mutations
    def apply_change(self, message_ids) -> None:
        """Re-read these messages and update in place; drop the ones that left.

        Called after every store mutation. The store is the authority — this
        does not guess what the change did, it asks.
        """
        touched = [mid for mid in message_ids if mid in self._by_id]
        if not touched:
            self._recount()
            return

        # Only the rows a query actually chose can leave it. A conversation
        # member shown from another folder was never in scope and must not be
        # removed for failing a filter it was never subject to.
        in_scope = [mid for mid in touched if self._by_id[mid].row.in_scope]
        staying = messages_repo.filter_ids(
            self._con, self._scope, self._filters, in_scope, search=self._search)

        for mid in touched:
            if mid not in in_scope or mid in staying:
                self._refresh_row(mid)
        for mid in [m for m in in_scope if m not in staying]:
            self._remove(mid)
        self._refresh_thread_counts()
        self._recount()

    def _refresh_row(self, message_id: int) -> None:
        node = self._by_id.get(message_id)
        if node is None:
            return
        fresh = messages_repo.get_row(self._con, message_id)
        if fresh is None:
            return
        # A context row stays a context row. `get_row` answers about the
        # message and knows nothing about which view is asking.
        node.row = fresh if node.row.in_scope else dataclasses.replace(
            fresh, in_scope=False)
        index = self._index_of_node(node)
        self.dataChanged.emit(index, index)

    def _remove(self, message_id: int) -> None:
        node = self._by_id.get(message_id)
        if node is None:
            return
        if node.parent is not None:
            parent = node.parent
            self.beginRemoveRows(self._index_of_node(parent),
                                 node.position, node.position)
            del parent.children[node.position]
            self._by_id.pop(message_id, None)
            self._reposition(parent)
            self.endRemoveRows()
            return

        survivors = [c for c in node.children if c.row.in_scope]
        if not survivors:
            # Nothing of this conversation is in view any more, context or not.
            self.beginRemoveRows(QModelIndex(), node.position, node.position)
            for child in node.children:
                self._by_id.pop(child.row.id, None)
            del self._roots[node.position]
            self._by_id.pop(message_id, None)
            self._reposition(None)
            self.endRemoveRows()
            return

        # PROMOTE the newest survivor into the root's own row, rather than
        # removing a subtree and building another. The thread keeps its place,
        # its expansion and the cursor on it. The newest IN-SCOPE survivor: a
        # top-level row is a message this view holds, and a conversation whose
        # only remaining members are context has left the view entirely.
        promoted = survivors[0]
        self.beginRemoveRows(self._index_of_node(node),
                             promoted.position, promoted.position)
        del node.children[promoted.position]
        self._reposition(node)
        self.endRemoveRows()
        self._by_id.pop(message_id, None)
        node.row = promoted.row
        self._by_id[node.row.id] = node
        index = self._index_of_node(node)
        self.dataChanged.emit(index, index)

    def _refresh_thread_counts(self) -> None:
        if not self.grouping:
            return
        keys = [node.key for node in self._roots if node.children]
        counts = threads_repo.counts(self._con, keys)
        for node in self._roots:
            if node.children and node.key in counts:
                node.count, node.unread = counts[node.key]
                index = self._index_of_node(node)
                self.dataChanged.emit(index, index)

    def _recount(self) -> None:
        self._total = messages_repo.count(self._con, self._scope, self._filters,
                                          search=self._search)
        loaded = sum(1 for node in self.walk() if node.row.in_scope)
        self._loaded = min(self._loaded, max(loaded, 0)) if loaded else 0
        self.counts_changed.emit(loaded, self._total)
