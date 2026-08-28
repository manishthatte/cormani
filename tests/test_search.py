# SPDX-License-Identifier: GPL-3.0-or-later
#
# Full-text search against a store: what a query finds, in what order, and with
# what shown beside it. The language itself is tests/test_searchlang.py.
#
# THE TESTS THAT MATTER MOST HERE ARE THE ONES NOBODY WOULD THINK TO WRITE —
# `TestNothingTheUserCanTypeIsAnError`. Most of the ordinary things a person
# types into a search box are FTS5 syntax errors, and each one would be an
# OperationalError out of a QLineEdit. The class of bug is "the user's text
# reached the parser", so the test is a list of hostile and merely ordinary
# strings, every one of which must come back with an answer.
#
# The rest guard the decisions that are invisible from the call site: that an
# exclusion is a second match rather than FTS5's binary NOT, that Trash and Junk
# are left out of a search but never left out SILENTLY, and that a message stays
# in a set of results after it is archived — because a search is not a folder.
#
# © Manish Jagdish Thatte
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

from cormani import __main__ as entry
from cormani import cli
from cormani.imap import envelope
from cormani.store import accounts as accounts_repo, views
from cormani.store import edits, folders, ingest, messages, search
from cormani.store.accounts import add_account
from cormani.store.folders import ensure_folder


def raw(subject: str, body: str, sender: str, *, to: str = "owner@manitlab.example",
        date: str = "Tue, 25 Aug 2026 10:00:00 +0000") -> bytes:
    text = (f"From: {sender}\n"
            f"To: {to}\n"
            f"Subject: {subject}\n"
            f"Message-ID: <{abs(hash((subject, body))) % 10 ** 9}@example.org>\n"
            f"Date: {date}\n"
            f"\n{body}\n")
    return text.replace("\n", "\r\n").encode("utf-8")


class Corpus:
    """A store with known mail in it, in known places.

    Written out rather than taken from the demo fixtures: every count below is
    exact, and a fixture that grew a message would make these tests wrong in a
    way that looks like a defect in the search.
    """

    def __init__(self, case):
        self.con = support.temp_store(case)
        self.mine = add_account(self.con, "owner@manitlab.example", "google",
                                display_name="manitlab")
        self.other = add_account(self.con, "krishna@example.com", "google",
                                 display_name="Krishna")
        self.inbox = ensure_folder(self.con, self.mine, "INBOX",
                                   role=folders.ROLE_INBOX)
        self.archive = ensure_folder(self.con, self.mine, "[Gmail]/All Mail",
                                     display_name="Archive",
                                     role=folders.ROLE_ARCHIVE)
        self.trash = ensure_folder(self.con, self.mine, "[Gmail]/Trash",
                                   display_name="Trash", role=folders.ROLE_TRASH)
        self.junk = ensure_folder(self.con, self.mine, "[Gmail]/Spam",
                                  display_name="Junk", role=folders.ROLE_JUNK)
        self.lists = ensure_folder(self.con, self.mine, "INBOX/Lists/debian")
        self.theirs = ensure_folder(self.con, self.other, "INBOX",
                                    role=folders.ROLE_INBOX)

        self.uid = 100
        self.wavelength = self.add(
            self.inbox, "DWCNT wavelength question",
            "The wavelengths we need are 1064 and 785 nanometres. "
            "Could you quote both, against quantity?",
            "Lyle Gordon <lyle@covalent.example>")
        self.invoices = self.add(
            self.inbox, "Supplier invoices — week 34",
            "Three invoices this week, all within the agreed terms.",
            "Frances Baker <frances@idlidu.example>",
            date="Mon, 17 Aug 2026 09:00:00 +0000")
        self.old = self.add(
            self.archive, "Capability statement",
            "Nothing more to it than the above. Wavelength coverage is broad.",
            "Lyle Gordon <lyle@covalent.example>",
            date="Wed, 04 Mar 2020 07:00:00 +0000")
        self.deleted = self.add(
            self.trash, "Wavelength — ignore this one",
            "Sent in error.", "Lyle Gordon <lyle@covalent.example>")
        self.spam = self.add(
            self.junk, "INVOICE OVERDUE", "Pay these invoices at once.",
            "Nobody <nobody@example.invalid>")
        self.debian = self.add(
            self.lists, "wavelength of the week",
            "A digest nobody reads.", "list@lists.example")
        self.hers = self.add(
            self.theirs, "Wavelength question, again",
            "Krishna asks about wavelengths too.",
            "Krishna <krishna@example.com>", to="krishna@example.com")

    def add(self, folder_id, subject, body, sender, **kw) -> int:
        self.uid += 1
        env = envelope.read(raw(subject, body, sender, **kw))
        return ingest.store_message(self.con, folder_id, self.uid, env).message_id


class SearchCase(unittest.TestCase):
    def setUp(self):
        self.corpus = Corpus(self)
        self.con = self.corpus.con
        self.scope = views.Scope()

    def ids(self, query=None, **kw):
        query = query if query is not None else search.Query(**kw)
        return [r.id for r in messages.fetch(self.con, self.scope,
                                             search=query, limit=100)]

    def rows(self, **kw):
        return messages.fetch(self.con, self.scope, search=search.Query(**kw),
                              sort=views.Sort(key=views.SORT_RELEVANCE),
                              limit=100)


# -------------------------------------------------------------- what it finds
class TestFinding(SearchCase):
    def test_a_word_is_found_in_the_body_and_in_the_subject(self):
        found = self.ids(text="wavelength")
        self.assertIn(self.corpus.wavelength, found)     # subject and body
        self.assertIn(self.corpus.old, found)            # body only
        self.assertIn(self.corpus.debian, found)         # subject only

    def test_a_search_crosses_folders_and_accounts(self):
        # The point of the box. The scope handed in is the unified inbox, and
        # the results are not confined to it.
        found = self.ids(text="wavelength")
        self.assertIn(self.corpus.old, found)            # Archive
        self.assertIn(self.corpus.hers, found)           # another account
        self.assertIn(self.corpus.debian, found)         # an ordinary folder

    def test_a_phrase_needs_the_words_in_that_order(self):
        self.assertIn(self.corpus.wavelength, self.ids(text='"quote both"'))
        self.assertEqual(self.ids(text='"both quote"'), [])

    def test_a_prefix_matches_what_a_whole_word_does_not(self):
        self.assertEqual(self.ids(text="nanome"), [])
        self.assertIn(self.corpus.wavelength, self.ids(text="nanome*"))

    def test_the_index_stems_so_a_plural_finds_the_singular(self):
        self.assertIn(self.corpus.invoices, self.ids(text="invoice"))
        self.assertIn(self.corpus.invoices, self.ids(text="invoices"))

    def test_an_exclusion_removes_what_it_names(self):
        with_all = self.ids(text="wavelength")
        without = self.ids(text="wavelength -krishna")
        self.assertIn(self.corpus.hers, with_all)
        self.assertNotIn(self.corpus.hers, without)
        self.assertIn(self.corpus.old, without)

    def test_an_exclusion_on_its_own_is_a_search(self):
        # `-word` alone is well formed here and is a syntax error in FTS5,
        # which is the whole reason exclusions are a second match.
        found = self.ids(text="-wavelength")
        self.assertIn(self.corpus.invoices, found)
        self.assertNotIn(self.corpus.wavelength, found)

    def test_a_field_prefix_restricts_to_that_part_of_the_message(self):
        self.assertIn(self.corpus.wavelength, self.ids(text="from:lyle"))
        self.assertNotIn(self.corpus.hers, self.ids(text="from:lyle"))
        self.assertIn(self.corpus.debian, self.ids(text="subject:wavelength"))
        self.assertNotIn(self.corpus.old, self.ids(text="subject:wavelength"))

    def test_an_address_is_found_whole_although_the_index_splits_it(self):
        self.assertIn(self.corpus.wavelength,
                      self.ids(text='"lyle@covalent.example"'))

    def test_the_from_chip_is_the_from_prefix_without_the_typing(self):
        self.assertEqual(self.ids(sender="lyle"), self.ids(text="from:lyle"))

    def test_the_account_chip_narrows_to_one_account(self):
        theirs = self.ids(text="wavelength", account_id=self.corpus.other)
        self.assertEqual(theirs, [self.corpus.hers])

    def test_the_date_chip_narrows_by_when_it_was_sent(self):
        recent = self.ids(text="wavelength", within="30d")
        self.assertIn(self.corpus.wavelength, recent)
        self.assertNotIn(self.corpus.old, recent)        # from 2020

    def test_the_attachment_chip_narrows_to_messages_carrying_one(self):
        self.con.execute("UPDATE message SET has_attachment = 1 WHERE id = ?",
                         (self.corpus.wavelength,))
        found = self.ids(text="wavelength", attachment=True)
        self.assertEqual(found, [self.corpus.wavelength])

    def test_the_quick_filters_still_apply_on_top_of_a_search(self):
        edits.set_seen(self.con, [self.corpus.wavelength], True)
        unread = messages.fetch(self.con, self.scope,
                                views.Filters(unread=True),
                                search=search.Query(text="wavelength"), limit=50)
        self.assertNotIn(self.corpus.wavelength, [r.id for r in unread])
        self.assertIn(self.corpus.old, [r.id for r in unread])

    def test_a_hidden_account_is_hidden_from_search_too(self):
        # Otherwise hiding an account would not do what it says: its mail would
        # come back the moment anything was searched for.
        accounts_repo.set_hidden(self.con, self.corpus.other, True)
        self.assertNotIn(self.corpus.hers, self.ids(text="wavelength"))

    def test_an_inactive_query_leaves_the_scope_alone(self):
        # A Query that asks nothing must not turn the inbox into every folder.
        plain = [r.id for r in messages.fetch(self.con, self.scope, limit=100)]
        self.assertEqual(self.ids(search.Query()), plain)
        self.assertNotIn(self.corpus.old, plain)

    def test_a_message_stays_in_the_results_after_it_is_archived(self):
        # A search is not a folder. `filter_ids` is what the list asks after a
        # mutation, and it must answer for the search rather than for the rail.
        query = search.Query(text="wavelength")
        staying = messages.filter_ids(self.con, self.scope, None,
                                      [self.corpus.wavelength], search=query)
        edits.archive(self.con, [self.corpus.wavelength])
        after = messages.filter_ids(self.con, self.scope, None,
                                    [self.corpus.wavelength], search=query)
        self.assertEqual(staying, {self.corpus.wavelength})
        self.assertEqual(after, {self.corpus.wavelength})


# ------------------------------------------------------------- trash and junk
class TestWhatIsLeftOut(SearchCase):
    def test_trash_and_junk_are_left_out_by_default(self):
        found = self.ids(text="wavelength")
        self.assertNotIn(self.corpus.deleted, found)
        self.assertNotIn(self.corpus.spam, self.ids(text="invoice"))

    def test_the_chip_puts_them_back(self):
        found = self.ids(text="wavelength", discarded=True)
        self.assertIn(self.corpus.deleted, found)
        self.assertIn(self.corpus.spam, self.ids(text="invoice", discarded=True))

    def test_a_message_the_server_marked_deleted_is_out_as_well(self):
        # The same statement made a different way by a server that marks rather
        # than moves.
        self.con.execute("UPDATE message SET deleted = 1 WHERE id = ?",
                         (self.corpus.old,))
        self.assertNotIn(self.corpus.old, self.ids(text="wavelength"))
        self.assertIn(self.corpus.old, self.ids(text="wavelength", discarded=True))

    def test_the_number_left_out_can_be_asked_for_rather_than_guessed(self):
        query = search.Query(text="wavelength")
        self.assertEqual(
            messages.count_discarded(self.con, self.scope, None, query), 1)
        self.assertEqual(
            messages.count_discarded(self.con, self.scope, None,
                                     query.with_changes(discarded=True)), 0)
        self.assertEqual(
            messages.count_discarded(self.con, self.scope, None,
                                     search.Query()), 0)


# --------------------------------------------------------------- the ordering
class TestRelevance(SearchCase):
    def test_a_hit_in_the_subject_outranks_the_same_word_in_a_body(self):
        # bm25 weighted per column: subject 10, sender 5, recipients 2, body 1.
        ranked = [r.id for r in self.rows(text="wavelength")]
        self.assertLess(ranked.index(self.corpus.debian),   # subject only
                        ranked.index(self.corpus.old))      # body only

    def test_relevance_falls_back_to_date_when_nothing_was_ranked(self):
        # Chips alone match nothing against the index, so there is no score.
        rows = messages.fetch(self.con, self.scope,
                              sort=views.Sort(key=views.SORT_RELEVANCE),
                              search=search.Query(within="30d"), limit=50)
        dates = [r.date_at for r in rows]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertFalse(search.has_rank(search.Query(within="30d")))
        self.assertTrue(search.has_rank(search.Query(text="wavelength")))

    def test_relevance_outside_a_search_is_the_date_order(self):
        by_relevance = [r.id for r in messages.fetch(
            self.con, self.scope, sort=views.Sort(key=views.SORT_RELEVANCE),
            limit=50)]
        by_date = [r.id for r in messages.fetch(self.con, self.scope, limit=50)]
        self.assertEqual(by_relevance, by_date)

    def test_the_other_orders_still_work_over_a_search(self):
        rows = messages.fetch(self.con, self.scope,
                              sort=views.Sort(key="subject", descending=False),
                              search=search.Query(text="wavelength"), limit=50)
        subjects = [(r.subject or "").lower() for r in rows]
        self.assertEqual(subjects, sorted(subjects))


# ---------------------------------------------------------------- the snippet
class TestSnippets(SearchCase):
    def row(self, message_id, **kw):
        for row in messages.fetch(self.con, self.scope, search=search.Query(**kw),
                                  limit=50):
            if row.id == message_id:
                return row
        self.fail("not found")

    def test_the_snippet_is_centred_on_the_word_that_was_searched_for(self):
        row = self.row(self.corpus.wavelength, text="quantity")
        self.assertIn("quantity", row.snippet)
        self.assertTrue(row.snippet.startswith("…"))

    def test_a_match_that_is_not_in_the_body_leaves_the_preview_alone(self):
        row = self.row(self.corpus.debian, text="subject:wavelength")
        self.assertEqual(row.snippet, "")

    def test_a_stemmed_match_still_lands_on_the_related_word(self):
        # The index stems and the scan does not: `invoices` matched a body that
        # says `invoices`, but `invoice` has to fall back to a prefix.
        row = self.row(self.corpus.invoices, text="invoice")
        self.assertIn("invoice", row.snippet)

    def test_no_snippet_at_all_outside_a_search(self):
        rows = messages.fetch(self.con, self.scope, limit=50)
        self.assertTrue(rows)
        self.assertEqual({r.snippet for r in rows}, {""})

    def test_a_snippet_is_one_line_however_the_body_was_wrapped(self):
        piece = search.snippet("first line\n\n  second   line here\n",
                               search.parse("second"))
        self.assertNotIn("\n", piece)
        self.assertIn("second line here", piece)

    def test_an_empty_body_produces_no_snippet_rather_than_an_ellipsis(self):
        self.assertEqual(search.snippet("", search.parse("x")), "")
        self.assertEqual(search.snippet("nothing to see", search.parse("x")), "")


# ------------------------------------------------------- where a hit lives
class TestLocation(SearchCase):
    def test_a_row_names_the_folder_and_the_account_it_is_in(self):
        rows = {r.id: r for r in messages.fetch(
            self.con, self.scope, search=search.Query(text="wavelength"), limit=50)}
        self.assertEqual(rows[self.corpus.wavelength].location, "Inbox · manitlab")
        self.assertEqual(rows[self.corpus.old].location, "Archive · manitlab")
        self.assertEqual(rows[self.corpus.hers].location, "Inbox · Krishna")

    def test_a_folder_with_no_role_is_named_by_its_own_last_segment(self):
        row = [r for r in messages.fetch(
            self.con, self.scope, search=search.Query(text="subject:wavelength"),
            limit=50) if r.id == self.corpus.debian][0]
        self.assertEqual(row.location, "debian · manitlab")


# -------------------------------------------------- nothing typed is an error
class TestNothingTheUserCanTypeIsAnError(SearchCase):
    # Twelve of these fifteen are FTS5 syntax errors when passed through: an
    # unterminated string, an unknown special query, "no such column: a". The
    # search box must answer all of them.
    HOSTILE = ('"unclosed', 'a:b', '-', '*', '^', '(', ')', 'x AND', 'AND x',
               'NOT x', 'x NEAR y', '""', '"" ""', 'x*y', '///',
               'http://example.org/a?b=c', 'lyle@covalent.example', '50%', 'a_b',
               "O'Brien", 'wavelength -', '- -', '"a""b"', 'x:y:z', '£¥€',
               'नमस्ते', '🙂', 'x' * 500)

    def test_every_one_of_them_returns_an_answer(self):
        for text in self.HOSTILE:
            with self.subTest(text=text):
                query = search.Query(text=text)
                rows = messages.fetch(self.con, self.scope, search=query, limit=5)
                self.assertIsInstance(rows, list)
                self.assertIsInstance(
                    messages.count(self.con, self.scope, search=query), int)

    def test_the_same_holds_for_the_chips(self):
        for value in self.HOSTILE:
            with self.subTest(value=value):
                for query in (search.Query(sender=value),
                              search.Query(subject=value)):
                    messages.fetch(self.con, self.scope, search=query, limit=5)

    def test_an_ordinary_word_still_works_after_all_that(self):
        # FTS5 corruption is silent until the next read; this is the canary.
        self.assertIn(self.corpus.wavelength, self.ids(text="wavelength"))


# ------------------------------------------------------------------ the index
class TestTheIndexCanBeRebuilt(SearchCase):
    """A row written without being indexed is invisible to search and looks
    perfectly healthy from everywhere else — the list shows it, the reader opens
    it, and only the search is quietly short. The demo fixtures did exactly that
    from stage 1 until stage 2 noticed."""

    def unindexed(self) -> int:
        cur = self.con.execute(
            "INSERT INTO message (folder_id, uid, subject, body_text, preview, "
            "date_at, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.corpus.inbox, 900, "A teapot", "Short and stout.", "",
             "2026-08-25T10:00:00+00:00", "2026-08-25T10:00:00+00:00"))
        self.con.commit()
        return int(cur.lastrowid)

    def test_a_message_nobody_indexed_becomes_findable_again(self):
        new_id = self.unindexed()
        self.assertNotIn(new_id, self.ids(text="teapot"))
        self.assertEqual(ingest.rebuild_search_index(self.con), 8)
        self.assertIn(new_id, self.ids(text="teapot"))
        # And everything else survived the rebuild.
        self.assertIn(self.corpus.wavelength, self.ids(text="wavelength"))

    def test_check_says_how_much_of_the_store_is_searchable(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli._report_index(self.con)
        self.assertIn("7 of 7 messages", out.getvalue())
        self.unindexed()
        out = io.StringIO()
        with redirect_stdout(out):
            cli._report_index(self.con)
        self.assertIn("NOT SEARCHABLE", out.getvalue())
        self.assertIn("--reindex", out.getvalue())

    def test_the_flag_reaches_the_command(self):
        with mock.patch.object(cli, "reindex", return_value=0) as called:
            self.assertEqual(entry.main(["--reindex"]), 0)
        self.assertTrue(called.called)


if __name__ == "__main__":
    unittest.main()
