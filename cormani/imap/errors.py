# SPDX-License-Identifier: GPL-3.0-or-later
#
# What went wrong, and whether trying again can fix it.
#
# The engine has exactly one question to ask of any failure: RETRY, or STOP AND
# TELL THE USER. Everything here exists to answer it, because getting it wrong
# is expensive in both directions. Retrying a rejected password is how fifteen
# accounts get an IP address blocked; giving up on a dropped connection is how
# a laptop that closed its lid stops syncing until someone notices.
#
# So the taxonomy is by RESPONSE, not by cause. `Transient` means try later.
# `Permanent` means the account will not sync again until a person does
# something — re-authorise, fix a hostname, choose another folder. The
# distinction is the whole module; the individual classes only exist to carry a
# better message to the interface.
#
# RATE LIMITING IS TRANSIENT BUT NOT ORDINARY. Gmail's daily download cap is
# the constraint on a first import of eight Google accounts (docs/accounts.txt),
# and the correct response is to wait hours, not seconds. It is its own class so
# the back-off can treat it that way rather than hammering a quota that has
# already said no.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re


class ImapError(Exception):
    """Anything the engine raises. Never carries a credential — see below."""


class Transient(ImapError):
    """Try again later. The connection, the server, or the network."""


class Permanent(ImapError):
    """Do not try again until a person changes something."""


class AuthFailed(Permanent):
    """Rejected credentials. Retrying is what gets an address blocked.

    Permanent by deliberate choice even though a refresh MIGHT fix it: the
    refresh is attempted once, by the auth layer, before the engine ever sees
    this. By the time it arrives here, the answer really is "ask the user".
    """


class MailboxGone(Permanent):
    """The folder is not there any more. The account is fine; this folder is
    not, and the sync moves on to the next one rather than stopping."""


class RateLimited(Transient):
    """A quota, not a fault. Waiting minutes is the wrong answer; hours is the
    right one, and `retry_after` says so when the server does."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProtocolError(Transient):
    """A BAD response, or a line neither side understood.

    Transient rather than permanent, and it is a judgement: a BAD is usually a
    bug on one side, but the servers that produce them intermittently are real,
    and stopping an account permanently over one is worse than trying again.
    """


# Response codes and phrases that decide the answer. Matched against the whole
# response text, which is why they are patterns and not equality: servers wrap
# the code in prose, and the prose differs between them and between versions.
_AUTH_FAILED = re.compile(
    r"AUTHENTICATIONFAILED|AUTHORIZATIONFAILED|Invalid credentials|"
    r"Authentication failed|LOGIN failed|\[ALERT\].*sign[- ]?in|"
    r"Application-specific password required|Web login required",
    re.IGNORECASE)

_RATE_LIMITED = re.compile(
    r"OVERQUOTA|Too many simultaneous connections|"
    r"Maximum number of connections|Daily limit|Rate limit|"
    r"Try again later|Temporary System Problem|Lookup failed|"
    r"\[LIMIT\]|\[INUSE\]|\[UNAVAILABLE\]|Request is throttled|ServerBusy",
    re.IGNORECASE)

_GONE = re.compile(r"NONEXISTENT|Unknown Mailbox|Mailbox doesn't exist|"
                   r"no such mailbox", re.IGNORECASE)


def classify(text: str, *, default: type[ImapError] = Transient) -> ImapError:
    """Turn a server's refusal into the right exception.

    The order matters. A Gmail message can say both "Invalid credentials" and
    "Web login required"; both are the user's problem. A message saying "Too
    many simultaneous connections" also often says "failed", and treating that
    as a bad password would stop an account that is merely busy — so the rate
    limit is tested BEFORE the authentication failure.
    """
    text = (text or "").strip()
    if _RATE_LIMITED.search(text):
        return RateLimited(text)
    if _AUTH_FAILED.search(text):
        return AuthFailed(text)
    if _GONE.search(text):
        return MailboxGone(text)
    return default(text)


def is_transient(exc: BaseException) -> bool:
    """The one question the engine asks."""
    return isinstance(exc, Transient)


def describe(exc: BaseException) -> str:
    """A line for `account.last_error` and the status bar.

    NEVER interpolates a credential. The engine stores this in the database and
    shows it in the interface, and an exception message is the single most
    likely thing to end up in a bug report — CONVENTIONS.txt §7.
    """
    text = str(exc).strip() or exc.__class__.__name__
    text = re.sub(r"\s+", " ", text)
    # Belt and braces: if a token or password ever reaches an exception message
    # through a path nobody anticipated, it stops here rather than in a log.
    text = re.sub(r"(?i)\b(auth=Bearer|password|token)\s*\S+", r"\1 <redacted>", text)
    return text[:300]
