# SPDX-License-Identifier: GPL-3.0-or-later
#
# The three panes, and the wiring between them.
#
# This is where the rail, the quick filter, the list and the reader are made to
# behave as one thing. It owns the message model, because the model is what all
# four of them are views of, and it owns the action dispatch, because the same
# four actions arrive from a hover button, a command bar button and a key.
#
# ONE PLACE DECIDES WHAT AN ACTION MEANS — `run_action`. A hover click, the
# command bar and the keyboard all call it. The alternative is three
# implementations of "archive" that differ in whether they refresh the rail.
#
# AUTO MARK-READ IS SUSPENDED WHILE THE UNREAD FILTER IS ON. Selecting a message
# normally marks it read, as both Outlook and Thunderbird do. But with "Unread"
# pressed, that would delete the row out from under the cursor the instant it
# was selected, and then select the next one, and mark that read too — a filter
# that empties itself as you look at it. So while that filter is on, reading is
# an explicit act.
#
# A SEARCH IS A STATE OF THIS PANE, NOT A PLACE. The box lives in the window,
# but what it produces lands here, beside the scope and the filters — so the
# rail row the user came from is still known, and clearing the search puts them
# back on it rather than in the inbox. Three things therefore leave a search:
# clearing the box, Escape in it, and choosing anything in the rail. The last is
# why `search_changed` exists — the bar has to be told when something other than
# the bar ended the search, or it would go on showing a query nobody is running.
#
# THE STATUS LINE IS EARNED, NOT DECORATIVE. It reports what an action actually
# did, including the awkward case: archiving four messages when one account has
# no archive folder says so, rather than reporting four.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QFrame, QLabel, QSplitter, QVBoxLayout, QWidget)

from ..platform import desktop
from ..store import edits as edits_repo
from ..store import messages as messages_repo
from . import listfooter
from . import shortcuts as shortcuts_mod
from .actions import Actions
from .messagelist import MessageList
from .models import messages as message_model
from .quickfilter import QuickFilterBar
from .rail import RailView
from .calendarhost import CalendarHost
from .contacthost import ContactHost
from .sitehost import SiteHost
from .trackhost import TrackHost
from . import panequery
from . import panespace
from . import panestate
from .reader import Reader
from .tabs import ViewState



class MailPane(QWidget):
    status_message = Signal(str)
    counts_changed = Signal(int, int)          # loaded, total
    view_changed = Signal()                    # the tab's state should be saved
    open_in_tab = Signal(int)                  # message id
    search_changed = Signal(object)            # search.Query, for the bar
    outbox_changed = Signal()                  # something is waiting to go out

    def __init__(self, con: sqlite3.Connection, *, page_size: int = 200,
                 attachments_root=None, attachment_cache=None,
                 dialogs=None, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._theme = None
        self._restoring = False

        # The order to go back to when a search ends, and whether the user has
        # since chosen one of their own. Relevance is the right default for a
        # search and the wrong thing to impose on someone who has just asked
        # for "by sender".
        self._sort_before_search = None
        self._sort_chosen_in_search = False
        # What the footer needs beyond the counts the model emits — how much a
        # search is holding back, and how many conversations the view holds —
        # cached against the query it belongs to. `ui/listfooter.py` owns both
        # the numbers and the sentences they go into.
        self._counts = listfooter.Counts(con)
        # Where inline images may be read from, and nowhere else. None in the
        # tests and in demo mode, which is why an absent root means no inline
        # image is served rather than a fallback that serves any file.
        self._attachments_root = attachments_root
        # Where a copy is put to be handed to another program. CACHE, and None
        # in the tests and in demo mode, in which case the strip says it has
        # nowhere to put one rather than choosing a directory of its own.
        self._attachment_cache = attachment_cache

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setChildrenCollapsible(False)

        self.rail = RailView(con, self)
        self.splitter.addWidget(self.rail)

        middle = self.middle = QWidget(self)
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(4)
        self.quick_filter = QuickFilterBar(con, middle)
        middle_layout.addWidget(self.quick_filter)
        self.list = MessageList(middle)
        middle_layout.addWidget(self.list, 1)
        self.list_footer = QLabel("", middle)
        self.list_footer.setFrameShape(QFrame.Shape.NoFrame)
        middle_layout.addWidget(self.list_footer)
        self.splitter.addWidget(middle)

        self.reader = Reader(self, con=con)
        self.splitter.addWidget(self.reader)

        # The calendar goes WHERE THE LIST AND THE READER ARE, which PLAN.txt
        # §3 asks for and which is why it is a sibling in this splitter rather
        # than a window or a tab of its own: the rail stays, and selecting a
        # calendar in it swaps the other two panes for this one.
        # Both calendar panes, and the switching between them and the mail.
        self.calendars = CalendarHost(self)

        # The tracking pane, and the reading pane's strip. One host for both,
        # because "Log call" appears on each and must mean the same thing —
        # `ui/trackhost.py` says why that is not a tidiness argument.
        self.tracking = TrackHost(self, dialogs=dialogs)

        # And the site panels. Made on first use rather than now: a
        # QWebEngineView is a browser, and six of them at start-up is six
        # render processes to show a window somebody opened to read mail.
        self.sites = SiteHost(self)

        # And the address book — the fourth thing that claims the space the
        # list and the reader occupy. `ui/contactpane.py` says why it is a tab
        # rather than a rail section, and `ui/contacthost.py` owns what each of
        # its commands means. The dialogs come from the same injection the
        # tracking layer's do, so that the suite can drive it without a click.
        self.contacts = ContactHost(self, dialogs=dialogs)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setStretchFactor(2, 3)
        self.splitter.setSizes([250, 420, 640])
        layout.addWidget(self.splitter, 1)

        self.model = message_model.MessageModel(con, page_size=page_size, parent=self)
        self.list.setModel(self.model)

        self.reader.invitation_answered.connect(
            lambda response: self.actions.answer_invitation(response))
        self.reader.invitation_dismissed.connect(
            lambda: self.actions.dismiss_invitation())
        self.rail.scope_chosen.connect(self._scope_chosen)
        self.rail.calendar_chosen.connect(self._calendar_chosen)
        self.rail.site_chosen.connect(self._site_chosen)
        self.rail.view_chosen.connect(self._view_chosen)
        self.rail.calendars_changed.connect(self._calendars_changed)
        self.rail.accounts_changed.connect(self._accounts_changed)
        self.quick_filter.changed.connect(self._filters_changed)
        self.list.action_requested.connect(self.run_action)
        self.list.open_requested.connect(self.open_in_tab)
        # THE COMMAND BAR REACHED NOTHING UNTIL NOW. `Reader.command` was
        # emitted and connected nowhere, so Archive, Flag, Mark read and Delete
        # above the reading pane did nothing at all, while the same four worked
        # from the list's hover actions and from the keyboard — which is why it
        # was not noticed. Found while wiring the attachment strip's status
        # signal into the same block.
        self.reader.command.connect(self._reader_command)
        self.reader.status_message.connect(self.status_message)
        self.reader.link_activated.connect(self._open_link)
        self.reader.inline.send_requested.connect(
            lambda text: self.actions.inline_reply(text))
        self.reader.inline.expand_requested.connect(
            lambda text: self.compose("reply", prefill=text))
        self.model.counts_changed.connect(self._counts_changed)
        selection = self.list.selectionModel()
        if selection is not None:
            selection.currentChanged.connect(lambda *_: self._selection_changed())

        self.actions = Actions(self)
        self._install_list_shortcuts()
        self._update_empty_text()
        # The model loaded before these signals were connected, so its first
        # count went nowhere. Said again rather than reordered: the model has to
        # exist before the view it is set on.
        self._counts_changed(self.model.rowCount(), self.model.total)

    # ------------------------------------------------------------- shortcuts
    def _install_list_shortcuts(self) -> None:
        """Bound to the list itself. ui/shortcuts.py explains why not the window."""
        for shortcut in shortcuts_mod.in_scope(shortcuts_mod.SCOPE_LIST):
            action = QAction(shortcut.label, self.list)
            action.setShortcut(QKeySequence(shortcut.key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.setEnabled(shortcut.ready)
            action.triggered.connect(
                lambda _=False, s=shortcut.id: self.run_shortcut(s))
            self.list.addAction(action)

    def run_shortcut(self, shortcut_id: str) -> None:
        """Act on a keyboard shortcut, or on the menu entry that mirrors it."""
        if shortcut_id == "next_unread":
            self._go_unread(forward=True)
        elif shortcut_id == "prev_unread":
            self._go_unread(forward=False)
        elif shortcut_id == "filter_slash":
            self.quick_filter.focus_text()
        elif shortcut_id == "open_tab":
            row = self.current_row()
            if row is not None:
                self.open_in_tab.emit(row.id)
        else:
            self.run_action(shortcut_id, self.selected_ids())

    def _go_unread(self, *, forward: bool) -> None:
        target = self.model.next_unread(self.list.currentIndex(), forward=forward)
        if not target.isValid():
            self.status_message.emit(
                "No more unread messages" if forward else "No earlier unread messages")
            return
        self.list.select(target)

    # ------------------------------------------------------------- calendar
    # The calendar and the agenda pane are `ui/calendarhost.py`'s, for the
    # reason `ui/actions.py` gives about itself: deciding what a command means
    # — or which half of the window is showing — is not the same job as
    # arranging four widgets, and the file that did both is the one the
    # 600-line rule caught. What is left here is the vocabulary the rest of
    # the window already speaks.
    @property
    def calendar(self):
        return self.calendars.calendar

    @property
    def agenda(self):
        return self.calendars.agenda

    def showing_calendar(self) -> bool:
        return self.calendars.showing

    def show_calendar(self) -> None:
        self.calendars.open()

    def calendar_mode(self, mode: str) -> None:
        self.calendars.mode(mode)

    def calendar_action(self, name: str) -> None:
        self.calendars.action(name)

    def set_agenda_visible(self, on: bool) -> None:
        self.calendars.set_agenda_visible(on)

    def agenda_visible(self) -> bool:
        return self.calendars.agenda_wanted

    # ------------------------------------------------------------- tracking
    # `ui/trackhost.py` owns the pane, the strip and what their buttons mean.
    # These are the verbs the menu and the keyboard reach it by.
    def show_tracking(self) -> None:
        self.tracking.open()
        self.view_changed.emit()

    def showing_tracking(self) -> bool:
        return self.tracking.showing

    def track_this(self) -> None:
        """Make a thread from the message being read. The commonest way in —
        `ui/trackingstrip.py` says why a blank New Thread dialog is not."""
        message_id = self.reader.message_id()
        if not message_id:
            self.status_message.emit(
                "Open a message first — a thread is made from one.")
            return
        if self.tracking.track_message(message_id):
            self.view_changed.emit()

    def tracking_action(self, name: str) -> None:
        self.tracking.action(name)

    # ---------------------------------------------------------- address book
    # `ui/contacthost.py` owns the pane and what its commands mean. These are
    # the verbs the menu and the keyboard reach it by.
    def show_contacts(self) -> None:
        self.contacts.open()
        self.view_changed.emit()

    def showing_contacts(self) -> bool:
        return self.contacts.showing

    def add_sender_to_contacts(self) -> None:
        """Make a card from the message being read. The commonest way a
        contact gets made in any client — `ui/contacthost.add_from_message`
        says why it has to cost one keystroke."""
        message_id = self.reader.message_id()
        if not message_id:
            self.status_message.emit(
                "Open a message first — a contact is made from its sender.")
            return
        if self.contacts.add_from_message(message_id):
            self.view_changed.emit()

    def contacts_action(self, name: str) -> None:
        self.contacts.action(name)

    # ------------------------------------------------- who owns the space
    def showing_mail(self) -> bool:
        """Whether the list and the reading pane are what is on screen.

        ONE PREDICATE, AND NOT A LIST OF THE OTHERS BY NAME. Four hosts claim
        this space — the calendar, tracking, the sites and the address book —
        and there will be a fifth. `ui/viewhost.save_current` asked three of
        them by name and was silently wrong the moment the address book became
        the fourth: Save this search from the address book would have saved a
        search of a mail view nobody was looking at, under a name that
        described it. Asking what IS showing rather than listing what is not
        is the shape that survives the fifth, and `ui/panespace.CLAIMANTS` is
        where the fifth would be written down.
        """
        return not panespace.showing(self)

    # ---------------------------------------------------------------- sites
    # `ui/sitehost.py` owns the panels. These are the verbs the rail and the
    # window reach them by.
    def _site_chosen(self, site_key: str, _node_key: str) -> None:
        self.sites.chosen(site_key)
        if not self._restoring:
            self.view_changed.emit()

    def showing_site(self) -> str:
        return self.sites.showing

    def show_site(self, site_key: str) -> bool:
        """Open one site's panel. The Panels menu's verb.

        THROUGH THE RAIL AND NOT STRAIGHT TO THE HOST, so that the rail's
        selection follows the panel. A panel shown with no row highlighted
        leaves the window disagreeing with itself about what it is showing —
        and `ui/panestate.py` records the RAIL's key, so a tab restored after
        the fact would come back to the mail rather than to the site.
        """
        from ..panels import sites as sites_mod

        return self.rail.select_key(sites_mod.rail_key(site_key))

    def open_link(self, url: str) -> None:
        """A URL from a panel — a popup it wanted, or a download it was
        refused — handed to the desktop through the same door a message's
        links use. Public because `ui/sitepanel.py` has no other way out, and
        deliberately the SAME door: the scheme is checked there, once, for
        everything that leaves."""
        self._open_link(url)

    def _view_chosen(self, view_id: int, _node_key: str) -> None:
        """A saved search, from the rail. `ui/viewhost.py` says what that
        means — including why it must not go through `_scope_chosen`."""
        from . import viewhost

        viewhost.apply_view(self, view_id)

    def _calendar_chosen(self, calendar_id: int, key: str) -> None:
        # The stand-down is `CalendarHost.show`'s own now, through
        # `ui/panespace.py`. It was done here as well as there, and the two
        # lists disagreed — which is how a direct `calendars.show(True)`, the
        # thing restoring a tab does, could leave the tracking board underneath.
        self.calendars.chosen(calendar_id)
        if not self._restoring:
            self.view_changed.emit()

    def _calendars_changed(self) -> None:
        self.calendars.refresh()

    def _calendar_view_changed(self) -> None:
        if not self._restoring:
            self.view_changed.emit()

    # ------------------------------------------------------------- the query
    # `ui/panequery.py` holds all six, and its header says why the seam is
    # there: what a tab REMEMBERS about the query is `ui/panestate.py` and what
    # CHANGES it is that file, and the two are siblings. The section header
    # these methods belong under had been left behind by an earlier move and
    # named nothing for two stages, which is how the rule found them.
    def _scope_chosen(self, scope, key: str) -> None:
        panequery.scope_chosen(self, scope, key)

    def _filters_changed(self, filters) -> None:
        panequery.filters_changed(self, filters)

    def set_sort(self, sort) -> None:
        panequery.set_sort(self, sort)

    def set_threaded(self, on: bool) -> None:
        panequery.set_threaded(self, on)

    def set_search(self, query) -> None:
        panequery.set_search(self, query)

    def _accounts_changed(self) -> None:
        panequery.accounts_changed(self)

    def reload(self) -> None:
        """The store changed underneath this pane — re-ask it everything.

        Used after a sync, which writes over a DIFFERENT connection on a worker
        thread. Nothing here can be incrementally patched: messages arrived,
        flags moved and rows left, and the model has no way to know which.
        """
        self._forget_discarded()
        self.rail.reload(keep=None)
        self.model.refresh()
        self._update_empty_text()
        self._selection_repaint()

    def _selection_repaint(self) -> None:
        """The command bar follows the cursor. Here rather than in the action
        dispatch: it is about what the reading pane shows."""
        self.reader.commands.set_message(self.current_row())

    def _update_empty_text(self) -> None:
        self.list.set_empty_text(listfooter.empty_text(
            search=self.model.search, filters=self.model.filters,
            scope=self.model.scope))

    def _counts_changed(self, loaded: int, total: int) -> None:
        discarded, conversations = (
            self._counts.of(self.model)
            if (self.model.search.active or self.model.grouping) else (0, 0))
        self.list_footer.setText(listfooter.footer_text(
            loaded=loaded, total=total, search=self.model.search,
            grouping=self.model.grouping, discarded=discarded,
            conversations=conversations))
        self.counts_changed.emit(loaded, total)

    def _forget_discarded(self) -> None:
        self._counts.forget()

    # ------------------------------------------------------------- selection
    def clear_selection(self) -> None:
        self.list.clear()
        self.reader.clear()

    # The cursor belongs to the VIEW — ui/messagelist.py says why, and these
    # are the pane's side of it: the reader has to be told, and the window and
    # the tests talk to the pane.
    def select_message(self, message_id: int) -> bool:
        return self.list.select_message(message_id)

    def select_row(self, position: int) -> bool:
        return self.list.select_row(position)

    def selected_ids(self) -> list[int]:
        return self.list.selected_ids()

    def current_row(self):
        return self.list.current_row()

    def _reader_command(self, action_id: str) -> None:
        """The reading pane's buttons, on the selection.

        On the SELECTION rather than on the message shown, so that the bar, the
        keyboard and the list's hover actions all mean the same thing. They
        differ only when several rows are selected and the pane is showing one
        of them, and there the selection is what the user said.
        """
        self.run_action(action_id, self.selected_ids())

    def _open_link(self, url: str) -> None:
        """A link clicked in a message body, handed to the desktop.

        The sanitiser already allowed only http, https, mailto and tel into the
        document, and `platform.desktop` checks the scheme again at the door.
        Failure is reported rather than swallowed: a link that silently does
        nothing reads as a broken message.
        """
        try:
            desktop.open_url(url)
        except desktop.OpenFailed as exc:
            self.status_message.emit(str(exc))
            return
        self.status_message.emit(f"Opened {url}")

    def _selection_changed(self) -> None:
        row = self.current_row()
        if row is None:
            self.reader.clear()
            return
        self.reader.show_message(
            row, messages_repo.bodies_of(self._con, row.id),
            messages_repo.attachments_of(self._con, row.id),
            attachments_root=self._attachments_root,
            attachment_cache=self._attachment_cache,
            invitation=self.actions.invitation_on(row.id))
        if not row.seen and not self.model.filters.unread:
            edits_repo.set_seen(self._con, [row.id], True)
            self.model.apply_change([row.id])
            self.rail.refresh_counts()
        if not self._restoring:
            self.view_changed.emit()

    # ---------------------------------------------------------------- actions
    def run_action(self, action_id: str, message_ids: list[int]) -> None:
        """Every command goes through ui/actions.py, which says why."""
        self.actions.run(action_id, message_ids)

    def undo(self) -> bool:
        return self.actions.undo()

    def apply_tag(self, tag_id: int) -> None:
        self.actions.apply_tag_id(tag_id, self.selected_ids())

    # -------------------------------------------------------------- writing
    def compose(self, kind: str, message_id: int | None = None,
                prefill: str = "", to: str = ""):
        """Open a composer. ui/actions.py holds them, and says why."""
        return self.actions.compose(kind, message_id, prefill, to=to)

    # ------------------------------------------------------------ view state
    # `ui/panestate.py` holds all three, and its header says why the seam is
    # here: what a tab REMEMBERS is a claim about the pane rather than part of
    # arranging it, and restoring without emitting is the delicate half.
    def view_state(self, title: str) -> ViewState:
        return panestate.view_state(self, title)

    def restore(self, state: ViewState) -> None:
        return panestate.restore(self, state)

    def title_for_scope(self) -> str:
        return panestate.title_for_scope(self)

    # --------------------------------------------------------------- theming
    def apply_theme(self, theme) -> None:
        self._theme = theme
        self.rail.set_theme(theme)
        self.list.set_theme(theme)
        self.reader.apply_theme(theme)
        self.quick_filter.apply_theme(theme)
        self.calendars.apply_theme(theme)
        self.tracking.apply_theme(theme)
        self.contacts.apply_theme(theme)
        self.reader.invitation.apply_theme(theme)
        self.list_footer.setStyleSheet(
            f"color: {theme.text_muted}; padding: 2px 6px;")

    def set_density(self, density) -> None:
        self.rail.set_density(density)
        self.list.set_density(density)
