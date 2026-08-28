# SPDX-License-Identifier: GPL-3.0-or-later
#
# The calendar half: two provider APIs over HTTPS, and the sync that uses them.
#
# ON THE NAME. This package shadows the standard library's `calendar` module
# only inside its own directory, and not even there: Python 3 resolves every
# bare `import calendar` absolutely, so a module here that wants the standard
# library's month arithmetic gets it, and this package is only ever reached as
# `cormani.calendar`. The alternative — calling it `cal` or `caldav` — would be
# a name chosen to avoid a collision that does not happen, and PLAN.txt §4
# names this layer `calendar`.
#
# WHAT IS IN HERE AND WHAT IS NOT. This package speaks to Google Calendar and
# to Microsoft Graph, and it holds nothing that draws. The split is the same
# one `imap/` observes: the store never imports the protocol, the protocol
# never imports Qt, and everything below the interface is testable with no
# display and no network.
#
# © Manish Jagdish Thatte
