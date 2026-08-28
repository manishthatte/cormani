# SPDX-License-Identifier: GPL-3.0-or-later
#
# Running the filter rules, and what a run changed.
#
# The third of the three: `store/rulematch.py` decides whether a rule matches
# and can be tested with no database, `store/rules.py` reads and writes the
# rule tables, and this one PERFORMS a match — which needs the store, the
# queue, the folder table and the tracking board.
#
# ── EVERY ACTION GOES THROUGH `store/edits.py` ─────────────────────────────
#
# Not through an UPDATE of its own, and this is the whole correctness argument
# for filters in an IMAP client. `store/edits.py` writes a `pending_op` beside
# every change it makes, and `imap/queue.py` drains that to the server. A
# filter that moved a message locally without one would be a message in the
# Archive here and in the Inbox there — and the NEXT sync would notice the
# inbox copy, ingest it again, and file it again, for ever.
#
# The distinction is `store/ingest.py`'s own: ingest applies what the server
# said and queues nothing; a filter is the USER acting through a rule they
# wrote, so it queues exactly as a click would. That is also why undo works on
# a filtered message without knowing a filter was involved.
#
# ── A RUN REPORTS WHAT IT COULD NOT DO ─────────────────────────────────────
#
# An account whose server has no Junk folder is a real configuration, a tag
# somebody deleted is a real state, and a rule that names a folder in another
# account is a mistake somebody made. None of these is an exception: the run
# finishes, the message keeps whatever else the rule asked for, and the
# `Outcome` carries a sentence about the part that did not happen.
# CONVENTIONS.txt §8 — the alternative is a sync that stops on the third of
# fifteen accounts because a folder was renamed.
#
# ── SILENCE IS AN OUTCOME, NOT AN EDIT ─────────────────────────────────────
#
# The `silence` action changes nothing in the store. It exists so that mail a
# rule has already dealt with does not also ring a bell, and the notifier reads
# it off the report rather than re-deriving it. A message a rule moved out of
# the Inbox is silenced too, and that is not the same rule: a filed message is
# not new mail to be told about, whether or not anybody asked for silence.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

from . import edits as edits_repo
from . import folders as folders_repo
from . import rulematch
from . import rules as rules_mod
from . import tags as tags_repo
from . import tracking as tracking_repo
from . import attach as attach_repo

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    """What the rules did to one message."""

    message_id: int
    fired: list[int] = field(default_factory=list)       # rule ids, in order
    did: list[str] = field(default_factory=list)         # in words, for a log
    problems: list[str] = field(default_factory=list)    # what could not be done
    filed: bool = False          # it left the folder it arrived in
    silenced: bool = False       # do not notify about this one

    @property
    def matched(self) -> bool:
        return bool(self.fired)

    @property
    def quiet(self) -> bool:
        """Whether the notifier should skip it. See the header."""
        return self.silenced or self.filed


@dataclass
class RunReport:
    """A whole run. `--sync` prints this and the interface counts it."""

    considered: int = 0
    matched: int = 0
    outcomes: dict = field(default_factory=dict)         # message_id -> Outcome
    problems: list[str] = field(default_factory=list)

    def outcome(self, message_id: int) -> Outcome | None:
        return self.outcomes.get(int(message_id))

    def quiet_ids(self) -> set:
        return {mid for mid, o in self.outcomes.items() if o.quiet}

    def describe(self) -> str:
        if not self.considered:
            return "no new mail to filter"
        parts = [f"{self.matched} of {self.considered} matched a rule"]
        if self.problems:
            parts.append(f"{len(self.problems)} could not be carried out")
        return ", ".join(parts)


def run(con: sqlite3.Connection, message_ids: Sequence[int], *,
        rules: Sequence[rulematch.Rule] | None = None,
        commit: bool = True) -> RunReport:
    """Put these messages through the rules, in order.

    `rules` is a parameter so that a caller filtering two hundred arrivals
    reads the rule table once. None means read it here, which is what a single
    message from the interface wants.

    `commit` COVERS THIS MODULE'S OWN WRITES AND NOT THE EDITS. Every function
    in `store/edits.py` and `store/tags.py` opens its own `with con:` and
    commits when it leaves — which is correct, because each is one thing a user
    could have done by hand — so a run is a sequence of committed changes and
    not one transaction. What `commit=False` defers is the match counters and
    the tracking writes, so a caller already inside a transaction can keep it.
    A run interrupted half way has done half the work and left the other half
    undone, exactly as a person interrupted half way through clicking would.
    """
    report = RunReport()
    ids = [int(m) for m in message_ids]
    if not ids:
        return report
    active = list(rules if rules is not None
                  else rules_mod.list_rules(con, enabled_only=True))
    active = [r for r in active if r.is_complete and r.enabled]
    if not active:
        return report

    known = rules_mod.known_addresses(con)
    matched_rules: list[int] = []
    for message_id in ids:
        candidate = rules_mod.candidate_for(con, message_id, known_senders=known)
        if candidate is None:
            continue                    # gone between the sync and here
        report.considered += 1
        outcome = Outcome(message_id=message_id)
        for rule in active:
            if not rulematch.matches(rule, candidate):
                continue
            outcome.fired.append(int(rule.id))
            matched_rules.append(int(rule.id))
            _perform(con, rule, candidate, outcome)
            if rule.stop_after:
                break
        if outcome.matched:
            report.matched += 1
            report.outcomes[message_id] = outcome
            report.problems.extend(outcome.problems)
    rules_mod.note_match(con, matched_rules, commit=False)
    if commit:
        con.commit()
    return report


def _perform(con: sqlite3.Connection, rule: rulematch.Rule,
             candidate: rulematch.Candidate, outcome: Outcome) -> None:
    for action in rule.actions:
        if action.is_terminal and outcome.filed:
            # A second move in one run is not an error and is not performed:
            # the message is already somewhere else, and moving it again would
            # queue a second op the server would have to reconcile.
            outcome.problems.append(
                f"“{rule.name}”: already filed, so {action.kind} was not done")
            continue
        try:
            _one(con, action, candidate, outcome, rule)
        except sqlite3.Error as exc:                        # pragma: no cover
            log.warning("filter %s: %s failed: %s", rule.name, action.kind, exc)
            outcome.problems.append(f"“{rule.name}”: {action.kind} failed")


def _one(con: sqlite3.Connection, action: rulematch.Action,
         candidate: rulematch.Candidate, outcome: Outcome,
         rule: rulematch.Rule) -> None:
    ids = [candidate.message_id]
    kind = action.kind

    if kind == "move":
        _move(con, action, candidate, outcome, rule)
    elif kind == "delete":
        _to_role(con, folders_repo.ROLE_TRASH, candidate, outcome, rule,
                 "moved to Trash")
    elif kind == "junk":
        _to_role(con, folders_repo.ROLE_JUNK, candidate, outcome, rule,
                 "moved to Junk")
    elif kind == "tag":
        # The FOREIGN KEY makes this unreachable through `save_rule` — deleting
        # a tag cascades the action away rather than leaving it pointing at
        # nothing, which `test_rules` asserts. The guard is for a store somebody
        # opened with sqlite3 and edited, which is a file on their own disk.
        tag = tags_repo.get_tag(con, int(action.tag_id or 0))
        if tag is None:
            outcome.problems.append(
                f"“{rule.name}”: the tag it names has been deleted")
            return
        tags_repo.set_on_messages(con, ids, tag.id, True)
        outcome.did.append(f"tagged {tag.name}")
    elif kind in ("flag", "unflag"):
        edits_repo.set_flagged(con, ids, kind == "flag")
        outcome.did.append("flagged" if kind == "flag" else "unflagged")
    elif kind in ("mark_read", "mark_unread"):
        edits_repo.set_seen(con, ids, kind == "mark_read")
        outcome.did.append("marked read" if kind == "mark_read" else "marked unread")
        if kind == "mark_read":
            # Read is not new. Telling somebody about mail a rule has already
            # marked read is the notification everybody turns off.
            outcome.silenced = True
    elif kind == "track":
        _track(con, action, candidate, outcome, rule)
    elif kind == "silence":
        outcome.silenced = True
        outcome.did.append("silenced")
    else:                                                    # pragma: no cover
        outcome.problems.append(f"“{rule.name}”: {kind} is not an action")


def _move(con: sqlite3.Connection, action: rulematch.Action,
          candidate: rulematch.Candidate, outcome: Outcome,
          rule: rulematch.Rule) -> None:
    """A move names a folder, or a role resolved in the arriving account."""
    if action.folder_id is not None:
        row = con.execute("SELECT account_id, path FROM folder WHERE id = ?",
                          (int(action.folder_id),)).fetchone()
        if row is None:
            outcome.problems.append(
                f"“{rule.name}”: the folder it names is gone")
            return
        if int(row["account_id"]) != candidate.account_id:
            # A rule scoped to every account, naming one account's folder. Not
            # a move to nowhere — a rule that cannot mean what it says, and the
            # dialog should not have let it be written. Reported, not guessed.
            outcome.problems.append(
                f"“{rule.name}”: that folder belongs to another account")
            return
        edits_repo.move_to_folder(con, [candidate.message_id],
                                  int(action.folder_id))
        outcome.filed = True
        outcome.did.append(f"moved to {row['path']}")
        return
    _to_role(con, action.value.strip(), candidate, outcome, rule,
             f"moved to {folders_repo.ROLE_LABELS.get(action.value.strip(), action.value)}")


def _to_role(con: sqlite3.Connection, role: str, candidate: rulematch.Candidate,
             outcome: Outcome, rule: rulematch.Rule, said: str) -> None:
    if not role:
        outcome.problems.append(f"“{rule.name}”: a move with nowhere to move to")
        return
    moved, skipped = edits_repo.move_to_role(con, [candidate.message_id], role)
    if skipped:
        label = folders_repo.ROLE_LABELS.get(role, role)
        outcome.problems.append(
            f"“{rule.name}”: this account has no {label} folder")
        return
    if moved:
        outcome.filed = True
        outcome.did.append(said)


def _track(con: sqlite3.Connection, action: rulematch.Action,
           candidate: rulematch.Candidate, outcome: Outcome,
           rule: rulematch.Rule) -> None:
    """File the message on a tracked thread, making the thread if need be.

    THE THREAD IS MADE WITH THE MESSAGE'S OWN DATE and not with today's, which
    `tracking.create_thread` documents as load-bearing: the address matcher
    bounds itself by the thread's first touch and falls back to `created_at`,
    so a thread claiming to have begun this morning files none of the mail that
    led to it.

    The whole conversation goes on with it — `attach.attach_message`'s default —
    because a rule that filed one message of an exchange and left the other
    nine in triage is a rule that made more work than it saved.
    """
    title = action.value.strip()
    if not title:
        outcome.problems.append(f"“{rule.name}”: track what?")
        return
    slug = tracking_repo.slugify(title)
    thread = tracking_repo.by_slug(con, slug)
    if thread is None:
        row = con.execute("SELECT date_at, received_at FROM message WHERE id = ?",
                          (candidate.message_id,)).fetchone()
        began = (row["date_at"] or row["received_at"]) if row else ""
        thread_id = tracking_repo.create_thread(
            con, title, slug=slug, created_at=began or "", commit=False)
        outcome.did.append(f"opened the thread “{title}”")
    else:
        thread_id = thread.id
    attach_repo.attach_message(con, thread_id, candidate.message_id, commit=False)
    outcome.did.append(f"filed on “{title}”")


def preview(con: sqlite3.Connection, rule: rulematch.Rule, *,
            limit: int = 50) -> list[int]:
    """Which messages already in the store this rule WOULD match.

    Nothing is changed. This is what makes a filter dialog worth using — a rule
    is a guess about a pattern in mail nobody has read, and the only honest way
    to check one is against mail that already arrived. A rule matching 11,000
    messages is a rule to think again about, and `limit` is why the answer
    arrives rather than the interface stopping.
    """
    if not rule.conditions:
        return []
    where, params = ["1 = 1"], []
    if rule.account_id is not None:
        where.append("f.account_id = ?")
        params.append(int(rule.account_id))
    rows = con.execute(f"""
        SELECT m.*, f.account_id AS account_id, f.role AS folder_role
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE {' AND '.join(where)}
        ORDER BY m.date_at DESC LIMIT ?""", [*params, max(1, int(limit)) * 40]
    ).fetchall()
    known = rules_mod.known_addresses(con)
    hit: list[int] = []
    # Enabled or not: a rule being written is disabled and previewing it is the
    # entire point, so the preview asks the CONDITIONS rather than `matches`.
    asking = rule.with_changes(enabled=True, actions=rule.actions or
                               (rulematch.Action(kind="silence"),))
    for row in rows:
        candidate = rulematch.candidate_from_row(row, known_senders=known)
        if rulematch.matches(asking, candidate):
            hit.append(int(row["id"]))
            if len(hit) >= limit:
                break
    return hit


def run_over_folder(con: sqlite3.Connection, folder_id: int, *,
                    limit: int = 2000) -> RunReport:
    """Run every enabled rule over a folder that is already here.

    The second way filters are used, and the reason `store/rules.py` matches
    only what the store holds. Bounded, because "run my filters on this folder"
    over fifteen years of mail is a request the interface must be able to
    finish.
    """
    rows = con.execute(
        "SELECT id FROM message WHERE folder_id = ? AND deleted = 0 "
        "ORDER BY date_at DESC LIMIT ?",
        (int(folder_id), max(1, int(limit)))).fetchall()
    return run(con, [int(r["id"]) for r in rows])
