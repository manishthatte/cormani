# SPDX-License-Identifier: GPL-3.0-or-later
#
# New-mail notifications and the system tray, wired to the window.
#
# THE SEAM `ui/accounthost.py` AND `ui/filterhost.py` ALREADY ARE. The window
# owns the menu bar and the status bar; what announcing mail and holding a tray
# icon MEAN belongs beside those things rather than inside `ui/window.py`,
# which sits near the 600-line limit and has no opinion about either.
#
# THE SYNC CONTROLLER EMITS THE RESULTS, NOT ONLY A SUMMARY. The summary is a
# sentence for the status bar; the notifier needs the ids, and deriving them
# from "3 new messages" is a guess. `ui/syncing.SyncController.results_ready`
# is that signal.
#
# CLOSING THE WINDOW HIDES IT WHEN A TRAY IS THERE. Quit is the other action,
# and it goes through `quit_application` so that a person who asked to leave
# actually leaves. Without a tray, close is still quit — the previous behaviour,
# kept for sessions that have nowhere to hide to.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from . import mailnotify
from . import tray as tray_mod


def attach(window) -> None:
    """Put a tray on the window when the desktop has one, and remember it."""
    window._force_quit = False
    window._tray = None
    if not tray_mod.available():
        return
    icon = tray_mod.Tray(window)
    icon.show()
    icon.set_unread(_unread(window))
    window._tray = icon


def on_results(window, results) -> None:
    """A sync finished. Announce what filters left, and refresh the tooltip."""
    refresh_unread(window)
    words = mailnotify.announce(
        results, window._store,
        window_active=_window_is_active(window))
    if words:
        # Same fallback the calendar reminders use: the words go where they
        # can at least be found when the desktop has no notification service.
        window.status_message.setText(words)


def refresh_unread(window) -> None:
    icon = getattr(window, "_tray", None)
    if icon is not None:
        icon.set_unread(_unread(window))


def handle_close(window, event) -> bool:
    """True when the close was consumed (hidden to the tray)."""
    icon = getattr(window, "_tray", None)
    if icon is None or getattr(window, "_force_quit", False):
        return False
    event.ignore()
    window.hide()
    return True


def _unread(window) -> int:
    from ..store import messages as messages_repo
    return sum(messages_repo.unread_counts(window._store).values())


def _window_is_active(window) -> bool:
    """Whether the person is looking at corMani right now.

    A hidden window is never active, which is the case that matters: mail that
    arrived while the window sat in the tray should announce.
    """
    return bool(window.isVisible() and window.isActiveWindow())
