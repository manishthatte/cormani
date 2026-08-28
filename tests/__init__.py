# SPDX-License-Identifier: GPL-3.0-or-later
#
# The test package.
#
# This directory is put on the import path so that `import support` works both
# under `python3 -m unittest discover -s tests -t .`, where these modules are
# imported as `tests.test_x`, and when a single file is run on its own, where
# there is no package at all. One line here rather than a two-branch import at
# the top of every test module.
#
# © Manish Jagdish Thatte
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
