# SPDX-License-Identifier: GPL-3.0-or-later
#
# The sites, as data.
#
# CONVENTIONS.txt §4: adding a channel, a provider or a view should be one file
# plus one line in a registry. This is that registry, and a site is a row in it
# — a name, a URL, a profile to keep its session in, and one expression that
# counts what is unread. There is no per-site class and there is not meant to
# be one.
#
# ── WHY THESE ARE PANELS AND NOT CLIENTS ───────────────────────────────────
#
# PLAN.txt §1 states it and it is the whole design: WhatsApp, LinkedIn, X and
# Facebook have NO API for personal messaging. Not restricted, not paid —
# absent. What exists is a web application that a person signs into.
#
# So corMani shows that web application, and does nothing else with it.
# PLAN.txt §7's decision reads "Web panels only. No protocol clients, no DOM
# automation beyond unread counts", and both halves of that sentence are load-
# bearing. A reverse-engineered protocol client — Baileys for WhatsApp, the
# Voyager endpoints for LinkedIn — gets accounts BANNED, and the account is
# the thing being protected. So does automation that reads or sends messages.
# The unread count is the one exception and it is deliberately the smallest
# one: a number that is already on the screen, read from the DOM, so the rail
# can carry a badge.
#
# ── THE UNREAD PROBE IS A STRING AND ITS LIMITS ARE THE POINT ──────────────
#
# One JavaScript expression per site, evaluated in the page, returning a
# number. It reads the title or an aria-label — the same characters a person
# reads — and touches nothing else. It is a string in this table rather than
# code in a module so that the whole of what corMani executes inside somebody
# else's page is visible in one screenful and can be audited at stage 9.
#
# THEY WILL BREAK, AND BREAKING MUST BE HARMLESS. These are other people's
# pages and the markup changes without notice. A probe that fails returns
# null, the badge disappears, and the panel is unaffected — `panels/unread.py`
# is where that guarantee lives. A count that silently went stale would be
# worse than no count, so a probe that stops matching stops reporting.
#
# ── THE DOCUMENT TITLE IS THE MOST DURABLE PLACE TO LOOK ───────────────────
#
# Every one of these sites puts its unread count in `document.title` — "(3)
# WhatsApp" — because that is how a browser tab tells you. It is the part of a
# web application that changes LEAST, because it is a product decision rather
# than a layout one, and it is visible to a person, which is the test of
# whether reading it is reasonable at all.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from dataclasses import dataclass

# Reading a leading "(12)" from the tab title, which is where all four of these
# put it. Written once and shared, because four copies of one regular
# expression is four places for it to be subtly different.
_TITLE_COUNT = """
(function () {
  var m = /^\\s*\\((\\d+)\\)/.exec(document.title || "");
  return m ? parseInt(m[1], 10) : 0;
})()
"""


@dataclass(frozen=True)
class Site:
    """One site, and everything corMani knows about it."""

    key: str
    name: str
    url: str
    # What the panel says while nobody is signed in. Written per site because
    # the sign-in is different in each and "not signed in" alone leaves a
    # person looking for a button that is on the page in front of them.
    hint: str
    unread_js: str = _TITLE_COUNT
    # Sites whose sign-in genuinely needs a second window. A panel that
    # silently swallowed the popup would be a panel nobody can sign into.
    allows_popups: bool = True
    # Off by default. PLAN.txt §5's stage 7 line calls the webmail panels
    # OPTIONAL, and they are the two a person may not want at all: corMani
    # already holds this mail over IMAP, and the panel is for the day the
    # provider's own interface is the only one that will do.
    default_on: bool = True

    @property
    def profile_name(self) -> str:
        """The storage name, which IS the isolation.

        A QWebEngineProfile constructed with a name is persistent and its
        storage and cache directories derive from it — so one name per site is
        one cookie jar per site, with nothing to configure. See
        docs/toolkit-verification.txt, finding 5.
        """
        return f"site-{self.key}"


SITES: tuple = (
    Site(key="whatsapp", name="WhatsApp", url="https://web.whatsapp.com/",
         hint="WhatsApp Web signs in by scanning a code with the telephone: "
              "open WhatsApp there, then Linked devices."),
    Site(key="linkedin", name="LinkedIn",
         url="https://www.linkedin.com/messaging/",
         hint="Sign in with the address and password LinkedIn knows. It may "
              "ask for a code by email the first time."),
    Site(key="x", name="X", url="https://x.com/messages",
         hint="Sign in as usual. Direct messages are the only part of X "
              "corMani opens."),
    Site(key="facebook", name="Facebook",
         url="https://www.facebook.com/messages/t/",
         hint="Sign in as usual. This opens Messenger inside Facebook rather "
              "than the separate Messenger site."),
    Site(key="telegram", name="Telegram Web",
         url="https://web.telegram.org/",
         hint="Sign in with the phone number Telegram knows. A code arrives "
              "by SMS or in the Telegram app on your phone."),
    Site(key="signal", name="Signal",
         url="https://signal.org/",
         hint="Signal has no full web client for messaging. This opens "
              "signal.org — use Signal Desktop or your phone for messages.",
         default_on=False),
    # The two optional webmail panels. Mail from these accounts is already in
    # corMani over IMAP; the panel is for the occasion when only the provider's
    # own interface will do — a Google Doc shared in a mail, a Teams invitation
    # that will not open anywhere else.
    Site(key="gmail", name="Gmail (web only)", url="https://mail.google.com/",
         hint="The web interface for a Google account. corMani already holds "
              "this mail over IMAP; this is for what only the web can do.",
         default_on=False),
    Site(key="outlook", name="Outlook (web only)",
         url="https://outlook.office.com/mail/",
         hint="The web interface for a Microsoft account. corMani already "
              "holds this mail over IMAP.",
         default_on=False),
)

BY_KEY = {site.key: site for site in SITES}


def get(key: str) -> Site | None:
    return BY_KEY.get((key or "").strip().lower())


def default_keys() -> list:
    """The sites a fresh installation shows. See `Site.default_on`."""
    return [site.key for site in SITES if site.default_on]


def rail_key(site: Site | str) -> str:
    """How the rail names this site's row. One spelling, one place."""
    key = site.key if isinstance(site, Site) else str(site)
    return f"site:{key}"


def from_rail_key(key: str) -> Site | None:
    prefix = "site:"
    if not (key or "").startswith(prefix):
        return None
    return get(key[len(prefix):])
