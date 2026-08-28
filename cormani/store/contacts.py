# SPDX-License-Identifier: GPL-3.0-or-later
#
# The address book, and the part of it that matters before pressing Send.
#
# THE BOUNCE GUARD IS THE POINT OF THIS FILE TODAY. A handle that has bounced is
# recorded — migration 1 gave it a status, a count and a date — and the one
# moment that knowledge is worth anything is while a message to it is being
# written. Afterwards it is a delivery report nobody reads.
#
# IT WARNS AND DOES NOT REFUSE. An address can bounce because a mail server was
# full on Tuesday, and a client that will not send to it on Wednesday is a
# client the user works around by pasting the address somewhere else. The
# composer says what is known and sends anyway if the person says so —
# CONVENTIONS.txt §8 again: report, do not guess.
#
# THE REST OF THE ADDRESS BOOK ARRIVED WITH STAGE 6, and the shape it took is
# the tracking layer's: a CONTACT is a person and a HANDLE is one way of
# reaching them. Nothing here knows about email in particular — an address, a
# telephone number and a LinkedIn profile are three handles of the same kind of
# thing, differing in `kind`, which is free text over a seed list so that
# adding a channel is data rather than a migration.
#
# ONE ADDRESS BELONGS TO EXACTLY ONE PERSON, which migration 1 fixed with
# UNIQUE(kind, value) and this module relies on completely. It makes "who wrote
# this" a lookup with one answer instead of a ranked guess, and it is what lets
# the reading pane's tracking strip find a thread from an address the person
# wrote from for the first time.
#
# A CONTACT IS CREATED FROM A MESSAGE ONLY WHEN ASKED. `contact_for_address`
# takes `create` and defaults it to False, because a mailbox contains thousands
# of addresses that are nobody — no-reply@, a mailing list, a receipt — and an
# address book that grew by itself would be a list nobody could use. The
# tracking layer creates one when a person is put on a thread, which is a
# decision somebody made.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import email.utils
import sqlite3
from dataclasses import dataclass, field

from .database import utc_now

STATUS_BOUNCED = "bounced"
STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
STATUS_STALE = "stale"

CONTACT_ACTIVE = "active"
CONTACT_LEFT_ORG = "left-org"
CONTACT_DO_NOT_CONTACT = "do-not-contact"

KIND_EMAIL = "email"
# Seeded, not enumerated. `handle.kind` is free text and the panels of stage 7
# will bring their own.
SEED_KINDS = (KIND_EMAIL, "phone", "whatsapp", "linkedin", "x", "facebook",
              "signal", "web")


def addresses_in(text: str) -> list[str]:
    """The addresses in a header field the user typed."""
    out: list[str] = []
    for _name, address in email.utils.getaddresses([text or ""]):
        address = address.strip()
        if address and address.lower() not in {a.lower() for a in out}:
            out.append(address)
    return out


def bounced(con: sqlite3.Connection, addresses) -> dict:
    """Which of these have bounced before, and what was said about it.

    Case-folded on both sides: the local part of an address is case-sensitive
    only in a specification nobody honours, and a guard that missed
    lyle@covalent.example because it recorded lyle@covalent.example is a guard.
    """
    wanted = [a for a in addresses if a]
    if not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    rows = con.execute(f"""
        SELECT h.value AS value, h.note AS note, h.bounce_count AS bounces,
               h.last_bounce_at AS last_bounce_at, c.name AS name
        FROM handle h LEFT JOIN contact c ON c.id = h.contact_id
        WHERE h.status = ? AND LOWER(h.value) IN ({marks})
    """, [STATUS_BOUNCED, *(a.lower() for a in wanted)]).fetchall()
    return {r["value"]: {"note": r["note"] or "", "bounces": r["bounces"] or 0,
                         "last": r["last_bounce_at"] or "", "name": r["name"] or ""}
            for r in rows}


def describe_bounces(found: dict) -> str:
    """One line for the composer's warning. Names the addresses and the reason.

    The server's own words are what the user needs — "mailbox full" and "no such
    user" call for opposite decisions — so the note is quoted rather than
    summarised into a status.
    """
    if not found:
        return ""
    parts = []
    for address, detail in sorted(found.items()):
        reason = detail["note"].strip()
        times = detail["bounces"]
        piece = address
        if times > 1:
            piece += f" (bounced {times} times)"
        elif times == 1:
            piece += " (bounced once)"
        if reason:
            piece += f": {reason}"
        parts.append(piece)
    return "; ".join(parts)


def note_bounce(con: sqlite3.Connection, address: str, reason: str, *,
                when: str = "", key: str = "", commit: bool = True) -> bool:
    """Record that a message to this address came back. Returns whether the
    address was known at all — an unknown one is not invented here, because a
    contact the user never made is a contact they will not recognise.

    `commit` is False when `store/ingest.py` calls this while writing the
    message row that IS the bounce: one transaction, so a store can never hold
    a delivery failure whose guard entry was never made.

    `key` IS THE DSN'S OWN Message-ID AND IT IS WHAT MAKES THE COUNT MEAN
    ANYTHING. A message row is idempotent on (folder, uid) and nothing else, so
    an interrupted sync re-writes it and --resync discards and re-fetches it.
    Counting each of those as a fresh failure makes an address that bounced
    once read as having bounced four times, and a number that grows by itself
    is one nobody can act on. Called without a key — by hand, from a dialog —
    every call counts, which is right: a person saying so twice means twice.
    """
    address = (address or "").strip().lower()
    row = con.execute("SELECT id, bounce_count FROM handle WHERE LOWER(value) = ?",
                      (address,)).fetchone()
    if row is None:
        return False
    if key:
        seen = con.execute("SELECT 1 FROM bounce_seen WHERE message_key = ?",
                           (key,)).fetchone()
        if seen is not None:
            return True
        con.execute("INSERT INTO bounce_seen (message_key, address, status, "
                    "seen_at) VALUES (?, ?, ?, ?)",
                    (key, address, reason[:40], when or utc_now()))
    con.execute(
        "UPDATE handle SET status = ?, note = ?, bounce_count = ?, "
        "last_bounce_at = ? WHERE id = ?",
        (STATUS_BOUNCED, reason[:300], int(row["bounce_count"] or 0) + 1,
         when or utc_now(), row["id"]))
    if commit:
        con.commit()
    return True


# --------------------------------------------------------------- the people
@dataclass(frozen=True)
class Handle:
    """One way of reaching a person."""

    id: int
    contact_id: int
    kind: str
    value: str
    status: str
    note: str
    bounce_count: int
    last_bounce_at: str
    created_at: str

    @property
    def is_bounced(self) -> bool:
        return self.status == STATUS_BOUNCED

    @property
    def label(self) -> str:
        """What a chip shows. The kind is dropped for email because that is
        what a reader assumes an unlabelled address is."""
        return self.value if self.kind == KIND_EMAIL else f"{self.kind}: {self.value}"


@dataclass(frozen=True)
class Contact:
    id: int
    name: str
    org: str
    role: str
    notes: str
    status: str
    created_at: str
    updated_at: str
    handles: tuple = field(default_factory=tuple)

    @property
    def label(self) -> str:
        """Never blank. A contact made from a message may have no name at all,
        and a card with an empty title is a defect to look at rather than a
        person to recognise."""
        return self.name or self.address or f"contact {self.id}"

    @property
    def address(self) -> str:
        """The address to write to: the first email handle that has not bounced,
        and otherwise the first one there is. Preferring a working address is
        the whole reason a person has more than one."""
        emails = [h for h in self.handles if h.kind == KIND_EMAIL]
        for handle in emails:
            if not handle.is_bounced:
                return handle.value
        return emails[0].value if emails else ""

    @property
    def reachable(self) -> bool:
        return self.status != CONTACT_DO_NOT_CONTACT and bool(self.address)


def _handle(row: sqlite3.Row) -> Handle:
    return Handle(id=int(row["id"]), contact_id=int(row["contact_id"]),
                  kind=row["kind"], value=row["value"],
                  status=row["status"] or STATUS_UNVERIFIED,
                  note=row["note"] or "",
                  bounce_count=int(row["bounce_count"] or 0),
                  last_bounce_at=row["last_bounce_at"] or "",
                  created_at=row["created_at"] or "")


def _contact(row: sqlite3.Row, handles: tuple = ()) -> Contact:
    return Contact(id=int(row["id"]), name=row["name"] or "",
                   org=row["org"] or "", role=row["role"] or "",
                   notes=row["notes"] or "",
                   status=row["status"] or CONTACT_ACTIVE,
                   created_at=row["created_at"] or "",
                   updated_at=row["updated_at"] or "", handles=handles)


def _handles_for(con: sqlite3.Connection, contact_ids) -> dict:
    """Handles for a set of contacts, in one query rather than one per card.

    Same judgement as `events._guests`: a list of two hundred contacts is two
    hundred queries otherwise, and the join is trivial.
    """
    contact_ids = [int(c) for c in contact_ids]
    if not contact_ids:
        return {}
    marks = ",".join("?" * len(contact_ids))
    out: dict = {}
    for row in con.execute(
            f"SELECT * FROM handle WHERE contact_id IN ({marks}) "
            f"ORDER BY CASE WHEN kind = ? THEN 0 ELSE 1 END, id",
            [*contact_ids, KIND_EMAIL]).fetchall():
        out.setdefault(int(row["contact_id"]), []).append(_handle(row))
    return {k: tuple(v) for k, v in out.items()}


def get_contact(con: sqlite3.Connection, contact_id: int) -> Contact | None:
    row = con.execute("SELECT * FROM contact WHERE id = ?",
                      (int(contact_id),)).fetchone()
    if row is None:
        return None
    return _contact(row, _handles_for(con, [contact_id]).get(int(contact_id), ()))


def list_contacts(con: sqlite3.Connection, *, query: str = "",
                  status: str = "", limit: int = 500,
                  offset: int = 0) -> list[Contact]:
    """The address book. `query` searches the person AND their handles, because
    the thing a person remembers is often the address rather than the name."""
    where, params = ["1=1"], []
    if query:
        where.append("(c.name LIKE ? OR c.org LIKE ? OR c.role LIKE ? "
                     "OR EXISTS (SELECT 1 FROM handle h WHERE h.contact_id = c.id "
                     "AND h.value LIKE ?))")
        params.extend([f"%{query}%"] * 4)
    if status:
        where.append("c.status = ?")
        params.append(status)
    rows = con.execute(
        f"SELECT c.* FROM contact c WHERE {' AND '.join(where)} "
        f"ORDER BY c.name COLLATE NOCASE, c.id LIMIT ? OFFSET ?",
        [*params, int(limit), int(offset)]).fetchall()
    handles = _handles_for(con, [r["id"] for r in rows])
    return [_contact(r, handles.get(int(r["id"]), ())) for r in rows]


def add_contact(con: sqlite3.Connection, name: str = "", *, org: str = "",
                role: str = "", notes: str = "",
                status: str = CONTACT_ACTIVE, commit: bool = True) -> int:
    stamp = utc_now()
    cur = con.execute(
        "INSERT INTO contact (name, org, role, notes, status, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, org, role, notes, status, stamp, stamp))
    if commit:
        con.commit()
    return int(cur.lastrowid)


_CONTACT_FIELDS = ("name", "org", "role", "notes", "status")


def update_contact(con: sqlite3.Connection, contact_id: int, *,
                   commit: bool = True, **fields) -> None:
    """As `tracking.update_thread`: an unknown field raises rather than
    silently changing nothing, because this is called from a dialog."""
    unknown = set(fields) - set(_CONTACT_FIELDS)
    if unknown:
        raise ValueError(f"not a contact field: {', '.join(sorted(unknown))}")
    if not fields:
        return
    sets = ", ".join(f"{name} = ?" for name in fields)
    con.execute(f"UPDATE contact SET {sets}, updated_at = ? WHERE id = ?",
                [*fields.values(), utc_now(), int(contact_id)])
    if commit:
        con.commit()


def delete_contact(con: sqlite3.Connection, contact_id: int, *,
                   commit: bool = True) -> None:
    """Handles cascade; a thread the contact was on keeps its timeline, because
    `touch.contact_id` is SET NULL and the touches are the thread's."""
    con.execute("DELETE FROM contact WHERE id = ?", (int(contact_id),))
    if commit:
        con.commit()


def add_handle(con: sqlite3.Connection, contact_id: int, kind: str, value: str,
               *, status: str = STATUS_UNVERIFIED, note: str = "",
               commit: bool = True) -> int:
    """Give a person a way of being reached. Idempotent on (kind, value).

    A handle that already belongs to SOMEBODY ELSE is MOVED rather than
    refused, and it is the only sane answer: an address has one owner, and a
    user typing it onto a second card is saying the first one was wrong.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("a handle needs a value")
    row = con.execute("SELECT id, contact_id FROM handle WHERE kind = ? "
                      "AND LOWER(value) = ?", (kind, value.lower())).fetchone()
    if row is not None:
        if int(row["contact_id"]) != int(contact_id):
            con.execute("UPDATE handle SET contact_id = ? WHERE id = ?",
                        (int(contact_id), row["id"]))
        if commit:
            con.commit()
        return int(row["id"])
    cur = con.execute(
        "INSERT INTO handle (contact_id, kind, value, status, note, "
        "bounce_count, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
        (int(contact_id), kind, value, status, note, utc_now()))
    if commit:
        con.commit()
    return int(cur.lastrowid)


def remove_handle(con: sqlite3.Connection, handle_id: int, *,
                  commit: bool = True) -> None:
    con.execute("DELETE FROM handle WHERE id = ?", (int(handle_id),))
    if commit:
        con.commit()


def contact_for_address(con: sqlite3.Connection, address: str, *,
                        name: str = "", create: bool = False,
                        commit: bool = True) -> Contact | None:
    """Who this address belongs to, and optionally make them.

    `create` defaults to False and the header says why: a mailbox holds
    thousands of addresses that are nobody, and an address book that grew by
    itself is a list nobody can use. The tracking layer passes True when a
    person is put on a thread, which is a decision somebody made.
    """
    address = (address or "").strip()
    if not address:
        return None
    row = con.execute("SELECT contact_id FROM handle WHERE kind = ? "
                      "AND LOWER(value) = ?",
                      (KIND_EMAIL, address.lower())).fetchone()
    if row is not None:
        return get_contact(con, int(row["contact_id"]))
    if not create:
        return None
    contact_id = add_contact(con, name or address.split("@")[0],
                             commit=False)
    add_handle(con, contact_id, KIND_EMAIL, address,
               status=STATUS_UNVERIFIED, commit=False)
    if commit:
        con.commit()
    return get_contact(con, contact_id)


def merge_contacts(con: sqlite3.Connection, keep_id: int, drop_id: int, *,
                   commit: bool = True) -> int:
    """One person, two cards. Returns how many handles moved.

    The kept card's own fields win and its empty ones are filled from the other,
    which is the useful half of a merge: the duplicate usually exists because
    one of them was made from a message and has an address and nothing else.
    """
    if int(keep_id) == int(drop_id):
        return 0
    moved = con.execute("UPDATE OR IGNORE handle SET contact_id = ? "
                        "WHERE contact_id = ?",
                        (int(keep_id), int(drop_id))).rowcount
    keep = con.execute("SELECT * FROM contact WHERE id = ?",
                       (int(keep_id),)).fetchone()
    drop = con.execute("SELECT * FROM contact WHERE id = ?",
                       (int(drop_id),)).fetchone()
    if keep is not None and drop is not None:
        filled = {name: drop[name] for name in ("name", "org", "role")
                  if not (keep[name] or "").strip() and (drop[name] or "").strip()}
        notes = "\n\n".join(x for x in (keep["notes"], drop["notes"]) if x)
        if notes != (keep["notes"] or ""):
            filled["notes"] = notes
        if filled:
            update_contact(con, keep_id, commit=False, **filled)
    con.execute("UPDATE thread_contact SET contact_id = ? WHERE contact_id = ? "
                "AND thread_id NOT IN (SELECT thread_id FROM thread_contact "
                "WHERE contact_id = ?)",
                (int(keep_id), int(drop_id), int(keep_id)))
    con.execute("UPDATE touch SET contact_id = ? WHERE contact_id = ?",
                (int(keep_id), int(drop_id)))
    con.execute("DELETE FROM contact WHERE id = ?", (int(drop_id),))
    if commit:
        con.commit()
    return moved


def counts(con: sqlite3.Connection) -> dict:
    """What the address book's header shows."""
    return {
        "contacts": con.execute("SELECT COUNT(*) FROM contact").fetchone()[0],
        "handles": con.execute("SELECT COUNT(*) FROM handle").fetchone()[0],
        "bounced": con.execute("SELECT COUNT(*) FROM handle WHERE status = ?",
                               (STATUS_BOUNCED,)).fetchone()[0],
    }
