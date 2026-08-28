# SPDX-License-Identifier: GPL-3.0-or-later
#
# The composer, and everything between it and the outbox.
#
# THE TESTS THAT MATTER RUN FROM A BUTTON TO A ROW IN THE STORE. Pressing Send
# has to save a draft, put an op in the outbox and close the window, and a test
# that only checked the window closed would pass with the message nowhere.
#
# THE GUARDS ARE THE OTHER HALF, and each of them is a question rather than a
# refusal: an address that has bounced before, a message with no subject, a
# draft with something typed in it being closed. Every one asks, and every one
# does what the answer said — including "no", which is the case a hurried
# implementation gets wrong by asking and then going ahead anyway.
#
# Debian packages no QTest, so the three dialogs are injected. The answers here
# are functions, and the assertions are about what was WRITTEN.
#
# © Manish Jagdish Thatte
import tempfile
import unittest
from pathlib import Path

import support
from test_threads import Store

from cormani.compose.draft import Draft
from cormani.smtp import outbox
from cormani.store import accounts, contacts, drafts, messages
from cormani.store.database import utc_now


@support.requires_qt
class ComposerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        self.fixture = Store(self)
        self.con = self.fixture.con
        accounts.add_identity(self.con, self.fixture.account,
                              "owner@manitlab.example", display_name="Manish Thatte",
                              signature="Manish Jagdish Thatte", is_default=True)
        self.original = self.fixture.store(
            subject="Wavelengths", message_id="<root@x>",
            sender="Lyle Gordon <lyle@covalent.example>")
        self.asked: list = []
        self.answer = True
        self.warned: list = []
        self.files: list = []

    # ---------------------------------------------------------- the dialogs
    def confirm(self, parent, title, text):
        self.asked.append(f"{title}: {text}")
        return self.answer

    def warn(self, parent, title, text):
        self.warned.append(f"{title}: {text}")

    def ask_files(self, parent):
        return list(self.files)

    def composer(self, kind="reply", **kw):
        from cormani.ui import composer as composer_mod

        row = messages.get_row(self.con, self.original)
        body = messages.body_of(self.con, self.original)
        common = dict(confirm=self.confirm, warn=self.warn,
                      ask_files=self.ask_files)
        common.update(kw)
        if kind == "new":
            window = composer_mod.for_new(self.con, self.fixture.account, **common)
        elif kind == "forward":
            window = composer_mod.for_forward(self.con, row, body, **common)
        else:
            window = composer_mod.for_reply(
                self.con, row, body, all_recipients=(kind == "reply_all"),
                **common)
        return support.own(self, window)

    def only_draft(self):
        row = self.con.execute(
            "SELECT id FROM message WHERE draft = 1 ORDER BY id DESC "
            "LIMIT 1").fetchone()
        return messages.get_row(self.con, row[0]) if row else None


class TestWhatOpens(ComposerCase):
    def test_a_reply_arrives_addressed_quoted_and_signed(self):
        window = self.composer("reply")
        self.assertEqual(window.to.text(), "Lyle Gordon <lyle@covalent.example>")
        self.assertEqual(window.subject.text(), "Re: Wavelengths")
        self.assertIn("wrote:", window.body.toPlainText())
        self.assertTrue(window.body.toPlainText().rstrip().endswith(
            "Manish Jagdish Thatte"))

    def test_a_forward_is_unaddressed(self):
        window = self.composer("forward")
        self.assertEqual(window.to.text(), "")
        self.assertEqual(window.subject.text(), "Fwd: Wavelengths")
        self.assertIn("Forwarded message", window.body.toPlainText())

    def test_a_new_message_is_empty_but_for_the_signature(self):
        window = self.composer("new")
        self.assertEqual(window.to.text(), "")
        self.assertEqual(window.subject.text(), "")
        self.assertIn("Manish Jagdish Thatte", window.body.toPlainText())
        self.assertTrue(window.draft().is_empty is False)   # the signature

    def test_the_from_box_offers_the_accounts_identities(self):
        accounts.add_identity(self.con, self.fixture.account,
                              "press@manitlab.example", display_name="Press")
        window = self.composer("new")
        addresses = [window.identity.itemData(n)
                     for n in range(window.identity.count())]
        self.assertIn("owner@manitlab.example", addresses)
        self.assertIn("press@manitlab.example", addresses)

    def test_changing_the_sending_address_changes_the_signature(self):
        accounts.add_identity(self.con, self.fixture.account,
                              "press@manitlab.example", display_name="Press",
                              signature="The Press Office")
        window = self.composer("new")
        window.identity.setCurrentIndex(
            [window.identity.itemData(n) for n in range(window.identity.count())]
            .index("press@manitlab.example"))
        body = window.body.toPlainText()
        self.assertIn("The Press Office", body)
        self.assertNotIn("Manish Jagdish Thatte", body)


class TestSending(ComposerCase):
    def test_send_saves_a_draft_queues_it_and_closes(self):
        window = self.composer("reply")
        self.assertTrue(window.send())
        self.assertEqual(outbox.waiting(self.con), 1)
        row = messages.get_row(self.con, window.draft().message_id)
        self.assertEqual(row.to_addrs, "Lyle Gordon <lyle@covalent.example>")
        self.assertEqual(row.subject, "Re: Wavelengths")

    def test_a_message_with_nobody_to_send_it_to_is_not_sent(self):
        window = self.composer("forward")            # no recipients by design
        self.assertFalse(window.send())
        self.assertEqual(outbox.waiting(self.con), 0)
        self.assertIn("Nobody", window.note.text())

    def test_a_message_with_no_subject_asks_first(self):
        window = self.composer("new")
        window.to.setText("lyle@covalent.example")
        self.answer = False
        self.assertFalse(window.send())
        self.assertIn("no subject", " ".join(self.asked).lower())
        self.assertEqual(outbox.waiting(self.con), 0)

        self.answer = True
        self.assertTrue(window.send())
        self.assertEqual(outbox.waiting(self.con), 1)

    def test_an_address_that_has_bounced_is_named_before_sending(self):
        cur = self.con.execute(
            "INSERT INTO contact (name, org, role, notes, status, created_at, "
            "updated_at) VALUES ('Lyle Gordon', '', '', '', 'active', ?, ?)",
            (utc_now(), utc_now()))
        self.con.execute(
            "INSERT INTO handle (contact_id, kind, value, status, note, "
            "bounce_count, created_at) VALUES (?, 'email', ?, 'bounced', ?, 2, ?)",
            (cur.lastrowid, "lyle@covalent.example", "mailbox full", utc_now()))
        self.con.commit()

        window = self.composer("reply")
        self.answer = False
        self.assertFalse(window.send())
        asked = " ".join(self.asked)
        self.assertIn("lyle@covalent.example", asked)
        self.assertIn("mailbox full", asked)
        self.assertEqual(outbox.waiting(self.con), 0)

        self.answer = True                          # the user knows better
        self.assertTrue(window.send())
        self.assertEqual(outbox.waiting(self.con), 1)

    def test_an_attachment_that_has_moved_stops_the_send_and_says_so(self):
        directory = Path(tempfile.mkdtemp())
        path = directory / "figures.pdf"
        path.write_bytes(b"%PDF")
        window = self.composer("reply")
        window.attach([str(path)])
        path.unlink()
        self.assertFalse(window.send())
        self.assertIn("figures.pdf", " ".join(self.warned))
        self.assertEqual(outbox.waiting(self.con), 0)


class TestDrafts(ComposerCase):
    def test_saving_twice_keeps_one_row_and_one_message_id(self):
        window = self.composer("reply")
        first = window.save()
        rfc = messages.get_row(self.con, first).message_id
        window.body.setPlainText("More thoughts.")
        second = window.save()
        self.assertEqual(first, second)
        self.assertEqual(messages.get_row(self.con, second).message_id, rfc)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM message WHERE draft = 1")
            .fetchone()[0], 1)

    def test_a_saved_draft_comes_back_as_it_was(self):
        window = self.composer("reply")
        window.cc.setText("frances@idlidu.example")
        window.body.setPlainText("Body of the draft.")
        row_id = window.save()

        again = drafts.load(self.con, row_id)
        self.assertEqual(again.cc, "frances@idlidu.example")
        self.assertEqual(again.body, "Body of the draft.")
        self.assertEqual(again.in_reply_to, "<root@x>")
        self.assertEqual(again.subject, "Re: Wavelengths")

    def test_closing_an_untouched_composer_asks_nothing(self):
        window = self.composer("new")
        window.body.setPlainText("")             # even the signature removed
        window.close()
        self.assertEqual(self.asked, [])
        self.assertIsNone(self.only_draft())

    def test_closing_with_something_typed_offers_to_keep_it(self):
        window = self.composer("reply")
        window.body.setPlainText("Half an answer.")
        self.answer = True
        window.close()
        self.assertIn("draft", " ".join(self.asked).lower())
        self.assertEqual(self.only_draft().subject, "Re: Wavelengths")

    def test_and_says_no_it_is_thrown_away(self):
        window = self.composer("reply")
        window.body.setPlainText("Half an answer.")
        self.answer = False
        window.close()
        self.assertIsNone(self.only_draft())

    def test_a_draft_already_saved_and_then_declined_is_removed(self):
        window = self.composer("reply")
        window.body.setPlainText("Half an answer.")
        window.save()
        self.assertIsNotNone(self.only_draft())
        self.answer = False
        window.close()
        self.assertIsNone(self.only_draft())

    def test_closing_after_sending_asks_nothing(self):
        window = self.composer("reply")
        window.send()
        self.asked.clear()
        window.close()
        self.assertEqual(self.asked, [])


class TestAttachments(ComposerCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(tempfile.mkdtemp())
        self.path = self.directory / "figures.pdf"
        self.path.write_bytes(b"%PDF")

    def test_files_can_be_added_and_taken_off_again(self):
        window = self.composer("reply")
        self.files = [str(self.path)]
        self.assertEqual(window.attach(), 1)
        self.assertEqual([a.name for a in window.draft().attachments],
                         ["figures.pdf"])
        window.remove_attachment(str(self.path))
        self.assertEqual(window.draft().attachments, ())

    def test_the_same_file_twice_is_attached_once(self):
        window = self.composer("reply")
        window.attach([str(self.path)])
        window.attach([str(self.path)])
        self.assertEqual(len(window.draft().attachments), 1)

    def test_an_attachment_survives_being_saved_and_loaded(self):
        window = self.composer("reply")
        window.attach([str(self.path)])
        row_id = window.save()
        again = drafts.load(self.con, row_id)
        self.assertEqual([a.path for a in again.attachments], [str(self.path)])
        self.assertTrue(messages.get_row(self.con, row_id).has_attachment)


@support.requires_qt
class TestFromTheWindow(unittest.TestCase):
    """The keys and the menu, through to the outbox."""

    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.window import MainWindow

        self.fixture = Store(self)
        self.con = self.fixture.con
        self.message = self.fixture.store(subject="Wavelengths",
                                          message_id="<root@x>")
        self.window = support.own(self, MainWindow(self.con, demo=False))
        self.mail = self.window.mail

    def test_r_opens_a_reply_to_the_selected_message(self):
        self.mail.select_message(self.message)
        window = self.mail.compose("reply")
        self.addCleanup(support.dispose, window)
        self.assertIsNotNone(window)
        self.assertEqual(window.subject.text(), "Re: Wavelengths")
        self.assertIn(window, self.mail.actions.composers)

    def test_replying_with_nothing_selected_says_so(self):
        self.mail.clear_selection()
        self.assertIsNone(self.mail.compose("reply"))
        self.assertIn("No message selected", self.window.status_message.text())

    def test_a_new_message_needs_no_selection(self):
        self.mail.clear_selection()
        window = self.mail.compose("new")
        self.addCleanup(support.dispose, window)
        self.assertIsNotNone(window)

    def test_queueing_shows_the_count_in_the_status_bar(self):
        self.mail.select_message(self.message)
        window = self.mail.compose("reply")
        self.addCleanup(support.dispose, window)
        window.send()
        self.assertIn("1 waiting", self.window.status_outbox.text())

    def test_the_reading_panes_reply_button_reaches_the_same_place(self):
        self.mail.select_message(self.message)
        self.mail.reader.command.emit("reply")
        self.assertEqual(len(self.mail.actions.composers), 1)
        window = self.mail.actions.composers[0]
        self.addCleanup(support.dispose, window)
        self.assertEqual(window.subject.text(), "Re: Wavelengths")

    def test_a_closed_composer_is_let_go_of(self):
        self.mail.select_message(self.message)
        window = self.mail.compose("reply")
        window.send()
        self.assertEqual(self.mail.actions.composers, [])


@support.requires_qt
class TestTheInlineReply(unittest.TestCase):
    """The box under the message. The same derivation, without a window."""

    @classmethod
    def setUpClass(cls):
        cls.app = support.qt_app()

    def setUp(self):
        from cormani.ui.window import MainWindow

        self.fixture = Store(self)
        self.con = self.fixture.con
        accounts.add_identity(self.con, self.fixture.account,
                              "owner@manitlab.example", display_name="Manish",
                              signature="Manish Jagdish Thatte", is_default=True)
        self.message = self.fixture.store(
            subject="Wavelengths", message_id="<root@x>",
            sender="Lyle Gordon <lyle@covalent.example>")
        self.window = support.own(self, MainWindow(self.con, demo=False))
        self.mail = self.window.mail
        self.inline = self.mail.reader.inline

    def sent_draft(self):
        row = self.con.execute(
            "SELECT id FROM message WHERE draft = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return drafts.load(self.con, row[0]) if row else None

    def test_it_is_hidden_until_there_is_something_to_answer(self):
        self.mail.clear_selection()
        self.assertIsNone(self.inline.message)
        self.mail.select_message(self.message)
        self.assertIsNotNone(self.inline.message)
        self.assertIn("Lyle", self.inline.text.placeholderText())

    def test_it_does_not_offer_to_answer_the_users_own_message(self):
        sent_folder = self.fixture.sent
        mine = self.fixture.store(sent_folder, subject="Mine",
                                  message_id="<mine@x>",
                                  sender="owner@manitlab.example")
        self.mail.select_message(mine)
        self.assertIsNone(self.inline.message)

    def test_an_empty_box_sends_nothing(self):
        self.mail.select_message(self.message)
        self.assertFalse(self.inline.send())
        self.assertFalse(self.inline.send_button.isEnabled())
        self.assertEqual(outbox.waiting(self.con), 0)

    def test_a_sentence_becomes_a_proper_reply(self):
        self.mail.select_message(self.message)
        self.inline.text.setPlainText("Thursday works.")
        self.assertTrue(self.inline.send())

        self.assertEqual(outbox.waiting(self.con), 1)
        draft = self.sent_draft()
        self.assertEqual(draft.to, "Lyle Gordon <lyle@covalent.example>")
        self.assertEqual(draft.subject, "Re: Wavelengths")
        self.assertEqual(draft.in_reply_to, "<root@x>")
        body = draft.body
        self.assertTrue(body.startswith("Thursday works."))
        self.assertIn("wrote:", body)                    # the quotation
        self.assertIn("Manish Jagdish Thatte", body)     # and the signature

    def test_it_empties_itself_and_says_what_happened(self):
        self.mail.select_message(self.message)
        self.inline.text.setPlainText("Thursday works.")
        self.inline.send()
        self.assertEqual(self.inline.text.toPlainText(), "")
        self.assertIn("Queued", self.window.status_message.text())
        self.assertIn("1 waiting", self.window.status_outbox.text())

    def test_half_a_sentence_does_not_follow_the_cursor_to_another_message(self):
        # Left in the box while another message is opened, it would be a reply
        # to the wrong person.
        other = self.fixture.store(subject="Something else", message_id="<o@x>")
        self.mail.select_message(self.message)
        self.inline.text.setPlainText("Half an answer")
        self.mail.select_message(other)
        self.assertEqual(self.inline.text.toPlainText(), "")

    def test_open_in_composer_carries_what_was_typed(self):
        self.mail.select_message(self.message)
        self.inline.text.setPlainText("This needs more room.")
        self.inline.expand()
        self.assertEqual(len(self.mail.actions.composers), 1)
        window = self.mail.actions.composers[0]
        self.addCleanup(support.dispose, window)
        self.assertTrue(window.body.toPlainText().startswith(
            "This needs more room."))
        self.assertEqual(window.to.text(), "Lyle Gordon <lyle@covalent.example>")
        self.assertEqual(self.inline.text.toPlainText(), "")


if __name__ == "__main__":
    unittest.main()
