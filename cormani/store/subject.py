# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a subject is, once the prefixes are off.
#
# `Re: Fwd: Re: Invoice` is the subject `Invoice`, and the store keeps both: the
# subject as it arrived, and the base. Two things need the base and they are on
# opposite sides of the tree — `imap/envelope.py` when a message is parsed, and
# `store/drafts.py` when one is written — so it lives here, where the store may
# have it and the protocol layer may import it. The other direction is the one
# that is not allowed: the store never imports the protocol.
#
# THE PREFIXES ARE NOT ONLY ENGLISH. Fifteen accounts' correspondence arrives
# with `AW:` from German clients, `SV:` from Scandinavian ones and `TR:` from
# French ones, and a client that strips only `Re:` shows a subject with somebody
# else's language on the front of it — and, worse, threads by a base that has a
# prefix in it.
#
# IT STRIPS THEM ALL IN ONE PASS. `Re: Fwd: Re: x` is what a subject looks like
# after three clients have each added their own, and a rule that removed one
# would leave the other two.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re

# The reply and forward prefixes, in the languages the correspondence is in.
PREFIXES = ("re", "aw", "sv", "vs", "antw", "odp", "fwd", "fw", "wg", "tr",
            "rif", "enc", "res", "ref")

# `Re[2]:` is Outlook's count of how many times round it has been.
PREFIX_RE = re.compile(
    r"^\s*(?:(?:" + "|".join(PREFIXES) + r")\s*(?:\[\d+\])?\s*:\s*)+",
    re.IGNORECASE)


def strip_subject(subject: str) -> str:
    """`Re: Fwd: Re: Invoice` to `Invoice`, in one pass."""
    return PREFIX_RE.sub("", subject or "").strip()
