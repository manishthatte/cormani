# SPDX-License-Identifier: GPL-3.0-or-later
#
# The three calendar views, and the dialog that writes an event.
#
# Debian packages no QTest, so nothing here synthesises a click. What is
# asserted is what the widgets COMPUTE — the six-week grid, the columns two
# overlapping meetings are given, the rows an agenda decides to draw, and the
# values the dialog produces — because those are the parts that can be wrong
# without anybody noticing on screen.
#
# `test_the_inclusive_end_survives_a_round_trip` is the one that matters most:
# an all-day end is exclusive in the store and inclusive in the dialog, and
# every calendar that has ever shipped an off-by-one has shipped it there.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import unittest

from cormani.store import calendars as calendars_repo
from cormani.store import events as events_repo
from cormani.store.accounts import add_account
import support

support.qt_app() if support.HAVE_QT else None

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class Fake:
    """The parts of an Event that laying out a week needs."""

    def __init__(self, event_id, start, end):
        self.id = event_id
        self.starts_at, self.ends_at = start, end

    def start(self, tz=None):
        return dt.datetime.fromisoformat(self.starts_at)

    def end(self, tz=None):
        return dt.datetime.fromisoformat(self.ends_at)


@support.requires_qt
class MonthGrid(unittest.TestCase):
    def view(self, day=dt.date(2026, 9, 12)):
        from cormani.ui.monthview import MonthView

        view = support.own(self, MonthView())
        view.set_anchor(day)
        view.resize(700, 500)
        return view

    def test_a_month_is_always_six_weeks_from_a_monday(self):
        for month in range(1, 13):
            view = self.view(dt.date(2026, month, 15))
            days = view.days()
            self.assertEqual(len(days), 42)
            self.assertEqual(days[0].weekday(), 0)
            self.assertLessEqual(days[0], dt.date(2026, month, 1))

    def test_the_grid_starts_before_the_month_and_ends_after_it(self):
        days = self.view().days()
        self.assertEqual(days[0], dt.date(2026, 8, 31))
        self.assertEqual(days[-1], dt.date(2026, 10, 11))

    def test_the_range_is_the_grid_and_not_the_month(self):
        start, end = self.view().range()
        self.assertEqual(start.date(), dt.date(2026, 8, 31))
        self.assertEqual((end - start).days, 42)

    def test_the_weekday_names_start_where_the_week_does(self):
        from cormani.ui.monthview import weekday_names

        self.assertEqual(len(weekday_names()), 7)
        self.assertNotEqual(weekday_names(0), weekday_names(6))


@support.requires_qt
class WeekGrid(unittest.TestCase):
    def test_two_overlapping_meetings_share_the_width(self):
        from cormani.ui.weekview import lay_out

        rows = lay_out([Fake(1, "2026-09-12T09:00:00+00:00",
                             "2026-09-12T10:00:00+00:00"),
                        Fake(2, "2026-09-12T09:30:00+00:00",
                             "2026-09-12T10:30:00+00:00")])
        self.assertEqual([(e.id, c, t) for e, c, t in rows],
                         [(1, 0, 2), (2, 1, 2)])

    def test_a_meeting_that_overlaps_nobody_keeps_the_whole_width(self):
        from cormani.ui.weekview import lay_out

        rows = lay_out([Fake(1, "2026-09-12T09:00:00+00:00",
                             "2026-09-12T10:00:00+00:00"),
                        Fake(2, "2026-09-12T11:00:00+00:00",
                             "2026-09-12T12:00:00+00:00")])
        self.assertEqual([(e.id, t) for e, _c, t in rows], [(1, 1), (2, 1)])

    def test_a_cluster_reuses_a_column_that_has_ended(self):
        """Three meetings, but never three at once: two columns, not three."""
        from cormani.ui.weekview import lay_out

        rows = lay_out([Fake(1, "2026-09-12T09:00:00+00:00",
                             "2026-09-12T10:00:00+00:00"),
                        Fake(2, "2026-09-12T09:30:00+00:00",
                             "2026-09-12T11:00:00+00:00"),
                        Fake(3, "2026-09-12T10:00:00+00:00",
                             "2026-09-12T10:45:00+00:00")])
        self.assertEqual([(e.id, c, t) for e, c, t in rows],
                         [(1, 0, 2), (2, 1, 2), (3, 0, 2)])

    def test_a_week_runs_monday_to_monday_and_a_day_view_is_one_day(self):
        from cormani.ui.weekview import WeekView

        week = support.own(self, WeekView(days=7))
        week.set_anchor(dt.date(2026, 9, 12))
        start, end = week.range()
        self.assertEqual(start.date(), dt.date(2026, 9, 7))
        self.assertEqual((end - start).days, 7)

        day = support.own(self, WeekView(days=1))
        day.set_anchor(dt.date(2026, 9, 12))
        start, end = day.range()
        self.assertEqual(start.date(), dt.date(2026, 9, 12))
        self.assertEqual((end - start).days, 1)

    def test_the_selection_is_one_across_the_header_and_the_grid(self):
        from cormani.ui.weekview import WeekView

        week = support.own(self, WeekView(days=7))
        seen = []
        week.event_selected.connect(seen.append)
        week.header.select_event(7)
        self.assertEqual(week.grid.selected_id(), 7)
        self.assertEqual(seen, [7])          # emitted once, not once per widget


@support.requires_qt
class Agenda(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, address="a@b.c", provider="google")
        self.calendar = calendars_repo.ensure_calendar(self.con, self.account,
                                                       "primary", name="Mine")

    def add(self, remote_id, start, end, **fields):
        fields.update(starts_at=start, ends_at=end)
        fields.setdefault("summary", remote_id)
        return events_repo.upsert(self.con, self.calendar, remote_id, fields)

    def view(self):
        from cormani.ui.agendaview import AgendaView

        view = support.own(self, AgendaView())
        view.set_anchor(dt.date(2026, 9, 12))
        view.set_timezone(IST)
        start, end = view.range()
        known = {c.id: c for c in calendars_repo.list_calendars(self.con)}
        view.set_events(events_repo.events_between(self.con, start, end), known)
        return view

    def test_only_days_with_something_in_them_are_listed(self):
        self.add("e1", "2026-09-12T09:00:00+00:00", "2026-09-12T10:00:00+00:00")
        self.add("e2", "2026-09-12T11:00:00+00:00", "2026-09-12T12:00:00+00:00")
        self.add("e3", "2026-09-20T09:00:00+00:00", "2026-09-20T10:00:00+00:00")
        rows = self.view().list.rows()
        self.assertEqual([kind for kind, _ in rows],
                         ["day", "event", "event", "day", "event"])
        self.assertEqual([item for kind, item in rows if kind == "day"],
                         [dt.date(2026, 9, 12), dt.date(2026, 9, 20)])

    def test_an_empty_month_draws_nothing_rather_than_thirty_empty_days(self):
        self.assertEqual(self.view().list.rows(), [])

    def test_the_range_is_the_month_so_switching_view_does_not_move_the_user(self):
        from cormani.ui.monthview import MonthView

        agenda = self.view()
        month = support.own(self, MonthView())
        month.set_anchor(dt.date(2026, 9, 12))
        self.assertEqual(agenda.anchor(), month.anchor())
        self.assertEqual(agenda.range()[0].date(), dt.date(2026, 9, 1))


@support.requires_qt
class Dialog(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, address="a@b.c", provider="google")
        self.calendar = calendars_repo.ensure_calendar(
            self.con, self.account, "primary", name="Mine", is_primary=True)
        calendars_repo.ensure_calendar(self.con, self.account, "holidays",
                                       name="Holidays", writable=False)

    def dialog(self, **kwargs):
        from cormani.ui.eventdialog import EventDialog

        kwargs.setdefault("account_id", self.account)
        kwargs.setdefault("tz", IST)
        return support.own(self, EventDialog(self.con, **kwargs))

    def test_only_writable_calendars_are_offered(self):
        dialog = self.dialog()
        labels = [dialog.calendar.itemText(n)
                  for n in range(dialog.calendar.count())]
        self.assertEqual(len(labels), 1)
        self.assertIn("Mine", labels[0])
        self.assertIn("a@b.c", labels[0])

    def test_a_new_event_is_proposed_at_the_next_half_hour(self):
        dialog = self.dialog(when=dt.datetime(2026, 9, 12, 14, 23, tzinfo=IST))
        values = dialog.values()
        self.assertEqual(values["starts_at"], "2026-09-12T09:00:00+00:00")
        self.assertEqual(values["ends_at"], "2026-09-12T10:00:00+00:00")
        self.assertFalse(values["all_day"])

    def test_the_inclusive_end_survives_a_round_trip(self):
        """A one-day event is entered as one day and stored as two dates."""
        dialog = self.dialog(when=dt.datetime(2026, 11, 8, 9, 0, tzinfo=IST))
        dialog.all_day.setChecked(True)
        dialog.summary.setText("Diwali")
        values = dialog.values()
        self.assertEqual((values["starts_at"], values["ends_at"]),
                         ("2026-11-08", "2026-11-09"))

        event_id = events_repo.upsert(self.con, self.calendar, "d1", values)
        again = self.dialog(event=events_repo.get_event(self.con, event_id))
        self.assertTrue(again.all_day.isChecked())
        # And the date the user is shown is the day it is ON, not the day after.
        self.assertEqual(again.end_date.date().toPython(), dt.date(2026, 11, 8))
        self.assertEqual(again.values()["ends_at"], "2026-11-09")

    def test_a_local_time_is_stored_as_utc(self):
        dialog = self.dialog(when=dt.datetime(2026, 9, 12, 15, 0, tzinfo=IST))
        self.assertEqual(dialog.values()["starts_at"],
                         "2026-09-12T09:30:00+00:00")

    def test_the_calendar_cannot_be_changed_on_an_existing_event(self):
        event_id = events_repo.upsert(
            self.con, self.calendar, "e1",
            {"summary": "Call", "starts_at": "2026-09-12T09:00:00+00:00",
             "ends_at": "2026-09-12T10:00:00+00:00"})
        dialog = self.dialog(event=events_repo.get_event(self.con, event_id))
        self.assertFalse(dialog.calendar.isEnabled())

    def test_an_event_with_no_title_is_refused_and_says_so(self):
        dialog = self.dialog()
        dialog.accept()
        self.assertIn("title", dialog.warning.text())
        self.assertEqual(dialog.result(), 0)

    def test_an_end_before_the_start_is_refused(self):
        dialog = self.dialog(when=dt.datetime(2026, 9, 12, 15, 0, tzinfo=IST))
        dialog.summary.setText("Backwards")
        from PySide6.QtCore import QDate

        dialog.end_date.setDate(QDate(2026, 9, 11))
        dialog.accept()
        self.assertIn("before the start", dialog.warning.text())

    def test_a_guest_that_is_not_an_address_is_named_rather_than_dropped(self):
        dialog = self.dialog(when=dt.datetime(2026, 9, 12, 15, 0, tzinfo=IST))
        dialog.summary.setText("Reading group")
        dialog.guests.setText("Baker, Frances <f@x.com>")
        dialog.accept()
        self.assertIn("Baker", dialog.warning.text())
        dialog.guests.setText('"Baker, Frances" <f@x.com>')
        dialog.accept()
        self.assertEqual([g["address"] for g in dialog.values()["attendees"]],
                         ["f@x.com"])

    def test_moving_the_start_drags_an_end_that_would_be_before_it(self):
        from PySide6.QtCore import QTime

        dialog = self.dialog(when=dt.datetime(2026, 9, 12, 9, 0, tzinfo=IST))
        dialog.start_time.setTime(QTime(23, 0))
        values = dialog.values()
        self.assertGreater(values["ends_at"], values["starts_at"])


if __name__ == "__main__":
    unittest.main()
