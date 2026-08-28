# SPDX-License-Identifier: GPL-3.0-or-later
#
# Preferences and the command readiness registry.
#
# © Manish Jagdish Thatte
import tempfile
import unittest
from pathlib import Path

from cormani.config import settings as config_mod
from cormani.ui import autosync as autosync_mod
from cormani.ui import commands as commands_mod


class TestCommands(unittest.TestCase):
    def test_reply_and_forward_are_ready(self):
        for command_id in ("reply", "reply_all", "forward", "compose", "print"):
            self.assertTrue(commands_mod.command_ready(command_id), command_id)

    def test_snooze_follows_whether_the_modules_exist(self):
        self.assertTrue(commands_mod.command_ready("snooze"))

    def test_unknown_commands_are_treated_as_ready(self):
        self.assertTrue(commands_mod.command_ready("archive"))


class TestSettingsSave(unittest.TestCase):
    def test_save_writes_a_parseable_file(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / "cormani.toml"
        s = config_mod.Settings(theme="solarized-dark", sync_interval_minutes=10,
                                sites=["whatsapp", "linkedin"])
        written = config_mod.save(s, path=path)
        self.assertEqual(written, path)
        loaded = config_mod.load(path=path)
        self.assertEqual(loaded.theme, "solarized-dark")
        self.assertEqual(loaded.sync_interval_minutes, 10)
        self.assertEqual(loaded.sites, ["whatsapp", "linkedin"])


class TestAutoSyncFormatting(unittest.TestCase):
    def test_last_checked_before_any_sync(self):
        self.assertEqual(autosync_mod.format_last_checked(None), "Not checked yet")

    def test_last_checked_just_now(self):
        import datetime as dt
        now = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(autosync_mod.format_last_checked(now, now=now),
                         "Checked just now")


if __name__ == "__main__":
    unittest.main()
