# SPDX-License-Identifier: GPL-3.0-or-later
#
# The saved-search half of the command line.
#
# THE FIXTURE IS test_cli's, IMPORTED, for the reason `test_rulecli.py` and
# `test_calcli.py` give: it redirects all four XDG variables and the keyring
# into a temporary directory.
#
# THAT `--searches` WRITES NOTHING IS ASSERTED BY THE MECHANISM. It opens the
# store READ-ONLY, so a reading path that wrote would raise `attempt to write a
# readonly database` and every test below would fail at once. `store/triage.py`
# had exactly that defect and `--check` is what found it.
#
# WHAT IS ASSERTED IS THE WORDS, and one sentence more than for filters. A
# saved search is visible in the rail, so most of this read-out is convenience;
# the line that is NOT is the one about a search whose folder has gone, because
# that state looks identical from the rail to a search that matches nothing
# today. `cormani/viewcli.py`'s header argues it, and the test that would go
# red if the sentence were dropped is `test_a_stale_view_says_it_cannot_run`.
#
# © Manish Jagdish Thatte
import unittest

from test_cli import Fixture

from cormani.store import savedviews, search, views


class ViewFixture(Fixture):
    def account(self, address="work@manitlab.invalid", provider="google"):
        from cormani.store.accounts import add_account
        con = self.store()
        return con, add_account(con, address, provider, display_name="Work")

    def view(self, con, name="Invoices", **kw):
        kw.setdefault("filters", views.Filters(unread=True))
        return savedviews.save_view(con, savedviews.SavedView(name=name, **kw))


class TestSearchesCommand(ViewFixture):
    def test_with_no_store_it_says_so_and_fails(self):
        code, text = self.run_cli("--searches")
        self.assertEqual(code, 1)
        self.assertIn("no store yet", text)

    def test_none_saved_is_not_a_failure_and_says_where_they_come_from(self):
        # "None" has to answer the question the person had, not merely be true.
        self.store()
        code, text = self.run_cli("--searches")
        self.assertEqual(code, 0)
        self.assertIn("no saved searches", text)
        self.assertIn("Save this search", text)

    def test_a_view_is_printed_with_what_it_asks_and_what_it_holds(self):
        con, _ = self.account()
        self.view(con)
        code, text = self.run_cli("--searches")
        self.assertEqual(code, 0)
        self.assertIn("Invoices", text)
        self.assertIn("in the rail", text)
        self.assertIn("unread", text)
        self.assertIn("holds", text)

    def test_the_scope_is_not_printed_twice(self):
        # It once read "every inbox · every inbox, unread" — the read-out named
        # the scope and then `describe` opened with it again.
        con, _ = self.account()
        self.view(con)
        _, text = self.run_cli("--searches")
        asks = [line for line in text.splitlines() if "asks" in line][0]
        self.assertEqual(asks.count("every inbox"), 1)

    def test_a_view_kept_out_of_the_rail_says_where_it_IS(self):
        con, _ = self.account()
        self.view(con, name="Attachments", in_rail=False)
        _, text = self.run_cli("--searches")
        self.assertIn("Search menu only", text)
        self.assertIn("still runs", text)

    def test_a_stale_view_says_it_cannot_run_and_names_what_went(self):
        """The sentence this switch exists for.

        From the rail, a saved search whose folder was deleted and one that
        matches nothing today look identical: an empty virtual folder.
        """
        from cormani.store.folders import ROLE_INBOX, ensure_folder
        con, account_id = self.account()
        folder = ensure_folder(con, account_id, "Lists/Tenders",
                               role=ROLE_INBOX)
        self.view(con, name="Tenders",
                  scope=views.Scope(kind="folder", folder_id=folder))
        con.execute("DELETE FROM folder WHERE id = ?", (folder,))
        con.commit()
        code, text = self.run_cli("--searches")
        self.assertEqual(code, 0)
        self.assertIn("CANNOT RUN", text)
        self.assertIn("folder", text)
        # And it says the search is KEPT, which is the part a person needs in
        # order not to go looking for what deleted it.
        self.assertIn("never deleted", text)

    def test_a_stale_view_prints_no_count_at_all(self):
        # A 0 meaning "this cannot run" beside a 0 meaning "no mail matches" is
        # two different facts wearing one digit.
        from cormani.store.folders import ROLE_INBOX, ensure_folder
        con, account_id = self.account()
        folder = ensure_folder(con, account_id, "Gone", role=ROLE_INBOX)
        self.view(con, name="Tenders",
                  scope=views.Scope(kind="folder", folder_id=folder))
        con.execute("DELETE FROM folder WHERE id = ?", (folder,))
        con.commit()
        _, text = self.run_cli("--searches")
        self.assertNotIn("0 messages", text)

    def test_a_full_text_search_is_described_by_what_it_looks_for(self):
        con, _ = self.account()
        self.view(con, name="Wavelength", filters=views.Filters(),
                  search=search.Query(text="wavelength"))
        _, text = self.run_cli("--searches")
        self.assertIn("wavelength", text)


class TestTheCheckLines(ViewFixture):
    def test_check_says_nothing_when_there_are_none(self):
        # A report is a page read in a hurry. A line saying "0 saved searches"
        # is a line spent on a feature the user has not used.
        self.store()
        _, text = self.run_cli("--check")
        self.assertNotIn("saved searches", text)

    def test_check_counts_them_and_the_ones_not_in_the_rail(self):
        con, _ = self.account()
        self.view(con, name="Drawn")
        self.view(con, name="Hidden", in_rail=False)
        _, text = self.run_cli("--check")
        self.assertIn("saved searches   2 saved", text)
        self.assertIn("1 not drawn in the rail", text)

    def test_check_names_the_stale_ones_and_points_at_the_switch(self):
        from cormani.store.folders import ROLE_INBOX, ensure_folder
        con, account_id = self.account()
        folder = ensure_folder(con, account_id, "Gone", role=ROLE_INBOX)
        self.view(con, name="Tenders",
                  scope=views.Scope(kind="folder", folder_id=folder))
        con.execute("DELETE FROM folder WHERE id = ?", (folder,))
        con.commit()
        _, text = self.run_cli("--check")
        self.assertIn("has gone", text)
        self.assertIn("--searches", text)

    def test_a_store_older_than_this_stage_is_not_asked(self):
        """`--check` must work when the migration is what is broken.

        The guard is the schema version and nothing else — `cli.check` has no
        try/except around any of its five sub-reports, which is the same
        bargain `calcli` and `rulecli` are on. So what is asserted is that the
        version is honoured: at v8 there is no `saved_view` table, the report
        must not be reached, and `--check` must still print its verdict.
        """
        con = self.store()
        con.execute("DROP TABLE saved_view")
        con.execute("PRAGMA user_version = 8")
        con.commit()
        code, text = self.run_cli("--check")
        self.assertIn("verdict", text)
        self.assertNotIn("saved searches", text)

    def test_searches_on_a_store_older_than_this_stage_says_so(self):
        con = self.store()
        con.execute("PRAGMA user_version = 8")
        con.commit()
        code, text = self.run_cli("--searches")
        self.assertEqual(code, 1)
        self.assertIn("predates saved searches", text)


if __name__ == "__main__":
    unittest.main()
