# SPDX-License-Identifier: GPL-3.0-or-later
#
# Sending, which is the other protocol.
#
# `imap/` reads and `smtp/` writes, and they meet only at the account row and
# the credential. Kept apart because they fail differently and are configured
# differently: a server that accepts IMAP on 993 may want submission on 587 with
# STARTTLS, and a token good for one is not always good for the other.
#
# © Manish Jagdish Thatte
