# SPDX-License-Identifier: GPL-3.0-or-later
#
# The migration that remembers a Thunderbird import's place in a file.
#
# An imported message is a normal `message` row in a `\Local\Thunderbird\…`
# folder. What is NOT normal is the resume state: size, mtime and byte offset
# of the mbox on disk. Those cannot live on `folder` — that table's sync
# columns are IMAP's (uid_validity, uid_next) — so they have a table of their
# own, keyed by the absolute path of the file being read.
#
# NOTHING HERE IS A CREDENTIAL OR A BODY. The path is a local filesystem path
# the user pointed at; the numbers are what `stat` returned last time.
#
# © Manish Jagdish Thatte
from __future__ import annotations

MIGRATION_10 = """
-- Where a Thunderbird (or other mbox) import stopped in each file.
-- source_path is absolute and unique: the same file imported into two
-- accounts would be two rows with different folder_id values, which is why
-- the key is (account_id, source_path) rather than source_path alone.
CREATE TABLE import_folder (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    folder_id       INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    source_path     TEXT    NOT NULL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    mtime           REAL    NOT NULL DEFAULT 0,
    resume_offset   INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL,
    UNIQUE (account_id, source_path)
);
CREATE INDEX ix_import_folder_folder ON import_folder(folder_id);
"""
