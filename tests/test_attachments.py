# SPDX-License-Identifier: GPL-3.0-or-later
#
# Opening and saving an attachment — stage 3, item 15.
#
# Three surfaces, and the reason they are in one file is that the interesting
# properties cross all three: a filename arrives from a stranger in
# `store/attachments`, is put on a person's disk by the strip, and is handed to
# another program by `platform/desktop`. Testing each half alone would miss the
# joins, which is where this class of bug lives.
#
# NOTHING HERE LAUNCHES ANYTHING. `desktop.open_path` takes its spawn as a
# parameter and the strip takes its opener and its dialogs, so what is asserted
# is the ARGUMENT VECTOR that would have been exec'd. That is the property
# worth pinning anyway: CONVENTIONS.txt §7 says argument array and never a
# shell string, and a test that watched a real xdg-open could not tell the
# difference between the two.
#
# © Manish Jagdish Thatte
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cormani.platform import desktop
from cormani.store import attachments as att

import support


class Recorder:
    """Stands in for the spawn, and remembers exactly what it was given."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))

    @property
    def argv(self):
        return self.calls[-1] if self.calls else None


def make_row(con, message_id, *, filename="report.pdf",
             content_type="application/pdf", content_id="", size=11,
             stored="", inline=False):
    con.execute("""
        INSERT INTO attachment (message_id, filename, content_type, content_id,
            size_bytes, part_number, stored_path, is_inline)
        VALUES (?, ?, ?, ?, ?, '2', ?, ?)
    """, (message_id, filename, content_type, content_id, size,
          str(stored), 1 if inline else 0))
    con.commit()
    return con.execute(
        "SELECT id, filename, content_type, content_id, size_bytes, "
        "stored_path, is_inline FROM attachment WHERE id = ?",
        (con.execute("SELECT last_insert_rowid()").fetchone()[0],)).fetchone()


class AttachmentCase(unittest.TestCase):
    """A store with real bytes on disk under a real attachments root."""

    def setUp(self):
        self.con = support.demo_store(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="cormani-att-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "attachments"
        self.cache = self.base / "cache"
        self.out = self.base / "out"
        for d in (self.root, self.cache, self.out):
            d.mkdir(parents=True)
        self.message_id = self.con.execute(
            "SELECT id FROM message ORDER BY id LIMIT 1").fetchone()[0]

    def written(self, name: str, data: bytes = b"twelve bytes", *,
                sub: str = "1/1") -> Path:
        """A file where ingest would have put it."""
        path = self.root / sub / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def part(self, **kw):
        return make_row(self.con, self.message_id, **kw)


# ----------------------------------------------------------------- naming
class TestNaming(AttachmentCase):
    def test_the_shown_name_is_the_stranger_s_name_made_safe(self):
        row = self.part(filename="../../.bashrc")
        name = att.display_name(row, index=1)
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)
        self.assertFalse(name.startswith("."))

    def test_a_part_with_no_name_is_named_for_its_place_and_its_type(self):
        row = self.part(filename="", content_type="image/png")
        self.assertEqual(att.display_name(row, index=3), "part-3.png")

    def test_jpeg_gets_the_extension_a_person_expects(self):
        row = self.part(filename="", content_type="image/jpeg")
        self.assertEqual(att.display_name(row, index=2), "part-2.jpg")

    def test_an_unknown_type_gets_no_extension_rather_than_a_wrong_one(self):
        row = self.part(filename="", content_type="application/octet-stream")
        self.assertEqual(att.display_name(row, index=1), "part-1")

    def test_the_suffix_decides_risk_not_the_declared_type(self):
        # The type is the sender's claim; the suffix is what the desktop
        # dispatches on, so a .desktop calling itself a PDF is still risky.
        self.assertTrue(att.is_risky("invoice.desktop"))
        self.assertTrue(att.is_risky("SETUP.EXE"))
        self.assertTrue(att.is_risky("run.sh"))
        self.assertFalse(att.is_risky("report.pdf"))
        self.assertFalse(att.is_risky("photo.jpg"))
        self.assertFalse(att.is_risky(""))


# -------------------------------------------------------------- containment
class TestContainment(AttachmentCase):
    def test_a_part_that_was_never_downloaded_says_so(self):
        row = self.part(stored="")
        with self.assertRaises(att.AttachmentMissing):
            att.stored_file(row, self.root)

    def test_a_stored_path_outside_the_root_is_refused(self):
        outside = self.base / "elsewhere.pdf"
        outside.write_bytes(b"not mine")
        row = self.part(stored=outside)
        with self.assertRaises(att.AttachmentEscapes):
            att.stored_file(row, self.root)

    def test_a_symlink_inside_the_root_that_points_out_of_it_is_refused(self):
        """The check that matters: the row sits under the root and the bytes
        do not. Resolution happens before the comparison for this reason."""
        secret = self.base / "secret.txt"
        secret.write_bytes(b"a decade of correspondence")
        link = self.root / "1" / "1" / "1-innocent.txt"
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(secret, link)
        row = self.part(stored=link)
        with self.assertRaises(att.AttachmentEscapes):
            att.stored_file(row, self.root)

    def test_a_row_whose_file_has_been_deleted_says_so_rather_than_raising_oserror(self):
        row = self.part(stored=self.root / "1" / "1" / "1-gone.pdf")
        with self.assertRaises(att.AttachmentMissing):
            att.stored_file(row, self.root)

    def test_a_real_part_resolves(self):
        path = self.written("1-report.pdf")
        row = self.part(stored=path)
        self.assertEqual(att.stored_file(row, self.root), path.resolve())


# ------------------------------------------------------------------ saving
class TestSaving(AttachmentCase):
    def test_save_as_writes_the_bytes_and_nothing_executable(self):
        path = self.written("1-report.pdf", b"%PDF-1.4 hello")
        row = self.part(stored=path)
        target = self.out / "chosen name.pdf"
        written = att.save_as(row, self.root, target)
        self.assertEqual(written.read_bytes(), b"%PDF-1.4 hello")
        self.assertFalse(written.stat().st_mode & stat.S_IXUSR)

    def test_save_as_overwrites_because_the_dialog_already_asked(self):
        path = self.written("1-report.pdf", b"new")
        row = self.part(stored=path)
        target = self.out / "report.pdf"
        target.write_bytes(b"old")
        att.save_as(row, self.root, target)
        self.assertEqual(target.read_bytes(), b"new")

    def test_a_second_file_of_the_same_name_is_numbered_not_overwritten(self):
        first = att.unique_path(self.out, "report.pdf")
        first.write_bytes(b"one")
        second = att.unique_path(self.out, "report.pdf")
        self.assertEqual(second.name, "report (2).pdf")
        second.write_bytes(b"two")
        self.assertEqual(att.unique_path(self.out, "report.pdf").name,
                         "report (3).pdf")
        self.assertEqual(first.read_bytes(), b"one")

    def test_a_name_from_the_message_cannot_climb_out_of_the_chosen_directory(self):
        target = att.unique_path(self.out, "../../.bashrc")
        self.assertEqual(target.parent, self.out.resolve())

    def test_save_all_skips_inline_parts_and_the_ones_with_no_bytes(self):
        rows = [
            self.part(filename="a.pdf", stored=self.written("1-a.pdf", b"aa")),
            self.part(filename="logo.png", inline=True,
                      stored=self.written("2-logo.png", b"bb")),
            self.part(filename="c.pdf", stored=""),
        ]
        written = att.save_all(rows, self.root, self.out)
        self.assertEqual([p.name for p in written], ["a.pdf"])

    def test_save_all_numbers_two_parts_with_the_same_name(self):
        rows = [
            self.part(filename="image001.png",
                      stored=self.written("1-image001.png", b"one")),
            self.part(filename="image001.png",
                      stored=self.written("2-image001.png", b"two", sub="1/2")),
        ]
        written = att.save_all(rows, self.root, self.out)
        self.assertEqual([p.name for p in written],
                         ["image001.png", "image001 (2).png"])
        self.assertEqual(written[0].read_bytes(), b"one")
        self.assertEqual(written[1].read_bytes(), b"two")


# ----------------------------------------------------------------- opening
class TestCopyForOpening(AttachmentCase):
    def test_the_copy_is_named_as_a_person_would_and_is_not_the_store_s_file(self):
        path = self.written("3-report.pdf", b"%PDF")
        row = self.part(filename="report.pdf", stored=path)
        copy = att.copy_for_opening(row, self.root, self.cache, index=3)
        self.assertEqual(copy.name, "report.pdf")
        self.assertNotEqual(copy.resolve(), path.resolve())
        self.assertEqual(copy.read_bytes(), b"%PDF")
        self.assertTrue(self.cache in copy.parents)

    def test_the_copy_is_private_and_not_executable(self):
        row = self.part(stored=self.written("1-run.pdf", b"x"))
        copy = att.copy_for_opening(row, self.root, self.cache, index=1)
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)

    def test_an_edited_copy_is_replaced_by_the_store_s_version(self):
        """The store's copy is the archive. Whatever opened the last one may
        have written to it, and that must not be what opens next time."""
        row = self.part(stored=self.written("1-report.pdf", b"original"))
        first = att.copy_for_opening(row, self.root, self.cache, index=1)
        first.write_bytes(b"edited by something else")
        second = att.copy_for_opening(row, self.root, self.cache, index=1)
        self.assertEqual(second, first)
        self.assertEqual(second.read_bytes(), b"original")

    def test_two_messages_carrying_the_same_name_do_not_open_each_other_s(self):
        a = self.part(filename="invoice.pdf",
                      stored=self.written("1-invoice.pdf", b"mine"))
        b = self.part(filename="invoice.pdf",
                      stored=self.written("1-invoice.pdf", b"theirs", sub="1/2"))
        first = att.copy_for_opening(a, self.root, self.cache, index=1)
        second = att.copy_for_opening(b, self.root, self.cache, index=1)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"mine")
        self.assertEqual(second.read_bytes(), b"theirs")


# ------------------------------------------------------- the desktop's door
class TestDesktop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cormani-desktop-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.spawn = Recorder()

    def test_a_file_is_handed_over_as_an_argument_vector(self):
        target = self.base / "report.pdf"
        target.write_bytes(b"x")
        desktop.open_path(target, spawn=self.spawn)
        self.assertEqual(self.spawn.argv, [desktop.LINUX_OPENER, str(target.resolve())])

    def test_a_hostile_filename_stays_one_argument(self):
        """CONVENTIONS.txt §7. If this were a shell string, the semicolon would
        be a second command and the quote would close the first."""
        name = "in'voice; rm -rf ~ \".pdf"
        target = self.base / name
        target.write_bytes(b"x")
        desktop.open_path(target, spawn=self.spawn)
        self.assertEqual(len(self.spawn.argv), 2)
        self.assertTrue(self.spawn.argv[1].endswith(name))

    def test_a_missing_file_is_refused_before_anything_is_launched(self):
        with self.assertRaises(desktop.OpenFailed):
            desktop.open_path(self.base / "not here.pdf", spawn=self.spawn)
        self.assertEqual(self.spawn.calls, [])

    def test_only_the_four_schemes_may_be_opened(self):
        for url in ("https://example.org/x", "http://example.org",
                    "mailto:someone@example.org", "tel:+441234567890"):
            self.assertTrue(desktop.scheme_allowed(url), url)
        for url in ("javascript:alert(1)", "file:///etc/passwd",
                    "data:text/html,<script>", "vbscript:x", "", "example.org",
                    "JAVASCRIPT:alert(1)"):
            self.assertFalse(desktop.scheme_allowed(url), url)

    def test_a_scheme_hidden_behind_whitespace_or_control_bytes_is_not_allowed(self):
        for url in ("java\nscript:alert(1)", "ht\ttp://example.org",
                    " javascript:alert(1)", "\x00https://example.org"):
            self.assertFalse(desktop.scheme_allowed(url), repr(url))

    def test_a_refused_link_never_reaches_the_desktop(self):
        with self.assertRaises(desktop.OpenFailed):
            desktop.open_url("file:///etc/passwd", spawn=self.spawn)
        self.assertEqual(self.spawn.calls, [])

    def test_an_allowed_link_is_handed_over_whole(self):
        desktop.open_url("https://example.org/a?b=c&d=e", spawn=self.spawn)
        self.assertEqual(self.spawn.argv,
                         [desktop.LINUX_OPENER, "https://example.org/a?b=c&d=e"])


# ------------------------------------------------------------------ the strip
@support.requires_qt
class TestStrip(AttachmentCase):
    """The widget, driven through its own methods.

    There is no QTest in Debian, so nothing here clicks. What is exercised is
    what a click would call, and the four hooks stand in for the two dialogs,
    the warning and the launch.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        super().setUp()
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        from cormani.ui import theme as theme_mod
        from cormani.ui.attachments import AttachmentStrip

        # Inside a holder rather than free-standing: the strip lives in the
        # reading pane, and a top-level widget whose height depends on its
        # width makes the offscreen platform complain about size hints it has
        # no window manager to propagate.
        self.holder = support.own(self, QWidget())
        QVBoxLayout(self.holder)
        self.strip = AttachmentStrip(self.holder)
        self.holder.layout().addWidget(self.strip)
        self.strip.apply_theme(theme_mod.SOLARIZED_LIGHT)
        self.opened = []
        self.said = []
        self.strip.open_file = self.opened.append
        self.strip.status.connect(self.said.append)

    def show(self, rows):
        self.strip.set_attachments(rows, root=self.root, cache=self.cache)

    def a_pdf(self, name="report.pdf", data=b"%PDF-1.4", sub="1/1"):
        return self.part(filename=name,
                         stored=self.written(f"1-{name}", data, sub=sub))

    # -------------------------------------------------------------- listing
    def test_an_inline_part_is_not_offered_as_a_file(self):
        self.show([self.part(filename="logo.png", inline=True,
                             stored=self.written("1-logo.png")),
                   self.a_pdf(sub="1/2")])
        self.assertEqual(len(self.strip._buttons), 1)
        self.assertIn("report.pdf", self.strip._buttons[0].text())

    def test_a_message_with_only_inline_parts_shows_no_strip(self):
        self.show([self.part(filename="logo.png", inline=True,
                             stored=self.written("1-logo.png"))])
        self.assertFalse(self.strip.isVisibleTo(self.holder))
        self.assertEqual(self.strip._buttons, [])

    def test_the_caption_counts_and_save_all_appears_only_for_several(self):
        self.show([self.a_pdf()])
        self.assertEqual(self.strip.caption.text(), "Attachment:")
        self.assertFalse(self.strip.save_all_button.isVisible())
        self.show([self.a_pdf(), self.a_pdf("second.pdf", sub="1/2")])
        self.assertIn("2 attachments ·", self.strip.caption.text())
        self.assertIn("total:", self.strip.caption.text())
        self.assertTrue(self.strip.save_all_button.isVisibleTo(self.strip))

    def test_showing_another_message_forgets_the_last_one(self):
        self.show([self.a_pdf(), self.a_pdf("second.pdf", sub="1/2")])
        self.strip.clear()
        self.assertEqual(self.strip._buttons, [])
        self.assertFalse(self.strip.open_attachment(0))

    def test_the_chip_names_the_file_and_its_size(self):
        self.show([self.part(filename="report.pdf", size=128000,
                             stored=self.written("1-report.pdf"))])
        text = self.strip._buttons[0].text()
        self.assertIn("report.pdf", text)
        self.assertIn("125 KB", text)

    def test_a_file_the_desktop_would_run_is_marked_before_it_is_clicked(self):
        """The confirmation is the gate; this is the warning that comes before
        the click, so nobody meets the gate by surprise."""
        from cormani.ui import theme as theme_mod
        from cormani.ui.attachments import chip_colour

        t = theme_mod.SOLARIZED_LIGHT
        self.assertEqual(chip_colour(t, missing=False, risky=False), t.accent)
        self.assertEqual(chip_colour(t, missing=False, risky=True), t.error)
        # Missing wins: a part with no bytes cannot be opened at all, which is
        # the more useful thing to say about it.
        self.assertEqual(chip_colour(t, missing=True, risky=True), t.text_muted)

        self.show([self.a_pdf("report.pdf"),
                   self.a_pdf("payload.desktop", sub="1/2")])
        drawn = [b.icon().pixmap(14).toImage() for b in self.strip._buttons]
        self.assertNotEqual(drawn[0], drawn[1])
        tips = [b.toolTip() for b in self.strip._buttons]
        self.assertIn("would run it", tips[1])
        self.assertNotIn("would run it", tips[0])

    # -------------------------------------------------------------- opening
    def test_opening_hands_a_cache_copy_to_the_desktop_not_the_store_s_file(self):
        stored = self.written("1-report.pdf", b"%PDF-1.4")
        self.show([self.part(filename="report.pdf", stored=stored)])
        self.assertTrue(self.strip.open_attachment(0))
        self.assertEqual(len(self.opened), 1)
        handed = Path(self.opened[0])
        self.assertNotEqual(handed.resolve(), stored.resolve())
        self.assertTrue(self.cache in handed.parents)
        self.assertEqual(handed.read_bytes(), b"%PDF-1.4")

    def test_a_file_the_desktop_would_run_is_not_opened_without_an_answer(self):
        self.strip.confirm_open = lambda name: False
        self.show([self.a_pdf("payload.desktop")])
        self.assertFalse(self.strip.open_attachment(0))
        self.assertEqual(self.opened, [])
        self.assertIn("not opened", self.said[-1])

    def test_the_same_file_opens_once_the_question_is_answered(self):
        asked = []
        self.strip.confirm_open = lambda name: asked.append(name) or True
        self.show([self.a_pdf("payload.desktop")])
        self.assertTrue(self.strip.open_attachment(0))
        self.assertEqual(asked, ["payload.desktop"])
        self.assertEqual(len(self.opened), 1)

    def test_an_ordinary_file_is_never_asked_about(self):
        self.strip.confirm_open = lambda name: self.fail(f"asked about {name}")
        self.show([self.a_pdf()])
        self.assertTrue(self.strip.open_attachment(0))

    def test_a_part_with_no_bytes_reports_why_rather_than_opening(self):
        self.show([self.part(filename="report.pdf", stored="")])
        self.assertFalse(self.strip.open_attachment(0))
        self.assertEqual(self.opened, [])
        self.assertIn("never downloaded", self.said[-1])

    def test_with_nowhere_to_put_a_copy_it_says_so_rather_than_choosing(self):
        self.strip.set_attachments([self.a_pdf()], root=self.root, cache=None)
        self.assertFalse(self.strip.open_attachment(0))
        self.assertIn("nowhere", self.said[-1])

    def test_a_launch_that_fails_is_reported_and_not_swallowed(self):
        def refuse(path):
            raise desktop.OpenFailed("xdg-open is not installed")
        self.strip.open_file = refuse
        self.show([self.a_pdf()])
        self.assertFalse(self.strip.open_attachment(0))
        self.assertIn("xdg-open", self.said[-1])

    # --------------------------------------------------------------- saving
    def test_saving_writes_where_the_dialog_said(self):
        self.show([self.a_pdf(data=b"%PDF-here")])
        target = self.out / "kept.pdf"
        self.assertTrue(self.strip.save_attachment(0, target))
        self.assertEqual(target.read_bytes(), b"%PDF-here")
        self.assertIn("Saved kept.pdf", self.said[-1])

    def test_a_cancelled_dialog_writes_nothing(self):
        self.strip.choose_file = lambda suggested: None
        self.show([self.a_pdf()])
        self.assertFalse(self.strip.save_attachment(0))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_the_dialog_is_offered_the_shown_name_not_the_stored_one(self):
        seen = []
        self.strip.choose_file = lambda suggested: seen.append(suggested) or None
        self.show([self.a_pdf("report.pdf")])
        self.strip.save_attachment(0)
        self.assertEqual(seen, ["report.pdf"])

    def test_save_all_writes_every_part_and_says_how_many(self):
        self.strip.choose_directory = lambda: str(self.out)
        self.show([self.a_pdf("a.pdf", b"aa"),
                   self.a_pdf("b.pdf", b"bb", sub="1/2")])
        self.assertEqual(self.strip.save_all(), 2)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["a.pdf", "b.pdf"])
        self.assertIn("Saved 2 attachments", self.said[-1])

    def test_save_all_says_what_it_could_not_write(self):
        """CONVENTIONS.txt §8 — the half that did not happen is the half worth
        reporting."""
        self.show([self.a_pdf("a.pdf", b"aa"),
                   self.part(filename="b.pdf", stored="")])
        self.assertEqual(self.strip.save_all(self.out), 1)
        self.assertIn("1 of 2", self.said[-1])
        self.assertIn("not downloaded", self.said[-1])

    # ----------------------------------------------------------- the menu
    def test_the_menu_offers_nothing_for_a_part_that_was_never_downloaded(self):
        self.show([self.part(filename="report.pdf", stored="")])
        actions = self.strip.build_menu(0).actions()
        self.assertEqual([a.text() for a in actions], ["Open", "Save as…"])
        self.assertFalse(any(a.isEnabled() for a in actions))

    def test_the_menu_offers_save_all_only_when_there_are_several(self):
        self.show([self.a_pdf()])
        self.assertNotIn("Save all…",
                         [a.text() for a in self.strip.build_menu(0).actions()])
        self.show([self.a_pdf(), self.a_pdf("second.pdf", sub="1/2")])
        self.assertIn("Save all…",
                      [a.text() for a in self.strip.build_menu(0).actions()])

    def test_a_downloaded_part_can_at_least_be_saved_from_the_menu(self):
        self.show([self.a_pdf()])
        by_name = {a.text(): a for a in self.strip.build_menu(0).actions()}
        self.assertTrue(by_name["Save as…"].isEnabled())


@support.requires_qt
class TestFlowLayout(unittest.TestCase):
    """The strip wraps, and a layout that lies about its height draws over
    whatever is beneath it."""

    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def test_a_narrower_width_needs_more_height(self):
        from PySide6.QtWidgets import QPushButton, QWidget

        from cormani.ui.flowlayout import FlowLayout

        holder = support.own(self, QWidget())
        layout = FlowLayout(holder, spacing=6)
        for n in range(8):
            layout.addWidget(QPushButton(f"attachment-{n}.pdf", holder))
        wide = layout.heightForWidth(4000)
        narrow = layout.heightForWidth(200)
        self.assertGreater(narrow, wide)

    def test_one_chip_wide_is_enough_so_the_rail_is_not_squeezed(self):
        """ui/commandbar.py met this and answered it by dropping labels; a
        wrapping layout answers it by wrapping. Either way the minimum must not
        be the sum."""
        from PySide6.QtWidgets import QPushButton, QWidget

        from cormani.ui.flowlayout import FlowLayout

        holder = support.own(self, QWidget())
        layout = FlowLayout(holder, spacing=6)
        buttons = [QPushButton(f"attachment-{n}.pdf", holder) for n in range(8)]
        for button in buttons:
            layout.addWidget(button)
        total = sum(b.sizeHint().width() for b in buttons)
        self.assertLess(layout.minimumSize().width(), total)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
