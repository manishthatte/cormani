# SPDX-License-Identifier: GPL-3.0-or-later
#
# The credential boundary.
#
# Nothing here writes to the real keyring — it is protected on this machine,
# and a test suite that stores secrets in a developer's login keyring is a test
# suite nobody should run twice. A fake backend is installed instead, which also
# lets the failure paths be exercised, which a real keyring cannot do on demand.
#
# © Manish Jagdish Thatte
import unittest
from unittest import mock

from cormani.secrets import store as secrets


class FakeKeyring:
    """Just enough of the keyring API, in memory."""

    def __init__(self, broken=False):
        self.data = {}
        self.broken = broken

    def set_password(self, service, key, value):
        if self.broken:
            raise RuntimeError("no backend")
        self.data[(service, key)] = value

    def get_password(self, service, key):
        if self.broken:
            raise RuntimeError("no backend")
        return self.data.get((service, key))

    def delete_password(self, service, key):
        if self.broken:
            raise RuntimeError("no backend")
        del self.data[(service, key)]

    def get_keyring(self):
        return self


class TestSecrets(unittest.TestCase):
    def _install(self, fake):
        patcher = mock.patch.object(secrets, "_backend", lambda: fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip(self):
        self._install(FakeKeyring())
        secrets.set_secret("a@example.org", "refresh_token", "tok-123")
        self.assertEqual(secrets.get_secret("a@example.org", "refresh_token"), "tok-123")
        self.assertTrue(secrets.has_secret("a@example.org", "refresh_token"))

    def test_purposes_are_separate_entries(self):
        # An OAuth token and an app password have different lifetimes;
        # revoking one must not disturb the other.
        self._install(FakeKeyring())
        secrets.set_secret("a@example.org", "refresh_token", "tok")
        secrets.set_secret("a@example.org", "app_password", "pw")
        secrets.delete_secret("a@example.org", "refresh_token")
        self.assertFalse(secrets.has_secret("a@example.org", "refresh_token"))
        self.assertEqual(secrets.get_secret("a@example.org", "app_password"), "pw")

    def test_accounts_are_separate_entries(self):
        self._install(FakeKeyring())
        secrets.set_secret("a@example.org", "app_password", "one")
        secrets.set_secret("b@example.org", "app_password", "two")
        self.assertEqual(secrets.get_secret("a@example.org", "app_password"), "one")
        self.assertEqual(secrets.get_secret("b@example.org", "app_password"), "two")

    def test_missing_is_distinct_from_unavailable(self):
        # One means "ask the user to sign in", the other means "this machine
        # cannot keep secrets". They need different messages.
        self._install(FakeKeyring())
        with self.assertRaises(secrets.SecretMissing):
            secrets.get_secret("nobody@example.org", "app_password")
        self._install(FakeKeyring(broken=True))
        with self.assertRaises(secrets.SecretUnavailable):
            secrets.get_secret("a@example.org", "app_password")

    def test_exceptions_never_carry_the_secret(self):
        # An exception is the most likely thing to reach a bug report.
        self._install(FakeKeyring(broken=True))
        for call in (lambda: secrets.set_secret("a@example.org", "app_password", "hunter2"),
                     lambda: secrets.get_secret("a@example.org", "app_password")):
            with self.assertRaises(secrets.SecretUnavailable) as ctx:
                call()
            self.assertNotIn("hunter2", str(ctx.exception))

    def test_delete_of_absent_is_not_an_error(self):
        # Removing an account must remove its secrets; some may never have
        # existed, and that is not a failure.
        self._install(FakeKeyring())
        secrets.delete_secret("nobody@example.org", "app_password")

    def test_availability_and_name_never_raise(self):
        self._install(FakeKeyring(broken=True))
        self.assertIsInstance(secrets.available(), bool)
        self.assertIsInstance(secrets.backend_name(), str)


if __name__ == "__main__":
    unittest.main()
