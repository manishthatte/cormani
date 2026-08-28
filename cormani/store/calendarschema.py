# SPDX-License-Identifier: GPL-3.0-or-later
#
# The migrations that make a calendar.
#
# Split out of `schema.py` when the 600-line rule fired on it, and split HERE
# rather than at some arbitrary line, because these two are one argument: 6 is
# the reading half and 7 is the writing half, and every decision in either is
# about the same question — what a client can hold when the thing it is holding
# is a function of time rather than a set of messages.
#
# The rules at the top of `schema.py` still bind: forward-only, never edited
# once shipped, times as ISO-8601 UTC strings, anything derived from a server
# nullable. `schema.py` also keeps the ORDER, which is the one thing that must
# stay in a single place — these are values, and the list that numbers them is
# there.
#
# © Manish Jagdish Thatte
from __future__ import annotations

# --------------------------------------------------------------------------
# 6 — calendars, events and attendees.
#
# THE SERVER EXPANDS RECURRENCE, AND THAT IS THE DECISION THIS SCHEMA IS BUILT
# ON. An `event` row is one INSTANCE — "the reading group, on 12 September" —
# and never a rule. Both providers will expand for a window: Google takes
# singleEvents with timeMin and timeMax, Graph has calendarView. The
# alternative is implementing RRULE, EXDATE, RDATE and their interaction with
# timezones and DST here, which is a library-sized problem, and CONVENTIONS.txt
# §3 forbids vendoring the library that solves it. A recurrence rule got subtly
# wrong is a meeting missed, and nobody notices until afterwards.
#
# The cost is honest and is recorded rather than hidden: the store holds a
# WINDOW of instances, `synced_from` and `synced_to` say which, and a view
# outside it has to fetch before it can draw. `series_id` keeps an instance's
# link to the series it belongs to, so "this and all following" can be sent to
# the server even though the rule was never parsed here.
#
# TIMES ARE UTC, AS EVERYWHERE ELSE IN THIS STORE. An ALL-DAY event is the
# exception and is a plain date: it starts on the 14th in Bombay and on the
# 14th in Berlin, and storing it as an instant would move it across midnight
# for half the world. `all_day` is what says which of the two a row is.
#
# `my_response` is on the event and duplicates the matching attendee row. It is
# the one field a list of thirty events asks for per row, and a join to
# `attendee` for each was measurably the wrong shape.
# --------------------------------------------------------------------------
MIGRATION_6 = """
CREATE TABLE calendar (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    -- The provider's own id. Google's is an address; Graph's is an opaque
    -- string. Never parsed, only carried.
    remote_id      TEXT    NOT NULL,
    name           TEXT    NOT NULL DEFAULT '',
    description    TEXT    NOT NULL DEFAULT '',
    -- The provider's colour where it gives one, so the calendar looks the same
    -- here as it does in their web interface. An account's colour is corMani's;
    -- this one is theirs.
    colour         TEXT    NOT NULL DEFAULT '',
    timezone       TEXT    NOT NULL DEFAULT '',
    is_primary     INTEGER NOT NULL DEFAULT 0,
    -- A calendar shared read-only, or a holiday feed. Writing to one is
    -- refused HERE rather than by the server, so the interface can say so
    -- before the user types anything.
    writable       INTEGER NOT NULL DEFAULT 1,
    shown          INTEGER NOT NULL DEFAULT 1,
    -- Google's syncToken or Graph's deltaLink: the provider's own bookmark for
    -- "everything that changed since". Opaque, and cleared when it expires.
    sync_token     TEXT,
    -- The window of instances this store actually holds.
    synced_from    TEXT,
    synced_to      TEXT,
    last_synced_at TEXT,
    last_error     TEXT    NOT NULL DEFAULT '',
    UNIQUE (account_id, remote_id)
);
CREATE INDEX ix_calendar_account ON calendar(account_id);

CREATE TABLE event (
    id             INTEGER PRIMARY KEY,
    calendar_id    INTEGER NOT NULL REFERENCES calendar(id) ON DELETE CASCADE,
    -- The INSTANCE's id, which for a recurring event is not the series'.
    remote_id      TEXT    NOT NULL,
    series_id      TEXT    NOT NULL DEFAULT '',
    -- The iCalendar UID, which is what an invitation in a mail message names.
    -- Stage 6's tracking layer joins on this.
    ical_uid       TEXT    NOT NULL DEFAULT '',
    etag           TEXT    NOT NULL DEFAULT '',
    summary        TEXT    NOT NULL DEFAULT '',
    description    TEXT    NOT NULL DEFAULT '',
    location       TEXT    NOT NULL DEFAULT '',
    -- UTC for a timed event; a plain YYYY-MM-DD for an all-day one.
    starts_at      TEXT    NOT NULL,
    ends_at        TEXT    NOT NULL,
    all_day        INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'confirmed',
    -- Whether it makes the user busy. A "free" event is drawn but does not
    -- count as a clash.
    busy           INTEGER NOT NULL DEFAULT 1,
    organiser_name TEXT    NOT NULL DEFAULT '',
    organiser_addr TEXT    NOT NULL DEFAULT '',
    my_response    TEXT    NOT NULL DEFAULT '',
    web_link       TEXT    NOT NULL DEFAULT '',
    recurring      INTEGER NOT NULL DEFAULT 0,
    -- Minutes before the start, for the soonest reminder the provider has.
    reminder       INTEGER,
    updated_at     TEXT,
    UNIQUE (calendar_id, remote_id)
);
-- The query every view makes: one calendar, overlapping a range.
CREATE INDEX ix_event_range ON event(calendar_id, starts_at, ends_at);
CREATE INDEX ix_event_start ON event(starts_at);
CREATE INDEX ix_event_uid   ON event(ical_uid);

CREATE TABLE attendee (
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL DEFAULT '',
    address      TEXT    NOT NULL DEFAULT '',
    response     TEXT    NOT NULL DEFAULT '',
    is_organiser INTEGER NOT NULL DEFAULT 0,
    is_self      INTEGER NOT NULL DEFAULT 0,
    optional     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_attendee_event ON attendee(event_id);
"""

# --------------------------------------------------------------------------
# 7 — writing a calendar back, and a calendar that left the provider.
#
# MIGRATION 6 IS THE READING HALF AND THIS IS THE WRITING ONE. They are two
# migrations rather than one because 6 had already run on a real store by the
# time this was written, and the rule at the top of this file is that a
# migration which has run is history.
#
# A SECOND QUEUE TABLE, NOT `pending_op`. Everything about the discipline is
# the same — id order per account, an attempt count, a failure recorded rather
# than a row deleted — and none of the COORDINATES are: `pending_op` is
# written in IMAP's, a folder and a UID, and there is no folder and no UID
# here. Storing an event id in a column called `message_id` with a foreign key
# to `message` is how a table stops meaning anything. What is shared is the
# rule and not the row.
#
# THE OP CARRIES THE ETAG THE USER ACTED ON. Both providers offer conditional
# writes — If-Match on Google, the same on Graph — and that is what turns "the
# event changed on the server while the laptop was shut" from a silent
# overwrite into a refusal this client can report. Recorded when the user
# acted, for the same reason `pending_op.source_uid` is.
#
# AN EVENT CREATED OFFLINE HAS NO REMOTE ID, so it is given one that no
# provider can produce — `local:` and a random tail — exactly as a folder
# invented here is given a `\Local\` path. It keeps the row addressable and
# keeps migration 6's UNIQUE(calendar_id, remote_id) honest until the server
# answers with an id of its own.
#
# `user_colour` EXISTS BECAUSE `colour` IS THE PROVIDER'S. A sync overwrites
# what the provider owns, every time, and a user's choice written into the
# same column would survive until the next sync and then vanish — which reads
# as the application forgetting rather than as a design.
#
# `present` IS THE FOLDER TABLE'S `subscribed` UNDER ANOTHER NAME. A calendar
# that stops being listed is not deleted, because deleting it cascades to
# every event it holds and the local store is the user's record of where they
# were. It is `shown` that the user owns; this one is the provider's answer.
# --------------------------------------------------------------------------
MIGRATION_7 = """
CREATE TABLE event_op (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    calendar_id     INTEGER NOT NULL REFERENCES calendar(id) ON DELETE CASCADE,
    -- The local row, when there still is one. NULL after the delete op that
    -- removed it, which is why this is SET NULL and not CASCADE: the op must
    -- outlive the row when the op is what deletes it.
    event_id        INTEGER REFERENCES event(id) ON DELETE SET NULL,
    kind            TEXT    NOT NULL,   -- create | update | delete | respond
    -- The server's coordinates as they stood when the user acted.
    remote_id       TEXT    NOT NULL DEFAULT '',
    etag            TEXT    NOT NULL DEFAULT '',
    -- JSON, and opaque to SQL for the same reason pending_op's is: a column
    -- per field would be migrated every time a provider invents one.
    payload         TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_error      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX ix_event_op_account  ON event_op(account_id, id);
CREATE INDEX ix_event_op_event    ON event_op(event_id);
CREATE INDEX ix_event_op_calendar ON event_op(calendar_id);

-- The marker on the row, exactly as message.pending_flags is: non-empty means
-- this event has something the server has not been told. The queue is the
-- truth; this is what the interface draws.
ALTER TABLE event ADD COLUMN pending TEXT NOT NULL DEFAULT '';
CREATE INDEX ix_event_pending ON event(pending) WHERE pending <> '';

ALTER TABLE calendar ADD COLUMN user_colour TEXT    NOT NULL DEFAULT '';
ALTER TABLE calendar ADD COLUMN present     INTEGER NOT NULL DEFAULT 1;

-- Minutes before the start, for a calendar whose events say only "the
-- default". Both providers answer "how long before" per CALENDAR and then let
-- an event say `useDefault`, so an event's own reminder is NULL far more often
-- than it is set, and a client that read only the event would never remind
-- anybody about anything.
ALTER TABLE calendar ADD COLUMN default_reminder INTEGER;

-- The back-off, per CALENDAR rather than per account, and that is the whole
-- reason these are not the account's own columns. A Google account with an app
-- password syncs mail and cannot sync calendar at all — Google issues those for
-- one and refuses them for the other — so a calendar failure that parked the
-- ACCOUNT would stop the mail of an account whose mail is working. The two
-- engines therefore keep their own counters and neither can silence the other.
ALTER TABLE calendar ADD COLUMN sync_failures   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE calendar ADD COLUMN next_attempt_at TEXT;

-- And the same three at the ACCOUNT level, because the first thing a calendar
-- sync does is ask for the list of calendars, and an account whose credential
-- is refused there has no calendar row to record it against. Without these, an
-- account that cannot authenticate would be retried on every tick for ever —
-- which is the exact failure migration 4 added the mail columns to prevent.
-- Separate columns rather than the mail ones for the reason above them: an
-- account can sync mail perfectly and be unable to read a calendar at all.
ALTER TABLE account ADD COLUMN calendar_error    TEXT    NOT NULL DEFAULT '';
ALTER TABLE account ADD COLUMN calendar_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE account ADD COLUMN calendar_next_at  TEXT;
"""
