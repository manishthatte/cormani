# SPDX-License-Identifier: GPL-3.0-or-later
#
# The search box, and everything between it and the store.
#
# THE LESSON THIS FILE IS WRITTEN AROUND. The command bar above the reading pane
# reached nothing for two whole stages: `Reader.command` was emitted and
# connected nowhere, and it survived because the bar HAD tests — they asserted
# which buttons were enabled, which is a different question from whether
# pressing one does anything. So the tests here that matter are the ones that
# start at a widget's signal and end at the rows in the model. A test that
# drives the widget's own methods proves the widget; only a test that crosses
# the wiring proves the wiring.
#
# There is no QTest in Debian, so nothing here synthesises a key press. Enter is
# `returnPressed.emit()` and a menu entry is `QAction.trigger()` — the signal
# the real key would produce, from the place Qt would produce it.
#
# The corpus comes from test_search.py, which is on the import path for the same
# reason `support` is; see tests/__init__.py.
#
# © Manish Jagdish Thatte
import unittest

from cormani.store import accounts as accounts_repo, views
from cormani.store import messages, search

import support
from test_search import Corpus


@support.requires_qt
class BarCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.searchbar import SearchBar

        self.corpus = Corpus(self)
        self.con = self.corpus.con
        self.bar = support.own(self, SearchBar(self.con))
        self.seen = []
        self.bar.changed.connect(self.seen.append)


# ------------------------------------------------------------------ the widget
class TestTheBarItself(BarCase):
    def test_the_box_and_every_chip_reach_the_query(self):
        self.bar.text.setText("wavelength")
        self.bar.sender_chip.editor.setText("lyle")
        self.bar.subject_chip.editor.setText("dwcnt")
        self.bar.attachment_chip.setChecked(True)
        self.bar.date_chip.set_value("7d")
        self.bar.account_chip.set_value(self.corpus.other)
        self.bar.discarded_chip.setChecked(True)
        self.assertEqual(
            self.bar.query,
            search.Query(text="wavelength", sender="lyle", subject="dwcnt",
                         attachment=True, within="7d",
                         account_id=self.corpus.other, discarded=True))

    def test_typing_does_not_search_and_enter_does(self):
        # The index matches whole terms, so a search on every keystroke shows
        # "no results" for every prefix of the word being typed.
        self.bar.text.setText("wavel")
        self.assertEqual(self.seen, [])
        self.bar.text.setText("wavelength")
        self.assertEqual(self.seen, [])
        self.bar.text.returnPressed.emit()
        self.assertEqual([q.text for q in self.seen], ["wavelength"])

    def test_emptying_the_box_acts_at_once(self):
        # The clear button Qt draws inside the box has to do something.
        self.bar.text.setText("wavelength")
        self.bar.text.returnPressed.emit()
        self.bar.text.setText("")
        self.assertEqual(len(self.seen), 2)
        self.assertFalse(self.seen[-1].active)

    def test_emptying_the_box_with_a_chip_still_on_searches_by_the_chip(self):
        # Not "go back to the mailbox": one control was cleared, not both.
        self.bar.sender_chip.editor.setText("lyle")
        self.bar.sender_chip._commit()
        self.bar.text.setText("wavelength")
        self.bar.text.returnPressed.emit()
        self.bar.text.setText("")
        self.assertTrue(self.seen[-1].active)
        self.assertEqual((self.seen[-1].text, self.seen[-1].sender), ("", "lyle"))

    def test_pressing_a_chip_searches_the_text_already_typed(self):
        # The two controls are in front of the user at the same time; a chip
        # that ignored the box would be answering a question nobody asked.
        self.bar.text.setText("wavelength")
        self.bar.attachment_chip.setChecked(True)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual((self.seen[0].text, self.seen[0].attachment),
                         ("wavelength", True))

    def test_a_committed_text_chip_emits_once_and_relabels(self):
        self.bar.sender_chip.editor.setText("lyle")
        self.bar.sender_chip._commit()
        self.assertEqual([q.sender for q in self.seen], ["lyle"])
        self.assertIn("lyle", self.bar.sender_chip.text())
        self.assertTrue(self.bar.sender_chip.font().bold())

    def test_a_chips_label_follows_its_editor_rather_than_its_commit(self):
        # `query` reads the editor live, so a label that only changed on Enter
        # would say "From" while the next search ran with a sender in it.
        self.bar.sender_chip.editor.setText("lyle")
        self.assertIn("lyle", self.bar.sender_chip.text())
        self.assertEqual(self.seen, [])          # and typing there searches nothing
        self.assertEqual(self.bar.query.sender, "lyle")

    def test_a_menu_chip_names_its_choice_and_forgets_it_again(self):
        entries = {a.text(): a for a in self.bar.date_chip._menu.actions()}
        entries["Last 7 days"].trigger()
        self.assertEqual(self.bar.date_chip.text(), "Last 7 days")
        self.assertEqual(self.seen[-1].within, "7d")
        entries["Any time"].trigger()
        self.assertEqual(self.bar.date_chip.text(), "Date")
        self.assertEqual(self.seen[-1].within, "")

    def test_setting_the_query_restores_the_controls_without_searching(self):
        query = search.Query(text="wavelength", sender="lyle", within="30d",
                             account_id=self.corpus.other, discarded=True)
        self.bar.set_query(query)
        self.assertEqual(self.seen, [])
        self.assertEqual(self.bar.query, query)
        self.assertTrue(self.bar.clear_button.isEnabled())

    def test_clearing_gives_up_the_search_once(self):
        self.bar.text.setText("wavelength")
        self.bar.text.returnPressed.emit()
        self.seen.clear()
        self.bar.clear()
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(self.seen[0].active)
        self.assertEqual(self.bar.query, search.Query())
        # And again on an empty bar: nothing to give up, nothing emitted.
        self.bar.clear()
        self.assertEqual(len(self.seen), 1)

    def test_escape_in_the_box_is_the_same_as_clearing(self):
        self.bar.text.setText("wavelength")
        self.bar.text.returnPressed.emit()
        self.seen.clear()
        self.bar.text.escaped.emit()
        self.assertFalse(self.seen[-1].active)
        self.assertEqual(self.bar.text.text(), "")

    def test_the_account_chip_lists_the_accounts_and_can_be_told_to_look_again(self):
        labels = [a.text() for a in self.bar.account_chip._menu.actions()]
        self.assertEqual(labels[0], "All accounts")
        self.assertIn("manitlab", labels)
        accounts_repo.add_account(self.con, "third@example.com", "google",
                                  display_name="Third")
        self.bar.reload_accounts()
        self.assertIn("Third",
                      [a.text() for a in self.bar.account_chip._menu.actions()])

    def test_a_hidden_account_is_not_offered(self):
        accounts_repo.set_hidden(self.con, self.corpus.other, True)
        self.bar.reload_accounts()
        self.assertNotIn("Krishna",
                         [a.text() for a in self.bar.account_chip._menu.actions()])


# ------------------------------------------------- from the signal to the store
@support.requires_qt
class TestTheBarReachesTheStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.window import MainWindow

        self.corpus = Corpus(self)
        self.con = self.corpus.con
        self.window = support.own(self, MainWindow(self.con, demo=False))
        self.mail = self.window.mail
        self.bar = self.window.search

    def search_for(self, text):
        self.bar.text.setText(text)
        self.bar.text.returnPressed.emit()

    def ids(self):
        return [r.id for r in self.mail.model.rows()]

    # ---------------------------------------------------------------- the wire
    def test_pressing_enter_in_the_box_changes_what_the_list_shows(self):
        before = self.ids()
        self.search_for("wavelength")
        self.assertNotEqual(self.ids(), before)
        self.assertIn(self.corpus.old, self.ids())          # another folder
        self.assertIn(self.corpus.hers, self.ids())         # another account

    def test_the_rows_carry_their_snippet_and_their_location(self):
        self.search_for("quantity")
        row = self.mail.model.rows()[0]
        self.assertIn("quantity", row.snippet)
        self.assertEqual(row.location, "Inbox · manitlab")
        self.assertTrue(self.mail.list.delegate.show_location)

    def test_the_location_is_drawn_only_while_a_search_is_showing(self):
        self.assertFalse(self.mail.list.delegate.show_location)
        self.search_for("wavelength")
        self.assertTrue(self.mail.list.delegate.show_location)
        self.bar.clear()
        self.assertFalse(self.mail.list.delegate.show_location)

    def test_a_search_that_finds_nothing_says_what_it_looked_for(self):
        self.search_for("aardvark")
        self.assertEqual(self.mail.model.rowCount(), 0)
        self.assertIn("aardvark", self.mail.list._empty_text)

    def test_the_footer_names_what_the_search_left_in_trash_and_junk(self):
        # The exclusion is a decision; hiding it would be the defect.
        self.search_for("wavelength")
        footer = self.mail.list_footer.text()
        self.assertIn("found", footer)
        self.assertIn("1 more in Trash or Junk", footer)
        self.bar.discarded_chip.setChecked(True)
        self.assertNotIn("Trash or Junk", self.mail.list_footer.text())

    def test_the_footer_is_asked_again_when_a_result_is_deleted(self):
        # Deleting a hit moves it into the very folders that number is about,
        # so the answer the footer is holding stops being true at that moment.
        self.search_for("wavelength")
        self.assertIn("1 more in Trash or Junk", self.mail.list_footer.text())
        self.mail.run_action("delete", [self.corpus.old])
        self.assertIn("2 more in Trash or Junk", self.mail.list_footer.text())
        self.assertNotIn(self.corpus.old, self.ids())

    def test_nothing_is_selected_in_a_fresh_set_of_results(self):
        # Selecting marks read, and a search must not mark anything read.
        self.search_for("wavelength")
        self.assertIsNone(self.mail.current_row())
        unread = self.con.execute(
            "SELECT COUNT(*) FROM message WHERE seen = 1").fetchone()[0]
        self.assertEqual(unread, 0)

    # -------------------------------------------------------------- the order
    def test_a_search_is_shown_best_first_and_gives_the_order_back(self):
        self.assertEqual(self.mail.model.sort.key, "date")
        self.search_for("wavelength")
        self.assertEqual(self.mail.model.sort.key, views.SORT_RELEVANCE)
        self.bar.clear()
        self.assertEqual(self.mail.model.sort.key, "date")

    def test_an_order_chosen_during_a_search_is_not_overridden(self):
        self.search_for("wavelength")
        self.mail.set_sort(views.Sort(key="sender", descending=False))
        self.search_for("wavelength question")
        self.assertEqual(self.mail.model.sort.key, "sender")

    def test_chips_alone_are_shown_by_date_because_nothing_was_ranked(self):
        self.bar.date_chip.set_value("30d")
        self.bar._emit()
        self.assertTrue(self.mail.model.search.active)
        self.assertEqual(self.mail.model.sort.key, "date")

    def test_the_relevance_menu_entry_is_live_only_while_something_is_ranked(self):
        entry = self.window.sort_actions[views.SORT_RELEVANCE]
        self.assertFalse(entry.isEnabled())
        self.search_for("wavelength")
        self.assertTrue(entry.isEnabled())
        self.assertTrue(entry.isChecked())
        self.bar.clear()
        self.assertFalse(entry.isEnabled())
        self.assertTrue(self.window.sort_actions["date"].isChecked())

    # ------------------------------------------------------- leaving a search
    def test_choosing_a_folder_in_the_rail_ends_the_search(self):
        self.search_for("wavelength")
        self.mail._scope_chosen(views.Scope(kind="account", role="archive",
                                               account_id=self.corpus.mine),
                                "account:1:archive")
        self.assertFalse(self.mail.model.search.active)
        self.assertEqual(self.ids(), [self.corpus.old])

    def test_and_the_box_is_told_rather_than_left_showing_a_dead_query(self):
        self.search_for("wavelength")
        self.mail._scope_chosen(views.Scope(kind="account", role="archive",
                                               account_id=self.corpus.mine),
                                "account:1:archive")
        self.assertEqual(self.bar.text.text(), "")
        self.assertEqual(self.bar.query, search.Query())

    def test_the_shortcut_that_focuses_the_box_is_live(self):
        # `hasFocus` is false in a window that was never shown — there is no
        # active window offscreen — so the assertion is on the window's own
        # focus widget, which is what Qt actually sets. Ctrl+F ARRIVING is Qt's
        # shortcut map and is not exercised here; the action being enabled and
        # connected is.
        self.assertTrue(self.window.act_search.isEnabled())
        self.window.act_search.trigger()
        self.assertIs(self.window.focusWidget(), self.bar.text)

    # ------------------------------------------------------------------ tabs
    def test_a_tab_remembers_its_search_and_the_row_it_came_from(self):
        self.search_for("wavelength")
        self.window._new_tab()
        self.window.tabs.setCurrentIndex(1)
        self.bar.clear()
        self.assertFalse(self.mail.model.search.active)

        self.window.tabs.setCurrentIndex(0)
        self.assertTrue(self.mail.model.search.active)
        self.assertEqual(self.mail.model.search.text, "wavelength")
        self.assertEqual(self.bar.text.text(), "wavelength")
        self.assertIn(self.corpus.old, self.ids())

    def test_the_tab_is_named_for_the_search_rather_than_the_folder_under_it(self):
        self.search_for("wavelength")
        self.assertEqual(self.window.tabs.tabText(0), "Search: wavelength")
        self.bar.clear()
        self.assertNotIn("Search", self.window.tabs.tabText(0))

    def test_clearing_a_search_puts_the_tab_back_where_it_was(self):
        # The scope is kept beside the search rather than replaced by it, which
        # is what makes this one line rather than a remembered stack.
        before = self.ids()
        self.search_for("wavelength")
        self.bar.clear()
        self.assertEqual(self.ids(), before)


if __name__ == "__main__":
    unittest.main()
