# SPDX-License-Identifier: GPL-3.0-or-later
#
# JSON over HTTPS, with a bearer token, and nothing else.
#
# `urllib.request` and `json`, because CONVENTIONS.txt §3 forbids vendoring and
# this is the shape of thing a dependency gets added for. It is a hundred and
# fifty lines, and PLAN.txt §4a counted on that when it committed to Debian.
#
# NOTHING HERE RETRIES, AND THAT IS DELIBERATE. The obvious design is a loop
# around a 429 with a sleep in it, and it is wrong twice over: it blocks the
# thread the sync is on for as long as a provider says to, and it hides the
# very thing the back-off in the database exists to count. A refusal is
# classified and raised; `engine.py` decides what waiting means, and it decides
# it with a record that survives a restart.
#
# THE TOKEN IS FETCHED PER REQUEST, THROUGH A CALLABLE. A calendar sync of
# fifteen accounts can outlast an access token's hour, and a client holding the
# string it was built with would fail halfway through with something that looks
# like a rejected credential. The callable is `auth/credentials.resolve`
# bound to one account, which refreshes when it must.
#
# THE TOKEN NEVER REACHES A REPR, A LOG OR AN EXCEPTION. The Authorization
# header is set at the moment of the call and the `Request` is not kept.
# `errors.describe` is the second line of that defence.
#
# THE OPENER IS INJECTED, AND THAT IS THE WHOLE TEST SEAM. `tests/fakecal.py`
# passes one that answers from an in-process implementation of the two APIs, so
# every calendar test exercises the real request-building — the URL, the query
# string, the method, the headers and the body — against a server that
# validates them. It is `tests/faketransport.py`'s trick in the other protocol.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .. import APP_NAME, __version__
from . import errors

USER_AGENT = f"{APP_NAME}/{__version__}"

# Long enough for a calendar list on a slow connection, short enough that a
# sync of fifteen accounts cannot hang the queue behind one of them.
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Response:
    status: int
    data: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)

    @property
    def etag(self) -> str:
        return self.headers.get("etag", "")


def _retry_after(headers: dict) -> float | None:
    """The provider's own answer to "how long", in seconds.

    Only the numeric form is read. The HTTP-date form is legal and neither
    provider sends it, and a date parsed wrongly would produce a wait of
    decades or of nothing — the engine's own ladder is a better answer than a
    guess.
    """
    value = headers.get("retry-after", "")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _headers_of(raw) -> dict:
    """Header names lowercased, because HTTP's are case-insensitive and the
    two providers do not agree on the case of ETag."""
    try:
        items = raw.items()
    except AttributeError:                                   # pragma: no cover
        return {}
    return {str(k).lower(): str(v) for k, v in items}


def _decode(body: bytes) -> dict:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {"items": payload}


class Http:
    """One account's authenticated conversation with one API."""

    def __init__(self, token: Callable[[], str] | str, *,
                 opener: Callable | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._token = token if callable(token) else (lambda: token)
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    def __repr__(self) -> str:                               # never the token
        return f"Http(timeout={self.timeout})"

    # ------------------------------------------------------------- verbs
    def get(self, url: str, params: dict | None = None, *,
            headers: dict | None = None) -> Response:
        return self.request("GET", url, params=params, headers=headers)

    def post(self, url: str, body: dict | None = None, *,
             params: dict | None = None, headers: dict | None = None) -> Response:
        return self.request("POST", url, params=params, body=body,
                            headers=headers)

    def patch(self, url: str, body: dict, *, params: dict | None = None,
              if_match: str = "") -> Response:
        return self.request("PATCH", url, params=params, body=body,
                            if_match=if_match)

    def put(self, url: str, body: dict, *, params: dict | None = None,
            if_match: str = "") -> Response:
        return self.request("PUT", url, params=params, body=body,
                            if_match=if_match)

    def delete(self, url: str, *, params: dict | None = None,
               if_match: str = "") -> Response:
        return self.request("DELETE", url, params=params, if_match=if_match)

    # ----------------------------------------------------------- the call
    def request(self, method: str, url: str, *, params: dict | None = None,
                body: dict | None = None, if_match: str = "",
                headers: dict | None = None) -> Response:
        if params:
            # `doseq`, because both APIs take repeated parameters — Graph's
            # $select and Google's more than one eventTypes — and a list
            # url-encoded without it becomes the repr of a Python list.
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        extra = dict(headers or {})
        headers = {"Authorization": f"Bearer {self._token()}",
                   "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        # Graph asks for `Prefer`, and it is a request header with meaning
        # rather than decoration: without odata.track-changes there is no delta
        # link, and without outlook.timezone every time comes back in the
        # mailbox's own zone rather than in UTC.
        headers.update(extra)
        if data is not None:
            headers["Content-Type"] = "application/json"
        if if_match:
            # The whole reason an etag is carried through the queue: the write
            # is refused rather than overwriting a change made elsewhere.
            headers["If-Match"] = if_match
        request = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
        try:
            with self._open(request, timeout=self.timeout) as response:
                return Response(status=getattr(response, "status", 200),
                                data=_decode(response.read()),
                                headers=_headers_of(response.headers))
        except urllib.error.HTTPError as exc:
            headers = _headers_of(exc.headers)
            try:
                payload = _decode(exc.read())
            except Exception:                                # pragma: no cover
                payload = {}
            raise errors.classify(exc.code, payload,
                                  retry_after=_retry_after(headers),
                                  text=exc.reason or "") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # The class name and not the message: a URLError's message can
            # contain the URL, and the URL of a calendar request contains the
            # account's own address.
            raise errors.Transient(
                f"could not reach the provider: {exc.__class__.__name__}") from None


def bearer_for(address: str, provider_name: str, *,
               resolve: Callable | None = None) -> Callable[[], str]:
    """A token callable for one account, refreshed when it has to be.

    Raises rather than returning an empty string when the account has only an
    app password: Google issues those for mail and refuses them for Calendar,
    and an empty bearer token would arrive as a 401 that reads like a revoked
    sign-in. docs/accounts.txt records that this is why the OAuth registration
    is required and not optional.
    """
    from ..auth import credentials as auth
    from ..auth.providers import METHOD_OAUTH2

    resolve = resolve or auth.resolve

    def token() -> str:
        credential = resolve(address, provider_name)
        if credential.method != METHOD_OAUTH2:
            raise errors.AuthFailed(
                f"{address} is authenticated with an app password, which no "
                f"provider accepts for Calendar. The account must be "
                f"authorised with OAuth before its calendar can be read.")
        return credential.secret

    return token
