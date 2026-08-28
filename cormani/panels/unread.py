# SPDX-License-Identifier: GPL-3.0-or-later
#
# The one thing corMani reads out of somebody else's page.
#
# PLAN.txt §7's decision: "Web panels only. No protocol clients, no DOM
# automation beyond unread counts." This file is the whole of that exception,
# and it is written to be read at stage 9's audit in one sitting.
#
# WHAT IT DOES: evaluates one expression per site, in the page, on a timer, and
# takes a number out of it. WHAT IT DOES NOT DO: read a message, send one,
# click anything, follow a link, or look at any part of the document except the
# characters a person can already see in the browser tab.
#
# ── WHY THE TAB TITLE ──────────────────────────────────────────────────────
#
# All four sites put the count there — "(3) WhatsApp" — because that is how a
# browser tab tells you. It is the most durable thing on the page: a product
# decision rather than a layout one, unchanged across years of redesigns that
# have moved every other element. And it is the honest place to look, because
# reading it takes nothing a person is not already being shown.
#
# ── A PROBE THAT BREAKS MUST GO QUIET, NOT WRONG ───────────────────────────
#
# These are other people's pages and the markup changes without notice. Three
# things follow, and they are the design:
#
#   A count is None until a probe SUCCEEDS. Not zero — zero is an answer,
#   meaning "nothing is waiting", and a badge that says nothing is waiting
#   because the probe stopped working is the quiet wrong CONVENTIONS.txt §8 is
#   about.
#
#   A failed probe clears the count rather than keeping the last one. A stale
#   badge is a promise the panel cannot keep.
#
#   Nothing about a failure reaches the user as an error. The panel still
#   works; only the badge is gone. A messaging site that changed its title
#   format is not a fault the person reading this can act on.
#
# ── IT ONLY RUNS WHILE A PANEL EXISTS ──────────────────────────────────────
#
# The timer belongs to the panel, so a site nobody has opened is a site
# corMani never executes anything in. There is no background polling and no
# hidden page: the count for a closed panel is unknown, which is the truth.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import logging

log = logging.getLogger("cormani")

# How often a panel asks its page. Six seconds is a compromise with one real
# constraint on each side: a badge that lags a minute behind is not worth
# carrying, and evaluating an expression in a page costs a round trip through
# the render process, which is not free on a laptop with six panels open.
INTERVAL_MS = 6000

# Nothing above this is believed. A title that yields a five-digit "unread"
# count is a title that has come to mean something else — a video duration, a
# price, a year — and a badge reading 12,438 is worse than no badge.
SANE_MAXIMUM = 999


def believable(value) -> int | None:
    """The number a probe returned, if it can be believed. None otherwise.

    SEPARATE FROM THE PROBE ITSELF so that it can be tested without a browser,
    which is most of what is worth testing here: the JavaScript is four lines
    and the judgement about what to trust is the part that has opinions.
    """
    if isinstance(value, bool) or value is None:
        # `True` is an int in Python and would count as one unread message.
        return None
    if not isinstance(value, (int, float)):
        return None
    # NaN and the infinities are floats and `int()` RAISES on them. JavaScript
    # produces NaN readily — `parseInt` of anything unparseable is NaN — and
    # this runs inside a Qt slot, where an exception is printed and swallowed
    # rather than reaching a caller. The badge would then stop for good, with a
    # traceback on a terminal nobody is reading. Found by a test that passed
    # `float("nan")` on the way to asserting something else.
    if value != value or value in (float("inf"), float("-inf")):
        return None
    number = int(value)
    if number < 0 or number > SANE_MAXIMUM:
        return None
    return number


class Counter:
    """Asks one page, on a timer, and reports what it can believe.

    Owned by the panel and stopped with it. `on_count` is called with an int
    or with None, and None means "not known" rather than "none waiting" — the
    rail draws nothing for None and a zero badge for 0.
    """

    def __init__(self, site, page, on_count, *, interval_ms: int = INTERVAL_MS):
        self._site = site
        self._page = page
        self._on_count = on_count
        self._count: int | None = None
        self._timer = None
        self._interval = interval_ms
        self._failures = 0

    @property
    def count(self) -> int | None:
        return self._count

    def start(self) -> None:
        from PySide6.QtCore import QTimer

        if self._timer is not None:
            return
        self._timer = QTimer()
        self._timer.setInterval(self._interval)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def poll(self) -> None:
        """Ask once. Never raises — a page that is loading, gone, or refusing
        to evaluate anything is an ordinary state and not a fault."""
        if self._page is None:
            return
        try:
            self._page.runJavaScript(self._site.unread_js, 0, self._answer)
        except (RuntimeError, TypeError):
            # The page has been deleted under us, which happens when a panel
            # closes between one timer tick and the next.
            self.stop()

    def _answer(self, value) -> None:
        number = believable(value)
        if number is None:
            self._failures += 1
            if self._failures == 3:
                # Once, at the third consecutive failure, and never again:
                # something has changed on the site and the badge is gone. It
                # belongs in the log rather than in front of a person, who can
                # do nothing about somebody else's markup.
                log.info("unread probe for %s stopped matching; the badge is "
                         "gone and the panel is unaffected", self._site.key)
            self._set(None)
            return
        self._failures = 0
        self._set(number)

    def _set(self, number: int | None) -> None:
        if number == self._count:
            return
        self._count = number
        if self._on_count is not None:
            self._on_count(self._site.key, number)
