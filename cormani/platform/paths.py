# SPDX-License-Identifier: GPL-3.0-or-later
#
# Where corMani is allowed to write.
#
# Implemented against the XDG Base Directory specification directly rather than
# through QStandardPaths, for two reasons: this module is imported before a
# QApplication exists, and the test suite must run with no display and no Qt.
#
# The specification is followed exactly, including the parts that are commonly
# ignored — STATE is not DATA, and CACHE is genuinely disposable. That matters
# for a mail client: the message store is DATA and must survive a cache purge,
# while rendered previews and web-engine caches are CACHE and must not be backed
# up. Getting this wrong is how a backup ends up ten gigabytes larger than the
# mail it protects.
#
# On Windows the same four roles map onto APPDATA and LOCALAPPDATA, which is the
# closest honest equivalent: roaming for what should follow the user, local for
# what should not.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import APP_ID


def _env_dir(name: str, default: Path) -> Path:
    """An XDG variable counts only if it is set AND absolute, per the spec."""
    value = os.environ.get(name)
    if value:
        p = Path(value)
        if p.is_absolute():
            return p
    return default


class Paths:
    """The four roles, resolved once.

    Construct with an explicit `root` in tests: every path then sits under a
    temporary directory and nothing touches the real profile.
    """

    def __init__(self, app_id: str = APP_ID, root: Path | None = None) -> None:
        self.app_id = app_id
        if root is not None:
            root = Path(root)
            self.config = root / "config"
            self.data = root / "data"
            self.cache = root / "cache"
            self.state = root / "state"
            return

        if sys.platform == "win32":
            roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            self.config = roaming / app_id / "config"
            self.data = roaming / app_id / "data"
            self.cache = local / app_id / "cache"
            self.state = local / app_id / "state"
            return

        home = Path.home()
        self.config = _env_dir("XDG_CONFIG_HOME", home / ".config") / app_id
        self.data = _env_dir("XDG_DATA_HOME", home / ".local" / "share") / app_id
        self.cache = _env_dir("XDG_CACHE_HOME", home / ".cache") / app_id
        self.state = _env_dir("XDG_STATE_HOME", home / ".local" / "state") / app_id

    # --- the specific files, named once so no caller spells them ------------

    @property
    def config_file(self) -> Path:
        return self.config / "cormani.toml"

    @property
    def database(self) -> Path:
        """The store. DATA, not CACHE — losing it loses mail."""
        return self.data / "cormani.sqlite3"

    @property
    def demo_database(self) -> Path:
        """The demo store, for `--demo`.

        CACHE, not DATA, and that is the whole safety argument: it is disposable
        by definition, it is not backed up, and it does not sit in the directory
        a person looks in for their mail. Fixture data therefore cannot reach the
        real store even if every other check failed, because the real store is
        never opened in demo mode.
        """
        return self.cache / "demo.sqlite3"

    @property
    def attachments(self) -> Path:
        """Extracted attachment bodies, kept beside the store they belong to."""
        return self.data / "attachments"

    @property
    def attachment_cache(self) -> Path:
        """Copies of attachments made to hand to another program.

        CACHE, and the distinction from `attachments` above is the whole point:
        that one is the archive and losing it loses a file nobody else has,
        this one is a copy the store can make again. An editor may write into
        what is here and a purge may delete it; neither may touch the other.
        """
        return self.cache / "open"

    @property
    def web_profiles(self) -> Path:
        """Per-site web-engine profiles: cookies and logins, so DATA."""
        return self.data / "web"

    @property
    def web_cache(self) -> Path:
        """Web-engine caches: disposable, so CACHE."""
        return self.cache / "web"

    @property
    def log_file(self) -> Path:
        return self.state / "cormani.log"

    def ensure(self) -> "Paths":
        """Create every directory. Called once at start-up, never per write."""
        for p in (self.config, self.data, self.cache, self.state,
                  self.attachments, self.attachment_cache,
                  self.web_profiles, self.web_cache):
            p.mkdir(parents=True, exist_ok=True)
        return self

    # --- the desktop environment's own directories -----------------------

    @property
    def applications(self) -> Path:
        """Where a .desktop entry goes for this user only.

        Per-user rather than system-wide because installing into /usr needs
        root, and corMani should be pinnable without it. A distribution package
        installs the same file into /usr/share/applications instead.
        """
        return _env_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / "applications"

    @property
    def icon_theme(self) -> Path:
        """The scalable-apps directory of the user's hicolor icon theme.

        hicolor is the fallback every icon theme inherits from, so an icon here
        is found whatever theme the desktop is set to.
        """
        return (_env_dir("XDG_DATA_HOME", Path.home() / ".local" / "share")
                / "icons" / "hicolor" / "scalable" / "apps")

    def __repr__(self) -> str:
        return (f"Paths(config={self.config}, data={self.data}, "
                f"cache={self.cache}, state={self.state})")


def icon_search_path() -> list:
    """Where a toolkit looks for icon THEMES, in the order it looks.

    The XDG icon spec: $HOME/.icons first, then $XDG_DATA_HOME/icons, then
    each of $XDG_DATA_DIRS/icons. THE ORDER IS THE WHOLE POINT and is not a
    detail — a theme is described by the FIRST `index.theme` found along this
    path, and that one description then governs every directory in it,
    including the system's. `configure._describing_index` is the caller and
    the header there records what happened when that was got wrong.
    """
    home = Path.home()
    dirs = [home / ".icons",
            _env_dir("XDG_DATA_HOME", home / ".local" / "share") / "icons"]
    raw = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for part in raw.split(":"):
        part = part.strip()
        if part:
            dirs.append(Path(part) / "icons")
    return dirs
