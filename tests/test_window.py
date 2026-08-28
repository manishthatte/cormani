# SPDX-License-Identifier: GPL-3.0-or-later
#
# The window: it builds, it is navigable, and it does so with no display.
#
# The offscreen platform and an isolated QSettings come from tests/support.py,
# so nothing here can move the user's real window geometry.
#
# One test is worth explaining. `test_menu_entries_do_not_install_a_second_copy
# _of_a_list_key` guards a failure that is silent at run time: two QActions
# bound to the same key in overlapping contexts make Qt report an ambiguous
# shortcut and fire NEITHER. The Message menu therefore shows its keys as text
# and installs none of them, and this test is what stops that being undone.
#
# © Manish Jagdish Thatte
import unittest

from cormani.store import accounts, messages, views
from cormani.ui import density as density_mod
from cormani.ui import shortcuts as shortcuts_mod
from cormani.ui.models import rail as rail_model

import support


@support.requires_qt
class TestMainWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def _window(self, *, demo=True):
        from cormani.ui.window import MainWindow
        con = support.demo_store(self) if demo else support.temp_store(self)
        return support.own(self, MainWindow(con, demo=demo))

    # ------------------------------------------------------------------ shape
    def test_constructs_with_three_panes_and_four_more_behind_them(self):
        """Seven widgets in the splitter, three of them showing.

        The rail, the list and the reader are the window; the calendar, the
        agenda pane, the tracking pane and the address book are SIBLINGS of
        them rather than windows of their own — PLAN.txt §3 — and all four are
        hidden until asked for, which is what makes swapping the panes keep the
        widths the user chose. The sites are absent because a QWebEngineView is
        a browser and `ui/sitehost.py` makes them on first use.

        THE PROPERTY IS ASSERTED AS WELL AS THE COUNT, and the property is the
        half that matters: this test previously said only "6" and the address
        book made it wrong by adding a widget, which is a test that has to be
        edited rather than one that catches anything. `showing_mail` is the
        invariant — the list and the reader own the space until something asks
        for it — and it is what a fifth claimant would have to keep true.
        """
        window = self._window()
        self.assertEqual(window.splitter.count(), 7)
        self.assertTrue(window.mail.showing_mail())
        self.assertFalse(window.mail.agenda_visible())
        for host in ("calendars", "tracking", "contacts", "sites"):
            self.assertFalse(getattr(window.mail, host).showing,
                             f"{host} claims the space at start-up")

    def test_the_startup_wiring_for_site_panels_actually_runs(self):
        """`app.run` calls this and NOTHING ELSE DID, which is how stage 7
        shipped a start-up crash.

        `RailModel.set_sites` called `self.reload()`, a method that does not
        exist — the name is `rebuild` — so `python3 -m cormani` died with an
        AttributeError before the window ever appeared. 1,277 tests passed
        over it, because every one of them built a window and none of them
        told it which sites to show. The panel tests cover the same path but
        need QtWebEngine; this one does not, so the guard survives on a machine
        without the browser.
        """
        from cormani.panels import sites as sites_mod

        window = self._window()
        window.attach_sites(sites_mod.default_keys())      # must not raise
        keys = [sites_mod.rail_key(k) for k in sites_mod.default_keys()]
        self.assertTrue(window.mail.rail.select_key(keys[0]),
                        "the rail drew no row for a site that is turned on")

    def test_an_unread_count_from_a_panel_reaches_the_rail(self):
        """`set_site_unread` carried the same fault as `set_sites` and would
        have raised on every badge update — the second half of one defect."""
        window = self._window()
        window.attach_sites(["whatsapp"])
        window.mail.rail.model_obj.set_site_unread("whatsapp", 3)   # must not raise
        window.mail.rail.model_obj.set_site_unread("whatsapp", None)

    def test_panes_cannot_be_collapsed_away(self):
        # A pane dragged to zero width looks like a broken window and is hard
        # to recover without knowing the splitter is there.
        self.assertFalse(self._window().splitter.childrenCollapsible())

    def test_window_has_a_title_and_a_floor_size(self):
        w = self._window()
        self.assertTrue(w.windowTitle())
        self.assertGreaterEqual(w.minimumWidth(), 800)

    def test_rail_accepts_reordering(self):
        from PySide6.QtWidgets import QAbstractItemView
        self.assertEqual(self._window().rail.dragDropMode(),
                         QAbstractItemView.DragDropMode.InternalMove)

    def test_rail_shows_its_five_sections(self):
        w = self._window(demo=False)
        model = w.rail.model()
        labels = [model.index(r, 0).data() for r in range(model.rowCount())]
        self.assertEqual(labels, ["Unified", "Accounts", "Saved searches",
                                  "Sites", "Calendars"])

    def test_an_empty_store_still_gives_a_working_window(self):
        # A named empty state, not a blank pane. Nothing selected, no crash.
        w = self._window(demo=False)
        self.assertEqual(w.mail.model.rowCount(), 0)
        self.assertIsNone(w.mail.current_row())

    # ----------------------------------------------------------- honest chrome
    def test_controls_with_no_body_yet_are_disabled(self):
        # CONVENTIONS.txt §8: a control that does nothing when clicked is worse
        # than one that is visibly not ready.
        w = self._window()
        self.assertFalse(w.act_sync.isEnabled())
        # Snooze is stage 6's — triage, with somewhere to keep a deadline —
        # and the button says so rather than doing nothing.
        self.assertFalse(w.mail.reader.commands._buttons["snooze"].isEnabled())

    def test_the_search_box_and_its_chips_are_live_now(self):
        # They were disabled placeholders until item 16. The pairing with the
        # test above is the point: this suite asserts which controls work.
        w = self._window()
        self.assertTrue(w.act_search.isEnabled())
        self.assertTrue(w.search.text.isEnabled())
        self.assertTrue(all(c.isEnabled() for c in w.search.chips))

    def test_the_two_text_boxes_say_which_is_which(self):
        # One filters what is in view; the other searches every account.
        # Confusing them is the point of the labels.
        w = self._window()
        self.assertIn("all accounts", w.search.text.placeholderText())
        self.assertIn("these messages", w.mail.quick_filter.text.placeholderText())

    def test_menu_entries_do_not_install_a_second_copy_of_a_list_key(self):
        w = self._window()
        list_keys = {s.key for s in shortcuts_mod.in_scope(shortcuts_mod.SCOPE_LIST)}
        installed = []
        for action in w.findChildren(type(w.act_sync)):
            if action.parent() is w and not action.shortcut().isEmpty():
                installed.append(action.shortcut().toString())
        self.assertEqual([k for k in installed if k in list_keys], [])

    def test_every_list_key_is_installed_exactly_once_on_the_list(self):
        w = self._window()
        keys = [a.shortcut().toString() for a in w.mail.list.actions()
                if not a.shortcut().isEmpty()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            set(keys),
            {a.shortcut().toString() for a in w.mail.list.actions()})

    def test_store_summary_is_settable(self):
        w = self._window()
        w.set_store_summary("schema v3 · 0 accounts")
        self.assertIn("schema v3", w.status_store.text())

    def test_the_demo_window_says_it_is_demo_data(self):
        w = self._window()
        self.assertIn("demo", w.windowTitle().lower())
        self.assertIn("demo", w.status_message.text().lower())

    # ------------------------------------------------------------ navigation
    def test_the_rail_drives_the_list(self):
        w = self._window()
        w.rail.select_key("unified:owed")
        self.assertEqual(w.mail.model.scope.role, views.ROLE_OWED)
        first = accounts.list_accounts(w.mail._con)[0]
        w.rail.select_key(f"account:{first.id}")
        self.assertEqual(w.mail.model.scope.account_id, first.id)

    def test_opening_a_folder_selects_nothing_and_reads_nothing(self):
        # Selecting the first message would mark it read; a folder click must
        # not silently reduce an unread count.
        w = self._window()
        before = sum(messages.unread_counts(w.mail._con).values())
        w.rail.select_key("unified:drafts")
        w.rail.select_key("unified:inbox")
        self.assertIsNone(w.mail.current_row())
        self.assertEqual(sum(messages.unread_counts(w.mail._con).values()), before)

    def test_selecting_a_message_shows_it_and_marks_it_read(self):
        w = self._window()
        w.mail.select_row(0)
        row = w.mail.current_row()
        self.assertIsNotNone(row)
        self.assertTrue(row.seen)
        self.assertEqual(w.mail.reader.subject.text(), row.subject_label)

    def test_the_selection_survives_a_counts_refresh(self):
        # A rail rebuild re-selects its own node; that must not read as the
        # user choosing a scope and clear the list.
        w = self._window()
        w.mail.select_row(0)
        w.mail.rail.refresh_counts()
        self.assertIsNotNone(w.mail.current_row())

    def test_next_unread_moves_and_then_reports_the_end(self):
        w = self._window()
        w.rail.select_key("unified:inbox")
        w.mail.run_shortcut("next_unread")
        self.assertIsNotNone(w.mail.current_row())
        w.mail.quick_filter.set_filters(views.Filters(text="no-such-string-xyz"))
        w.mail._filters_changed(views.Filters(text="no-such-string-xyz"))
        w.mail.run_shortcut("next_unread")
        self.assertIn("unread", w.status_message.text().lower())

    # ------------------------------------------------------------ preferences
    def test_density_changes_the_row_height(self):
        w = self._window()
        heights = []
        for key in ("compact", "normal", "relaxed"):
            w.set_density(key)
            heights.append(self._row_height(w))
            self.assertTrue(w.density_actions[key].isChecked())
        self.assertEqual(heights, sorted(heights))
        self.assertLess(heights[0], heights[-1])

    @staticmethod
    def _row_height(w):
        from PySide6.QtWidgets import QStyleOptionViewItem
        option = QStyleOptionViewItem()
        option.font = w.mail.list.font()
        return w.mail.list.delegate.sizeHint(option, w.mail.model.index(0, 0)).height()

    def test_an_unknown_density_falls_back_rather_than_raising(self):
        w = self._window()
        w.set_density("enormous")
        self.assertEqual(w.mail.list.delegate.density.key,
                         density_mod.DEFAULT_DENSITY)

    def test_switching_to_system_does_not_keep_the_previous_palette(self):
        # `system` means the desktop's colours, not whichever theme ran last.
        w = self._window()
        w.apply_theme("solarized-dark")
        dark = w.mail.list.delegate.theme.surface
        w.apply_theme("system")
        self.assertNotEqual(w.mail.list.delegate.theme.surface, dark)

    def test_every_theme_leaves_the_delegates_with_real_colours(self):
        w = self._window()
        for key in ("solarized-light", "solarized-dark", "system"):
            w.apply_theme(key)
            for theme in (w.mail.list.delegate.theme, w.rail._delegate.theme):
                for role in ("surface", "text", "text_muted", "accent", "flagged"):
                    self.assertTrue(getattr(theme, role), f"{key}/{role}")

    # ------------------------------------------------------------------ tabs
    def test_a_new_tab_keeps_its_own_view(self):
        w = self._window()
        w.rail.select_key("unified:inbox")
        w._new_tab()
        self.assertEqual(w.tabs.count(), 2)
        w.rail.select_key("unified:owed")
        owed_rows = w.mail.model.rowCount()
        w.tabs.setCurrentIndex(0)
        self.assertEqual(w.mail.rail.current_key(), "unified:inbox")
        w.tabs.setCurrentIndex(1)
        self.assertEqual(w.mail.rail.current_key(), "unified:owed")
        self.assertEqual(w.mail.model.rowCount(), owed_rows)

    def test_the_last_tab_cannot_be_closed(self):
        w = self._window()
        self.assertFalse(w.tabs.close_current())
        self.assertEqual(w.tabs.count(), 1)
        w._new_tab()
        self.assertTrue(w.tabs.close_current())
        self.assertEqual(w.tabs.count(), 1)

    def test_no_tab_is_ever_created_while_the_close_button_is_enabled(self):
        """A ✕ painted over the middle of a tab's own title.

        A tab added while `tabsClosable` is true keeps the close button's SLOT
        after the button itself is taken away again, and the style paints into
        it — so `Re: DWCNT wavelength question` read as `Re: DW✕NT wavelength
        question`. Qt reports no button there (`tabButton` returns None) and
        the size hint is unchanged, so the only thing a test can hold on to is
        the ORDER: the bar starts closed, opens at the second tab, and closes
        again when it comes back to one.
        """
        w = self._window()
        self.assertFalse(w.tabs.tabsClosable())
        w._new_tab()
        self.assertTrue(w.tabs.tabsClosable())
        w.tabs.close_current()
        self.assertFalse(w.tabs.tabsClosable())

    # ------------------------------------------------------- conversations
    def test_conversations_can_be_turned_off_and_the_menu_follows(self):
        w = self._window()
        self.assertTrue(w.act_threaded.isChecked())
        self.assertTrue(w.mail.model.grouping)
        w.act_threaded.setChecked(False)
        self.assertFalse(w.mail.model.threaded)
        self.assertFalse(w.mail.model.grouping)
        w.act_threaded.setChecked(True)
        self.assertTrue(w.mail.model.grouping)

    def test_the_entry_is_disabled_where_conversations_cannot_be_drawn(self):
        # A refusal that says which it is, rather than a menu entry that turns
        # itself off again a moment after it is pressed.
        w = self._window()
        w._sort_by("sender")
        self.assertFalse(w.act_threaded.isEnabled())
        self.assertIn("date order", w.act_threaded.statusTip())
        w._sort_by("date")
        self.assertTrue(w.act_threaded.isEnabled())
        w.search.text.setText("wavelength")
        w.search.text.returnPressed.emit()
        self.assertFalse(w.act_threaded.isEnabled())
        w.search.clear()
        self.assertTrue(w.act_threaded.isEnabled())

    def test_a_tab_keeps_its_own_grouping(self):
        w = self._window()
        w._new_tab()
        w.tabs.setCurrentIndex(1)
        w.act_threaded.setChecked(False)
        w.tabs.setCurrentIndex(0)
        self.assertTrue(w.mail.model.threaded)
        self.assertTrue(w.act_threaded.isChecked())
        w.tabs.setCurrentIndex(1)
        self.assertFalse(w.mail.model.threaded)
        self.assertFalse(w.act_threaded.isChecked())

    def test_a_message_tab_is_named_after_the_message(self):
        w = self._window()
        row = messages.fetch(w.mail._con, views.Scope(), limit=1)[0]
        w._open_message_tab(row.id)
        self.assertEqual(w.tabs.count(), 2)
        self.assertIn(row.subject_label[:20], w.tabs.tabText(1))
        self.assertEqual(w.mail.current_row().id, row.id)

    # --------------------------------------------------------------- actions
    def test_archiving_removes_the_row_and_moves_the_cursor_on(self):
        w = self._window()
        w.mail.select_row(0)
        row = w.mail.current_row()
        # WHICH rows, not how many. Archiving the head of a conversation
        # promotes the next message in view into its place; archiving the last
        # message a conversation had in view takes the whole conversation, its
        # context rows included. Both are one action and neither is "one row".
        before = {r.id for r in w.mail.model.rows()}
        w.mail.run_action("archive", [row.id])
        after = {r.id for r in w.mail.model.rows()}
        self.assertNotIn(row.id, after)
        self.assertTrue(after < before)
        self.assertFalse(w.mail.model.index_of(row.id).isValid())
        self.assertIsNotNone(w.mail.current_row())
        self.assertIn("Archived", w.status_message.text())

    def test_flagging_keeps_the_row_and_repaints_it(self):
        w = self._window()
        w.mail.select_row(0)
        row = w.mail.current_row()
        before = w.mail.model.rowCount()
        w.mail.run_action("flag", [row.id])
        self.assertEqual(w.mail.model.rowCount(), before)
        self.assertTrue(w.mail.model.row_at(w.mail.model.index_of(row.id)).flagged)

    def test_a_command_with_no_body_yet_says_so_and_changes_nothing(self):
        w = self._window()
        w.mail.select_row(0)
        row = w.mail.current_row()
        before = w.mail.model.rowCount()
        w.mail.run_action("snooze", [row.id])
        self.assertIn("stage 6", w.status_message.text())
        self.assertEqual(w.mail.model.rowCount(), before)

    def test_the_reading_pane_s_buttons_reach_the_same_actions(self):
        """They reached NOTHING until stage 3 item 15: `Reader.command` was
        emitted and connected nowhere, so Archive, Flag, Mark read and Delete
        above the reading pane did nothing, while the identical four worked
        from the list and from the keyboard."""
        w = self._window()
        w.mail.select_row(0)
        row = w.mail.current_row()
        before = {r.id for r in w.mail.model.rows()}
        w.mail.reader.command.emit("archive")
        self.assertNotIn(row.id, {r.id for r in w.mail.model.rows()})
        self.assertTrue({r.id for r in w.mail.model.rows()} < before)
        self.assertFalse(w.mail.model.index_of(row.id).isValid())
        self.assertIn("Archived", w.status_message.text())

    def test_a_reading_pane_button_with_no_body_yet_says_so_there_too(self):
        w = self._window()
        w.mail.select_row(0)
        w.mail.reader.command.emit("snooze")
        self.assertIn("stage 6", w.status_message.text())

    def test_what_the_attachment_strip_did_reaches_the_status_bar(self):
        w = self._window()
        ids = [r.id for r in w.mail.model.rows_loaded()] if hasattr(
            w.mail.model, "rows_loaded") else []
        del ids
        for index in range(w.mail.model.rowCount()):
            w.mail.select_row(index)
            if w.mail.reader.attachments._buttons:
                break
        else:                                                # pragma: no cover
            self.skipTest("no fixture message carries an attachment")
        # Demo data has no server behind it, so the parts have no bytes. What
        # is pinned is that the reason travels: strip → reader → pane → window.
        w.mail.reader.attachments.open_attachment(0)
        self.assertIn("downloaded", w.status_message.text())

    def test_a_link_in_a_message_is_refused_unless_its_scheme_is_ours(self):
        w = self._window()
        w.mail.reader.link_activated.emit("javascript:alert(1)")
        self.assertIn("refused", w.status_message.text())

    def test_an_ordinary_link_is_handed_to_the_desktop(self):
        from unittest import mock

        w = self._window()
        with mock.patch("cormani.ui.mailpane.desktop.open_url") as opener:
            w.mail.reader.link_activated.emit("https://example.org/a")
        opener.assert_called_once_with("https://example.org/a")
        self.assertIn("https://example.org/a", w.status_message.text())

    def test_an_action_with_nothing_selected_says_so(self):
        w = self._window()
        w.mail.clear_selection()
        w.mail.run_action("archive", [])
        self.assertIn("No message selected", w.status_message.text())

    def test_a_tag_key_applies_and_removes(self):
        w = self._window()
        w.mail.select_row(0)
        row = w.mail.current_row()
        w.mail.run_shortcut("tag_3")
        self.assertIn("Tagged", w.status_message.text())
        w.mail.run_shortcut("tag_3")
        self.assertIn("Untagged", w.status_message.text())

    def test_the_rail_counts_follow_an_action(self):
        w = self._window()
        w.rail.select_key("unified:inbox")
        model = w.rail.model()
        inbox = model.index_for_key("unified:inbox")
        before = inbox.data(rail_model.CountRole)
        unread = [r for r in w.mail.model.rows() if not r.seen][0]
        w.mail.run_action("mark_read", [unread.id])
        after = w.rail.model().index_for_key("unified:inbox").data(rail_model.CountRole)
        self.assertEqual(after, before - 1)


if __name__ == "__main__":
    unittest.main()
