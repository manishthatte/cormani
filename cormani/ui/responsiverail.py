# SPDX-License-Identifier: GPL-3.0-or-later
#
# Hide the rail on narrow windows — ui.md §9.4.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtWidgets import QPushButton

RAIL_BREAKPOINT = 900


def install(pane) -> None:
    pane._rail_pinned = False
    pane.rail_toggle = QPushButton("Accounts", pane.middle)
    pane.rail_toggle.setFlat(True)
    pane.rail_toggle.setToolTip("Show accounts and folders")
    pane.rail_toggle.clicked.connect(lambda: show(pane))
    pane.rail_toggle.setVisible(False)
    pane.middle.layout().insertWidget(0, pane.rail_toggle)
    original = pane.resizeEvent

    def resize_event(event) -> None:
        original(event)
        on_resize(pane)

    pane.resizeEvent = resize_event  # type: ignore[method-assign]


def on_resize(pane) -> None:
    narrow = pane.width() < RAIL_BREAKPOINT
    if narrow and not pane._rail_pinned:
        pane.rail.hide()
        pane.rail_toggle.setVisible(True)
    else:
        if not narrow:
            pane._rail_pinned = False
        pane.rail.show()
        pane.rail_toggle.setVisible(False)


def show(pane) -> None:
    pane._rail_pinned = True
    pane.rail.show()
    pane.rail_toggle.setVisible(False)
