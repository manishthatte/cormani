# SPDX-License-Identifier: GPL-3.0-or-later
#
# The main window: search across the top, then tabs, then the three panes.
#
# The window owns the frame and the things that are true of the whole
# application — the menus, the tab strip, the status bar, the theme, the density
# and the window geometry. It owns no mail. The three panes and everything about
# them are ui/mailpane.py, which is what keeps this file from becoming the god
# file CONVENTIONS.txt §4 forbids.
#
# MENU ITEMS THAT MIRROR A LIST KEY CARRY NO SHORTCUT. `Archive` in the Message
# menu shows `A` in the shortcut column, written into the label after a tab
# character, but does not install it. Installing it would put a second QAction
# on the same key at a different context, and when the list has focus Qt would
# find two candidates and report an ambiguous shortcut — at which point neither
# fires. The key itself lives on the list; see ui/shortcuts.py for why it lives
# there rather than here.
#
# TWO KINDS OF STATE, IN TWO PLACES, ON PURPOSE. Anything about this window —
# its geometry, the splitter, the density — is QSettings. Anything about the
# accounts — their order, colour, group and whether a group is collapsed — is the
# database. The rule is who the state belongs to: a second machine syncing the
# store should get the user's account arrangement and not the user's window
# size.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (QApplication, QLabel, QMainWindow, QMenuBar,
                               QStatusBar, QVBoxLayout, QWidget)

from .. import APP_NAME
from ..store import messages as messages_repo
from ..store import views as views_repo
from ..store import search as search_mod
from ..store import tags as tags_repo
from ..smtp import outbox as outbox_repo
from . import density as density_mod
from . import help as help_mod
from . import icons
from . import menus
from . import shortcuts as shortcuts_mod
from . import tagsdialog
from . import theme as theme_mod
from .mailpane import MailPane
from .searchbar import SearchBar
from .tabs import TabStrip, ViewState

SETTINGS_GEOMETRY = "window/geometry"
SETTINGS_STATE = "window/state"
SETTINGS_SPLITTER = "window/splitter"
SETTINGS_DENSITY = "view/density"
SETTINGS_THEME = "view/theme"
SETTINGS_AGENDA = "view/agenda_pane"



class MainWindow(QMainWindow):
    # Kept as class attributes: the previous stage's tests name them, and they
    # are the sort of constant that gets duplicated the moment it is not.
    SETTINGS_GEOMETRY = SETTINGS_GEOMETRY
    SETTINGS_STATE = SETTINGS_STATE
    SETTINGS_SPLITTER = SETTINGS_SPLITTER

    def __init__(self, store: sqlite3.Connection, *, settings=None,
                 theme_key: str | None = None, demo: bool = False,
                 sync=None, attachments_root=None,
                 attachment_cache=None) -> None:
        super().__init__()
        self._store = store
        self._settings = settings
        self._demo = demo
        self._engine_note = ""
        # None in demo mode and in the tests: there is no server behind demo
        # data, and the menu says so rather than offering a key that does
        # nothing. `app.py` supplies the real one.
        self._sync = sync
        self._calendar_sync = None
        self._reminders = None
        self.setWindowTitle(f"{APP_NAME} — demo data" if demo else APP_NAME)
        self.setMinimumSize(960, 600)

        page_size = getattr(settings, "list_page_size", 200)
        self.mail = MailPane(store, page_size=page_size,
                             attachments_root=attachments_root,
                             attachment_cache=attachment_cache, parent=self)

        self._build_body()
        self._build_menus()
        self._build_status()

        self.mail.status_message.connect(self.status_message.setText)
        self.mail.view_changed.connect(self._save_tab_state)
        self.mail.open_in_tab.connect(self._open_message_tab)
        # The bar is a VIEW of the pane's search, not the owner of it. Anything
        # that ends a search — a rail click, switching tabs — comes back here,
        # so the box never shows a query that is not being run.
        self.mail.search_changed.connect(self._search_changed)
        self.mail.outbox_changed.connect(self._outbox_changed)
        self.mail.rail.accounts_changed.connect(self.search.reload_accounts)
        self.tabs.state_chosen.connect(self._tab_chosen)

        saved = QSettings().value(SETTINGS_THEME)
        self._theme_key = theme_key or (saved if isinstance(saved, str) else None)
        self.apply_theme(self._theme_key)
        self.set_density(self._saved_density(), remember=False)

        self.tabs.add_state(ViewState(title=self.mail.title_for_scope()))
        self._restore_geometry()

    # ------------------------------------------------------------------- body
    def _build_body(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 4)
        outer.setSpacing(6)

        # Full width, and above the tabs, because it is not about the tab: it
        # searches every account whatever this tab is showing, and what it finds
        # lands in whichever tab asked. ui/searchbar.py owns the controls.
        self.search = SearchBar(self._store, central)
        self.search.changed.connect(self.mail.set_search)
        outer.addWidget(self.search)

        self.tabs = TabStrip(central)
        outer.addWidget(self.tabs)
        outer.addWidget(self.mail, 1)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------ menus
    def _build_menus(self) -> None:
        menus.build(self)

    # ----------------------------------------------------------------- status
    def _build_status(self) -> None:
        bar = QStatusBar(self)
        self.status_message = QLabel("")
        bar.addWidget(self.status_message, 1)
        self.status_counts = QLabel("")
        bar.addPermanentWidget(self.status_counts)
        self.status_outbox = QLabel("")
        bar.addPermanentWidget(self.status_outbox)
        self.status_store = QLabel("")
        bar.addPermanentWidget(self.status_store)
        self.setStatusBar(bar)
        self.mail.counts_changed.connect(self._counts)
        # The pane counted before this connection existed. Same reason as the
        # matching line in MailPane.__init__.
        self._counts(self.mail.model.rowCount(), self.mail.model.total)
        self._show_outbox()
        if self._demo:
            self.status_message.setText(
                "Demo data. This is a disposable store in the cache directory; "
                "your real mail store is untouched.")

    def _outbox_changed(self) -> None:
        """Something was queued. Say so, and try to send it now.

        Now rather than at the next scheduled sync, because a person who
        pressed Send meant now. If a sync is already running the message goes
        with that one; if there is no server behind this store — demo data —
        the count stands and says what is waiting.
        """
        self._show_outbox()
        if self._sync is not None:
            self.sync_now()

    def _show_outbox(self) -> None:
        waiting = outbox_repo.waiting(self._store)
        self.status_outbox.setText(
            f"{waiting} waiting to send" if waiting else "")

    def _counts(self, loaded: int, total: int) -> None:
        # NOT the number of messages in view — the list's own footer already
        # says that, and a status bar repeating the thing directly above it is
        # a status bar people stop reading. Unread across every visible account
        # is the number that is not otherwise on screen.
        unread = sum(messages_repo.unread_counts(self._store).values())
        self.status_counts.setText(f"{unread} unread" if unread else "")

    def set_store_summary(self, text: str) -> None:
        self.status_store.setText(text)

    def set_engine_note(self, text: str) -> None:
        self._engine_note = text

    def _show_agenda(self, on: bool) -> None:
        self.mail.set_agenda_visible(on)
        QSettings().setValue(SETTINGS_AGENDA, bool(on))

    # ------------------------------------------------------------------ sync
    def sync_now(self) -> bool:
        """F5. Returns whether a sync actually started.

        Pressing it during one is not an error and not a queue: two engines
        against one store would fight over the offline queue and re-fetch each
        other's work. The status bar says so and nothing else happens.
        """
        started = False
        if self._sync is not None and self._sync.start():
            started = True
        elif self._sync is not None:
            self.status_message.setText("A sync is already running…")
        # The calendars go with the mail: F5 means "everything, now", and a
        # calendar that only refreshed when somebody found the calendar pane
        # would be a day stale every time it was opened.
        if self._calendar_sync is not None:
            started = self._calendar_sync.start() or started
        return started

    def _sync_started(self) -> None:
        self.act_sync.setEnabled(False)
        self.status_message.setText("Fetching mail…")

    def _sync_finished(self, summary: str, ok: bool) -> None:
        from . import notifyhost

        self.act_sync.setEnabled(True)
        self.status_message.setText(summary)
        self._show_outbox()
        # Unconditionally, even when the sync failed: an account that failed
        # after three others succeeded still leaves three accounts' new mail on
        # the disk and not on the screen.
        self.mail.reload()
        self._counts(self.mail.model.rowCount(), self.mail.model.total)
        notifyhost.refresh_unread(self)

    def start_reminders(self) -> None:
        """Watch for a meeting about to start.

        Not in `__init__`: a window built for a test must not start a timer,
        and demo data must not remind anybody about a fixture. `app.py` calls
        this for a real store and nothing else does.
        """
        from .reminders import Reminders

        self._reminders = Reminders(self._store, parent=self)
        self._reminders.fired.connect(self._reminded)
        self._reminders.start()

    def _reminded(self, _event_id: int, title: str, body: str,
                  sent: bool) -> None:
        """The status bar is the fallback, not the point.

        `platform/notify.py` reports whether it could SEND, never whether
        anybody saw it — so when there is no notification service on this
        desktop the same words go where they can at least be found.
        """
        self.status_message.setText(f"{title} — {body}" if body else title)

    def attach_sites(self, keys, *, user_agent: str = "") -> None:
        """Which site panels this window offers, and what they claim to be.

        Called once at start-up. An empty `keys` is a real answer — the panels
        are optional, and docs/toolkit-verification.txt finding 2 is why that
        is a requirement rather than a preference: the embedded Chromium is
        pinned by Debian and will eventually be refused by these sites, and
        mail and calendar must be unaffected when it is.
        """
        self.mail.sites.set_user_agent(user_agent)
        self.mail.rail.set_sites(keys)
        self._build_sites_menu(keys)

    def _build_sites_menu(self, keys) -> None:
        """The Panels menu: open one, and sign out of one.

        SIGNING OUT NEEDED A SURFACE. `panels/profiles.forget` was written at
        stage 7 and had no caller at all — the same gap `contacts.note_bounce`
        sat in from stage 4 until stage 6, and worth closing before it becomes
        a habit. A person who signs into WhatsApp on this machine and later
        wants that session GONE had, until now, no way to say so from inside
        the application.
        """
        from ..panels import sites as sites_mod

        menu = self.sites_menu
        menu.clear()
        keys = [k for k in (keys or []) if sites_mod.get(k) is not None]
        menu.menuAction().setVisible(bool(keys))
        if not keys:
            return

        for key in keys:
            site = sites_mod.get(key)
            action = menu.addAction(site.name)
            action.triggered.connect(
                lambda _=False, k=key: self.mail.show_site(k))

        menu.addSeparator()
        out = menu.addMenu("Sign &out of")
        for key in keys:
            site = sites_mod.get(key)
            action = out.addAction(f"{site.name}…")
            action.triggered.connect(
                lambda _=False, k=key: self.sign_out_of_site(k))

    def sign_out_of_site(self, key: str, *, confirm=None) -> bool:
        """Destroy one site's stored session, having asked first.

        ASKED, BECAUSE IT CANNOT BE UNDONE AND IS NOT CHEAP TO REVERSE. Signing
        back into WhatsApp Web means fetching the telephone and scanning a code;
        LinkedIn may post a fresh code by mail. That is a different order of
        cost from the actions that go through `store/undo.py`, and there is
        nothing to undo here — the cookies are gone from disk.

        `confirm` is injected for the same reason `configure.add_account` takes
        its prompts: the suite has no display and cannot dismiss a dialog.
        """
        from ..panels import sites as sites_mod

        site = sites_mod.get(key)
        if site is None:
            return False
        ask = confirm if confirm is not None else self._confirm_sign_out
        if not ask(site):
            return False
        had = self.mail.sites.sign_out(site.key)
        self.status_message.setText(
            f"Signed out of {site.name}." if had
            else f"There was no {site.name} session to sign out of.")
        return True

    def _confirm_sign_out(self, site) -> bool:            # pragma: no cover
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self, f"Sign out of {site.name}?",
            f"This deletes the {site.name} session stored on this computer. "
            f"You will have to sign in again the next time you open the "
            f"panel, and corMani cannot undo it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def attach_calendar_sync(self, controller) -> None:
        """The calendar's own controller. Separate from the mail's, and
        `ui/calsyncing.py` says why."""
        self._calendar_sync = controller
        controller.progressed.connect(self.status_message.setText)
        controller.finished.connect(self._calendar_sync_finished)
        self.mail.calendar.fetch_requested.connect(self._fetch_range)

    def _calendar_sync_finished(self, summary: str, ok: bool) -> None:
        self.status_message.setText(summary)
        self.mail.calendars.refresh()
        self.mail.rail.reload(keep=self.mail.rail.current_key())

    def _fetch_range(self, request) -> None:
        """A view drew a range the store does not hold, and said so.

        Quiet when one is already running: the signal fires on every
        navigation, and a message about it each time would be noise about
        something that is already being handled.
        """
        if self._calendar_sync is not None:
            self._calendar_sync.fetch(request)

    def attach_sync(self, controller) -> None:
        """Give the window a sync controller and wire its signals."""
        from . import notifyhost

        self._sync = controller
        controller.started.connect(self._sync_started)
        controller.progressed.connect(self.status_message.setText)
        controller.results_ready.connect(
            lambda results: notifyhost.on_results(self, results))
        controller.finished.connect(self._sync_finished)
        self.act_sync.setEnabled(True)
        self.act_sync.setStatusTip(
            shortcuts_mod.by_id("sync").description)

    def attach_tray(self) -> None:
        """The system tray, when the desktop has one. Called once from app.run."""
        from . import notifyhost
        notifyhost.attach(self)

    def quit_application(self) -> None:
        """File ▸ Quit — end the process, even when a tray would otherwise hide."""
        self._force_quit = True
        self.close()

    def _about(self) -> None:
        help_mod.AboutDialog(self, engine=self._engine_note,
                             store=self.status_store.text()).exec()

    # ------------------------------------------------------------------- tabs
    def _new_tab(self) -> None:
        state = self.mail.view_state(self.mail.title_for_scope())
        self.tabs.add_state(state)

    def _open_message_tab(self, message_id: int) -> None:
        row = messages_repo.get_row(self._store, message_id)
        title = row.subject_label if row else "Message"
        state = self.mail.view_state(title[:40]).with_changes(
            title=title[:40], selected_id=message_id, pinned_id=message_id)
        self.tabs.add_state(state)

    def _tab_chosen(self, index: int) -> None:
        state = self.tabs.state_at(index)
        if state is not None:
            self.mail.restore(state)
            self._sync_sort_menu()

    def _build_tags_menu(self) -> None:
        """The tags, with the key each one answers to.

        The key is SHOWN and not installed here, for the reason the top of this
        file gives about the Message menu: the digits are bound to the list, and
        a second QAction on the same key in an overlapping context makes Qt fire
        neither.
        """
        self.tags_menu.clear()
        for tag in tags_repo.list_tags(self._store):
            action = QAction(
                f"{tag.name}\t{tag.shortcut}" if tag.shortcut else tag.name, self)
            action.setIcon(icons.icon("tag", tag.colour or "#888888", 14,
                                      filled=True))
            action.triggered.connect(
                lambda _=False, t=tag.id: self.mail.apply_tag(t))
            self.tags_menu.addAction(action)
        if not self.tags_menu.actions():
            empty = self.tags_menu.addAction("No tags yet")
            empty.setEnabled(False)
        self.tags_menu.addSeparator()
        self.tags_menu.addAction("&Manage tags…").triggered.connect(self.manage_tags)

    def manage_tags(self) -> None:
        dialog = tagsdialog.TagsDialog(self._store, self)
        # The list draws a tag's name and colour on every row that carries it,
        # and those rows were read from the store before this dialog opened.
        dialog.changed.connect(self._tags_changed)
        dialog.exec()

    def _tags_changed(self) -> None:
        self.mail.quick_filter.reload_tags()
        self.mail.reload()
        self._build_tags_menu()

    def _label_undo(self) -> None:
        what = self.mail.actions.undoable
        self.act_undo.setText(f"&Undo — {what.lower()}" if what else "&Undo")
        self.act_undo.setEnabled(bool(what))

    def _search_changed(self, query) -> None:
        """The pane's search changed by some route other than the box."""
        self.search.set_query(query)
        self._sync_sort_menu()

    def _sync_sort_menu(self) -> None:
        """Point the View menu at what the pane is actually doing.

        Asked rather than tracked: the pane changes the order itself when a
        search starts and again when it ends, and turns conversations off when
        the order or a search makes them meaningless. A menu that only followed
        the clicks it received would be wrong for every one of those.
        """
        model = self.mail.model
        ranked = search_mod.has_rank(model.search)
        relevance = self.sort_actions.get(views_repo.SORT_RELEVANCE)
        if relevance is not None:
            relevance.setEnabled(ranked)
        action = self.sort_actions.get(model.sort.key)
        if action is not None:
            action.setChecked(True)
        self.act_descending.setChecked(model.sort.descending)

        # Blocked, because setChecked here would otherwise be indistinguishable
        # from the user pressing it and would put the pane back where it was.
        blocked = self.act_threaded.blockSignals(True)
        try:
            possible = model.sort.key == "date" and not model.search.active
            self.act_threaded.setChecked(model.threaded)
            self.act_threaded.setEnabled(possible)
            self.act_threaded.setStatusTip(
                "Group the list into conversations, with each thread's other "
                "messages beneath the newest" if possible else
                "Conversations need date order and no search — a thread is a "
                "run of messages in time, and a search returns single hits")
        finally:
            self.act_threaded.blockSignals(blocked)

    def _save_tab_state(self) -> None:
        self._sync_sort_menu()
        state = self.tabs.current_state()
        if state is None:
            return
        title = state.title if state.pinned_id else self.mail.title_for_scope()
        self.tabs.replace_state(self.tabs.currentIndex(),
                                self.mail.view_state(title).with_changes(
                                    pinned_id=state.pinned_id))

    # -------------------------------------------------------- view preferences
    def _saved_density(self) -> str:
        value = QSettings().value(SETTINGS_DENSITY)
        return value if isinstance(value, str) else density_mod.DEFAULT_DENSITY

    def set_density(self, key: str, *, remember: bool = True) -> None:
        density = density_mod.get(key)
        self.mail.set_density(density)
        action = self.density_actions.get(density.key)
        if action is not None:
            action.setChecked(True)
        if remember:
            QSettings().setValue(SETTINGS_DENSITY, density.key)

    def apply_theme(self, key: str | None) -> None:
        app = QApplication.instance()
        theme = theme_mod.get(key)
        if app is not None:
            theme = theme_mod.apply_to(app, theme.key)
        resolved = theme_mod.resolved(
            theme, app.palette() if app is not None else None)
        self._theme_key = theme.key
        self.mail.apply_theme(resolved)
        self.search.apply_theme(resolved)
        action = self.theme_actions.get(theme.key) if hasattr(self, "theme_actions") else None
        if action is not None:
            action.setChecked(True)
        QSettings().setValue(SETTINGS_THEME, theme.key)

    def _sort_by(self, key: str) -> None:
        self.mail.set_sort(views_repo.Sort(
            key=key, descending=self.act_descending.isChecked()))

    def _sort_direction(self, descending: bool) -> None:
        self.mail.set_sort(views_repo.Sort(
            key=self.mail.model.sort.key, descending=descending))

    def _show_hidden(self, show: bool) -> None:
        self.mail.rail.set_show_hidden(show)

    # --------------------------------------------------------------- geometry
    @property
    def splitter(self):
        """The three-pane splitter. Exposed because geometry is restored here
        while the splitter belongs to the pane."""
        return self.mail.splitter

    @property
    def rail(self):
        return self.mail.rail

    def _restore_geometry(self) -> None:
        s = QSettings()
        geom = s.value(SETTINGS_GEOMETRY)
        if isinstance(geom, QByteArray) and not geom.isEmpty():
            self.restoreGeometry(geom)
        else:
            self.resize(1400, 900)
        state = s.value(SETTINGS_STATE)
        if isinstance(state, QByteArray) and not state.isEmpty():
            self.restoreState(state)
        split = s.value(SETTINGS_SPLITTER)
        if isinstance(split, QByteArray) and not split.isEmpty():
            self.splitter.restoreState(split)
        agenda = s.value(SETTINGS_AGENDA)
        # QSettings returns the string "true" from an INI file and a real bool
        # from the native backend, and `bool("false")` is True — which is how a
        # setting that was turned off comes back on.
        wanted = agenda in (True, "true", "True", 1, "1")
        self.act_agenda.setChecked(wanted)
        self.mail.set_agenda_visible(wanted)

    def closeEvent(self, event) -> None:                       # noqa: N802
        from . import notifyhost

        # Hide to the tray when there is one, unless Quit asked to leave.
        if notifyhost.handle_close(self, event):
            return
        s = QSettings()
        s.setValue(SETTINGS_GEOMETRY, self.saveGeometry())
        s.setValue(SETTINGS_STATE, self.saveState())
        s.setValue(SETTINGS_SPLITTER, self.splitter.saveState())
        s.sync()
        if self._reminders is not None:
            self._reminders.stop()
        # The third is the account setup thread, which exists only from the
        # first time somebody adds an account — `ui/accounthost.py` makes it
        # then — so it is asked for rather than assumed.
        for controller in (self._sync, self._calendar_sync,
                           getattr(self, "_account_setup", None)):
            if controller is not None:
                # Waited for rather than killed. A sync interrupted mid-folder
                # is the case the store is built to survive — the state is
                # written after the messages — and a thread torn down
                # mid-write is not.
                controller.stop()
        tray = getattr(self, "_tray", None)
        if tray is not None:
            tray.hide()
        super().closeEvent(event)
