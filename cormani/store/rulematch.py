# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a filter rule IS, and whether a message matches it.
#
# Pure. No sqlite3, no Qt, nothing that has to be set up first: a rule and a
# candidate go in and a yes or a no comes out, and `tests/test_rulematch.py`
# runs the whole of it with no database at all. Reading and writing the rule
# tables is `store/rules.py`; performing what a match MEANS is
# `store/rulerun.py`.
#
# ── THE SEAM IS A CLAIM THE OLD FILE MADE AND DID NOT KEEP ─────────────────
#
# These two modules were one until the 600-line rule fired on it, and the seam
# was not a new idea: that file's own header said "deciding is pure and can be
# tested with no database at all" while holding two hundred and fifty lines of
# SQL. The split is that sentence, made true, and a test in
# `tests/test_rulematch.py` asserts it stays true — the module must not import
# sqlite3, in the same spirit as `tests/test_packaging.py` refusing a Qt import
# outside `ui/`.
#
# ── WHY `candidate_from_row` IS ON THIS SIDE OF IT ─────────────────────────
#
# It reads a mapping and returns a Candidate; it never queries, so it CAN be
# here. What decides that it must is what adding a condition field costs:
# `FIELDS`, an accessor, a `Candidate` field and the column it is read from.
# Three of those on one side of a seam and the fourth on the other is a seam
# that invites a field nobody fills in — which fails as a condition that is
# quietly always false, the worst way for a filter to be wrong.
#
# ── THE MATCHER SEES ONLY WHAT THE STORE HOLDS ─────────────────────────────
#
# The obvious design is to match against the message as it arrives, headers and
# all, because that is the moment a filter runs. It is wrong, and the reason is
# the second way filters are used: RUNNING THEM AGAIN over mail already in the
# store — after writing a rule, after fixing one, after an import. That path
# has a `message` row and nothing else.
#
# If a condition could look at a header the row does not keep, the same rule
# would match on arrival and not match on a re-run, and a person would have no
# way to tell which answer was the real one. So a `Candidate` is built from a
# stored row, and the arrival path builds one from the row it has just written
# rather than from the bytes it wrote it out of. There is exactly one matcher
# and it has exactly one input.
#
# The visible cost is that there is no `List-Id` condition, because no column
# holds one. `bulk` is offered instead — `imap/delivery.py` already derives it
# from List-Unsubscribe and Precedence and `store/ingest.py` writes it to the
# row — and it is the question people were asking anyway. CONVENTIONS.txt §8:
# a field that says what it can is worth more than one that guesses.
#
# ── A RULE WITH NO CONDITIONS MATCHES NOTHING ──────────────────────────────
#
# `all([])` is True, so the literal reading of "every condition holds" would
# make an empty rule match every message that ever arrives — and a rule is
# empty for exactly as long as it takes to type the first condition into the
# dialog. An empty rule that archived fifteen accounts would be the last thing
# anybody tried. It matches nothing, and `Rule.is_complete` is what the
# interface asks so it can say why.
#
# ── A REGULAR EXPRESSION IS THE USER'S, THE BODY IS NOT ─────────────────────
#
# `matches` compiles a pattern the user typed, and applies it to text a
# stranger sent. Python's `re` has no timeout, so catastrophic backtracking in
# a hand-written pattern is a sync that stops rather than an exception, and the
# input that triggers it arrives by post. Two guards, and neither is clever:
# the pattern is compiled when the rule is SAVED, so a broken one is an error
# in a dialog rather than a failure at three in the morning; and the haystack
# is capped at `_REGEX_LIMIT`, which bounds the blow-up to something that ends.
# A pattern needing more than 8 KB of body was not going to be right anyway.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

# ── The vocabulary ─────────────────────────────────────────────────────────

# What a condition can look at. The value is what the interface calls it.
FIELDS: dict[str, str] = {
    "from": "From",
    "to": "To",
    "cc": "Cc",
    "recipient": "To or Cc",
    "subject": "Subject",
    "body": "Body",
    "attachment": "Has an attachment",
    "bulk": "Is bulk mail",
    "known": "Sender is in the address book",
    "size": "Size in KB",
    "age": "Age in days",
}

# How it compares. Which of these a field offers is `ops_for` below.
OPS: dict[str, str] = {
    "contains": "contains",
    "excludes": "does not contain",
    "is": "is",
    "is_not": "is not",
    "starts": "starts with",
    "ends": "ends with",
    "matches": "matches the pattern",
    "gt": "is more than",
    "lt": "is less than",
    "is_true": "yes",
    "is_false": "no",
}

_TEXT_OPS = ("contains", "excludes", "is", "is_not", "starts", "ends", "matches")
_NUMBER_OPS = ("gt", "lt")
_BOOL_OPS = ("is_true", "is_false")

_BOOL_FIELDS = ("attachment", "bulk", "known")
_NUMBER_FIELDS = ("size", "age")

# What an action does.
ACTIONS: dict[str, str] = {
    "move": "Move to",
    "tag": "Tag with",
    "flag": "Flag it",
    "unflag": "Remove the flag",
    "mark_read": "Mark as read",
    "mark_unread": "Mark as unread",
    "delete": "Move to Trash",
    "junk": "Move to Junk",
    "track": "Put it on the tracking board",
    "silence": "Do not notify",
}

# The actions that need a target, and which column holds it.
NEEDS_FOLDER = ("move",)
NEEDS_TAG = ("tag",)

# See the header. 8 KB of body is more than any honest pattern needs and is a
# bound on how long a bad one can run.
_REGEX_LIMIT = 8192


def ops_for(field_name: str) -> tuple[str, ...]:
    """Which comparisons this field offers. The dialog builds its second box
    from this, so an impossible pair — "has an attachment starts with" — is not
    something the interface can be used to express."""
    if field_name in _BOOL_FIELDS:
        return _BOOL_OPS
    if field_name in _NUMBER_FIELDS:
        return _NUMBER_OPS
    return _TEXT_OPS


def takes_value(field_name: str) -> bool:
    return field_name not in _BOOL_FIELDS


# ── A rule, as data ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Condition:
    field: str = "from"
    op: str = "contains"
    value: str = ""
    id: int | None = None

    def describe(self) -> str:
        label = FIELDS.get(self.field, self.field)
        op = OPS.get(self.op, self.op)
        if not takes_value(self.field):
            return f"{label}: {op}"
        return f"{label} {op} “{self.value}”"


@dataclass(frozen=True)
class Action:
    kind: str = "tag"
    folder_id: int | None = None
    tag_id: int | None = None
    value: str = ""             # a folder ROLE, when a move names no folder
    id: int | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether this action takes the message out of the folder it is in.
        Two of them in one rule is not an error but the second is a no-op, and
        `rulerun` reports it rather than performing it twice."""
        return self.kind in ("move", "delete", "junk")


@dataclass(frozen=True)
class Rule:
    id: int | None = None
    name: str = ""
    account_id: int | None = None       # None: every account
    enabled: bool = True
    match_all: bool = True
    stop_after: bool = False
    sort_order: int = 0
    match_count: int = 0
    last_matched_at: str | None = None
    conditions: tuple[Condition, ...] = field(default=())
    actions: tuple[Action, ...] = field(default=())

    @property
    def is_complete(self) -> bool:
        """Whether this rule could do anything. A rule missing either half is
        kept, disabled or not — half a rule is a rule somebody is still
        writing — but it never runs, and the dialog says which half."""
        return bool(self.conditions and self.actions)

    @property
    def incomplete_reason(self) -> str:
        if not self.conditions:
            return "no conditions, so it would match nothing"
        if not self.actions:
            return "no actions, so it would do nothing"
        return ""

    def describe(self) -> str:
        """The rule in one line, for the list and for --filters."""
        if not self.conditions:
            return "matches nothing yet"
        joiner = " and " if self.match_all else " or "
        return joiner.join(c.describe() for c in self.conditions)

    def with_changes(self, **changes) -> "Rule":
        return replace(self, **changes)


# ── What the matcher sees ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One message, as a condition can look at it.

    Built from a stored row, never from the wire — see the header. `account_id`
    and `folder_role` are not condition fields; they are what decides whether a
    rule applies to this message at all.
    """

    message_id: int = 0
    account_id: int = 0
    folder_role: str = ""
    from_name: str = ""
    from_addr: str = ""
    to_addrs: str = ""
    cc_addrs: str = ""
    bcc_addrs: str = ""
    subject: str = ""
    body: str = ""
    has_attachment: bool = False
    is_bulk: bool = False
    sender_known: bool = False
    size_bytes: int = 0
    age_days: float = 0.0

    @property
    def sender(self) -> str:
        """What a From condition is compared against: the name AND the address,
        because a person writes "from Frances" as readily as they write the
        address, and matching only one of the two makes half of those rules
        silently never fire."""
        return f"{self.from_name} {self.from_addr}".strip()

    @property
    def recipients(self) -> str:
        return " ".join(x for x in (self.to_addrs, self.cc_addrs,
                                    self.bcc_addrs) if x)


def _haystack(candidate: Candidate, field_name: str) -> str:
    return {
        "from": candidate.sender,
        "to": candidate.to_addrs,
        "cc": candidate.cc_addrs,
        "recipient": candidate.recipients,
        "subject": candidate.subject,
        "body": candidate.body,
    }.get(field_name, "")


def _flag(candidate: Candidate, field_name: str) -> bool:
    return {
        "attachment": candidate.has_attachment,
        "bulk": candidate.is_bulk,
        "known": candidate.sender_known,
    }.get(field_name, False)


def _number(candidate: Candidate, field_name: str) -> float:
    if field_name == "size":
        return candidate.size_bytes / 1024.0
    if field_name == "age":
        return candidate.age_days
    return 0.0


def test(condition: Condition, candidate: Candidate) -> bool:
    """Whether one condition holds. Never raises.

    A condition that cannot be evaluated is FALSE rather than an exception: it
    runs inside a sync, over mail from strangers, and a rule nobody can see is
    not worth stopping fifteen accounts for. The one thing that could raise —
    a bad pattern — cannot reach here, because `validate` refuses it at the
    dialog.
    """
    name, op = condition.field, condition.op
    if name in _BOOL_FIELDS:
        want = op == "is_true"
        return _flag(candidate, name) is want
    if name in _NUMBER_FIELDS:
        try:
            wanted = float(condition.value)
        except (TypeError, ValueError):
            return False
        got = _number(candidate, name)
        return got > wanted if op == "gt" else got < wanted

    hay = _haystack(candidate, name)
    needle = condition.value or ""
    if op == "matches":
        try:
            return re.search(needle, hay[:_REGEX_LIMIT],
                             re.IGNORECASE | re.DOTALL) is not None
        except re.error:                                     # pragma: no cover
            return False
    lowered, wanted = hay.lower(), needle.lower()
    if op == "contains":
        return wanted in lowered
    if op == "excludes":
        return wanted not in lowered
    if op == "is":
        return lowered.strip() == wanted.strip()
    if op == "is_not":
        return lowered.strip() != wanted.strip()
    if op == "starts":
        return lowered.strip().startswith(wanted)
    if op == "ends":
        return lowered.strip().endswith(wanted)
    return False


def applies_to(rule: Rule, candidate: Candidate) -> bool:
    """Whether this rule is even about this message.

    Separate from `matches` because it is not a condition and must not be
    reported as one: a rule scoped to one account did not FAIL to match mail in
    another, it was never asked.
    """
    if not rule.enabled or not rule.is_complete:
        return False
    if rule.account_id is not None and rule.account_id != candidate.account_id:
        return False
    return True


def matches(rule: Rule, candidate: Candidate) -> bool:
    """Whether this rule fires on this message. Pure: no store, no Qt."""
    if not applies_to(rule, candidate):
        return False
    results = [test(c, candidate) for c in rule.conditions]
    if not results:
        return False                    # see the header: never `all([])`
    return all(results) if rule.match_all else any(results)


def validate(rule: Rule) -> str:
    """What is wrong with this rule, in words, or "" if nothing is.

    Called before a rule is written, so that a pattern that cannot compile is
    a sentence in a dialog rather than a condition that silently never holds.
    """
    if not rule.name.strip():
        return "a rule needs a name"
    for condition in rule.conditions:
        if condition.field not in FIELDS:
            return f"“{condition.field}” is not something a rule can look at"
        if condition.op not in ops_for(condition.field):
            return (f"{FIELDS[condition.field]} cannot be compared with "
                    f"“{OPS.get(condition.op, condition.op)}”")
        if takes_value(condition.field) and not condition.value.strip():
            return f"{FIELDS[condition.field]} {OPS[condition.op]} what?"
        if condition.op == "matches":
            try:
                re.compile(condition.value)
            except re.error as exc:
                return f"that pattern will not compile: {exc}"
        if condition.op in _NUMBER_OPS:
            try:
                float(condition.value)
            except (TypeError, ValueError):
                return f"“{condition.value}” is not a number"
    for action in rule.actions:
        if action.kind not in ACTIONS:
            return f"“{action.kind}” is not something a rule can do"
        if action.kind in NEEDS_FOLDER and action.folder_id is None \
                and not action.value.strip():
            return "a move needs somewhere to move to"
        if action.kind in NEEDS_TAG and action.tag_id is None:
            return "a tag action needs a tag"
    return ""


# ── A stored row, as the matcher sees it ───────────────────────────────────


def candidate_from_row(row, *, known_senders: set | frozenset | tuple = ()) -> Candidate:
    """A joined message row as a Candidate. A mapping in, a Candidate out.

    `known_senders` is the caller's cache of the addresses the address book
    holds: a sync filing two hundred messages must not ask the database two
    hundred times for the same handful of them. It is a plain SET and not a
    connection because this side of the split does not have one —
    `store/rules.candidate_for` is what runs the query and fills it.
    """
    address = (row["from_addr"] or "").strip().lower()
    return Candidate(
        message_id=int(row["id"]),
        account_id=int(row["account_id"]),
        folder_role=row["folder_role"] or "",
        from_name=row["from_name"] or "",
        from_addr=row["from_addr"] or "",
        to_addrs=row["to_addrs"] or "",
        cc_addrs=row["cc_addrs"] or "",
        bcc_addrs=row["bcc_addrs"] or "",
        subject=row["subject"] or "",
        body=row["body_text"] or "",
        has_attachment=bool(row["has_attachment"]),
        is_bulk=bool(row["is_bulk"]),
        sender_known=bool(address) and address in known_senders,
        size_bytes=int(row["size_bytes"] or 0),
        age_days=_age_days(row["date_at"] or row["received_at"]),
    )


def _age_days(when: str | None) -> float:
    """How old, in days. Zero when the message carries no usable date — which
    is not "brand new" so much as "no answer", and an age condition on a
    message with no date is one the user cannot have meant either way."""
    if not when:
        return 0.0
    import datetime as dt
    try:
        stamp = dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    delta = dt.datetime.now(dt.timezone.utc) - stamp
    return max(0.0, delta.total_seconds() / 86400.0)
