# SPDX-License-Identifier: GPL-3.0-or-later
#
# Schema and migrations.
#
# © Manish Jagdish Thatte
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cormani.store import schema
from cormani.store.database import (SchemaTooNew, connect, get_meta, migrate,
                                    open_store, schema_version, set_meta, utc_now)


class TestSchema(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_fresh_store_is_at_latest_version(self):
        con = open_store(self.path)
        self.assertEqual(schema_version(con), schema.LATEST_VERSION)
        con.close()

    def test_migrations_are_idempotent(self):
        con = open_store(self.path)
        self.assertEqual(migrate(con), [])
        con.close()

    def test_reopening_does_not_re_migrate(self):
        open_store(self.path).close()
        con = open_store(self.path)
        self.assertEqual(schema_version(con), schema.LATEST_VERSION)
        con.close()

    def test_expected_tables_exist(self):
        con = open_store(self.path)
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("account", "account_group", "identity", "folder", "message",
                  "attachment", "contact", "handle", "meta", "message_fts",
                  "pending_op"):
            self.assertIn(t, names, f"missing table {t}")
        con.close()

    def test_wal_is_enabled(self):
        # A mail client reads while it syncs. Without WAL those block each
        # other and the interface stops during every sync.
        con = open_store(self.path)
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        con.close()

    def test_foreign_keys_cascade(self):
        con = open_store(self.path)
        now = utc_now()
        con.execute("INSERT INTO account (address, provider, created_at, updated_at) "
                    "VALUES ('a@example.org','imap',?,?)", (now, now))
        acc = con.execute("SELECT id FROM account").fetchone()["id"]
        con.execute("INSERT INTO folder (account_id, path) VALUES (?, 'INBOX')", (acc,))
        con.execute("DELETE FROM account WHERE id = ?", (acc,))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM folder").fetchone()[0], 0)
        con.close()

    def test_address_is_unique(self):
        con = open_store(self.path)
        now = utc_now()
        con.execute("INSERT INTO account (address, provider, created_at, updated_at) "
                    "VALUES ('a@example.org','google',?,?)", (now, now))
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO account (address, provider, created_at, updated_at) "
                        "VALUES ('a@example.org','google',?,?)", (now, now))
        con.close()

    def test_a_newer_schema_is_refused_not_opened(self):
        # Writing to a file built by a later version could corrupt data the
        # user can still recover by running that version.
        con = open_store(self.path)
        con.execute(f"PRAGMA user_version = {schema.LATEST_VERSION + 5}")
        con.commit()
        con.close()
        con = connect(self.path)
        with self.assertRaises(SchemaTooNew):
            migrate(con)
        con.close()

    def test_meta_roundtrip_and_overwrite(self):
        con = open_store(self.path)
        self.assertIsNone(get_meta(con, "absent"))
        self.assertEqual(get_meta(con, "absent", "fallback"), "fallback")
        set_meta(con, "k", "one")
        set_meta(con, "k", "two")
        self.assertEqual(get_meta(con, "k"), "two")
        con.close()

    # ------------------------------------------------- migration 4, the queue
    def _account_and_folder(self, con):
        now = utc_now()
        con.execute("INSERT INTO account (address, provider, created_at, updated_at) "
                    "VALUES ('a@example.org','imap',?,?)", (now, now))
        acc = con.execute("SELECT id FROM account").fetchone()["id"]
        con.execute("INSERT INTO folder (account_id, path) VALUES (?, 'INBOX')", (acc,))
        fld = con.execute("SELECT id FROM folder").fetchone()["id"]
        con.execute("INSERT INTO message (folder_id, uid) VALUES (?, 7)", (fld,))
        msg = con.execute("SELECT id FROM message").fetchone()["id"]
        return acc, fld, msg

    def test_an_older_store_upgrades_in_place_without_losing_rows(self):
        # ALTER TABLE on a populated `account` is the part of migration 4 that
        # a fresh-store test cannot exercise at all.
        con = connect(self.path)
        for version, _, sql in schema.MIGRATIONS[:3]:
            con.executescript(sql)
            con.execute(f"PRAGMA user_version = {version}")
        con.commit()
        now = utc_now()
        con.execute("INSERT INTO account (address, provider, created_at, updated_at) "
                    "VALUES ('kept@example.org','google',?,?)", (now, now))
        con.commit()
        con.close()

        con = open_store(self.path)
        self.assertEqual(schema_version(con), schema.LATEST_VERSION)
        row = con.execute("SELECT address, sync_failures, last_error "
                          "FROM account").fetchone()
        self.assertEqual(row["address"], "kept@example.org")
        self.assertEqual(row["sync_failures"], 0)
        self.assertEqual(row["last_error"], "")
        con.close()

    def test_a_queued_op_outlives_the_message_it_refers_to(self):
        # The op must survive the row when the op is what deletes the row.
        # That is why message_id is SET NULL and not CASCADE.
        con = open_store(self.path)
        acc, fld, msg = self._account_and_folder(con)
        con.execute("INSERT INTO pending_op (account_id, message_id, kind, "
                    "source_folder_id, source_uid, created_at) "
                    "VALUES (?, ?, 'expunge', ?, 7, ?)", (acc, msg, fld, utc_now()))
        con.execute("DELETE FROM message WHERE id = ?", (msg,))
        row = con.execute("SELECT message_id, source_uid FROM pending_op").fetchone()
        self.assertIsNone(row["message_id"])
        self.assertEqual(row["source_uid"], 7, "the server coordinates must remain")
        con.close()

    def test_queued_ops_die_with_their_account(self):
        con = open_store(self.path)
        acc, fld, msg = self._account_and_folder(con)
        con.execute("INSERT INTO pending_op (account_id, kind, created_at) "
                    "VALUES (?, 'flag', ?)", (acc, utc_now()))
        con.execute("DELETE FROM account WHERE id = ?", (acc,))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM pending_op").fetchone()[0], 0)
        con.close()

    def test_migrations_are_sequential_from_one(self):
        # Forward-only and never renumbered; a gap means one was edited away.
        versions = [v for v, _, _ in schema.MIGRATIONS]
        self.assertEqual(versions, list(range(1, len(versions) + 1)))

    def test_timestamps_are_iso_utc(self):
        stamp = utc_now()
        self.assertTrue(stamp.endswith("+00:00"), stamp)
        self.assertEqual(stamp[4], "-")


if __name__ == "__main__":
    unittest.main()
