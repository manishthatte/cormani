# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing fetched mail into the store.
#
# The filename cases are the ones that matter most: an attachment's name
# arrived from a stranger over the internet and is used to name a file.
#
# © Manish Jagdish Thatte
import tempfile
import unittest
from pathlib import Path

import support

from cormani.imap import envelope
from cormani.store import ingest
from cormani.store.accounts import add_account
from cormani.store.folders import ensure_folder, ROLE_INBOX


def message(subject="Quarterly figures", body="Numbers attached.",
            sender="Priya Raman <priya@example.org>", extra="") -> bytes:
    raw = (f"From: {sender}\n"
           f"To: owner@manitlab.example\n"
           f"Subject: {subject}\n"
           f"Message-ID: <{abs(hash(subject)) % 10 ** 8}@example.org>\n"
           f"Date: Tue, 25 Aug 2026 10:00:00 +0000\n"
           f"{extra}\n{body}\n")
    return raw.replace("\n", "\r\n").encode("utf-8")


class TestSafeFilename(unittest.TestCase):
    def test_a_traversal_attempt_becomes_a_name(self):
        for hostile in ("../../.ssh/authorized_keys", "..\\..\\windows\\system32",
                        "/etc/passwd", "....//....//etc/shadow"):
            got = ingest.safe_filename(hostile)
            self.assertNotIn("/", got, hostile)
            self.assertNotIn("\\", got, hostile)
            self.assertNotEqual(got, "..", hostile)

    def test_names_that_reduce_to_nothing_get_the_fallback(self):
        for empty in ("", ".", "..", "...", "   ", "/", "///", "   .  "):
            self.assertEqual(ingest.safe_filename(empty), "part", repr(empty))

    def test_control_characters_and_nuls_are_removed(self):
        got = ingest.safe_filename("in\x00voi\x07ce\r\n.pdf")
        self.assertEqual(got, "invoice.pdf")

    def test_an_absurd_name_is_truncated_but_keeps_its_extension(self):
        got = ingest.safe_filename("a" * 4000 + ".pdf")
        self.assertLessEqual(len(got), 120)
        self.assertTrue(got.endswith(".pdf"))

    def test_an_ordinary_name_is_left_recognisable(self):
        self.assertEqual(ingest.safe_filename("Invoice 2026-08.pdf"),
                         "Invoice 2026-08.pdf")

    def test_a_unicode_name_is_kept_readable_without_being_a_path(self):
        got = ingest.safe_filename("Rechnung März.pdf")
        self.assertTrue(got.endswith(".pdf"))
        self.assertNotIn("/", got)


class TestAttachmentPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_the_path_is_under_the_root(self):
        path = ingest.attachment_path(self.root, 1, 2, 1, "invoice.pdf")
        self.assertEqual(path.parent, (self.root / "1" / "2").resolve())
        self.assertTrue(path.name.endswith("invoice.pdf"))

    def test_a_traversing_name_cannot_leave_the_root(self):
        # Both barriers, together: the name is made safe, and the result is
        # then checked against the root anyway.
        for hostile in ("../../../../etc/passwd", "..", "../secret"):
            path = ingest.attachment_path(self.root, 1, 2, 1, hostile)
            self.assertIn(self.root.resolve(), path.parents, hostile)

    def test_two_attachments_of_the_same_name_do_not_collide(self):
        a = ingest.attachment_path(self.root, 1, 2, 1, "scan.pdf")
        b = ingest.attachment_path(self.root, 1, 2, 2, "scan.pdf")
        self.assertNotEqual(a, b)


class TestStoreMessage(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@manitlab.example", "google")
        self.folder = ensure_folder(self.con, self.account, "INBOX",
                                    role=ROLE_INBOX)
        self._tmp = tempfile.TemporaryDirectory()
        self.attachments = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def rows(self):
        return self.con.execute("SELECT * FROM message ORDER BY id").fetchall()

    def test_a_message_becomes_a_row(self):
        env = envelope.read(message())
        got = ingest.store_message(self.con, self.folder, 101, env,
                                   flags=["\\Seen"],
                                   internaldate="2026-08-25T10:00:00+00:00")
        self.assertTrue(got.created)
        row = self.rows()[0]
        self.assertEqual(row["uid"], 101)
        self.assertEqual(row["subject"], "Quarterly figures")
        self.assertEqual(row["from_addr"], "priya@example.org")
        self.assertEqual(row["seen"], 1)
        self.assertEqual(row["flagged"], 0)
        self.assertEqual(row["received_at"], "2026-08-25T10:00:00+00:00")

    def test_received_at_is_the_servers_arrival_time_not_todays_import(self):
        # Otherwise every message of a first import looks like it arrived today,
        # and "what came in while I was away" is useless on the day it matters.
        env = envelope.read(message())
        ingest.store_message(self.con, self.folder, 101, env,
                             internaldate="2019-03-04T07:00:00+00:00")
        self.assertEqual(self.rows()[0]["received_at"], "2019-03-04T07:00:00+00:00")

    def test_storing_the_same_uid_twice_updates_rather_than_duplicates(self):
        # A sync interrupted halfway is re-run from the start.
        env = envelope.read(message())
        first = ingest.store_message(self.con, self.folder, 101, env)
        second = ingest.store_message(self.con, self.folder, 101, env,
                                      flags=["\\Seen"])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(first.message_id, second.message_id)
        self.assertFalse(second.created)
        self.assertEqual(self.rows()[0]["seen"], 1)

    def test_the_same_message_in_two_folders_is_two_rows(self):
        # Gmail shows one message under several labels; de-duplicating would
        # make archiving from the unified inbox strip a label nobody touched.
        other = ensure_folder(self.con, self.account, "[Gmail]/All Mail")
        env = envelope.read(message())
        ingest.store_message(self.con, self.folder, 101, env)
        ingest.store_message(self.con, other, 55, env)
        self.assertEqual(len(self.rows()), 2)

    def test_flags_map_onto_the_five_columns(self):
        env = envelope.read(message())
        ingest.store_message(self.con, self.folder, 1, env, flags=[
            "\\Seen", "\\Flagged", "\\Answered", "\\Draft", "\\Deleted"])
        row = self.rows()[0]
        for column in ("seen", "flagged", "answered", "draft", "deleted"):
            self.assertEqual(row[column], 1, column)

    def test_a_servers_own_keyword_is_ignored_not_guessed_at(self):
        env = envelope.read(message())
        ingest.store_message(self.con, self.folder, 1, env,
                             flags=["\\Seen", "$Phishing", "NonJunk"])
        self.assertEqual(self.rows()[0]["seen"], 1)

    def test_flags_are_matched_case_insensitively(self):
        env = envelope.read(message())
        ingest.store_message(self.con, self.folder, 1, env, flags=["\\SEEN"])
        self.assertEqual(self.rows()[0]["seen"], 1)


class TestAttachmentsOnDisk(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@manitlab.example", "google")
        self.folder = ensure_folder(self.con, self.account, "INBOX")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def with_pdf(self, filename="invoice.pdf") -> bytes:
        raw = ('From: a@example.org\nSubject: with a file\n'
               'Content-Type: multipart/mixed; boundary="B"\n\n'
               '--B\nContent-Type: text/plain\n\nSee attached.\n'
               '--B\nContent-Type: application/pdf\n'
               f'Content-Disposition: attachment; filename="{filename}"\n'
               'Content-Transfer-Encoding: base64\n\ncGRmIGJ5dGVz\n--B--\n')
        return raw.replace("\n", "\r\n").encode("utf-8")

    def test_the_bytes_reach_the_disk_and_the_row_names_them(self):
        env = envelope.read(self.with_pdf())
        got = ingest.store_message(self.con, self.folder, 1, env,
                                   attachments_root=self.root)
        self.assertEqual(got.attachments, 1)
        row = self.con.execute("SELECT * FROM attachment").fetchone()
        self.assertEqual(row["filename"], "invoice.pdf")
        stored = Path(row["stored_path"])
        self.assertTrue(stored.exists())
        self.assertEqual(stored.read_bytes(), b"pdf bytes")
        self.assertIn(self.root.resolve(), stored.parents)
        self.assertEqual(self.con.execute(
            "SELECT has_attachment FROM message").fetchone()[0], 1)

    def test_a_hostile_filename_is_written_inside_the_root(self):
        env = envelope.read(self.with_pdf("../../../../tmp/owned.pdf"))
        ingest.store_message(self.con, self.folder, 1, env,
                             attachments_root=self.root)
        stored = Path(self.con.execute(
            "SELECT stored_path FROM attachment").fetchone()[0])
        self.assertIn(self.root.resolve(), stored.parents)
        self.assertTrue(stored.exists())
        # The name a person sees is the one the sender gave; only the path is
        # made safe.
        self.assertEqual(self.con.execute(
            "SELECT filename FROM attachment").fetchone()[0],
            "../../../../tmp/owned.pdf")

    def test_with_no_root_the_row_is_written_and_no_file_is(self):
        # `--check` and the tests must be able to ingest without a data
        # directory at all.
        env = envelope.read(self.with_pdf())
        got = ingest.store_message(self.con, self.folder, 1, env)
        self.assertEqual(got.attachments, 0)
        self.assertEqual(self.con.execute(
            "SELECT stored_path FROM attachment").fetchone()[0], "")

    def test_re_ingesting_replaces_rather_than_accumulates(self):
        env = envelope.read(self.with_pdf())
        ingest.store_message(self.con, self.folder, 1, env, attachments_root=self.root)
        first = Path(self.con.execute("SELECT stored_path FROM attachment").fetchone()[0])
        ingest.store_message(self.con, self.folder, 1, env, attachments_root=self.root)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM attachment").fetchone()[0], 1)
        self.assertTrue(first.exists(), "the replacement writes the same path")

    def test_forgetting_a_message_takes_its_file_with_it(self):
        env = envelope.read(self.with_pdf())
        ingest.store_message(self.con, self.folder, 1, env, attachments_root=self.root)
        stored = Path(self.con.execute("SELECT stored_path FROM attachment").fetchone()[0])
        ingest.forget_uids(self.con, self.folder, [1])
        self.assertFalse(stored.exists())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM attachment").fetchone()[0], 0)

    def test_a_file_already_gone_does_not_stop_the_sync(self):
        env = envelope.read(self.with_pdf())
        ingest.store_message(self.con, self.folder, 1, env, attachments_root=self.root)
        Path(self.con.execute("SELECT stored_path FROM attachment").fetchone()[0]).unlink()
        self.assertEqual(ingest.forget_uids(self.con, self.folder, [1]), 1)


class TestFlagsAndExpunge(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@manitlab.example", "google")
        self.folder = ensure_folder(self.con, self.account, "INBOX")
        for uid in (1, 2, 3):
            ingest.store_message(self.con, self.folder, uid,
                                 envelope.read(message(subject=f"m{uid}")))

    def test_the_servers_flags_are_applied(self):
        changed = ingest.update_flags(self.con, self.folder,
                                      {1: ["\\Seen"], 2: ["\\Flagged"]})
        self.assertEqual(changed, 2)
        rows = {r["uid"]: r for r in self.con.execute("SELECT * FROM message")}
        self.assertEqual(rows[1]["seen"], 1)
        self.assertEqual(rows[2]["flagged"], 1)
        self.assertEqual(rows[3]["seen"], 0)

    def test_applying_the_same_flags_twice_changes_nothing(self):
        ingest.update_flags(self.con, self.folder, {1: ["\\Seen"]})
        self.assertEqual(ingest.update_flags(self.con, self.folder, {1: ["\\Seen"]}), 0)

    def test_clearing_a_flag_is_applied_too(self):
        ingest.update_flags(self.con, self.folder, {1: ["\\Seen"]})
        ingest.update_flags(self.con, self.folder, {1: []})
        self.assertEqual(self.con.execute(
            "SELECT seen FROM message WHERE uid = 1").fetchone()[0], 0)

    def test_a_flag_for_an_unknown_uid_invents_nothing(self):
        ingest.update_flags(self.con, self.folder, {999: ["\\Seen"]})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 3)

    def test_what_the_store_holds_is_reported_for_comparison(self):
        self.assertEqual(ingest.uids_in(self.con, self.folder), {1, 2, 3})
        ingest.update_flags(self.con, self.folder, {2: ["\\Seen", "\\Flagged"]})
        local = ingest.local_flags(self.con, self.folder)
        self.assertEqual(local[2], frozenset({"\\seen", "\\flagged"}))
        self.assertEqual(local[1], frozenset())

    def test_expunged_uids_are_removed(self):
        self.assertEqual(ingest.forget_uids(self.con, self.folder, [1, 3]), 2)
        self.assertEqual(ingest.uids_in(self.con, self.folder), {2})

    def test_forgetting_nothing_is_not_an_error(self):
        self.assertEqual(ingest.forget_uids(self.con, self.folder, []), 0)
        self.assertEqual(ingest.forget_uids(self.con, self.folder, [999]), 0)


class TestSearchIndex(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@manitlab.example", "google")
        self.folder = ensure_folder(self.con, self.account, "INBOX")

    def hits(self, query: str) -> list[int]:
        return [r[0] for r in self.con.execute(
            "SELECT rowid FROM message_fts WHERE message_fts MATCH ?", (query,))]

    def test_a_stored_message_is_searchable(self):
        env = envelope.read(message(subject="Quarterly figures",
                                    body="The turnover was up."))
        got = ingest.store_message(self.con, self.folder, 1, env)
        self.assertEqual(self.hits("quarterly"), [got.message_id])
        self.assertEqual(self.hits("turnover"), [got.message_id])
        self.assertEqual(self.hits("priya"), [got.message_id])

    def test_a_forgotten_message_leaves_no_phantom(self):
        # An external-content index cannot delete what it does not store: the
        # old values are the delete command's arguments. Skip that and search
        # returns a row the reader cannot open.
        got = ingest.store_message(self.con, self.folder, 1,
                                   envelope.read(message(subject="Quarterly figures")))
        ingest.forget_uids(self.con, self.folder, [1])
        self.assertEqual(self.hits("quarterly"), [])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM message_fts").fetchone()[0], 0)

    def test_re_ingesting_does_not_index_the_message_twice(self):
        env = envelope.read(message(subject="Quarterly figures"))
        got = ingest.store_message(self.con, self.folder, 1, env)
        ingest.store_message(self.con, self.folder, 1, env)
        self.assertEqual(self.hits("quarterly"), [got.message_id])

    def test_a_changed_subject_does_not_leave_the_old_one_findable(self):
        ingest.store_message(self.con, self.folder, 1,
                             envelope.read(message(subject="Quarterly figures")))
        ingest.store_message(self.con, self.folder, 1,
                             envelope.read(message(subject="Annual figures")))
        self.assertEqual(self.hits("quarterly"), [])
        self.assertEqual(len(self.hits("annual")), 1)

    def test_forgetting_an_unindexed_row_does_not_corrupt_the_index(self):
        # FTS5 does not refuse a delete for a rowid it never had: it subtracts
        # the terms anyway, and the NEXT read fails with "database disk image is
        # malformed". Any writer that makes a message row without indexing it
        # leaves that landmine, so the guard belongs here rather than in each
        # of them.
        indexed = ingest.store_message(self.con, self.folder, 1,
                                       envelope.read(message(subject="Indexed one")))
        self.con.execute("INSERT INTO message (folder_id, uid, subject) "
                         "VALUES (?, 2, 'never indexed')", (self.folder,))
        self.con.commit()
        ingest.forget_uids(self.con, self.folder, [2])
        # The index must still be readable, and must still hold the other one.
        self.assertEqual(self.hits("indexed"), [indexed.message_id])
        self.assertEqual(self.con.execute(
            "INSERT INTO message_fts (message_fts) VALUES ('integrity-check')"
        ).fetchall(), [])

    def test_the_demo_fixtures_are_searchable(self):
        # They write message rows directly, so they index them too — both
        # because search should work over demo data and because an unindexed
        # row is the landmine above.
        from cormani.store import fixtures
        con = support.temp_store(self)
        fixtures.install(con)
        messages = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        indexed = con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]
        self.assertEqual(indexed, messages)

    def test_the_index_can_be_made_again_from_the_messages(self):
        for uid in (1, 2, 3):
            ingest.store_message(self.con, self.folder, uid,
                                 envelope.read(message(subject=f"Report {uid}")))
        # A contentless FTS5 table has no rows to DELETE FROM; `delete-all` is
        # the command that empties one.
        self.con.execute("INSERT INTO message_fts (message_fts) VALUES ('delete-all')")
        self.con.commit()
        self.assertEqual(self.hits("report"), [])
        self.assertEqual(ingest.rebuild_search_index(self.con), 3)
        self.assertEqual(len(self.hits("report")), 3)


if __name__ == "__main__":
    unittest.main()
