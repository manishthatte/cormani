# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tags.
#
# Thunderbird's design, kept: a name, a colour, and optionally one of the keys
# 1-9. The keys are what make tagging worth having — a tag you must reach a menu
# for is a tag applied twice and then forgotten.
#
# `toggle` applies to a whole selection and decides once, from the first
# message, whether the gesture is "tag these" or "untag these". Deciding per
# message would make pressing 1 over a mixed selection invert it, which is never
# what the gesture meant.
#
# A KEY BELONGS TO ONE TAG, AND ASSIGNING IT MOVES IT. The schema says so with a
# UNIQUE constraint, and the alternative to moving it is refusing — which makes
# the user go and find the tag that has the key, clear it, and come back. The
# dialog reports which tag lost it, because a change nobody was told about is
# the thing CONVENTIONS.txt §8 is against.
#
# TAGS ARE LOCAL AND ARE NOT IMAP KEYWORDS. Thunderbird stores them as keywords
# on the server; corMani keeps them in its own store, because not every server
# accepts arbitrary keywords, because a keyword is per-account and a tag here
# spans fifteen, and because the mark is the user's own on their own copy. The
# cost is that tags do not follow the mail to another client, and that is
# recorded here rather than discovered.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Tag:
    id: int
    name: str
    colour: str
    shortcut: int | None
    sort_order: int


def _tag(row: sqlite3.Row) -> Tag:
    return Tag(id=row["id"], name=row["name"], colour=row["colour"],
               shortcut=row["shortcut"], sort_order=row["sort_order"])


def list_tags(con: sqlite3.Connection) -> list[Tag]:
    rows = con.execute("SELECT * FROM tag ORDER BY sort_order, id").fetchall()
    return [_tag(r) for r in rows]


def get_tag(con: sqlite3.Connection, tag_id: int) -> Tag | None:
    row = con.execute("SELECT * FROM tag WHERE id = ?", (tag_id,)).fetchone()
    return _tag(row) if row else None


def by_shortcut(con: sqlite3.Connection, key: int) -> Tag | None:
    row = con.execute("SELECT * FROM tag WHERE shortcut = ?", (key,)).fetchone()
    return _tag(row) if row else None


def add_tag(con: sqlite3.Connection, name: str, colour: str, *,
            shortcut: int | None = None) -> int:
    nxt = con.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tag").fetchone()[0]
    cur = con.execute(
        "INSERT INTO tag (name, colour, shortcut, sort_order) VALUES (?, ?, ?, ?)",
        (name, colour, shortcut, nxt))
    con.commit()
    return int(cur.lastrowid)


def delete_tag(con: sqlite3.Connection, tag_id: int) -> None:
    """Removing a tag unfiles every message from it, and deletes no mail. The
    cascade in the schema does the second half; this comment is the first."""
    con.execute("DELETE FROM tag WHERE id = ?", (tag_id,))
    con.commit()


def tags_for(con: sqlite3.Connection,
             message_ids: Sequence[int]) -> dict[int, list[Tag]]:
    """Every tag on each of these messages, in one query.

    One query rather than one per row because this is called for a page of the
    message list, and a per-row query there is the classic way to make a list
    that scrolls smoothly at ten rows and stutters at two hundred.
    """
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    rows = con.execute(f"""
        SELECT mt.message_id AS mid, t.* FROM message_tag mt
        JOIN tag t ON t.id = mt.tag_id
        WHERE mt.message_id IN ({marks})
        ORDER BY t.sort_order, t.id
    """, list(message_ids)).fetchall()
    out: dict[int, list[Tag]] = {}
    for row in rows:
        out.setdefault(row["mid"], []).append(_tag(row))
    return out


def set_on_messages(con: sqlite3.Connection, message_ids: Sequence[int],
                    tag_id: int, on: bool) -> None:
    if not message_ids:
        return
    with con:
        if on:
            con.executemany(
                "INSERT OR IGNORE INTO message_tag (message_id, tag_id) VALUES (?, ?)",
                [(mid, tag_id) for mid in message_ids])
        else:
            marks = ",".join("?" * len(message_ids))
            con.execute(
                f"DELETE FROM message_tag WHERE tag_id = ? AND message_id IN ({marks})",
                [tag_id, *message_ids])


def toggle(con: sqlite3.Connection, message_ids: Sequence[int],
           tag_id: int) -> bool:
    """Apply the tag to the selection, or remove it. Returns what it did."""
    if not message_ids:
        return False
    first = con.execute(
        "SELECT 1 FROM message_tag WHERE message_id = ? AND tag_id = ?",
        (message_ids[0], tag_id)).fetchone()
    on = first is None
    set_on_messages(con, message_ids, tag_id, on)
    return on


def unused_name(con: sqlite3.Connection, base: str = "New tag") -> str:
    """A name no tag has yet. The `name` column is UNIQUE, so "add" needs one."""
    taken = {t.name for t in list_tags(con)}
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


def update_tag(con: sqlite3.Connection, tag_id: int, *, name: str | None = None,
               colour: str | None = None,
               shortcut: int | None = None,
               clear_shortcut: bool = False) -> str:
    """Change one tag. Returns the name of the tag that LOST the key, if any.

    `clear_shortcut` rather than `shortcut=None` meaning clear: None already
    means "leave it alone" for every other field here, and one argument that
    means the opposite of its neighbours is how a tag silently loses its key.
    """
    displaced = ""
    if shortcut is not None and not clear_shortcut:
        holder = by_shortcut(con, shortcut)
        if holder is not None and holder.id != tag_id:
            con.execute("UPDATE tag SET shortcut = NULL WHERE id = ?", (holder.id,))
            displaced = holder.name
    sets, params = [], []
    if name is not None:
        sets.append("name = ?")
        params.append(name.strip() or unused_name(con))
    if colour is not None:
        sets.append("colour = ?")
        params.append(colour)
    if clear_shortcut:
        sets.append("shortcut = NULL")
    elif shortcut is not None:
        sets.append("shortcut = ?")
        params.append(int(shortcut))
    if sets:
        con.execute(f"UPDATE tag SET {', '.join(sets)} WHERE id = ?",
                    [*params, tag_id])
        con.commit()
    return displaced


def message_counts(con: sqlite3.Connection) -> dict:
    """How many messages carry each tag. What makes deleting one an informed
    decision rather than a leap."""
    return {int(r[0]): int(r[1]) for r in con.execute(
        "SELECT tag_id, COUNT(*) FROM message_tag GROUP BY tag_id").fetchall()}
