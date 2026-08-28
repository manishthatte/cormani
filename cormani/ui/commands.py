# SPDX-License-Identifier: GPL-3.0-or-later
#
# Whether a command is ready, and what it says when it is not.
#
# ONE PLACE ANSWERS, shared by the command bar, the menus and the help dialog.
# A reply button that is greyed out while the Message menu works is two surfaces
# disagreeing about the same thing, and the one the user sees most often is the
# one that lies.
#
# Snooze is ready only when both halves exist — somewhere to keep a deadline
# and a dialog to ask for one. Until then the button stays disabled and the
# tooltip names what is missing rather than a stage number that stopped being
# true when tracking shipped.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _module_exists(relative: str) -> bool:
    path = _ROOT / relative
    return path.is_file()


_SNOOZE_READY = (_module_exists("store/snooze.py")
                 and _module_exists("ui/snoozedialog.py"))

# (ready, tooltip when enabled, status when not ready)
_REGISTRY: dict[str, tuple[bool, str, str]] = {
    "compose": (True, "Write a new message", ""),
    "reply": (True, "Reply to the sender", ""),
    "reply_all": (True, "Reply to everyone on this message", ""),
    "forward": (True, "Forward this message to someone else", ""),
    "snooze": (
        _SNOOZE_READY,
        "Take it off the list and bring it back later",
        "Snooze is not available in this build yet",
    ),
    "print": (True, "Print the message being read", ""),
}


def known(command_id: str) -> bool:
    return command_id in _REGISTRY


def command_ready(command_id: str) -> bool:
    """Whether this command may run. Unknown ids are treated as ready."""
    entry = _REGISTRY.get(command_id)
    return entry[0] if entry is not None else True


def command_tooltip(command_id: str) -> str:
    """The tooltip for a command-bar button, or the status tip for a menu item."""
    entry = _REGISTRY.get(command_id)
    if entry is None:
        return ""
    ready, tip, _not_ready = entry
    return tip if ready else _not_ready


def command_not_ready_message(command_id: str) -> str:
    """What to put in the status bar when a not-ready command is reached."""
    entry = _REGISTRY.get(command_id)
    if entry is None:
        return f"No such command: {command_id}"
    _ready, _tip, not_ready = entry
    return not_ready or command_tooltip(command_id)
