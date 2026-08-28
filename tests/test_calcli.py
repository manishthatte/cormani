# SPDX-License-Identifier: GPL-3.0-or-later
#
# The calendar half of the command line.
#
# `test_cli.py` is the mail half and is at 589 of the 600 lines the packaging
# test allows, which is the reason this is a second file rather than a longer
# first one — and the split falls where the module split does.
#
# THE FIXTURE IS test_cli's, IMPORTED. It redirects all four XDG variables and
# the keyring into a temporary directory, and that is not a convenience: this
# module runs `--sync`, and a sync resolves a credential BY ADDRESS from the
# system keyring. An address that happens to be real, in a store that happens
# to be writable, is a test that goes to Gmail and fetches somebody's mail.
# Every address below is at .invalid — RFC 2606 reserves it, so nothing can
# resolve it and no keyring entry can plausibly exist for one.
#
# NOTHING HERE REACHES A NETWORK, and the mechanism is worth stating because it
# looks accidental: no credential is stored for these accounts, so both engines
# stop at `NotConfigured` before a socket is opened. A test that added one
# would be a test that dials out.
#
# © Manish Jagdish Thatte
import unittest

import support
from test_cli import Fixture

from cormani import calcli


class CalendarFixture(Fixture):
    def account(self, address="work@manitlab.invalid", provider="google"):
        from cormani.store.accounts import add_account
        con = self.store()
        return con, add_account(con, address, provider, display_name="Work")

    def calendar(self, con, account_id, remote_id="work@manitlab.invalid",
                 **kwargs):
        from cormani.store import calendars
        options = {"name": "Work", "colour": "#3377BB", "is_primary": True}
        options.update(kwargs)
        return calendars.ensure_calendar(con, account_id, remote_id, **options)


class TestCalendarsCommand(CalendarFixture):
    def test_with_no_store_it_says_so_and_fails(self):
        code, text = self.run_cli("--calendars")
        self.assertEqual(code, 1)
        self.assertIn("no store yet", text)

    def test_an_empty_store_is_not_a_failure(self):
        # Nothing is wrong with a store that has no accounts yet, and an exit
        # code that said otherwise would make a scheduled --check useless.
        self.store()
        code, text = self.run_cli("--calendars")
        self.assertEqual(code, 0)
        self.assertIn("no accounts are configured", text)

    def test_it_names_the_calendar_its_window_and_its_events(self):
        con, account_id = self.account()
        calendar_id = self.calendar(con, account_id)
        from cormani.store import calendars, events
        calendars.record_sync_state(
            con, calendar_id, sync_token="TOKEN-VALUE-abc123",
            synced_from="2026-05-31T18:30:00+00:00",
            synced_to="2027-02-28T18:30:00+00:00",
            last_synced_at="2026-08-26T04:31:12+00:00")
        events.upsert(con, calendar_id, "e1",
                      {"summary": "Lab meeting",
                       "starts_at": "2026-08-27T04:30:00+00:00",
                       "ends_at": "2026-08-27T05:30:00+00:00"})
        code, text = self.run_cli("--calendars")
        self.assertEqual(code, 0)
        self.assertIn("work@manitlab.invalid", text)
        self.assertIn("Work", text)
        self.assertIn("1 event(s)", text)
        self.assertIn("primary", text)

    def test_it_never_prints_the_sync_token_itself(self):
        # It is a bearer bookmark. CONVENTIONS.txt §7 — a secret is not printed
        # even into a terminal the user owns, because terminals get pasted.
        con, account_id = self.account()
        calendar_id = self.calendar(con, account_id)
        from cormani.store import calendars
        calendars.record_sync_state(con, calendar_id,
                                    sync_token="TOKEN-VALUE-abc123")
        _, text = self.run_cli("--calendars")
        self.assertNotIn("TOKEN-VALUE-abc123", text)
        self.assertIn("a token is held", text)

    def test_it_says_when_the_next_pass_will_not_be_incremental(self):
        # The first question OPEN ITEM 9 asks of a real run, and the store's
        # answer to it is invisible in the interface by design.
        con, account_id = self.account()
        self.calendar(con, account_id)
        _, text = self.run_cli("--calendars")
        self.assertIn("fetches the whole window", text)

    def test_the_window_is_printed_in_local_dates_not_the_stored_utc(self):
        """The class of bug is slicing the stored text instead of converting it.

        At UTC+05:30 a window anchored to the first of June is stored as 31 May,
        18:30 UTC, and `synced_from[:10]` would report a window a day short of
        the one the store keeps — for the person who is trying to find out
        whether a particular day is inside it.
        """
        from cormani.store import calendars, times
        con, account_id = self.account()
        calendar_id = self.calendar(con, account_id)
        stored = "2026-05-31T18:30:00+00:00"
        calendars.record_sync_state(con, calendar_id, synced_from=stored,
                                    synced_to="2027-02-28T18:30:00+00:00")
        _, text = self.run_cli("--calendars")
        self.assertIn(times.to_local(stored).date().isoformat(), text)

    def test_an_address_narrows_it_to_one_account(self):
        con, _ = self.account()
        from cormani.store.accounts import add_account
        add_account(con, "other@manitlab.invalid", "google")
        _, text = self.run_cli("--calendars", "work@manitlab.invalid")
        self.assertIn("work@manitlab.invalid", text)
        self.assertNotIn("other@manitlab.invalid", text)

    def test_an_address_that_is_not_configured_is_an_error(self):
        self.account()
        code, text = self.run_cli("--calendars", "nobody@manitlab.invalid")
        self.assertEqual(code, 1)
        self.assertIn("not configured", text)

    def test_a_provider_with_no_calendar_api_is_said_rather_than_shown_empty(self):
        self.account("plain@manitlab.invalid", "imap")
        _, text = self.run_cli("--calendars")
        self.assertIn("no calendar API", text)

    def test_an_account_that_has_never_synced_says_so(self):
        self.account()
        _, text = self.run_cli("--calendars")
        self.assertIn("has not synced yet", text)

    def test_a_parked_calendar_reports_why_and_until_when(self):
        con, account_id = self.account()
        calendar_id = self.calendar(con, account_id)
        from cormani.store import calendars
        calendars.record_failure(con, calendar_id, "404 Not Found",
                                 "2026-08-26T10:00:00+00:00")
        _, text = self.run_cli("--calendars")
        self.assertIn("404 Not Found", text)
        self.assertIn("parked until", text)

    def test_an_all_day_event_is_listed_on_its_own_date(self):
        """The second question OPEN ITEM 9 asks, and the one a summary cannot
        answer: an all-day event is a DATE, and an instant would put it on the
        wrong day for everybody east of Greenwich."""
        import datetime as dt

        from cormani.store import events, times
        con, account_id = self.account()
        calendar_id = self.calendar(con, account_id)
        day = times.now_local().date() + dt.timedelta(days=2)
        events.upsert(con, calendar_id, "hol", {
            "summary": "Summer bank holiday", "all_day": True,
            "starts_at": day.isoformat(),
            "ends_at": (day + dt.timedelta(days=1)).isoformat()})
        _, text = self.run_cli("--calendars")
        self.assertIn(f"{day.isoformat()}  ALL DAY", text)

    def test_the_listing_is_in_day_order_and_not_all_day_first(self):
        """`events.upcoming` sorts all-day events ahead of timed ones, which is
        right inside a day column and wrong down a fortnight — taking the first
        twelve of that order returns twelve all-day events and no appointments.
        """
        import datetime as dt

        from cormani.store import events, times
        con, account_id = self.account()
        calendar_id = self.calendar(con, account_id)
        today = times.now_local()
        soon = today.replace(hour=11, minute=0, second=0, microsecond=0)
        events.upsert(con, calendar_id, "timed", {
            "summary": "Sooner, and timed",
            "starts_at": times.to_utc_text(soon + dt.timedelta(days=1)),
            "ends_at": times.to_utc_text(soon + dt.timedelta(days=1, hours=1))})
        later = today.date() + dt.timedelta(days=5)
        events.upsert(con, calendar_id, "allday", {
            "summary": "Later, and all day", "all_day": True,
            "starts_at": later.isoformat(),
            "ends_at": (later + dt.timedelta(days=1)).isoformat()})
        _, text = self.run_cli("--calendars")
        self.assertLess(text.index("Sooner, and timed"),
                        text.index("Later, and all day"))


class TestCheckReportsCalendars(CalendarFixture):
    def test_a_store_with_no_calendars_still_reports_the_line(self):
        # Silence would be indistinguishable from a --check that predates the
        # calendar entirely.
        self.store()
        code, text = self.run_cli("--check")
        self.assertIn("calendars", text)
        self.assertIn("none has been synced yet", text)

    def test_it_counts_the_calendars_and_the_events(self):
        con, account_id = self.account()
        calendar_id = self.calendar(con, account_id)
        from cormani.store import events
        events.upsert(con, calendar_id, "e1", {
            "summary": "Lab meeting", "starts_at": "2026-08-27T04:30:00+00:00",
            "ends_at": "2026-08-27T05:30:00+00:00"})
        _, text = self.run_cli("--check")
        self.assertIn("1 across 1 account(s), 1 events held", text)

    def test_a_calendar_nothing_has_fetched_is_counted_as_such(self):
        con, account_id = self.account()
        self.calendar(con, account_id)
        _, text = self.run_cli("--check")
        self.assertIn("never fetched", text)

    def test_an_account_parked_with_no_calendar_rows_is_still_named(self):
        """The account most in need of parking is the one with no calendars to
        park: the first thing a sync does is ASK for the list, so a refusal
        arrives before any calendar row exists. Reporting only the per-calendar
        failures would hide exactly that case."""
        con, account_id = self.account()
        self.calendar(con, account_id)
        from cormani.store import accounts
        accounts.record_calendar_failure(con, account_id, "403 Forbidden",
                                         "2026-08-27T00:00:00+00:00")
        _, text = self.run_cli("--check")
        self.assertIn("403 Forbidden", text)
        self.assertIn("work@manitlab.invalid", text)

    def test_an_unanswered_invitation_is_reported(self):
        con, account_id = self.account()
        calendar_id = self.calendar(con, account_id)
        import datetime as dt

        from cormani.store import events, times
        when = times.now_local() + dt.timedelta(days=3)
        events.upsert(con, calendar_id, "inv", {
            "summary": "Someone else's meeting",
            "starts_at": times.to_utc_text(when),
            "ends_at": times.to_utc_text(when + dt.timedelta(hours=1)),
            "organiser_addr": "lyle.gordon@covalent.example",
            "organiser_name": "Lyle Gordon",
            "my_response": "needsAction"},
            attendees=[{"address": "lyle.gordon@covalent.example",
                        "is_organiser": True},
                       {"address": "work@manitlab.invalid", "is_self": True}])
        _, text = self.run_cli("--check")
        self.assertIn("invitations", text)
        self.assertIn("1 not yet answered", text)


class TestSyncCoversBothHalves(CalendarFixture):
    def test_a_bare_sync_runs_the_calendar_half_too(self):
        # F5 means everything, now. The command line said otherwise until this.
        #
        # The "calendars" heading is the seam between the two halves, which is
        # what makes it usable as the assertion: the mail half is everything
        # printed before it, and each half names the account once it starts.
        self.account()
        _, text = self.run_cli("--sync")
        self.assertIn("calendars", text)
        mail, calendar = text.split("calendars", 1)
        self.assertIn("work@manitlab.invalid", mail)
        self.assertIn("work@manitlab.invalid", calendar)

    def test_sync_mail_leaves_the_calendars_alone(self):
        self.account()
        _, text = self.run_cli("--sync", "mail")
        self.assertIn("work@manitlab.invalid", text)
        self.assertNotIn("calendars", text)

    def test_sync_calendars_leaves_the_mail_alone(self):
        self.account()
        _, text = self.run_cli("--sync", "calendars")
        self.assertTrue(text.startswith("calendars"), text[:40])
        self.assertIn("work@manitlab.invalid", text)

    def test_a_calendar_failure_alone_reaches_the_exit_code(self):
        # A scheduled run asks one question and must get one answer, so a
        # calendar that could not be read has to fail the command even when
        # every message arrived.
        self.account()
        code, text = self.run_cli("--sync", "calendars")
        self.assertEqual(code, 1)
        self.assertIn("FAILED", text)

    def test_an_account_with_no_calendar_api_is_not_a_failure(self):
        # A plain IMAP account has no calendar to sync and is behaving exactly
        # as expected; a red mark against it would be a lie.
        self.account("plain@manitlab.invalid", "imap")
        code, text = self.run_cli("--sync", "calendars")
        self.assertEqual(code, 0)
        self.assertIn("no account's provider has a calendar API", text)

    def test_an_empty_store_says_there_are_no_accounts(self):
        self.store()
        code, text = self.run_cli("--sync", "calendars")
        self.assertEqual(code, 0)
        self.assertIn("no accounts are configured", text)

    def test_a_parked_account_is_reported_as_waiting_not_as_missing(self):
        self.account()
        self.run_cli("--sync", "calendars")             # park it
        code, text = self.run_cli("--sync", "calendars")
        self.assertEqual(code, 0)
        self.assertIn("no calendar is due", text)

    def test_quiet_drops_the_commentary_and_keeps_the_summary(self):
        self.account()
        _, loud = self.run_cli("--sync", "calendars")
        from cormani.platform.paths import Paths
        from cormani.store import database
        con = database.connect(Paths().database)
        con.execute("UPDATE account SET calendar_next_at = NULL")
        con.commit()
        con.close()
        _, quiet = self.run_cli("--sync", "calendars", "--quiet")
        self.assertIn("…", loud)
        self.assertNotIn("…", quiet)
        self.assertIn("FAILED", quiet)


class TestArguments(CalendarFixture):
    def test_a_bare_calendars_flag_is_the_report_and_not_a_window(self):
        """The empty string means "every account", so a truthiness test here
        would let a bare --calendars fall through to opening a window — which
        is the failure `__main__.py`'s header is about."""
        code, text = self.run_cli("--calendars")
        self.assertEqual(code, 1)                       # no store, not a window
        self.assertIn("no store yet", text)

    def test_sync_only_accepts_the_three_halves_it_has(self):
        from cormani import __main__ as entry
        with self.assertRaises(SystemExit):
            entry.build_parser().parse_args(["--sync", "everything"])


class TestReportIsOffline(unittest.TestCase):
    def test_the_check_report_asks_nothing_of_the_network(self):
        """`--check` has to run when the network IS the problem, so the report
        may only read the store. Proved by running it over a connection with no
        credential and no host configured — anything that dialled out would
        raise rather than print."""
        import io
        from contextlib import redirect_stdout

        from cormani.store.accounts import add_account
        from cormani.store.calendars import ensure_calendar

        con = support.temp_store(self)
        account_id = add_account(con, "work@manitlab.invalid", "google")
        ensure_calendar(con, account_id, "work@manitlab.invalid", name="Work")
        out = io.StringIO()
        with redirect_stdout(out):
            calcli.report(con)
        self.assertIn("calendars", out.getvalue())


if __name__ == "__main__":
    unittest.main()
