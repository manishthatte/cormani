# SPDX-License-Identifier: GPL-3.0-or-later
#
# One IMAP connection, and the commands corMani sends over it.
#
# A thin layer over `imaplib` and deliberately so: imaplib is standard library,
# it is packaged in Debian by definition, and CONVENTIONS.txt §3 forbids
# vendoring the alternatives. What it does NOT do is anything above the
# protocol, which is the rest of this package.
#
# Four things here are not obvious:
#
# IMAPLIB DOES NOT RAISE ON `NO`. Only `BAD` and a dropped connection become
# exceptions; a refusal comes back as `('NO', [reason])` from an ordinary
# return. Every command therefore goes through `_ok`, which is the single place
# a refusal becomes the right member of the errors taxonomy. A caller that
# reads `typ` itself has bypassed the one thing that decides retry-or-stop.
#
# TWO PLACES TOUCH IMAPLIB'S PRIVATE SURFACE, AND THERE ARE EXACTLY TWO.
# Both are named here so that a third has to be argued for.
#
#   `idle()` — Python 3.13's imaplib has no `IMAP4.idle()`. It arrives in a
#   later version, and corMani runs on the Python Debian ships today. The whole
#   sequence is in one method, so replacing it with the standard one is a
#   single substitution rather than a search.
#
#   `_tagged_command()` — `IMAP4.uid()` DISCARDS the tagged response and hands
#   back the untagged FETCH lines instead. COPYUID arrives only in the tagged
#   line, and without it a moved message has no UID in its new folder, is
#   fetched again by the next sync, and ends up in the store twice.
#
# THE 29-MINUTE LIMIT IS NOT A CHOICE. RFC 2177 says a server may drop an idle
# connection after 30 minutes, and Gmail does. The renewal is the caller's
# loop; this method returns when its timeout expires so that the caller can
# re-issue rather than discovering the connection died an hour ago.
#
# BODY.PEEK[], NEVER BODY[]. `BODY[]` sets \Seen as a side effect of reading.
# A sync that marks mail read merely by downloading it is the single most
# destructive bug a mail client can have, and the difference is six characters.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import imaplib
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from . import parse
from .errors import (AuthFailed, ImapError, MailboxGone, ProtocolError,
                     Transient, classify)

# IDLE is not in imaplib's command table on this Python, and `_command`
# refuses anything that is not. Registered once, at import: the states are the
# ones RFC 2177 permits.
imaplib.Commands.setdefault("IDLE", ("AUTH", "SELECTED"))

DEFAULT_PORT = 993
DEFAULT_TIMEOUT = 60.0

# What a body fetch asks for. PEEK, always — see the module header.
BODY_ITEMS = "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])"
FLAG_ITEMS = "(UID FLAGS)"


@dataclass(frozen=True)
class FolderState:
    """What SELECT said. The input to every decision the sync makes."""

    path: str
    exists: int
    uid_validity: int | None
    uid_next: int | None
    highest_modseq: int | None
    readonly: bool


def _text(data) -> str:
    """A response payload as one readable line, for an error message."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    if isinstance(data, (list, tuple)):
        return " ".join(_text(d) for d in data if d is not None)
    return "" if data is None else str(data)


class Connection:
    """One authenticated connection to one account's server."""

    def __init__(self, imap: imaplib.IMAP4, *, address: str = "") -> None:
        self._imap = imap
        self.address = address
        self.selected: str | None = None
        self._capabilities = self._read_capabilities()

    # -------------------------------------------------------- construction
    @classmethod
    def connect(cls, host: str, port: int = DEFAULT_PORT, *,
                address: str = "", timeout: float = DEFAULT_TIMEOUT,
                ssl_context: ssl.SSLContext | None = None,
                factory: Callable[[], imaplib.IMAP4] | None = None) -> "Connection":
        """Open a TLS connection. `factory` is how the tests supply a server.

        TLS from the first byte rather than STARTTLS: every provider corMani
        speaks to offers 993, and a client that can be talked down to plaintext
        by a hostile network is a client that can be read by one.
        """
        if factory is not None:
            # Wrapped like the real path: a connection that fails while being
            # made must arrive as `Transient` whichever route built it, or the
            # engine's back-off never sees it.
            try:
                return cls(factory(), address=address)
            except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
                raise Transient(f"could not reach {host}: {exc}") from None
        if ssl_context is None:
            ssl_context = ssl.create_default_context()
            # Left at the defaults deliberately: certificate verification and
            # hostname checking are both on, and a mail client is exactly the
            # program that must not turn them off for convenience.
        try:
            imap = imaplib.IMAP4_SSL(host=host, port=port,
                                     ssl_context=ssl_context, timeout=timeout)
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise Transient(f"could not reach {host}:{port}: {exc}") from None
        return cls(imap, address=address)

    def _read_capabilities(self) -> frozenset[str]:
        raw = getattr(self._imap, "capabilities", ()) or ()
        return frozenset(str(c).upper() for c in raw)

    def has(self, capability: str) -> bool:
        return capability.upper() in self._capabilities

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    # ------------------------------------------------------------- calling
    def _ok(self, name: str, result, *, allow=("OK",)):
        """The one place a refusal becomes an exception of the right kind."""
        typ, data = result
        if typ in allow:
            return data
        raise classify(f"{name}: {_text(data)}", default=ProtocolError)

    def _tagged_command(self, *args, allow=("OK",)):
        """A command whose TAGGED response text is the answer.

        The second of the two places that use imaplib's private surface; see
        the module header for why there is no public route to this. Everything
        else goes through `_call`.
        """
        name = " ".join(str(a) for a in args[:2])
        try:
            result = self._imap._simple_command(*args)
        except imaplib.IMAP4.abort as exc:
            raise Transient(f"{name}: {exc}") from None
        except imaplib.IMAP4.error as exc:
            raise classify(f"{name}: {exc}", default=ProtocolError) from None
        except (OSError, ssl.SSLError) as exc:
            raise Transient(f"{name}: {exc}") from None
        return self._ok(name, result, allow=allow)

    def _call(self, name: str, *args, uid: bool = False, allow=("OK",)):
        try:
            if uid:
                result = self._imap.uid(name, *args)
            else:
                result = getattr(self._imap, name.lower())(*args)
        except imaplib.IMAP4.abort as exc:
            # imaplib's own signal for a connection that must be re-made.
            raise Transient(f"{name}: {exc}") from None
        except imaplib.IMAP4.error as exc:
            raise classify(f"{name}: {exc}", default=ProtocolError) from None
        except (OSError, ssl.SSLError) as exc:
            raise Transient(f"{name}: {exc}") from None
        return self._ok(name, result, allow=allow)

    # ---------------------------------------------------------------- auth
    def login(self, user: str, password: str) -> None:
        """An app password. Google still permits these for mail; see
        docs/accounts.txt for why calendar cannot use them."""
        try:
            self._imap.login(user, password)
        except imaplib.IMAP4.abort as exc:
            raise Transient(f"LOGIN: {exc}") from None
        except imaplib.IMAP4.error as exc:
            raise classify(f"LOGIN: {exc}", default=AuthFailed) from None
        except (OSError, ssl.SSLError) as exc:
            raise Transient(f"LOGIN: {exc}") from None
        self._after_auth()

    def authenticate_xoauth2(self, user: str, access_token: str) -> None:
        """SASL XOAUTH2, which is how both Google and Microsoft take a token.

        The callable is what imaplib hands the server's challenge to. The
        challenge is ignored — XOAUTH2 has no challenge worth reading — but the
        signature is imaplib's, so it is honoured rather than worked around.
        """
        payload = (f"user={user}\x01auth=Bearer {access_token}\x01\x01").encode("utf-8")
        try:
            self._imap.authenticate("XOAUTH2", lambda _challenge: payload)
        except imaplib.IMAP4.abort as exc:
            raise Transient(f"AUTHENTICATE: {exc}") from None
        except imaplib.IMAP4.error as exc:
            raise classify(f"AUTHENTICATE: {exc}", default=AuthFailed) from None
        except (OSError, ssl.SSLError) as exc:
            raise Transient(f"AUTHENTICATE: {exc}") from None
        self._after_auth()

    def _after_auth(self) -> None:
        """Ask again what the server can do, because the answer has changed.

        A server advertises far less before login than after — Gmail withholds
        CONDSTORE and MOVE until then. A client that keeps the greeting's list
        concludes the server cannot do things it can, and syncs the slow way
        forever.

        The command is REQUIRED, not a re-read: imaplib fills `capabilities`
        once at connect and never touches it again, so asking the object would
        return the pre-login answer no matter how many times it was asked.
        """
        try:
            data = self._call("CAPABILITY")
        except ImapError:
            return                       # keep what the greeting said
        found: set[str] = set()
        for item in data or ():
            if isinstance(item, (bytes, bytearray)):
                found |= {t.decode("ascii", "replace").upper()
                          for t in bytes(item).split()}
        if found:
            self._capabilities = frozenset(found)
        if self.has("CONDSTORE") or self.has("QRESYNC"):
            try:
                self._call("ENABLE", "CONDSTORE")
            except ImapError:
                pass                     # advertised but unusable: no matter

    def logout(self) -> None:
        try:
            self._imap.logout()
        except Exception:
            pass                         # a failed goodbye is not a failure

    # ------------------------------------------------------------- folders
    def list_mailboxes(self) -> list[parse.Mailbox]:
        data = self._call("LIST")
        boxes = []
        for line in data or ():
            box = parse.parse_list_line(line)
            if box is not None:
                boxes.append(box)
        return boxes

    def subscribed_paths(self) -> set[str]:
        """LSUB, for servers where LIST returns far more than a person wants.

        Failure is not an error: a server without LSUB is not broken, and the
        answer "everything is subscribed" is the right default.
        """
        try:
            data = self._call("LSUB")
        except ImapError:
            return set()
        out = set()
        for line in data or ():
            box = parse.parse_list_line(line)
            if box is not None:
                out.add(box.path)
        return out

    def select(self, path: str, *, readonly: bool = False) -> FolderState:
        """Open a mailbox and read back the numbers the sync turns on."""
        try:
            result = self._imap.select(_quote(path), readonly=readonly)
        except imaplib.IMAP4.abort as exc:
            raise Transient(f"SELECT: {exc}") from None
        except imaplib.IMAP4.error as exc:
            raise classify(f"SELECT {path}: {exc}", default=MailboxGone) from None
        except (OSError, ssl.SSLError) as exc:
            raise Transient(f"SELECT: {exc}") from None
        data = self._ok(f"SELECT {path}", result)

        values = parse.status_values(self._imap.untagged_responses)
        exists = values.get("EXISTS")
        if exists is None:
            exists = parse.parse_uid_list(data)
            exists = exists[0] if exists else 0
        self.selected = path
        return FolderState(path=path, exists=int(exists),
                           uid_validity=values.get("UIDVALIDITY"),
                           uid_next=values.get("UIDNEXT"),
                           highest_modseq=values.get("HIGHESTMODSEQ"),
                           readonly=readonly)

    def close_folder(self) -> None:
        """Leave the mailbox WITHOUT expunging, which CLOSE would do.

        CLOSE silently erases every message flagged \\Deleted. UNSELECT exists
        precisely to avoid that, and where it is not offered, selecting nothing
        is safer than closing.
        """
        self.selected = None
        try:
            if self.has("UNSELECT"):
                self._call("UNSELECT")
            else:
                self._imap.select(readonly=True)
        except Exception:
            pass

    # ------------------------------------------------------------ messages
    def search_uids(self, *criteria: str) -> list[int]:
        data = self._call("SEARCH", None, *(criteria or ("ALL",)), uid=True)
        return parse.parse_uid_list(data)

    def fetch(self, uids: Sequence[int] | str, items: str) -> list[parse.Fetched]:
        """A UID FETCH over a set, collapsed into ranges.

        The collapse is not cosmetic: a first sync of a busy account fetches
        flags for tens of thousands of UIDs, and one per comma builds a command
        line servers refuse outright.
        """
        spec = uids if isinstance(uids, str) else parse.uid_ranges(uids)
        if not spec:
            return []
        data = self._call("FETCH", spec, items, uid=True)
        return parse.parse_fetch(data or [])

    def fetch_flags(self, uids: Sequence[int] | str = "1:*", *,
                    changed_since: int | None = None) -> list[parse.Fetched]:
        """Flags only. With CONDSTORE, only those changed since a MODSEQ."""
        items = FLAG_ITEMS
        if changed_since is not None:
            items = f"(UID FLAGS MODSEQ) (CHANGEDSINCE {int(changed_since)})"
        return self.fetch(uids, items)

    def store_flags(self, uids: Sequence[int], *, add: Sequence[str] = (),
                    remove: Sequence[str] = ()) -> None:
        spec = parse.uid_ranges(uids)
        if not spec:
            return
        # .SILENT: the untagged FETCH echoes are a response this client does
        # not read, and on a thousand messages they are a thousand lines.
        if add:
            self._call("STORE", spec, "+FLAGS.SILENT", f"({' '.join(add)})", uid=True)
        if remove:
            self._call("STORE", spec, "-FLAGS.SILENT", f"({' '.join(remove)})", uid=True)

    def move(self, uids: Sequence[int], target: str) -> dict[int, int]:
        """Move, by the one command if the server has it and by hand if not.

        Returns the source UID to destination UID mapping the server reported,
        which is empty on a server without UIDPLUS. The caller needs it: a
        moved message with no UID in its new folder is fetched again by the
        next sync and ends up in the store twice.

        The fallback is COPY, flag \\Deleted, UID EXPUNGE — and the ORDER
        matters. Copying first means a failure between the steps leaves the
        message in BOTH folders, which a person can fix. The other order loses
        mail.
        """
        spec = parse.uid_ranges(uids)
        if not spec:
            return {}
        if self.has("MOVE"):
            return parse.parse_copyuid(
                self._tagged_command("UID", "MOVE", spec, _quote(target)))
        mapping = parse.parse_copyuid(
            self._tagged_command("UID", "COPY", spec, _quote(target)))
        self.store_flags(uids, add=["\\Deleted"])
        self.expunge_uids(uids)
        return mapping

    def copy(self, uids: Sequence[int], target: str) -> dict[int, int]:
        spec = parse.uid_ranges(uids)
        if not spec:
            return {}
        return parse.parse_copyuid(
            self._tagged_command("UID", "COPY", spec, _quote(target)))

    def append(self, path: str, raw: bytes, *, flags: Sequence[str] = ("\\Seen",),
               when=None) -> None:
        """Put a message INTO a folder on the server. What files a sent copy.

        The date is the server's business when none is given: `imaplib.Time2
        Internaldate` wants a very particular shape and getting it wrong makes
        the whole command fail, so an absent date — which means "now" — is the
        safer default and the one this client uses.
        """
        stamp = imaplib.Time2Internaldate(when) if when is not None else None
        flag_list = f"({' '.join(flags)})" if flags else None
        try:
            result, data = self._imap.append(_quote(path), flag_list, stamp, raw)
        except imaplib.IMAP4.abort as exc:
            raise Transient(f"APPEND: {exc}") from None
        except imaplib.IMAP4.error as exc:
            raise classify(f"APPEND: {exc}") from None
        except (OSError, ssl.SSLError) as exc:
            raise Transient(f"APPEND: {exc}") from None
        self._ok("APPEND", (result, data))

    def expunge_uids(self, uids: Sequence[int]) -> None:
        """Erase these, and only these.

        A bare EXPUNGE removes every message in the folder flagged \\Deleted,
        including ones another client flagged and has not yet erased. UIDPLUS
        exists to say which; without it the safe answer is to do nothing rather
        than to erase someone else's message.
        """
        spec = parse.uid_ranges(uids)
        if not spec:
            return
        if self.has("UIDPLUS"):
            self._call("EXPUNGE", spec, uid=True)

    def noop(self) -> None:
        self._call("NOOP")

    # ---------------------------------------------------------------- IDLE
    def idle(self, seconds: float = 29 * 60) -> list[str]:
        """Wait for the server to say something. Returns the untagged lines.

        THE ONE PLACE THAT TOUCHES IMAPLIB'S PRIVATE SURFACE. Python 3.13 has
        no `IMAP4.idle()`; it arrives later, and corMani runs on the Python
        Debian ships today. Confined to this method, with the sequence written
        out, so that swapping it for the standard one is a single replacement.
        The sequence is: send IDLE, wait for the `+` continuation, read
        untagged lines until the timeout, send DONE, read the tagged
        completion. Every one of those steps is required — a client that
        forgets DONE leaves the server holding a connection that will never
        answer another command.
        """
        if not self.has("IDLE"):
            raise Transient("the server does not offer IDLE")
        imap = self._imap
        try:
            tag = imap._command("IDLE")
            response = imap._get_response()
            if response is not None:
                # Not a continuation: the server refused, and the tagged reply
                # is already in hand.
                typ, data = imap._get_tagged_response(tag)
                raise classify(f"IDLE: {_text(data)}", default=ProtocolError)

            lines: list[str] = []
            deadline = time.monotonic() + max(1.0, seconds)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._set_read_timeout(remaining)
                try:
                    line = imap._get_line()
                except (socket.timeout, TimeoutError):
                    break
                except imaplib.IMAP4.abort as exc:
                    raise Transient(f"IDLE: {exc}") from None
                text = _text(line).strip()
                if text:
                    lines.append(text)
                # An untagged BYE means the server is closing; there is nothing
                # to wait for and DONE will never be answered.
                if text.upper().startswith("* BYE"):
                    raise Transient("IDLE: the server closed the connection")
            return lines
        finally:
            self._set_read_timeout(None)
            self._done(locals().get("tag"))

    def _done(self, tag) -> None:
        """End the IDLE. Failure here is reported by the next command failing."""
        if tag is None:
            return
        imap = self._imap
        try:
            imap.send(b"DONE\r\n")
            imap._command_complete("IDLE", tag)
        except Exception:
            pass

    def _set_read_timeout(self, seconds: float | None) -> None:
        """A read deadline, on whatever this connection's socket really is.

        The fake server in the test suite provides the same small surface, so
        that the IDLE loop under test is the one that runs in the field rather
        than a variant written for testability.
        """
        sock = getattr(self._imap, "sock", None)
        if sock is None:
            return
        try:
            sock.settimeout(seconds)
        except Exception:
            pass


def _quote(path: str) -> str:
    """A mailbox name as an argument.

    imaplib quotes an argument containing a space, but not one containing a
    bracket — and `[Gmail]/All Mail` has both. Quoted unconditionally, which is
    always legal, rather than reasoning about which characters need it.
    """
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
