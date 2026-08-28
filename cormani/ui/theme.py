# SPDX-License-Identifier: GPL-3.0-or-later
#
# Colour.
#
# Themes are DATA, not code: a `Theme` is sixteen colour roles and a name, and
# adding one is a single entry in `THEMES`. Nothing else in the interface may
# name a colour — widgets ask for a role. That is the difference between a
# themeable application and one with a dark mode bolted on, and it is only
# cheap while there is no interface yet.
#
# The roles are semantic rather than literal. `surface` and `surface_raised`,
# not `white` and `light_grey`: a role survives inversion, a colour name does
# not. Solarized is the reason this matters here — its light and dark variants
# are the same sixteen values with the greys reversed, which only works if the
# code asks for meaning.
#
# Solarized Light is the default because it was asked for. `system` is kept
# alongside it and is not an afterthought: a desktop where the user has chosen
# a colour scheme, or needs a high-contrast one, must be able to say so and be
# obeyed. A client that insists on its own palette is one an accessibility
# setting cannot reach.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    """Sixteen roles. Every colour the interface may use."""

    key: str
    name: str
    dark: bool

    surface: str            # window and pane background
    surface_raised: str     # rows, headers, selected backgrounds
    surface_sunken: str     # the rail, the status bar
    border: str

    text: str               # body text
    text_strong: str        # subjects, sender names, anything weighted
    text_muted: str         # dates, previews, counts
    text_inverse: str       # text on an accent fill

    accent: str             # selection, links, the active rail row
    accent_muted: str       # hover, the selected row when unfocused

    unread: str             # the unread marker and its bold text
    flagged: str
    owed: str               # a thread where they replied last and you did not
    deadline: str           # a date that cannot slip
    error: str


# Ethan Schoonover's sixteen values, unmodified. Light and dark differ only in
# which end of the grey ramp is background — which is exactly why the roles
# above are semantic.
_S = {
    "base03": "#002b36", "base02": "#073642", "base01": "#586e75",
    "base00": "#657b83", "base0": "#839496", "base1": "#93a1a1",
    "base2": "#eee8d5", "base3": "#fdf6e3",
    "yellow": "#b58900", "orange": "#cb4b16", "red": "#dc322f",
    "magenta": "#d33682", "violet": "#6c71c4", "blue": "#268bd2",
    "cyan": "#2aa198", "green": "#859900",
}

SOLARIZED_LIGHT = Theme(
    key="solarized-light", name="Solarized Light", dark=False,
    surface=_S["base3"], surface_raised=_S["base2"], surface_sunken=_S["base2"],
    border="#ddd6c1",
    text=_S["base00"], text_strong=_S["base01"], text_muted=_S["base1"],
    text_inverse=_S["base3"],
    accent=_S["blue"], accent_muted="#d6e6f4",
    unread=_S["blue"], flagged=_S["yellow"], owed=_S["cyan"],
    deadline=_S["orange"], error=_S["red"],
)

SOLARIZED_DARK = Theme(
    key="solarized-dark", name="Solarized Dark", dark=True,
    surface=_S["base03"], surface_raised=_S["base02"], surface_sunken=_S["base02"],
    border="#0b4552",
    text=_S["base0"], text_strong=_S["base1"], text_muted=_S["base01"],
    text_inverse=_S["base03"],
    accent=_S["blue"], accent_muted="#0d4a63",
    unread=_S["blue"], flagged=_S["yellow"], owed=_S["cyan"],
    deadline=_S["orange"], error=_S["red"],
)

# Not a palette: a marker meaning "do not touch the palette at all". Applying it
# leaves whatever the desktop chose, including a high-contrast scheme.
SYSTEM = Theme(
    key="system", name="System", dark=False,
    surface="", surface_raised="", surface_sunken="", border="",
    text="", text_strong="", text_muted="", text_inverse="",
    accent="", accent_muted="",
    unread="", flagged="", owed="", deadline="", error="",
)

THEMES: dict[str, Theme] = {t.key: t for t in (SOLARIZED_LIGHT, SOLARIZED_DARK, SYSTEM)}

DEFAULT_THEME = SOLARIZED_LIGHT.key


def get(key: str | None) -> Theme:
    """Resolve a name. An unknown one falls back rather than raising: a theme
    named in a config file by a newer version must not stop the client opening."""
    return THEMES.get((key or "").strip().lower(), THEMES[DEFAULT_THEME])


def stylesheet(theme: Theme) -> str:
    """The Qt stylesheet for a theme. Empty for `system`, by design."""
    if theme.key == "system":
        return ""
    t = theme
    return f"""
QWidget {{ background: {t.surface}; color: {t.text}; }}
QMainWindow, QDialog {{ background: {t.surface}; }}

QMenuBar, QStatusBar {{ background: {t.surface_sunken}; color: {t.text}; }}
QMenuBar::item:selected {{ background: {t.accent_muted}; }}
QMenu {{ background: {t.surface}; border: 1px solid {t.border}; }}
QMenu::item:selected {{ background: {t.accent}; color: {t.text_inverse}; }}

QTreeWidget, QTreeView, QListView, QTableView {{
    background: {t.surface}; alternate-background-color: {t.surface_raised};
    border: 1px solid {t.border}; selection-background-color: {t.accent};
    selection-color: {t.text_inverse};
}}
QTreeView::item:hover, QListView::item:hover {{ background: {t.accent_muted}; }}
QHeaderView::section {{
    background: {t.surface_raised}; color: {t.text_muted};
    border: 0; border-bottom: 1px solid {t.border}; padding: 4px 6px;
}}

QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {t.surface}; color: {t.text};
    border: 1px solid {t.border}; border-radius: 4px; padding: 3px 6px;
    selection-background-color: {t.accent}; selection-color: {t.text_inverse};
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {t.accent}; }}

QPushButton, QToolButton {{
    background: {t.surface_raised}; color: {t.text_strong};
    border: 1px solid {t.border}; border-radius: 4px; padding: 4px 10px;
}}
QPushButton:hover, QToolButton:hover {{ background: {t.accent_muted}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {t.text_muted}; }}

QSplitter::handle {{ background: {t.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QScrollBar:vertical, QScrollBar:horizontal {{ background: {t.surface}; border: 0; }}
QScrollBar::handle {{ background: {t.text_muted}; border-radius: 4px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {t.border}; }}
QToolTip {{
    background: {t.surface_raised}; color: {t.text_strong};
    border: 1px solid {t.border};
}}
"""


def resolved(theme: Theme, palette) -> Theme:
    """Fill a theme's empty roles from a Qt palette.

    `system` is deliberately all-empty — it means "do not touch the palette" —
    but the message list and the rail are DRAWN rather than laid out, and a
    delegate cannot paint with an empty string. So under `system` the roles are
    taken from whatever the desktop chose, which is the whole point: the rows
    then follow a high-contrast or dark scheme without corMani knowing it did.

    Four roles have no palette equivalent and keep their literal values. There
    is no such thing as a system colour for "flagged", and inventing one from
    Highlight would make a flag indistinguishable from a selection.
    """
    from PySide6.QtGui import QPalette

    if all((theme.surface, theme.text, theme.accent)):
        return theme

    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    def colour(r, g=group.Active) -> str:
        return palette.color(g, r).name()

    return Theme(
        key=theme.key, name=theme.name,
        dark=palette.color(role.Window).lightness() < 128,
        surface=theme.surface or colour(role.Base),
        surface_raised=theme.surface_raised or colour(role.AlternateBase),
        surface_sunken=theme.surface_sunken or colour(role.Window),
        border=theme.border or colour(role.Mid),
        text=theme.text or colour(role.Text),
        text_strong=theme.text_strong or colour(role.WindowText),
        text_muted=theme.text_muted or colour(role.Text, group.Disabled),
        text_inverse=theme.text_inverse or colour(role.HighlightedText),
        accent=theme.accent or colour(role.Highlight),
        accent_muted=theme.accent_muted or colour(role.Midlight),
        unread=theme.unread or colour(role.Highlight),
        flagged=theme.flagged or _S["yellow"],
        owed=theme.owed or _S["cyan"],
        deadline=theme.deadline or _S["orange"],
        error=theme.error or _S["red"],
    )


# The palette the desktop handed us, captured before anything overwrites it.
# Without this, switching to `system` after Solarized Dark leaves the dark
# palette in place and `system` silently means "whatever was applied last" —
# which is the one thing that theme is defined not to be.
_BASE_PALETTE = None


def apply_to(app, key: str | None = None) -> Theme:
    """Apply a theme to a QApplication. Qt is imported here, not at module
    level, so the palette can be tested without a display."""
    global _BASE_PALETTE
    from PySide6.QtGui import QColor, QPalette

    if _BASE_PALETTE is None:
        _BASE_PALETTE = QPalette(app.palette())

    theme = get(key)
    if theme.key == "system":
        app.setStyleSheet("")
        app.setPalette(QPalette(_BASE_PALETTE))
        return theme

    # Both a palette and a stylesheet. The stylesheet styles the widgets we
    # draw; the palette reaches the ones Qt draws for itself — native dialogs,
    # tooltips, the text cursor — which a stylesheet alone leaves in the old
    # colours and makes the window look half-themed.
    p = QPalette()
    role = QPalette.ColorRole
    p.setColor(role.Window, QColor(theme.surface))
    p.setColor(role.WindowText, QColor(theme.text))
    p.setColor(role.Base, QColor(theme.surface))
    p.setColor(role.AlternateBase, QColor(theme.surface_raised))
    p.setColor(role.Text, QColor(theme.text))
    p.setColor(role.Button, QColor(theme.surface_raised))
    p.setColor(role.ButtonText, QColor(theme.text_strong))
    p.setColor(role.Highlight, QColor(theme.accent))
    p.setColor(role.HighlightedText, QColor(theme.text_inverse))
    p.setColor(role.ToolTipBase, QColor(theme.surface_raised))
    p.setColor(role.ToolTipText, QColor(theme.text_strong))
    p.setColor(role.PlaceholderText, QColor(theme.text_muted))
    p.setColor(role.Link, QColor(theme.accent))
    app.setPalette(p)
    app.setStyleSheet(stylesheet(theme))
    return theme
