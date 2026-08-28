# SPDX-License-Identifier: GPL-3.0-or-later
#
# A message being written, as data.
#
# PLAIN VALUES AND NOTHING LIVE. A draft outlives the window it was typed in —
# it is saved, closed, reopened tomorrow, and sent from the queue — so it holds
# strings and ids rather than a connection, a widget or a `Row`. What is on the
# wire tomorrow must not depend on what was on the screen today.
#
# ADDRESSES ARE HELD AS TYPED AND PARSED WHEN NEEDED. A person writing to three
# people types three things separated by commas and expects to see them again
# exactly as typed; normalising on every keystroke moves the cursor and loses
# the half-finished address they were in the middle of. `recipients` parses, and
# `is_addressed` is what the Send button asks.
#
# THE BODY IS PLAIN TEXT. Stage 3's reading pane renders HTML and sanitises it;
# WRITING it is a different and much larger job — an editor, a serialiser, and a
# second set of quoting rules — and every one of the correspondents this client
# exists for reads plain text fine. The message goes out as text/plain and says
# so, rather than as HTML that pretends the user chose fonts they never touched.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import email.utils
from dataclasses import dataclass, field, replace

# The separator RFC 3676 §4.3 defines: a line of exactly "-- ". Clients that
# understand it hide or grey what follows, and clients that do not show two
# dashes, which is what a signature has looked like since before MIME.
SIGNATURE_MARK = "-- "


@dataclass(frozen=True)
class Attachment:
    """A file to send. The path is read at send time and not before.

    Held as a path rather than as bytes so that a draft saved on Monday and sent
    on Tuesday sends Tuesday's version of the file — which is what a person who
    edited it in between meant.
    """

    path: str
    filename: str = ""
    content_type: str = ""

    @property
    def name(self) -> str:
        import os

        return self.filename or os.path.basename(self.path)


@dataclass(frozen=True)
class Draft:
    """Everything a message needs before it is bytes."""

    account_id: int
    from_address: str = ""
    from_name: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = ""
    in_reply_to: str = ""
    references: str = ""
    attachments: tuple = field(default=())
    # The row this draft is saved as, once it has been saved. None until then.
    message_id: int | None = None

    def with_changes(self, **changes) -> "Draft":
        return replace(self, **changes)

    # ------------------------------------------------------------ addresses
    @property
    def sender(self) -> str:
        return email.utils.formataddr((self.from_name, self.from_address))

    def recipients(self) -> list[str]:
        """Every address this goes to, To, Cc and Bcc together.

        What SMTP is given, and deliberately not what any header says: the Bcc
        recipients are on the envelope and must never be in the message.
        """
        out: list[str] = []
        for field_ in (self.to, self.cc, self.bcc):
            for _name, address in email.utils.getaddresses([field_ or ""]):
                address = address.strip()
                if address and address not in out:
                    out.append(address)
        return out

    @property
    def is_addressed(self) -> bool:
        return bool(self.recipients())

    @property
    def is_empty(self) -> bool:
        """Nothing typed anywhere. What "close without asking" means."""
        return not any((self.to.strip(), self.cc.strip(), self.bcc.strip(),
                        self.subject.strip(), self.body.strip(),
                        self.attachments))

    def summary(self) -> str:
        """One line for a title bar or a status message."""
        return self.subject.strip() or "(no subject)"


def with_signature(body: str, signature: str) -> str:
    """The body with the identity's signature under it, once.

    Idempotent on purpose: the composer re-applies this when the user changes
    the identity, and a second signature stacked under the first is the classic
    result of doing that with a plain concatenation.
    """
    stripped = strip_signature(body)
    if not signature.strip():
        return stripped
    return f"{stripped.rstrip()}\n\n{SIGNATURE_MARK}\n{signature.rstrip()}\n"


def strip_signature(body: str) -> str:
    """Everything above the LAST signature separator.

    The LAST, because that is where a signature is; an earlier one is the
    user's own line of dashes and cutting there would throw away what they went
    on to write. A separator inside a QUOTED message cannot match at all — the
    quoting prefixes every line, so it arrives as "> -- ".
    """
    lines = body.splitlines()
    for position in range(len(lines) - 1, -1, -1):
        if lines[position].rstrip() == SIGNATURE_MARK.rstrip():
            return "\n".join(lines[:position]).rstrip("\n")
    return body
