# SPDX-License-Identifier: GPL-3.0-or-later
#
# The sync as the interface sees it: the summary line, and the F5 key.
#
# The controller's THREAD is not exercised here — this suite never spins an
# event loop, so a QThread started in it would never deliver a signal. What is
# exercised is everything either side of the thread: what the engine's results
# become in words, and what the window does when told a sync started, made
# progress, or ended.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.imap.engine import AccountResult


class TestSummary(unittest.TestCase):
    def summarise(self, *results):
        from cormani.ui.syncing import summarise
        return summarise(list(results))

    def test_nothing_due(self):
        self.assertIn("No account is due", self.summarise())

    def test_new_mail_is_counted(self):
        self.assertEqual(
            self.summarise(AccountResult(1, "a@x", ok=True, new=3)),
            "3 new messages.")

    def test_one_message_is_singular(self):
        self.assertEqual(self.summarise(AccountResult(1, "a@x", ok=True, new=1)),
                         "1 new message.")

    def test_no_new_mail_says_so_rather_than_zero(self):
        self.assertEqual(self.summarise(AccountResult(1, "a@x", ok=True)),
                         "No new mail.")

    def test_sent_changes_are_mentioned(self):
        self.assertIn("2 changes sent",
                      self.summarise(AccountResult(1, "a@x", ok=True, sent=2)))

    def test_a_chunked_import_says_more_is_coming(self):
        self.assertIn("400 still to fetch",
                      self.summarise(AccountResult(1, "a@x", ok=True, remaining=400)))

    def test_a_failure_is_named_not_hidden(self):
        # A report mentioning only the successes hides the account that has
        # stopped receiving mail. CONVENTIONS.txt §8.
        text = self.summarise(
            AccountResult(1, "a@x", ok=True, new=2),
            AccountResult(2, "b@x", ok=False, error="[AUTHENTICATIONFAILED] no"))
        self.assertIn("2 new messages", text)
        self.assertIn("b@x failed", text)
        self.assertIn("AUTHENTICATIONFAILED", text)

    def test_several_failures_are_counted_rather_than_listed(self):
        text = self.summarise(*[AccountResult(n, f"{n}@x", ok=False, error="e")
                                for n in range(4)])
        self.assertIn("4 accounts failed", text)


@support.requires_qt
class TestWindowWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def _controller(self):
        """A controller with the real signals and no thread behind them."""
        from PySide6.QtCore import QObject, Signal

        class FakeController(QObject):
            started = Signal()
            progressed = Signal(str)
            results_ready = Signal(object)
            finished = Signal(str, bool)

            def __init__(self):
                super().__init__()
                self.starts = 0
                self.busy = False
                self.stopped = 0

            def start(self):
                if self.busy:
                    return False
                self.busy = True
                self.starts += 1
                self.started.emit()
                return True

            def stop(self):
                self.stopped += 1

        return FakeController()

    def _window(self, *, with_sync=True):
        from cormani.ui.window import MainWindow

        con = support.demo_store(self)
        window = support.own(self, MainWindow(con, demo=False))
        controller = None
        if with_sync:
            controller = self._controller()
            window.attach_sync(controller)
        return window, controller

    def test_without_a_controller_the_key_is_offered_but_inert(self):
        # Demo data has no server behind it. The menu says so rather than
        # offering a key that does nothing.
        window, _ = self._window(with_sync=False)
        self.assertFalse(window.act_sync.isEnabled())
        self.assertIn("demo", window.act_sync.statusTip())
        self.assertFalse(window.sync_now())

    def test_attaching_a_controller_enables_the_key(self):
        window, _ = self._window()
        self.assertTrue(window.act_sync.isEnabled())
        self.assertNotIn("demo", window.act_sync.statusTip())

    def test_f5_starts_a_sync_and_disables_itself(self):
        window, controller = self._window()
        self.assertTrue(window.sync_now())
        self.assertEqual(controller.starts, 1)
        self.assertFalse(window.act_sync.isEnabled(),
                         "the key must not fire twice while one is running")
        self.assertIn("Fetching", window.status_message.text())

    def test_pressing_it_again_is_a_message_not_an_error(self):
        # Two engines against one store would fight over the offline queue and
        # re-fetch each other's work.
        window, controller = self._window()
        window.sync_now()
        self.assertFalse(window.sync_now())
        self.assertEqual(controller.starts, 1)
        self.assertIn("already running", window.status_message.text())

    def test_progress_reaches_the_status_bar(self):
        window, controller = self._window()
        controller.progressed.emit("Checking admin@idlidu.example…")
        self.assertEqual(window.status_message.text(),
                         "Checking admin@idlidu.example…")

    def test_finishing_restores_the_key_and_shows_the_summary(self):
        window, controller = self._window()
        window.sync_now()
        controller.finished.emit("3 new messages.", True)
        self.assertTrue(window.act_sync.isEnabled())
        self.assertEqual(window.status_message.text(), "3 new messages.")

    def test_a_failed_sync_still_reloads_the_list(self):
        # An account that failed after three others succeeded still leaves
        # three accounts' new mail on the disk and not on the screen.
        window, controller = self._window()
        reloads = []
        original = window.mail.reload
        window.mail.reload = lambda: (reloads.append(1), original())[1]
        controller.finished.emit("one account failed", False)
        self.assertEqual(len(reloads), 1)

    def test_closing_waits_for_a_running_sync(self):
        # A thread torn down mid-write is exactly what the store is not built
        # to survive.
        window, controller = self._window()
        window.sync_now()
        window.close()
        self.assertEqual(controller.stopped, 1)


if __name__ == "__main__":
    unittest.main()
