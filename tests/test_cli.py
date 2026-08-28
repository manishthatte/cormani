# SPDX-License-Identifier: GPL-3.0-or-later
#
# The command line, which is the half of corMani that runs without a display.
#
# `--check` is the first thing to run when something is wrong, so it must work
# when everything is wrong: no store, no credential, no network. `--sync` is
# how a first import is actually run — for hours, in a terminal, surviving the
# window being closed — and how a scheduled run reports that it failed.
#
# Every test redirects the XDG variables, so nothing here can touch the real
# store or the real configuration.
#
# © Manish Jagdish Thatte
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support

from cormani import __main__ as entry
from cormani import cli, configure


class Fixture(unittest.TestCase):
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

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = entry.main(list(argv))
        return code, out.getvalue()

    def store(self):
        from cormani.platform.paths import Paths
        from cormani.store import database
        paths = Paths().ensure()
        con = database.open_store(paths.database)
        self.addCleanup(con.close)
        return con


class TestCheck(Fixture):
    def test_it_works_with_no_store_at_all(self):
        # The first thing to run when something is wrong must work when
        # everything is wrong.
        code, text = self.run_cli("--check")
        self.assertIn("not created yet", text)
        self.assertIn("verdict", text)

    def test_it_reports_the_schema_and_the_accounts(self):
        from cormani.store.accounts import add_account
        add_account(self.store(), "owner@manitlab.example", "google")
        code, text = self.run_cli("--check")
        # The version, from the schema itself rather than from a number typed
        # here: this assertion is about --check reporting what it found, and
        # every migration would otherwise break a test about something else.
        from cormani.store import schema
        self.assertIn(f"schema v{schema.LATEST_VERSION}, 1 accounts", text)
        self.assertIn("owner@manitlab.example", text)

    def test_it_says_which_accounts_have_no_credential(self):
        from cormani.store.accounts import add_account
        add_account(self.store(), "owner@manitlab.example", "google")
        code, text = self.run_cli("--check")
        self.assertIn("no credential stored", text)

    def test_it_never_asks_the_network_whether_a_credential_works(self):
        # `--check` has to run when the network is the problem.
        from cormani.auth import credentials
        from cormani.store.accounts import add_account
        add_account(self.store(), "owner@manitlab.example", "google")
        credentials.set_password("owner@manitlab.example", "pw")
        code, text = self.run_cli("--check")
        self.assertNotIn("no credential stored", text)

    def test_it_reports_the_offline_queue(self):
        from cormani.store import edits, ingest, messages
        from cormani.store.accounts import add_account
        from cormani.store.folders import ensure_folder
        from cormani.imap import envelope
        con = self.store()
        account = add_account(con, "owner@manitlab.example", "google")
        folder = ensure_folder(con, account, "INBOX", role="inbox")
        stored = ingest.store_message(
            con, folder, 7, envelope.read(b"From: a@x\r\nSubject: s\r\n\r\nb\r\n"))
        edits.set_seen(con, [stored.message_id], True)
        code, text = self.run_cli("--check")
        self.assertIn("offline queue    1 waiting", text)

    def test_it_says_why_an_account_is_being_held_back(self):
        from cormani.store.accounts import add_account
        con = self.store()
        add_account(con, "owner@manitlab.example", "google")
        con.execute("UPDATE account SET last_error = '[OVERQUOTA] Daily limit "
                    "exceeded', next_attempt_at = '2026-08-26T00:00:00+00:00'")
        con.commit()
        code, text = self.run_cli("--check")
        self.assertIn("waiting until 2026-08-26", text)
        self.assertIn("OVERQUOTA", text)


class TestSync(Fixture):
    def test_with_no_store_it_says_so_and_fails(self):
        code, text = self.run_cli("--sync")
        self.assertEqual(code, 1)
        self.assertIn("no store yet", text)

    def test_an_empty_store_says_there_are_no_accounts(self):
        # Not "every one is disabled or waiting", which sends someone looking
        # for a disabled account that was never there.
        self.store()
        code, text = self.run_cli("--sync")
        self.assertEqual(code, 0)
        self.assertIn("no accounts are configured", text)

    def test_with_nothing_due_it_says_so_and_succeeds(self):
        from cormani.store.accounts import add_account
        con = self.store()
        add_account(con, "owner@manitlab.example", "google")
        con.execute("UPDATE account SET enabled = 0")
        con.commit()
        code, text = self.run_cli("--sync")
        self.assertEqual(code, 0)
        self.assertIn("no account is due", text)

    def test_a_failing_account_makes_the_exit_code_non_zero(self):
        # So that a scheduled run can be noticed.
        from cormani.store.accounts import add_account
        add_account(self.store(), "owner@manitlab.example", "google")
        code, text = self.run_cli("--sync")
        self.assertEqual(code, 1)
        self.assertIn("FAILED", text)
        self.assertIn("no credential", text)
        self.assertIn("next attempt", text)

    def test_a_failure_parks_the_account_so_the_next_run_leaves_it_alone(self):
        from cormani.store.accounts import add_account
        add_account(self.store(), "owner@manitlab.example", "google")
        self.run_cli("--sync")
        code, text = self.run_cli("--sync")
        self.assertEqual(code, 0)
        self.assertIn("no account is due", text)

    def test_quiet_prints_the_summary_and_not_the_running_commentary(self):
        from cormani.store.accounts import add_account
        add_account(self.store(), "owner@manitlab.example", "google")
        _, loud = self.run_cli("--sync")
        self.run_cli("--sync")               # park it again for a clean compare
        from cormani.platform.paths import Paths
        from cormani.store import database
        con = database.connect(Paths().database)
        con.execute("UPDATE account SET next_attempt_at = NULL")
        con.commit()
        con.close()
        _, quiet = self.run_cli("--sync", "--quiet")
        self.assertIn("…", loud)
        self.assertNotIn("…", quiet)
        self.assertIn("FAILED", quiet)


class TestArguments(unittest.TestCase):
    def test_the_version_is_reported(self):
        with self.assertRaises(SystemExit) as caught:
            entry.main(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_a_misspelled_option_is_refused_not_passed_to_qt(self):
        # `parse_known_args` alone hands an unknown --option to Qt, which
        # ignores it and opens a window — so a typo appears to have worked.
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stderr(err):
                entry.main(["--add-acount", "owner@manitlab.example"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--add-acount", err.getvalue())

    def test_import_thunderbird_without_into_is_refused(self):
        # Was the placeholder that held the unimplemented switch: now the
        # switch exists, and what it still refuses is running without --into.
        err = io.StringIO()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = entry.main(["--import-thunderbird"])
        self.assertEqual(code, 1)
        self.assertIn("--into", out.getvalue())

    def test_interrupting_a_prompt_is_not_a_crash(self):
        # Ctrl-C at a password prompt is a person saying "not now". A stack
        # trace makes it look as though something broke and buries the one
        # fact that matters: nothing was written.
        from cormani import configure as cli_mod
        err = io.StringIO()
        with mock.patch.object(cli_mod, "_ask_secret", side_effect=KeyboardInterrupt):
            with redirect_stderr(err):
                code = entry.main(["--add-account", "someone@gmail.com",
                                   "--auth", "password"])
        self.assertEqual(code, 130, "the shell's convention for SIGINT")
        self.assertIn("nothing was changed", err.getvalue())

    def test_qt_switches_pass_through_untouched(self):
        # Qt reads its own switches from argv and they take a single dash.
        parser = entry.build_parser()
        args, rest = parser.parse_known_args(
            ["--check", "-style", "Fusion", "-platform", "offscreen"])
        entry._reject_unknown(parser, rest)          # must not raise
        self.assertEqual(rest, ["-style", "Fusion", "-platform", "offscreen"])


class TestTheStoreLocationOverride(Fixture):
    """`data_dir` must mean the same thing to the command line as to the window.

    It did not. `app.build_paths` applied the override for `app.run`, and
    every command-line entry point built a bare `Paths()` instead — so with an
    override set there were two stores, and `--add-account` wrote into the one
    the application never opens. `config/settings.py` offers the setting as
    "point it at an encrypted volume if you would rather", which makes the
    consequence of ignoring it mail fetched onto the unencrypted default path.
    """

    def override_to(self, where: Path) -> None:
        from cormani.platform.paths import Paths

        config_file = Paths().config_file
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(f'data_dir = "{where}"\n', encoding="utf-8")

    def test_the_configured_directory_is_the_one_the_cli_uses(self):
        from cormani.app import current_paths

        where = Path(self._tmp.name) / "encrypted-volume"
        self.override_to(where)
        self.assertEqual(current_paths().data, where)
        self.assertEqual(current_paths().database, where / "cormani.sqlite3")

    def test_check_reports_the_override_and_not_the_default(self):
        where = Path(self._tmp.name) / "encrypted-volume"
        self.override_to(where)
        code, text = self.run_cli("--check")
        self.assertEqual(code, 0)
        self.assertIn(f"data dir         {where}", text)

    def test_panel_sessions_follow_the_store(self):
        """They are DATA — cookies and logins — so an override that moves the
        store onto an encrypted volume must move the signed-in sessions with
        it, or the sessions stay behind on the unencrypted disk."""
        from cormani.app import current_paths

        where = Path(self._tmp.name) / "encrypted-volume"
        self.override_to(where)
        self.assertEqual(current_paths().web_profiles, where / "web")

    def test_saying_nothing_leaves_the_standard_location_alone(self):
        from cormani.app import current_paths
        from cormani.platform.paths import Paths

        self.assertEqual(current_paths().data, Paths().data)

    def test_asking_where_things_are_does_not_create_them(self):
        """`--check` is a diagnostic and must not bring into existence the
        thing it was asked to report on."""
        from cormani.app import current_paths

        where = Path(self._tmp.name) / "encrypted-volume"
        self.override_to(where)
        paths = current_paths()
        self.assertFalse(where.exists())
        self.assertFalse(paths.web_profiles.exists())


class TestProviderInference(unittest.TestCase):
    def test_the_obvious_consumer_domains(self):
        self.assertEqual(configure.infer_provider("user@gmail.com"), "google")
        self.assertEqual(configure.infer_provider("user@outlook.com"), "microsoft")
        self.assertEqual(configure.infer_provider("user@hotmail.com"), "microsoft")

    def test_a_custom_domain_says_nothing(self):
        # owner@manitlab.example IS Google Workspace and manitlab.org is in no
        # list. A wrong guess means the wrong hostnames and the wrong
        # mechanism, and the failure looks like a rejected password.
        self.assertEqual(configure.infer_provider("owner@manitlab.example"), "")
        self.assertEqual(configure.infer_provider("admin@nanomani.example"), "")

    def test_it_is_case_and_whitespace_tolerant(self):
        self.assertEqual(configure.infer_provider("  User@GMail.com "), "google")


class TestAddAccount(Fixture):
    """Adding an account against the IMAP server in tests/fakeimap.py."""

    def setUp(self):
        super().setUp()
        import fakeimap
        from cormani.imap import client as client_mod

        self.server = fakeimap.Server()
        self.server.passwords["owner@manitlab.example"] = "apppassword"
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

        patcher = mock.patch.object(client_mod.Connection, "connect",
                                    staticmethod(connect))
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, address="owner@manitlab.example", secret="apppassword", **kwargs):
        kwargs.setdefault("provider", "google")
        kwargs.setdefault("auth", "password")
        out = io.StringIO()
        with redirect_stdout(out):
            code = configure.add_account(address, ask=lambda p, d="": d,
                                   ask_secret=lambda p: secret, **kwargs)
        return code, out.getvalue()

    def accounts(self):
        from cormani.store.accounts import list_accounts
        return list_accounts(self.store())

    def test_a_good_password_adds_the_account_and_its_folders(self):
        code, text = self.add()
        self.assertEqual(code, 0, text)
        self.assertEqual([a.address for a in self.accounts()],
                         ["owner@manitlab.example"])
        self.assertIn("4 mailboxes", text)
        # The roles matter more than the count: they are what makes "archive
        # this" work across accounts whose folders have different names.
        self.assertIn("archive  [Gmail]/All Mail", text)
        self.assertIn("inbox    INBOX", text)

    def test_the_password_is_stored_only_in_the_keyring(self):
        self.add()
        from cormani.secrets import store as secrets
        self.assertTrue(secrets.has_secret("owner@manitlab.example", "app-password"))
        blob = repr(self.keyring.data)
        self.assertIn("apppassword", blob)
        rows = self.store().execute("SELECT * FROM account").fetchall()
        self.assertNotIn("apppassword", repr([dict(r) for r in rows]))

    def test_the_spaces_google_prints_in_an_app_password_are_ignored(self):
        code, text = self.add(secret="appp assw ord ")
        self.assertEqual(code, 0, text)

    def test_a_rejected_password_writes_nothing_at_all(self):
        # A failed setup must leave no half-made account and no keyring entry
        # for an address that does not work.
        from cormani.secrets import store as secrets
        code, text = self.add(secret="wrong")
        self.assertEqual(code, 1)
        self.assertIn("refused", text)
        self.assertEqual(self.accounts(), [])
        self.assertFalse(secrets.has_secret("owner@manitlab.example", "app-password"))

    def test_an_unreachable_server_writes_nothing_either(self):
        self.server.drop_after = 0
        code, text = self.add()
        self.assertEqual(code, 1)
        self.assertEqual(self.accounts(), [])

    def test_the_same_address_is_not_added_twice(self):
        self.add()
        code, text = self.add()
        self.assertEqual(code, 1)
        self.assertIn("already configured", text)
        self.assertEqual(len(self.accounts()), 1)

    def test_a_custom_domain_needs_the_provider_named(self):
        code, text = self.add(provider="")
        self.assertEqual(code, 1)
        self.assertIn("--provider", text)
        self.assertEqual(self.accounts(), [])

    def test_a_consumer_domain_infers_it(self):
        self.server.passwords["user@gmail.com"] = "apppassword"
        code, text = self.add(address="user@gmail.com", provider="")
        self.assertEqual(code, 0, text)
        self.assertIn("provider: google", text)

    def test_microsoft_refuses_an_app_password_before_connecting(self):
        # Basic authentication is withdrawn; offering it offers something that
        # cannot work.
        code, text = self.add(address="user@hotmail.com",
                              provider="microsoft", auth="password")
        self.assertEqual(code, 1)
        self.assertIn("oauth2", text)
        self.assertEqual(self.server.log, [], "nothing was even attempted")

    def test_the_providers_default_hostname_is_used_and_recorded(self):
        self.add()
        row = self.store().execute(
            "SELECT imap_host, imap_port, smtp_host, auth_method "
            "FROM account").fetchone()
        self.assertEqual(row["imap_host"], "imap.gmail.com")
        self.assertEqual(row["imap_port"], 993)
        self.assertEqual(row["smtp_host"], "smtp.gmail.com")
        self.assertEqual(row["auth_method"], "password")

    def test_an_explicit_host_overrides_the_default(self):
        self.add(imap_host="mail.example.org", imap_port=1993)
        row = self.store().execute(
            "SELECT imap_host, imap_port FROM account").fetchone()
        self.assertEqual((row["imap_host"], row["imap_port"]),
                         ("mail.example.org", 1993))

    def test_it_tells_you_what_to_run_next(self):
        _, text = self.add()
        self.assertIn("--sync", text)

    def test_a_nonsense_address_is_refused(self):
        code, text = self.add(address="not-an-address")
        self.assertEqual(code, 1)
        self.assertIn("not an email address", text)


class TestResync(Fixture):
    def test_an_unknown_account_is_refused(self):
        self.store()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.resync("nobody@example.org")
        self.assertEqual(code, 1)
        self.assertIn("not configured", out.getvalue())

    def test_it_discards_the_cache_and_resets_the_watermark(self):
        from cormani.imap import envelope
        from cormani.store import folders as folders_repo
        from cormani.store import ingest
        from cormani.store.accounts import add_account
        from cormani.store.folders import ensure_folder

        con = self.store()
        account = add_account(con, "owner@manitlab.example", "google")
        folder = ensure_folder(con, account, "INBOX", role="inbox")
        for uid in (1, 2, 3):
            ingest.store_message(con, folder, uid, envelope.read(
                b"From: a@x\r\nSubject: s\r\n\r\nbody\r\n"))
        folders_repo.record_sync_state(con, folder, uid_next=4, uid_validity=99)

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.resync("owner@manitlab.example")
        self.assertEqual(code, 0)
        self.assertIn("discarded 3", out.getvalue())
        self.assertEqual(con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 0)
        state = folders_repo.sync_state(con, folder)
        self.assertIsNone(state["uid_next"], "the next sync starts from the top")
        self.assertEqual(state["uid_validity"], 99, "the server's own value stays")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0], 0)

    def test_it_restores_the_first_sync_window(self):
        # Without this, --resync silently turns "fetch the last ninety days
        # again" into "fetch ten years": engine._since only windows an account
        # that has never finished a sync. Observed on a live account, where a
        # re-fetch of 77 messages became 1,339 and counting.
        from cormani.imap.engine import Engine, Options
        from cormani.store.accounts import add_account, list_accounts

        con = self.store()
        add_account(con, "owner@manitlab.example", "google")
        con.execute("UPDATE account SET last_sync_at = '2026-08-25T04:00:00+00:00'")
        con.commit()
        account = list_accounts(con)[0]

        engine = Engine(con, options=Options(initial_days=90))
        self.assertIsNone(engine._since(account),
                          "an account that has synced is not windowed")

        out = io.StringIO()
        with redirect_stdout(out):
            cli.resync("owner@manitlab.example")
        self.assertIn("window applies again", out.getvalue())
        self.assertIsNotNone(Engine(con, options=Options(initial_days=90))
                             ._since(account), "and now it is again")

    def test_it_refuses_while_changes_are_still_unsent(self):
        # Discarding would throw away the rows those ops point at before the
        # server has been told.
        from cormani.imap import envelope
        from cormani.store import edits, ingest
        from cormani.store.accounts import add_account
        from cormani.store.folders import ensure_folder

        con = self.store()
        account = add_account(con, "owner@manitlab.example", "google")
        folder = ensure_folder(con, account, "INBOX", role="inbox")
        stored = ingest.store_message(con, folder, 1, envelope.read(
            b"From: a@x\r\nSubject: s\r\n\r\nbody\r\n"))
        edits.set_seen(con, [stored.message_id], True)

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.resync("owner@manitlab.example")
        self.assertEqual(code, 1)
        self.assertIn("--sync first", out.getvalue())
        self.assertEqual(con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 1)


class TestOAuthRegistration(Fixture):
    def test_it_is_recorded_once_per_provider(self):
        from cormani.auth import credentials
        out = io.StringIO()
        with redirect_stdout(out):
            code = configure.set_oauth("google", ask=lambda p, d="": "the-client-id",
                                 ask_secret=lambda p: "the-secret")
        self.assertEqual(code, 0)
        self.assertEqual(credentials.registration("google"),
                         ("the-client-id", "the-secret"))
        self.assertIn("every Google account", out.getvalue())

    def test_it_warns_that_testing_mode_expires_refresh_tokens(self):
        out = io.StringIO()
        with redirect_stdout(out):
            configure.set_oauth("google", ask=lambda p, d="": "id",
                          ask_secret=lambda p: "")
        self.assertIn("seven days", out.getvalue())

    def test_a_provider_with_no_oauth_says_so(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = configure.set_oauth("imap", ask=lambda p, d="": "x",
                                 ask_secret=lambda p: "")
        self.assertEqual(code, 1)

    def test_nothing_is_recorded_without_a_client_id(self):
        from cormani.auth import credentials
        out = io.StringIO()
        with redirect_stdout(out):
            code = configure.set_oauth("google", ask=lambda p, d="": "",
                                 ask_secret=lambda p: "")
        self.assertEqual(code, 1)
        self.assertEqual(credentials.registration("google"), ("", ""))


if __name__ == "__main__":
    unittest.main()
