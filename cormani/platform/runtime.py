# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the embedded browser tells the world it is.
#
# QtWebEngine's default user agent carries a `QtWebEngine/6.8.2` token, and
# WhatsApp Web refuses to load against agents it does not recognise. So it has
# to be replaced — but the replacement is not free to say anything it likes.
#
# Measured on 25 August 2026 against a loopback server (see
# docs/toolkit-verification.txt): overriding the user agent in Qt does NOT
# suppress the `sec-ch-ua` client hints, unlike overriding it over the DevTools
# Protocol, where it silently does. Qt keeps sending them, and they keep naming
# the real Chromium version. Claiming Chrome/151 while the hints say 122
# therefore produces a contradiction no real browser emits — a stronger
# fingerprint than an unusual but coherent identity.
#
# So the agent claims exactly the version the engine actually is, and that
# version is READ FROM THE RUNTIME rather than written down. Debian's Qt will
# move, the embedded Chromium will move with it, and a hardcoded number would
# then become the mismatch this module exists to prevent.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re

# `Chrome/122.0.0.0` — the token every Chromium-derived agent carries.
_CHROME_TOKEN = re.compile(r"\bChrome/(\d+(?:\.\d+)*)\b")
# `QtWebEngine/6.8.2` — the token that identifies this as not-a-browser.
_QT_TOKEN = re.compile(r"\s*QtWebEngine/\S+")

# Used only when the runtime agent cannot be read at all, which should not
# happen. Deliberately a bare major version: wrong-but-plausible beats absent.
FALLBACK_CHROME_MAJOR = "122"


def chrome_version(default_agent: str) -> str | None:
    """The Chromium version the engine really is, from its own default agent."""
    m = _CHROME_TOKEN.search(default_agent or "")
    return m.group(1) if m else None


def derive_user_agent(default_agent: str, platform_token: str = "X11; Linux x86_64") -> str:
    """An ordinary Chrome agent for the engine actually running.

    Built by removing the QtWebEngine token from the engine's own default rather
    than by composing a string from scratch, so that everything else Qt puts
    there — the platform, the WebKit version — stays true.
    """
    if default_agent:
        cleaned = _QT_TOKEN.sub("", default_agent).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        if _CHROME_TOKEN.search(cleaned):
            return cleaned

    version = chrome_version(default_agent) or FALLBACK_CHROME_MAJOR
    if "." not in version:
        version = f"{version}.0.0.0"
    return (f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


# The Chromium major below which sites begin refusing a panel outright. One
# spelling, read by the start-up warning and by `--check`, because two copies
# of a threshold are two thresholds the day one of them moves.
MIN_COMFORTABLE_CHROME = 120


def engine_versions() -> dict:
    """What the embedded browser IS, asked of the toolkit rather than a profile.

    These are free functions in QtWebEngineCore and need NO QApplication, which
    is the whole reason `--check` can report them from a headless command line.
    `engine_report` below answers a different question — what the browser SAYS
    it is — and needs a running application and a profile to ask.

    THE TWO VERSIONS ARE DIFFERENT NUMBERS AND BOTH MATTER. The Chromium
    version is what a panel claims to be and what a site decides to accept;
    the security-patch version is how current the fixes inside it are, and Qt
    backports a long way past the feature version — 122 against 132 here, most
    of a year apart. Finding 2 was written from the first number alone and
    reads more alarmingly than the second one justifies. Reporting only the
    feature version understates the engine; reporting only the patch version
    overstates it.

    Raises ImportError when QtWebEngine is not installed, which is a real and
    supported state: the panels are optional.
    """
    from PySide6 import QtWebEngineCore as core

    def ask(name: str) -> str:
        # A Qt without one of these is not impossible; an empty string is a
        # better answer than a traceback out of a diagnostic command.
        fn = getattr(core, name, None)
        try:
            return str(fn()) if fn is not None else ""
        except Exception:                                    # pragma: no cover
            return ""

    chromium = ask("qWebEngineChromiumVersion")
    return {
        "qt": ask("qWebEngineVersion"),
        "chromium": chromium,
        "security": ask("qWebEngineChromiumSecurityPatchVersion"),
        "chromium_major": (int(chromium.split(".")[0])
                           if chromium.split(".")[0].isdigit() else None),
    }


def engine_report(default_agent: str) -> dict:
    """What to write to the log at start-up, so an aged engine is visible.

    Chromium 122 is from early 2024 and Debian stable moves slowly. When a site
    panel starts refusing to load, the first question is how old the engine is,
    and the answer should already be in the log rather than needing a rebuild
    to discover.
    """
    version = chrome_version(default_agent)
    return {
        "chrome_version": version,
        "chrome_major": int(version.split(".")[0]) if version else None,
        "user_agent": derive_user_agent(default_agent),
        "is_qt_default": bool(_QT_TOKEN.search(default_agent or "")),
    }


def resource(name: str) -> Path | None:
    """Find a shipped data file — the icon, the metainfo — or None.

    Checked in order: beside the source tree, which is how corMani runs today
    since pip is not installed; then the installed data directories, which is
    where a Debian package puts it. None rather than a guess, because a caller
    that gets a path to a file which is not there produces a worse error later
    than the one it would have produced here.
    """
    from pathlib import Path as _Path

    candidates = [_Path(__file__).resolve().parent.parent.parent / "data" / name]
    import os

    for root in os.environ.get(
            "XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if root:
            candidates.append(_Path(root) / "cormani" / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
