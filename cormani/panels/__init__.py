# SPDX-License-Identifier: GPL-3.0-or-later
#
# The site panels: the messaging services that have no API.
#
# PLAN.txt §1 is the whole justification. WhatsApp, LinkedIn, X and Facebook
# have no interface for personal messaging — not restricted, not paid, ABSENT.
# What exists is a web application a person signs into, so corMani shows that
# web application and does nothing else with it. Reverse-engineered protocol
# clients get accounts banned, and the account is what is being protected.
#
# Nothing in this package reaches the store, and nothing in the store reaches
# it. A panel is a signed-in session on somebody else's site; CONVENTIONS.txt
# §7 requires it to be unable to touch a decade of correspondence, and the
# simplest way to guarantee that is for the code to have no path to it.
#
# `sites.py` is the registry — a site is a row, and adding one is a line.
# `profiles.py` owns the persistent profiles, which must outlive their pages.
# `unread.py` is the one thing corMani reads out of a page, and its limits.
#
# © Manish Jagdish Thatte
