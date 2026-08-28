# SPDX-License-Identifier: GPL-3.0-or-later
#
# The migration that makes the tracking layer.
#
# Split out of `schema.py` for the reason `calendarschema.py` was: it is one
# argument rather than an addition to another one, and `schema.py` keeps only
# the ORDER, which must stay in a single place. The rules there still bind —
# forward-only, never edited once shipped, times as ISO-8601 UTC strings,
# anything derived from a server nullable.
#
# ── THE WORD "THREAD" MEANS TWO THINGS IN THIS CODEBASE AND THIS IS ONE OF
# THEM ─────────────────────────────────────────────────────────────────────
#
# `message.thread_key` and `store/threads.py` mean a MAIL CONVERSATION: a
# References chain, derived from headers, owned by nobody, and rebuilt by
# --reindex whenever the derivation changes. The interface calls it a
# conversation and --check counts it as one.
#
# The `thread` TABLE below is the other thing: a CONVERSATION THE USER IS
# PURSUING, across every channel, authored by hand, carrying a state, a next
# action, a nudge cadence and possibly a statutory deadline. It is not derived
# from anything and --reindex must never touch it.
#
# The collision was chosen deliberately — it is PLAN.txt §4's word and the word
# the prototype used for four years — so the discipline that makes it safe is
# written here rather than assumed: EVERY module that touches either says which
# one it means in its first paragraph, and nothing named `thread*` in `store/`
# is allowed to mean both. `store/threads.py` is the conversation;
# `store/tracking.py` is this.
#
# ── WHY THE TIMELINE ROW IS CALLED A TOUCH ─────────────────────────────────
#
# The prototype called it an `event`, and stage 5 has already spent that word
# on a calendar instance. Two tables called `event` is not a naming quibble in
# a store where a meeting IS one of the things that goes on a timeline — the
# join would have had to say which `event` it meant, in SQL, for ever. A touch
# is one thing that happened on a tracked thread: a message sent or received, a
# call logged, a meeting held, a note written.
#
# A TOUCH POINTS AT WHAT IT CAME FROM AND NEVER COPIES IT. `message_id` and
# `cal_event_id` are the two sources this application already holds, and both
# are ON DELETE SET NULL rather than CASCADE: a message deleted from the server
# does not delete the fact that it arrived. The timeline keeps `subject` and
# `body` of its own for the touches that have no row anywhere else — a phone
# call has no message, and that is the whole point of logging one.
#
# ── WHY `wrote_to` IS A TABLE AND NOT A QUERY ──────────────────────────────
#
# It is the set of addresses the user has ever written to, and it is what
# separates "a person I am in correspondence with" from the other 28,000
# messages in a real mailbox. Derived from the Sent folders, rebuildable, and
# dropping it costs a rebuild and nothing else. It is a table because the
# triage query filters on it on every page load, and the honest query — a
# correlated scan of every Sent message's To and Cc — was the difference
# between a queue that renders and one that hangs.
#
# ── THE THREE MESSAGE COLUMNS ──────────────────────────────────────────────
#
# `is_bulk`, `is_bounce` and the three bounce fields are on `message` rather
# than in a table of their own because every one of them is a property of the
# message that a WHERE clause asks about. Triage without `is_bulk` is a
# landfill; the bounce guard without `bounce_rcpt` knows only what it is told,
# and until now nothing told it — `contacts.note_bounce` has been written and
# uncalled since stage 4.
#
# © Manish Jagdish Thatte
from __future__ import annotations

# --------------------------------------------------------------------------
# 8 — the tracking layer: threads across channels, and the queue of what is
#     not yet on one.
#
# STATE IS A SMALL SET AND IS NOT DERIVED. "Replied" and "awaiting" look
# derivable from the direction of the last touch, and are not: a thread can be
# awaiting something that was asked for by telephone, and blocked is a fact
# about the world rather than about the mailbox. What IS derived, and therefore
# has no column, is silence, the effective due date and whether a nudge is
# overdue — `store/tracking.decorate` computes all three, because a stored
# `overdue` is a lie the moment the clock passes it.
#
# A DEADLINE IS NOT A NUDGE AND THEY ARE TWO COLUMNS FOR THAT REASON.
# `due_date` is soft: when to follow up, and it defaults to the last touch plus
# the cadence. `deadline_date` is hard — a VAT return, a confirmation
# statement, a filing — and no amount of polite reminding satisfies it. Storing
# one number for both would let a follow-up sent on the day count as the
# deadline met.
#
# `slug` IS STABLE AND HUMAN-READABLE, which the prototype earned the hard way:
# a thread is referred to from notes, from commit messages and from the user's
# own memory, and an integer id is none of those. UNIQUE, and never reused.
#
# THE QUEUE OF DISMISSALS IS A TABLE AND NOT A FLAG ON THE MESSAGE. It is a
# decision the user made — "this needs no answer" — and it must survive a
# --resync, which throws the message rows away and fetches them again. So it is
# keyed on the MESSAGE-ID rather than on the row id, and it holds the reason.
# --------------------------------------------------------------------------
MIGRATION_8 = """
CREATE TABLE thread (
    id             INTEGER PRIMARY KEY,
    -- Stable, human-readable, unique, never reused. See the note above.
    slug           TEXT    NOT NULL UNIQUE,
    title          TEXT    NOT NULL,
    org            TEXT    NOT NULL DEFAULT '',
    -- The user's own categories, free text over a seed list rather than an
    -- enum: a new one is data and not a migration.
    track          TEXT    NOT NULL DEFAULT 'correspondence',
    -- open | awaiting | replied | blocked | closed | dead
    state          TEXT    NOT NULL DEFAULT 'open',
    priority       INTEGER NOT NULL DEFAULT 3,     -- 1 highest .. 5 lowest
    -- Days of silence after which a nudge is due, when no due_date is set.
    cadence_days   INTEGER NOT NULL DEFAULT 7,
    due_date       TEXT,                           -- soft: when to nudge
    deadline_date  TEXT,                           -- hard: cannot slip
    deadline_note  TEXT    NOT NULL DEFAULT '',
    next_action    TEXT    NOT NULL DEFAULT '',
    note           TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE INDEX ix_thread_state    ON thread(state);
CREATE INDEX ix_thread_track    ON thread(track);
CREATE INDEX ix_thread_due      ON thread(due_date);
CREATE INDEX ix_thread_deadline ON thread(deadline_date);

CREATE TABLE thread_contact (
    thread_id  INTEGER NOT NULL REFERENCES thread(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, contact_id)
) WITHOUT ROWID;
CREATE INDEX ix_thread_contact_contact ON thread_contact(contact_id);

CREATE TABLE touch (
    id           INTEGER PRIMARY KEY,
    thread_id    INTEGER NOT NULL REFERENCES thread(id) ON DELETE CASCADE,
    contact_id   INTEGER REFERENCES contact(id) ON DELETE SET NULL,
    -- email | phone | whatsapp | linkedin | x | facebook | meeting | note | …
    -- Free text over a seed list, so that adding a channel is data.
    channel      TEXT    NOT NULL,
    direction    TEXT    NOT NULL,          -- in | out | note
    occurred_at  TEXT    NOT NULL,          -- UTC
    subject      TEXT    NOT NULL DEFAULT '',
    body         TEXT    NOT NULL DEFAULT '',
    -- Where this came from: attached | logged | queued. `attached` means a
    -- matcher filed it and a re-run must not file it twice; `logged` means a
    -- person typed it and nothing derived may ever remove it.
    source       TEXT    NOT NULL DEFAULT 'logged',
    status       TEXT    NOT NULL DEFAULT '',   -- sent | received | bounced
    -- SET NULL and not CASCADE: a message deleted from the server does not
    -- delete the fact that it arrived.
    message_id   INTEGER REFERENCES message(id) ON DELETE SET NULL,
    cal_event_id INTEGER REFERENCES event(id) ON DELETE SET NULL,
    -- The correspondent's own identifier where there is one — a Message-ID, a
    -- call reference. Carried, never parsed, and what makes filing idempotent.
    ext_id       TEXT,
    to_repr      TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    UNIQUE (thread_id, ext_id)
);
CREATE INDEX ix_touch_thread   ON touch(thread_id, occurred_at);
CREATE INDEX ix_touch_when     ON touch(occurred_at);
CREATE INDEX ix_touch_channel  ON touch(channel);
CREATE INDEX ix_touch_message  ON touch(message_id);
CREATE INDEX ix_touch_contact  ON touch(contact_id);

CREATE TABLE wrote_to (
    address    TEXT PRIMARY KEY,
    first_at   TEXT,
    last_at    TEXT,
    n          INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;

-- WHICH DELIVERY FAILURES HAVE ALREADY BEEN COUNTED. Keyed on the DSN's own
-- Message-ID for the same reason `triage_dismissed` is keyed on one: a message
-- row is idempotent on (folder, uid) and nothing else, so an interrupted sync
-- re-writes it and --resync discards and re-fetches it. Without this, an
-- address that bounced once reads as having bounced four times because the
-- import was restarted, and a count that inflates by itself is a count nobody
-- can act on.
CREATE TABLE bounce_seen (
    message_key TEXT PRIMARY KEY,
    address     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT '',
    seen_at     TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX ix_bounce_seen_address ON bounce_seen(address);

-- Keyed on the RFC 5322 Message-ID and not on a row, because the decision must
-- survive --resync, which discards every message row and fetches them again.
CREATE TABLE triage_dismissed (
    message_key TEXT PRIMARY KEY,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
) WITHOUT ROWID;

ALTER TABLE message ADD COLUMN is_bulk INTEGER NOT NULL DEFAULT 0;
ALTER TABLE message ADD COLUMN is_bounce INTEGER NOT NULL DEFAULT 0;
-- The address the delivery failed FOR, which is the whole value of a DSN: the
-- guard needs to know who cannot be reached, not that something came back.
ALTER TABLE message ADD COLUMN bounce_rcpt TEXT NOT NULL DEFAULT '';
ALTER TABLE message ADD COLUMN bounce_status TEXT NOT NULL DEFAULT '';
ALTER TABLE message ADD COLUMN bounce_diag TEXT NOT NULL DEFAULT '';

-- The triage query filters on all three at once, and the prototype measured
-- what happens without the composite: SQLite picks the narrowest single index,
-- matches tens of thousands of rows and filters them by hand — two seconds on
-- every page load, because the rail carries the count.
CREATE INDEX ix_message_triage ON message(is_bulk, date_at);
CREATE INDEX ix_message_bounce ON message(is_bounce, bounce_rcpt);
CREATE INDEX ix_message_from_date ON message(from_addr, date_at);
"""
