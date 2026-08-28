# SPDX-License-Identifier: GPL-3.0-or-later
#
# Adding an account from the window.
#
# `tests/test_cli.py::TestAddAccount` is what adding an account MEANS, against
# the IMAP server in tests/fakeimap.py, and none of it is repeated here: this
# is the window's half — what the form resolves to, what it refuses before
# touching the network, and that the thread between the two carries the same
# result back.
#
# ── THE FORM IS READ, NEVER LOOKED AT ──────────────────────────────────────
#
# `tests/test_contactsui.py` records why: `isVisible()` is False for every
# widget whose window has not been shown, and this suite shows none of them.
# Every assertion below reads `request()`, `problem()` or a model, and each of
# those is what the dialog itself uses — so a test cannot pass over a form the
# application would read differently.
#
# ── AND THE REFUSALS ARE ASSERTED AS SENTENCES ─────────────────────────────
#
# `problem()` returning something truthy is not the property worth holding. A
# form that refused every address with "no" would satisfy that; what the user
# needs is the reason, so the assertions name the words that make the sentence
# actionable — the address, the provider, "already configured".
#
# © Manish Jagdish Thatte
import dataclasses
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support

from cormani import configure
from cormani.auth import credentials
from cormani.auth.providers import METHOD_OAUTH2, METHOD_PASSWORD

support.qt_app() if support.HAVE_QT else None


class Fixture(unittest.TestCase):
    """A store, a keyring and an IMAP server, none of them real.

    The XDG redirection is what points `configure.add_account` — which finds
    its own store through `app.current_paths()` — at a temporary directory
    rather than at the developer's mail.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keyring = support.fake_keyring(self)

        import fakeimap
        from cormani.imap import client as client_mod

        self.server = fakeimap.Server()
        self.server.passwords["owner@manitlab.example"] = "apppassword"
        self.server.passwords["user@gmail.com"] = "apppassword"
        self.server.add_mailbox("INBOX", attributes=("\\HasNoChildren",))
        self.server.add_mailbox("[Gmail]/All Mail", attributes=("\\All",))
        self.server.add_mailbox("[Gmail]/Sent Mail", attributes=("\\Sent",))
        self.server.add_mailbox("[Gmail]/Trash", attributes=("\\Trash",))

        original = client_mod.Connection.connect

        def connect(host, port=993, **kwargs):
            kwargs.pop("factory", None)
            kwargs.pop("ssl_context", None)
            kwargs.pop("timeout", None)
            return original(host, port,
                            factory=lambda: fakeimap.IMAP4_Fake(self.server),
                            **kwargs)

        imap_patch = mock.patch.object(client_mod.Connection, "connect",
                                       staticmethod(connect))
        imap_patch.start()
        self.addCleanup(imap_patch.stop)

    def store(self):
        from cormani.platform.paths import Paths
        from cormani.store import database

        con = database.open_store(Paths().ensure().database)
        self.addCleanup(con.close)
        return con

    def accounts(self):
        from cormani.store.accounts import list_accounts
        return [a.address for a in list_accounts(self.store())]

    def dialog(self, con=None):
        from cormani.ui.accountdialog import AddAccountDialog
        from cormani.ui.accountsetup import AccountSetup

        self.setup = AccountSetup()
        self.addCleanup(self.setup.stop)
        return support.own(self, AddAccountDialog(con or self.store(),
                                                  self.setup))

    def spin(self, until, seconds=20.0):
        """Deliver queued signals until something has happened.

        The worker's results reach this thread by QUEUED connection, so they
        arrive only inside an event loop and this suite runs none. That is also
        what makes `running` deterministic in the test below: nothing can
        retire the controller between `start` and the first `processEvents`.
        """
        from PySide6.QtCore import QCoreApplication

        deadline = time.monotonic() + seconds
        while not until() and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        QCoreApplication.processEvents()
        self.assertTrue(until(), "the attempt never finished")


# ------------------------------------------------------------------ the form
@support.requires_qt
class TestTheForm(Fixture):
    def test_the_provider_follows_the_address(self):
        dialog = self.dialog()
        dialog.address.setText("user@gmail.com")
        self.assertEqual(dialog.resolved_provider(), "google")
        # And the provider's own hostnames follow it, which is what makes this
        # a form rather than a questionnaire.
        self.assertEqual(dialog.imap_host.text(), "imap.gmail.com")
        self.assertEqual(dialog.smtp_host.text(), "smtp.gmail.com")
        self.assertEqual(dialog.imap_port.value(), 993)

    def test_a_custom_domain_leaves_the_provider_to_be_chosen(self):
        dialog = self.dialog()
        dialog.address.setText("owner@manitlab.example")
        self.assertEqual(dialog.resolved_provider(), "")
        self.assertIn("cannot tell which provider", dialog.problem())
        dialog.provider.setCurrentIndex(dialog.provider.findData("google"))
        self.assertEqual(dialog.resolved_provider(), "google")
        self.assertEqual(dialog.imap_host.text(), "imap.gmail.com")

    def test_a_typed_host_is_never_overwritten(self):
        """The whole of `_follow`: a default may replace a default, and
        nothing may replace what somebody typed."""
        dialog = self.dialog()
        dialog.address.setText("someone@example.org")
        dialog.provider.setCurrentIndex(dialog.provider.findData("imap"))
        dialog.imap_host.setText("mail.example.org")
        dialog.provider.setCurrentIndex(dialog.provider.findData("google"))
        self.assertEqual(dialog.imap_host.text(), "mail.example.org")

    def test_microsoft_is_not_offered_an_app_password_at_all(self):
        # Basic authentication is withdrawn. Offering it and refusing later is
        # a form that lets somebody fill in a password nothing will accept.
        from cormani.ui.accountdialog import methods_for

        self.assertEqual(methods_for("microsoft"), (METHOD_OAUTH2,))
        dialog = self.dialog()
        dialog.address.setText("user@hotmail.com")
        self.assertEqual(
            [dialog.method.itemData(i) for i in range(dialog.method.count())],
            [METHOD_OAUTH2])

    def test_a_plain_imap_server_is_not_offered_a_browser_sign_in(self):
        from cormani.ui.accountdialog import methods_for

        self.assertEqual(methods_for("imap"), (METHOD_PASSWORD,))
        self.assertEqual(methods_for("google"),
                         (METHOD_OAUTH2, METHOD_PASSWORD))
        self.assertEqual(methods_for(""), (), "nothing to offer until it is known")

    def test_the_choice_survives_correcting_the_provider(self):
        dialog = self.dialog()
        dialog.address.setText("user@gmail.com")
        dialog.method.setCurrentIndex(dialog.method.findData(METHOD_PASSWORD))
        dialog.provider.setCurrentIndex(dialog.provider.findData("imap"))
        dialog.provider.setCurrentIndex(dialog.provider.findData("google"))
        self.assertEqual(dialog.method.currentData(), METHOD_PASSWORD)

    def test_the_request_is_what_the_command_line_would_be_given(self):
        dialog = self.dialog()
        dialog.address.setText("  User@GMail.com ")
        dialog.display_name.setText("Krishna")
        dialog.method.setCurrentIndex(dialog.method.findData(METHOD_PASSWORD))
        dialog.secret.setText("apppassword")
        request = dialog.request()
        self.assertEqual(request.address, "user@gmail.com")
        self.assertEqual(request.provider, "google")
        self.assertEqual(request.auth, METHOD_PASSWORD)
        self.assertEqual(request.display_name, "Krishna")
        self.assertEqual((request.imap_host, request.imap_port),
                         ("imap.gmail.com", 993))
        self.assertEqual((request.smtp_host, request.smtp_port),
                         ("smtp.gmail.com", 587))

    def test_the_app_password_is_in_the_repr_of_nothing(self):
        # CONVENTIONS.txt §7. The request crosses a thread boundary and would
        # otherwise print a credential into any log line that touched it.
        dialog = self.dialog()
        dialog.address.setText("user@gmail.com")
        dialog.method.setCurrentIndex(dialog.method.findData(METHOD_PASSWORD))
        dialog.secret.setText("hunter2-not-in-a-repr")
        request = dialog.request()
        self.assertEqual(request.secret, "hunter2-not-in-a-repr")
        self.assertNotIn("hunter2", repr(request))


# ----------------------------------------------------- refused without dialling
@support.requires_qt
class TestWhatItRefusesBeforeTouchingTheNetwork(Fixture):
    def ready(self, address="owner@manitlab.example", secret="apppassword"):
        dialog = self.dialog()
        dialog.address.setText(address)
        dialog.provider.setCurrentIndex(dialog.provider.findData("google"))
        dialog.method.setCurrentIndex(dialog.method.findData(METHOD_PASSWORD))
        dialog.secret.setText(secret)
        return dialog

    def test_a_complete_form_has_no_problem_and_a_live_button(self):
        dialog = self.ready()
        self.assertEqual(dialog.problem(), "")
        self.assertTrue(dialog.add_button.isEnabled())

    def test_an_empty_address_says_what_to_type(self):
        dialog = self.dialog()
        self.assertIn("Type the address", dialog.problem())
        self.assertFalse(dialog.add_button.isEnabled())

    def test_something_that_is_not_an_address_is_named(self):
        dialog = self.ready(address="not-an-address")
        self.assertIn("not an email address", dialog.problem())

    def test_an_address_already_configured_is_refused_by_name(self):
        from cormani.store.accounts import add_account

        add_account(self.store(), "owner@manitlab.example", "google")
        dialog = self.ready()
        self.assertEqual(dialog.problem(),
                         "owner@manitlab.example is already configured.")

    def test_an_app_password_that_was_not_typed_is_asked_for(self):
        dialog = self.ready(secret="")
        self.assertIn("app password", dialog.problem())
        self.assertIn("myaccount.google.com", dialog.problem(),
                      "where Google keeps them, since that is the question")
        self.assertFalse(dialog.add_button.isEnabled())

    def test_a_browser_sign_in_needs_a_registration_first(self):
        dialog = self.ready()
        dialog.method.setCurrentIndex(dialog.method.findData(METHOD_OAUTH2))
        self.assertIn("No OAuth registration is recorded for Google",
                      dialog.problem())
        # And recording one clears it, without the form being touched again.
        credentials.set_registration("google", "an-id", "a-secret")
        dialog._revalidate()
        self.assertEqual(dialog.problem(), "")

    def test_no_keyring_is_refused_outright(self):
        # corMani keeps credentials in the keyring and nowhere else, so with no
        # backend there is no half-measure to offer.
        from cormani.secrets import store as secrets

        dialog = self.ready()
        with mock.patch.object(secrets, "available", return_value=False):
            self.assertIn("No system keyring is available", dialog.problem())

    def test_a_refused_form_does_not_start_anything(self):
        dialog = self.ready(secret="")
        self.assertFalse(dialog.start())
        self.assertFalse(self.setup.running)
        self.assertEqual(self.accounts(), [])


# ---------------------------------------------------------------- the thread
@support.requires_qt
class TestTheWorker(Fixture):
    """The worker alone, run on this thread. What it carries, not how."""

    def run_worker(self, **fields):
        from cormani.ui.accountsetup import Request, _Worker

        request = Request(address="owner@manitlab.example", provider="google",
                          auth=METHOD_PASSWORD, imap_host="imap.gmail.com",
                          imap_port=993, smtp_host="smtp.gmail.com",
                          smtp_port=587, secret="apppassword")
        request = dataclasses.replace(request, **fields)
        worker = _Worker(request)
        said, codes = [], []
        worker.said.connect(said.append)
        worker.done.connect(codes.append)
        out = io.StringIO()
        # Redirected as a CHECK, not as a capture: the sink exists so that the
        # window can read this without the process's stdout being taken from
        # every other thread, and a line arriving here would mean it does not.
        with redirect_stdout(out):
            worker.run()
        self.assertEqual(out.getvalue(), "",
                         "the commentary went to the sink, not to stdout")
        return said, codes[0]

    def test_it_reports_what_the_command_line_reports(self):
        said, code = self.run_worker()
        self.assertEqual(code, 0, said)
        self.assertEqual(self.accounts(), ["owner@manitlab.example"])
        text = "\n".join(said)
        self.assertIn("4 mailboxes", text)
        self.assertIn("archive  [Gmail]/All Mail", text)
        self.assertIn("inbox    INBOX", text)

    def test_a_rejected_password_writes_nothing_at_all(self):
        from cormani.secrets import store as secrets

        said, code = self.run_worker(secret="wrong")
        self.assertEqual(code, 1)
        self.assertIn("refused", "\n".join(said))
        self.assertEqual(self.accounts(), [])
        self.assertFalse(secrets.has_secret("owner@manitlab.example",
                                            "app-password"))

    def test_the_password_reaches_the_keyring_and_no_line_of_commentary(self):
        said, code = self.run_worker(secret="apppassword")
        self.assertEqual(code, 0)
        self.assertNotIn("apppassword", "\n".join(said))
        self.assertIn("apppassword", repr(self.keyring.data))

    def test_an_unexpected_failure_becomes_a_sentence_and_its_own_code(self):
        """Not a dead thread and a dialog that never finishes.

        `add_account` reports the failures it expects and returns 1; this is
        the other kind — a keyring that refuses to store the password after the
        server has already accepted it, for one.

        AND IT IS A DIFFERENT CODE, which is the point of the test. Code 1
        carries a guarantee: nothing was written. An exception carries none, so
        the window must not be able to say "nothing was written" over it.
        """
        from cormani.ui.accountsetup import UNEXPECTED

        with mock.patch.object(configure, "add_account",
                               side_effect=RuntimeError("the keyring said no")):
            said, code = self.run_worker()
        self.assertEqual(code, UNEXPECTED)
        self.assertNotEqual(UNEXPECTED, 1)
        self.assertIn("RuntimeError: the keyring said no", "\n".join(said))

    def test_a_failed_folder_listing_does_not_unadd_the_account(self):
        """The listing is a SECOND connection, made after the row is written.

        It can fail on its own — a server that dropped the first one, a network
        that went away between the two — and reporting a complete account as a
        failure is worse than not listing its folders, which the next sync does
        again anyway.
        """
        from cormani.imap import folders as folder_sync
        from cormani.imap.errors import Transient

        with mock.patch.object(folder_sync, "sync_folders",
                               side_effect=Transient("the connection dropped")):
            said, code = self.run_worker()
        self.assertEqual(code, 0, said)
        self.assertEqual(self.accounts(), ["owner@manitlab.example"])
        self.assertIn("could not be listed", "\n".join(said))


@support.requires_qt
class TestTheDialogEndToEnd(Fixture):
    """The dialog, the controller, a real thread, and the fake server."""

    def test_it_adds_the_account_and_says_so(self):
        dialog = self.dialog()
        added = []
        dialog.added.connect(added.append)
        dialog.address.setText("owner@manitlab.example")
        dialog.provider.setCurrentIndex(dialog.provider.findData("google"))
        dialog.method.setCurrentIndex(dialog.method.findData(METHOD_PASSWORD))
        dialog.secret.setText("apppassword")

        self.assertTrue(dialog.start())
        # Deterministic: the controller is retired by a queued signal, which
        # cannot be delivered before the first `processEvents` below.
        self.assertTrue(self.setup.running)
        self.assertFalse(self.setup.start(dialog.request()),
                         "one at a time — two sign-ins is two consent screens")
        self.spin(lambda: bool(added))

        self.assertEqual(added, ["owner@manitlab.example"])
        self.assertEqual(self.accounts(), ["owner@manitlab.example"])
        self.assertIn("4 mailboxes", dialog.log.toPlainText())
        self.assertIn("was added", dialog.problem_label.text())
        # Cleared for the next one; the server details stay, because the next
        # account is very often on the same one.
        self.assertEqual(dialog.address.text(), "")
        self.assertEqual(dialog.secret.text(), "")
        self.assertEqual(dialog.imap_host.text(), "imap.gmail.com")

    def test_the_server_details_survive_a_provider_taken_from_the_address(self):
        """The case a screenshot found and the test above could not.

        The test above CHOSE Google in the list, so clearing the address left
        the provider chosen and the hostnames alone. With the provider left at
        its default — "From the address", which is the usual way — clearing the
        address took the resolved provider back to nothing, and `_follow` then
        replaced `imap.gmail.com` with the empty default of no provider at all.
        A form that wipes itself after every success is a form that makes the
        second of fifteen accounts as much work as the first.
        """
        dialog = self.dialog()
        added = []
        dialog.added.connect(added.append)
        dialog.address.setText("user@gmail.com")
        self.assertEqual(dialog.provider.currentData(), "",
                         "the provider is INFERRED here, not chosen")
        dialog.method.setCurrentIndex(dialog.method.findData(METHOD_PASSWORD))
        dialog.secret.setText("apppassword")

        self.assertTrue(dialog.start())
        self.spin(lambda: bool(added))

        self.assertEqual(self.accounts(), ["user@gmail.com"])
        self.assertEqual(dialog.address.text(), "")
        self.assertEqual(dialog.provider.currentData(), "google")
        self.assertEqual(dialog.imap_host.text(), "imap.gmail.com")
        self.assertEqual(dialog.smtp_host.text(), "smtp.gmail.com")
        self.assertEqual(dialog.method.currentData(), METHOD_PASSWORD,
                         "and the way in is still offered")

    def test_a_failure_leaves_the_form_usable_and_says_nothing_was_written(self):
        dialog = self.dialog()
        finished = []
        self.setup.finished.connect(lambda *a: finished.append(a))
        dialog.address.setText("owner@manitlab.example")
        dialog.provider.setCurrentIndex(dialog.provider.findData("google"))
        dialog.method.setCurrentIndex(dialog.method.findData(METHOD_PASSWORD))
        dialog.secret.setText("wrong")

        self.assertTrue(dialog.start())
        self.spin(lambda: bool(finished))

        self.assertEqual(finished, [("owner@manitlab.example", 1)])
        self.assertEqual(self.accounts(), [])
        self.assertIn("nothing was written", dialog.problem_label.text())
        self.assertTrue(dialog.address.isEnabled(),
                        "the form comes back, with what was typed in it")
        self.assertEqual(dialog.address.text(), "owner@manitlab.example")


if __name__ == "__main__":
    unittest.main()
