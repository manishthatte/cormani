# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a sync does with the mail once it has it.
#
# Two passes run inside `imap/engine.sync_account`, and both are here because
# both are the same kind of claim: a step that only ONE of the two ways of
# driving corMani performs is a step that works differently depending on how it
# was started. Filing onto tracked threads is stage 6's; the filter rules are
# stage 8's.
#
# THE ASSERTIONS ARE ABOUT THE STORE AND THE SERVER, never about a return
# value. A filter that files a message locally and queues nothing leaves it in
# the Inbox on the server, and the next sync ingests it and files it again, for
# ever — so each of these looks at where the row ended up AND at what the queue
# was told.
#
# © Manish Jagdish Thatte
from enginefixture import Fixture, raw


class TestFilingHappensOnSync(Fixture):
    """The tracking layer's matchers run inside the engine and not in a caller.

    There are two callers — the terminal and the window — and a step either of
    them could forget is a step that works differently depending on how corMani
    was started. `store/attach.py` is idempotent, so the engine runs it after
    every sync that brought anything in.
    """

    def thread_for(self, address):
        from cormani.store import contacts, tracking

        thread = tracking.create_thread(self.con, "A tracked matter")
        contact = contacts.contact_for_address(self.con, address,
                                               name="Priya", create=True)
        tracking.link_contact(self.con, thread, contact.id)
        return thread

    def test_new_mail_is_filed_onto_a_thread_the_sender_is_on(self):
        from cormani.store import touches

        thread = self.thread_for("priya@example.org")
        touches.log_call(self.con, thread, summary="Rang first",
                         occurred_at="2026-08-01T09:00:00+00:00")
        self.servers["owner@manitlab.example"].add_message("INBOX", raw())
        self.engine.sync_all()
        filed = [t for t in touches.timeline(self.con, thread)
                 if t.channel == "email"]
        self.assertEqual([t.subject for t in filed], ["Quarterly figures"])

    def test_a_sync_that_brought_nothing_files_nothing_and_says_so(self):
        # `rebuild_wrote_to` is a pass over every Sent folder. Running it to
        # discover nothing changed is what makes a quiet sync feel slow.
        self.thread_for("priya@example.org")
        seen = []
        self.engine.sync_all(progress=lambda name, detail: seen.append(name))
        self.assertNotIn("filed", seen)

    def test_the_progress_callback_reports_what_was_filed(self):
        self.thread_for("priya@example.org")
        from cormani.store import touches

        touches.log_call(self.con, self.thread_for("priya@example.org"),
                         summary="Rang", occurred_at="2026-08-01T09:00:00+00:00")
        self.servers["owner@manitlab.example"].add_message("INBOX", raw())
        seen = {}
        self.engine.sync_all(
            progress=lambda name, detail: seen.setdefault(name, detail))
        self.assertIn("filed", seen)
        self.assertGreater(seen["filed"]["filed"].total, 0)


class TestFiltersOnArrival(Fixture):
    """The rules, run by the sync that fetched the mail.

    THE ASSERTIONS ARE ABOUT THE STORE AND THE SERVER, not about a return
    value: a filter that files a message locally and queues nothing leaves it
    in the Inbox on the server, and the next sync ingests it and files it
    again, for ever. So each of these looks at where the row ended up AND at
    what the queue was told.
    """

    def rule(self, **kw):
        from cormani.store import folders as folders_repo
        from cormani.store import rulematch, rules
        conditions = kw.pop("conditions",
                            (rulematch.Condition("subject", "contains", "invoice"),))
        actions = kw.pop("actions",
                         (rulematch.Action(kind="move", value=folders_repo.ROLE_ARCHIVE),))
        return rules.save_rule(self.con, rulematch.Rule(
            name=kw.pop("name", "invoices"), conditions=conditions,
            actions=actions, **kw))

    def folder_path(self, message_id):
        return self.con.execute(
            "SELECT f.path FROM message m JOIN folder f ON f.id = m.folder_id "
            "WHERE m.id = ?", (message_id,)).fetchone()[0]

    def test_a_rule_files_mail_the_sync_has_just_fetched(self):
        self.rule()
        address = "owner@manitlab.example"
        self.servers[address].add_message("INBOX", raw("Invoice 41 attached"))
        self.servers[address].add_message("INBOX", raw("Lunch?"))
        self.engine.sync_all()
        filed = self.con.execute(
            "SELECT id FROM message WHERE subject LIKE 'Invoice%'").fetchone()[0]
        kept = self.con.execute(
            "SELECT id FROM message WHERE subject = 'Lunch?'").fetchone()[0]
        self.assertEqual(self.folder_path(filed), "Archive")
        self.assertEqual(self.folder_path(kept), "INBOX")

    def test_the_move_a_rule_made_is_queued_for_the_server(self):
        from cormani.store import pending
        self.rule()
        address = "owner@manitlab.example"
        self.servers[address].add_message("INBOX", raw("Invoice 41 attached"))
        self.engine.sync_all()
        message_id = self.con.execute("SELECT id FROM message").fetchone()[0]
        self.assertIsNotNone(pending.queued_move(self.con, message_id))

    def test_a_re_fetch_does_not_run_the_rules_a_second_time(self):
        """A pass that RE-WRITES a row must not re-filter it.

        `store_message` is idempotent on (folder, uid): an interrupted sync
        re-fetches what it already holds, and `--resync` discards and re-fetches
        on purpose. If the filter ran on everything a pass WROTE rather than on
        what it CREATED, every one of those passes would re-apply every rule —
        marking read what the user had marked unread, and filing again what
        they had taken out of the folder a rule put it in.

        THE RULE HERE MARKS READ RATHER THAN MOVING, deliberately. A move takes
        the UID off the row and the queue writes the server's new one back when
        it drains, so a moving rule would make this test assert the queue's
        behaviour at the same time as the filter's, and a failure would not say
        which. A flag stays put and the row keeps its identity.

        And the re-fetch is PROVED rather than assumed: the pass reports one
        message written and no ids created, which is the exact distinction the
        `new_ids` field exists to make. Without that assertion this test would
        still pass over a sync that fetched nothing at all.
        """
        from cormani.store import edits, folders as folders_repo
        from cormani.store import rulematch
        self.rule(actions=(rulematch.Action(kind="mark_read"),))
        address = "owner@manitlab.example"
        self.servers[address].add_message("INBOX", raw("Invoice 41 attached"))
        self.engine.sync_all()
        message_id = self.con.execute("SELECT id FROM message").fetchone()[0]
        self.assertTrue(self.con.execute(
            "SELECT seen FROM message WHERE id = ?", (message_id,)).fetchone()[0])

        # The user disagrees with the rule, and then the folder is fetched
        # again from the beginning — which is what an interrupted pass and
        # `--resync` both leave behind.
        edits.set_seen(self.con, [message_id], False)
        inbox = self.con.execute(
            "SELECT id FROM folder WHERE path = 'INBOX' AND account_id = ?",
            (self.accounts[address],)).fetchone()[0]
        folders_repo.record_sync_state(self.con, inbox, uid_next=1)

        seen = {}
        result = self.engine.sync_account(
            self.account(address),
            progress=lambda name, detail: seen.setdefault(name, detail))
        self.assertFalse(self.con.execute(          # the user's answer stood
            "SELECT seen FROM message WHERE id = ?", (message_id,)).fetchone()[0])
        self.assertIsNone(result.filtered)
        report = seen["folder:done"]["report"]
        self.assertEqual(report.new, 1)          # it really was fetched again
        self.assertEqual(report.new_ids, [])     # and it really was not new

    def test_a_rule_does_not_run_over_the_sent_folder(self):
        # A filter is about incoming mail. Fetching Sent must not put the
        # user's own outgoing mail through rules written for arrivals.
        address = "owner@manitlab.example"
        self.servers[address].add_mailbox("Sent", attributes=("\\Sent",))
        self.servers[address].add_message("Sent", raw("Invoice 41 attached"))
        self.rule()
        self.engine.sync_all()
        message_id = self.con.execute("SELECT id FROM message").fetchone()[0]
        self.assertEqual(self.folder_path(message_id), "Sent")

    def test_the_result_carries_what_the_filters_did(self):
        self.rule()
        address = "owner@manitlab.example"
        self.servers[address].add_message("INBOX", raw("Invoice 41 attached"))
        results = {r.address: r for r in self.engine.sync_all()}
        report = results[address].filtered
        self.assertEqual(report.matched, 1)
        self.assertEqual(results["admin@idlidu.example"].filtered, None)

    def test_a_filed_message_is_reported_as_one_not_to_announce(self):
        self.rule()
        address = "owner@manitlab.example"
        self.servers[address].add_message("INBOX", raw("Invoice 41 attached"))
        results = {r.address: r for r in self.engine.sync_all()}
        message_id = self.con.execute("SELECT id FROM message").fetchone()[0]
        self.assertIn(message_id, results[address].filtered.quiet_ids())
        # And `arrived` still names it: the notifier subtracts quiet_ids from
        # arrived, so dropping arrived while leaving quiet_ids would silently
        # announce nothing forever.
        self.assertEqual(results[address].arrived, [message_id])

    def test_with_no_rules_at_all_nothing_is_filtered_and_nothing_breaks(self):
        address = "owner@manitlab.example"
        self.servers[address].add_message("INBOX", raw("Invoice 41 attached"))
        results = {r.address: r for r in self.engine.sync_all()}
        self.assertIsNone(results[address].filtered)
        message_id = self.con.execute("SELECT id FROM message").fetchone()[0]
        self.assertEqual(self.folder_path(message_id), "INBOX")
        # Arrived is carried even when there was nothing to filter: the
        # notifier announces arrived minus quiet_ids, and with no rules that
        # is every new Inbox row.
        self.assertEqual(results[address].arrived, [message_id])

    def test_a_rule_scoped_to_one_account_leaves_the_other_alone(self):
        from cormani.store import folders as folders_repo
        from cormani.store import rulematch, rules
        self.rule(account_id=self.accounts["owner@manitlab.example"])
        for address, server in self.servers.items():
            server.add_message("INBOX", raw("Invoice 41 attached"))
        self.engine.sync_all()
        by_account = {
            r["address"]: r["path"] for r in self.con.execute(
                "SELECT a.address AS address, f.path AS path FROM message m "
                "JOIN folder f ON f.id = m.folder_id "
                "JOIN account a ON a.id = f.account_id")}
        self.assertEqual(by_account["owner@manitlab.example"], "Archive")
        self.assertEqual(by_account["admin@idlidu.example"], "INBOX")
