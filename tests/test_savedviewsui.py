# SPDX-License-Identifier: GPL-3.0-or-later
#
# The saved-searches dialog, and the rail rows it manages.
#
# `tests/test_viewhost.py` is the menu items; this is the dialog they open and
# the rows the rail draws from the same table.
#
# A WIDGET IS NOT A FACT. `isVisible()` is False for every widget whose window
# has not been SHOWN and this suite shows none of them, so everything here asks
# the MODEL what a row means rather than the widget what it is doing.
# `ui/ruleeditor.ConditionRow` carries the same note.
#
# THE PROMPTS ARE INJECTED. Debian ships no QTest, so both the rename box and
# the delete confirmation are passed in.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import savedviews, search, views


@support.requires_qt
class SavedViewsUICase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        self.con = support.demo_store(self)
        self.asked = []
        self.answer = True
        self.new_name = "Renamed"

    def confirm(self, parent, title, text):
        self.asked.append(text)
        return self.answer

    def ask_name(self, parent, title, label, initial):
        return self.new_name

    def dialog(self):
        from cormani.ui.savedviewsdialog import SavedViewsDialog
        return support.own(self, SavedViewsDialog(
            self.con, confirm=self.confirm, ask_name=self.ask_name))

    def names(self):
        return [v.name for v in savedviews.list_views(self.con)]


class TestTheList(SavedViewsUICase):
    def test_it_lists_every_saved_search_including_the_hidden_ones(self):
        # The dialog is where a view kept out of the rail is put BACK, so a
        # dialog that only listed the rail's own would be the one place the
        # setting could not be undone.
        dialog = self.dialog()
        self.assertEqual(dialog.list.count(), len(self.names()))

    def test_a_row_says_what_the_view_asks_and_what_it_holds(self):
        dialog = self.dialog()
        text = dialog.list.item(0).text()
        self.assertIn(savedviews.list_views(self.con)[0].name, text)
        self.assertIn("right now", text)

    def test_the_tick_is_in_rail_and_writing_it_reaches_the_store(self):
        from PySide6.QtCore import Qt
        dialog = self.dialog()
        first = savedviews.list_views(self.con)[0]
        self.assertTrue(first.in_rail)
        item = dialog.list.item(0)
        item.setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(savedviews.get_view(self.con, first.id).in_rail)
        # And it is NOT deleted — out of the rail is not gone.
        self.assertIsNotNone(savedviews.get_view(self.con, first.id))

    def test_a_stale_view_is_shown_and_says_it_cannot_run(self):
        from PySide6.QtCore import Qt
        gone = savedviews.save_view(self.con, savedviews.SavedView(
            name="Tenders",
            scope=views.Scope(kind="folder", folder_id=999999)))
        dialog = self.dialog()
        rows = [dialog.list.item(i) for i in range(dialog.list.count())]
        row = [r for r in rows
               if r.data(Qt.ItemDataRole.UserRole) == gone.id][0]
        self.assertIn("CANNOT RUN", row.text())


class TestRenaming(SavedViewsUICase):
    def test_renaming_writes_through(self):
        dialog = self.dialog()
        first = savedviews.list_views(self.con)[0]
        dialog.list.setCurrentRow(0)
        dialog.rename()
        self.assertEqual(savedviews.get_view(self.con, first.id).name,
                         "Renamed")

    def test_renaming_onto_a_name_in_use_SAYS_so_and_changes_nothing(self):
        # The store refuses rather than choosing — `store/savedviews.py`'s
        # header — and this is where the refusal has to become a sentence
        # instead of an exception the user is standing in front of.
        views_now = savedviews.list_views(self.con)
        self.new_name = views_now[1].name
        dialog = self.dialog()
        dialog.list.setCurrentRow(0)
        dialog.rename()
        self.assertIn("already a saved search", dialog.note.text())
        self.assertEqual(savedviews.get_view(self.con, views_now[0].id).name,
                         views_now[0].name)


class TestDeleting(SavedViewsUICase):
    def test_deleting_asks_first_and_says_what_the_view_was_for(self):
        dialog = self.dialog()
        first = savedviews.list_views(self.con)[0]
        dialog.list.setCurrentRow(0)
        dialog.delete()
        self.assertEqual(len(self.asked), 1)
        self.assertIn(first.name, self.asked[0])
        self.assertIsNone(savedviews.get_view(self.con, first.id))

    def test_declining_keeps_it(self):
        self.answer = False
        dialog = self.dialog()
        first = savedviews.list_views(self.con)[0]
        dialog.list.setCurrentRow(0)
        dialog.delete()
        self.assertIsNotNone(savedviews.get_view(self.con, first.id))

    def test_deleting_a_search_touches_no_mail(self):
        # The confirmation says so, so the test says so.
        before = self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        dialog = self.dialog()
        dialog.list.setCurrentRow(0)
        dialog.delete()
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0],
            before)


class TestOrdering(SavedViewsUICase):
    def test_moving_one_down_writes_the_whole_order(self):
        before = self.names()
        dialog = self.dialog()
        dialog.list.setCurrentRow(0)
        dialog.down()
        after = self.names()
        self.assertEqual(after[0], before[1])
        self.assertEqual(after[1], before[0])

    def test_the_ends_refuse_rather_than_wrapping(self):
        before = self.names()
        dialog = self.dialog()
        dialog.list.setCurrentRow(0)
        dialog.up()
        self.assertEqual(self.names(), before)

    def test_the_buttons_are_off_at_the_ends(self):
        dialog = self.dialog()
        dialog.list.setCurrentRow(0)
        self.assertFalse(dialog.button_up.isEnabled())
        self.assertTrue(dialog.button_down.isEnabled())
        dialog.list.setCurrentRow(dialog.list.count() - 1)
        self.assertTrue(dialog.button_up.isEnabled())
        self.assertFalse(dialog.button_down.isEnabled())


@support.requires_qt
class TestTheRailSection(unittest.TestCase):
    """The rows, from the model rather than from the widget."""

    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        self.con = support.demo_store(self)

    def model(self):
        from cormani.ui.models.rail import RailModel
        return RailModel(self.con)

    def rows(self, model):
        section = model.section_index("saved")
        return [model.index(r, 0, section)
                for r in range(model.rowCount(section))]

    def test_the_section_holds_the_views_that_are_in_the_rail(self):
        from cormani.ui.models import rail as rail_model
        drawn = savedviews.list_views(self.con, rail_only=True)
        rows = self.rows(self.model())
        self.assertEqual([r.data() for r in rows], [v.name for v in drawn])
        for row in rows:
            self.assertEqual(row.data(rail_model.KindRole), rail_model.SAVED)

    def test_a_view_kept_out_of_the_rail_is_NOT_drawn(self):
        savedviews.save_view(self.con, savedviews.SavedView(
            name="Hidden here", in_rail=False,
            filters=views.Filters(flagged=True)))
        self.assertNotIn("Hidden here",
                         [r.data() for r in self.rows(self.model())])

    def test_each_row_carries_its_view_id_and_its_key(self):
        from cormani.ui.models import rail as rail_model
        for row in self.rows(self.model()):
            view_id = row.data(rail_model.ViewIdRole)
            self.assertIsNotNone(view_id)
            self.assertEqual(row.data(rail_model.KeyRole),
                             rail_model.saved_key(view_id))

    def test_the_count_is_the_capped_one(self):
        from cormani.ui.models import rail as rail_model
        by_id = {v.id: v for v in savedviews.list_views(self.con)}
        for row in self.rows(self.model()):
            view = by_id[row.data(rail_model.ViewIdRole)]
            self.assertEqual(row.data(rail_model.CountRole),
                             savedviews.count_capped(self.con, view))

    def test_a_stale_view_is_drawn_QUIETLY_with_the_reason_in_its_tooltip(self):
        from PySide6.QtCore import Qt
        from cormani.ui.models import rail as rail_model
        savedviews.save_view(self.con, savedviews.SavedView(
            name="Tenders", scope=views.Scope(kind="folder",
                                              folder_id=999999)))
        row = [r for r in self.rows(self.model()) if r.data() == "Tenders"][0]
        self.assertTrue(row.data(rail_model.HiddenRole))
        self.assertIn("gone", row.data(Qt.ItemDataRole.ToolTipRole))

    def test_an_empty_section_offers_a_hint_that_cannot_be_selected(self):
        from PySide6.QtCore import Qt
        from cormani.ui.models import rail as rail_model
        con = support.temp_store(self)
        from cormani.ui.models.rail import RailModel
        model = RailModel(con)
        section = model.section_index("saved")
        self.assertEqual(model.rowCount(section), 1)
        hint = model.index(0, 0, section)
        self.assertEqual(hint.data(rail_model.KindRole), rail_model.HINT)
        self.assertFalse(model.flags(hint) & Qt.ItemFlag.ItemIsEnabled)

    def test_a_saved_row_is_selectable_and_a_section_is_not(self):
        from PySide6.QtCore import Qt
        model = self.model()
        row = self.rows(model)[0]
        self.assertTrue(model.flags(row) & Qt.ItemFlag.ItemIsSelectable)
        self.assertFalse(model.flags(model.section_index("saved"))
                         & Qt.ItemFlag.ItemIsSelectable)


if __name__ == "__main__":
    unittest.main()
