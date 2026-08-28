# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a finished sync should announce, and what it must not.
#
# The engine's half — `arrived` and `quiet_ids` — is asserted in
# `tests/test_arrivals.py`. This is the window's half: which of those ids
# become a notification, what the words are, and the three silences (still
# fetching, window active, nothing left after filters).
#
# © Manish Jagdish Thatte
import unittest
from unittest import mock

import support

from cormani.imap.engine import AccountResult


class FakeFiltered:
    def __init__(self, quiet):
        self._quiet = set(quiet)

    def quiet_ids(self):
        return set(self._quiet)


class TestAnnounceable(unittest.TestCase):
    def test_nothing_arrived(self):
        from cormani.ui.mailnotify import announceable
        self.assertEqual(announceable([AccountResult(1, "a@x", ok=True)]), [])

    def test_a_failed_account_contributes_nothing(self):
        from cormani.ui.mailnotify import announceable
        self.assertEqual(
            announceable([AccountResult(1, "a@x", ok=False, arrived=[7])]),
            [])

    def test_without_filters_everything_that_arrived_is_announced(self):
        from cormani.ui.mailnotify import announceable
        self.assertEqual(
            announceable([AccountResult(1, "a@x", ok=True, arrived=[3, 4])]),
            [3, 4])

    def test_quiet_ids_are_skipped(self):
        from cormani.ui.mailnotify import announceable
        result = AccountResult(1, "a@x", ok=True, arrived=[3, 4, 5],
                               filtered=FakeFiltered([4]))
        self.assertEqual(announceable([result]), [3, 5])

    def test_still_fetching_is_the_remaining_count(self):
        from cormani.ui.mailnotify import still_fetching
        self.assertFalse(still_fetching([AccountResult(1, "a@x", ok=True)]))
        self.assertTrue(still_fetching(
            [AccountResult(1, "a@x", ok=True, remaining=40)]))


class TestDescribeAndAnnounce(unittest.TestCase):
    def setUp(self):
        self.con = support.demo_store(self)

    def _inbox_ids(self, n=1):
        rows = self.con.execute(
            "SELECT m.id FROM message m JOIN folder f ON f.id = m.folder_id "
            "WHERE f.role = 'inbox' AND m.deleted = 0 ORDER BY m.id LIMIT ?",
            (n,)).fetchall()
        return [r[0] for r in rows]

    def test_one_message_names_the_sender_and_subject(self):
        from cormani.ui.mailnotify import describe
        mid = self._inbox_ids(1)[0]
        title, body = describe(self.con, [mid])
        self.assertTrue(title)
        self.assertTrue(body)
        self.assertNotIn("new messages", title)

    def test_several_messages_are_a_count_with_a_preview(self):
        from cormani.ui.mailnotify import describe
        ids = self._inbox_ids(5)
        self.assertGreaterEqual(len(ids), 4)
        title, body = describe(self.con, ids)
        self.assertIn(f"{len(ids)} new messages", title)
        self.assertIn("—", body)

    def test_announce_is_silent_when_the_window_is_active(self):
        from cormani.ui.mailnotify import announce
        result = AccountResult(1, "a@x", ok=True, arrived=self._inbox_ids(1))
        sent = []
        words = announce([result], self.con, notifier=lambda *a, **k: sent.append(a) or True,
                         window_active=True)
        self.assertIsNone(words)
        self.assertEqual(sent, [])

    def test_announce_is_silent_while_more_is_still_to_fetch(self):
        from cormani.ui.mailnotify import announce
        result = AccountResult(1, "a@x", ok=True, arrived=self._inbox_ids(1),
                               remaining=100)
        sent = []
        self.assertIsNone(announce(
            [result], self.con,
            notifier=lambda *a, **k: sent.append(a) or True))
        self.assertEqual(sent, [])

    def test_announce_sends_one_notification_and_returns_nothing_when_it_did(self):
        from cormani.ui.mailnotify import announce
        result = AccountResult(1, "a@x", ok=True, arrived=self._inbox_ids(1))
        sent = []
        words = announce([result], self.con,
                         notifier=lambda *a, **k: sent.append(a) or True)
        self.assertIsNone(words)
        self.assertEqual(len(sent), 1)

    def test_a_failed_send_returns_the_words_for_the_status_bar(self):
        from cormani.ui.mailnotify import announce
        result = AccountResult(1, "a@x", ok=True, arrived=self._inbox_ids(1))
        words = announce([result], self.con, notifier=lambda *a, **k: False)
        self.assertIsNotNone(words)
        self.assertTrue(words)

    def test_a_silenced_message_is_not_announced(self):
        from cormani.ui.mailnotify import announce
        mid = self._inbox_ids(1)[0]
        result = AccountResult(1, "a@x", ok=True, arrived=[mid],
                               filtered=FakeFiltered([mid]))
        sent = []
        self.assertIsNone(announce(
            [result], self.con,
            notifier=lambda *a, **k: sent.append(a) or True))
        self.assertEqual(sent, [])


@support.requires_qt
class TestTrayAndClose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def test_quit_sets_the_flag_that_close_honours(self):
        from cormani.ui.window import MainWindow

        window = support.own(self, MainWindow(support.demo_store(self),
                                              demo=True))
        window._force_quit = False
        window.quit_application()
        self.assertTrue(window._force_quit)

    def test_without_a_tray_close_still_ends_the_window(self):
        from cormani.ui import notifyhost
        from cormani.ui.window import MainWindow
        from PySide6.QtGui import QCloseEvent

        window = support.own(self, MainWindow(support.demo_store(self),
                                              demo=True))
        window._tray = None
        event = QCloseEvent()
        self.assertFalse(notifyhost.handle_close(window, event))

    def test_with_a_tray_close_hides_unless_quit_was_asked(self):
        from cormani.ui import notifyhost
        from cormani.ui.window import MainWindow
        from PySide6.QtGui import QCloseEvent

        window = support.own(self, MainWindow(support.demo_store(self),
                                              demo=True))

        class FakeTray:
            def hide(self):
                pass

        window._tray = FakeTray()
        window._force_quit = False
        event = QCloseEvent()
        self.assertTrue(notifyhost.handle_close(window, event))
        self.assertFalse(event.isAccepted())
        window._force_quit = True
        event2 = QCloseEvent()
        self.assertFalse(notifyhost.handle_close(window, event2))


@support.requires_qt
class TestResultsReachTheNotifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def test_results_ready_calls_the_host(self):
        from cormani.ui import notifyhost
        from cormani.ui.window import MainWindow
        from PySide6.QtCore import QObject, Signal

        class FakeController(QObject):
            started = Signal()
            progressed = Signal(str)
            results_ready = Signal(object)
            finished = Signal(str, bool)

            def start(self):
                return True

            def stop(self):
                pass

        window = support.own(self, MainWindow(support.demo_store(self),
                                              demo=False))
        controller = FakeController()
        window.attach_sync(controller)
        seen = []
        with mock.patch.object(notifyhost, "on_results",
                               side_effect=lambda w, r: seen.append(r)):
            controller.results_ready.emit([AccountResult(1, "a@x", ok=True)])
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
