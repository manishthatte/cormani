# SPDX-License-Identifier: GPL-3.0-or-later
#
# The tracking pane, the reading pane's strip, and what their buttons mean.
#
# EVERY TEST HERE ENDS AT THE STORE. That is the rule stage 3's command bar and
# stage 4's inline reply each cost a feature to learn, and stage 5 repeated it:
# a widget test that drives the widget's own methods proves the widget, and only
# a test that starts at a SIGNAL and asserts a row proves the wiring between
# two of them. So logging a call asserts a touch, tracking a message asserts a
# thread AND its filed conversation, and closing asserts the state.
#
# THE DIALOGS ARE INJECTED. Debian packages no QTest and a modal dialog cannot
# be driven from a test, which is why `TrackHost` takes its dialogs as
# parameters — the same seam the calendar pane and the attachment strip use.
# `Values` below is a dialog that was never opened, answering with what a
# person would have typed.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import unittest

from cormani.store import contacts as contacts_repo
from cormani.store import folders as folders_repo
from cormani.store import ingest, times
from cormani.store import touches as touches_repo
from cormani.store import tracking as tracking_repo
from cormani.store.accounts import add_account
import support

support.qt_app() if support.HAVE_QT else None

TODAY = dt.date(2026, 9, 12)


class Values:
    """A dialog that was never opened, answering with what a person typed."""

    def __init__(self, **values):
        self._values = values

    def values(self) -> dict:
        return dict(self._values)


@support.requires_qt
class Pane(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "manish@manitlab.invalid",
                                   "google")
        self.inbox = folders_repo.ensure_folder(self.con, self.account, "INBOX",
                                                display_name="Inbox",
                                                role="inbox")
        self.answers = {}
        self._uid = 0

    def pane(self, **dialogs):
        from cormani.ui.mailpane import MailPane

        dialogs.setdefault("run", lambda dialog: True)
        return support.own(self, MailPane(self.con, dialogs=dialogs))

    def message(self, *, subject="Re: DWCNT wavelength question",
                key="<in1@covalent.example>",
                frm="Lyle Gordon <lyle@covalent.example>", headers=b""):
        from cormani.imap import envelope

        self._uid += 1
        raw = (f"From: {frm}\nTo: manish@manitlab.invalid\n"
               f"Subject: {subject}\nMessage-ID: {key}\n"
               f"Date: Sat, 12 Sep 2026 09:00:00 +0000\n").encode()
        raw += headers + b"\nA body.\n"
        return ingest.store_message(self.con, self.inbox, self._uid,
                                    envelope.read(raw)).message_id

    def thread(self, title="DWCNT wavelengths", **kwargs):
        kwargs.setdefault("org", "Covalent Example")
        return tracking_repo.create_thread(self.con, title, **kwargs)

    def timeline(self, thread_id):
        return touches_repo.timeline(self.con, thread_id)


class TestTheBoard(Pane):
    def test_it_draws_what_is_being_tracked(self):
        self.thread("One")
        self.thread("Two")
        pane = self.pane()
        pane.show_tracking()
        self.assertEqual(pane.tracking.track.board.count(), 2)

    def test_a_deadline_is_marked_and_an_owed_thread_is_marked_differently(self):
        """Three kinds of attention, drawn three ways. Drawing them the same is
        how a statutory date becomes one more red row among forty."""
        owed = self.thread("Owed one")
        touches_repo.add_touch(self.con, owed, channel="email", direction="in",
                               occurred_at=times.to_utc_text(
                                   times.now_local() - dt.timedelta(days=3)))
        hard = self.thread("Must file",
                           deadline_date=(times.now_local().date()
                                          + dt.timedelta(days=5)).isoformat())
        pane = self.pane()
        pane.show_tracking()
        labels = [pane.tracking.track.board.item(i).text()
                  for i in range(pane.tracking.track.board.count())]
        self.assertTrue(any("⏰" in text for text in labels), labels)
        self.assertTrue(any("owed" in text for text in labels), labels)
        self.assertTrue(hard and owed)

    def test_the_footer_says_which_kind_of_empty_it_is(self):
        # "Nothing to show" over an empty board is indistinguishable from a
        # feature that is broken.
        pane = self.pane()
        pane.show_tracking()
        self.assertIn("Nothing is being tracked yet",
                      pane.tracking.track.footer.text())

    def test_the_tab_title_carries_the_counts(self):
        """The whole compensation for tracking being a tab rather than a rail
        section: a tab left open is the badge the rail does not carry."""
        thread = self.thread()
        touches_repo.add_touch(self.con, thread, channel="email",
                               direction="in",
                               occurred_at=times.to_utc_text(
                                   times.now_local() - dt.timedelta(days=2)))
        pane = self.pane()
        pane.show_tracking()
        self.assertIn("owed", pane.title_for_scope())

    def test_choosing_a_thread_shows_its_timeline(self):
        thread = self.thread()
        touches_repo.log_call(self.con, thread, summary="Rang Lyle")
        pane = self.pane()
        pane.show_tracking()
        pane.tracking.track.board.select(thread)
        self.assertEqual(pane.tracking.track.view.timeline.count(), 1)
        self.assertIn("Rang Lyle",
                      pane.tracking.track.view.timeline.item(0).text())


class TestTheButtonsWrite(Pane):
    """Each starts at the pane's own signal path and ends at a row."""

    def test_logging_a_call_puts_it_on_the_timeline(self):
        thread = self.thread()
        pane = self.pane(**{"log-call": lambda **k: Values(
            summary="Rang Lyle; he will send pricing", channel="phone",
            direction="out", occurred_at=times.to_utc_text(times.now_local()),
            body="")})
        pane.show_tracking()
        pane.tracking.track.board.select(thread)
        pane.tracking.track.view.action.emit("log-call")
        rows = self.timeline(thread)
        self.assertEqual([(t.channel, t.direction) for t in rows],
                         [("phone", "out")])

    def test_a_note_is_direction_note_and_never_discharges_what_is_owed(self):
        thread = self.thread()
        touches_repo.add_touch(self.con, thread, channel="email",
                               direction="in",
                               occurred_at=times.to_utc_text(
                                   times.now_local() - dt.timedelta(days=2)))
        pane = self.pane(note=lambda **k: Values(text="Must reply to this"))
        pane.show_tracking()
        pane.tracking.track.board.select(thread)
        pane.tracking.track.view.action.emit("note")
        self.assertEqual([t.direction for t in self.timeline(thread)][-1],
                         "note")
        self.assertTrue(tracking_repo.get_thread(self.con, thread).owed)

    def test_setting_a_deadline_writes_the_date_and_the_reason(self):
        thread = self.thread()
        pane = self.pane(deadline=lambda **k: Values(
            deadline_date="2026-09-30", deadline_note="Board meets"))
        pane.show_tracking()
        pane.tracking.track.board.select(thread)
        pane.tracking.track.view.action.emit("deadline")
        found = tracking_repo.get_thread(self.con, thread)
        self.assertEqual(found.deadline_date, "2026-09-30")
        self.assertEqual(found.deadline_note, "Board meets")

    def test_closing_is_a_state_and_never_a_delete(self):
        """A closed thread is the record of a matter that was settled, and it
        is what the next enquiry from the same supplier is read against."""
        thread = self.thread()
        pane = self.pane()
        pane.show_tracking()
        pane.tracking.track.board.select(thread)
        pane.tracking.track.view.action.emit("close")
        found = tracking_repo.get_thread(self.con, thread)
        self.assertIsNotNone(found)
        self.assertEqual(found.state, tracking_repo.STATE_CLOSED)

    def test_a_cancelled_dialog_writes_nothing(self):
        thread = self.thread()
        pane = self.pane(run=lambda dialog: False,
                         **{"log-call": lambda **k: Values(summary="No")})
        pane.show_tracking()
        pane.tracking.track.board.select(thread)
        pane.tracking.track.view.action.emit("log-call")
        self.assertEqual(self.timeline(thread), [])

    def test_a_button_with_no_thread_selected_says_so_rather_than_failing(self):
        pane = self.pane()
        pane.show_tracking()
        said = []
        pane.status_message.connect(said.append)
        pane.tracking.track.view.action.emit("log-call")
        self.assertTrue(any("No thread" in text for text in said), said)


class TestTheReadingPaneStrip(Pane):
    def strip(self, pane):
        return pane.reader.tracking

    def test_it_is_quiet_for_a_message_on_no_thread(self):
        # Most messages are on no thread and most never will be.
        pane = self.pane()
        self.strip(pane).show_message(self.message())
        strip = self.strip(pane)
        self.assertIn("Not on a tracked thread", strip.name.text())
        self.assertIsNone(strip.thread_id())
        # `isHidden` and not `isVisible`: nothing here is shown on screen, so
        # `isVisible` is False for every widget in the window and would pass
        # whatever the strip did. `isHidden` reports the explicit setVisible.
        self.assertFalse(strip.buttons["track"].isHidden())
        self.assertTrue(strip.buttons["log-call"].isHidden())

    def test_it_finds_the_thread_from_the_senders_address(self):
        """A correspondent writing from their phone for the first time still
        finds their own thread — which is the case that would otherwise look
        like the feature not working."""
        thread = self.thread()
        contact = contacts_repo.contact_for_address(
            self.con, "lyle@covalent.example", name="Lyle Gordon", create=True)
        tracking_repo.link_contact(self.con, thread, contact.id)
        pane = self.pane()
        self.strip(pane).show_message(self.message())
        self.assertEqual(self.strip(pane).thread_id(), thread)
        self.assertIn("DWCNT", self.strip(pane).name.text())

    def test_a_filed_message_names_its_own_thread_rather_than_the_senders(self):
        # Filed is an answer; the address lookup is an inference.
        filed = self.thread("The one it is filed on")
        other = self.thread("The sender's other thread")
        contact = contacts_repo.contact_for_address(
            self.con, "lyle@covalent.example", name="Lyle Gordon", create=True)
        tracking_repo.link_contact(self.con, other, contact.id)
        message = self.message()
        touches_repo.from_message(self.con, filed, message)
        pane = self.pane()
        self.strip(pane).show_message(message)
        self.assertEqual(self.strip(pane).thread_id(), filed)

    def test_the_standing_line_shows_a_deadline_instead_of_a_nudge(self):
        """One line, and a statutory date is the thing that must be on it.
        Both side by side is how a filing date reads as one more reminder."""
        thread = self.thread(
            deadline_date=(TODAY + dt.timedelta(days=3)).isoformat())
        contact = contacts_repo.contact_for_address(
            self.con, "lyle@covalent.example", create=True)
        tracking_repo.link_contact(self.con, thread, contact.id)
        pane = self.pane()
        self.strip(pane).show_message(self.message(), today=TODAY)
        text = self.strip(pane).standing.text()
        self.assertIn("⏰", text)
        self.assertNotIn("nudge", text)

    def test_tracking_a_message_makes_a_thread_and_files_its_conversation(self):
        """A "Track this" that filed one message would leave the person doing
        by hand exactly the work this layer is for."""
        first = self.message(subject="DWCNT wavelength question",
                             key="<a@covalent.example>")
        second = self.message(subject="Re: DWCNT wavelength question",
                              key="<b@covalent.example>",
                              headers=b"References: <a@covalent.example>\n")
        pane = self.pane(thread=lambda **k: Values(
            title="DWCNT wavelengths", org="Covalent Example",
            track="supplier", state="open", priority=3, cadence_days=7,
            next_action="", note=""))
        thread_id = pane.tracking.track_message(second)
        self.assertTrue(thread_id)
        filed = {t.message_id for t in self.timeline(thread_id)}
        self.assertEqual(filed, {first, second})

    def test_tracking_a_message_puts_its_correspondent_on_the_thread(self):
        # So the NEXT four replies are filed by the matcher rather than landing
        # in the queue one at a time.
        message = self.message()
        pane = self.pane(thread=lambda **k: Values(
            title="DWCNT", org="", track="supplier", state="open", priority=3,
            cadence_days=7, next_action="", note=""))
        thread_id = pane.tracking.track_message(message)
        people = [c.address for c, _r in
                  tracking_repo.thread_contacts(self.con, thread_id)]
        self.assertEqual(people, ["lyle@covalent.example"])

    def test_the_strips_log_call_lands_on_the_MESSAGES_thread(self):
        """Not on whatever the tab happens to have selected. "Log call" appears
        twice and must mean the same thing; landing a call on the wrong thread
        is the class of defect this whole layer exists to prevent."""
        selected = self.thread("The tab's selection")
        senders = self.thread("The message's thread")
        contact = contacts_repo.contact_for_address(
            self.con, "lyle@covalent.example", create=True)
        tracking_repo.link_contact(self.con, senders, contact.id)
        pane = self.pane(**{"log-call": lambda **k: Values(
            summary="Rang about this one", channel="phone", direction="out",
            occurred_at=times.to_utc_text(times.now_local()), body="")})
        pane.show_tracking()
        pane.tracking.track.board.select(selected)
        self.strip(pane).show_message(self.message())
        self.strip(pane).action.emit("log-call")
        self.assertEqual(self.timeline(selected), [])
        self.assertEqual([t.subject for t in self.timeline(senders)],
                         ["Rang about this one"])


class TestTheTabRemembersIt(Pane):
    def test_a_tracking_tab_saves_and_restores_its_thread(self):
        thread = self.thread()
        pane = self.pane()
        pane.show_tracking()
        pane.tracking.track.board.select(thread)
        state = pane.view_state("Tracking")
        self.assertTrue(state.is_tracking)
        self.assertEqual(state.thread_id, thread)

        pane.tracking.show(False)
        pane.restore(state)
        self.assertTrue(pane.showing_tracking())
        self.assertEqual(pane.tracking.track.thread_id(), thread)

    def test_a_mail_tab_is_not_tracking(self):
        state = self.pane().view_state("Inbox")
        self.assertFalse(state.is_tracking)
        self.assertIsNone(state.thread_id)

    def test_the_calendar_and_the_tracking_pane_are_never_shown_together(self):
        """Both claim the space the list and the reader occupy. A pane left
        visible under another looks like a rendering fault."""
        pane = self.pane()
        pane.show_tracking()
        self.assertTrue(pane.tracking.showing)
        pane.calendars.show(True)
        pane.tracking.show(True)
        self.assertTrue(pane.tracking.showing)
        self.assertFalse(pane.calendars.showing)


if __name__ == "__main__":
    unittest.main()


@support.requires_qt
class TestTheQueue(Pane):
    """The triage queue, and the two things a person does with a row of it.

    A queue with no pane is a library nobody can reach, and the whole claim of
    the tracking layer is that an unfiled reply is VISIBLE rather than silent.
    """

    def sent(self, to="Lyle Gordon <lyle@covalent.example>"):
        """Something in the Sent folder, which is what puts an address in
        `wrote_to` — and the narrow scope is defined by that table."""
        from cormani.imap import envelope
        from cormani.store import attach as attach_repo

        folder = folders_repo.ensure_folder(self.con, self.account, "Sent",
                                            display_name="Sent", role="sent")
        self._uid += 1
        raw = (f"From: manish@manitlab.invalid\nTo: {to}\n"
               f"Subject: An opening\nMessage-ID: <out{self._uid}@x>\n"
               f"Date: Sat, 05 Sep 2026 09:00:00 +0000\n\nHello.\n").encode()
        ingest.store_message(self.con, folder, self._uid, envelope.read(raw))
        attach_repo.rebuild_wrote_to(self.con)

    def queue(self, pane):
        return pane.tracking.track.queue

    def test_it_lists_what_is_on_no_thread(self):
        self.sent()
        self.message()
        pane = self.pane()
        pane.show_tracking()
        pane.tracking.track.show_queue(True)
        self.assertEqual(self.queue(pane).list.count(), 1)

    def test_the_scope_chooser_carries_all_three_counts(self):
        """A narrow default that HID the rest would be a queue that lied. The
        wider numbers are on the chooser, so nothing is hidden — it is deferred
        and the number says by how much."""
        self.message(frm="Stranger <who@nowhere.example>", key="<s@x>")
        pane = self.pane()
        pane.show_tracking()
        pane.tracking.track.show_queue(True)
        labels = [self.queue(pane).scope.itemText(i)
                  for i in range(self.queue(pane).scope.count())]
        self.assertEqual(len(labels), 3)
        self.assertTrue(any(text.endswith("1") for text in labels), labels)

    def test_filing_puts_it_on_the_thread_the_board_has_selected(self):
        thread = self.thread()
        self.sent()
        message = self.message()
        pane = self.pane()
        pane.show_tracking()
        pane.tracking.track.show_queue(True)
        pane.tracking.track.board.select(thread)
        self.queue(pane).list.setCurrentRow(0)
        self.queue(pane).file_requested.emit(message)
        self.assertEqual([t.message_id for t in self.timeline(thread)],
                         [message])

    def test_filing_with_no_thread_selected_says_so_rather_than_guessing(self):
        """Filing onto a thread the person did not choose is the one mistake
        this layer must never make."""
        self.sent()
        message = self.message()
        pane = self.pane()
        said = []
        pane.status_message.connect(said.append)
        pane.show_tracking()
        pane.tracking.track.show_queue(True)
        pane.tracking.track.board.setCurrentItem(None)
        self.queue(pane).file_requested.emit(message)
        self.assertTrue(any("Choose a thread" in text for text in said), said)

    def test_dismissing_takes_it_out_and_undo_puts_it_back(self):
        # A queue you cannot undo is one people are afraid to work through, and
        # a queue nobody works through is the failure this layer exists for.
        self.sent()
        self.message()
        pane = self.pane()
        pane.show_tracking()
        pane.tracking.track.show_queue(True)
        queue = self.queue(pane)
        queue.list.setCurrentRow(0)
        queue._dismiss()
        self.assertEqual(queue.list.count(), 0)
        self.assertTrue(queue.undo_button.isEnabled())
        queue._undo()
        self.assertEqual(queue.list.count(), 1)

    def test_the_footer_tells_the_three_empties_apart(self):
        pane = self.pane()
        pane.show_tracking()
        pane.tracking.track.show_queue(True)
        self.assertIn("filed or", self.queue(pane).footer.text())

        self.message(frm="News <news@weekly.example>", key="<n@x>",
                     headers=b"List-Id: <weekly.example>\n")
        self.queue(pane).reload()
        self.assertIn("widest one", self.queue(pane).footer.text())

    def test_the_button_carries_the_count(self):
        self.sent()
        self.message()
        pane = self.pane()
        pane.show_tracking()
        self.assertIn("(1)", pane.tracking.track.queue_button.text())
