# SPDX-License-Identifier: GPL-3.0-or-later
#
# Reading an mbox without writing it, and landing the messages in the store.
#
# The assertions are about three things the prototype already proved and that
# must not be lost in the port: the source file's bytes are unchanged after an
# import; a second pass over an unchanged file imports nothing; a file that
# grew is resumed from the old offset rather than re-read.
#
# © Manish Jagdish Thatte
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

# Two messages, mboxrd. The second body's `From ` is escaped as `>From `.
_MBOX = (
    b"From me@example.org Mon Aug 25 10:00:00 2026\n"
    b"From: Alice <alice@example.org>\n"
    b"To: you@example.org\n"
    b"Subject: First\n"
    b"Message-ID: <one@example.org>\n"
    b"Date: Mon, 25 Aug 2026 10:00:00 +0000\n"
    b"X-Mozilla-Status: 0001\n"
    b"\n"
    b"Hello.\n"
    b"\n"
    b"From me@example.org Mon Aug 25 11:00:00 2026\n"
    b"From: Bob <bob@example.org>\n"
    b"To: you@example.org\n"
    b"Subject: Second\n"
    b"Message-ID: <two@example.org>\n"
    b"Date: Mon, 25 Aug 2026 11:00:00 +0000\n"
    b"X-Mozilla-Status: 0000\n"
    b"\n"
    b">From the top: still Bob.\n"
    b"\n"
)


class TestMboxReader(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "INBOX"
        self.path.write_bytes(_MBOX)

    def test_it_yields_each_message_without_the_separator(self):
        from cormani.importer import mbox
        messages = list(mbox.iter_mbox(self.path))
        self.assertEqual(len(messages), 2)
        start, end, raw = messages[0]
        self.assertLess(start, end)
        self.assertFalse(raw.startswith(b"From "))
        self.assertIn(b"Subject: First", raw)
        self.assertIn(b"From the top: still Bob.", messages[1][2],
                      "mboxrd >From was not unescaped")
        self.assertNotIn(b">From the top", messages[1][2])

    def test_it_does_not_write_the_file(self):
        from cormani.importer import mbox
        before = self.path.read_bytes()
        list(mbox.iter_mbox(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_resume_offset_is_a_separator(self):
        from cormani.importer import mbox
        messages = list(mbox.iter_mbox(self.path))
        # The end of message 0 is the start of message 1's From line.
        self.assertTrue(mbox.is_separator_at(self.path, messages[0][1]))
        self.assertEqual(messages[0][1], messages[1][0])
        self.assertFalse(mbox.is_separator_at(self.path, messages[1][0] + 1))


class TestDiscover(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        imap = self.root / "ImapMail" / "imap.example.com"
        imap.mkdir(parents=True)
        (imap / "INBOX").write_bytes(_MBOX)
        (imap / "INBOX.msf").write_text("summary")
        (imap / "Trash").write_bytes(_MBOX)
        sbd = imap / "Lists.sbd"
        sbd.mkdir()
        (sbd / "debian").write_bytes(_MBOX)

    def test_it_finds_mbox_files_and_skips_summaries(self):
        from cormani.importer import discover
        found = list(discover.folder_files([self.root / "ImapMail"]))
        labels = sorted(label for _, _, label in found)
        self.assertEqual(labels, ["INBOX", "Lists/debian"])

    def test_include_junk_keeps_trash(self):
        from cormani.importer import discover
        found = list(discover.folder_files([self.root / "ImapMail"],
                                           include_junk=True))
        labels = {label for _, _, label in found}
        self.assertIn("Trash", labels)

    def test_role_for_common_labels(self):
        from cormani.importer import discover
        from cormani.store import folders as folders_repo
        self.assertEqual(discover.role_for("INBOX"), folders_repo.ROLE_INBOX)
        self.assertEqual(discover.role_for("Sent Mail"), folders_repo.ROLE_SENT)
        self.assertEqual(discover.role_for("Lists/debian"), "")


class TestImportRun(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        from cormani.store.accounts import add_account
        self.account_id = add_account(self.con, "you@example.org", "imap")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mbox_path = Path(self._tmp.name) / "INBOX"
        self.mbox_path.write_bytes(_MBOX)

    def test_it_stores_messages_under_a_local_folder(self):
        from cormani.importer import run
        from cormani.store import folders as folders_repo

        report = run(self.con, self.account_id, path=self.mbox_path)
        self.assertEqual(report.new, 2)
        self.assertEqual(report.folders, 1)
        rows = self.con.execute(
            "SELECT m.subject, m.seen, m.from_addr, f.path FROM message m "
            "JOIN folder f ON f.id = m.folder_id ORDER BY m.uid").fetchall()
        self.assertEqual([r["subject"] for r in rows], ["First", "Second"])
        self.assertTrue(folders_repo.is_local(rows[0]["path"]))
        self.assertIn("Thunderbird", rows[0]["path"])
        # First has X-Mozilla-Status 0001 (read); second is 0000 (unread).
        self.assertEqual([r["seen"] for r in rows], [1, 0])
        self.assertIn("From the top: still Bob.", self.con.execute(
            "SELECT body_text FROM message WHERE subject = 'Second'"
        ).fetchone()[0])

    def test_a_second_pass_over_an_unchanged_file_imports_nothing(self):
        from cormani.importer import run
        run(self.con, self.account_id, path=self.mbox_path)
        report = run(self.con, self.account_id, path=self.mbox_path)
        self.assertEqual(report.new, 0)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM message").fetchone()[0], 2)

    def test_appended_mail_is_picked_up_from_the_resume_offset(self):
        from cormani.importer import run
        run(self.con, self.account_id, path=self.mbox_path)
        extra = (
            b"From me@example.org Mon Aug 25 12:00:00 2026\n"
            b"From: Carol <carol@example.org>\n"
            b"To: you@example.org\n"
            b"Subject: Third\n"
            b"Message-ID: <three@example.org>\n"
            b"Date: Mon, 25 Aug 2026 12:00:00 +0000\n"
            b"\n"
            b"And another.\n"
            b"\n"
        )
        with self.mbox_path.open("ab") as fh:
            fh.write(extra)
        report = run(self.con, self.account_id, path=self.mbox_path)
        self.assertEqual(report.new, 1)
        subjects = [r[0] for r in self.con.execute(
            "SELECT subject FROM message ORDER BY uid")]
        self.assertEqual(subjects, ["First", "Second", "Third"])

    def test_the_source_bytes_are_unchanged(self):
        from cormani.importer import run
        before = self.mbox_path.read_bytes()
        run(self.con, self.account_id, path=self.mbox_path)
        self.assertEqual(self.mbox_path.read_bytes(), before)


class TestImportCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        support.fake_keyring(self)

        from cormani.app import current_paths
        from cormani.store import database
        from cormani.store.accounts import add_account

        paths = current_paths().ensure()
        self.con = database.open_store(paths.database)
        self.addCleanup(self.con.close)
        add_account(self.con, "you@example.org", "imap")
        self.mbox = root / "mail.mbox"
        self.mbox.write_bytes(_MBOX)

    def test_into_is_required(self):
        from cormani import importcli
        self.assertEqual(importcli.import_thunderbird(str(self.mbox)), 1)

    def test_it_imports_through_the_command(self):
        from cormani import importcli
        code = importcli.import_thunderbird(str(self.mbox),
                                            into="you@example.org")
        self.assertEqual(code, 0)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM message").fetchone()[0], 2)

    def test_the_parser_accepts_the_switch(self):
        from cormani.__main__ import build_parser
        args = build_parser().parse_args(
            ["--import-thunderbird", str(self.mbox),
             "--into", "you@example.org"])
        self.assertEqual(args.import_thunderbird, str(self.mbox))
        self.assertEqual(args.into, "you@example.org")


if __name__ == "__main__":
    unittest.main()
