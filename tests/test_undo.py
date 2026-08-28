# SPDX-License-Identifier: GPL-3.0-or-later
#
# Undo, on both sides of the offline queue.
#
# THE TEST THAT MATTERS IS THE ONE ABOUT THE QUEUE. Putting a row back is the
# easy half and the half a test would naturally assert; the half that breaks is
# what the server is going to be told. Undoing an archive whose op is still
# queued has to DELETE that op — if it does not, the next sync moves the message
# away again in front of a user who watched it come back, and no assertion about
# `folder_id` would have noticed.
#
# The other half of the same rule: an op the server has already accepted is gone
# from the table, and undo then has to queue the opposite rather than nothing.
# Both cases are here, and they are the same user gesture.
#
# © Manish Jagdish Thatte
import unittest

import support
from test_threads import Store

from cormani.store import edits, folders, messages, pending, tags, undo


class UndoCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Store(self)
        self.con = self.fixture.con
        self.inbox = self.fixture.inbox
        self.archive = self.fixture.archive
        self.first = self.fixture.store(subject="Wavelengths",
                                        message_id="<a@x>")
        self.second = self.fixture.store(subject="Invoices", message_id="<b@x>")

    # ------------------------------------------------------------- helpers
    def ops(self, kind=None):
        rows = self.con.execute(
            "SELECT * FROM pending_op ORDER BY id").fetchall()
        return [pending._op(r) for r in rows
                if kind is None or r["kind"] == kind]

    def folder_of(self, message_id):
        return self.con.execute("SELECT folder_id, uid FROM message WHERE id = ?",
                                (message_id,)).fetchone()

    def seen(self, message_id):
        return bool(self.con.execute("SELECT seen FROM message WHERE id = ?",
                                     (message_id,)).fetchone()[0])


class TestFlags(UndoCase):
    def test_a_flag_goes_back_and_the_queued_op_cancels_itself(self):
        step = undo.capture_flag(self.con, [self.first], "seen", "Marked 1 read")
        edits.set_seen(self.con, [self.first], True)
        self.assertTrue(self.seen(self.first))
        self.assertEqual(len(self.ops(pending.KIND_FLAG)), 1)

        undo.reverse(self.con, step)
        self.assertFalse(self.seen(self.first))
        # ONE op, not two, and it says what the row now IS rather than what was
        # done to it: the queue's job is to make the server match the store, and
        # after this it does. Coalescing is what keeps a mark-read-then-undo
        # from being two round trips.
        queued = self.ops(pending.KIND_FLAG)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload, {"add": [], "remove": ["\\Seen"]})

    def test_a_mixed_selection_goes_back_to_the_state_each_row_had(self):
        edits.set_seen(self.con, [self.first], True)
        step = undo.capture_flag(self.con, [self.first, self.second], "seen",
                                 "Marked 2 unread")
        edits.set_seen(self.con, [self.first, self.second], False)
        undo.reverse(self.con, step)
        self.assertTrue(self.seen(self.first))
        self.assertFalse(self.seen(self.second))

    def test_an_op_the_server_took_is_answered_with_its_opposite(self):
        step = undo.capture_flag(self.con, [self.first], "flagged", "Flagged 1")
        edits.set_flagged(self.con, [self.first], True)
        pending.complete(self.con, [op.id for op in self.ops()])
        self.assertEqual(self.ops(), [])

        undo.reverse(self.con, step)
        queued = self.ops(pending.KIND_FLAG)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload, {"add": [], "remove": ["\\Flagged"]})

    def test_a_step_with_nothing_in_it_does_nothing(self):
        self.assertEqual(undo.reverse(self.con, undo.Step("", undo.KIND_FLAG)), 0)
        self.assertEqual(
            undo.capture_flag(self.con, [], "seen", "x").before, ())
        self.assertEqual(undo.capture_move(self.con, [], "x").before, ())
        self.assertEqual(undo.capture_tag(self.con, [], 1, "x").before, ())


class TestMoves(UndoCase):
    def test_a_move_the_server_has_not_heard_is_unsaid_entirely(self):
        before = self.folder_of(self.first)
        step = undo.capture_move(self.con, [self.first], "Archived")
        edits.archive(self.con, [self.first])
        self.assertEqual(self.folder_of(self.first)["folder_id"], self.archive)
        self.assertIsNone(self.folder_of(self.first)["uid"])
        self.assertEqual(len(self.ops(pending.KIND_MOVE)), 1)

        undo.reverse(self.con, step)
        after = self.folder_of(self.first)
        self.assertEqual(after["folder_id"], before["folder_id"])
        # THE UID TOO. Without it the message is a row the server cannot be
        # asked about, and a later flag change on it would be dropped.
        self.assertEqual(after["uid"], before["uid"])
        self.assertEqual(self.ops(pending.KIND_MOVE), [])
        self.assertEqual(pending.unsent_message_ids(self.con), set())

    def test_a_move_the_server_took_is_answered_with_a_move_back(self):
        step = undo.capture_move(self.con, [self.first], "Archived")
        edits.archive(self.con, [self.first])
        # The reconciler's half: the server accepted it and wrote back the UID
        # the message now has in its new folder.
        pending.complete(self.con, [op.id for op in self.ops()])
        self.con.execute("UPDATE message SET uid = 900 WHERE id = ?", (self.first,))
        self.con.commit()

        undo.reverse(self.con, step)
        self.assertEqual(self.folder_of(self.first)["folder_id"], self.inbox)
        queued = self.ops(pending.KIND_MOVE)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].target_folder_id, self.inbox)
        self.assertEqual(queued[0].source_folder_id, self.archive)
        self.assertEqual(queued[0].source_uid, 900)

    def test_a_move_that_never_happened_is_not_reversed(self):
        # An account with no Archive folder archives nothing, and a step built
        # from the state — rather than from the intent — knows the difference.
        empty = Store(self)
        con = empty.con
        con.execute("DELETE FROM folder WHERE id = ?", (empty.archive,))
        con.commit()
        message = empty.store(subject="Nowhere to go", message_id="<n@x>")
        step = undo.capture_move(con, [message], "Archived")
        moved, skipped = edits.archive(con, [message])
        self.assertEqual((moved, skipped), (0, [message]))
        self.assertEqual(undo.reverse(con, step), 0)
        self.assertEqual(
            con.execute("SELECT folder_id FROM message WHERE id = ?",
                        (message,)).fetchone()[0], empty.inbox)

    def test_deleting_is_a_move_and_comes_back_the_same_way(self):
        trash = folders.ensure_folder(self.con, self.fixture.account,
                                      "[Gmail]/Trash", display_name="Trash",
                                      role=folders.ROLE_TRASH)
        step = undo.capture_move(self.con, [self.first], "Deleted")
        edits.trash(self.con, [self.first])
        self.assertEqual(self.folder_of(self.first)["folder_id"], trash)
        undo.reverse(self.con, step)
        self.assertEqual(self.folder_of(self.first)["folder_id"], self.inbox)


class TestTags(UndoCase):
    def setUp(self):
        super().setUp()
        self.tag = tags.list_tags(self.con)[0].id

    def carrying(self):
        return {int(r[0]) for r in self.con.execute(
            "SELECT message_id FROM message_tag WHERE tag_id = ?",
            (self.tag,)).fetchall()}

    def test_tagging_and_taking_it_back(self):
        step = undo.capture_tag(self.con, [self.first, self.second], self.tag, "x")
        tags.set_on_messages(self.con, [self.first, self.second], self.tag, True)
        self.assertEqual(self.carrying(), {self.first, self.second})
        undo.reverse(self.con, step)
        self.assertEqual(self.carrying(), set())

    def test_a_row_that_already_carried_it_keeps_it(self):
        tags.set_on_messages(self.con, [self.first], self.tag, True)
        step = undo.capture_tag(self.con, [self.first, self.second], self.tag, "x")
        tags.set_on_messages(self.con, [self.first, self.second], self.tag, False)
        undo.reverse(self.con, step)
        self.assertEqual(self.carrying(), {self.first})

    def test_tags_queue_nothing_because_they_are_not_the_servers(self):
        step = undo.capture_tag(self.con, [self.first], self.tag, "x")
        tags.set_on_messages(self.con, [self.first], self.tag, True)
        undo.reverse(self.con, step)
        self.assertEqual(self.ops(), [])


# ------------------------------------------------------------- from the window
@support.requires_qt
class TestUndoInTheWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.window import MainWindow

        self.fixture = Store(self)
        self.con = self.fixture.con
        self.first = self.fixture.store(subject="Wavelengths", message_id="<a@x>")
        self.second = self.fixture.store(subject="Invoices", message_id="<b@x>")
        self.window = support.own(self, MainWindow(self.con, demo=False))
        self.mail = self.window.mail

    def ids(self):
        return [r.id for r in self.mail.model.rows()]

    def test_archiving_and_taking_it_back_puts_the_row_where_it_was(self):
        self.mail.select_message(self.first)
        self.mail.run_action("archive", [self.first])
        self.assertNotIn(self.first, self.ids())

        self.assertTrue(self.window.mail.undo())
        self.assertIn(self.first, self.ids())
        self.assertIn("Undone", self.window.status_message.text())
        # The move the server was going to be told about is gone. The flag op
        # from reading the message on the way past is not undone and should not
        # be — it is not what the user asked to take back.
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM pending_op WHERE kind = 'move'")
            .fetchone()[0], 0)

    def test_the_menu_says_what_would_come_back(self):
        self.window._label_undo()
        self.assertEqual(self.window.act_undo.text(), "&Undo")
        self.assertFalse(self.window.act_undo.isEnabled())

        self.mail.run_action("flag", [self.first])
        self.window._label_undo()
        self.assertIn("flagged 1", self.window.act_undo.text())
        self.assertTrue(self.window.act_undo.isEnabled())

    def test_undo_with_nothing_behind_it_says_so_and_changes_nothing(self):
        before = self.ids()
        self.assertFalse(self.mail.undo())
        self.assertIn("Nothing to undo", self.window.status_message.text())
        self.assertEqual(self.ids(), before)

    def test_the_stack_goes_back_more_than_one_step(self):
        self.mail.run_action("flag", [self.first])
        self.mail.run_action("archive", [self.second])
        self.assertNotIn(self.second, self.ids())

        self.mail.undo()
        self.assertIn(self.second, self.ids())
        self.mail.undo()
        row = messages.get_row(self.con, self.first)
        self.assertFalse(row.flagged)
        self.assertFalse(self.mail.undo())

    def test_it_does_not_grow_without_bound(self):
        from cormani.ui import actions as actions_mod

        for _ in range(actions_mod.UNDO_DEPTH + 5):
            self.mail.run_action("flag", [self.first])
        self.assertEqual(len(self.mail.actions.stack), actions_mod.UNDO_DEPTH)

    def test_a_tag_key_is_undoable_too(self):
        tag = tags.by_shortcut(self.con, 1)
        self.mail.select_message(self.first)
        self.mail.run_action("tag_1", [self.first])
        row = messages.get_row(self.con, self.first)
        self.assertEqual([t.id for t in row.tags], [tag.id])

        self.mail.undo()
        self.assertEqual(messages.get_row(self.con, self.first).tags, ())


if __name__ == "__main__":
    unittest.main()
