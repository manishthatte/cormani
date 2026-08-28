# SPDX-License-Identifier: GPL-3.0-or-later
#
# The demo data.
#
# Two properties are load-bearing and are tested as such. It must be
# DETERMINISTIC, or every test written against it is flaky and every comparison
# with an earlier screenshot is meaningless. And it must REFUSE to run over a
# store that already holds accounts, because eleven fictional accounts in
# someone's real mail store is a mess that takes longer to unpick than it takes
# to lose confidence in the application.
#
# © Manish Jagdish Thatte
import datetime as dt
import hashlib
import unittest

from cormani.store import accounts, calendars, events, fixtures, folders
from cormani.store import messages, savedviews, times, touches, tracking, triage
from cormani.store import views

import support


def event_digest(con):
    """The calendar half of the determinism check.

    A second digest rather than more columns in the first, because the two
    halves fail differently: a message digest that moved would mean the mail
    fixtures changed, and this one moving means the calendar's did — and the
    calendar's is the one with a clock anywhere near it.
    """
    rows = con.execute(
        "SELECT calendar_id, remote_id, series_id, summary, starts_at, "
        "ends_at, all_day, status, my_response FROM event ORDER BY id").fetchall()
    sha = hashlib.sha256()
    for row in rows:
        sha.update(repr(tuple(row)).encode("utf-8"))
    return sha.hexdigest()


def digest(con):
    rows = con.execute(
        "SELECT folder_id, message_id, subject, date_at, from_addr, to_addrs, "
        "seen, flagged, answered, has_attachment, preview FROM message "
        "ORDER BY id").fetchall()
    sha = hashlib.sha256()
    for row in rows:
        sha.update(repr(tuple(row)).encode("utf-8"))
    return sha.hexdigest()


class TestFixtures(unittest.TestCase):
    def test_it_installs_what_it_says_it_installed(self):
        con = support.temp_store(self)
        report = fixtures.install(con)
        self.assertEqual(report["accounts"], len(accounts.list_accounts(con)))
        self.assertEqual(report["groups"], len(accounts.list_groups(con)))
        self.assertEqual(
            report["messages"],
            con.execute("SELECT COUNT(*) FROM message").fetchone()[0])
        self.assertTrue(fixtures.is_demo(con))

    def test_a_store_with_no_fixtures_does_not_claim_to_be_demo_data(self):
        self.assertFalse(fixtures.is_demo(support.temp_store(self)))

    def test_two_installs_are_byte_for_byte_identical(self):
        first, second = support.temp_store(self), support.temp_store(self)
        fixtures.install(first)
        fixtures.install(second)
        self.assertEqual(digest(first), digest(second))

    def test_it_refuses_a_store_that_already_has_an_account(self):
        con = support.temp_store(self)
        accounts.add_account(con, "real@example.com", "google")
        with self.assertRaises(RuntimeError) as caught:
            fixtures.install(con)
        self.assertIn("refusing", str(caught.exception).lower())
        # And it left nothing behind.
        self.assertEqual(len(accounts.list_accounts(con)), 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 0)

    def test_every_account_has_the_six_folders_with_their_roles(self):
        con = support.demo_store(self)
        for account in accounts.list_accounts(con):
            roles = {f.role for f in folders.list_folders(con, account.id)}
            self.assertEqual(roles, set(folders.ROLES), account.address)

    def test_no_message_sits_in_another_account_s_folder(self):
        con = support.demo_store(self)
        rows = messages.fetch(con, views.Scope(kind="unified",
                                                  role=folders.ROLE_INBOX), limit=500)
        for row in rows:
            folder = folders.get_folder(con, row.folder_id)
            self.assertEqual(folder.account_id, row.account_id)

    def test_the_list_is_long_enough_to_need_paging(self):
        # A list pane that has only ever been seen with thirty rows in it has
        # not been seen scrolling.
        con = support.demo_store(self)
        self.assertGreater(messages.count(con, views.Scope()), 120)

    def test_there_is_something_for_every_quick_filter_to_find(self):
        con = support.demo_store(self)
        scope = views.Scope()
        for name in ("unread", "flagged", "attachment", "contact", "tagged"):
            self.assertGreater(
                messages.count(con, scope, views.Filters(**{name: True})), 0, name)

    def test_the_bounced_address_is_present_for_the_guard_to_refuse(self):
        con = support.demo_store(self)
        row = con.execute(
            "SELECT value, bounce_count FROM handle WHERE status = 'bounced'").fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(row["bounce_count"], 0)


class TestCalendarFixtures(unittest.TestCase):
    """The calendar half, which stage 5 shipped empty.

    `--demo` opened a month grid that was correct and useless, and every
    judgement the interface asks to have made about a calendar — do the
    per-calendar colours read at a glance, does a clash draw sensibly, is the
    "+N more" overflow legible — is a claim about a calendar with something in
    it. So what is tested here is mostly not "a row exists" but "the views can
    be judged against this".
    """

    def setUp(self):
        self.con = support.demo_store(self)
        self.base = times.parse(fixtures.BASE_TIME).astimezone(times.local_zone())

    def window(self):
        """The window the demo claims, taken from a calendar rather than
        recomputed: recomputing it here would be a second definition, and the
        point of the assertion is that the recorded one is right."""
        first = calendars.list_calendars(self.con)[0]
        return (times.to_local(first.synced_from).date(),
                times.to_local(first.synced_to).date() - dt.timedelta(days=1))

    def test_it_installs_what_it_says_it_installed(self):
        report = fixtures.install(support.temp_store(self))
        self.assertEqual(report["calendars"],
                         len(calendars.list_calendars(self.con)))
        self.assertEqual(
            report["events"],
            self.con.execute("SELECT COUNT(*) FROM event").fetchone()[0])

    def test_two_installs_are_byte_for_byte_identical(self):
        # `events.new_local_id` is a uuid4, and one call to it in the fixture
        # would defeat this without failing anything else.
        first, second = support.temp_store(self), support.temp_store(self)
        fixtures.install(first)
        fixtures.install(second)
        self.assertEqual(event_digest(first), event_digest(second))

    def test_the_rail_has_calendars_ticked_and_calendars_not(self):
        # The tick is a control, and a control that is on for everything has
        # only been seen in one position.
        known = calendars.list_calendars(self.con)
        self.assertTrue([c for c in known if c.shown])
        self.assertTrue([c for c in known if not c.shown])

    def test_no_view_inside_the_window_says_it_has_not_been_fetched(self):
        """`ui/calendarpane._footer` refuses to draw an empty month as though
        it were an answer, and says "press F5" instead — which over demo data
        is advice that cannot be taken, because sync is disabled there. So
        every month of the claimed window must be covered."""
        first, last = self.window()
        shown = calendars.shown_ids(self.con)
        known = {c.id: c for c in calendars.list_calendars(self.con)}
        month = times.month_start(first)
        while month <= last:
            start, end = times.month_bounds(month)
            bounds = (times.to_utc_text(start), times.to_utc_text(end))
            for calendar_id in shown:
                self.assertTrue(known[calendar_id].covers(*bounds),
                                f"{known[calendar_id].label} in {month}")
            month = times.month_start(times.month_end(month)
                                      + dt.timedelta(days=1))

    def test_every_week_of_the_window_has_something_in_it(self):
        """The reason recurrence is expanded rather than a fortnight of
        one-offs written: a week view paged to any week of the window is a week
        somebody is going to look at, and an empty one tests nothing."""
        first, last = self.window()
        shown = calendars.shown_ids(self.con)
        day, empty = times.week_start(first), []
        while day <= last:
            start, end = times.week_bounds(day)
            if not events.events_between(self.con, start, end,
                                         calendar_ids=shown):
                empty.append(day.isoformat())
            day += dt.timedelta(days=7)
        self.assertEqual(empty, [])

    def test_one_day_is_busy_enough_to_overflow_a_month_cell(self):
        # "+N more" is not a decoration — `ui/monthview` gives up a line to
        # say what it hid, and a fixture where nothing is ever hidden leaves
        # that line undrawn and unjudged.
        start, end = times.month_bounds(self.base.date())
        found = events.events_between(self.con, start, end,
                                      calendar_ids=calendars.shown_ids(self.con))
        buckets = events.by_day(found, start, end)
        self.assertGreaterEqual(max(len(v) for v in buckets.values()), 6)

    def test_two_calendars_clash_on_one_day(self):
        # The case a week grid draws worst, and the reason per-calendar colour
        # exists at all.
        start, end = times.month_bounds(self.base.date())
        found = events.events_between(self.con, start, end,
                                      calendar_ids=calendars.shown_ids(self.con))
        clashes = [(a, b) for i, a in enumerate(found) for b in found[i + 1:]
                   if not a.all_day and not b.all_day
                   and a.calendar_id != b.calendar_id
                   and a.start() < b.end() and b.start() < a.end()]
        self.assertTrue(clashes)

    def test_a_one_day_all_day_event_is_drawn_on_one_day(self):
        """The class of bug the exclusive end exists to cause: both providers
        make an all-day end the NEXT date, and a view that reads it naively
        draws a one-day holiday across two."""
        start = dt.datetime.combine(self.base.date() - dt.timedelta(days=200),
                                    dt.time(0, 0), self.base.tzinfo)
        end = start + dt.timedelta(days=500)
        found = [e for e in events.events_between(self.con, start, end)
                 if e.all_day]
        self.assertTrue(found)
        buckets = events.by_day(found, start, end)
        for event in found:
            days = [d for d, on in buckets.items() if event in on]
            length = (dt.date.fromisoformat(event.ends_at)
                      - dt.date.fromisoformat(event.starts_at)).days
            self.assertEqual(len(days), length, event.summary)

    def test_there_is_an_invitation_waiting_to_be_answered(self):
        # Which is what the invitation bar and the Owed view are for.
        self.assertTrue(events.needing_reply(self.con, now=self.base))

    def test_there_is_an_event_the_user_made_offline(self):
        """All three parts of it, because any one alone is a state the
        application cannot reach: a local id with no op will never be sent, and
        an op with no marker is a row the list draws as though it had been."""
        row = self.con.execute(
            "SELECT id, remote_id, pending FROM event WHERE pending <> ''"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(events.is_local(row["remote_id"]))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM event_op WHERE event_id = ?",
                             (row["id"],)).fetchone()[0], 1)

    def test_a_recurring_series_is_instances_that_share_one_uid(self):
        """Stage 5's first decision: the server expands the rule and a row is
        one INSTANCE. The instances are separate rows with separate remote ids
        and one iCalendar UID between them, which is what stage 6's tracking
        layer will join on."""
        series = self.con.execute(
            "SELECT series_id FROM event WHERE series_id <> '' "
            "GROUP BY series_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
        rows = self.con.execute(
            "SELECT remote_id, ical_uid FROM event WHERE series_id = ?",
            (series,)).fetchall()
        self.assertGreater(len(rows), 10)
        self.assertEqual(len({r["remote_id"] for r in rows}), len(rows))
        self.assertEqual(len({r["ical_uid"] for r in rows}), 1)

    def test_a_cancelled_event_is_held_and_not_drawn(self):
        held = self.con.execute(
            "SELECT COUNT(*) FROM event WHERE status = 'cancelled'").fetchone()[0]
        self.assertGreater(held, 0)
        start, end = times.month_bounds(self.base.date())
        drawn = events.events_between(self.con, start, end)
        self.assertEqual([e for e in drawn if e.status == "cancelled"], [])

    def test_no_event_sits_in_another_account_s_calendar(self):
        rows = self.con.execute(
            "SELECT e.id FROM event e JOIN calendar c ON c.id = e.calendar_id "
            "JOIN account a ON a.id = c.account_id").fetchall()
        self.assertEqual(
            len(rows),
            self.con.execute("SELECT COUNT(*) FROM event").fetchone()[0])

    def test_the_guests_are_people_the_address_book_knows(self):
        # An attendee chip that resolves to a contact renders differently from
        # one that does not, and both have to be on screen.
        known = {r[0] for r in self.con.execute(
            "SELECT value FROM handle WHERE kind = 'email'").fetchall()}
        guests = {r[0] for r in self.con.execute(
            "SELECT address FROM attendee WHERE is_self = 0").fetchall()}
        self.assertTrue(guests)
        self.assertTrue(guests & known)


if __name__ == "__main__":
    unittest.main()


class TestTrackingFixtures(unittest.TestCase):
    """The tracking layer's demo data, which is where stage 6 is judged from.

    The calendar taught this at a cost: stage 5 shipped with no demo events and
    the month grid was correct and useless. Every judgement the tracking pane
    asks to have made is a claim about a board with something on it, so what is
    tested here is that the board has one of each KIND of row — not that a
    particular thread exists.
    """

    def setUp(self):
        self.con = support.demo_store(self)
        self.today = times.parse(fixtures.BASE_TIME).astimezone(
            times.local_zone()).date()

    def test_it_installs_what_it_says_it_installed(self):
        report = fixtures.install(support.temp_store(self))
        self.assertEqual(report["threads"],
                         len(tracking.list_threads(self.con, state="")))
        self.assertEqual(
            report["touches"],
            self.con.execute("SELECT COUNT(*) FROM touch").fetchone()[0])

    def test_the_timelines_are_built_by_the_REAL_matchers(self):
        """Not invented here. If a matcher breaks, the demo board goes visibly
        thin — which is a test nobody had to write."""
        filed = [t for t in self.con.execute(
            "SELECT source, message_id FROM touch WHERE message_id IS NOT NULL"
        ).fetchall()]
        self.assertTrue(filed)
        self.assertIn(touches.SOURCE_ATTACHED, {r["source"] for r in filed})

    def test_there_is_one_row_of_every_kind_of_attention(self):
        """Owed, a deadline coming, a deadline PASSED, and a plain nudge. They
        are drawn three different ways and a demo missing one never draws it."""
        live = tracking.list_threads(self.con, state="live")
        self.assertTrue([t for t in live if t.owed], "nothing owed")
        self.assertTrue([t for t in live
                         if (t.days_to_deadline(self.today) or -99) > 0],
                        "no deadline ahead")
        self.assertTrue([t for t in live
                         if (t.days_to_deadline(self.today) or 99) < 0],
                        "no deadline passed")
        self.assertTrue([t for t in live if t.overdue(self.today)
                         and t.deadline_date == ""], "no plain nudge")

    def test_a_closed_thread_is_there_so_the_board_is_not_only_live_rows(self):
        self.assertTrue(tracking.list_threads(self.con,
                                              state=tracking.STATE_CLOSED))

    def test_a_blocked_thread_keeps_its_deadline_and_loses_its_nudge(self):
        blocked = tracking.list_threads(self.con,
                                        state=tracking.STATE_BLOCKED)
        self.assertTrue(blocked)
        for thread in blocked:
            self.assertFalse(thread.overdue(self.today), thread.title)

    def test_a_timeline_crosses_channels(self):
        # The whole claim of PLAN.txt §2: one timeline per correspondent
        # regardless of how they reached you.
        channels = set()
        for thread in tracking.list_threads(self.con, state=""):
            channels |= set(thread.channels)
        self.assertTrue({"email", "phone"} <= channels, channels)
        self.assertTrue(channels & {"whatsapp", "meeting"}, channels)

    def test_the_triage_queue_has_something_in_its_NARROW_scope(self):
        """The default scope is the one a person opens. A demo whose default
        queue is empty demonstrates the queue not at all — and the wider counts
        beside it are what the scoping argument is about."""
        counts = triage.counts(self.con)
        self.assertGreater(counts[triage.SCOPE_KNOWN], 0)
        self.assertGreater(counts[triage.SCOPE_ALL], counts[triage.SCOPE_KNOWN])

    def test_no_thread_is_buried_under_generated_filler(self):
        """`fixtures._FILLER_SENDERS` reuses two real contacts' addresses, so
        putting either on a thread makes the address matcher file all hundred
        and sixty — a timeline of "Weekly digest #37" where the story is four
        rows. The guard is that no demo timeline is absurdly long."""
        for thread in tracking.list_threads(self.con, state=""):
            self.assertLess(thread.touches, 15, thread.title)

    def test_two_installs_are_byte_for_byte_identical(self):
        first, second = support.temp_store(self), support.temp_store(self)
        fixtures.install(first)
        fixtures.install(second)
        digest = lambda con: [tuple(r) for r in con.execute(
            "SELECT thread_id, channel, direction, occurred_at, subject "
            "FROM touch ORDER BY thread_id, occurred_at, id").fetchall()]
        self.assertEqual(digest(first), digest(second))


class TestTheDemoStoreStaysCurrent(unittest.TestCase):
    """A cached demo store must not go on demonstrating an older edition.

    THE CASE THIS EXISTS FOR ACTUALLY HAPPENED. The demo store lives in the
    cache directory and was installed once; a store built before stage 5 then
    sat through the whole of stages 5 and 6 holding 197 messages, no calendars
    and no tracked threads — demonstrating precisely the emptiness those stages
    existed to fix. Nothing was wrong except that the fixtures had improved and
    the store had no way to know.
    """

    def test_a_fresh_install_records_the_edition_it_wrote(self):
        con = support.temp_store(self)
        fixtures.install(con)
        self.assertEqual(fixtures.installed_version(con),
                         fixtures.FIXTURE_VERSION)
        self.assertTrue(fixtures.is_current(con))

    def test_a_store_from_before_the_number_existed_is_not_current(self):
        from cormani.store.database import set_meta

        con = support.temp_store(self)
        fixtures.install(con)
        set_meta(con, fixtures.DEMO_VERSION_KEY, "0")
        con.commit()
        self.assertTrue(fixtures.is_demo(con))
        self.assertFalse(fixtures.is_current(con))

    def test_opening_a_stale_demo_store_rebuilds_it(self):
        """End to end through `app.open_store`, because the rebuild is the part
        that had to be added and it lives there rather than in the fixtures."""
        import tempfile
        from pathlib import Path

        from cormani.app import open_store
        from cormani.platform.paths import Paths
        from cormani.store import database

        # `root=` is what Paths offers tests, so nothing touches the real
        # profile — which matters twice here, because this test DELETES a
        # store.
        paths = Paths(root=Path(tempfile.mkdtemp())).ensure()

        con = open_store(paths, demo=True)
        self.assertGreater(
            con.execute("SELECT COUNT(*) FROM thread").fetchone()[0], 0)
        # Pretend it was built by an older edition, and empty the half that
        # edition did not know about.
        database.set_meta(con, fixtures.DEMO_VERSION_KEY, "0")
        con.execute("DELETE FROM thread")
        con.commit()
        con.close()

        again = open_store(paths, demo=True)
        self.addCleanup(again.close)
        self.assertTrue(fixtures.is_current(again))
        self.assertGreater(
            again.execute("SELECT COUNT(*) FROM thread").fetchone()[0], 0)

    def test_a_current_demo_store_is_left_exactly_as_it_was(self):
        # Rebuilding one that is already current would throw away whatever the
        # person had been doing in it between one launch and the next.
        import tempfile
        from pathlib import Path

        from cormani.app import open_store
        from cormani.platform.paths import Paths

        paths = Paths(root=Path(tempfile.mkdtemp())).ensure()
        con = open_store(paths, demo=True)
        tracking.create_thread(con, "Something the user added")
        con.close()

        again = open_store(paths, demo=True)
        self.addCleanup(again.close)
        self.assertTrue([t for t in tracking.list_threads(again, state="")
                         if t.title == "Something the user added"])

class TestTheDemoSavedSearches(unittest.TestCase):
    """Virtual folders a person can click, that find something when clicked.

    A demo whose saved searches all read zero would show the interface in
    exactly the state somebody would be in if the feature were broken — three
    named rows, every one of them empty. `store/viewfixtures.py` argues it, and
    what makes the argument checkable is that the fixture COUNTS each view as
    it writes it rather than asserting the count here from memory.
    """

    def setUp(self):
        self.con = support.temp_store(self)
        self.report = fixtures.install(self.con)
        self.views = {v.name: v for v in savedviews.list_views(self.con)}

    def test_the_demo_has_saved_searches_and_says_how_many(self):
        self.assertEqual(self.report["views"], len(self.views))
        self.assertGreaterEqual(len(self.views), 3)

    def test_every_one_of_them_FINDS_something(self):
        # The assertion the whole fixture exists for.
        for name, held in self.report["views_found"].items():
            with self.subTest(name):
                self.assertGreater(held, 0)

    def test_what_the_fixture_reported_is_what_the_store_now_says(self):
        # The fixture's own count against a fresh one, so a report that had
        # drifted from the data could not pass the test above by itself.
        for name, held in self.report["views_found"].items():
            with self.subTest(name):
                self.assertEqual(
                    savedviews.count_in(self.con, self.views[name]), held)

    def test_one_is_deliberately_kept_out_of_the_rail(self):
        # `in_rail` cannot be demonstrated by a list in which every row is
        # ticked, and the Saved searches menu would look like a duplicate of
        # the rail rather than the place the unticked ones live.
        drawn = [v for v in self.views.values() if v.in_rail]
        self.assertTrue(drawn)
        self.assertNotEqual(len(drawn), len(self.views))

    def test_none_of_them_is_stale(self):
        # A demo fixture that came out `unresolved` would put "THIS CANNOT RUN"
        # in a screenshot. Every scope is unified for that reason.
        for name, view in self.views.items():
            with self.subTest(name):
                self.assertEqual(savedviews.unresolved(self.con, view), "")

    def test_one_of_them_is_a_full_text_search(self):
        # The half a Quick Filter cannot do at all. A demo of three toggle
        # combinations would not show what a saved SEARCH is.
        self.assertTrue(any(v.search.active for v in self.views.values()))


class TestTheDemoFilterRules(unittest.TestCase):
    """Rules a person can look at, and counts that are not invented.

    The point of a filter read-out is that `match_count` is the only evidence a
    rule ever offers. A fixture that wrote plausible numbers into it would be
    showing a demo of a mailbox where forty-one messages had been filed and
    nothing had moved — so the fixture RUNS the rules and takes whatever
    happened, and these assert that the two agree.
    """

    def setUp(self):
        from cormani.store import rules
        self.con = support.temp_store(self)
        self.report = fixtures.install(self.con)
        self.rules = {r.name: r for r in rules.list_rules(self.con)}

    def tagged(self, name: str) -> int:
        return self.con.execute(
            "SELECT COUNT(*) FROM message_tag t JOIN tag g ON g.id = t.tag_id "
            "WHERE g.name = ?", (name,)).fetchone()[0]

    def test_the_demo_has_rules_and_says_how_many(self):
        self.assertEqual(self.report["rules"], len(self.rules))
        self.assertGreaterEqual(len(self.rules), 3)

    def test_the_enabled_rules_have_actually_run(self):
        covalent = self.rules["Covalent Example"]
        reading = self.rules["Reading, not doing"]
        self.assertGreater(covalent.match_count, 0)
        self.assertGreater(reading.match_count, 0)
        self.assertEqual(self.report["filtered"],
                         covalent.match_count + reading.match_count)

    def test_the_switched_off_rule_has_not(self):
        off = self.rules["File the receipts"]
        self.assertFalse(off.enabled)
        self.assertEqual(off.match_count, 0)
        self.assertIsNone(off.last_matched_at)

    def test_the_counts_agree_with_what_is_in_the_mailbox(self):
        # The whole point: evidence a person can check against the mail in
        # front of them.
        self.assertGreaterEqual(self.tagged("Later"),
                                self.rules["Reading, not doing"].match_count)
        work = self.con.execute(
            "SELECT m.from_addr FROM message m "
            "JOIN message_tag t ON t.message_id = m.id "
            "JOIN tag g ON g.id = t.tag_id WHERE g.name = 'Work'").fetchall()
        self.assertTrue(any("covalent.example" in r[0] for r in work))

    def test_the_demo_owes_a_server_nothing(self):
        """A demo store with a queued op would show an outbox that can never
        drain — there is no server behind it, and the status bar would be
        saying something untrue for as long as the store exists.

        It holds because a demo message has `uid = NULL`, so the queue skips
        it, AND because the enabled rules only tag, which is local anyway.
        """
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM pending_op").fetchone()[0], 0)

    def test_two_installs_catch_the_same_mail(self):
        from cormani.store import rules
        second = support.temp_store(self)
        fixtures.install(second)
        self.assertEqual(
            [(r.name, r.match_count) for r in rules.list_rules(second)],
            [(r.name, r.match_count) for r in rules.list_rules(self.con)])

