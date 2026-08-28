# SPDX-License-Identifier: GPL-3.0-or-later
#
# The rail: the view over the account tree.
#
# Everything here is drawn rather than styled, for one reason: a rail row has an
# account's colour on it, a count that is weighted when it means unread, and a
# muted state for a hidden account, and none of the three can be expressed in a
# stylesheet against a QTreeView. Once one of them has to be painted, all of
# them should be, or the row is assembled from two mechanisms that disagree
# about padding.
#
# COLLAPSE IS PERSISTED, AND IT IS ACCOUNT DATA. A user with fifteen accounts
# collapses the groups they are not working in today, and that arrangement is
# theirs in the way the order is theirs. It is written to the database on the
# signal, not at shutdown, for the same reason the order is: an arrangement that
# survives only a clean quit is one people stop trusting.
#
# SELECTION IS RESTORED BY KEY, NOT BY INDEX. The model resets on every mutation
# — see models/rail.py for why — and an index does not survive a reset. Every
# node carries a stable string key, and the view puts the selection back on the
# key it had. Without it, archiving a message would return the rail to the
# unified inbox, which is the sort of thing that makes an application feel like
# it is fighting you.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QInputDialog,
                               QMenu, QStyle, QStyledItemDelegate, QTreeView)

from ..store import accounts as accounts_repo
from ..store import calendars as calendars_repo
from . import density as density_mod
from . import icons
from . import theme as theme_mod
from .models import rail as rail_model
from .models.rail import ACCOUNT, GROUP, HINT, SECTION, SPECIAL, RailModel


class RailDelegate(QStyledItemDelegate):
    """One row: an optional colour bar, a label, and a count."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme_mod.SOLARIZED_LIGHT
        self.density = density_mod.NORMAL

    def sizeHint(self, option, index) -> QSize:
        metrics = option.fontMetrics
        height = density_mod.rail_row_height(self.density, metrics.height())
        if index.data(rail_model.KindRole) == SECTION:
            height += 4                    # sections get air above and below
        return QSize(option.rect.width(), height)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.theme
        d = self.density
        rect = option.rect
        kind = index.data(rail_model.KindRole)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)

        if selected:
            painter.fillRect(rect, QColor(t.accent))
            fg = QColor(t.text_inverse)
            muted = QColor(t.text_inverse)
            muted.setAlpha(190)
        else:
            if hovered and kind not in (SECTION, HINT):
                painter.fillRect(rect, QColor(t.accent_muted))
            fg = QColor(t.text_strong if kind in (SECTION, ACCOUNT, GROUP) else t.text)
            muted = QColor(t.text_muted)
        if not enabled:
            fg = QColor(t.text_muted)

        font = QFont(option.font)
        if kind == SECTION:
            font.setBold(True)
            font.setPointSizeF(max(6.0, font.pointSizeF() - 1))
            fg = QColor(t.text_muted)
        elif kind in (GROUP, ACCOUNT):
            font.setBold(True)
        painter.setFont(font)

        left = rect.left() + d.pad_h // 2
        right = rect.right() - d.pad_h // 2

        # The account's colour, carried from here onto every message row. A bar
        # rather than a dot: at fifteen accounts a dot is a pixel of hue and a
        # bar is readable at a glance, which is the whole job.
        colour = index.data(rail_model.ColourRole)
        if kind == ACCOUNT and colour:
            bar = QRectF(left, rect.center().y() - d.icon / 2.0,
                         float(d.swatch), float(d.icon))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawRoundedRect(bar, d.swatch / 2.0, d.swatch / 2.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            left += d.swatch + 5

        if index.data(rail_model.HiddenRole):
            box = QRectF(left, rect.center().y() - d.icon / 2.0, d.icon, d.icon)
            icons.paint(painter, "hidden", box, muted)
            left += d.icon + 4

        count = index.data(rail_model.CountRole) or 0
        if count:
            text = str(count) if count < 1000 else "999+"
            count_font = QFont(option.font)
            count_font.setBold(kind in (SPECIAL, ACCOUNT, GROUP))
            count_font.setPointSizeF(max(6.0, count_font.pointSizeF() - 1))
            width = option.fontMetrics.boundingRect(text).width() + 12
            pill = QRectF(right - width, rect.center().y() - d.icon / 2.0,
                          float(width), float(d.icon))
            if not selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(t.surface_raised))
                painter.drawRoundedRect(pill, d.icon / 2.0, d.icon / 2.0)
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setFont(count_font)
            painter.setPen(QPen(QColor(t.unread) if not selected else fg))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
            painter.setFont(font)
            right -= width + 6

        painter.setPen(QPen(fg))
        label = index.data(Qt.ItemDataRole.DisplayRole) or ""
        available = max(0, right - left)
        elided = option.fontMetrics.elidedText(
            label, Qt.TextElideMode.ElideRight, available)
        painter.drawText(rect.adjusted(left - rect.left(), 0,
                                       right - rect.right(), 0),
                         int(Qt.AlignmentFlag.AlignVCenter |
                             Qt.AlignmentFlag.AlignLeft), elided)
        painter.restore()


class RailView(QTreeView):
    scope_chosen = Signal(object, str)         # Scope, node key
    # A calendar, or 0 for "every calendar that is ticked". A SECOND signal
    # rather than a fourth kind of Scope, for the reason the search bar's
    # decision table gives about scopes: a Scope answers "which mail", and no
    # value of it describes a week of a calendar. The two travel side by side
    # and the pane decides which half of the window is showing.
    calendar_chosen = Signal(int, str)
    site_chosen = Signal(str, str)             # site key, node key
    # A saved search. Its own signal and not `scope_chosen`, for the reason
    # that is the whole point of a saved search: `MailPane._scope_chosen` ENDS
    # a search, deliberately — clicking a folder is how a person says "show me
    # this instead". A virtual folder whose scope arrived that way would clear
    # the very query it exists to run, and would do it silently.
    view_chosen = Signal(int, str)             # saved_view id, node key
    accounts_changed = Signal()
    calendars_changed = Signal()
    # The rail is where somebody with no accounts is looking when they wonder
    # how to get one, so the context menu offers it — and asks the window
    # rather than doing it, because adding an account is a network act with a
    # thread behind it and the rail is a view of the account tree.
    add_account_wanted = Signal()

    def __init__(self, con, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._restoring = 0
        self.setObjectName("accountRail")
        self.setHeaderHidden(True)
        self.setUniformRowHeights(False)
        self.setMouseTracking(True)
        self.setExpandsOnDoubleClick(False)
        self.setAnimated(False)
        self.setIndentation(14)
        # Wide enough for a grouped account name at the deepest indent. Narrower
        # than this and the rail shows colour bars with no words beside them.
        self.setMinimumWidth(150)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self._delegate = RailDelegate(self)
        self.setItemDelegate(self._delegate)
        self._model = RailModel(con, self)
        self.setModel(self._model)

        self._model.layout_changed_by_drop.connect(self._after_drop)
        self.expanded.connect(self._remember_expansion)
        self.collapsed.connect(self._remember_expansion)
        self.customContextMenuRequested.connect(self._context_menu)
        sel = self.selectionModel()
        if sel is not None:
            sel.currentChanged.connect(self._current_changed)

        self._apply_expansion()
        self.select_key("unified:inbox")

    @contextmanager
    def _quiet(self):
        """Suppress the signals a rebuild would otherwise emit.

        A COUNTER, not a flag. `reload` calls `_apply_expansion`, which needs the
        same suppression; with a boolean the inner call's `finally` cleared the
        outer one's guard, and the `setCurrentIndex` at the end of `reload` then
        looked like the user choosing a scope. The list pane took that at face
        value and cleared its selection — so marking a message read, which
        refreshes the counts, deselected the message being read.
        """
        self._restoring += 1
        try:
            yield
        finally:
            self._restoring -= 1

    # ---------------------------------------------------------------- theme
    def set_theme(self, theme) -> None:
        self._delegate.theme = theme
        self.viewport().update()

    def set_density(self, density) -> None:
        self._delegate.density = density
        self.scheduleDelayedItemsLayout()

    @property
    def model_obj(self):
        """The model, for the things that push into it — the site panels'
        unread counts. Named so as not to collide with QAbstractItemView.model,
        which returns the same object through Qt's own accessor and would be a
        confusing thing to shadow."""
        return self._model

    def set_sites(self, keys) -> None:
        """Which site panels the rail lists. See `models/rail._sites`."""
        key = self.current_key()
        with self._quiet():
            self._model.set_sites(keys)
            self._apply_expansion()
            index = self._model.index_for_key(key)
            if index.isValid():
                self.setCurrentIndex(index)

    def set_show_hidden(self, show: bool) -> None:
        """Show or hide the accounts the user has hidden, keeping the rail's
        expansion and selection across the rebuild that causes."""
        key = self.current_key()
        with self._quiet():
            self._model.set_show_hidden(show)
            self._apply_expansion()
            index = self._model.index_for_key(key)
            if index.isValid():
                self.setCurrentIndex(index)

    # ------------------------------------------------------------ selection
    def current_key(self) -> str:
        index = self.currentIndex()
        return index.data(rail_model.KeyRole) or "" if index.isValid() else ""

    def select_key(self, key: str) -> bool:
        index = self._model.index_for_key(key)
        if not index.isValid():
            return False
        parent = index.parent()
        while parent.isValid():
            self.expand(parent)
            parent = parent.parent()
        self.setCurrentIndex(index)
        return True

    def _current_changed(self, current: QModelIndex, _previous) -> None:
        if self._restoring or not current.isValid():
            return
        key = current.data(rail_model.KeyRole)
        if current.data(rail_model.KindRole) == rail_model.CALENDAR:
            self.calendar_chosen.emit(
                int(current.data(rail_model.CalendarIdRole) or 0), key)
            return
        if current.data(rail_model.KindRole) == rail_model.SAVED:
            view_id = current.data(rail_model.ViewIdRole)
            if view_id is not None:
                self.view_chosen.emit(int(view_id), key)
            return
        if current.data(rail_model.KindRole) == rail_model.SITE:
            # The site's own key rather than the node's. `panels/sites.py`
            # owns the spelling of both and converts between them, so nothing
            # else has to know that a rail row is "site:whatsapp".
            from ..panels import sites as sites_mod

            site = sites_mod.from_rail_key(key)
            if site is not None:
                self.site_chosen.emit(site.key, key)
            return
        scope = current.data(rail_model.ScopeRole)
        if scope is not None:
            self.scope_chosen.emit(scope, key)

    # ----------------------------------------------------------- expansion
    def _apply_expansion(self) -> None:
        """Sections open; groups as the database remembers them."""
        with self._quiet():
            for row in range(self._model.rowCount()):
                index = self._model.index(row, 0)
                self.setExpanded(index, True)
            collapsed = {g.id: g.collapsed
                         for g in accounts_repo.list_groups(self._con)}
            section = self._model.section_index("accounts")
            for row in range(self._model.rowCount(section)):
                index = self._model.index(row, 0, section)
                group_id = index.data(rail_model.GroupIdRole)
                if index.data(rail_model.KindRole) == GROUP:
                    self.setExpanded(index, not collapsed.get(group_id, False))

    def _remember_expansion(self, index: QModelIndex) -> None:
        if self._restoring or index.data(rail_model.KindRole) != GROUP:
            return
        group_id = index.data(rail_model.GroupIdRole)
        if group_id is not None:
            accounts_repo.set_group_collapsed(
                self._con, group_id, not self.isExpanded(index))

    # -------------------------------------------------------------- reload
    def reload(self, *, keep: str | None = None) -> None:
        """Rebuild from the store, then put back what a reset destroys."""
        key = keep if keep is not None else self.current_key()
        with self._quiet():
            self._model.rebuild()
            self._apply_expansion()
            if key:
                index = self._model.index_for_key(key)
                if index.isValid():
                    self.setCurrentIndex(index)

    def refresh_counts(self) -> None:
        self.reload()

    def _after_drop(self) -> None:
        self._apply_expansion()
        self.accounts_changed.emit()

    # -------------------------------------------------------- context menu
    def _context_menu(self, point) -> None:
        index = self.indexAt(point)
        kind = index.data(rail_model.KindRole) if index.isValid() else None
        menu = QMenu(self)

        if kind == ACCOUNT:
            self._account_menu(menu, index)
        elif kind == GROUP:
            self._group_menu(menu, index)
        elif kind == rail_model.CALENDAR:
            self._calendar_menu(menu, index)

        # ABOVE "New group…", because a group holds accounts and there is no
        # sense in the second before the first. It is offered on every kind,
        # including the "none yet" hint and the bare section header, which is
        # exactly where a right-click lands when there is nothing in the rail.
        menu.addAction("Add mail account…").triggered.connect(
            self.add_account_wanted.emit)
        menu.addAction("New group…").triggered.connect(self._new_group)
        menu.addSeparator()
        show_hidden = menu.addAction("Show hidden accounts")
        show_hidden.setCheckable(True)
        show_hidden.setChecked(self._model.show_hidden)
        show_hidden.toggled.connect(self._model.set_show_hidden)
        show_hidden.toggled.connect(lambda _: self._apply_expansion())
        menu.exec(self.viewport().mapToGlobal(point))

    def _calendar_menu(self, menu: QMenu, index: QModelIndex) -> None:
        """Ticking a calendar, and choosing a colour for it.

        The colour written here is the USER'S — `calendar.user_colour` — and
        never the provider's, which the next sync overwrites. store/calendars.py
        keeps the two apart and this is the only place the first is set.
        """
        calendar_id = int(index.data(rail_model.CalendarIdRole) or 0)
        if not calendar_id:
            return
        shown = not bool(index.data(rail_model.HiddenRole))
        action = menu.addAction("Show in the calendar")
        action.setCheckable(True)
        action.setChecked(shown)
        action.toggled.connect(
            lambda on, c=calendar_id: self._set_shown(c, on))
        menu.addAction("Colour…").triggered.connect(
            lambda _=False, c=calendar_id: self._pick_calendar_colour(c))
        reset = menu.addAction("Use the provider's colour")
        reset.triggered.connect(
            lambda _=False, c=calendar_id: self._set_calendar_colour(c, ""))
        menu.addSeparator()

    def _set_shown(self, calendar_id: int, shown: bool) -> None:
        calendars_repo.set_shown(self._con, calendar_id, shown)
        self.reload(keep=self.current_key())
        self.calendars_changed.emit()

    def _set_calendar_colour(self, calendar_id: int, colour: str) -> None:
        calendars_repo.set_user_colour(self._con, calendar_id, colour)
        self.reload(keep=self.current_key())
        self.calendars_changed.emit()

    def _pick_calendar_colour(self, calendar_id: int) -> None:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog

        calendar = calendars_repo.get_calendar(self._con, calendar_id)
        current = QColor(calendar.display_colour if calendar else "")
        chosen = QColorDialog.getColor(current, self, "Calendar colour")
        if chosen.isValid():
            self._set_calendar_colour(calendar_id, chosen.name())

    def _account_menu(self, menu: QMenu, index: QModelIndex) -> None:
        account_id = index.data(rail_model.AccountIdRole)
        hidden = bool(index.data(rail_model.HiddenRole))

        action = menu.addAction("Show in the rail" if hidden else "Hide from the rail")
        action.setToolTip("Hiding removes the account from the rail. Its mail "
                          "stays in the store and in search.")
        action.triggered.connect(
            lambda: self._set_hidden(account_id, not hidden))

        colours = menu.addMenu("Colour")
        current = index.data(rail_model.ColourRole)
        for value in accounts_repo.ACCOUNT_COLOURS:
            entry = colours.addAction(value)
            entry.setIcon(icons.icon("dot", value, 14, filled=True))
            entry.setCheckable(True)
            entry.setChecked(value.lower() == (current or "").lower())
            entry.triggered.connect(
                lambda _=False, v=value: self._set_colour(account_id, v))
        colours.addSeparator()
        colours.addAction("Custom…").triggered.connect(
            lambda: self._pick_colour(account_id, current))

        groups = menu.addMenu("Move to group")
        for group in accounts_repo.list_groups(self._con):
            entry = groups.addAction(group.name)
            entry.triggered.connect(
                lambda _=False, g=group.id: self._move_to_group(account_id, g))
        groups.addSeparator()
        groups.addAction("No group").triggered.connect(
            lambda: self._move_to_group(account_id, None))
        menu.addSeparator()

    def _group_menu(self, menu: QMenu, index: QModelIndex) -> None:
        group_id = index.data(rail_model.GroupIdRole)
        name = index.data(Qt.ItemDataRole.DisplayRole)
        menu.addAction("Rename group…").triggered.connect(
            lambda: self._rename_group(group_id, name))
        remove = menu.addAction("Delete group")
        remove.setToolTip("The group goes; its accounts become ungrouped. No "
                          "mail is touched.")
        remove.triggered.connect(lambda: self._delete_group(group_id))
        menu.addSeparator()

    # ------------------------------------------------------------- commands
    def _set_hidden(self, account_id: int, hidden: bool) -> None:
        accounts_repo.set_hidden(self._con, account_id, hidden)
        self.reload()
        self.accounts_changed.emit()

    def _set_colour(self, account_id: int, colour: str) -> None:
        accounts_repo.set_colour(self._con, account_id, colour)
        self.reload()
        self.accounts_changed.emit()

    def _pick_colour(self, account_id: int, current: str | None) -> None:
        chosen = QColorDialog.getColor(
            QColor(current or "#268bd2"), self, "Colour for this account")
        if chosen.isValid():
            self._set_colour(account_id, chosen.name())

    def _move_to_group(self, account_id: int, group_id: int | None) -> None:
        accounts_repo.move_account(self._con, account_id, group_id, 10_000)
        self.reload()
        self.accounts_changed.emit()

    def _new_group(self) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Name:")
        if ok and name.strip():
            accounts_repo.add_group(self._con, name.strip())
            self.reload()
            self.accounts_changed.emit()

    def _rename_group(self, group_id: int, current: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename group", "Name:", text=current)
        if ok and name.strip():
            accounts_repo.rename_group(self._con, group_id, name.strip())
            self.reload()

    def _delete_group(self, group_id: int) -> None:
        accounts_repo.delete_group(self._con, group_id)
        self.reload()
        self.accounts_changed.emit()
