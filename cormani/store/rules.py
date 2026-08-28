# SPDX-License-Identifier: GPL-3.0-or-later
#
# The filter rules somebody wrote, as the store keeps them.
#
# `filter_rule`, `filter_condition` and `filter_action` — migration 9 — and the
# questions asked of them, which is what every other module in `store/` is.
# What a rule MEANS and whether a message matches one is `store/rulematch.py`,
# which has no database in it at all; what a match then DOES is
# `store/rulerun.py`.
#
# ── A RULE IS READ AND WRITTEN WHOLE ───────────────────────────────────────
#
# Never a condition at a time. The dialog edits a rule in memory and saves it
# once, so a half-written rule is never a state the store can be in and a rule
# being edited cannot fire in the middle of the edit. `list_rules` is the same
# bargain from the reading side: two queries for every rule and its parts, not
# 2N+1, because a sync asks for all of them on every pass.
#
# ── THE CANDIDATE QUERY IS HERE AND THE CANDIDATE IS NOT ───────────────────
#
# `candidate_for` is a join; `rulematch.candidate_from_row` is the mapping from
# columns to fields. The first needs a connection and the second must not have
# one, and that is the whole of why they are in different files.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from typing import Sequence

from .database import utc_now
from .rulematch import (ACTIONS, Action, Candidate, Condition, Rule,
                        candidate_from_row, validate)

# ── Building a candidate from the store ────────────────────────────────────


def candidate_for(con: sqlite3.Connection, message_id: int,
                  *, known_senders: set | None = None) -> Candidate | None:
    """One message as the matcher sees it, or None if it is gone."""
    row = con.execute("""
        SELECT m.*, f.account_id AS account_id, f.role AS folder_role
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE m.id = ?""", (int(message_id),)).fetchone()
    if row is None:
        return None
    if known_senders is None:
        # No cache: ask about this one address rather than reading the whole
        # address book, which is what a single message from the interface
        # wants and what `known_addresses` would cost it.
        address = (row["from_addr"] or "").strip().lower()
        known_senders = {address} if _is_known(con, address) else set()
    return candidate_from_row(row, known_senders=known_senders)


def validate_here(con: sqlite3.Connection, rule: Rule) -> str:
    """What is wrong with this rule, INCLUDING what only the store can see.

    `rulematch.validate` is pure, so the one mistake it cannot catch is the one
    most easily made in a dialog with fifteen accounts in it: a rule scoped to
    ONE account whose move names a folder belonging to a DIFFERENT one. That
    rule can never fire correctly for any message — every message it sees
    arrives in the wrong account for its own target.
    `store/rulerun.py` reports it once per message and refuses to guess, which
    is right at the moment the rule fires and far too late for the person who
    wrote it; refusing it when it is WRITTEN is the difference between a
    sentence in a dialog and a line in a report nobody reads.

    It is here rather than in the dialog for the reason a check should never
    live in one caller: the next caller does not make it. `save_rule` calls
    this, so there is no route by which such a rule reaches the table — not
    the dialog, not an import, not a future migration — and the dialog calls
    it too, one step earlier, so that the sentence appears beside the field
    instead of as an exception.
    """
    problem = validate(rule)
    if problem:
        return problem
    for action in rule.actions:
        if action.kind != "move" or action.folder_id is None:
            continue
        row = con.execute("SELECT account_id, path FROM folder WHERE id = ?",
                          (int(action.folder_id),)).fetchone()
        if row is None:
            return "the folder this rule moves to no longer exists"
        # A RULE WITH NO ACCOUNT NAMING ONE FOLDER IS ALLOWED, and the first
        # version of this check refused it — wrongly, and ten tests said so.
        # It is not a rule that cannot mean what it says: it fires normally on
        # mail arriving in the account that owns the folder, and reports the
        # mismatch for the others. With one account configured it is the
        # ordinary thing to write. What is impossible is the case below.
        if rule.account_id is None:
            continue
        if int(row["account_id"]) != int(rule.account_id):
            return (f"“{row['path']}” belongs to a different account from the "
                    f"one this rule runs against")
    return ""


def describe_action(con: sqlite3.Connection, action: Action) -> str:
    """One action in words, with its target NAMED rather than numbered.

    Here and not in `rulematch.py` because a folder id and a tag id mean
    nothing without the store: a read-out saying "move to folder 7" is one
    nobody can check, and checking is the whole purpose of `--filters` and of
    the line under each action in the dialog.

    A MOVE TO A ROLE SAYS THAT IT IS ONE. "Move to Archive" and "move to each
    account's Archive" are different rules — the first names one folder in one
    account and the second resolves per account when it fires — and a person
    reading a list of fifteen rules cannot tell them apart unless the words do.
    """
    from . import folders as folders_repo
    from . import tags as tags_repo

    label = ACTIONS.get(action.kind, action.kind)
    if action.kind == "move":
        if action.folder_id is not None:
            row = con.execute("SELECT path FROM folder WHERE id = ?",
                              (int(action.folder_id),)).fetchone()
            return f"move to {row['path']}" if row else "move to a folder that is gone"
        role = action.value.strip()
        named = folders_repo.ROLE_LABELS.get(role, role)
        return f"move to each account's {named}" if named else "move to nowhere"
    if action.kind == "tag":
        tag = tags_repo.get_tag(con, int(action.tag_id or 0))
        return f"tag {tag.name}" if tag else "tag with a tag that is gone"
    if action.kind == "track":
        title = action.value.strip()
        return f"track it as “{title}”" if title else "track it — under no title"
    return label[0].lower() + label[1:] if label else action.kind


def known_addresses(con: sqlite3.Connection) -> set:
    """Every address the address book holds, lowercased. The cache above."""
    return {(r[0] or "").strip().lower()
            for r in con.execute(
                "SELECT value FROM handle WHERE kind = 'email'")
            if (r[0] or "").strip()}


def _is_known(con: sqlite3.Connection, address: str) -> bool:
    if not address:
        return False
    return con.execute(
        "SELECT 1 FROM handle WHERE kind = 'email' "
        "AND LOWER(value) = ? LIMIT 1", (address,)).fetchone() is not None


# ── The rules themselves ───────────────────────────────────────────────────


def list_rules(con: sqlite3.Connection, *, enabled_only: bool = False) -> list[Rule]:
    """Every rule, in the order they run, with their conditions and actions.

    Two queries and not 2N+1: a person may have fifty rules and this is called
    once per sync pass.
    """
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = con.execute(
        f"SELECT * FROM filter_rule {where} ORDER BY sort_order, id").fetchall()
    if not rows:
        return []
    ids = [int(r["id"]) for r in rows]
    marks = ",".join("?" * len(ids))
    conditions: dict[int, list[Condition]] = {}
    for r in con.execute(
            f"SELECT * FROM filter_condition WHERE rule_id IN ({marks}) "
            "ORDER BY sort_order, id", ids):
        conditions.setdefault(int(r["rule_id"]), []).append(
            Condition(field=r["field"], op=r["op"], value=r["value"] or "",
                      id=int(r["id"])))
    actions: dict[int, list[Action]] = {}
    for r in con.execute(
            f"SELECT * FROM filter_action WHERE rule_id IN ({marks}) "
            "ORDER BY sort_order, id", ids):
        actions.setdefault(int(r["rule_id"]), []).append(
            Action(kind=r["kind"],
                   folder_id=r["folder_id"] and int(r["folder_id"]),
                   tag_id=r["tag_id"] and int(r["tag_id"]),
                   value=r["value"] or "", id=int(r["id"])))
    return [_rule(r, tuple(conditions.get(int(r["id"]), ())),
                  tuple(actions.get(int(r["id"]), ()))) for r in rows]


def _rule(row, conditions: tuple, actions: tuple) -> Rule:
    return Rule(
        id=int(row["id"]), name=row["name"],
        account_id=row["account_id"] and int(row["account_id"]),
        enabled=bool(row["enabled"]), match_all=bool(row["match_all"]),
        stop_after=bool(row["stop_after"]), sort_order=int(row["sort_order"]),
        match_count=int(row["match_count"]), last_matched_at=row["last_matched_at"],
        conditions=conditions, actions=actions)


def get_rule(con: sqlite3.Connection, rule_id: int) -> Rule | None:
    for rule in list_rules(con):
        if rule.id == rule_id:
            return rule
    return None


def save_rule(con: sqlite3.Connection, rule: Rule, *, commit: bool = True) -> Rule:
    """Write a rule whole — the row, its conditions and its actions.

    WHOLE, and never a condition at a time. The dialog edits a rule in memory
    and saves it once, so a half-written rule is never a state the store can be
    in, and a rule being edited cannot fire in the middle of the edit. The
    conditions and actions are replaced rather than reconciled: they carry no
    identity a user could care about, and a diff would be code that exists to
    save two DELETEs.
    """
    # `validate_here` and not `validate`: see its own docstring. A rule that
    # runs against every account and moves into one account's folder can never
    # do what it says, and the table is not where it should be found out.
    problem = validate_here(con, rule)
    if problem:
        raise ValueError(problem)
    now = utc_now()
    if rule.id is None:
        order = rule.sort_order or _next_order(con)
        cur = con.execute(
            "INSERT INTO filter_rule (name, account_id, enabled, match_all, "
            "stop_after, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rule.name.strip(), rule.account_id, int(rule.enabled),
             int(rule.match_all), int(rule.stop_after), order, now, now))
        rule_id = int(cur.lastrowid)
    else:
        rule_id = int(rule.id)
        con.execute(
            "UPDATE filter_rule SET name = ?, account_id = ?, enabled = ?, "
            "match_all = ?, stop_after = ?, sort_order = ?, updated_at = ? "
            "WHERE id = ?",
            (rule.name.strip(), rule.account_id, int(rule.enabled),
             int(rule.match_all), int(rule.stop_after), rule.sort_order, now,
             rule_id))
        con.execute("DELETE FROM filter_condition WHERE rule_id = ?", (rule_id,))
        con.execute("DELETE FROM filter_action WHERE rule_id = ?", (rule_id,))

    for order, condition in enumerate(rule.conditions):
        con.execute(
            "INSERT INTO filter_condition (rule_id, field, op, value, sort_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (rule_id, condition.field, condition.op, condition.value, order))
    for order, action in enumerate(rule.actions):
        con.execute(
            "INSERT INTO filter_action (rule_id, kind, folder_id, tag_id, "
            "value, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
            (rule_id, action.kind, action.folder_id, action.tag_id,
             action.value, order))
    if commit:
        con.commit()
    return get_rule(con, rule_id)


def _next_order(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT COALESCE(MAX(sort_order), 0) FROM filter_rule").fetchone()
    return int(row[0]) + 1


def delete_rule(con: sqlite3.Connection, rule_id: int, *,
                commit: bool = True) -> None:
    con.execute("DELETE FROM filter_rule WHERE id = ?", (int(rule_id),))
    if commit:
        con.commit()


def set_enabled(con: sqlite3.Connection, rule_id: int, enabled: bool, *,
                commit: bool = True) -> None:
    con.execute("UPDATE filter_rule SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(bool(enabled)), utc_now(), int(rule_id)))
    if commit:
        con.commit()


def reorder(con: sqlite3.Connection, rule_ids: Sequence[int], *,
            commit: bool = True) -> None:
    """The order rules run in, which is the order they are listed in.

    ORDER IS MEANING HERE and not presentation — `stop_after` makes a rule
    above another one able to claim a message outright — so this is a write to
    the rule, not to a view of it.
    """
    for order, rule_id in enumerate(rule_ids, start=1):
        con.execute("UPDATE filter_rule SET sort_order = ? WHERE id = ?",
                    (order, int(rule_id)))
    if commit:
        con.commit()


def note_match(con: sqlite3.Connection, rule_ids: Sequence[int], *,
               commit: bool = True) -> None:
    """Count what fired. See rulesschema.py for why this and not a log."""
    if not rule_ids:
        return
    now = utc_now()
    for rule_id in rule_ids:
        con.execute(
            "UPDATE filter_rule SET match_count = match_count + 1, "
            "last_matched_at = ? WHERE id = ?", (now, int(rule_id)))
    if commit:
        con.commit()


def counts(con: sqlite3.Connection) -> dict:
    """For --check: how many rules there are, and how many do anything."""
    rules = list_rules(con)
    return {
        "rules": len(rules),
        "enabled": sum(1 for r in rules if r.enabled),
        "incomplete": sum(1 for r in rules if not r.is_complete),
        "never_matched": sum(1 for r in rules
                             if r.enabled and r.is_complete and not r.match_count),
    }
