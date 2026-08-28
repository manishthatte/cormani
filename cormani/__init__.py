# SPDX-License-Identifier: GPL-3.0-or-later
#
# corMani — a correspondence client.
#
# Package metadata only. Nothing here imports Qt: the module is loaded by the
# test suite, by packaging tools and by the command line, and only one of those
# has a display.
#
# © Manish Jagdish Thatte
"""corMani — mail, calendar and correspondence tracking."""

__version__ = "0.1.0"

# Used for XDG paths, the keyring service name, the D-Bus name and the Qt
# application name. Changing it moves the user's data, so it is defined once.
APP_ID = "cormani"
APP_NAME = "corMani"
APP_ORG = "Manish Jagdish Thatte"
