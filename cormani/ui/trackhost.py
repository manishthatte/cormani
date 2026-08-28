# SPDX-License-Identifier: GPL-3.0-or-later
#
# The tracking pane, the reading pane's strip, and what their buttons MEAN.
#
# `ui/calendarhost.py`'s shape, and for the same reason it has that shape:
# holding a pane is not the same job as arranging four widgets, and deciding
# what a command means is a third job again — `ui/actions.py` made that
# argument and it holds here. Both the tab and the strip emit names; this file
# is the only place that turns a name into a write.
#
# ── ONE HOST FOR TWO SURFACES, AND THAT IS THE POINT ───────────────────────
#
# "Log call" appears twice — once in the tracking tab and once under the
# reading pane — and it must mean exactly the same thing in both. A second
# implementation would be a second set of bugs about which thread a call landed
# on, which is the class of defect the whole tracking layer exists to prevent.
#
# ── THE DIALOGS ARE INJECTED ───────────────────────────────────────────────
#
# Debian ships no QTest, so a modal dialog cannot be driven from a test. The
# host takes its four dialogs as parameters, exactly as `ui/calendarpane.py`
# does, and the suite hands it objects that answer `values()` with what a
# person would have typed. Every test then STARTS at a signal and ENDS at the
# store, which is the rule stage 3 and stage 4 each paid a feature to learn.
#
# ── TRACKING A MESSAGE MAKES A THREAD *AND* FILES THE CONVERSATION ─────────
#
# `store/attach.attach_message` files the whole References chain and puts the
# correspondent on the thread, so the next four replies are filed by matcher 2
# instead of arriving in the triage queue one at a time. A "Track this" that
# filed one message would leave the person doing by hand exactly the work this
# layer is for.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from ..store import attach as attach_repo
from ..store import touches as touches_repo
from ..store import tracking as tracking_repo
from . import panespace
from .trackpane import TrackPane


class TrackHost:
    """The tracking pane, and every command either surface can ask for."""

    def __init__(self, pane, *, dialogs=None) -> None:
        self.pane = pane
        self._dialogs = dialogs or {}
        con = pane._con

        self.track = TrackPane(con, pane)
        self.track.setVisible(False)
        self.track.status_message.connect(pane.status_message)
        self.track.action.connect(self.action)
        self.track.message_activated.connect(self._open_message)
        self.track.thread_chosen.connect(lambda _id: pane.view_changed.emit())
        self.track.file_requested.connect(self._file_from_queue)
        pane.splitter.addWidget(self.track)

        # The strip lives in the reading pane and is driven from here.
        strip = getattr(pane.reader, "tracking", None)
        if hasattr(strip, "action"):
            strip.action.connect(self._strip_action)

        self.showing = False

    # ------------------------------------------------------------- swapping
    def show(self, on: bool) -> None:
        """Swap the middle and the reader for the tracking pane, or back.

        The others are asked to stand down first. Two panes both claiming the
        space the list and the reader occupy is a window with one drawn over
        the other, and the person who owns the space is whoever was asked last.
        WHICH others is `ui/panespace.py`'s to know: this method used to carry
        its own list and that list was made wrong, silently, by the address
        book arriving in a different file.
        """
        on = bool(on)
        if on:
            panespace.claim(self.pane, "tracking")
        self.showing = on
        for widget in (self.pane.middle, self.pane.reader):
            widget.setVisible(not on)
        self.track.setVisible(on)
        if on:
            self.track.reload()

    def open(self, thread_id: int | None = None) -> None:
        """Show the tracking pane, at a thread if one was named."""
        self.show(True)
        if thread_id:
            self.track.show_thread(int(thread_id))

    def title(self) -> str:
        return self.track.title()

    # ------------------------------------------------------------- commands
    def action(self, name: str, *, thread_id: int | None = None) -> bool:
        """What a button means. Returns whether anything was written.

        The thread is the one the tab is showing unless the caller names one —
        which is how the reading pane's strip acts on the thread the MESSAGE is
        on rather than on whatever the tab happens to have selected.
        """
        thread_id = thread_id or self.track.thread_id()
        if name == "open":
            self.open(thread_id)
            return False
        if not thread_id:
            self.pane.status_message.emit("No thread is selected.")
            return False
        handler = {"log-call": self._log_call, "note": self._note,
                   "deadline": self._deadline, "edit": self._edit,
                   "close": self._close}.get(name)
        if handler is None:
            return False
        wrote = handler(int(thread_id))
        if wrote:
            self.refresh()
        return wrote

    def _log_call(self, thread_id: int) -> bool:
        dialog = self._dialog("log-call")
        if dialog is None or not self._run(dialog):
            return False
        values = dialog.values()
        touches_repo.log_call(
            self.pane._con, thread_id, summary=values["summary"],
            channel=values.get("channel", touches_repo.CHANNEL_PHONE),
            direction=values.get("direction", touches_repo.DIRECTION_OUT),
            occurred_at=values.get("occurred_at", ""),
            body=values.get("body", ""))
        self.pane.status_message.emit(f"Logged: {values['summary']}")
        return True

    def _note(self, thread_id: int) -> bool:
        dialog = self._dialog("note")
        if dialog is None or not self._run(dialog):
            return False
        text = dialog.values().get("text", "").strip()
        if not text:
            return False
        touches_repo.add_note(self.pane._con, thread_id, text)
        self.pane.status_message.emit("Note added.")
        return True

    def _deadline(self, thread_id: int) -> bool:
        thread = tracking_repo.get_thread(self.pane._con, thread_id)
        dialog = self._dialog("deadline", thread=thread)
        if dialog is None or not self._run(dialog):
            return False
        values = dialog.values()
        tracking_repo.update_thread(self.pane._con, thread_id, **values)
        self.pane.status_message.emit(
            f"Deadline {values['deadline_date']}" if values["deadline_date"]
            else "Deadline removed.")
        return True

    def _edit(self, thread_id: int) -> bool:
        thread = tracking_repo.get_thread(self.pane._con, thread_id)
        dialog = self._dialog("thread", thread=thread)
        if dialog is None or not self._run(dialog):
            return False
        tracking_repo.update_thread(self.pane._con, thread_id,
                                    **dialog.values())
        return True

    def _close(self, thread_id: int) -> bool:
        """Closing is a STATE and never a delete.

        A closed thread is the record of a matter that was settled, and it is
        what the next enquiry from the same supplier is read against. Deleting
        it is available and is a different command with a different word.
        """
        tracking_repo.set_state(self.pane._con, thread_id,
                                tracking_repo.STATE_CLOSED)
        self.pane.status_message.emit("Thread closed.")
        return True

    # ------------------------------------------------- from the reading pane
    def _strip_action(self, name: str) -> None:
        strip = self.pane.reader.tracking
        if name == "track":
            self.track_message(self.pane.reader.message_id())
            return
        self.action(name, thread_id=strip.thread_id())
        strip.show_message(self.pane.reader.message_id())

    def track_message(self, message_id: int | None) -> int:
        """Make a thread from the message being read, and file its exchange.

        Returns the new thread's id, or 0 if the dialog was cancelled. The
        title comes from `subject_base`, so a thread is not called "Re: Re:
        Fwd: DWCNT" the first time anybody looks at it.
        """
        if not message_id:
            return 0
        row = self.pane._con.execute(
            "SELECT id, subject, subject_base, from_addr FROM message "
            "WHERE id = ?", (int(message_id),)).fetchone()
        if row is None:
            return 0
        dialog = self._dialog("thread", row=row)
        if dialog is None or not self._run(dialog):
            return 0
        values = dialog.values()
        thread_id = tracking_repo.create_thread(self.pane._con, **values)
        filed = attach_repo.attach_message(self.pane._con, thread_id,
                                           int(message_id))
        self.pane.status_message.emit(
            f"Tracking “{values['title']}” — {filed} message(s) filed")
        self.refresh()
        self.pane.reader.tracking.show_message(int(message_id))
        return thread_id

    def _file_from_queue(self, message_id: int, thread_id: int) -> None:
        """Take one out of the queue and put it on a thread, conversation and
        correspondent included — `store/attach.attach_message` says why filing
        one message alone leaves the other nine in the queue."""
        filed = attach_repo.attach_message(self.pane._con, thread_id,
                                           int(message_id))
        self.pane.status_message.emit(f"Filed {filed} message(s).")
        self.refresh()

    def _open_message(self, message_id: int) -> None:
        """A timeline row opens the message behind it, in a tab of its own.

        A tab rather than the tracking pane's own space: the point of clicking
        is to READ the thing, and the tracking pane is not a reading pane.
        """
        self.pane.open_in_tab.emit(int(message_id))

    # -------------------------------------------------------------- redrawing
    def refresh(self, *, today: dt.date | None = None) -> None:
        if self.showing:
            self.track.reload(today=today)
        strip = getattr(self.pane.reader, "tracking", None)
        if hasattr(strip, "show_message"):
            strip.show_message(self.pane.reader.message_id())

    def apply_theme(self, theme) -> None:
        self.track.set_theme(theme)

    # ------------------------------------------------------------ view state
    def state(self) -> int | None:
        """The thread id for the tab to remember, or None when not showing.

        Zero rather than None while showing with nothing selected, which is the
        convention `ViewState.calendar_id` already uses: None means "this tab
        is not tracking", and 0 means "tracking, no thread chosen".
        """
        if not self.showing:
            return None
        return self.track.thread_id() or 0

    def restore(self, state) -> None:
        self.open(state.thread_id or None)

    # --------------------------------------------------------------- dialogs
    def _dialog(self, name: str, **kwargs):
        maker = self._dialogs.get(name)
        if maker is not None:
            return maker(**kwargs)
        return self._default_dialog(name, **kwargs)

    def _default_dialog(self, name: str, **kwargs):
        from .threaddialog import (DeadlineDialog, LogCallDialog, NoteDialog,
                                   ThreadDialog)

        if name == "log-call":
            return LogCallDialog(self.pane)
        if name == "note":
            return NoteDialog(self.pane)
        if name == "deadline":
            return DeadlineDialog(self.pane, thread=kwargs.get("thread"))
        if name == "thread":
            tracks = tracking_repo.tracks(self.pane._con)
            row = kwargs.get("row")
            if row is not None:
                return ThreadDialog.from_message(self.pane, row, tracks=tracks)
            return ThreadDialog(self.pane, thread=kwargs.get("thread"),
                                tracks=tracks)
        return None                                          # pragma: no cover

    def _run(self, dialog) -> bool:
        runner = self._dialogs.get("run")
        if runner is not None:
            return bool(runner(dialog))
        return bool(dialog.exec())                           # pragma: no cover
