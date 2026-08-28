# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a message says about ITSELF: whether it is bulk, and whether it is a
# delivery failure.
#
# Two questions, one module, because they are the same kind of question — both
# are answered from headers rather than from content, both decide whether a
# message is WORK, and both exist to serve the tracking layer. Separate from
# `envelope.py` because that file is at 437 of the 600 lines the packaging test
# allows and because this is a subject rather than an addition to one.
#
# ── WHY BULK MATTERS AT ALL ────────────────────────────────────────────────
#
# The triage queue asks "what has arrived that is on no thread". Without a
# bulk test the answer includes every newsletter, receipt and notification, and
# the prototype measured what that does: a queue of 17,774 items where the real
# answer was 40. A queue nobody works through is worse than no queue, because
# the whole point is that an unfiled reply is visible rather than silent.
#
# THE TEST IS THE SENDER'S OWN DECLARATION AND NEVER A GUESS ABOUT THE TEXT.
# List-Id (RFC 2919), List-Unsubscribe (RFC 2369), Precedence, and
# Auto-Submitted (RFC 3834) are all things the sender put there to say "this is
# automatic". Nothing here looks for the word "unsubscribe" in a body or
# reasons about how many recipients there are: a false positive hides a message
# somebody is waiting on, and CONVENTIONS.txt §8 is about exactly this — the
# quiet wrong is worse than the loud one.
#
# A DSN IS NOT BULK, whatever else it declares. Every bounce carries
# `Auto-Submitted: auto-replied` and most carry `Precedence: bulk`, and a
# delivery failure hidden by the bulk filter is the single most useful message
# in the mailbox thrown away. So the bounce test runs first and wins.
#
# ── WHY THE RECIPIENT IS THE POINT OF A DSN ────────────────────────────────
#
# `contacts.note_bounce` has existed since stage 4 and nothing has ever called
# it, so the bounce guard has only ever known what it was told by hand. What it
# needs is the address that FAILED — which is not the From of the bounce (that
# is a mailer daemon), not the To (that is the user), and not in the subject.
# RFC 3464 puts it in a `message/delivery-status` part, as `Final-Recipient`,
# and that is the only place it is reliably machine-readable.
#
# WHAT IS PARSED IS THE STRUCTURE, AND THE HEURISTIC IS THE FALLBACK. A
# well-formed DSN is `multipart/report; report-type=delivery-status` with a
# per-recipient block; that path is exact. Servers that send a human-readable
# bounce with no report part exist and are common enough to matter, so there is
# a second path — and it is deliberately narrow, requiring a mailer-daemon
# sender AND a recognisable status code, because a false bounce marks a working
# address as dead and the composer then warns about it for ever.
#
# A `delayed` ACTION IS NOT A FAILURE. "Your message has not been delivered
# yet, still trying" arrives as a DSN with Action: delayed, and treating it as a
# bounce would blacklist an address whose mail is about to arrive.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message

# The sender's own declarations that this is automatic. Each is a header the
# sender CHOSE to add; none is inferred.
_LIST_HEADERS = ("List-Id", "List-Unsubscribe", "List-Post", "List-Help",
                 "List-Subscribe", "List-Archive")
_BULK_PRECEDENCE = ("bulk", "list", "junk", "auto_reply")

# RFC 3834: any value but "no" means the message was generated automatically.
# "no" is the explicit statement that a person wrote it.
_AUTO_SUBMITTED_HUMAN = "no"

_STATUS = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")

# The local parts a mail system uses to say "I am a machine". Matched on the
# local part alone because the domain is the failing server's and is never
# predictable.
_DAEMON_LOCALS = ("mailer-daemon", "postmaster", "mail-daemon",
                  "mailerdaemon", "no-reply-dsn", "bounce", "bounces")

_FAILED_ACTIONS = ("failed", "failure")


@dataclass(frozen=True)
class Delivery:
    """What the headers say about the message's own nature.

    Every field is a fact taken from a header. `is_bounce` without a
    `recipient` is possible and is honest: some servers report a failure
    without naming the address in a machine-readable way, and saying "this came
    back but I cannot tell you for whom" is better than guessing an address and
    marking the wrong one dead.
    """

    is_bulk: bool = False
    is_bounce: bool = False
    recipient: str = ""          # the address delivery failed FOR
    status: str = ""             # the enhanced status code, e.g. 5.1.1
    diagnostic: str = ""         # the server's own words, kept verbatim
    original_message_id: str = ""    # the Message-ID of what came back

    @property
    def permanent(self) -> bool:
        """A 5.x.x is final; a 4.x.x will be retried by the sending server.

        The distinction is what stops a full mailbox on Tuesday marking an
        address dead for ever — `store/contacts.py` says the guard warns rather
        than refuses for the same reason.
        """
        return self.status.startswith("5")


def read(message: Message) -> Delivery:
    """Everything this module can say about one parsed message."""
    bounce = _bounce(message)
    if bounce.is_bounce:
        # A DSN is never bulk. See the header: every bounce declares itself
        # automatic, and hiding one is throwing away the most useful message in
        # the mailbox.
        return bounce
    return Delivery(is_bulk=is_bulk(message))


# ------------------------------------------------------------------- bulk
def is_bulk(message: Message) -> bool:
    """Whether the SENDER declared this automatic. Never inferred from content."""
    for header in _LIST_HEADERS:
        if message.get(header):
            return True
    precedence = str(message.get("Precedence") or "").strip().lower()
    if precedence in _BULK_PRECEDENCE:
        return True
    auto = str(message.get("Auto-Submitted") or "").strip().lower()
    if auto and not auto.startswith(_AUTO_SUBMITTED_HUMAN):
        return True
    # Microsoft's own marker, which Exchange and Outlook.com both set on
    # generated mail and which no human-composed message carries.
    if str(message.get("X-Auto-Response-Suppress") or "").strip():
        return True
    return bool(str(message.get("X-Mailer-Daemon") or "").strip())


# ----------------------------------------------------------------- bounces
def _bounce(message: Message) -> Delivery:
    report = _report_part(message)
    if report is not None:
        parsed = _from_report(report)
        if parsed.is_bounce:
            return Delivery(is_bulk=False, is_bounce=True,
                            recipient=parsed.recipient, status=parsed.status,
                            diagnostic=parsed.diagnostic,
                            original_message_id=_original_id(message))
        # A well-formed report that is NOT a failure — a delayed notice, or a
        # delivered receipt. Structured, understood, and deliberately not a
        # bounce.
        return Delivery(is_bulk=is_bulk(message))
    return _heuristic(message)


def _report_part(message: Message) -> Message | None:
    """The `message/delivery-status` part of an RFC 3464 report, if there is one.

    Walked rather than indexed: the part is the second of three by convention
    and by convention only, and a report with a preamble or a wrapper puts it
    somewhere else.
    """
    if not message.is_multipart():
        return None
    for part in message.walk():
        if (part.get_content_type() or "").lower() == "message/delivery-status":
            return part
    return None


@dataclass(frozen=True)
class _Report:
    is_bounce: bool = False
    recipient: str = ""
    status: str = ""
    diagnostic: str = ""


def _from_report(part: Message) -> _Report:
    """One `message/delivery-status` part, as its per-recipient blocks.

    The part is a sequence of RFC 822-style field groups separated by blank
    lines: one per-message group, then one per RECIPIENT.

    THE EMAIL PACKAGE PARSES THIS PART AS A MULTIPART, and that is not a quirk
    to work around — it is right, and it is the easy path. Each field group
    becomes a sub-Message whose HEADERS are the group's fields, so
    `get_payload()` hands back a list of them and `get_payload(decode=True)`
    hands back None. Reading the text and splitting on blank lines works only
    for the parts the package did NOT parse that way, so both paths are here:
    the structured one first, and the text one for anything else.

    THE FIRST FAILED RECIPIENT WINS. A DSN can name several and the guard
    records one address at a time; taking the first FAILURE rather than the
    first block is what stops a report whose first recipient succeeded from
    marking that working address dead.
    """
    for fields in _groups(part):
        action = fields.get("action", "").lower()
        if not any(action.startswith(a) for a in _FAILED_ACTIONS):
            continue
        recipient = _address(fields.get("final-recipient", "")
                             or fields.get("original-recipient", ""))
        status = fields.get("status", "")
        match = _STATUS.search(status)
        return _Report(is_bounce=True, recipient=recipient,
                       status=match.group(0) if match else status.strip(),
                       diagnostic=_tidy(fields.get("diagnostic-code", "")))
    return _Report()


def _groups(part: Message) -> list:
    """The field groups of a delivery-status part, however it was parsed."""
    payload = part.get_payload()
    if isinstance(payload, list):
        return [{name.strip().lower(): " ".join(str(value).split())
                 for name, value in group.items()} for group in payload]
    decoded = part.get_payload(decode=True)
    text = (decoded.decode("utf-8", "replace") if isinstance(decoded, bytes)
            else str(payload or ""))
    return [_fields(block)
            for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n"))]


def _fields(block: str) -> dict:
    """One field group, folded continuation lines included.

    A `Diagnostic-Code` is routinely three lines long, and reading only the
    first gives "smtp; 550-5.1.1 The email account that you tried to" — a
    sentence that stops before it says anything.
    """
    out: dict = {}
    name = ""
    for line in block.split("\n"):
        if not line.strip():
            continue
        if line[:1] in " \t" and name:
            out[name] = f"{out[name]} {line.strip()}"
            continue
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        out[name] = value.strip()
    return out


def _address(value: str) -> str:
    """`rfc822; someone@example.com` — the type label stripped off.

    The label is not optional in the RFC and is present in practice, but a
    value without one is read as the address itself rather than discarded.
    """
    value = (value or "").strip()
    if ";" in value:
        value = value.split(";", 1)[1]
    return value.strip().strip("<>").strip()


def _tidy(diagnostic: str) -> str:
    """The server's words, kept verbatim but without the transport label.

    Verbatim because it is what the composer shows a person who is about to
    write to the address again, and a paraphrase of "550 5.1.1 no such user"
    helps nobody. Truncated by the store, not here.
    """
    diagnostic = " ".join((diagnostic or "").split())
    if diagnostic.lower().startswith("smtp;"):
        diagnostic = diagnostic[5:].strip()
    return diagnostic


def _original_id(message: Message) -> str:
    """The Message-ID of the message that bounced.

    In the third part of a report — `message/rfc822` or `text/rfc822-headers` —
    and it is what links a failure to the thread it belongs to. Without it a
    bounce is a fact about an address; with it, it is a fact about a
    conversation.
    """
    for part in message.walk():
        kind = (part.get_content_type() or "").lower()
        if kind == "text/rfc822-headers":
            payload = part.get_payload(decode=True)
            text = (payload.decode("utf-8", "replace")
                    if isinstance(payload, bytes) else str(part.get_payload()))
            found = _fields(text.replace("\r\n", "\n")).get("message-id", "")
            if found:
                return found.strip()
        elif kind == "message/rfc822":
            inner = part.get_payload()
            candidate = inner[0] if isinstance(inner, list) and inner else None
            if candidate is not None and hasattr(candidate, "get"):
                found = str(candidate.get("Message-ID") or "").strip()
                if found:
                    return found
    return ""


def _heuristic(message: Message) -> Delivery:
    """A bounce with no report part. Deliberately narrow.

    BOTH conditions are required — a machine sender AND a subject that says so
    — because the cost of a false positive is asymmetric: a missed bounce is a
    message the user reads and understands, while a false one marks a working
    address dead and the composer then warns about it every time.

    No recipient is reported from this path even when one could be guessed out
    of the subject line. "This came back and I cannot tell you for whom" is the
    honest answer, and `store/ingest.py` uses `is_bounce` to show the row for
    what it is without touching the guard.
    """
    _name, addr = _sender(message)
    local = addr.split("@", 1)[0].lower() if addr else ""
    if local not in _DAEMON_LOCALS:
        return Delivery(is_bulk=is_bulk(message))
    subject = str(message.get("Subject") or "").lower()
    if not any(word in subject for word in
               ("undeliver", "returned to sender", "delivery status",
                "delivery failure", "failure notice", "mail delivery")):
        return Delivery(is_bulk=is_bulk(message))
    return Delivery(is_bulk=False, is_bounce=True,
                    original_message_id=_original_id(message))


def _sender(message: Message) -> tuple:
    import email.utils

    from_header = str(message.get("From") or "")
    name, addr = email.utils.parseaddr(from_header)
    return name, (addr or "").strip().lower()
