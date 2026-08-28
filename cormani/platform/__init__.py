# SPDX-License-Identifier: GPL-3.0-or-later
#
# Everything that differs between Linux and Windows lives in this package, and
# nowhere else.
#
# The rule exists because portability decays invisibly. A single join against a
# fixed dot-directory in the user's home, or one SIGTERM, spreads until the
# Windows build is a rewrite. Keeping the differences behind two modules means
# the rest of the application never asks which system it is on.
#
# Nothing in this package imports Qt. Paths are needed before a QApplication
# exists, and the tests must run without a display.
#
# © Manish Jagdish Thatte
