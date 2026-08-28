# SPDX-License-Identifier: GPL-3.0-or-later
#
# The offline queue, and telling the server afterwards.
#
# Offline-first means the local change lands first. These tests are about what
# happens next — including the cases where "next" is a week later, on a
# different network, after somebody else moved the message.
#
# © Manish Jagdish Thatte
import unittest

import support
from fakeimap import IMAP4_Fake, Server

from cormani.imap import queue
from cormani.imap.client import Connection
from cormani.imap import folders as folder_sync
from cormani.store import folders as folders_repo
from cormani.store import edits, ingest, messages, pending
from cormani.imap import envelope
from cormani.store.accounts import add_account

RAW = (b"From: Priya Raman <priya@example.org>\r\n"
       b"Subject: Quarterly figures\r\n"
       b"Date: Tue, 25 Aug 2026 10:00:00 +0000\r\n\r\nbody\r\n")


class Fixture(unittest.TestCase):
    """A store and a server holding the same three messages."""

    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@manitlab.example", "google")
        self.server = Server()
        self.server.passwords["owner@manitlab.example"] = "pw"
        self.server.add_mailbox("INBOX")
        self.server.add_mailbox("Archive", attributes=("\\Archive",))
        self.server.add_mailbox("Trash", attributes=("\\Trash",))
        self.uids = [self.server.add_message("INBOX", RAW) for _ in range(3)]
        folder_sync.sync_folders(self.con, self.connect(), self.account)
        self.inbox = folders_repo.by_role(self.con, self.account,
                                          folders_repo.ROLE_INBOX).id
        self.archive = folders_repo.by_role(self.con, self.account,
                                            folders_repo.ROLE_ARCHIVE).id
        self.ids = [ingest.store_message(self.con, self.inbox, uid,
                                         envelope.read(RAW)).message_id
                    for uid in self.uids]

    def connect(self):
        conn = Connection.connect("x", factory=lambda: IMAP4_Fake(self.server))
        conn.login("owner@manitlab.example", "pw")
        return conn

    def drain(self, conn=None):
        return queue.drain(self.con, conn or self.connect(), self.account)

    def ops(self):
        return pending.pending_for(self.con, self.account, include_stuck=True)

    def server_flags(self, uid, path="INBOX"):
        message = self.server.mailboxes[path].by_uid(uid)
        return message.flags if message else None


class TestEnqueueing(Fixture):
    def test_marking_read_queues_it(self):
        edits.set_seen(self.con, [self.ids[0]], True)
        ops = self.ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].kind, pending.KIND_FLAG)
        self.assertEqual(ops[0].payload["add"], ["\\Seen"])
        self.assertEqual(ops[0].source_uid, self.uids[0])

    def test_the_row_is_marked_as_not_yet_sent(self):
        edits.set_seen(self.con, [self.ids[0]], True)
        self.assertEqual(pending.unsent_message_ids(self.con), {self.ids[0]})

    def test_read_then_unread_then_read_is_one_op(self):
        # Eighty messages marked read is eighty ops. Marking them read, unread
        # and read again must still be eighty, not two hundred and forty.
        for value in (True, False, True):
            edits.set_seen(self.con, self.ids, value)
        ops = self.ops()
        self.assertEqual(len(ops), 3, "one per message, not one per keypress")
        self.assertEqual(ops[0].payload["add"], ["\\Seen"])
        self.assertEqual(ops[0].payload["remove"], [])

    def test_unreading_a_queued_read_leaves_the_right_intention(self):
        edits.set_seen(self.con, [self.ids[0]], True)
        edits.set_seen(self.con, [self.ids[0]], False)
        op = self.ops()[0]
        self.assertEqual(op.payload["add"], [])
        self.assertEqual(op.payload["remove"], ["\\Seen"])

    def test_different_flags_accumulate_rather_than_replace(self):
        edits.set_seen(self.con, [self.ids[0]], True)
        edits.set_flagged(self.con, [self.ids[0]], True)
        op = self.ops()[0]
        self.assertEqual(op.payload["add"], ["\\Flagged", "\\Seen"])

    def test_archiving_queues_the_source_not_the_destination(self):
        # The queue needs where the server still thinks the message is, and the
        # move is what destroys it.
        edits.archive(self.con, [self.ids[0]])
        op = self.ops()[0]
        self.assertEqual(op.kind, pending.KIND_MOVE)
        self.assertEqual(op.source_folder_id, self.inbox)
        self.assertEqual(op.source_uid, self.uids[0])
        self.assertEqual(op.target_folder_id, self.archive)
        self.assertIsNone(self.con.execute(
            "SELECT uid FROM message WHERE id = ?", (self.ids[0],)).fetchone()[0])

    def test_a_flag_after_a_move_does_not_merge_into_it(self):
        # It would overtake the move, and after the move the UID is gone.
        edits.archive(self.con, [self.ids[0]])
        edits.set_seen(self.con, [self.ids[0]], True)
        kinds = [op.kind for op in self.ops()]
        self.assertEqual(kinds, [pending.KIND_MOVE, pending.KIND_FLAG])

    def test_a_message_never_on_the_server_queues_nothing(self):
        local_only = self.con.execute(
            "INSERT INTO message (folder_id, uid, subject) VALUES (?, NULL, 'x')",
            (self.inbox,)).lastrowid
        self.con.commit()
        edits.set_seen(self.con, [local_only], True)
        self.assertEqual(self.ops(), [])

    def test_the_syncs_own_writes_are_not_queued_back_at_the_server(self):
        # Otherwise every flag the server reported would be sent straight back
        # to it, forever.
        ingest.update_flags(self.con, self.inbox, {self.uids[0]: ["\\Seen"]})
        self.assertEqual(self.ops(), [])


class TestDraining(Fixture):
    def test_a_flag_reaches_the_server(self):
        edits.set_seen(self.con, [self.ids[0]], True)
        report = self.drain()
        self.assertEqual(report.sent, 1)
        self.assertTrue(report.clean)
        self.assertIn("\\Seen", self.server_flags(self.uids[0]))
        self.assertEqual(self.ops(), [], "a sent op is retired")
        self.assertEqual(pending.unsent_message_ids(self.con), set())

    def test_clearing_a_flag_reaches_the_server(self):
        self.server.set_flags("INBOX", self.uids[0], ["\\Seen"])
        edits.set_seen(self.con, [self.ids[0]], False)
        self.drain()
        self.assertNotIn("\\Seen", self.server_flags(self.uids[0]))

    def test_one_command_for_many_messages(self):
        edits.set_seen(self.con, self.ids, True)
        self.drain()
        stores = self.server.commands_matching(r"UID STORE")
        self.assertEqual(len(stores), 1, stores)
        self.assertIn("1:3", stores[0])

    def test_order_is_preserved_across_a_move(self):
        # Read, then archive, then flagged. The archive must not be overtaken.
        edits.set_seen(self.con, [self.ids[0]], True)
        edits.archive(self.con, [self.ids[0]])
        edits.set_flagged(self.con, [self.ids[0]], True)
        self.drain()
        self.assertIsNone(self.server.mailboxes["INBOX"].by_uid(self.uids[0]))
        moved = self.server.mailboxes["Archive"].messages[0]
        self.assertEqual(moved.flags, {"\\Seen", "\\Flagged"},
                         "both flag changes landed, on the right side of the move")

    def test_a_move_writes_the_new_uid_back(self):
        # Otherwise the next sync of the destination sees the message as new
        # and the store holds it twice.
        edits.archive(self.con, [self.ids[0]])
        self.drain()
        row = self.con.execute("SELECT folder_id, uid FROM message WHERE id = ?",
                               (self.ids[0],)).fetchone()
        self.assertEqual(row["folder_id"], self.archive)
        self.assertEqual(row["uid"], self.server.mailboxes["Archive"].messages[0].uid)

    def test_without_uidplus_the_row_is_removed_for_the_sync_to_refetch(self):
        self.server.capabilities = ["IMAP4rev1", "IDLE"]
        edits.archive(self.con, [self.ids[0]])
        self.drain()
        self.assertEqual(len(self.server.mailboxes["Archive"].messages), 1)
        self.assertIsNone(self.con.execute(
            "SELECT id FROM message WHERE id = ?", (self.ids[0],)).fetchone(),
            "no UID means no way to match it; the next sync fetches it properly")

    def test_deleting_moves_to_trash_rather_than_erasing(self):
        edits.trash(self.con, [self.ids[0]])
        self.drain()
        self.assertEqual(len(self.server.mailboxes["Trash"].messages), 1)
        self.assertIsNone(self.server.mailboxes["INBOX"].by_uid(self.uids[0]))

    def test_draining_an_empty_queue_sends_no_commands(self):
        before = len(self.server.log)
        report = self.drain()
        self.assertEqual(report.sent, 0)
        # Only the connection's own handshake, no SELECT or STORE.
        self.assertEqual(self.server.commands_matching(r"UID (STORE|MOVE)"), [])


class TestConflicts(Fixture):
    def test_an_op_with_no_coordinates_left_is_dropped_not_retried(self):
        # A flag queued behind a move has no UID of its own; if the move ahead
        # of it is dropped, nothing on the server answers to it any more. The
        # intention has been overtaken by events.
        edits.archive(self.con, [self.ids[0]])
        edits.set_seen(self.con, [self.ids[0]], True)
        move_op = [op for op in self.ops() if op.kind == pending.KIND_MOVE][0]
        self.con.execute("DELETE FROM pending_op WHERE id = ?", (move_op.id,))
        self.con.commit()
        report = self.drain()
        self.assertEqual(report.dropped, 1)
        self.assertEqual(report.sent, 0)
        self.assertEqual(self.ops(), [])

    def test_a_message_someone_else_moved_is_not_an_error(self):
        # The local row keeps its UID until the next sync, and an IMAP STORE
        # against a UID the server no longer has is a no-op rather than a
        # refusal. The op is sent, retired, and the next sync reconciles.
        edits.set_seen(self.con, [self.ids[0]], True)
        self.server.expunge_uid("INBOX", self.uids[0])
        report = self.drain()
        self.assertEqual(report.sent, 1)
        self.assertTrue(report.clean)

    def test_a_folder_that_has_gone_drops_its_ops(self):
        edits.set_seen(self.con, [self.ids[0]], True)
        del self.server.mailboxes["INBOX"]
        report = self.drain()
        self.assertEqual(report.dropped, 1)
        self.assertTrue(report.errors)
        self.assertEqual(self.ops(), [])

    def test_a_refused_op_is_retried_not_lost(self):
        edits.set_seen(self.con, [self.ids[0]], True)
        self.server.fail_next["UID"] = "NO [SERVERBUG] not today"
        report = self.drain()
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.sent, 0)
        self.assertEqual(len(self.ops()), 1)
        self.assertEqual(self.ops()[0].attempts, 1)
        self.assertIn("not today", self.ops()[0].last_error)
        # And it goes on the next attempt.
        self.assertEqual(self.drain().sent, 1)

    def test_an_op_that_never_succeeds_becomes_stuck_and_is_reported(self):
        # Never silently emptied: the interface can say "could not be sent".
        edits.set_seen(self.con, [self.ids[0]], True)
        op_id = self.ops()[0].id
        for _ in range(pending.MAX_ATTEMPTS):
            pending.record_failure(self.con, op_id, "no")
        report = self.drain()
        self.assertEqual(report.stuck, 1)
        self.assertEqual(report.sent, 0)
        self.assertEqual(len(self.ops()), 1, "kept, not deleted")
        self.assertTrue(self.ops()[0].stuck)

    def test_a_stuck_op_does_not_block_the_ones_behind_it(self):
        edits.set_seen(self.con, [self.ids[0]], True)
        pending.record_failure(self.con, self.ops()[0].id, "no")
        for _ in range(pending.MAX_ATTEMPTS):
            pending.record_failure(self.con, self.ops()[0].id, "no")
        edits.set_flagged(self.con, [self.ids[1]], True)
        report = self.drain()
        self.assertEqual(report.sent, 1)
        self.assertIn("\\Flagged", self.server_flags(self.uids[1]))

    def test_the_drain_gives_up_after_three_failures_in_a_row(self):
        # Three rather than one: a single refused op is usually about that op,
        # three in a row is about the connection.
        # Three DIFFERENT changes, so they cannot merge into one command: the
        # batching is by compatibility, and identical ops would be one run and
        # therefore one failure.
        edits.set_seen(self.con, [self.ids[0]], True)
        edits.set_flagged(self.con, [self.ids[1]], True)
        edits.set_answered(self.con, [self.ids[2]], True)
        conn = self.connect()
        self.server.drop_after = len(self.server.log)
        report = self.drain(conn)
        self.assertTrue(report.errors)
        self.assertIn("three failures", report.errors[-1])
        self.assertGreater(len(self.ops()), 0, "the rest stay queued")

    def test_a_duplicate_from_a_race_with_the_sync_is_resolved(self):
        # The sync fetched the moved message into the destination before the
        # queue drained. The row the user moved is the duplicate.
        edits.archive(self.con, [self.ids[0]])
        moved_uid = self.server.mailboxes["INBOX"].by_uid(self.uids[0])
        ingest.store_message(self.con, self.archive, 1, envelope.read(RAW))
        report = self.drain()
        rows = self.con.execute(
            "SELECT COUNT(*) FROM message WHERE folder_id = ?",
            (self.archive,)).fetchone()[0]
        self.assertEqual(rows, 1, "one message, not two")


class TestCounts(Fixture):
    def test_the_status_bar_numbers(self):
        edits.set_seen(self.con, self.ids, True)
        summary = queue.pending_summary(self.con)
        self.assertEqual(summary[self.account]["pending"], 3)
        self.assertEqual(summary[self.account]["stuck"], 0)

    def test_clearing_is_only_ever_at_the_users_request(self):
        edits.set_seen(self.con, self.ids, True)
        self.assertEqual(pending.clear_for_account(self.con, self.account), 3)
        self.assertEqual(self.ops(), [])
        self.assertEqual(pending.unsent_message_ids(self.con), set())


if __name__ == "__main__":
    unittest.main()
