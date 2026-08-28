# SPDX-License-Identifier: GPL-3.0-or-later
#
# Getting the credential an account needs, right now.
#
# The engine asks for one thing — "how do I authenticate this account?" — and
# gets back a mechanism and a value. Everything between, including refreshing
# an access token that expired while the laptop was shut, happens here.
#
# WHAT IS KEPT, AND WHY EACH. Four entries per account at most, all in the
# system keyring and nowhere else:
#
#   app-password   The alternative to OAuth, where the provider still allows
#                  one. Google does for mail and does not for calendar.
#   refresh-token  The durable credential. Losing it means signing in again.
#   access-token   Short-lived, and stored rather than kept in memory because
#                  fifteen accounts refreshing at every launch is fifteen
#                  requests to a rate-limited endpoint for no gain. It is
#                  stored WITH its expiry, as one JSON blob, so the two cannot
#                  drift apart.
#
# and per PROVIDER, not per account, because one Google Cloud project and one
# Azure registration cover every account (docs/accounts.txt):
#
#   client-id / client-secret   The installation's own registration.
#
# A REFRESH IS ATTEMPTED EXACTLY ONCE, AND THEN IT IS THE USER'S PROBLEM. This
# is where `errors.AuthFailed` being PERMANENT is earned: by the time the
# engine sees a refusal, the one thing that might have fixed it has already
# been tried. Retrying beyond that is how fifteen accounts get an address
# blocked.
#
# NOTHING HERE RETURNS A SECRET IN A REPR, A LOG OR AN EXCEPTION. `Credential`
# has a `__repr__` that says how long the value is and not what it is.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import secrets as secrets_module
from dataclasses import dataclass
from typing import Callable

from ..secrets import store as secrets
from ..secrets.store import SecretMissing, SecretUnavailable
from . import oauth, providers
from .providers import METHOD_OAUTH2, METHOD_PASSWORD, Provider

PURPOSE_PASSWORD = "app-password"
PURPOSE_REFRESH = "refresh-token"
PURPOSE_ACCESS = "access-token"
PURPOSE_CLIENT_ID = "client-id"
PURPOSE_CLIENT_SECRET = "client-secret"

# Provider registrations are keyed under this rather than an address, so that
# they cannot collide with an account of the same name.
_PROVIDER_PREFIX = "provider:"


class NotConfigured(RuntimeError):
    """Nothing is stored for this account. The user must sign in, or say which
    provider registration to use. Distinct from a REFUSED credential, because
    the two need different words in the interface."""


@dataclass(frozen=True)
class Credential:
    method: str                     # oauth2 | password
    user: str
    secret: str

    def __repr__(self) -> str:
        return (f"Credential(method={self.method!r}, user={self.user!r}, "
                f"secret=<{len(self.secret)} chars>)")


# ------------------------------------------------------- storing and asking
def provider_key(name: str) -> str:
    return f"{_PROVIDER_PREFIX}{(name or '').strip().lower()}"


def set_registration(provider_name: str, client_id: str,
                     client_secret: str = "") -> None:
    """Record the installation's own OAuth registration.

    One per provider covers every account on it, which is why this is keyed by
    provider and not by address.
    """
    secrets.set_secret(provider_key(provider_name), PURPOSE_CLIENT_ID, client_id)
    if client_secret:
        secrets.set_secret(provider_key(provider_name), PURPOSE_CLIENT_SECRET,
                           client_secret)


def registration(provider_name: str) -> tuple:
    key = provider_key(provider_name)
    try:
        client_id = secrets.get_secret(key, PURPOSE_CLIENT_ID)
    except (SecretMissing, SecretUnavailable):
        return "", ""
    try:
        client_secret = secrets.get_secret(key, PURPOSE_CLIENT_SECRET)
    except (SecretMissing, SecretUnavailable):
        client_secret = ""
    return client_id, client_secret


def has_registration(provider_name: str) -> bool:
    return bool(registration(provider_name)[0])


def set_password(address: str, password: str) -> None:
    secrets.set_secret(address, PURPOSE_PASSWORD, password)


def set_tokens(address: str, tokens: oauth.TokenSet) -> None:
    """Keep the access token and its expiry as one blob, and the refresh token
    separately — they have different lifetimes, and revoking one should not
    disturb the other."""
    secrets.set_secret(address, PURPOSE_ACCESS, tokens.to_json())
    if tokens.refresh_token:
        secrets.set_secret(address, PURPOSE_REFRESH, tokens.refresh_token)


def stored_tokens(address: str) -> oauth.TokenSet:
    try:
        tokens = oauth.TokenSet.from_json(
            secrets.get_secret(address, PURPOSE_ACCESS))
    except (SecretMissing, SecretUnavailable):
        tokens = oauth.TokenSet(access_token="")
    if not tokens.refresh_token:
        try:
            tokens = oauth.TokenSet(
                access_token=tokens.access_token,
                refresh_token=secrets.get_secret(address, PURPOSE_REFRESH),
                expires_at=tokens.expires_at, scope=tokens.scope)
        except (SecretMissing, SecretUnavailable):
            pass
    return tokens


def forget(address: str) -> None:
    """Removing an account removes its secrets. All of them."""
    for purpose in (PURPOSE_PASSWORD, PURPOSE_REFRESH, PURPOSE_ACCESS):
        secrets.delete_secret(address, purpose)


def configured(address: str, provider_name: str) -> bool:
    """Whether this account could authenticate at all, without trying."""
    if secrets.has_secret(address, PURPOSE_PASSWORD):
        return True
    return bool(stored_tokens(address).refresh_token
                and has_registration(provider_name))


# ------------------------------------------------------------- the answer
def resolve(address: str, provider_name: str, *,
            method: str = "",
            post: Callable = oauth._post,
            now: dt.datetime | None = None) -> Credential:
    """The credential to authenticate this account with, refreshed if needed.

    `method` is the account's `auth_method` column. Empty means "whatever is
    stored", which is what a first connection after an import wants.
    """
    provider = providers.get(provider_name)

    if method == METHOD_PASSWORD or (not method and not provider.supports_oauth):
        return _password(address, provider)

    tokens = stored_tokens(address)
    if not tokens.refresh_token and not tokens.access_token:
        if provider.allows_password and secrets.has_secret(address, PURPOSE_PASSWORD):
            # Nothing from OAuth but a password is stored: use it rather than
            # refusing. An account that works should work.
            return _password(address, provider)
        raise NotConfigured(f"no credential is stored for {address}")

    if not tokens.expired(now=now):
        return Credential(method=METHOD_OAUTH2, user=address,
                          secret=tokens.access_token)

    client_id, client_secret = registration(provider.name)
    if not client_id:
        raise NotConfigured(
            f"no OAuth registration is stored for {provider.label}; "
            f"corMani cannot refresh {address} without one")
    # THE ONE ATTEMPT. Whatever comes back from here, the engine treats a
    # refusal as permanent, because this was the thing that might have fixed it.
    refreshed = oauth.refresh_token(
        provider, client_id=client_id, client_secret=client_secret,
        refresh=tokens.refresh_token, post=post, now=now)
    set_tokens(address, refreshed)
    return Credential(method=METHOD_OAUTH2, user=address,
                      secret=refreshed.access_token)


def sign_in(address: str, provider_name: str, *,
            open_browser: Callable | None = None, calendar: bool = False,
            post: Callable = oauth._post, timeout: float = 300.0) -> oauth.TokenSet:
    """Take one account through the browser flow and keep what comes back.

    Interactive, and therefore not something the engine ever calls: a sync that
    could open a browser would open fifteen of them the first time a laptop
    came back from a week away. The engine reports `AuthFailed` and the
    interface offers this.
    """
    provider = providers.get(provider_name)
    if not provider.supports_oauth:
        raise NotConfigured(f"{provider.label} accounts do not use OAuth")
    client_id, client_secret = registration(provider.name)
    if not client_id:
        raise NotConfigured(
            f"no OAuth registration is stored for {provider.label}. One Google "
            f"Cloud project or Azure app registration covers every account on "
            f"it; record it once with `set_registration`")

    verifier = oauth.make_verifier()
    state = secrets_module.token_urlsafe(24)
    server = oauth.listen()
    try:
        redirect_uri = oauth.redirect_uri_for(server)
        url = oauth.authorization_url(
            provider, client_id, redirect_uri, state=state, verifier=verifier,
            login_hint=address, calendar=calendar)
        opener = open_browser or _open_browser
        opener(url)
        code = oauth.wait_for_code(server, state=state, timeout=timeout)
    except BaseException:
        # `wait_for_code` closes the socket itself; anything failing before it
        # would otherwise leave a listener bound for the life of the process.
        try:
            server.server_close()
        except Exception:
            pass
        raise

    tokens = oauth.exchange_code(
        provider, client_id=client_id, client_secret=client_secret, code=code,
        redirect_uri=redirect_uri, verifier=verifier, post=post)
    if not tokens.refresh_token:
        # Worth refusing rather than storing: without it the account works for
        # an hour and then asks to sign in again, and the cause is a consent
        # screen that was answered weeks ago.
        raise NotConfigured(
            f"{provider.label} returned no refresh token for {address}. "
            f"Revoke corMani's access in the account's security settings and "
            f"sign in again")
    set_tokens(address, tokens)
    return tokens


def _open_browser(url: str) -> None:
    """Kept behind a name so the flow can be driven without a browser."""
    import webbrowser

    webbrowser.open(url)


def _password(address: str, provider: Provider) -> Credential:
    if not provider.allows_password:
        raise NotConfigured(
            f"{provider.label} no longer accepts an app password; "
            f"{address} must be authorised with OAuth")
    try:
        return Credential(method=METHOD_PASSWORD, user=address,
                          secret=secrets.get_secret(address, PURPOSE_PASSWORD))
    except SecretMissing:
        raise NotConfigured(f"no app password is stored for {address}") from None


def authenticate(connection, credential: Credential) -> None:
    """Apply a credential to a connection. The one place the two words meet."""
    if credential.method == METHOD_OAUTH2:
        connection.authenticate_xoauth2(credential.user, credential.secret)
    else:
        connection.login(credential.user, credential.secret)
