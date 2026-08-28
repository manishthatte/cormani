# SPDX-License-Identifier: GPL-3.0-or-later
#
# The account repository: order, groups, colour and hiding.
#
# These are the four things docs/accounts.txt asks for, and each one is tested
# for the class of failure rather than an instance. The order test reopens the
# database, because "the order persisted" and "the order is still in this
# connection's memory" are different claims and only one of them matters.
#
# © Manish Jagdish Thatte
import unittest

from cormani.store import accounts, folders, messages, views

import support


class TestColours(unittest.TestCase):
    def test_the_first_accounts_all_get_different_colours(self):
        # Fifteen accounts must be distinguishable without the user doing any
        # work; the ramp is sized for that.
        chosen = []
        for _ in range(len(accounts.ACCOUNT_COLOURS)):
            chosen.append(accounts.next_colour(chosen))
        self.assertEqual(len(set(chosen)), len(accounts.ACCOUNT_COLOURS))

    def test_beyond_the_ramp_it_reuses_the_least_used(self):
        used = list(accounts.ACCOUNT_COLOURS) + [accounts.ACCOUNT_COLOURS[0]]
        # Every colour is used once except the first, used twice — so the next
        # must not be the first.
        self.assertNotEqual(accounts.next_colour(used), accounts.ACCOUNT_COLOURS[0])

    def test_a_colour_it_does_not_recognise_does_not_break_the_count(self):
        self.assertIn(accounts.next_colour(["#123456", "", None]),
                      accounts.ACCOUNT_COLOURS)


class TestAccounts(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)

    def test_a_new_account_is_given_a_colour_and_a_place(self):
        first = accounts.add_account(self.con, "a@example.com", "google")
        second = accounts.add_account(self.con, "b@example.com", "google")
        a, b = accounts.get_account(self.con, first), accounts.get_account(self.con, second)
        self.assertTrue(a.colour and b.colour)
        self.assertNotEqual(a.colour, b.colour)
        self.assertLess(a.sort_order, b.sort_order)

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError):
            accounts.add_account(self.con, "a@example.com", "carrier-pigeon")

    def test_the_label_falls_back_to_the_address(self):
        account_id = accounts.add_account(self.con, "a@example.com", "google")
        self.assertEqual(accounts.get_account(self.con, account_id).label,
                         "a@example.com")

    def test_the_order_survives_being_written_to_disk(self):
        group = accounts.add_group(self.con, "work")
        ids = [accounts.add_account(self.con, f"{n}@example.com", "google",
                                    group_id=group) for n in range(4)]
        accounts.reorder_accounts(self.con, group, list(reversed(ids)))
        fresh = support.reopened(self.con)
        self.addCleanup(fresh.close)
        self.assertEqual([a.id for a in accounts.list_accounts(fresh)],
                         list(reversed(ids)))

    def test_a_drop_past_the_end_lands_at_the_end(self):
        # A view computing a row index off the end of a list must place the
        # account there, not raise inside a drag.
        group = accounts.add_group(self.con, "work")
        ids = [accounts.add_account(self.con, f"{n}@example.com", "google",
                                    group_id=group) for n in range(3)]
        accounts.move_account(self.con, ids[0], group, 99)
        self.assertEqual([a.id for a in accounts.list_accounts(self.con)],
                         [ids[1], ids[2], ids[0]])
        accounts.move_account(self.con, ids[0], group, -5)
        self.assertEqual(accounts.list_accounts(self.con)[0].id, ids[0])

    def test_moving_an_account_to_no_group_leaves_it_loose_and_last(self):
        group = accounts.add_group(self.con, "work")
        first = accounts.add_account(self.con, "a@example.com", "google",
                                     group_id=group)
        accounts.add_account(self.con, "b@example.com", "google", group_id=group)
        accounts.move_account(self.con, first, None, 0)
        listed = accounts.list_accounts(self.con)
        self.assertIsNone(listed[-1].group_id)
        self.assertEqual(listed[-1].id, first)

    def test_deleting_a_group_keeps_its_accounts(self):
        group = accounts.add_group(self.con, "work")
        account_id = accounts.add_account(self.con, "a@example.com", "google",
                                          group_id=group)
        accounts.delete_group(self.con, group)
        survivor = accounts.get_account(self.con, account_id)
        self.assertIsNotNone(survivor)
        self.assertIsNone(survivor.group_id)

    def test_collapse_is_remembered(self):
        group = accounts.add_group(self.con, "work")
        accounts.set_group_collapsed(self.con, group, True)
        fresh = support.reopened(self.con)
        self.addCleanup(fresh.close)
        self.assertTrue(accounts.get_group(fresh, group).collapsed)

    def test_hiding_removes_it_from_the_rail_and_keeps_the_mail(self):
        account_id = accounts.add_account(self.con, "a@example.com", "google")
        folder = folders.ensure_folder(self.con, account_id, "INBOX",
                                       role=folders.ROLE_INBOX)
        self.con.execute(
            "INSERT INTO message (folder_id, subject, seen) VALUES (?, 'hello', 0)",
            (folder,))
        self.con.commit()

        accounts.set_hidden(self.con, account_id, True)
        self.assertNotIn(account_id, views.visible_account_ids(self.con))
        self.assertEqual(
            len(accounts.list_accounts(self.con, include_hidden=False)), 0)
        # The mail is still there, and still findable.
        self.assertEqual(messages.count(
            self.con, views.Scope(kind="account", account_id=account_id)), 1)

    def test_identity_addresses_include_both_accounts_and_identities(self):
        account_id = accounts.add_account(self.con, "a@example.com", "google")
        accounts.add_identity(self.con, account_id, "Alias@Example.com")
        self.assertEqual(accounts.list_identity_addresses(self.con),
                         {"a@example.com", "alias@example.com"})


if __name__ == "__main__":
    unittest.main()
