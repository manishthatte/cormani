# SPDX-License-Identifier: GPL-3.0-or-later
#
# The keyboard map, in one place.
#
# corMani is meant to be usable without the mouse, which means the keys are a
# design surface rather than a scattering of connect() calls. Declaring them as
# data buys three things: a test can assert that no two shortcuts collide, a
# help window can list them without a second list going stale, and rebinding
# them later is one table rather than a search through the widgets.
#
# WHY THE SCOPES ARE WHAT THEY ARE — this is the non-obvious part.
#
# Qt normally lets a focused QLineEdit keep a plain letter that is also a
# shortcut, by accepting the ShortcutOverride event that precedes the key press.
# If that works, a window-wide `a` for archive is harmless while someone types
# in the filter box. If it does not, every letter typed into that box archives a
# message.
#
# That behaviour could not be verified here: Debian does not package
# python3-pyside6.qttest, so the suite cannot synthesise a real key press, and a
# synthetic sendEvent does not go through the shortcut map at all. Rather than
# depend on something unverified, the single-letter actions are bound to the
# MESSAGE LIST with WidgetShortcut — they fire only while the list itself has
# focus, so no line edit is ever in the way. It also happens to be the behaviour
# Thunderbird has: keys act on the focused pane.
#
# Everything with a modifier is window-wide, where there is no such ambiguity.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from dataclasses import dataclass

# Where a shortcut is installed, and therefore when it fires.
SCOPE_WINDOW = "window"     # anywhere in the window; modified keys only
SCOPE_LIST = "list"         # only while the message list has focus


@dataclass(frozen=True)
class Shortcut:
    id: str
    label: str              # menu text, and the help window's left column
    key: str                # a QKeySequence string
    scope: str
    description: str
    # False for anything whose implementation arrives in a later stage. The
    # action is still created and still listed, and is visibly disabled, because
    # a control that does nothing when pressed is worse than one that says it is
    # not ready. CONVENTIONS.txt §8.
    ready: bool = True


_BASE: tuple[Shortcut, ...] = (
    # --- window ------------------------------------------------------------
    Shortcut("sync", "&Sync now", "F5", SCOPE_WINDOW,
             "Fetch new mail for every account"),
    Shortcut("search", "&Search all accounts", "Ctrl+F", SCOPE_WINDOW,
             "Full-text search across every account"),
    Shortcut("compose", "&New message", "Ctrl+N", SCOPE_WINDOW,
             "Write a new message"),
    Shortcut("undo", "&Undo", "Ctrl+Z", SCOPE_WINDOW,
             "Take back the last change, on the server as well as here"),
    Shortcut("filter", "&Filter these messages", "Ctrl+Shift+K", SCOPE_WINDOW,
             "Focus the Quick Filter box"),
    Shortcut("new_tab", "New &tab", "Ctrl+T", SCOPE_WINDOW,
             "Open the current view in a new tab"),
    Shortcut("close_tab", "&Close tab", "Ctrl+W", SCOPE_WINDOW,
             "Close the current tab"),
    Shortcut("next_tab", "Ne&xt tab", "Ctrl+Tab", SCOPE_WINDOW, "Cycle forwards"),
    Shortcut("prev_tab", "Pre&vious tab", "Ctrl+Shift+Tab", SCOPE_WINDOW,
             "Cycle backwards"),
    # --- the calendar ------------------------------------------------------
    # All window-scope and all modified, which the note at the top of this file
    # requires: the calendar has no list to hold focus, and a bare letter here
    # would fire while somebody types a meeting title.
    # Ctrl+Shift+C is THUNDERBIRD'S key for the calendar, and matching it is
    # worth moving `clear_filters` to Ctrl+Shift+X for: the people this
    # replaces Thunderbird for have that one in their fingers, and a filter
    # that clears is found from a menu while a calendar that will not open is
    # a client that feels wrong.
    Shortcut("calendar", "Show the &calendar", "Ctrl+Shift+C", SCOPE_WINDOW,
             "Put the calendar where the list and the reading pane are"),
    Shortcut("new_event", "New e&vent", "Ctrl+Shift+N", SCOPE_WINDOW,
             "Put an event in a calendar"),
    Shortcut("calendar_today", "&Today", "Ctrl+Shift+T", SCOPE_WINDOW,
             "Go back to today"),
    Shortcut("calendar_next", "&Next", "Alt+Right", SCOPE_WINDOW,
             "The next month, week or day, whichever is being shown"),
    Shortcut("calendar_prev", "&Previous", "Alt+Left", SCOPE_WINDOW,
             "The previous month, week or day"),
    Shortcut("calendar_month", "&Month", "Ctrl+Shift+1", SCOPE_WINDOW,
             "Show the month"),
    Shortcut("calendar_week", "&Week", "Ctrl+Shift+2", SCOPE_WINDOW,
             "Show the week"),
    Shortcut("calendar_day", "&Day", "Ctrl+Shift+3", SCOPE_WINDOW,
             "Show one day"),
    Shortcut("calendar_agenda", "&Agenda", "Ctrl+Shift+4", SCOPE_WINDOW,
             "List what is in the range"),
    # --- tracking ----------------------------------------------------------
    # Window-scope and modified, for the reason the calendar's are: the
    # tracking pane has no list to hold focus and a bare letter would fire
    # while somebody types a note. Ctrl+Shift+O and not the initial of
    # "tracking": T is the calendar's today, C its own, N a new event, and K
    # belongs to the Quick Filter — which the conflict test caught the moment
    # K was tried. O is for OWED, which PLAN.txt §2 calls the reason corMani
    # exists and which is the headline of the pane this opens.
    Shortcut("tracking", "Show &tracking", "Ctrl+Shift+O", SCOPE_WINDOW,
             "Put the tracked threads where the list and the reading pane are"),
    Shortcut("track_this", "&Track this message…", "Ctrl+Shift+A", SCOPE_WINDOW,
             "Make a thread from the message being read, and file its exchange"),
    Shortcut("log_call", "&Log a call…", "Ctrl+Shift+L", SCOPE_WINDOW,
             "Put something that left no trace onto the timeline"),
    Shortcut("clear_filters", "Clear &filters", "Ctrl+Shift+X", SCOPE_WINDOW,
             "Turn off every Quick Filter toggle"),

    # --- filters -----------------------------------------------------------
    # TWO THINGS IN THIS PROGRAM ARE CALLED A FILTER and both keep their name,
    # because both names are the ones people arrive with: the Quick Filter bar
    # (Ctrl+Shift+K, above) narrows what is on screen and forgets it, and a
    # MESSAGE FILTER is a rule that moves mail as it arrives. Thunderbird has
    # the same pair with the same two names. What keeps them apart here is that
    # the rules are never called anything but "message filters" — in this map,
    # in the menu, and in `--filters`.
    Shortcut("message_filters", "&Message filters…", "Ctrl+Shift+F",
             SCOPE_WINDOW,
             "The rules that move, tag and mark mail as it arrives"),

    # --- saved searches ----------------------------------------------------
    # AND A THIRD THING IS NEARLY CALLED ONE. A saved search is a query kept
    # under a name and drawn in the rail as a virtual folder; it is not a rule
    # and it moves nothing. Ctrl+Shift+S rather than a Ctrl+S nobody would
    # expect in a mail client, and it sits beside Ctrl+F because saving a
    # search is what a person does immediately after making one.
    #
    # ONLY THE ONE WITH A KEY IS HERE. Managing them is a plain QAction in
    # `ui/menus.py`, the way `Create a filter from this message…` is: every
    # entry in this map is asserted to have a key, and a keyless one would be
    # asking the registry to be a menu instead of a keyboard map.
    Shortcut("save_search", "&Save this search…", "Ctrl+Shift+S", SCOPE_WINDOW,
             "Keep what is on screen as a named virtual folder in the rail"),

    # --- the address book --------------------------------------------------
    # Ctrl+Shift+B is THUNDERBIRD'S key for the address book, and matching it
    # is worth having for the same reason Ctrl+Shift+C was worth moving
    # `clear_filters` for: the people this replaces Thunderbird for have it in
    # their fingers. B is free — the conflict test is what says so rather than
    # a reading of the list, and it said so on the first try here.
    #
    # ONLY THE ONE WITH A KEY IS HERE, as with saved searches. "Add the sender
    # to the address book" is a plain QAction in `ui/menus.py`: every entry in
    # this map is asserted to have a key, and a keyless one would be asking the
    # registry to be a menu instead of a keyboard map.
    Shortcut("contacts", "&Address book", "Ctrl+Shift+B", SCOPE_WINDOW,
             "Put the address book where the list and the reading pane are"),

    # --- the message list --------------------------------------------------
    Shortcut("next_unread", "Next unread", "N", SCOPE_LIST,
             "Select the next unread message"),
    Shortcut("prev_unread", "Previous unread", "P", SCOPE_LIST,
             "Select the previous unread message"),
    Shortcut("reply", "&Reply", "R", SCOPE_LIST, "Reply to the sender"),
    Shortcut("reply_all", "Reply &all", "Shift+R", SCOPE_LIST,
             "Reply to everyone"),
    Shortcut("forward", "&Forward", "F", SCOPE_LIST,
             "Send this message on to someone else"),
    Shortcut("archive", "&Archive", "A", SCOPE_LIST,
             "Move to this account's archive folder"),
    Shortcut("delete", "&Delete", "Del", SCOPE_LIST,
             "Move to this account's trash folder"),
    Shortcut("mark_read", "&Mark read or unread", "M", SCOPE_LIST,
             "Toggle the read state"),
    Shortcut("flag", "Fla&g", "S", SCOPE_LIST, "Toggle the flag"),
    Shortcut("open_tab", "Open in a new tab", "Ctrl+Return", SCOPE_LIST,
             "Open the selected message in its own tab"),
    Shortcut("filter_slash", "Filter these messages", "/", SCOPE_LIST,
             "Focus the Quick Filter box"),
)

# Tags on 1-9. Generated rather than typed out, because nine near-identical
# entries are nine chances to transpose a digit.
_TAGS: tuple[Shortcut, ...] = tuple(
    Shortcut(f"tag_{n}", f"Tag {n}", str(n), SCOPE_LIST,
             f"Apply or remove the tag on key {n}")
    for n in range(1, 10))

SHORTCUTS: tuple[Shortcut, ...] = _BASE + _TAGS

_BY_ID = {s.id: s for s in SHORTCUTS}


def by_id(shortcut_id: str) -> Shortcut:
    return _BY_ID[shortcut_id]


def in_scope(scope: str) -> tuple[Shortcut, ...]:
    return tuple(s for s in SHORTCUTS if s.scope == scope)


def tag_shortcut_key(shortcut_id: str) -> int | None:
    """The digit behind a tag_N id, or None if this is not a tag shortcut."""
    if not shortcut_id.startswith("tag_"):
        return None
    return int(shortcut_id[4:])


def collisions() -> list[str]:
    """Two shortcuts that could both fire on one key press.

    Only within a scope: `F` on the list and `Ctrl+F` on the window are
    different keys, and two scopes that never have focus at the same time cannot
    collide. A test asserts this is empty, which is the point of the function —
    a collision is silent at run time, and Qt resolves it by picking one.
    """
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for s in SHORTCUTS:
        key = (s.scope, s.key.lower())
        if key in seen:
            clashes.append(f"{s.scope}: {s.key} on both {seen[key]} and {s.id}")
        seen[key] = s.id
    return clashes
