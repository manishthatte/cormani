# SPDX-License-Identifier: GPL-3.0-or-later
#
# Adding an account without freezing the window.
#
# The whole of what adding an account MEANS is `cormani/configure.py`, and
# nothing of it is repeated here. That was settled before either half existed:
# the prompts are injected parameters so that the dialog can call the same
# function with its own values, and the verify-before-writing order — obtain
# the credential, connect, list the folders, and only then write the row and
# the secret — is the part that must not be lost by having a second
# implementation behind a window.
#
# So this module is glue, and it has the same three obligations `ui/syncing.py`
# has:
#
# THE WORK HAPPENS ON A THREAD, because all of it is network. A browser sign-in
# waits up to five minutes for the redirect; an IMAP connection to a server
# that is not answering waits for its own timeout; the folder listing that
# follows is another round trip. Done on the GUI thread that is a window which
# has stopped repainting.
#
# NOTHING HERE TOUCHES A WIDGET. The worker turns everything into a signal —
# including the request to open a browser, which is a GUI act and belongs to
# the thread that has a display. Qt queues a cross-thread connection by itself.
#
# THE WORKER OPENS ITS OWN DATABASE CONNECTION, or rather `configure` does: it
# calls `app.current_paths()` and opens the store itself, which is the same
# reason `ui/syncing.py` gives — an sqlite3 connection belongs to the thread
# that made it, and handing the interface's handle to a worker is how a mail
# client corrupts its own store.
#
# ONE AT A TIME. `start` returns False when an attempt is already running, and
# the caller says so rather than raising. Two at once would be two browser
# windows and two half-answered consent screens.
#
# AND IT CANNOT BE CANCELLED, WHICH IS SAID RATHER THAN PRETENDED. The sign-in
# waits on `oauth.wait_for_code`, which is a one-request HTTP server inside
# `handle_request`; there is no flag it consults and closing its socket from
# another thread is a race. Closing the dialog therefore stops the WATCHING and
# not the attempt — the outcome arrives in the status bar instead. Nothing is
# left half-made either way, because nothing is written until the server has
# accepted the credential.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QThread, Signal

from .. import configure

# What `_Worker` reports when `add_account` raised instead of returning. Its
# own codes are 0 and 1, and both of them mean something precise; see `run`.
UNEXPECTED = 2


@dataclass(frozen=True)
class Request:
    """One filled-in form, on its way to `configure.add_account`.

    THE SECRET IS IN THE REPR OF NOTHING. `field(repr=False)` is why: this
    object crosses a thread boundary and would otherwise print an app password
    into any log line, exception or debugger that touched it, which
    CONVENTIONS.txt §7 forbids without qualification. It is held here for the
    few seconds between the form and `ask_secret`, and the keyring is the only
    place it comes to rest.
    """

    address: str
    provider: str = ""
    auth: str = ""
    display_name: str = ""
    imap_host: str = ""
    imap_port: int = 0
    smtp_host: str = ""
    smtp_port: int = 0
    secret: str = field(default="", repr=False)


class _Worker(QObject):
    """Lives on the setup thread. Owns nothing the interface can see."""

    said = Signal(str)                  # one line of `configure`'s commentary
    browser_wanted = Signal(str)        # a URL, for the thread that has a screen
    done = Signal(int)                  # `add_account`'s exit code

    def __init__(self, request: Request) -> None:
        super().__init__()
        self._request = request

    def run(self) -> None:
        r = self._request
        try:
            code = configure.add_account(
                r.address, provider=r.provider, auth=r.auth,
                display_name=r.display_name,
                imap_host=r.imap_host, imap_port=r.imap_port,
                smtp_host=r.smtp_host, smtp_port=r.smtp_port,
                ask=self._ask, ask_secret=self._ask_secret,
                open_browser=self.browser_wanted.emit,
                out=self.said.emit)
        except Exception as exc:
            # `add_account` reports the failures it expects and returns 1. This
            # is for the ones it does not — a keyring that refuses to store the
            # password after the server accepted it, for one — which must reach
            # the window as a sentence rather than as a dead thread and a
            # dialog that never finishes.
            #
            # AND IT IS CODE 2, NOT 1, WHICH IS THE HONEST PART. Every failure
            # `add_account` REPORTS carries a guarantee with it: nothing was
            # written, no half-made row and no keyring entry for an address
            # that does not work. An exception it did not expect carries no
            # such guarantee, and a window saying "nothing was written" over
            # one would be saying something it does not know.
            self.said.emit(f"{exc.__class__.__name__}: {exc}")
            self.done.emit(UNEXPECTED)
            return
        self.done.emit(code)

    def _ask(self, prompt: str, default: str = "") -> str:
        """A prompt reaching here is one the form did not cover.

        The form collects every value `add_account` can ask for, so this is not
        expected to run at all. It answers with the default rather than
        raising, which is what a terminal would do for somebody pressing
        Return, and `add_account` then reports the missing value in its own
        words — "an IMAP host is needed" — rather than in a traceback.
        """
        if not default:
            self.said.emit(f"{prompt}: nothing was given")
        return default

    def _ask_secret(self, _prompt: str) -> str:
        """The app password the form collected. Never emitted, never logged."""
        return self._request.secret


class AccountSetup(QObject):
    """Runs one `add_account` at a time and reports on it.

    Parented to the WINDOW rather than to the dialog, deliberately. The dialog
    can be closed while an attempt is in flight — see the note at the top about
    cancellation — and a controller owned by it would be destroyed underneath a
    thread that is still running.
    """

    said = Signal(str)
    browser_wanted = Signal(str)
    finished = Signal(str, int)         # the address, and the exit code

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._address = ""

    @property
    def running(self) -> bool:
        return self._thread is not None

    @property
    def address(self) -> str:
        """Which address is being added, for a caller that has to say so."""
        return self._address

    def start(self, request: Request) -> bool:
        """Begin. False when an attempt is already under way."""
        if self.running:
            return False
        thread = QThread()
        worker = _Worker(request)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.said.connect(self.said)
        worker.browser_wanted.connect(self.browser_wanted)
        worker.done.connect(self._done)
        self._thread, self._worker, self._address = thread, worker, request.address
        thread.start()
        return True

    def stop(self) -> None:
        """Wait for the attempt to end. Called when the window closes.

        Bounded, and it does not interrupt anything: there is nothing in the
        sign-in or the IMAP connection that watches for a request to stop, so
        the choice is between waiting and abandoning. It waits the same thirty
        seconds `ui/syncing.py` waits, which covers every case but a browser
        sign-in nobody is answering — and abandoning that is safe, because the
        account is written only after the server has accepted the credential.
        """
        thread = self._thread
        if thread is None:
            return
        thread.quit()
        thread.wait(30000)
        self._retire()

    def _done(self, code: int) -> None:
        address = self._address
        self._retire()
        self.finished.emit(address, int(code))

    def _retire(self) -> None:
        thread, self._thread, self._worker = self._thread, None, None
        self._address = ""
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(5000)
