# SPDX-License-Identifier: GPL-3.0-or-later
#
# Authorisation: the flow, the refresh, and what is kept where.
#
# No network and no real keyring. The token endpoint is a callable the caller
# supplies, which is also how the failure paths are reached — a provider cannot
# be asked to refuse on demand.
#
# © Manish Jagdish Thatte
import datetime as dt
import json
import unittest
import urllib.parse

import support

from cormani.auth import credentials, oauth, providers
from cormani.auth.oauth import OAuthError, TokenSet
from cormani.auth.providers import METHOD_OAUTH2, METHOD_PASSWORD
from cormani.secrets import store as secrets

NOW = dt.datetime(2026, 8, 25, 12, 0, 0, tzinfo=dt.timezone.utc)


class Recorder:
    """A token endpoint that records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, fields, **kwargs):
        self.calls.append((url, dict(fields)))
        reply = self.replies.pop(0) if self.replies else {}
        if isinstance(reply, Exception):
            raise reply
        return reply


class TestPkce(unittest.TestCase):
    def test_a_verifier_is_the_right_shape(self):
        verifier = oauth.make_verifier()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)

    def test_two_verifiers_differ(self):
        self.assertNotEqual(oauth.make_verifier(), oauth.make_verifier())

    def test_the_challenge_is_the_rfc_7636_one(self):
        # The worked example from RFC 7636 appendix B.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(oauth.challenge_for(verifier),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_the_challenge_carries_no_padding(self):
        self.assertNotIn("=", oauth.challenge_for(oauth.make_verifier()))


class TestAuthorizationUrl(unittest.TestCase):
    def url(self, provider, **kwargs):
        kwargs.setdefault("state", "st")
        kwargs.setdefault("verifier", "v" * 50)
        raw = oauth.authorization_url(provider, "client-id",
                                      "http://127.0.0.1:9/", **kwargs)
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(raw).query))

    def test_the_required_parameters(self):
        params = self.url(providers.GOOGLE)
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["client_id"], "client-id")
        self.assertEqual(params["redirect_uri"], "http://127.0.0.1:9/")
        self.assertIn("https://mail.google.com/", params["scope"])

    def test_google_asks_for_offline_access_and_consent(self):
        # Without both, Google returns no refresh token on the SECOND
        # authorisation of an account, and the failure is silent for an hour.
        params = self.url(providers.GOOGLE)
        self.assertEqual(params["access_type"], "offline")
        self.assertEqual(params["prompt"], "consent")

    def test_microsoft_asks_for_offline_access_by_scope(self):
        params = self.url(providers.MICROSOFT)
        self.assertIn("offline_access", params["scope"])

    def test_a_login_hint_names_the_account(self):
        # Eight Google accounts: without it the browser offers whichever is
        # signed in and the wrong one is a click away.
        params = self.url(providers.GOOGLE, login_hint="admin@idlidu.example")
        self.assertEqual(params["login_hint"], "admin@idlidu.example")

    def test_calendar_scopes_are_not_asked_for_while_setting_up_mail(self):
        self.assertNotIn("calendar", self.url(providers.GOOGLE)["scope"])
        self.assertIn("calendar", self.url(providers.GOOGLE, calendar=True)["scope"])

    def test_the_challenge_matches_the_verifier(self):
        verifier = oauth.make_verifier()
        params = self.url(providers.GOOGLE, verifier=verifier)
        self.assertEqual(params["code_challenge"], oauth.challenge_for(verifier))


class TestTokenSet(unittest.TestCase):
    def test_a_fresh_token_is_not_expired(self):
        tokens = TokenSet("a", expires_at=(NOW + dt.timedelta(hours=1)).isoformat())
        self.assertFalse(tokens.expired(now=NOW))

    def test_a_token_about_to_expire_counts_as_expired(self):
        # One that dies between the check and the command produces a failure
        # that looks like a rejected credential.
        tokens = TokenSet("a", expires_at=(NOW + dt.timedelta(seconds=30)).isoformat())
        self.assertTrue(tokens.expired(now=NOW))

    def test_a_token_of_unknown_age_counts_as_expired(self):
        self.assertTrue(TokenSet("a").expired(now=NOW))
        self.assertTrue(TokenSet("a", expires_at="nonsense").expired(now=NOW))

    def test_the_round_trip_keeps_the_expiry_with_the_token(self):
        tokens = TokenSet("a", "r", NOW.isoformat(), "scope")
        self.assertEqual(TokenSet.from_json(tokens.to_json()), tokens)

    def test_nonsense_json_is_an_empty_token_not_an_exception(self):
        self.assertEqual(TokenSet.from_json("{").access_token, "")
        self.assertEqual(TokenSet.from_json("[]").access_token, "")

    def test_the_repr_never_shows_the_token(self):
        # A repr ends up in tracebacks and bug reports.
        text = repr(TokenSet("ya29.SECRETVALUE", "1//SECRETREFRESH"))
        self.assertNotIn("SECRET", text)
        self.assertIn("chars", text)


class TestExchangeAndRefresh(unittest.TestCase):
    def test_a_code_becomes_a_token_set(self):
        post = Recorder({"access_token": "at", "refresh_token": "rt",
                         "expires_in": 3600, "scope": "s"})
        tokens = oauth.exchange_code(
            providers.GOOGLE, client_id="cid", client_secret="cs", code="the-code",
            redirect_uri="http://127.0.0.1:9/", verifier="v", post=post, now=NOW)
        self.assertEqual(tokens.access_token, "at")
        self.assertEqual(tokens.refresh_token, "rt")
        self.assertEqual(tokens.expires_at, "2026-08-25T13:00:00+00:00")
        url, fields = post.calls[0]
        self.assertEqual(url, providers.GOOGLE.token_url)
        self.assertEqual(fields["grant_type"], "authorization_code")
        self.assertEqual(fields["code_verifier"], "v")

    def test_a_refresh_keeps_the_refresh_token_when_none_comes_back(self):
        # A refresh response usually omits it, meaning "keep the one you have".
        # Dropping it is how an account silently needs signing in again.
        post = Recorder({"access_token": "new", "expires_in": 3600})
        tokens = oauth.refresh_token(providers.GOOGLE, client_id="cid",
                                     client_secret="cs", refresh="rt",
                                     post=post, now=NOW)
        self.assertEqual(tokens.access_token, "new")
        self.assertEqual(tokens.refresh_token, "rt")

    def test_a_rotated_refresh_token_replaces_the_old_one(self):
        post = Recorder({"access_token": "new", "refresh_token": "rt2",
                         "expires_in": 3600})
        tokens = oauth.refresh_token(providers.MICROSOFT, client_id="cid",
                                     client_secret="", refresh="rt", post=post)
        self.assertEqual(tokens.refresh_token, "rt2")

    def test_refreshing_without_a_refresh_token_says_so(self):
        with self.assertRaises(OAuthError) as caught:
            oauth.refresh_token(providers.GOOGLE, client_id="c",
                                client_secret="", refresh="")
        self.assertIn("sign in", str(caught.exception))

    def test_a_reply_with_no_token_is_an_error(self):
        post = Recorder({"error": "invalid_grant"})
        with self.assertRaises(OAuthError) as caught:
            oauth.refresh_token(providers.GOOGLE, client_id="c",
                                client_secret="", refresh="rt", post=post)
        self.assertIn("invalid_grant", str(caught.exception))


class TestLoopbackCatcher(unittest.TestCase):
    def test_the_server_binds_loopback_and_nothing_else(self):
        # The thing arriving on that socket is an authorisation code.
        server = oauth.listen()
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertTrue(oauth.redirect_uri_for(server)
                            .startswith("http://127.0.0.1:"))
        finally:
            server.server_close()

    def test_a_mismatched_state_is_discarded(self):
        # The check that stops a code from another page being accepted.
        server = oauth.listen()
        oauth._Catcher.result = {"code": "abc", "state": "not-ours"}
        server.handle_request = lambda: None
        with self.assertRaises(OAuthError) as caught:
            oauth.wait_for_code(server, state="ours")
        self.assertIn("did not match", str(caught.exception))

    def test_the_providers_own_refusal_is_reported(self):
        server = oauth.listen()
        oauth._Catcher.result = {"error": "access_denied", "state": "ours"}
        server.handle_request = lambda: None
        with self.assertRaises(OAuthError) as caught:
            oauth.wait_for_code(server, state="ours")
        self.assertIn("access_denied", str(caught.exception))

    def test_a_matching_state_yields_the_code(self):
        server = oauth.listen()
        oauth._Catcher.result = {"code": "abc", "state": "ours"}
        server.handle_request = lambda: None
        self.assertEqual(oauth.wait_for_code(server, state="ours"), "abc")

    def test_nothing_arriving_is_reported_rather_than_hanging_forever(self):
        server = oauth.listen()
        oauth._Catcher.result = {}
        server.handle_request = lambda: None
        with self.assertRaises(OAuthError) as caught:
            oauth.wait_for_code(server, state="ours", timeout=0.01)
        self.assertIn("timeout", str(caught.exception))


class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.keyring = support.fake_keyring(self)

    def test_an_app_password_is_returned_as_one(self):
        credentials.set_password("owner@manitlab.example", "sixteen-letters")
        got = credentials.resolve("owner@manitlab.example", "google",
                                  method=METHOD_PASSWORD)
        self.assertEqual(got.method, METHOD_PASSWORD)
        self.assertEqual(got.secret, "sixteen-letters")

    def test_microsoft_refuses_an_app_password_rather_than_offering_one(self):
        # Basic authentication is withdrawn; offering it offers something that
        # cannot work.
        credentials.set_password("user@hotmail.com", "anything")
        with self.assertRaises(credentials.NotConfigured) as caught:
            credentials.resolve("user@hotmail.com", "microsoft",
                                method=METHOD_PASSWORD)
        self.assertIn("OAuth", str(caught.exception))

    def test_a_live_token_is_used_without_a_request(self):
        post = Recorder()
        credentials.set_tokens("a@example.org", TokenSet(
            "live", "rt", (NOW + dt.timedelta(hours=1)).isoformat()))
        got = credentials.resolve("a@example.org", "google", post=post, now=NOW)
        self.assertEqual(got.secret, "live")
        self.assertEqual(got.method, METHOD_OAUTH2)
        self.assertEqual(post.calls, [], "a live token needs no round trip")

    def test_an_expired_token_is_refreshed_and_the_new_one_kept(self):
        credentials.set_registration("google", "cid", "cs")
        credentials.set_tokens("a@example.org", TokenSet(
            "stale", "rt", (NOW - dt.timedelta(hours=2)).isoformat()))
        post = Recorder({"access_token": "fresh", "expires_in": 3600})
        got = credentials.resolve("a@example.org", "google", post=post, now=NOW)
        self.assertEqual(got.secret, "fresh")
        self.assertEqual(credentials.stored_tokens("a@example.org").access_token,
                         "fresh", "the refreshed token is kept, not thrown away")
        self.assertEqual(credentials.stored_tokens("a@example.org").refresh_token,
                         "rt")

    def test_refreshing_without_a_registration_says_which_is_missing(self):
        credentials.set_tokens("a@example.org", TokenSet("stale", "rt"))
        with self.assertRaises(credentials.NotConfigured) as caught:
            credentials.resolve("a@example.org", "google", now=NOW)
        self.assertIn("Google", str(caught.exception))

    def test_an_account_with_nothing_stored_is_not_configured(self):
        with self.assertRaises(credentials.NotConfigured):
            credentials.resolve("nobody@example.org", "google")
        self.assertFalse(credentials.configured("nobody@example.org", "google"))

    def test_a_password_is_used_when_oauth_has_nothing(self):
        # An account that works should work.
        credentials.set_password("a@example.org", "pw")
        got = credentials.resolve("a@example.org", "google")
        self.assertEqual(got.method, METHOD_PASSWORD)

    def test_one_registration_serves_every_account_on_a_provider(self):
        # One Google Cloud project covers all eight; docs/accounts.txt.
        credentials.set_registration("google", "cid", "cs")
        for address in ("a@example.org", "b@example.org"):
            credentials.set_tokens(address, TokenSet(
                "stale", f"rt-{address}", (NOW - dt.timedelta(hours=2)).isoformat()))
        post = Recorder({"access_token": "one", "expires_in": 3600},
                        {"access_token": "two", "expires_in": 3600})
        credentials.resolve("a@example.org", "google", post=post, now=NOW)
        credentials.resolve("b@example.org", "google", post=post, now=NOW)
        self.assertEqual([c[1]["client_id"] for c in post.calls], ["cid", "cid"])
        self.assertEqual([c[1]["refresh_token"] for c in post.calls],
                         ["rt-a@example.org", "rt-b@example.org"])

    def test_forgetting_an_account_removes_every_secret(self):
        credentials.set_password("a@example.org", "pw")
        credentials.set_tokens("a@example.org", TokenSet("at", "rt"))
        credentials.forget("a@example.org")
        self.assertFalse(credentials.configured("a@example.org", "google"))
        self.assertFalse(secrets.has_secret("a@example.org", "app-password"))
        self.assertFalse(secrets.has_secret("a@example.org", "refresh-token"))

    def test_the_credential_repr_never_shows_the_secret(self):
        credentials.set_password("a@example.org", "SECRETPASSWORD")
        got = credentials.resolve("a@example.org", "google", method=METHOD_PASSWORD)
        self.assertNotIn("SECRET", repr(got))

    def test_signing_in_stores_what_comes_back(self):
        credentials.set_registration("google", "cid", "cs")
        opened = []
        post = Recorder({"access_token": "at", "refresh_token": "rt",
                         "expires_in": 3600})

        def open_browser(url):
            opened.append(url)
            # Standing in for the browser's redirect back to the loopback port.
            params = dict(urllib.parse.parse_qsl(
                urllib.parse.urlparse(url).query))
            oauth._Catcher.result = {"code": "the-code",
                                     "state": params["state"]}

        original = oauth.listen

        def listen_without_serving(port=0):
            server = original(port)
            server.handle_request = lambda: None
            return server

        oauth.listen = listen_without_serving
        self.addCleanup(setattr, oauth, "listen", original)

        tokens = credentials.sign_in("a@example.org", "google",
                                     open_browser=open_browser, post=post)
        self.assertEqual(tokens.refresh_token, "rt")
        self.assertEqual(credentials.stored_tokens("a@example.org").access_token,
                         "at")
        self.assertIn("login_hint=a%40example.org", opened[0])
        self.assertEqual(post.calls[0][1]["redirect_uri"][:17],
                         "http://127.0.0.1:")

    def test_a_sign_in_that_returns_no_refresh_token_is_refused(self):
        # Storing it means the account works for an hour and then asks again,
        # with the cause weeks in the past.
        credentials.set_registration("google", "cid", "cs")
        post = Recorder({"access_token": "at", "expires_in": 3600})
        original = oauth.listen

        def listen_without_serving(port=0):
            server = original(port)
            server.handle_request = lambda: None
            return server

        oauth.listen = listen_without_serving
        self.addCleanup(setattr, oauth, "listen", original)

        def open_browser(url):
            params = dict(urllib.parse.parse_qsl(
                urllib.parse.urlparse(url).query))
            oauth._Catcher.result = {"code": "c", "state": params["state"]}

        with self.assertRaises(credentials.NotConfigured) as caught:
            credentials.sign_in("a@example.org", "google",
                                open_browser=open_browser, post=post)
        self.assertIn("Revoke", str(caught.exception))
        self.assertFalse(credentials.configured("a@example.org", "google"))

    def test_signing_in_without_a_registration_explains_what_is_needed(self):
        with self.assertRaises(credentials.NotConfigured) as caught:
            credentials.sign_in("a@example.org", "google",
                                open_browser=lambda url: None)
        self.assertIn("Google Cloud project", str(caught.exception))

    def test_nothing_is_stored_outside_the_keyring(self):
        credentials.set_registration("google", "cid", "cs")
        credentials.set_password("a@example.org", "pw")
        credentials.set_tokens("a@example.org", TokenSet("at", "rt"))
        blob = json.dumps({str(k): v for k, v in self.keyring.data.items()})
        for value in ("cid", "cs", "pw", "at", "rt"):
            self.assertIn(value, blob)
        self.assertEqual(len(self.keyring.data), 5)


if __name__ == "__main__":
    unittest.main()
