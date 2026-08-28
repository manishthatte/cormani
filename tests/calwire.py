# SPDX-License-Identifier: GPL-3.0-or-later
#
# The HTTP that is not HTTP: an opener the calendar client cannot tell from
# `urllib.request.urlopen`.
#
# `tests/faketransport.py` does this for IMAP by replacing the socket under a
# real `imaplib`. The same trick does not apply here, because there is no
# stateful protocol to speak — what `calendar/http.py` produces is one
# `urllib.request.Request` per call, and the honest seam is therefore the
# opener it hands that Request to. Everything above the seam is the real code:
# the URL, the query string, the method, the headers and the JSON body are all
# built by the client under test, and the servers in `fakecal.py` read them the
# way the real ones would.
#
# WHAT THIS DOES NOT TEST, SAID PLAINLY: TLS, certificate verification,
# redirects, and urllib's own handling of a chunked response. Those are the
# standard library's and a double that reimplemented them would be testing
# itself. What it does test is every decision corMani makes about what to send
# and what to believe.
#
# A FAILURE IS RAISED AS `urllib.error.HTTPError`, WITH A BODY, because that is
# what the client catches and what carries the provider's `reason` — and the
# reason is what `calendar/errors.py` classifies a 403 by. A double that
# returned a status code without a body would make every 403 permanent and
# every test of that agree with itself.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import json
import urllib.error
import urllib.parse
from email.message import Message


class Reply:
    """Enough of an `http.client.HTTPResponse` for `calendar/http.py`."""

    def __init__(self, status: int = 200, payload=None, headers: dict | None = None):
        self.status = status
        self._body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)

    def read(self) -> bytes:
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fail(status: int, payload, headers: dict | None = None) -> Exception:
    """A refusal, in the shape urllib hands to a caller."""
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = str(value)
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        "https://example.invalid/", status, str(status), message,
        _Body(body))


class _Body:
    """`HTTPError.read()` reads from the file object it was given."""

    def __init__(self, data: bytes):
        self.data = data

    def read(self, *args) -> bytes:
        data, self.data = self.data, b""
        return data

    def close(self):
        pass


class Call:
    """One request, parsed the way a server would parse it."""

    def __init__(self, request):
        self.method = request.get_method()
        self.url = request.full_url
        parts = urllib.parse.urlsplit(self.url)
        self.path = parts.path
        self.query = {k: v[-1] for k, v in
                      urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
        self.query_all = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        # urllib lowercases header names it is given through `headers=`; both
        # spellings are accepted here because a server would not care.
        self.headers = {k.lower(): v for k, v in request.header_items()}
        self.body = {}
        if request.data:
            try:
                self.body = json.loads(request.data.decode("utf-8"))
            except ValueError:                               # pragma: no cover
                self.body = {}

    @property
    def token(self) -> str:
        value = self.headers.get("authorization", "")
        return value[7:] if value.lower().startswith("bearer ") else ""

    @property
    def if_match(self) -> str:
        return self.headers.get("if-match", "")

    def prefers(self, needle: str) -> bool:
        return needle.lower() in self.headers.get("prefer", "").lower()

    def segments(self) -> list:
        return [urllib.parse.unquote(s) for s in self.path.strip("/").split("/") if s]

    def __repr__(self) -> str:                               # pragma: no cover
        return f"Call({self.method} {self.path})"


class Transport:
    """An opener that records what it was asked and answers from a server."""

    def __init__(self, server):
        self.server = server
        self.calls: list = []

    def __call__(self, request, timeout=None):
        call = Call(request)
        self.calls.append(call)
        return self.server.handle(call)

    def paths(self) -> list:
        return [f"{c.method} {c.path}" for c in self.calls]

    def last(self) -> Call:
        return self.calls[-1]
