# SPDX-License-Identifier: GPL-3.0-or-later
#
# Status bar wiring split out of window.py — counts, outbox, store tooltip.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QStatusBar

from ..smtp import outbox as outbox_repo
from ..store import messages as messages_repo
from . import theme as theme_mod


def build(window) -> None:
    bar = QStatusBar(window)
    window.status_message = QLabel("")
    bar.addWidget(window.status_message, 1)
    window.status_counts = QLabel("")
    bar.addPermanentWidget(window.status_counts)
    window.status_outbox = QLabel("")
    window.status_outbox.setCursor(Qt.CursorShape.PointingHandCursor)
    window.status_outbox.mousePressEvent = lambda e: outbox_clicked(window, e)
    bar.addPermanentWidget(window.status_outbox)
    window.status_sync = QLabel("")
    bar.addPermanentWidget(window.status_sync)
    window.status_last_checked = QLabel("")
    bar.addPermanentWidget(window.status_last_checked)
    window.status_store = QLabel("")
    bar.addPermanentWidget(window.status_store)
    window.setStatusBar(bar)
    window.mail.counts_changed.connect(lambda a, b: counts(window, a, b))
    counts(window, window.mail.model.rowCount(), window.mail.model.total)
    show_outbox(window)
    if window._demo:
        window.status_message.setText(
            "Demo data. This is a disposable store in the cache directory; "
            "your real mail store is untouched.")


def show_outbox(window) -> None:
    from ..store import pending as pending_repo

    stuck = pending_repo.stuck_ops(window._store)
    waiting = outbox_repo.waiting(window._store)
    if stuck:
        n = len(stuck)
        word = "failure" if n == 1 else "failures"
        window.status_outbox.setText(f"{n} send {word}")
        window.status_outbox.setToolTip("Click for details")
    elif waiting:
        window.status_outbox.setText(f"{waiting} waiting to send")
        window.status_outbox.setToolTip("")
    else:
        window.status_outbox.setText("")
        window.status_outbox.setToolTip("")


def outbox_clicked(window, event) -> None:
    from ..store import pending as pending_repo

    if event.button() != Qt.MouseButton.LeftButton:
        return
    stuck = pending_repo.stuck_ops(window._store)
    if not stuck:
        return
    QMessageBox.warning(
        window, "Outbox failures",
        pending_repo.describe_stuck(window._store))


def counts(window, loaded: int, total: int) -> None:
    unread = sum(messages_repo.unread_counts(window._store).values())
    window.status_counts.setText(f"{unread} unread" if unread else "")
    if window._store_summary:
        window.status_counts.setToolTip(window._store_summary)


def set_store_summary(window, text: str) -> None:
    window._store_summary = text or ""
    window.status_counts.setToolTip(window._store_summary)
    window.status_store.setText("")


def update_keyring_banner(window) -> None:
    from ..secrets import store as secrets

    if secrets.available():
        window.keyring_banner.setVisible(False)
        return
    window.keyring_banner.setText(
        "No system keyring is available — you can read mail already "
        "downloaded, but adding an account needs GNOME Keyring or KWallet.")
    window.keyring_banner.setVisible(True)
    theme = theme_mod.resolved(theme_mod.get(window._theme_key))
    window.keyring_banner.setStyleSheet(
        f"color: {theme.error}; background: {theme.surface_raised}; "
        f"padding: 6px 8px; border: 1px solid {theme.border};")
