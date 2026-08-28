# SPDX-License-Identifier: GPL-3.0-or-later
#
# Incremental synchronisation, against a server that changes underneath it.
#
# Every case here is a thing that happens on a real account over a year:
# messages arrive while a sync runs, another client marks something read, the
# server renumbers a mailbox, a first import is too big to take in one go.
#
# © Manish Jagdish Thatte
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import support
from fakeimap import IMAP4_Fake, Server

from cormani.imap import folders as folder_sync
from cormani.imap import sync
from cormani.imap.client import Connection
from cormani.store import folders as folders_repo
from cormani.store import ingest
from cormani.store.accounts import add_account


def raw(subject="Quarterly figures", sender="priya@example.org", body="body"):
    return (f"From: {sender}\r\nTo: owner@manitlab.example\r\n"
            f"Subject: {subject}\r\nMessage-ID: <{abs(hash(subject)) % 10**8}@x>\r\n"
            f"Date: Tue, 25 Aug 2026 10:00:00 +0000\r\n\r\n{body}\r\n").encode()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@manitlab.example", "google")
        self.server = Server()
        self.server.passwords["owner@manitlab.example"] = "pw"
        self.server.add_mailbox("INBOX")
        self.server.add_mailbox("Archive", attributes=("\\Archive",))
        self._tmp = tempfile.TemporaryDirectory()
        self.attachments = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        folder_sync.sync_folders(self.con, self.connect(), self.account)

    def connect(self):
        conn = Connection.connect("x", factory=lambda: IMAP4_Fake(self.server))
        conn.login("owner@manitlab.example", "pw")
        return conn

    def inbox(self):
        return folders_repo.by_role(self.con, self.account,
                                    folders_repo.ROLE_INBOX)

    def run_sync(self, conn=None, **kwargs):
        kwargs.setdefault("attachments_root", self.attachments)
        return sync.sync_folder(self.con, conn or self.connect(),
                                self.inbox(), **kwargs)

    def stored(self):
        return {r["uid"]: r for r in self.con.execute(
            "SELECT * FROM message WHERE folder_id = ?", (self.inbox().id,))}


class TestFirstSync(Fixture):
    def test_everything_arrives(self):
        for n in range(3):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        report = self.run_sync()
        self.assertEqual(report.new, 3)
        self.assertTrue(report.complete)
        stored = self.stored()
        self.assertEqual(set(stored), {1, 2, 3})
        self.assertEqual(stored[1]["subject"], "Report 0")
        self.assertEqual(stored[1]["from_addr"], "priya@example.org")

    def test_flags_arrive_with_the_bodies(self):
        self.server.add_message("INBOX", raw(), flags=["\\Seen", "\\Flagged"])
        self.run_sync()
        row = self.stored()[1]
        self.assertEqual((row["seen"], row["flagged"]), (1, 1))

    def test_the_arrival_time_is_the_servers(self):
        self.server.add_message("INBOX", raw(),
                                internaldate="4-Mar-2019 07:00:00 +0000")
        self.run_sync()
        self.assertEqual(self.stored()[1]["received_at"], "2019-03-04T07:00:00+00:00")

    def test_a_date_window_limits_a_first_import(self):
        # Gmail's DAILY DOWNLOAD CAP is the constraint on a first import across
        # eight Google accounts — see docs/accounts.txt.
        self.server.add_message("INBOX", raw("Ancient"),
                                internaldate="1-Jan-2019 09:00:00 +0000")
        self.server.add_message("INBOX", raw("Recent"),
                                internaldate="20-Aug-2026 09:00:00 +0000")
        report = self.run_sync(since=dt.date(2026, 1, 1))
        self.assertEqual(report.new, 1)
        self.assertEqual(self.stored()[2]["subject"], "Recent")

    def test_a_large_folder_is_taken_in_chunks_and_says_so(self):
        for n in range(12):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        report = self.run_sync(max_new=5)
        self.assertEqual(report.new, 5)
        self.assertEqual(report.remaining, 7)
        self.assertFalse(report.complete)
        self.assertTrue(report.notes)

    def test_a_chunked_import_resumes_where_it_stopped(self):
        for n in range(12):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        total = 0
        for _ in range(5):
            report = self.run_sync(max_new=5)
            total += report.new
            if report.complete:
                break
        self.assertEqual(total, 12)
        self.assertEqual(len(self.stored()), 12)

    def test_an_incomplete_pass_records_where_it_actually_reached(self):
        # Not the server's UIDNEXT, which would skip the rest for good — and
        # not nothing, which would make the next pass start from the beginning
        # and take the same chunk again, forever.
        for n in range(12):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        self.run_sync(max_new=5)
        state = folders_repo.sync_state(self.con, self.inbox().id)
        self.assertEqual(state["uid_next"], 6)
        self.assertIsNone(state["highest_modseq"],
                          "an unfinished pass has not seen every change either")

    def test_a_chunked_import_downloads_each_message_once(self):
        for n in range(12):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        for _ in range(5):
            if self.run_sync(max_new=5).complete:
                break
        fetched = " ".join(self.server.commands_matching(r"UID FETCH .*BODY"))
        self.assertEqual(len(self.stored()), 12)
        self.assertEqual(fetched.count("BODY"), 3, "three chunks, no repeats")

    def test_attachments_reach_the_disk(self):
        message = (b'From: a@example.org\r\nSubject: with a file\r\n'
                   b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
                   b'--B\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n'
                   b'--B\r\nContent-Type: application/pdf\r\n'
                   b'Content-Disposition: attachment; filename="invoice.pdf"\r\n'
                   b'Content-Transfer-Encoding: base64\r\n\r\ncGRmIGJ5dGVz\r\n--B--\r\n')
        self.server.add_message("INBOX", message)
        self.run_sync()
        stored_path = Path(self.con.execute(
            "SELECT stored_path FROM attachment").fetchone()[0])
        self.assertTrue(stored_path.exists())
        self.assertEqual(stored_path.read_bytes(), b"pdf bytes")


class TestBatchingAndRefusal(Fixture):
    def test_bodies_are_fetched_in_batches(self):
        for n in range(5):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        self.run_sync(batch=2)
        fetches = self.server.commands_matching(r"UID FETCH .*BODY")
        self.assertEqual(len(fetches), 3, "5 messages in batches of 2")

    def refuse_fetch_when(self, predicate):
        """Make the server refuse a body FETCH whose UID set matches.

        Keyed on the expanded set rather than the literal range: the client
        chooses its own ranges and halves them, which is the behaviour under
        test.
        """
        from fakeimap import Refused
        from imapwire import expand_uid_set

        original = self.server._uid_fetch
        box = self.server.mailboxes["INBOX"]

        def maybe_refuse(tag, args):
            spec = args[0].decode()
            items = b" ".join(args[1:]).decode().upper()
            if "BODY" in items:
                wanted = expand_uid_set(spec, [m.uid for m in box.messages])
                if predicate(wanted):
                    raise Refused("[SERVERBUG] System Error (Failure)")
            return original(tag, args)

        self.server._uid_fetch = maybe_refuse

    def test_a_refused_batch_is_halved_rather_than_abandoned(self):
        # Observed against Gmail: a FETCH of fifty bodies came back
        # "System Error (Failure)". Twenty-five was accepted.
        for n in range(8):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        self.refuse_fetch_when(lambda uids: len(uids) > 4)
        report = self.run_sync(batch=8)
        self.assertEqual(report.new, 8, "every message still arrived")
        self.assertEqual(report.unreadable, 0)
        self.assertGreater(len(self.server.commands_matching(r"UID FETCH .*BODY")), 1,
                           "it retried with a smaller batch")

    def test_a_message_the_server_always_refuses_is_skipped_not_retried_forever(self):
        # The first version aborted the folder BEFORE writing the watermark, so
        # the next pass asked for the identical range and stalled for good.
        for n in range(4):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        self.refuse_fetch_when(lambda uids: 2 in uids)

        first = self.run_sync(batch=4)
        self.assertEqual(first.unreadable, 1, "the one message that cannot be had")
        self.assertEqual(first.new, 3, "the other three arrived")
        self.assertIn("refused", " ".join(first.notes))

        second = self.run_sync(batch=4)
        self.assertEqual(second.new, 0, "it does not ask for the same range again")
        self.assertEqual(second.unreadable, 0, "and does not re-ask for uid 2")
        self.assertEqual(set(self.stored()), {1, 3, 4})

    def test_progress_before_a_refusal_is_kept(self):
        # Committing per batch is what makes the work already done survive the
        # batch that fails.
        for n in range(6):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        self.refuse_fetch_when(lambda uids: 3 in uids)
        report = self.run_sync(batch=2)
        self.assertGreaterEqual(report.new, 2, "the first batch survived")
        self.assertIn(1, self.stored())
        self.assertIn(2, self.stored())


class TestIncremental(Fixture):
    def setUp(self):
        super().setUp()
        for n in range(3):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        self.run_sync()
        self.baseline = len(self.server.log)

    def test_a_second_pass_downloads_nothing(self):
        report = self.run_sync()
        self.assertEqual(report.new, 0)
        self.assertEqual(
            self.server.commands_matching(r"UID FETCH .*BODY")[3:], [],
            "no body was re-downloaded")

    def test_only_the_new_messages_are_fetched(self):
        self.server.add_message("INBOX", raw("Report 3"))
        report = self.run_sync()
        self.assertEqual(report.new, 1)
        self.assertEqual(len(self.stored()), 4)
        last = self.server.commands_matching(r"UID FETCH .*BODY")[-1]
        self.assertIn(" 4 ", f" {last} ".replace(":", " "))

    def test_a_flag_set_by_another_client_is_picked_up(self):
        self.server.set_flags("INBOX", 2, ["\\Seen"])
        report = self.run_sync()
        self.assertEqual(report.flags_changed, 1)
        self.assertEqual(self.stored()[2]["seen"], 1)
        self.assertEqual(report.new, 0)

    def test_a_flag_cleared_by_another_client_is_picked_up(self):
        self.server.set_flags("INBOX", 2, ["\\Seen"])
        self.run_sync()
        self.server.set_flags("INBOX", 2, [])
        self.run_sync()
        self.assertEqual(self.stored()[2]["seen"], 0)

    def test_a_message_deleted_elsewhere_is_removed_here(self):
        self.server.expunge_uid("INBOX", 2)
        report = self.run_sync()
        self.assertEqual(report.vanished, 1)
        self.assertEqual(set(self.stored()), {1, 3})

    def test_nothing_deleted_costs_no_extra_round_trip(self):
        # EXISTS came free with SELECT; asking for the whole UID list every
        # five minutes across fifteen accounts is a lot of traffic to learn
        # that nothing happened.
        before = len(self.server.commands_matching(r"UID SEARCH ALL"))
        self.run_sync()
        after = len(self.server.commands_matching(r"UID SEARCH ALL"))
        self.assertEqual(after, before)

    def test_a_deletion_does_cost_one(self):
        self.server.expunge_uid("INBOX", 2)
        before = len(self.server.commands_matching(r"UID SEARCH ALL"))
        self.run_sync()
        self.assertEqual(len(self.server.commands_matching(r"UID SEARCH ALL")),
                         before + 1)

    def test_condstore_asks_only_for_changed_flags(self):
        self.server.set_flags("INBOX", 1, ["\\Seen"])
        self.run_sync()
        self.assertTrue(self.server.commands_matching(r"CHANGEDSINCE"))

    def test_a_server_without_condstore_still_gets_flags_right(self):
        # Correct and slow beats fast and wrong.
        self.server.capabilities = ["IMAP4rev1", "UIDPLUS", "IDLE"]
        self.server.set_flags("INBOX", 3, ["\\Flagged"])
        report = self.run_sync()
        self.assertEqual(report.flags_changed, 1)
        self.assertEqual(self.stored()[3]["flagged"], 1)
        self.assertEqual(self.server.commands_matching(r"CHANGEDSINCE"), [])

    def test_an_unchanged_folder_with_condstore_fetches_no_flags_at_all(self):
        before = len(self.server.commands_matching(r"UID FETCH"))
        self.run_sync()
        self.assertEqual(len(self.server.commands_matching(r"UID FETCH")), before)


class TestUidValidity(Fixture):
    def test_a_renumbered_mailbox_is_taken_again_from_the_start(self):
        for n in range(3):
            self.server.add_message("INBOX", raw(f"Report {n}"))
        self.run_sync()
        self.assertEqual(len(self.stored()), 3)

        # The server renumbers: every stored UID is now meaningless.
        box = self.server.mailboxes["INBOX"]
        box.uidvalidity = 9999
        box.messages = []
        box.uidnext = 1
        for n in range(2):
            self.server.add_message("INBOX", raw(f"After {n}"))

        report = self.run_sync()
        self.assertEqual(report.discarded, 3)
        self.assertEqual(report.new, 2)
        self.assertEqual({r["subject"] for r in self.stored().values()},
                         {"After 0", "After 1"})

    def test_the_search_index_survives_a_renumbering(self):
        self.server.add_message("INBOX", raw("Quarterly figures"))
        self.run_sync()
        box = self.server.mailboxes["INBOX"]
        box.uidvalidity, box.messages, box.uidnext = 9999, [], 1
        self.server.add_message("INBOX", raw("Annual figures"))
        self.run_sync()
        hits = [r[0] for r in self.con.execute(
            "SELECT rowid FROM message_fts WHERE message_fts MATCH 'figures'")]
        self.assertEqual(len(hits), 1)
        self.assertEqual(self.con.execute(
            "INSERT INTO message_fts (message_fts) VALUES ('integrity-check')"
        ).fetchall(), [])


class TestAcrossFolders(Fixture):
    def test_every_subscribed_folder_is_synced_inbox_first(self):
        self.server.add_message("INBOX", raw("In the inbox"))
        self.server.add_message("Archive", raw("In the archive"))
        reports = sync.sync_account_folders(
            self.con, self.connect(), self.account,
            attachments_root=self.attachments)
        self.assertEqual([r.folder for r in reports], ["INBOX", "Archive"])
        self.assertEqual(sum(r.new for r in reports), 2)

    def test_an_unsubscribed_folder_is_left_alone(self):
        self.server.add_mailbox("Noise", subscribed=False)
        self.server.add_message("Noise", raw("Ignored"))
        folder_sync.sync_folders(self.con, self.connect(), self.account)
        reports = sync.sync_account_folders(
            self.con, self.connect(), self.account,
            attachments_root=self.attachments)
        self.assertNotIn("Noise", [r.folder for r in reports])


class TestRobustness(Fixture):
    def test_a_message_the_server_will_not_send_is_counted_not_fatal(self):
        # The UID stays unseen, so the next pass asks again.
        self.server.add_message("INBOX", raw("Fine"))
        report = self.run_sync()
        self.assertEqual(report.new, 1)
        self.assertEqual(report.unreadable, 0)

    def test_a_malformed_message_still_becomes_a_row(self):
        # A sync that stops on one bad message never reaches the ten thousand
        # behind it.
        self.server.add_message("INBOX", b"\x00\x01 not a message at all\r\n")
        self.server.add_message("INBOX", raw("Perfectly fine"))
        report = self.run_sync()
        self.assertEqual(report.new, 2)
        self.assertEqual(len(self.stored()), 2)

    def test_an_empty_folder_syncs_to_nothing(self):
        report = self.run_sync()
        self.assertEqual(report.changed, 0)
        self.assertTrue(report.complete)


if __name__ == "__main__":
    unittest.main()
