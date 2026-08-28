# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing a message: the draft, the derivation, and the bytes.
#
# THE TESTS THAT MATTER ARE THE ONES ABOUT WHO A REPLY GOES TO. Every one of
# them is a mistake that produces a message which LOOKS fine: a reply-all with
# the user's own address in Cc, a reply to a mailing list sent to the person who
# posted, a subject that grows a second "Re:", a References chain with one entry
# in it so the correspondent's client starts a new thread. None of those raise,
# and none of them are noticed until somebody else notices.
#
# And one that is not about correctness at all: BCC IS NOT A HEADER. It is on
# the envelope and must never be in the message, and the test for it is the
# difference between a blind copy and a broken confidence.
#
# © Manish Jagdish Thatte
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import support                                              # noqa: F401
from test_threads import Store

from cormani.compose import build, quote
from cormani.compose.draft import (Attachment, Draft, SIGNATURE_MARK,
                                   strip_signature, with_signature)
from cormani.store import messages


class Identity:
    """Enough of store.accounts.Identity for the derivations."""

    def __init__(self, address, display_name="", signature=""):
        self.address = address
        self.display_name = display_name
        self.signature = signature


class TestTheDraft(unittest.TestCase):
    def test_the_envelope_is_to_cc_and_bcc_together_without_repeats(self):
        draft = Draft(account_id=1, to="a@x, Lyle <b@x>", cc="b@x",
                      bcc="c@x, a@x")
        self.assertEqual(draft.recipients(), ["a@x", "b@x", "c@x"])
        self.assertTrue(draft.is_addressed)

    def test_a_draft_with_nothing_in_it_knows_so(self):
        self.assertTrue(Draft(account_id=1).is_empty)
        self.assertFalse(Draft(account_id=1, body="hello").is_empty)
        self.assertFalse(Draft(account_id=1, to="a@x").is_empty)
        self.assertFalse(Draft(account_id=1).is_addressed)

    def test_a_display_name_with_a_comma_in_it_is_quoted(self):
        # Or one recipient becomes two on the way out.
        draft = Draft(account_id=1, from_name="Thatte, Manish",
                      from_address="m@x")
        self.assertEqual(draft.sender, '"Thatte, Manish" <m@x>')

    def test_a_signature_is_applied_once_however_often_it_is_asked_for(self):
        body = with_signature("Text.", "Manish\nmanitlab.org")
        again = with_signature(body, "Manish\nmanitlab.org")
        self.assertEqual(body, again)
        self.assertEqual(body.count(SIGNATURE_MARK), 1)

    def test_changing_the_signature_replaces_it_rather_than_stacking(self):
        body = with_signature("Text.", "One")
        swapped = with_signature(body, "Two")
        self.assertIn("Two", swapped)
        self.assertNotIn("One", swapped)
        self.assertEqual(swapped.count(SIGNATURE_MARK), 1)

    def test_a_quoted_signature_is_not_mistaken_for_the_users_own(self):
        # The separator inside a quotation arrives as "> -- " and must not cut.
        body = "My answer.\n\nOn Tuesday, Lyle wrote:\n> hello\n> -- \n> Lyle"
        self.assertEqual(strip_signature(body), body)

    def test_removing_a_signature_that_is_not_there_changes_nothing(self):
        self.assertEqual(with_signature("Text.", "  "), "Text.")


class QuoteCase(unittest.TestCase):
    """The fixture both derivations need. A base with no tests of its own —
    inheriting a TestCase that HAS tests runs them a second time under the
    child's name, which is a suite that grows without covering anything."""

    def setUp(self):
        self.fixture = Store(self)
        self.con = self.fixture.con
        self.identities = [Identity("owner@manitlab.example", "Manish Thatte",
                                    "Manish Jagdish Thatte")]
        self.mine = {"owner@manitlab.example", "krishna@example.com"}

    def message(self, **columns) -> messages.Row:
        message_id = self.fixture.store(
            subject=columns.pop("subject", "Wavelengths"),
            message_id=columns.pop("message_id", "<root@x>"),
            sender=columns.pop("sender", "Lyle Gordon <lyle@covalent.example>"),
            **columns)
        return messages.get_row(self.con, message_id)

    def set(self, row, **columns):
        for name, value in columns.items():
            self.con.execute(f"UPDATE message SET {name} = ? WHERE id = ?",
                             (value, row.id))
        self.con.commit()
        return messages.get_row(self.con, row.id)

class TestReplying(QuoteCase):
    def test_a_reply_goes_to_the_sender(self):
        draft = quote.reply(self.message(), "body", self.identities,
                            mine=self.mine)
        self.assertEqual(draft.to, "Lyle Gordon <lyle@covalent.example>")
        self.assertEqual(draft.cc, "")
        self.assertEqual(draft.from_address, "owner@manitlab.example")

    def test_reply_to_wins_over_from(self):
        # A correspondent who set it is asking to be answered somewhere else,
        # and the commonest reason is a mailing list.
        row = self.set(self.message(), reply_to="list@lists.example")
        draft = quote.reply(row, "body", self.identities, mine=self.mine)
        self.assertEqual(draft.to, "list@lists.example")

    def test_reply_all_keeps_everyone_else_and_drops_the_user(self):
        row = self.set(self.message(),
                       to_addrs="owner@manitlab.example, Frances <frances@idlidu.example>",
                       cc_addrs="krishna@example.com, anil@example.org")
        draft = quote.reply(row, "body", self.identities, all_recipients=True,
                            mine=self.mine)
        self.assertEqual(draft.to, "Lyle Gordon <lyle@covalent.example>")
        self.assertIn("frances@idlidu.example", draft.cc)
        self.assertIn("anil@example.org", draft.cc)
        self.assertNotIn("owner@manitlab.example", draft.cc)
        self.assertNotIn("krishna@example.com", draft.cc)

    def test_reply_all_never_copies_the_person_it_is_addressed_to(self):
        row = self.set(self.message(),
                       to_addrs="owner@manitlab.example",
                       cc_addrs="Lyle <lyle@covalent.example>, x@y.example")
        draft = quote.reply(row, "body", self.identities, all_recipients=True,
                            mine=self.mine)
        self.assertNotIn("lyle@covalent.example", draft.cc)
        self.assertIn("x@y.example", draft.cc)

    def test_the_subject_gets_one_prefix_however_many_it_arrived_with(self):
        for subject, expected in (
                ("Wavelengths", "Re: Wavelengths"),
                ("Re: Wavelengths", "Re: Wavelengths"),
                ("RE: Re: Wavelengths", "Re: Wavelengths"),
                ("AW: Wavelengths", "Re: Wavelengths"),
                ("Re[2]: Wavelengths", "Re: Wavelengths"),
                ("Fwd: Wavelengths", "Re: Wavelengths")):
            row = self.set(self.message(), subject=subject)
            draft = quote.reply(row, "b", self.identities, mine=self.mine)
            self.assertEqual(draft.subject, expected, subject)

    def test_the_chain_is_kept_and_extended(self):
        row = self.set(self.message(), references_="<a@x> <b@x>")
        draft = quote.reply(row, "body", self.identities, mine=self.mine)
        self.assertEqual(draft.in_reply_to, "<root@x>")
        self.assertEqual(draft.references, "<a@x> <b@x> <root@x>")

    def test_a_message_with_no_chain_starts_one(self):
        draft = quote.reply(self.message(), "body", self.identities,
                            mine=self.mine)
        self.assertEqual(draft.references, "<root@x>")

    def test_it_answers_from_the_address_the_message_was_sent_to(self):
        identities = [Identity("owner@manitlab.example", "Manish"),
                      Identity("saptarang@outlook.example", "Saptarang")]
        row = self.set(self.message(), to_addrs="saptarang@outlook.example")
        draft = quote.reply(row, "body", identities, mine=self.mine)
        self.assertEqual(draft.from_address, "saptarang@outlook.example")

    def test_the_quotation_is_marked_and_not_reflowed(self):
        body = "one\n\n    indented and quite long, deliberately so\nthree"
        draft = quote.reply(self.message(), body, self.identities,
                            mine=self.mine)
        self.assertIn("> one", draft.body)
        self.assertIn(">     indented and quite long, deliberately so", draft.body)
        self.assertIn("wrote:", draft.body)

    def test_the_signature_goes_under_the_quotation(self):
        draft = quote.reply(self.message(), "hello", self.identities,
                            mine=self.mine, signature="Manish")
        self.assertTrue(draft.body.rstrip().endswith("Manish"))
        self.assertLess(draft.body.index("> hello"),
                        draft.body.index(SIGNATURE_MARK))

    def test_an_empty_quotation_still_has_the_attribution(self):
        draft = quote.reply(self.message(), "", self.identities, mine=self.mine)
        self.assertIn("Lyle Gordon wrote:", draft.body.replace("\n", " "))


class TestForwarding(QuoteCase):
    def test_a_forward_is_addressed_by_the_person_forwarding_it(self):
        draft = quote.forward(self.message(), "body", self.identities)
        self.assertEqual(draft.to, "")
        self.assertEqual(draft.cc, "")

    def test_it_says_where_the_message_came_from(self):
        draft = quote.forward(self.message(), "body", self.identities)
        self.assertIn("Forwarded message", draft.body)
        self.assertIn("lyle@covalent.example", draft.body)
        self.assertEqual(draft.subject, "Fwd: Wavelengths")

    def test_the_attachments_come_with_it(self):
        draft = quote.forward(self.message(), "body", self.identities,
                              attachments=(("/tmp/one.pdf", "one.pdf",
                                            "application/pdf"),))
        self.assertEqual([a.name for a in draft.attachments], ["one.pdf"])

    def test_the_original_is_not_quoted_with_angle_brackets(self):
        # A forward shows the message; it does not answer it.
        draft = quote.forward(self.message(), "the body", self.identities)
        self.assertIn("the body", draft.body)
        self.assertNotIn("> the body", draft.body)


class TestBuilding(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.when = dt.datetime(2026, 8, 25, 15, 48, tzinfo=dt.timezone.utc)
        self.draft = Draft(
            account_id=1, from_address="owner@manitlab.example",
            from_name="Manish Thatte", to="Lyle <lyle@covalent.example>",
            cc="frances@idlidu.example", bcc="quiet@example.org",
            subject="Re: Wavelengths", body="Two lines.\nHere.",
            in_reply_to="<root@x>", references="<a@x> <root@x>")

    def message(self, draft=None):
        return build.build(draft or self.draft, message_id="<new@manitlab.example>",
                           now=self.when)

    def test_the_headers_a_reply_needs_are_all_there(self):
        message = self.message()
        self.assertEqual(message["From"], "Manish Thatte <owner@manitlab.example>")
        self.assertEqual(message["To"], "Lyle <lyle@covalent.example>")
        self.assertEqual(message["Cc"], "frances@idlidu.example")
        self.assertEqual(message["Subject"], "Re: Wavelengths")
        self.assertEqual(message["In-Reply-To"], "<root@x>")
        self.assertEqual(message["References"], "<a@x> <root@x>")
        self.assertEqual(message["Message-ID"], "<new@manitlab.example>")
        self.assertIn("corMani", message["User-Agent"])

    def test_bcc_is_on_the_envelope_and_never_in_the_message(self):
        raw = build.to_bytes(self.draft, message_id="<new@x>", now=self.when)
        self.assertNotIn(b"quiet@example.org", raw)
        self.assertIn("quiet@example.org", self.draft.recipients())

    def test_the_date_carries_an_offset(self):
        self.assertIn("+0000", self.message()["Date"])

    def test_the_body_is_plain_text_and_ends_with_a_newline(self):
        message = self.message()
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertTrue(message.get_content().endswith("\n"))

    def test_a_message_id_is_made_from_the_senders_own_domain(self):
        # And not from the machine's hostname, which would put the name of the
        # computer the mail was written on into every message.
        made = build.new_message_id("owner@manitlab.example")
        self.assertTrue(made.startswith("<") and made.endswith(">"))
        self.assertIn("@manitlab.example", made)

    def test_an_attachment_arrives_with_its_name_and_type(self):
        path = self.root / "figures.pdf"
        path.write_bytes(b"%PDF-1.4 not really")
        draft = self.draft.with_changes(
            attachments=(Attachment(path=str(path)),))
        message = self.message(draft)
        parts = list(message.iter_attachments())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_filename(), "figures.pdf")
        self.assertEqual(parts[0].get_content_type(), "application/pdf")
        self.assertEqual(parts[0].get_payload(decode=True), b"%PDF-1.4 not really")

    def test_an_unknown_extension_is_sent_as_bytes_rather_than_refused(self):
        path = self.root / "notes.wombat"
        path.write_bytes(b"\x00\x01\x02")
        message = self.message(self.draft.with_changes(
            attachments=(Attachment(path=str(path)),)))
        part = list(message.iter_attachments())[0]
        self.assertEqual(part.get_content_type(), build.DEFAULT_TYPE)

    def test_an_absurd_attachment_is_refused_before_the_server_refuses_it(self):
        path = self.root / "huge.bin"
        path.write_bytes(b"x")
        draft = self.draft.with_changes(attachments=(Attachment(path=str(path)),))
        from unittest import mock
        with mock.patch.object(build, "MAX_ATTACHMENT", 0):
            with self.assertRaises(build.TooLarge):
                build.build(draft)


if __name__ == "__main__":
    unittest.main()
