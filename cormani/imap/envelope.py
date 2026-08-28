# SPDX-License-Identifier: GPL-3.0-or-later
#
# An RFC 5322 message to the fields the store keeps.
#
# The input is whatever arrived. Mail from twenty years of correspondents
# includes headers in charsets that no longer exist, encoded words that are not
# valid base64, dates with no offset, and multiparts nested four deep with the
# text at the bottom. NOTHING HERE MAY RAISE. A message that cannot be parsed
# still has to appear in the list, because the alternative is a sync that stops
# on one bad message and never reaches the ten thousand behind it.
#
# Four decisions worth the words:
#
# COMPAT32, NOT THE MODERN POLICY. `email.policy.default` produces nicer header
# objects and raises on defects that compat32 tolerates. Since this module's
# whole job is tolerating defects, the forgiving parser plus explicit decoding
# here is the right way round; the modern policy's strictness would have to be
# caught and undone at every access.
#
# AN HTML-ONLY MESSAGE STILL GETS A body_text. The column is documented as the
# plain-text body, and the plain-text body of an HTML-only message is that HTML
# with its tags taken out. Leaving it empty would be honest about the wire and
# useless in practice: the preview line would be blank and the message would be
# invisible to search. The raw HTML is kept in `body_html` regardless, so
# nothing is lost and stage 3's sanitiser still has the original to render.
#
# THE STRIPPER HERE IS NOT A SANITISER AND MUST NEVER BE USED AS ONE. It exists
# to produce text for a preview and an index. Rendering is stage 3's, through a
# real sanitising view; CONVENTIONS.txt §7.
#
# message/rfc822 IS AN ATTACHMENT, NOT A SUBTREE. A forwarded message is one
# thing a person saves or opens, and every mail client presents it that way.
# Recursing into it would scatter its parts among the outer message's own.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import email.header
import email.utils
import html as html_module
import re
from dataclasses import dataclass, field
from email.message import Message
from email.parser import BytesParser
from email.policy import compat32

from . import delivery as delivery_mod

from ..store import subject as subject_mod

# Tried in order when a part declares no charset, or declares one this build
# does not have. latin-1 is last because it cannot fail, which ends the chain.
_CHARSET_FALLBACKS = ("utf-8", "cp1252", "latin-1")

_PREVIEW_LENGTH = 200


@dataclass(frozen=True)
class Part:
    """One attachment, with its bytes. The store writes them to disk."""

    filename: str
    content_type: str
    content_id: str
    size_bytes: int
    part_number: str
    is_inline: bool
    payload: bytes = b""


@dataclass(frozen=True)
class Envelope:
    """Everything a `message` row needs that did not come from IMAP itself.

    Flags, the UID and the folder come from the protocol; everything here came
    out of the bytes.
    """

    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    date_at: str | None = None
    from_name: str = ""
    from_addr: str = ""
    to_addrs: str = ""
    cc_addrs: str = ""
    bcc_addrs: str = ""
    reply_to: str = ""
    subject: str = ""
    subject_base: str = ""
    body_text: str = ""
    body_html: str = ""
    preview: str = ""
    size_bytes: int = 0
    parts: tuple[Part, ...] = field(default=())
    # What the message says about its OWN nature: bulk, or a delivery failure
    # and for whom. `imap/delivery.py` derives it, and it is here rather than
    # beside it because a `message` row carries both and `store/ingest.py`
    # writes the row from exactly one object.
    delivery: "delivery_mod.Delivery" = field(
        default_factory=lambda: delivery_mod.Delivery())

    @property
    def has_attachment(self) -> bool:
        """The paperclip. Inline images are not attachments to a reader — an
        HTML mail with a signature logo would otherwise show one on every row.
        """
        return any(not p.is_inline for p in self.parts)

    # THERE IS NO thread_key HERE, and there used to be. A thread is not a
    # property of a message: which conversation this one joins depends on what
    # the store already holds, and a reply can arrive before the message it
    # answers. `store/threads.py` decides it, at the moment the row is written,
    # from `message_id`, `in_reply_to` and `references` — which is everything
    # this class contributes to the question.


# --------------------------------------------------------------- decoding
def decode_words(value: str | None) -> str:
    """RFC 2047 encoded words to text. Never raises, never returns None."""
    if not value:
        return ""
    if isinstance(value, email.header.Header):               # pragma: no cover
        value = str(value)
    try:
        pieces = email.header.decode_header(value)
    except Exception:
        return _collapse(str(value))
    out: list[str] = []
    for raw, charset in pieces:
        if isinstance(raw, str):
            out.append(raw)
            continue
        out.append(_decode_bytes(raw, charset))
    # Encoded words abut without a space by design; the decoder gives them back
    # as separate pieces and joining with a space would insert one that the
    # sender did not write.
    return _collapse("".join(out))


def _decode_bytes(raw: bytes, charset: str | None) -> str:
    candidates = []
    if charset:
        charset = charset.strip().lower()
        # Two charset names that appear in the wild and are not codecs.
        if charset not in ("unknown-8bit", "x-unknown", "unknown", "none"):
            candidates.append(charset)
    candidates.extend(_CHARSET_FALLBACKS)
    for name in candidates:
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("latin-1", "replace")                  # pragma: no cover


def _collapse(text: str) -> str:
    """Headers may be folded across lines; a subject is one line to a reader."""
    return re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()


# The characters RFC 5322 requires a display name to be quoted for. The comma
# is the one that matters here: `Raman, Priya` unquoted turns one recipient
# into two the moment anything reads the list back.
_SPECIALS = re.compile(r'[][\\()<>@,:;".]')


def _format_address(name: str, addr: str) -> str:
    """`Name <addr>`, quoted where the name needs it.

    NOT `email.utils.formataddr`, which re-encodes a non-ASCII display name as
    an RFC 2047 encoded word. The store holds decoded text — René is stored as
    René — and encoding belongs on the wire, which is stage 4's composer.
    """
    if not name:
        return addr
    if not addr:
        return name
    if _SPECIALS.search(name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def _addresses(message: Message, header: str) -> str:
    """One header's addresses as `Name <addr>, Name <addr>`.

    The RFC form rather than bare addresses, because it is what a reply needs
    and what the list shows for a sent message. `store.messages.display_name`
    is the one place that reduces it to a name for display.
    """
    values = message.get_all(header)
    if not values:
        return ""
    try:
        pairs = email.utils.getaddresses([str(v) for v in values])
    except Exception:                                        # pragma: no cover
        return _collapse(", ".join(str(v) for v in values))
    out: list[str] = []
    for name, addr in pairs:
        name = decode_words(name)
        addr = _collapse(addr)
        if not addr and not name:
            continue
        out.append(_format_address(name, addr))
    return ", ".join(out)


def _one_address(message: Message, header: str) -> tuple[str, str]:
    values = message.get_all(header)
    if not values:
        return "", ""
    try:
        pairs = email.utils.getaddresses([str(v) for v in values])
    except Exception:                                        # pragma: no cover
        return _collapse(decode_words(str(values[0]))), ""
    for name, addr in pairs:
        if addr or name:
            return decode_words(name), _collapse(addr)
    return "", ""


def _date(message: Message) -> str | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        stamp = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if stamp is None:                                        # pragma: no cover
        return None
    if stamp.tzinfo is None:
        # RFC 5322 requires an offset. When one is missing there is no better
        # answer than UTC, and inventing the local zone would put a message an
        # hour out for every reader in a different one.
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def strip_subject(subject: str) -> str:
    """`Re: Fwd: Re: Invoice` to `Invoice`. store/subject.py holds the rule,
    because the composer needs the same answer and may not import this."""
    return subject_mod.strip_subject(subject)


# ------------------------------------------------------------------ bodies
# Tag names that only ever appear in markup. Deliberately NOT a naive `<...>`
# scan: a quoted reply is full of things like <priya@example.org>, and a plain
# message discussing arithmetic is full of `a < b > c`. Both match the naive
# form and neither is HTML.
_HTML_TAG = re.compile(
    r"</?(?:html|head|body|div|p|br|hr|table|tbody|thead|tr|td|th|span|img"
    r"|ul|ol|li|h[1-6]|font|center|strong|em|b|i|u|style|meta|link|a)\b[^>]*>",
    re.IGNORECASE)

# Eight is high enough that prose quoting a few tags is safe, low enough to
# catch a real mailer's output. The observed case had 926.
_HTML_THRESHOLD = 8


def looks_like_html(text: str) -> bool:
    """Whether a part DECLARED text/plain is in fact markup.

    Senders do this. A marketing mailer observed in the wild put 72 KB
    containing 926 tags into the plain half of a multipart/alternative, which
    is legal, useless, and would put raw markup on the list row's preview line
    and `div`, `td` and `span` into the search index.

    Conservative on purpose. Being wrong in one direction costs a preview that
    keeps a few stray angle brackets; being wrong in the other silently
    rewrites someone's genuine plain text.
    """
    if not text:
        return False
    opening = text[:4096].lower()
    if "<html" in opening or "<body" in opening or "<!doctype html" in opening:
        return True
    return len(_HTML_TAG.findall(text)) >= _HTML_THRESHOLD


_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style|head|title)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BREAKS = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h[1-6])\b[^>]*>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


def text_from_html(html: str) -> str:
    """HTML to readable text, for a preview and for the search index.

    NOT a sanitiser. Nothing here is safe to render — it exists to produce
    characters, and stage 3 owns rendering. See the module header.
    """
    if not html:
        return ""
    text = _SCRIPT_OR_STYLE.sub(" ", html)
    text = _BREAKS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def _normalise_newlines(text: str) -> str:
    """CRLF and bare CR to LF.

    The wire uses CRLF and a body rendered from HTML uses LF, so without this
    the column holds two conventions depending on where the text happened to
    come from — measured at 58 of 77 on a live account. Every reader
    downstream would have to strip them: the reading pane, stage 4's quoting,
    any export. `body_html` is NOT touched; it is the original document and
    stage 3 renders it as it arrived.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    return _decode_bytes(payload, part.get_content_charset())


# The text subtypes that are a message BODY. Everything else that calls itself
# text is a part — and `text/calendar` is why this list exists rather than a
# maintype test. Google puts the invitation for a meeting in a bare
# `text/calendar` inside the multipart/alternative, with no filename and no
# disposition, so a rule that read "text is a body" made the LAST alternative
# — the iCalendar source — the body of the message. Every invitation from
# Google previewed as `BEGIN:VCALENDAR`, and the reading pane showed it.
_BODY_SUBTYPES = ("plain", "html")


def _is_attachment(part: Message) -> bool:
    disposition = str(part.get("Content-Disposition") or "").strip().lower()
    if disposition.startswith("attachment"):
        return True
    if part.get_filename():
        return True
    if part.get_content_maintype() == "text":
        return part.get_content_subtype() not in _BODY_SUBTYPES
    return True


def _walk(part: Message, number: str, out: list[tuple[str, Message]]) -> None:
    """Number the parts the way IMAP does, without recursing into a forward."""
    if part.get_content_maintype() == "multipart" and part.is_multipart():
        children = part.get_payload()
        if isinstance(children, list):
            for n, child in enumerate(children, start=1):
                _walk(child, f"{number}.{n}" if number else str(n), out)
            return
    out.append((number or "1", part))


def _preview_from(text: str) -> str:
    """The grey third line of a list row.

    Quoted lines are dropped first, because a one-line reply above four
    screenfuls of quotation would otherwise preview as the quotation. If that
    leaves nothing — a message that IS a quotation — the unfiltered text is
    used rather than showing an empty row.
    """
    if not text:
        return ""
    kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    body = " ".join(" ".join(kept).split()) or " ".join(text.split())
    return body[:_PREVIEW_LENGTH].strip()


# -------------------------------------------------------------------- read
def read(raw: bytes) -> Envelope:
    """Parse one message. Returns an Envelope whatever the input is."""
    if not raw:
        return Envelope()
    try:
        message = BytesParser(policy=compat32).parsebytes(raw)
    except Exception:                                        # pragma: no cover
        return Envelope(size_bytes=len(raw),
                        subject="(unreadable message)",
                        subject_base="(unreadable message)")

    numbered: list[tuple[str, Message]] = []
    _walk(message, "", numbered)

    plain: list[str] = []
    html: list[str] = []
    parts: list[Part] = []
    for number, part in numbered:
        if _is_attachment(part):
            payload = part.get_payload(decode=True) or b""
            disposition = str(part.get("Content-Disposition") or "").lower()
            content_id = str(part.get("Content-ID") or "").strip().strip("<>")
            parts.append(Part(
                filename=decode_words(part.get_filename() or ""),
                content_type=(part.get_content_type() or "").lower(),
                content_id=content_id,
                size_bytes=len(payload),
                part_number=number,
                # Inline means "displayed within the message", which is what
                # having a Content-ID that the HTML refers to amounts to.
                is_inline=("inline" in disposition) or bool(content_id),
                payload=payload))
            continue
        subtype = part.get_content_subtype()
        if subtype == "html":
            html.append(_part_text(part))
        else:
            plain.append(_part_text(part))

    # The LAST alternative is the richest one — MIME orders them worst first.
    body_html = html[-1].strip() if html else ""
    body_text = plain[-1].strip() if plain else ""
    if not body_text and body_html:
        body_text = text_from_html(body_html)
    elif body_text and looks_like_html(body_text):
        # The sender declared plain and sent markup. Rendered from the real
        # HTML part where there is one, since that is the better source, and
        # from the mislabelled part itself where there is not.
        body_text = text_from_html(body_html or body_text)

    subject = decode_words(message.get("Subject"))
    from_name, from_addr = _one_address(message, "From")
    references = _collapse(str(message.get("References") or ""))

    return Envelope(
        message_id=_collapse(str(message.get("Message-ID") or "")),
        in_reply_to=_collapse(str(message.get("In-Reply-To") or "")),
        references=" ".join(references.split()),
        date_at=_date(message),
        from_name=from_name,
        from_addr=from_addr,
        to_addrs=_addresses(message, "To"),
        cc_addrs=_addresses(message, "Cc"),
        bcc_addrs=_addresses(message, "Bcc"),
        reply_to=_addresses(message, "Reply-To"),
        subject=subject,
        subject_base=strip_subject(subject),
        body_text=_normalise_newlines(body_text),
        body_html=body_html,
        preview=_preview_from(body_text),
        size_bytes=len(raw),
        parts=tuple(parts),
        delivery=delivery_mod.read(message))
