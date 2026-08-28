# SPDX-License-Identifier: GPL-3.0-or-later
#
# First-run wizard.
#
# Shown once when `QSettings` has no `first_run` flag — keyring check, optional
# OAuth registration, then add an account or open demo data. After that the
# flag is set and the ordinary window is enough.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (QCheckBox, QLabel, QVBoxLayout, QWizard,
                               QWizardPage)

from .. import APP_NAME
from ..secrets import store as secrets
from . import accounthost

SETTINGS_FIRST_RUN = "onboarding/first_run_done"


class _WelcomePage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle(f"Welcome to {APP_NAME}")
        self.setSubTitle(
            "Mail, calendar and correspondence tracking in one window.")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "This short setup checks that this machine can keep account "
            "passwords safely, and offers to add your first account."))
        self._keyring = QLabel("", self)
        layout.addWidget(self._keyring)
        layout.addStretch(1)

    def initializePage(self) -> None:
        if secrets.available():
            self._keyring.setText(
                f"A system keyring is available ({secrets.backend_name()}). "
                f"Account passwords will be stored there.")
        else:
            self._keyring.setText(
                "No system keyring was found. You can read mail already "
                "downloaded, but adding an account needs a keyring service "
                "such as GNOME Keyring or KWallet.")


class _OAuthPage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("OAuth registration")
        self.setSubTitle(
            "Google and Microsoft accounts need this installation's own OAuth "
            "client id, recorded once in the keyring.")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "If you plan to add a Google or Microsoft account, register OAuth "
            "now. You can also do this later from File ▸ OAuth registration."))
        self._register = QCheckBox("Register OAuth before I add an account", self)
        self._register.setChecked(True)
        layout.addWidget(self._register)
        layout.addStretch(1)

    def isComplete(self) -> bool:
        return True


class _AccountPage(QWizardPage):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Your first account")
        self.setSubTitle(
            "Add a mail account now, or explore with demo data first.")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Demo data is a disposable store in the cache directory. Your real "
            "mail is untouched."))
        self._demo = QCheckBox("Open demo data instead of adding an account now",
                               self)
        layout.addWidget(self._demo)
        layout.addStretch(1)

    def isComplete(self) -> bool:
        return True


class OnboardingWizard(QWizard):
    def __init__(self, window, parent=None) -> None:
        super().__init__(parent or window)
        self._window = window
        self.setWindowTitle(f"Set up {APP_NAME}")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.addPage(_WelcomePage(self))
        self._oauth = _OAuthPage(self)
        self.addPage(self._oauth)
        self._account = _AccountPage(self)
        self.addPage(self._account)

    def accept(self) -> None:
        QSettings().setValue(SETTINGS_FIRST_RUN, True)
        super().accept()
        if self._oauth._register.isChecked() and not self._window._demo:
            accounthost.record_registration(self._window)
        if self._account._demo.isChecked():
            self._window.status_message.setText(
                "Explore with demo data from Help ▸ About, or add an account "
                "from File when you are ready.")
        elif not self._window._demo:
            accounthost.add_account(self._window)


def should_show() -> bool:
    value = QSettings().value(SETTINGS_FIRST_RUN)
    return value not in (True, "true", "True", 1, "1")


def maybe_show(window) -> None:
    if window._demo or not should_show():
        QSettings().setValue(SETTINGS_FIRST_RUN, True)
        return
    wizard = OnboardingWizard(window, parent=window)
    wizard.exec()
