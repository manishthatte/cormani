# SPDX-License-Identifier: GPL-3.0-or-later
#
# Telling the person something when corMani is not the window they are looking
# at.
#
# A REMINDER THAT ONLY APPEARS INSIDE THE APPLICATION IS NOT A REMINDER. The
# whole point of one is that the meeting is in ten minutes and the user is in a
# terminal, so this has to leave the process. On Debian that is the desktop's
# own notification service, reached through `notify-send`; on Windows Qt's tray
# balloon is the equivalent and stage 8 owns the tray.
#
# `notify-send` IS DRIVEN AS A PROGRAM, WITH AN ARGUMENT ARRAY — CONVENTIONS.txt
# §7 and the same rule `desktop.py` follows. A meeting title comes from a
# stranger's invitation, so it is exactly the kind of string that must never
# reach a shell. `--` ends the options, so a title beginning with a hyphen is a
# title rather than an unrecognised switch.
#
# AND `gdbus` IS THE SECOND ROUTE, BECAUSE THIS MACHINE HAS NO notify-send.
# Measured on 25 August 2026: `libnotify-bin` is not installed here, while
# `gdbus` is — it comes with GLib, which anything running a GNOME session
# already has. It calls the SAME service by the same interface, so the two are
# not a first choice and a compromise but two spellings of one thing; the
# ordering is only that `notify-send` is the one whose arguments are easy to
# read. Without this, reminders on the author's own desktop would have been
# silently unavailable — and would have been "working" in every test.
#
# IT REPORTS WHETHER IT COULD SEND, AND THE CALLER HAS A FALLBACK. There is no
# way to know whether a notification was SEEN, so this promises only what it
# can: `notify` returns False when there is no service to send to, and
# `ui/reminders.py` then puts the same words in the status bar. CONVENTIONS.txt
# §8 — a feature that says "could not" is worth more than one that guesses.
#
# NOTHING FROM A MESSAGE OR AN INVITATION IS EVER PUT IN A SHELL STRING, A LOG,
# OR A FILE HERE. The body goes to the notification service and nowhere else.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import shutil
import subprocess
import sys

from .. import APP_ID, APP_NAME

# How long a reminder stays on screen, in milliseconds. Long enough to be read
# from across a room, short enough not to sit there after the meeting started.
DEFAULT_TIMEOUT = 15000


def available() -> bool:
    """Whether anything here can reach the desktop at all."""
    if sys.platform.startswith("win"):
        return True                                          # pragma: no cover
    return bool(_transport())


def _transport() -> str:
    for name in ("notify-send", "gdbus"):
        if shutil.which(name):
            return name
    return ""


def notify(title: str, body: str = "", *, urgent: bool = False,
           runner=None) -> bool:
    """Show one notification. False when there was nothing to show it with."""
    run = runner or _run
    if sys.platform.startswith("win") and runner is None:    # pragma: no cover
        return _windows(title, body)
    # An injected runner IS the transport, so the availability check is the
    # caller's to have made. Without this the suite could only assert that a
    # machine with no notify-send sends nothing, which is the uninteresting
    # half of the behaviour.
    transport = _transport() if runner is None else "notify-send"
    if not transport:
        return False
    command = (_notify_send(title, body, urgent) if transport == "notify-send"
               else _gdbus(title, body, urgent))
    try:
        run(command)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _notify_send(title: str, body: str, urgent: bool) -> list:
    return ["notify-send", "--app-name", APP_NAME, "--icon", APP_ID,
            "--expire-time", str(DEFAULT_TIMEOUT),
            "--urgency", "critical" if urgent else "normal",
            "--", str(title), str(body)]


def _gdbus(title: str, body: str, urgent: bool) -> list:
    """org.freedesktop.Notifications.Notify, positionally.

    The signature is (app_name, replaces_id, icon, summary, body, actions,
    hints, timeout). `replaces_id` of 0 means "a new one"; the two empty
    containers are the actions and the hints, and they are literals rather
    than anything derived from the event — `gdbus` parses those arguments as
    GVariant text, so a value built from a meeting title could otherwise be
    read as syntax.
    """
    return ["gdbus", "call", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.Notify",
            APP_NAME, "0", APP_ID, str(title), str(body), "[]", "{}",
            str(DEFAULT_TIMEOUT)]


def _run(command: list) -> None:
    """Started and released, exactly as `desktop.py` releases a handler.

    A notification daemon that is slow to answer must not hold the interface,
    and its output must not land on the terminal corMani was started from.
    """
    subprocess.Popen(command, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _windows(title: str, body: str) -> bool:                 # pragma: no cover
    """Qt's tray balloon, which is the Windows equivalent.

    Imported here rather than at the top of the file so that this module keeps
    the property every other `platform/` module has: no Qt, and importable
    without a display.
    """
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    except ImportError:
        return False
    if QApplication.instance() is None or not QSystemTrayIcon.isSystemTrayAvailable():
        return False
    tray = QSystemTrayIcon()
    tray.show()
    tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information,
                     DEFAULT_TIMEOUT)
    return True
