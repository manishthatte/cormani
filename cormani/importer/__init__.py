# SPDX-License-Identifier: GPL-3.0-or-later
#
# Importing mail from another program's store into corMani's.
#
# Thunderbird first: its on-disk layout is what this machine still holds, and
# PLAN.txt names the mbox reader as the piece that ports from the prototype.
# A vCard or CSV address-book reader belongs beside this when somebody asks
# for it — open item 11 — and shares the rule that the source is opened
# read-only.
#
# © Manish Jagdish Thatte
"""Import mail from Thunderbird (and other mbox trees) into the store."""

from .run import Report, run

__all__ = ["Report", "run"]
