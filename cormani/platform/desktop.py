# SPDX-License-Identifier: GPL-3.0-or-later
#
# Handing a file or a link to the desktop.
#
# Opening an attachment means asking another program to run, on a file a
# stranger sent. That is two rules from CONVENTIONS.txt at once — §5, process
# handling goes behind a platform module, and §7, shell out with an argument
# array and never a shell string — so it lives here rather than in the widget
# that has the button.
#
# THE ARGUMENT IS NEVER A STRING TO BE PARSED. `xdg-open` is exec'd with argv,
# so a filename containing a space, a quote, a semicolon or a newline is one
# argument and cannot become a second command. On Windows `os.startfile` takes
# a path rather than a command line, which is the same property by a different
# route. Nothing here ever reaches a shell.
#
# THE CHILD IS NOT WAITED FOR. `xdg-open` usually returns at once, but on a
# system with no desktop portal it can exec the handler directly and block
# until the user closes it. Waiting on that would freeze the interface, so the
# child is started and released — in its own session, so it outlives corMani
# and does not arrive back as a zombie, and with its output discarded, so a
# chatty handler does not write over the terminal corMani was started from.
#
# WHAT THAT COSTS IS HONESTY ABOUT THE EXIT CODE, and the boundary is drawn
# where it can be defended: this module reports whether the handler could be
# STARTED, not whether it succeeded. A missing `xdg-open` is an error the user
# is told about; a handler that opens and then complains about the file is
# between the user and that program. CONVENTIONS.txt §8 — say what is known.
#
# A LINK IS CHECKED AGAIN HERE. `render/sanitise.py` already permits only
# http, https, mailto and tel in a message, so a `file:` or a `javascript:`
# cannot reach a click. It is checked a second time at the door anyway, because
# the sanitiser's list exists to make a page safe to DISPLAY and this list
# exists to make a URL safe to HAND TO ANOTHER PROGRAM — the same values today,
# but two different reasons, and they should be free to diverge.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# The schemes that may be handed to the desktop. `file` is deliberately absent:
# a link in a message that opens a local path is either useless or an attack,
# and attachments are opened by path through `open_path` instead.
LINK_SCHEMES = ("http", "https", "mailto", "tel")

# The Linux handler. One name, not a search: `gio open` and `kde-open` differ
# in their argument handling, and a fallback chain is three behaviours to be
# right about instead of one.
LINUX_OPENER = "xdg-open"


class OpenFailed(Exception):
    """The desktop could not be asked. The message is fit to show a user."""


def _spawn(argv: list[str]) -> None:
    """Start a detached child, or raise OpenFailed with a readable reason."""
    try:
        subprocess.Popen(                       # noqa: S603 — argv, never a shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as exc:
        raise OpenFailed(f"could not run {argv[0]}: {exc}") from exc


def opener_available() -> bool:
    """Whether this machine has anything to open a file with.

    Called before offering the action rather than after failing it: a menu item
    that always reports the same error is worse than one that is not there.
    """
    if sys.platform == "win32":
        return True
    return shutil.which(LINUX_OPENER) is not None


def open_path(path: Path | str, *, spawn=None) -> None:
    """Ask the desktop to open one file.

    `spawn` is injected by the tests, which must never launch a real program.
    The path is resolved and required to be absolute first, so that nothing
    relative to corMani's working directory can be opened by accident and so
    that no argument can begin with a dash and be read as an option.
    """
    target = Path(path).expanduser()
    try:
        target = target.resolve()
    except OSError as exc:                                   # pragma: no cover
        raise OpenFailed(f"could not resolve {path}: {exc}") from exc
    if not target.is_absolute():                             # pragma: no cover
        raise OpenFailed(f"not an absolute path: {target}")
    if not target.exists():
        raise OpenFailed(f"no such file: {target}")

    if spawn is None and sys.platform == "win32":            # pragma: no cover
        try:
            os.startfile(str(target))                        # noqa: S606
        except OSError as exc:
            raise OpenFailed(f"could not open {target.name}: {exc}") from exc
        return

    if spawn is None and shutil.which(LINUX_OPENER) is None:
        raise OpenFailed(
            f"{LINUX_OPENER} is not installed, so corMani cannot ask the "
            f"desktop to open this. Save it instead.")

    (spawn or _spawn)([LINUX_OPENER, str(target)])


def open_url(url: str, *, spawn=None) -> None:
    """Ask the desktop to open one link, if its scheme is one of ours."""
    text = (url or "").strip()
    if not scheme_allowed(text):
        raise OpenFailed(f"refused to open a {_scheme_of(text) or 'schemeless'} link")

    if spawn is None and sys.platform == "win32":            # pragma: no cover
        try:
            os.startfile(text)                               # noqa: S606
        except OSError as exc:
            raise OpenFailed(f"could not open the link: {exc}") from exc
        return

    if spawn is None and shutil.which(LINUX_OPENER) is None:
        raise OpenFailed(f"{LINUX_OPENER} is not installed, so corMani cannot "
                         f"open links. Copy the address instead.")

    (spawn or _spawn)([LINUX_OPENER, text])


def _scheme_of(url: str) -> str:
    """The scheme, lowercased, or "" — without urlsplit.

    `urllib.parse` accepts and normalises things this check must reject, such as
    control characters inside the scheme, so the parse is done by hand: up to
    the first colon, and only if what precedes it is a plausible scheme.
    """
    head, colon, _ = (url or "").partition(":")
    if not colon or not head:
        return ""
    if not head[0].isascii() or not head[0].isalpha():
        return ""
    if not all(ch.isascii() and (ch.isalnum() or ch in "+-.") for ch in head):
        return ""
    return head.lower()


def scheme_allowed(url: str) -> bool:
    return _scheme_of(url) in LINK_SCHEMES
