# SPDX-License-Identifier: GPL-3.0-or-later
#
# The calendar sync, the calendar queue, and the engine over both.
#
# Every test runs the REAL client against the servers in tests/fakecal.py, so
# what is exercised is the request corMani would send and the answer a provider
# would give. Nothing here is mocked; the only double is the transport.
#
# The three that matter most:
#
#   * `test_the_second_pass_is_incremental` — because the whole window design
#     exists to make it so, and because a client that silently did a full pass
#     every time would look identical from the outside until the day somebody
#     measured the requests.
#   * `test_a_month_boundary_is_the_only_thing_that_widens_the_window` — the
#     quantised window, which is what keeps the bookmark valid.
#   * `test_a_create_is_adopted_rather_than_duplicated` — the one place two
#     rows for one event could appear.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import unittest

from cormani.calendar import errors
from cormani.calendar import queue as calqueue
from cormani.calendar import sync as calsync
from cormani.calendar.engine import Engine, Options
from cormani.calendar.google import GoogleCalendar
from cormani.calendar.graph import GraphCalendar
from cormani.calendar.http import Http
from cormani.store import calendars as calendars_repo
from cormani.store import eventedits, eventqueue
from cormani.store import events as events_repo
from cormani.store.accounts import (add_account,
                                     calendar_state as account_state,
                                     record_calendar_success as
                                     clear_calendar_backoff)
from tests.calwire import fail
from tests.fakecal import TOKEN, FakeGoogle, FakeGraph
import support

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 12, 15, 0, tzinfo=IST)


class Base(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, address="someone@gmail.com",
                                   provider="google")
        self.server = FakeGoogle()
        self.server.add_calendar("primary", summary="Manish", primary=True)
        self.transport = self.server.transport()
        self.client = GoogleCalendar(Http(TOKEN, opener=self.transport),
                                     address="someone@gmail.com")
        self.window = calsync.window_for(NOW)

    def calendars(self):
        calsync.sync_calendar_list(self.con, self.client, self.account)
        return calendars_repo.list_calendars(self.con, self.account)

    def primary(self):
        return calendars_repo.by_remote(self.con, self.account, "primary")

    def event(self, event_id="e1", **kwargs):
        kwargs.setdefault("summary", event_id)
        kwargs.setdefault("start", "2026-09-12T09:00:00+00:00")
        kwargs.setdefault("end", "2026-09-12T10:00:00+00:00")
        return self.server.add_event("primary", event_id, **kwargs)

    def run_sync(self, calendar=None):
        return calsync.sync_calendar(self.con, self.client,
                                     calendar or self.primary(),
                                     window=self.window)


class Syncing(Base):
    def test_the_calendar_list_carries_access_and_absence(self):
        self.server.add_calendar("holidays", summary="Holidays", access="reader")
        found = self.calendars()
        self.assertEqual([(c.remote_id, c.writable) for c in found],
                         [("primary", True), ("holidays", False)])
        # And one that stops being listed is hidden rather than deleted.
        self.server.calendars.pop()
        _, gone = calsync.sync_calendar_list(self.con, self.client, self.account)
        self.assertEqual(gone, ["holidays"])
        self.assertEqual(len(calendars_repo.list_calendars(self.con,
                                                           self.account)), 1)

    def test_a_first_pass_is_full_and_records_the_window(self):
        self.calendars()
        self.event("e1", summary="Call")
        report = self.run_sync()
        self.assertTrue(report.full)
        self.assertEqual(report.changed, 1)
        calendar = self.primary()
        self.assertEqual((calendar.synced_from, calendar.synced_to), self.window)
        self.assertTrue(calendar.sync_token)
        self.assertTrue(calendar.covers(*self.window))

    def test_the_second_pass_is_incremental(self):
        self.calendars()
        self.event("e1", summary="Call")
        self.event("e2", summary="Other",
                   start="2026-09-13T09:00:00+00:00",
                   end="2026-09-13T10:00:00+00:00")
        self.run_sync()
        self.server.touch("primary", "e2", summary="Renamed")
        report = self.run_sync()
        self.assertFalse(report.full)
        self.assertEqual(report.changed, 1)
        self.assertIn("syncToken", self.transport.last().query)
        self.assertEqual(
            events_repo.by_remote(self.con, self.primary().id, "e2").summary,
            "Renamed")

    def test_an_incremental_deletion_removes_the_row(self):
        self.calendars()
        self.event("e1")
        self.run_sync()
        self.server.cancel_event("primary", "e1")
        report = self.run_sync()
        self.assertEqual(report.removed, 1)
        self.assertIsNone(events_repo.by_remote(self.con, self.primary().id, "e1"))

    def test_a_full_pass_learns_about_a_deletion_by_subtraction(self):
        self.calendars()
        self.event("e1")
        self.run_sync()
        # Wipe the bookmark, as a provider does after some weeks of silence.
        calendars_repo.record_sync_state(self.con, self.primary().id,
                                         sync_token="")
        self.server.events["primary"].clear()
        report = self.run_sync()
        self.assertTrue(report.full)
        self.assertEqual(report.removed, 1)

    def test_an_expired_bookmark_is_handled_and_not_reported(self):
        self.calendars()
        self.event("e1")
        self.run_sync()
        self.server.tokens.clear()          # every token invalidated at once
        report = self.run_sync()
        self.assertTrue(report.full)
        self.assertEqual(report.error, "")
        self.assertTrue(any("expired" in note for note in report.notes))
        self.assertTrue(self.primary().sync_token)

    def test_a_month_boundary_is_the_only_thing_that_widens_the_window(self):
        """A window that slid with the clock would throw the bookmark away."""
        same_month = [calsync.window_for(NOW.replace(day=day))
                      for day in (1, 12, 30)]
        self.assertEqual(len(set(same_month)), 1)
        next_month = calsync.window_for(NOW.replace(month=10, day=1))
        self.assertNotEqual(next_month, same_month[0])

        self.calendars()
        self.event("e1")
        self.run_sync()
        report = calsync.sync_calendar(self.con, self.client, self.primary(),
                                       window=next_month)
        self.assertTrue(report.full)
        self.assertEqual(self.primary().synced_from, next_month[0])

    def test_a_range_outside_the_window_does_not_claim_to_be_watched(self):
        self.calendars()
        self.run_sync()
        self.server.add_event("primary", "old", summary="Long ago",
                              start="2019-03-04T09:00:00+00:00",
                              end="2019-03-04T10:00:00+00:00")
        calsync.fetch_range(self.con, self.client, self.primary(),
                            "2019-01-01T00:00:00+00:00",
                            "2019-04-01T00:00:00+00:00")
        calendar = self.primary()
        self.assertIsNotNone(events_repo.by_remote(self.con, calendar.id, "old"))
        self.assertEqual((calendar.synced_from, calendar.synced_to), self.window)
        self.assertFalse(calendar.covers("2019-01-01T00:00:00+00:00",
                                         "2019-04-01T00:00:00+00:00"))

    def test_an_all_day_event_survives_the_round_trip(self):
        self.calendars()
        self.event("d1", summary="Diwali", start="2026-11-08", end="2026-11-09",
                   all_day=True)
        self.run_sync()
        stored = events_repo.by_remote(self.con, self.primary().id, "d1")
        self.assertTrue(stored.all_day)
        found = events_repo.events_between(
            self.con, dt.datetime(2026, 11, 8, tzinfo=IST),
            dt.datetime(2026, 11, 9, tzinfo=IST))
        self.assertEqual([e.remote_id for e in found], ["d1"])


class Draining(Base):
    def setUp(self):
        super().setUp()
        self.calendars()
        self.calendar = self.primary()

    def drain(self):
        return calqueue.drain(self.con, self.client, self.account)

    def make(self, **kwargs):
        kwargs.setdefault("summary", "Reading group")
        kwargs.setdefault("starts_at", "2026-09-12T09:00:00+00:00")
        kwargs.setdefault("ends_at", "2026-09-12T10:00:00+00:00")
        return eventedits.create_event(self.con, self.calendar.id, **kwargs)

    def test_a_create_is_adopted_rather_than_duplicated(self):
        event_id = self.make()
        report = self.drain()
        self.assertEqual((report.sent, report.failed), (1, 0))
        event = events_repo.get_event(self.con, event_id)
        self.assertFalse(event.is_local)
        self.assertEqual(event.pending, "")
        self.assertEqual(len(self.server.events["primary"]), 1)
        # And the sync that follows must not make a second row for it.
        self.run_sync(self.calendar)
        self.assertEqual(events_repo.counts_by_calendar(self.con)
                         [self.calendar.id], 1)

    def test_an_edit_queued_behind_a_create_is_re_addressed(self):
        event_id = self.make()
        eventqueue.record_failure(self.con,
                                  eventqueue.for_event(self.con, event_id)[0].id,
                                  "no network")
        eventedits.update_event(self.con, event_id, location="Room 2")
        self.assertEqual([op.kind for op in
                          eventqueue.pending_for(self.con, self.account)],
                         ["create", "update"])
        report = self.drain()
        self.assertEqual(report.sent, 2)
        remote_id = events_repo.get_event(self.con, event_id).remote_id
        self.assertEqual(self.server.events["primary"][remote_id]["location"],
                         "Room 2")

    def test_a_time_change_carries_the_kind_of_time_it_is(self):
        """A patch that moved a meeting without saying `all_day` could turn it
        into a day off: the two are different keys, not a flag."""
        event_id = self.make()
        self.drain()
        eventedits.update_event(self.con, event_id,
                                starts_at="2026-09-12T11:00:00+00:00",
                                ends_at="2026-09-12T12:00:00+00:00")
        self.drain()
        sent = self.transport.last().body
        self.assertEqual(sent["start"], {"dateTime": "2026-09-12T11:00:00+00:00",
                                         "timeZone": "UTC"})
        self.assertNotIn("date", sent["start"])

    def test_a_conflict_is_reported_and_the_change_is_not_forced(self):
        event_id = self.make()
        self.drain()
        remote_id = events_repo.get_event(self.con, event_id).remote_id
        self.run_sync(self.calendar)                   # so the row has an etag
        eventedits.update_event(self.con, event_id, summary="Mine")
        self.server.touch("primary", remote_id, summary="Theirs")
        report = self.drain()
        self.assertEqual((report.sent, report.conflicts), (0, 1))
        self.assertTrue(any("changed elsewhere" in e for e in report.errors))
        self.assertEqual(self.server.events["primary"][remote_id]["summary"],
                         "Theirs")
        # The op is kept and reported rather than deleted.
        self.assertEqual([op.kind for op in eventqueue.pending_for(
            self.con, self.account, include_stuck=True)], ["update"])

    def test_an_event_already_gone_is_dropped_rather_than_failed(self):
        event_id = self.make()
        self.drain()
        remote_id = events_repo.get_event(self.con, event_id).remote_id
        eventedits.delete_event(self.con, event_id)
        del self.server.events["primary"][remote_id]
        report = self.drain()
        self.assertEqual((report.dropped, report.failed), (1, 0))
        self.assertEqual(eventqueue.pending_for(self.con, self.account), [])

    def test_a_transient_failure_stops_the_run_and_keeps_the_order(self):
        first = self.make(summary="One")
        second = self.make(summary="Two")
        calls = {"n": 0}

        def flaky(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise fail(503, {"error": {"message": "service unavailable"}})
            return self.transport(request, timeout)

        client = GoogleCalendar(Http(TOKEN, opener=flaky))
        report = calqueue.drain(self.con, client, self.account)
        self.assertEqual((report.sent, report.failed), (0, 1))
        ops = eventqueue.pending_for(self.con, self.account)
        self.assertEqual([op.event_id for op in ops], [first, second])
        self.assertEqual(ops[0].attempts, 1)

    def test_answering_an_invitation_reaches_the_provider(self):
        self.server.add_event(
            "primary", "inv1", summary="Their meeting",
            organiser="them@example.com",
            start="2026-09-12T09:00:00+00:00", end="2026-09-12T10:00:00+00:00",
            attendees=["them@example.com", "someone@gmail.com"])
        self.run_sync(self.calendar)
        event = events_repo.by_remote(self.con, self.calendar.id, "inv1")
        self.assertTrue(events_repo.get_event(self.con, event.id).needs_reply)
        eventedits.set_response(self.con, event.id, "accepted")
        report = self.drain()
        self.assertEqual(report.sent, 1)
        guests = self.server.events["primary"]["inv1"]["attendees"]
        self.assertEqual([g["responseStatus"] for g in guests
                          if g["email"] == "someone@gmail.com"], ["accepted"])


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, address="someone@gmail.com",
                                   provider="google")
        self.server = FakeGoogle()
        self.server.add_calendar("primary", primary=True)
        self.server.add_calendar("holidays", access="reader")
        self.transport = self.server.transport()

    def engine(self, client=None, clock=None):
        made = client or (lambda account: GoogleCalendar(
            Http(TOKEN, opener=self.transport), address=account.address))
        return Engine(self.con, client=made, options=Options(),
                      clock=clock or (lambda: NOW))

    def test_an_account_whose_provider_has_no_calendar_says_so_once(self):
        plain = add_account(self.con, address="me@example.org", provider="imap")
        engine = self.engine()
        result = engine.sync_account(
            [a for a in engine.due(now=NOW) if a.id == plain] and None or
            _account(self.con, plain))
        self.assertTrue(result.ok)
        self.assertTrue(result.unsupported)
        self.assertFalse(result.error)
        self.assertNotIn(plain, [a.id for a in engine.due(now=NOW)])

    def test_a_whole_account_syncs_and_reports(self):
        self.server.add_event("primary", "e1", summary="Call",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        result = self.engine().sync_account(_account(self.con, self.account))
        self.assertTrue(result.ok)
        self.assertEqual(result.calendars, 2)
        self.assertEqual(result.changed, 1)

    def test_one_bad_calendar_does_not_stop_the_others(self):
        self.server.add_event("primary", "e1",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        del self.server.events["holidays"]        # a 404 for that one alone
        result = self.engine().sync_account(_account(self.con, self.account))
        self.assertTrue(result.ok)
        self.assertEqual(result.changed, 1)
        self.assertTrue(any("holidays" in note for note in result.notes))
        parked = calendars_repo.failures(self.con)
        self.assertEqual(len(parked), 1)

    def test_a_refused_credential_parks_the_calendars_and_not_the_account(self):
        def refuse(account):
            return GoogleCalendar(Http("wrong", opener=self.transport))

        result = self.engine(client=refuse).sync_account(
            _account(self.con, self.account))
        self.assertFalse(result.ok)
        self.assertTrue(result.retry_at)
        row = self.con.execute("SELECT next_attempt_at, sync_failures "
                               "FROM account WHERE id = ?",
                               (self.account,)).fetchone()
        # The mail engine's own back-off is untouched: this account's mail
        # works even when its calendar cannot be read at all.
        self.assertIsNone(row["next_attempt_at"])
        self.assertEqual(row["sync_failures"], 0)
        state = account_state(self.con)[self.account]
        self.assertTrue(state["error"])
        self.assertEqual(state["failures"], 1)
        self.assertEqual(self.engine().due(now=NOW), [])

    def test_clearing_the_back_off_makes_an_account_due_again(self):
        def refuse(account):
            return GoogleCalendar(Http("wrong", opener=self.transport))

        self.engine(client=refuse).sync_account(_account(self.con, self.account))
        self.assertEqual(self.engine().due(now=NOW), [])
        clear_calendar_backoff(self.con, self.account)
        self.assertEqual([a.id for a in self.engine().due(now=NOW)],
                         [self.account])


class GraphEndToEnd(unittest.TestCase):
    """The other provider, through the same sync. Different words, same shape."""

    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, address="someone@outlook.com",
                                   provider="microsoft")
        self.server = FakeGraph()
        self.server.add_calendar("cal-1", name="Calendar", default=True)
        self.transport = self.server.transport()
        self.client = GraphCalendar(Http(TOKEN, opener=self.transport),
                                    address="someone@outlook.com")

    def test_a_full_pass_then_an_incremental_one(self):
        self.server.add_event("cal-1", "e1", subject="Call",
                              start="2026-09-12T09:00:00+00:00",
                              end="2026-09-12T10:00:00+00:00")
        calsync.sync_calendar_list(self.con, self.client, self.account)
        calendar = calendars_repo.by_remote(self.con, self.account, "cal-1")
        window = calsync.window_for(NOW)
        first = calsync.sync_calendar(self.con, self.client, calendar,
                                      window=window)
        self.assertTrue(first.full)
        self.assertEqual(first.changed, 1)

        calendar = calendars_repo.by_remote(self.con, self.account, "cal-1")
        self.assertTrue(calendar.sync_token.startswith("https://"))
        self.server.touch("cal-1", "e1", subject="Renamed")
        second = calsync.sync_calendar(self.con, self.client, calendar,
                                       window=window)
        self.assertFalse(second.full)
        self.assertEqual(second.changed, 1)
        self.assertEqual(
            events_repo.by_remote(self.con, calendar.id, "e1").summary,
            "Renamed")

    def test_the_engine_drives_graph_as_well(self):
        engine = Engine(self.con,
                        client=lambda a: self.client, clock=lambda: NOW)
        result = engine.sync_account(_account(self.con, self.account))
        self.assertTrue(result.ok)
        self.assertEqual(result.calendars, 1)


def _account(con, account_id):
    from cormani.store.accounts import list_accounts

    return [a for a in list_accounts(con) if a.id == account_id][0]


if __name__ == "__main__":
    unittest.main()
