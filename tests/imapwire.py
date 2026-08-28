# SPDX-License-Identifier: GPL-3.0-or-later
#
# The IMAP vocabulary the fake server speaks, apart from the server itself.
#
# Splitting arguments, expanding a UID set and formatting a response are the
# grammar; deciding what a command DOES is the server. Kept apart because they
# fail differently — a mistake here is a malformed line that imaplib rejects
# loudly, a mistake there is a server that answers the wrong question quietly.
#
# NOTHING HERE IMPORTS THE CODE UNDER TEST. `cormani.imap.parse` solves the
# mirror image of the same problem, and sharing an implementation between the
# two would let a single misreading of the RFC satisfy both sides of every
# test. The duplication is the point.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

CRLF = b"\r\n"

# English, always — see the note in cormani/imap/parse.py. The same fact bites
# on both sides of the wire.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(when: dt.datetime) -> str:
    """A datetime as INTERNALDATE gives it."""
    return (f"{when.day:2d}-{MONTHS[when.month - 1]}-{when.year} "
            f"{when.hour:02d}:{when.minute:02d}:{when.second:02d} +0000")


def parse_search_date(text: str) -> dt.date:
    day, month, year = text.strip('"').split("-")
    return dt.date(int(year), MONTHS.index(month[:3].title()) + 1, int(day))


def internal_to_date(text: str) -> dt.date:
    day, month, rest = text.strip().split("-", 2)
    return dt.date(int(rest.split()[0]),
                   MONTHS.index(month[:3].title()) + 1, int(day))


def split_args(line: bytes) -> list:
    """Command arguments, honouring quotes and keeping a (…) group whole."""
    out: list = []
    i, n = 0, len(line)
    while i < n:
        c = line[i:i + 1]
        if c in b" \t":
            i += 1
        elif c == b'"':
            i += 1
            buf = bytearray()
            while i < n and line[i:i + 1] != b'"':
                if line[i:i + 1] == b"\\" and i + 1 < n:
                    i += 1
                buf += line[i:i + 1]
                i += 1
            i += 1
            out.append(bytes(buf))
        elif c == b"(":
            depth, start = 0, i
            while i < n:
                if line[i:i + 1] == b"(":
                    depth += 1
                elif line[i:i + 1] == b")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            out.append(line[start:i])
        else:
            start = i
            while i < n and line[i:i + 1] not in b" \t":
                i += 1
            out.append(line[start:i])
    return out


def expand_uid_set(spec: str, uids: list) -> list:
    """`1:5,9,20:*` against the UIDs a mailbox actually has.

    `*` is the HIGHEST EXISTING UID, not uid_next and not infinity. Getting
    that wrong makes `1:*` on an empty mailbox mean something, and makes
    `900:*` silently miss the last message.
    """
    if not uids:
        return []
    highest = max(uids)
    wanted: set = set()
    for piece in spec.replace(" ", "").split(","):
        if not piece:
            continue
        if ":" in piece:
            lo_s, hi_s = piece.split(":", 1)
            lo = highest if lo_s == "*" else int(lo_s)
            hi = highest if hi_s == "*" else int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            wanted |= {u for u in uids if lo <= u <= hi}
        elif piece == "*":
            wanted.add(highest)
        else:
            wanted.add(int(piece))
    return sorted(u for u in uids if u in wanted)


# ------------------------------------------------------------- formatting
def untagged(text: str) -> bytes:
    return b"* " + text.encode("ascii") + CRLF


def tagged(tag: bytes, text: str) -> bytes:
    return tag + b" " + text.encode("ascii") + CRLF


def list_line(word: bytes, attributes, delimiter: str, path: str) -> bytes:
    attrs = " ".join(attributes) or "\\HasNoChildren"
    delim = f'"{delimiter}"'.encode("ascii") if delimiter else b"NIL"
    return (b"* " + word + b" (" + attrs.encode("ascii") + b") " + delim +
            b' "' + path.encode("ascii") + b'"' + CRLF)


def fetch_line(seq: int, pieces: list, literal: tuple | None) -> bytes:
    """`* n FETCH (...)`, with the literal in the MIDDLE when there is one.

    The closing parenthesis goes after the literal rather than before it. That
    is what makes imaplib return a (prefix, bytes) tuple followed by a separate
    item — the shape `cormani.imap.parse` exists to survive, and it must be
    produced here rather than assumed.
    """
    head = f"* {seq} FETCH (".encode("ascii") + b" ".join(pieces)
    if literal is None:
        return head + b")" + CRLF
    name, payload = literal
    return (head + b" " + name.encode("ascii") +
            b" {" + str(len(payload)).encode("ascii") + b"}" + CRLF +
            payload + b")" + CRLF)
