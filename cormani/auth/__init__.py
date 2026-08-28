# SPDX-License-Identifier: GPL-3.0-or-later
#
# Proving to a server that the mail is ours to read.
#
# Two mechanisms, and the choice is the provider's rather than corMani's:
# OAuth2 with SASL XOAUTH2, and an app password over LOGIN. Google still
# permits app passwords for mail and does NOT permit them for calendar, so an
# account that only reads mail can avoid OAuth entirely and an account with a
# calendar cannot — see docs/accounts.txt.
#
# Nothing in this package writes a secret anywhere except the system keyring,
# and nothing puts one in an exception message. `secrets/store.py` is the only
# door.
#
# © Manish Jagdish Thatte
from __future__ import annotations
