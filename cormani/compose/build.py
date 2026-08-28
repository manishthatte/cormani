# SPDX-License-Identifier: GPL-3.0-or-later
#
# A draft, as the bytes that go on the wire.
#
# `email.message.EmailMessage` and the standard library's policy, not a string
# template. Every part of a mail header has a rule — encoded words for a name
# with an accent in it, folding at 78 columns, a comma that must be quoted
# inside a display name — and a client that formats its own headers gets one of
# them wrong and sends a message that arrives addressed to two people.
#
# BCC IS NOT A HEADER. It goes to SMTP as an envelope recipient and never into
# the message, which is the whole meaning of the field. `Draft.recipients`
# returns all three for the envelope; this file writes only two.
#
# THE MESSAGE-ID IS OURS AND IS FIXED AT SAVE TIME, not at send time. It is what
# the reply the correspondent sends will name in its References, and what this
# store threads the conversation by, so it has to be the same id in the copy
# filed in Sent and in the copy that left. Generated once, passed in here.
#
# THE DATE IS LOCAL AND SAYS ITS OFFSET. A Date header in UTC on a message
# written at half past three in the afternoon is one a correspondent reads as
# having been written at ten in the morning.
#
# WHAT IS DELIBERATELY NOT DONE: HTML. See compose/draft.py — the body is
# text/plain, and a client that sent HTML nobody asked for would also have to
# sanitise its own output.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import email.utils
import mimetypes
from email.message import EmailMessage
from pathlib import Path

from .. import APP_NAME, __version__
from .draft import Draft

# What an attachment of unknown type is called. RFC 2046 §4.5.1: the type for
# "some bytes, and the reader must decide".
DEFAULT_TYPE = "application/octet-stream"

# Read in one go. A mail attachment that does not fit in memory is a mail
# attachment no server will accept either — the common ceiling is 25 MB — so the
# streaming version would be complexity spent on a case that fails anyway.
MAX_ATTACHMENT = 64 * 1024 * 1024


class TooLarge(RuntimeError):
    """An attachment beyond anything a mail server would take."""


def new_message_id(address: str) -> str:
    """A Message-ID for something we are about to send.

    The domain comes from the sender's own address rather than the machine's
    hostname: `make_msgid` would otherwise leak the name of the computer the
    mail was written on into every message.
    """
    domain = address.partition("@")[2].strip() or "localhost"
    return email.utils.make_msgid(domain=domain)


def build(draft: Draft, *, message_id: str = "",
          now: dt.datetime | None = None) -> EmailMessage:
    """The draft as a message object. Raises TooLarge for an absurd attachment."""
    message = EmailMessage()
    message["From"] = draft.sender
    if draft.to.strip():
        message["To"] = draft.to.strip()
    if draft.cc.strip():
        message["Cc"] = draft.cc.strip()
    message["Subject"] = draft.subject.strip()
    message["Date"] = email.utils.format_datetime(now or _now())
    message["Message-ID"] = message_id or new_message_id(draft.from_address)
    if draft.in_reply_to.strip():
        message["In-Reply-To"] = draft.in_reply_to.strip()
    if draft.references.strip():
        message["References"] = draft.references.strip()
    message["User-Agent"] = f"{APP_NAME}/{__version__}"

    message.set_content(_body(draft), subtype="plain", charset="utf-8")
    for attachment in draft.attachments:
        _attach(message, attachment)
    return message


def to_bytes(draft: Draft, *, message_id: str = "",
             now: dt.datetime | None = None) -> bytes:
    return build(draft, message_id=message_id, now=now).as_bytes()


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _body(draft: Draft) -> str:
    """The text, with CRLF left alone.

    Line endings are the transport's business — smtplib and IMAP APPEND each
    normalise what they are given — and a body that arrives here with both kinds
    in it came from a paste, which is not an error.
    """
    body = draft.body or ""
    return body if body.endswith("\n") else body + "\n"


def _attach(message: EmailMessage, attachment) -> None:
    path = Path(attachment.path)
    size = path.stat().st_size
    if size > MAX_ATTACHMENT:
        raise TooLarge(f"{attachment.name} is {size // (1024 * 1024)} MB — "
                       f"too large to send")
    data = path.read_bytes()
    guessed = attachment.content_type or mimetypes.guess_type(attachment.name)[0]
    # A content type may carry PARAMETERS, and one of them is load-bearing:
    # an iTIP reply is `text/calendar; method=REPLY`, and an organiser's client
    # that receives it without the method treats it as a file rather than as
    # an answer. Splitting the whole string on "/" would make the subtype
    # `calendar; method=REPLY`, which is not a subtype at all.
    kind, _, parameters = (guessed or DEFAULT_TYPE).partition(";")
    main, _, sub = kind.strip().partition("/")
    message.add_attachment(data, maintype=main or "application",
                           subtype=sub or "octet-stream",
                           filename=attachment.name)
    if parameters.strip():
        part = message.get_payload()[-1]
        for chunk in parameters.split(";"):
            key, _, value = chunk.partition("=")
            if key.strip():
                part.set_param(key.strip(), value.strip().strip('"'))
