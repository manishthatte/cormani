# SPDX-License-Identifier: GPL-3.0-or-later
#
# The tracking layer's store: tracked threads, their timelines, and the people.
#
# WHICH "THREAD" THIS FILE IS ABOUT. The TRACKED one — authored by hand,
# carrying a state and a deadline. `tests/test_threads.py` is the other one, a
# References chain derived from headers. `store/trackschema.py` records why one
# word carries both and what keeps them apart.
#
# WHAT IS WORTH TESTING HERE IS THE DERIVATION AND NOT THE COLUMNS. A test that
# writes `state='open'` and reads back `'open'` is testing SQLite. What can
# actually be wrong is the arithmetic that turns a timeline into "owed for four
# days", "overdue", "eleven days to a deadline" — every one of which is a
# function of TODAY, which is why the methods take it and every test here
# passes one. A suite whose answers change at midnight is a suite that fails
# once a month and is re-run until it passes.
#
# © Manish Jagdish Thatte
import datetime as dt
import unittest

import support

from cormani.store import contacts, times, touches, tracking

TODAY = dt.date(2026, 9, 12)


def at(days_ago: int, hour: int = 12) -> str:
    """A UTC stamp `days_ago` before TODAY, local-noon by default.

    Noon and not midnight, deliberately: an event at local midnight is on a
    different UTC DAY for half the world, and a fixture that straddles the
    boundary tests the machine's timezone rather than the code.
    """
    when = dt.datetime.combine(TODAY - dt.timedelta(days=days_ago),
                               dt.time(hour, 0), times.local_zone())
    return times.to_utc_text(when)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)

    def thread(self, title="DWCNT wavelengths", **kwargs):
        kwargs.setdefault("org", "Covalent Example")
        return tracking.create_thread(self.con, title, **kwargs)

    def touch(self, thread_id, direction, days_ago, *, hour=12, **kwargs):
        kwargs.setdefault("channel", touches.CHANNEL_EMAIL)
        kwargs.setdefault("subject", f"{direction} {days_ago}")
        return touches.add_touch(self.con, thread_id, direction=direction,
                                 occurred_at=at(days_ago, hour), **kwargs)


class TestSlugs(Fixture):
    def test_a_slug_is_derived_from_the_org_and_the_title(self):
        tid = self.thread()
        self.assertEqual(tracking.get_thread(self.con, tid).slug,
                         "covalent-example-dwcnt-wavelengths")

    def test_a_second_thread_of_the_same_name_gets_its_own_slug(self):
        self.thread()
        second = self.thread()
        self.assertEqual(tracking.get_thread(self.con, second).slug,
                         "covalent-example-dwcnt-wavelengths-2")

    def test_a_title_with_no_usable_characters_still_gets_a_slug(self):
        # A slug is UNIQUE and NOT NULL, so an empty one is a constraint
        # failure at insert rather than an ugly URL.
        tid = tracking.create_thread(self.con, "«»", org="")
        self.assertEqual(tracking.get_thread(self.con, tid).slug, "thread")

    def test_a_deleted_slug_is_not_handed_out_again(self):
        """A thread is referred to from notes and from memory. A slug that came
        back pointing at something else is worse than one that points at
        nothing."""
        first = self.thread()
        tracking.delete_thread(self.con, first)
        second = self.thread()
        self.assertEqual(tracking.get_thread(self.con, second).slug,
                         "covalent-example-dwcnt-wavelengths")
        third = self.thread()
        self.assertNotEqual(tracking.get_thread(self.con, third).slug,
                            tracking.get_thread(self.con, second).slug)


class TestOwed(Fixture):
    """PLAN.txt §2: Owed is a FACT — they answered last and this side has not."""

    def test_they_answered_last_and_this_side_has_not(self):
        tid = self.thread()
        self.touch(tid, "out", 9)
        self.touch(tid, "in", 4)
        thread = tracking.get_thread(self.con, tid)
        self.assertTrue(thread.owed)
        self.assertEqual(thread.owed_days(TODAY), 4)

    def test_answering_discharges_it(self):
        tid = self.thread()
        self.touch(tid, "in", 4)
        self.touch(tid, "out", 1)
        thread = tracking.get_thread(self.con, tid)
        self.assertFalse(thread.owed)
        self.assertIsNone(thread.owed_days(TODAY))

    def test_a_logged_call_discharges_it_too(self):
        """The whole reason log-a-call exists. Settle a matter on the telephone
        and the board must stop saying a reply is owed — otherwise the number
        everything is sorted by is measuring the mailbox rather than the
        correspondence."""
        tid = self.thread()
        self.touch(tid, "in", 4)
        touches.log_call(self.con, tid, summary="Rang Lyle; settled",
                         occurred_at=at(1))
        self.assertFalse(tracking.get_thread(self.con, tid).owed)

    def test_a_note_to_yourself_does_not_discharge_it(self):
        """A note is direction `note` for exactly this reason: it came from
        nobody and went to nobody, and counting it as outbound would let a
        thread be marked answered by thinking about it."""
        tid = self.thread()
        self.touch(tid, "in", 4)
        touches.add_note(self.con, tid, "Must reply to this",
                         occurred_at=at(1))
        thread = tracking.get_thread(self.con, tid)
        self.assertTrue(thread.owed)
        self.assertEqual(thread.owed_days(TODAY), 4)

    def test_a_meeting_does_not_discharge_it_either(self):
        # Two people met; neither now owes the other a reply because of it.
        tid = self.thread()
        self.touch(tid, "in", 4)
        self.touch(tid, "note", 1, channel=touches.CHANNEL_MEETING)
        self.assertTrue(tracking.get_thread(self.con, tid).owed)

    def test_a_thread_nobody_has_written_in_to_is_not_owed(self):
        # Nothing to answer. The first test is `last_in` rather than a
        # comparison, which against two empty strings would be true.
        tid = self.thread()
        self.touch(tid, "out", 3)
        self.assertFalse(tracking.get_thread(self.con, tid).owed)

    def test_the_owed_query_returns_only_live_threads(self):
        owed = self.thread("Owed one")
        self.touch(owed, "in", 2)
        closed = self.thread("Closed one")
        self.touch(closed, "in", 2)
        tracking.set_state(self.con, closed, tracking.STATE_CLOSED)
        self.assertEqual([t.id for t in tracking.owed(self.con)], [owed])


class TestNudgesAndDeadlines(Fixture):
    """A deadline is not a nudge, and they must never be folded into one."""

    def test_the_cadence_makes_a_due_date_without_one_being_typed(self):
        tid = self.thread(cadence_days=7)
        self.touch(tid, "out", 9)
        thread = tracking.get_thread(self.con, tid)
        self.assertEqual(thread.effective_due(),
                         TODAY - dt.timedelta(days=2))
        self.assertTrue(thread.overdue(TODAY))

    def test_an_explicit_due_date_beats_the_cadence(self):
        tid = self.thread(cadence_days=7,
                          due_date=(TODAY + dt.timedelta(days=5)).isoformat())
        self.touch(tid, "out", 9)
        thread = tracking.get_thread(self.con, tid)
        self.assertEqual(thread.effective_due(), TODAY + dt.timedelta(days=5))
        self.assertFalse(thread.overdue(TODAY))

    def test_a_thread_with_no_touches_at_all_is_not_overdue(self):
        # There is nothing to be silent since. A cadence applied to a thread
        # made this morning would report it overdue before it began.
        tid = self.thread(cadence_days=7)
        thread = tracking.get_thread(self.con, tid)
        self.assertIsNone(thread.effective_due())
        self.assertFalse(thread.overdue(TODAY))

    def test_a_closed_or_blocked_thread_is_never_nudged(self):
        for state in (tracking.STATE_CLOSED, tracking.STATE_DEAD,
                      tracking.STATE_BLOCKED):
            tid = self.thread(f"Thread {state}", cadence_days=1)
            self.touch(tid, "out", 30)
            tracking.set_state(self.con, tid, state)
            self.assertFalse(tracking.get_thread(self.con, tid).overdue(TODAY),
                             state)

    def test_a_deadline_is_NOT_silenced_by_being_blocked(self):
        """The one that matters. A statutory date on a blocked thread is
        exactly the one that must still shout — being blocked is why it is
        about to be missed."""
        tid = self.thread(deadline_date=(TODAY + dt.timedelta(days=3)).isoformat())
        tracking.set_state(self.con, tid, tracking.STATE_BLOCKED)
        thread = tracking.get_thread(self.con, tid)
        self.assertFalse(thread.overdue(TODAY))
        self.assertEqual(thread.days_to_deadline(TODAY), 3)

    def test_a_deadline_that_has_passed_reports_a_negative_number(self):
        # And is still listed: a missed statutory date is the most important
        # row on the board and the easiest to filter out by accident.
        tid = self.thread(deadline_date=(TODAY - dt.timedelta(days=2)).isoformat())
        self.assertEqual(
            tracking.get_thread(self.con, tid).days_to_deadline(TODAY), -2)
        self.assertEqual(
            [t.id for t in tracking.deadlines(self.con, today=TODAY)], [tid])

    def test_the_board_puts_deadlines_before_everything_else(self):
        soon = self.thread("Nudge tomorrow",
                           due_date=(TODAY + dt.timedelta(days=1)).isoformat())
        hard = self.thread("Files in a month",
                           deadline_date=(TODAY + dt.timedelta(days=30)).isoformat())
        order = [t.id for t in tracking.list_threads(self.con, order="due")]
        self.assertEqual(order[0], hard, "a hard date outranks a soft one")
        self.assertIn(soon, order)

    def test_a_thread_with_no_dates_sorts_last_and_not_first(self):
        nothing = self.thread("No dates at all")
        dated = self.thread("Due next week",
                            due_date=(TODAY + dt.timedelta(days=7)).isoformat())
        order = [t.id for t in tracking.list_threads(self.con, order="due")]
        self.assertEqual(order, [dated, nothing])


class TestSilence(Fixture):
    def test_silence_is_counted_in_local_days(self):
        """Counted in days a person lived through. At UTC+05:30 a message that
        arrived at 03:00 local arrived yesterday by UTC's reckoning, and every
        late-evening message would be off by one."""
        tid = self.thread()
        self.touch(tid, "in", 3, hour=1)
        self.assertEqual(tracking.get_thread(self.con, tid).silent_days(TODAY), 3)

    def test_a_thread_with_no_touches_has_no_silence_rather_than_zero(self):
        # None and not 0: "nothing has ever happened" and "something happened
        # today" are different answers and the board draws them differently.
        tid = self.thread()
        self.assertIsNone(tracking.get_thread(self.con, tid).silent_days(TODAY))


class TestTimeline(Fixture):
    def test_filing_the_same_message_twice_is_a_no_op(self):
        # The matchers re-examine mail they have already seen on every sync.
        tid = self.thread()
        first = touches.add_touch(self.con, tid, channel="email", direction="in",
                                  occurred_at=at(1), ext_id="<a@x>")
        second = touches.add_touch(self.con, tid, channel="email", direction="in",
                                   occurred_at=at(1), ext_id="<a@x>")
        self.assertTrue(first)
        self.assertEqual(second, 0)
        self.assertEqual(len(touches.timeline(self.con, tid)), 1)

    def test_two_calls_on_one_day_are_two_calls(self):
        # A touch with no ext_id may be added twice, and must be.
        tid = self.thread()
        touches.log_call(self.con, tid, summary="Rang, no answer",
                         occurred_at=at(1))
        touches.log_call(self.con, tid, summary="Rang again, spoke",
                         occurred_at=at(1))
        self.assertEqual(len(touches.timeline(self.con, tid)), 2)

    def test_the_same_message_may_be_on_two_threads(self):
        """An email that answers one question and raises another. The UNIQUE is
        on (thread_id, ext_id) and not on ext_id alone, deliberately."""
        first, second = self.thread("One"), self.thread("Two")
        for tid in (first, second):
            touches.add_touch(self.con, tid, channel="email", direction="in",
                              occurred_at=at(1), ext_id="<a@x>")
        self.assertEqual(len(touches.timeline(self.con, first)), 1)
        self.assertEqual(len(touches.timeline(self.con, second)), 1)

    def test_the_timeline_is_oldest_first(self):
        tid = self.thread()
        self.touch(tid, "in", 1)
        self.touch(tid, "out", 5)
        self.assertEqual([t.subject for t in touches.timeline(self.con, tid)],
                         ["out 5", "in 1"])

    def test_a_direction_that_is_not_one_of_the_three_raises(self):
        tid = self.thread()
        with self.assertRaises(ValueError):
            touches.add_touch(self.con, tid, channel="email",
                              direction="sideways", occurred_at=at(1))


class TestMerging(Fixture):
    def test_merging_moves_the_timeline_and_keeps_the_note(self):
        keep, drop = self.thread("Keep"), self.thread("Drop")
        tracking.update_thread(self.con, drop, note="Rang the switchboard twice")
        self.touch(keep, "out", 5)
        self.touch(drop, "in", 2)
        moved = tracking.merge_threads(self.con, keep, drop)
        self.assertEqual(moved, 1)
        self.assertIsNone(tracking.get_thread(self.con, drop))
        kept = tracking.get_thread(self.con, keep)
        self.assertEqual(kept.touches, 2)
        self.assertIn("switchboard", kept.note)

    def test_a_message_both_threads_had_filed_does_not_break_the_merge(self):
        """UNIQUE(thread_id, ext_id) is what makes filing idempotent, and a
        merge is exactly where two copies of one Message-ID meet. The kept
        thread's copy stays and the other is dropped rather than the whole
        merge failing."""
        keep, drop = self.thread("Keep"), self.thread("Drop")
        for tid in (keep, drop):
            touches.add_touch(self.con, tid, channel="email", direction="in",
                              occurred_at=at(2), ext_id="<same@x>")
        self.touch(drop, "out", 1, ext_id="<other@x>")
        tracking.merge_threads(self.con, keep, drop)
        kept = touches.timeline(self.con, keep)
        self.assertEqual(sorted(t.ext_id for t in kept),
                         ["<other@x>", "<same@x>"])

    def test_merging_a_thread_into_itself_does_nothing(self):
        tid = self.thread()
        self.touch(tid, "in", 1)
        self.assertEqual(tracking.merge_threads(self.con, tid, tid), 0)
        self.assertIsNotNone(tracking.get_thread(self.con, tid))


class TestContacts(Fixture):
    def test_an_address_belongs_to_exactly_one_person(self):
        first = contacts.add_contact(self.con, "Lyle Gordon")
        second = contacts.add_contact(self.con, "L. Gordon")
        contacts.add_handle(self.con, first, "email", "lyle@covalent.example")
        # Typing it onto a second card is saying the first was wrong.
        contacts.add_handle(self.con, second, "email", "lyle@covalent.example")
        found = contacts.contact_for_address(self.con, "lyle@covalent.example")
        self.assertEqual(found.id, second)
        self.assertEqual(contacts.get_contact(self.con, first).handles, ())

    def test_a_contact_is_not_invented_from_an_address_unless_asked(self):
        # A mailbox holds thousands of addresses that are nobody.
        self.assertIsNone(
            contacts.contact_for_address(self.con, "no-reply@service.example"))
        made = contacts.contact_for_address(self.con, "real@person.example",
                                            name="A Person", create=True)
        self.assertEqual(made.name, "A Person")

    def test_the_address_to_write_to_skips_one_that_bounced(self):
        cid = contacts.add_contact(self.con, "Lyle Gordon")
        contacts.add_handle(self.con, cid, "email", "old@covalent.example")
        contacts.add_handle(self.con, cid, "email", "new@covalent.example")
        contacts.note_bounce(self.con, "old@covalent.example", "550 unknown")
        self.assertEqual(contacts.get_contact(self.con, cid).address,
                         "new@covalent.example")

    def test_merging_two_cards_fills_the_gaps_rather_than_overwriting(self):
        keep = contacts.add_contact(self.con, "Lyle Gordon")
        drop = contacts.add_contact(self.con, "", org="Covalent Example")
        contacts.add_handle(self.con, drop, "email", "lyle@covalent.example")
        contacts.merge_contacts(self.con, keep, drop)
        merged = contacts.get_contact(self.con, keep)
        self.assertEqual(merged.name, "Lyle Gordon")
        self.assertEqual(merged.org, "Covalent Example")
        self.assertEqual(merged.address, "lyle@covalent.example")

    def test_a_thread_is_found_from_any_of_a_persons_addresses(self):
        """The reading pane's strip asks by address, and a person writing from
        their phone for the first time must still find their own thread."""
        tid = self.thread()
        cid = contacts.add_contact(self.con, "Lyle Gordon")
        contacts.add_handle(self.con, cid, "email", "lyle@covalent.example")
        contacts.add_handle(self.con, cid, "email", "lyle@personal.example")
        tracking.link_contact(self.con, tid, cid)
        for address in ("lyle@covalent.example", "LYLE@personal.example"):
            self.assertEqual(
                [t.id for t in tracking.threads_for_address(self.con, address)],
                [tid], address)


if __name__ == "__main__":
    unittest.main()
