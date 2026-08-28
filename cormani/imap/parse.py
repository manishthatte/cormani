# SPDX-License-Identifier: GPL-3.0-or-later
#
# Reading what the server actually said.
#
# imaplib parses the protocol far enough to hand back a command's payload and
# no further, so everything below the response line is this module's problem.
# Three of those problems are worth stating, because each one has a wrong
# answer that looks right in testing and fails in the field.
#
# ONE — A FETCH RESPONSE IS NOT A LIST OF LINES. imaplib returns a list whose
# items are bytes when the response carried no literal and a (prefix, literal)
# TUPLE when it did, with the closing parenthesis — and any attribute that
# followed the literal — arriving as the NEXT item. Servers order FETCH items
# as they please, so `BODY[] {...}` may be followed by ` FLAGS (\Seen))`, and a
# parser that assumes the tuple is the whole response silently loses the flags.
# The assembly here walks the list as a stream of chunks and does not care
# where the boundaries fell.
#
# TWO — MONTH NAMES IN INTERNALDATE ARE ENGLISH, ALWAYS. `strptime` with %b
# reads them through the process locale, so the obvious implementation works on
# this machine and fails on a French one. The month table below is explicit for
# that reason, and the same table generates the SEARCH date strings.
#
# THREE — MAILBOX NAMES ARE MODIFIED UTF-7, AND THE STORE KEEPS THEM RAW. RFC
# 3501 §5.1.3 is a variant of UTF-7 that no codec in the standard library
# implements: `&` introduces base64 of UTF-16BE, `,` replaces `/`, padding is
# dropped, and a literal ampersand is `&-`. It is decoded for DISPLAY only.
# `folder.path` keeps the server's bytes, because it is the key used to select
# the mailbox again and a tidied name is a mailbox that can no longer be
# opened.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import base64
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# IMAP's month names, which are English regardless of the machine's locale.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NUMBER = {name.lower(): n for n, name in enumerate(_MONTHS, start=1)}


# ------------------------------------------------------- modified UTF-7
def decode_mutf7(name: str) -> str:
    """RFC 3501 §5.1.3 to text, for display. Never for a path sent to a server.

    Anything that does not decode is returned as it stands rather than raising:
    a server sending a malformed name is not a reason to hide the mailbox from
    the user, and the raw form is still recognisable.
    """
    out: list[str] = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = name.find("-", i + 1)
        if end < 0:                              # unterminated: keep it whole
            out.append(name[i:])
            break
        chunk = name[i + 1:end]
        if not chunk:                            # "&-" is a literal ampersand
            out.append("&")
        else:
            try:
                b64 = chunk.replace(",", "/")
                b64 += "=" * ((4 - len(b64) % 4) % 4)
                # validate=True is load-bearing: without it base64 DISCARDS
                # characters outside its alphabet, so a malformed run decodes
                # to the empty string and the mailbox quietly loses its name.
                raw = base64.b64decode(b64.encode("ascii"), validate=True)
                out.append(raw.decode("utf-16-be"))
            except Exception:
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


def encode_mutf7(name: str) -> str:
    """Text back to the wire form, for a mailbox this client creates."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        raw = "".join(run).encode("utf-16-be")
        b64 = base64.b64encode(raw).decode("ascii").rstrip("=")
        out.append("&" + b64.replace("/", ",") + "-")
        run.clear()

    for ch in name:
        if ch == "&":
            flush()
            out.append("&-")
        elif "\x20" <= ch <= "\x7e":
            flush()
            out.append(ch)
        else:
            run.append(ch)
    flush()
    return "".join(out)


# ------------------------------------------------------------- tokenising
class Literal(bytes):
    """A value that arrived as a {n} literal, kept apart from a quoted string.

    The distinction is not cosmetic: a literal may contain CRLF, quotes and
    unbalanced parentheses, so it must never be re-scanned as protocol text.
    """


_OPEN, _CLOSE = b"(", b")"

# `{1234}` or `{1234+}` at the end of a line — the literal announcement.
_LITERAL_SIZE = re.compile(rb"\s*\{\d+\+?\}\s*$")


def _scan(chunk: bytes) -> list[Any]:
    """One bytes chunk to tokens: b'(', b')', or a value as bytes.

    Atoms carry their bracketed section with them, because `BODY[HEADER.FIELDS
    (DATE FROM)]` is ONE item name containing spaces and parentheses. Splitting
    on whitespace here is the single most common way to get this wrong.
    """
    tokens: list[Any] = []
    i, n = 0, len(chunk)
    while i < n:
        c = chunk[i:i + 1]
        if c in b" \t\r\n":
            i += 1
        elif c in (_OPEN, _CLOSE):
            tokens.append(c)
            i += 1
        elif c == b'"':
            i += 1
            buf = bytearray()
            while i < n and chunk[i:i + 1] != b'"':
                if chunk[i:i + 1] == b"\\" and i + 1 < n:
                    i += 1
                buf += chunk[i:i + 1]
                i += 1
            i += 1                                # the closing quote
            tokens.append(bytes(buf))
        else:
            start = i
            depth = 0
            while i < n:
                d = chunk[i:i + 1]
                if d == b"[":
                    depth += 1
                elif d == b"]":
                    depth -= 1
                elif depth == 0 and (d in b" \t\r\n" or d in (_OPEN, _CLOSE)):
                    break
                i += 1
            tokens.append(chunk[start:i])
    return tokens


def _tokens(chunks: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for chunk in chunks:
        if isinstance(chunk, Literal):
            out.append(chunk)
        elif isinstance(chunk, (bytes, bytearray)):
            out.extend(_scan(bytes(chunk)))
        else:                                     # pragma: no cover - defensive
            out.append(chunk)
    return out


def _parse_group(tokens: Sequence[Any], i: int) -> tuple[list[Any], int]:
    """Read a parenthesised list starting at tokens[i] == b'('."""
    assert tokens[i] == _OPEN
    i += 1
    items: list[Any] = []
    while i < len(tokens) and tokens[i] != _CLOSE:
        if tokens[i] == _OPEN:
            sub, i = _parse_group(tokens, i)
            items.append(sub)
        else:
            items.append(tokens[i])
            i += 1
    return items, i + 1                           # step past the b')'


# ------------------------------------------------------------------ FETCH
@dataclass(frozen=True)
class Fetched:
    """One message's FETCH result, with the items the server chose to send.

    `items` is keyed by the attribute name upper-cased exactly as it arrived,
    so `BODY[]` and `BODY[HEADER]` are different keys and a caller asking for
    one cannot accidentally read the other.
    """

    seq: int
    items: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> int | None:
        return _as_int(self.items.get("UID"))

    @property
    def size(self) -> int:
        return _as_int(self.items.get("RFC822.SIZE")) or 0

    @property
    def modseq(self) -> int | None:
        value = self.items.get("MODSEQ")
        if isinstance(value, list) and value:
            return _as_int(value[0])
        return _as_int(value)

    @property
    def flags(self) -> tuple[str, ...]:
        value = self.items.get("FLAGS")
        if not isinstance(value, list):
            return ()
        return tuple(_text(f) for f in value if isinstance(f, (bytes, bytearray)))

    @property
    def internaldate(self) -> str | None:
        return parse_internaldate(self.items.get("INTERNALDATE"))

    @property
    def body(self) -> bytes | None:
        """The full message, however the server was asked for it."""
        for key in ("BODY[]", "RFC822", "BODY[]<0>"):
            value = self.items.get(key)
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
        return None

    @property
    def header(self) -> bytes | None:
        for key in ("BODY[HEADER]", "RFC822.HEADER"):
            value = self.items.get(key)
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(bytes(value).decode("ascii", "replace"))
    except (ValueError, TypeError):
        return None


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("ascii", "replace")
    return "" if value is None else str(value)


def _chunks(data: Sequence[Any]) -> list[list[Any]]:
    """Split imaplib's FETCH payload into one chunk list per message.

    Parenthesis depth decides, not item boundaries: the closing bracket of a
    response that carried a literal arrives as its own item, sometimes with
    further attributes in front of it. Counting depth over the TEXT only —
    literals are opaque — is what makes that irrelevant.
    """
    out: list[list[Any]] = []
    current: list[Any] = []
    depth = 0
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):
            # The prefix ends `... BODY[] {1234}`. That count is the protocol
            # announcing the literal, not a value: left in place it becomes the
            # attribute's value and the body itself is never read.
            prefix = _LITERAL_SIZE.sub(b"", bytes(item[0]))
            current.append(prefix)
            current.append(Literal(bytes(item[1])))
            depth += _depth(prefix)
            continue
        raw = bytes(item)
        if not raw.strip():
            continue
        if depth <= 0 and current:
            out.append(current)
            current = []
            depth = 0
        current.append(raw)
        depth += _depth(raw)
        if depth <= 0:
            out.append(current)
            current = []
            depth = 0
    if current:
        out.append(current)
    return out


def _depth(raw: bytes) -> int:
    """Net parenthesis depth of protocol text, ignoring quoted sections."""
    depth = 0
    in_quotes = False
    i = 0
    while i < len(raw):
        c = raw[i:i + 1]
        if in_quotes:
            if c == b"\\":
                i += 2
                continue
            if c == b'"':
                in_quotes = False
        elif c == b'"':
            in_quotes = True
        elif c == _OPEN:
            depth += 1
        elif c == _CLOSE:
            depth -= 1
        i += 1
    return depth


def parse_fetch(data: Sequence[Any]) -> list[Fetched]:
    """imaplib's FETCH payload to one `Fetched` per message, in wire order."""
    results: list[Fetched] = []
    for chunk in _chunks(data):
        tokens = _tokens(chunk)
        if not tokens:
            continue
        seq = _as_int(tokens[0]) or 0
        start = 1
        # A server may repeat the command name; skip to the attribute list.
        while start < len(tokens) and tokens[start] != _OPEN:
            start += 1
        if start >= len(tokens):
            continue
        group, _ = _parse_group(tokens, start)
        items: dict[str, Any] = {}
        i = 0
        while i < len(group):
            name = group[i]
            if not isinstance(name, (bytes, bytearray)):     # pragma: no cover
                i += 1
                continue
            key = bytes(name).decode("ascii", "replace").upper()
            value: Any = None
            if i + 1 < len(group):
                value = group[i + 1]
                i += 2
            else:
                i += 1
            if isinstance(value, (bytes, bytearray)) and bytes(value) == b"NIL":
                value = None
            items[key] = value
        results.append(Fetched(seq=seq, items=items))
    return results


# ----------------------------------------------------------------- LIST
@dataclass(frozen=True)
class Mailbox:
    attributes: tuple[str, ...]
    delimiter: str
    path: str                 # the server's name, raw; the key to select it
    display_name: str         # the same name decoded, for a person to read

    @property
    def selectable(self) -> bool:
        return "\\noselect" not in {a.lower() for a in self.attributes}


def parse_list_line(line: Any) -> Mailbox | None:
    """One LIST/LSUB response to a Mailbox, or None if it is not one.

    None rather than an exception: servers emit untagged responses in the
    middle of a LIST, and a client that dies on the first unexpected line is a
    client that cannot talk to half the servers in the world.
    """
    chunks: list[Any] = []
    if isinstance(line, tuple):
        chunks = [bytes(line[0]), Literal(bytes(line[1]))]
    elif isinstance(line, (bytes, bytearray)):
        chunks = [bytes(line)]
    else:                                                    # pragma: no cover
        return None

    tokens = _tokens(chunks)
    if not tokens or tokens[0] != _OPEN:
        return None
    attrs_raw, i = _parse_group(tokens, 0)
    attributes = tuple(_text(a) for a in attrs_raw if isinstance(a, (bytes, bytearray)))

    if i >= len(tokens):
        return None
    delim_raw = tokens[i]
    i += 1
    delimiter = "" if bytes(delim_raw) == b"NIL" else _text(delim_raw)

    if i >= len(tokens):
        return None
    name_raw = tokens[i]
    if isinstance(name_raw, Literal):
        path = bytes(name_raw).decode("utf-8", "replace")
    else:
        path = bytes(name_raw).decode("ascii", "replace")
    if not path:
        return None
    return Mailbox(attributes=attributes, delimiter=delimiter, path=path,
                   display_name=decode_mutf7(path))


# ------------------------------------------------------------------ dates
def parse_internaldate(value: Any) -> str | None:
    """An INTERNALDATE to the store's ISO-8601 UTC string.

    The month table is explicit rather than %b: IMAP's month names are English
    and `strptime` reads them through the process locale.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else _text(value)
    text = text.strip().strip('"')
    m = re.match(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+"
                 r"(\d{2}):(\d{2}):(\d{2})\s*([+-]\d{4})?\s*$", text)
    if not m:
        return None
    month = _MONTH_NUMBER.get(m.group(2).lower())
    if month is None:
        return None
    offset = dt.timedelta(0)
    if m.group(7):
        sign = 1 if m.group(7)[0] == "+" else -1
        offset = sign * dt.timedelta(hours=int(m.group(7)[1:3]),
                                     minutes=int(m.group(7)[3:5]))
    try:
        stamp = dt.datetime(int(m.group(3)), month, int(m.group(1)),
                            int(m.group(4)), int(m.group(5)), int(m.group(6)),
                            tzinfo=dt.timezone.utc) - offset
    except ValueError:
        return None
    return stamp.replace(microsecond=0).isoformat()


def search_date(when: dt.date | dt.datetime) -> str:
    """A SEARCH date — `1-Jan-2026`. Same table, same reason."""
    return f"{when.day}-{_MONTHS[when.month - 1]}-{when.year}"


# ------------------------------------------------- untagged status values
_STATUS_NAMES = ("UIDVALIDITY", "UIDNEXT", "HIGHESTMODSEQ", "UNSEEN", "EXISTS")
_STATUS = re.compile(rb"\[?\s*(" + b"|".join(n.encode() for n in _STATUS_NAMES) +
                     rb")\s+(\d+)", re.IGNORECASE)


def status_values(responses: Iterable[Any]) -> dict[str, int]:
    """Pull UIDVALIDITY, UIDNEXT and friends out of whatever SELECT returned.

    Written as a scan rather than a lookup because the same numbers arrive in
    three shapes depending on the server and on imaplib's mood: as an untagged
    response, inside an OK response code, and as a bare number for EXISTS.
    """
    found: dict[str, int] = {}
    for item in _flatten(responses):
        if not isinstance(item, (bytes, bytearray)):
            continue
        for m in _STATUS.finditer(bytes(item)):
            found[m.group(1).decode("ascii").upper()] = int(m.group(2))
    # imaplib's `untagged_responses` is a mapping, so the name and the number
    # never appear in the same string and the scan above cannot see them. Read
    # last, so that the authoritative shape wins.
    if isinstance(responses, dict):
        for key, value in responses.items():
            name = _text(key).upper() if not isinstance(key, str) else key.upper()
            if name not in _STATUS_NAMES:
                continue
            for item in _flatten(value):
                digits = re.search(rb"\d+", bytes(item) if not isinstance(item, str)
                                   else item.encode("ascii", "replace"))
                if digits:
                    found[name] = int(digits.group(0))
                    break
    return found


def _flatten(value: Any) -> Iterable[Any]:
    if isinstance(value, (bytes, bytearray, str)):
        yield value
    elif isinstance(value, dict):
        for key, sub in value.items():
            yield key.encode("ascii", "replace") if isinstance(key, str) else key
            yield from _flatten(sub)
    elif isinstance(value, (list, tuple)):
        for sub in value:
            yield from _flatten(sub)
    elif value is not None:                                  # pragma: no cover
        yield str(value).encode("ascii", "replace")


def parse_uid_list(data: Sequence[Any]) -> list[int]:
    """A UID SEARCH result to a sorted list of UIDs."""
    uids: set[int] = set()
    for item in _flatten(data):
        if isinstance(item, str):                            # pragma: no cover
            item = item.encode("ascii", "replace")
        for token in bytes(item).split():
            if token.isdigit():
                uids.add(int(token))
    return sorted(uids)


def expand_uid_set(spec: str) -> list[int]:
    """`1:3,7` back to [1, 2, 3, 7].

    No `*` handling, deliberately: the only place this is needed is COPYUID,
    where both sets are explicit. A `*` here would have to mean "the highest
    UID in some mailbox", and this function has no mailbox.
    """
    out: list[int] = []
    for piece in (spec or "").replace(" ", "").split(","):
        if not piece:
            continue
        if ":" in piece:
            lo_s, hi_s = piece.split(":", 1)
            if not (lo_s.isdigit() and hi_s.isdigit()):
                continue
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
        elif piece.isdigit():
            out.append(int(piece))
    return out


_COPYUID = re.compile(rb"\[COPYUID\s+(\d+)\s+([\d:,]+)\s+([\d:,]+)\]", re.IGNORECASE)


def parse_copyuid(data: Any) -> dict[int, int]:
    """The UIDs a COPY or MOVE gave the messages in their new folder.

    Without this a moved message keeps no UID at all, the next sync of the
    destination sees it as new, and the store ends up holding it twice — once
    as the row the user moved and once as the row the server sent. The two sets
    are positionally paired, which is what the RFC 4315 grammar means and why
    both are expanded rather than compared.
    """
    for item in _flatten(data):
        if isinstance(item, str):                            # pragma: no cover
            item = item.encode("ascii", "replace")
        m = _COPYUID.search(bytes(item))
        if not m:
            continue
        source = expand_uid_set(m.group(2).decode("ascii"))
        destination = expand_uid_set(m.group(3).decode("ascii"))
        if len(source) != len(destination):                  # pragma: no cover
            return {}
        return dict(zip(source, destination))
    return {}


def uid_ranges(uids: Sequence[int]) -> str:
    """Collapse UIDs into an IMAP set — `1:5,9,20:22`.

    Worth the code: a first sync of a Gmail account fetches flags for tens of
    thousands of UIDs, and sending them one per comma builds a command line
    long enough that servers refuse it.
    """
    ordered = sorted(set(int(u) for u in uids))
    if not ordered:
        return ""
    parts: list[str] = []
    start = previous = ordered[0]
    for uid in ordered[1:]:
        if uid == previous + 1:
            previous = uid
            continue
        parts.append(str(start) if start == previous else f"{start}:{previous}")
        start = previous = uid
    parts.append(str(start) if start == previous else f"{start}:{previous}")
    return ",".join(parts)
