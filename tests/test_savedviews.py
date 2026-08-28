# SPDX-License-Identifier: GPL-3.0-or-later
#
# Saved searches: what survives being written down, and what a stale one says.
#
# WHAT THESE ASSERT IS NOT "THE FUNCTION RETURNED". A saved view is a blob, and
# a blob has exactly one failure mode worth testing at length: something goes
# in and something ELSE comes out, silently, because JSON has no schema and
# nothing between here and the rail would object. So every round-trip asserts
# the VALUE of each of the five objects rather than that a view came back.
#
# THE TOLERANCE TESTS ARE THE OTHER HALF, and they are not hypothetical
# defensiveness: `store/database.py` refuses a schema NEWER than it knows, and
# a blob is the one thing that guard does not cover, because the version lives
# inside the value. A definition this corMani cannot read must open as the
# default view — not raise, because it is read inside the rail's build loop and
# one bad row would be a client that will not draw its own rail.
#
# © Manish Jagdish Thatte
import json
import unittest

import support

from cormani.store import savedviews, search, views
from cormani.store.accounts import add_account
from cormani.store.folders import ROLE_INBOX, ROLE_SENT, ensure_folder
from cormani.store.tags import add_tag


class SavedViewStoreCase(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "one@example.invalid", "imap")
        self.inbox = ensure_folder(self.con, self.account, "INBOX",
                                   role=ROLE_INBOX)
        self.sent = ensure_folder(self.con, self.account, "Sent", role=ROLE_SENT)

    def a_view(self, **changes):
        base = dict(
            name="Everything from Lyle",
            scope=views.Scope(kind="folder", account_id=self.account,
                              folder_id=self.inbox),
            filters=views.Filters(unread=True, attachment=True, text="tube"),
            search=search.Query(text="wavelength", sender="lyle", within="30d",
                                discarded=True),
            sort=views.Sort(key="sender", descending=False),
            threaded=False)
        base.update(changes)
        return savedviews.SavedView(**base)


class TestRoundTrip(SavedViewStoreCase):
    def test_every_one_of_the_five_objects_comes_back_the_same(self):
        # THE VALUE, field by field, and not `is not None`. A blob that lost
        # `descending` or flipped `threaded` would still be a SavedView.
        wanted = self.a_view()
        got = savedviews.save_view(self.con, wanted)
        self.assertEqual(got.scope, wanted.scope)
        self.assertEqual(got.filters, wanted.filters)
        self.assertEqual(got.search, wanted.search)
        self.assertEqual(got.sort, wanted.sort)
        self.assertEqual(got.threaded, wanted.threaded)

    def test_it_survives_being_read_by_a_second_connection(self):
        # The blob has to reach the DISK correctly, not merely this
        # connection's cache: the rail reads it in another process on restart.
        saved = savedviews.save_view(self.con, self.a_view())
        again = savedviews.get_view(support.reopened(self.con), saved.id)
        self.assertEqual(again.search, saved.search)
        self.assertEqual(again.filters, saved.filters)
        self.assertEqual(again.threaded, saved.threaded)

    def test_threaded_is_kept_although_the_schema_note_said_four_objects(self):
        # `store/rulesschema.py` carries a dated correction about this. The
        # test is what stops the correction from quietly becoming untrue again.
        flat = savedviews.save_view(self.con, self.a_view(threaded=False))
        self.assertFalse(savedviews.get_view(self.con, flat.id).threaded)

    def test_the_definition_is_json_with_a_version_in_it(self):
        saved = savedviews.save_view(self.con, self.a_view())
        raw = self.con.execute("SELECT definition FROM saved_view WHERE id = ?",
                               (saved.id,)).fetchone()[0]
        self.assertEqual(json.loads(raw)["version"],
                         savedviews.DEFINITION_VERSION)


class TestTolerance(unittest.TestCase):
    """A definition this corMani cannot read opens as the default view."""

    def read(self, text):
        return savedviews.from_definition(text)

    def test_nothing_at_all_is_the_default_view(self):
        scope, filters, query, sort, threaded = self.read("")
        self.assertEqual(scope, views.Scope())
        self.assertEqual(filters, views.Filters())
        self.assertEqual(query, search.Query())
        self.assertEqual(sort, views.Sort())
        self.assertTrue(threaded)

    def test_text_that_is_not_json_does_not_raise(self):
        self.assertEqual(self.read("not json at all")[0], views.Scope())

    def test_json_that_is_not_an_object_does_not_raise(self):
        self.assertEqual(self.read("[1, 2, 3]")[0], views.Scope())

    def test_a_scope_kind_this_version_does_not_have_becomes_unified(self):
        # NOT "folder with folder_id None", which `scope_where` turns into the
        # literal 0 — a saved search that silently shows nothing at all.
        scope = self.read('{"scope": {"kind": "constellation"}}')[0]
        self.assertEqual(scope.kind, "unified")

    def test_a_date_range_this_version_does_not_have_becomes_any_time(self):
        query = self.read('{"search": {"within": "fortnight"}}')[2]
        self.assertEqual(query.within, "")

    def test_a_sort_key_this_version_does_not_have_becomes_date(self):
        self.assertEqual(self.read('{"sort": {"key": "colour"}}')[3].key, "date")

    def test_an_id_that_is_not_a_number_becomes_none(self):
        # A string reaching a parameter slot is the failure this coercion is
        # for: `account_id = 'seven'` compares equal to nothing and the view
        # silently empties.
        query = self.read('{"search": {"account_id": "seven"}}')[2]
        self.assertIsNone(query.account_id)

    def test_a_field_written_by_a_later_version_is_ignored_not_fatal(self):
        scope, filters, query, sort, threaded = self.read(
            '{"version": 99, "scope": {"kind": "unified"}, "snoozed": true}')
        self.assertEqual(scope.kind, "unified")
        self.assertTrue(threaded)


class TestNamesAreUnique(SavedViewStoreCase):
    def test_a_second_view_of_the_same_name_is_refused_with_a_sentence(self):
        savedviews.save_view(self.con, self.a_view(name="Invoices"))
        with self.assertRaises(ValueError) as raised:
            savedviews.save_view(self.con, self.a_view(name="Invoices"))
        self.assertIn("Invoices", str(raised.exception))

    def test_saving_a_view_over_ITSELF_is_not_a_clash(self):
        # The update path: the dialog loads a view and saves it back. Refusing
        # that would make renaming impossible in the one direction that matters.
        saved = savedviews.save_view(self.con, self.a_view(name="Invoices"))
        again = savedviews.save_view(
            self.con, saved.with_changes(filters=views.Filters(flagged=True)))
        self.assertEqual(again.id, saved.id)
        self.assertTrue(again.filters.flagged)

    def test_a_view_with_no_name_is_refused(self):
        with self.assertRaises(ValueError):
            savedviews.save_view(self.con, self.a_view(name="   "))

    def test_renaming_onto_an_existing_name_is_refused(self):
        savedviews.save_view(self.con, self.a_view(name="Invoices"))
        other = savedviews.save_view(self.con, self.a_view(name="Receipts"))
        with self.assertRaises(ValueError):
            savedviews.rename(self.con, other.id, "Invoices")
        self.assertEqual(savedviews.get_view(self.con, other.id).name,
                         "Receipts")


class TestUnresolved(SavedViewStoreCase):
    """What a view says once the thing it names has gone.

    Every one of these is `0` in `store/views.scope_where` — the correct WHERE
    clause and a terrible explanation. These assert that the explanation exists.
    """

    def test_a_healthy_view_says_nothing(self):
        saved = savedviews.save_view(self.con, self.a_view())
        self.assertEqual(savedviews.unresolved(self.con, saved), "")

    def test_a_deleted_folder_is_named(self):
        saved = savedviews.save_view(self.con, self.a_view())
        self.con.execute("DELETE FROM folder WHERE id = ?", (self.inbox,))
        self.con.commit()
        self.assertIn("folder", savedviews.unresolved(
            self.con, savedviews.get_view(self.con, saved.id)))

    def test_a_removed_account_is_named(self):
        saved = savedviews.save_view(self.con, self.a_view(
            scope=views.Scope(kind="account", account_id=self.account)))
        self.con.execute("DELETE FROM account WHERE id = ?", (self.account,))
        self.con.commit()
        self.assertIn("account", savedviews.unresolved(
            self.con, savedviews.get_view(self.con, saved.id)))

    def test_a_deleted_tag_is_named(self):
        tag_id = add_tag(self.con, "Bordereaux", "#268bd2")
        saved = savedviews.save_view(self.con, self.a_view(
            scope=views.Scope(), filters=views.Filters(tag_id=tag_id)))
        self.con.execute("DELETE FROM tag WHERE id = ?", (tag_id,))
        self.con.commit()
        self.assertIn("tag", savedviews.unresolved(
            self.con, savedviews.get_view(self.con, saved.id)))

    def test_a_stale_view_is_KEPT(self):
        # The whole point: it is marked, never swept up. The folder comes back
        # when the account is re-added.
        saved = savedviews.save_view(self.con, self.a_view())
        self.con.execute("DELETE FROM folder WHERE id = ?", (self.inbox,))
        self.con.commit()
        self.assertIsNotNone(savedviews.get_view(self.con, saved.id))


class TestCounting(unittest.TestCase):
    def setUp(self):
        self.con = support.demo_store(self)

    def test_the_capped_count_agrees_with_the_exact_one_below_the_cap(self):
        # The cap is only sound if it is invisible under the threshold, which
        # is the entire argument for choosing the delegate's own 999.
        for view in savedviews.list_views(self.con):
            exact = savedviews.count_in(self.con, view)
            self.assertLess(exact, savedviews.RAIL_COUNT_CAP)
            self.assertEqual(savedviews.count_capped(self.con, view), exact)

    def test_the_cap_actually_stops_counting(self):
        # With the defect — a cap that was not applied — this returns the whole
        # count and the test fails. Asserted against a cap of 2 so that the
        # demo store is big enough to reach it.
        view = savedviews.list_views(self.con)[0]
        self.assertGreater(savedviews.count_in(self.con, view), 2)
        self.assertEqual(savedviews.count_capped(self.con, view, cap=2), 2)

    def test_the_count_is_the_one_the_list_would_show(self):
        # Through `store/messages.count`, so that the badge beside a virtual
        # folder is the number of rows it opens with rather than a second
        # opinion about it.
        #
        # THIS AGREEMENT IS SAME-ORIGIN AND PROVES ONLY THE DELEGATION —
        # `count_in` calls the function it is being compared with. That is what
        # it is for; the test below is the one that crosses an origin.
        from cormani.store import messages as messages_repo

        view = savedviews.list_views(self.con)[0]
        self.assertEqual(
            savedviews.count_in(self.con, view),
            messages_repo.count(self.con, view.scope, view.filters,
                                search=view.search))

    def test_the_count_agrees_with_SQL_written_independently(self):
        """The cross-origin check, and the reason it is worth the duplication.

        Every other count assertion in this file runs through
        `store/views.clause` on both sides, so a fault in the clause builder
        would move both numbers together and every one of them would still
        pass. This spells the WHERE out by hand for one view whose meaning is
        unambiguous — unread and flagged, in the inbox roles — so the two
        answers have nothing in common but the mailbox.
        """
        view = savedviews.save_view(self.con, savedviews.SavedView(
            name="Unread and flagged",
            filters=views.Filters(unread=True, flagged=True)))
        by_hand = self.con.execute(
            "SELECT COUNT(*) FROM message m JOIN folder f ON f.id = m.folder_id "
            "JOIN account a ON a.id = f.account_id "
            "WHERE f.role = 'inbox' AND m.seen = 0 AND m.flagged = 1 "
            "AND m.deleted = 0 AND a.hidden = 0 AND a.enabled = 1").fetchone()[0]
        # A positive control: a query that counts nothing agrees with anything.
        self.assertGreater(by_hand, 0)
        self.assertEqual(savedviews.count_in(self.con, view), by_hand)


class TestOrderAndRail(SavedViewStoreCase):
    def test_new_views_are_appended_in_the_order_they_were_saved(self):
        for name in ("first", "second", "third"):
            savedviews.save_view(self.con, self.a_view(name=name))
        self.assertEqual([v.name for v in savedviews.list_views(self.con)],
                         ["first", "second", "third"])

    def test_reorder_writes_the_whole_sequence(self):
        made = [savedviews.save_view(self.con, self.a_view(name=n))
                for n in ("first", "second", "third")]
        savedviews.reorder(self.con, [made[2].id, made[0].id, made[1].id])
        self.assertEqual([v.name for v in savedviews.list_views(self.con)],
                         ["third", "first", "second"])

    def test_rail_only_leaves_out_the_ones_taken_out_of_the_rail(self):
        savedviews.save_view(self.con, self.a_view(name="drawn"))
        hidden = savedviews.save_view(self.con,
                                      self.a_view(name="hidden", in_rail=False))
        self.assertEqual([v.name for v in
                          savedviews.list_views(self.con, rail_only=True)],
                         ["drawn"])
        # And it is still THERE — out of the rail is not deleted.
        self.assertIsNotNone(savedviews.get_view(self.con, hidden.id))

    def test_set_in_rail_survives_a_reopen(self):
        saved = savedviews.save_view(self.con, self.a_view())
        savedviews.set_in_rail(self.con, saved.id, False)
        self.assertFalse(
            savedviews.get_view(support.reopened(self.con), saved.id).in_rail)


class TestDescribing(SavedViewStoreCase):
    def test_asks_anything_is_false_for_the_plain_unified_inbox(self):
        self.assertFalse(savedviews.SavedView(name="x").asks_anything)

    def test_asks_anything_notices_each_way_of_narrowing(self):
        for narrowed in (
                dict(filters=views.Filters(unread=True)),
                dict(search=search.Query(text="a")),
                dict(scope=views.Scope(kind="folder", folder_id=self.inbox)),
                dict(scope=views.Scope(role=ROLE_SENT))):
            with self.subTest(**narrowed):
                self.assertTrue(
                    savedviews.SavedView(name="x", **narrowed).asks_anything)

    def test_narrowing_does_not_repeat_the_scope(self):
        # The read-out prints the scope itself and then this. Together they
        # once read "every inbox · every inbox, unread".
        view = savedviews.SavedView(name="x", filters=views.Filters(unread=True))
        self.assertNotIn("inbox", view.narrowing())
        self.assertIn("inbox", view.describe())

    def test_narrowing_is_empty_when_nothing_is_narrowed(self):
        self.assertEqual(savedviews.SavedView(name="x").narrowing(), "")

    def test_describes_is_true_only_for_the_identical_five(self):
        view = self.a_view()
        self.assertTrue(view.describes(view.scope, view.filters, view.search,
                                       view.sort, view.threaded))
        # One field apart, and each one apart on its own: a tab named for a
        # saved search it no longer matches is a claim about its contents.
        self.assertFalse(view.describes(views.Scope(), view.filters,
                                        view.search, view.sort, view.threaded))
        self.assertFalse(view.describes(view.scope, views.Filters(),
                                        view.search, view.sort, view.threaded))
        self.assertFalse(view.describes(view.scope, view.filters,
                                        search.Query(), view.sort,
                                        view.threaded))
        self.assertFalse(view.describes(view.scope, view.filters, view.search,
                                        views.Sort(), view.threaded))
        self.assertFalse(view.describes(view.scope, view.filters, view.search,
                                        view.sort, not view.threaded))

    def test_describe_scope_here_names_the_folder(self):
        saved = savedviews.save_view(self.con, self.a_view())
        self.assertIn("INBOX",
                      savedviews.describe_scope_here(self.con, saved))

    def test_describe_scope_here_falls_back_when_the_folder_is_gone(self):
        saved = savedviews.save_view(self.con, self.a_view())
        self.con.execute("DELETE FROM folder WHERE id = ?", (self.inbox,))
        self.con.commit()
        # A sentence rather than a crash or an empty string.
        self.assertTrue(savedviews.describe_scope_here(
            self.con, savedviews.get_view(self.con, saved.id)))


class TestCounts(SavedViewStoreCase):
    def test_counts_reports_what_check_prints(self):
        savedviews.save_view(self.con, self.a_view(name="drawn"))
        savedviews.save_view(self.con, self.a_view(name="hidden", in_rail=False))
        self.con.execute("DELETE FROM folder WHERE id = ?", (self.inbox,))
        self.con.commit()
        counts = savedviews.counts(self.con)
        self.assertEqual(counts["views"], 2)
        self.assertEqual(counts["in_rail"], 1)
        self.assertEqual(counts["unresolved"], 2)

    def test_counts_on_an_empty_store_are_zero_rather_than_absent(self):
        self.assertEqual(savedviews.counts(self.con),
                         {"views": 0, "in_rail": 0, "unresolved": 0})


class TestDeleting(SavedViewStoreCase):
    def test_deleting_removes_the_row_and_nothing_else(self):
        saved = savedviews.save_view(self.con, self.a_view())
        before = self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        savedviews.delete_view(self.con, saved.id)
        self.assertIsNone(savedviews.get_view(self.con, saved.id))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM message").fetchone()[0],
            before)


if __name__ == "__main__":
    unittest.main()
