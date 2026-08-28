# SPDX-License-Identifier: GPL-3.0-or-later
#
# The schedule: which account, when, and what happens when one refuses.
#
# The back-off cases matter most. A provider counting refusals does not forget
# them because corMani restarted, and getting this wrong is how fifteen
# accounts turn a rate limit into a ban.
#
# WHAT A SYNC DOES WITH THE MAIL once it has fetched it — filing it onto
# tracked threads, and putting it through the filter rules — is
# `test_arrivals.py`. The 600-line rule found that seam, and the code had
# already drawn it: `_file_what_arrived` and `_filter` are siblings in
# `imap/engine.py` and neither is about scheduling.
#
# © Manish Jagdish Thatte
import datetime as dt
import unittest
from pathlib import Path

from enginefixture import NOW, Fixture, raw

from cormani.auth.credentials import NotConfigured
from cormani.imap import errors
from cormani.imap.engine import Engine, Options
from cormani.store import edits


class TestSyncing(Fixture):
    def test_every_account_is_synced(self):
        for address, server in self.servers.items():
            server.add_message("INBOX", raw(f"For {address}"))
        results = self.engine.sync_all()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(sum(r.new for r in results), 2)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM message").fetchone()[0], 2)

    def test_success_clears_the_error_and_records_the_time(self):
        self.engine.sync_all()
        row = self.state("owner@manitlab.example")
        self.assertTrue(row["last_sync_at"])
        self.assertEqual(row["last_error"], "")
        self.assertEqual(row["sync_failures"], 0)
        self.assertIsNone(row["next_attempt_at"])

    def test_progress_is_reported_as_it_goes(self):
        self.servers["owner@manitlab.example"].add_message("INBOX", raw())
        seen = []
        self.engine.sync_all(progress=lambda name, detail: seen.append(name))
        self.assertIn("account:start", seen)
        self.assertIn("folder:done", seen)
        self.assertIn("account:done", seen)

    def test_a_disabled_account_is_not_contacted(self):
        from cormani.store.accounts import set_enabled
        set_enabled(self.con, self.accounts["admin@idlidu.example"], False)
        results = self.engine.sync_all()
        self.assertEqual([r.address for r in results], ["owner@manitlab.example"])

    def test_a_hidden_account_still_syncs(self):
        # Hiding takes an account out of the rail and leaves its mail in the
        # store and in search — which means it has to keep arriving.
        from cormani.store.accounts import set_hidden
        set_hidden(self.con, self.accounts["admin@idlidu.example"], True)
        self.assertEqual(len(self.engine.sync_all()), 2)

    def test_the_queue_drains_before_the_fetch(self):
        # Otherwise the fetch brings an archived message straight back and the
        # archive appears not to have worked.
        address = "owner@manitlab.example"
        self.servers[address].add_message("INBOX", raw())
        self.engine.sync_all()
        message_id = self.con.execute("SELECT id FROM message").fetchone()[0]
        edits.archive(self.con, [message_id])
        result = self.engine.sync_account(self.account(address))
        self.assertEqual(result.sent, 1)
        commands = self.servers[address].log
        move = next(i for i, c in enumerate(commands) if "UID MOVE" in c)
        fetch = next(i for i, c in enumerate(commands) if "UID FETCH" in c
                     and "BODY" in c and i > move) if any(
                         "UID FETCH" in c and "BODY" in c for c in commands[move:]
                     ) else len(commands)
        self.assertLess(move, fetch)
        self.assertEqual(len(self.servers[address].mailboxes["Archive"].messages), 1)

    def test_a_first_sync_can_be_windowed(self):
        # Gmail's daily download cap is the constraint; docs/accounts.txt.
        address = "owner@manitlab.example"
        self.servers[address].add_message(
            "INBOX", raw("Ancient"), internaldate="1-Jan-2019 09:00:00 +0000")
        self.servers[address].add_message(
            "INBOX", raw("Recent"), internaldate="20-Aug-2026 09:00:00 +0000")
        engine = self.make_engine(options=Options(
            attachments_root=Path(self._tmp.name), initial_days=90))
        result = engine.sync_account(self.account(address))
        self.assertEqual(result.new, 1)

    def test_the_window_applies_only_to_a_first_sync(self):
        # Once a folder has a UIDNEXT the window is irrelevant, and applying it
        # anyway would hide mail already fetched.
        address = "owner@manitlab.example"
        engine = self.make_engine(options=Options(
            attachments_root=Path(self._tmp.name), initial_days=90))
        engine.sync_account(self.account(address))
        self.assertIsNone(engine._since(self.account(address)))


class TestFailures(Fixture):
    def test_one_bad_account_does_not_stop_the_others(self):
        # Fifteen accounts where one has a rejected password must still sync
        # the other fourteen.
        self.servers["owner@manitlab.example"].fail_next["LOGIN"] = (
            "NO [AUTHENTICATIONFAILED] Invalid credentials")
        self.servers["admin@idlidu.example"].add_message("INBOX", raw())
        results = {r.address: r for r in self.engine.sync_all()}
        self.assertFalse(results["owner@manitlab.example"].ok)
        self.assertTrue(results["admin@idlidu.example"].ok)
        self.assertEqual(results["admin@idlidu.example"].new, 1)

    def test_a_rejected_credential_parks_the_account_for_a_day(self):
        # The one refresh that might have fixed it has already happened.
        # Retrying is how fifteen accounts get an address blocked.
        address = "owner@manitlab.example"
        self.servers[address].fail_next["LOGIN"] = (
            "NO [AUTHENTICATIONFAILED] Invalid credentials")
        result = self.engine.sync_account(self.account(address))
        self.assertFalse(result.ok)
        self.assertIn("AUTHENTICATIONFAILED", result.error)
        parked = dt.datetime.fromisoformat(self.state(address)["next_attempt_at"])
        self.assertEqual((parked - NOW).total_seconds(), 24 * 3600)

    def test_an_unconfigured_account_is_parked_rather_than_retried(self):
        def refuse(address, provider, **kwargs):
            raise NotConfigured(f"no credential is stored for {address}")
        engine = self.make_engine(resolve=refuse)
        result = engine.sync_account(self.account("owner@manitlab.example"))
        self.assertFalse(result.ok)
        self.assertIn("no credential", result.error)

    def test_a_transient_failure_backs_off_gently_and_doubles(self):
        address = "owner@manitlab.example"
        waits = []
        for _ in range(4):
            self.servers[address].fail_next["LOGIN"] = "NO [UNAVAILABLE] later"
            self.engine.sync_account(self.account(address))
            row = self.state(address)
            waits.append(
                (dt.datetime.fromisoformat(row["next_attempt_at"]) - NOW)
                .total_seconds())
        # UNAVAILABLE is classified as a rate limit, which is the slow ladder.
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(waits[-1], waits[0])
        self.assertEqual(self.state(address)["sync_failures"], 4)

    def test_a_dropped_connection_backs_off_on_the_fast_ladder(self):
        address = "owner@manitlab.example"
        server = self.servers[address]
        server.drop_after = 0
        result = self.engine.sync_account(self.account(address))
        self.assertFalse(result.ok)
        wait = (dt.datetime.fromisoformat(self.state(address)["next_attempt_at"])
                - NOW).total_seconds()
        self.assertEqual(wait, 60, "a minute, then doubling")

    def test_the_back_off_is_capped(self):
        address = "owner@manitlab.example"
        self.con.execute("UPDATE account SET sync_failures = 40 WHERE id = ?",
                         (self.accounts[address],))
        self.con.commit()
        self.servers[address].drop_after = 0
        self.engine.sync_account(self.account(address))
        wait = (dt.datetime.fromisoformat(self.state(address)["next_attempt_at"])
                - NOW).total_seconds()
        self.assertEqual(wait, 3600)

    def test_a_rate_limit_waits_hours_not_minutes(self):
        # A quota is not a fault, and minutes are the wrong unit for one.
        address = "owner@manitlab.example"
        self.servers[address].fail_next["LOGIN"] = (
            "NO [OVERQUOTA] Daily limit exceeded")
        self.engine.sync_account(self.account(address))
        wait = (dt.datetime.fromisoformat(self.state(address)["next_attempt_at"])
                - NOW).total_seconds()
        self.assertGreaterEqual(wait, 30 * 60)

    def test_the_reason_is_recorded_where_the_interface_can_show_it(self):
        address = "owner@manitlab.example"
        self.servers[address].fail_next["LOGIN"] = "NO [UNAVAILABLE] maintenance"
        self.engine.sync_account(self.account(address))
        parked = self.engine.backed_off()
        self.assertIn("maintenance", parked[self.accounts[address]]["error"])
        self.assertEqual(parked[self.accounts[address]]["failures"], 1)

    def test_a_credential_never_reaches_the_recorded_error(self):
        def leak(address, provider, **kwargs):
            raise errors.AuthFailed("auth=Bearer ya29.SECRETTOKEN was refused")
        engine = self.make_engine(resolve=leak)
        engine.sync_account(self.account("owner@manitlab.example"))
        self.assertNotIn("SECRET", self.state("owner@manitlab.example")["last_error"])


class TestScheduling(Fixture):
    def test_a_backed_off_account_is_not_due(self):
        address = "owner@manitlab.example"
        self.servers[address].drop_after = 0
        self.engine.sync_account(self.account(address))
        due = [a.address for a in self.engine.due(now=NOW)]
        self.assertNotIn(address, due)
        self.assertIn("admin@idlidu.example", due)

    def test_it_becomes_due_again_once_the_wait_has_passed(self):
        address = "owner@manitlab.example"
        self.servers[address].drop_after = 0
        self.engine.sync_account(self.account(address))
        later = NOW + dt.timedelta(hours=2)
        self.assertIn(address, [a.address for a in self.engine.due(now=later)])

    def test_the_back_off_survives_a_restart(self):
        # A provider counting refusals does not forget them because corMani
        # restarted. Resetting on relaunch turns a rate limit into a ban.
        address = "owner@manitlab.example"
        self.servers[address].fail_next["LOGIN"] = "NO [OVERQUOTA] later"
        self.engine.sync_account(self.account(address))
        fresh = Engine(self.con, connect=self.connect, resolve=self.resolve,
                       clock=lambda: NOW)
        self.assertNotIn(address, [a.address for a in fresh.due()])

    def test_signing_in_again_clears_the_park(self):
        address = "owner@manitlab.example"
        self.servers[address].fail_next["LOGIN"] = "NO [AUTHENTICATIONFAILED] no"
        self.engine.sync_account(self.account(address))
        self.engine.clear_backoff(self.accounts[address])
        self.assertIn(address, [a.address for a in self.engine.due(now=NOW)])
        self.assertEqual(self.state(address)["last_error"], "")


class TestWatching(Fixture):
    def test_a_message_arriving_during_the_wait_is_reported(self):
        address = "owner@manitlab.example"
        self.engine.sync_account(self.account(address))
        # The message has to land after the Inbox is selected and before the
        # wait begins, which is where a real arrival lands. Hooking `idle` puts
        # it exactly there.
        conn = self.connect(self.account(address))
        waiting = conn.idle

        def arrive_then_wait(**kwargs):
            self.servers[address].add_message("INBOX", raw("Arrived"))
            return waiting(**kwargs)

        conn.idle = arrive_then_wait
        original = self.engine._connect
        self.engine._connect = lambda account: conn
        self.addCleanup(setattr, self.engine, "_connect", original)
        lines = self.engine.watch_once(self.account(address), seconds=1)
        self.assertTrue(any("EXISTS" in line for line in lines), lines)

    def test_an_account_with_no_inbox_is_not_watched(self):
        address = "owner@manitlab.example"
        self.assertEqual(self.engine.watch_once(self.account(address)), [])


class TestTheOutboxOnTheWay(Fixture):
    """Sending happens on the same cycle as fetching, over the connection the
    engine already has. This is the wiring between two protocols, and neither
    module's own tests can see it."""

    def setUp(self):
        super().setUp()
        import fakesmtp

        from cormani.compose.draft import Draft
        from cormani.smtp import outbox
        from cormani.smtp.client import Sender
        from cormani.store import drafts, folders

        self.smtp = fakesmtp.Server(password="pw")
        self.engine = self.make_engine(
            submit=lambda host, port, credential: Sender.connect(
                host, port, credential, factory=fakesmtp.factory_for(self.smtp)))
        account_id = self.accounts["owner@manitlab.example"]
        folders.ensure_folder(self.con, account_id, "Sent",
                              display_name="Sent", role=folders.ROLE_SENT)
        self.servers["owner@manitlab.example"].add_mailbox("Sent")
        row_id, _rfc = drafts.save(self.con, Draft(
            account_id=account_id, from_address="owner@manitlab.example",
            from_name="Manish", to="lyle@covalent.example", subject="Wavelengths",
            body="1064 and 785."))
        outbox.queue(self.con, row_id)
        self.row_id = row_id

    def test_a_queued_message_goes_out_with_the_next_sync(self):
        from cormani.smtp import outbox

        results = self.engine.sync_all()
        posted = [r for r in results if r.address == "owner@manitlab.example"][0]
        self.assertTrue(posted.ok)
        self.assertEqual(posted.posted, 1)
        self.assertEqual(len(self.smtp.delivered), 1)
        self.assertEqual(outbox.waiting(self.con), 0)
        self.assertIn(b"Subject: Wavelengths", self.smtp.delivered[0][2])

    def test_the_copy_is_filed_over_the_connection_already_open(self):
        # An account whose provider does not file one — a plain IMAP server —
        # gets an APPEND rather than a second login. Its submission host comes
        # from the account row, because the provider has no default to offer.
        self.con.execute(
            "UPDATE account SET provider = 'imap', smtp_host = ? WHERE id = ?",
            ("smtp.fake.invalid", self.accounts["owner@manitlab.example"]))
        self.con.commit()
        self.engine.sync_all()
        appended = self.servers["owner@manitlab.example"].appended
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0][0], "Sent")

    def test_google_files_its_own_and_is_not_appended_to(self):
        self.engine.sync_all()
        self.assertEqual(self.servers["owner@manitlab.example"].appended, [])

    def test_an_account_with_no_submission_server_says_so(self):
        # And does not stop: mail still arrives at an account that cannot send.
        self.con.execute(
            "UPDATE account SET provider = 'imap', smtp_host = '' WHERE id = ?",
            (self.accounts["owner@manitlab.example"],))
        self.con.commit()
        results = self.engine.sync_all()
        result = [r for r in results if r.address == "owner@manitlab.example"][0]
        self.assertTrue(result.ok)
        self.assertEqual(result.posted, 0)
        self.assertIn("SMTP", " ".join(result.notes))

    def test_a_refused_submission_does_not_stop_the_fetch(self):
        # Mail still arrives when the outgoing half is broken; a client that
        # gave up on the account would hide new mail because an old message
        # could not go.
        self.smtp.password = "something else"
        self.servers["owner@manitlab.example"].add_message("INBOX", raw("Incoming"))
        results = self.engine.sync_all()
        result = [r for r in results if r.address == "owner@manitlab.example"][0]
        self.assertTrue(result.ok)
        self.assertEqual(result.new, 1)
        self.assertEqual(result.posted, 0)
        self.assertTrue(result.notes)


if __name__ == "__main__":
    unittest.main()


