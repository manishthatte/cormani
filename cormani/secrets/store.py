# SPDX-License-Identifier: GPL-3.0-or-later
#
# The only place corMani keeps a secret.
#
# OAuth refresh tokens and app passwords go to the operating system's keyring —
# GNOME Keyring or KWallet through the Secret Service API on Linux, the
# Credential Manager on Windows — and nowhere else. Not the config file, not the
# database, not an environment variable, not a log line. CONVENTIONS.txt §7.
#
# Two design points worth stating because they are easy to undo by accident:
#
# `keyring` is imported lazily. The module must be importable in a test run and
# on a machine with no secret service, so that everything which merely *refers*
# to credentials can be tested without any being stored. `available()` reports
# the truth rather than raising.
#
# Nothing here ever returns a secret in a repr, a log, or an exception message.
# The exceptions carry the account and the purpose, never the value — an
# exception is the most likely thing to end up in a bug report.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from .. import APP_ID


class SecretUnavailable(RuntimeError):
    """No keyring backend. The caller must ask the user, not invent a value."""


class SecretMissing(KeyError):
    """Nothing stored under that key. Distinct from the backend being absent:
    one means 'ask the user to sign in', the other means 'this machine cannot
    keep secrets', and they need different messages."""


def _backend():
    try:
        import keyring
    except ImportError as exc:                       # pragma: no cover
        raise SecretUnavailable(
            "the `keyring` module is not installed; on Debian this is "
            "python3-keyring") from exc
    return keyring


def available() -> bool:
    """Whether a usable backend exists. Never raises."""
    try:
        keyring = _backend()
        from keyring.backends import fail
        return not isinstance(keyring.get_keyring(), fail.Keyring)
    except Exception:
        return False


def backend_name() -> str:
    """For the start-up log and the diagnostics screen. Never raises."""
    try:
        kr = _backend().get_keyring()
        return f"{kr.__class__.__module__}.{kr.__class__.__name__}"
    except Exception as exc:
        return f"unavailable ({exc.__class__.__name__})"


def _key(account: str, purpose: str) -> str:
    """One entry per (account, purpose).

    Separate entries rather than one blob per account: an OAuth refresh token
    and an app password have different lifetimes, and revoking one should not
    disturb the other.
    """
    return f"{account}:{purpose}"


def set_secret(account: str, purpose: str, value: str) -> None:
    keyring = _backend()
    try:
        keyring.set_password(APP_ID, _key(account, purpose), value)
    except Exception as exc:
        raise SecretUnavailable(
            f"could not store the {purpose} for {account}: "
            f"{exc.__class__.__name__}") from None


def get_secret(account: str, purpose: str) -> str:
    keyring = _backend()
    try:
        value = keyring.get_password(APP_ID, _key(account, purpose))
    except Exception as exc:
        raise SecretUnavailable(
            f"could not read the {purpose} for {account}: "
            f"{exc.__class__.__name__}") from None
    if value is None:
        raise SecretMissing(f"no {purpose} stored for {account}")
    return value


def has_secret(account: str, purpose: str) -> bool:
    try:
        get_secret(account, purpose)
        return True
    except (SecretMissing, SecretUnavailable):
        return False


def delete_secret(account: str, purpose: str) -> None:
    """Removing an account must remove its secrets. Absent is not an error."""
    keyring = _backend()
    try:
        keyring.delete_password(APP_ID, _key(account, purpose))
    except Exception:
        pass
