# SPDX-License-Identifier: GPL-3.0-or-later
#
# The migration that lets a message be snoozed until a time.
#
# Snooze hides a message from every view until `snooze_until`, which is an
# ISO-8601 UTC string in the same shape as every other timestamp in the store.
# Empty means not snoozed. The index exists so expired snoozes can be cleared
# without scanning the whole table.
#
# © Manish Jagdish Thatte
from __future__ import annotations

MIGRATION_11 = """
ALTER TABLE message ADD COLUMN snooze_until TEXT NOT NULL DEFAULT '';
CREATE INDEX ix_message_snooze ON message(snooze_until)
    WHERE snooze_until <> '';
"""
