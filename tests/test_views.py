# SPDX-License-Identifier: GPL-3.0-or-later
#
# The views: the row delegate, the quick filter, the command bar and the reader.
#
# The delegate is tested through its geometry rather than its pixels. Comparing
# a rendering against a reference image would fail on any machine with different
# fonts, which is every machine; what actually has to hold is that the hover
# buttons are inside the row, do not overlap, and that the view's hit test finds
# the same rectangle the delegate painted. That last one is the bug this file
# exists to prevent — a button that highlights under the cursor and then does its
# neighbour's job.
#
# © Manish Jagdish Thatte
import datetime as dt
import unittest

from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyleOptionViewItem

from cormani.store import edits, messages, tags, views
from cormani.ui import density as density_mod
from cormani.ui import icons
from cormani.ui import messagelist as messagelist_mod
from cormani.ui import shortcuts as shortcuts_mod
from cormani.ui import theme as theme_mod

import support


class TestDensity(unittest.TestCase):
    """No Qt at all: these are numbers, and the arithmetic must not depend on
    which fonts happen to be installed."""

    def test_the_three_densities_are_ordered(self):
        heights = [density_mod.row_height(density_mod.get(k), [16, 15, 14])
                   for k in ("compact", "normal", "relaxed")]
        self.assertEqual(heights, sorted(heights))
        self.assertLess(heights[0], heights[-1])

    def test_compact_drops_the_preview_rather_than_shrinking_it(self):
        self.assertFalse(density_mod.COMPACT.shows_preview)
        self.assertEqual(density_mod.COMPACT.lines, 2)
        self.assertTrue(density_mod.NORMAL.shows_preview)

    def test_a_row_is_never_shorter_than_the_icons_it_must_show(self):
        for key in density_mod.DENSITIES:
            d = density_mod.get(key)
            self.assertGreaterEqual(density_mod.row_height(d, [1, 1, 1]), d.icon)

    def test_an_unknown_name_falls_back(self):
        self.assertEqual(density_mod.get("enormous"), density_mod.NORMAL)
        self.assertEqual(density_mod.get(None), density_mod.NORMAL)


class TestDateFormatting(unittest.TestCase):
    def test_the_format_shortens_as_the_message_ages(self):
        now = dt.datetime(2026, 8, 25, 12, 0)
        self.assertEqual(messagelist_mod.format_date("2026-08-25T09:30:00", now), "09:30")
        self.assertEqual(messagelist_mod.format_date("2026-08-21T15:48:00", now), "21 Aug")
        self.assertEqual(messagelist_mod.format_date("2025-11-02T08:00:00", now),
                         "2 Nov 25")

    def test_a_stored_time_is_shown_in_the_reader_s_own_timezone(self):
        # The store keeps UTC. This machine is not on UTC, and for five and a
        # half hours a day the two dates differ — which is when a naive
        # formatter puts yesterday's date on this morning's mail.
        stamp = "2026-08-24T22:51:00+00:00"
        local = messagelist_mod.to_local(stamp)
        self.assertIsNotNone(local)
        self.assertEqual(local.utcoffset(),
                         dt.datetime.now().astimezone().utcoffset())
        self.assertEqual(messagelist_mod.format_date(stamp),
                         local.strftime("%H:%M") if local.date() ==
                         dt.datetime.now().date() else local.strftime("%-d %b"))

    def test_nonsense_does_not_raise_inside_a_paint_event(self):
        for value in ("", "rubbish", "2026-13-45T99:99"):
            self.assertIsInstance(messagelist_mod.format_date(value), str)


class TestRowGeometry(unittest.TestCase):
    def test_the_hover_buttons_fit_inside_the_row_and_do_not_overlap(self):
        for key in density_mod.DENSITIES:
            d = density_mod.get(key)
            row = QRect(0, 0, 520, density_mod.row_height(d, [16, 15, 14]))
            rects = list(messagelist_mod.action_rects(row, d).values())
            self.assertEqual(len(rects), len(messagelist_mod.HOVER_ACTIONS))
            for rect in rects:
                self.assertTrue(row.contains(rect), f"{key}: {rect}")
            for n, first in enumerate(rects):
                for second in rects[n + 1:]:
                    self.assertFalse(first.intersects(second), key)

    def test_the_strip_begins_before_the_first_button(self):
        d = density_mod.NORMAL
        row = QRect(0, 0, 520, 64)
        rects = messagelist_mod.action_rects(row, d)
        self.assertLess(messagelist_mod.action_strip_left(row, d),
                        min(r.left() for r in rects.values()))

    def test_the_destructive_action_is_furthest_from_where_the_cursor_arrives(self):
        ids = [a for a, _glyph, _tip in messagelist_mod.HOVER_ACTIONS]
        self.assertEqual(ids[-1], "delete")


@support.requires_qt
class TestDelegateAndList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.messagelist import MessageList
        from cormani.ui.models.messages import MessageModel
        self.con = support.demo_store(self)
        self.model = MessageModel(self.con, page_size=30)
        self.view = MessageList()
        self.view.setModel(self.model)
        self.view.set_theme(theme_mod.SOLARIZED_LIGHT)
        self.view.resize(520, 400)
        support.own(self, self.view)

    def test_the_view_hit_test_finds_what_the_delegate_painted(self):
        rect = self.view.visualRect(self.model.index(0, 0))
        for name, box in messagelist_mod.action_rects(
                rect, self.view.delegate.density).items():
            _index, action = self.view._action_at(box.center())
            self.assertEqual(action, name)

    def test_a_click_on_the_text_is_not_a_click_on_an_action(self):
        rect = self.view.visualRect(self.model.index(0, 0))
        _index, action = self.view._action_at(rect.topLeft() + rect.center() / 2)
        self.assertEqual(action, "")

    def test_it_paints_every_kind_of_row_without_raising(self):
        # Read, unread, flagged, tagged, with an attachment, hovered, selected,
        # and at all three densities. Painting is where an exception is worst:
        # it happens hundreds of times a second while the list scrolls.
        self.model.fetch_all()
        image = QImage(520, 200, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        option = QStyleOptionViewItem()
        option.font = self.view.font()
        drawn = 0
        for key in density_mod.DENSITIES:
            self.view.set_density(density_mod.get(key))
            for n in range(min(40, self.model.rowCount())):
                index = self.model.index(n, 0)
                option.rect = QRect(0, 0, 520,
                                    self.view.delegate.sizeHint(option, index).height())
                self.view.delegate.hover_row = n if n % 3 == 0 else -1
                self.view.delegate.hover_action = "archive" if n % 6 == 0 else ""
                self.view.delegate.paint(painter, option, index)
                drawn += 1
        painter.end()
        self.assertGreater(drawn, 60)

    def test_painting_actually_puts_ink_on_the_row(self):
        image = QImage(520, 80, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        option = QStyleOptionViewItem()
        option.font = self.view.font()
        option.rect = QRect(0, 0, 520, 70)
        self.view.delegate.paint(painter, option, self.model.index(0, 0))
        painter.end()
        ink = sum(1 for y in range(0, 70, 3) for x in range(0, 520, 3)
                  if image.pixelColor(x, y).alpha() > 0)
        self.assertGreater(ink, 100)

    def test_an_empty_list_says_which_kind_of_empty_it_is(self):
        self.view.set_empty_text("No message matches these filters")
        self.assertIn("filters", self.view._empty_text)


@support.requires_qt
class TestQuickFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.quickfilter import QuickFilterBar
        self.con = support.demo_store(self)
        self.bar = support.own(self, QuickFilterBar(self.con))
        self.seen = []
        self.bar.changed.connect(self.seen.append)

    def test_each_toggle_sets_its_own_field(self):
        from cormani.ui.quickfilter import TOGGLES
        for name, _label, _glyph, _tip in TOGGLES:
            self.bar._buttons[name].setChecked(True)
            self.assertTrue(getattr(self.bar.filters, name), name)
            self.bar._buttons[name].setChecked(False)
            self.assertFalse(getattr(self.bar.filters, name), name)

    def test_typing_narrows_and_clearing_restores(self):
        self.bar.text.setText("diwali")
        self.assertEqual(self.bar.filters.text, "diwali")
        self.assertTrue(self.bar.filters.active)
        self.bar.clear()
        self.assertFalse(self.bar.filters.active)
        self.assertEqual(self.bar.text.text(), "")

    def test_the_clear_button_is_only_offered_when_there_is_something_to_clear(self):
        self.assertFalse(self.bar.clear_button.isEnabled())
        self.bar._buttons["unread"].setChecked(True)
        self.assertTrue(self.bar.clear_button.isEnabled())

    def test_choosing_a_tag_narrows_from_any_tag_to_that_tag(self):
        tag = tags.by_shortcut(self.con, 1)
        self.bar.tag_button.setChecked(True)
        self.assertTrue(self.bar.filters.tagged)
        self.assertIsNone(self.bar.filters.tag_id)
        self.bar._choose_tag(tag.id)
        self.assertEqual(self.bar.filters.tag_id, tag.id)
        self.assertEqual(self.bar.tag_button.text(), tag.name)

    def test_restoring_a_saved_state_does_not_announce_a_change(self):
        # Otherwise switching tabs would overwrite the tab being left with the
        # state of the tab being entered.
        self.seen.clear()
        self.bar.set_filters(views.Filters(unread=True, text="x"))
        self.assertEqual(self.seen, [])
        self.assertTrue(self.bar._buttons["unread"].isChecked())
        self.assertEqual(self.bar.text.text(), "x")


@support.requires_qt
class TestCommandBarAndReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.commandbar import CommandBar
        from cormani.ui.reader import Reader
        self.con = support.demo_store(self)
        self.bar = CommandBar()
        self.reader = Reader()
        self.reader.apply_theme(theme_mod.SOLARIZED_LIGHT)
        support.own(self, self.bar)
        support.own(self, self.reader)

    def test_nothing_is_enabled_without_a_message(self):
        self.bar.set_message(None)
        self.assertFalse(any(b.isEnabled() for b in self.bar._buttons.values()))

    def test_only_the_commands_that_work_become_enabled(self):
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        self.bar.set_message(row)
        for command_id, button in self.bar._buttons.items():
            self.assertEqual(button.isEnabled(), self.bar._ready[command_id],
                             command_id)
        self.assertTrue(self.bar._buttons["archive"].isEnabled())
        self.assertFalse(self.bar._buttons["reply"].isEnabled())

    def test_a_command_that_is_not_ready_says_which_stage_brings_it(self):
        from cormani.ui.commandbar import COMMANDS
        for command_id, _label, _glyph, ready, _primary, tip in COMMANDS:
            if command_id and not ready:
                self.assertIn("stage", tip.lower(), command_id)

    def test_the_three_reply_commands_keep_their_labels(self):
        # PLAN.txt §2 asks for these as buttons rather than a menu; a button
        # whose label was dropped to save room is halfway back to a menu.
        from cormani.ui.commandbar import COMMANDS
        primary = {c for c, _l, _g, _r, p, _t in COMMANDS if c and p}
        self.assertEqual(primary, {"reply", "reply_all", "forward"})

    def test_the_two_toggles_are_relabelled_by_the_message(self):
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        self.bar.set_message(row)
        first = self.bar._buttons["mark_read"].text()
        edits.set_seen(self.con, [row.id], not row.seen)
        self.bar.set_message(messages.get_row(self.con, row.id))
        self.assertNotEqual(self.bar._buttons["mark_read"].text(), first)

    def test_the_reading_pane_renders_html_only_through_the_sanitiser(self):
        # This test used to assert the OPPOSITE — that the body was a
        # QPlainTextEdit and could not render HTML at all. That was stage 1's
        # placeholder and its deliberate guard: a widget that renders HTML
        # before a sanitiser exists is how the requirement gets quietly lost.
        # The sanitiser exists now, so the guard becomes the stronger claim.
        # CONVENTIONS.txt §7.
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        markup = "<script>alert(1)</script><b>bold</b><p onclick='x()'>text</p>"
        self.reader.show_message(row, ("plain fallback", markup), [])
        shown = self.reader.body.toPlainText()
        self.assertIn("bold", shown)
        self.assertIn("text", shown)
        self.assertNotIn("alert", shown)
        self.assertNotIn("onclick", self.reader.body.toHtml().lower())

    def test_a_message_with_no_html_is_shown_as_plain_text(self):
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        self.reader.show_message(row, ("line one\n  indented", ""), [])
        self.assertIn("indented", self.reader.body.toPlainText())

    def test_the_body_can_reach_nothing_it_was_not_given(self):
        # loadResource is the only door. Anything that is not one of THIS
        # message's own inline attachments is refused, and Qt then makes no
        # request at all.
        from PySide6.QtCore import QUrl
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        self.reader.show_message(row, ("x", "<p>x</p>"), [])
        for url in ("file:///etc/passwd", "http://tracker.example/p.gif",
                    "file:///home/someone/.ssh/id_rsa"):    # noqa: path
            self.assertIsNone(self.reader.body.loadResource(2, QUrl(url)), url)

    def test_withheld_remote_content_is_reported_and_can_be_asked_for(self):
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        self.reader.show_message(
            row, ("x", '<p>hi</p><img src="http://tracker.example/p.gif">'), [])
        self.assertEqual(self.reader.body.blocked_remote, 1)
        self.assertTrue(self.reader.remote_bar.isVisibleTo(self.reader))
        self.assertIn("1 remote resource", self.reader.remote_note.text())
        self.assertIn("tells the sender", self.reader.remote_note.text())

        self.reader._load_remote()
        self.assertFalse(self.reader.remote_bar.isVisibleTo(self.reader))
        self.assertIn("tracker.example", self.reader.body.toHtml())

    def test_the_bar_stays_away_when_nothing_was_withheld(self):
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        self.reader.show_message(row, ("x", "<p>no images here</p>"), [])
        self.assertFalse(self.reader.remote_bar.isVisibleTo(self.reader))

    def test_asking_for_images_applies_to_this_message_only(self):
        # A per-sender memory is a decision the user should make deliberately,
        # not one made for them by clicking once.
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        body = ("x", '<img src="http://tracker.example/p.gif">')
        self.reader.show_message(row, body, [])
        self.reader._load_remote()
        self.reader.show_message(row, body, [])
        self.assertTrue(self.reader.remote_bar.isVisibleTo(self.reader),
                        "the next message starts withheld again")

    def test_the_reader_names_the_account_a_message_arrived_on(self):
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        self.reader.show_message(row, ("body", ""), [])
        self.assertEqual(self.reader.account.text(), row.account_label)
        self.assertTrue(self.reader.when.text())

    def test_attachments_are_listed_with_their_size(self):
        """The strip is a widget now, not a line of text — stage 3, item 15.
        Its own behaviour is in tests/test_attachments.py; what this pins is
        that the reading pane still hands it the parts."""
        row = [r for r in messages.fetch(self.con, views.Scope(), limit=200)
               if r.has_attachment][0]
        self.reader.show_message(row, ("body", ""),
                                 messages.attachments_of(self.con, row.id))
        chips = self.reader.attachments._buttons
        self.assertTrue(chips)
        self.assertIn("KB", chips[0].text())
        self.assertIn(".pdf", chips[0].text())

    def test_the_strip_forwards_what_it_did_to_the_status_bar(self):
        said = []
        self.reader.status_message.connect(said.append)
        row = [r for r in messages.fetch(self.con, views.Scope(), limit=200)
               if r.has_attachment][0]
        self.reader.show_message(row, ("body", ""),
                                 messages.attachments_of(self.con, row.id))
        # Demo data has no server behind it, so no part has bytes; the point is
        # that the reason reaches the window rather than being swallowed here.
        self.assertFalse(self.reader.attachments.open_attachment(0))
        self.assertTrue(said)
        self.assertIn("downloaded", said[-1])

    def test_clearing_leaves_no_stale_message_on_screen(self):
        row = messages.fetch(self.con, views.Scope(), limit=1)[0]
        self.reader.show_message(row, ("body", ""), [])
        self.reader.clear()
        self.assertEqual(self.reader.body.toPlainText(), "")
        self.assertIn("No message", self.reader.subject.text())


class TestShortcutMap(unittest.TestCase):
    def test_no_two_shortcuts_share_a_key_in_one_scope(self):
        # Silent at run time: Qt resolves a collision by picking one.
        self.assertEqual(shortcuts_mod.collisions(), [])

    def test_every_shortcut_has_a_key_a_label_and_a_description(self):
        for shortcut in shortcuts_mod.SHORTCUTS:
            self.assertTrue(shortcut.key, shortcut.id)
            self.assertTrue(shortcut.label, shortcut.id)
            self.assertTrue(shortcut.description, shortcut.id)
            self.assertIn(shortcut.scope,
                          (shortcuts_mod.SCOPE_WINDOW, shortcuts_mod.SCOPE_LIST))

    def test_single_key_shortcuts_are_confined_to_the_list(self):
        # A bare letter bound window-wide would be at the mercy of Qt's
        # ShortcutOverride handling in every line edit. See ui/shortcuts.py.
        for shortcut in shortcuts_mod.in_scope(shortcuts_mod.SCOPE_WINDOW):
            self.assertTrue(
                any(part in shortcut.key for part in ("Ctrl", "Alt", "Shift", "F")),
                f"{shortcut.id} is a bare key at window scope")

    def test_the_tag_keys_are_one_to_nine(self):
        keys = sorted(shortcuts_mod.tag_shortcut_key(s.id)
                      for s in shortcuts_mod.SHORTCUTS
                      if s.id.startswith("tag_"))
        self.assertEqual(keys, list(range(1, 10)))
        self.assertIsNone(shortcuts_mod.tag_shortcut_key("archive"))


@support.requires_qt
class TestIcons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def test_every_glyph_draws_something(self):
        blank = []
        for name in icons.GLYPHS:
            image = QImage(32, 32, QImage.Format.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            icons.paint(painter, name, QRectF(2, 2, 28, 28), "#000000")
            painter.end()
            ink = sum(1 for y in range(32) for x in range(32)
                      if image.pixelColor(x, y).alpha() > 20)
            if ink < 20:
                blank.append(name)
        self.assertEqual(blank, [])

    def test_an_unknown_glyph_draws_nothing_and_does_not_raise(self):
        image = QImage(16, 16, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        icons.paint(painter, "no-such-glyph", QRectF(0, 0, 16, 16), "#000000")
        icons.paint(painter, "flag", QRectF(0, 0, 0, 0), "#000000")
        painter.end()

    def test_an_icon_can_be_made_at_any_size_in_any_colour(self):
        for size in (12, 16, 24):
            pixmap = icons.pixmap("archive", "#268bd2", size)
            self.assertEqual(pixmap.width(), size)
            self.assertFalse(icons.icon("trash", "#dc322f", size).isNull())


if __name__ == "__main__":
    unittest.main()
