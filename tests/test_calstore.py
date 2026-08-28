# SPDX-License-Identifier: GPL-3.0-or-later
#
# The calendar store: calendars, instances, and the two kinds of time.
#
# The test that matters most in this file is `test_all_day_is_not_an_instant`,
# and it guards a CLASS of defect rather than an instance of one. ISO-8601
# sorts lexically, so comparing a plain date against a timestamp compiles,
# runs, and is right about half the time — it goes wrong only for a user whose
# zone is not UTC, which is every user of this application. The whole four-
# bound design of `events_between` exists for it.
#
# Every test here fixes its own zone rather than reading the machine's. A suite
# that passes at UTC+05:30 and fails in Lisbon is worse than no suite: it makes
# the defect look like somebody else's environment.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import unittest

from cormani.store import calendars as calendars_repo
from cormani.store import eventedits, eventqueue
from cormani.store import events as events_repo
from cormani.store import times
from cormani.store.accounts import add_account
import support

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
LISBON = dt.timezone(dt.timedelta(hours=1))
LOS_ANGELES = dt.timezone(dt.timedelta(hours=-7))


def make_account(con, address="someone@gmail.com"):
    return add_account(con, address=address, provider="google")


def make_calendar(con, account_id, remote_id="primary", **kwargs):
    return calendars_repo.ensure_calendar(con, account_id, remote_id, **kwargs)


class TimeWindows(unittest.TestCase):
    def test_window_dates_come_from_the_local_ends(self):
        start, end = times.day_bounds(dt.date(2026, 9, 12), IST)
        from_utc, to_utc, from_date, to_date = times.window(start, end)
        # The UTC bounds name the ELEVENTH, because a local day at +05:30
        # begins at 18:30Z the day before. The dates must not.
        self.assertEqual(from_utc, "2026-09-11T18:30:00+00:00")
        self.assertEqual(to_utc, "2026-09-12T18:30:00+00:00")
        self.assertEqual((from_date, to_date), ("2026-09-12", "2026-09-13"))

    def test_month_grid_is_always_six_weeks(self):
        for month in range(1, 13):
            start, end = times.month_grid(dt.date(2026, month, 15))
            self.assertEqual((end - start).days, 42)
            self.assertEqual(start.weekday(), 0)

    def test_days_between_excludes_an_exclusive_midnight_end(self):
        start, end = times.week_bounds(dt.date(2026, 9, 12), tz=IST)
        self.assertEqual(len(times.days_between(start, end)), 7)

    def test_a_plain_date_parses_as_local_midnight(self):
        when = times.parse("2026-09-12", IST)
        self.assertEqual(when.isoformat(), "2026-09-12T00:00:00+05:30")

    def test_a_timestamp_keeps_its_own_offset(self):
        when = times.parse("2026-09-12T10:00:00+00:00", IST)
        self.assertEqual(when.utcoffset(), dt.timedelta(0))


class Calendars(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = make_account(self.con)

    def test_ensure_is_idempotent_on_the_remote_id(self):
        first = make_calendar(self.con, self.account, "primary", name="Work")
        again = make_calendar(self.con, self.account, "primary", name="Work v2")
        self.assertEqual(first, again)
        self.assertEqual(calendars_repo.get_calendar(self.con, first).name,
                         "Work v2")

    def test_everything_the_first_sight_is_told_is_kept(self):
        """The insert and the update must take the same set of fields.

        They did not: `default_reminder` was applied only when the calendar
        already existed, so a calendar learned its reminder on the SECOND sync
        and reminded nobody about anything until then. The two paths are
        listed in one place now, and this asserts the whole set rather than
        that one field.
        """
        cal = make_calendar(self.con, self.account, "primary", name="Work",
                            description="notes", colour="#111111",
                            timezone="Asia/Kolkata", is_primary=True,
                            writable=False, default_reminder=10)
        stored = calendars_repo.get_calendar(self.con, cal)
        self.assertEqual(
            (stored.name, stored.description, stored.colour, stored.timezone,
             stored.is_primary, stored.writable, stored.default_reminder),
            ("Work", "notes", "#111111", "Asia/Kolkata", True, False, 10))

    def test_the_users_colour_survives_a_sync(self):
        cal = make_calendar(self.con, self.account, "primary", colour="#111111")
        calendars_repo.set_user_colour(self.con, cal, "#268bd2")
        calendars_repo.update_calendar(self.con, cal, colour="#222222")
        stored = calendars_repo.get_calendar(self.con, cal)
        self.assertEqual(stored.colour, "#222222")
        self.assertEqual(stored.user_colour, "#268bd2")
        self.assertEqual(stored.display_colour, "#268bd2")

    def test_a_calendar_that_leaves_is_marked_not_deleted(self):
        keep = make_calendar(self.con, self.account, "primary")
        gone = make_calendar(self.con, self.account, "holidays")
        events_repo.upsert(self.con, gone, "e1",
                           {"summary": "Diwali", "starts_at": "2026-11-08",
                            "ends_at": "2026-11-09", "all_day": True})
        marked = calendars_repo.mark_absent(self.con, self.account, ["primary"])
        self.assertEqual(marked, ["holidays"])
        self.assertEqual([c.id for c in calendars_repo.list_calendars(
            self.con, self.account)], [keep])
        self.assertEqual(len(calendars_repo.list_calendars(
            self.con, self.account, include_absent=True)), 2)
        # And its events are still there, which is the point.
        self.assertEqual(events_repo.counts_by_calendar(self.con)[gone], 1)

    def test_a_calendar_that_comes_back_is_not_duplicated(self):
        first = make_calendar(self.con, self.account, "shared")
        calendars_repo.mark_absent(self.con, self.account, [])
        again = make_calendar(self.con, self.account, "shared")
        self.assertEqual(first, again)
        self.assertTrue(calendars_repo.get_calendar(self.con, again).present)

    def test_covers_answers_honestly_outside_the_window(self):
        cal = make_calendar(self.con, self.account, "primary")
        calendars_repo.record_sync_state(
            self.con, cal, synced_from="2026-09-01T00:00:00+00:00",
            synced_to="2026-10-01T00:00:00+00:00")
        stored = calendars_repo.get_calendar(self.con, cal)
        self.assertTrue(stored.covers("2026-09-10T00:00:00+00:00",
                                      "2026-09-20T00:00:00+00:00"))
        self.assertFalse(stored.covers("2026-08-10T00:00:00+00:00",
                                       "2026-09-20T00:00:00+00:00"))

    def test_primary_falls_back_to_the_first_writable(self):
        make_calendar(self.con, self.account, "holidays", writable=False)
        mine = make_calendar(self.con, self.account, "other")
        self.assertEqual(calendars_repo.primary(self.con, self.account).id, mine)


class Events(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = make_account(self.con)
        self.calendar = make_calendar(self.con, self.account, "primary")

    def add(self, remote_id, **fields):
        fields.setdefault("summary", remote_id)
        return events_repo.upsert(self.con, self.calendar, remote_id, fields,
                                  attendees=fields.pop("attendees", None))

    def test_all_day_is_not_an_instant(self):
        """Diwali is on the 8th, and it is on the 8th everywhere.

        Stored as a plain date, it must appear on the 8th and on no other day,
        in every zone. Compared as though it were midnight UTC it leaks into
        the 7th west of Greenwich and into the 9th east of it.
        """
        self.add("diwali", starts_at="2026-11-08", ends_at="2026-11-09",
                 all_day=True)
        for zone in (IST, LISBON, LOS_ANGELES, dt.timezone.utc):
            for day, expected in ((dt.date(2026, 11, 7), []),
                                  (dt.date(2026, 11, 8), ["diwali"]),
                                  (dt.date(2026, 11, 9), [])):
                found = [e.remote_id for e in events_repo.events_between(
                    self.con, *times.day_bounds(day, zone))]
                self.assertEqual(found, expected,
                                 f"{day} at {zone} gave {found}")

    def test_a_timed_event_is_an_instant_and_moves_with_the_zone(self):
        """The other half of the same rule: 20:00 UTC is the next day here."""
        self.add("call", starts_at="2026-11-08T20:00:00+00:00",
                 ends_at="2026-11-08T21:00:00+00:00")
        here = [e.remote_id for e in events_repo.events_between(
            self.con, *times.day_bounds(dt.date(2026, 11, 9), IST))]
        there = [e.remote_id for e in events_repo.events_between(
            self.con, *times.day_bounds(dt.date(2026, 11, 8), LISBON))]
        self.assertEqual(here, ["call"])
        self.assertEqual(there, ["call"])

    def test_the_window_is_half_open(self):
        self.add("ends-at-nine", starts_at="2026-09-12T07:00:00+00:00",
                 ends_at="2026-09-12T09:00:00+00:00")
        self.add("starts-at-nine", starts_at="2026-09-12T09:00:00+00:00",
                 ends_at="2026-09-12T10:00:00+00:00")
        window = (dt.datetime(2026, 9, 12, 9, 0, tzinfo=dt.timezone.utc),
                  dt.datetime(2026, 9, 12, 10, 0, tzinfo=dt.timezone.utc))
        found = [e.remote_id for e in events_repo.events_between(self.con, *window)]
        self.assertEqual(found, ["starts-at-nine"])

    def test_all_day_sorts_before_the_hours(self):
        self.add("morning", starts_at="2026-09-12T02:00:00+00:00",
                 ends_at="2026-09-12T03:00:00+00:00")
        self.add("holiday", starts_at="2026-09-12", ends_at="2026-09-13",
                 all_day=True)
        found = [e.remote_id for e in events_repo.events_between(
            self.con, *times.day_bounds(dt.date(2026, 9, 12), IST))]
        self.assertEqual(found, ["holiday", "morning"])

    def test_by_day_puts_a_one_day_all_day_event_in_one_day(self):
        self.add("holiday", starts_at="2026-09-12", ends_at="2026-09-13",
                 all_day=True)
        self.add("trip", starts_at="2026-09-09", ends_at="2026-09-12",
                 all_day=True)
        start, end = times.week_bounds(dt.date(2026, 9, 12), tz=IST)
        self.assertEqual((start.date(), end.date()),
                         (dt.date(2026, 9, 7), dt.date(2026, 9, 14)))
        found = events_repo.events_between(self.con, start, end)
        buckets = events_repo.by_day(found, start, end, tz=IST)
        named = {day: sorted(e.remote_id for e in rows)
                 for day, rows in buckets.items() if rows}
        # The trip ends on the 12th EXCLUSIVELY, which is the 11th inclusively,
        # and the holiday occupies one day rather than two.
        self.assertEqual(named, {dt.date(2026, 9, 9): ["trip"],
                                 dt.date(2026, 9, 10): ["trip"],
                                 dt.date(2026, 9, 11): ["trip"],
                                 dt.date(2026, 9, 12): ["holiday"]})

    def test_a_cancelled_instance_is_not_drawn(self):
        self.add("gone", starts_at="2026-09-12T07:00:00+00:00",
                 ends_at="2026-09-12T08:00:00+00:00", status="cancelled")
        window = times.day_bounds(dt.date(2026, 9, 12), IST)
        self.assertEqual(events_repo.events_between(self.con, *window), [])
        self.assertEqual(len(events_repo.events_between(
            self.con, *window, include_cancelled=True)), 1)

    def test_upsert_is_idempotent_and_updates_in_place(self):
        first = self.add("e1", starts_at="2026-09-12T07:00:00+00:00",
                         ends_at="2026-09-12T08:00:00+00:00")
        again = self.add("e1", summary="Renamed",
                         starts_at="2026-09-12T07:00:00+00:00",
                         ends_at="2026-09-12T08:00:00+00:00")
        self.assertEqual(first, again)
        self.assertEqual(events_repo.get_event(self.con, first).summary,
                         "Renamed")

    def test_attendees_are_replaced_wholesale(self):
        event = self.add("e1", starts_at="2026-09-12T07:00:00+00:00",
                         ends_at="2026-09-12T08:00:00+00:00",
                         attendees=[{"name": "Lyle", "address": "l@x.com"},
                                    {"address": "f@y.com"}])
        self.assertEqual(len(events_repo.get_event(self.con, event).attendees), 2)
        events_repo.set_attendees(self.con, event,
                                  [{"address": "l@x.com",
                                    "response": "accepted"}])
        guests = events_repo.get_event(self.con, event).attendees
        self.assertEqual([(g.address, g.response) for g in guests],
                         [("l@x.com", "accepted")])

    def test_by_ical_uid_finds_every_account_holding_it(self):
        other = make_account(self.con, "other@gmail.com")
        second = make_calendar(self.con, other, "primary")
        self.add("e1", ical_uid="uid-42", starts_at="2026-09-12T07:00:00+00:00",
                 ends_at="2026-09-12T08:00:00+00:00")
        events_repo.upsert(self.con, second, "e9",
                           {"ical_uid": "uid-42",
                            "starts_at": "2026-09-12T07:00:00+00:00",
                            "ends_at": "2026-09-12T08:00:00+00:00"})
        self.assertEqual(len(events_repo.by_ical_uid(self.con, "uid-42")), 2)
        self.assertEqual(len(events_repo.by_ical_uid(self.con, "uid-42",
                                                     account_id=other)), 1)

    def test_prune_removes_only_what_the_window_should_have_held(self):
        self.add("inside", starts_at="2026-09-12T07:00:00+00:00",
                 ends_at="2026-09-12T08:00:00+00:00")
        self.add("outside", starts_at="2026-10-12T07:00:00+00:00",
                 ends_at="2026-10-12T08:00:00+00:00")
        removed = events_repo.prune_window(
            self.con, self.calendar, "2026-09-01T00:00:00+00:00",
            "2026-10-01T00:00:00+00:00", keep=[])
        self.assertEqual(removed, 1)
        self.assertIsNotNone(events_repo.by_remote(self.con, self.calendar,
                                                   "outside"))

    def test_prune_never_removes_an_event_made_here(self):
        """A local event is not in the provider's answer BY DEFINITION."""
        eventedits.create_event(
            self.con, self.calendar, summary="Mine",
            starts_at="2026-09-12T07:00:00+00:00",
            ends_at="2026-09-12T08:00:00+00:00")
        removed = events_repo.prune_window(
            self.con, self.calendar, "2026-09-01T00:00:00+00:00",
            "2026-10-01T00:00:00+00:00", keep=[])
        self.assertEqual(removed, 0)


class Editing(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = make_account(self.con)
        self.calendar = make_calendar(self.con, self.account, "primary")

    def make(self, **kwargs):
        kwargs.setdefault("summary", "Reading group")
        kwargs.setdefault("starts_at", "2026-09-12T07:00:00+00:00")
        kwargs.setdefault("ends_at", "2026-09-12T08:00:00+00:00")
        return eventedits.create_event(self.con, self.calendar, **kwargs)

    def ops(self):
        return eventqueue.pending_for(self.con, self.account)

    def test_a_new_event_is_visible_at_once_and_queued(self):
        event_id = self.make()
        event = events_repo.get_event(self.con, event_id)
        self.assertTrue(event.is_local)
        self.assertEqual(event.pending, "create")
        self.assertEqual([op.kind for op in self.ops()], ["create"])
        self.assertEqual(self.ops()[0].payload["summary"], "Reading group")

    def test_the_creator_is_an_attendee_and_is_marked(self):
        event = events_repo.get_event(self.con, self.make(
            attendees=[{"address": "lyle@covalent.example"}]))
        self.assertEqual([(g.address, g.is_organiser, g.is_self)
                          for g in event.attendees],
                         [("someone@gmail.com", True, True),
                          ("lyle@covalent.example", False, False)])
        self.assertFalse(event.is_invitation)

    def test_an_edit_before_the_sync_folds_into_the_create(self):
        event_id = self.make()
        eventedits.update_event(self.con, event_id, summary="Reading group II",
                                location="The usual")
        ops = self.ops()
        self.assertEqual([op.kind for op in ops], ["create"])
        self.assertEqual(ops[0].payload["summary"], "Reading group II")
        self.assertEqual(ops[0].payload["location"], "The usual")

    def test_an_edit_after_the_sync_is_a_patch_carrying_the_etag(self):
        event_id = self.make()
        eventqueue.complete(self.con, [op.id for op in self.ops()])
        events_repo.upsert(self.con, self.calendar,
                           events_repo.get_event(self.con, event_id).remote_id,
                           {"etag": "W/\"7\""})
        eventedits.update_event(self.con, event_id, location="Room 2")
        ops = self.ops()
        self.assertEqual([op.kind for op in ops], ["update"])
        self.assertEqual(ops[0].etag, "W/\"7\"")
        # A patch: the fields the user changed, and nothing else.
        self.assertEqual(set(ops[0].payload), {"location"})

    def test_two_edits_after_the_sync_are_one_write(self):
        event_id = self.make()
        eventqueue.complete(self.con, [op.id for op in self.ops()])
        eventedits.update_event(self.con, event_id, location="Room 2")
        eventedits.update_event(self.con, event_id, summary="Moved")
        ops = self.ops()
        self.assertEqual([op.kind for op in ops], ["update"])
        self.assertEqual(set(ops[0].payload), {"location", "summary"})

    def test_an_edit_does_not_overtake_an_attempted_op(self):
        event_id = self.make()
        eventqueue.record_failure(self.con, self.ops()[0].id, "no network")
        eventedits.update_event(self.con, event_id, location="Room 2")
        self.assertEqual([op.kind for op in self.ops()], ["create", "update"])

    def test_deleting_an_unsent_event_tells_the_provider_nothing(self):
        event_id = self.make()
        removed = eventedits.delete_event(self.con, event_id)
        self.assertEqual(removed.summary, "Reading group")
        self.assertEqual(self.ops(), [])
        self.assertIsNone(events_repo.get_event(self.con, event_id))

    def test_deleting_a_synced_event_queues_a_delete(self):
        event_id = self.make()
        eventqueue.complete(self.con, [op.id for op in self.ops()])
        eventedits.delete_event(self.con, event_id)
        ops = self.ops()
        self.assertEqual([op.kind for op in ops], ["delete"])
        self.assertTrue(ops[0].remote_id)
        self.assertIsNone(ops[0].event_id)      # the row is gone; the op is not

    def test_responding_is_allowed_on_a_read_only_calendar(self):
        shared = make_calendar(self.con, self.account, "shared", writable=False)
        event_id = events_repo.upsert(
            self.con, shared, "e1",
            {"summary": "Their meeting", "organiser_addr": "them@x.com",
             "starts_at": "2026-09-12T07:00:00+00:00",
             "ends_at": "2026-09-12T08:00:00+00:00"},
            attendees=[{"address": "them@x.com", "is_organiser": True},
                       {"address": "someone@gmail.com", "is_self": True}])
        self.assertTrue(events_repo.get_event(self.con, event_id).needs_reply)
        eventedits.set_response(self.con, event_id, "accepted")
        event = events_repo.get_event(self.con, event_id)
        self.assertEqual(event.my_response, "accepted")
        self.assertFalse(event.needs_reply)
        self.assertEqual([(g.address, g.response) for g in event.attendees],
                         [("them@x.com", "needsAction"),
                          ("someone@gmail.com", "accepted")])
        self.assertEqual([op.kind for op in self.ops()], ["respond"])

    def test_editing_a_read_only_calendar_is_refused_before_anything_is_typed(self):
        shared = make_calendar(self.con, self.account, "shared", writable=False)
        with self.assertRaises(eventedits.NotWritable):
            eventedits.create_event(self.con, shared, summary="No",
                                    starts_at="2026-09-12T07:00:00+00:00",
                                    ends_at="2026-09-12T08:00:00+00:00")

    def test_a_stuck_op_leaves_the_queue_but_stays_in_the_table(self):
        self.make()
        op = self.ops()[0]
        eventqueue.give_up(self.con, op.id, "the provider refused")
        self.assertEqual(self.ops(), [])
        stuck = eventqueue.pending_for(self.con, self.account, include_stuck=True)
        self.assertEqual([(o.kind, o.stuck) for o in stuck], [("create", True)])
        self.assertEqual(eventqueue.counts(self.con)[self.account]["stuck"], 1)

    def test_moving_between_calendars_says_it_is_not_offered(self):
        with self.assertRaises(eventedits.NotSupported):
            eventedits.move_to_calendar(self.con, self.make(), self.calendar)


if __name__ == "__main__":
    unittest.main()
