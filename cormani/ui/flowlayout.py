# SPDX-License-Identifier: GPL-3.0-or-later
#
# A layout that wraps.
#
# Qt has no flow layout, and the reading pane needs one twice: the attachment
# strip, where a photo set is twelve chips and a single row would clip eleven
# of them out of sight, and the tag chips that arrive with the rest of stage 3.
# A horizontal box layout does not wrap and a scroll area inside a vertical
# stack is a worse answer than a second row.
#
# THE PART THAT IS EASY TO GET WRONG is `heightForWidth`. A wrapping layout's
# height is not a property of its contents but of the width it is given, and a
# widget that does not declare that dependency is measured at one row, sized
# for one row, and then draws three — over whatever is beneath it. So the
# layout answers `heightForWidth` honestly and the widget using it must set a
# height-for-width size policy, which `AttachmentStrip` does.
#
# `_arrange` runs in two modes on purpose. Measuring and placing must use
# exactly the same arithmetic, because a measurement that disagrees with the
# placement is the same bug as not measuring at all, arrived at more slowly.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy


class FlowLayout(QLayout):
    """Left to right, wrapping at the width available."""

    def __init__(self, parent=None, *, margin: int = 0, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    # Qt calls this on destruction; the list is what holds the references.
    def __del__(self) -> None:                                # pragma: no cover
        self._items = []

    # ------------------------------------------------- the QLayout contract
    def addItem(self, item) -> None:                          # noqa: N802 (Qt)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):                             # noqa: N802 (Qt)
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):                             # noqa: N802 (Qt)
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):                            # noqa: N802 (Qt)
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:                      # noqa: N802 (Qt)
        return True

    def heightForWidth(self, width: int) -> int:              # noqa: N802 (Qt)
        return self._arrange(QRect(0, 0, width, 0), place=False)

    def setGeometry(self, rect: QRect) -> None:               # noqa: N802 (Qt)
        super().setGeometry(rect)
        self._arrange(rect, place=True)

    def sizeHint(self) -> QSize:                              # noqa: N802 (Qt)
        return self.minimumSize()

    def minimumSize(self) -> QSize:                           # noqa: N802 (Qt)
        """The widest single item, not the sum.

        A row of chips whose minimum width is their total width sets a floor
        for the pane holding it, and the splitter then takes that width from
        whichever pane can shrink — the rail. ui/commandbar.py met the same
        problem and this is the wrapping answer to it: one chip wide is enough,
        because everything past it wraps.
        """
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    # ----------------------------------------------------------- the layout
    def _arrange(self, rect: QRect, *, place: bool) -> int:
        """Place the items, or measure where they would go. Returns the height."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y = area.x(), area.y()
        line_height = 0
        gap = max(0, self.spacing())

        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisibleTo(widget.parentWidget()):
                continue
            hint = item.sizeHint()
            step = x + hint.width() + gap
            if line_height > 0 and step - gap > area.right() + 1:
                x = area.x()
                y = y + line_height + gap
                line_height = 0
                step = x + hint.width() + gap
            if place:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = step
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()


def height_for_width_policy(widget) -> None:
    """Tell Qt this widget's height depends on its width, which is the half of
    a flow layout that has to be declared on the widget rather than the layout."""
    policy = widget.sizePolicy()
    policy.setHeightForWidth(True)
    policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
    widget.setSizePolicy(policy)
