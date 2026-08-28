# SPDX-License-Identifier: GPL-3.0-or-later
#
# Reading the message list: what is in view, and in what order.
#
# Everything the list pane shows comes through here. That is the point of the
# module: the view asks for a scope, a set of filters, an order and — since
# stage 3 — a search, and gets rows. It reads and never writes; the two things
# that write a message row are named below.
#
# WHY A SCOPE OBJECT RATHER THAN A FOLDER ID. Half the useful views are not a
# folder — the unified inbox is fifteen folders, Owed is a question asked across
# them, and a saved search will be a predicate. Passing a folder id everywhere
# would mean every one of those becomes a special case in the view instead of a
# scope here.
#
# OWED IS DELIBERATELY THE SIMPLE VERSION. It answers "inbound, unanswered, not
# from me", which is honest and useful today. The real one — they replied last
# in a thread that may have crossed from email to WhatsApp to a phone call — is
# the tracking layer at stage 6, and needs threads that do not exist yet. The
# interface says which one it is showing rather than implying the other.
#
# A SEARCH IS A FOURTH AXIS, NOT A FOURTH SCOPE (stage 3). The box at the top
# of the window asks every folder of every account, which no views_mod.Scope describes —
# but the tab must still remember the rail row to come BACK to when the search
# is cleared. So a `search.Query` travels beside the scope rather than replacing
# it: while one is active it decides the WHERE clause, and clearing it costs
# nothing because the scope was never overwritten. `store/search.py` owns the
# query language and the index; this module owns which mail is in view.
#
# TWO WRITERS, AND NEITHER OF THEM IS HERE. `store/ingest.py` applies what the
# server said and queues nothing; `store/edits.py` applies what the USER did and
# every one of its functions owes the server a `pending_op`. A flag the server
# reported and had queued straight back at it is a loop that never settles, and
# keeping the two apart is what stops it — the split is that rule made
# structural. Each of those files argues its own half.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import email.utils
import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

from . import folders as folders_repo
from . import search as search_mod
from . import snooze as snooze_mod
from . import tags as tags_repo
from . import threads as threads_mod
from . import views as views_mod

# The pseudo-role. Not a folder anywhere, and not written to the folder table.
views_mod.ROLE_OWED = "owed"

views_mod.SORT_KEYS = ("date", "sender", "subject")

# The fourth order, and it is not in views_mod.SORT_KEYS because it is not always
# available: bm25 needs something matched against the index, so relevance means
# nothing outside a search and falls back to date rather than failing.
views_mod.SORT_RELEVANCE = "relevance"

_SORT_SQL = {
    "date": "m.date_at",
    "sender": "LOWER(COALESCE(NULLIF(m.from_name, ''), m.from_addr))",
    "subject": "LOWER(COALESCE(NULLIF(m.subject_base, ''), m.subject))",
}


def display_name(addresses: str) -> str:
    """The first of a stored address list, as a person reads it.

    Parsed rather than split on the comma. The column holds the RFC form, so a
    correspondent called `Raman, Priya` is stored quoted — and splitting would
    cut their name in half and show a surname as the whole recipient.
    """
    if not addresses:
        return ""
    try:
        pairs = email.utils.getaddresses([addresses])
    except Exception:                                        # pragma: no cover
        return addresses.split(",")[0].strip()
    for name, addr in pairs:
        if name or addr:
            return name or addr
    return addresses.split(",")[0].strip()


@dataclass(frozen=True)
class Row:
    id: int
    folder_id: int
    folder_role: str
    account_id: int
    account_label: str
    account_colour: str
    message_id: str
    from_name: str
    from_addr: str
    to_addrs: str
    subject: str
    # THE FOUR A REPLY NEEDS. They were not here until stage 4 and their
    # absence was silent: `quote.reply` read them off the Row, found nothing,
    # and produced a reply that dropped the Cc list, ignored Reply-To, put a
    # second "Re:" on the subject and cited a one-message References chain.
    # Every one of those is a worse message that still looks like a message.
    cc_addrs: str
    reply_to: str
    subject_base: str
    references_: str
    preview: str
    date_at: str
    seen: bool
    flagged: bool
    answered: bool
    draft: bool
    has_attachment: bool
    size_bytes: int
    tags: tuple = field(default=())
    # Where this message is. Carried on every row, but only DRAWN in search
    # results: in a folder the answer is the folder you are looking at, and in
    # a search it is half of what the reader needs to make sense of a hit.
    folder_name: str = ""
    folder_path: str = ""
    # The conversation this message is in — a message-id, from store/threads.py.
    # On the Row rather than looked up per row because the list groups by it,
    # and a query per row is how a list that scrolls becomes a list that stutters.
    thread_key: str = ""
    # False for a row shown only as part of a conversation — a reply of yours
    # in Sent, beneath the message in the inbox that it answers. It is in the
    # LIST but not in the VIEW, so a filter never chose it and an action on the
    # thread does not reach it.
    in_scope: bool = True
    # The line of body the search matched in, when it matched in the body.
    # Empty otherwise, and the list falls back to the stored preview — see
    # search.snippet for why a wrong snippet is worse than no snippet.
    snippet: str = ""

    @property
    def outgoing(self) -> bool:
        return self.folder_role in (folders_repo.ROLE_SENT, folders_repo.ROLE_DRAFTS)

    @property
    def correspondent(self) -> str:
        """The other person, whichever direction the message went."""
        if self.outgoing:
            return display_name(self.to_addrs) or self.from_name or self.from_addr
        return self.from_name or self.from_addr

    @property
    def subject_label(self) -> str:
        return self.subject or "(no subject)"

    @property
    def folder_label(self) -> str:
        """The folder's own name — the ROLE's name when it has one, so that
        fifteen accounts' inboxes all read "Inbox" rather than "INBOX",
        "Inbox" and "[Gmail]/All Mail" depending on the server."""
        return (folders_repo.ROLE_LABELS.get(self.folder_role)
                or folders_repo.label_for(self.folder_name, self.folder_path))

    @property
    def location(self) -> str:
        return f"{self.folder_label} · {self.account_label}"


_COLUMNS = """
    m.id, m.folder_id, m.message_id, m.from_name, m.from_addr,
           m.to_addrs, m.cc_addrs, m.reply_to, m.subject, m.subject_base,
    m.references_, m.preview, m.date_at, m.seen, m.flagged,
           m.answered, m.draft, m.has_attachment, m.size_bytes, m.thread_key,
           f.role AS folder_role, f.account_id AS account_id,
           f.display_name AS folder_name, f.path AS folder_path,
           COALESCE(NULLIF(a.display_name, ''), a.address) AS account_label,
           a.colour AS account_colour
"""

_FROM = """
    FROM message m
    JOIN folder f ON f.id = m.folder_id
    JOIN account a ON a.id = f.account_id
"""

_SELECT = f"SELECT {_COLUMNS} {_FROM}"


def _row(r: sqlite3.Row, tags: tuple = (), snippet: str = "",
         in_scope: bool = True) -> Row:
    return Row(
        id=r["id"], folder_id=r["folder_id"], folder_role=r["folder_role"],
        account_id=r["account_id"], account_label=r["account_label"],
        account_colour=r["account_colour"], message_id=r["message_id"] or "",
        from_name=r["from_name"], from_addr=r["from_addr"],
        to_addrs=r["to_addrs"], cc_addrs=r["cc_addrs"] or "",
        reply_to=r["reply_to"] or "", subject=r["subject"],
        subject_base=r["subject_base"] or "",
        references_=r["references_"] or "", preview=r["preview"],
        date_at=r["date_at"] or "", seen=bool(r["seen"]),
        flagged=bool(r["flagged"]), answered=bool(r["answered"]),
        draft=bool(r["draft"]), has_attachment=bool(r["has_attachment"]),
        size_bytes=r["size_bytes"], tags=tags,
        folder_name=r["folder_name"], folder_path=r["folder_path"],
        thread_key=r["thread_key"] or "", snippet=snippet, in_scope=in_scope)


def fetch(con: sqlite3.Connection, scope: views_mod.Scope, filters: views_mod.Filters | None = None,
          sort: views_mod.Sort | None = None, *, search=None, threaded: bool = False,
          limit: int = 200, offset: int = 0) -> list[Row]:
    snooze_mod.clear_expired(con)
    filters = filters or views_mod.Filters()
    sort = sort or views_mod.Sort()
    search = search if views_mod.searching(search) else None
    join, join_params = search_mod.join_sql(search)
    where, params = views_mod.clause(con, scope, filters, search)
    if threaded:
        # The window function is computed over the rows this WHERE clause chose,
        # so a thread's date is its newest message IN THIS VIEW — which is what
        # decides where the conversation sits in the list.
        select = f"SELECT {_COLUMNS}, {threads_mod.THREAD_AT} {_FROM}"
        order = threads_mod.ORDER_BY
    else:
        select = _SELECT
        order = views_mod.order_by(sort, ranked=search_mod.has_rank(search))
    rows = con.execute(
        f"{select}{join} WHERE {where} {order} LIMIT ? OFFSET ?",
        [*join_params, *params, limit, offset]).fetchall()
    ids = [r["id"] for r in rows]
    by_tag = tags_repo.tags_for(con, ids)
    snippets = search_mod.snippets(con, ids, search)
    return [_row(r, tuple(by_tag.get(r["id"], ())), snippets.get(r["id"], ""))
            for r in rows]


def count(con: sqlite3.Connection, scope: views_mod.Scope,
          filters: views_mod.Filters | None = None, *, search=None) -> int:
    search = search if views_mod.searching(search) else None
    join, join_params = search_mod.join_sql(search)
    where, params = views_mod.clause(con, scope, filters or views_mod.Filters(), search)
    return con.execute(
        f"SELECT COUNT(*) FROM message m JOIN folder f ON f.id = m.folder_id "
        f"JOIN account a ON a.id = f.account_id{join} WHERE {where}",
        [*join_params, *params]).fetchone()[0]


def count_capped(con: sqlite3.Connection, scope: views_mod.Scope,
                 filters: views_mod.Filters | None = None, *, search=None,
                 cap: int = 1000) -> int:
    """`count`, abandoned once `cap` rows have been seen.

    For a caller that draws a badge rather than a total, and redraws it often.
    COUNT(*) has to visit every row that matches; a badge reading "999+" past a
    threshold does not need them, and the difference is 107 ms against 1.4 ms
    on a store of a hundred thousand messages. `store/savedviews.count_capped`
    carries the measurement and the reason the number is a thousand.

    Here rather than at the caller because this file owns the counting query,
    and a second copy of the join is a second thing to keep in step with
    `views.clause`.
    """
    search = search if views_mod.searching(search) else None
    join, join_params = search_mod.join_sql(search)
    where, params = views_mod.clause(con, scope, filters or views_mod.Filters(), search)
    return con.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM message m "
        f"JOIN folder f ON f.id = m.folder_id "
        f"JOIN account a ON a.id = f.account_id{join} WHERE {where} LIMIT ?)",
        [*join_params, *params, int(cap)]).fetchone()[0]


def thread_context(con: sqlite3.Connection, keys: Sequence[str],
                   exclude_ids: Sequence[int], *,
                   limit: int = 2000) -> list[Row]:
    """The rest of these conversations — the messages NOT in view.

    Small in practice, and that is an argument rather than a hope: a
    conversation's members that share the view's folder are already in the page
    above, so what comes back here is the handful filed elsewhere — the replies
    the user sent. Bounded anyway, because "in practice" is not "always".
    """
    where, params = threads_mod.context_where(keys, exclude_ids)
    rows = con.execute(
        f"{_SELECT} WHERE {where} ORDER BY m.date_at DESC, m.id DESC LIMIT ?",
        [*params, limit]).fetchall()
    by_tag = tags_repo.tags_for(con, [r["id"] for r in rows])
    return [_row(r, tuple(by_tag.get(r["id"], ())), in_scope=False) for r in rows]


def count_threads(con: sqlite3.Connection, scope: views_mod.Scope,
                  filters: views_mod.Filters | None = None, *, search=None) -> int:
    """How many conversations this view holds, as against how many messages."""
    search = search if views_mod.searching(search) else None
    join, join_params = search_mod.join_sql(search)
    where, params = views_mod.clause(con, scope, filters or views_mod.Filters(), search)
    return con.execute(
        f"SELECT COUNT(DISTINCT m.thread_key) FROM message m "
        f"JOIN folder f ON f.id = m.folder_id "
        f"JOIN account a ON a.id = f.account_id{join} WHERE {where}",
        [*join_params, *params]).fetchone()[0]


def count_discarded(con: sqlite3.Connection, scope: views_mod.Scope,
                    filters: views_mod.Filters | None, search) -> int:
    """How many MORE this search would find in Trash and Junk.

    Asked so that the exclusion can be stated instead of silently applied. A
    search that quietly leaves out the folder holding the message being looked
    for is the failure this number exists to prevent — CONVENTIONS.txt §8.
    """
    if not views_mod.searching(search) or search.discarded:
        return 0
    return (count(con, scope, filters,
                  search=search.with_changes(discarded=True))
            - count(con, scope, filters, search=search))


def filter_ids(con: sqlite3.Connection, scope: views_mod.Scope, filters: views_mod.Filters | None,
               message_ids: Sequence[int], *, search=None) -> set[int]:
    """Of these messages, which still belong in this view?

    Asked after a mutation. Archiving a message removes it from the inbox but
    flagging one does not, and the difference is the whole scope-and-filter
    expression rather than anything the caller can work out. One query answers
    it for a whole selection, so the list can drop exactly the rows that left
    and repaint the rest in place — which is what stops every keystroke from
    resetting the model and losing the scroll position.
    """
    if not message_ids:
        return set()
    search = search if views_mod.searching(search) else None
    join, join_params = search_mod.join_sql(search)
    where, params = views_mod.clause(con, scope, filters or views_mod.Filters(), search)
    marks = ",".join("?" * len(message_ids))
    rows = con.execute(
        f"SELECT m.id FROM message m JOIN folder f ON f.id = m.folder_id "
        f"JOIN account a ON a.id = f.account_id{join} "
        f"WHERE ({where}) AND m.id IN ({marks})",
        [*join_params, *params, *message_ids]).fetchall()
    return {r[0] for r in rows}


def get_row(con: sqlite3.Connection, message_id: int) -> Row | None:
    r = con.execute(f"{_SELECT} WHERE m.id = ?", (message_id,)).fetchone()
    if r is None:
        return None
    return _row(r, tuple(tags_repo.tags_for(con, [message_id]).get(message_id, ())))


def bodies_of(con: sqlite3.Connection, message_id: int) -> tuple:
    """(plain, html) for one message, in one query.

    Both, because the reading pane needs to know whether there IS html: a
    message with none is shown as plain text deliberately, since its line
    breaks and alignment are its formatting.
    """
    row = con.execute(
        "SELECT body_text, body_html FROM message WHERE id = ?",
        (message_id,)).fetchone()
    return (row["body_text"] or "", row["body_html"] or "") if row else ("", "")


def body_of(con: sqlite3.Connection, message_id: int) -> str:
    row = con.execute("SELECT body_text FROM message WHERE id = ?",
                      (message_id,)).fetchone()
    return row["body_text"] if row else ""


def attachments_of(con: sqlite3.Connection, message_id: int) -> list[sqlite3.Row]:
    """EVERY part, inline ones included, with the path holding its bytes.

    Inline parts were filtered out here until stage 3, when they stopped being
    invisible: an HTML message's `cid:` images ARE its inline parts, and the
    reading pane resolves them by Content-ID. The strip that lists attachments
    to the reader still shows only the non-inline ones — a signature logo is
    not an attachment to a person — so the filtering moved to the one place
    that draws the strip, and the query returns the truth.
    """
    return con.execute(
        "SELECT id, filename, content_type, content_id, size_bytes, "
        "stored_path, is_inline FROM attachment "
        "WHERE message_id = ? ORDER BY is_inline, id",
        (message_id,)).fetchall()


# ------------------------------------------------------------------ counts
def unread_counts(con: sqlite3.Connection) -> dict[int, int]:
    """Unread inbox messages per account. The rail's numbers.

    Inbox only, on purpose: a count that included Junk would make an account
    look like it needed attention when what it needed was emptying.
    """
    rows = con.execute("""
        SELECT f.account_id AS account_id, COUNT(*) AS n
        FROM message m JOIN folder f ON f.id = m.folder_id
        WHERE f.role = ? AND m.seen = 0 AND m.deleted = 0
        GROUP BY f.account_id
    """, (folders_repo.ROLE_INBOX,)).fetchall()
    return {r["account_id"]: r["n"] for r in rows}


def unread_by_folder(con: sqlite3.Connection) -> dict[int, int]:
    rows = con.execute("""
        SELECT m.folder_id AS folder_id, COUNT(*) AS n FROM message m
        WHERE m.seen = 0 AND m.deleted = 0 GROUP BY m.folder_id
    """).fetchall()
    return {r["folder_id"]: r["n"] for r in rows}


def scope_counts(con: sqlite3.Connection) -> dict[str, int]:
    """The four unified rows' numbers, in one place.

    Inbox and Owed count what needs attention — unread and unanswered
    respectively. Drafts and Sent count everything, because an unread draft is
    not a concept and a Sent folder's unread count is always nought.
    """
    unified = views_mod.Scope(kind="unified", role=folders_repo.ROLE_INBOX)
    return {
        "inbox": count(con, unified, views_mod.Filters(unread=True)),
        "owed": count(con, views_mod.Scope(kind="unified", role=views_mod.ROLE_OWED)),
        "drafts": count(con, views_mod.Scope(kind="unified", role=folders_repo.ROLE_DRAFTS)),
        "sent": 0,
    }
