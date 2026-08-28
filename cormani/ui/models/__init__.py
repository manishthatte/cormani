# SPDX-License-Identifier: GPL-3.0-or-later
#
# Item models: the layer between the store's repositories and the views.
#
# Kept in their own package because the split matters. A model here holds no
# SQL — it asks store/ for rows — and no drawing, which belongs to the delegate.
# What it does hold is the mapping from a row to Qt's roles, and that mapping is
# the thing two views must agree on if they are ever to show the same data.
#
# © Manish Jagdish Thatte
