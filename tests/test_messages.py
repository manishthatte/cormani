# SPDX-License-Identifier: GPL-3.0-or-later
#
# The message query layer: scopes, filters, order and the mutations.
#
# The tests that matter most here are the ones guarding a CLASS of mistake: that
# a filter's "off" state does not constrain, that a substring search for "50%"
# looks for a per-cent sign rather than a wildcard, and that a move which cannot
# be performed is reported rather than silently counted as done.
#
# © Manish Jagdish Thatte
import unittest

from cormani.store import accounts, edits, folders, messages, tags, views

import support


class TestScopes(unittest.TestCase):
    def setUp(self):
        self.con = support.demo_store(self)

    def test_a_unified_scope_covers_every_visible_account(self):
        total = messages.count(self.con, views.Scope())
        per_account = sum(
            messages.count(self.con, views.Scope(kind="account", account_id=a.id))
            for a in accounts.list_accounts(self.con))
        self.assertEqual(total, per_account)
        self.assertGreater(total, 0)

    def test_hiding_an_account_takes_it_out_of_the_unified_view(self):
        first = accounts.list_accounts(self.con)[0]
        before = messages.count(self.con, views.Scope())
        mine = messages.count(self.con, views.Scope(kind="account",
                                                       account_id=first.id))
        accounts.set_hidden(self.con, first.id, True)
        self.assertEqual(messages.count(self.con, views.Scope()), before - mine)

    def test_a_scope_that_selects_nothing_returns_nothing_rather_than_failing(self):
        # An empty IN () is a syntax error; the right answer is an empty list.
        for scope in (views.Scope(kind="folder", folder_id=None),
                      views.Scope(kind="account", account_id=None),
                      views.Scope(kind="account", role="nonesuch", account_id=1)):
            self.assertEqual(messages.fetch(self.con, scope), [])
            self.assertEqual(messages.count(self.con, scope), 0)

    def test_owed_is_inbound_and_unanswered(self):
        rows = messages.fetch(self.con, views.Scope(role=views.ROLE_OWED),
                              limit=500)
        mine = accounts.list_identity_addresses(self.con)
        self.assertTrue(rows)
        for row in rows:
            self.assertFalse(row.answered)
            self.assertFalse(row.draft)
            self.assertNotIn(row.from_addr.lower(), mine)

    def test_sent_and_drafts_name_the_recipient(self):
        for role in (folders.ROLE_SENT, folders.ROLE_DRAFTS):
            for row in messages.fetch(self.con, views.Scope(role=role), limit=20):
                self.assertTrue(row.outgoing)
                self.assertEqual(row.correspondent, row.to_addrs.split(",")[0].strip())


class TestFilters(unittest.TestCase):
    def setUp(self):
        self.con = support.demo_store(self)
        self.scope = views.Scope()

    def _count(self, **kw):
        return messages.count(self.con, self.scope, views.Filters(**kw))

    def test_no_filter_constrains_nothing(self):
        self.assertEqual(self._count(), messages.count(self.con, self.scope))
        self.assertFalse(views.Filters().active)

    def test_each_toggle_narrows_and_matches_its_predicate(self):
        base = self._count()
        for field, check in (
                ("unread", lambda r: not r.seen),
                ("flagged", lambda r: r.flagged),
                ("attachment", lambda r: r.has_attachment),
                ("tagged", lambda r: bool(r.tags))):
            narrowed = self._count(**{field: True})
            self.assertLess(narrowed, base, field)
            self.assertGreater(narrowed, 0, field)
            rows = messages.fetch(self.con, self.scope,
                                  views.Filters(**{field: True}), limit=500)
            self.assertTrue(all(check(r) for r in rows), field)

    def test_the_contact_filter_uses_the_address_book(self):
        rows = messages.fetch(self.con, self.scope,
                              views.Filters(contact=True), limit=500)
        known = {r[0].lower() for r in
                 self.con.execute("SELECT value FROM handle WHERE kind='email'")}
        self.assertTrue(rows)
        self.assertTrue(all(r.from_addr.lower() in known for r in rows))

    def test_a_specific_tag_beats_the_any_tag_toggle(self):
        tag = tags.by_shortcut(self.con, 1)
        specific = self._count(tagged=True, tag_id=tag.id)
        self.assertLessEqual(specific, self._count(tagged=True))
        rows = messages.fetch(self.con, self.scope,
                              views.Filters(tag_id=tag.id), limit=500)
        self.assertTrue(all(tag.id in [t.id for t in r.tags] for r in rows))

    def test_the_text_filter_treats_wildcards_as_characters(self):
        # There is a message titled "50% off office supplies…" in the demo
        # trash. A search for "50%" must find it, and one for "50_" must not.
        trash = views.Scope(kind="unified", role=folders.ROLE_TRASH)
        self.assertEqual(messages.count(self.con, trash,
                                        views.Filters(text="50%")), 1)
        self.assertEqual(messages.count(self.con, trash,
                                        views.Filters(text="50_")), 0)
        # A lone per-cent sign is a search for that character, so it finds the
        # same message — not everything, which is what an unescaped wildcard
        # would have done.
        # And a lone per-cent sign matches the CHARACTER, not everything. Across
        # the whole inbox it finds only the messages that really contain one; an
        # unescaped wildcard would have returned every row.
        everything = messages.count(self.con, views.Scope())
        percent = messages.count(self.con, views.Scope(),
                                 views.Filters(text="%"))
        self.assertGreater(percent, 0)
        self.assertLess(percent, everything // 10)

    def test_the_text_filter_looks_at_more_than_the_subject(self):
        self.assertGreater(
            messages.count(self.con, self.scope,
                           views.Filters(text="sound engineer")), 0)


class TestOrderAndPaging(unittest.TestCase):
    def setUp(self):
        self.con = support.demo_store(self)

    def test_every_sort_key_is_honoured_in_both_directions(self):
        for key in views.SORT_KEYS:
            for descending in (True, False):
                rows = messages.fetch(
                    self.con, views.Scope(),
                    sort=views.Sort(key=key, descending=descending), limit=40)
                self.assertGreater(len(rows), 1)
                values = [self._key_of(r, key) for r in rows]
                self.assertEqual(values, sorted(values, reverse=descending),
                                 f"{key} {'desc' if descending else 'asc'}")

    @staticmethod
    def _key_of(row, key):
        if key == "date":
            return row.date_at
        if key == "sender":
            return (row.from_name or row.from_addr).lower()
        return (row.subject or "").lower().removeprefix("re: ").removeprefix("fwd: ")

    def test_toggling_a_sort_reverses_it_and_a_new_key_starts_sensibly(self):
        sort = views.Sort()
        self.assertTrue(sort.descending)
        self.assertFalse(sort.toggled("date").descending)
        self.assertFalse(views.Sort().toggled("sender").descending)
        self.assertTrue(views.Sort(key="sender").toggled("date").descending)

    def test_paging_walks_the_whole_list_without_repeating_a_row(self):
        seen, offset = [], 0
        while True:
            page = messages.fetch(self.con, views.Scope(), limit=25, offset=offset)
            if not page:
                break
            seen.extend(r.id for r in page)
            offset += 25
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), messages.count(self.con, views.Scope()))


class TestMutations(unittest.TestCase):
    def setUp(self):
        self.con = support.demo_store(self)

    def _first(self):
        return messages.fetch(self.con, views.Scope(), limit=1)[0]

    def test_flags_are_set_and_cleared(self):
        row = self._first()
        edits.set_seen(self.con, [row.id], True)
        edits.set_flagged(self.con, [row.id], True)
        after = messages.get_row(self.con, row.id)
        self.assertTrue(after.seen and after.flagged)
        edits.set_flagged(self.con, [row.id], False)
        self.assertFalse(messages.get_row(self.con, row.id).flagged)

    def test_archive_moves_to_that_account_s_own_archive_folder(self):
        row = self._first()
        moved, skipped = edits.archive(self.con, [row.id])
        after = messages.get_row(self.con, row.id)
        self.assertEqual((moved, skipped), (1, []))
        self.assertEqual(after.folder_role, folders.ROLE_ARCHIVE)
        self.assertEqual(after.account_id, row.account_id)

    def test_a_move_that_cannot_be_made_is_reported_not_swallowed(self):
        row = self._first()
        self.con.execute("DELETE FROM folder WHERE account_id = ? AND role = ?",
                         (row.account_id, folders.ROLE_ARCHIVE))
        self.con.commit()
        moved, skipped = edits.archive(self.con, [row.id])
        self.assertEqual(moved, 0)
        self.assertEqual(skipped, [row.id])

    def test_a_moved_message_gives_up_its_uid(self):
        # UNIQUE (folder_id, uid) is per folder; carrying the old one across
        # would collide with a real message in the destination.
        row = self._first()
        self.con.execute("UPDATE message SET uid = 4242 WHERE id = ?", (row.id,))
        self.con.commit()
        edits.archive(self.con, [row.id])
        self.assertIsNone(self.con.execute(
            "SELECT uid FROM message WHERE id = ?", (row.id,)).fetchone()[0])

    def test_delete_moves_to_trash_rather_than_erasing(self):
        row = self._first()
        edits.trash(self.con, [row.id])
        self.assertIsNotNone(messages.get_row(self.con, row.id))
        self.assertEqual(messages.get_row(self.con, row.id).folder_role,
                         folders.ROLE_TRASH)

    def test_filter_ids_says_which_messages_left_the_view(self):
        rows = messages.fetch(self.con, views.Scope(), limit=3)
        ids = [r.id for r in rows]
        edits.archive(self.con, ids[:1])
        staying = messages.filter_ids(self.con, views.Scope(), None, ids)
        self.assertEqual(staying, set(ids[1:]))

    def test_a_mutation_of_nothing_does_nothing(self):
        self.assertEqual(edits.set_seen(self.con, [], True), 0)
        self.assertEqual(edits.archive(self.con, []), (0, []))
        self.assertEqual(messages.filter_ids(self.con, views.Scope(), None, []),
                         set())


class TestCounts(unittest.TestCase):
    def setUp(self):
        self.con = support.demo_store(self)

    def test_unread_counts_are_per_account_and_inbox_only(self):
        counts = messages.unread_counts(self.con)
        for account_id, count in counts.items():
            expected = messages.count(
                self.con,
                views.Scope(kind="account", role=folders.ROLE_INBOX,
                               account_id=account_id),
                views.Filters(unread=True))
            self.assertEqual(count, expected)

    def test_the_unified_counts_agree_with_the_scopes_they_describe(self):
        counts = messages.scope_counts(self.con)
        self.assertEqual(counts["inbox"],
                         sum(messages.unread_counts(self.con).values()))
        self.assertEqual(counts["owed"], messages.count(
            self.con, views.Scope(role=views.ROLE_OWED)))


if __name__ == "__main__":
    unittest.main()
