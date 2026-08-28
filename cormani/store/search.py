# SPDX-License-Identifier: GPL-3.0-or-later
#
# Full-text search across every account: the query language, and the SQL under
# it.
#
# The Quick Filter above the list narrows what is already in front of you by
# substring. This is the other act — finding a message you cannot see, in any
# folder of any account, by what it SAYS. It runs against the `message_fts`
# index that migration 2 created and every writer has populated since.
#
# THE USER'S TEXT NEVER REACHES FTS5. That is the whole of this module's first
# half, and it is not caution — it is measured. Of fifteen ordinary things a
# person might type, twelve are FTS5 syntax errors: `"unclosed` is
# `unterminated string`, a lone `*` is `unknown special query`, and `-`, `^`,
# `(` and a trailing `AND` are each `fts5: syntax error`. Even `a:b` fails with
# `no such column: a`, which is what someone typing a URL or a time gets. A
# search box that raises OperationalError on a colon is not a search box, and
# catching the exception afterwards only converts a crash into "no results" for
# a query that was perfectly reasonable.
#
# So the text is tokenised HERE, by rules written here, and what goes to SQLite
# is an expression this module generated. The escape is a double-quoted FTS5
# string: inside one, the only special character is `"`, which doubles — and
# the tokenizer still splits its contents, so `"lyle@covalent.example"` is the
# phrase lyle+covalent+com and matches the address wherever it appears. One
# rule neutralises every operator, and `AND` typed by a user searches for the
# word `and`, which is what they meant.
#
# EXCLUSIONS ARE A SECOND MATCH, NOT `NOT` INSIDE THE FIRST. FTS5's NOT is
# binary: `NOT zzz` on its own is a syntax error, so `-holiday` — a perfectly
# sensible thing to type — has nothing to subtract from. As a separate
# `m.id NOT IN (SELECT rowid ... MATCH ...)` it is well formed at any size,
# including when it is the only thing in the query.
#
# THE SNIPPET IS COMPUTED HERE BECAUSE FTS5 CANNOT. `snippet()` and
# `highlight()` on a contentless index do not raise — they return NULL. A
# reasonable implementation ships an empty line under every result and looks
# like missing data rather than a defect, which is the kind of thing that
# survives a stage. The index stores no text to snippet FROM; the `message`
# table does, so the centring is done against that.
#
# AND THE INDEX STEMS WHILE A SUBSTRING SCAN DOES NOT. A search for `invoices`
# matches a body that says `invoice`, and then the scan looking for the word to
# centre on finds nothing. It falls back to a prefix of the term, and if that
# fails too the row keeps its ordinary preview — a wrong snippet would be worse
# than the honest one. CONVENTIONS.txt §8.
#
# TRASH AND JUNK ARE OUT UNLESS ASKED FOR. Delete here means move to Trash, so
# a search that ranked a message the user threw away beside the one they kept
# would make the box worse at its main job — and Junk is, by definition, mail
# they have already been told to ignore. Both are excluded by default and the
# `discarded` field puts them back. The exclusion is NOT silent: the list's
# footer says how many more are in them, because a search that quietly narrows
# what it looked at is the "quietly wrong" that CONVENTIONS.txt §8 forbids.
#
# WHAT IS DELIBERATELY NOT OFFERED: boolean operators. `AND`, `OR`, `NEAR` and
# parentheses are English words to everyone who has not read the FTS5 manual,
# and every term is combined with AND, which is what a person means when they
# type two words. `-word` excludes, `"a phrase"` is a phrase, `word*` matches
# by prefix, and `from:`, `to:`, `subject:` and `body:` restrict a term to one
# part of the message. That is the whole language.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, replace

from . import folders as folders_repo
from . import times

# A field prefix, and the FTS5 column behind it. `to` covers Cc as well: the
# index puts both in `to_repr`, because a message addressed to you in Cc was
# still addressed to you and nobody searching for it remembers which.
FIELDS: dict[str, str] = {
    "from": "from_repr",
    "sender": "from_repr",
    "to": "to_repr",
    "cc": "to_repr",
    "subject": "subject",
    "body": "body",
}

# The bm25 weights, in the column order `message_fts` declares: subject, body,
# from_repr, to_repr. A hit in the subject is what someone means far more often
# than the same word buried in a quoted signature, and a hit in the sender is
# the next strongest — searching a person's name should rank their mail above a
# newsletter that mentions them. bm25 returns a NEGATIVE score and the better
# match is the more negative one, which is why "best first" sorts ascending.
_WEIGHTS = (10.0, 1.0, 5.0, 2.0)
_SCORE = f"bm25(message_fts, {', '.join(str(w) for w in _WEIGHTS)})"

# The Date chip's ranges, as (key, label). Fixed ranges rather than a date
# picker: these five are what people actually reach for, and a two-ended
# calendar dialog for "last week" is three clicks where this is one. A custom
# range can be added later without changing anything else here.
WITHIN = (
    ("", "Any time"),
    ("today", "Today"),
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
    ("year", "This year"),
    ("custom", "Custom range…"),
)
_WITHIN_LABEL = dict(WITHIN)

_TOKEN = re.compile(r'(-?)(?:([A-Za-z]+):)?(?:"([^"]*)"|(\S+))')
# A token the tokenizer would actually index something from. `unicode61` keeps
# letters, digits and the underscore and throws the rest away.
_INDEXABLE = re.compile(r"\w", re.UNICODE)

# A term that cannot match anything: the empty phrase. What a query made
# entirely of punctuation deserves — it asked for something, and the honest
# answer is no results rather than every message in fifteen accounts.
_MATCHES_NOTHING: "Term"     # defined below, once Term exists


@dataclass(frozen=True)
class Term:
    """One thing to look for. Frozen, because a Query holds these and a Query
    is part of a tab's saved state, which is compared for equality."""

    text: str
    field: str = ""             # an FTS5 column, or '' for every column
    negated: bool = False
    prefix: bool = False        # the user wrote a trailing *

    def expression(self) -> str:
        """This term as FTS5 the parser can be held responsible for."""
        quoted = '"' + self.text.replace('"', '""') + '"'
        if self.prefix:
            quoted += "*"
        return f"{self.field}:{quoted}" if self.field else quoted


_MATCHES_NOTHING = Term(text="")


def parse(text: str) -> tuple[Term, ...]:
    """A line of search text as terms.

    Nothing here can fail. Anything unrecognised is literal text — `Re:` and
    `http://x` are things people paste into a search box, and an unknown prefix
    is not an error, it is two words.
    """
    terms: list[Term] = []
    for match in _TOKEN.finditer(text or ""):
        negated = bool(match.group(1))
        prefix_name = (match.group(2) or "").lower()
        phrase, bare = match.group(3), match.group(4)
        field = FIELDS.get(prefix_name, "")
        if phrase is not None:
            body, is_prefix = phrase, False
        else:
            body = bare or ""
            if not field and prefix_name:
                # An unrecognised prefix was not a prefix. Put it back.
                body = f"{prefix_name}:{body}"
            is_prefix = body.endswith("*")
            if is_prefix:
                body = body[:-1]
        body = body.strip()
        if not _INDEXABLE.search(body):
            # Nothing the tokenizer would keep. A stray `-` or `*` between two
            # words must not become a term: quoted, it is an EMPTY phrase, and
            # an empty phrase ANDed with the rest matches nothing at all — so
            # `wavelength - question` would find none of the mail that
            # `wavelength question` finds.
            continue
        terms.append(Term(text=body, field=field, negated=negated,
                          prefix=is_prefix))
    return tuple(terms)


@dataclass(frozen=True)
class Query:
    """A search, as the interface holds it: the text, and the chips.

    The chips are separate fields rather than text the bar writes into the box,
    so that a chip can be turned off again without editing a string, and so
    that restoring a tab restores the chips rather than a query that merely
    looks like them.
    """

    text: str = ""
    sender: str = ""            # the From chip
    subject: str = ""           # the Subject chip
    attachment: bool = False    # the Attachment chip
    within: str = ""            # the Date chip: a key from WITHIN
    date_from: str = ""         # custom range start, YYYY-MM-DD local
    date_to: str = ""           # custom range end, YYYY-MM-DD local
    account_id: int | None = None   # the Account chip; None is every account
    # The Trash & Junk chip. NOT part of `active`: "include the mail I threw
    # away" is a qualifier on a search and not a search, and turning it on with
    # an empty box must not list every message in fifteen accounts.
    discarded: bool = False

    @property
    def active(self) -> bool:
        """Whether this asks anything at all. An inactive query is not run:
        matching every message in every account is not a search, it is a wait."""
        return bool(self.text.strip() or self.sender.strip()
                    or self.subject.strip() or self.attachment
                    or self.within or self.date_from or self.date_to
                    or self.account_id is not None)

    @property
    def terms(self) -> tuple[Term, ...]:
        """The text's terms, plus the two chips that are terms themselves."""
        terms = list(parse(self.text))
        if self.text.strip() and not terms:
            terms.append(_MATCHES_NOTHING)
        if self.sender.strip():
            terms.append(Term(text=self.sender.strip(), field="from_repr"))
        if self.subject.strip():
            terms.append(Term(text=self.subject.strip(), field="subject"))
        return tuple(terms)

    def with_changes(self, **changes) -> "Query":
        return replace(self, **changes)

    def describe(self) -> str:
        """What this search asked, in words, for the status bar and the empty
        list. A view that shows nothing must be able to say what it looked for."""
        parts: list[str] = []
        if self.text.strip():
            parts.append(f'“{self.text.strip()}”')
        if self.sender.strip():
            parts.append(f"from {self.sender.strip()}")
        if self.subject.strip():
            parts.append(f"subject {self.subject.strip()}")
        if self.attachment:
            parts.append("with an attachment")
        if self.within and self.within != "custom":
            parts.append(_WITHIN_LABEL.get(self.within, self.within).lower())
        if self.date_from or self.date_to:
            if self.date_from and self.date_to:
                parts.append(f"from {self.date_from} to {self.date_to}")
            elif self.date_from:
                parts.append(f"from {self.date_from}")
            else:
                parts.append(f"until {self.date_to}")
        described = ", ".join(parts) or "everything"
        return f"{described}, including Trash and Junk" if self.discarded else described


def expression(terms) -> str | None:
    """The positive terms as one FTS5 expression, or None if there are none."""
    positive = [t.expression() for t in terms if not t.negated]
    return " AND ".join(positive) if positive else None


def exclusion(terms) -> str | None:
    """The negated terms, as their own expression. See the module header for
    why they are not folded into the first one."""
    negative = [t.expression() for t in terms if t.negated]
    return " AND ".join(negative) if negative else None


# ------------------------------------------------------------------- dates
def since(within: str, now: dt.datetime | None = None) -> str | None:
    """The start of a Date chip's range, as the store stores timestamps.

    AT LOCAL MIDNIGHT, CONVERTED. The store keeps UTC and the reader lives at
    UTC+05:30; a "Today" computed from UTC midnight starts five and a half
    hours late and hides this morning's mail for that whole window — the same
    defect `ui/messagelist.to_local` exists to prevent at the other end.
    """
    if not within:
        return None
    now = (now or dt.datetime.now()).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if within == "today":
        start = midnight
    elif within == "7d":
        # Seven CALENDAR days including today, not a rolling 168 hours. A
        # message from last Tuesday afternoon is in "the last 7 days" on
        # Tuesday morning, to everyone except a subtraction.
        start = midnight - dt.timedelta(days=6)
    elif within == "30d":
        start = midnight - dt.timedelta(days=29)
    elif within == "year":
        start = midnight.replace(month=1, day=1)
    else:
        return None
    return start.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _day_start(date_text: str) -> str | None:
    """Midnight local on this calendar date, as UTC ISO."""
    text = (date_text or "").strip()
    if not text:
        return None
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        return None
    start = dt.datetime.combine(day, dt.time.min)
    return times.to_utc_text(start)


def _day_end(date_text: str) -> str | None:
    """Last instant local on this calendar date, as UTC ISO."""
    text = (date_text or "").strip()
    if not text:
        return None
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        return None
    end = dt.datetime.combine(day, dt.time(23, 59, 59))
    return times.to_utc_text(end)


# --------------------------------------------------------------------- SQL
def join_sql(query: Query | None) -> tuple[str, list]:
    """The ranking join, or nothing.

    A JOIN rather than `m.id IN (SELECT …)`, because relevance ordering needs
    the score and a subquery in the WHERE clause cannot hand one out. It is
    also the faster shape: the index yields the matching rowids and the join
    drives the message table from them, rather than the other way round.
    """
    if query is None:
        return "", []
    match = expression(query.terms)
    if match is None:
        return "", []
    return (f" JOIN (SELECT rowid AS rid, {_SCORE} AS score FROM message_fts "
            f"WHERE message_fts MATCH ?) hit ON hit.rid = m.id"), [match]


def has_rank(query: Query | None) -> bool:
    """Whether a relevance ordering is available — that is, whether anything
    was actually matched against the index. Chips alone produce no score."""
    return bool(query is not None and expression(query.terms))


def where_sql(query: Query | None, *,
              now: dt.datetime | None = None) -> tuple[str, list]:
    """Everything about a query that is not the index: the chips, and the
    exclusions."""
    if query is None:
        return "", []
    clauses: list[str] = []
    params: list = []
    if not query.discarded:
        # See the module header. `m.deleted` is here beside the two roles
        # because it is the same statement made a different way: a server that
        # marks rather than moves says \Deleted, and a message the user threw
        # away is out of a search however their provider chose to record it.
        clauses.append("f.role NOT IN (?, ?) AND m.deleted = 0")
        params.extend([folders_repo.ROLE_TRASH, folders_repo.ROLE_JUNK])
    excluded = exclusion(query.terms)
    if excluded is not None:
        clauses.append("m.id NOT IN (SELECT rowid FROM message_fts "
                       "WHERE message_fts MATCH ?)")
        params.append(excluded)
    if query.attachment:
        clauses.append("m.has_attachment = 1")
    if query.account_id is not None:
        clauses.append("f.account_id = ?")
        params.append(int(query.account_id))
    start = _day_start(query.date_from) if query.date_from else None
    end = _day_end(query.date_to) if query.date_to else None
    if start is not None:
        clauses.append("m.date_at >= ?")
        params.append(start)
    if end is not None:
        clauses.append("m.date_at <= ?")
        params.append(end)
    if start is None and end is None:
        preset = since(query.within, now) if query.within and query.within != "custom" else None
        if preset is not None:
            clauses.append("m.date_at >= ?")
            params.append(preset)
    return (" AND ".join(clauses), params)


# ---------------------------------------------------------------- snippets
_WHITESPACE = re.compile(r"\s+")


def _needles(terms) -> list[str]:
    """What to centre a snippet on: the positive terms, longest first, so that
    a snippet lands on the distinctive word rather than on `the`."""
    words: list[str] = []
    for term in terms:
        if term.negated:
            continue
        for word in term.text.split():
            word = word.strip().casefold()
            if word:
                words.append(word)
    return sorted(set(words), key=len, reverse=True)


def snippet(body: str, terms, *, width: int = 150) -> str:
    """A line of the body around the first match, or '' if there is none.

    '' rather than the opening of the message, because the caller already has
    something better for that case — the stored preview — and a snippet that
    silently shows the first line of every result is a snippet that has stopped
    meaning anything.
    """
    if not body:
        return ""
    flat = _WHITESPACE.sub(" ", body).strip()
    folded = flat.casefold()
    position = -1
    for needle in _needles(terms):
        position = folded.find(needle)
        if position < 0 and len(needle) > 4:
            # The index stems and this scan does not: `invoices` found nothing
            # because the body says `invoice`. A prefix is a heuristic and is
            # allowed to be — the worst outcome is a snippet centred on a
            # related word, which still shows the reader where they are.
            position = folded.find(needle[:max(4, len(needle) - 3)])
        if position >= 0:
            break
    if position < 0:
        return ""
    start = max(0, position - width // 3)
    end = min(len(flat), start + width)
    if start > 0:
        # To a word boundary, so the snippet does not open mid-word.
        space = flat.find(" ", start, position)
        if space != -1:
            start = space + 1
    piece = flat[start:end].strip()
    return ("…" if start > 0 else "") + piece + ("…" if end < len(flat) else "")


def snippets(con: sqlite3.Connection, message_ids, query: Query | None) -> dict:
    """A snippet for each of these messages, keyed by id. Empty when there is
    nothing to centre on — the caller keeps the stored preview for those.

    A SECOND QUERY, over exactly the ids on the page being shown. The list's own
    query selects the preview and not the body, and that must stay true: a body
    is two orders of magnitude larger than the row it belongs to, and dragging
    one along for a scope that can be a hundred thousand messages, to snippet
    the two hundred actually on screen, is the wrong end of that ratio.
    """
    terms = tuple(t for t in (query.terms if query else ()) if not t.negated)
    ids = [int(m) for m in message_ids]
    if not ids or not terms:
        return {}
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT id, body_text FROM message WHERE id IN ({marks})", ids).fetchall()
    out = {}
    for row in rows:
        piece = snippet(row["body_text"] or "", terms)
        if piece:
            out[int(row["id"])] = piece
    return out
