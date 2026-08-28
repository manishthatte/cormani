# SPDX-License-Identifier: GPL-3.0-or-later
#
# Demo data, so that stage 1 can be built and tested before stage 2 exists.
#
# Stage 1 is the shell over real models. Without messages there is nothing to
# check: a three-line row, a density setting, a quick filter and a per-account
# colour are all claims about how the interface behaves with a list in it, and
# none of them can be verified against an empty table. This module makes the
# list, deterministically, so a test can assert on a row and a person can look
# at the window and judge it.
#
# DETERMINISTIC, AND SAID TWICE BECAUSE IT MATTERS. No randomness anywhere. Two
# installs from the same base time produce byte-identical rows, so a test may
# assert "the fourth row is flagged" and a screenshot from last week can be
# compared with one from today. A generator seeded from the clock would make
# every failure a possibly-real one.
#
# IT REFUSES TO RUN OVER REAL DATA. `install` raises if the store already holds
# an account. The application goes further and puts the demo store in the CACHE
# directory under a different filename, so the real one is not merely protected
# by this check but never opened at all — see app.py. Belt and braces, because
# the failure mode is a user's mail store with eleven fictional accounts in it.
#
# The addresses are fictional (`*.example`, RFC 2606) but their lengths and
# provider mix match a fifteen-account rail — the layout problem the demo is
# for. The correspondents are invented.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
import zlib

from . import accounts as accounts_repo
from . import calendarfixtures
from . import contactfixtures
from . import rulefixtures
from . import viewfixtures
from . import trackfixtures
from . import folders as folders_repo
from . import ingest
from . import tags as tags_repo
from . import threads
from .database import get_meta, set_meta, utc_now

DEMO_META_KEY = "demo_fixtures"
# WHICH EDITION OF THE DEMO DATA A STORE HOLDS, and it exists because the demo
# store is CACHED. `app.open_store` installs the fixtures once and then never
# looks again, so every later improvement to them is invisible to anybody whose
# demo store predates it — which is how a store built before stage 5 sat there
# through the whole of stages 5 and 6 with 197 messages, no calendars and no
# tracked threads, demonstrating exactly the emptiness those stages fixed.
#
# BUMP IT WHENEVER THE FIXTURES CHANGE MATERIALLY. A store behind this number
# is rebuilt from scratch, which is safe because a demo store is disposable by
# construction: it lives in the CACHE directory and holds nothing of anybody's.
DEMO_VERSION_KEY = "demo_fixtures_version"
FIXTURE_VERSION = 7

# Fixed, so every install is identical. Any date arithmetic below is relative
# to this and never to the clock.
BASE_TIME = "2026-08-25T09:00:00+00:00"

# (group name, ordered)
_GROUPS = ("manitlab", "Saptarang", "idlidu", "personal")

# (address, provider, display name, group)
_ACCOUNTS = (
    ("owner@manitlab.example", "google", "manitlab", "manitlab"),
    ("admin@nanomani.example", "google", "nanoMani", "manitlab"),
    ("saptarang@outlook.example", "microsoft", "Saptarang", "Saptarang"),
    ("saptarangphotography@mail.example", "google", "Saptarang Photography", "Saptarang"),
    ("saptarang.hitech@mail.example", "google", "Saptarang Hi-Tech", "Saptarang"),
    ("admin@idlidu.example", "google", "idlidu", "idlidu"),
    ("idlidultd@mail.example", "google", "idlidu Ltd", "idlidu"),
    ("owner.busy@mail.example", "google", "Manish (gmail)", "personal"),
    ("owner.alt@hotmail.example", "microsoft", "Manish (hotmail)", "personal"),
    ("colleague.a@mail.example", "google", "Krishna (gmail)", "personal"),
    ("colleague.b@hotmail.example", "microsoft", "Krishna (hotmail)", "personal"),
)

# (path, display, role). The paths are the shapes the two providers actually
# use, including Gmail's bracketed ones, because a rail that looks right against
# tidy names and wrong against real ones has tested nothing.
_FOLDERS = (
    ("INBOX", "Inbox", folders_repo.ROLE_INBOX),
    ("[Gmail]/Drafts", "Drafts", folders_repo.ROLE_DRAFTS),
    ("[Gmail]/Sent Mail", "Sent", folders_repo.ROLE_SENT),
    ("[Gmail]/All Mail", "Archive", folders_repo.ROLE_ARCHIVE),
    ("[Gmail]/Spam", "Junk", folders_repo.ROLE_JUNK),
    ("[Gmail]/Trash", "Trash", folders_repo.ROLE_TRASH),
)

_MS_FOLDERS = (
    ("INBOX", "Inbox", folders_repo.ROLE_INBOX),
    ("Drafts", "Drafts", folders_repo.ROLE_DRAFTS),
    ("Sent Items", "Sent", folders_repo.ROLE_SENT),
    ("Archive", "Archive", folders_repo.ROLE_ARCHIVE),
    ("Junk Email", "Junk", folders_repo.ROLE_JUNK),
    ("Deleted Items", "Trash", folders_repo.ROLE_TRASH),
)

# (name, org, email). Present in the address book, which is what the Quick
# Filter's Contact toggle tests against.
_CONTACTS = (
    ("Lyle Gordon", "Covalent Example", "lyle.gordon@covalent.example"),
    ("Frances Baker", "M2016 Labs", "f.baker@m2016labs.co.uk"),
    ("Dr. Sarita Rane", "IIT Bombay", "s.rane@iitb.ac.in"),
    ("Anil Kulkarni", "Saptarang Trust", "anil@saptarang-trust.org"),
    ("Priya Deshpande", "idlidu Ltd", "priya@idlidu.example"),
    ("Tom Whitfield", "Northgate Print", "tom@northgateprint.co.uk"),
    ("Meera Iyer", "Bharat Nano", "meera.iyer@bharatnano.in"),
    ("Registrar", "Companies House", "enquiries@companieshouse.gov.uk"),
)

# An address that has already refused mail. Carried in the fixtures because the
# bounce guard at stage 4 needs something to refuse, and because a handle whose
# status is 'bounced' is the state the contact card must render correctly.
_BOUNCED = ("j.harrington@covalent.example", "550 5.1.1 unknown recipient")

# (account index, role, from name, from addr, subject, preview, days ago, hour,
#  seen, flagged, answered, attachment, tag shortcut or 0)
_MESSAGES = (
    (0, "inbox", "Lyle Gordon", "lyle.gordon@covalent.example",
     "Re: DWCNT wavelength question",
     "Happy to provide some pricing options against quantity once the question "
     "of wavelengths is answered.", 4, 15, 0, 0, 0, 1, 1),
    (0, "inbox", "Frances Baker", "f.baker@m2016labs.co.uk",
     "RE: M2016L3 inner diameter",
     "Our lab confirms the inner diameter is within the tolerance you quoted, "
     "and the TDS is attached.", 1, 11, 0, 1, 0, 1, 1),
    (0, "inbox", "Dr. Sarita Rane", "s.rane@iitb.ac.in",
     "Ternary logic reading group",
     "We are restarting the reading group in September and wondered whether "
     "you would speak on balanced ternary.", 2, 9, 0, 0, 0, 0, 4),
    (0, "inbox", "Meera Iyer", "meera.iyer@bharatnano.in",
     "Nanotube supply — revised quotation",
     "Revised quotation attached. The lead time has come down to six weeks.",
     6, 14, 1, 0, 1, 1, 0),
    (0, "inbox", "arXiv Daily", "no-reply@arxiv.org",
     "arXiv: cond-mat.mes-hall digest",
     "Fourteen new submissions in your subscribed areas.", 0, 6, 1, 0, 0, 0, 0),
    (0, "sent", "Lyle Gordon", "lyle.gordon@covalent.example",
     "Re: DWCNT wavelength question",
     "The wavelengths we need are 1064 and 785. Could you quote both?",
     5, 10, 1, 0, 0, 0, 0),
    # Written to and not tracked, deliberately: it is what puts something in
    # the triage queue's NARROW scope, which is the one a person opens. A demo
    # whose default queue is empty demonstrates the queue not at all.
    #
    # KAVITA AND NOT A CORRESPONDENT WHO IS ALSO A FILLER SENDER. Meera Iyer
    # and Frances Baker are both in `_FILLER_SENDERS`, so writing to either
    # puts them in `wrote_to` and drags twenty-eight generated messages into
    # the queue behind the one that belongs there.
    (3, "sent", "Kavita Joshi", "kavita@photocircle.in",
     "Re: Print sizes for the Diwali set",
     "A2 for the six large ones and A3 for the rest, please.",
     6, 16, 1, 0, 0, 0, 0),
    (0, "archive", "Lyle Gordon", "lyle.gordon@covalent.example",
     "Covalent Example — capability statement",
     "Attaching our capability statement as promised.", 21, 13, 1, 0, 1, 1, 0),
    (0, "drafts", "Dr. Sarita Rane", "s.rane@iitb.ac.in",
     "Re: Ternary logic reading group",
     "Thank you for the invitation — I would be glad to. A few dates that work",
     1, 22, 1, 0, 0, 0, 0),

    (1, "inbox", "Registrar", "enquiries@companieshouse.gov.uk",
     "Confirmation statement due 14 September 2026",
     "Your confirmation statement is due. File it online to avoid a late "
     "filing penalty.", 3, 8, 0, 1, 0, 0, 1),
    (1, "inbox", "Tom Whitfield", "tom@northgateprint.co.uk",
     "Proof: nanoMani datasheet",
     "The proof is attached. Let me know about the bleed on page 2.",
     8, 16, 1, 0, 0, 1, 0),
    (1, "sent", "Tom Whitfield", "tom@northgateprint.co.uk",
     "Re: Proof: nanoMani datasheet",
     "The bleed looks right to me. Please proceed.", 7, 18, 1, 0, 0, 0, 0),

    (2, "inbox", "Anil Kulkarni", "anil@saptarang-trust.org",
     "Diwali programme — venue confirmation",
     "The hall is confirmed for both evenings. We need the running order by "
     "the end of the month.", 2, 19, 0, 0, 0, 0, 4),
    (2, "inbox", "Anil Kulkarni", "anil@saptarang-trust.org",
     "Re: Diwali programme — venue confirmation",
     "Also, the sound engineer has asked whether we need the second rig.",
     1, 20, 0, 0, 0, 0, 0),
    (2, "inbox", "Ticket Office", "boxoffice@thehexagon.co.uk",
     "Your booking reference SPT-40218",
     "This confirms your provisional booking for two evenings in October.",
     11, 12, 1, 0, 0, 1, 0),
    (2, "sent", "Anil Kulkarni", "anil@saptarang-trust.org",
     "Running order — first draft",
     "First draft of the running order attached. Comments welcome by Friday.",
     9, 21, 1, 0, 0, 1, 0),

    (3, "inbox", "Kavita Joshi", "kavita@photocircle.in",
     "Print sizes for the Diwali set",
     "Which sizes do you want for the exhibition set? A2 and A3 are both "
     "possible at this resolution.", 5, 10, 0, 0, 0, 0, 0),
    (3, "inbox", "Adobe", "no-reply@adobe.com",
     "Your subscription renews on 12 September",
     "Your Photography plan renews automatically.", 12, 7, 1, 0, 0, 0, 0),

    (4, "inbox", "Priya Deshpande", "priya@idlidu.example",
     "Hi-Tech stand — floor plan",
     "The floor plan puts us next to the entrance this year. Attached.",
     6, 11, 0, 0, 0, 1, 0),
    (4, "inbox", "Exhibition Team", "stands@techexpo.in",
     "Stand allocation confirmed",
     "Your stand allocation for the November exhibition is confirmed.",
     14, 15, 1, 0, 1, 0, 0),

    (5, "inbox", "Priya Deshpande", "priya@idlidu.example",
     "Payroll — August",
     "August payroll is ready for approval. Nothing unusual this month.",
     3, 9, 0, 1, 0, 1, 1),
    (5, "inbox", "HMRC", "noreply@hmrc.gov.uk",
     "VAT return due 7 September 2026",
     "Your VAT return for the period ending 31 July is due.", 4, 6, 0, 0, 0, 0, 1),
    (5, "sent", "Priya Deshpande", "priya@idlidu.example",
     "Re: Payroll — August", "Approved. Please process.", 2, 17, 1, 0, 0, 0, 0),
    (5, "trash", "Marketing", "offers@supplies-direct.co.uk",
     "50% off office supplies this week only",
     "Our biggest sale of the year ends Friday.", 10, 13, 1, 0, 0, 0, 0),

    (6, "inbox", "Priya Deshpande", "priya@idlidu.example",
     "Supplier invoices — week 34",
     "Three invoices this week, all within the agreed terms.", 7, 10, 1, 0, 1, 1, 0),
    (6, "inbox", "Companies House", "enquiries@companieshouse.gov.uk",
     "Accounts due 30 September 2026",
     "Your annual accounts are due at the end of September.", 5, 8, 0, 1, 0, 0, 1),

    (7, "inbox", "Dr. Sarita Rane", "s.rane@iitb.ac.in",
     "Re: Visiting Bombay in October",
     "That week works. I will book a room at the guest house if you confirm "
     "the dates.", 0, 8, 0, 0, 0, 0, 4),
    (7, "inbox", "Frances Baker", "f.baker@m2016labs.co.uk",
     "Draft paper — comments",
     "I have marked up sections 3 and 4. Nothing structural.", 1, 16, 0, 0, 0, 1, 0),
    (7, "inbox", "GitHub", "noreply@github.com",
     "[demo-org/demo-app] New issue: LLVM backend float remainder",
     "A new issue was opened in a repository you watch.", 2, 21, 1, 0, 0, 0, 0),
    (7, "inbox", "Bank", "alerts@bank.example",
     "Your statement is ready",
     "Your monthly statement is available to download.", 9, 5, 1, 0, 0, 0, 0),
    (7, "sent", "Dr. Sarita Rane", "s.rane@iitb.ac.in",
     "Re: Visiting Bombay in October",
     "The 12th to the 19th suits me. Shall I book?", 0, 9, 1, 0, 0, 0, 0),
    (7, "archive", "Meera Iyer", "meera.iyer@bharatnano.in",
     "Introduction — Bharat Nano",
     "Good to meet you at the conference. Here is the introduction I promised.",
     45, 11, 1, 0, 1, 0, 0),

    (8, "inbox", "Insurance", "renewals@shieldcover.co.uk",
     "Home insurance renewal — 2 October",
     "Your policy renews on 2 October. The premium has increased by 4%.",
     6, 9, 0, 0, 0, 1, 5),
    (8, "inbox", "Council Tax", "billing@council.example.gov.uk",
     "Your bill for 2026/27", "The revised bill is attached.", 20, 10, 1, 0, 0, 1, 0),

    (9, "inbox", "Krishna Thatte", "krishna.thatte@example.com",
     "Photos from Sunday",
     "Sending the photos from Sunday — the light was better than I expected.",
     3, 20, 0, 0, 0, 1, 3),
    (9, "inbox", "Library", "noreply@library.example.org",
     "Item due back on 1 September", "One item is due back next week.",
     4, 12, 1, 0, 0, 0, 0),

    (10, "inbox", "Sports Club", "membership@club.example",
     "Membership renewal", "Your membership is due for renewal this month.",
     15, 14, 1, 0, 0, 0, 0),
    (10, "inbox", "Krishna Thatte", "krishna.thatte@example.com",
     "Re: Weekend", "Either day works. Let me know which.", 2, 18, 0, 0, 0, 0, 0),
)

# Filler for the busiest account — index 7 (`owner.busy@mail.example`), which
# carries enough generated traffic that the list pane has been seen scrolling.
_FILLER_SENDERS = (
    ("Notifications", "noreply@service.example"),
    ("Mailing list", "list@discuss.example.org"),
    ("Meera Iyer", "meera.iyer@bharatnano.in"),
    ("Frances Baker", "f.baker@m2016labs.co.uk"),
    ("Newsletter", "news@weekly.example"),
    ("Support", "support@vendor.example"),
)
_FILLER_SUBJECTS = (
    "Weekly digest", "Re: measurement run", "Your receipt",
    "Re: sample dispatch", "Scheduled maintenance", "Re: draft figures",
    "Monthly summary", "Re: conference travel",
)
FILLER_COUNT = 160


def _iso(base: dt.datetime, days_ago: int, hour: int) -> str:
    when = (base - dt.timedelta(days=days_ago)).replace(
        hour=hour, minute=(hour * 7) % 60, second=0, microsecond=0)
    return when.isoformat()


def _body(preview: str, sender: str) -> str:
    """A body long enough that the reading pane has something to lay out, built
    from the preview so the two never disagree."""
    return (f"{preview}\n\n"
            "I have kept this short — the full detail is in the attachment "
            "where there is one, and otherwise there is nothing more to it "
            "than the above.\n\n"
            f"Best regards,\n{sender}\n")


def is_demo(con: sqlite3.Connection) -> bool:
    return get_meta(con, DEMO_META_KEY) == "1"


def installed_version(con: sqlite3.Connection) -> int:
    """Which edition of the demo data this store holds. 0 for none, or for one
    installed before the number existed — which is the case that matters."""
    try:
        return int(get_meta(con, DEMO_VERSION_KEY) or 0)
    except (TypeError, ValueError):                          # pragma: no cover
        return 0


def is_current(con: sqlite3.Connection) -> bool:
    return is_demo(con) and installed_version(con) >= FIXTURE_VERSION


def install(con: sqlite3.Connection, *, base_time: str = BASE_TIME) -> dict:
    """Fill an empty store with demo data. Refuses a store that has accounts.

    The refusal is not a nicety. Eleven fictional accounts appearing in a real
    mail store is a mess that takes a person longer to unpick than it takes to
    lose confidence in the application.
    """
    existing = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
    if existing:
        raise RuntimeError(
            f"refusing to install demo data over a store that already holds "
            f"{existing} account(s)")

    base = dt.datetime.fromisoformat(base_time)
    group_ids = {name: accounts_repo.add_group(con, name) for name in _GROUPS}

    account_ids: list[int] = []
    folder_ids: list[dict[str, int]] = []
    for address, provider, display, group in _ACCOUNTS:
        account_id = accounts_repo.add_account(
            con, address, provider, display_name=display,
            group_id=group_ids[group],
            imap_host="imap.gmail.com" if provider == "google" else "outlook.office365.com",
            smtp_host="smtp.gmail.com" if provider == "google" else "smtp.office365.com")
        account_ids.append(account_id)
        layout = _FOLDERS if provider == "google" else _MS_FOLDERS
        folder_ids.append({
            role: folders_repo.ensure_folder(con, account_id, path,
                                             display_name=display_name, role=role)
            for path, display_name, role in layout})

    for name, org, email in _CONTACTS:
        cur = con.execute(
            "INSERT INTO contact (name, org, role, notes, status, created_at, "
            "updated_at) VALUES (?, ?, '', '', 'active', ?, ?)",
            (name, org, utc_now(), utc_now()))
        con.execute(
            "INSERT INTO handle (contact_id, kind, value, status, note, "
            "bounce_count, created_at) VALUES (?, 'email', ?, 'verified', '', 0, ?)",
            (cur.lastrowid, email, utc_now()))
    con.execute(
        "INSERT INTO handle (contact_id, kind, value, status, note, "
        "bounce_count, last_bounce_at, created_at) "
        "VALUES ((SELECT id FROM contact WHERE name = 'Lyle Gordon'), 'email', "
        "?, 'bounced', ?, 2, ?, ?)",
        (_BOUNCED[0], _BOUNCED[1], _iso(base, 30, 9), utc_now()))
    con.commit()

    by_shortcut = {t.shortcut: t.id for t in tags_repo.list_tags(con)
                   if t.shortcut is not None}
    written = 0

    for spec in _MESSAGES:
        (idx, role, name, addr, subject, preview, days, hour,
         seen, flagged, answered, attachment, tag_key) = spec
        message_id = _insert(
            con, folder_ids[idx][role], name, addr,
            _ACCOUNTS[idx][0], subject, preview, _iso(base, days, hour),
            seen, flagged, answered, attachment, role)
        if tag_key and tag_key in by_shortcut:
            tags_repo.set_on_messages(con, [message_id], by_shortcut[tag_key], True)
        written += 1

    busy = 7                                   # owner.busy@mail.example
    for n in range(FILLER_COUNT):
        name, addr = _FILLER_SENDERS[n % len(_FILLER_SENDERS)]
        subject = _FILLER_SUBJECTS[n % len(_FILLER_SUBJECTS)]
        role = "inbox" if n % 4 else "archive"
        _insert(con, folder_ids[busy][role], name, addr, _ACCOUNTS[busy][0],
                f"{subject} #{n + 1}",
                "Generated filler, so that the list pane is long enough to "
                "scroll and page.",
                _iso(base, 3 + n // 3, (7 + n) % 24),
                1 if n % 5 else 0, 1 if n % 23 == 0 else 0,
                1 if n % 7 == 0 else 0, 1 if n % 11 == 0 else 0, role)
        written += 1

    _fabricate_reply_headers(con)
    # The calendar half, in `calendarfixtures.py` because the 600-line rule
    # fired here and because it is one subject rather than an addition to this
    # one. It writes inside THIS transaction and does not commit: a demo store
    # with mail and no calendars, because the calendar half raised halfway
    # through, would pass `is_demo` and never be built again.
    calendar = calendarfixtures.install(con, account_ids, _ACCOUNTS, base)
    # And the tracking layer, in the same transaction and for the same reason.
    # LAST, because it files the mail written above through the real matchers:
    # a demo whose timelines were invented here would not notice a matcher
    # breaking, and noticing is most of what a fixture is for.
    tracked = trackfixtures.install(con, base)
    # And the saved searches, in this transaction too. Unlike `rulefixtures`
    # below, nothing here commits on its own: a saved view is one row in one
    # table, so it has no reason to leave the transaction the way a filter's
    # actions do.
    saved = viewfixtures.install(con)
    # And the address book's detail — the roles, the notes and the handles
    # that are not email. AFTER `trackfixtures`, which is what creates the
    # contacts for the people on its threads: filling in a card that does not
    # exist yet is a fixture that silently writes nothing, and
    # `contactfixtures.install` counts what it filled so the test can see it.
    book = contactfixtures.install(con)
    con.commit()
    set_meta(con, DEMO_META_KEY, "1")
    set_meta(con, DEMO_VERSION_KEY, str(FIXTURE_VERSION))
    con.commit()
    # AFTER the transaction and after the store is marked, which is the one
    # fixture that cannot be inside it: a filter's actions go through
    # `store/edits.py` and `store/tags.py`, and each of those commits because
    # each is one thing a person could have done by hand. `rulefixtures.py`
    # argues it at length. A failure there leaves a usable demo whose rules
    # have caught nothing; a failure inside the transaction would leave
    # accounts written with `is_demo` unset, which `install` then refuses to
    # build over.
    filtered = rulefixtures.install(con)
    return {"accounts": len(account_ids), "groups": len(group_ids),
            "messages": written, "contacts": book["contacts"],
            "calendars": calendar["calendars"], "events": calendar["events"],
            "threads": tracked["threads"], "touches": tracked["filed"]
            + tracked["logged"], "rules": filtered["rules"],
            "filtered": filtered["matched"], "views": saved["views"],
            "views_found": saved["found"],
            "contacts_filled": book["filled"],
            "contact_handles": book["handles"],
            "contacts_added": book["added"]}


def _fabricate_reply_headers(con: sqlite3.Connection) -> int:
    """Give the demo's replies the headers a real client would have sent.

    THE SUBJECT GROUPING HERE IS THE FIXTURE'S, NOT THE APPLICATION'S. Real mail
    arrives with References already in it and store/threads.py argues at length
    why a subject is not evidence of a conversation. But demo data has no sender
    to have written one, so this manufactures what that sender would have sent:
    within one account, messages sharing a base subject are treated as one
    exchange, oldest first, each replying to the one before it and all of them
    citing the first. The application then threads them the same way it threads
    anything else, which is the point — the demo must exercise the real code
    path and not a second one.
    """
    rows = con.execute("""
        SELECT m.id, m.message_id, m.date_at, LOWER(m.subject_base) AS base,
               f.account_id AS account_id
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE m.subject_base <> ''
        ORDER BY f.account_id, LOWER(m.subject_base), m.date_at, m.id
    """).fetchall()

    exchanges: dict[tuple, list] = {}
    for row in rows:
        exchanges.setdefault((row["account_id"], row["base"]), []).append(row)

    written = 0
    for members in exchanges.values():
        if len(members) < 2:
            continue
        chain = [members[0]["message_id"]]
        for row in members[1:]:
            con.execute(
                "UPDATE message SET in_reply_to = ?, references_ = ? WHERE id = ?",
                (chain[-1], " ".join(chain), row["id"]))
            chain.append(row["message_id"])
            written += 1
    con.commit()
    threads.rethread(con)
    return written


def _insert(con: sqlite3.Connection, folder_id: int, from_name: str,
            from_addr: str, account_address: str, subject: str, preview: str,
            date_at: str, seen: int, flagged: int, answered: int,
            attachment: int, role: str) -> int:
    """Write one message. `from_name`/`from_addr` always name the OTHER person;
    for a sent or draft row the direction is flipped here, so the caller never
    has to remember which way round a folder faces."""
    outgoing = role in (folders_repo.ROLE_SENT, folders_repo.ROLE_DRAFTS)
    correspondent = from_name or from_addr
    to_addrs = from_addr if outgoing else account_address
    if outgoing:
        from_name, from_addr = "", account_address

    base_subject = subject
    for prefix in ("Re: ", "RE: ", "Fwd: ", "FW: "):
        if base_subject.startswith(prefix):
            base_subject = base_subject[len(prefix):]

    # crc32, not hash(): Python salts string hashing per process, so hash() here
    # would give a different Message-ID on every install and quietly break the
    # determinism this module exists to provide.
    token = zlib.crc32(f"{folder_id}|{subject}|{date_at}".encode("utf-8"))
    cur = con.execute("""
        INSERT INTO message (folder_id, uid, message_id, in_reply_to,
            references_, thread_key, date_at, received_at, from_name, from_addr,
            to_addrs, cc_addrs, bcc_addrs, reply_to, subject, subject_base,
            body_text, body_html, preview, size_bytes, has_attachment,
            seen, flagged, answered, draft, deleted, pending_flags)
        VALUES (?, NULL, ?, '', '', ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, ?, '',
                ?, ?, ?, ?, ?, ?, ?, 0, '')
    """, (folder_id, f"<demo-{token:08x}@cormani>", base_subject.lower(),
          date_at, date_at, from_name, from_addr, to_addrs, subject,
          base_subject, _body(preview, correspondent), preview,
          2048 + len(preview) * 37, attachment, seen, flagged, answered,
          1 if role == folders_repo.ROLE_DRAFTS else 0))
    message_id = int(cur.lastrowid)
    # Indexed here rather than left out: an unindexed row is a row the search
    # index must never be told to delete, and `ingest._fts_forget` explains
    # what happens when it is. It also makes demo data searchable, which is
    # what stage 3 will expect of it.
    ingest.index_message(con, message_id)
    if attachment:
        con.execute("""
            INSERT INTO attachment (message_id, filename, content_type,
                content_id, size_bytes, part_number, stored_path, is_inline)
            VALUES (?, ?, 'application/pdf', '', ?, '2', '', 0)
        """, (message_id, f"{base_subject.split(' ')[0].lower()}.pdf",
              128000 + message_id * 311))
    return message_id
