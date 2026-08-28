# SPDX-License-Identifier: GPL-3.0-or-later
#
# The IMAP engine: the wire, the sync, and the offline queue.
#
# Nothing in here imports Qt or touches the interface. The engine is driven
# from a worker thread and reports through plain callbacks, so that the whole
# of it can be tested with no display and no socket — see tests/fakeimap.py,
# which is a real IMAP server implementation that imaplib talks to in-process.
#
# © Manish Jagdish Thatte
from __future__ import annotations
