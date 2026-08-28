# SPDX-License-Identifier: GPL-3.0-or-later
#
# Deriving one message from another: reply, reply all, and forward.
#
# WHO A REPLY GOES TO IS NOT OBVIOUS AND IS NOT A PREFERENCE. Reply-To wins over
# From, because a correspondent who set it is asking to be answered somewhere
# else and the commonest reason is a mailing list. Reply all keeps everyone the
# original reached and drops the user's OWN addresses — every one of them, not
# just the account this arrived at, because replying to fifteen accounts' worth
# of mail with yourself in Cc is how a person ends up with their own mail in
# their own inbox.
#
# AND IT DROPS DUPLICATES ACROSS To AND Cc. A message addressed to you and
# copied to you is one recipient; a reply that put them in both fields would
# send two copies and look like a mistake, because it is one.
#
# THE SUBJECT PREFIX IS ADDED ONCE, and this strips before it adds rather than
# trusting the caller to hand over something already stripped. "Re: Re: Re:" is
# what happens when a client checks for its own prefix and not for the four
# other spellings of it — and, as stage 4 found, what happens when the column
# holding the stripped subject is simply not in the row being replied to.
#
# THE ATTRIBUTION LINE NAMES A PERSON AND A TIME IN THE READER'S OWN ZONE. The
# store keeps UTC — ui/messagelist.to_local exists for this at the other end —
# and a quotation that says 09:30 when the reader remembers 15:00 is a quotation
# they will distrust.
#
# FORWARDING IS INLINE, and it carries the original's attachments. As an
# attached message it is tidier and it is also unreadable to anyone whose client
# will not open a message/rfc822 inline, which in practice means anyone reading
# it on a phone.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import email.utils

from ..store import subject as subject_mod
from .draft import Attachment, Draft, with_signature

# What goes before a quoted line. One character and a space, and not "  " —
# every client in the world understands this one, and a quotation nobody's
# client can see is a quotation that reads as though the user wrote it.
QUOTE_PREFIX = "> "

REPLY_PREFIX = "Re: "
FORWARD_PREFIX = "Fwd: "


def _addresses(*fields: str) -> list[tuple[str, str]]:
    """(name, address) for everything in these header fields, in order."""
    out: list[tuple[str, str]] = []
    for value in fields:
        for name, address in email.utils.getaddresses([value or ""]):
            if address.strip():
                out.append((name.strip(), address.strip()))
    return out


def _join(pairs) -> str:
    return ", ".join(email.utils.formataddr(pair) for pair in pairs)


def _without(pairs, exclude) -> list[tuple[str, str]]:
    """The pairs whose address is not in `exclude`, keeping the first of each.

    Case-folded, because the local part of an address is case-sensitive only in
    a specification nobody honours, and treating Lyle@ and lyle@ as two people
    is how a reply-all grows a duplicate.
    """
    lowered = {a.strip().lower() for a in exclude}
    out, seen = [], set()
    for name, address in pairs:
        key = address.lower()
        if key in lowered or key in seen:
            continue
        seen.add(key)
        out.append((name, address))
    return out


def subject_for(subject: str, prefix: str) -> str:
    """`Re: ` or `Fwd: ` on the subject, and only one of them.

    It STRIPS first rather than trusting a caller to pass something already
    stripped. That is not defensiveness: the stored `subject_base` was missing
    from the list's Row until stage 4 noticed, and a rule that reads "add a
    prefix to whatever I am given" turns one absent column into `Re: Re:`.
    """
    base = subject_mod.strip_subject(subject or "")
    return f"{prefix}{base}" if base else prefix.strip()


def attribution(row, *, now: dt.datetime | None = None) -> str:
    """The line above a quotation: when, and who.

    Thunderbird's shape — "On <date>, <who> wrote:" — because it is the one
    every correspondent has read a thousand times and therefore does not have
    to read at all.
    """
    who = row.from_name or row.from_addr or "someone"
    when = _local(row.date_at)
    if when is None:
        return f"{who} wrote:"
    return f"On {when.strftime('%-d %b %Y at %H:%M')}, {who} wrote:"


def _local(iso: str):
    if not iso:
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is not None else parsed


def quoted(body: str) -> str:
    """The body with every line marked as somebody else's.

    NOT rewrapped. A quotation that reflows destroys the one thing quoting is
    for — showing what was actually said — and mangles anything the sender
    aligned: a table, a stack trace, a list of figures.
    """
    lines = (body or "").replace("\r\n", "\n").rstrip().split("\n")
    return "\n".join(f"{QUOTE_PREFIX}{line}".rstrip() for line in lines)


def _identity_for_reply(identities, row) -> object:
    """Which of the user's addresses to answer FROM.

    The one the message was addressed to, if it was addressed to one of them.
    Answering a message sent to `saptarang@` from `manish@` is a reply the
    correspondent has to think about, and they should not have to.
    """
    addressed = {a.lower() for _n, a in _addresses(row.to_addrs)}
    for identity in identities:
        if identity.address.lower() in addressed:
            return identity
    return identities[0] if identities else None


def signature_for_reply(identities, row) -> str:
    """The signature of the identity a reply to this message comes from."""
    identity = _identity_for_reply(list(identities), row)
    return identity.signature if identity else ""


def reply(row, body: str, identities, *, all_recipients: bool = False,
          mine=(), signature: str = "") -> Draft:
    """A reply to this message, ready to be edited.

    `mine` is every address that is the user — see accounts.list_identity_
    addresses. It is passed in rather than derived here because this module
    does not open the store, and because "who am I" spans fifteen accounts.
    """
    identity = _identity_for_reply(list(identities), row)
    reply_to = _addresses(row.reply_to or "")
    to_pairs = reply_to or _addresses(row.from_addr and
                                      email.utils.formataddr(
                                          (row.from_name, row.from_addr)) or "")
    exclude = set(mine) | {a for _n, a in to_pairs}
    if identity is not None:
        exclude.add(identity.address)

    cc_pairs = []
    if all_recipients:
        cc_pairs = _without(_addresses(row.to_addrs, row.cc_addrs), exclude)

    references = " ".join(x for x in ((row.references_ or ""),
                                      (row.message_id or "")) if x).strip()
    quoted_body = f"\n\n{attribution(row)}\n{quoted(body)}\n"
    return Draft(
        account_id=row.account_id,
        from_address=identity.address if identity else "",
        from_name=identity.display_name if identity else "",
        to=_join(to_pairs), cc=_join(cc_pairs),
        subject=subject_for(row.subject, REPLY_PREFIX),
        body=with_signature(quoted_body, signature),
        in_reply_to=row.message_id or "", references=references,
        attachments=())


def forward(row, body: str, identities, *, attachments=(),
            signature: str = "") -> Draft:
    """The message again, to somebody else, with its attachments.

    No recipients: a forward is addressed by the person forwarding it, and a
    field filled in for them is a field they will send without reading.
    """
    identity = _identity_for_reply(list(identities), row)
    header = "\n".join([
        "---------- Forwarded message ----------",
        f"From: {email.utils.formataddr((row.from_name, row.from_addr))}",
        f"Date: {_stamp(row.date_at)}",
        f"Subject: {row.subject}",
        f"To: {row.to_addrs}",
    ])
    forwarded = f"\n\n{header}\n\n{body or ''}".rstrip() + "\n"
    return Draft(
        account_id=row.account_id,
        from_address=identity.address if identity else "",
        from_name=identity.display_name if identity else "",
        subject=subject_for(row.subject, FORWARD_PREFIX),
        body=with_signature(forwarded, signature),
        references=" ".join(x for x in ((row.references_ or ""),
                                        (row.message_id or "")) if x).strip(),
        attachments=tuple(Attachment(path=str(path), filename=name,
                                     content_type=content_type)
                          for path, name, content_type in attachments))


def _stamp(iso: str) -> str:
    when = _local(iso)
    return when.strftime("%-d %b %Y at %H:%M") if when else (iso or "")


def blank(account_id: int, identity, *, signature: str = "",
          to: str = "") -> Draft:
    """A new message from nothing, addressed to somebody when one is named.

    `to` exists for the address book's Write to: writing to a person you have
    just looked up is the one thing a contact card is FOR, and a composer that
    opened empty would ask them to type an address the application is showing
    them. `ui/composer.py` already focuses the body rather than the To line
    when a draft arrives addressed, which is the behaviour this wants.
    """
    return Draft(account_id=account_id,
                 from_address=identity.address if identity else "",
                 from_name=identity.display_name if identity else "",
                 to=to,
                 body=with_signature("", signature))
