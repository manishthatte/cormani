# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing mail: what a reply is, and what goes on the wire.
#
# Three files, and the split is the same one the rest of this tree uses. `draft`
# is what the user is writing, as data. `quote` derives one message from another
# — who a reply goes to, what its subject is, and how the original is quoted
# underneath. `build` turns a draft into the bytes an SMTP server is handed.
#
# NONE OF IT IMPORTS Qt OR A SOCKET. A reply's recipients are a question with a
# right answer, and answering it in a widget would mean the only way to test it
# is to open one.
#
# © Manish Jagdish Thatte
