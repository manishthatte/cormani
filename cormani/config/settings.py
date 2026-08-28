# SPDX-License-Identifier: GPL-3.0-or-later
#
# The configuration file, and what deliberately is not in it.
#
# `cormani.toml` holds preferences a person might reasonably want to set by
# hand: a log level, an override for where the store lives, engine flags. It is
# optional, and corMani never writes to it.
#
# ACCOUNTS ARE NOT HERE. They live in the database, because the application
# edits them — added through a dialog, reordered by dragging, grouped, coloured,
# hidden. Anything the application edits and the user also edits by hand needs
# a merge strategy, and every merge strategy for a config file is either lossy
# or complicated. Splitting on "who writes it" removes the problem instead of
# solving it: the file is the user's and read-only to us, the database is ours.
#
# Read with tomllib, which is standard library from 3.11. There is no standard
# library TOML *writer*, and this module needs none — which is the point.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..platform.paths import Paths


@dataclass
class Settings:
    """Everything the file can say. Every field has a working default, so a
    machine with no configuration file at all is a supported configuration."""

    log_level: str = "info"                 # debug | info | warning | error
    # Colour. "system" leaves the desktop's own scheme alone, which is what an
    # accessibility setting needs; anything else is a named palette from
    # ui/theme.py. Solarized Light by default.
    theme: str = "solarized-light"
    # An override for the store's location — an encrypted volume, say. Empty
    # means the XDG location.
    data_dir: str = ""
    # Extra switches for the embedded browser. Present because the engine's
    # behaviour is the thing most likely to need a workaround on a machine we
    # cannot test on, and rebuilding to add a flag is a poor answer.
    chromium_flags: list[str] = field(default_factory=list)
    # Ask before loading remote content in a message. Default on, and the
    # dialog exists rather than a silent block, because a tracking pixel is a
    # disclosure and the user should be the one deciding. CONVENTIONS.txt §7.
    block_remote_content: bool = True
    # Minutes between automatic mail checks. Zero means manual only.
    sync_interval_minutes: int = 5
    # Rows in the message list before paging. Fifteen accounts unified is a
    # large list, and drawing all of it is what makes a client feel slow.
    list_page_size: int = 200
    # How far back a FIRST sync of an account reaches, in days. 0 means all of
    # it — correct, and not the default, because docs/accounts.txt measured the
    # constraint: Gmail's DAILY DOWNLOAD CAP, not its connection limit, is what
    # a full history across eight Google accounts trips.
    initial_sync_days: int = 90
    # Message bodies taken from one folder in one pass. What makes a first
    # import resumable, and what stops one enormous folder starving the other
    # fourteen accounts.
    sync_max_new: int = 500
    # WHICH SITE PANELS TO SHOW, by key — see `panels/sites.py`. An empty list
    # means the registry's own defaults, which are the four messaging sites and
    # not the two webmail ones: corMani already holds that mail over IMAP.
    #
    # THE POINT OF THE SETTING IS THAT PANELS ARE OPTIONAL, and that is not a
    # preference. docs/toolkit-verification.txt finding 2: Debian pins the
    # embedded Chromium, so the engine ages between releases and these sites
    # will eventually refuse it. When that day comes, mail and calendar must be
    # unaffected — which means a person must be able to turn the panels off
    # entirely, with `sites = []` meaning defaults and `sites = ["none"]`
    # meaning none.
    sites: list[str] = field(default_factory=list)

    source: str = ""                        # which file this came from, if any

    @property
    def is_default(self) -> bool:
        return not self.source

    def site_keys(self) -> list:
        """The site panels to show, resolved.

        `[]` means the registry's defaults and `["none"]` means none at all.
        A sentinel rather than a second boolean, because "no panels" and "I
        have not said" are genuinely different answers and an empty list can
        only carry one of them.
        """
        from ..panels import sites as sites_mod

        asked = [str(k).strip().lower() for k in (self.sites or []) if str(k).strip()]
        if not asked:
            return sites_mod.default_keys()
        if asked == ["none"]:
            return []
        return [k for k in asked if sites_mod.get(k) is not None]


_ALLOWED = {f: type(getattr(Settings, f, None)) for f in Settings.__dataclass_fields__}


def load(paths: Paths | None = None, path: Path | None = None) -> Settings:
    """Read the configuration file if there is one.

    A malformed file raises rather than being silently ignored: a user who
    edited it and made a mistake needs to be told, not to have their change
    quietly dropped and wonder why nothing happened.

    An *unknown key* does not raise. It is reported by `unknown_keys()` so the
    interface can mention it, because the common cause is a file written for a
    newer version, and refusing to start over a key we do not need would be
    worse than ignoring it.
    """
    paths = paths or Paths()
    path = path or paths.config_file
    if not path.exists():
        return Settings()

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    s = Settings(source=str(path))
    for key, value in raw.items():
        if key in Settings.__dataclass_fields__ and key != "source":
            setattr(s, key, value)
    return s


def unknown_keys(path: Path) -> list[str]:
    """Keys in the file this build does not understand. For the log, not an error."""
    if not path.exists():
        return []
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return []
    return sorted(k for k in raw if k not in Settings.__dataclass_fields__ or k == "source")


EXAMPLE = """\
# corMani configuration. Every setting is optional; this file may be deleted.
#
# Accounts are NOT configured here — they are added in the application and kept
# in its database, because the application edits them too.
#
# © Manish Jagdish Thatte

# debug | info | warning | error
log_level = "info"

# Colour: "solarized-light", "solarized-dark", or "system" to leave the
# desktop's own scheme alone.
theme = "solarized-light"

# Where the message store lives. Empty means the standard location for this
# system. Point it at an encrypted volume if you would rather.
data_dir = ""

# Ask before loading images and other remote content in a message. Leaving this
# on means a sender cannot learn that you opened their mail.
block_remote_content = true

# Minutes between automatic checks. 0 checks only when you ask.
sync_interval_minutes = 5

# Rows drawn before paging.
list_page_size = 200

# How far back the FIRST sync of an account reaches, in days. 0 fetches
# everything, which is slow and can exhaust a provider's daily download
# allowance across several accounts. Later syncs always fetch everything new.
initial_sync_days = 90

# Message bodies taken from one folder per pass, so a large first import makes
# progress everywhere rather than finishing one mailbox and starving the rest.
sync_max_new = 500

# Extra switches for the embedded browser used by the site panels.
# chromium_flags = ["--disable-gpu"]
"""
