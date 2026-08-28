# SPDX-License-Identifier: GPL-3.0-or-later
#
# A submission server, in this process.
#
# The same bargain as tests/fakeimap.py: a REAL `smtplib.SMTP` talks to this,
# and only the socket is faked. Everything above the socket — EHLO, the
# extension list, AUTH's base64 dance, dot-stuffing in DATA, the reply-line
# parser — is smtplib's own, which is what makes a test here worth anything.
#
# WHAT IS NOT EXERCISED, said rather than hidden: the TLS handshake. Wrapping a
# socket that is not a socket needs a file descriptor, so `SMTP_Fake.starttls`
# performs the command, checks the reply and resets the connection state exactly
# as smtplib does — and then does not wrap. The client's decision to REFUSE a
# server that does not offer STARTTLS is fully tested; the encryption itself is
# the platform's, not this codebase's.
#
# The server is deliberately strict about order: no MAIL before AUTH, no RCPT
# before MAIL, no DATA before RCPT. A client that gets that wrong against a real
# server gets a 503, and a fake that shrugs would let it through.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import base64
import smtplib

HOSTNAME = "smtp.fake.invalid"


class Server:
    """One submission server, and everything it has been handed."""

    def __init__(self, *, hostname: str = HOSTNAME, offer_starttls: bool = True,
                 require_auth: bool = True, password: str = "correct-horse",
                 token: str = "ya29.fake-token", refuse: tuple = (),
                 data_reply: str = "250 2.0.0 Ok: queued as 7A3C1") -> None:
        self.hostname = hostname
        self.offer_starttls = offer_starttls
        self.require_auth = require_auth
        self.password = password
        self.token = token
        self.refuse = {r.lower() for r in refuse}
        self.data_reply = data_reply

        self.tls = False
        self.authenticated = False
        self.user = ""
        self.delivered: list = []
        self.log: list = []
        self._buffer = bytearray()
        self._in_data = False
        self._pending_auth = ""
        self._mail_from = ""
        self._rcpts: list = []
        self._data = bytearray()

    # ------------------------------------------------------------ the wire
    def greeting(self) -> bytes:
        return f"220 {self.hostname} ESMTP corMani-fake\r\n".encode("ascii")

    def feed(self, data: bytes) -> bytes:
        """Bytes in, reply bytes out. Whole lines only, as a socket delivers."""
        self._buffer += data
        out = bytearray()
        while b"\r\n" in self._buffer:
            line, _, rest = bytes(self._buffer).partition(b"\r\n")
            self._buffer = bytearray(rest)
            out += self._line(line)
        return bytes(out)

    def _line(self, raw: bytes) -> bytes:
        if self._in_data:
            return self._data_line(raw)
        text = raw.decode("utf-8", "replace")
        self.log.append(text)
        verb, _, rest = text.partition(" ")
        handler = getattr(self, f"_cmd_{verb.upper().replace('-', '_')}", None)
        if handler is None:
            return _reply("500 5.5.2 Command unrecognised")
        return handler(rest.strip())

    # ------------------------------------------------------------- commands
    def _cmd_EHLO(self, argument: str) -> bytes:
        lines = [f"{self.hostname} at your service"]
        if self.offer_starttls and not self.tls:
            lines.append("STARTTLS")
        if self.tls or not self.offer_starttls:
            lines.append("AUTH LOGIN PLAIN XOAUTH2")
        lines.append("SIZE 35882577")
        lines.append("8BITMIME")
        body = "".join(f"250-{line}\r\n" for line in lines[:-1])
        return (body + f"250 {lines[-1]}\r\n").encode("ascii")

    def _cmd_HELO(self, argument: str) -> bytes:
        return _reply(f"250 {self.hostname}")

    def _cmd_STARTTLS(self, argument: str) -> bytes:
        if not self.offer_starttls:
            return _reply("454 4.7.0 TLS not available")
        self.tls = True
        return _reply("220 2.0.0 Ready to start TLS")

    def _cmd_AUTH(self, argument: str) -> bytes:
        mechanism, _, blob = argument.partition(" ")
        mechanism = mechanism.upper()
        if mechanism == "LOGIN":
            self._pending_auth = "login-user"
            return _reply("334 " + _b64("Username:"))
        if mechanism == "PLAIN":
            if not blob:
                self._pending_auth = "plain"
                return _reply("334 ")
            return self._plain(blob)
        if mechanism == "XOAUTH2":
            return self._xoauth2(blob)
        return _reply("504 5.5.4 Unrecognised authentication type")

    def _continue_auth(self, raw: bytes) -> bytes:
        stage, self._pending_auth = self._pending_auth, ""
        if stage == "login-user":
            self.user = _unb64(raw)
            self._pending_auth = "login-pass"
            return _reply("334 " + _b64("Password:"))
        if stage == "login-pass":
            return self._accept(_unb64(raw) == self.password)
        if stage == "plain":
            return self._plain(raw.decode("ascii", "replace"))
        return _reply("503 5.5.1 Unexpected")

    def _plain(self, blob: str) -> bytes:
        try:
            _authzid, user, password = _unb64(blob.encode()).split("\x00")
        except ValueError:
            return _reply("535 5.7.8 Malformed")
        self.user = user
        return self._accept(password == self.password)

    def _xoauth2(self, blob: str) -> bytes:
        decoded = _unb64(blob.encode())
        parts = dict(piece.split("=", 1) for piece in decoded.split("\x01") if "=" in piece)
        self.user = parts.get("user", "")
        return self._accept(parts.get("auth", "") == f"Bearer {self.token}")

    def _accept(self, ok: bool) -> bytes:
        self.authenticated = bool(ok)
        return _reply("235 2.7.0 Accepted" if ok
                      else "535 5.7.8 Username and Password not accepted")

    def _cmd_MAIL(self, argument: str) -> bytes:
        if self.require_auth and not self.authenticated:
            return _reply("530 5.7.0 Authentication Required")
        self._mail_from = _angle(argument.partition(":")[2])
        self._rcpts = []
        return _reply("250 2.1.0 OK")

    def _cmd_RCPT(self, argument: str) -> bytes:
        if not self._mail_from:
            return _reply("503 5.5.1 MAIL first")
        address = _angle(argument.partition(":")[2])
        if address.lower() in self.refuse:
            return _reply(f"550 5.1.1 <{address}>: no such user")
        self._rcpts.append(address)
        return _reply("250 2.1.5 OK")

    def _cmd_DATA(self, argument: str) -> bytes:
        if not self._rcpts:
            return _reply("503 5.5.1 RCPT first")
        self._in_data = True
        self._data = bytearray()
        return _reply("354 Go ahead")

    def _data_line(self, raw: bytes) -> bytes:
        if raw == b".":
            self._in_data = False
            self.delivered.append(
                (self._mail_from, tuple(self._rcpts), bytes(self._data)))
            self._mail_from, self._rcpts = "", []
            return _reply(self.data_reply)
        # Dot-stuffing, undone: smtplib doubles a leading dot and the server is
        # what puts it back. A test that skipped this would pass a message with
        # a corrupted body.
        self._data += (raw[1:] if raw.startswith(b"..") else raw) + b"\r\n"
        return b""

    def _cmd_RSET(self, argument: str) -> bytes:
        self._mail_from, self._rcpts = "", []
        return _reply("250 2.0.0 OK")

    def _cmd_NOOP(self, argument: str) -> bytes:
        return _reply("250 2.0.0 OK")

    def _cmd_QUIT(self, argument: str) -> bytes:
        return _reply(f"221 2.0.0 {self.hostname} closing connection")


def _reply(text: str) -> bytes:
    return (text + "\r\n").encode("ascii")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _unb64(raw) -> str:
    if isinstance(raw, str):
        raw = raw.encode("ascii")
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return ""


def _angle(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and ">" in value:
        return value[1:value.index(">")]
    return value.split(" ")[0]


# --------------------------------------------------------- the fake socket
class _FakeFile:
    """What smtplib reads replies from. A line at a time, from the buffer."""

    def __init__(self, sock) -> None:
        self._sock = sock

    def readline(self, limit: int = -1) -> bytes:
        buffer = self._sock.buffer
        end = buffer.find(b"\n")
        if end < 0:
            return b""                   # a closed connection, to smtplib
        line = bytes(buffer[:end + 1])
        del buffer[:end + 1]
        return line

    def close(self) -> None:
        pass


class _FakeSocket:
    def __init__(self, server: Server) -> None:
        self.server = server
        self.buffer = bytearray(server.greeting())
        self.closed = False

    def sendall(self, data) -> None:
        if self._pending_auth_continuation():
            self.buffer += self.server._continue_auth(bytes(data).strip())
            return
        self.buffer += self.server.feed(bytes(data))

    def _pending_auth_continuation(self) -> bool:
        return bool(self.server._pending_auth)

    def makefile(self, mode: str = "rb", *args, **kw) -> _FakeFile:
        return _FakeFile(self)

    def settimeout(self, seconds) -> None:
        pass

    def gettimeout(self):
        return None

    def close(self) -> None:
        self.closed = True

    def shutdown(self, how) -> None:      # pragma: no cover
        self.closed = True


class SMTP_Fake(smtplib.SMTP):
    """A real smtplib client whose socket is this process."""

    def __init__(self, server: Server, *, host: str = HOSTNAME,
                 port: int = 587) -> None:
        self._server = server
        super().__init__(host=host, port=port)

    def _get_socket(self, host, port, timeout):
        return _FakeSocket(self._server)

    def starttls(self, *, context=None, **kw):
        """The command, the reply and the state reset — but no wrapping.

        Everything smtplib's own `starttls` does apart from the handshake, which
        needs a file descriptor. See the note at the top of this file.
        """
        self.ehlo_or_helo_if_needed()
        code, reply = self.docmd("STARTTLS")
        if code == 220:
            self.file = None
            self.helo_resp = None
            self.ehlo_resp = None
            self.esmtp_features = {}
            self.does_esmtp = False
        else:
            raise smtplib.SMTPResponseException(code, reply)
        return code, reply


def factory_for(server: Server):
    """A `Sender.connect` factory bound to this server."""
    def factory(host, port, timeout):
        return SMTP_Fake(server, host=host, port=port)
    return factory
