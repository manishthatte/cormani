# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the filter menu items MEAN, driven from a window.
#
# `tests/test_filtersui.py` is the two dialogs. This is the layer between them
# and the menu bar — `ui/filterhost.py` — and the three things worth asserting
# about it are the three ways it can be wrong without looking wrong:
#
#   - a menu item that quietly does nothing, because the view is not a folder
#     or there are no rules. Each of those has to SAY so.
#   - a bulk change carried out without asking. Running the rules again can
#     move two thousand messages, every move is queued for the server, and none
#     of it is undoable.
#   - the wrong messages. "Run filters on this folder" over a search result or
#     over the unanswered-mail view is how somebody's Sent mail ends up
#     archived by a rule written for their Inbox.
#
# THE CONFIRMATION IS INJECTED, as everywhere else in this tree: Debian ships
# no QTest and the real one is a modal.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import rulematch, rules, views
from cormani.store.folders import ROLE_ARCHIVE, ROLE_INBOX


@support.requires_qt
class FilterHostCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        """A store with UIDs in it, and not the demo one.

        DEMO MESSAGES HAVE `uid = NULL` on purpose — there is no server behind
        demo data — and `edits.set_flagged` queues nothing for a message the
        server has never heard of. A test of "what a run did is queued" over
        demo data would therefore assert zero and pass for the wrong reason.
        """
        from cormani.store.accounts import add_account
        from cormani.store.folders import ensure_folder
        from cormani.ui.window import MainWindow

        self.con = support.temp_store(self)
        self.account = add_account(self.con, "one@example.invalid", "imap")
        self.inbox = ensure_folder(self.con, self.account, "INBOX",
                                   role=ROLE_INBOX)
        self.archive = ensure_folder(self.con, self.account, "Archive",
                                     role=ROLE_ARCHIVE)
        for uid, subject in enumerate(("Invoice 41", "Invoice 42", "Lunch?",
                                       "Notes"), start=1):
            self.message(uid, subject)
        self.window = support.own(self, MainWindow(self.con, demo=False))
        self.asked = []
        self.answer = True

    def message(self, uid: int, subject: str, *, folder=None) -> int:
        cur = self.con.execute(
            "INSERT INTO message (folder_id, uid, message_id, subject, "
            "subject_base, from_addr, from_name, to_addrs, body_text, date_at, "
            "received_at, size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, '', 'body', "
            "?, ?, 2048)",
            (folder or self.inbox, uid, f"<{uid}@x.invalid>", subject, subject,
             "frances@covalent.example", "Frances Baker",
             "2026-08-25T10:00:00+00:00", "2026-08-25T10:00:00+00:00"))
        self.con.commit()
        return int(cur.lastrowid)

    def confirm(self, parent, title, text):
        self.asked.append(text)
        return self.answer

    def rule(self, name="archive the lot", **kw):
        return rules.save_rule(self.con, rulematch.Rule(
            name=name,
            conditions=kw.pop("conditions",
                              (rulematch.Condition("subject", "contains",
                                                    "invoice"),)),
            actions=kw.pop("actions",
                           (rulematch.Action(kind="flag"),)), **kw))

    def go_to_inbox(self):
        self.window.mail.model.set_query(
            scope=views.Scope(kind="unified", role=ROLE_INBOX))

    def go_to_folder(self):
        self.window.mail.model.set_query(
            scope=views.Scope(kind="folder", folder_id=self.inbox))
        return self.inbox


class TestWhichFoldersAreMeant(FilterHostCase):
    def test_one_folder_is_that_folder(self):
        from cormani.ui import filterhost
        folder_id = self.go_to_folder()
        self.assertEqual(filterhost.folders_in_view(self.window), [folder_id])

    def test_the_unified_inbox_is_every_visible_account_s_inbox(self):
        # The case worth supporting: it is where somebody is standing when
        # they decide their filters need running again.
        from cormani.ui import filterhost
        self.go_to_inbox()
        expected = {r[0] for r in self.con.execute(
            "SELECT id FROM folder WHERE role = ?", (ROLE_INBOX,))}
        self.assertTrue(expected)
        self.assertEqual(set(filterhost.folders_in_view(self.window)), expected)

    def test_a_search_is_not_a_folder(self):
        from cormani.ui import filterhost
        from cormani.store import search as search_mod
        self.go_to_inbox()
        self.window.mail.model.set_query(search=search_mod.Query(text="invoice"))
        self.assertEqual(filterhost.folders_in_view(self.window), [])

    def test_owed_is_not_a_folder_either(self):
        """Owed is a view OVER the Inbox, and resolving its role would run the
        rules over every message in every Inbox — which is not what the rail
        said was being looked at."""
        from cormani.ui import filterhost
        self.window.mail.model.set_query(
            scope=views.Scope(kind="unified", role=views.ROLE_OWED))
        self.assertEqual(filterhost.folders_in_view(self.window), [])


class TestRunningThemAgain(FilterHostCase):
    def test_with_no_rules_it_says_so_and_asks_nothing(self):
        from cormani.ui import filterhost
        self.go_to_folder()
        self.assertIsNone(filterhost.run_over_view(self.window,
                                                   confirm=self.confirm))
        self.assertEqual(self.asked, [])
        self.assertIn("no filter rules", self.window.status_message.text())

    def test_over_a_search_it_says_which_view_it_needs(self):
        from cormani.ui import filterhost
        from cormani.store import search as search_mod
        self.rule()
        self.go_to_inbox()
        self.window.mail.model.set_query(search=search_mod.Query(text="invoice"))
        self.assertIsNone(filterhost.run_over_view(self.window,
                                                   confirm=self.confirm))
        self.assertEqual(self.asked, [])
        self.assertIn("Choose a folder", self.window.status_message.text())

    def test_it_asks_before_it_changes_anything_and_says_what_is_at_stake(self):
        from cormani.ui import filterhost
        self.rule()
        self.go_to_folder()
        self.answer = False
        self.assertIsNone(filterhost.run_over_view(self.window,
                                                   confirm=self.confirm))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("1 rule", self.asked[0])
        self.assertIn("4 messages", self.asked[0])
        self.assertIn("cannot be undone", self.asked[0])
        self.assertIn("sent to the server", self.asked[0])
        # And saying no changed nothing at all.
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM message WHERE flagged = 1").fetchone()[0], 0)

    def test_saying_yes_runs_the_rules_and_reports_what_happened(self):
        from cormani.ui import filterhost
        self.rule(actions=(rulematch.Action(kind="flag"),))
        folder_id = self.go_to_folder()
        report = filterhost.run_over_view(self.window, confirm=self.confirm)
        self.assertIsNotNone(report)
        self.assertEqual(report.considered, 4)      # everything in the folder
        self.assertEqual(report.matched, 2)         # the two invoices
        flagged = [r[0] for r in self.con.execute(
            "SELECT subject FROM message WHERE flagged = 1 AND folder_id = ?",
            (folder_id,))]
        self.assertEqual(sorted(flagged), ["Invoice 41", "Invoice 42"])
        self.assertIn("matched a rule", self.window.status_message.text())

    def test_what_a_run_did_is_queued_for_the_server(self):
        # The whole correctness argument for filters in an IMAP client: a
        # change made locally and not queued is a change the next sync undoes.
        from cormani.ui import filterhost
        self.rule(actions=(rulematch.Action(kind="flag"),))
        self.go_to_folder()
        report = filterhost.run_over_view(self.window, confirm=self.confirm)
        queued = self.con.execute(
            "SELECT COUNT(*) FROM pending_op WHERE kind = 'flag'").fetchone()[0]
        self.assertEqual(queued, report.matched)

    def test_a_rule_that_cannot_be_carried_out_is_reported_not_hidden(self):
        """A run finishes rather than stopping, so what it could NOT do has to
        reach the person who asked — or the rules look as though they worked."""
        from cormani.ui import filterhost
        self.con.execute("UPDATE folder SET role = '' WHERE role = ?",
                         (ROLE_ARCHIVE,))
        self.con.commit()
        self.rule(actions=(rulematch.Action(kind="move", value=ROLE_ARCHIVE),))
        self.go_to_folder()
        report = filterhost.run_over_view(self.window, confirm=self.confirm)
        self.assertTrue(report.problems)
        self.assertIn("no Archive folder", self.window.status_message.text())


class TestStartingOneFromAMessage(FilterHostCase):
    def open_a_message(self) -> int:
        message_id = self.con.execute(
            "SELECT id FROM message ORDER BY id LIMIT 1").fetchone()[0]
        self.assertTrue(self.window.mail.select_message(message_id))
        self.assertEqual(self.window.mail.reader.message_id(), message_id)
        return message_id

    def test_with_nothing_open_it_says_to_open_something(self):
        from cormani.ui import filterhost
        self.assertFalse(filterhost.filter_from_message(self.window))
        self.assertIn("Open a message first",
                      self.window.status_message.text())

    def test_the_rule_it_starts_is_about_the_sender(self):
        # Every client offers this and the useful answer is nearly always the
        # address: a subject repeats across one conversation and then never
        # again.
        from cormani.ui import filterhost
        from cormani.ui import ruleeditor
        from cormani.store import messages as messages_repo
        message_id = self.open_a_message()
        row = messages_repo.get_row(self.con, message_id)
        started = ruleeditor.from_message(row)
        self.assertEqual(started.conditions[0].field, "from")
        self.assertEqual(started.conditions[0].value, row.from_addr)
        self.assertEqual(started.name, row.from_addr)
        self.assertEqual(started.actions, ())     # what to DO is theirs to say

    def test_it_opens_an_editor_on_that_rule_and_reports_the_save(self):
        from cormani.ui import filterhost
        message_id = self.open_a_message()
        seen = {}

        class FakeEditor:
            def __init__(self, con, rule, parent=None):
                seen["rule"] = rule
                self.saved = rules.save_rule(con, rule.with_changes(
                    actions=(rulematch.Action(kind="flag"),)))

            def exec(self):
                return 1

        self.assertTrue(filterhost.filter_from_message(self.window,
                                                       editor=FakeEditor))
        self.assertEqual(seen["rule"].conditions[0].field, "from")
        self.assertEqual(len(rules.list_rules(self.con)), 1)
        self.assertIn("will run on the next sync",
                      self.window.status_message.text())

class TestTheMenuReachesIt(FilterHostCase):
    """The menu entries are CONNECTED, which is a different question from
    whether they exist.

    The command bar over the reading pane survived two stages emitting a signal
    that was connected to nothing, and the site panels shipped a start-up crash
    down a path no test had ever taken. Both were wiring, and wiring is only
    proved from one end to the other. So this finds the actions by their text
    and TRIGGERS one — the one that is safe to trigger, because with no message
    open it does nothing but say so, and saying so is the proof that the click
    arrived at `ui/filterhost.py`.
    """

    def tools_menu(self):
        for menu in self.window.menuBar().findChildren(type(
                self.window.menuBar().addMenu("x"))):
            if menu.title().replace("&", "") == "Tools":
                return menu
        self.fail("there is no Tools menu")

    def test_the_tools_menu_holds_the_three_filter_entries(self):
        labels = [a.text().replace("&", "") for a in self.tools_menu().actions()
                  if not a.isSeparator()]
        self.assertIn("Message filters…", labels)
        self.assertIn("Create a filter from this message…", labels)
        self.assertIn("Run filters on this folder", labels)

    def test_creating_a_filter_from_nothing_reaches_filterhost(self):
        for action in self.tools_menu().actions():
            if action.text().replace("&", "").startswith("Create a filter"):
                action.trigger()
                self.assertIn("Open a message first",
                              self.window.status_message.text())
                return
        self.fail("no entry for creating a filter from a message")



if __name__ == "__main__":
    unittest.main()
