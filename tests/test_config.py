# SPDX-License-Identifier: GPL-3.0-or-later
#
# Configuration, and the boundary that keeps accounts out of it.
#
# © Manish Jagdish Thatte
import tempfile
import unittest
from pathlib import Path

from cormani.config.settings import EXAMPLE, Settings, load, save, unknown_keys


class TestSettings(unittest.TestCase):
    def _write(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "cormani.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_absent_file_gives_working_defaults(self):
        s = load(path=Path("/nonexistent/cormani.toml"))
        self.assertTrue(s.is_default)
        self.assertEqual(s.log_level, "info")
        self.assertTrue(s.block_remote_content)

    def test_shipped_example_parses(self):
        s = load(path=self._write(EXAMPLE))
        self.assertFalse(s.is_default)
        self.assertEqual(s.sync_interval_minutes, 5)

    def test_values_override_defaults(self):
        s = load(path=self._write(
            'log_level = "debug"\nsync_interval_minutes = 0\n'
            'block_remote_content = false\nchromium_flags = ["--disable-gpu"]\n'))
        self.assertEqual(s.log_level, "debug")
        self.assertEqual(s.sync_interval_minutes, 0)
        self.assertFalse(s.block_remote_content)
        self.assertEqual(s.chromium_flags, ["--disable-gpu"])

    def test_unknown_keys_are_reported_not_fatal(self):
        # The likely cause is a file written for a newer version. Refusing to
        # start over a key we do not need would be worse than ignoring it.
        p = self._write('log_level = "debug"\nfrom_the_future = true\n')
        s = load(path=p)
        self.assertEqual(s.log_level, "debug")
        self.assertEqual(unknown_keys(p), ["from_the_future"])

    def test_malformed_file_raises(self):
        # A user who edited it and made a mistake must be told, not have the
        # change silently dropped.
        import tomllib
        with self.assertRaises(tomllib.TOMLDecodeError):
            load(path=self._write("log_level = \n"))

    def test_accounts_are_not_a_setting(self):
        # The boundary this module exists to hold: the application edits
        # accounts, so they live in the database. Anything both sides edit
        # needs a merge strategy, and every merge strategy is lossy.
        self.assertNotIn("accounts", Settings.__dataclass_fields__)
        self.assertNotIn("account", Settings.__dataclass_fields__)
        self.assertNotIn("accounts", EXAMPLE.split("#")[0])

    def test_remote_content_is_blocked_by_default(self):
        # A tracking pixel is a disclosure. CONVENTIONS.txt §7.
        self.assertTrue(Settings().block_remote_content)

    def test_save_round_trips_known_fields(self):
        p = self._write('log_level = "info"\n')
        original = load(path=p)
        original.log_level = "warning"
        original.sync_interval_minutes = 10
        original.chromium_flags = ["--disable-gpu"]
        save(original, p)
        again = load(path=p)
        self.assertEqual(again.log_level, "warning")
        self.assertEqual(again.sync_interval_minutes, 10)
        self.assertEqual(again.chromium_flags, ["--disable-gpu"])


if __name__ == "__main__":
    unittest.main()
