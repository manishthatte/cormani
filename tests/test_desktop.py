# SPDX-License-Identifier: GPL-3.0-or-later
#
# Appearing in the desktop, which needs more things to line up than it looks.
#
# Split from `tests/test_cli.py` when that reached 639 of the 600 lines the
# packaging test allows — the fourteenth seam the rule has found — and it falls
# here because being PINNABLE is a different subject from what --check and
# --sync report. `cormani/configure.py` owns both halves and this is the half
# that talks to the desktop rather than to a mail server.
#
# THREE THINGS HAVE TO LINE UP AND EACH HAS FAILED SEPARATELY:
#
#   the ENTRY, whose Exec line must actually start something — `Exec=cormani`
#   was the stage 0 placeholder and there is no such binary on this machine;
#
#   the WINDOW, which on Wayland is matched to its launcher by the xdg-shell
#   app_id and not by StartupWMClass, which is X11's and is ignored; and
#
#   the ICON, which is where this file grew. GTK treats an existing
#   `icon-theme.cache` as AUTHORITATIVE: a cache older than the icon beside it
#   reports the icon as ABSENT rather than falling back to scanning the
#   directory. So an installation can put every file in exactly the right place
#   and produce an application with no icon — which is what happened, and what
#   the four cache and index tests below are about.
#
# © Manish Jagdish Thatte
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from test_cli import Fixture

from cormani import configure


class TestDesktopEntry(Fixture):
    """Being pinnable, which needs three things to line up."""

    def paths(self):
        from cormani.platform.paths import Paths
        return Paths()

    def install(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = configure.install_desktop(paths=self.paths())
        return code, out.getvalue()

    def entry_text(self):
        return (self.paths().applications / "cormani.desktop").read_text()

    def test_the_entry_and_the_icon_are_both_written(self):
        code, text = self.install()
        self.assertEqual(code, 0)
        self.assertTrue((self.paths().applications / "cormani.desktop").exists())
        self.assertTrue((self.paths().icon_theme / "cormani.svg").exists())

    def test_the_icon_goes_into_hicolor_so_every_theme_finds_it(self):
        self.install()
        installed = self.paths().icon_theme / "cormani.svg"
        self.assertIn("hicolor", str(installed))
        self.assertIn("scalable", str(installed))
        self.assertTrue(installed.read_text().startswith("<?xml"))

    def test_the_icon_theme_index_is_written_so_a_cache_can_be_built(self):
        """Without an index.theme the directory is not a theme, and
        `gtk-update-icon-cache` refuses to build a cache for it — which leaves
        an installation with a STALE cache it cannot replace. GTK treats an
        existing cache as AUTHORITATIVE, so a cache older than the icon beside
        it reports the icon as absent rather than falling back to a scan. That
        is an application with no icon and every file correctly in place."""
        self.install()
        index = self.paths().icon_theme.parent.parent / "index.theme"
        self.assertTrue(index.exists())
        self.assertIn("scalable/apps", index.read_text())

    def test_the_index_names_EVERY_directory_and_not_only_ours(self):
        """`Directories=` is what GTK consults to learn which subdirectories
        are part of the theme. One that names only corMani's HIDES every icon
        anybody else installed into that tree — which is exactly what happened
        to VSCodium's while this was being written."""
        root = self.paths().icon_theme.parent.parent
        (root / "512x512" / "apps").mkdir(parents=True, exist_ok=True)
        (root / "512x512" / "apps" / "somebody-else.png").write_bytes(b"x")
        self.install()
        index = (root / "index.theme").read_text()
        self.assertIn("512x512/apps", index)
        self.assertIn("scalable/apps", index)

    def test_an_index_somebody_else_wrote_is_left_alone(self):
        # It is not ours to rewrite, and a theme we did not install may
        # describe directories by rules of its own.
        root = self.paths().icon_theme.parent.parent
        root.mkdir(parents=True, exist_ok=True)
        (root / "index.theme").write_text("[Icon Theme]\nName=Theirs\n")
        self.install()
        self.assertIn("Name=Theirs", (root / "index.theme").read_text())

    def test_a_cache_that_cannot_be_rebuilt_is_removed_rather_than_left(self):
        """No cache is better than a stale one: a stale cache is what the
        shell BELIEVES, and it believes the icon is not there."""
        from unittest import mock

        root = self.paths().icon_theme.parent.parent
        self.install()
        cache = root / "icon-theme.cache"
        cache.write_bytes(b"stale")
        with mock.patch("subprocess.run", side_effect=OSError("absent")):
            out = io.StringIO()
            with redirect_stdout(out):
                configure.install_desktop(paths=self.paths())
        self.assertFalse(cache.exists())
        self.assertIn("stale", out.getvalue())

    def test_the_exec_line_can_actually_start_it(self):
        # `Exec=cormani` was the stage 0 placeholder and there is no such
        # binary on this machine — the entry would appear and do nothing.
        self.install()
        text = self.entry_text()
        exec_line = [l for l in text.splitlines() if l.startswith("Exec=")][0]
        self.assertNotEqual(exec_line, "Exec=cormani %u")
        self.assertIn("%u", exec_line)
        import shutil
        if not shutil.which("cormani"):
            self.assertIn("-m cormani", exec_line)
            self.assertIn("Path=", text,
                          "python3 -m cormani needs the repository as its cwd")

    def test_the_working_directory_is_the_package_root_not_its_parent(self):
        # From one level up, `cormani` resolves to the repository directory,
        # which has no __main__.
        import shutil
        if shutil.which("cormani"):
            self.skipTest("an installed entry point needs no working directory")
        self.install()
        line = [l for l in self.entry_text().splitlines()
                if l.startswith("Path=")][0]
        root = Path(line[len("Path="):])
        self.assertTrue((root / "cormani" / "__main__.py").exists(), root)

    def test_the_desktop_file_name_matches_what_qt_announces(self):
        # On Wayland GNOME matches a window to its launcher by the xdg-shell
        # app_id, which Qt takes from setDesktopFileName. If the two differ the
        # running window is a second, anonymous entry that cannot be pinned.
        from cormani import APP_ID
        self.install()
        self.assertEqual((self.paths().applications / "cormani.desktop").stem,
                         APP_ID)
        source = Path("cormani/app.py").read_text()
        self.assertIn("setDesktopFileName(APP_ID)", source)

    def test_it_declares_itself_for_mailto_links(self):
        self.install()
        self.assertIn("x-scheme-handler/mailto", self.entry_text())

    def test_installing_twice_is_not_an_error(self):
        self.install()
        code, _ = self.install()
        self.assertEqual(code, 0)

    def test_it_can_be_taken_out_again(self):
        self.install()
        out = io.StringIO()
        with redirect_stdout(out):
            code = configure.uninstall_desktop(paths=self.paths())
        self.assertEqual(code, 0)
        self.assertFalse((self.paths().applications / "cormani.desktop").exists())
        self.assertFalse((self.paths().icon_theme / "cormani.svg").exists())

    def test_removing_what_was_never_installed_says_so(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = configure.uninstall_desktop(paths=self.paths())
        self.assertEqual(code, 0)
        self.assertIn("nothing was installed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
