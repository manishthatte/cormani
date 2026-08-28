# SPDX-License-Identifier: GPL-3.0-or-later
#
# An invitation that arrived as mail, and what answering it means.
#
# There are two ways to answer, and which one is right depends on something the
# user should never have to think about:
#
# THROUGH THE CALENDAR, when this account has a calendar corMani syncs and that
# calendar already holds the meeting. The response goes to the provider's API,
# the provider files it and mails the organiser, and the event's own row shows
# the answer immediately. This is the good path and it is what happens for
# every Google and Microsoft account.
#
# THROUGH THE MAIL, otherwise. A plain IMAP account has no calendar API at all,
# and an invitation can arrive before the calendar sync has seen the meeting.
# The answer is then an iTIP REPLY — the same `text/calendar` part the
# invitation came in, carrying `METHOD:REPLY` and one ATTENDEE line — sent as
# an ordinary message through the outbox. Every mail client understands it,
# because it is how invitations were answered before there were APIs.
#
# THE FALLBACK IS NOT A LESSER ANSWER AND IS NOT PRESENTED AS ONE. It reaches
# the organiser either way; what it does not do is put the meeting in a
# calendar this application syncs, and the interface says which of the two
# happened rather than claiming success in the same words.
#
# THE REPLY IS QUEUED, NEVER SENT HERE. `SESSION_STATE.txt`'s decision holds:
# send means queue. Answering an invitation on a train saves a draft and an
# outbox op, and the next sync sends it.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..store import calendars as calendars_repo
from ..store import eventedits
from ..store import events as events_repo
from ..store.calendars import RESPONSE_LABELS
from . import itip
from .itip import Invitation

# The types an invitation arrives as. `application/ics` is what Google attaches
# beside the inline part, and `text/calendar` is the part itself; both hold the
# same document.
CALENDAR_TYPES = ("text/calendar", "application/ics", "text/x-vcalendar",
                  "application/x-vcalendar")

ANSWER_CALENDAR = "calendar"
ANSWER_MAIL = "mail"


@dataclass(frozen=True)
class Found:
    """An invitation, and everything needed to answer it."""

    message_id: int
    account_id: int
    address: str
    display_name: str
    invitation: Invitation
    event_id: int | None = None
    calendar_id: int | None = None

    @property
    def in_a_calendar(self) -> bool:
        return self.event_id is not None

    @property
    def my_response(self) -> str:
        return self.invitation.response_of(self.address)


@dataclass(frozen=True)
class Answer:
    kind: str
    response: str
    detail: str
    draft_id: int | None = None
    event_id: int | None = None


def _rows(con: sqlite3.Connection, message_id: int) -> list:
    marks = ",".join("?" * len(CALENDAR_TYPES))
    return con.execute(f"""
        SELECT * FROM attachment
        WHERE message_id = ?
          AND LOWER(SUBSTR(content_type, 1, INSTR(content_type || ';', ';') - 1))
              IN ({marks})
        ORDER BY id
    """, [int(message_id), *CALENDAR_TYPES]).fetchall()


def _account_of(con: sqlite3.Connection, message_id: int) -> tuple:
    row = con.execute("""
        SELECT a.id AS id, a.address AS address, a.display_name AS display_name
        FROM message m JOIN folder f ON f.id = m.folder_id
                       JOIN account a ON a.id = f.account_id
        WHERE m.id = ?
    """, (int(message_id),)).fetchone()
    return (int(row["id"]), row["address"], row["display_name"] or "") if row \
        else (0, "", "")


def find(con: sqlite3.Connection, message_id: int,
         root: Path | str | None) -> Found | None:
    """The invitation this message carries, if it carries one.

    Parsed on demand rather than at ingest, and that is a decision: a
    `text/calendar` part is already stored like any other attachment, the
    parse costs a millisecond, and the alternative is a column that would have
    to be backfilled for every message already in the store.
    """
    from ..store.attachments import stored_file

    account_id, address, display_name = _account_of(con, message_id)
    if not account_id:
        return None
    for row in _rows(con, message_id):
        try:
            text = stored_file(row, root).read_text(
                encoding="utf-8", errors="replace") if root else ""
        except (OSError, ValueError):
            continue
        invitation = itip.parse(text)
        if invitation is None or not invitation.uid:
            continue
        event_id, calendar_id = _local_event(con, account_id, invitation)
        return Found(message_id=int(message_id), account_id=account_id,
                     address=address, display_name=display_name,
                     invitation=invitation, event_id=event_id,
                     calendar_id=calendar_id)
    return None


def _local_event(con: sqlite3.Connection, account_id: int,
                 invitation: Invitation) -> tuple:
    """The row in this account's calendars that the invitation names.

    Joined on the iCalendar UID, which is the identity the invitation and the
    provider's own copy of the event share. `event.ical_uid` is indexed for
    exactly this, and it is why migration 6 carries the column at all.
    """
    for event in events_repo.by_ical_uid(con, invitation.uid,
                                         account_id=account_id):
        return event.id, event.calendar_id
    return None, None


# ------------------------------------------------------------------ answering
def answer(con: sqlite3.Connection, found: Found, response: str, *,
           comment: str = "", root: Path | str | None = None,
           now=None) -> Answer:
    """Answer an invitation the best way this account can.

    Never raises for the ordinary refusals — an account with no calendar is
    not an error — and always says which of the two routes was taken.
    """
    label = RESPONSE_LABELS.get(response, response)
    if found.in_a_calendar:
        eventedits.set_response(con, found.event_id, response, comment=comment)
        calendar = calendars_repo.get_calendar(con, found.calendar_id)
        return Answer(
            kind=ANSWER_CALENDAR, response=response, event_id=found.event_id,
            detail=f"{label} — the answer will reach the organiser through "
                   f"{calendar.label if calendar else 'the calendar'} on the "
                   f"next sync")
    draft_id = queue_reply(con, found, response, comment=comment, root=root,
                           now=now)
    return Answer(
        kind=ANSWER_MAIL, response=response, draft_id=draft_id,
        detail=f"{label} — this meeting is not in a calendar corMani syncs, "
               f"so the reply has been queued to go to "
               f"{found.invitation.organiser_addr or 'the organiser'} by mail")


def queue_reply(con: sqlite3.Connection, found: Found, response: str, *,
                comment: str = "", root: Path | str | None = None,
                now=None) -> int | None:
    """Save the iTIP reply as a draft and put it in the outbox.

    Returns the draft's row id, or None when there is no organiser to answer —
    which happens with a `PUBLISH` document that was never an invitation at
    all, and which is reported rather than sent to nobody.
    """
    from ..compose.draft import Attachment, Draft
    from ..smtp import outbox
    from ..store import drafts as drafts_repo

    invitation = found.invitation
    if not invitation.organiser_addr:
        return None
    text = itip.build_reply(invitation, found.address, response,
                            name=found.display_name, comment=comment, now=now)
    path = _write_reply(root, found, text)
    body = _body(invitation, response, comment)
    draft = Draft(account_id=found.account_id, from_address=found.address,
                  from_name=found.display_name,
                  to=invitation.organiser_addr,
                  subject=itip.reply_subject(invitation, response), body=body,
                  attachments=((Attachment(
                      path=str(path), filename="reply.ics",
                      # The method is what makes this an ANSWER rather than an
                      # attached file; `compose/build.py` carries the
                      # parameter through to the header.
                      content_type=f"{itip.CONTENT_TYPE}; "
                                   f"method={itip.METHOD_REPLY}; charset=UTF-8"),)
                               if path else ()))
    row_id, _ = drafts_repo.save(con, draft)
    outbox.queue(con, row_id)
    return row_id


def _write_reply(root: Path | str | None, found: Found, text: str) -> Path | None:
    """Put the reply where the outbox will find it at send time.

    Under the attachments root rather than a temporary directory, and the
    reason is the queue: a draft written on a train is sent when there is a
    network, which may be after a restart and after everything in /tmp has
    gone. The path is derived from identifiers alone and checked against the
    root, the same rule every other stored file follows.
    """
    if not root:
        return None
    from ..store.ingest import attachment_path

    target = attachment_path(Path(root), found.account_id, found.message_id, 0,
                             "reply.ics")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _body(invitation: Invitation, response: str, comment: str) -> str:
    """A sentence a person can read, above the part a program reads.

    Not decoration: an organiser whose client does not understand iTIP sees
    only this, and "Accepted: Reading group" with an empty body reads as a
    mistake.
    """
    label = RESPONSE_LABELS.get(response, response)
    lines = [f"{label}: {invitation.summary or '(no subject)'}"]
    if invitation.starts_at:
        lines.append(f"When: {invitation.starts_at}")
    if comment:
        lines.extend(["", comment])
    return "\n".join(lines) + "\n"


def cancelled_events(con: sqlite3.Connection, found: Found) -> int:
    """Act on a CANCEL that arrived by mail. Returns the rows removed.

    An organiser's cancellation reaches the store twice — here, and as a
    deletion in the next calendar sync — and either may be first. Doing it
    here as well is what makes a cancellation that arrives at nine o'clock
    disappear from the day at nine o'clock rather than at the next sync.
    """
    if not found.invitation.is_cancellation:
        return 0
    removed = 0
    for event in events_repo.by_ical_uid(con, found.invitation.uid,
                                         account_id=found.account_id):
        removed += events_repo.forget_remote(con, event.calendar_id,
                                             [event.remote_id], commit=False)
    con.commit()
    return removed
