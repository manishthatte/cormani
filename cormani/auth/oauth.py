# SPDX-License-Identifier: GPL-3.0-or-later
#
# The OAuth2 authorisation-code flow, with PKCE, over a loopback redirect.
#
# Standard library only — `urllib.request` and `http.server` — because
# CONVENTIONS.txt §3 forbids vendoring and this is exactly the kind of thing a
# library would be added for. It is about two hundred lines.
#
# WHY LOOPBACK AND NOT A DEVICE CODE. A desktop application cannot keep a
# secret, which is why PKCE exists, and both Google and Microsoft accept
# `http://127.0.0.1:<any port>` as a redirect for a desktop client without the
# port being registered. The device-code flow is the alternative and is worse
# here: it makes the user type a code into a phone to authorise the machine
# they are already sitting at.
#
# THE SERVER BINDS 127.0.0.1, NEVER 0.0.0.0. CONVENTIONS.txt §7, and here it is
# load-bearing rather than hygiene: the thing arriving on that socket is an
# authorisation code, and a socket on 0.0.0.0 accepts it from the network.
#
# PKCE IS NOT OPTIONAL EVEN THOUGH A CLIENT SECRET IS SENT. Google's desktop
# clients are issued a "secret" that ships in every copy of the application and
# is therefore not one; PKCE is what actually binds the code to this request.
# `state` is checked as well, against a different attack — a code injected by
# another page — and both are compared with `compare_digest`.
#
# NOTHING HERE LOGS, PRINTS, OR PUTS A TOKEN IN AN EXCEPTION. The error paths
# carry the provider's `error` field and nothing else, because an exception is
# the most likely thing to end up in a bug report.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets as secrets_module
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from .providers import Provider

# How long before an access token actually expires corMani treats it as
# expired. A token that dies between the check and the command produces an
# authentication failure that looks like a rejected credential, and the whole
# point of the taxonomy is that those two are different.
EXPIRY_MARGIN = dt.timedelta(seconds=120)

_LOOPBACK = "127.0.0.1"


class OAuthError(RuntimeError):
    """The provider refused. Carries its `error` field and nothing else."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str = ""
    expires_at: str = ""            # ISO-8601 UTC, as everywhere else
    scope: str = ""

    def expired(self, *, now: dt.datetime | None = None) -> bool:
        """True when the token should be refreshed before being used.

        No expiry recorded counts as expired: a token of unknown age is one
        that might already be dead, and a needless refresh costs one request
        while a dead token costs a failed sync.
        """
        if not self.expires_at:
            return True
        try:
            deadline = dt.datetime.fromisoformat(self.expires_at)
        except ValueError:                                   # pragma: no cover
            return True
        now = now or dt.datetime.now(dt.timezone.utc)
        if deadline.tzinfo is None:                          # pragma: no cover
            deadline = deadline.replace(tzinfo=dt.timezone.utc)
        return deadline - EXPIRY_MARGIN <= now

    def to_json(self) -> str:
        return json.dumps({"access_token": self.access_token,
                           "refresh_token": self.refresh_token,
                           "expires_at": self.expires_at, "scope": self.scope})

    @classmethod
    def from_json(cls, text: str) -> "TokenSet":
        try:
            data = json.loads(text)
        except ValueError:
            return cls(access_token="")
        if not isinstance(data, dict):                       # pragma: no cover
            return cls(access_token="")
        return cls(access_token=str(data.get("access_token", "")),
                   refresh_token=str(data.get("refresh_token", "")),
                   expires_at=str(data.get("expires_at", "")),
                   scope=str(data.get("scope", "")))

    def __repr__(self) -> str:
        """Never the token itself. A repr ends up in tracebacks and logs."""
        return (f"TokenSet(access_token=<{len(self.access_token)} chars>, "
                f"refresh_token={'yes' if self.refresh_token else 'no'}, "
                f"expires_at={self.expires_at!r})")


# ------------------------------------------------------------------- PKCE
def make_verifier() -> str:
    """RFC 7636's code verifier: 43-128 characters of unreserved ASCII."""
    return secrets_module.token_urlsafe(64)[:96]


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorization_url(provider: Provider, client_id: str, redirect_uri: str, *,
                      state: str, verifier: str, login_hint: str = "",
                      calendar: bool = False) -> str:
    """Where to send the browser."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes(calendar=calendar)),
        "state": state,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
    }
    if provider.name == "google":
        # Without both of these Google returns no refresh token on the second
        # and subsequent authorisations of the same account, and the failure is
        # silent until the first access token expires an hour later.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    if login_hint:
        # Fifteen accounts, eight of them Google: without this the browser
        # offers whichever is already signed in, and the wrong one is one click
        # away every time.
        params["login_hint"] = login_hint
    return provider.authorize_url + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------- exchange
def _post(url: str, fields: dict, *, timeout: float = 30.0) -> dict:
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("error") or payload.get("error_description") or "")
        except Exception:
            detail = ""
        # The request body held the refresh token and the client secret. Only
        # the provider's own error word is repeated back.
        raise OAuthError(f"the provider refused the token request"
                         f"{': ' + detail if detail else ''}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OAuthError(f"could not reach the provider: "
                         f"{exc.__class__.__name__}") from None
    except ValueError:
        raise OAuthError("the provider's reply was not JSON") from None


def _token_set(payload: dict, *, previous_refresh: str = "",
               now: dt.datetime | None = None) -> TokenSet:
    access = str(payload.get("access_token", ""))
    if not access:
        raise OAuthError(str(payload.get("error", "no access token was returned")))
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        lifetime = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):                          # pragma: no cover
        lifetime = 3600
    expires_at = (now + dt.timedelta(seconds=lifetime)).replace(
        microsecond=0).isoformat()
    # A refresh response usually omits the refresh token, meaning "keep the one
    # you have". Dropping it there is how an account silently needs signing in
    # again an hour later.
    refresh = str(payload.get("refresh_token", "") or previous_refresh)
    return TokenSet(access_token=access, refresh_token=refresh,
                    expires_at=expires_at, scope=str(payload.get("scope", "")))


def exchange_code(provider: Provider, *, client_id: str, client_secret: str,
                  code: str, redirect_uri: str, verifier: str,
                  post: Callable = _post,
                  now: dt.datetime | None = None) -> TokenSet:
    fields = {"client_id": client_id, "code": code,
              "redirect_uri": redirect_uri, "grant_type": "authorization_code",
              "code_verifier": verifier}
    if client_secret:
        fields["client_secret"] = client_secret
    return _token_set(post(provider.token_url, fields), now=now)


def refresh_token(provider: Provider, *, client_id: str, client_secret: str,
                  refresh: str, post: Callable = _post,
                  now: dt.datetime | None = None) -> TokenSet:
    if not refresh:
        raise OAuthError("no refresh token is stored; the account must sign in")
    fields = {"client_id": client_id, "refresh_token": refresh,
              "grant_type": "refresh_token"}
    if client_secret:
        fields["client_secret"] = client_secret
    return _token_set(post(provider.token_url, fields),
                      previous_refresh=refresh, now=now)


# ------------------------------------------------------- the loopback catch
_PAGE = ("<!doctype html><meta charset=utf-8><title>corMani</title>"
         "<body style='font:16px/1.5 system-ui;margin:4rem auto;max-width:32rem'>"
         "<h1>{heading}</h1><p>{detail}</p>"
         "<p>You can close this tab and return to corMani.</p>")


class _Catcher(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):                                        # noqa: N802 (stdlib)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).result = {k: v[0] for k, v in query.items()}
        ok = "code" in type(self).result
        page = _PAGE.format(
            heading="Signed in" if ok else "Sign-in failed",
            detail=("corMani has the authorisation it needs." if ok else
                    "The provider did not return an authorisation code."))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Silence. The request line contains the authorisation code, and the
        default handler prints it to stderr."""


def listen(port: int = 0) -> HTTPServer:
    """A one-request server on the loopback interface. Never 0.0.0.0."""
    _Catcher.result = {}
    return HTTPServer((_LOOPBACK, port), _Catcher)


def redirect_uri_for(server: HTTPServer) -> str:
    return f"http://{_LOOPBACK}:{server.server_port}/"


def wait_for_code(server: HTTPServer, *, state: str,
                  timeout: float = 300.0) -> str:
    """Serve one request and return the code, having checked `state`.

    `compare_digest`, not `==`: the comparison is against a value an attacker
    chooses, and this is the check that stops a code from another page being
    accepted as the answer to this request.
    """
    server.timeout = timeout
    try:
        server.handle_request()
    finally:
        server.server_close()
    result = _Catcher.result or {}
    if not result:
        raise OAuthError("no reply arrived from the browser before the timeout")
    if result.get("error"):
        raise OAuthError(f"the provider refused: {result['error']}")
    if not hmac.compare_digest(str(result.get("state", "")), state):
        raise OAuthError("the reply did not match this request and was discarded")
    code = result.get("code", "")
    if not code:
        raise OAuthError("no authorisation code was returned")
    return code
