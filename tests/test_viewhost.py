# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the saved-search menu items MEAN, driven from a window.
#
# `tests/test_savedviews.py` is the store and `tests/test_savedviewsui.py` is
# the dialog. This is the layer between them and the menu bar —
# `ui/viewhost.py` — and the four things worth asserting about it are the four
# ways it can be wrong without looking wrong:
#
#   - OPENING ONE CLEARS ITS OWN SEARCH. The rail's ordinary path ENDS a
#     search, deliberately, and a saved search routed through it would empty
#     the very query it exists to run. Silent: the list simply shows the whole
#     folder, which is a plausible thing for a virtual folder to show.
#   - THE SAVED ORDER IS OVERRULED. `set_search` sorts a ranked query
#     best-first; a saved "oldest first" that came back as relevance would look
#     like an ordering preference not sticking.
#   - A MENU ITEM QUIETLY DOING NOTHING. Saving from a calendar tab, saving a
#     view of everything, cancelling — each has to SAY something.
#   - REPLACING A SAVED SEARCH WITHOUT ASKING. The name is UNIQUE, and
#     overwriting one somebody forgot they had is a loss they cannot see.
#
# THE PROMPTS ARE INJECTED, as everywhere else in this tree: Debian ships no
# QTest and both the name box and the confirmation are modals.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import savedviews, search, views
from cormani.store.folders import ROLE_ARCHIVE, ROLE_INBOX


@support.requires_qt
class ViewHostCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
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
        self.named = []
        self.answer = True
        self.name_to_give = "Invoices"

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

    def ask_name(self, parent, title, label, initial):
        self.named.append(initial)
        return self.name_to_give

    def narrow_the_view(self):
        """Put something on screen that is worth saving."""
        self.window.mail.set_search(search.Query(text="invoice"))

    def save(self, **kw):
        from cormani.ui import viewhost
        return viewhost.save_current(self.window, ask_name=self.ask_name,
                                     confirm=self.confirm, **kw)

    def status(self) -> str:
        return self.window.status_message.text()


class TestSaving(ViewHostCase):
    def test_saving_writes_what_is_on_screen(self):
        self.narrow_the_view()
        saved = self.save()
        self.assertIsNotNone(saved)
        # THE VALUE, not that a row appeared: a saved search that kept the
        # name and dropped the query is the failure worth catching.
        self.assertEqual(saved.name, "Invoices")
        self.assertEqual(saved.search.text, "invoice")

    def test_the_name_box_opens_on_what_was_typed(self):
        # A dialog opening on an empty field asks a question the screen has
        # already answered.
        self.narrow_the_view()
        self.save()
        self.assertEqual(self.named, ["invoice"])

    def test_a_view_of_everything_is_refused_and_says_why(self):
        # Nothing narrowed: this is the Inbox row under a second name.
        self.assertIsNone(self.save())
        self.assertIn("nothing to save", self.status())
        self.assertEqual(savedviews.list_views(self.con), [])

    def test_cancelling_the_name_box_writes_nothing(self):
        self.narrow_the_view()
        self.name_to_give = ""
        self.assertIsNone(self.save())
        self.assertEqual(savedviews.list_views(self.con), [])

    def test_saving_from_a_pane_that_is_not_mail_is_refused_and_says_why(self):
        # The TRACKING pane and not the calendar: `show_calendar` goes through
        # the rail, so with no calendars configured it does nothing at all and
        # the test would pass for the wrong reason — it would be asserting
        # against the plain inbox. `show_tracking` needs no rail row.
        self.window.mail.show_tracking()
        self.assertTrue(self.window.mail.showing_tracking())
        self.assertIsNone(self.save())
        self.assertIn("mail view", self.status())

    def test_a_clashing_name_ASKS_before_replacing(self):
        self.narrow_the_view()
        first = self.save()
        self.window.mail.set_search(search.Query(text="lunch"))
        second = self.save()
        self.assertEqual(len(self.asked), 1)
        self.assertIn("already a saved search", self.asked[0])
        # Replaced in place rather than duplicated.
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.search.text, "lunch")
        self.assertEqual(len(savedviews.list_views(self.con)), 1)

    def test_declining_the_replacement_leaves_the_first_one_ALONE(self):
        self.narrow_the_view()
        first = self.save()
        self.window.mail.set_search(search.Query(text="lunch"))
        self.answer = False
        self.assertIsNone(self.save())
        kept = savedviews.get_view(self.con, first.id)
        self.assertEqual(kept.search.text, "invoice")

    def test_a_saved_view_lands_in_the_rail(self):
        from cormani.ui.models import rail as rail_model
        self.narrow_the_view()
        saved = self.save()
        self.assertTrue(
            self.window.mail.rail._model.index_for_key(
                rail_model.saved_key(saved.id)).isValid())


class TestOpening(ViewHostCase):
    def saved(self, **changes):
        base = dict(name="Invoices", search=search.Query(text="invoice"))
        base.update(changes)
        return savedviews.save_view(self.con, savedviews.SavedView(**base))

    def test_opening_one_RUNS_its_search(self):
        # THE DEFECT THIS EXISTS FOR: routed through the rail's ordinary
        # `scope_chosen`, a saved search arrives with its query cleared and the
        # list shows the whole folder — which looks like a working virtual
        # folder that happens to be broad.
        from cormani.ui import viewhost
        view = self.saved()
        self.assertTrue(viewhost.apply_view(self.window.mail, view.id))
        self.assertEqual(self.window.mail.model.search.text, "invoice")
        self.assertTrue(self.window.mail.model.search.active)

    def test_selecting_it_in_the_rail_runs_it_too(self):
        # Through the rail rather than the host, because the wiring between the
        # two is the part that a unit test of either half cannot see.
        from cormani.ui.models import rail as rail_model
        view = self.saved()
        self.window.mail.rail.refresh_counts()
        self.assertTrue(self.window.mail.rail.select_key(
            rail_model.saved_key(view.id)))
        self.assertEqual(self.window.mail.model.search.text, "invoice")

    def test_the_saved_ORDER_arrives(self):
        from cormani.ui import viewhost
        view = self.saved(sort=views.Sort(key="date", descending=False))
        viewhost.apply_view(self.window.mail, view.id)
        self.assertEqual(self.window.mail.model.sort.key, "date")
        self.assertFalse(self.window.mail.model.sort.descending)

    def test_the_saved_ORDER_survives_the_NEXT_search_change(self):
        """The half the assertion above cannot see, and the one that breaks.

        `apply_view` writes the sort straight into the model, so the test above
        passes whether or not the pane was told the order was CHOSEN. What the
        flag governs is the NEXT call to `set_search`: a ranked query with the
        flag clear is re-sorted best-first, and the order the user saved lasts
        only until they touch the search box.

        THE SORT HERE IS `subject` DESCENDING, AND THE CHOICE IS THE TEST. The
        first version of this used "date, oldest first" and stayed GREEN with
        the defect reintroduced, because `descending=False` disagrees with the
        View menu's tick — so `_sync_sort_menu`, which every `view_changed`
        runs, called `act_descending.setChecked(False)`, that fired `toggled`,
        and the round trip through `_sort_direction` set the flag back by
        accident. A descending sort leaves the tick where it already is, fires
        nothing, and lets the assertion see what it is actually asserting.
        """
        from cormani.ui import viewhost
        view = self.saved(sort=views.Sort(key="subject", descending=True))
        viewhost.apply_view(self.window.mail, view.id)
        self.window.mail.set_search(search.Query(text="invoice 41"))
        self.assertEqual(self.window.mail.model.sort.key, "subject")

    def test_the_saved_THREADING_survives_being_opened(self):
        from cormani.ui import viewhost
        view = self.saved(threaded=False)
        viewhost.apply_view(self.window.mail, view.id)
        self.assertFalse(self.window.mail.model.threaded)

    def test_opening_one_stands_the_other_panes_down(self):
        # Three panes claim the space the list occupies and whoever was asked
        # last owns it. A pane left visible under another reads as a rendering
        # fault rather than as a mistake.
        from cormani.ui import viewhost
        self.window.mail.show_tracking()
        self.assertTrue(self.window.mail.showing_tracking())
        viewhost.apply_view(self.window.mail, self.saved().id)
        self.assertFalse(self.window.mail.showing_tracking())

    def test_opening_a_deleted_one_is_false_rather_than_a_crash(self):
        from cormani.ui import viewhost
        view = self.saved()
        savedviews.delete_view(self.con, view.id)
        self.assertFalse(viewhost.apply_view(self.window.mail, view.id))

    def test_one_kept_out_of_the_rail_still_opens_from_the_menu(self):
        # That is what `in_rail = 0` MEANS: not drawn, still runnable.
        from cormani.ui import viewhost
        view = self.saved(in_rail=False)
        self.assertTrue(viewhost.open_named(self.window, view.id))
        self.assertEqual(self.window.mail.model.search.text, "invoice")


class TestWhatTheTabIsCalled(ViewHostCase):
    def saved(self):
        return savedviews.save_view(self.con, savedviews.SavedView(
            name="Invoices", search=search.Query(text="invoice")))

    def test_a_tab_showing_a_saved_search_is_NAMED_for_it(self):
        from cormani.ui import viewhost
        from cormani.ui.models import rail as rail_model
        view = self.saved()
        self.window.mail.rail.refresh_counts()
        self.window.mail.rail.select_key(rail_model.saved_key(view.id))
        self.assertIsNotNone(viewhost.showing(self.window.mail))
        self.assertEqual(self.window.mail.title_for_scope(), "Invoices")

    def test_editing_the_search_gives_the_tab_its_name_BACK(self):
        # A tab that went on carrying the saved search's name after the query
        # was changed would be making a claim about its contents that had
        # stopped being true.
        from cormani.ui import viewhost
        from cormani.ui.models import rail as rail_model
        view = self.saved()
        self.window.mail.rail.refresh_counts()
        self.window.mail.rail.select_key(rail_model.saved_key(view.id))
        self.window.mail.set_search(search.Query(text="something else"))
        self.assertIsNone(viewhost.showing(self.window.mail))
        self.assertNotEqual(self.window.mail.title_for_scope(), "Invoices")


class TestDeletingFromTheHost(ViewHostCase):
    def test_deleting_asks_and_says_what_the_view_was_for(self):
        from cormani.ui import viewhost
        view = savedviews.save_view(self.con, savedviews.SavedView(
            name="Invoices", filters=views.Filters(unread=True)))
        self.assertTrue(viewhost.delete_view(self.window, view.id,
                                             confirm=self.confirm))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("unread", self.asked[0])
        self.assertIsNone(savedviews.get_view(self.con, view.id))

    def test_declining_keeps_it(self):
        from cormani.ui import viewhost
        view = savedviews.save_view(self.con, savedviews.SavedView(
            name="Invoices", filters=views.Filters(unread=True)))
        self.answer = False
        self.assertFalse(viewhost.delete_view(self.window, view.id,
                                              confirm=self.confirm))
        self.assertIsNotNone(savedviews.get_view(self.con, view.id))


class TestTheMenu(ViewHostCase):
    def entries(self):
        from cormani.ui import viewhost
        viewhost.build_menu(self.window, self.window.saved_views_menu)
        return [a.text() for a in self.window.saved_views_menu.actions()]

    def test_an_empty_store_says_so_rather_than_showing_nothing(self):
        # An empty submenu reads as a broken menu.
        entries = self.entries()
        self.assertEqual(entries, ["none saved yet"])
        self.assertFalse(self.window.saved_views_menu.actions()[0].isEnabled())

    def test_every_saved_search_is_listed_INCLUDING_the_hidden_ones(self):
        savedviews.save_view(self.con, savedviews.SavedView(
            name="Drawn", filters=views.Filters(unread=True)))
        savedviews.save_view(self.con, savedviews.SavedView(
            name="Hidden", in_rail=False, filters=views.Filters(flagged=True)))
        self.assertEqual(self.entries(), ["Drawn", "Hidden"])

    def test_the_menu_is_rebuilt_rather_than_kept(self):
        self.assertEqual(self.entries(), ["none saved yet"])
        savedviews.save_view(self.con, savedviews.SavedView(
            name="Later", filters=views.Filters(unread=True)))
        self.assertEqual(self.entries(), ["Later"])


if __name__ == "__main__":
    unittest.main()
