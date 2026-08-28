# SPDX-License-Identifier: GPL-3.0-or-later
#
# The mailbox, seen through a person.
#
# `store/contacts.py` owns the address book itself — who somebody is, every way
# of reaching them, and the bounce guard. This file owns the questions that are
# not about the contact table at all: what has actually passed between the two
# of you, who in the mailbox is nobody yet, and which two cards are one person.
# The seam is the same one `store/rules.py` gave up to `store/rulematch.py`,
# drawn before the 600-line rule fired rather than after: contacts.py is a
# record of people and this is a report about mail.
#
# ── THE COUNTS ARE TAKEN ON SELECTION AND NEVER PER ROW ────────────────────
#
# `to_addrs` is a text column with no index, so "was this person written to"
# is `LIKE '%…%'` and a scan. Saved searches measured that shape at about
# 107 ms over 75,776 rows, and it is affordable exactly once — when a card is
# opened — and not affordable at all in a list that redraws. So
# `correspondence` takes ONE contact and the pane calls it for the selected
# card only. A "messages" column beside every name in the list would be the
# rail's mistake with two orders of magnitude less excuse.
#
# ── AND THEY ARE TAKEN FROM `message`, NOT FROM `wrote_to` ─────────────────
#
# `wrote_to` already holds a per-address count derived from the Sent folders,
# and it is tempting because it is indexed. It is a CACHE: `imap/engine.py`
# skips `rebuild_wrote_to` entirely when nothing new arrived, so it is right
# whenever a sync happened and stale whenever one did not. A count on a card is
# read as a fact about the mailbox; one that lags a sync is a fact about the
# cache. The scan is the honest answer and the card can afford it.
#
# ── "SENT" IS A FOLDER ROLE AND NOT A GUESS ────────────────────────────────
#
# There is no "I wrote this" column. What there is, is `folder.role = 'sent'`,
# which is what `attach.rebuild_wrote_to` already treats as the definition, and
# agreeing with it matters more than being clever: two places that decide
# direction differently would put a message on the card's timeline pointing one
# way and on the thread's pointing the other.
#
# ── A SUGGESTION IS AN OFFER AND NEVER A WRITE ─────────────────────────────
#
# `contacts.contact_for_address` defaults `create` to False because a mailbox
# holds thousands of addresses that are nobody — no-reply@, a list, a receipt —
# and an address book that grew by itself is a list nobody can use. `suggest`
# is how that stays true while the book still fills up: it RANKS the strangers
# by how much mail there is, and a person picks. Nothing here writes anything.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import contacts as contacts_repo
from .folders import ROLE_DRAFTS, ROLE_JUNK, ROLE_SENT, ROLE_TRASH

# What a card's recent list shows, and what `suggest` considers. Both bounded:
# a card is a card and not a folder, and a suggestion list nobody can read to
# the end is a list nobody uses.
RECENT_LIMIT = 12
SUGGEST_LIMIT = 25

# Addresses that are nobody by construction. A suggestion list whose top row is
# `no-reply@` is a list a person stops opening — and unlike a contact, these
# cannot be told apart by how much mail there is, because there is always more
# of it. Matched on the LOCAL PART, so a domain that happens to contain the
# word is unaffected.
_NOT_PEOPLE = ("no-reply", "noreply", "do-not-reply", "donotreply",
               "bounce", "mailer-daemon", "postmaster", "notifications",
               "notification", "automated", "auto-reply", "autoreply")


@dataclass(frozen=True)
class Correspondence:
    """What has passed between the user and one person.

    `received` and `sent` are counted separately rather than summed, because
    the pair is what says which kind of correspondent this is: forty in and two
    out is a newsletter, and two in and forty out is somebody not answering.
    """

    received: int = 0
    sent: int = 0
    first_at: str = ""
    last_at: str = ""

    @property
    def total(self) -> int:
        return self.received + self.sent

    @property
    def any(self) -> bool:
        return self.total > 0

    def describe(self) -> str:
        """One line for the card. Says the shape, not only the size."""
        if not self.any:
            return "no mail either way"
        parts = []
        if self.received:
            parts.append(f"{self.received} from them")
        if self.sent:
            parts.append(f"{self.sent} to them")
        return ", ".join(parts)


def describe_mail(contact, seen: Correspondence) -> str:
    """What to say about one person's mail. The WORDS, without the date.

    ONE FUNCTION BECAUSE THERE ARE TWO SILENCES AND THEY ARE DIFFERENT FACTS:
    somebody with an address and nothing sent either way, and somebody who
    cannot be written to at all. `Correspondence` cannot tell them apart — it
    knows about mail and not about the card — so the question needs the contact
    as well, and a caller that asked `describe()` alone got "no mail either
    way" for a contact who has only a telephone number. `--contacts` did
    exactly that, and printed it under a person whose card said the other
    thing, which is one question answered two ways on two surfaces.

    THE DATE IS THE CALLER'S. The card writes "25 Aug 2026" and the read-out
    writes "2026-08-25", and a date format is presentation; which sentence is
    true is not.
    """
    if seen.any:
        return seen.describe()
    if contact.address:
        return ("No mail either way — this card was made by hand or from "
                "another channel")
    return "No address, so nothing can be sent from here"


@dataclass(frozen=True)
class Stranger:
    """An address in the mailbox that belongs to no contact."""

    address: str
    name: str
    messages: int
    last_at: str

    @property
    def label(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address


@dataclass(frozen=True)
class Duplicate:
    """Two cards that are probably one person, and what says so."""

    keep_id: int
    drop_id: int
    reason: str


def _emails(contact) -> list[str]:
    """Their email addresses, lower-cased. The unit every query here works in.

    A contact with none is not an error: a card made for a telephone number is
    a perfectly ordinary thing, and it simply has no mail.
    """
    return [h.value.strip().lower() for h in contact.handles
            if h.kind == contacts_repo.KIND_EMAIL and h.value.strip()]


# --------------------------------------------------------- what passed between
def correspondence(con: sqlite3.Connection, contact) -> Correspondence:
    """Mail from and to this person, over every address they have.

    WHAT COUNTS AS ARRIVING IS THE SAME FOUR ROLES THE MATCHER EXCLUDES —
    `store/attach.py` and `store/triage.py` both spell it
    `NOT IN ('sent', 'drafts', 'junk', 'trash')`, and agreeing with them is
    worth more than any opinion of my own about spam: a card whose count
    disagreed with the triage queue would send somebody looking for a message
    that is in neither.

    `recent_messages` applies the identical rule, because a card saying
    "12 from them" above a list of nine is a card nobody trusts again.
    """
    addresses = _emails(contact)
    if not addresses:
        return Correspondence()

    marks = ",".join("?" * len(addresses))
    received = con.execute(f"""
        SELECT COUNT(*) AS n, MIN(m.date_at) AS first_at,
               MAX(m.date_at) AS last_at
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE LOWER(m.from_addr) IN ({marks})
          AND (f.role IS NULL OR f.role NOT IN (?, ?, ?, ?))
    """, [*addresses, ROLE_SENT, ROLE_DRAFTS, ROLE_JUNK,
          ROLE_TRASH]).fetchone()

    # The outbound half cannot use IN: the address sits inside a header field
    # holding a list of them, so it is one LIKE per address. Bounded by the
    # number of handles one person has, which is a number a person types.
    clause = " OR ".join(["(LOWER(m.to_addrs) LIKE ? OR LOWER(m.cc_addrs) LIKE ?)"]
                         * len(addresses))
    params: list = []
    for address in addresses:
        params.extend([f"%{address}%"] * 2)
    sent = con.execute(f"""
        SELECT COUNT(*) AS n, MIN(m.date_at) AS first_at,
               MAX(m.date_at) AS last_at
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE f.role = ? AND ({clause})
    """, [ROLE_SENT, *params]).fetchone()

    stamps = [s for s in (received["first_at"], received["last_at"],
                          sent["first_at"], sent["last_at"]) if s]
    return Correspondence(received=int(received["n"] or 0),
                          sent=int(sent["n"] or 0),
                          first_at=min(stamps) if stamps else "",
                          last_at=max(stamps) if stamps else "")


def recent_messages(con: sqlite3.Connection, contact, *,
                    limit: int = RECENT_LIMIT) -> list[sqlite3.Row]:
    """The last few messages either way, newest first.

    Rows rather than a dataclass: the card draws four fields of each and
    `store/messages.py` already owns the message vocabulary — a second `Message`
    shape declared here would be a second thing to keep in step with the schema.

    `outbound` is computed by the QUERY rather than by re-testing the address in
    Python, so the arrow on a row and the counts above it can never disagree.
    """
    addresses = _emails(contact)
    if not addresses:
        return []
    marks = ",".join("?" * len(addresses))
    clause = " OR ".join(["(LOWER(m.to_addrs) LIKE ? OR LOWER(m.cc_addrs) LIKE ?)"]
                         * len(addresses))
    likes: list = []
    for address in addresses:
        likes.extend([f"%{address}%"] * 2)
    return con.execute(f"""
        SELECT m.id, m.subject, m.date_at, m.from_name, m.from_addr, m.seen,
               f.role AS role,
               CASE WHEN f.role = ? THEN 1 ELSE 0 END AS outbound
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE ((f.role IS NULL OR f.role NOT IN (?, ?, ?, ?))
               AND LOWER(m.from_addr) IN ({marks}))
           OR (f.role = ? AND ({clause}))
        ORDER BY m.date_at DESC, m.id DESC
        LIMIT ?
    """, [ROLE_SENT, ROLE_SENT, ROLE_DRAFTS, ROLE_JUNK, ROLE_TRASH,
          *addresses, ROLE_SENT, *likes, int(limit)]).fetchall()


# ------------------------------------------------------------ who is nobody yet
def suggest(con: sqlite3.Connection, *, limit: int = SUGGEST_LIMIT,
            include_sent: bool = True) -> list[Stranger]:
    """Addresses in the mailbox that belong to no contact, most mail first.

    ONE PASS AND NOT ONE PER ADDRESS. The obvious shape — list the senders,
    then ask `contact_for_address` about each — is a query per row and gets
    slower exactly as the mailbox gets more interesting. The handle table is
    joined instead, and the absence of a match is the test.

    `include_sent` is not decoration: somebody the user has WRITTEN to is a far
    better candidate than somebody who wrote to them, and a first run over a
    real mailbox otherwise offers a page of senders the user has never answered.
    """
    rows = con.execute("""
        SELECT LOWER(m.from_addr) AS address,
               MAX(m.from_name) AS name,
               COUNT(*) AS messages,
               MAX(m.date_at) AS last_at,
               MAX(CASE WHEN w.address IS NOT NULL THEN 1 ELSE 0 END) AS answered
        FROM message m
        JOIN folder f ON f.id = m.folder_id
        LEFT JOIN handle h ON h.kind = 'email'
                          AND LOWER(h.value) = LOWER(m.from_addr)
        LEFT JOIN wrote_to w ON w.address = LOWER(m.from_addr)
        WHERE m.from_addr <> ''
          AND h.id IS NULL
          AND (f.role IS NULL OR f.role NOT IN (?, ?, ?, ?))
        GROUP BY LOWER(m.from_addr)
        ORDER BY answered DESC, messages DESC, last_at DESC
    """, [ROLE_SENT, ROLE_DRAFTS, ROLE_JUNK, ROLE_TRASH]).fetchall()

    out: list[Stranger] = []
    for row in rows:
        address = (row["address"] or "").strip()
        if not address or is_machine(address):
            continue
        if not include_sent and int(row["answered"] or 0):
            continue
        out.append(Stranger(address=address, name=(row["name"] or "").strip(),
                            messages=int(row["messages"] or 0),
                            last_at=row["last_at"] or ""))
        if len(out) >= int(limit):
            break
    return out


def is_machine(address: str) -> bool:
    """Whether this address is something rather than somebody.

    Deliberately a small hard-coded list and deliberately on the LOCAL PART.
    The general version — "does this look automated" — is a heuristic that
    would eventually hide a real person behind a plausible rule, and a
    suggestion list that silently omits somebody is worse than one with a
    no-reply in it.
    """
    local = (address or "").split("@", 1)[0].lower()
    return any(mark in local for mark in _NOT_PEOPLE)


# ---------------------------------------------------------- one person, two cards
def duplicates(con: sqlite3.Connection) -> list[Duplicate]:
    """Pairs that are probably one person. Offered, never merged.

    Two rules, and no third. The same NAME, case-folded — which is how the
    duplicate usually arises, one card made by hand and one made from a message
    — and the same address at a different KIND, which is how a WhatsApp number
    and a telephone number end up on two cards.

    A guess about the same person from initials or a shared domain is what this
    deliberately does not do: `merge_contacts` moves handles and deletes a row,
    and a wrong merge is not undoable. The pair is shown with its reason and a
    person decides.

    THE ORDER WITHIN A PAIR IS NOT ARBITRARY. `keep_id` is the card with more
    filled in, because `merge_contacts` fills the kept card's EMPTY fields from
    the other — so keeping the fuller one loses nothing and keeping the emptier
    one loses whichever field both of them have.
    """
    found: dict[tuple[int, int], str] = {}
    for row in con.execute("""
        SELECT a.id AS a_id, b.id AS b_id, a.name AS name
        FROM contact a JOIN contact b
          ON LOWER(TRIM(a.name)) = LOWER(TRIM(b.name)) AND a.id < b.id
        WHERE TRIM(a.name) <> ''
    """).fetchall():
        found[(int(row["a_id"]), int(row["b_id"]))] = \
            f"both are called {row['name']}"

    for row in con.execute("""
        SELECT ha.contact_id AS a_id, hb.contact_id AS b_id,
               ha.value AS value
        FROM handle ha JOIN handle hb
          ON LOWER(ha.value) = LOWER(hb.value) AND ha.kind <> hb.kind
        WHERE ha.contact_id < hb.contact_id
    """).fetchall():
        key = (int(row["a_id"]), int(row["b_id"]))
        found.setdefault(key, f"both hold {row['value']}")

    out = []
    for (first, second), reason in sorted(found.items()):
        keep, drop = _fuller(con, first, second)
        out.append(Duplicate(keep_id=keep, drop_id=drop, reason=reason))
    return out


def _fuller(con: sqlite3.Connection, first: int, second: int) -> tuple[int, int]:
    """Which of two cards has more on it. Ties keep the older id, which is the
    one every other row already points at."""
    def weight(contact_id: int) -> tuple:
        row = con.execute("SELECT name, org, role, notes FROM contact "
                          "WHERE id = ?", (int(contact_id),)).fetchone()
        filled = sum(1 for field in ("name", "org", "role", "notes")
                     if row is not None and (row[field] or "").strip())
        handles = con.execute("SELECT COUNT(*) FROM handle WHERE contact_id = ?",
                              (int(contact_id),)).fetchone()[0]
        return (filled + int(handles), -int(contact_id))

    return (first, second) if weight(first) >= weight(second) else (second, first)


# ------------------------------------------------------------------- the report
def summary(con: sqlite3.Connection) -> dict:
    """What `--check` and the pane's footer both need, in one pass.

    `no_email` is here rather than in `contacts.counts` because it is the one
    number that says something is WRONG rather than something is so: a contact
    with no address cannot be written to, and the composer will never offer it.
    """
    counts = dict(contacts_repo.counts(con))
    counts["no_email"] = con.execute("""
        SELECT COUNT(*) FROM contact c
        WHERE NOT EXISTS (SELECT 1 FROM handle h
                          WHERE h.contact_id = c.id AND h.kind = 'email')
    """).fetchone()[0]
    counts["kinds"] = {
        row["kind"]: int(row["n"]) for row in con.execute(
            "SELECT kind, COUNT(*) AS n FROM handle GROUP BY kind "
            "ORDER BY n DESC, kind").fetchall()}
    counts["duplicates"] = len(duplicates(con))
    return counts
