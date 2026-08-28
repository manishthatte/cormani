# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writing a tracked thread down, logging a call, and setting a deadline.
#
# Three dialogs in one file because they are three views of one object and
# would otherwise share a form class across three files. Each returns `values()`
# — a plain dict of exactly what the store takes — and none of them writes
# anything: `ui/actions.py` made the argument that one place decides what a
# command means, and it is what lets the suite drive these without a click,
# since Debian ships no QTest.
#
# ── A THREAD IS USUALLY MADE FROM A MESSAGE, NOT FROM A BLANK FORM ─────────
#
# `from_message` fills the title from the subject with its Re: and Fwd: already
# stripped, and the org from the sender's domain. A tracking layer whose only
# entry was an empty New Thread dialog is one nobody fills in — the moment a
# person knows a conversation matters is while they are reading it, and the
# dialog's job is to need one keystroke at that moment rather than eight.
#
# ── THE CADENCE IS OFFERED AND THE DEADLINE IS NOT ─────────────────────────
#
# Every thread gets a nudge cadence, defaulted, because it costs nothing and
# makes the board work without anybody typing a date. A DEADLINE is a separate
# dialog reached by a separate button, and that separation is the point:
# `store/trackschema.py` keeps them in two columns because no amount of polite
# reminding satisfies a statutory date, and an interface that offered them in
# one form with two date fields would invite exactly that confusion.
#
# ── A LOGGED CALL DEFAULTS TO NOW AND TO OUTBOUND ──────────────────────────
#
# Because the overwhelmingly common case is "I have just rung them". Both are
# changeable; neither should have to be.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QDate, QDateTime
from PySide6.QtWidgets import (QComboBox, QDateEdit, QDateTimeEdit, QDialog,
                               QDialogButtonBox, QFormLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QVBoxLayout)

from ..store import times
from ..store import touches as touches_repo
from ..store import tracking as tracking_repo

# The cadences offered. Days, and a label that says what the number means —
# "7" alone in a box beside the word "cadence" is a number nobody can judge.
CADENCES = ((3, "Chase after 3 days"), (7, "Chase after a week"),
            (14, "Chase after a fortnight"), (30, "Chase after a month"),
            (90, "Chase after three months"), (0, "Never chase"))

PRIORITIES = ((1, "1 — highest"), (2, "2"), (3, "3 — normal"), (4, "4"),
              (5, "5 — lowest"))


def _domain_org(address: str) -> str:
    """An organisation's name, guessed from an address, for the form to offer.

    A GUESS, and it is in a field the person is looking at rather than written
    anywhere: "covalent.example.com" becomes "Covalent Example", which is
    right often enough to save typing and obviously wrong when it is not.
    Nothing downstream depends on it.
    """
    domain = (address or "").split("@")[-1].strip().lower()
    if not domain or "." not in domain:
        return ""
    name = domain.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    return " ".join(word.capitalize() for word in name.replace("-", " ").split())


class ThreadDialog(QDialog):
    """New thread, or edit one."""

    def __init__(self, parent=None, *, thread=None, title: str = "",
                 org: str = "", tracks=()) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit thread" if thread else "Track this")
        self._thread = thread

        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)

        self.title = QLineEdit(thread.title if thread else title, self)
        form.addRow("Title", self.title)
        self.org = QLineEdit(thread.org if thread else org, self)
        form.addRow("Organisation", self.org)

        self.track = QComboBox(self)
        self.track.setEditable(True)          # free text over a seed list
        for name in (tracks or tracking_repo.SEED_TRACKS):
            self.track.addItem(name)
        self.track.setCurrentText(thread.track if thread
                                  else tracking_repo.DEFAULT_TRACK)
        form.addRow("Track", self.track)

        self.state = QComboBox(self)
        for name in tracking_repo.STATES:
            self.state.addItem(name)
        self.state.setCurrentText(thread.state if thread
                                  else tracking_repo.STATE_OPEN)
        form.addRow("State", self.state)

        self.priority = QComboBox(self)
        for value, label in PRIORITIES:
            self.priority.addItem(label, value)
        self.priority.setCurrentIndex(
            [v for v, _ in PRIORITIES].index(thread.priority if thread else 3))
        form.addRow("Priority", self.priority)

        self.cadence = QComboBox(self)
        for value, label in CADENCES:
            self.cadence.addItem(label, value)
        self._select_cadence(thread.cadence_days if thread
                             else tracking_repo.DEFAULT_CADENCE_DAYS)
        form.addRow("Nudge", self.cadence)

        self.next_action = QLineEdit(thread.next_action if thread else "", self)
        self.next_action.setPlaceholderText("What has to happen next")
        form.addRow("Next action", self.next_action)

        self.note = QPlainTextEdit(thread.note if thread else "", self)
        self.note.setPlaceholderText("Anything worth remembering")
        form.addRow("Note", self.note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.title.setFocus()

    def _select_cadence(self, days: int) -> None:
        """Pick the offered cadence, or add the one that is already stored.

        A thread whose cadence was set elsewhere — imported, or edited by hand
        — must not have it silently changed to seven by being opened.
        """
        values = [v for v, _ in CADENCES]
        if days not in values:
            self.cadence.addItem(f"Chase after {days} days", days)
            values.append(days)
        self.cadence.setCurrentIndex(values.index(days))

    @classmethod
    def from_message(cls, parent, row, *, tracks=()) -> "ThreadDialog":
        """A dialog filled in from the message being read.

        `subject_base` and not `subject`: the prefixes are already off it, and
        a thread called "Re: Re: Fwd: DWCNT" is one somebody renames by hand
        the first time they see it.
        """
        subject = (row["subject_base"] or row["subject"] or "").strip()
        return cls(parent, title=subject,
                   org=_domain_org(row["from_addr"] or ""), tracks=tracks)

    def values(self) -> dict:
        return {"title": self.title.text().strip() or "Untitled",
                "org": self.org.text().strip(),
                "track": self.track.currentText().strip()
                or tracking_repo.DEFAULT_TRACK,
                "state": self.state.currentText(),
                "priority": int(self.priority.currentData()),
                "cadence_days": int(self.cadence.currentData()),
                "next_action": self.next_action.text().strip(),
                "note": self.note.toPlainText().strip()}


class LogCallDialog(QDialog):
    """A telephone call, or anything else that left no trace.

    The channel is a choice and not a constant: WhatsApp, LinkedIn and a
    conversation in a corridor are all things that happened and none of them is
    in a mailbox. Stage 7's panels will add their own to the same list.
    """

    def __init__(self, parent=None, *, now: dt.datetime | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Log a call")

        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)

        self.summary = QLineEdit(self)
        self.summary.setPlaceholderText("What was said, in one line")
        form.addRow("Summary", self.summary)

        self.channel = QComboBox(self)
        self.channel.setEditable(True)
        for name in touches_repo.SEED_CHANNELS:
            if name != touches_repo.CHANNEL_EMAIL:
                self.channel.addItem(name)
        self.channel.setCurrentText(touches_repo.CHANNEL_PHONE)
        form.addRow("Channel", self.channel)

        self.direction = QComboBox(self)
        # Outbound first: the common case is "I have just rung them".
        for value, label in ((touches_repo.DIRECTION_OUT, "I contacted them"),
                             (touches_repo.DIRECTION_IN, "They contacted me")):
            self.direction.addItem(label, value)
        form.addRow("Direction", self.direction)

        self.when = QDateTimeEdit(self)
        self.when.setCalendarPopup(True)
        moment = now or times.now_local()
        self.when.setDateTime(QDateTime(
            QDate(moment.year, moment.month, moment.day),
            self.when.time().fromString(moment.strftime("%H:%M"), "HH:mm")))
        form.addRow("When", self.when)

        self.body = QPlainTextEdit(self)
        self.body.setPlaceholderText("Anything longer")
        form.addRow("Detail", self.body)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.summary.setFocus()

    def values(self) -> dict:
        moment = self.when.dateTime().toPython()
        return {"summary": self.summary.text().strip() or "Call",
                "channel": self.channel.currentText().strip()
                or touches_repo.CHANNEL_PHONE,
                "direction": self.direction.currentData(),
                "occurred_at": times.to_utc_text(times.aware(moment)),
                "body": self.body.toPlainText().strip()}


class DeadlineDialog(QDialog):
    """A date that cannot slip, and why.

    THE NOTE IS NOT OPTIONAL IN SPIRIT, which is why the dialog asks for it
    above the date: a deadline with no reason is one that gets moved when it is
    inconvenient, and six months later nobody remembers whether "12 Oct" was a
    filing date or a preference.
    """

    def __init__(self, parent=None, *, thread=None,
                 today: dt.date | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set a deadline")
        today = today or times.now_local().date()

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(
            "A deadline is not a nudge. Nothing satisfies it but the thing "
            "being done.", self))
        form = QFormLayout()
        outer.addLayout(form)

        self.note = QLineEdit(thread.deadline_note if thread else "", self)
        self.note.setPlaceholderText("What must be done by then")
        form.addRow("What", self.note)

        self.date = QDateEdit(self)
        self.date.setCalendarPopup(True)
        existing = (times.parse_date(thread.deadline_date)
                    if thread and thread.deadline_date else None)
        chosen = existing or (today + dt.timedelta(days=14))
        self.date.setDate(QDate(chosen.year, chosen.month, chosen.day))
        form.addRow("By", self.date)

        self.clear = QComboBox(self)
        self.clear.addItem("Set this deadline", False)
        self.clear.addItem("Remove the deadline", True)
        form.addRow("", self.clear)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.note.setFocus()

    def values(self) -> dict:
        """Empty strings when removing, which is what the store takes for
        "there is no deadline" — a NULL and an empty string would be two ways
        of saying it, and `Thread.deadline()` would have to know both."""
        if bool(self.clear.currentData()):
            return {"deadline_date": "", "deadline_note": ""}
        return {"deadline_date": self.date.date().toPython().isoformat(),
                "deadline_note": self.note.text().strip()}


class NoteDialog(QDialog):
    """A note on the timeline. Direction `note`, so it answers nobody.

    Separate from `LogCallDialog` although both write a touch, because they are
    different acts: a call is something that HAPPENED with somebody, and a note
    is something the user thought. `store/touches.add_note` keeps them apart in
    the store for the reason that matters — a note must never discharge what is
    owed.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a note")
        outer = QVBoxLayout(self)
        self.text = QPlainTextEdit(self)
        self.text.setPlaceholderText("A note to yourself about this thread")
        outer.addWidget(self.text)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.text.setFocus()

    def values(self) -> dict:
        return {"text": self.text.toPlainText().strip()}
