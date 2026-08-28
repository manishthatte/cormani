# SPDX-License-Identifier: GPL-3.0-or-later
#
# Snooze: hide until a time, then return.
#
# © Manish Jagdish Thatte
import datetime as dt
import unittest

import support

from cormani.store import edits, ingest, messages, schema, snooze, views
from cormani.store.accounts import add_account
from cormani.store.database import schema_version
from cormani.store.folders import ROLE_INBOX, ensure_folder
from cormani.imap import envelope


def _message(con, folder_id, subject="Hello", uid=1):
    raw = (f"From: a@example.com\r\nTo: b@example.com\r\n"
           f"Subject: {subject}\r\nMessage-ID: <{uid}@x>\r\n"
           f"Date: Tue, 25 Aug 2026 10:00:00 +0000\r\n\r\nBody.\r\n")
    env = envelope.read(raw.encode("utf-8"))
    return ingest.store_message(con, folder_id, uid, env).message_id


class SnoozeCase(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "owner@example.com", "google")
        self.inbox = ensure_folder(self.con, self.account, "INBOX", role=ROLE_INBOX)
        self.scope = views.Scope()

    def ids(self):
        return [r.id for r in messages.fetch(self.con, self.scope, limit=50)]

    def test_schema_has_snooze_until(self):
        self.assertEqual(schema_version(self.con), schema.LATEST_VERSION)
        self.assertEqual(schema.LATEST_VERSION, 11)
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(message)")}
        self.assertIn("snooze_until", cols)

    def test_snoozed_message_is_hidden_until_its_time(self):
        mid = _message(self.con, self.inbox)
        self.assertIn(mid, self.ids())
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).replace(
            microsecond=0).isoformat()
        edits.snooze(self.con, [mid], until)
        self.assertNotIn(mid, self.ids())
        edits.clear_snooze(self.con, [mid])
        self.assertIn(mid, self.ids())

    def test_clear_expired_clears_past_snoozes(self):
        mid = _message(self.con, self.inbox)
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).replace(
            microsecond=0).isoformat()
        edits.snooze(self.con, [mid], past)
        self.assertEqual(snooze.clear_expired(self.con), 1)
        row = self.con.execute(
            "SELECT snooze_until FROM message WHERE id = ?", (mid,)).fetchone()
        self.assertEqual(row["snooze_until"], "")


if __name__ == "__main__":
    unittest.main()
