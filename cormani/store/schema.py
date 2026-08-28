# SPDX-License-Identifier: GPL-3.0-or-later
#
# The schema, as an ordered list of migrations.
#
# Forward-only and never edited once shipped. A migration that has run on a
# user's machine is history; changing it makes two databases claiming the same
# version differ, which is the one failure a version number cannot detect. To
# change something, add a migration.
#
# `PRAGMA user_version` holds the number. It is part of the file rather than a
# table, so it is readable without knowing whether the schema exists yet.
#
# Two conventions throughout:
#
# Times are ISO-8601 UTC strings, not integers. SQLite has no date type, and a
# string that sorts correctly and can be read by a human in a debugging session
# is worth more than the bytes saved. Every column holding one ends `_at`.
#
# Anything derived from a server is nullable; anything the user authored is not.
# The distinction matters when a sync half-fails: a missing subject is normal,
# a missing account name is a bug.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from .calendarschema import MIGRATION_6, MIGRATION_7
from .enhanceschema import MIGRATION_11
from .importschema import MIGRATION_10
from .rulesschema import MIGRATION_9
from .trackschema import MIGRATION_8

# --------------------------------------------------------------------------
# 1 — accounts, folders, messages, contacts.
#
# The durable core. Calendars and the correspondence-tracking tables arrive in
# later migrations, when the stages that own them are built.
# --------------------------------------------------------------------------
MIGRATION_1 = """
-- Groups the user defines and names. Accounts belong to at most one, and a
-- group reorders and collapses as a unit. Fifteen accounts in a flat rail is
-- unreadable; see docs/accounts.txt.
CREATE TABLE account_group (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    collapsed   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE account (
    id            INTEGER PRIMARY KEY,
    address       TEXT    NOT NULL UNIQUE,
    display_name  TEXT    NOT NULL DEFAULT '',
    provider      TEXT    NOT NULL,             -- google | microsoft | imap
    -- Servers are stored rather than inferred. A provider's hostnames are a
    -- fact about today, and an account whose host moves must be fixable
    -- without a new release.
    imap_host     TEXT    NOT NULL DEFAULT '',
    imap_port     INTEGER NOT NULL DEFAULT 993,
    smtp_host     TEXT    NOT NULL DEFAULT '',
    smtp_port     INTEGER NOT NULL DEFAULT 587,
    auth_method   TEXT    NOT NULL DEFAULT 'oauth2',   -- oauth2 | password
    -- Presentation, which the user owns: the rail's order is theirs, not the
    -- application's, and the colour is what makes a unified inbox readable.
    group_id      INTEGER REFERENCES account_group(id) ON DELETE SET NULL,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    colour        TEXT    NOT NULL DEFAULT '',
    -- Hidden leaves the rail but keeps the mail: in the store, in search.
    hidden        INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX ix_account_order ON account(sort_order, id);
CREATE INDEX ix_account_group ON account(group_id);

-- An account may send as several addresses. Kept separate from `account`
-- because the sending identity is not always the account that owns the mailbox.
CREATE TABLE identity (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    address      TEXT    NOT NULL,
    display_name TEXT    NOT NULL DEFAULT '',
    signature    TEXT    NOT NULL DEFAULT '',
    is_default   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_id, address)
);

CREATE TABLE folder (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    -- The server's name, exactly as IMAP gives it, including its delimiter.
    -- Never normalised: it is the key used to talk to the server again.
    path          TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    -- The RFC 6154 role, when the server declares one: inbox, sent, drafts,
    -- trash, junk, archive, all. Empty when it is an ordinary folder.
    role          TEXT    NOT NULL DEFAULT '',
    parent_id     INTEGER REFERENCES folder(id) ON DELETE CASCADE,
    subscribed    INTEGER NOT NULL DEFAULT 1,
    -- IMAP synchronisation state. uid_validity invalidates every stored UID
    -- when the server changes it, which is the whole point of the value.
    uid_validity  INTEGER,
    uid_next      INTEGER,
    highest_modseq INTEGER,
    last_synced_at TEXT,
    UNIQUE (account_id, path)
);
CREATE INDEX ix_folder_account ON folder(account_id, path);
CREATE INDEX ix_folder_parent  ON folder(parent_id);

CREATE TABLE message (
    id            INTEGER PRIMARY KEY,
    folder_id     INTEGER NOT NULL REFERENCES folder(id) ON DELETE CASCADE,
    uid           INTEGER,                       -- IMAP UID within the folder
    message_id    TEXT,                          -- RFC 5322 Message-ID
    in_reply_to   TEXT,
    -- The References chain, space separated, kept whole. Threading needs the
    -- order, so it is not split into a join table.
    references_   TEXT    NOT NULL DEFAULT '',
    -- The message-id this conversation is rooted at. Migration 5 gave it that
    -- meaning; before it, the normalised subject. store/threads.py owns it.
    thread_key    TEXT,
    date_at       TEXT,                          -- the Date header, UTC
    received_at   TEXT,                          -- when this store first saw it
    from_name     TEXT    NOT NULL DEFAULT '',
    from_addr     TEXT    NOT NULL DEFAULT '',
    to_addrs      TEXT    NOT NULL DEFAULT '',
    cc_addrs      TEXT    NOT NULL DEFAULT '',
    bcc_addrs     TEXT    NOT NULL DEFAULT '',
    reply_to      TEXT    NOT NULL DEFAULT '',
    subject       TEXT    NOT NULL DEFAULT '',
    -- Subject with the Re:/Fwd: prefixes removed, for threading and grouping.
    subject_base  TEXT    NOT NULL DEFAULT '',
    -- The plain-text body. HTML is kept separately and never rendered as-is;
    -- see CONVENTIONS.txt §7.
    body_text     TEXT    NOT NULL DEFAULT '',
    body_html     TEXT    NOT NULL DEFAULT '',
    preview       TEXT    NOT NULL DEFAULT '',   -- first line, for the list row
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    has_attachment INTEGER NOT NULL DEFAULT 0,
    -- IMAP flags, one column each: they are queried constantly and a bitfield
    -- would have to be decoded in every WHERE clause.
    seen          INTEGER NOT NULL DEFAULT 0,
    flagged       INTEGER NOT NULL DEFAULT 0,
    answered      INTEGER NOT NULL DEFAULT 0,
    draft         INTEGER NOT NULL DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0,
    -- Set while a change is made locally and not yet accepted by the server.
    -- Offline-first means the user's action wins the interface immediately and
    -- reconciles later; this column is how the reconciler finds its work.
    pending_flags TEXT    NOT NULL DEFAULT '',
    UNIQUE (folder_id, uid)
);
CREATE INDEX ix_message_folder   ON message(folder_id, date_at DESC);
CREATE INDEX ix_message_msgid    ON message(message_id);
CREATE INDEX ix_message_thread   ON message(thread_key, date_at);
CREATE INDEX ix_message_from     ON message(from_addr);
CREATE INDEX ix_message_unseen   ON message(folder_id, seen, date_at DESC);
CREATE INDEX ix_message_pending  ON message(pending_flags) WHERE pending_flags <> '';

CREATE TABLE attachment (
    id           INTEGER PRIMARY KEY,
    message_id   INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    filename     TEXT    NOT NULL DEFAULT '',
    content_type TEXT    NOT NULL DEFAULT '',
    content_id   TEXT    NOT NULL DEFAULT '',    -- for cid: references in HTML
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    part_number  TEXT    NOT NULL DEFAULT '',    -- IMAP BODYSTRUCTURE path
    -- Bodies live on disk under the attachments directory, not in the row.
    -- A 30 MB blob in a table that the message list scans makes every query
    -- slower for a payload almost never wanted.
    stored_path  TEXT    NOT NULL DEFAULT '',
    is_inline    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_attachment_message ON attachment(message_id);

-- People, and every way of reaching them. One contact, many handles: an
-- address, a phone number, a profile. The channels that have no API still have
-- handles, and a thread has to be able to name who it is with.
CREATE TABLE contact (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    org        TEXT NOT NULL DEFAULT '',
    role       TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'active',   -- active | left-org | do-not-contact
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_contact_name ON contact(name);
CREATE INDEX ix_contact_org  ON contact(org);

CREATE TABLE handle (
    id           INTEGER PRIMARY KEY,
    contact_id   INTEGER NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL,               -- email | phone | whatsapp | linkedin | x | …
    value        TEXT    NOT NULL,
    -- 'bounced' is load-bearing: the composer refuses an address that has
    -- already refused mail. Guessing an address format at a large organisation
    -- failed eleven times out of eleven in the record this replaces.
    status       TEXT    NOT NULL DEFAULT 'unverified',
    note         TEXT    NOT NULL DEFAULT '',
    bounce_count INTEGER NOT NULL DEFAULT 0,
    last_bounce_at TEXT,
    created_at   TEXT    NOT NULL,
    UNIQUE (kind, value)
);
CREATE INDEX ix_handle_contact ON handle(contact_id);

-- Small key/value for things that are genuinely singular: the last sync time,
-- the window geometry, the triage horizon. Not a dumping ground — anything
-- with more than one instance gets a table.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# --------------------------------------------------------------------------
# 2 — full-text search.
#
# Separate from migration 1 so that a corrupt or outdated index can be dropped
# and rebuilt by re-running one migration rather than the whole schema.
#
# `content=''` makes this an external-content index: the text is not stored
# twice. The cost is that the index must be maintained explicitly rather than
# by triggers — deliberate, because a trigger firing on every row of a
# hundred-thousand-message import turns a minute into an hour.
# --------------------------------------------------------------------------
MIGRATION_2 = """
CREATE VIRTUAL TABLE message_fts USING fts5(
    subject,
    body,
    from_repr,
    to_repr,
    content='',
    tokenize='porter unicode61 remove_diacritics 2'
);
"""

# --------------------------------------------------------------------------
# 3 — tags.
#
# Thunderbird's model: a tag is a name, a colour and optionally one of the keys
# 1-9. The keys are the reason tagging is fast in practice rather than a feature
# people configure once and never use.
#
# The colour is a literal value, not a theme role, for the same reason an
# account's colour is: it is the user's mark on a message and must mean the same
# thing after a theme change. A tag that turns from red to green because the
# palette inverted has lost the only thing it was for.
#
# No timestamps here. A tag has no history worth keeping — it exists, or the
# user deleted it — and a column that is never read is a column that will
# eventually be wrong.
# --------------------------------------------------------------------------
MIGRATION_3 = """
CREATE TABLE tag (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    colour     TEXT    NOT NULL DEFAULT '',
    -- 1-9, or NULL for a tag with no key. UNIQUE stops two tags claiming one
    -- key; SQLite does not treat NULLs as equal, so any number of tags may have
    -- no key at all, which is exactly the wanted behaviour.
    shortcut   INTEGER UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

-- WITHOUT ROWID: the whole row IS the key, and the table is read by joining on
-- it constantly while the quick filter is on. A rowid here would be a second
-- index over data that is already an index.
CREATE TABLE message_tag (
    message_id INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, tag_id)
) WITHOUT ROWID;
CREATE INDEX ix_message_tag_tag ON message_tag(tag_id);

-- The five Thunderbird ships with, because they are the ones people expect to
-- find already there. Seeded in the migration rather than at first run: first
-- run is a code path that only executes once and therefore only gets tested
-- once, and a migration is tested by every database that has ever opened.
INSERT INTO tag (name, colour, shortcut, sort_order) VALUES
    ('Important', '#dc322f', 1, 1),
    ('Work',      '#b58900', 2, 2),
    ('Personal',  '#859900', 3, 3),
    ('To Do',     '#268bd2', 4, 4),
    ('Later',     '#6c71c4', 5, 5);
"""

# --------------------------------------------------------------------------
# 4 — the offline queue, and what a sync leaves behind.
#
# `message.pending_flags` was reserved for this in migration 1 and is not
# enough on its own, which is the whole reason for a table. A queued FLAG
# change can be expressed against the local row; a queued MOVE cannot. By the
# time the reconciler runs, the row has already moved — that is what
# offline-first means — and the server still holds the message where it was.
# Replaying it needs the folder and the UID the message had WHEN THE USER
# ACTED, and both are gone from the row by then.
#
# So the queue is written in the SERVER'S coordinates, not the store's.
# `source_folder_id` and `source_uid` are what the reconciler talks to the
# server with; `message_id` is a convenience for the interface — "this row has
# not been accepted yet" — and is ON DELETE SET NULL rather than CASCADE
# precisely because the op must outlive the row when the op is what deletes it.
#
# `pending_flags` keeps its job as the MARKER: non-empty means this row has
# work outstanding. Migration 1's partial index over it exists for that query
# and nothing else. The queue is the truth; the column is the flag on the row.
#
# The account columns are the back-off state. An account that is failing must
# not be retried on every tick — fifteen accounts each retrying a refused
# login every five minutes is how a provider decides corMani is abusive. The
# failure count is the exponent, `next_attempt_at` is the gate, and both live
# beside the account rather than in memory so a restart does not reset a
# back-off that a provider is still counting.
# --------------------------------------------------------------------------
MIGRATION_4 = """
CREATE TABLE pending_op (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    -- The local row, when there still is one. Never the thing replayed.
    message_id       INTEGER REFERENCES message(id) ON DELETE SET NULL,
    kind             TEXT    NOT NULL,   -- flag | move | delete | expunge
    -- Where the server still believes the message to be, recorded at the
    -- moment the user acted.
    source_folder_id INTEGER REFERENCES folder(id) ON DELETE CASCADE,
    source_uid       INTEGER,
    target_folder_id INTEGER REFERENCES folder(id) ON DELETE CASCADE,
    -- JSON, and deliberately opaque to SQL: {"add": ["\\Seen"], "remove": []}.
    -- A column per flag would have to be migrated for every keyword a server
    -- invents, and servers invent them.
    payload          TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_attempt_at  TEXT,
    last_error       TEXT    NOT NULL DEFAULT ''
);
-- Drained in id order per account: a flag change and a move on one message
-- must reach the server in the order the user made them.
CREATE INDEX ix_pending_op_account ON pending_op(account_id, id);
CREATE INDEX ix_pending_op_message ON pending_op(message_id);

ALTER TABLE account ADD COLUMN last_sync_at    TEXT;
ALTER TABLE account ADD COLUMN last_error      TEXT    NOT NULL DEFAULT '';
ALTER TABLE account ADD COLUMN sync_failures   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE account ADD COLUMN next_attempt_at TEXT;
"""

# --------------------------------------------------------------------------
# 5 — threading by References.
#
# `thread_key` has held the normalised subject since migration 1, which was a
# placeholder and said so. It now holds the message-id the conversation is
# rooted at; store/threads.py argues why that is the only honest evidence, and
# why subject threading is not offered.
#
# THE INDEX IS THE HALF THAT MATTERS. Assigning a thread asks "does the store
# already know any of the ids this message names", once per message stored.
# Without an index on `message_id` that is a full scan each time, which makes a
# hundred-thousand-message first import quadratic.
#
# The recomputation below is the CLAIMED root — the first reference, then the
# message replied to, then the message's own id — followed by three passes that
# follow a claimed root to the thread its own message ended up in. That is
# store/threads.assign without the merge, expressed in SQL because the runner
# takes SQL. It is right for every thread whose members agree on their root,
# which is nearly all of them; `--reindex` runs the real thing in Python for
# the rest, and for any store whose threads are ever in doubt.
# --------------------------------------------------------------------------
MIGRATION_5 = """
CREATE INDEX IF NOT EXISTS ix_message_msgid ON message(message_id);

UPDATE message SET thread_key = COALESCE(
    NULLIF(CASE WHEN INSTR(TRIM(references_), ' ') > 0
                THEN SUBSTR(TRIM(references_), 1, INSTR(TRIM(references_), ' ') - 1)
                ELSE TRIM(references_) END, ''),
    NULLIF(TRIM(COALESCE(in_reply_to, '')), ''),
    NULLIF(TRIM(COALESCE(message_id, '')), ''),
    'id:' || id);

UPDATE message SET thread_key = (
    SELECT p.thread_key FROM message p
    WHERE p.message_id = message.thread_key AND p.thread_key <> message.thread_key)
WHERE EXISTS (
    SELECT 1 FROM message p
    WHERE p.message_id = message.thread_key AND p.thread_key <> message.thread_key);

UPDATE message SET thread_key = (
    SELECT p.thread_key FROM message p
    WHERE p.message_id = message.thread_key AND p.thread_key <> message.thread_key)
WHERE EXISTS (
    SELECT 1 FROM message p
    WHERE p.message_id = message.thread_key AND p.thread_key <> message.thread_key);

UPDATE message SET thread_key = (
    SELECT p.thread_key FROM message p
    WHERE p.message_id = message.thread_key AND p.thread_key <> message.thread_key)
WHERE EXISTS (
    SELECT 1 FROM message p
    WHERE p.message_id = message.thread_key AND p.thread_key <> message.thread_key);
"""

# 6 and 7 live in `calendarschema.py`, 8 in `trackschema.py`, 9 in
# `rulesschema.py` and 10 in `importschema.py`; only their PLACE in the order
# is here.
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "accounts, folders, messages, contacts", MIGRATION_1),
    (2, "full-text search index", MIGRATION_2),
    (3, "tags", MIGRATION_3),
    (4, "the offline queue and per-account sync state", MIGRATION_4),
    (5, "threading by References", MIGRATION_5),
    (6, "calendars, events and attendees", MIGRATION_6),
    (7, "writing a calendar back", MIGRATION_7),
    (8, "the tracking layer: threads across channels", MIGRATION_8),
    (9, "filters that run on arrival, and saved searches", MIGRATION_9),
    (10, "Thunderbird / mbox import resume state", MIGRATION_10),
    (11, "snooze until a chosen time", MIGRATION_11),
]

LATEST_VERSION = MIGRATIONS[-1][0]
