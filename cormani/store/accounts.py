# SPDX-License-Identifier: GPL-3.0-or-later
#
# Accounts and the groups they sit in.
#
# This is the repository the rail is drawn from, and almost everything in it
# exists because of one number: fifteen. A flat list of fifteen accounts is
# unreadable, so the rail needs groups, colours, an order the user chose and a
# way to put a dormant account out of sight. docs/accounts.txt is the
# requirement; this module is its implementation.
#
# Three decisions worth the words:
#
# ORDER IS WRITTEN THROUGH IMMEDIATELY. A drag that survives only until quit is
# worse than no reordering at all, because the user stops trusting it and then
# stops using it. `reorder` and `move_account` renumber and commit in one
# transaction; there is no in-memory order to lose.
#
# THE COLOUR RAMP LIVES HERE, NOT IN THE THEME. An account's colour is the mark
# that says which identity a message arrived on, carried onto every row of a
# unified inbox. It is stored data, chosen once, and it must mean the same thing
# after the user switches from Solarized Light to Dark. A colour taken from a
# theme role would silently renumber every account when the palette inverted,
# which is the one thing a mark may not do. The consequence is that the values
# below are literal, and are chosen to be legible on both a light and a dark
# background rather than to match either.
#
# SORT ORDER IS PER GROUP, NOT GLOBAL. Groups reorder as units and accounts
# reorder within them, so a global sequence would have to be renumbered on every
# group move. Loose accounts — those in no group — sort among themselves and are
# listed after the groups.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import email.utils
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

from .database import utc_now

# Sixteen distinguishable hues. Solarized's eight accents first, because they
# are what the default theme is built from and an account added on a fresh
# install should look at home; then eight more, spaced around the wheel, for
# installs that outgrow the first eight. Fifteen accounts fit without repeating.
ACCOUNT_COLOURS: tuple[str, ...] = (
    "#268bd2",  # blue
    "#859900",  # green
    "#d33682",  # magenta
    "#b58900",  # yellow
    "#2aa198",  # cyan
    "#cb4b16",  # orange
    "#6c71c4",  # violet
    "#dc322f",  # red
    "#4c7a9e",  # slate
    "#7d8c3f",  # olive
    "#a3579a",  # plum
    "#8d6e3a",  # bronze
    "#3f8f7a",  # teal
    "#b06848",  # terracotta
    "#5a6acf",  # indigo
    "#9c4b5c",  # maroon
)

PROVIDERS = ("google", "microsoft", "fastmail", "yahoo", "icloud", "imap")


@dataclass(frozen=True)
class Group:
    id: int
    name: str
    sort_order: int
    collapsed: bool


# The calendar engine's back-off, kept apart from the mail engine's. See
# migration 7: one account can sync mail and be unable to read a calendar,
# because Google issues app passwords for the first and refuses them for the
# second, and a shared counter would let either engine silence the other.
def record_calendar_failure(con: sqlite3.Connection, account_id: int,
                            error: str, retry_at: str, *,
                            commit: bool = True) -> None:
    con.execute(
        "UPDATE account SET calendar_error = ?, "
        "calendar_failures = calendar_failures + 1, calendar_next_at = ? "
        "WHERE id = ?", (error[:300], retry_at, int(account_id)))
    if commit:
        con.commit()


def record_calendar_success(con: sqlite3.Connection, account_id: int, *,
                            commit: bool = True) -> None:
    con.execute(
        "UPDATE account SET calendar_error = '', calendar_failures = 0, "
        "calendar_next_at = NULL WHERE id = ?", (int(account_id),))
    if commit:
        con.commit()


def calendar_state(con: sqlite3.Connection) -> dict:
    """Each account's calendar back-off, for the interface and the engine."""
    return {int(r["id"]): {"error": r["calendar_error"],
                           "failures": int(r["calendar_failures"] or 0),
                           "until": r["calendar_next_at"]}
            for r in con.execute(
                "SELECT id, calendar_error, calendar_failures, calendar_next_at "
                "FROM account").fetchall()}


@dataclass(frozen=True)
class Account:
    id: int
    address: str
    display_name: str
    provider: str
    group_id: int | None
    sort_order: int
    colour: str
    hidden: bool
    enabled: bool
    last_error: str = ""

    @property
    def label(self) -> str:
        """What the rail shows. The address is the fallback, never blank: an
        account with no display name is common and must still be identifiable."""
        return self.display_name or self.address


def _group(row: sqlite3.Row) -> Group:
    return Group(id=row["id"], name=row["name"], sort_order=row["sort_order"],
                 collapsed=bool(row["collapsed"]))


def _account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"], address=row["address"], display_name=row["display_name"],
        provider=row["provider"], group_id=row["group_id"],
        sort_order=row["sort_order"], colour=row["colour"],
        hidden=bool(row["hidden"]), enabled=bool(row["enabled"]),
        last_error=row["last_error"] if "last_error" in row.keys() else "")


# ---------------------------------------------------------------- colours
def next_colour(used: Iterable[str]) -> str:
    """Pick a colour for a new account: the first in the ramp that is unused,
    and once the ramp is exhausted the least-used one.

    Least-used rather than round-robin, because accounts get deleted: after
    removing three, round-robin would hand out a colour already on screen while
    three others sat free.
    """
    counts = {c: 0 for c in ACCOUNT_COLOURS}
    for colour in used:
        key = (colour or "").strip().lower()
        if key in counts:
            counts[key] += 1
    return min(ACCOUNT_COLOURS, key=lambda c: (counts[c], ACCOUNT_COLOURS.index(c)))


# ----------------------------------------------------------------- groups
def list_groups(con: sqlite3.Connection) -> list[Group]:
    rows = con.execute(
        "SELECT * FROM account_group ORDER BY sort_order, id").fetchall()
    return [_group(r) for r in rows]


def get_group(con: sqlite3.Connection, group_id: int) -> Group | None:
    row = con.execute("SELECT * FROM account_group WHERE id = ?",
                      (group_id,)).fetchone()
    return _group(row) if row else None


def add_group(con: sqlite3.Connection, name: str) -> int:
    nxt = con.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM account_group").fetchone()[0]
    cur = con.execute(
        "INSERT INTO account_group (name, sort_order, collapsed, created_at) "
        "VALUES (?, ?, 0, ?)", (name, nxt, utc_now()))
    con.commit()
    return int(cur.lastrowid)


def rename_group(con: sqlite3.Connection, group_id: int, name: str) -> None:
    con.execute("UPDATE account_group SET name = ? WHERE id = ?", (name, group_id))
    con.commit()


def set_group_collapsed(con: sqlite3.Connection, group_id: int,
                        collapsed: bool) -> None:
    """Collapse state is account data, not window state.

    A user with fifteen accounts collapses the groups they are not working in,
    and that arrangement is as much theirs as the order is. Window geometry
    belongs to the window; this belongs to the rail.
    """
    con.execute("UPDATE account_group SET collapsed = ? WHERE id = ?",
                (1 if collapsed else 0, group_id))
    con.commit()


def delete_group(con: sqlite3.Connection, group_id: int) -> None:
    """Remove the group. Its accounts become loose — never deleted with it.

    The schema's ON DELETE SET NULL says the same thing, and it is repeated here
    because it is the behaviour that matters: deleting a folder-like thing in a
    mail client must never be able to delete mail.
    """
    con.execute("DELETE FROM account_group WHERE id = ?", (group_id,))
    con.commit()


def reorder_groups(con: sqlite3.Connection, ordered_ids: Sequence[int]) -> None:
    with con:
        for position, group_id in enumerate(ordered_ids, start=1):
            con.execute("UPDATE account_group SET sort_order = ? WHERE id = ?",
                        (position, group_id))


# --------------------------------------------------------------- accounts
def list_accounts(con: sqlite3.Connection, *, include_hidden: bool = True,
                  include_disabled: bool = True) -> list[Account]:
    """Every account, in rail order: grouped accounts by group, then loose ones.

    Loose accounts sort last because a group is a deliberate act and an
    ungrouped account is usually a new one; putting new arrivals at the bottom
    keeps a familiar rail familiar.
    """
    clauses = []
    if not include_hidden:
        clauses.append("hidden = 0")
    if not include_disabled:
        clauses.append("enabled = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = con.execute(f"""
        SELECT a.* FROM account a
        LEFT JOIN account_group g ON g.id = a.group_id
        {where}
        ORDER BY (a.group_id IS NULL), COALESCE(g.sort_order, 0), a.group_id,
                 a.sort_order, a.id
    """).fetchall()
    return [_account(r) for r in rows]


def get_account(con: sqlite3.Connection, account_id: int) -> Account | None:
    row = con.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    return _account(row) if row else None


def find_by_address(con: sqlite3.Connection, address: str) -> Account | None:
    row = con.execute("SELECT * FROM account WHERE address = ?",
                      (address.strip().lower(),)).fetchone()
    return _account(row) if row else None


def add_account(con: sqlite3.Connection, address: str, provider: str, *,
                display_name: str = "", group_id: int | None = None,
                colour: str = "", imap_host: str = "", imap_port: int = 993,
                smtp_host: str = "", smtp_port: int = 587,
                auth_method: str = "oauth2") -> int:
    """Add an account and give it a colour and a place in the rail.

    The colour is assigned rather than asked for. Fifteen accounts each needing
    a colour decision at creation time is fifteen decisions the user did not ask
    to make, and the ramp is chosen so any assignment is legible. It remains
    changeable afterwards, which is where the decision belongs.
    """
    address = address.strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")
    if not colour:
        used = [r[0] for r in con.execute("SELECT colour FROM account").fetchall()]
        colour = next_colour(used)
    nxt = con.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM account "
        "WHERE group_id IS ?", (group_id,)).fetchone()[0]
    now = utc_now()
    cur = con.execute("""
        INSERT INTO account (address, display_name, provider, imap_host,
                             imap_port, smtp_host, smtp_port, auth_method,
                             group_id, sort_order, colour, hidden, enabled,
                             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
    """, (address, display_name, provider, imap_host, imap_port, smtp_host,
          smtp_port, auth_method, group_id, nxt, colour, now, now))
    con.commit()
    return int(cur.lastrowid)


def _touch(con: sqlite3.Connection, account_id: int) -> None:
    con.execute("UPDATE account SET updated_at = ? WHERE id = ?",
                (utc_now(), account_id))


def set_display_name(con: sqlite3.Connection, account_id: int, name: str) -> None:
    con.execute("UPDATE account SET display_name = ? WHERE id = ?", (name, account_id))
    _touch(con, account_id)
    con.commit()


def set_colour(con: sqlite3.Connection, account_id: int, colour: str) -> None:
    con.execute("UPDATE account SET colour = ? WHERE id = ?", (colour, account_id))
    _touch(con, account_id)
    con.commit()


def set_hidden(con: sqlite3.Connection, account_id: int, hidden: bool) -> None:
    """Hide leaves the rail. It does not touch the mail.

    Stated as a comment because it is the whole point of the column: a dormant
    account's messages stay in the store, stay in search, and stay findable —
    hiding is a change to the rail and nothing else. Anything that made it also
    stop syncing would be `enabled`, which is a different decision.
    """
    con.execute("UPDATE account SET hidden = ? WHERE id = ?",
                (1 if hidden else 0, account_id))
    _touch(con, account_id)
    con.commit()


def set_enabled(con: sqlite3.Connection, account_id: int, enabled: bool) -> None:
    con.execute("UPDATE account SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, account_id))
    _touch(con, account_id)
    con.commit()


def reorder_accounts(con: sqlite3.Connection, group_id: int | None,
                     ordered_ids: Sequence[int]) -> None:
    """Renumber one group's accounts. Also moves them into that group.

    Both at once because a drag does both at once, and a two-step version has a
    state between the steps where an account is in the new group at the old
    position — which is what a crash mid-drag would leave behind.
    """
    with con:
        for position, account_id in enumerate(ordered_ids, start=1):
            con.execute(
                "UPDATE account SET group_id = ?, sort_order = ?, updated_at = ? "
                "WHERE id = ?", (group_id, position, utc_now(), account_id))


def move_account(con: sqlite3.Connection, account_id: int,
                 group_id: int | None, position: int) -> None:
    """The drag-and-drop primitive: put this account here.

    `position` is a zero-based index among the accounts already in the target
    group, with the moved account removed from the list first — which is what
    the drop indicator between two rows means. Out-of-range positions clamp
    rather than raise: a view computing a row index off by one at the end of a
    list should place the account at the end, not fail the drop.
    """
    current = get_account(con, account_id)
    if current is None:
        raise KeyError(f"no account {account_id}")
    siblings = [a.id for a in list_accounts(con)
                if a.group_id == group_id and a.id != account_id]
    position = max(0, min(position, len(siblings)))
    siblings.insert(position, account_id)
    reorder_accounts(con, group_id, siblings)


# ------------------------------------------------------------- identities
def list_identity_addresses(con: sqlite3.Connection) -> set[str]:
    """Every address that is 'me'.

    The account addresses plus any extra sending identities. Used to decide
    whether a message is inbound, which is what the Owed view rests on: a
    message from one of these is something the user sent, and cannot be owed a
    reply.
    """
    addresses = {r[0].strip().lower()
                 for r in con.execute("SELECT address FROM account").fetchall()}
    addresses |= {r[0].strip().lower()
                  for r in con.execute("SELECT address FROM identity").fetchall()}
    return {a for a in addresses if a}


@dataclass(frozen=True)
class Identity:
    """An address the user sends AS, and what goes at the foot of it.

    Separate from the account because the two are not the same thing: one
    mailbox may send as three addresses, and a signature belongs to the address
    a correspondent sees rather than to the server the mail went through.
    """

    id: int | None
    account_id: int
    address: str
    display_name: str
    signature: str
    is_default: bool

    @property
    def sender(self) -> str:
        """The From header, as RFC 5322 wants it.

        `formataddr` rather than an f-string: a display name containing a comma
        — "Thatte, Manish" — has to be quoted, and a header that gets that
        wrong turns one recipient into two on the way out.
        """
        return email.utils.formataddr((self.display_name, self.address))


def _identity(row: sqlite3.Row) -> Identity:
    return Identity(id=row["id"], account_id=row["account_id"],
                    address=row["address"], display_name=row["display_name"],
                    signature=row["signature"], is_default=bool(row["is_default"]))


def list_identities(con: sqlite3.Connection, account_id: int) -> list[Identity]:
    """Every address this account may send as, the account's own first.

    THE ACCOUNT ITSELF IS ALWAYS ONE OF THEM, and it is synthesised rather than
    written into the table at creation: an account can send as its own address
    by definition, and a row that says so would be a row that can go missing.
    """
    account = get_account(con, account_id)
    out: list[Identity] = []
    if account is not None:
        out.append(Identity(id=None, account_id=account_id,
                            address=account.address,
                            display_name=account.display_name, signature="",
                            is_default=True))
    rows = con.execute(
        "SELECT * FROM identity WHERE account_id = ? ORDER BY is_default DESC, id",
        (account_id,)).fetchall()
    for row in rows:
        identity = _identity(row)
        if identity.address == (account.address if account else ""):
            # The same address, written down: the stored row wins, because it
            # is the one with the signature on it.
            out[0] = identity
        else:
            out.append(identity)
    return out


def default_identity(con: sqlite3.Connection, account_id: int) -> Identity | None:
    """The one a new message is from, unless the user says otherwise."""
    identities = list_identities(con, account_id)
    for identity in identities:
        if identity.is_default:
            return identity
    return identities[0] if identities else None


def identity_for(con: sqlite3.Connection, account_id: int,
                 address: str) -> Identity | None:
    """The identity with this address, or None. For putting a reply back on the
    address the original was addressed to."""
    wanted = (address or "").strip().lower()
    for identity in list_identities(con, account_id):
        if identity.address.lower() == wanted:
            return identity
    return None


def add_identity(con: sqlite3.Connection, account_id: int, address: str, *,
                 display_name: str = "", signature: str = "",
                 is_default: bool = False) -> int:
    cur = con.execute("""
        INSERT INTO identity (account_id, address, display_name, signature, is_default)
        VALUES (?, ?, ?, ?, ?)
    """, (account_id, address.strip().lower(), display_name, signature,
          1 if is_default else 0))
    con.commit()
    return int(cur.lastrowid)
