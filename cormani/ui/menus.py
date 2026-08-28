# SPDX-License-Identifier: GPL-3.0-or-later
#
# The menu bar.
#
# Split out of `ui/window.py` when the 600-line rule fired on it, and split
# here because building a menu bar is a different job from being a window: it
# is one long declaration, it reads top to bottom, and nothing else in the
# window needs to see it. The same seam `ui/actions.py` and
# `ui/calendarhost.py` were found at — a file that both arranges widgets and
# decides what something means is the file that grows.
#
# FUNCTIONS TAKING THE WINDOW, NOT A CLASS. There is no state here: `build`
# creates the actions and hangs them on the window, exactly as the method it
# came from did, because the window is what the rest of the code reaches them
# through — `window.act_sync`, `window.density_actions` — and hiding them
# behind an object would be a second name for each.
#
# THE THREE HELPERS ARE NOT INTERCHANGEABLE. `window_action` INSTALLS a
# shortcut; `list_action` shows the same key in a menu and installs nothing,
# because the single-letter keys are bound to the message list rather than to
# the window and `ui/shortcuts.py` explains at length why. Using the first
# where the second belongs would put `a` for archive on the window, and every
# letter typed into a box would archive a message.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QMenuBar

from .. import APP_NAME
from ..store import views as views_repo
from . import density as density_mod
from . import accounthost
from . import commands as commands_mod
from . import filterhost
from . import help as help_mod
from . import healthdialog as healthdialog_mod
from . import preferences as preferences_mod
from . import shortcuts as shortcuts_mod
from . import theme as theme_mod
from . import viewhost

# Which list shortcuts appear in the Message and Go menus, in order.
_MESSAGE_MENU = ("reply", "reply_all", "forward", None, "archive", "delete",
                 None, "mark_read", "flag")
_GO_MENU = ("next_unread", "prev_unread")

def build(window) -> None:
    bar: QMenuBar = window.menuBar()

    m_file = bar.addMenu("&File")
    window.act_compose = window_action(window, 
        "compose", lambda: window.mail.compose("new"))
    m_file.addAction(window.act_compose)
    m_file.addSeparator()

    # ACCOUNTS ARE ADDED FROM HERE NOW, and this is where a person looks for
    # it: Thunderbird and Outlook both put it in File. It was the command line
    # or nothing until stage 8, which is defensible for a first import — that
    # runs for hours and belongs in a terminal — and indefensible for the
    # sixteenth account added a year later.
    #
    # THE REGISTRATION SITS BESIDE IT rather than under Tools, because it is a
    # PRECONDITION of the entry above and not a tool: a Microsoft account
    # cannot be added at all without one, and the two are looked for in the
    # same breath.
    window.act_add_account = QAction("Add mail &account…", window)
    window.act_add_account.setStatusTip(
        "Add an account to corMani. Nothing is written until the server has "
        "accepted the credential")
    window.act_add_account.triggered.connect(
        lambda: accounthost.add_account(window))
    m_file.addAction(window.act_add_account)
    # The rail's context menu asks for the same thing, and goes through this
    # action rather than calling the host itself — so that the entry being
    # disabled over demo data disables BOTH routes, from one line.
    window.mail.rail.add_account_wanted.connect(window.act_add_account.trigger)

    window.act_registration = QAction("OAuth &registration…", window)
    window.act_registration.setStatusTip(
        "This installation's own OAuth client id. One covers every account on "
        "that provider, and it is kept in the system keyring")
    window.act_registration.triggered.connect(
        lambda: accounthost.record_registration(window))
    m_file.addAction(window.act_registration)

    if window._demo:
        # The same reason `act_sync` is disabled below, with a different
        # consequence: this window is showing a disposable store in the cache
        # directory, and `configure.add_account` writes to the real one. An
        # account added here would be added correctly and appear to vanish.
        for action in (window.act_add_account, window.act_registration):
            action.setEnabled(False)
            action.setStatusTip(accounthost.DEMO_REFUSAL)

    m_file.addSeparator()
    window.act_sync = window_action(window, "sync", window.sync_now)
    window.act_sync.setEnabled(window._sync is not None)
    if window._sync is None:
        window.act_sync.setStatusTip(
            "Not available over demo data — there is no server behind it")
    m_file.addAction(window.act_sync)
    m_file.addSeparator()
    m_file.addAction(window_action(window, "new_tab", window._new_tab))
    m_file.addAction(window_action(window, "close_tab", window.tabs.close_current))
    m_file.addSeparator()
    # Quit, not close: once a tray is attached, close hides the window and
    # leaves the process running, which is the point of the tray. This is the
    # action that ends it.
    act_quit = QAction("&Quit", window)
    act_quit.setShortcut(QKeySequence.StandardKey.Quit)
    act_quit.triggered.connect(window.quit_application)
    m_file.addAction(act_quit)

    m_edit = bar.addMenu("&Edit")
    window.act_undo = window_action(window, "undo", window.mail.undo)
    m_edit.addAction(window.act_undo)
    # The LABEL says what would come back. "Undo" alone asks the user to
    # remember what they did, which after a minute of triage is exactly what
    # they do not; naming it is the difference between pressing it and
    # wondering whether to.
    m_edit.aboutToShow.connect(window._label_undo)
    m_edit.addSeparator()
    act_prefs = QAction("&Preferences…", window)
    act_prefs.setStatusTip("Appearance, sync, panels and privacy settings")
    act_prefs.triggered.connect(lambda: preferences_mod.show(window))
    m_edit.addAction(act_prefs)
    m_edit.addSeparator()
    window.act_search = window_action(window, "search", window.search.focus_text)
    m_edit.addAction(window.act_search)
    m_edit.addAction(window_action(window, "filter", window.mail.quick_filter.focus_text))
    m_edit.addAction(window_action(window, "clear_filters", window.mail.quick_filter.clear))
    m_edit.addSeparator()
    # SAVING A SEARCH GOES BESIDE SEARCHING, because it is what a person does
    # in the same breath as making one. Opening a saved search is the other
    # act and is a submenu, not a line: there may be thirty of them, and one
    # kept out of the rail has nowhere else to be reached from.
    window.act_save_search = window_action(
        window, "save_search", lambda: viewhost.save_current(window))
    m_edit.addAction(window.act_save_search)
    window.saved_views_menu = m_edit.addMenu("Saved sea&rches")
    # Rebuilt each time it opens, for the reason the Tags menu is: one can be
    # saved, renamed or deleted while the window is open, and a menu built once
    # is a menu that is wrong from then on.
    window.saved_views_menu.aboutToShow.connect(
        lambda: viewhost.build_menu(window, window.saved_views_menu))
    viewhost.build_menu(window, window.saved_views_menu)
    window.act_manage_views = QAction("&Manage saved searches…", window)
    window.act_manage_views.setStatusTip(
        "Rename, reorder, hide from the rail or delete the searches you have "
        "saved")
    window.act_manage_views.triggered.connect(
        lambda: viewhost.show_manager(window))
    m_edit.addAction(window.act_manage_views)

    m_view = bar.addMenu("&View")
    density_menu = m_view.addMenu("&Density")
    window._density_group = QActionGroup(window)
    window._density_group.setExclusive(True)
    window.density_actions = {}
    for key, item in density_mod.DENSITIES.items():
        action = QAction(f"&{item.name}", window)
        action.setCheckable(True)
        action.triggered.connect(lambda _=False, k=key: window.set_density(k))
        window._density_group.addAction(action)
        density_menu.addAction(action)
        window.density_actions[key] = action

    theme_menu = m_view.addMenu("&Theme")
    window._theme_group = QActionGroup(window)
    window._theme_group.setExclusive(True)
    window.theme_actions = {}
    for key, item in theme_mod.THEMES.items():
        action = QAction(item.name, window)
        action.setCheckable(True)
        action.triggered.connect(lambda _=False, k=key: window.apply_theme(k))
        window._theme_group.addAction(action)
        theme_menu.addAction(action)
        window.theme_actions[key] = action

    sort_menu = m_view.addMenu("&Sort by")
    window._sort_group = QActionGroup(window)
    window._sort_group.setExclusive(True)
    window.sort_actions = {}
    for key, label in (("date", "&Date"), ("sender", "&Sender"),
                       ("subject", "S&ubject"),
                       (views_repo.SORT_RELEVANCE, "&Relevance")):
        action = QAction(label, window)
        action.setCheckable(True)
        action.setChecked(key == "date")
        if key == views_repo.SORT_RELEVANCE:
            # Enabled only while something has been matched against the
            # index. bm25 has no opinion about a message no query ranked,
            # and an entry that silently means "by date" is a lie.
            action.setEnabled(False)
            action.setStatusTip("Best match first — while a search is running")
            action.setToolTip(
                "Available only while a search is active — sorts by how well "
                "each message matches the query")
        action.triggered.connect(lambda _=False, k=key: window._sort_by(k))
        window._sort_group.addAction(action)
        sort_menu.addAction(action)
        window.sort_actions[key] = action
    sort_menu.addSeparator()
    window.act_descending = QAction("Newest or largest &first", window)
    window.act_descending.setCheckable(True)
    window.act_descending.setChecked(True)
    window.act_descending.toggled.connect(window._sort_direction)
    sort_menu.addAction(window.act_descending)

    m_view.addSeparator()
    window.act_back_to_mail = QAction("&Back to mail", window)
    window.act_back_to_mail.setShortcut("Ctrl+Shift+M")
    window.act_back_to_mail.setStatusTip(
        "Return to the message list from calendar, tracking, contacts or a site")
    window.act_back_to_mail.triggered.connect(window.mail.show_mail)
    m_view.addAction(window.act_back_to_mail)

    m_view.addSeparator()
    window.act_threaded = QAction("&Conversations", window)
    window.act_threaded.setCheckable(True)
    window.act_threaded.setChecked(True)
    window.act_threaded.setStatusTip(
        "Group the list into conversations, with each thread's other "
        "messages beneath the newest")
    window.act_threaded.toggled.connect(window.mail.set_threaded)
    m_view.addAction(window.act_threaded)

    m_view.addSeparator()
    window.act_agenda = QAction("&Agenda beside the mail", window)
    window.act_agenda.setCheckable(True)
    window.act_agenda.setStatusTip(
        "A narrow list of what is coming, beside the message list")
    window.act_agenda.toggled.connect(window._show_agenda)
    m_view.addAction(window.act_agenda)

    m_view.addSeparator()
    window.act_show_hidden = QAction("Show &hidden accounts", window)
    window.act_show_hidden.setCheckable(True)
    window.act_show_hidden.toggled.connect(window._show_hidden)
    m_view.addAction(window.act_show_hidden)

    m_message = bar.addMenu("&Message")
    for entry in _MESSAGE_MENU:
        if entry is None:
            m_message.addSeparator()
        else:
            m_message.addAction(list_action(window, entry))
    act_print = QAction("&Print…", window)
    act_print.setShortcut(QKeySequence.StandardKey.Print)
    act_print.setStatusTip(commands_mod.command_tooltip("print"))
    act_print.setEnabled(commands_mod.command_ready("print"))
    act_print.triggered.connect(lambda: window.mail.run_action("print", _selected_ids(window)))
    m_message.addAction(act_print)
    act_pdf = QAction("Export as &PDF…", window)
    act_pdf.setStatusTip("Save the message being read as a PDF file")
    act_pdf.triggered.connect(
        lambda: window.mail.run_action("export_pdf", _selected_ids(window)))
    m_message.addAction(act_pdf)
    m_message.addSeparator()
    # Rebuilt each time it opens, because a tag can be renamed, recoloured
    # or given a key while the window is open — and a menu built once is a
    # menu that is wrong from then on.
    window.tags_menu = m_message.addMenu("&Tags")
    window.tags_menu.aboutToShow.connect(window._build_tags_menu)
    window._build_tags_menu()

    # A TOOLS MENU AND NOT A LINE IN Message. What is under Message is done to
    # the message in front of you; a filter rule outlives every message it will
    # ever see, and "run the filters over this folder" is about a folder. The
    # address book arrived here in stage 8, as this note said it would; the
    # Thunderbird import is the one still to come.
    m_tools = bar.addMenu("&Tools")
    # THE ADDRESS BOOK IS FIRST, above the filters, because it is the entry a
    # person reaches for by name — "where are my contacts" — and the filters
    # are something they go looking for. Order in a menu is a claim about how
    # often each is wanted.
    m_tools.addAction(window_action(window, "contacts",
                                    window.mail.show_contacts))
    window.act_add_contact = QAction("Add the &sender to the address book…",
                                     window)
    window.act_add_contact.setStatusTip(
        "Make a contact card from the message being read — somebody already "
        "in the book is shown rather than added twice")
    window.act_add_contact.triggered.connect(
        lambda: window.mail.add_sender_to_contacts())
    m_tools.addAction(window.act_add_contact)
    m_tools.addSeparator()
    m_tools.addAction(window_action(window, "message_filters",
                                    lambda: filterhost.show_filters(window)))
    window.act_filter_from = QAction("Create a filter from this &message…",
                                     window)
    window.act_filter_from.setStatusTip(
        "Start a rule from the message being read — its sender, to begin with")
    window.act_filter_from.triggered.connect(
        lambda: filterhost.filter_from_message(window))
    m_tools.addAction(window.act_filter_from)
    m_tools.addSeparator()
    window.act_run_filters = QAction("&Run filters on this folder", window)
    window.act_run_filters.setStatusTip(
        "Apply every rule to the mail already here. Asks first: it can move a "
        "great deal at once and cannot be undone")
    window.act_run_filters.triggered.connect(
        lambda: filterhost.run_over_view(window))
    m_tools.addAction(window.act_run_filters)

    m_track = bar.addMenu("&Tracking")
    m_track.addAction(window_action(window, "tracking",
                                    window.mail.show_tracking))
    m_track.addSeparator()
    m_track.addAction(window_action(window, "track_this",
                                    window.mail.track_this))
    m_track.addAction(window_action(window, "log_call",
                                    lambda: window.mail.tracking_action("log-call")))

    m_calendar = bar.addMenu("&Calendar")
    m_calendar.addAction(window_action(window, "calendar", window.mail.show_calendar))
    window.act_new_event = window_action(window, 
        "new_event", lambda: window.mail.calendar_action("new"))
    m_calendar.addAction(window.act_new_event)
    m_calendar.addSeparator()
    for key, name in (("calendar_month", "month"), ("calendar_week", "week"),
                      ("calendar_day", "day"), ("calendar_agenda", "agenda")):
        m_calendar.addAction(window_action(window, 
            key, lambda n=name: window.mail.calendar_mode(n)))
    m_calendar.addSeparator()
    m_calendar.addAction(window_action(window, 
        "calendar_today", lambda: window.mail.calendar_action("today")))
    m_calendar.addAction(window_action(window, 
        "calendar_prev", lambda: window.mail.calendar_action("previous")))
    m_calendar.addAction(window_action(window, 
        "calendar_next", lambda: window.mail.calendar_action("next")))

    # EMPTY AND HIDDEN UNTIL `attach_sites` SAYS OTHERWISE. Which sites exist
    # is a configuration answer that is not known when the bar is built, and
    # the panels can be off entirely — finding 2 requires that they can be. A
    # menu offering WhatsApp on a build with the panels turned off would be a
    # menu that lies.
    window.sites_menu = bar.addMenu("&Panels")

    m_go = bar.addMenu("&Go")
    for entry in _GO_MENU:
        m_go.addAction(list_action(window, entry))

    m_help = bar.addMenu("&Help")
    if window._demo:
        act_tour = QAction("Take &tour", window)
        act_tour.setStatusTip(
            "Open the tracking tab and see how corMani follows conversations")
        act_tour.triggered.connect(window._demo_tour)
        m_help.addAction(act_tour)
        m_help.addSeparator()
    act_keys = QAction("&Keyboard shortcuts…", window)
    act_keys.triggered.connect(
        lambda: help_mod.ShortcutsDialog(window).exec())
    m_help.addAction(act_keys)
    act_health = QAction("&Installation health…", window)
    act_health.setStatusTip(
        "Report what is installed, configured and present — like cormani --check")
    act_health.triggered.connect(lambda: healthdialog_mod.show(window))
    m_help.addAction(act_health)
    window.act_about = QAction(f"&About {APP_NAME}", window)
    window.act_about.triggered.connect(window._about)
    m_help.addAction(window.act_about)

def window_action(window, shortcut_id: str, slot=None) -> QAction:
    shortcut = shortcuts_mod.by_id(shortcut_id)
    ready = _action_ready(shortcut_id)
    action = QAction(shortcut.label, window)
    action.setShortcut(QKeySequence(shortcut.key))
    tip = commands_mod.command_tooltip(shortcut_id) or shortcut.description
    action.setStatusTip(tip)
    action.setEnabled(ready)
    if slot is not None and ready:
        action.triggered.connect(lambda _=False: slot())
    window.addAction(action)
    return action

def list_action(window, shortcut_id: str) -> QAction:
    """A menu entry that shows a key but does not install it. See the note
    at the top of this file."""
    shortcut = shortcuts_mod.by_id(shortcut_id)
    ready = _action_ready(shortcut_id)
    action = QAction(f"{shortcut.label}\t{shortcut.key}", window)
    tip = commands_mod.command_tooltip(shortcut_id) or shortcut.description
    action.setStatusTip(tip)
    action.setEnabled(ready)
    action.triggered.connect(
        lambda _=False, s=shortcut_id: run_list_action(window, s))
    return action

def _action_ready(shortcut_id: str) -> bool:
    if commands_mod.known(shortcut_id):
        return commands_mod.command_ready(shortcut_id)
    return shortcuts_mod.by_id(shortcut_id).ready

def _selected_ids(window) -> list[int]:
    row = window.mail.current_row()
    return [row.id] if row is not None else []

def run_list_action(window, shortcut_id: str) -> None:
    window.mail.run_shortcut(shortcut_id)

