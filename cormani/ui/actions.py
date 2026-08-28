# SPDX-License-Identifier: GPL-3.0-or-later
#
# What a command means.
#
# ONE PLACE DECIDES, and this is it. Archive arrives from a hover button, from
# the bar above the reading pane, from the Message menu and from the `a` key,
# and all four call `run`. The alternative is four implementations that differ
# in whether they refresh the rail — which is not a hypothetical: the command
# bar reached nothing at all for two stages precisely because it had a path of
# its own.
#
# THE STATUS LINE IS EARNED, NOT DECORATIVE. Every branch here reports what
# actually happened, including the awkward case: archiving four messages when
# one account has no Archive folder says so, and names the account, rather than
# reporting four. CONVENTIONS.txt §8.
#
# EVERY DESTRUCTIVE COMMAND RECORDS HOW TO TAKE IT BACK, before it does it —
# `store/undo.py` captures the state that is about to be destroyed, and this is
# the only place that captures it, for the same reason this is the only place
# that decides what a command means. An action that reached the store by another
# path would be an action nobody could undo.
#
# IT HOLDS THE PANE RATHER THAN BEING PART OF IT. An action needs the store, the
# model, the list's cursor and the rail's counts — the whole pane, in other
# words — but deciding what a command means is not the same job as arranging
# four widgets, and the file that did both was the one the 600-line rule caught.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from ..calendar import invites
from ..compose import quote as quote_mod
from ..smtp import outbox as outbox_repo
from ..store import accounts as accounts_repo
from ..store import drafts as drafts_repo
from ..store import edits as edits_repo
from ..store import messages as messages_repo
from ..store import tags as tags_repo
from ..store import undo as undo_repo
from ..store import views as views_repo
from . import composer as composer_mod
from . import commands as commands_mod
from . import printmessage as printmessage_mod
from . import shortcuts as shortcuts_mod
from . import snoozedialog as snoozedialog_mod

# How far back undo reaches. A session's worth of triage rather than a history:
# what a person wants back is the thing they just did and, occasionally, the two
# before it. store/undo.py argues why there is no redo.
UNDO_DEPTH = 20

# Commands whose implementation belongs to a later stage. Named here rather
# than scattered, so the interface says the same thing wherever one is reached
# from — a hover button, the command bar or a key.
_NOT_READY: dict[str, str] = {}

# Commands that open a window rather than changing a message. They take the
# CURRENT row rather than the selection: a reply is to one message, and a reply
# to four at once is not a thing.
_COMPOSING = ("reply", "reply_all", "forward")


class Actions:
    """The dispatch, holding the pane it acts on."""

    def __init__(self, pane) -> None:
        self.pane = pane
        # In memory, and gone when the window closes. An undo stack in the
        # store would survive a restart and mean offering to take back
        # something the server was told about days ago.
        self.stack: list[undo_repo.Step] = []
        # Open composer windows. A modeless QDialog with nothing referring to it
        # is collected the moment the method that made it returns, and the user
        # watches it appear and vanish.
        self.composers: list = []

    def run(self, action_id: str, message_ids: list[int]) -> None:
        if action_id == "compose":
            action_id = "new"
        if action_id in _NOT_READY:
            self.pane.status_message.emit(_NOT_READY[action_id])
            return
        if not commands_mod.command_ready(action_id):
            self.pane.status_message.emit(
                commands_mod.command_not_ready_message(action_id))
            return
        if action_id == "print":
            self._print(message_ids)
            return
        if action_id == "export_pdf":
            self._export_pdf(message_ids)
            return
        if action_id == "snooze":
            if not message_ids:
                self.pane.status_message.emit("No message selected")
                return
            self._snooze(message_ids)
            return
        if action_id in _COMPOSING:
            self.pane.compose(action_id, message_ids[0] if message_ids else None)
            return
        if not message_ids:
            self.pane.status_message.emit("No message selected")
            return

        tag_key = shortcuts_mod.tag_shortcut_key(action_id)
        if tag_key is not None:
            self._apply_tag(tag_key, message_ids)
            return

        rows = [r for r in (messages_repo.get_row(self.pane._con, m) for m in message_ids)
                if r is not None]
        if not rows:
            return
        fallback = self.pane.list.cursor_place()

        if action_id == "mark_read":
            seen = not rows[0].seen
            label = f"Marked {len(message_ids)} {'read' if seen else 'unread'}"
            self._record(undo_repo.capture_flag(
                self.pane._con, message_ids, "seen", label))
            edits_repo.set_seen(self.pane._con, message_ids, seen)
            self.pane.status_message.emit(label)
        elif action_id == "flag":
            flagged = not rows[0].flagged
            label = f"{'Flagged' if flagged else 'Unflagged'} {len(message_ids)}"
            self._record(undo_repo.capture_flag(
                self.pane._con, message_ids, "flagged", label))
            edits_repo.set_flagged(self.pane._con, message_ids, flagged)
            self.pane.status_message.emit(label)
        elif action_id == "archive":
            self._move(message_ids, "archive")
        elif action_id == "delete":
            self._move(message_ids, "delete")
        else:
            self.pane.status_message.emit(f"No such command: {action_id}")
            return

        self._after(message_ids, fallback)

    def _move(self, message_ids: list[int], kind: str) -> None:
        mover = edits_repo.archive if kind == "archive" else edits_repo.trash
        verb = "Archived" if kind == "archive" else "Deleted"
        # BEFORE the move, and of the state rather than of the intent: an
        # account with no Archive folder archives nothing, and a step built
        # from what was asked for would try to reverse a move that never was.
        step = undo_repo.capture_move(self.pane._con, message_ids, verb)
        moved, skipped = mover(self.pane._con, message_ids)
        if moved:
            self._record(step)
        if skipped:
            # Named, not swallowed. An account with no archive folder is a real
            # configuration and the user needs to know which one it was.
            names = sorted({r.account_label for r in
                            (messages_repo.get_row(self.pane._con, m) for m in skipped)
                            if r is not None})
            self.pane.status_message.emit(
                f"{verb} {moved}; {len(skipped)} could not be moved — "
                f"no {'archive' if kind == 'archive' else 'trash'} folder for "
                f"{', '.join(names)}")
        else:
            self.pane.status_message.emit(f"{verb} {moved}")

    def _snooze(self, message_ids: list[int], fallback) -> None:
        until = snoozedialog_mod.ask(self.pane)
        if not until:
            return
        edits_repo.snooze(self.pane._con, message_ids, until)
        when = until.replace("T", " ").replace("+00:00", " UTC")
        self.pane.status_message.emit(
            f"Snoozed {len(message_ids)} until {when}")
        self._after(message_ids, fallback)

    def _apply_tag(self, key: int, message_ids: list[int]) -> None:
        tag = tags_repo.by_shortcut(self.pane._con, key)
        if tag is None:
            self.pane.status_message.emit(f"No tag on key {key}")
            return
        self.apply_tag_id(tag.id, message_ids)

    def apply_tag_id(self, tag_id: int, message_ids: list[int]) -> None:
        """The same act reached from the Tags menu, where a tag needs no key.

        The keys are the fast path and not the only one: a tag beyond the ninth,
        or one the user has not given a key, is still a tag.
        """
        tag = tags_repo.get_tag(self.pane._con, tag_id)
        if tag is None or not message_ids:
            self.pane.status_message.emit(
                "No message selected" if tag is not None else "No such tag")
            return
        step = undo_repo.capture_tag(self.pane._con, message_ids, tag.id, "")
        applied = tags_repo.toggle(self.pane._con, message_ids, tag.id)
        label = (f"{'Tagged' if applied else 'Untagged'} {len(message_ids)}"
                 f" — {tag.name}")
        self._record(undo_repo.Step(label=label, kind=step.kind,
                                    tag_id=step.tag_id, before=step.before))
        self.pane.status_message.emit(label)
        self._after(message_ids, self.pane.list.cursor_place())

    def _after(self, message_ids: list[int], fallback) -> None:
        self.pane._forget_discarded()
        before = self.pane.model.rowCount()
        self.pane.model.apply_change(message_ids)
        self.pane.rail.refresh_counts()
        if not self.pane.list.currentIndex().isValid() or self.pane.model.rowCount() < before:
            # Rows left the view: put the cursor where they were, which is what
            # makes archiving a run of messages one key press each.
            self.pane.list.select(self.pane.list.index_near(*fallback))
        else:
            self.pane._selection_repaint()

    # --------------------------------------------------------------- writing
    def compose(self, kind: str, message_id: int | None = None,
                prefill: str = "", to: str = ""):
        """Open a composer. `kind` is reply, reply_all, forward or new.

        `to` addresses a NEW message and is ignored by the other three, whose
        recipients are derived from the message being answered — a reply whose
        To could be overridden from a caller would be a reply to somebody else.

        The pane KEEPS the window: a modeless QDialog with nothing referring to
        it is collected the moment this method returns, and the user watches it
        appear and vanish. `_composers` is that reference, and the finished
        signal is what lets it go.
        """
        row = (messages_repo.get_row(self.pane._con, message_id)
               if message_id is not None else self.pane.current_row())
        if kind != "new" and row is None:
            self.pane.status_message.emit("No message selected")
            return None
        body = messages_repo.body_of(self.pane._con, row.id) if row is not None else ""

        if kind == "forward":
            parts = [(a["stored_path"], a["filename"], a["content_type"])
                     for a in messages_repo.attachments_of(self.pane._con, row.id)
                     if not a["is_inline"] and a["stored_path"]]
            window = composer_mod.for_forward(self.pane._con, row, body,
                                              attachments=parts, parent=self.pane)
        elif kind == "new":
            window = composer_mod.for_new(self.pane._con, self._account_for_new(),
                                          parent=self.pane, to=to)
        else:
            window = composer_mod.for_reply(
                self.pane._con, row, body, all_recipients=(kind == "reply_all"),
                parent=self.pane)

        if prefill:
            # Whatever was typed in the inline box, above the quotation the
            # derivation put there. The composer is taking over, not starting
            # again.
            window.body.setPlainText(
                f"{prefill}\n{window.body.toPlainText()}")
        window.status_message.connect(self.pane.status_message)
        window.queued.connect(self._queued)
        window.saved.connect(lambda _id: self.pane.reload())
        window.finished.connect(lambda _r, w=window: self._forget_composer(w))
        self.composers.append(window)
        window.show()
        return window

    def inline_reply(self, text: str) -> None:
        """Send a one-line answer without opening anything.

        The SAME derivation the composer uses — recipients, subject, chain and
        signature all come from compose/quote.py — with the typed text above
        the quotation. A second idea of what a reply is would be a second set
        of bugs about who it goes to.
        """
        row = self.pane.reader.inline.message
        if row is None or not text.strip():
            return
        identities = accounts_repo.list_identities(self.pane._con, row.account_id)
        draft = quote_mod.reply(
            row, messages_repo.body_of(self.pane._con, row.id), identities,
            mine=accounts_repo.list_identity_addresses(self.pane._con),
            signature=_signature_of(identities, row))
        draft = draft.with_changes(body=f"{text.strip()}\n{draft.body}")
        row_id, _rfc = drafts_repo.save(self.pane._con, draft)
        outbox_repo.queue(self.pane._con, row_id)
        self.pane.reader.inline.clear()
        self.pane.status_message.emit(
            f"Queued — your reply to {row.correspondent} will go out with the "
            f"next sync")
        self._queued(row_id)

    def _forget_composer(self, window) -> None:
        if window in self.composers:
            self.composers.remove(window)

    def _queued(self, message_id: int) -> None:
        self.pane.reload()
        self.pane.outbox_changed.emit()

    def _account_for_new(self) -> int:
        """Which account a message written from nothing comes from.

        The one being looked at, and the first visible one otherwise. Not a
        preference: the account whose mail is on the screen is the account the
        user is thinking in.
        """
        scope = self.pane.model.scope
        if scope.account_id is not None:
            return int(scope.account_id)
        row = self.pane.current_row()
        if row is not None:
            return int(row.account_id)
        visible = views_repo.visible_account_ids(self.pane._con)
        return int(visible[0]) if visible else 0

    # ------------------------------------------------------------------ undo
    # ------------------------------------------------------------ invitations
    def invitation_on(self, message_id: int):
        """The invitation this message carries, if it carries one.

        Parsed on selection rather than at ingest — `calendar/invites.py`
        argues why — and never allowed to stop a message being read: a
        malformed `text/calendar` part is somebody else's bug and must not
        blank the reading pane.
        """
        try:
            return invites.find(self.pane._con, message_id,
                                self.pane._attachments_root)
        except Exception:                                    # pragma: no cover
            return None

    def answer_invitation(self, response: str) -> None:
        """Accept, Maybe or Decline, whichever route this account has.

        `calendar/invites.py` chooses between the provider's API and an iTIP
        reply through the outbox, and the sentence it returns says which — so
        this reports that rather than a word of its own.
        """
        found = self.pane.reader.invitation.found()
        if found is None:
            return
        answer = invites.answer(self.pane._con, found, response,
                                root=self.pane._attachments_root)
        self.pane.status_message.emit(answer.detail)
        if answer.kind == invites.ANSWER_MAIL:
            self.pane.outbox_changed.emit()
        # Redrawn FROM THE STORE, so that what the bar shows is what was
        # written — the same rule the command bar and the inline reply were
        # each fixed by.
        self.pane.reader.invitation.set_invitation(
            self.invitation_on(found.message_id))
        if self.pane.showing_calendar():
            self.pane.calendar.reload()

    def dismiss_invitation(self) -> None:
        found = self.pane.reader.invitation.found()
        if found is None:
            return
        removed = invites.cancelled_events(self.pane._con, found)
        self.pane.status_message.emit(
            "Taken out of the calendar" if removed else
            "That meeting is not in a calendar corMani syncs")
        if self.pane.showing_calendar():
            self.pane.calendar.reload()

    def _record(self, step: undo_repo.Step) -> None:
        if not step.before:
            return
        self.stack.append(step)
        del self.stack[:-UNDO_DEPTH]

    @property
    def undoable(self) -> str:
        """What the next undo would take back, for the menu entry's label."""
        return self.stack[-1].label if self.stack else ""

    def undo(self) -> bool:
        """Take back the last destructive thing. Returns whether there was one."""
        if not self.stack:
            self.pane.status_message.emit("Nothing to undo")
            return False
        step = self.stack.pop()
        touched = [message_id for message_id, _ in step.before]
        undo_repo.reverse(self.pane._con, step)
        # The rows may be coming BACK into the view, which `apply_change` cannot
        # do — it updates and removes what the model already holds. A refresh is
        # the honest answer, and undo is rare enough to afford one.
        self.pane.model.refresh()
        self.pane.rail.refresh_counts()
        self.pane._forget_discarded()
        if touched:
            self.pane.list.select_message(touched[0])
        self.pane.status_message.emit(f"Undone — {step.label.lower()}")
        return True

    def _print(self, message_ids: list[int]) -> None:
        if not message_ids:
            self.pane.status_message.emit("No message selected")
            return
        row = messages_repo.get_row(self.pane._con, message_ids[0])
        if row is None:
            return
        plain, html = messages_repo.bodies_of(self.pane._con, row.id)
        from .reader import format_full_date
        printed = printmessage_mod.print_message(
            subject=row.subject_label,
            correspondent=row.correspondent,
            when=format_full_date(row.date_at),
            body_html=html or "",
            body_plain=plain or "",
            parent=self.pane)
        if printed:
            self.pane.status_message.emit("Sent to the printer")

    def _export_pdf(self, message_ids: list[int]) -> None:
        if not message_ids:
            self.pane.status_message.emit("No message selected")
            return
        row = messages_repo.get_row(self.pane._con, message_ids[0])
        if row is None:
            return
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            self.pane, "Export as PDF", f"{row.subject_label[:40] or 'message'}.pdf",
            "PDF files (*.pdf);;All files (*)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        plain, html = messages_repo.bodies_of(self.pane._con, row.id)
        from .reader import format_full_date
        printmessage_mod.export_pdf(
            subject=row.subject_label,
            correspondent=row.correspondent,
            when=format_full_date(row.date_at),
            body_html=html or "",
            body_plain=plain or "",
            path=path,
            parent=self.pane)
        self.pane.status_message.emit(f"Saved to {path}")

    def _snooze(self, message_ids: list[int]) -> None:
        if not message_ids:
            self.pane.status_message.emit("No message selected")
            return
        if not commands_mod.command_ready("snooze"):
            self.pane.status_message.emit(
                commands_mod.command_not_ready_message("snooze"))
            return
        from . import snoozedialog as snoozedialog_mod
        until = snoozedialog_mod.ask(self.pane)
        if not until:
            return
        edits_repo.snooze(self.pane._con, message_ids, until)
        self.pane.status_message.emit(f"Snoozed {len(message_ids)}")
        self._after(message_ids, self.pane.list.cursor_place())


def _signature_of(identities, row) -> str:
    """The signature of the identity a reply to this message comes from."""
    identity = quote_mod._identity_for_reply(list(identities), row)
    return identity.signature if identity else ""
