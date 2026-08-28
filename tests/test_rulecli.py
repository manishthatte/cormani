# SPDX-License-Identifier: GPL-3.0-or-later
#
# The filter half of the command line.
#
# THE FIXTURE IS test_cli's, IMPORTED, for the reason `test_calcli.py` gives:
# it redirects all four XDG variables and the keyring into a temporary
# directory. Nothing here syncs, but every address is at .invalid anyway —
# belt and braces, and RFC 2606 reserves the domain.
#
# THAT `--filters` WRITES NOTHING IS ASSERTED BY THE MECHANISM RATHER THAN BY
# AN ASSERTION. It opens the store READ-ONLY, so a reading path that wrote
# would raise `attempt to write a readonly database` and every test below would
# fail at once. `store/triage.py` had exactly that defect and `--check` is what
# found it; the same guard covers this file for free.
#
# WHAT IS ASSERTED IS THE WORDS. A read-out is the only evidence a filter ever
# offers — a rule that works and a rule that never fires leave a mailbox
# looking the same — so the sentences it prints ARE the feature, and a test
# that only checked the exit code would be testing that a report exists rather
# than that it says anything.
#
# © Manish Jagdish Thatte
import unittest

from test_cli import Fixture

from cormani.store import rulematch
from cormani.store.folders import ROLE_ARCHIVE


class RuleFixture(Fixture):
    def account(self, address="work@manitlab.invalid", provider="google"):
        from cormani.store.accounts import add_account
        con = self.store()
        return con, add_account(con, address, provider, display_name="Work")

    def rule(self, con, name="invoices", conditions=None, actions=None, **kw):
        from cormani.store import rules
        return rules.save_rule(con, rulematch.Rule(
            name=name,
            conditions=conditions or (
                rulematch.Condition("subject", "contains", "invoice"),),
            actions=actions or (
                rulematch.Action(kind="move", value=ROLE_ARCHIVE),),
            **kw))


class TestFiltersCommand(RuleFixture):
    def test_with_no_store_it_says_so_and_fails(self):
        code, text = self.run_cli("--filters")
        self.assertEqual(code, 1)
        self.assertIn("no store yet", text)

    def test_no_rules_is_not_a_failure_and_says_what_that_means(self):
        # A person running this has a suspicion about their mail. "No rules"
        # has to answer it, not merely be true.
        self.store()
        code, text = self.run_cli("--filters")
        self.assertEqual(code, 0)
        self.assertIn("no filter rules", text)
        self.assertIn("moved, tagged or marked", text)

    def test_a_rule_is_printed_with_its_conditions_and_its_actions(self):
        con, _ = self.account()
        self.rule(con)
        code, text = self.run_cli("--filters")
        self.assertEqual(code, 0)
        self.assertIn("1. invoices", text)
        self.assertIn("every account", text)
        self.assertIn("Subject contains", text)
        self.assertIn("invoice", text)
        self.assertIn("move to each account's Archive", text)

    def test_a_move_to_one_folder_names_the_folder_and_not_a_number(self):
        from cormani.store.folders import ensure_folder
        con, account_id = self.account()
        folder = ensure_folder(con, account_id, "Lists/Tenders")
        self.rule(con, actions=(rulematch.Action(kind="move", folder_id=folder),))
        _, text = self.run_cli("--filters")
        self.assertIn("move to Lists/Tenders", text)
        self.assertNotIn(f"folder {folder}", text)

    def test_a_role_move_and_a_folder_move_do_not_read_alike(self):
        # They are different rules — one names a folder in one account, the
        # other resolves per account when it fires — and a list of fifteen is
        # unreadable if the words do not say which.
        from cormani.store.folders import ensure_folder
        con, account_id = self.account()
        folder = ensure_folder(con, account_id, "Archive", role=ROLE_ARCHIVE)
        self.rule(con, name="by role",
                  actions=(rulematch.Action(kind="move", value=ROLE_ARCHIVE),))
        self.rule(con, name="by folder",
                  actions=(rulematch.Action(kind="move", folder_id=folder),))
        _, text = self.run_cli("--filters")
        self.assertIn("move to each account's Archive", text)
        self.assertIn("move to Archive\n", text)

    def test_a_tag_action_names_the_tag(self):
        from cormani.store import tags
        con, _ = self.account()
        tag = tags.list_tags(con)[0]
        self.rule(con, actions=(rulematch.Action(kind="tag", tag_id=tag.id),))
        _, text = self.run_cli("--filters")
        self.assertIn(f"tag {tag.name}", text)

    def test_a_switched_off_rule_says_so(self):
        con, _ = self.account()
        self.rule(con, enabled=False)
        _, text = self.run_cli("--filters")
        self.assertIn("SWITCHED OFF", text)

    def test_a_half_written_rule_is_listed_with_the_half_that_is_missing(self):
        # It is kept and never runs, and the read-out is the only place that
        # distinction is visible.
        from cormani.store import rules
        con, _ = self.account()
        rules.save_rule(con, rulematch.Rule(
            name="unfinished",
            conditions=(rulematch.Condition("subject", "contains", "x"),),
            actions=()))
        _, text = self.run_cli("--filters")
        self.assertIn("HALF-WRITTEN", text)
        self.assertIn("so it would do nothing", text)
        # And the mark and the explanation say it ONCE each, not twice: the
        # mark is what a person scanning the headings sees, the line under it
        # is which half is missing.
        self.assertEqual(text.count("so it would do nothing"), 1)

    def test_stopping_the_run_is_named_and_explained_at_the_foot(self):
        con, _ = self.account()
        self.rule(con, stop_after=True)
        _, text = self.run_cli("--filters")
        self.assertIn("stops the run", text)
        self.assertIn("never see it", text)

    def test_the_numbers_are_the_order_the_rules_run_in(self):
        from cormani.store import rules
        con, _ = self.account()
        first = self.rule(con, name="alpha")
        second = self.rule(con, name="beta")
        rules.reorder(con, [second.id, first.id])
        _, text = self.run_cli("--filters")
        self.assertLess(text.index("1. beta"), text.index("2. alpha"))

    def test_the_match_count_is_printed_and_starts_at_nothing(self):
        from cormani.store import rules
        con, _ = self.account()
        saved = self.rule(con)
        _, text = self.run_cli("--filters")
        self.assertIn("matched nothing yet", text)
        rules.note_match(con, [saved.id, saved.id, saved.id])
        _, text = self.run_cli("--filters")
        self.assertIn("matched 3 times", text)
        self.assertIn("last on", text)


class TestNarrowingToOneAccount(RuleFixture):
    def test_an_unknown_address_is_refused(self):
        self.account()
        code, text = self.run_cli("--filters", "nobody@nowhere.invalid")
        self.assertEqual(code, 1)
        self.assertIn("not configured", text)

    def test_a_rule_for_another_account_is_left_out(self):
        from cormani.store.accounts import add_account
        con, first = self.account()
        second = add_account(con, "other@manitlab.invalid", "imap")
        self.rule(con, name="mine", account_id=first)
        self.rule(con, name="theirs", account_id=second)
        _, text = self.run_cli("--filters", "work@manitlab.invalid")
        self.assertIn("mine", text)
        self.assertNotIn("theirs", text)

    def test_a_rule_tied_to_no_account_runs_against_every_one_of_them(self):
        # The answer people forget when they ask why a rule fired, so the
        # narrowed view has to include it rather than only the tied ones.
        con, _ = self.account()
        self.rule(con, name="everywhere")
        _, text = self.run_cli("--filters", "work@manitlab.invalid")
        self.assertIn("everywhere", text)
        self.assertIn("every account", text)

    def test_an_account_with_no_rules_of_its_own_is_told_so(self):
        from cormani.store.accounts import add_account
        con, first = self.account()
        add_account(con, "other@manitlab.invalid", "imap")
        self.rule(con, name="mine", account_id=first)
        code, text = self.run_cli("--filters", "other@manitlab.invalid")
        self.assertEqual(code, 0)
        self.assertIn("no filter rules run against", text)

    def test_a_narrowed_list_keeps_the_global_numbers_and_says_why(self):
        # 1, 3, 4 with no explanation reads as a missing rule. The numbers
        # cannot be renumbered: the order is the one thing about a set of
        # rules that cannot be worked out by reading them.
        from cormani.store.accounts import add_account
        con, first = self.account()
        second = add_account(con, "other@manitlab.invalid", "imap")
        self.rule(con, name="first here")
        self.rule(con, name="theirs", account_id=second)
        self.rule(con, name="third here")
        _, text = self.run_cli("--filters", "work@manitlab.invalid")
        self.assertIn("1. first here", text)
        self.assertIn("3. third here", text)
        self.assertNotIn("2.", text)
        self.assertIn("the gap is the one rule that does not run", text)


class TestCheckReportsThem(RuleFixture):
    def test_check_is_silent_when_there_are_no_rules(self):
        # `--check` is one line per subsystem and a subsystem nobody uses is
        # not a line worth spending.
        self.account()
        _, text = self.run_cli("--check")
        self.assertNotIn("filters", text)

    def test_check_counts_the_rules_and_the_ones_switched_off(self):
        con, _ = self.account()
        self.rule(con, name="on")
        self.rule(con, name="off", enabled=False)
        _, text = self.run_cli("--check")
        self.assertIn("2 rule(s)", text)
        self.assertIn("1 switched off", text)

    def test_check_names_the_rules_that_have_never_matched_anything(self):
        # The whole reason `filter_rule` keeps two counters instead of an
        # audit log — see store/rulesschema.py.
        con, _ = self.account()
        self.rule(con)
        _, text = self.run_cli("--check")
        self.assertIn("never matched anything", text)

    def test_a_rule_that_has_matched_is_not_reported_as_idle(self):
        from cormani.store import rules
        con, _ = self.account()
        saved = self.rule(con)
        rules.note_match(con, [saved.id])
        _, text = self.run_cli("--check")
        self.assertNotIn("never matched", text)

    def test_check_counts_the_half_written_ones_separately(self):
        from cormani.store import rules
        con, _ = self.account()
        rules.save_rule(con, rulematch.Rule(name="unfinished", actions=(
            rulematch.Action(kind="flag"),)))
        _, text = self.run_cli("--check")
        self.assertIn("half-written", text)


if __name__ == "__main__":
    unittest.main()
