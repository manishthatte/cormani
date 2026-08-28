# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing a filter rule down, and the list of them.
#
# `tests/test_rules.py` is the store's half and `tests/test_rulematch.py` is
# whether a rule matches. This is the half a person actually touches, and what
# it asserts is the STORE — a dialog test that drove the widgets and then
# checked the widgets would prove that a form remembers what was typed into it.
#
# THE SECOND BOX IS THE POINT OF THE EDITOR. `rulematch.ops_for` says which
# comparisons a field offers, and the interface is built from it rather than
# checked against it, so "has an attachment starts with" is not a rule that can
# be written and then refused: it is not a rule that can be expressed. That is
# asserted here by asking the widget what it offers.
#
# THERE IS NO QTest IN DEBIAN, so nothing below synthesises a click. Every
# dialog is driven through its own methods, and the two that would open a
# modal — the editor from the list, and the confirmation before a delete — are
# injected, exactly as `ui/tagsdialog.py`'s colour picker is.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import rulematch, rules
from cormani.store.accounts import add_account
from cormani.store.folders import ensure_folder, ROLE_ARCHIVE, ROLE_INBOX


class UiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "one@example.invalid", "imap")
        self.other = add_account(self.con, "two@example.invalid", "imap")
        self.inbox = ensure_folder(self.con, self.account, "INBOX",
                                   role=ROLE_INBOX)
        self.archive = ensure_folder(self.con, self.account, "Archive",
                                     role=ROLE_ARCHIVE)
        self.theirs = ensure_folder(self.con, self.other, "Keep")
        self.warnings = []

    def _warn(self, parent, title, text):
        self.warnings.append(text)

    def editor(self, rule=None):
        from cormani.ui.ruleeditor import RuleEditor
        return support.own(self, RuleEditor(self.con, rule, warn=self._warn))


class TestTheEditor(UiCase):
    def test_a_new_rule_opens_with_one_condition_and_one_action(self):
        # Empty is a correct state and a discouraging one: two headings and
        # two buttons is not a form somebody starts filling in.
        editor = self.editor()
        self.assertEqual(len(editor._conditions), 1)
        self.assertEqual(len(editor._actions), 1)

    def test_the_operators_offered_are_the_ones_the_field_has(self):
        editor = self.editor()
        row = editor._conditions[0]
        row.field.setCurrentIndex(row.field.findData("attachment"))
        offered = {row.op.itemData(i) for i in range(row.op.count())}
        self.assertEqual(offered, {"is_true", "is_false"})
        # And a value typed before the field was changed is not carried into a
        # field that has nothing to compare it with.
        row.value.setText("left over")
        self.assertEqual(row.condition().value, "")

        row.field.setCurrentIndex(row.field.findData("subject"))
        offered = {row.op.itemData(i) for i in range(row.op.count())}
        self.assertIn("contains", offered)
        self.assertNotIn("is_true", offered)

    def test_changing_the_field_keeps_an_operator_the_new_field_also_has(self):
        # Silently turning "does not contain" into "contains" would leave a
        # rule meaning the opposite of what it said a moment earlier.
        editor = self.editor()
        row = editor._conditions[0]
        row.op.setCurrentIndex(row.op.findData("excludes"))
        row.field.setCurrentIndex(row.field.findData("subject"))
        self.assertEqual(row.op.currentData(), "excludes")

    def test_a_rule_written_here_reaches_the_store_whole(self):
        editor = self.editor()
        editor.name.setText("invoices")
        editor.account.setCurrentIndex(editor.account.findData(self.account))
        row = editor._conditions[0]
        row.field.setCurrentIndex(row.field.findData("subject"))
        row.op.setCurrentIndex(row.op.findData("contains"))
        row.value.setText("invoice")
        action = editor._actions[0]
        action.kind.setCurrentIndex(action.kind.findData("move"))
        action.folder.setCurrentIndex(
            action.folder.findData(f"folder:{self.archive}"))
        editor.save()

        saved = rules.list_rules(self.con)
        self.assertEqual([r.name for r in saved], ["invoices"])
        self.assertEqual(saved[0].account_id, self.account)
        self.assertEqual(saved[0].conditions[0].value, "invoice")
        self.assertEqual(saved[0].actions[0].folder_id, self.archive)

    def test_a_role_move_is_written_as_a_role_and_not_as_a_folder(self):
        # The difference is the whole reason a cross-account rule can be
        # written once instead of fifteen times.
        editor = self.editor()
        editor.name.setText("archive them")
        editor._conditions[0].value.setText("x")
        action = editor._actions[0]
        action.kind.setCurrentIndex(action.kind.findData("move"))
        action.folder.setCurrentIndex(
            action.folder.findData(f"role:{ROLE_ARCHIVE}"))
        editor.save()
        saved = rules.list_rules(self.con)[0]
        self.assertIsNone(saved.actions[0].folder_id)
        self.assertEqual(saved.actions[0].value, ROLE_ARCHIVE)

    def test_a_rule_with_no_name_is_refused_with_a_sentence(self):
        editor = self.editor()
        editor._conditions[0].value.setText("x")
        editor.save()
        self.assertEqual(rules.list_rules(self.con), [])
        self.assertIn("name", self.warnings[0])

    def test_a_pattern_that_will_not_compile_is_refused_here(self):
        # Not at three in the morning inside a sync, which is where the other
        # answer puts it.
        editor = self.editor()
        editor.name.setText("bad pattern")
        row = editor._conditions[0]
        row.op.setCurrentIndex(row.op.findData("matches"))
        row.value.setText("(unclosed")
        editor.save()
        self.assertEqual(rules.list_rules(self.con), [])
        self.assertIn("will not compile", self.warnings[0])

    def test_a_folder_in_another_account_is_refused_before_it_is_saved(self):
        # `rulematch.validate` is pure and cannot see this; the dialog asks
        # `rules.validate_here`, which can.
        editor = self.editor()
        editor.name.setText("wrong account")
        editor.account.setCurrentIndex(editor.account.findData(self.account))
        editor._conditions[0].value.setText("x")
        action = editor._actions[0]
        action.kind.setCurrentIndex(action.kind.findData("move"))
        action.folder.setCurrentIndex(
            action.folder.findData(f"folder:{self.theirs}"))
        editor.save()
        self.assertEqual(rules.list_rules(self.con), [])
        self.assertIn("different account", self.warnings[0])

    def test_an_existing_rule_loads_into_the_widgets_it_came_from(self):
        saved = rules.save_rule(self.con, rulematch.Rule(
            name="two of each", match_all=False, stop_after=True,
            conditions=(rulematch.Condition("from", "ends", "@x.invalid"),
                        rulematch.Condition("bulk", "is_true", "")),
            actions=(rulematch.Action(kind="flag"),
                     rulematch.Action(kind="track", value="Tender"))))
        editor = self.editor(saved)
        self.assertEqual(editor.name.text(), "two of each")
        self.assertEqual(len(editor._conditions), 2)
        self.assertEqual(len(editor._actions), 2)
        self.assertTrue(editor.stop_after.isChecked())
        self.assertFalse(editor.match_all.currentData())
        self.assertEqual(editor._actions[1].title.text(), "Tender")
        # And what it says is what it was: a round trip that changed the rule
        # would be a rule edited by being looked at.
        said = editor.rule()
        self.assertEqual([(c.field, c.op, c.value) for c in said.conditions],
                         [(c.field, c.op, c.value) for c in saved.conditions])
        self.assertEqual([(a.kind, a.folder_id, a.tag_id, a.value)
                          for a in said.actions],
                         [(a.kind, a.folder_id, a.tag_id, a.value)
                          for a in saved.actions])

    def test_saving_an_edited_rule_replaces_it_rather_than_adding_one(self):
        saved = rules.save_rule(self.con, rulematch.Rule(
            name="first", conditions=(rulematch.Condition("subject", "is", "a"),),
            actions=(rulematch.Action(kind="flag"),)))
        editor = self.editor(saved)
        editor.name.setText("second")
        editor.save()
        after = rules.list_rules(self.con)
        self.assertEqual([r.name for r in after], ["second"])
        self.assertEqual(after[0].id, saved.id)

    def test_a_condition_can_be_taken_out_and_the_last_one_may_go(self):
        # A rule with no conditions matches NOTHING — `rulematch.matches` is
        # explicit that it is never `all([])` — so an empty one is safe. It is
        # kept rather than refused, because half a rule is a rule somebody is
        # still writing; refusing the REMOVAL would trap somebody who wants to
        # start their conditions again from nothing.
        editor = self.editor()
        editor.name.setText("empty")
        editor._drop_condition(editor._conditions[0])
        self.assertEqual(editor._conditions, [])
        editor.save()
        saved = rules.list_rules(self.con)
        self.assertEqual([r.name for r in saved], ["empty"])
        self.assertFalse(saved[0].is_complete)
        self.assertEqual(self.warnings, [])
        # And the thing that matters: it can never do anything.
        self.assertFalse(rulematch.matches(saved[0], rulematch.Candidate(
            subject="anything at all", from_addr="anyone@x.invalid")))


class TestThePreview(UiCase):
    def message(self, subject="Invoice 41", sender="pay@x.invalid"):
        cur = self.con.execute(
            "INSERT INTO message (folder_id, uid, message_id, subject, "
            "from_addr, from_name, to_addrs, body_text, date_at, received_at, "
            "size_bytes) VALUES (?, ?, ?, ?, ?, ?, '', 'body', ?, ?, 100)",
            (self.inbox, self._uid(), f"<{self._uid()}@x.invalid>", subject,
             sender, "Payer", "2026-08-25T10:00:00+00:00",
             "2026-08-25T10:00:00+00:00"))
        self.con.commit()
        return int(cur.lastrowid)

    _next = 500

    def _uid(self):
        TestThePreview._next += 1
        return TestThePreview._next

    def test_it_counts_what_the_conditions_would_have_caught(self):
        for _ in range(3):
            self.message()
        self.message(subject="Lunch?")
        editor = self.editor()
        row = editor._conditions[0]
        row.field.setCurrentIndex(row.field.findData("subject"))
        row.value.setText("invoice")
        self.assertEqual(editor.preview(), 3)
        self.assertIn("3 messages", editor.preview_label.text())

    def test_it_changes_nothing(self):
        message_id = self.message()
        before = self.con.execute(
            "SELECT folder_id, seen, flagged FROM message WHERE id = ?",
            (message_id,)).fetchone()
        editor = self.editor()
        editor._conditions[0].value.setText("Payer")
        action = editor._actions[0]
        action.kind.setCurrentIndex(action.kind.findData("move"))
        editor.preview()
        after = self.con.execute(
            "SELECT folder_id, seen, flagged FROM message WHERE id = ?",
            (message_id,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM pending_op").fetchone()[0], 0)

    def test_a_rule_with_no_conditions_says_so_rather_than_counting_nothing(self):
        editor = self.editor()
        editor._drop_condition(editor._conditions[0])
        self.assertEqual(editor.preview(), 0)
        self.assertIn("Add a condition", editor.preview_label.text())


class TestTheList(UiCase):
    def dialog(self, editor=None):
        from cormani.ui.filtersdialog import FiltersDialog
        self.asked = []
        self.answer = True
        return support.own(self, FiltersDialog(
            self.con, confirm=self._confirm, editor=editor))

    def _confirm(self, parent, title, text):
        self.asked.append(text)
        return self.answer

    def rule(self, name, **kw):
        return rules.save_rule(self.con, rulematch.Rule(
            name=name,
            conditions=kw.pop("conditions",
                              (rulematch.Condition("subject", "contains", "x"),)),
            actions=kw.pop("actions", (rulematch.Action(kind="flag"),)), **kw))

    def rows(self, dialog):
        return [dialog.list.item(i).text() for i in range(dialog.list.count())]

    def test_every_rule_is_listed_with_its_position_and_its_count(self):
        self.rule("alpha")
        self.rule("beta")
        dialog = self.dialog()
        self.assertEqual(len(self.rows(dialog)), 2)
        self.assertIn("1.  alpha", self.rows(dialog)[0])
        self.assertIn("2.  beta", self.rows(dialog)[1])
        self.assertIn("matched nothing yet", self.rows(dialog)[0])

    def test_the_count_is_what_the_rule_has_actually_done(self):
        saved = self.rule("alpha")
        rules.note_match(self.con, [saved.id, saved.id])
        dialog = self.dialog()
        self.assertIn("matched 2 times", self.rows(dialog)[0])

    def test_the_tick_box_is_the_rule_s_own_enabled_and_it_is_written(self):
        saved = self.rule("alpha")
        dialog = self.dialog()
        from PySide6.QtCore import Qt
        dialog.list.item(0).setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(rules.get_rule(self.con, saved.id).enabled)
        self.assertIn("will not run", dialog.note.text())

    def test_moving_a_rule_writes_the_order_down(self):
        # Order is meaning: `stop_after` makes the rule above able to claim a
        # message outright, so this is a write to the rules and not to a view.
        first, second = self.rule("alpha"), self.rule("beta")
        dialog = self.dialog()
        dialog.list.setCurrentRow(1)
        dialog.up()
        self.assertEqual([r.id for r in rules.list_rules(self.con)],
                         [second.id, first.id])
        self.assertIn("1.  beta", self.rows(dialog)[0])

    def test_the_first_rule_cannot_move_up_and_the_last_cannot_move_down(self):
        self.rule("alpha")
        self.rule("beta")
        dialog = self.dialog()
        dialog.list.setCurrentRow(0)
        self.assertFalse(dialog.button_up.isEnabled())
        self.assertTrue(dialog.button_down.isEnabled())
        dialog.list.setCurrentRow(1)
        self.assertTrue(dialog.button_up.isEnabled())
        self.assertFalse(dialog.button_down.isEnabled())

    def test_deleting_asks_first_and_names_what_is_lost(self):
        saved = self.rule("alpha")
        rules.note_match(self.con, [saved.id])
        dialog = self.dialog()
        dialog.list.setCurrentRow(0)
        dialog.delete()
        self.assertIn("alpha", self.asked[0])
        self.assertIn("matched 1 time", self.asked[0])
        self.assertIn("cannot be undone", self.asked[0])
        self.assertEqual(rules.list_rules(self.con), [])

    def test_saying_no_to_the_question_keeps_the_rule(self):
        self.rule("alpha")
        dialog = self.dialog()
        self.answer = False
        dialog.list.setCurrentRow(0)
        dialog.delete()
        self.assertEqual([r.name for r in rules.list_rules(self.con)], ["alpha"])

    def test_a_half_written_rule_says_so_in_the_list(self):
        rules.save_rule(self.con, rulematch.Rule(
            name="unfinished",
            conditions=(rulematch.Condition("subject", "contains", "x"),)))
        dialog = self.dialog()
        self.assertIn("HALF-WRITTEN", self.rows(dialog)[0])

    def test_the_list_reloads_after_the_editor_saves(self):
        made = []

        def editor(rule):
            saved = rules.save_rule(self.con, rulematch.Rule(
                name="from the editor",
                conditions=(rulematch.Condition("subject", "is", "x"),),
                actions=(rulematch.Action(kind="flag"),)))
            made.append(saved)
            return saved

        dialog = self.dialog(editor=editor)
        dialog.new()
        self.assertEqual(len(made), 1)
        self.assertIn("from the editor", self.rows(dialog)[0])
        self.assertEqual(dialog.current_id(), made[0].id)


if __name__ == "__main__":
    unittest.main()
