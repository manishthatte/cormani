# SPDX-License-Identifier: GPL-3.0-or-later
#
# The rules that make Debian packaging possible, enforced rather than hoped for.
#
# Every one of these is cheap now and expensive later: a vendored dependency, a
# missing licence header or a hardcoded home directory is trivial to fix on the
# day it appears and a sweep through the tree a year on.
#
# © Manish Jagdish Thatte
import ast
import builtins
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "cormani"


def python_sources():
    return sorted(p for p in PACKAGE.rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py"))


class TestPackaging(unittest.TestCase):
    def test_no_runtime_dependencies_are_declared(self):
        # PySide6 and keyring come from the distribution. Declaring them would
        # invite pip to install a second, vendored copy — what Debian forbids.
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["dependencies"], [])

    def test_licence_is_gpl3_or_later(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["license"], "GPL-3.0-or-later")
        self.assertTrue((ROOT / "LICENSE").exists())
        self.assertIn("GNU GENERAL PUBLIC LICENSE",
                      (ROOT / "LICENSE").read_text()[:400].upper())

    def test_every_source_carries_the_spdx_identifier(self):
        missing = [str(p.relative_to(ROOT)) for p in python_sources()
                   if "SPDX-License-Identifier: GPL-3.0-or-later"
                   not in p.read_text(encoding="utf-8")[:400]]
        self.assertEqual(missing, [], f"missing SPDX header: {missing}")

    def test_every_source_carries_the_copyright_line(self):
        # CONVENTIONS.txt §1. Sole authorship underpins the dual licence.
        missing = [str(p.relative_to(ROOT)) for p in python_sources()
                   if "© Manish Jagdish Thatte" not in p.read_text(encoding="utf-8")]
        self.assertEqual(missing, [], f"missing copyright line: {missing}")

    def test_no_hardcoded_home_directories(self):
        # One of these is how the Windows build becomes a rewrite, and how a
        # user with a relocated home loses their mail.
        bad = re.compile(r"(/home/[a-z]|~/\.cormani|C:\\\\Users)")
        offenders = []
        for p in python_sources():
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if bad.search(line) and "noqa: path" not in line:
                    offenders.append(f"{p.relative_to(ROOT)}:{n}")
        self.assertEqual(offenders, [], f"hardcoded paths: {offenders}")

    def test_no_god_files(self):
        # CONVENTIONS.txt §4.
        big = [f"{p.relative_to(ROOT)} ({len(p.read_text().splitlines())} lines)"
               for p in python_sources()
               if len(p.read_text(encoding="utf-8").splitlines()) > 600]
        self.assertEqual(big, [], f"files past 600 lines: {big}")

    def test_the_suite_imports_its_helpers_under_one_name(self):
        """`import support`, never `from tests import support`.

        Both spellings work and they are DIFFERENT MODULE OBJECTS: the second
        loads the file again as a submodule of the `tests` package, with its
        own `_APP`, its own `_SETTINGS_DIR` and its own web-profile
        redirection. Six modules spelled it the second way, and the visible
        cost was one line of Qt on every run —

            Attribute Qt::AA_ShareOpenGLContexts must be set before
            QCoreApplication is created.

        — because one copy created the QApplication at import time and the
        other then set the attribute that `docs/toolkit-verification.txt`
        finding 4 says must be set FIRST. It was harmless in the end, since the
        copy that created the application had set it correctly; what is not
        harmless is a warning nobody can act on, because it is where the next
        one hides. The same duplication would silently give the two copies
        different temporary directories to redirect panel sessions into.
        """
        offenders = []
        for path in sorted((ROOT / "tests").rglob("*.py")):
            # The IMPORTS and not the text: the paragraph above quotes the
            # spelling it forbids, and a substring check would report this
            # file. Reading what a module actually imports is also the only
            # way to be right about a line that merely mentions one.
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "tests":
                    offenders.append(str(path.relative_to(ROOT)))
                    break
        self.assertEqual(offenders, [], f"a second copy of a helper: {offenders}")

    def test_qt_is_confined_to_the_ui_and_app_modules(self):
        # Everything else must be importable, and testable, without a display.
        allowed = {"cormani/ui", "cormani/app.py"}
        offenders = []
        for p in sorted(PACKAGE.rglob("*.py")):
            rel = str(p.relative_to(ROOT.joinpath()))
            if any(rel.startswith(f"cormani/{a.split('/')[-1]}") or rel == a
                   for a in ("cormani/ui", "cormani/app.py")):
                continue
            text = p.read_text(encoding="utf-8")
            for n, line in enumerate(text.splitlines(), 1):
                if re.match(r"\s*(from|import)\s+PySide6", line):
                    # A lazy import inside a function is fine; a module-level
                    # one is what breaks a headless run.
                    if not line.startswith((" ", "\t")):
                        offenders.append(f"{rel}:{n}")
        self.assertEqual(offenders, [], f"module-level Qt import: {offenders}")

    def test_every_qt_name_used_is_actually_imported(self):
        """A Qt name that was never imported is a NameError, at PAINT TIME.

        FOUND, NOT INVENTED. `ui/messagelist.paintEvent` used `QPen` and
        `QColor` and imported neither, from stage 3 until 27 August 2026 — so
        the message list's EMPTY-STATE TEXT had never once been drawn. Every
        sentence `_update_empty_text` composes ("No messages match this
        search", and the ones a saved search needs) went to a painter that
        raised before it wrote anything.

        THE SUITE COULD NOT SEE IT AND STILL CANNOT, BY CONSTRUCTION. `paintEvent`
        runs only when a widget is painted, and this suite shows no widgets —
        `tests/support.py` says so at the top, as a requirement. So the check
        cannot be behavioural, and it is made statically instead: collect every
        name BOUND anywhere in a module — imported, assigned, defined, a
        parameter — and flag any `Q`-prefixed name loaded that is not among
        them.

        It is pyflakes' job and pyflakes is not packaged here, so this is the
        narrow version: Q-prefixed names only, and "bound anywhere in the file"
        rather than a real scope analysis. That is enough for the defect it was
        written for and gives no false positives over the package as it stands.
        """
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            bound = set(dir(builtins))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        bound.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        bound.add(alias.asname or alias.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    bound.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(
                        node.ctx, (ast.Store, ast.Del)):
                    bound.add(node.id)
                elif isinstance(node, ast.arg):
                    bound.add(node.arg)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    bound.add(node.name)
                elif isinstance(node, (ast.Global, ast.Nonlocal)):
                    bound.update(node.names)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Name)
                        and isinstance(node.ctx, ast.Load)
                        and node.id.startswith("Q") and node.id not in bound):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: {node.id}")
        self.assertEqual(offenders, [], f"Qt name never imported: {offenders}")

    def test_every_switch_the_parser_accepts_is_in_the_man_page(self):
        """A man page that documents some of the switches is worse than one
        that documents none, because it reads as complete.

        It WAS incomplete: --reindex, --install-desktop, --uninstall-desktop,
        --name and the four --imap/--smtp host and port switches were all
        accepted and none was written down. Undocumented switches are found by
        reading the source, which is not a thing to ask of somebody trying to
        add an account. Held mechanically for the same reason the SPDX header
        and the 600-line rule are.
        """
        from cormani import __main__ as entry

        wanted = {option
                  for action in entry.build_parser()._actions
                  for option in action.option_strings
                  if option.startswith("--")}
        # roff escapes a leading hyphen as \- so that it is a hyphen and not a
        # typographic minus; compare against the unescaped text.
        page = (ROOT / "man" / "cormani.1").read_text(
            encoding="utf-8").replace("\\-", "-")
        missing = sorted(o for o in wanted if o not in page)
        self.assertEqual(missing, [], f"undocumented switches: {missing}")

    PACKAGING_ASSETS = ("data/cormani.desktop", "data/cormani.svg",
                        "data/io.github.manishthatte.corMani.metainfo.xml",
                        "man/cormani.1")

    def test_desktop_and_appstream_files_exist(self):
        for name in self.PACKAGING_ASSETS:
            self.assertTrue((ROOT / name).exists(), name)

    def test_the_packaging_assets_are_actually_IN_the_repository(self):
        """Existing on this machine is not the same as being committed.

        `.gitignore` held `data/`, which ignores the DIRECTORY — and git will
        not un-ignore a file inside an ignored directory, so the icon, the
        launcher and the AppStream metadata were excluded from every commit
        from stage 0 onward. The test above passed the whole time, because the
        files were sitting right there. A fresh clone had none of them.

        Presence and tracking are different properties and only one of them
        survives a clone.
        """
        import shutil
        import subprocess

        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        for name in self.PACKAGING_ASSETS:
            result = subprocess.run(
                ["git", "check-ignore", "-q", name],
                cwd=ROOT, capture_output=True)
            # check-ignore exits 0 when the path IS ignored.
            self.assertNotEqual(result.returncode, 0,
                                f"{name} is excluded by .gitignore and would "
                                f"be missing from a fresh clone")

    def test_the_mail_store_still_cannot_reach_the_repository(self):
        """The other half of the same rule. Loosening `data/` to `data/*` must
        not have let correspondence in behind it."""
        import shutil
        import subprocess

        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        for name in ("data/cormani.sqlite3", "data/anything.eml",
                     "data/attachments/1/2/secret.pdf", "tokens/refresh.json"):
            result = subprocess.run(
                ["git", "check-ignore", "-q", name],
                cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 0,
                             f"{name} is NOT ignored and could be committed")

    def test_gitignore_excludes_the_store_and_secrets(self):
        text = (ROOT / ".gitignore").read_text()
        for pattern in ("*.sqlite3", "data/", "tokens/"):
            self.assertIn(pattern, text)


if __name__ == "__main__":
    unittest.main()
