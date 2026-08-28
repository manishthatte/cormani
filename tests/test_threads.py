# SPDX-License-Identifier: GPL-3.0-or-later
#
# Threading: which messages are one conversation.
#
# THE TESTS THAT MATTER HERE ARE ABOUT ARRIVAL ORDER. A reply can be stored
# before the message it answers, two halves of a conversation can each be rooted
# at an id the store has never seen, and the message that joins them can arrive
# last. Every one of those has to end with the same thread, which is what
# `assign`'s merge is for — and none of them can be seen by testing a single
# message.
#
# The other half is the negative: a subject is not evidence. "Re: Invoice" from
# two different people in two different years is two conversations, and a
# threading rule that says otherwise is wrong in a way nobody notices until
# their mail is one long thread.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.imap import envelope
from cormani.store import folders, ingest, messages, threads, views
from cormani.store.accounts import add_account
from cormani.store.folders import ensure_folder


def raw(subject: str, *, message_id: str, in_reply_to: str = "",
        references: str = "", sender: str = "Lyle <lyle@covalent.example>",
        date: str = "Tue, 25 Aug 2026 10:00:00 +0000", body: str = "text") -> bytes:
    lines = [f"From: {sender}", "To: owner@manitlab.example", f"Subject: {subject}",
             f"Message-ID: {message_id}", f"Date: {date}"]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append(f"References: {references}")
    text = "\n".join(lines) + f"\n\n{body}\n"
    return text.replace("\n", "\r\n").encode("utf-8")


class Store:
    """An account with an inbox and a Sent folder, and a way to fill them.

    A plain class rather than a TestCase, so that another test module can build
    the same store without inheriting — and re-running — the tests here.
    """

    def __init__(self, case):
        self.con = support.temp_store(case)
        self.account = add_account(self.con, "owner@manitlab.example", "google",
                                   display_name="manitlab")
        self.inbox = ensure_folder(self.con, self.account, "INBOX",
                                   role=folders.ROLE_INBOX)
        self.sent = ensure_folder(self.con, self.account, "[Gmail]/Sent Mail",
                                  display_name="Sent", role=folders.ROLE_SENT)
        # An account with somewhere to archive TO. Without it `edits.archive`
        # reports every message as skipped, which is correct and makes a test
        # that meant to move one look as though the move did nothing.
        self.archive = ensure_folder(self.con, self.account, "[Gmail]/All Mail",
                                     display_name="Archive",
                                     role=folders.ROLE_ARCHIVE)
        self.uid = 0

    def store(self, folder_id=None, **kw) -> int:
        self.uid += 1
        return ingest.store_message(self.con, folder_id or self.inbox, self.uid,
                                    envelope.read(raw(**kw))).message_id

    def key(self, message_id: int) -> str:
        return threads.key_of(self.con, message_id)


class Conversation(Store):
    """One conversation of three — an incoming message, the user's reply in
    Sent, and the answer to that — plus one message that is on its own. The
    shape every question about threading needs: something to group, something
    not to group, and a member that is not in the folder being looked at."""

    def __init__(self, case):
        super().__init__(case)
        self.root = self.store(subject="Wavelengths", message_id="<root@x>",
                               date="Mon, 24 Aug 2026 09:00:00 +0000")
        self.mine = self.store(
            self.sent, subject="Re: Wavelengths", message_id="<mine@x>",
            references="<root@x>", date="Mon, 24 Aug 2026 12:00:00 +0000")
        self.reply = self.store(subject="Re: Wavelengths", message_id="<r2@x>",
                                references="<root@x> <mine@x>",
                                date="Tue, 25 Aug 2026 09:00:00 +0000")
        self.other = self.store(subject="Unrelated", message_id="<u@x>",
                                date="Tue, 25 Aug 2026 11:00:00 +0000")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.store_ = Store(self)
        self.con = self.store_.con
        self.account = self.store_.account
        self.inbox = self.store_.inbox
        self.sent = self.store_.sent

    def store(self, folder_id=None, **kw) -> int:
        return self.store_.store(folder_id, **kw)

    def key(self, message_id: int) -> str:
        return self.store_.key(message_id)


# ------------------------------------------------------------ reading headers
class TestReadingTheChain(unittest.TestCase):
    def test_ids_are_split_on_whitespace_and_kept_whole(self):
        self.assertEqual(threads.parse_ids("<a@x> <b@y>"), ("<a@x>", "<b@y>"))
        self.assertEqual(threads.parse_ids("<a@x>,<b@y>"), ("<a@x>", "<b@y>"))
        self.assertEqual(threads.parse_ids("  "), ())
        self.assertEqual(threads.parse_ids(None), ())

    def test_a_repeated_id_is_kept_once_and_in_order(self):
        self.assertEqual(threads.parse_ids("<a> <b> <a>"), ("<a>", "<b>"))

    def test_an_absurd_id_is_not_a_key(self):
        # A mangled header is not a conversation. Bounded rather than validated:
        # anything may be an id, but a megabyte of junk may not.
        huge = "<" + "x" * (threads.MAX_ID + 10) + ">"
        self.assertEqual(threads.parse_ids(f"{huge} <b@y>"), ("<b@y>",))

    def test_the_claimed_root_is_the_first_reference_then_the_reply_then_itself(self):
        self.assertEqual(
            threads.claimed_root("<me>", "<parent>", "<root> <parent>"), "<root>")
        self.assertEqual(threads.claimed_root("<me>", "<parent>", ""), "<parent>")
        self.assertEqual(threads.claimed_root("<me>", "", ""), "<me>")
        self.assertEqual(threads.claimed_root("", "", "", fallback="id:7"), "id:7")

    def test_the_candidates_include_the_message_itself(self):
        # The half that is easy to forget: a root arriving after its replies is
        # recognised only because the replies already name it.
        self.assertIn("<me>", threads.candidates("<me>", "", "<root>"))


# --------------------------------------------------------------- putting them
class TestAssigning(StoreCase):
    def test_a_reply_joins_the_message_it_answers(self):
        root = self.store(subject="Wavelengths", message_id="<root@x>")
        reply = self.store(subject="Re: Wavelengths", message_id="<r1@x>",
                           in_reply_to="<root@x>", references="<root@x>")
        self.assertEqual(self.key(root), self.key(reply))

    def test_a_reply_that_arrives_first_still_ends_in_the_same_thread(self):
        reply = self.store(subject="Re: Wavelengths", message_id="<r1@x>",
                           in_reply_to="<root@x>", references="<root@x>")
        root = self.store(subject="Wavelengths", message_id="<root@x>")
        self.assertEqual(self.key(root), self.key(reply))

    def test_a_client_that_sends_only_in_reply_to_is_threaded_too(self):
        root = self.store(subject="Wavelengths", message_id="<root@x>")
        first = self.store(subject="Re: Wavelengths", message_id="<r1@x>",
                           references="<root@x>")
        # No References at all, and its parent is the REPLY rather than the root.
        second = self.store(subject="Re: Wavelengths", message_id="<r2@x>",
                            in_reply_to="<r1@x>")
        self.assertEqual({self.key(root), self.key(first), self.key(second)},
                         {self.key(root)})

    def test_two_orphan_halves_are_merged_by_the_message_that_names_both(self):
        # Neither names an id the store holds, so each is its own conversation
        # until the root arrives.
        left = self.store(subject="Re: Wavelengths", message_id="<a@x>",
                          references="<root@x>")
        right = self.store(subject="Re: Wavelengths", message_id="<b@x>",
                           in_reply_to="<other@x>")
        self.assertNotEqual(self.key(left), self.key(right))
        joiner = self.store(subject="Re: Wavelengths", message_id="<c@x>",
                            references="<root@x> <other@x> <b@x>")
        self.assertEqual(self.key(left), self.key(right))
        self.assertEqual(self.key(left), self.key(joiner))

    def test_a_message_with_no_chain_is_its_own_conversation(self):
        one = self.store(subject="Wavelengths", message_id="<a@x>")
        two = self.store(subject="Wavelengths", message_id="<b@x>")
        self.assertNotEqual(self.key(one), self.key(two))

    def test_a_shared_subject_is_not_a_conversation(self):
        # The rule store/threads.py argues for at length: a subject is a guess
        # and a chain is a fact.
        a = self.store(subject="Re: Invoice", message_id="<a@x>",
                       sender="Frances <frances@x>")
        b = self.store(subject="Re: Invoice", message_id="<b@x>",
                       sender="Nobody <nobody@y>")
        self.assertNotEqual(self.key(a), self.key(b))

    def test_a_message_with_no_id_at_all_still_gets_a_key_of_its_own(self):
        first = self.store(subject="No id", message_id="")
        second = self.store(subject="No id either", message_id="")
        self.assertTrue(self.key(first))
        self.assertNotEqual(self.key(first), self.key(second))
        self.assertTrue(self.key(first).startswith("id:"))

    def test_rethreading_reaches_the_same_answer_from_the_messages_alone(self):
        root = self.store(subject="Wavelengths", message_id="<root@x>")
        reply = self.store(subject="Re: Wavelengths", message_id="<r1@x>",
                           references="<root@x>")
        self.con.execute("UPDATE message SET thread_key = 'nonsense'")
        self.con.commit()
        self.assertEqual(threads.rethread(self.con), 2)
        self.assertEqual(self.key(root), self.key(reply))
        self.assertNotEqual(self.key(root), "nonsense")

    def test_the_members_of_a_thread_come_back_oldest_first(self):
        root = self.store(subject="Wavelengths", message_id="<root@x>",
                          date="Mon, 24 Aug 2026 09:00:00 +0000")
        reply = self.store(subject="Re: Wavelengths", message_id="<r1@x>",
                           references="<root@x>",
                           date="Tue, 25 Aug 2026 09:00:00 +0000")
        self.assertEqual(threads.members(self.con, self.key(root)), [root, reply])
        self.assertEqual(threads.members(self.con, ""), [])


# ------------------------------------------------------------- what the list sees
class TestTheThreadedList(unittest.TestCase):
    def setUp(self):
        self.fixture = Conversation(self)
        self.con = self.fixture.con
        self.account = self.fixture.account
        self.root, self.mine = self.fixture.root, self.fixture.mine
        self.reply, self.other = self.fixture.reply, self.fixture.other
        self.scope = views.Scope()

    def store(self, folder_id=None, **kw) -> int:
        return self.fixture.store(folder_id, **kw)

    def key(self, message_id: int) -> str:
        return self.fixture.key(message_id)

    def test_a_conversation_comes_back_as_one_run_newest_first(self):
        rows = messages.fetch(self.con, self.scope, threaded=True, limit=50)
        ids = [r.id for r in rows]
        self.assertEqual(ids, [self.other, self.reply, self.root])
        self.assertEqual(rows[1].thread_key, rows[2].thread_key)

    def test_the_conversation_is_placed_by_its_newest_message_in_view(self):
        # The Sent reply is not in the inbox, so it does not decide where the
        # conversation sits: the newest INBOX message does.
        rows = messages.fetch(self.con, self.scope, threaded=True, limit=50)
        self.assertEqual(rows[0].id, self.other)

    def test_the_rest_of_the_conversation_can_be_asked_for_separately(self):
        key = self.key(self.root)
        context = messages.thread_context(self.con, [key], [self.root, self.reply])
        self.assertEqual([r.id for r in context], [self.mine])
        self.assertFalse(context[0].in_scope)
        self.assertEqual(context[0].location, "Sent · manitlab")

    def test_context_never_includes_what_was_already_in_view(self):
        key = self.key(self.root)
        context = messages.thread_context(self.con, [key], [])
        self.assertEqual({r.id for r in context}, {self.root, self.mine, self.reply})
        self.assertEqual(messages.thread_context(self.con, [], []), [])

    def test_trash_is_not_dragged_in_as_context(self):
        trash = ensure_folder(self.con, self.account, "[Gmail]/Trash",
                              display_name="Trash", role=folders.ROLE_TRASH)
        binned = self.store(trash, subject="Re: Wavelengths", message_id="<t@x>",
                            references="<root@x>")
        context = messages.thread_context(self.con, [self.key(self.root)], [])
        self.assertNotIn(binned, [r.id for r in context])

    def test_the_count_is_the_conversation_and_not_the_folder(self):
        counts = threads.counts(self.con, [self.key(self.root)])
        total, unread = counts[self.key(self.root)]
        self.assertEqual(total, 3)              # including the one in Sent
        self.assertEqual(unread, 3)
        self.assertEqual(threads.counts(self.con, []), {})

    def test_conversations_are_counted_as_well_as_messages(self):
        self.assertEqual(messages.count(self.con, self.scope), 3)   # the inbox
        self.assertEqual(messages.count_threads(self.con, self.scope), 2)

    def test_an_unthreaded_fetch_is_the_plain_date_order(self):
        rows = messages.fetch(self.con, self.scope, threaded=False, limit=50)
        dates = [r.date_at for r in rows]
        self.assertEqual(dates, sorted(dates, reverse=True))


if __name__ == "__main__":
    unittest.main()
