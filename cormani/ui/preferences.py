# SPDX-License-Identifier: GPL-3.0-or-later
#
# Preferences — the settings file, without an editor.
#
# Theme and density already live in the View menu; everything else lived only
# in `cormani.toml` until this dialog. Saving goes through
# `config/settings.save`, which is the one write path corMani has for the file.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)

from ..config import settings as config_mod
from ..panels import sites as sites_mod
from ..platform.paths import Paths
from . import density as density_mod
from . import theme as theme_mod


class PreferencesDialog(QDialog):
    def __init__(self, window, *, settings: config_mod.Settings,
                 paths: Paths | None = None, parent=None) -> None:
        super().__init__(parent or window)
        self._window = window
        self._paths = paths or Paths()
        self._settings = settings
        self.setWindowTitle("Preferences")
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        layout.addWidget(tabs)

        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._sync_tab(), "Sync")
        tabs.addTab(self._panels_tab(), "Panels")
        tabs.addTab(self._privacy_tab(), "Privacy")
        tabs.addTab(self._notifications_tab(), "Notifications")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _appearance_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        self._theme = QComboBox(page)
        for key, item in theme_mod.THEMES.items():
            self._theme.addItem(item.name, key)
        idx = self._theme.findData(self._window._theme_key or self._settings.theme)
        if idx >= 0:
            self._theme.setCurrentIndex(idx)
        form.addRow("Theme:", self._theme)

        self._density = QComboBox(page)
        for key, item in density_mod.DENSITIES.items():
            self._density.addItem(item.name, key)
        d_idx = self._density.findData(self._window._saved_density())
        if d_idx >= 0:
            self._density.setCurrentIndex(d_idx)
        form.addRow("Density:", self._density)
        return page

    def _sync_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        self._interval = QSpinBox(page)
        self._interval.setRange(0, 24 * 60)
        self._interval.setSuffix(" min")
        self._interval.setSpecialValueText("Manual only")
        self._interval.setValue(int(self._settings.sync_interval_minutes))
        form.addRow("Check for mail every:", self._interval)

        self._initial_days = QSpinBox(page)
        self._initial_days.setRange(0, 3650)
        self._initial_days.setSuffix(" days")
        self._initial_days.setSpecialValueText("Everything")
        self._initial_days.setValue(int(self._settings.initial_sync_days))
        form.addRow("First sync reaches back:", self._initial_days)

        self._sync_max = QSpinBox(page)
        self._sync_max.setRange(50, 10000)
        self._sync_max.setValue(int(self._settings.sync_max_new))
        form.addRow("Bodies per folder per pass:", self._sync_max)

        self._page_size = QSpinBox(page)
        self._page_size.setRange(50, 2000)
        self._page_size.setValue(int(self._settings.list_page_size))
        form.addRow("Message list page size:", self._page_size)

        note = QLabel(
            "Changes to sync limits apply on the next sync. The list page size "
            "needs a restart to take effect.", page)
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def _panels_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(
            "Which site panels appear in the rail. Uncheck all to hide every "
            "panel.", page))
        self._site_boxes: dict[str, QCheckBox] = {}
        selected = set(self._settings.site_keys())
        none_selected = (self._settings.sites or []) == ["none"]
        for site in sites_mod.SITES:
            box = QCheckBox(site.name, page)
            if none_selected:
                box.setChecked(False)
            else:
                box.setChecked(site.key in selected)
            self._site_boxes[site.key] = box
            layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _notifications_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self._quiet = QCheckBox("Quiet hours — no desktop notifications", page)
        self._quiet.setChecked(bool(self._settings.notify_quiet_enabled))
        layout.addWidget(self._quiet)
        row = QHBoxLayout()
        row.addWidget(QLabel("From", page))
        self._quiet_start = QSpinBox(page)
        self._quiet_start.setRange(0, 23)
        self._quiet_start.setValue(int(self._settings.notify_quiet_start))
        row.addWidget(self._quiet_start)
        row.addWidget(QLabel("to", page))
        self._quiet_end = QSpinBox(page)
        self._quiet_end.setRange(0, 23)
        self._quiet_end.setValue(int(self._settings.notify_quiet_end))
        row.addWidget(self._quiet_end)
        row.addWidget(QLabel("(local time, 24-hour)", page))
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(QLabel(
            "During quiet hours, new-mail notifications go to the status bar "
            "only.", page))
        layout.addStretch(1)
        return page

    def _privacy_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self._block_remote = QCheckBox(
            "Ask before loading remote images in a message", page)
        self._block_remote.setChecked(bool(self._settings.block_remote_content))
        layout.addWidget(self._block_remote)
        layout.addWidget(QLabel(
            "Leaving this on means a sender cannot learn that you opened their "
            "mail by fetching a tracking pixel.", page))
        layout.addStretch(1)
        return page

    def _accept(self) -> None:
        theme_key = self._theme.currentData()
        density_key = self._density.currentData()
        sites = [key for key, box in self._site_boxes.items() if box.isChecked()]
        if not sites:
            sites = ["none"]
        updated = config_mod.Settings(
            source=self._settings.source,
            log_level=self._settings.log_level,
            theme=str(theme_key),
            data_dir=self._settings.data_dir,
            chromium_flags=list(self._settings.chromium_flags),
            block_remote_content=self._block_remote.isChecked(),
            sync_interval_minutes=int(self._interval.value()),
            list_page_size=int(self._page_size.value()),
            initial_sync_days=int(self._initial_days.value()),
            sync_max_new=int(self._sync_max.value()),
            sites=sites,
            notify_quiet_enabled=self._quiet.isChecked(),
            notify_quiet_start=int(self._quiet_start.value()),
            notify_quiet_end=int(self._quiet_end.value()),
        )
        try:
            config_mod.save(updated, paths=self._paths)
        except OSError as exc:
            self._window.status_message.setText(
                f"Could not write preferences: {exc}")
            return
        self._window.apply_theme(str(theme_key))
        self._window.set_density(str(density_key))
        self._window.attach_sites(updated.site_keys())
        autosync = getattr(self._window, "_autosync", None)
        if autosync is not None:
            autosync.stop()
            autosync._interval = updated.sync_interval_minutes
            autosync.start()
        self._window.status_message.setText(
            f"Preferences saved to {self._paths.config_file}")
        self.accept()


def show(window, *, settings: config_mod.Settings | None = None,
         paths: Paths | None = None) -> None:
    settings = settings or getattr(window, "_cfg", None) or config_mod.load()
    paths = paths or Paths()
    PreferencesDialog(window, settings=settings, paths=paths, parent=window).exec()
