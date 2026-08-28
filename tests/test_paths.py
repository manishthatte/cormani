# SPDX-License-Identifier: GPL-3.0-or-later
#
# XDG behaviour, including the parts commonly got wrong.
#
# © Manish Jagdish Thatte
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cormani.platform.paths import Paths


class TestPaths(unittest.TestCase):
    def test_respects_xdg_variables(self):
        with tempfile.TemporaryDirectory() as d:
            env = {f"XDG_{k}_HOME": f"{d}/{k.lower()}"
                   for k in ("CONFIG", "DATA", "CACHE", "STATE")}
            with mock.patch.dict(os.environ, env, clear=False):
                p = Paths("testapp")
                self.assertEqual(p.config, Path(d) / "config" / "testapp")
                self.assertEqual(p.data, Path(d) / "data" / "testapp")
                self.assertEqual(p.cache, Path(d) / "cache" / "testapp")
                self.assertEqual(p.state, Path(d) / "state" / "testapp")

    def test_relative_xdg_value_is_ignored(self):
        # The specification says a relative value is invalid and must be
        # treated as unset. Honouring one would put the store somewhere
        # depending on the working directory, which is how mail goes missing.
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "relative/path"}):
            p = Paths("testapp")
            self.assertTrue(p.data.is_absolute())
            self.assertNotIn("relative", str(p.data))

    def test_state_is_not_data_and_cache_is_neither(self):
        # The distinction is the reason this module exists: the store must
        # survive a cache purge, and a backup should not carry the web cache.
        p = Paths("testapp")
        self.assertNotEqual(p.data, p.cache)
        self.assertNotEqual(p.data, p.state)
        self.assertNotEqual(p.cache, p.state)

    def test_database_is_data_and_web_cache_is_cache(self):
        p = Paths("testapp")
        self.assertTrue(str(p.database).startswith(str(p.data)))
        self.assertTrue(str(p.attachments).startswith(str(p.data)))
        self.assertTrue(str(p.web_profiles).startswith(str(p.data)))
        self.assertTrue(str(p.web_cache).startswith(str(p.cache)))
        self.assertTrue(str(p.log_file).startswith(str(p.state)))

    def test_root_override_isolates_everything(self):
        with tempfile.TemporaryDirectory() as d:
            p = Paths("testapp", root=Path(d)).ensure()
            for path in (p.config, p.data, p.cache, p.state):
                self.assertTrue(path.is_dir())
                self.assertTrue(str(path).startswith(d))

    def test_ensure_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Paths("testapp", root=Path(d))
            p.ensure()
            p.ensure()
            self.assertTrue(p.data.is_dir())


if __name__ == "__main__":
    unittest.main()
