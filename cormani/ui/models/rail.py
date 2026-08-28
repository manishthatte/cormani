# SPDX-License-Identifier: GPL-3.0-or-later
#
# The rail, as a tree model.
#
# The rail is heterogeneous — four fixed sections, then user-named groups, then
# accounts, then each account's folders, and later sites and calendars — so it
# is built as a tree of small nodes rather than mapped onto one table. A node
# knows its kind, what it is called, what to count and which scope selecting it
# means. Everything the view needs comes through a role; the view never queries.
#
# DRAG AND DROP WRITES THROUGH IMMEDIATELY. A drop calls the repository and then
# rebuilds from what the database now says, rather than moving a node in memory
# and saving later. It is one more query per drag and it removes the entire
# class of bug where the rail and the store disagree — including the one where
# the disagreement is only visible after a restart, by which time the user has
# rearranged fifteen accounts and lost it.
#
# WHY REBUILD RATHER THAN INSERT AND REMOVE ROWS. A move can change three things
# at once — the account's group, its position, and the counts on both groups —
# and expressing that as begin/endMoveRows is where subtle model corruption
# lives. The tree is at most a couple of hundred nodes and is rebuilt in under a
# millisecond; correctness is worth more than the saved redraw. The cost is that
# expansion state is not preserved by Qt, so the view restores it explicitly —
# which it has to do anyway, because collapse is stored per group in the
# database.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import json
import sqlite3
from typing import Any

from PySide6.QtCore import (QAbstractItemModel, QMimeData, QModelIndex, Qt,
                            Signal)

from ...store import accounts as accounts_repo
from ...store import calendars as calendars_repo
from ...store import folders as folders_repo
from ...store import messages as messages_repo
from ...store import savedviews as savedviews_repo
from ...store import views as views_repo

MIME_TYPE = "application/x-cormani-rail"

# Node kinds.
SECTION = "section"
SPECIAL = "special"          # a unified view: Inbox, Owed, Drafts, Sent
GROUP = "group"
ACCOUNT = "account"
FOLDER = "folder"
SITE = "site"
CALENDAR = "calendar"
SAVED = "saved"              # a saved search, drawn as a virtual folder
HINT = "hint"                # an empty state, never selectable

_ROLE = Qt.ItemDataRole.UserRole
KindRole = _ROLE + 1
ScopeRole = _ROLE + 2
ColourRole = _ROLE + 3
CountRole = _ROLE + 4
AccountIdRole = _ROLE + 5
GroupIdRole = _ROLE + 6
FolderIdRole = _ROLE + 7
HiddenRole = _ROLE + 8
KeyRole = _ROLE + 9          # a stable string identity, for restoring selection
CalendarIdRole = _ROLE + 10  # 0 on the "every calendar" row, which is not one
ViewIdRole = _ROLE + 11      # the saved_view row a virtual folder stands for


# The sites are `panels/sites.py`'s registry now, and this module does not keep
# a second list of them. Until stage 7 they were four names here, shown and
# visibly not ready — a rail that hides what is coming reads as a rail that
# will never have it — and the registry replaced that rather than joining it.
#
# WHICH of them appears is the user's, through `config/settings.sites`: the two
# webmail panels are off by default because corMani already holds that mail
# over IMAP, and any of them can be turned off entirely. PLAN.txt's stage 7
# line calls the panels optional and docs/toolkit-verification.txt finding 2
# says why it matters — the embedded Chromium is pinned by Debian and will age
# out, and mail and calendar must be unaffected when it does.


def _detail(con, view) -> str:
    """A saved search's tooltip: the scope NAMED, then what it narrows to.

    `narrowing` rather than `describe`, which opens with the scope in general
    terms — printing both reads "every inbox · every inbox, unread".
    """
    where = savedviews_repo.describe_scope_here(con, view)
    narrowing = view.narrowing()
    return f"{where} · {narrowing}" if narrowing else where


def saved_key(view_id) -> str:
    """The rail key for a saved search, spelled in ONE place.

    `panels/sites.py` owns `rail_key`/`from_rail_key` for the same reason and
    says it: nothing outside should have to know that a rail row is
    "site:whatsapp". A saved view is selected by key after being written — by
    the menu that saved it, and by a tab being restored — so the spelling has
    at least three readers and no obvious home among them.
    """
    return f"view:{int(view_id)}"


def view_id_from_key(key: str):
    """The saved view a rail key names, or None if it names something else."""
    if not isinstance(key, str) or not key.startswith("view:"):
        return None
    try:
        return int(key[5:])
    except ValueError:
        return None


class Node:
    __slots__ = ("kind", "label", "key", "colour", "count", "scope", "detail",
                 "account_id", "group_id", "folder_id", "calendar_id",
                 "view_id", "hidden", "ready", "children", "parent", "row")

    def __init__(self, kind: str, label: str, key: str, **kw: Any) -> None:
        self.kind = kind
        self.label = label
        self.key = key
        self.colour: str = kw.get("colour", "")
        self.count: int = kw.get("count", 0)
        self.scope = kw.get("scope")
        self.detail: str = kw.get("detail", "")
        self.account_id: int | None = kw.get("account_id")
        self.group_id: int | None = kw.get("group_id")
        self.folder_id: int | None = kw.get("folder_id")
        self.calendar_id: int | None = kw.get("calendar_id")
        self.view_id: int | None = kw.get("view_id")
        self.hidden: bool = kw.get("hidden", False)
        self.ready: bool = kw.get("ready", True)
        self.children: list["Node"] = []
        self.parent: "Node | None" = None
        self.row = 0

    def add(self, child: "Node") -> "Node":
        child.parent = self
        child.row = len(self.children)
        self.children.append(child)
        return child

    @property
    def selectable(self) -> bool:
        return self.kind in (SPECIAL, ACCOUNT, FOLDER, SAVED) and self.ready


class RailModel(QAbstractItemModel):
    """Groups, accounts and folders, over the store."""

    layout_changed_by_drop = Signal()

    def __init__(self, con: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._show_hidden = False
        # Which site panels to draw, and what each last reported unread. Both
        # live on the model rather than in the store: they are a property of
        # this session's window, not of a decade of correspondence, and a count
        # is only known while a panel is open.
        self._sites_enabled: list = []
        self._site_unread: dict = {}
        self._root = Node(SECTION, "", "root")
        self.rebuild()

    # -------------------------------------------------------------- building
    @property
    def show_hidden(self) -> bool:
        return self._show_hidden

    def set_show_hidden(self, show: bool) -> None:
        if show != self._show_hidden:
            self._show_hidden = show
            self.rebuild()

    def rebuild(self) -> None:
        self.beginResetModel()
        self._root = Node(SECTION, "", "root")
        try:
            self._build()
        finally:
            self.endResetModel()

    def _build(self) -> None:
        con = self._con
        scope_counts = messages_repo.scope_counts(con)
        unread = messages_repo.unread_counts(con)
        folder_unread = messages_repo.unread_by_folder(con)

        unified = self._root.add(Node(SECTION, "Unified", "section:unified"))
        for key, label, role, count in (
                ("inbox", "Inbox", folders_repo.ROLE_INBOX, scope_counts["inbox"]),
                ("owed", "Owed", views_repo.ROLE_OWED, scope_counts["owed"]),
                ("drafts", "Drafts", folders_repo.ROLE_DRAFTS, scope_counts["drafts"]),
                ("sent", "Sent", folders_repo.ROLE_SENT, 0)):
            unified.add(Node(
                SPECIAL, label, f"unified:{key}", count=count,
                scope=views_repo.Scope(kind="unified", role=role),
                detail=("Unanswered inbound mail. The thread-aware version, "
                        "across channels, arrives with stage 6."
                        if key == "owed" else "")))

        section = self._root.add(Node(SECTION, "Accounts", "section:accounts"))
        every = accounts_repo.list_accounts(con)
        visible = [a for a in every if self._show_hidden or not a.hidden]
        by_group: dict[int | None, list] = {}
        for account in visible:
            by_group.setdefault(account.group_id, []).append(account)

        for group in accounts_repo.list_groups(con):
            members = by_group.get(group.id, [])
            node = section.add(Node(
                GROUP, group.name, f"group:{group.id}", group_id=group.id,
                count=sum(unread.get(a.id, 0) for a in members)))
            for account in members:
                self._account_node(node, account, unread, folder_unread)

        for account in by_group.get(None, []):
            self._account_node(section, account, unread, folder_unread)

        if not visible:
            hidden_count = len(every)
            section.add(Node(
                HINT,
                "none yet" if not hidden_count else f"{hidden_count} hidden",
                "hint:accounts", ready=False,
                # An empty state that does not say what to do next is a dead
                # end, and this is the one a new installation opens on.
                detail=("File ▸ Add mail account… — or right-click here"
                        if not hidden_count else
                        "View ▸ Show hidden accounts brings them back")))

        self._saved(self._root.add(
            Node(SECTION, "Saved searches", "section:saved")))

        self._sites(self._root.add(Node(SECTION, "Sites", "section:sites")))

        self._calendars(self._root.add(
            Node(SECTION, "Calendars", "section:calendars")))

    def _saved(self, section: Node) -> None:
        """The saved searches, as virtual folders. PLAN.txt §2's own words.

        AFTER Accounts and before Sites, which puts every MAIL row above every
        other subsystem. A saved search is a view over mail in the way the
        Unified rows are; a calendar and a web panel are not.

        THE COUNT IS CAPPED, and `store/savedviews.count_capped` carries the
        measurement. In one line: the rail rebuilds whole on every drop and
        every sync, an exact count of a broad view costs 107 ms on a store of a
        hundred thousand messages, and the delegate above draws "999+" past 999
        anyway — so stopping the query at a thousand changes the cost by two
        orders of magnitude and the screen by nothing.

        A VIEW THAT CAN NO LONGER MEAN WHAT IT SAYS IS DRAWN QUIETLY, with the
        reason in its tooltip, and is NOT removed. `hidden` is the rail's word
        for that and the calendars use it for an un-ticked calendar. Deleting a
        saved search because the folder it names went away would be the client
        throwing away something the user made, over a folder that comes back
        when the account is re-added.
        """
        views = savedviews_repo.list_views(self._con, rail_only=True)
        if not views:
            section.add(Node(HINT, "none yet", "hint:saved", ready=False,
                             detail="Search for something, then Edit ▸ Save "
                                    "this search."))
            return
        for view in views:
            wrong = savedviews_repo.unresolved(self._con, view)
            section.add(Node(
                SAVED, view.name, saved_key(view.id), view_id=view.id,
                count=savedviews_repo.count_capped(self._con, view),
                hidden=bool(wrong),
                detail=(f"{view.name} — {wrong}" if wrong
                        else _detail(self._con, view))))

    def _sites(self, section: Node) -> None:
        """The site panels the user has turned on, with what is unread on each.

        THE COUNT IS None UNTIL A PANEL HAS BEEN OPENED, and that is not the
        same as zero. A site nobody has opened is a site corMani has executed
        nothing in — there is no background polling and no hidden page — so the
        honest answer is that the number is unknown, and the rail draws no
        badge rather than a confident nought. `panels/unread.py` carries the
        same distinction all the way down.
        """
        from ...panels import sites as sites_mod

        chosen = self._site_keys()
        if not chosen:
            section.add(Node(HINT, "none turned on", "hint:sites", ready=False,
                             detail="Site panels are optional. Turn one on in "
                                    "the View menu."))
            return
        for key in chosen:
            site = sites_mod.get(key)
            if site is None:
                continue
            count = self._site_unread.get(key)
            section.add(Node(SITE, site.name, sites_mod.rail_key(site),
                             count=count or 0, detail=site.hint))

    def _site_keys(self) -> list:
        """Which sites to draw. The setting when there is one, else the
        registry's own defaults."""
        from ...panels import sites as sites_mod

        chosen = list(getattr(self, "_sites_enabled", None) or [])
        return chosen if chosen else sites_mod.default_keys()

    def set_sites(self, keys) -> None:
        self._sites_enabled = list(keys or [])
        self.rebuild()

    def set_site_unread(self, key: str, count) -> None:
        """What a panel reported. None means not known; see `_sites`."""
        if count is None:
            self._site_unread.pop(key, None)
        else:
            self._site_unread[key] = int(count)
        self.rebuild()

    def _calendars(self, section: Node) -> None:
        """Every calendar, flat, with the account it belongs to in its tooltip.

        FLAT RATHER THAN GROUPED BY ACCOUNT, which is the opposite of what the
        Accounts section does and is deliberate: a calendar view draws Monday
        across every account at once, so the account an entry belongs to is a
        DETAIL of that entry rather than the level above it. Fifteen accounts
        with two calendars each is thirty rows either way; grouping would add
        fifteen more that select nothing.
        """
        known = calendars_repo.list_calendars(self._con)
        if not known:
            section.add(Node(HINT, "none yet", "hint:calendars", ready=False,
                             detail="Add a Google or Microsoft account; a "
                                    "plain IMAP account has no calendar."))
            return
        addresses = {int(r["id"]): r["address"] for r in self._con.execute(
            "SELECT id, address FROM account").fetchall()}
        section.add(Node(CALENDAR, "All calendars", "calendar:all",
                         calendar_id=0,
                         detail="Every calendar that is ticked"))
        for calendar in known:
            section.add(Node(
                CALENDAR, calendar.label, f"calendar:{calendar.id}",
                colour=calendar.display_colour, calendar_id=calendar.id,
                account_id=calendar.account_id,
                # `hidden` is the rail's word for "drawn quietly"; here it
                # means the user has un-ticked the calendar, which is a
                # different fact from an account being hidden and is shown the
                # same way on purpose.
                hidden=not calendar.shown,
                detail=(f"{addresses.get(calendar.account_id, '')}"
                        f"{' · read-only' if not calendar.writable else ''}"
                        f"{' · ' + calendar.last_error if calendar.last_error else ''}")))

    def _account_node(self, parent: Node, account, unread: dict,
                      folder_unread: dict) -> Node:
        node = parent.add(Node(
            ACCOUNT, account.label, f"account:{account.id}",
            colour=account.colour, count=unread.get(account.id, 0),
            account_id=account.id, group_id=account.group_id,
            hidden=account.hidden, detail=account.address,
            scope=views_repo.Scope(kind="account",
                                      role=folders_repo.ROLE_INBOX,
                                      account_id=account.id)))
        for folder in folders_repo.list_folders(self._con, account.id):
            # Drafts counts everything it holds; every other folder counts what
            # is unread in it. An unread draft is not a thing.
            count = folder_unread.get(folder.id, 0)
            node.add(Node(
                FOLDER, folder.label, f"folder:{folder.id}",
                colour=account.colour, count=count, folder_id=folder.id,
                account_id=account.id, detail=folder.path,
                scope=views_repo.Scope(kind="folder", role=folder.role,
                                          account_id=account.id,
                                          folder_id=folder.id)))
        return node

    # ------------------------------------------------------------- Qt basics
    def _node(self, index: QModelIndex) -> Node:
        return index.internalPointer() if index.isValid() else self._root

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        children = self._node(parent).children
        if row >= len(children):
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(self, index=QModelIndex()):
        node = self._node(index)
        if node is self._root or node.parent is None or node.parent is self._root:
            return QModelIndex()
        return self.createIndex(node.parent.row, 0, node.parent)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.column() > 0 else len(self._node(parent).children)

    def columnCount(self, parent=QModelIndex()):
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = self._node(index)
        if role == Qt.ItemDataRole.DisplayRole:
            return node.label
        if role == Qt.ItemDataRole.ToolTipRole:
            return node.detail or None
        if role == KindRole:
            return node.kind
        if role == ScopeRole:
            return node.scope
        if role == ColourRole:
            return node.colour
        if role == CountRole:
            return node.count
        if role == AccountIdRole:
            return node.account_id
        if role == GroupIdRole:
            return node.group_id
        if role == FolderIdRole:
            return node.folder_id
        if role == CalendarIdRole:
            return node.calendar_id
        if role == ViewIdRole:
            return node.view_id
        if role == HiddenRole:
            return node.hidden
        if role == KeyRole:
            return node.key
        return None

    def flags(self, index):
        if not index.isValid():
            # The invalid index is the drop target for "put this at the top
            # level", which nothing here allows: an account outside the Accounts
            # section has nowhere to be drawn.
            return Qt.ItemFlag.NoItemFlags
        node = self._node(index)
        flags = Qt.ItemFlag.ItemIsEnabled
        if node.selectable:
            flags |= Qt.ItemFlag.ItemIsSelectable
        if node.kind in (ACCOUNT, GROUP):
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        if node.kind == GROUP or node.key == "section:accounts":
            # Only the Accounts section takes a drop. Sites and Calendars are
            # not places an account can be, and a drop indicator that appears
            # over them is an invitation to a move that cannot happen.
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        if node.kind == ACCOUNT:
            # Dropping ON an account is not a move into it; the view turns that
            # into a move to the account's position. Accepting the drop here is
            # what makes the indicator appear between rows.
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        if not node.ready and node.kind != SECTION:
            flags &= ~Qt.ItemFlag.ItemIsEnabled
        return flags

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        return None

    # ------------------------------------------------------------ drag, drop
    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self):
        return [MIME_TYPE]

    def mimeData(self, indexes):
        payload = []
        for index in indexes:
            if not index.isValid() or index.column() != 0:
                continue
            node = self._node(index)
            if node.kind == ACCOUNT:
                payload.append({"kind": ACCOUNT, "id": node.account_id})
            elif node.kind == GROUP:
                payload.append({"kind": GROUP, "id": node.group_id})
        data = QMimeData()
        data.setData(MIME_TYPE, json.dumps(payload).encode("utf-8"))
        return data

    def _payload(self, data: QMimeData) -> list[dict]:
        if not data.hasFormat(MIME_TYPE):
            return []
        try:
            loaded = json.loads(bytes(data.data(MIME_TYPE)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # Malformed payload: refuse the drop rather than raise. The only way
            # to get here is another application claiming our mime type.
            return []
        return loaded if isinstance(loaded, list) else []

    def canDropMimeData(self, data, action, row, column, parent):
        payload = self._payload(data)
        if not payload:
            return False
        target = self._node(parent)
        kinds = {p.get("kind") for p in payload}
        if kinds == {ACCOUNT}:
            return target.kind in (GROUP, ACCOUNT) or target.key == "section:accounts"
        if kinds == {GROUP}:
            return target.key == "section:accounts" or target.kind == GROUP
        return False

    def dropMimeData(self, data, action, row, column, parent):
        if action == Qt.DropAction.IgnoreAction:
            return True
        payload = self._payload(data)
        if not payload:
            return False
        target = self._node(parent)

        moved = False
        for entry in payload:
            if entry.get("kind") == ACCOUNT and entry.get("id") is not None:
                moved |= self._drop_account(int(entry["id"]), target, row)
            elif entry.get("kind") == GROUP and entry.get("id") is not None:
                moved |= self._drop_group(int(entry["id"]), target, row)
        if moved:
            self.rebuild()
            self.layout_changed_by_drop.emit()
        return moved

    def _drop_account(self, account_id: int, target: Node, row: int) -> bool:
        if target.kind == ACCOUNT:
            # Dropped onto a row rather than between two: land where that row
            # is, which is what the user pointed at.
            group_id = target.group_id
            position = target.row
        elif target.kind == GROUP:
            group_id = target.group_id
            position = row if row >= 0 else len(target.children)
        elif target.key == "section:accounts":
            # The section itself: leave whatever group the account was in. This
            # is the only gesture that ungroups, and it needs to exist —
            # otherwise a group can be joined and never left.
            group_id = None
            position = row if row >= 0 else 10_000
        else:
            return False
        accounts_repo.move_account(self._con, account_id, group_id, position)
        return True

    def _drop_group(self, group_id: int, target: Node, row: int) -> bool:
        order = [g.id for g in accounts_repo.list_groups(self._con)]
        if group_id not in order:
            return False
        if target.kind == GROUP:
            position = order.index(target.group_id)
        elif row >= 0:
            # Rows under the Accounts section count groups first, then loose
            # accounts, so a row past the last group means "last".
            position = min(row, len(order) - 1)
        else:
            position = len(order) - 1
        order.remove(group_id)
        order.insert(max(0, min(position, len(order))), group_id)
        accounts_repo.reorder_groups(self._con, order)
        return True

    # ------------------------------------------------------------- selection
    def index_for_key(self, key: str) -> QModelIndex:
        """Find a node by its stable key. Used to restore the selection after a
        rebuild, which is every mutation — without it, archiving a message would
        drop the rail's selection and jump the list back to the unified inbox."""
        def walk(node: Node, parent_index: QModelIndex) -> QModelIndex:
            for child in node.children:
                index = self.index(child.row, 0, parent_index)
                if child.key == key:
                    return index
                found = walk(child, index)
                if found.isValid():
                    return found
            return QModelIndex()
        return walk(self._root, QModelIndex())

    def group_index(self, group_id: int) -> QModelIndex:
        return self.index_for_key(f"group:{group_id}")

    def section_index(self, key: str) -> QModelIndex:
        return self.index_for_key(f"section:{key}")

    def default_index(self) -> QModelIndex:
        return self.index_for_key("unified:inbox")
