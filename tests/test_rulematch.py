# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a filter rule matches, asked with no database at all.
#
# `store/rulematch.py` is pure by construction, so this file builds a
# `Candidate` by hand and asks one question of it. Its companion,
# `tests/test_rules.py`, needs the store: the split here fell exactly where the
# module split did, which is the answer whenever it is available.
#
# THE LAST TEST IN THIS FILE IS ABOUT THE SPLIT ITSELF. A module that claims to
# be pure and is not is a claim nobody can rely on, and the way that claim
# rotted the first time was one function at a time.
#
# © Manish Jagdish Thatte
import ast
import pathlib
import unittest

from cormani.store import rulematch as rules
from cormani.store.folders import ROLE_INBOX


def candidate(**kw) -> rules.Candidate:
    base = dict(message_id=1, account_id=1, folder_role=ROLE_INBOX,
                from_name="Frances Baker", from_addr="frances@covalent.example",
                to_addrs="owner@manitlab.example", subject="RE: M2016L3 inner tube",
                body="Our lab confirms the wavelength.")
    base.update(kw)
    return rules.Candidate(**base)


def rule(*conditions, **kw) -> rules.Rule:
    base = dict(id=1, name="a rule", conditions=tuple(conditions),
                actions=(rules.Action(kind="flag"),))
    base.update(kw)
    return rules.Rule(**base)


class TestConditions(unittest.TestCase):
    """One condition against one message. No store, no Qt."""

    def test_from_matches_the_name_as_well_as_the_address(self):
        # Both, and the test says so in both directions: a person writes "from
        # Frances" as readily as the address, and matching one of the two makes
        # half the rules anybody writes silently never fire.
        for needle in ("frances@covalent", "Frances Baker", "frances"):
            self.assertTrue(
                rules.test(rules.Condition("from", "contains", needle),
                           candidate()), needle)

    def test_from_does_not_match_someone_else(self):
        self.assertFalse(
            rules.test(rules.Condition("from", "contains", "lyle@"), candidate()))

    def test_contains_is_case_insensitive_both_ways(self):
        self.assertTrue(rules.test(
            rules.Condition("subject", "contains", "INNER TUBE"), candidate()))
        self.assertTrue(rules.test(
            rules.Condition("subject", "contains", "inner tube"),
            candidate(subject="RE: M2016L3 INNER TUBE")))

    def test_excludes_is_the_opposite_of_contains_on_the_same_pair(self):
        # Both orderings of the pair, which is the rule P44 taught: a probe
        # that only asks the true case cannot tell a working comparison from
        # one that always says yes.
        yes = rules.Condition("subject", "excludes", "wavelength")
        no = rules.Condition("subject", "excludes", "M2016L3")
        self.assertTrue(rules.test(yes, candidate()))
        self.assertFalse(rules.test(no, candidate()))

    def test_is_wants_the_whole_field_and_contains_does_not(self):
        whole = rules.Condition("from", "is", "Frances Baker frances@covalent.example")
        part = rules.Condition("from", "is", "frances")
        self.assertTrue(rules.test(whole, candidate()))
        self.assertFalse(rules.test(part, candidate()))

    def test_starts_and_ends(self):
        self.assertTrue(rules.test(
            rules.Condition("subject", "starts", "re:"), candidate()))
        self.assertTrue(rules.test(
            rules.Condition("subject", "ends", "tube"), candidate()))
        self.assertFalse(rules.test(
            rules.Condition("subject", "ends", "re:"), candidate()))

    def test_recipient_is_to_and_cc_together(self):
        c = candidate(to_addrs="a@example.org", cc_addrs="b@example.org")
        self.assertTrue(rules.test(
            rules.Condition("recipient", "contains", "b@example.org"), c))
        self.assertFalse(rules.test(
            rules.Condition("to", "contains", "b@example.org"), c))

    def test_a_pattern_is_applied_to_the_body(self):
        c = candidate(body="Invoice INV-2026-0041 is attached.")
        self.assertTrue(rules.test(
            rules.Condition("body", "matches", r"INV-\d{4}-\d{4}"), c))
        self.assertFalse(rules.test(
            rules.Condition("body", "matches", r"PO-\d{4}"), c))

    def test_a_pattern_that_cannot_compile_is_false_and_not_a_crash(self):
        self.assertFalse(rules.test(
            rules.Condition("subject", "matches", "([unclosed"), candidate()))

    def test_the_body_a_pattern_sees_is_capped(self):
        # The guard from the header, asserted as behaviour rather than trusted:
        # a match beyond the cap is not found, which is what bounds a bad
        # pattern's running time.
        far = "x" * 9000 + "NEEDLE"
        self.assertFalse(rules.test(
            rules.Condition("body", "matches", "NEEDLE"), candidate(body=far)))
        near = "x" * 100 + "NEEDLE"
        self.assertTrue(rules.test(
            rules.Condition("body", "matches", "NEEDLE"), candidate(body=near)))

    def test_the_boolean_fields_answer_both_ways(self):
        with_attachment = candidate(has_attachment=True)
        without = candidate(has_attachment=False)
        yes = rules.Condition("attachment", "is_true", "")
        no = rules.Condition("attachment", "is_false", "")
        self.assertTrue(rules.test(yes, with_attachment))
        self.assertFalse(rules.test(yes, without))
        self.assertTrue(rules.test(no, without))
        self.assertFalse(rules.test(no, with_attachment))

    def test_size_is_in_kilobytes(self):
        c = candidate(size_bytes=2048)
        self.assertTrue(rules.test(rules.Condition("size", "gt", "1"), c))
        self.assertFalse(rules.test(rules.Condition("size", "gt", "3"), c))
        self.assertTrue(rules.test(rules.Condition("size", "lt", "3"), c))

    def test_a_number_field_given_a_word_is_false_rather_than_a_crash(self):
        self.assertFalse(rules.test(
            rules.Condition("size", "gt", "large"), candidate(size_bytes=99999)))


class TestMatching(unittest.TestCase):
    def test_all_means_all_and_any_means_any(self):
        hit = rules.Condition("from", "contains", "frances")
        miss = rules.Condition("subject", "contains", "invoice")
        self.assertFalse(rules.matches(rule(hit, miss, match_all=True), candidate()))
        self.assertTrue(rules.matches(rule(hit, miss, match_all=False), candidate()))

    def test_a_rule_with_no_conditions_matches_nothing(self):
        # `all([])` is True. If this ever regresses, an empty rule archives
        # fifteen accounts — see the module header in store/rules.py.
        empty = rules.Rule(id=1, name="empty",
                           actions=(rules.Action(kind="flag"),))
        self.assertFalse(rules.matches(empty, candidate()))
        self.assertFalse(empty.is_complete)
        self.assertIn("match nothing", empty.incomplete_reason)

    def test_a_rule_with_no_actions_never_runs(self):
        r = rules.Rule(id=1, name="half",
                       conditions=(rules.Condition("from", "contains", "frances"),))
        self.assertFalse(rules.matches(r, candidate()))
        self.assertIn("do nothing", r.incomplete_reason)

    def test_a_disabled_rule_does_not_match(self):
        r = rule(rules.Condition("from", "contains", "frances"), enabled=False)
        self.assertFalse(rules.matches(r, candidate()))

    def test_an_account_scoped_rule_ignores_other_accounts(self):
        r = rule(rules.Condition("from", "contains", "frances"), account_id=7)
        self.assertFalse(rules.matches(r, candidate(account_id=3)))
        self.assertTrue(rules.matches(r, candidate(account_id=7)))


class TestValidate(unittest.TestCase):
    def test_a_pattern_that_cannot_compile_is_refused_when_saved(self):
        r = rule(rules.Condition("subject", "matches", "([unclosed"))
        self.assertIn("will not compile", rules.validate(r))

    def test_an_impossible_pair_is_refused(self):
        r = rule(rules.Condition("attachment", "contains", "x"))
        self.assertTrue(rules.validate(r))

    def test_a_text_condition_with_no_value_is_refused(self):
        self.assertTrue(rules.validate(rule(rules.Condition("subject", "contains", " "))))

    def test_a_tag_action_with_no_tag_is_refused(self):
        r = rule(rules.Condition("from", "contains", "x"),
                 actions=(rules.Action(kind="tag"),))
        self.assertIn("needs a tag", rules.validate(r))

    def test_a_move_with_neither_folder_nor_role_is_refused(self):
        r = rule(rules.Condition("from", "contains", "x"),
                 actions=(rules.Action(kind="move"),))
        self.assertIn("somewhere to move to", rules.validate(r))

    def test_a_workable_rule_validates(self):
        self.assertEqual(rules.validate(
            rule(rules.Condition("from", "contains", "x"))), "")

    def test_ops_for_never_offers_an_impossible_comparison(self):
        for name in rules.FIELDS:
            for op in rules.ops_for(name):
                self.assertIn(op, rules.OPS, f"{name}/{op}")
            r = rule(rules.Condition(name, rules.ops_for(name)[0], "3"))
            self.assertEqual(rules.validate(r), "", name)


class TestTheSplitItself(unittest.TestCase):
    """`store/rulematch.py` is pure, and stays pure.

    Asserted mechanically for the same reason `tests/test_packaging.py` refuses
    a module-level Qt import outside `ui/`: the convention is worth nothing if
    the only thing holding it is that everybody remembered. The check is on the
    IMPORTS rather than on the behaviour because that is the boundary that
    actually moved — the file these two came out of grew its SQL one function
    at a time, each of which looked reasonable on its own.
    """

    def test_the_matcher_imports_no_database(self):
        source = pathlib.Path(__file__).resolve().parent.parent / \
            "cormani" / "store" / "rulematch.py"
        tree = ast.parse(source.read_text())
        imported = [a.name for n in ast.walk(tree)
                    if isinstance(n, ast.Import) for a in n.names]
        imported += [n.module or "" for n in ast.walk(tree)
                     if isinstance(n, ast.ImportFrom)]
        self.assertNotIn("sqlite3", imported)
        self.assertEqual([m for m in imported if m.startswith(".")], [],
                         "the pure half must not reach into the store")


if __name__ == "__main__":
    unittest.main()
