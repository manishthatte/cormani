# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the account menu entries MEAN, and the registration dialog beside them.
#
# Split from `tests/test_accountsetup.py` when the 600-line rule fired: that
# file is the form, the worker and the dialog; this is the host that hangs them
# on the window, and the registration dialog the host opens. The shared
# Fixture is imported rather than duplicated — it is the same store, keyring
# and IMAP double both halves need, and a second copy would be two places for
# the XDG redirection to drift.
#
# © Manish Jagdish Thatte
import unittest
from unittest import mock

import support

from cormani import configure
from cormani.auth import credentials
from test_accountsetup import Fixture

support.qt_app() if support.HAVE_QT else None


# ------------------------------------------------------------ the registration
@support.requires_qt
class TestTheRegistration(Fixture):
    def registration_dialog(self, provider="google"):
        from cormani.ui.accountdialog import RegistrationDialog

        return support.own(self, RegistrationDialog(provider))

    def test_it_records_one_and_says_what_it_covers(self):
        dialog = self.registration_dialog()
        dialog.client_id.setText("an-id.apps.googleusercontent.com")
        dialog.client_secret.setText("a-secret")
        self.assertTrue(dialog.record())
        self.assertEqual(credentials.registration("google"),
                         ("an-id.apps.googleusercontent.com", "a-secret"))
        self.assertIn("every Google account", dialog.status.text())
        self.assertEqual(dialog.client_secret.text(), "",
                         "a secret is not left sitting in a widget")

    def test_a_provider_with_no_oauth_is_not_in_the_list(self):
        dialog = self.registration_dialog()
        names = [dialog.provider.itemData(i)
                 for i in range(dialog.provider.count())]
        self.assertEqual(sorted(names), ["google", "microsoft"])

    def test_a_blank_client_secret_is_an_answer_and_not_a_prompt(self):
        """The provider may have issued none.

        `set_oauth` asks for anything it was not given, and a terminal prompt
        opened from a window is a window that has stopped responding for
        reasons nobody can see.
        """
        dialog = self.registration_dialog()
        dialog.client_id.setText("an-id")
        with mock.patch.object(configure, "_ask_secret",
                               side_effect=AssertionError("it prompted")):
            self.assertTrue(dialog.record())
        self.assertEqual(credentials.registration("google"), ("an-id", ""))

    def test_it_will_not_record_nothing(self):
        dialog = self.registration_dialog()
        self.assertFalse(dialog.record_button.isEnabled())
        dialog.client_id.setText("an-id")
        self.assertTrue(dialog.record_button.isEnabled())

    def test_it_says_when_one_is_already_recorded(self):
        credentials.set_registration("google", "an-id")
        dialog = self.registration_dialog()
        self.assertIn("already recorded", dialog.status.text())
        self.assertNotIn("an-id", dialog.status.text(),
                         "the keyring is not a place secrets are read out of")


# ------------------------------------------------------------------ the window
@support.requires_qt
class TestTheWindowRoutes(Fixture):
    def window(self, *, demo=False):
        from cormani.ui.window import MainWindow

        con = support.demo_store(self) if demo else support.temp_store(self)
        return support.own(self, MainWindow(con, demo=demo))

    def test_the_menu_entry_is_there_and_live(self):
        window = self.window()
        self.assertTrue(window.act_add_account.isEnabled())
        self.assertIn("Add mail", window.act_add_account.text())

    def test_it_is_refused_over_demo_data(self):
        """The demo window is showing a disposable store in the cache
        directory, and `configure.add_account` writes to the real one — so an
        account added here would be added correctly and appear to vanish."""
        from cormani.ui import accounthost

        window = self.window(demo=True)
        self.assertFalse(window.act_add_account.isEnabled())
        self.assertFalse(accounthost.add_account(window))
        self.assertIn("demo data", window.status_message.text())

    def test_the_rail_asks_through_the_same_action(self):
        """The rail's context menu goes through `act_add_account`.

        One route and one enabled state, which is what makes the demo refusal
        above cover the rail as well: a disabled QAction swallows `trigger()`,
        so there is no second place for the rule to be got wrong.

        The host is patched because the real one opens a modal dialog, and a
        suite that opened one would stop there.
        """
        from cormani.ui import accounthost

        window = self.window()
        with mock.patch.object(accounthost, "add_account") as opened:
            window.mail.rail.add_account_wanted.emit()
        opened.assert_called_once_with(window)

    def test_the_disabled_action_swallows_the_rail_route_too(self):
        window = self.window(demo=True)
        from cormani.ui import accounthost
        with mock.patch.object(accounthost, "add_account") as opened:
            window.mail.rail.add_account_wanted.emit()
        opened.assert_not_called()

    def test_the_entry_opens_the_dialog_over_the_window_s_own_store(self):
        """The one path a modal dialog keeps a suite out of: `exec`.

        Patched here rather than left uncovered, because everything before it
        is what breaks — the controller being made and kept on the window, the
        dialog being built against the store this window opened, the signals
        being connected. `exec` itself is Qt's.
        """
        from cormani.ui import accounthost
        from cormani.ui.accountdialog import AddAccountDialog

        window = self.window()
        with mock.patch.object(AddAccountDialog, "exec",
                               return_value=0) as shown:
            self.assertTrue(accounthost.add_account(window))
        shown.assert_called_once()
        controller = window._account_setup
        self.assertIsNotNone(controller, "the controller outlives the dialog")
        self.addCleanup(controller.stop)
        # And a second opening reuses it rather than making a second thread.
        with mock.patch.object(AddAccountDialog, "exec", return_value=0):
            accounthost.add_account(window)
        self.assertIs(window._account_setup, controller)

    def test_the_registration_entry_opens_its_own_dialog(self):
        from cormani.ui import accounthost
        from cormani.ui.accountdialog import RegistrationDialog

        window = self.window()
        with mock.patch.object(RegistrationDialog, "exec",
                               return_value=0) as shown:
            self.assertTrue(accounthost.record_registration(window))
        shown.assert_called_once()
        self.assertFalse(accounthost.record_registration(self.window(demo=True)))

    def test_an_added_account_reaches_the_rail_without_a_restart(self):
        from cormani.store.accounts import add_account
        from cormani.ui import accounthost

        window = self.window()
        self.assertNotIn("owner@manitlab.example", self.rail_labels(window))
        add_account(window._store, "owner@manitlab.example", "google")
        accounthost._added(window, "owner@manitlab.example")
        self.assertIn("owner@manitlab.example", self.rail_labels(window))
        self.assertIn("F5", window.status_message.text())

    def test_the_empty_rail_says_where_the_command_is(self):
        window = self.window()
        model = window.mail.rail.model_obj
        index = model.index_for_key("hint:accounts")
        self.assertTrue(index.isValid(), "an account-less rail has the hint")
        from PySide6.QtCore import Qt
        self.assertIn("Add mail account",
                      index.data(Qt.ItemDataRole.ToolTipRole) or "")

    @staticmethod
    def rail_labels(window):
        model = window.mail.rail.model_obj

        def walk(parent):
            from PySide6.QtCore import Qt
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                yield str(index.data(Qt.ItemDataRole.DisplayRole))
                yield from walk(index)

        from PySide6.QtCore import QModelIndex
        return list(walk(QModelIndex()))


if __name__ == "__main__":
    unittest.main()
