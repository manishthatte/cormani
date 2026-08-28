# SPDX-License-Identifier: GPL-3.0-or-later
#
# The two item models.
#
# The rail's drag and drop is tested through the model's own mime interface
# rather than by simulating a drag, because python3-pyside6.qttest is not
# packaged in Debian and there is therefore no way to synthesise the mouse. What
# CAN be tested is everything Qt would call — canDropMimeData, dropMimeData —
# and that the result reached the database rather than only the tree.
#
# © Manish Jagdish Thatte
import unittest

from PySide6.QtCore import QMimeData, QModelIndex, Qt

from cormani.store import accounts, edits, messages, search, views
from cormani.ui.models import messages as message_model
from cormani.ui.models import rail as rail_model

import support
from test_threads import Conversation


@support.requires_qt
class TestRailModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        self.con = support.demo_store(self)
        self.model = rail_model.RailModel(self.con)

    def _keys(self, parent=QModelIndex()):
        return [self.model.index(r, 0, parent).data(rail_model.KeyRole)
                for r in range(self.model.rowCount(parent))]

    def test_the_five_sections_are_always_there(self):
        # Saved searches sits between Accounts and Sites so that every MAIL row
        # is above every other subsystem — `RailModel._saved` argues it.
        self.assertEqual(self._keys(), ["section:unified", "section:accounts",
                                        "section:saved", "section:sites",
                                        "section:calendars"])

    def test_a_group_badge_is_the_sum_of_its_accounts(self):
        section = self.model.section_index("accounts")
        for row in range(self.model.rowCount(section)):
            index = self.model.index(row, 0, section)
            if index.data(rail_model.KindRole) != rail_model.GROUP:
                continue
            children = [self.model.index(r, 0, index).data(rail_model.CountRole)
                        for r in range(self.model.rowCount(index))]
            self.assertEqual(index.data(rail_model.CountRole), sum(children))

    def test_an_account_carries_its_colour_down_to_its_folders(self):
        account = accounts.list_accounts(self.con)[0]
        index = self.model.index_for_key(f"account:{account.id}")
        self.assertEqual(index.data(rail_model.ColourRole), account.colour)
        folder = self.model.index(0, 0, index)
        self.assertEqual(folder.data(rail_model.ColourRole), account.colour)

    def test_every_node_answers_error_role_so_the_delegate_can_paint(self):
        # `error` was added to Node without a default once; paint then raised
        # AttributeError on Inbox and every other row, and the rail went blank.
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

        from cormani.ui import theme as theme_mod
        from cormani.ui.rail import RailDelegate

        index = self.model.index_for_key("unified:inbox")
        self.assertFalse(index.data(rail_model.ErrorRole))
        delegate = RailDelegate()
        delegate.theme = theme_mod.resolved(
            theme_mod.SOLARIZED_LIGHT, self.app.palette())
        pix = QPixmap(200, 24)
        pix.fill()
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 200, 24)
        opt.state = QStyle.StateFlag.State_Enabled
        opt.font = self.app.font()
        painter = QPainter(pix)
        delegate.paint(painter, opt, index)
        painter.end()

    def test_only_a_leaf_that_means_something_is_selectable(self):
        for key, selectable in (("section:unified", False), ("unified:inbox", True),
                                ("site:whatsapp", False), ("hint:calendars", False)):
            index = self.model.index_for_key(key)
            flags = self.model.flags(index)
            self.assertEqual(bool(flags & Qt.ItemFlag.ItemIsSelectable), selectable, key)

    def test_only_the_accounts_section_and_its_groups_accept_a_drop(self):
        for key, droppable in (("section:accounts", True), ("section:unified", False),
                               ("section:sites", False), ("group:1", True)):
            flags = self.model.flags(self.model.index_for_key(key))
            self.assertEqual(bool(flags & Qt.ItemFlag.ItemIsDropEnabled),
                             droppable, key)

    def test_a_drop_moves_the_account_in_the_database(self):
        account = accounts.list_accounts(self.con)[0]
        target = [g for g in accounts.list_groups(self.con)
                  if g.id != account.group_id][0]
        data = self.model.mimeData([self.model.index_for_key(f"account:{account.id}")])
        parent = self.model.group_index(target.id)
        self.assertTrue(self.model.canDropMimeData(
            data, Qt.DropAction.MoveAction, -1, 0, parent))
        self.assertTrue(self.model.dropMimeData(
            data, Qt.DropAction.MoveAction, -1, 0, parent))

        fresh = support.reopened(self.con)
        self.addCleanup(fresh.close)
        self.assertEqual(accounts.get_account(fresh, account.id).group_id, target.id)

    def test_dropping_on_the_section_itself_ungroups(self):
        account = accounts.list_accounts(self.con)[0]
        data = self.model.mimeData([self.model.index_for_key(f"account:{account.id}")])
        self.model.dropMimeData(data, Qt.DropAction.MoveAction, -1, 0,
                                self.model.section_index("accounts"))
        self.assertIsNone(accounts.get_account(self.con, account.id).group_id)

    def test_a_group_can_be_reordered(self):
        before = [g.id for g in accounts.list_groups(self.con)]
        data = self.model.mimeData([self.model.group_index(before[-1])])
        self.model.dropMimeData(data, Qt.DropAction.MoveAction, -1, 0,
                                self.model.group_index(before[0]))
        after = [g.id for g in accounts.list_groups(self.con)]
        self.assertEqual(after[0], before[-1])
        self.assertEqual(sorted(after), sorted(before))

    def test_a_payload_it_did_not_write_is_refused(self):
        # The only way to reach this is another application claiming the mime
        # type. Refuse the drop; do not raise inside a drag.
        for payload in (b"{not json", b"null", b'[{"kind": "wombat"}]', b"[]"):
            data = QMimeData()
            data.setData(rail_model.MIME_TYPE, payload)
            parent = self.model.group_index(1)
            self.assertFalse(self.model.canDropMimeData(
                data, Qt.DropAction.MoveAction, -1, 0, parent), payload)
            self.assertFalse(self.model.dropMimeData(
                data, Qt.DropAction.MoveAction, -1, 0, parent), payload)

    def test_hidden_accounts_appear_only_when_asked_for(self):
        account = accounts.list_accounts(self.con)[0]
        accounts.set_hidden(self.con, account.id, True)
        self.model.rebuild()
        self.assertFalse(self.model.index_for_key(f"account:{account.id}").isValid())
        self.model.set_show_hidden(True)
        index = self.model.index_for_key(f"account:{account.id}")
        self.assertTrue(index.isValid())
        self.assertTrue(index.data(rail_model.HiddenRole))

    def test_a_store_with_no_accounts_still_builds_a_tree(self):
        model = rail_model.RailModel(support.temp_store(self))
        self.assertEqual(model.rowCount(), 5)
        hint = model.index_for_key("hint:accounts")
        self.assertTrue(hint.isValid())
        self.assertFalse(model.flags(hint) & Qt.ItemFlag.ItemIsEnabled)


@support.requires_qt
class TestMessageModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        self.con = support.demo_store(self)
        self.model = message_model.MessageModel(self.con, page_size=25)
        # FLAT for this class. Paging, the roles and the mutations behave the
        # same either way, and asserting them against a flat list is asserting
        # them against a list rather than against the grouping. The tree is
        # TestThreadedModel below.
        self.model.set_query(threaded=False)

    def test_it_pages_rather_than_loading_everything(self):
        self.assertEqual(self.model.rowCount(), 25)
        self.assertGreater(self.model.total, 25)
        self.assertTrue(self.model.canFetchMore())
        self.model.fetchMore()
        self.assertEqual(self.model.rowCount(), 50)

    def test_fetch_all_reaches_the_end_and_then_stops(self):
        self.model.fetch_all()
        self.assertEqual(self.model.rowCount(), self.model.total)
        self.assertFalse(self.model.canFetchMore())

    def test_the_roles_the_delegate_reads_are_all_present(self):
        index = self.model.index(0, 0)
        row = index.data(message_model.RowRole)
        self.assertIsNotNone(row)
        self.assertEqual(index.data(message_model.MessageIdRole), row.id)
        self.assertEqual(index.data(message_model.UnreadRole), not row.seen)
        self.assertEqual(index.data(message_model.ColourRole), row.account_colour)
        self.assertEqual(index.data(Qt.ItemDataRole.DisplayRole), row.subject_label)

    def test_unthreaded_every_row_is_a_root(self):
        for position in range(self.model.rowCount()):
            index = self.model.index(position, 0)
            self.assertFalse(self.model.parent(index).isValid())
            self.assertEqual(self.model.rowCount(index), 0)
            self.assertEqual(index.data(message_model.ThreadCountRole), 0)

    def test_a_flag_change_repaints_and_a_move_removes(self):
        row = self.model.row_at(0)
        before = self.model.rowCount()

        edits.set_flagged(self.con, [row.id], True)
        self.model.apply_change([row.id])
        self.assertEqual(self.model.rowCount(), before)
        self.assertTrue(self.model.row_at(0).flagged)

        edits.archive(self.con, [row.id])
        self.model.apply_change([row.id])
        self.assertEqual(self.model.rowCount(), before - 1)
        self.assertFalse(self.model.index_of(row.id).isValid())

    def test_removing_several_at_once_keeps_the_rest(self):
        rows = [self.model.row_at(n) for n in range(4)]
        doomed = [rows[0].id, rows[2].id]
        survivors = [rows[1].id, rows[3].id]
        edits.archive(self.con, doomed)
        self.model.apply_change(doomed)
        for message_id in survivors:
            self.assertTrue(self.model.index_of(message_id).isValid())
        for message_id in doomed:
            self.assertFalse(self.model.index_of(message_id).isValid())

    def test_next_unread_walks_forwards_and_backwards_and_gives_up(self):
        self.model.fetch_all()
        unread = [n for n, row in enumerate(self.model.rows()) if not row.seen]
        self.assertTrue(unread)

        def at(position):
            return self.model.index(position, 0)

        self.assertEqual(self.model.next_unread(QModelIndex()).row(), unread[0])
        self.assertEqual(self.model.next_unread(at(unread[0])).row(), unread[1])
        self.assertEqual(
            self.model.next_unread(at(unread[1]), forward=False).row(), unread[0])
        self.assertFalse(
            self.model.next_unread(at(unread[0]), forward=False).isValid())
        self.assertFalse(self.model.next_unread(at(unread[-1])).isValid())

    def test_next_unread_fetches_past_the_loaded_page(self):
        # `n` means the next unread message, not the next one already fetched.
        model = message_model.MessageModel(self.con, page_size=25)
        model.set_query(threaded=False, sort=views.Sort(key="date", descending=False))
        loaded = model.rowCount()
        found = model.next_unread(model.index(loaded - 1, 0))
        if found.isValid():
            self.assertGreaterEqual(found.row(), loaded)

    def test_changing_the_query_reloads_from_the_top(self):
        self.model.set_query(filters=views.Filters(flagged=True))
        self.assertTrue(all(r.flagged for r in self.model.rows()))
        self.assertEqual(self.model.total,
                         messages.count(self.con, self.model.scope,
                                        self.model.filters))

    def test_counts_are_announced(self):
        seen = []
        self.model.counts_changed.connect(lambda a, b: seen.append((a, b)))
        self.model.set_query(filters=views.Filters(unread=True))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][1], self.model.total)


if __name__ == "__main__":
    unittest.main()


@support.requires_qt
class TestThreadedModel(unittest.TestCase):
    """The tree. The fixture is one conversation of three — two in the inbox and
    the user's own reply in Sent — beside one message that is on its own."""

    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        self.fixture = Conversation(self)
        self.con = self.fixture.con
        self.model = message_model.MessageModel(self.con, page_size=50)

    def conversation(self):
        for position in range(self.model.rowCount()):
            index = self.model.index(position, 0)
            if self.model.rowCount(index):
                return index
        self.fail("no conversation in the list")

    def test_a_conversation_is_one_row_with_the_rest_beneath_it(self):
        self.assertEqual(self.model.rowCount(), 2)          # and four messages
        index = self.conversation()
        self.assertEqual(self.model.rowCount(index), 2)
        self.assertEqual(index.data(message_model.ThreadCountRole), 3)
        self.assertFalse(self.model.parent(index).isValid())
        child = self.model.index(0, 0, index)
        self.assertEqual(self.model.parent(child), index)

    def test_the_row_is_the_newest_message_in_this_view(self):
        # NOT the newest message of the conversation: the Sent reply is not in
        # the inbox, and every top-level row is a message the inbox holds.
        index = self.conversation()
        self.assertEqual(index.data(message_model.RowRole).id, self.fixture.reply)
        self.assertTrue(index.data(message_model.InScopeRole))

    def test_the_reply_from_another_folder_is_there_and_says_where_it_is(self):
        index = self.conversation()
        rows = [self.model.index(n, 0, index).data(message_model.RowRole)
                for n in range(self.model.rowCount(index))]
        mine = [r for r in rows if r.id == self.fixture.mine]
        self.assertEqual(len(mine), 1)
        self.assertFalse(mine[0].in_scope)
        self.assertEqual(mine[0].location, "Sent · manitlab")

    def test_a_conversation_stands_for_the_messages_in_view_and_no_others(self):
        index = self.conversation()
        self.assertEqual(sorted(self.model.thread_ids(index)),
                         sorted([self.fixture.root, self.fixture.reply]))

    def test_archiving_the_row_promotes_the_next_message_in_view(self):
        index = self.conversation()
        edits.archive(self.con, [self.fixture.reply])
        self.model.apply_change([self.fixture.reply])
        index = self.conversation()
        self.assertEqual(index.data(message_model.RowRole).id, self.fixture.root)
        self.assertFalse(self.model.index_of(self.fixture.reply).isValid())
        # And the reply that is only context is still there beneath it.
        self.assertTrue(self.model.index_of(self.fixture.mine).isValid())

    def test_a_conversation_leaves_entirely_when_its_last_message_does(self):
        edits.archive(self.con, [self.fixture.reply, self.fixture.root])
        self.model.apply_change([self.fixture.reply, self.fixture.root])
        self.assertFalse(self.model.index_of(self.fixture.mine).isValid())
        self.assertEqual(self.model.rowCount(), 1)

    def test_a_context_row_is_never_removed_by_a_filter_it_was_not_subject_to(self):
        index = self.conversation()
        edits.set_seen(self.con, [self.fixture.mine], True)
        self.model.apply_change([self.fixture.mine])
        self.assertTrue(self.model.index_of(self.fixture.mine).isValid())

    def test_grouping_gives_way_to_an_order_it_cannot_keep(self):
        self.assertTrue(self.model.grouping)
        self.model.set_query(sort=views.Sort(key="sender"))
        self.assertFalse(self.model.grouping)
        self.assertTrue(self.model.threaded)        # asked for, not available
        self.assertEqual(self.model.rowCount(), 3)  # the inbox, flat

    def test_grouping_gives_way_to_a_search(self):
        self.model.set_query(search=search.Query(text="wavelengths"))
        self.assertFalse(self.model.grouping)
        for position in range(self.model.rowCount()):
            self.assertEqual(self.model.rowCount(self.model.index(position, 0)), 0)

    def test_turning_it_off_flattens_the_list(self):
        self.model.set_query(threaded=False)
        self.assertEqual(self.model.rowCount(), 3)  # the inbox, unthreaded
        self.assertFalse(self.model.grouping)
