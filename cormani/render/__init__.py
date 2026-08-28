# SPDX-License-Identifier: GPL-3.0-or-later
#
# Turning a message into something safe to show.
#
# Nothing in here imports Qt. The sanitiser is the security boundary and it has
# to be testable exhaustively, with no display and no widget — a boundary that
# can only be exercised through a rendering widget is a boundary nobody tests
# the hard cases of.
#
# © Manish Jagdish Thatte
from __future__ import annotations
