# SPDX-License-Identifier: GPL-3.0-or-later
#
# One SMTP submission: connect, prove who you are, hand over the bytes.
#
# TLS IS NOT OPTIONAL AND IS NOT NEGOTIABLE DOWNWARDS. Port 465 is implicit TLS
# and port 587 is STARTTLS, and if STARTTLS is not offered the connection is
# ABANDONED rather than used. A submission carries the user's password or token
# and a decade of correspondence's worth of trust; a server that will not
# encrypt it is a server this client does not talk to. Certificates are verified
# for the same reason, with the platform's own store.
#
# ONE FAILURE CLASS WITH A FLAG, and not a taxonomy. The offline queue already
# counts attempts and gives up after ten — `store/pending.py` — so the only
# thing the caller needs from a failure is whether trying again could possibly
# help. `SendFailed.permanent` is that, and it is set from the SMTP reply code:
# 5xx is the server saying no, 4xx is it saying not now.
#
# A PARTIAL SEND IS A SEND. `smtplib.sendmail` raises when SOME recipients are
# refused, and the message has still gone to the others; retrying it would
# deliver a second copy to everyone it reached. So refusals are collected and
# REPORTED rather than raised, and the caller writes them beside the message —
# CONVENTIONS.txt §8, and the one case where "it worked" and "it failed" are
# both true.
#
# THE CREDENTIAL IS NEVER LOGGED, and neither is the message. What comes back
# from here is a code, a recipient list and the server's own words.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass, field

from ..auth.credentials import METHOD_OAUTH2, Credential

# The submission ports RFC 6409 and everyone's practice settled on.
PORT_STARTTLS = 587
PORT_SSL = 465

# Long enough for a slow server to accept a large attachment, short enough that
# a dead one does not hold a sync open. The queue retries either way.
TIMEOUT = 60.0


class SendFailed(RuntimeError):
    """The message did not go. `permanent` says whether asking again could help."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True)
class Sent:
    """What happened, when something happened.

    `refused` is not an error: those recipients were rejected and the rest were
    accepted, and the message is gone. It is here so the caller can say so.
    """

    recipients: tuple = field(default=())
    refused: dict = field(default_factory=dict)

    @property
    def partial(self) -> bool:
        return bool(self.refused)


def _permanent(code: int | None) -> bool:
    """5xx is a refusal; 4xx is a delay; anything else is worth one more try."""
    return bool(code and 500 <= int(code) < 600)


def default_factory(host: str, port: int, timeout: float):
    """A real smtplib client. Replaced in tests by one wired to a fake socket.

    The same shape as `imap.client.Connection.connect`'s factory, and for the
    same reason: the suite talks to a real client library against a server in
    this process, rather than to a mock of the library.
    """
    if port == PORT_SSL:
        return smtplib.SMTP_SSL(host=host, port=port, timeout=timeout,
                                context=ssl.create_default_context())
    return smtplib.SMTP(host=host, port=port, timeout=timeout)


class Sender:
    """One connection, for one account, for as long as it takes to send."""

    def __init__(self, smtp, *, address: str = "") -> None:
        self._smtp = smtp
        self.address = address

    # ------------------------------------------------------------ connecting
    @classmethod
    def connect(cls, host: str, port: int, credential: Credential, *,
                factory=default_factory, timeout: float = TIMEOUT) -> "Sender":
        if not host:
            raise SendFailed("no SMTP server is configured for this account",
                             permanent=True)
        port = int(port or PORT_STARTTLS)
        try:
            smtp = factory(host, port, timeout)
        except (OSError, socket.timeout, smtplib.SMTPException, ssl.SSLError) as exc:
            raise SendFailed(f"could not reach {host}:{port} — {exc}") from None

        sender = cls(smtp, address=credential.user)
        try:
            sender._secure(host, port)
            sender._authenticate(credential)
        except SendFailed:
            sender.close()
            raise
        return sender

    def _secure(self, host: str, port: int) -> None:
        if port == PORT_SSL:
            return                       # the socket was TLS from the first byte
        try:
            self._smtp.ehlo_or_helo_if_needed()
            if not self._smtp.has_extn("starttls"):
                raise SendFailed(
                    f"{host} does not offer STARTTLS on port {port}; refusing "
                    f"to send a password and a message in the clear",
                    permanent=True)
            self._smtp.starttls(context=ssl.create_default_context())
            # A second EHLO, because the extension list before TLS is not the
            # one that counts: AUTH is commonly offered only afterwards.
            self._smtp.ehlo()
        except smtplib.SMTPException as exc:
            raise SendFailed(f"STARTTLS failed: {exc}") from None
        except (OSError, ssl.SSLError) as exc:
            raise SendFailed(f"STARTTLS failed: {exc}") from None

    def _authenticate(self, credential: Credential) -> None:
        try:
            if credential.method == METHOD_OAUTH2:
                self._xoauth2(credential)
            else:
                self._smtp.login(credential.user, credential.secret)
        except smtplib.SMTPAuthenticationError as exc:
            # Permanent by deliberate choice: the token was already refreshed
            # once by the auth layer before this connection was made, so by the
            # time a refusal arrives here it really does need a person.
            raise SendFailed(f"the server rejected the credential for "
                             f"{credential.user} ({exc.smtp_code})",
                             permanent=True) from None
        except smtplib.SMTPException as exc:
            raise SendFailed(f"authentication failed: {exc}") from None
        except (OSError, ssl.SSLError) as exc:
            raise SendFailed(f"authentication failed: {exc}") from None

    def _xoauth2(self, credential: Credential) -> None:
        """SASL XOAUTH2, the same blob IMAP uses — see imap/client.py."""
        import base64

        payload = base64.b64encode(
            f"user={credential.user}\x01auth=Bearer {credential.secret}"
            f"\x01\x01".encode("utf-8")).decode("ascii")
        code, response = self._smtp.docmd("AUTH", f"XOAUTH2 {payload}")
        if code != 235:
            text = response.decode("utf-8", "replace") if isinstance(
                response, bytes) else str(response)
            raise smtplib.SMTPAuthenticationError(code, text)

    # ---------------------------------------------------------------- sending
    def send(self, sender: str, recipients, raw: bytes) -> Sent:
        """Hand over one message. Returns what the server accepted."""
        recipients = [r for r in recipients if r]
        if not recipients:
            raise SendFailed("no recipients", permanent=True)
        try:
            refused = self._smtp.sendmail(sender, recipients, raw)
        except smtplib.SMTPRecipientsRefused as exc:
            raise SendFailed(
                f"every recipient was refused: "
                f"{', '.join(sorted(exc.recipients))}", permanent=True) from None
        except smtplib.SMTPSenderRefused as exc:
            raise SendFailed(f"the server refused {sender}: {exc.smtp_error}",
                             permanent=_permanent(exc.smtp_code)) from None
        except smtplib.SMTPResponseException as exc:
            raise SendFailed(f"the server said {exc.smtp_code}: "
                             f"{_text(exc.smtp_error)}",
                             permanent=_permanent(exc.smtp_code)) from None
        except smtplib.SMTPException as exc:
            raise SendFailed(f"sending failed: {exc}") from None
        except (OSError, ssl.SSLError) as exc:
            raise SendFailed(f"sending failed: {exc}") from None
        return Sent(recipients=tuple(recipients),
                    refused={k: _text(v[1]) for k, v in (refused or {}).items()})

    def close(self) -> None:
        try:
            self._smtp.quit()
        except Exception:                                    # pragma: no cover
            # A connection that will not say goodbye is still a connection that
            # is finished with. Nothing here is worth reporting to anyone.
            try:
                self._smtp.close()
            except Exception:
                pass

    def __enter__(self) -> "Sender":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
