# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the provider said, and whether trying again can fix it.
#
# `imap/errors.py`'s taxonomy, over HTTP instead of over a protocol's response
# lines, and for the same single question: RETRY, or STOP AND TELL THE USER.
# What differs is that HTTP gives a number, so most of the classification is
# exact rather than a regular expression over prose — and the exceptions to
# that are the interesting part.
#
# 403 IS THE ONE THAT MATTERS. Google uses it for both "you may not do this"
# and "you are doing it too fast": `rateLimitExceeded`, `userRateLimitExceeded`
# and `quotaExceeded` all arrive as 403, and treating them as permanent would
# park an account for a day over a burst that a minute would have cleared.
# The JSON body's `reason` is what tells them apart, so a 403 is classified by
# its body and not by its status.
#
# 410 CARRIES AN INSTRUCTION, NOT A FAILURE. Google answers a stale syncToken
# with 410 and `fullSyncRequired`, and Graph expires a deltaLink the same way.
# The right response is to forget the bookmark and fetch the window again,
# which is why `TokenExpired` is its own class: it is the one error the sync
# handles by itself and never reports.
#
# 412 IS A CONFLICT AND IS THE POINT OF SENDING THE ETAG AT ALL. The event
# changed between the user reading it and the queue draining, so the write was
# refused rather than silently overwriting what somebody did from a phone.
# `Conflict` is permanent for that op and harmless for the account.
#
# NOTHING HERE CARRIES A TOKEN. `describe` is the same belt-and-braces
# redaction the IMAP side does, and for the same reason: this text is stored in
# the database and shown in the interface.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re


class CalendarError(Exception):
    """Anything this package raises."""


class Transient(CalendarError):
    """Try again later. The network, the service, or a burst."""


class Permanent(CalendarError):
    """Do not try again until a person changes something."""


class AuthFailed(Permanent):
    """Rejected or expired credentials, after the one refresh that might have
    fixed it. Permanent for the same reason as the IMAP side's."""


class NotAuthorised(Permanent):
    """Authenticated, and not allowed. A calendar shared read-only, a scope
    that was never granted, an account whose administrator says no."""


class NotFound(Permanent):
    """The calendar or the event is not there. Permanent for that op and not
    for the account: the sync moves on, exactly as a missing folder does."""


class Conflict(Permanent):
    """The etag did not match. Somebody else changed it first."""


class TokenExpired(CalendarError):
    """The provider's own sync bookmark is stale. Handled, never reported.

    Deliberately NOT a Transient: transient means wait, and this means fetch
    the window again now.
    """


class RateLimited(Transient):
    """A quota, not a fault. `retry_after` when the provider said one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServiceError(Transient):
    """A 5xx. Theirs, and it will pass."""


# Google's `reason` values that mean "slow down" rather than "you may not".
_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded",
                 "backenderror", "rateLimitExceeded".lower()}
# And the ones that mean the bookmark is stale rather than the request wrong.
_STALE_REASONS = {"fullsyncrequired", "syncstateNotFound".lower(),
                  "resyncrequired"}

_ACTIVITY_LIMIT = re.compile(r"ActivityLimitReached|TooManyRequests|"
                             r"throttl|quota", re.IGNORECASE)


def reason_of(payload) -> str:
    """The provider's own machine-readable word for what went wrong.

    Two shapes, because there are two providers: Google nests
    `error.errors[0].reason`, Graph puts `error.code` at the top. Neither is
    guaranteed to be there, and an empty string is the honest answer when it
    is not.
    """
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason", "") or "")
    return str(error.get("code", "") or "")


def message_of(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, dict):                        # Graph nests it
            message = message.get("value", "")
        return str(message or "")
    return str(payload.get("error_description", "") or "")


def classify(status: int, payload=None, *, retry_after: float | None = None,
             text: str = "") -> CalendarError:
    """The right exception for one refusal. Status first, body where it must be."""
    reason = reason_of(payload).lower()
    detail = message_of(payload) or text or f"HTTP {status}"

    if status in (401,):
        return AuthFailed(detail)
    if status == 403:
        # See the module header: the body decides, not the number.
        if reason in _RATE_REASONS or _ACTIVITY_LIMIT.search(detail):
            return RateLimited(detail, retry_after)
        return NotAuthorised(detail)
    if status == 404:
        return NotFound(detail)
    if status in (409, 412):
        return Conflict(detail)
    if status == 410:
        if not reason or reason in _STALE_REASONS:
            return TokenExpired(detail)
        return NotFound(detail)
    if status == 429:
        return RateLimited(detail, retry_after)
    if status >= 500:
        return ServiceError(detail)
    if status >= 400:
        if reason in _STALE_REASONS:
            return TokenExpired(detail)
        return Permanent(detail)
    return CalendarError(detail)                             # pragma: no cover


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, Transient)


def describe(exc: BaseException) -> str:
    """A line for `calendar.last_error` and the status bar. Never a secret."""
    text = str(exc).strip() or exc.__class__.__name__
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?i)\b(Bearer|password|token|access_token)\s*\S+",
                  r"\1 <redacted>", text)
    return text[:300]
