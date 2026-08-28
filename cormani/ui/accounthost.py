# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the two account menu entries MEAN.
#
# The seam `ui/filterhost.py`, `ui/trackhost.py` and `ui/calendarhost.py`
# already are: the window owns the menu bar, and what a command DOES belongs
# beside the thing it does it to. `ui/window.py` stands at 566 of the 600 lines
# the packaging test allows, so this was that or the fifteenth seam found by
# length instead of by subject.
#
# THE CONTROLLER OUTLIVES THE DIALOG, and is kept on the window for that
# reason. An attempt cannot be cancelled — `ui/accountsetup.py` says why at
# length — so closing the dialog stops the watching and not the work, and the
# outcome has to have somewhere to arrive. It arrives in the status bar.
#
# IT IS REFUSED OVER DEMO DATA, and the reason is not tidiness. The demo window
# is looking at a disposable store in the CACHE directory, while
# `configure.add_account` writes to the store `app.current_paths()` names —
# the real one. An account added from a demo window would therefore be added
# correctly, to a store this window is not showing, and would appear to have
# vanished. The menu entries are disabled and say so; this refuses again in
# case something else calls it.
#
# THE BROWSER IS OPENED HERE AND NOT ON THE THREAD. `QDesktopServices.openUrl`
# is a GUI call, and the sign-in flow runs on a worker; the worker asks by
# signal and this is the slot at the other end. Qt queues the connection
# itself.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from . import accountdialog
from .accountsetup import AccountSetup

DEMO_REFUSAL = ("Not available over demo data — this window is showing a "
                "disposable store, and an account would be added to the real "
                "one.")


def setup_for(window) -> AccountSetup:
    """The window's one setup controller, made when it is first needed.

    Not in `MainWindow.__init__`, because a window built for a test must not
    acquire a thread it never uses, and because the great majority of sessions
    never add an account.
    """
    controller = getattr(window, "_account_setup", None)
    if controller is None:
        controller = AccountSetup(window)
        controller.browser_wanted.connect(
            lambda url: _open_browser(window, url))
        controller.finished.connect(
            lambda address, code: _finished(window, address, code))
        window._account_setup = controller
    return controller


def add_account(window) -> bool:
    """File ▸ Add mail account…  False when the dialog was not opened."""
    if window._demo:
        window.status_message.setText(DEMO_REFUSAL)
        return False
    dialog = accountdialog.AddAccountDialog(
        window._store, setup_for(window), window)
    dialog.added.connect(lambda address: _added(window, address))
    dialog.exec()
    return True


def record_registration(window) -> bool:
    """File ▸ OAuth registration…  The other half of open item 2.

    Reachable on its own as well as from inside the add dialog, because it is
    needed BEFORE the first Microsoft account can be added at all and because
    it expires: a Google project left in Testing issues refresh tokens that die
    after seven days, and re-recording is what fixes that.
    """
    if window._demo:
        window.status_message.setText(DEMO_REFUSAL)
        return False
    accountdialog.RegistrationDialog("google", window).exec()
    return True


def _added(window, address: str) -> None:
    """An account is in the store. Put it on the screen.

    The rail is rebuilt rather than appended to — `ui/models/rail.py` explains
    why every mutation rebuilds — and the search bar's account list is reloaded
    with it, because "search this account" is a filter over a list that was
    read when the window opened.

    NO SYNC IS STARTED. A first import can run for hours and is the one thing
    in corMani that should be begun deliberately; the status bar says which key
    begins it.
    """
    window.mail.rail.reload(keep=window.mail.rail.current_key())
    window.search.reload_accounts()
    window.status_message.setText(
        f"{address} was added. Press F5 to fetch its mail — a first import can "
        f"take hours and is resumable.")


def _finished(window, address: str, code: int) -> None:
    """The outcome, for the case where the dialog has been closed.

    Harmless when it has not: the dialog says the same thing in its own words
    beside the log, and this is the status bar. What must not happen is an
    attempt that ends with nobody told.
    """
    if code == 0:
        return                          # `_added` has already said it, better
    window.status_message.setText(
        f"{address} was not added, and nothing was written. File ▸ Add mail "
        f"account… to see why.")


def _open_browser(window, url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))
    window.status_message.setText(
        "A browser was opened to authorise the account. corMani is waiting for "
        "it to come back.")
