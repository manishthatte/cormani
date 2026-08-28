# SPDX-License-Identifier: GPL-3.0-or-later
#
# The socket that is not a socket.
#
# `imaplib.IMAP4` is designed to be subclassed at the transport — `IMAP4_SSL`
# and `IMAP4_stream` both do it — so replacing `open`, `read`, `readline`,
# `send` and `shutdown` is enough to put a server in this process. Everything
# above those five methods is imaplib's own: command construction, literals,
# continuation lines, response parsing. That is the whole reason the suite
# talks to a real server rather than a mock, and it is why this file is short.
#
# Kept apart from fakeimap.py because the two fail differently. A mistake here
# is bytes not arriving; a mistake there is a server answering the wrong
# question.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import imaplib


class Broken(Exception):
    """A connection the server decided to drop.

    Defined here rather than beside the server because it is a fact about the
    transport: the server raises it, and only this layer knows what to turn it
    into — `imaplib.IMAP4.abort`, which is imaplib's own signal for a
    connection that must be made again.
    """


class _FakeSocket:
    """Just enough socket for a read deadline.

    The IDLE loop sets one and treats the resulting timeout as "nothing more is
    coming". Modelling that here means the loop under test is the one that runs
    in the field, rather than a variant written to be testable.
    """

    def __init__(self):
        self.timeout = None

    def settimeout(self, seconds):
        self.timeout = seconds


class IMAP4_Fake(imaplib.IMAP4):
    """A real imaplib client whose socket is this process.

    Only `open`, `read`, `readline`, `send` and `shutdown` are replaced.
    Everything above them — command construction, literals, continuation lines,
    response parsing — is imaplib's own, which is the point.
    """

    def __init__(self, server: Server):
        self._server = server
        self._out = bytearray()
        super().__init__(host="fakeimap.invalid", port=993)

    def open(self, host="", port=993, timeout=None):
        self.host, self.port = host, port
        self.sock = _FakeSocket()
        self.file = None
        self._server.begin_session()
        self._out = bytearray(self._server.greeting())

    def read(self, size):
        data = bytes(self._out[:size])
        del self._out[:size]
        if len(data) < size:                                 # pragma: no cover
            raise self.abort("fake server: short read")
        return data

    def readline(self):
        if b"\n" not in self._out:
            # Anything the server is holding — an EXISTS pushed during IDLE —
            # is delivered here, which is where a real socket would produce it.
            self._out += self._server.pending()
        end = self._out.find(b"\n")
        if end < 0:
            if self.sock.timeout is not None:
                # A read deadline is set and nothing arrived: a timeout, which
                # is what ends an IDLE wait. Not a dropped connection.
                raise TimeoutError("fake server: no data within the timeout")
            # No data, none coming, and no deadline. The same condition as a
            # dropped connection, and imaplib's own signal for one.
            raise self.abort("fake server: connection closed")
        line = bytes(self._out[:end + 1])
        del self._out[:end + 1]
        return line

    def send(self, data):
        try:
            self._out += self._server.feed(bytes(data))
        except Broken as exc:
            raise self.abort(str(exc)) from None

    def shutdown(self):
        self._server.closed = True
