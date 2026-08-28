# SPDX-License-Identifier: GPL-3.0-or-later
#
# Filters: writing a rule down, and what a match then does.
#
# The half that needs the store. `tests/test_rulematch.py` is the other half —
# whether a rule matches — and needs no database at all.
#
# WHAT THESE ASSERT IS NOT "THE FUNCTION RETURNED". It is WHERE THE MESSAGE
# ENDED UP and WHAT THE QUEUE WAS TOLD, because a filter that files a message
# locally and tells the server nothing is the exact failure this design exists
# to prevent: the next sync finds the message still in the Inbox, ingests it,
# and files it again, for ever.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import pending, rulematch, rulerun, rules, tags, tracking
from cormani.store.accounts import add_account
from cormani.store.folders import ensure_folder, ROLE_ARCHIVE, ROLE_INBOX, ROLE_TRASH


class RuleStoreCase(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "one@example.invalid", "imap")
        self.inbox = ensure_folder(self.con, self.account, "INBOX", role=ROLE_INBOX)
        self.archive = ensure_folder(self.con, self.account, "Archive",
                                     role=ROLE_ARCHIVE)
        self.trash = ensure_folder(self.con, self.account, "Trash", role=ROLE_TRASH)

    def message(self, *, sender="frances@covalent.example", subject="Inner tube",
                folder=None, body="Numbers attached.", bulk=0,
                date_at="2026-08-25T10:00:00+00:00") -> int:
        cur = self.con.execute(
            "INSERT INTO message (folder_id, uid, message_id, subject, "
            "from_addr, from_name, to_addrs, body_text, date_at, received_at, "
            "is_bulk, size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (folder or self.inbox, self._uid(), f"<{self._uid()}@x.invalid>",
             subject, sender, "Frances Baker", "owner@manitlab.example", body,
             date_at, date_at, bulk, 4096))
        self.con.commit()
        return int(cur.lastrowid)

    _next_uid = 100

    def _uid(self) -> int:
        RuleStoreCase._next_uid += 1
        return RuleStoreCase._next_uid

    def folder_of(self, message_id: int) -> int:
        return int(self.con.execute(
            "SELECT folder_id FROM message WHERE id = ?", (message_id,)).fetchone()[0])


class TestSaving(RuleStoreCase):
    def test_a_rule_survives_a_round_trip_whole(self):
        saved = rules.save_rule(self.con, rulematch.Rule(
            name="Covalent", match_all=False, stop_after=True,
            conditions=(rulematch.Condition("from", "contains", "covalent"),
                        rulematch.Condition("subject", "contains", "tube")),
            actions=(rulematch.Action(kind="flag"),
                     rulematch.Action(kind="move", folder_id=self.archive))))
        back = rules.get_rule(self.con, saved.id)
        self.assertEqual(back.name, "Covalent")
        self.assertFalse(back.match_all)
        self.assertTrue(back.stop_after)
        self.assertEqual([c.field for c in back.conditions], ["from", "subject"])
        self.assertEqual([a.kind for a in back.actions], ["flag", "move"])
        self.assertEqual(back.actions[1].folder_id, self.archive)

    def test_saving_replaces_the_conditions_rather_than_adding_to_them(self):
        saved = rules.save_rule(self.con, rulematch.Rule(
            name="r", conditions=(rulematch.Condition("from", "contains", "a"),
                                  rulematch.Condition("from", "contains", "b")),
            actions=(rulematch.Action(kind="flag"),)))
        again = rules.save_rule(self.con, saved.with_changes(
            conditions=(rulematch.Condition("from", "contains", "c"),)))
        self.assertEqual(len(again.conditions), 1)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM filter_condition").fetchone()[0], 1)

    def test_an_invalid_rule_raises_rather_than_being_written(self):
        with self.assertRaises(ValueError):
            rules.save_rule(self.con, rulematch.Rule(
                name="bad",
                conditions=(rulematch.Condition("subject", "matches", "(["),),
                actions=(rulematch.Action(kind="flag"),)))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM filter_rule").fetchone()[0], 0)

    def test_deleting_a_rule_takes_its_conditions_with_it(self):
        saved = rules.save_rule(self.con, rulematch.Rule(
            name="r", conditions=(rulematch.Condition("from", "contains", "a"),),
            actions=(rulematch.Action(kind="flag"),)))
        rules.delete_rule(self.con, saved.id)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM filter_condition").fetchone()[0], 0)

    def test_the_order_rules_run_in_is_the_order_they_are_listed_in(self):
        a = rules.save_rule(self.con, rulematch.Rule(
            name="a", conditions=(rulematch.Condition("from", "contains", "a"),),
            actions=(rulematch.Action(kind="flag"),)))
        b = rules.save_rule(self.con, rulematch.Rule(
            name="b", conditions=(rulematch.Condition("from", "contains", "b"),),
            actions=(rulematch.Action(kind="flag"),)))
        rules.reorder(self.con, [b.id, a.id])
        self.assertEqual([r.name for r in rules.list_rules(self.con)], ["b", "a"])

    def test_a_deleted_folder_takes_the_action_with_it(self):
        saved = rules.save_rule(self.con, rulematch.Rule(
            name="r", conditions=(rulematch.Condition("from", "contains", "a"),),
            actions=(rulematch.Action(kind="move", folder_id=self.archive),)))
        self.con.execute("DELETE FROM folder WHERE id = ?", (self.archive,))
        self.con.commit()
        back = rules.get_rule(self.con, saved.id)
        self.assertEqual(back.actions, ())
        self.assertFalse(back.is_complete)


class TestRunning(RuleStoreCase):
    def save(self, *conditions, actions=(), **kw):
        return rules.save_rule(self.con, rulematch.Rule(
            name=kw.pop("name", "r"), conditions=tuple(conditions),
            actions=tuple(actions) or (rulematch.Action(kind="flag"),), **kw))

    def test_a_move_lands_the_message_and_tells_the_server(self):
        # The two halves that must both be true. A filter that moved the row
        # and queued nothing would put the message in Archive here and leave it
        # in the Inbox there — and the next sync would file it again, for ever.
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="move", folder_id=self.archive),))
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertEqual(report.matched, 1)
        self.assertEqual(self.folder_of(message_id), self.archive)
        op = pending.queued_move(self.con, message_id)
        self.assertIsNotNone(op, "the move was not queued for the server")

    def test_a_flag_is_queued_too(self):
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="flag"),))
        message_id = self.message()
        rulerun.run(self.con, [message_id])
        row = self.con.execute("SELECT flagged FROM message WHERE id = ?",
                               (message_id,)).fetchone()
        self.assertEqual(row["flagged"], 1)
        self.assertTrue(pending.pending_for(self.con, self.account))

    def test_a_tag_action_applies_the_tag(self):
        tag = tags.list_tags(self.con)[0]
        self.save(rulematch.Condition("subject", "contains", "tube"),
                  actions=(rulematch.Action(kind="tag", tag_id=tag.id),))
        message_id = self.message()
        rulerun.run(self.con, [message_id])
        self.assertIn(tag.id, [t.id for t in
                               tags.tags_for(self.con, [message_id])[message_id]])

    def test_deleting_a_tag_takes_the_action_with_it_rather_than_leaving_a_stub(self):
        # The FOREIGN KEY is what makes this safe, and the assertion is that it
        # is ON, not merely declared: the store enables foreign keys per
        # connection, and a database opened without that would leave a rule
        # pointing at a tag that is gone.
        tag = tags.list_tags(self.con)[0]
        saved = self.save(rulematch.Condition("subject", "contains", "tube"),
                          actions=(rulematch.Action(kind="tag", tag_id=tag.id),))
        tags.delete_tag(self.con, tag.id)
        back = rules.get_rule(self.con, saved.id)
        self.assertEqual(back.actions, ())
        self.assertFalse(back.is_complete)
        # And a rule reduced to nothing does not then run and do nothing
        # visible: it is skipped, and the report says it considered nothing.
        report = rulerun.run(self.con, [self.message()])
        self.assertEqual(report.considered, 0)

    def test_a_move_to_a_role_the_account_lacks_is_reported(self):
        self.con.execute("UPDATE folder SET role = '' WHERE id = ?", (self.trash,))
        self.con.commit()
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="delete"),))
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertEqual(self.folder_of(message_id), self.inbox)
        self.assertTrue(report.problems)
        self.assertIn("Trash", report.problems[0])

    def test_a_move_naming_another_accounts_folder_is_refused(self):
        other = add_account(self.con, "two@example.invalid", "imap")
        elsewhere = ensure_folder(self.con, other, "Archive", role=ROLE_ARCHIVE)
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="move", folder_id=elsewhere),))
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertEqual(self.folder_of(message_id), self.inbox)
        self.assertIn("another account", report.problems[0])

    def test_stop_after_stops_the_run(self):
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="move", folder_id=self.archive),),
                  name="first", stop_after=True, sort_order=1)
        second = self.save(rulematch.Condition("from", "contains", "covalent"),
                           actions=(rulematch.Action(kind="flag"),),
                           name="second", sort_order=2)
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertNotIn(second.id, report.outcome(message_id).fired)
        self.assertEqual(
            self.con.execute("SELECT flagged FROM message WHERE id = ?",
                             (message_id,)).fetchone()["flagged"], 0)

    def test_without_stop_after_both_rules_fire(self):
        first = self.save(rulematch.Condition("from", "contains", "covalent"),
                          actions=(rulematch.Action(kind="flag"),),
                          name="first", sort_order=1)
        second = self.save(rulematch.Condition("subject", "contains", "tube"),
                           actions=(rulematch.Action(kind="mark_read"),),
                           name="second", sort_order=2)
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertEqual(report.outcome(message_id).fired, [first.id, second.id])

    def test_a_second_move_in_one_run_is_not_performed(self):
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="move", folder_id=self.archive),),
                  name="first", sort_order=1)
        self.save(rulematch.Condition("subject", "contains", "tube"),
                  actions=(rulematch.Action(kind="delete"),),
                  name="second", sort_order=2)
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertEqual(self.folder_of(message_id), self.archive)
        self.assertTrue(any("already filed" in p for p in report.problems))

    def test_a_filed_message_is_quiet_and_an_untouched_one_is_not(self):
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="move", folder_id=self.archive),))
        filed = self.message()
        report = rulerun.run(self.con, [filed])
        self.assertIn(filed, report.quiet_ids())

    def test_silence_changes_nothing_but_the_report(self):
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="silence"),))
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertEqual(self.folder_of(message_id), self.inbox)
        self.assertIn(message_id, report.quiet_ids())

    def test_marking_read_silences_it_too(self):
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="mark_read"),))
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertIn(message_id, report.quiet_ids())

    def test_a_rule_counts_its_own_matches(self):
        saved = self.save(rulematch.Condition("from", "contains", "covalent"))
        rulerun.run(self.con, [self.message(), self.message()])
        back = rules.get_rule(self.con, saved.id)
        self.assertEqual(back.match_count, 2)
        self.assertTrue(back.last_matched_at)

    def test_a_message_matching_nothing_is_left_entirely_alone(self):
        self.save(rulematch.Condition("from", "contains", "nobody"))
        message_id = self.message()
        report = rulerun.run(self.con, [message_id])
        self.assertEqual(report.matched, 0)
        self.assertEqual(report.considered, 1)
        self.assertIsNone(report.outcome(message_id))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM pending_op").fetchone()[0], 0)

    def test_the_known_sender_field_reads_the_address_book(self):
        from cormani.store import contacts
        contact = contacts.add_contact(self.con, "Frances Baker")
        contacts.add_handle(self.con, contact, "email", "frances@covalent.example")
        self.save(rulematch.Condition("known", "is_true", ""))
        known = self.message(sender="frances@covalent.example")
        stranger = self.message(sender="nobody@example.invalid")
        report = rulerun.run(self.con, [known, stranger])
        self.assertEqual(report.matched, 1)
        self.assertIsNotNone(report.outcome(known))

    def test_track_opens_a_thread_and_files_the_message_on_it(self):
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="track", value="Covalent tubing"),))
        message_id = self.message()
        rulerun.run(self.con, [message_id])
        thread = tracking.by_slug(self.con, tracking.slugify("Covalent tubing"))
        self.assertIsNotNone(thread)
        touched = self.con.execute(
            "SELECT COUNT(*) FROM touch WHERE thread_id = ? AND message_id = ?",
            (thread.id, message_id)).fetchone()[0]
        self.assertEqual(touched, 1)

    def test_a_tracked_thread_begins_when_the_mail_did_and_not_today(self):
        # `tracking.create_thread` documents `created_at` as load-bearing: the
        # address matcher bounds itself by it, so a thread dated today files
        # none of the mail that led to it.
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="track", value="Old business"),))
        rulerun.run(self.con, [self.message(date_at="2024-01-05T09:00:00+00:00")])
        thread = tracking.by_slug(self.con, tracking.slugify("Old business"))
        self.assertTrue(thread.created_at.startswith("2024-01-05"))

    def test_a_second_message_joins_the_thread_the_first_opened(self):
        self.save(rulematch.Condition("from", "contains", "covalent"),
                  actions=(rulematch.Action(kind="track", value="Covalent tubing"),))
        rulerun.run(self.con, [self.message()])
        rulerun.run(self.con, [self.message(subject="A separate matter")])
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM thread").fetchone()[0], 1)


class TestPreview(RuleStoreCase):
    def test_a_preview_finds_matching_mail_and_changes_nothing(self):
        keep = self.message(sender="frances@covalent.example")
        other = self.message(sender="someone@elsewhere.invalid")
        r = rulematch.Rule(name="draft",
                       conditions=(rulematch.Condition("from", "contains", "covalent"),),
                       actions=(rulematch.Action(kind="move", folder_id=self.archive),))
        hits = rulerun.preview(self.con, r)
        self.assertEqual(hits, [keep])
        self.assertEqual(self.folder_of(keep), self.inbox)
        self.assertEqual(self.folder_of(other), self.inbox)

    def test_a_preview_works_on_a_rule_that_is_not_saved_or_enabled(self):
        message_id = self.message()
        r = rulematch.Rule(name="unsaved", enabled=False,
                       conditions=(rulematch.Condition("from", "contains", "covalent"),))
        self.assertEqual(rulerun.preview(self.con, r), [message_id])

    def test_a_preview_of_an_empty_rule_finds_nothing(self):
        self.message()
        self.assertEqual(rulerun.preview(self.con, rulematch.Rule(name="x")), [])


class TestRunOverAFolder(RuleStoreCase):
    def test_running_again_over_a_folder_files_what_is_already_here(self):
        # The second way filters are used, and the reason the matcher reads a
        # stored row rather than the wire: this path has nothing else.
        first = self.message()
        second = self.message(sender="nobody@example.invalid")
        rules.save_rule(self.con, rulematch.Rule(
            name="late", conditions=(rulematch.Condition("from", "contains", "covalent"),),
            actions=(rulematch.Action(kind="move", folder_id=self.archive),)))
        report = rulerun.run_over_folder(self.con, self.inbox)
        self.assertEqual(report.matched, 1)
        self.assertEqual(self.folder_of(first), self.archive)
        self.assertEqual(self.folder_of(second), self.inbox)


class TestCounts(RuleStoreCase):
    def test_counts_tell_the_incomplete_from_the_idle(self):
        rules.save_rule(self.con, rulematch.Rule(
            name="works", conditions=(rulematch.Condition("from", "contains", "a"),),
            actions=(rulematch.Action(kind="flag"),)))
        rules.save_rule(self.con, rulematch.Rule(name="half"))
        got = rules.counts(self.con)
        self.assertEqual(got["rules"], 2)
        self.assertEqual(got["incomplete"], 1)
        self.assertEqual(got["never_matched"], 1)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
