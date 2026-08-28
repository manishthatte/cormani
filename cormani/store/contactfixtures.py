# SPDX-License-Identifier: GPL-3.0-or-later
#
# The demo's address book: the handles a card is FOR.
#
# Beside `fixtures.py` for the reason `calendarfixtures.py`, `trackfixtures.py`,
# `rulefixtures.py` and `viewfixtures.py` are: one subject rather than an
# addition to another one.
#
# ── WHAT `fixtures.py` ALREADY DID, AND WHAT IT COULD NOT ──────────────────
#
# Eight contacts have existed since stage 1, each with one email address, and
# one of them carries a bounced handle with a comment saying it is there
# "because a handle whose status is 'bounced' is the state the contact card
# must render correctly". That was written four stages before there was a card.
# What those eight cannot show is the thing PLAN.txt §2 asks for by name —
# "contact cards carrying every handle a person has: addresses, numbers,
# profiles" — because every one of them has exactly one handle of one kind. A
# demo of the address book built from them would show a list of names beside a
# panel with one line in it, which is the interface in the state somebody would
# be in if the feature had not been built.
#
# ── SO THIS FILLS THEM IN RATHER THAN ADDING MORE PEOPLE ───────────────────
#
# Roles, organisations that were missing, notes, and the non-email handles.
# `store/contacts.SEED_KINDS` names seven kinds beyond email and six of them
# appear here, because a card is the ONLY surface in corMani where a WhatsApp
# number and a LinkedIn profile are visible at all — the tracking layer stores
# a channel on a touch, and the panels are browsers.
#
# ── TWO PEOPLE ARE ADDED, AND EACH IS A STATE NOTHING ELSE PRODUCES ────────
#
# A contact with NO email address — a card that exists for a telephone number,
# which the composer can never offer and which `--check` counts — and a
# DUPLICATE of somebody already here, which is what `store/addressbook.duplicates`
# is for and what the Merge button acts on. Neither can arise from the demo's
# mail, because a contact made from a message always has the address it was
# made from and never has a twin: `handle` is UNIQUE on (kind, value).
#
# ── AND NOTHING HERE IS ON A THREAD ────────────────────────────────────────
#
# `store/trackfixtures.py` carries the warning and it applies to this file
# unchanged: Frances Baker and Meera Iyer are also filler senders, so putting
# either on a thread files a hundred and sixty generated messages onto it.
# Giving somebody a telephone number is not putting them on a thread, and this
# file writes only to `contact` and `handle` — but the person who adds the next
# fixture here should know why it stops there.
#
# ── COUNTED AS THEY ARE WRITTEN ────────────────────────────────────────────
#
# `viewfixtures.py`'s rule. `fixtures.install` reports what it installed and a
# report that is one short is a report nobody can check against.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3

from . import contacts as contacts_repo

# (email address of the contact to fill in, role, notes)
# Keyed on the ADDRESS and not the name, because the address is what
# `fixtures._CONTACTS` and `trackfixtures._PEOPLE` agree on — the name in one
# is "Dr. Sarita Rane" and in the other is the same string only by luck.
_DETAIL = (
    ("lyle.gordon@covalent.example", "Sales engineer",
     "Quotes against quantity; wants the wavelength settled first."),
    ("f.baker@m2016labs.co.uk", "Laboratory manager", ""),
    ("s.rane@iitb.ac.in", "Professor, Physics",
     "Reading group convener. October slot agreed."),
    ("anil@saptarang-trust.org", "Trustee", "WhatsApp is the fastest way."),
    ("priya@idlidu.example", "Bookkeeper", "VAT quarters; rings rather than writes."),
    ("tom@northgateprint.co.uk", "Account manager", ""),
    ("meera.iyer@bharatnano.in", "Head of supply", ""),
    ("enquiries@companieshouse.gov.uk", "Filing enquiries",
     "No individual — the enquiries desk."),
)

# (email address of the contact, kind, value, status)
# Six kinds beyond email, because the card is the only place any of them is
# visible. The numbers are in the reserved ranges: +44 7700 900xxx is Ofcom's
# drama range and 99999 xxxxx is TRAI's, so nothing here can be a real
# telephone — the same care `.invalid` addresses take in the tests.
_HANDLES = (
    ("lyle.gordon@covalent.example", "phone", "+44 7700 900123",
     contacts_repo.STATUS_VERIFIED),
    ("lyle.gordon@covalent.example", "linkedin", "in/lyle-gordon-covalent",
     contacts_repo.STATUS_UNVERIFIED),
    ("s.rane@iitb.ac.in", "phone", "+91 99999 10101",
     contacts_repo.STATUS_VERIFIED),
    ("s.rane@iitb.ac.in", "web", "https://www.phy.iitb.ac.in/~srane",
     contacts_repo.STATUS_UNVERIFIED),
    ("anil@saptarang-trust.org", "whatsapp", "+91 99999 20202",
     contacts_repo.STATUS_VERIFIED),
    ("anil@saptarang-trust.org", "phone", "+91 99999 20202",
     contacts_repo.STATUS_VERIFIED),
    ("priya@idlidu.example", "phone", "+91 99999 30303",
     contacts_repo.STATUS_VERIFIED),
    ("tom@northgateprint.co.uk", "phone", "+44 7700 900456",
     contacts_repo.STATUS_UNVERIFIED),
    ("meera.iyer@bharatnano.in", "linkedin", "in/meera-iyer-bharatnano",
     contacts_repo.STATUS_UNVERIFIED),
    ("f.baker@m2016labs.co.uk", "x", "@fbaker_m2016",
     contacts_repo.STATUS_UNVERIFIED),
    ("meera.iyer@bharatnano.in", "signal", "+91 99999 40404",
     contacts_repo.STATUS_UNVERIFIED),
)

# Somebody with no email address at all. `--check` counts these and the card
# says so, and nothing in a mailbox can produce one.
_NO_ADDRESS = ("Ravi at the unit", "Northgate Print", "Press operator",
               "Rings about proofs. No email — the works telephone only.",
               "phone", "+44 7700 900789")

# And a second card for somebody already here, which is what Merge is for.
# The name matches exactly so that `addressbook.duplicates` reports it by its
# first rule, and it is the THIN card of the two — it has a name and one handle
# and nothing else — so the pair is offered the right way round.
_DUPLICATE = ("Tom Whitfield", "phone", "+44 7700 900457")


def install(con: sqlite3.Connection) -> dict:
    """Fill the demo's contacts in. Does not commit — `fixtures.install` owns
    the transaction, for the reason it owns the calendar's."""
    filled = 0
    for address, role, notes in _DETAIL:
        contact = contacts_repo.contact_for_address(con, address, commit=False)
        if contact is None:
            # Not an error and not silent either: the address book is built
            # from `fixtures._CONTACTS`, and a row here naming an address that
            # file no longer has would otherwise vanish without trace.
            continue
        fields = {"role": role}
        if notes:
            fields["notes"] = notes
        contacts_repo.update_contact(con, contact.id, commit=False, **fields)
        filled += 1

    handles = 0
    for address, kind, value, status in _HANDLES:
        contact = contacts_repo.contact_for_address(con, address, commit=False)
        if contact is None:
            continue
        contacts_repo.add_handle(con, contact.id, kind, value, status=status,
                                 commit=False)
        handles += 1

    name, org, role, notes, kind, value = _NO_ADDRESS
    telephone_only = contacts_repo.add_contact(con, name, org=org, role=role,
                                               notes=notes, commit=False)
    contacts_repo.add_handle(con, telephone_only, kind, value,
                             status=contacts_repo.STATUS_VERIFIED,
                             commit=False)

    name, kind, value = _DUPLICATE
    twin = contacts_repo.add_contact(con, name, commit=False)
    contacts_repo.add_handle(con, twin, kind, value, commit=False)

    # COUNTED FROM THE TABLE AND NOT ADDED UP. `viewfixtures.py`'s rule taken
    # one step further: a total assembled from `len(_HANDLES) + 2` is a claim
    # about what this function MEANT to write, and the two diverge the moment
    # a row here names an address `fixtures._CONTACTS` no longer has — which is
    # exactly the case the `continue` above is silent about.
    counted = contacts_repo.counts(con)
    return {"filled": filled, "written": handles + 2,
            "contacts": counted["contacts"], "handles": counted["handles"],
            "added": 2}
