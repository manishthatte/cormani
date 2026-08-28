# SPDX-License-Identifier: GPL-3.0-or-later
#
# The connection, against a real IMAP server running in this process.
#
# Everything below imaplib is fakeimap.Server; imaplib itself is genuine, so
# these tests exercise the command construction, literal handling and response
# parsing that actually run.
#
# © Manish Jagdish Thatte
import unittest

import fakeimap
from fakeimap import IMAP4_Fake, Server

from cormani.imap import errors
from cormani.imap.client import Connection

BODY = (b"From: Priya Raman <priya@example.org>\r\n"
        b"To: owner@manitlab.example\r\n"
        b"Subject: Quarterly figures\r\n"
        b"Date: Tue, 25 Aug 2026 10:00:00 +0000\r\n\r\n"
        b"Numbers attached.\r\n")


def server(**kwargs) -> Server:
    s = Server(**kwargs)
    s.passwords["owner@manitlab.example"] = "app-password"
    s.tokens.add("ya29.good")
    s.add_mailbox("INBOX", attributes=("\\HasNoChildren",))
    s.add_mailbox("[Gmail]/All Mail", attributes=("\\HasNoChildren", "\\All"))
    s.add_mailbox("[Gmail]/Trash", attributes=("\\HasNoChildren", "\\Trash"))
    return s


def connected(s: Server) -> Connection:
    return Connection.connect("fake", factory=lambda: IMAP4_Fake(s))


def logged_in(s: Server) -> Connection:
    conn = connected(s)
    conn.login("owner@manitlab.example", "app-password")
    return conn


class TestAuthentication(unittest.TestCase):
    def test_an_app_password(self):
        s = server()
        conn = logged_in(s)
        self.assertEqual(s.user, "owner@manitlab.example")
        conn.logout()

    def test_a_wrong_password_is_permanent_not_retried(self):
        # Retrying a rejected password is how fifteen accounts get an address
        # blocked.
        s = server()
        conn = connected(s)
        with self.assertRaises(errors.AuthFailed) as caught:
            conn.login("owner@manitlab.example", "wrong")
        self.assertFalse(errors.is_transient(caught.exception))

    def test_xoauth2_with_a_good_token(self):
        s = server()
        conn = connected(s)
        conn.authenticate_xoauth2("owner@manitlab.example", "ya29.good")
        self.assertEqual(s.user, "owner@manitlab.example")

    def test_xoauth2_with_a_stale_token_fails_without_hanging(self):
        # The server sends a base64 challenge and waits for a line before the
        # tagged NO. A client that does not send it hangs forever.
        s = server()
        conn = connected(s)
        with self.assertRaises(errors.AuthFailed):
            conn.authenticate_xoauth2("owner@manitlab.example", "ya29.expired")

    def test_the_token_never_appears_in_the_error(self):
        s = server()
        conn = connected(s)
        try:
            conn.authenticate_xoauth2("owner@manitlab.example", "ya29.SECRET")
        except errors.AuthFailed as exc:
            self.assertNotIn("SECRET", errors.describe(exc))
            self.assertNotIn("SECRET", str(exc))

    def test_capabilities_are_read_again_after_authenticating(self):
        # Gmail withholds CONDSTORE and MOVE until after login. A client that
        # trusts the greeting syncs the slow way forever.
        s = server(capabilities=("IMAP4rev1", "AUTH=XOAUTH2"))
        conn = connected(s)
        self.assertFalse(conn.has("MOVE"))
        s.capabilities.extend(["MOVE", "CONDSTORE", "IDLE"])
        conn.login("owner@manitlab.example", "app-password")
        self.assertTrue(conn.has("MOVE"))
        self.assertTrue(conn.has("CONDSTORE"))

    def test_a_rate_limit_is_transient_not_a_bad_password(self):
        # "Too many simultaneous connections" often also says "failed".
        s = server()
        s.fail_next["LOGIN"] = "NO [LIMIT] Too many simultaneous connections"
        conn = connected(s)
        with self.assertRaises(errors.RateLimited) as caught:
            conn.login("owner@manitlab.example", "app-password")
        self.assertTrue(errors.is_transient(caught.exception))


class TestFolders(unittest.TestCase):
    def test_listing(self):
        s = server()
        conn = logged_in(s)
        paths = {box.path for box in conn.list_mailboxes()}
        self.assertEqual(paths, {"INBOX", "[Gmail]/All Mail", "[Gmail]/Trash"})

    def test_special_use_attributes_survive(self):
        s = server()
        conn = logged_in(s)
        by_path = {b.path: b for b in conn.list_mailboxes()}
        self.assertIn("\\All", by_path["[Gmail]/All Mail"].attributes)

    def test_a_bracketed_name_is_quoted_and_selects(self):
        # imaplib quotes an argument with a space but not one with a bracket,
        # and `[Gmail]/All Mail` has both.
        s = server()
        s.add_message("[Gmail]/All Mail", BODY)
        conn = logged_in(s)
        state = conn.select("[Gmail]/All Mail")
        self.assertEqual(state.exists, 1)
        self.assertEqual(s.selected, "[Gmail]/All Mail")

    def test_select_reports_the_numbers_the_sync_turns_on(self):
        s = server()
        for _ in range(3):
            s.add_message("INBOX", BODY)
        conn = logged_in(s)
        state = conn.select("INBOX")
        self.assertEqual(state.exists, 3)
        self.assertEqual(state.uid_validity, 1000)
        self.assertEqual(state.uid_next, 4)
        self.assertIsNotNone(state.highest_modseq)
        self.assertFalse(state.readonly)

    def test_a_missing_folder_is_permanent_but_only_for_that_folder(self):
        s = server()
        conn = logged_in(s)
        with self.assertRaises(errors.MailboxGone):
            conn.select("No/Such/Folder")

    def test_leaving_a_folder_does_not_expunge_it(self):
        # CLOSE silently erases every message flagged \Deleted, including ones
        # another client flagged and has not yet erased.
        s = server()
        uid = s.add_message("INBOX", BODY, flags=["\\Deleted"])
        conn = logged_in(s)
        conn.select("INBOX")
        conn.close_folder()
        self.assertIsNotNone(s.mailboxes["INBOX"].by_uid(uid),
                             "leaving a folder must not erase mail")
        self.assertEqual(s.commands_matching(r"\bCLOSE\b"), [])


class TestFetching(unittest.TestCase):
    def setUp(self):
        self.s = server()
        self.uids = [self.s.add_message("INBOX", BODY.replace(b"Quarterly",
                                                              f"Report{n}".encode()))
                     for n in range(5)]
        self.conn = logged_in(self.s)
        self.conn.select("INBOX")

    def test_searching(self):
        self.assertEqual(self.conn.search_uids("ALL"), self.uids)

    def test_bodies_come_back_whole(self):
        from cormani.imap.client import BODY_ITEMS
        got = self.conn.fetch(self.uids, BODY_ITEMS)
        self.assertEqual(len(got), 5)
        self.assertTrue(all(f.body and f.body.endswith(b"Numbers attached.\r\n")
                            for f in got))
        self.assertEqual([f.uid for f in got], self.uids)

    def test_a_body_fetch_does_not_mark_mail_read(self):
        # BODY[] sets \Seen as a side effect. The difference is six characters
        # and it is the most destructive bug a mail client can have.
        from cormani.imap.client import BODY_ITEMS
        self.conn.fetch(self.uids, BODY_ITEMS)
        for uid in self.uids:
            self.assertNotIn("\\Seen", self.s.mailboxes["INBOX"].by_uid(uid).flags,
                             f"uid {uid} was marked read by being downloaded")
        self.assertEqual(self.s.commands_matching(r"BODY\[\](?!PEEK)"), [])

    def test_uids_are_collapsed_into_ranges(self):
        # One per comma builds a command line servers refuse.
        self.conn.fetch(self.uids, "(UID FLAGS)")
        sent = self.s.commands_matching(r"UID FETCH")[-1]
        self.assertIn("1:5", sent)

    def test_fetching_nothing_sends_no_command(self):
        before = len(self.s.log)
        self.assertEqual(self.conn.fetch([], "(UID FLAGS)"), [])
        self.assertEqual(len(self.s.log), before)

    def test_flags_only(self):
        self.s.set_flags("INBOX", self.uids[0], ["\\Seen", "\\Flagged"])
        got = {f.uid: f.flags for f in self.conn.fetch_flags()}
        self.assertEqual(set(got[self.uids[0]]), {"\\Seen", "\\Flagged"})
        self.assertEqual(got[self.uids[1]], ())
        self.assertTrue(all(f.body is None for f in self.conn.fetch_flags()))

    def test_condstore_asks_only_for_what_changed(self):
        before = self.s.mailboxes["INBOX"].highest_modseq
        self.s.set_flags("INBOX", self.uids[2], ["\\Seen"])
        changed = self.conn.fetch_flags(changed_since=before)
        self.assertEqual([f.uid for f in changed], [self.uids[2]])
        self.assertIsNotNone(changed[0].modseq)


class TestMutations(unittest.TestCase):
    def setUp(self):
        self.s = server()
        self.uid = self.s.add_message("INBOX", BODY)
        self.conn = logged_in(self.s)
        self.conn.select("INBOX")

    def flags(self, path="INBOX", uid=None):
        return self.s.mailboxes[path].by_uid(uid or self.uid).flags

    def test_setting_and_clearing_a_flag(self):
        self.conn.store_flags([self.uid], add=["\\Seen"])
        self.assertIn("\\Seen", self.flags())
        self.conn.store_flags([self.uid], remove=["\\Seen"])
        self.assertNotIn("\\Seen", self.flags())

    def test_the_echo_is_suppressed(self):
        # On a thousand messages the untagged echoes are a thousand lines this
        # client does not read.
        self.conn.store_flags([self.uid], add=["\\Seen"])
        self.assertIn("SILENT", self.s.commands_matching(r"UID STORE")[-1].upper())

    def test_move_uses_the_one_command_when_offered(self):
        self.conn.move([self.uid], "[Gmail]/Trash")
        self.assertIsNone(self.s.mailboxes["INBOX"].by_uid(self.uid))
        self.assertEqual(len(self.s.mailboxes["[Gmail]/Trash"].messages), 1)
        self.assertTrue(self.s.commands_matching(r"UID MOVE"))

    def test_move_falls_back_to_copy_then_delete_in_that_order(self):
        # Copy first: a failure between the steps leaves the message in BOTH
        # folders, which a person can fix. The other order loses mail.
        s = server(capabilities=("IMAP4rev1", "UIDPLUS", "IDLE"))
        uid = s.add_message("INBOX", BODY)
        conn = logged_in(s)
        conn.select("INBOX")
        conn.move([uid], "[Gmail]/Trash")
        self.assertEqual(len(s.mailboxes["[Gmail]/Trash"].messages), 1)
        self.assertIsNone(s.mailboxes["INBOX"].by_uid(uid))
        order = [c for c in s.log if "UID COPY" in c or "UID STORE" in c
                 or "UID EXPUNGE" in c]
        self.assertIn("COPY", order[0])
        self.assertIn("STORE", order[1])
        self.assertIn("EXPUNGE", order[2])

    def test_expunge_names_the_uids_rather_than_erasing_the_folder(self):
        # A bare EXPUNGE removes every \Deleted message, including ones another
        # client flagged and has not yet erased.
        other = self.s.add_message("INBOX", BODY, flags=["\\Deleted"])
        self.conn.store_flags([self.uid], add=["\\Deleted"])
        self.conn.expunge_uids([self.uid])
        self.assertIsNone(self.s.mailboxes["INBOX"].by_uid(self.uid))
        self.assertIsNotNone(self.s.mailboxes["INBOX"].by_uid(other),
                             "another client's deletion must not be carried out")

    def test_without_uidplus_nothing_is_erased_at_all(self):
        s = server(capabilities=("IMAP4rev1", "IDLE"))
        uid = s.add_message("INBOX", BODY, flags=["\\Deleted"])
        conn = logged_in(s)
        conn.select("INBOX")
        conn.expunge_uids([uid])
        self.assertIsNotNone(s.mailboxes["INBOX"].by_uid(uid),
                             "doing nothing beats erasing someone else's message")


class TestIdle(unittest.TestCase):
    def test_a_message_arriving_during_the_wait_is_reported(self):
        s = server()
        conn = logged_in(s)
        conn.select("INBOX")
        s.push("INBOX", BODY)
        lines = conn.idle(seconds=1)
        self.assertTrue(any("EXISTS" in line for line in lines), lines)

    def test_done_is_always_sent(self):
        # A client that forgets DONE leaves the server holding a connection
        # that will never answer another command.
        s = server()
        conn = logged_in(s)
        conn.select("INBOX")
        conn.idle(seconds=1)
        self.assertIn("DONE", s.log)
        self.assertFalse(s.idling)

    def test_the_connection_still_works_afterwards(self):
        s = server()
        s.add_message("INBOX", BODY)
        conn = logged_in(s)
        conn.select("INBOX")
        conn.idle(seconds=1)
        self.assertEqual(len(conn.search_uids("ALL")), 1)

    def test_a_server_without_idle_says_so_rather_than_hanging(self):
        s = server(capabilities=("IMAP4rev1",))
        conn = logged_in(s)
        conn.select("INBOX")
        with self.assertRaises(errors.Transient):
            conn.idle(seconds=1)


class TestFailures(unittest.TestCase):
    def test_a_dropped_connection_is_transient(self):
        s = server()
        conn = logged_in(s)
        s.drop_after = len(s.log)
        with self.assertRaises(errors.Transient):
            conn.select("INBOX")

    def test_a_refusal_carries_the_servers_own_words(self):
        s = server()
        s.fail_next["SELECT"] = "NO [SERVERBUG] the server is unwell"
        conn = logged_in(s)
        with self.assertRaises(errors.ImapError) as caught:
            conn.select("INBOX")
        self.assertIn("unwell", str(caught.exception))

    def test_logout_never_raises(self):
        s = server()
        conn = logged_in(s)
        s.drop_after = len(s.log)
        conn.logout()                                   # a failed goodbye is not
        conn.logout()                                   # a failure


if __name__ == "__main__":
    unittest.main()
