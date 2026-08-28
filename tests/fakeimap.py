# SPDX-License-Identifier: GPL-3.0-or-later
#
# An IMAP server, in the test process.
#
# CONVENTIONS.txt §9 requires the suite to pass with no network. The tempting
# reading is to mock `imaplib` — and it is the wrong one, because the thing
# most likely to be wrong is precisely how imaplib hands responses back. A mock
# returns whatever shape the test author imagined, which is the same
# imagination that wrote the parser.
#
# So this is a real server speaking the real protocol, and a real
# `imaplib.IMAP4` talks to it. What is faked is only the SOCKET: `open`, `read`,
# `readline` and `send` are redirected into this process. Command construction,
# literal handling, continuation lines and response parsing are all imaplib's
# own, so the tests exercise the code that actually runs.
#
# The server keeps a `log` of every command line it was given. Tests assert on
# it for things the result cannot show — that a second sync did not re-download
# bodies, that one ranged FETCH went out rather than a thousand small ones.
#
# It is strict where a real server is strict: a command in the wrong state is
# refused, UIDs are never reused, and UIDVALIDITY changes when a test says so.
# The faults it can be told to inject exist because the back-off and
# resynchronisation paths cannot be reached any other way.
#
# ONE SERVER, ONE SESSION AT A TIME. The mailbox store and the connection
# state live on the same object, which keeps a test to two lines and costs one
# restriction: two `IMAP4_Fake` clients on one `Server` share `selected` and
# `state` and will confuse each other. Opening a connection RESETS the session
# — a logout must not poison the server for the next connection, which is
# exactly what an engine does between accounts — while the mailboxes, the log
# and any injected fault persist for the server's whole life.
#
# The protocol grammar lives in imapwire.py, written independently of
# cormani.imap.parse so that one misreading of the RFC cannot satisfy both
# sides of a test. The socket that is not a socket lives in faketransport.py,
# and `IMAP4_Fake` is re-exported here because `fakeimap.IMAP4_Fake` is what
# every test asks for.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

from faketransport import Broken, IMAP4_Fake  # noqa: F401  (re-exported)
from imapwire import (CRLF, expand_uid_set, fetch_line, imap_date,  # noqa: F401
                      internal_to_date, list_line, parse_search_date,
                      split_args, tagged, untagged)

DEFAULT_CAPABILITIES = (
    "IMAP4rev1", "LITERAL+", "SASL-IR", "AUTH=PLAIN", "AUTH=XOAUTH2",
    "IDLE", "NAMESPACE", "UIDPLUS", "MOVE", "CONDSTORE", "SPECIAL-USE",
)


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    flags: set = field(default_factory=set)
    internaldate: str = "25-Aug-2026 10:00:00 +0000"
    modseq: int = 1


@dataclass
class Mailbox:
    path: str
    attributes: tuple = ()
    uidvalidity: int = 1000
    uidnext: int = 1
    highest_modseq: int = 1
    subscribed: bool = True
    messages: list = field(default_factory=list)

    def by_uid(self, uid: int):
        for m in self.messages:
            if m.uid == uid:
                return m
        return None


class Refused(Exception):
    """A NO response — a refusal, not a protocol failure."""


class Server:
    """A mailbox store, and the commands over it."""

    def __init__(self, *, capabilities=DEFAULT_CAPABILITIES, delimiter="/",
                 greeting_capabilities: bool = True):
        self.capabilities = list(capabilities)
        self.delimiter = delimiter
        self.greeting_capabilities = greeting_capabilities
        self.mailboxes: dict = {}
        self.passwords: dict = {}
        self.tokens: set = set()
        self.log: list = []
        self.state = "nonauth"
        self.selected: str | None = None
        self.readonly = False
        self.user: str | None = None
        self.closed = False
        self.idling = False
        self.condstore_enabled = False
        # Fault injection: the only way to reach back-off and resynchronisation.
        self.fail_next: dict = {}
        # Whatever a client has APPENDed, for a test to look at, and the state
        # of a literal being read.
        self.appended: list = []
        self._literal = None
        self.drop_after: int | None = None
        self._commands = 0
        self._untagged: list = []
        self._awaiting = None
        self._buffer = bytearray()
        self._idle_tag = b""

    # ------------------------------------------------------------ building
    def add_mailbox(self, path: str, *, attributes=(), uidvalidity: int = 1000,
                    subscribed: bool = True) -> Mailbox:
        box = Mailbox(path=path, attributes=tuple(attributes),
                      uidvalidity=uidvalidity, subscribed=subscribed)
        self.mailboxes[path] = box
        return box

    def add_message(self, path: str, raw: bytes, *, flags=(),
                    internaldate: str | None = None) -> int:
        box = self.mailboxes[path]
        box.highest_modseq += 1
        message = FakeMessage(uid=box.uidnext, raw=raw, flags=set(flags),
                              modseq=box.highest_modseq)
        if internaldate:
            message.internaldate = internaldate
        box.messages.append(message)
        box.uidnext += 1
        if self.selected == path:
            self._untagged.append(f"{len(box.messages)} EXISTS")
        return message.uid

    def expunge_uid(self, path: str, uid: int) -> None:
        """Remove a message behind the client's back, as another client would."""
        box = self.mailboxes[path]
        box.messages = [m for m in box.messages if m.uid != uid]
        box.highest_modseq += 1

    def set_flags(self, path: str, uid: int, flags) -> None:
        box = self.mailboxes[path]
        message = box.by_uid(uid)
        if message is not None:
            message.flags = set(flags)
            box.highest_modseq += 1
            message.modseq = box.highest_modseq

    def begin_session(self) -> None:
        """A new connection. Resets what belongs to a session and nothing else.

        The mailboxes, the command log and any injected fault survive: they are
        the server's, and a test that sets `drop_after` before a reconnection
        means it to apply to the reconnection.
        """
        self.state = "nonauth"
        self.selected = None
        self.readonly = False
        self.user = None
        self.closed = False
        self.idling = False
        self._untagged = []
        self._awaiting = None
        self._buffer = bytearray()

    def commands_matching(self, pattern: str) -> list:
        rx = re.compile(pattern, re.IGNORECASE)
        return [line for line in self.log if rx.search(line)]

    # ------------------------------------------------------------- feeding
    def greeting(self) -> bytes:
        if self.greeting_capabilities:
            return b"* OK [CAPABILITY " + self._caps() + b"] fakeimap ready" + CRLF
        return b"* OK fakeimap ready" + CRLF

    def _caps(self) -> bytes:
        return " ".join(self.capabilities).encode("ascii")

    def feed(self, data: bytes) -> bytes:
        """Consume client bytes; return whatever the server has to say."""
        if self.closed:
            raise Broken("the client wrote to a closed connection")
        out = bytearray()
        self._buffer += data
        while True:
            if self._literal is not None:
                # A literal is COUNTED, not delimited: the bytes may contain
                # CRLF and often do, so the line loop must not touch them.
                wanted, handler = self._literal
                if len(self._buffer) < wanted:
                    break
                payload = bytes(self._buffer[:wanted])
                del self._buffer[:wanted]
                self._literal = None
                out += handler(payload)
                continue
            end = self._buffer.find(CRLF)
            if end < 0:
                break
            line = bytes(self._buffer[:end])
            del self._buffer[:end + 2]
            out += self._line(line)
        return bytes(out)

    def _line(self, line: bytes) -> bytes:
        if self._awaiting is not None:
            handler, self._awaiting = self._awaiting, None
            return handler(line)
        if self.idling:
            if line.strip().upper() != b"DONE":
                return b""
            # Logged, so a test can assert the client actually sent it. A
            # client that forgets DONE leaves the server holding a connection
            # that will never answer another command.
            self.log.append("DONE")
            self.idling = False
            return self._flush() + tagged(self._idle_tag, "OK IDLE terminated")

        self.log.append(line.decode("ascii", "replace"))
        self._commands += 1
        if self.drop_after is not None and self._commands > self.drop_after:
            self.closed = True
            raise Broken("the server dropped the connection")

        parts = split_args(line)
        if len(parts) < 2:
            return untagged("BAD unparseable")
        tag = parts[0]
        name = parts[1].decode("ascii", "replace").upper()

        forced = self.fail_next.pop(name, None)
        if forced:
            return tagged(tag, forced)

        handler = getattr(self, f"_do_{name.lower()}", None)
        if handler is None:
            return tagged(tag, f"BAD unknown command {name}")
        try:
            return handler(tag, parts[2:])
        except Refused as exc:
            return tagged(tag, f"NO {exc}")
        except Broken:
            raise
        except Exception as exc:                             # pragma: no cover
            return tagged(tag, f"BAD {exc.__class__.__name__}: {exc}")

    # ---------------------------------------------------------- appending
    def _do_append(self, tag, args):
        """APPEND, which is how a client files a copy of what it sent.

        The interesting half is the LITERAL: imaplib sends the command line
        ending in {N}, waits for a continuation, and then writes N bytes. A fake
        that read those bytes as lines would corrupt every message with a blank
        line in it, which is every message.
        """
        parts = [a.decode("ascii", "replace") for a in args]
        path = parts[0].strip('"')
        if path not in self.mailboxes:
            raise Refused(f"[TRYCREATE] no such mailbox: {path}")
        flags = ()
        for piece in parts[1:]:
            if piece.startswith("("):
                flags = tuple(
                    f for f in " ".join(parts[1:]).partition("(")[2]
                    .partition(")")[0].split() if f)
                break
        size = 0
        for piece in parts[-1:]:
            if piece.startswith("{") and piece.endswith("}"):
                size = int(piece[1:-1].rstrip("+"))

        def finish(payload: bytes) -> bytes:
            # imaplib writes the literal and then a CRLF of its own, which the
            # line loop consumes as an empty line. Nothing to do about that
            # here; the message is the payload.
            uid = self.add_message(path, payload.rstrip(CRLF) + CRLF,
                                   flags=flags)
            self.appended.append((path, payload))
            return tagged(tag, f"OK [APPENDUID {self.mailboxes[path].uidvalidity} "
                               f"{uid}] APPEND completed")

        self._literal = (size, finish)
        return b"+ Ready for literal data" + CRLF

    # ---------------------------------------------------- session commands
    def _do_capability(self, tag, args):
        return b"* CAPABILITY " + self._caps() + CRLF + tagged(tag, "OK done")

    def _do_noop(self, tag, args):
        return self._flush() + tagged(tag, "OK NOOP done")

    def _do_logout(self, tag, args):
        self.state = "logout"
        return untagged("BYE logging out") + tagged(tag, "OK LOGOUT done")

    def _do_enable(self, tag, args):
        if any(a.decode("ascii", "replace").upper() == "CONDSTORE" for a in args):
            self.condstore_enabled = True
        return tagged(tag, "OK ENABLE done")

    def _do_login(self, tag, args):
        if len(args) < 2:
            return tagged(tag, "BAD LOGIN needs two arguments")
        user = args[0].decode("utf-8", "replace")
        if self.passwords.get(user) != args[1].decode("utf-8", "replace"):
            raise Refused("[AUTHENTICATIONFAILED] Invalid credentials")
        self.user, self.state = user, "auth"
        return tagged(tag, "OK LOGIN completed")

    def _do_authenticate(self, tag, args):
        mechanism = args[0].decode("ascii", "replace").upper() if args else ""
        if mechanism != "XOAUTH2":
            raise Refused(f"unsupported mechanism {mechanism}")
        if len(args) > 1:                       # SASL-IR: the initial response
            return self._xoauth2(tag, args[1])
        self._awaiting = lambda line: self._xoauth2(tag, line)
        return b"+ " + CRLF

    def _xoauth2(self, tag, payload):
        try:
            decoded = base64.b64decode(payload).decode("utf-8", "replace")
        except Exception:
            raise Refused("[AUTHENTICATIONFAILED] undecodable SASL payload")
        m = re.match(r"user=(?P<user>[^\x01]*)\x01"
                     r"auth=Bearer (?P<token>[^\x01]*)\x01\x01", decoded)
        if not m:
            raise Refused("[AUTHENTICATIONFAILED] malformed XOAUTH2")
        if m.group("token") not in self.tokens:
            # A real server sends a base64 error and waits for an empty line
            # before the tagged NO. Modelled, because a client that does not
            # send that line HANGS — a bug worth catching here rather than in
            # the field at three in the morning.
            self._awaiting = lambda line: tagged(
                tag, "NO [AUTHENTICATIONFAILED] Invalid credentials")
            return b"+ " + base64.b64encode(
                b'{"status":"400","schemes":"Bearer"}') + CRLF
        self.user, self.state = m.group("user"), "auth"
        return tagged(tag, "OK XOAUTH2 authentication successful")

    def _require_auth(self):
        if self.state not in ("auth", "selected"):
            raise Refused("please authenticate first")

    # ------------------------------------------------------------ mailboxes
    def _do_list(self, tag, args):
        self._require_auth()
        return self._listing(tag, b"LIST", subscribed_only=False)

    def _do_lsub(self, tag, args):
        self._require_auth()
        return self._listing(tag, b"LSUB", subscribed_only=True)

    def _listing(self, tag, word, *, subscribed_only):
        out = bytearray()
        for box in self.mailboxes.values():
            if subscribed_only and not box.subscribed:
                continue
            out += list_line(word, box.attributes, self.delimiter, box.path)
        return bytes(out) + tagged(tag, f"OK {word.decode()} done")

    def _do_select(self, tag, args):
        return self._select(tag, args, readonly=False)

    def _do_examine(self, tag, args):
        return self._select(tag, args, readonly=True)

    def _select(self, tag, args, *, readonly):
        self._require_auth()
        path = args[0].decode("utf-8", "replace") if args else ""
        box = self.mailboxes.get(path)
        if box is None:
            raise Refused(f"[NONEXISTENT] Unknown Mailbox: {path}")
        self.selected, self.state, self.readonly = path, "selected", readonly
        self._untagged = []
        out = bytearray()
        out += untagged(f"{len(box.messages)} EXISTS")
        out += untagged("0 RECENT")
        out += untagged("FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)")
        out += untagged(f"OK [UIDVALIDITY {box.uidvalidity}] UIDs valid")
        out += untagged(f"OK [UIDNEXT {box.uidnext}] Predicted next UID")
        if "CONDSTORE" in self.capabilities:
            out += untagged(f"OK [HIGHESTMODSEQ {box.highest_modseq}]")
        word = "[READ-ONLY]" if readonly else "[READ-WRITE]"
        return bytes(out) + tagged(tag, f"OK {word} done")

    def _do_close(self, tag, args):
        self.selected, self.state = None, "auth"
        return tagged(tag, "OK CLOSE done")

    def _do_unselect(self, tag, args):
        return self._do_close(tag, args)

    def _box(self) -> Mailbox:
        if self.state != "selected" or self.selected is None:
            raise Refused("no mailbox selected")
        return self.mailboxes[self.selected]

    # ------------------------------------------------------------- messages
    def _do_uid(self, tag, args):
        if not args:
            return tagged(tag, "BAD UID needs a command")
        name = args[0].decode("ascii", "replace").upper()
        rest = args[1:]
        if name == "SEARCH":
            return self._uid_search(tag, rest)
        if name == "FETCH":
            return self._uid_fetch(tag, rest)
        if name == "STORE":
            return self._uid_store(tag, rest)
        if name in ("COPY", "MOVE"):
            return self._uid_copy(tag, rest, move=(name == "MOVE"))
        if name == "EXPUNGE":
            return self._uid_expunge(tag, rest)
        return tagged(tag, "BAD unknown UID command")

    def _do_search(self, tag, args):
        return self._uid_search(tag, args)

    def _uid_search(self, tag, args):
        box = self._box()
        words = [a.decode("ascii", "replace") for a in args]
        uids = [m.uid for m in box.messages]
        matched, i = list(uids), 0
        while i < len(words):
            word = words[i].upper()
            if word == "UID" and i + 1 < len(words):
                allowed = set(expand_uid_set(words[i + 1], uids))
                matched = [u for u in matched if u in allowed]
                i += 2
            elif word == "UNSEEN":
                matched = [u for u in matched if "\\Seen" not in box.by_uid(u).flags]
                i += 1
            elif word == "SINCE" and i + 1 < len(words):
                cutoff = parse_search_date(words[i + 1])
                matched = [u for u in matched
                           if internal_to_date(box.by_uid(u).internaldate) >= cutoff]
                i += 2
            else:                                    # ALL, and anything unknown
                i += 1
        body = " ".join(str(u) for u in matched)
        return untagged(f"SEARCH {body}".strip()) + tagged(tag, "OK SEARCH done")

    def _uid_fetch(self, tag, args):
        box = self._box()
        if not args:
            return tagged(tag, "BAD UID FETCH needs a set")
        items = b" ".join(args[1:]).decode("ascii", "replace").upper()
        changed = re.search(r"CHANGEDSINCE\s+(\d+)", items)
        since = int(changed.group(1)) if changed else None
        wanted = set(expand_uid_set(args[0].decode("ascii", "replace"),
                                    [m.uid for m in box.messages]))
        out = bytearray()
        for seq, message in enumerate(box.messages, start=1):
            if message.uid not in wanted:
                continue
            if since is not None and message.modseq <= since:
                continue
            out += self._fetch_one(seq, message, items)
        return bytes(out) + tagged(tag, "OK UID FETCH done")

    def _fetch_one(self, seq, message, items):
        # UID comes back on a UID FETCH whether or not it was asked for. Real
        # servers do this and clients rely on it.
        pieces = [f"UID {message.uid}".encode("ascii")]
        if "FLAGS" in items:
            pieces.append(f"FLAGS ({' '.join(sorted(message.flags))})".encode("ascii"))
        if "INTERNALDATE" in items:
            pieces.append(f'INTERNALDATE "{message.internaldate}"'.encode("ascii"))
        if "RFC822.SIZE" in items:
            pieces.append(f"RFC822.SIZE {len(message.raw)}".encode("ascii"))
        if "MODSEQ" in items or "CHANGEDSINCE" in items:
            pieces.append(f"MODSEQ ({message.modseq})".encode("ascii"))
        literal = None
        if "BODY.PEEK[]" in items or "BODY[]" in items:
            literal = ("BODY[]", message.raw)
        elif "BODY.PEEK[HEADER]" in items or "BODY[HEADER]" in items:
            literal = ("BODY[HEADER]",
                       message.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n")
        return fetch_line(seq, pieces, literal)

    def _uid_store(self, tag, args):
        box = self._box()
        if self.readonly:
            raise Refused("mailbox is read-only")
        if len(args) < 3:
            return tagged(tag, "BAD UID STORE needs a set, an item and flags")
        wanted = set(expand_uid_set(args[0].decode("ascii", "replace"),
                                    [m.uid for m in box.messages]))
        item = args[1].decode("ascii", "replace").upper()
        flags = set(args[2].decode("ascii", "replace").strip("()").split())
        out = bytearray()
        for seq, message in enumerate(box.messages, start=1):
            if message.uid not in wanted:
                continue
            if item.startswith("+FLAGS"):
                message.flags |= flags
            elif item.startswith("-FLAGS"):
                message.flags -= flags
            else:
                message.flags = set(flags)
            box.highest_modseq += 1
            message.modseq = box.highest_modseq
            if not item.endswith(".SILENT"):
                out += untagged(f"{seq} FETCH (UID {message.uid} FLAGS "
                                f"({' '.join(sorted(message.flags))}))")
        return bytes(out) + tagged(tag, "OK UID STORE done")

    def _uid_copy(self, tag, args, *, move):
        box = self._box()
        if len(args) < 2:
            return tagged(tag, "BAD needs a set and a mailbox")
        wanted = set(expand_uid_set(args[0].decode("ascii", "replace"),
                                    [m.uid for m in box.messages]))
        target_path = args[1].decode("utf-8", "replace")
        target = self.mailboxes.get(target_path)
        if target is None:
            raise Refused(f"[TRYCREATE] Unknown Mailbox: {target_path}")
        source, destination = [], []
        for message in list(box.messages):
            if message.uid not in wanted:
                continue
            target.highest_modseq += 1
            copied = FakeMessage(uid=target.uidnext, raw=message.raw,
                                 flags=set(message.flags),
                                 internaldate=message.internaldate,
                                 modseq=target.highest_modseq)
            target.messages.append(copied)
            target.uidnext += 1
            source.append(message.uid)
            destination.append(copied.uid)
            if move:
                box.messages.remove(message)
        word = "MOVE" if move else "COPY"
        # COPYUID is how a client learns the destination UIDs without a second
        # sync — and it comes from UIDPLUS. A server that does not advertise it
        # does not send it, which is the case the client's fallback exists for.
        code = ""
        if "UIDPLUS" in self.capabilities:
            code = (f"[COPYUID {target.uidvalidity} "
                    f"{','.join(str(u) for u in source)} "
                    f"{','.join(str(u) for u in destination)}] ")
        return tagged(tag, f"OK {code}UID {word} done")

    def _uid_expunge(self, tag, args):
        box = self._box()
        wanted = set(expand_uid_set(args[0].decode("ascii", "replace"),
                                    [m.uid for m in box.messages])) if args else set()
        return self._expunge(tag, [m for m in box.messages
                                   if m.uid in wanted and "\\Deleted" in m.flags])

    def _do_expunge(self, tag, args):
        return self._expunge(tag, [m for m in self._box().messages
                                   if "\\Deleted" in m.flags])

    def _expunge(self, tag, doomed):
        box = self._box()
        if self.readonly:
            raise Refused("mailbox is read-only")
        out = bytearray()
        for message in doomed:
            seq = box.messages.index(message) + 1
            box.messages.remove(message)
            box.highest_modseq += 1
            out += untagged(f"{seq} EXPUNGE")
        return bytes(out) + tagged(tag, "OK EXPUNGE done")

    # ----------------------------------------------------------------- IDLE
    def _do_idle(self, tag, args):
        self._require_auth()
        self.idling = True
        self._idle_tag = tag
        return b"+ idling" + CRLF

    def push(self, path: str, raw: bytes, **kwargs) -> int:
        """Mail arriving while the client waits. The EXISTS goes out on the
        next read, which is what makes an IDLE test possible at all."""
        return self.add_message(path, raw, **kwargs)

    def pending(self) -> bytes:
        """Untagged lines the server is holding, for the transport to deliver."""
        return self._flush()

    def _flush(self) -> bytes:
        out = bytearray()
        for text in self._untagged:
            out += untagged(text)
        self._untagged = []
        return bytes(out)
