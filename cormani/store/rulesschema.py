# SPDX-License-Identifier: GPL-3.0-or-later
#
# The migration that makes filters and saved searches.
#
# Split out of `schema.py` for the reason `calendarschema.py` and
# `trackschema.py` were: it is one argument rather than an addition to another
# one, and `schema.py` keeps only the ORDER. The rules there still bind —
# forward-only, never edited once shipped, times as ISO-8601 UTC strings,
# anything derived from a server nullable.
#
# ── A RULE IS THREE TABLES AND NOT ONE ROW ─────────────────────────────────
#
# The prototype kept a filter as a row with `field`, `op`, `value` and an
# action, and every rule anybody actually wanted needed two of them: "from this
# address AND subject contains this". Conditions and actions are both lists, so
# they are both tables, and the rule row holds only what is true of the whole
# rule — its name, whether ALL conditions must hold or ANY, and whether a match
# stops the run.
#
# ── WHY A MOVE CAN NAME A ROLE INSTEAD OF A FOLDER ─────────────────────────
#
# `filter_action.folder_id` is exactly one folder in exactly one account, which
# is what a rule written for one account wants. But a rule with `account_id`
# NULL runs against fifteen accounts, and "file this in Archive" then means
# fifteen different folders. So a move action carries EITHER a `folder_id` or,
# in `value`, a folder ROLE — and the role is resolved per account, at the
# moment the rule fires, against the account the message actually arrived in.
#
# Without that, a cross-account rule could only ever have been written fifteen
# times, once per account, and the first time a server renamed a folder all
# fifteen would have been silently wrong.
#
# ── WHY THE RULE COUNTS ITS OWN MATCHES ────────────────────────────────────
#
# `match_count` and `last_matched_at` are on the rule because the question a
# person asks about a filter is not "what did it do to message 41,338", it is
# "is this thing doing anything at all" — and a filter that stopped matching
# when a correspondent changed their address is invisible from every other
# direction. Two integers answer it. A full audit log of every action on every
# message would answer more and would be a table that grows with the mailbox;
# this grows with the number of rules, which is a number a person types.
#
# ── WHY A SAVED VIEW IS JSON AND NOT COLUMNS ───────────────────────────────
#
# Everywhere else in this store, a thing is columns. A saved view is not,
# and the reason is what a view IS: `store/views.py` §1 says a view is four
# objects — a Scope, a Filters, a search Query and a Sort — and that a tab can
# be saved as the four rather than as a query somebody would have to parse
# back. That is exactly what this table holds.
#
# Nothing ever queries a saved view BY its parts. No code asks which saved
# views filter on unread, or orders them by scope; they are listed by name and
# then loaded whole. A blob is right precisely when the database is not the
# thing doing the reading — and the alternative is a migration every time
# `Filters` gains a field, for a column no WHERE clause will ever mention.
#
# The reader is tolerant by construction (`store/savedviews.py` builds each
# object with `.get` and a default), so a definition written before a field
# existed still opens. The version number is in the JSON rather than in a
# column for the same reason: it is part of the definition, not part of the
# row.
#
# CORRECTION, 27 August 2026, when the reader was written: the definition holds
# FIVE things and the paragraph above says four. `store/views.py`'s own header
# lists the parts as the scope, the filters, the order, the search AND
# `store/threads.py` — and the fifth, whether the list is grouped into
# conversations, was left out here by oversight rather than by decision. A
# saved search that reopens flat when it was saved threaded is not the view
# that was saved. It cost nothing to add, because a blob is what this column
# holds and the reader defaults what it does not find — which is the argument
# above, working.
#
# © Manish Jagdish Thatte
from __future__ import annotations

# --------------------------------------------------------------------------
# 9 — filters that run on arrival, and saved searches.
# --------------------------------------------------------------------------
MIGRATION_9 = """
-- One rule. The conditions and the actions are below; this row is what is
-- true of the rule as a whole.
CREATE TABLE filter_rule (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    -- Whose arriving mail this rule sees. NULL is every account, which is the
    -- useful default in a client built around a unified inbox.
    account_id  INTEGER REFERENCES account(id) ON DELETE CASCADE,
    enabled     INTEGER NOT NULL DEFAULT 1,
    -- 1: every condition must hold. 0: any one of them. A rule with no
    -- conditions at all matches nothing, never everything — see store/rules.py.
    match_all   INTEGER NOT NULL DEFAULT 1,
    -- Stop the run when this rule matches, so a specific rule above a general
    -- one can claim a message outright.
    stop_after  INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    -- What this rule has actually done. Not an audit log; see the header.
    match_count     INTEGER NOT NULL DEFAULT 0,
    last_matched_at TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX ix_filter_rule_order ON filter_rule(sort_order, id);

CREATE TABLE filter_condition (
    id         INTEGER PRIMARY KEY,
    rule_id    INTEGER NOT NULL REFERENCES filter_rule(id) ON DELETE CASCADE,
    -- What to look at: from, to, cc, recipient, subject, body, list, header,
    -- attachment, size, age. store/rules.py is authoritative.
    field      TEXT    NOT NULL,
    -- How to compare: contains, excludes, is, is_not, starts, ends, matches
    -- (a regular expression), gt, lt, is_true, is_false.
    op         TEXT    NOT NULL,
    value      TEXT    NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_filter_condition_rule ON filter_condition(rule_id, sort_order);

CREATE TABLE filter_action (
    id         INTEGER PRIMARY KEY,
    rule_id    INTEGER NOT NULL REFERENCES filter_rule(id) ON DELETE CASCADE,
    -- move, tag, flag, unflag, mark_read, mark_unread, delete, junk,
    -- track (put it on the tracking board), silence (no notification).
    kind       TEXT    NOT NULL,
    -- A move names EITHER this folder or, in `value`, a role — see the header.
    -- ON DELETE CASCADE rather than SET NULL: a move whose folder is gone is
    -- not a move to nowhere, it is a rule that no longer says what to do, and
    -- a rule silently doing less is worse than one that visibly lost an action.
    folder_id  INTEGER REFERENCES folder(id) ON DELETE CASCADE,
    tag_id     INTEGER REFERENCES tag(id) ON DELETE CASCADE,
    value      TEXT    NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_filter_action_rule ON filter_action(rule_id, sort_order);

-- A search somebody named and kept. The definition is the four objects a view
-- is, as JSON; see the header for why this one is not columns.
CREATE TABLE saved_view (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    definition  TEXT    NOT NULL,
    -- Drawn in the rail, or kept for the Search menu only. Both are useful:
    -- three virtual folders are a section, thirty are a wall.
    in_rail     INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX ix_saved_view_order ON saved_view(sort_order, id);
"""
