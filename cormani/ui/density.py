# SPDX-License-Identifier: GPL-3.0-or-later
#
# Row metrics, in one place.
#
# Density is Thunderbird's setting and it is not cosmetic: at compact a screen
# holds half again as many messages, and someone triaging fifteen accounts wants
# that. Getting it right means one rule — THE DELEGATE OWNS ROW HEIGHT. Density
# is a set of numbers the delegate reads when it measures and paints; it is
# never a stylesheet change, never a font size set on a widget, and never a
# fixed row height set on a view.
#
# The reason is that the three-line row is drawn, not laid out by Qt. Its height
# is three font ascents plus two gaps plus padding, and only the code that draws
# it can know that. A stylesheet that set a row height would be a fourth opinion
# about the same number, and the one that disagreed would win silently.
#
# There is no Qt in this module on purpose. These are numbers and a little
# arithmetic, so the metrics can be tested without a display and a font.
#
# Compact drops the preview line rather than shrinking it. A preview squeezed to
# five pixels of leading is unreadable and still costs the space; dropping it
# gives Outlook's two-line row, which is a real design rather than a broken
# three-line one.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Density:
    key: str
    name: str

    pad_v: int          # above and below the row's text block
    pad_h: int          # left and right inside the row
    line_gap: int       # between one text line and the next
    lines: int          # 3 normally; 2 at compact, where the preview goes
    swatch: int         # width of the account colour bar at the left edge
    gutter: int         # space between that bar and the text
    icon: int           # square size for a status or hover-action icon
    icon_gap: int       # between two icons in a row of them
    tag_height: int     # the little tag chips on the subject line

    # Point-size offsets from the application font. Negative shrinks. The
    # preview is smaller than the subject at every density because it is the
    # line the eye should skip when it is not wanted.
    sender_pt: int
    subject_pt: int
    preview_pt: int

    @property
    def shows_preview(self) -> bool:
        return self.lines >= 3


COMPACT = Density(
    key="compact", name="Compact",
    pad_v=3, pad_h=8, line_gap=1, lines=2, swatch=3, gutter=7,
    icon=13, icon_gap=4, tag_height=12,
    sender_pt=0, subject_pt=0, preview_pt=-1)

NORMAL = Density(
    key="normal", name="Normal",
    pad_v=6, pad_h=10, line_gap=2, lines=3, swatch=3, gutter=9,
    icon=15, icon_gap=6, tag_height=14,
    sender_pt=0, subject_pt=0, preview_pt=-1)

RELAXED = Density(
    key="relaxed", name="Relaxed",
    pad_v=10, pad_h=13, line_gap=4, lines=3, swatch=4, gutter=12,
    icon=17, icon_gap=8, tag_height=16,
    sender_pt=1, subject_pt=0, preview_pt=-1)

DENSITIES: dict[str, Density] = {d.key: d for d in (COMPACT, NORMAL, RELAXED)}

DEFAULT_DENSITY = NORMAL.key

# The rail is denser than the list at every setting — it holds fifteen accounts
# plus their folders, and matching the list's padding would push the last
# account off a laptop screen.
RAIL_ROW_PADDING = {"compact": 2, "normal": 4, "relaxed": 7}


def get(key: str | None) -> Density:
    """Resolve a name, falling back rather than raising. Same reasoning as
    ui/theme.get: a setting written by a newer version must not stop the window
    opening."""
    return DENSITIES.get((key or "").strip().lower(), DENSITIES[DEFAULT_DENSITY])


def row_height(density: Density, line_heights: Sequence[int]) -> int:
    """The height of a message row, given the height of each text line.

    Takes the measurements rather than the fonts so it stays free of Qt, and so
    a test can assert the arithmetic without depending on which fonts happen to
    be installed on the machine running it.
    """
    used = list(line_heights)[:density.lines]
    if not used:
        return density.pad_v * 2
    gaps = density.line_gap * (len(used) - 1)
    # An icon taller than the text must not be clipped: a row is at least tall
    # enough for the hover actions it has to be able to show.
    content = max(sum(used) + gaps, density.icon)
    return content + density.pad_v * 2


def rail_row_height(density: Density, line_height: int) -> int:
    padding = RAIL_ROW_PADDING.get(density.key, RAIL_ROW_PADDING[DEFAULT_DENSITY])
    return max(line_height, density.icon) + padding * 2
