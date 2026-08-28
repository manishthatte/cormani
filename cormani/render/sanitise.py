# SPDX-License-Identifier: GPL-3.0-or-later
#
# The HTML sanitiser. CONVENTIONS.txt §7, and the only thing standing between
# a stranger's markup and the widget that draws it.
#
# ALLOWLIST, NEVER BLOCKLIST. Every tag, every attribute and every URL scheme
# is refused unless it is named here. A blocklist is a list of the attacks
# somebody thought of, and the interesting ones are always the other kind:
# `<sVg/onload=…>`, `<img src=x onerror=…>`, `<a href="jAvAsCrIpT:…">`,
# `<style>@import url(…)</style>`. None of those need to be enumerated to be
# stopped, because none of them are on the list.
#
# DROPPED WITH CONTENTS, versus UNWRAPPED. Two different treatments and
# confusing them is a hole. `<script>`, `<style>`, `<svg>` and friends have
# their CONTENTS discarded as well as their tags, because the contents are the
# payload. `<html>`, `<body>`, `<font>` and anything unrecognised are UNWRAPPED
# — the tag goes, the text inside stays — because the text is the message and
# throwing it away loses mail.
#
# CSS IS FILTERED, NOT PASSED THROUGH AND NOT DISCARDED. Discarding it makes
# every HTML message look like a ransom note, which fails the stage's own test
# of being better than Thunderbird's. Passing it through admits `url(…)`, which
# is a network request, `expression(…)`, which is script on old engines, and
# `position:fixed`, which lets a message escape its own frame. So: a property
# allowlist, and a value pattern that permits no parentheses call, no scheme
# and no `@` rule.
#
# REMOTE CONTENT IS WITHHELD, AND SAYING SO IS PART OF THE JOB. A tracking
# pixel is a disclosure: loading it tells the sender the message was opened,
# when, and from which address. The count of what was withheld is returned so
# the interface can offer to load it, rather than silently deciding for the
# user in either direction.
#
# THIS IS THE FIRST OF THREE BARRIERS, NOT THE ONLY ONE. The view disables
# scripting and refuses every network request the engine attempts, and
# `document()` emits a Content-Security-Policy as well. Any one of the three
# should be enough; none of them is trusted to be.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Kept whole: tag and text. Layout and emphasis, nothing that executes or
# fetches. `img` is here because its `src` is filtered separately and
# thoroughly; every other fetching element is absent by design.
ALLOWED_TAGS = frozenset("""
    a abbr address article aside b bdi bdo big blockquote br caption center
    cite code col colgroup dd del dfn div dl dt em fieldset figcaption figure
    font footer h1 h2 h3 h4 h5 h6 header hgroup hr i img ins kbd legend li
    main mark nav ol p pre q s samp section small span strike strong sub
    summary sup table tbody td tfoot th thead time tr tt u ul var wbr
""".split())

# Dropped with their CONTENTS, because the contents are the payload.
DROPPED_SUBTREES = frozenset("""
    script style iframe frame frameset object embed applet form input button
    select option optgroup textarea label output link meta base noscript svg
    math template head title canvas audio video source track map area param
    marquee blink portal dialog slot xml
""".split())

# Written without a closing tag.
VOID_TAGS = frozenset("area base br col embed hr img input link meta param "
                      "source track wbr".split())

# Per-tag attribute allowlists. Anything not named is dropped, which is what
# makes `onclick`, `onerror`, `srcset`, `formaction` and `xlink:href` a
# non-problem rather than five separate rules.
_GLOBAL_ATTRS = frozenset({"title", "dir", "lang", "align", "valign", "style"})
_ATTRS = {
    "a": {"href", "name"},
    "img": {"src", "alt", "width", "height", "border"},
    "td": {"colspan", "rowspan", "width", "height", "bgcolor", "nowrap"},
    "th": {"colspan", "rowspan", "width", "height", "bgcolor", "nowrap"},
    "table": {"width", "border", "cellpadding", "cellspacing", "bgcolor"},
    "tr": {"bgcolor"},
    "col": {"span", "width"},
    "colgroup": {"span", "width"},
    "ol": {"start", "type"},
    "ul": {"type"},
    "li": {"value"},
    "font": {"color", "size", "face"},
    "blockquote": {"cite"},
    "q": {"cite"},
    "time": {"datetime"},
    "hr": {"width", "size", "noshade"},
}

# The only schemes a link may use. `mailto` is here because a message full of
# addresses that cannot be clicked is worse mail than one that can.
_LINK_SCHEMES = ("http://", "https://", "mailto:", "tel:")

# Self-contained image data, which discloses nothing. SVG is excluded on
# purpose: it is a document format that can carry script, not an image format.
_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp|bmp);base64,[A-Za-z0-9+/=\s]+$",
                         re.IGNORECASE)

# CSS properties worth keeping. Everything structural that could move content
# out of its own box — position, z-index, transform, content, behavior — is
# absent, which is the point.
_STYLE_PROPERTIES = frozenset("""
    color background-color font font-family font-size font-style font-weight
    font-variant line-height letter-spacing word-spacing text-align
    text-decoration text-indent text-transform vertical-align white-space
    margin margin-top margin-bottom margin-left margin-right
    padding padding-top padding-bottom padding-left padding-right
    border border-top border-bottom border-left border-right border-color
    border-style border-width border-radius border-collapse border-spacing
    width height max-width min-width display list-style list-style-type
    direction opacity
""".split())

# A declaration's value may be words, numbers, units, hashes, commas, quotes
# and percentages. It may NOT contain a bracket — which excludes url(),
# expression() and every other functional notation in one stroke — nor a colon
# or semicolon, which excludes an embedded scheme or a second declaration.
_SAFE_VALUE = re.compile(r"^[#%\w\s.,'\"/+-]*$")

_MAX_STYLE_DECLARATIONS = 32

_LEADING_NUMBER = re.compile(r"^\s*(-?\d*\.?\d+)")


def _is_zero(value: str) -> bool:
    """Whether a length is zero or negative, whatever unit it carries."""
    m = _LEADING_NUMBER.match(value)
    if not m:
        return False
    try:
        return float(m.group(1)) <= 0
    except ValueError:                                       # pragma: no cover
        return False


@dataclass(frozen=True)
class Result:
    """Sanitised markup, and an honest account of what was removed."""

    html: str = ""
    blocked_remote: int = 0
    stripped: tuple = field(default=())

    @property
    def had_remote(self) -> bool:
        return self.blocked_remote > 0


def _safe_style(value: str) -> str:
    """Filter a style attribute to the allowlisted properties, or empty."""
    kept = []
    for declaration in value.split(";")[:_MAX_STYLE_DECLARATIONS]:
        name, sep, val = declaration.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        val = val.strip()
        if name not in _STYLE_PROPERTIES or not val:
            continue
        if not _SAFE_VALUE.match(val):
            continue
        if name == "font-size" and _is_zero(val):
            # A zero font size is never something a reader is meant to see. It
            # is the oldest hidden-text trick — words a spam filter reads and a
            # person does not — and Qt complains about it once per element,
            # which was how it was noticed: ninety warnings from seventy-seven
            # real messages.
            continue
        kept.append(f"{name}: {val}")
    return "; ".join(kept)


def _safe_link(value: str) -> str:
    """A href we are willing to draw, or empty.

    Whitespace and control characters are removed BEFORE the scheme is
    examined: `java\\tscript:alert(1)` is a working URL in more engines than it
    should be, and it does not start with `javascript:` until it is cleaned.
    """
    cleaned = re.sub(r"[\s\x00-\x20]", "", value or "")
    lowered = cleaned.lower()
    if lowered.startswith("#"):
        return ""                    # an anchor into a document we do not have
    if any(lowered.startswith(scheme) for scheme in _LINK_SCHEMES):
        return cleaned
    return ""


class _Sanitiser(HTMLParser):
    def __init__(self, *, allow_remote: bool, cid_map: dict) -> None:
        # convert_charrefs leaves text alone as much as possible; entities are
        # re-escaped on the way out, so a `&lt;` in the source stays one.
        super().__init__(convert_charrefs=True)
        self.allow_remote = allow_remote
        self.cid_map = {k.strip().strip("<>"): v for k, v in (cid_map or {}).items()}
        self.out: list = []
        self.open_tags: list = []
        self.blocked = 0
        self.stripped: set = set()
        self._skip_depth = 0
        self._skip_tag = ""

    # ---------------------------------------------------------------- tags
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in DROPPED_SUBTREES:
            self.stripped.add(tag)
            if tag not in VOID_TAGS:
                self._skip_tag, self._skip_depth = tag, 1
            return
        if tag not in ALLOWED_TAGS:
            # Unwrapped: the tag goes and the text inside stays. Throwing the
            # contents away here would lose the message itself for anything
            # wrapped in an element nobody anticipated.
            self.stripped.add(tag)
            return
        rendered = self._attributes(tag, attrs)
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{rendered}>")
            return
        self.out.append(f"<{tag}{rendered}>")
        self.open_tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_depth:
            return
        if tag in DROPPED_SUBTREES or tag not in ALLOWED_TAGS:
            self.stripped.add(tag)
            return
        self.out.append(f"<{tag}{self._attributes(tag, attrs)}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if not self._skip_depth:
                    self._skip_tag = ""
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag not in self.open_tags:
            return                   # a close with no open; the browser's job
        # Close everything opened inside it, so that unbalanced input cannot
        # leave the document nested a thousand deep.
        while self.open_tags:
            inner = self.open_tags.pop()
            self.out.append(f"</{inner}>")
            if inner == tag:
                break

    # --------------------------------------------------------------- text
    def handle_data(self, data):
        if not self._skip_depth and data:
            self.out.append(html_module.escape(data, quote=False))

    def handle_comment(self, data):
        # Dropped entirely. A conditional comment carries markup that some
        # engines act on, which makes a comment a hiding place rather than a
        # note.
        return

    def handle_decl(self, decl):
        return

    def handle_pi(self, data):
        return

    def unknown_decl(self, data):
        return

    # --------------------------------------------------------- attributes
    def _attributes(self, tag: str, attrs) -> str:
        allowed = _ATTRS.get(tag, frozenset()) | _GLOBAL_ATTRS
        parts = []
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name not in allowed:
                continue
            if name == "style":
                value = _safe_style(value)
            elif name == "href":
                value = _safe_link(value)
            elif name == "src":
                value = self._image(value)
            elif name in ("cite", "datetime"):
                value = _safe_link(value) if name == "cite" else value
            if not value:
                continue
            parts.append(f' {name}="{html_module.escape(value, quote=True)}"')
        return "".join(parts)

    def _image(self, value: str) -> str:
        """An image source: inline data, a stored attachment, or withheld."""
        cleaned = re.sub(r"[\s\x00-\x20]", "", value or "")
        lowered = cleaned.lower()
        if lowered.startswith("cid:"):
            # An image the message carried with it. Already on this disk, so
            # showing it discloses nothing.
            target = self.cid_map.get(cleaned[4:].strip("<>"))
            return target or ""
        if lowered.startswith("data:"):
            return cleaned if _DATA_IMAGE.match(cleaned) else ""
        if lowered.startswith(("http://", "https://")):
            if self.allow_remote:
                return cleaned
            self.blocked += 1
            return ""
        return ""

    def result(self) -> Result:
        while self.open_tags:
            self.out.append(f"</{self.open_tags.pop()}>")
        return Result(html="".join(self.out), blocked_remote=self.blocked,
                      stripped=tuple(sorted(self.stripped)))


def sanitise(html: str, *, allow_remote: bool = False,
             cid_map: dict | None = None) -> Result:
    """Make a message body safe to draw. Never raises.

    A message that cannot be parsed still has to be readable, because the
    alternative is a reading pane that shows nothing and a person who cannot
    tell whether the mail is broken or corMani is.
    """
    if not html:
        return Result()
    parser = _Sanitiser(allow_remote=allow_remote, cid_map=cid_map or {})
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # HTMLParser is forgiving, but not infinitely. Whatever was understood
        # before the failure is kept and closed off.
        pass
    return parser.result()


# --------------------------------------------------------------- the page
#
# The stylesheet does three jobs and only three: make the message legible at
# the reader's chosen colours, stop a wide table forcing the pane wider than
# the window, and draw a placeholder where a withheld image used to be. It
# deliberately does NOT try to normalise the sender's design.
_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<style>
  html, body {{ margin: 0; padding: 0; }}
  body {{
    font-family: {font}; font-size: {size}pt;
    color: {fg}; background: {bg};
    padding: 12px 16px; overflow-wrap: break-word;
  }}
  a {{ color: {link}; }}
  /* A sender's table sized for a 900px newsletter must not widen the pane. */
  img, table, pre {{ max-width: 100%; }}
  img {{ height: auto; }}
  table {{ table-layout: fixed; }}
  pre {{ white-space: pre-wrap; }}
  blockquote {{
    margin: 0 0 0 8px; padding-left: 10px;
    border-left: 2px solid {quote}; color: {quote};
  }}
  /* Where an image was withheld. It keeps its place so the layout holds and
     the reader can see that something is missing rather than wondering. */
  img:not([src]) {{
    display: inline-block; min-width: 40px; min-height: 16px;
    border: 1px dashed {quote}; background: transparent;
  }}
</style></head><body>{body}</body></html>"""

# `default-src 'none'` refuses everything, then two exceptions are made:
# inline CSS, because the stylesheet above is inline, and images from sources
# that cannot reach the network. When remote content HAS been asked for, http
# and https are added — and only then.
_CSP_BLOCKED = "default-src 'none'; style-src 'unsafe-inline'; img-src data: file:"
_CSP_ALLOWED = ("default-src 'none'; style-src 'unsafe-inline'; "
                "img-src data: file: http: https:")


def document(result: Result, *, palette: dict | None = None,
             allow_remote: bool = False) -> str:
    """Wrap a sanitised fragment in a page the view can load.

    The Content-Security-Policy is the THIRD barrier, after the allowlist above
    and the request interceptor in the view. It is here rather than only in the
    view because it travels with the document: anything that ever renders this
    string, in any context, carries its own refusal with it.
    """
    palette = palette or {}
    return _PAGE.format(
        csp=_CSP_ALLOWED if allow_remote else _CSP_BLOCKED,
        font=palette.get("font", "system-ui, sans-serif"),
        size=palette.get("size", 10),
        fg=palette.get("fg", "#073642"),
        bg=palette.get("bg", "#fdf6e3"),
        link=palette.get("link", "#268bd2"),
        quote=palette.get("quote", "#93a1a1"),
        body=result.html or "")


def plain_document(text: str, *, palette: dict | None = None) -> str:
    """A message with no HTML at all, shown as what it is.

    Escaped and wrapped in `<pre>` rather than converted to markup: the sender
    wrote plain text, and its line breaks and alignment ARE the formatting.
    """
    escaped = html_module.escape(text or "", quote=False)
    return document(Result(html=f"<pre>{escaped}</pre>"), palette=palette)
