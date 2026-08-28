# SPDX-License-Identifier: GPL-3.0-or-later
#
# What the command line does about the address book.
#
# The sixth file of the command line — `cli.py` is mail, `calcli.py` is
# calendars, `configure.py` is account setup, `rulecli.py` is filters,
# `viewcli.py` is saved searches — and beside them rather than inside any of
# them for the reason `viewcli.py` gives: the seam falls where the MODULE split
# does, and `store/contacts.py` and `store/addressbook.py` are one subject that
# none of the other five holds.
#
# ── WHY AN ADDRESS BOOK NEEDS A READ-OUT, WHICH IS NOT THE OBVIOUS REASON ──
#
# It is not that the pane cannot show it. It is that the address book is the
# one part of this store that is EDITED BY THINGS THAT ARE NOT THE ADDRESS
# BOOK: `store/ingest.py` marks a handle bounced from a delivery report,
# `store/attach.py` creates a contact when a person is put on a thread, and
# `store/touches.py` does the same from a message. So the interesting question
# is "what has happened to my contacts while I was not looking at them", and
# three of the numbers below answer exactly that — the bounces, the cards with
# no address, and the duplicates.
#
# A DUPLICATE IS THE ONE THAT MATTERS. Two cards for one person split their
# handles, so the composer's bounce guard checks one of them and the tracking
# layer files against the other, and neither is wrong in a way anything can
# see. `store/addressbook.duplicates` offers pairs and never merges, so
# somebody has to be told they exist.
#
# ── READ-ONLY AND OFFLINE, LIKE `--check`, `--calendars`, `--filters` AND
# `--searches` ────────────────────────────────────────────────────────────
#
# It opens the store read-only and speaks to nothing. `store/triage.py` had a
# READING path that wrote and `--check` is what found it; a report has to work
# when the disk is what is wrong.
#
# ── THE CORRESPONDENCE COUNTS ARE NOT PRINTED FOR EVERY CARD ───────────────
#
# `store/addressbook.correspondence` is a scan per contact and its header says
# the cost is affordable once, when a card opens. Two hundred contacts is two
# hundred scans, which is a report that takes a minute over a real mailbox to
# print a column nobody asked for. So it is printed for a NARROWED list only —
# `--contacts QUERY` — where the number of cards is one a person chose.
#
# NOTHING HERE IMPORTS QT.
#
# © Manish Jagdish Thatte
from __future__ import annotations

# The schema version at which the address book has everything this reads.
# EIGHT and not one, although `contact` and `handle` are migration 1: the
# suggestions join `wrote_to` and the card names tracked threads, and both
# arrived with the tracking layer. Guarded for the reason
# `viewcli.VIEWS_SCHEMA` is — `--check` must survive a store older than this
# stage, and above all one that has not been migrated because the migration is
# what is broken.
ADDRESS_BOOK_SCHEMA = 8

# Above this many cards, `--contacts` prints the list and not the detail. A
# report nobody reads to the end is a report nobody runs twice, and the
# per-card correspondence scan is what makes the long form expensive.
DETAIL_LIMIT = 40


# ------------------------------------------------------------------- report
def report(con) -> None:
    """The address-book lines of `--check`. Read-only; never raises.

    One line, and a further line for each of the three states that mean
    something is WRONG rather than something is so.
    """
    from .store import addressbook as book_repo

    counts = book_repo.summary(con)
    if not counts["contacts"]:
        return
    kinds = counts["kinds"]
    channels = ", ".join(f"{n} {kind}" for kind, n in kinds.items())
    print(f"  address book     {counts['contacts']} contacts, "
          f"{counts['handles']} handles ({channels})")

    bounced = counts["bounced"]
    if bounced:
        print(f"    {bounced} address has bounced and the composer will warn "
              f"about it" if bounced == 1 else
              f"    {bounced} addresses have bounced and the composer will "
              f"warn about them")
    if counts["no_email"]:
        # NOT the same fault as a bounce and said separately. A bounced address
        # is one that refused; no address at all is a card the composer can
        # never offer, and the remedy is different.
        blank = counts["no_email"]
        print(f"    {blank} contact has no email address, so nothing can be "
              f"written to them" if blank == 1 else
              f"    {blank} contacts have no email address, so nothing can be "
              f"written to them")
    if counts["duplicates"]:
        pairs = counts["duplicates"]
        print(f"    {pairs} possible duplicate — two cards for one person "
              f"split their handles (see --contacts)" if pairs == 1 else
              f"    {pairs} possible duplicates — two cards for one person "
              f"split their handles (see --contacts)")


# ----------------------------------------------------------------- contacts
def contacts(query: str = "") -> int:
    """`--contacts [QUERY]`: everybody, or the ones matching, and their
    handles."""
    from .app import current_paths
    from .store import contacts as contacts_repo
    from .store import database

    paths = current_paths()
    if not paths.database.exists():
        print(f"no store yet ({paths.database})")
        return 1
    con = database.connect(paths.database, read_only=True)
    try:
        if database.schema_version(con) < ADDRESS_BOOK_SCHEMA:
            print("this store predates the address book — start corMani once "
                  "to migrate it")
            return 1
        found = contacts_repo.list_contacts(con, query=query)
        if not found:
            print(f"nobody matches “{query}”" if query else
                  "no contacts yet — Tools ▸ Address book, then Add from mail, "
                  "offers the people you already write to")
            return 0
        detailed = len(found) <= DETAIL_LIMIT
        for contact in found:
            _print_contact(con, contact, detailed=detailed)
        _print_footer(con, found, query=query, detailed=detailed)
        return 0
    finally:
        con.close()


def _print_contact(con, contact, *, detailed: bool) -> None:
    from .store import addressbook as book_repo
    from .store import tracking as tracking_repo

    where = " · ".join(x for x in (contact.org, contact.role) if x)
    standing = "" if contact.status == "active" else f"   [{contact.status}]"
    print(f"\n{contact.label}{'   ' + where if where else ''}{standing}")
    for handle in contact.handles:
        mark = ""
        if handle.is_bounced:
            # THE SERVER'S OWN WORDS, quoted rather than summarised into a
            # status: "mailbox full" and "no such user" call for opposite
            # decisions. `contacts.describe_bounces` argues it for the
            # composer's warning and it is the same argument here.
            times = handle.bounce_count
            mark = ("   BOUNCED once" if times == 1
                    else f"   BOUNCED {times} times")
            if handle.note:
                mark += f": {handle.note}"
        elif handle.status == "verified":
            mark = "   verified"
        print(f"   {handle.kind:<9} {handle.value}{mark}")
    if not contact.handles:
        print("   (no way of reaching them)")
    if not detailed:
        return

    seen = book_repo.correspondence(con, contact)
    # `describe_mail` and not `seen.describe()`. This line said "no mail
    # either way" under a contact who has no ADDRESS — true and useless, and
    # the card two files away said the other thing about the same person.
    print(f"   mail      {book_repo.describe_mail(contact, seen)}"
          + (f", last {seen.last_at[:10]}" if seen.last_at else ""))
    threads = tracking_repo.threads_for_contact(con, contact.id)
    for thread in threads:
        print(f"   thread    {thread.title[:44]} — {thread.state}")


def _print_footer(con, found, *, query: str, detailed: bool) -> None:
    """What the list above does not say for itself."""
    from .store import addressbook as book_repo

    if not detailed:
        print(f"\n{len(found)} contacts, so the mail counts are left out: "
              f"each one is a scan of the message table. Narrow it — "
              f"--contacts NAME — to see them.")
    pairs = book_repo.duplicates(con)
    if pairs and not query:
        # NAMED and not counted, unlike in `--check`. A pair is only actionable
        # if you know which two, and the merge is in the address book pane —
        # this report is read-only and says so rather than offering to do it.
        print("\nPossibly the same person, which splits their handles between "
              "two cards:")
        for pair in pairs:
            keep = _label(con, pair.keep_id)
            drop = _label(con, pair.drop_id)
            print(f"   {keep} and {drop} — {pair.reason}")
        print("Merge them in Tools ▸ Address book; the fuller card is the one "
              "to keep, because a merge fills its empty fields from the other.")


def _label(con, contact_id: int) -> str:
    """One card, named so it can be told from the other one.

    THE NAME ALONE IS NOT ENOUGH AND THE COMMON CASE PROVES IT: the first rule
    `duplicates` applies is "these two have the same name", so printing the
    name twice reads "Tom Whitfield and Tom Whitfield", which names a pair
    nobody can act on. The organisation and the first handle are what differ,
    and a card with neither is named by its id rather than by nothing.
    """
    from .store import contacts as contacts_repo

    contact = contacts_repo.get_contact(con, contact_id)
    if contact is None:
        return f"contact {contact_id}"
    marks = [x for x in (contact.org,
                         contact.handles[0].value if contact.handles else "")
             if x]
    return (f"{contact.label} ({', '.join(marks)})" if marks
            else f"{contact.label} (#{contact.id}, nothing else on the card)")
