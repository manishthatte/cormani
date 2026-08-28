# SPDX-License-Identifier: GPL-3.0-or-later
#
# The site panels: the registry, the profiles, and what a panel REFUSES.
#
# MOST OF THIS FILE IS ABOUT REFUSALS, and that is the right shape for it. A
# panel is the only place in corMani where a stranger's JavaScript runs, on a
# profile holding a signed-in session — so what matters is not that it can show
# a page, which is Chromium's job and not corMani's, but that it declines the
# camera, cancels a download, rejects a certificate error and hands a popup
# out rather than swallowing it.
#
# NOTHING HERE TOUCHES THE NETWORK. No panel is ever told to load; the tests
# call the handlers a page would have triggered, with objects that record what
# was done to them. That is the only honest way to test a refusal — a test that
# waited for WhatsApp to ask for a microphone would be a test that never runs.
#
# THE ISOLATION TEST IS THE ONE THAT MATTERS MOST. CONVENTIONS.txt §7 requires
# that a panel cannot reach the store, and the way that is guaranteed is that
# there is no path from the widget to a connection. So it is asserted as an
# absence, which is unusual and is deliberate: an attribute that appeared later
# would be a bridge nobody meant to build.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import unittest
import unittest.mock

import support

from cormani.config.settings import Settings
from cormani.panels import sites as sites_mod
from cormani.panels import unread as unread_mod

support.qt_app() if support.HAVE_QT else None


class TestTheRegistry(unittest.TestCase):
    """A site is a row in a table; adding one is a line. CONVENTIONS.txt §4."""

    def test_every_site_has_its_own_profile_name(self):
        """The storage name IS the isolation — one name per site is one cookie
        jar per site. Two sites sharing a name would share a session, silently
        and completely."""
        names = [site.profile_name for site in sites_mod.SITES]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len({s.key for s in sites_mod.SITES}), len(names))

    def test_the_four_messaging_sites_are_on_and_the_webmail_ones_are_not(self):
        # corMani already holds Gmail and Outlook mail over IMAP. A panel for
        # them is for what only the web can do, and is not the default.
        self.assertEqual(sites_mod.default_keys(),
                         ["whatsapp", "linkedin", "x", "facebook"])

    def test_a_rail_key_goes_there_and_back(self):
        for site in sites_mod.SITES:
            key = sites_mod.rail_key(site)
            self.assertEqual(sites_mod.from_rail_key(key), site)
        self.assertIsNone(sites_mod.from_rail_key("calendar:all"))
        self.assertIsNone(sites_mod.from_rail_key("site:nosuchthing"))

    def test_every_site_says_how_to_sign_into_it(self):
        """"Not signed in" alone leaves a person looking for a button that is
        on the page in front of them — WhatsApp Web's is a QR code on a
        telephone, which is not guessable from a mail client."""
        for site in sites_mod.SITES:
            self.assertTrue(site.hint.strip(), site.key)

    def test_every_site_is_https(self):
        for site in sites_mod.SITES:
            self.assertTrue(site.url.startswith("https://"), site.url)


class TestWhatTheyAreOptional(unittest.TestCase):
    """Panels must be able to be off entirely.

    Not a preference: docs/toolkit-verification.txt finding 2 says the embedded
    Chromium is pinned by Debian and these sites will eventually refuse it. On
    that day mail and calendar must be unaffected, which means a person must be
    able to turn the panels off.
    """

    def test_nothing_configured_means_the_registry_defaults(self):
        self.assertEqual(Settings().site_keys(), sites_mod.default_keys())

    def test_none_means_none_and_is_different_from_saying_nothing(self):
        # An empty list can only carry one of the two answers, so "none" is a
        # sentinel rather than a second boolean.
        self.assertEqual(Settings(sites=["none"]).site_keys(), [])
        self.assertNotEqual(Settings(sites=[]).site_keys(), [])

    def test_a_site_this_build_does_not_know_is_dropped_not_fatal(self):
        # The common cause is a file written for a newer version, which
        # `config/settings.load` already refuses to treat as an error.
        self.assertEqual(Settings(sites=["whatsapp", "nosuch"]).site_keys(),
                         ["whatsapp"])


class TestTheUnreadJudgement(unittest.TestCase):
    """What a probe returns, and what may be believed.

    Tested apart from any browser, because the JavaScript is four lines and
    this is the part with opinions in it.
    """

    def test_a_plain_count_is_taken(self):
        self.assertEqual(unread_mod.believable(0), 0)
        self.assertEqual(unread_mod.believable(7), 7)
        self.assertEqual(unread_mod.believable(7.0), 7)

    def test_true_is_not_one_unread_message(self):
        # `True` is an int in Python, and a probe that returned a boolean would
        # otherwise put a badge saying 1 on every site that has none.
        self.assertIsNone(unread_mod.believable(True))
        self.assertIsNone(unread_mod.believable(False))

    def test_nothing_and_nonsense_are_not_counts(self):
        for value in (None, "3", [3], {}, float("nan")):
            self.assertIsNone(unread_mod.believable(value), value)

    def test_an_absurd_number_is_refused(self):
        """A title yielding a five-digit count is a title that has come to mean
        something else — a duration, a price, a year — and a badge reading
        12,438 is worse than no badge."""
        self.assertIsNone(unread_mod.believable(12438))
        self.assertIsNone(unread_mod.believable(-1))
        self.assertEqual(unread_mod.believable(unread_mod.SANE_MAXIMUM),
                         unread_mod.SANE_MAXIMUM)


class FakePage:
    """A page that answers `runJavaScript` with whatever it was told to."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def runJavaScript(self, script, world, callback):     # noqa: N802
        self.asked.append(script)
        callback(self.answer() if callable(self.answer) else self.answer)


class TestTheCounter(unittest.TestCase):
    def counter(self, answer):
        seen = []
        page = FakePage(answer)
        counter = unread_mod.Counter(sites_mod.get("whatsapp"), page,
                                     lambda key, n: seen.append((key, n)))
        return counter, page, seen

    def test_a_good_answer_is_reported_once(self):
        counter, _page, seen = self.counter(4)
        counter.poll()
        counter.poll()
        self.assertEqual(counter.count, 4)
        self.assertEqual(seen, [("whatsapp", 4)], "reported on change only")

    def test_a_probe_that_stops_matching_clears_the_count(self):
        """A stale badge is a promise the panel cannot keep. None means NOT
        KNOWN, which the rail draws as no badge — not as a confident nought."""
        answers = iter([3, None, None])
        counter, _page, seen = self.counter(lambda: next(answers))
        counter.poll()
        self.assertEqual(counter.count, 3)
        counter.poll()
        self.assertIsNone(counter.count)
        self.assertEqual(seen[-1], ("whatsapp", None))

    def test_zero_is_an_answer_and_not_an_absence(self):
        counter, _page, seen = self.counter(0)
        counter.poll()
        self.assertEqual(counter.count, 0)
        self.assertEqual(seen, [("whatsapp", 0)])

    def test_a_page_that_has_gone_stops_the_timer_rather_than_raising(self):
        class Gone:
            def runJavaScript(self, *a):                  # noqa: N802
                raise RuntimeError("wrapped C/C++ object has been deleted")

        counter = unread_mod.Counter(sites_mod.get("x"), Gone(), None)
        counter.poll()          # must not raise
        self.assertIsNone(counter.count)

    def test_the_probe_reads_the_title_and_nothing_else(self):
        """PLAN.txt §7: no DOM automation beyond unread counts. The whole of
        what corMani executes in somebody else's page is in the registry, and
        it touches `document.title`."""
        for site in sites_mod.SITES:
            self.assertIn("document.title", site.unread_js)
            for forbidden in ("querySelector", "click(", "innerHTML",
                              "fetch(", "XMLHttpRequest", "localStorage"):
                self.assertNotIn(forbidden, site.unread_js,
                                 f"{site.key} probe does more than read")


class Recorder:
    """The objects a page hands to a panel, recording what was done to them."""

    class Download:
        def __init__(self, url="https://example.invalid/f.pdf"):
            self._url, self.cancelled = url, False

        def url(self):
            from PySide6.QtCore import QUrl
            return QUrl(self._url)

        def cancel(self):
            self.cancelled = True

    class Permission:
        def __init__(self, name="MediaAudioCapture"):
            self._name, self.denied, self.granted = name, False, False

        def permissionType(self):                         # noqa: N802
            return type("T", (), {"name": self._name})()

        def deny(self):
            self.denied = True

        def grant(self):
            self.granted = True

    class Certificate:
        def __init__(self):
            self.rejected = False

        def description(self):
            return "self-signed certificate"

        def rejectCertificate(self):                      # noqa: N802
            self.rejected = True

    class NewWindow:
        def __init__(self, url="https://accounts.example.invalid/oauth"):
            self._url = url

        def requestedUrl(self):                           # noqa: N802
            from PySide6.QtCore import QUrl
            return QUrl(self._url)


@support.requires_webengine
class TestThePanelRefuses(unittest.TestCase):
    """A panel holds a signed-in session. These are its edges."""

    def setUp(self):
        from cormani.ui.sitepanel import SitePanel

        self.panel = support.own(
            self, SitePanel(sites_mod.get("whatsapp"),
                            user_agent="Mozilla/5.0 Chrome/122.0.0.0"))
        self.addCleanup(self.panel.shutdown)

    def test_a_panel_holds_no_database_connection(self):
        """Asserted as an ABSENCE, which is unusual and is the point: the
        guarantee that a panel cannot reach the store is that there is no path
        from here to it. An attribute appearing later would be a bridge nobody
        meant to build."""
        self.assertFalse(hasattr(self.panel, "_con"))
        self.assertFalse(hasattr(self.panel, "con"))
        held = [v for v in vars(self.panel).values()
                if type(v).__module__ == "sqlite3"]
        self.assertEqual(held, [])

    def test_the_page_is_on_the_sites_own_profile(self):
        self.assertEqual(self.panel.view.page().profile().storageName(),
                         "site-whatsapp")
        self.assertFalse(self.panel.view.page().profile().isOffTheRecord())

    def test_two_sites_do_not_share_a_cookie_jar(self):
        from cormani.panels import profiles as profiles_mod
        from cormani.ui.sitepanel import SitePanel

        other = support.own(self, SitePanel(sites_mod.get("linkedin")))
        self.addCleanup(other.shutdown)
        paths = profiles_mod.storage_paths()
        self.assertNotEqual(paths["whatsapp"], paths["linkedin"])

    def test_a_permission_request_is_denied(self):
        asked = Recorder.Permission()
        self.panel._permission(asked)
        self.assertTrue(asked.denied)
        self.assertFalse(asked.granted)

    def test_a_download_is_cancelled_and_handed_to_the_desktop(self):
        seen = []
        self.panel.open_externally.connect(seen.append)
        item = Recorder.Download()
        self.panel._download(item)
        self.assertTrue(item.cancelled)
        self.assertEqual(seen, ["https://example.invalid/f.pdf"])

    def test_a_certificate_error_is_rejected_and_never_offered(self):
        """"Continue anyway" on a panel holding somebody's WhatsApp is the one
        click that turns a network somebody else controls into that session."""
        error = Recorder.Certificate()
        self.panel._certificate_error(error)
        self.assertTrue(error.rejected)

    def test_a_popup_goes_to_the_browser_rather_than_being_swallowed(self):
        # The sign-in flows need one, and a panel that ate them is a panel
        # nobody can sign into.
        seen = []
        self.panel.open_externally.connect(seen.append)
        self.panel._new_window(Recorder.NewWindow())
        self.assertEqual(seen, ["https://accounts.example.invalid/oauth"])

    def test_a_failed_load_says_which_of_the_three_things_it_might_be(self):
        # A blank white panel answers nothing. And the engine's age must read
        # as a known cost rather than as a fault.
        self.panel._load_finished(False)
        text = self.panel.status.text()
        self.assertIn("network", text)
        self.assertIn("Chromium", text)
        self.assertIn("unaffected", text)

    def test_the_user_agent_carries_no_QtWebEngine_token(self):
        """finding 1: WhatsApp Web refuses an unusual user agent, and
        "QtWebEngine/6.8.2" is one."""
        agent = self.panel.view.page().profile().httpUserAgent()
        self.assertNotIn("QtWebEngine", agent)
        self.assertIn("Chrome/", agent)


@support.requires_webengine
class TestTheProfileSettings(unittest.TestCase):
    def test_the_dangerous_attributes_are_off(self):
        from PySide6.QtWebEngineCore import QWebEngineSettings

        from cormani.panels import profiles as profiles_mod

        profile = profiles_mod.for_site(sites_mod.get("facebook"))
        settings = profile.settings()
        attribute = QWebEngineSettings.WebAttribute
        for name in ("ScreenCaptureEnabled", "HyperlinkAuditingEnabled",
                     "ReadingFromCanvasEnabled", "AllowRunningInsecureContent",
                     "LocalContentCanAccessFileUrls",
                     "LocalContentCanAccessRemoteUrls",
                     "JavascriptCanAccessClipboard"):
            self.assertFalse(settings.testAttribute(getattr(attribute, name)),
                             name)
        self.assertTrue(settings.testAttribute(
            attribute.WebRTCPublicInterfacesOnly))
        # And the one that stays ON, because there is no site here without it.
        # The honest cost of the feature, which stage 9 audits.
        self.assertTrue(settings.testAttribute(attribute.JavascriptEnabled))

    def test_a_sites_profile_is_made_once_and_then_kept(self):
        """A second profile for one site would be a second cookie jar, and a
        sign-in the first panel could not see. And releasing one while a page
        holds it is finding 3's "Expect troubles!"."""
        from cormani.panels import profiles as profiles_mod

        site = sites_mod.get("outlook")
        first = profiles_mod.for_site(site)
        self.assertIs(profiles_mod.for_site(site), first)

    def test_cookies_are_persistent_so_a_panel_stays_signed_in(self):
        from PySide6.QtWebEngineCore import QWebEngineProfile

        from cormani.panels import profiles as profiles_mod

        profile = profiles_mod.for_site(sites_mod.get("gmail"))
        self.assertEqual(
            profile.persistentCookiesPolicy(),
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)


class TestWhereASessionLives(unittest.TestCase):
    """The location is corMani's decision, and it is one location.

    A named QWebEngineProfile derives its directory from QStandardPaths, which
    reads the organisation name — so before this was set explicitly, a panel's
    cookies went to ~/.local/share/Manish Jagdish Thatte/cormani/QtWebEngine/
    rather than to `paths.web_profiles`, which had declared the right answer
    since stage 0 and was created empty at every start-up. Nothing noticed
    because no panel has ever loaded a page.
    """

    def setUp(self):
        from cormani.panels import profiles as profiles_mod

        self.profiles = profiles_mod
        self.saved = dict(profiles_mod._ROOTS)
        self.addCleanup(self._restore)

    def _restore(self):
        self.profiles._ROOTS.clear()
        self.profiles._ROOTS.update(self.saved)

    def test_a_session_lives_under_the_declared_profile_directory(self):
        import tempfile

        from cormani.platform.paths import Paths

        with tempfile.TemporaryDirectory() as root:
            paths = Paths(root=root)
            self.profiles.use_storage(paths.web_profiles, paths.web_cache)
            site = sites_mod.get("whatsapp")
            self.assertEqual(self.profiles.storage_for(site),
                             paths.web_profiles / "site-whatsapp")
            self.assertEqual(self.profiles.cache_for(site),
                             paths.web_cache / "site-whatsapp")

    def test_the_default_is_cormanis_own_tree_and_not_qts_derived_one(self):
        """A caller that forgets `use_storage` must still land in corMani's
        directory. The failure this guards is a SILENT relocation, so the
        fallback has to be the right answer rather than a null."""
        from cormani.platform.paths import Paths

        self.profiles._ROOTS.clear()
        where = self.profiles.storage_for(sites_mod.get("whatsapp"))
        self.assertEqual(where, Paths().web_profiles / "site-whatsapp")
        self.assertNotIn("QtWebEngine", where.parts)

    def test_cookies_and_cache_are_kept_apart(self):
        """`web_profiles` is DATA and losing it signs every site out;
        `web_cache` is CACHE and a purge may delete it. The same distinction
        `attachments` and `attachment_cache` are held to."""
        site = sites_mod.get("whatsapp")
        self.assertNotEqual(self.profiles.storage_for(site),
                            self.profiles.cache_for(site))


@support.requires_webengine
class TestTheTwoAnswersAgree(unittest.TestCase):
    """`storage_for` answers with no QApplication, for `--check`;
    `storage_paths` asks the live profile. They must not drift apart."""

    def test_a_live_profile_is_where_storage_for_said_it_would_be(self):
        from pathlib import Path

        from cormani.panels import profiles as profiles_mod

        site = sites_mod.get("facebook")
        profile = profiles_mod.for_site(site)
        self.assertEqual(Path(profile.persistentStoragePath()),
                         profiles_mod.storage_for(site))
        self.assertEqual(Path(profile.cachePath()),
                         profiles_mod.cache_for(site))

    def test_the_suite_writes_no_session_outside_its_temporary_directory(self):
        """support.py's third obligation, held to mechanically. Until the
        storage root was set here, this suite created a directory per site
        under ~/.local/share/cormani-test/ — outside the temporary one."""
        import tempfile
        from pathlib import Path

        from cormani.panels import profiles as profiles_mod

        where = Path(profiles_mod.for_site(sites_mod.get("x"))
                     .persistentStoragePath()).resolve()
        self.assertTrue(
            str(where).startswith(str(Path(tempfile.gettempdir()).resolve())),
            f"a panel session was written to {where}")


@support.requires_webengine
class TestSigningOut(unittest.TestCase):
    """`panels/profiles.forget` shipped at stage 7 with NO CALLER.

    The same gap `contacts.note_bounce` sat in from stage 4 until stage 6, and
    the reason it matters is not tidiness: a person who signed into WhatsApp on
    this machine had no way, from inside the application, to say that the
    session should be gone.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def _window(self, keys=("whatsapp", "linkedin")):
        from cormani.ui.window import MainWindow

        con = support.demo_store(self)
        window = support.own(self, MainWindow(con, demo=True))
        window.attach_sites(list(keys))
        return window

    def test_the_menu_offers_exactly_the_sites_that_are_on(self):
        window = self._window(("whatsapp", "x"))
        labels = [a.text() for a in window.sites_menu.actions() if a.text()]
        self.assertIn("WhatsApp", labels)
        self.assertIn("X", labels)
        self.assertNotIn("LinkedIn", labels)

    def test_with_no_panels_the_menu_is_not_shown_at_all(self):
        """The panels can be off entirely — finding 2 makes that a
        requirement — and a menu offering WhatsApp on a build with them off
        would be a menu that lies."""
        window = self._window(())
        self.assertFalse(window.sites_menu.menuAction().isVisible())
        self.assertEqual(window.sites_menu.actions(), [])

    def test_signing_out_asks_first_and_a_refusal_changes_nothing(self):
        """It cannot be undone and is not cheap to reverse: signing back into
        WhatsApp Web means fetching the telephone and scanning a code."""
        from cormani.panels import profiles as profiles_mod

        window = self._window()
        called = []
        with unittest.mock.patch.object(profiles_mod, "forget",
                                        side_effect=lambda k: called.append(k)):
            done = window.sign_out_of_site("whatsapp", confirm=lambda site: False)
        self.assertFalse(done)
        self.assertEqual(called, [], "a refusal must not reach the profile")

    def test_saying_yes_throws_the_session_away(self):
        from cormani.panels import profiles as profiles_mod

        window = self._window()
        called = []
        with unittest.mock.patch.object(profiles_mod, "forget",
                                        side_effect=lambda k: called.append(k) or True):
            done = window.sign_out_of_site("whatsapp", confirm=lambda site: True)
        self.assertTrue(done)
        self.assertEqual(called, ["whatsapp"])
        self.assertIn("Signed out", window.status_message.text())

    def test_a_site_this_build_does_not_know_is_refused_not_fatal(self):
        window = self._window()
        self.assertFalse(
            window.sign_out_of_site("myspace", confirm=lambda site: True))

    def test_the_profile_object_survives_so_a_live_page_is_not_orphaned(self):
        """Finding 3: releasing a profile a page still references is the
        "Expect troubles!" case. Signing out clears the DATA and keeps the
        object."""
        from cormani.panels import profiles as profiles_mod

        site = sites_mod.get("linkedin")
        profile = profiles_mod.for_site(site)
        profiles_mod.forget(site.key)
        self.assertIs(profiles_mod.for_site(site), profile)

    def test_forgetting_a_site_never_opened_says_there_was_nothing(self):
        from cormani.panels import profiles as profiles_mod

        self.assertFalse(profiles_mod.forget("no-such-site"))


if __name__ == "__main__":
    unittest.main()
