# SPDX-License-Identifier: GPL-3.0-or-later
#
# The calendar pane, the rail that opens it, and the invitation bar.
#
# EVERY TEST HERE ENDS AT THE STORE. That is the rule stage 3's command bar and
# stage 4's inline reply each cost a feature to learn: a widget test that drives
# the widget's own methods proves the widget, and only a test that starts at a
# SIGNAL and asserts a row proves the wiring between two of them. So making an
# event asserts an event row and a queued op, answering an invitation asserts
# the response on the event, and choosing a calendar in the rail asserts that
# the pane swapped.
#
# The dialogs are injected. Debian packages no QTest and a modal dialog cannot
# be driven from a test, which is why `CalendarPane` takes its four dialogs as
# parameters — the same seam the attachment strip uses.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from cormani.store import calendars as calendars_repo
from cormani.store import eventqueue
from cormani.store import events as events_repo
from cormani.store import folders as folders_repo
from cormani.store import ingest
from cormani.store.accounts import add_account
import support

support.qt_app() if support.HAVE_QT else None

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TODAY = dt.date(2026, 9, 12)


class Values:
    """A dialog that was never opened, answering with what a person typed."""

    def __init__(self, **values):
        self._values = values

    def values(self) -> dict:
        return dict(self._values)


@support.requires_qt
class Pane(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, address="a@b.c", provider="google")
        self.calendar = calendars_repo.ensure_calendar(
            self.con, self.account, "primary", name="Mine", is_primary=True)
        self.answers = {}

    def pane(self, **dialogs):
        from cormani.ui.calendarpane import CalendarPane

        dialogs.setdefault("run", lambda dialog: True)
        pane = support.own(self, CalendarPane(self.con, dialogs=dialogs))
        pane.set_timezone(IST)
        pane.set_anchor(TODAY)
        return pane

    def add(self, remote_id="e1", start="2026-09-12T09:00:00+00:00",
            end="2026-09-12T10:00:00+00:00", **fields):
        fields.update(starts_at=start, ends_at=end)
        fields.setdefault("summary", remote_id)
        return events_repo.upsert(self.con, self.calendar, remote_id, fields,
                                  attendees=fields.pop("attendees", None))

    # ------------------------------------------------------------- drawing
    def test_the_pane_draws_what_the_store_holds(self):
        self.add("e1", summary="Reading group")
        pane = self.pane()
        self.assertEqual([e.summary for e in pane.view()._events],
                         ["Reading group"])
        self.assertIn("1 event", pane.footer.text())

    def test_switching_view_keeps_the_day_the_user_was_on(self):
        pane = self.pane()
        for mode in ("week", "day", "agenda", "month"):
            pane.set_mode(mode)
            self.assertEqual(pane.anchor(), TODAY)
        self.assertEqual(pane.title(), "September 2026")

    def test_the_arrows_step_by_the_unit_being_shown(self):
        pane = self.pane()
        pane.step(1)
        self.assertEqual(pane.anchor(), dt.date(2026, 10, 1))
        pane.set_anchor(TODAY)
        pane.set_mode("week")
        pane.step(-1)
        self.assertEqual(pane.anchor(), dt.date(2026, 9, 5))
        pane.set_mode("day")
        pane.step(1)
        self.assertEqual(pane.anchor(), dt.date(2026, 9, 6))

    def test_a_range_that_was_never_fetched_says_so_rather_than_looking_empty(self):
        pane = self.pane()
        asked = []
        pane.fetch_requested.connect(asked.append)
        pane.set_anchor(dt.date(2019, 3, 4))
        self.assertIn("not been fetched", pane.footer.text())
        self.assertTrue(asked)

        calendars_repo.record_sync_state(
            self.con, self.calendar, synced_from="2019-01-01T00:00:00+00:00",
            synced_to="2020-01-01T00:00:00+00:00")
        pane.reload()
        self.assertNotIn("not been fetched", pane.footer.text())

    def test_an_unticked_calendar_is_not_drawn(self):
        self.add("e1")
        pane = self.pane()
        self.assertEqual(len(pane.view()._events), 1)
        calendars_repo.set_shown(self.con, self.calendar, False)
        pane.reload()
        self.assertEqual(len(pane.view()._events), 0)
        self.assertIn("No calendar is being shown", pane.footer.text())

    # ------------------------------------------------------------- writing
    def test_making_an_event_writes_a_row_and_queues_it(self):
        pane = self.pane(event=lambda **kw: Values(
            calendar_id=self.calendar, summary="Reading group",
            starts_at="2026-09-12T09:00:00+00:00",
            ends_at="2026-09-12T10:00:00+00:00", all_day=False, busy=True,
            location="", description="", reminder=None, attendees=[]))
        said = []
        pane.status_message.connect(said.append)
        pane.new_event(None)

        events = events_repo.events_between(
            self.con, dt.datetime(2026, 9, 12, tzinfo=IST),
            dt.datetime(2026, 9, 13, tzinfo=IST))
        self.assertEqual([e.summary for e in events], ["Reading group"])
        self.assertTrue(events[0].is_local)
        self.assertEqual([op.kind for op in
                          eventqueue.pending_for(self.con, self.account)],
                         ["create"])
        self.assertTrue(any("next sync" in message for message in said))

    def test_editing_from_the_detail_panel_reaches_the_store(self):
        event_id = self.add("e1", summary="Old")
        pane = self.pane(event=lambda **kw: Values(
            calendar_id=self.calendar, summary="New",
            starts_at="2026-09-12T09:00:00+00:00",
            ends_at="2026-09-12T10:00:00+00:00", all_day=False, busy=True,
            location="Room 2", description="", reminder=None, attendees=[]))
        pane.view().select_event(event_id)
        pane.detail.command.emit("edit")
        stored = events_repo.get_event(self.con, event_id)
        self.assertEqual((stored.summary, stored.location), ("New", "Room 2"))
        self.assertEqual([op.kind for op in
                          eventqueue.pending_for(self.con, self.account)],
                         ["update"])

    def test_deleting_asks_first_and_then_removes_the_row(self):
        event_id = self.add("e1")
        asked = []
        pane = self.pane(confirm=lambda title, detail: asked.append(title) or True)
        pane.view().select_event(event_id)
        pane.detail.command.emit("delete")
        self.assertTrue(asked)
        self.assertIsNone(events_repo.get_event(self.con, event_id))

    def test_a_refused_deletion_changes_nothing(self):
        event_id = self.add("e1")
        pane = self.pane(confirm=lambda title, detail: False)
        pane.view().select_event(event_id)
        pane.detail.command.emit("delete")
        self.assertIsNotNone(events_repo.get_event(self.con, event_id))

    def test_answering_from_the_detail_panel_reaches_the_store(self):
        event_id = self.add(
            "inv1", organiser_addr="them@x.com",
            attendees=[{"address": "them@x.com", "is_organiser": True},
                       {"address": "a@b.c", "is_self": True}])
        pane = self.pane()
        pane.view().select_event(event_id)
        self.assertTrue(pane.detail.answers.isVisibleTo(pane.detail))
        pane.detail.respond.emit("accepted")
        self.assertEqual(events_repo.get_event(self.con, event_id).my_response,
                         "accepted")
        self.assertEqual([op.kind for op in
                          eventqueue.pending_for(self.con, self.account)],
                         ["respond"])

    def test_a_read_only_calendar_offers_no_edit_button(self):
        shared = calendars_repo.ensure_calendar(self.con, self.account,
                                                "holidays", name="Holidays",
                                                writable=False)
        event_id = events_repo.upsert(
            self.con, shared, "h1",
            {"summary": "Diwali", "starts_at": "2026-09-12", "all_day": True,
             "ends_at": "2026-09-13"})
        pane = self.pane()
        pane.view().select_event(event_id)
        self.assertFalse(pane.detail.edit_button.isEnabled())
        self.assertFalse(pane.detail.delete_button.isEnabled())
        self.assertIn("read-only", pane.detail.note.text())


@support.requires_qt
class InTheWindow(unittest.TestCase):
    """The rail, the pane it swaps in, and the tab that remembers which."""

    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, address="a@b.c", provider="google")
        self.calendar = calendars_repo.ensure_calendar(
            self.con, self.account, "primary", name="Mine")

    def pane(self):
        from cormani.ui.mailpane import MailPane

        return support.own(self, MailPane(self.con))

    def test_choosing_a_calendar_in_the_rail_swaps_the_panes(self):
        pane = self.pane()
        self.assertFalse(pane.showing_calendar())
        self.assertTrue(pane.rail.select_key(f"calendar:{self.calendar}"))
        self.assertTrue(pane.showing_calendar())
        self.assertEqual(pane.calendar.calendar_ids(), [self.calendar])
        self.assertEqual(pane.title_for_scope(), "Mine")

    def test_choosing_a_folder_puts_the_mail_back(self):
        pane = self.pane()
        pane.rail.select_key(f"calendar:{self.calendar}")
        pane.rail.select_key("unified:inbox")
        self.assertFalse(pane.showing_calendar())

    def test_all_calendars_means_every_ticked_one(self):
        second = calendars_repo.ensure_calendar(self.con, self.account,
                                                "other", name="Other")
        pane = self.pane()
        pane.rail.select_key("calendar:all")
        self.assertEqual(sorted(pane.calendar.calendar_ids()),
                         sorted([self.calendar, second]))
        self.assertEqual(pane.calendar.chosen_id(), 0)

    def test_a_tab_remembers_that_it_was_showing_the_calendar(self):
        pane = self.pane()
        pane.rail.select_key(f"calendar:{self.calendar}")
        pane.calendar.set_mode("week")
        pane.calendar.set_anchor(TODAY)
        state = pane.view_state("Mine")
        self.assertTrue(state.is_calendar)

        pane.rail.select_key("unified:inbox")
        self.assertFalse(pane.showing_calendar())
        pane.restore(state)
        self.assertTrue(pane.showing_calendar())
        self.assertEqual(pane.calendar.mode(), "week")
        self.assertEqual(pane.calendar.anchor(), TODAY)

    def test_the_menu_can_open_the_calendar_without_the_rail(self):
        pane = self.pane()
        pane.calendar_mode("agenda")
        self.assertTrue(pane.showing_calendar())
        self.assertEqual(pane.calendar.mode(), "agenda")
        pane.calendar_action("today")
        self.assertEqual(pane.calendar.anchor(),
                         dt.datetime.now().astimezone().date())

    def test_unticking_a_calendar_redraws_the_pane(self):
        events_repo.upsert(self.con, self.calendar, "e1",
                           {"summary": "Call",
                            "starts_at": "2026-09-12T09:00:00+00:00",
                            "ends_at": "2026-09-12T10:00:00+00:00"})
        pane = self.pane()
        pane.rail.select_key("calendar:all")
        pane.calendar.set_anchor(TODAY)
        self.assertEqual(len(pane.calendar.view()._events), 1)
        pane.rail._set_shown(self.calendar, False)
        self.assertEqual(len(pane.calendar.view()._events), 0)

    def test_a_store_with_no_calendars_says_so_instead_of_opening_one(self):
        from cormani.store import database

        con = support.temp_store(self)
        add_account(con, address="plain@example.org", provider="imap")
        from cormani.ui.mailpane import MailPane

        pane = support.own(self, MailPane(con))
        said = []
        pane.status_message.connect(said.append)
        pane.show_calendar()
        self.assertFalse(pane.showing_calendar())
        self.assertTrue(any("no calendar" in m.lower() for m in said))
        del database


@support.requires_qt
class Invitations(unittest.TestCase):
    """From the bar in the reading pane to the row in the store."""

    def setUp(self):
        from tests.test_itip import GOOGLE_ICS, invitation_mail
        from cormani.imap import envelope

        self.con = support.temp_store(self)
        self.root = Path(tempfile.mkdtemp(prefix="cormani-test-att-"))
        self.addCleanup(_rmtree, self.root)
        self.account = add_account(self.con, address="someone@gmail.com",
                                   provider="google", display_name="Manish")
        self.folder = folders_repo.ensure_folder(self.con, self.account,
                                                 "INBOX", role="inbox")
        env = envelope.read(invitation_mail(GOOGLE_ICS))
        self.message = ingest.store_message(
            self.con, self.folder, 101, env, attachments_root=self.root,
            account_id=self.account).message_id

    def pane(self):
        from cormani.ui.mailpane import MailPane

        return support.own(self, MailPane(self.con, attachments_root=self.root))

    def test_the_bar_appears_on_an_invitation_and_not_on_other_mail(self):
        pane = self.pane()
        pane.select_message(self.message)
        bar = pane.reader.invitation
        self.assertIsNotNone(bar.found())
        self.assertEqual(bar.summary.text(), "DWCNT wavelengths, and the rest")
        self.assertIn("by mail", bar.route.text())

    def test_answering_from_the_bar_queues_a_reply(self):
        pane = self.pane()
        pane.select_message(self.message)
        said = []
        pane.status_message.connect(said.append)
        pane.reader.invitation.answered.emit("accepted")

        drafts = self.con.execute(
            "SELECT id, subject FROM message WHERE draft = 1").fetchall()
        self.assertEqual(len(drafts), 1)
        self.assertTrue(drafts[0]["subject"].startswith("Accepted:"))
        # A `flag` op is there too and is not this test's business: selecting
        # the message marked it read, which is the mail half doing its job.
        ops = self.con.execute(
            "SELECT kind, message_id FROM pending_op WHERE kind = 'send'"
        ).fetchall()
        self.assertEqual([o["message_id"] for o in ops], [drafts[0]["id"]])
        self.assertTrue(any("queued" in m for m in said))

    def test_answering_a_meeting_in_a_calendar_goes_to_the_provider(self):
        calendar = calendars_repo.ensure_calendar(self.con, self.account,
                                                  "primary")
        event_id = events_repo.upsert(
            self.con, calendar, "srv-1",
            {"ical_uid": "abc123@google.com", "summary": "DWCNT",
             "organiser_addr": "frances@example.com",
             "starts_at": "2026-09-12T09:30:00+00:00",
             "ends_at": "2026-09-12T10:30:00+00:00"})
        pane = self.pane()
        pane.select_message(self.message)
        self.assertIn("your calendar", pane.reader.invitation.route.text())
        pane.reader.invitation.answered.emit("declined")
        self.assertEqual(events_repo.get_event(self.con, event_id).my_response,
                         "declined")
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM message WHERE draft = 1"
                             ).fetchone()[0], 0)

    def test_the_bar_shows_the_answer_that_was_just_given(self):
        pane = self.pane()
        pane.select_message(self.message)
        pane.reader.invitation.answered.emit("tentative")
        # Redrawn FROM THE STORE rather than from what the click said.
        self.assertIn("not in a calendar", pane.reader.invitation.route.text())

    def test_an_ordinary_message_has_no_bar(self):
        from cormani.imap import envelope

        env = envelope.read(b"From: a@b.c\r\nSubject: Hi\r\n\r\nNothing.\r\n")
        other = ingest.store_message(self.con, self.folder, 202, env,
                                     attachments_root=self.root,
                                     account_id=self.account).message_id
        pane = self.pane()
        pane.select_message(other)
        self.assertIsNone(pane.reader.invitation.found())


def _rmtree(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


@support.requires_qt
class DemoData(unittest.TestCase):
    """The demo store's calendar, which stage 5 shipped empty.

    One test, and it is the one a person would actually perform: open --demo on
    the month the fixtures are built around, and look. Everything else about
    the fixture is asserted against the STORE in test_fixtures.py; this is the
    half only the widget can answer, because the footer is where an unfetched
    range is confessed rather than drawn as an empty month.

    The pane's own timezone is left alone deliberately — it defaults to the
    machine's, and so do the fixtures, which is the pairing a person sees.
    """

    def test_the_demo_month_is_full_and_asks_for_no_sync(self):
        from cormani.store import fixtures, times
        from cormani.ui.calendarpane import CalendarPane

        con = support.demo_store(self)
        base = times.parse(fixtures.BASE_TIME).astimezone(times.local_zone())
        pane = support.own(self, CalendarPane(con, dialogs={}))
        pane.set_mode("month")
        pane.set_anchor(base.date())
        # The footer rather than the view's own list: it is what a person
        # reads, and a WeekView is two widgets each holding half the events,
        # so there is no single list to count anyway.
        self.assertGreater(int(pane.footer.text().split()[0]), 20)
        # "press F5" over demo data is advice that cannot be taken: there is no
        # server behind it and the menu says so.
        self.assertNotIn("has not been fetched", pane.footer.text())

    def test_a_week_a_year_out_is_still_worth_looking_at(self):
        """The fortnight problem: a fixture of one-off events near the base
        makes every view outside that fortnight empty again, which is the
        defect this replaced rather than a smaller version of it."""
        import datetime as dt

        from cormani.store import fixtures, times
        from cormani.ui.calendarpane import CalendarPane

        con = support.demo_store(self)
        base = times.parse(fixtures.BASE_TIME).astimezone(times.local_zone())
        pane = support.own(self, CalendarPane(con, dialogs={}))
        pane.set_mode("week")
        pane.set_anchor(base.date() + dt.timedelta(days=300))
        self.assertGreater(int(pane.footer.text().split()[0]), 0)
        self.assertNotIn("has not been fetched", pane.footer.text())


if __name__ == "__main__":
    unittest.main()
