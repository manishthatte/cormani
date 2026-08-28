# SPDX-License-Identifier: GPL-3.0-or-later
#
# Installation health — the in-app face of `cormani --check`.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import io
import sys

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

from .. import cli


class HealthDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Installation health")
        self.resize(640, 480)
        layout = QVBoxLayout(self)
        view = QPlainTextEdit(self)
        view.setReadOnly(True)
        view.setPlainText(_check_text())
        layout.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _check_text() -> str:
    buffer = io.StringIO()
    old = sys.stdout
    sys.stdout = buffer
    try:
        cli.check()
    finally:
        sys.stdout = old
    return buffer.getvalue().rstrip()


def show(parent=None) -> None:
    HealthDialog(parent).exec()
