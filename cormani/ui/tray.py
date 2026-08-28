# SPDX-License-Identifier: GPL-3.0-or-later
#
# The icon in the system tray, and what closing the window means once it is
# there.
#
# A NOTIFICATION THAT HAS NOWHERE TO POINT IS HALF A FEATURE. The tray is the
# other half: it holds the unread count where a person already looks, and it is
# how the window comes back after being closed — which is the Thunderbird
# behaviour stage 8 is catching up with. Without it, closing the window ends
# the process, and a reminder or a new-mail balloon then has nothing to raise.
#
# CLOSING HIDES; QUIT ENDS THE PROCESS. `QAction("Quit")` used to call
# `window.close`, and once close means hide that would never quit. The host
# sets a flag and Quit goes through `quit_application` instead. The distinction
# is said in the tray menu rather than discovered when the process will not
# leave.
#
# ABSENT RATHER THAN BROKEN WHEN THERE IS NO TRAY. Some sessions have none —
# a remote X display, a window manager that refuses them — and inventing a
# floating widget there would be worse than nothing. `available()` is False and
# every method is a no-op; closing the window then ends the process as before.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import APP_NAME
from ..platform.runtime import resource


def available() -> bool:
    return QSystemTrayIcon.isSystemTrayAvailable()


def app_icon() -> QIcon:
    path = resource("cormani.svg")
    return QIcon(str(path)) if path is not None else QIcon()


class Tray:
    """One system-tray icon, owned by the window that made it."""

    def __init__(self, window) -> None:
        self._window = window
        self._icon = QSystemTrayIcon(app_icon(), window)
        self._icon.setToolTip(APP_NAME)
        menu = QMenu(window)
        show = QAction(f"Open {APP_NAME}", window)
        show.triggered.connect(self.raise_window)
        menu.addAction(show)
        if hasattr(window, "act_sync"):
            menu.addAction(window.act_sync)
        menu.addSeparator()
        quit_act = QAction(f"Quit {APP_NAME}", window)
        quit_act.triggered.connect(window.quit_application)
        menu.addAction(quit_act)
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._activated)

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    @property
    def visible(self) -> bool:
        return self._icon.isVisible()

    def set_unread(self, count: int) -> None:
        """The tooltip is the badge. Qt has no numeric overlay on every desktop."""
        if count <= 0:
            self._icon.setToolTip(APP_NAME)
        elif count == 1:
            self._icon.setToolTip(f"{APP_NAME} — 1 unread")
        else:
            self._icon.setToolTip(f"{APP_NAME} — {count} unread")

    def raise_window(self) -> None:
        window = self._window
        window.show()
        window.raise_()
        window.activateWindow()

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.raise_window()
