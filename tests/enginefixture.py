# SPDX-License-Identifier: GPL-3.0-or-later
#
# An engine, wired to IMAP servers that run in this process.
#
# Shared by `test_engine.py` — the schedule, and what happens when an account
# refuses — and `test_arrivals.py`, which is what a sync DOES with the mail
# once it has it. It is here rather than in either of them because a fixture
# imported from a test module is a test module that runs twice.
#
# © Manish Jagdish Thatte
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import support
from fakeimap import IMAP4_Fake, Server

from cormani.auth.credentials import Credential
from cormani.auth.providers import METHOD_PASSWORD
from cormani.imap.client import Connection
from cormani.imap.engine import Engine, Options
from cormani.store.accounts import add_account, list_accounts

NOW = dt.datetime(2026, 8, 25, 12, 0, 0, tzinfo=dt.timezone.utc)


def raw(subject="Quarterly figures"):
    return (f"From: priya@example.org\r\nTo: owner@manitlab.example\r\n"
            f"Subject: {subject}\r\nDate: Tue, 25 Aug 2026 10:00:00 +0000\r\n\r\n"
            f"body\r\n").encode()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.servers = {}
        self.accounts = {}
        for address in ("owner@manitlab.example", "admin@idlidu.example"):
            self.accounts[address] = add_account(
                self.con, address, "google", imap_host="imap.gmail.com")
            server = Server()
            server.passwords[address] = "pw"
            server.add_mailbox("INBOX")
            server.add_mailbox("Archive", attributes=("\\Archive",))
            self.servers[address] = server
        self.engine = self.make_engine()

    def make_engine(self, **kwargs):
        kwargs.setdefault("options", Options(
            attachments_root=Path(self._tmp.name), initial_days=None))
        kwargs.setdefault("connect", self.connect)
        kwargs.setdefault("resolve", self.resolve)
        kwargs.setdefault("clock", lambda: NOW)
        return Engine(self.con, **kwargs)

    def connect(self, account):
        server = self.servers[account.address]
        return Connection.connect("x", address=account.address,
                                  factory=lambda: IMAP4_Fake(server))

    def resolve(self, address, provider, **kwargs):
        return Credential(method=METHOD_PASSWORD, user=address, secret="pw")

    def account(self, address):
        return [a for a in list_accounts(self.con)
                if a.address == address][0]

    def state(self, address):
        return self.con.execute(
            "SELECT last_sync_at, last_error, sync_failures, next_attempt_at "
            "FROM account WHERE id = ?",
            (self.accounts[address],)).fetchone()


