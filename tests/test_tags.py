# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tags: the store's half, and the dialog that edits them.
#
# The tags themselves have worked since stage 1 — five ship with the store and
# the keys 1-5 apply them. What arrived with item 18 is everything after the
# first week: a sixth tag, a rename, a colour that is not one of the five, and
# the key that has to come off one tag before it can go on another.
#
# THE KEY IS THE INTERESTING CASE. The column is UNIQUE, so assigning a key
# another tag holds either fails or moves it, and moving it silently is a change
# nobody was told about. The dialog says which tag lost it; the test asserts
# that it says so, not merely that the key moved.
#
# There is no QTest in Debian, so the colour picker and the confirmation are
# injected functions rather than clicks — the same arrangement the attachment
# strip uses, and for the same reason.
#
# © Manish Jagdish Thatte
import unittest

import support
from test_threads import Store

from cormani.store import tags


class TestTheStoresHalf(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)

    def names(self):
        return [t.name for t in tags.list_tags(self.con)]

    def test_the_five_thunderbird_ships_are_already_there(self):
        # Seeded by migration 3 rather than at first run: a first-run code path
        # executes once and is therefore tested once.
        self.assertEqual(self.names(),
                         ["Important", "Work", "Personal", "To Do", "Later"])
        self.assertEqual([t.shortcut for t in tags.list_tags(self.con)],
                         [1, 2, 3, 4, 5])

    def test_a_new_tag_gets_a_name_no_other_tag_has(self):
        first = tags.add_tag(self.con, tags.unused_name(self.con), "#93a1a1")
        second = tags.add_tag(self.con, tags.unused_name(self.con), "#93a1a1")
        self.assertEqual(tags.get_tag(self.con, first).name, "New tag")
        self.assertEqual(tags.get_tag(self.con, second).name, "New tag 2")

    def test_renaming_and_recolouring(self):
        tag = tags.list_tags(self.con)[0]
        tags.update_tag(self.con, tag.id, name="Urgent", colour="#ff0000")
        fresh = tags.get_tag(self.con, tag.id)
        self.assertEqual((fresh.name, fresh.colour), ("Urgent", "#ff0000"))
        self.assertEqual(fresh.shortcut, tag.shortcut)      # untouched

    def test_a_key_moves_from_the_tag_that_had_it_and_says_which(self):
        work = tags.by_shortcut(self.con, 2)
        later = tags.by_shortcut(self.con, 5)
        displaced = tags.update_tag(self.con, later.id, shortcut=2)
        self.assertEqual(displaced, work.name)
        self.assertEqual(tags.by_shortcut(self.con, 2).id, later.id)
        self.assertIsNone(tags.get_tag(self.con, work.id).shortcut)
        self.assertIsNone(tags.by_shortcut(self.con, 5))

    def test_clearing_a_key_is_asked_for_explicitly(self):
        tag = tags.by_shortcut(self.con, 1)
        # `shortcut=None` means "leave it alone" for every other field here, so
        # it cannot also mean "clear it".
        tags.update_tag(self.con, tag.id, name="Important still")
        self.assertEqual(tags.get_tag(self.con, tag.id).shortcut, 1)
        tags.update_tag(self.con, tag.id, clear_shortcut=True)
        self.assertIsNone(tags.get_tag(self.con, tag.id).shortcut)

    def test_deleting_a_tag_unfiles_it_and_keeps_the_mail(self):
        fixture = Store(self)
        message = fixture.store(subject="Wavelengths", message_id="<a@x>")
        tag = tags.list_tags(fixture.con)[0]
        tags.set_on_messages(fixture.con, [message], tag.id, True)
        self.assertEqual(tags.message_counts(fixture.con)[tag.id], 1)

        tags.delete_tag(fixture.con, tag.id)
        self.assertEqual(tags.message_counts(fixture.con), {})
        self.assertEqual(
            fixture.con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 1)


@support.requires_qt
class TestTheDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.tagsdialog import TagsDialog

        self.con = support.temp_store(self)
        self.asked = []
        self.colour = "#123456"
        self.answer = True
        self.dialog = support.own(self, TagsDialog(
            self.con, ask_colour=lambda parent, current: self.colour,
            confirm=self._confirm))
        self.changes = []
        self.dialog.changed.connect(lambda: self.changes.append(1))

    def _confirm(self, parent, title, text):
        self.asked.append(text)
        return self.answer

    def select(self, name):
        for row in range(self.dialog.list.count()):
            item = self.dialog.list.item(row)
            if item.text().startswith(name):
                self.dialog.list.setCurrentRow(row)
                return item
        self.fail(f"no tag called {name}")

    def test_it_lists_every_tag_with_its_key(self):
        self.assertEqual(self.dialog.list.count(), 5)
        self.assertIn("Important", self.dialog.list.item(0).text())
        self.assertIn("1", self.dialog.list.item(0).text())

    def test_selecting_one_fills_the_fields(self):
        self.select("Work")
        self.assertEqual(self.dialog.name.text(), "Work")
        self.assertEqual(self.dialog.key.currentData(), 2)

    def test_adding_makes_one_and_selects_it(self):
        tag_id = self.dialog.add()
        self.assertEqual(self.dialog.list.count(), 6)
        self.assertEqual(self.dialog.current_id(), tag_id)
        self.assertEqual(self.dialog.name.text(), "New tag")
        self.assertTrue(self.changes)

    def test_renaming_writes_it(self):
        self.select("Later")
        self.dialog.name.setText("Someday")
        self.dialog._rename()
        self.assertEqual(tags.get_tag(self.con, self.dialog.current_id()).name,
                         "Someday")
        self.assertIn("Someday", self.dialog.list.currentItem().text())

    def test_a_name_another_tag_has_is_refused_and_said_so(self):
        self.select("Later")
        self.dialog.name.setText("Work")
        self.dialog._rename()
        self.assertEqual(self.dialog.name.text(), "Later")
        self.assertIn("already a tag", self.dialog.note.text())
        self.assertEqual([t.name for t in tags.list_tags(self.con)].count("Work"), 1)

    def test_the_colour_comes_from_the_picker_and_is_written(self):
        self.select("Work")
        self.dialog._pick_colour()
        self.assertEqual(tags.get_tag(self.con, self.dialog.current_id()).colour,
                         "#123456")

    def test_a_picker_that_was_cancelled_changes_nothing(self):
        self.select("Work")
        before = tags.get_tag(self.con, self.dialog.current_id()).colour
        self.colour = ""
        self.dialog._pick_colour()
        self.assertEqual(tags.get_tag(self.con, self.dialog.current_id()).colour,
                         before)

    def test_taking_a_key_says_which_tag_lost_it(self):
        self.select("Later")                     # key 5
        self.dialog.key.setCurrentIndex(list(self.dialog.key.itemData(n)
                                             for n in range(self.dialog.key.count())
                                             ).index(2))
        self.assertEqual(tags.by_shortcut(self.con, 2).name, "Later")
        self.assertIn("Work", self.dialog.note.text())
        self.assertIn("no longer has a key", self.dialog.note.text())

    def test_deleting_asks_first_and_says_how_many_carry_it(self):
        self.select("Work")
        self.answer = False
        self.assertFalse(self.dialog.delete())
        self.assertEqual(self.dialog.list.count(), 5)
        self.assertIn("0 messages carry it", self.asked[0])

        self.answer = True
        self.assertTrue(self.dialog.delete())
        self.assertEqual(self.dialog.list.count(), 4)
        self.assertIsNone(tags.by_shortcut(self.con, 2))


@support.requires_qt
class TestTheTagsMenu(unittest.TestCase):
    """From the menu to the store — the wiring, not the widget."""

    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.window import MainWindow

        self.fixture = Store(self)
        self.con = self.fixture.con
        self.message = self.fixture.store(subject="Wavelengths",
                                          message_id="<a@x>")
        self.window = support.own(self, MainWindow(self.con, demo=False))

    def entries(self):
        return [a.text() for a in self.window.tags_menu.actions()]

    def test_it_lists_the_tags_and_the_way_to_manage_them(self):
        self.window._build_tags_menu()
        self.assertIn("Important\t1", self.entries())
        self.assertIn("&Manage tags…", self.entries())

    def test_choosing_one_tags_the_selection_and_can_be_undone(self):
        self.window.mail.select_message(self.message)
        self.window._build_tags_menu()
        [a for a in self.window.tags_menu.actions()
         if a.text().startswith("Work")][0].trigger()

        row = self.window.mail.model.row_at(
            self.window.mail.model.index_of(self.message))
        self.assertEqual([t.name for t in row.tags], ["Work"])
        self.window.mail.undo()
        row = self.window.mail.model.row_at(
            self.window.mail.model.index_of(self.message))
        self.assertEqual(row.tags, ())

    def test_a_tag_with_no_key_is_still_reachable(self):
        tag_id = tags.add_tag(self.con, "Keyless", "#93a1a1")
        self.window.mail.select_message(self.message)
        self.window._build_tags_menu()
        self.assertIn("Keyless", self.entries())
        self.window.mail.apply_tag(tag_id)
        row = self.window.mail.model.row_at(
            self.window.mail.model.index_of(self.message))
        self.assertEqual([t.name for t in row.tags], ["Keyless"])

    def test_the_menu_follows_a_rename(self):
        tag = tags.by_shortcut(self.con, 1)
        tags.update_tag(self.con, tag.id, name="Urgent")
        self.window._build_tags_menu()
        self.assertIn("Urgent\t1", self.entries())
        self.assertNotIn("Important\t1", self.entries())


if __name__ == "__main__":
    unittest.main()
