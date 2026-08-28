# SPDX-License-Identifier: GPL-3.0-or-later
#
# The search language: what a line of typed text means, and what a date chip is.
#
# No store and no Qt — these are the pure half of store/search.py, and they are
# separated from test_search.py because they are a different kind of test: what
# the parser DOES with a string, rather than what the database returns for it.
#
# THE USER'S TEXT NEVER REACHES FTS5, and this is where that is proved. Every
# operator FTS5 understands has to survive being typed by someone who has never
# heard of it: `AND` is the word "and", `-` between two words is punctuation,
# and an unknown `word:` prefix is two words rather than "no such column".
#
# © Manish Jagdish Thatte
import datetime as dt
import time
import unittest

import support                                              # noqa: F401

from cormani.store import search


# --------------------------------------------------------------- the language
class TestParsing(unittest.TestCase):
    def terms(self, text):
        return [(t.text, t.field, t.negated, t.prefix) for t in search.parse(text)]

    def test_two_words_are_two_terms(self):
        self.assertEqual(self.terms("wavelength question"),
                         [("wavelength", "", False, False),
                          ("question", "", False, False)])

    def test_a_quoted_phrase_is_one_term(self):
        self.assertEqual(self.terms('"pricing options" now'),
                         [("pricing options", "", False, False),
                          ("now", "", False, False)])

    def test_a_trailing_star_is_a_prefix_and_is_not_part_of_the_word(self):
        self.assertEqual(self.terms("invoic*"), [("invoic", "", False, True)])

    def test_a_leading_minus_negates(self):
        self.assertEqual(self.terms("-holiday"), [("holiday", "", True, False)])

    def test_each_field_prefix_maps_to_its_column(self):
        for typed, column in (("from", "from_repr"), ("sender", "from_repr"),
                              ("to", "to_repr"), ("cc", "to_repr"),
                              ("subject", "subject"), ("body", "body")):
            self.assertEqual(self.terms(f"{typed}:lyle"),
                             [("lyle", column, False, False)], typed)

    def test_an_unknown_prefix_is_literal_text_rather_than_an_error(self):
        # `Re:` and `http://x` are things people paste into a search box. The
        # case is left as typed; the index is case-insensitive either way.
        self.assertEqual(self.terms("a:b"), [("a:b", "", False, False)])
        self.assertEqual(self.terms("Re: figures"),
                         [("Re:", "", False, False), ("figures", "", False, False)])
        self.assertEqual(self.terms("http://example.org/a"),
                         [("http://example.org/a", "", False, False)])

    def test_punctuation_between_words_does_not_poison_the_query(self):
        # A term the tokenizer would index NOTHING from must be dropped, not
        # quoted: an empty phrase ANDed with the rest matches nothing at all,
        # so `wavelength - question` would find none of what `wavelength
        # question` finds.
        self.assertEqual(self.terms("wavelength - question"),
                         [("wavelength", "", False, False),
                          ("question", "", False, False)])

    def test_the_boolean_words_are_words(self):
        for word in ("AND", "OR", "NOT", "NEAR"):
            self.assertEqual(self.terms(word), [(word, "", False, False)], word)

    def test_a_query_of_pure_punctuation_matches_nothing_rather_than_everything(self):
        # It asked for something. "No results" is the honest answer; every
        # message in fifteen accounts is not.
        query = search.Query(text="***")
        self.assertTrue(query.active)
        self.assertEqual([t.text for t in query.terms], [""])
        self.assertEqual(search.expression(query.terms), '""')

    def test_a_quote_inside_a_term_is_doubled_rather_than_closing_it(self):
        self.assertEqual(search.Term(text='say "hi"').expression(),
                         '"say ""hi"""')

    def test_an_exclusion_is_its_own_expression_and_not_a_NOT(self):
        # FTS5's NOT is binary, so `-holiday` alone has nothing to subtract
        # from and is a syntax error inside the first expression.
        terms = search.parse("wavelength -holiday")
        self.assertEqual(search.expression(terms), '"wavelength"')
        self.assertEqual(search.exclusion(terms), '"holiday"')
        self.assertIsNone(search.expression(search.parse("-holiday")))
        self.assertEqual(search.exclusion(search.parse("-holiday")), '"holiday"')


class TestTheQueryObject(unittest.TestCase):
    def test_an_empty_query_asks_nothing_and_is_not_run(self):
        self.assertFalse(search.Query().active)
        for query in (search.Query(text="x"), search.Query(sender="x"),
                      search.Query(subject="x"), search.Query(attachment=True),
                      search.Query(within="7d"), search.Query(account_id=1)):
            self.assertTrue(query.active, query)

    def test_including_trash_is_a_qualifier_and_not_a_search(self):
        # Otherwise pressing one chip with an empty box lists every message in
        # every account, which is a wait rather than an answer.
        self.assertFalse(search.Query(discarded=True).active)

    def test_the_two_text_chips_become_terms_on_their_own_columns(self):
        query = search.Query(text="wavelength", sender="lyle", subject="dwcnt")
        self.assertEqual([(t.text, t.field) for t in query.terms],
                         [("wavelength", ""), ("lyle", "from_repr"),
                          ("dwcnt", "subject")])

    def test_describe_says_what_was_asked(self):
        query = search.Query(text="wavelength", sender="lyle",
                             attachment=True, within="7d")
        described = query.describe()
        for fragment in ("wavelength", "from lyle", "with an attachment",
                         "last 7 days"):
            self.assertIn(fragment, described)
        self.assertEqual(search.Query().describe(), "everything")
        self.assertIn("including Trash and Junk",
                      search.Query(text="x", discarded=True).describe())


# ------------------------------------------------------------------ the dates
class TestDateRanges(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 8, 25, 2, 0, tzinfo=dt.timezone.utc)

    def parse(self, value):
        return dt.datetime.fromisoformat(value)

    def test_no_range_constrains_nothing(self):
        self.assertIsNone(search.since(""))
        self.assertIsNone(search.since("nonesuch", self.now))

    def test_every_range_starts_at_local_midnight_and_is_stored_as_utc(self):
        # The store keeps UTC and the reader lives somewhere else; a "Today"
        # computed from UTC midnight hides this morning's mail for as many
        # hours as the offset. The same defect ui/messagelist.to_local exists
        # to prevent, at the other end.
        for within in ("today", "7d", "30d", "year"):
            value = search.since(within, self.now)
            self.assertTrue(value.endswith("+00:00"), value)
            local = self.parse(value).astimezone()
            self.assertEqual((local.hour, local.minute, local.second),
                             (0, 0, 0), within)

    def test_seven_days_means_seven_calendar_days_including_today(self):
        today = self.parse(search.since("today", self.now))
        week = self.parse(search.since("7d", self.now))
        self.assertEqual((today - week).days, 6)
        month = self.parse(search.since("30d", self.now))
        self.assertEqual((today - month).days, 29)

    def test_this_year_starts_on_the_first_of_january_locally(self):
        start = self.parse(search.since("year", self.now)).astimezone()
        self.assertEqual((start.month, start.day), (1, 1))

    @unittest.skipUnless(hasattr(time, "tzset"), "no tzset on this platform")
    def test_the_offset_this_machine_actually_runs_at(self):
        # Asia/Kolkata is UTC+05:30, which is where this is developed and where
        # the two dates disagree for five and a half hours out of every day.
        import os
        previous = os.environ.get("TZ")

        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "Asia/Kolkata"
        time.tzset()
        # 02:00 UTC on the 25th is 07:30 on the 25th in Kolkata, so "today"
        # begins at 18:30 UTC on the 24th.
        self.assertEqual(search.since("today", self.now),
                         "2026-08-24T18:30:00+00:00")


if __name__ == "__main__":
    unittest.main()
