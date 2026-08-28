# SPDX-License-Identifier: GPL-3.0-or-later
#
# Folder discovery, and which folder plays which role.
#
# The role is what makes "archive this" work across fifteen accounts whose
# archive folder has fifteen different names, so a wrong one files mail
# somewhere the user does not look.
#
# © Manish Jagdish Thatte
import unittest

import support
from fakeimap import IMAP4_Fake, Server

from cormani.imap import folders as sync
from cormani.imap.client import Connection
from cormani.imap.parse import Mailbox
from cormani.store import folders as repo
from cormani.store.accounts import add_account


def box(path, *attributes, delimiter="/"):
    return Mailbox(attributes=tuple(attributes), delimiter=delimiter,
                   path=path, display_name=path)


class TestRoleAssignment(unittest.TestCase):
    def test_inbox_is_decided_by_name_which_is_a_rule(self):
        # RFC 3501: the name INBOX is case-insensitive and always means this.
        for name in ("INBOX", "Inbox", "inbox"):
            roles = sync.assign_roles([box(name)])
            self.assertEqual(roles[name], repo.ROLE_INBOX, name)

    def test_the_declared_attributes_win(self):
        roles = sync.assign_roles([
            box("INBOX"),
            box("Sent Messages", "\\HasNoChildren", "\\Sent"),
            box("Bin", "\\Trash"),
            box("Rubbish", "\\Junk"),
            box("Entwürfe", "\\Drafts"),
        ])
        self.assertEqual(roles["Sent Messages"], repo.ROLE_SENT)
        self.assertEqual(roles["Bin"], repo.ROLE_TRASH)
        self.assertEqual(roles["Rubbish"], repo.ROLE_JUNK)
        self.assertEqual(roles["Entwürfe"], repo.ROLE_DRAFTS)

    def test_gmail_all_mail_becomes_the_archive(self):
        # Gmail declares \All and has no \Archive at all, so archiving on a
        # Gmail account has to mean All Mail.
        roles = sync.assign_roles([
            box("INBOX"),
            box("[Gmail]/All Mail", "\\All"),
            box("[Gmail]/Sent Mail", "\\Sent"),
            box("[Gmail]/Trash", "\\Trash"),
        ])
        self.assertEqual(roles["[Gmail]/All Mail"], repo.ROLE_ARCHIVE)

    def test_a_real_archive_beats_all_mail(self):
        # Fastmail offers both. The declared \Archive must win, and that is
        # only decidable once every mailbox has been seen.
        roles = sync.assign_roles([
            box("INBOX"),
            box("All Mail", "\\All"),
            box("Archive", "\\Archive"),
        ])
        self.assertEqual(roles["Archive"], repo.ROLE_ARCHIVE)
        self.assertNotIn("All Mail", roles)

    def test_the_listing_order_does_not_change_the_answer(self):
        boxes = [box("All Mail", "\\All"), box("Archive", "\\Archive"),
                 box("INBOX")]
        self.assertEqual(sync.assign_roles(boxes)["Archive"], repo.ROLE_ARCHIVE)
        self.assertEqual(sync.assign_roles(list(reversed(boxes)))["Archive"],
                         repo.ROLE_ARCHIVE)

    def test_names_are_a_last_resort_for_servers_declaring_nothing(self):
        roles = sync.assign_roles([
            box("INBOX"), box("Sent Items"), box("Drafts"),
            box("Deleted Items"), box("Junk E-mail"), box("Archive"),
        ])
        self.assertEqual(roles["Sent Items"], repo.ROLE_SENT)
        self.assertEqual(roles["Drafts"], repo.ROLE_DRAFTS)
        self.assertEqual(roles["Deleted Items"], repo.ROLE_TRASH)
        self.assertEqual(roles["Junk E-mail"], repo.ROLE_JUNK)
        self.assertEqual(roles["Archive"], repo.ROLE_ARCHIVE)

    def test_a_declared_role_is_not_displaced_by_a_similar_name(self):
        # A folder genuinely called "Archive of 2019" must not become THE
        # archive when the server has already said which one is.
        roles = sync.assign_roles([
            box("INBOX"),
            box("[Gmail]/All Mail", "\\All"),
            box("Archive"),
        ])
        self.assertEqual(roles["[Gmail]/All Mail"], repo.ROLE_ARCHIVE)
        self.assertNotIn("Archive", roles)

    def test_only_the_last_segment_is_matched(self):
        roles = sync.assign_roles([box("INBOX"), box("Work/2026/Archive")])
        self.assertEqual(roles["Work/2026/Archive"], repo.ROLE_ARCHIVE)

    def test_an_unknown_name_gets_no_role_rather_than_a_guess(self):
        roles = sync.assign_roles([box("INBOX"), box("Lists/debian-devel")])
        self.assertNotIn("Lists/debian-devel", roles)

    def test_a_noselect_container_cannot_take_a_role(self):
        roles = sync.assign_roles([box("INBOX"), box("Archive", "\\Noselect")])
        self.assertNotIn("Archive", roles)

    def test_two_folders_cannot_share_a_role(self):
        roles = sync.assign_roles([box("INBOX"), box("Sent", "\\Sent"),
                                   box("Sent Items", "\\Sent")])
        self.assertEqual(sum(1 for r in roles.values() if r == repo.ROLE_SENT), 1)


class TestFolderSync(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@manitlab.example", "google")
        self.server = Server()
        self.server.passwords["owner@manitlab.example"] = "pw"

    def connect(self):
        conn = Connection.connect("x", factory=lambda: IMAP4_Fake(self.server))
        conn.login("owner@manitlab.example", "pw")
        return conn

    def gmail(self):
        self.server.add_mailbox("INBOX", attributes=("\\HasNoChildren",))
        self.server.add_mailbox("[Gmail]", attributes=("\\Noselect", "\\HasChildren"))
        self.server.add_mailbox("[Gmail]/All Mail", attributes=("\\All",))
        self.server.add_mailbox("[Gmail]/Sent Mail", attributes=("\\Sent",))
        self.server.add_mailbox("[Gmail]/Trash", attributes=("\\Trash",))
        self.server.add_mailbox("[Gmail]/Drafts", attributes=("\\Drafts",))
        self.server.add_mailbox("[Gmail]/Spam", attributes=("\\Junk",))

    def paths(self, subscribed_only=False):
        return {f.path: f for f in repo.list_folders(self.con, self.account,
                                                     subscribed_only=subscribed_only)}

    def test_a_gmail_account_gets_every_role(self):
        self.gmail()
        report = sync.sync_folders(self.con, self.connect(), self.account)
        self.assertEqual(len(report.added), 6, "the \\Noselect container is skipped")
        stored = self.paths()
        self.assertEqual(stored["INBOX"].role, repo.ROLE_INBOX)
        self.assertEqual(stored["[Gmail]/All Mail"].role, repo.ROLE_ARCHIVE)
        self.assertEqual(stored["[Gmail]/Sent Mail"].role, repo.ROLE_SENT)
        for role in repo.ROLES:
            self.assertIsNotNone(repo.by_role(self.con, self.account, role), role)

    def test_the_stored_path_is_the_servers_and_the_label_is_readable(self):
        self.gmail()
        sync.sync_folders(self.con, self.connect(), self.account)
        folder = self.paths()["[Gmail]/Sent Mail"]
        self.assertEqual(folder.path, "[Gmail]/Sent Mail")
        self.assertEqual(folder.display_name, "Sent Mail")

    def test_running_it_twice_changes_nothing(self):
        self.gmail()
        sync.sync_folders(self.con, self.connect(), self.account)
        report = sync.sync_folders(self.con, self.connect(), self.account)
        self.assertEqual(report.added, ())
        self.assertEqual(report.updated, ())
        self.assertEqual(len(self.paths()), 6)

    def test_nesting_follows_the_servers_delimiter(self):
        self.server.delimiter = "."
        self.server.add_mailbox("INBOX")
        self.server.add_mailbox("INBOX.Lists")
        self.server.add_mailbox("INBOX.Lists.debian")
        sync.sync_folders(self.con, self.connect(), self.account)
        stored = self.paths()
        self.assertEqual(stored["INBOX.Lists"].parent_id, stored["INBOX"].id)
        self.assertEqual(stored["INBOX.Lists.debian"].parent_id,
                         stored["INBOX.Lists"].id)
        self.assertIsNone(stored["INBOX"].parent_id)
        self.assertEqual(stored["INBOX.Lists.debian"].label, "debian")

    def test_a_new_folder_is_added_without_disturbing_the_others(self):
        self.gmail()
        sync.sync_folders(self.con, self.connect(), self.account)
        inbox_id = self.paths()["INBOX"].id
        repo.record_sync_state(self.con, inbox_id, uid_validity=42, uid_next=9)
        self.server.add_mailbox("Work")
        report = sync.sync_folders(self.con, self.connect(), self.account)
        self.assertEqual(report.added, ("Work",))
        self.assertEqual(repo.sync_state(self.con, inbox_id)["uid_next"], 9,
                         "an existing folder's sync state must survive")

    def test_a_vanished_folder_is_unsubscribed_and_reported_not_deleted(self):
        # Removing it cascades to every message in it, and the local store is
        # the user's archive. A rename would silently destroy mail.
        self.gmail()
        conn = self.connect()
        sync.sync_folders(self.con, conn, self.account)
        folder_id = self.paths()["[Gmail]/Spam"].id
        self.con.execute("INSERT INTO message (folder_id, uid) VALUES (?, 1)",
                         (folder_id,))
        self.con.commit()
        del self.server.mailboxes["[Gmail]/Spam"]
        report = sync.sync_folders(self.con, self.connect(), self.account)
        self.assertEqual(report.vanished, ("[Gmail]/Spam",))
        self.assertIn("[Gmail]/Spam", self.paths())
        self.assertFalse(self.paths()["[Gmail]/Spam"].subscribed)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM message").fetchone()[0], 1, "mail must survive")

    def test_a_renamed_role_folder_is_picked_up(self):
        self.server.add_mailbox("INBOX")
        self.server.add_mailbox("Sent", attributes=("\\Sent",))
        sync.sync_folders(self.con, self.connect(), self.account)
        self.assertEqual(repo.by_role(self.con, self.account, repo.ROLE_SENT).path,
                         "Sent")
        self.server.mailboxes["Sent"].attributes = ()
        self.server.add_mailbox("Sent Mail", attributes=("\\Sent",))
        report = sync.sync_folders(self.con, self.connect(), self.account)
        self.assertIn("Sent", report.updated)
        self.assertEqual(repo.by_role(self.con, self.account, repo.ROLE_SENT).path,
                         "Sent Mail")

    def test_an_unsubscribed_folder_is_recorded_as_such(self):
        self.server.add_mailbox("INBOX")
        self.server.add_mailbox("Noise", subscribed=False)
        sync.sync_folders(self.con, self.connect(), self.account)
        self.assertFalse(self.paths()["Noise"].subscribed)
        self.assertTrue(self.paths()["INBOX"].subscribed)
        self.assertNotIn("Noise", self.paths(subscribed_only=True))


class TestUidValidity(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@manitlab.example", "google")
        self.folder = repo.ensure_folder(self.con, self.account, "INBOX",
                                         role=repo.ROLE_INBOX)
        for uid in (1, 2, 3):
            self.con.execute("INSERT INTO message (folder_id, uid, subject) "
                             "VALUES (?, ?, ?)", (self.folder, uid, f"m{uid}"))
        self.con.commit()

    def messages(self):
        return self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0]

    def test_the_first_sight_of_a_value_records_it(self):
        self.assertEqual(sync.check_uid_validity(self.con, self.folder, 1000), 0)
        self.assertEqual(repo.sync_state(self.con, self.folder)["uid_validity"], 1000)
        self.assertEqual(self.messages(), 3)

    def test_an_unchanged_value_does_nothing(self):
        sync.check_uid_validity(self.con, self.folder, 1000)
        self.assertEqual(sync.check_uid_validity(self.con, self.folder, 1000), 0)
        self.assertEqual(self.messages(), 3)

    def test_a_changed_value_discards_the_cache(self):
        # The server has declared every stored UID meaningless. Keeping the
        # rows leaves messages that can never be matched to it again.
        sync.check_uid_validity(self.con, self.folder, 1000)
        self.assertEqual(sync.check_uid_validity(self.con, self.folder, 2000), 3)
        self.assertEqual(self.messages(), 0)
        state = repo.sync_state(self.con, self.folder)
        self.assertEqual(state["uid_validity"], 2000)
        self.assertIsNone(state["uid_next"], "the next sync starts from the top")

    def test_a_missing_value_is_not_a_change(self):
        # Absence is not a new value, and discarding a folder's mail because a
        # response was short is the worst reading of missing information.
        sync.check_uid_validity(self.con, self.folder, 1000)
        self.assertEqual(sync.check_uid_validity(self.con, self.folder, None), 0)
        self.assertEqual(self.messages(), 3)


if __name__ == "__main__":
    unittest.main()
