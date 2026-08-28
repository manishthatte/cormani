# SPDX-License-Identifier: GPL-3.0-or-later
#
# The user agent. Guards the finding recorded in docs/toolkit-verification.txt:
# Qt keeps sending client hints naming the real Chromium version, so the agent
# must claim that version and no other.
#
# © Manish Jagdish Thatte
import unittest

from cormani.platform.runtime import chrome_version, derive_user_agent, engine_report

QT_DEFAULT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "QtWebEngine/6.8.2 Chrome/122.0.0.0 Safari/537.36")


class TestUserAgent(unittest.TestCase):
    def test_qt_token_is_removed(self):
        ua = derive_user_agent(QT_DEFAULT)
        self.assertNotIn("QtWebEngine", ua)

    def test_version_is_preserved_not_inflated(self):
        # The whole point. Claiming a newer Chrome than the engine is, while
        # sec-ch-ua keeps saying the real one, is a contradiction no real
        # browser emits — a stronger fingerprint than an unusual agent.
        ua = derive_user_agent(QT_DEFAULT)
        self.assertIn("Chrome/122.0.0.0", ua)
        self.assertNotIn("151", ua)

    def test_result_looks_like_an_ordinary_chrome_agent(self):
        ua = derive_user_agent(QT_DEFAULT)
        for token in ("Mozilla/5.0", "AppleWebKit/537.36", "KHTML, like Gecko",
                      "Chrome/", "Safari/537.36"):
            self.assertIn(token, ua)
        self.assertNotIn("  ", ua)

    def test_platform_token_survives(self):
        self.assertIn("X11; Linux x86_64", derive_user_agent(QT_DEFAULT))

    def test_tracks_a_future_engine(self):
        # When Debian ships a newer Qt the embedded Chromium moves with it, and
        # a hardcoded version would become the mismatch this exists to avoid.
        future = QT_DEFAULT.replace("122.0.0.0", "140.0.7100.5").replace("6.8.2", "6.11.0")
        self.assertIn("Chrome/140.0.7100.5", derive_user_agent(future))

    def test_empty_agent_falls_back_without_raising(self):
        ua = derive_user_agent("")
        self.assertIn("Chrome/", ua)
        self.assertNotIn("QtWebEngine", ua)

    def test_chrome_version_extraction(self):
        self.assertEqual(chrome_version(QT_DEFAULT), "122.0.0.0")
        self.assertIsNone(chrome_version("Mozilla/5.0 (nothing useful)"))

    def test_report_shape(self):
        r = engine_report(QT_DEFAULT)
        self.assertEqual(r["chrome_major"], 122)
        self.assertTrue(r["is_qt_default"])
        self.assertNotIn("QtWebEngine", r["user_agent"])


if __name__ == "__main__":
    unittest.main()
