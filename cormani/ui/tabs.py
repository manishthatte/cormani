# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tabs, and the view state each one holds.
#
# Thunderbird's tabs, and what makes them worth having is that a tab remembers
# the whole view and not just the folder: the scope, the quick filters, the
# search, the sort and which message was selected. Opening the unified inbox in
# one tab and "Owed, flagged, sorted by sender" in another is the point; a tab
# that only remembered the folder would put the second one back to the first
# every time it was left.
#
# So `ViewState` is the unit, and the tab bar is a thin thing that owns a list of
# them. Stage 5's calendar and stage 7's site panels become further kinds of
# state rather than a second tab mechanism.
#
# There is always at least one tab. Closing the last one is refused rather than
# leaving a window with no view in it, which is a state every other part of the
# interface would then have to handle.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from dataclasses import dataclass, field, replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QTabBar

from ..store import messages as messages_repo
from ..store import views as views_repo
from ..store import search as search_mod


@dataclass(frozen=True)
class ViewState:
    """Everything needed to put a view back exactly as it was."""

    title: str
    rail_key: str = "unified:inbox"
    scope: views_repo.Scope = field(default_factory=views_repo.Scope)
    filters: views_repo.Filters = field(default_factory=views_repo.Filters)
    sort: views_repo.Sort = field(default_factory=views_repo.Sort)
    # The search this tab is showing, if any. Beside the scope rather than
    # instead of it: clearing a search has to put the tab back where it was,
    # and a tab that had forgotten its rail row could only go to the inbox.
    search: search_mod.Query = field(default_factory=search_mod.Query)
    # Whether this tab groups its list into conversations. Per tab rather than
    # per window: "the inbox, threaded" and "everything from Lyle, flat" are two
    # views a person keeps side by side, which is what tabs are for.
    threaded: bool = True
    selected_id: int | None = None
    # Set when the tab is pinned to one message rather than to a view. The list
    # still shows the surrounding scope, so the tab is a bookmark rather than a
    # separate window — which is what makes "open in a tab and come back to it"
    # useful during a triage session.
    pinned_id: int | None = None
    # The calendar half of a tab. A tab showing the calendar is still a tab —
    # "the week" and "the inbox" are two things a person keeps open side by
    # side, which is what tabs are for — so its state lives here beside the
    # mail state rather than in a second kind of tab. `calendar_id` of None
    # means the tab is showing mail; 0 means every ticked calendar.
    calendar_id: int | None = None
    calendar_mode: str = "month"
    calendar_anchor: str = ""
    # The tracking half, and the same convention as the calendar's for the
    # same reason: None means this tab is not tracking, 0 means it is and no
    # thread is chosen. Stage 6 is a third kind of state rather than a third
    # kind of tab, which is what stage 5 established when the calendar became
    # the second.
    thread_id: int | None = None
    # And which SITE, if any. A string rather than the calendar's None/0/id
    # convention because a site has a name of its own and no numeric identity
    # — `panels/sites.py` keys everything on it. Empty means this tab is not
    # showing a panel.
    site_key: str = ""
    # The address book half, and the calendar's None/0/id convention for the
    # third time: None means this tab is not the address book, 0 means it is
    # and nobody is chosen. Stage 8's fourth kind of state rather than a fourth
    # kind of tab — which is what stage 5 established when the calendar became
    # the second, and the reason it keeps working is that the four are mutually
    # exclusive: they all claim the space the list and the reader occupy.
    contact_id: int | None = None

    @property
    def is_calendar(self) -> bool:
        return self.calendar_id is not None

    @property
    def is_tracking(self) -> bool:
        return self.thread_id is not None

    @property
    def is_site(self) -> bool:
        return bool(self.site_key)

    @property
    def is_contacts(self) -> bool:
        return self.contact_id is not None

    def with_changes(self, **changes) -> "ViewState":
        return replace(self, **changes)


class TabStrip(QTabBar):
    state_chosen = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setExpanding(False)
        self.setDocumentMode(True)
        # OFF until there are two tabs, and off HERE rather than only in
        # `_update_closable`: a tab created while this is true keeps the close
        # button's SLOT after the button itself is taken away, and the style
        # then paints a ✕ into it — over the middle of the tab's own text, the
        # moment the title is long enough to reach it. `Re: DWCNT wavelength
        # question` read as `Re: DW✕NT wavelength question`.
        self.setTabsClosable(False)
        self.setMovable(True)
        self.setDrawBase(False)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self._states: list[ViewState] = []
        self.currentChanged.connect(self._current_changed)
        self.tabCloseRequested.connect(self.close_tab)
        self.tabMoved.connect(self._tab_moved)

    # ----------------------------------------------------------------- state
    def add_state(self, state: ViewState, *, make_current: bool = True) -> int:
        index = self.addTab(state.title)
        self._states.append(state)
        self.setTabToolTip(index, state.title)
        if make_current:
            self.setCurrentIndex(index)
        self._update_closable()
        return index

    def state_at(self, index: int) -> ViewState | None:
        if 0 <= index < len(self._states):
            return self._states[index]
        return None

    def current_state(self) -> ViewState | None:
        return self.state_at(self.currentIndex())

    def replace_state(self, index: int, state: ViewState) -> None:
        """Write a tab's state back. Called as the user changes the view, so a
        tab is always current rather than current as of when it was last left."""
        if not 0 <= index < len(self._states):
            return
        self._states[index] = state
        if self.tabText(index) != state.title:
            self.setTabText(index, state.title)
            self.setTabToolTip(index, state.title)

    def update_current(self, **changes) -> ViewState | None:
        index = self.currentIndex()
        state = self.state_at(index)
        if state is None:
            return None
        updated = state.with_changes(**changes)
        self.replace_state(index, updated)
        return updated

    # --------------------------------------------------------------- closing
    def close_tab(self, index: int) -> bool:
        if len(self._states) <= 1:
            return False
        del self._states[index]
        self.removeTab(index)
        self._update_closable()
        return True

    def close_current(self) -> bool:
        return self.close_tab(self.currentIndex())

    def _update_closable(self) -> None:
        # A single tab shows no close button: it cannot be closed, and a button
        # that does nothing is worse than no button.
        self.setTabsClosable(len(self._states) > 1)

    # --------------------------------------------------------------- cycling
    def step(self, delta: int) -> None:
        if self.count() < 2:
            return
        self.setCurrentIndex((self.currentIndex() + delta) % self.count())

    def _current_changed(self, index: int) -> None:
        if 0 <= index < len(self._states):
            self.state_chosen.emit(index)

    def _tab_moved(self, from_index: int, to_index: int) -> None:
        if 0 <= from_index < len(self._states):
            self._states.insert(to_index, self._states.pop(from_index))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0 and self.close_tab(index):
                event.accept()
                return
        super().mousePressEvent(event)
