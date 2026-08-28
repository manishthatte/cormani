# SPDX-License-Identifier: GPL-3.0-or-later
#
# The two dialogs that put an account into corMani.
#
# Until now accounts existed only on the command line, which is defensible for
# a first import — it runs for hours and belongs in a terminal — and indefensible
# for the sixteenth account added a year later. This is that half.
#
# WHAT IT DOES IS `cormani/configure.py`. Not a line of the order in which
# things happen lives here: the credential is obtained, the server is connected
# to, the folders are listed, and the row and the secret are written only after
# the server has accepted — so a failed attempt leaves nothing behind, and it
# leaves nothing behind whether it was started from a terminal or from this
# window. `ui/accountsetup.py` is the thread between the two.
#
# THE BUTTON IS DEAD UNTIL THE FORM IS RIGHT, AND SAYS WHY. Every refusal
# `add_account` can make that is knowable without dialling — no keyring, an
# address that is not one, a provider that cannot be guessed, an address
# already configured, a missing app password, a missing OAuth registration — is
# made here, in a sentence under the form, rather than by starting a network
# attempt that will fail in ten seconds. What is NOT knowable without dialling
# is left to `add_account`, which reports it in the log below.
#
# AN IMPOSSIBLE COMBINATION IS NOT OFFERED RATHER THAN REFUSED. Microsoft
# withdrew basic authentication, so a Microsoft account gets no "app password"
# entry in the list at all; an ordinary IMAP server has no OAuth, so it gets no
# browser entry. The alternative — every choice always present, with a refusal
# after the fact — is a form that lets somebody fill in a password Microsoft
# will never accept.
#
# THE PROVIDER FOLLOWS THE ADDRESS, AND THE HOSTS FOLLOW THE PROVIDER, UNTIL
# SOMEBODY TYPES OVER THEM. `imap.gmail.com` appearing the moment an @gmail.com
# address is typed is what makes this a form rather than a questionnaire; a
# host the user has edited is never overwritten by a later provider change,
# which is the whole of `_follow`.
#
# NOTHING HERE ASKS A WIDGET WHETHER IT IS VISIBLE. `tests/test_contactsui.py`
# records what that costs: `isVisible()` is False for every widget in a window
# that has not been shown, and a dialog read that way loses everything the user
# typed. `request()` and `problem()` are what the tests read, and they are what
# the dialog itself uses.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from .. import configure
from ..auth import credentials
from ..auth import providers
from ..auth.providers import METHOD_OAUTH2, METHOD_PASSWORD
from ..secrets import store as secrets
from ..store import accounts as accounts_repo
from . import accountsetup
from .accountsetup import Request

# "" is a real choice and the default one: the address usually says which
# provider it is, and being asked to repeat what you have just typed is the
# sort of thing that makes a form feel like paperwork.
PROVIDER_CHOICES = (("", "From the address"),
                    ("google", "Google"),
                    ("microsoft", "Microsoft"),
                    ("imap", "Other (IMAP)"))

METHOD_LABELS = {METHOD_OAUTH2: "A browser sign-in (OAuth)",
                 METHOD_PASSWORD: "An app password"}


def methods_for(provider_name: str) -> tuple:
    """The ways this provider can be signed into, most usual first.

    Empty for an unresolved provider, which is not the same as "none": the
    caller has nothing to offer until the address says who it belongs to, and
    a list guessed in the meantime would be a list that changes under the
    user's hand.
    """
    if not provider_name:
        return ()
    spec = providers.get(provider_name)
    out = []
    if spec.supports_oauth:
        out.append(METHOD_OAUTH2)
    if spec.allows_password:
        out.append(METHOD_PASSWORD)
    return tuple(out)


class AddAccountDialog(QDialog):
    """One account: who it is, how to sign in, and where the server is."""

    added = Signal(str)                 # the address, once it is in the store

    def __init__(self, con: sqlite3.Connection, setup, parent=None) -> None:
        super().__init__(parent)
        self._con = con
        self._setup = setup
        self._provider = ""             # what the address and the list resolve to
        self.setWindowTitle("Add a mail account")
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)

        self.address = QLineEdit(self)
        self.address.setPlaceholderText("you@example.org")
        self.address.textChanged.connect(self._address_changed)
        form.addRow("&Address", self.address)

        self.display_name = QLineEdit(self)
        self.display_name.setPlaceholderText("what correspondents see (optional)")
        form.addRow("&Name", self.display_name)

        self.provider = QComboBox(self)
        for key, label in PROVIDER_CHOICES:
            self.provider.addItem(label, key)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        form.addRow("&Provider", self.provider)

        signin = QWidget(self)
        row = QHBoxLayout(signin)
        row.setContentsMargins(0, 0, 0, 0)
        self.method = QComboBox(signin)
        self.method.currentIndexChanged.connect(lambda _: self._method_changed())
        row.addWidget(self.method, 1)
        self.registration = QPushButton("&Registration…", signin)
        self.registration.setToolTip(
            "The OAuth client id this installation signs in with. One Google "
            "Cloud project or Azure app registration covers every account on "
            "that provider")
        self.registration.clicked.connect(self.record_registration)
        row.addWidget(self.registration)
        form.addRow("&Sign in with", signin)

        self.secret = QLineEdit(self)
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("sixteen letters; spaces are ignored")
        self.secret.textChanged.connect(lambda _: self._revalidate())
        form.addRow("App pass&word", self.secret)

        outer.addWidget(self._server_box())

        # The refusal, and the reason for it, in the same place every time.
        self.problem_label = QLabel(self)
        self.problem_label.setWordWrap(True)
        outer.addWidget(self.problem_label)

        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(120)
        self.log.setPlaceholderText(
            "Nothing is written until the server has accepted the credential. "
            "What happens will be reported here.")
        outer.addWidget(self.log, 1)

        buttons = QDialogButtonBox(self)
        # ActionRole, not AcceptRole: pressing it starts a network attempt and
        # must not close the dialog that is about to report on it.
        self.add_button = buttons.addButton(
            "&Add account", QDialogButtonBox.ButtonRole.ActionRole)
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self.start)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        setup.said.connect(self._said)
        setup.finished.connect(self._finished)
        self._refresh_provider()

    def _server_box(self) -> QGroupBox:
        box = QGroupBox("Server", self)
        form = QFormLayout(box)
        self.imap_host = QLineEdit(box)
        self.imap_host.textChanged.connect(lambda _: self._revalidate())
        form.addRow("IMAP &host", self.imap_host)
        self.imap_port = QSpinBox(box)
        self.imap_port.setRange(1, 65535)
        self.imap_port.setValue(993)
        form.addRow("IMAP p&ort", self.imap_port)
        self.smtp_host = QLineEdit(box)
        form.addRow("SMTP h&ost", self.smtp_host)
        self.smtp_port = QSpinBox(box)
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(587)
        form.addRow("SMTP po&rt", self.smtp_port)
        return box

    # --------------------------------------------------------- what it says
    def resolved_provider(self) -> str:
        """The provider this form is about: the one chosen, or the one the
        address gives away. Empty when neither answers."""
        chosen = self.provider.currentData()
        if chosen:
            return str(chosen)
        return configure.infer_provider(self.address.text().strip())

    def request(self) -> Request:
        """The form, as `configure.add_account`'s arguments."""
        return Request(
            address=self.address.text().strip().lower(),
            provider=self.resolved_provider(),
            auth=str(self.method.currentData() or ""),
            display_name=self.display_name.text().strip(),
            imap_host=self.imap_host.text().strip(),
            imap_port=int(self.imap_port.value()),
            smtp_host=self.smtp_host.text().strip(),
            smtp_port=int(self.smtp_port.value()),
            secret=self.secret.text())

    def problem(self) -> str:
        """Why this form cannot be sent yet, or "" when it can.

        Every one of these is knowable without touching the network. The order
        is the order a person fills the form in, so that the sentence under it
        moves forward rather than jumping about.
        """
        if not secrets.available():
            return (f"No system keyring is available ({secrets.backend_name()}). "
                    f"corMani keeps credentials there and nowhere else, so an "
                    f"account cannot be added until one is running.")
        address = self.address.text().strip().lower()
        if not address:
            return "Type the address of the account to add."
        if "@" not in address:
            return f"{address} is not an email address."
        provider = self.resolved_provider()
        if not provider:
            return (f"corMani cannot tell which provider {address} belongs to — "
                    f"a custom domain says nothing. Choose one.")
        if accounts_repo.find_by_address(self._con, address) is not None:
            return f"{address} is already configured."
        spec = providers.get(provider)
        method = str(self.method.currentData() or "")
        if method == METHOD_PASSWORD and not self.secret.text().strip():
            hint = ("Google calls these App Passwords, at "
                    "myaccount.google.com → Security. " if spec.name == "google"
                    else "")
            return f"{hint}An app password for {address} is needed."
        if method == METHOD_OAUTH2 and not credentials.has_registration(spec.name):
            return (f"No OAuth registration is recorded for {spec.label}. One "
                    f"covers every {spec.label} account on this machine — "
                    f"record it with Registration… first.")
        if not self.imap_host.text().strip():
            return "An IMAP host is needed."
        return ""

    # ------------------------------------------------------------ the form
    def _address_changed(self, _text: str) -> None:
        self._refresh_provider()

    def _provider_changed(self, _index: int) -> None:
        self._refresh_provider()

    def _method_changed(self) -> None:
        """An app password has a field; a browser sign-in has a registration."""
        method = str(self.method.currentData() or "")
        self.secret.setEnabled(method == METHOD_PASSWORD)
        self.registration.setEnabled(method == METHOD_OAUTH2)
        self._revalidate()

    def _refresh_provider(self) -> None:
        """Follow the address, and the provider's defaults with it.

        Only when the ANSWER changes, not on every keystroke: refilling four
        fields while somebody types their address would fight the person
        editing one of them.
        """
        provider = self.resolved_provider()
        if provider == self._provider:
            self._revalidate()
            return
        was, self._provider = self._provider, provider
        old, new = providers.get(was), providers.get(provider)
        self._follow(self.imap_host, old.imap_host, new.imap_host)
        self._follow(self.smtp_host, old.smtp_host, new.smtp_host)
        self._follow_port(self.imap_port, old.imap_port, new.imap_port or 993)
        self._follow_port(self.smtp_port, old.smtp_port, new.smtp_port or 587)
        self._fill_methods(provider)
        self._revalidate()

    @staticmethod
    def _follow(widget: QLineEdit, was: str, now: str) -> None:
        """Replace a default with a default. Never replace what was typed."""
        if widget.text().strip() in ("", was):
            widget.setText(now)

    @staticmethod
    def _follow_port(widget: QSpinBox, was: int, now: int) -> None:
        if not was or widget.value() == was:
            widget.setValue(now)

    def _fill_methods(self, provider: str) -> None:
        """The ways in, for this provider only.

        The previous choice is kept when the new provider also offers it, so
        that correcting the provider on a form does not silently move somebody
        from an app password to a browser sign-in.
        """
        wanted = str(self.method.currentData() or "")
        blocked = self.method.blockSignals(True)
        try:
            self.method.clear()
            for method in methods_for(provider):
                self.method.addItem(METHOD_LABELS[method], method)
            index = self.method.findData(wanted)
            self.method.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.method.blockSignals(blocked)
        self._method_changed()

    def _revalidate(self) -> None:
        if self._setup.running:
            return
        problem = self.problem()
        self.problem_label.setText(problem)
        self.add_button.setEnabled(not problem)

    # ----------------------------------------------------------- the attempt
    def start(self) -> bool:
        """Hand the form to the setup thread. False when it did not go."""
        problem = self.problem()
        if problem:
            self.problem_label.setText(problem)
            return False
        if not self._setup.start(self.request()):
            self.problem_label.setText(
                f"{self._setup.address} is still being added. One at a time: "
                f"two sign-ins at once is two half-answered consent screens.")
            return False
        self.log.clear()
        self._enable_form(False)
        self.problem_label.setText(
            "Working. Closing this window does not stop it — the result will "
            "arrive in the status bar.")
        return True

    def _said(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _finished(self, address: str, code: int) -> None:
        self._enable_form(True)
        if code == 0:
            self.added.emit(address)
            # PINNED BEFORE THE ADDRESS GOES, and this is not tidiness. With
            # the provider left at "From the address" — the default, and the
            # usual case — clearing the address takes the resolved provider
            # back to nothing, and the hostnames and the sign-in list go with
            # it. So the provider that was just used is written into the list
            # first, and the server details survive into the next account,
            # which is very often on the same one.
            index = self.provider.findData(self._provider)
            if index >= 0:
                self.provider.setCurrentIndex(index)
            # Cleared so that the next one can be typed straight in — fifteen
            # accounts is the case this application exists for.
            self.address.clear()
            self.display_name.clear()
            self.secret.clear()
            self.problem_label.setText(f"{address} was added. Add another, "
                                       f"or Close.")
            return
        if code == accountsetup.UNEXPECTED:
            # No claim about the store: see `accountsetup.UNEXPECTED`.
            self.problem_label.setText(
                f"{address}: something unexpected went wrong, and the line "
                f"above is all corMani knows. Open this again to see whether "
                f"the address is now configured.")
            return
        self.problem_label.setText(
            f"{address} was not added, and nothing was written. The reason is "
            f"above.")

    def _enable_form(self, on: bool) -> None:
        for widget in (self.address, self.display_name, self.provider,
                       self.method, self.secret, self.imap_host,
                       self.imap_port, self.smtp_host, self.smtp_port,
                       self.registration):
            widget.setEnabled(on)
        if on:
            self._method_changed()          # the secret field is conditional
            self._revalidate()
        else:
            self.add_button.setEnabled(False)

    def record_registration(self) -> None:
        """The OAuth registration dialog, on the provider being added."""
        dialog = RegistrationDialog(self.resolved_provider(), self)
        dialog.exec()
        self._revalidate()


class RegistrationDialog(QDialog):
    """This installation's own OAuth client id, for one provider.

    A SEPARATE THING FROM AN ACCOUNT, and it is worth the second dialog to say
    so: one Google Cloud project and one Azure app registration cover every
    address on that provider — fifteen accounts do not need fifteen of these —
    and they belong to the person running corMani rather than to corMani, which
    is why none ships in the repository.

    NO THREAD. Unlike adding an account this touches nothing but the keyring;
    there is no network in it at all, so there is nothing to wait for.
    """

    def __init__(self, provider_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OAuth registration")
        self.setMinimumWidth(480)

        outer = QVBoxLayout(self)
        note = QLabel(
            "The Google project must be published to Production, not left in "
            "Testing, or its refresh tokens expire every seven days. "
            "Unverified is fine. The application type is “Desktop app”. What "
            "you type here is kept in the system keyring and nowhere else.",
            self)
        note.setWordWrap(True)
        outer.addWidget(note)

        form = QFormLayout()
        outer.addLayout(form)
        self.provider = QComboBox(self)
        for name, spec in providers.PROVIDERS.items():
            if spec.supports_oauth:
                self.provider.addItem(spec.label, name)
        index = self.provider.findData(provider_name)
        if index >= 0:
            self.provider.setCurrentIndex(index)
        self.provider.currentIndexChanged.connect(lambda _: self._show_stored())
        form.addRow("&Provider", self.provider)

        self.client_id = QLineEdit(self)
        self.client_id.textChanged.connect(lambda _: self._revalidate())
        form.addRow("Client &id", self.client_id)
        self.client_secret = QLineEdit(self)
        self.client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret.setPlaceholderText("blank if the provider issued none")
        form.addRow("Client &secret", self.client_secret)

        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.status)

        buttons = QDialogButtonBox(self)
        self.record_button = buttons.addButton(
            "&Record", QDialogButtonBox.ButtonRole.ActionRole)
        self.record_button.clicked.connect(self.record)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._show_stored()

    def provider_name(self) -> str:
        return str(self.provider.currentData() or "")

    def _show_stored(self) -> None:
        """Say whether one is already recorded — never what it is.

        The client id is not much of a secret and is still not printed back:
        the whole point of the keyring is that corMani is not a place secrets
        can be read out of, and a dialog that displays one is one screenshot
        away from being that place.
        """
        name = self.provider_name()
        label = providers.get(name).label
        self.status.setText(
            f"A registration is already recorded for {label}; recording "
            f"another replaces it." if credentials.has_registration(name)
            else f"Nothing is recorded for {label} yet.")
        self._revalidate()

    def _revalidate(self) -> None:
        self.record_button.setEnabled(bool(self.client_id.text().strip()))

    def record(self) -> bool:
        """Write it to the keyring. False when nothing was written."""
        lines: list = []
        code = configure.set_oauth(
            self.provider_name(),
            client_id=self.client_id.text().strip(),
            client_secret=self.client_secret.text().strip(),
            # Both prompts are answered by the form: `set_oauth` asks for
            # anything it was not given, and a blank client secret is a real
            # answer rather than a reason to open getpass on a terminal that
            # this process may not even have.
            ask=lambda _p, default="": default,
            ask_secret=lambda _p: "",
            out=lines.append)
        self.status.setText(lines[-1] if lines else "")
        self.client_secret.clear()
        return code == 0
