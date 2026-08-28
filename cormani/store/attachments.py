# SPDX-License-Identifier: GPL-3.0-or-later
#
# Getting an attachment back out again.
#
# `store/ingest.py` is the writer's side — the only place an attachment's bytes
# reach the disk. This is the reader's side, and the pair is deliberate: the
# rules about where those bytes may live are stated once, in ingest, and every
# way back out goes through the check in `stored_file` below.
#
# THE STORED NAME AND THE SHOWN NAME ARE DIFFERENT THINGS, and conflating them
# is how a message names a file on someone's disk. On disk the part is
# `3-invoice.pdf` under a directory made of integers, because `ingest` put it
# there through `safe_filename`. To a person it is `invoice.pdf`. Everything
# written out of here is named from the SHOWN name put back through
# `safe_filename` — never from the stored path, whose index prefix is an
# implementation detail, and never from the raw header, which is a stranger's
# string.
#
# OPENING WORKS ON A COPY, NOT ON THE STORE'S FILE. Three reasons, in order of
# how much they cost when ignored: the application the desktop chooses may
# write back, and the store's copy is the archive; a re-sync of the message
# deletes and rewrites that file under an open editor; and the stored name
# carries the index prefix, which would then be what the user sees in a title
# bar and in a Save As of their own. The copy goes to CACHE, because it is
# reproducible from the store and must not be backed up.
#
# NOTHING WRITTEN HERE IS EXECUTABLE. `shutil.copyfile` copies contents and not
# the mode, and the cache copy is narrowed to 0600 besides. That is not the
# whole of the problem — `xdg-open` on a `.desktop` file RUNS it, whatever its
# permission bits say — so `is_risky` names the suffixes for which the
# interface must ask first. It is a short list of what the desktop will
# execute, and it is not a virus scanner; it is the question a person should be
# asked before a stranger's file is handed to a program.
#
# AN EXISTING FILE IS NEVER OVERWRITTEN when the name came from the message.
# `save_all` puts twenty parts into a directory the user chose, and two of them
# being called `image001.png` is ordinary; losing one to the other is not.
# `save_as` does overwrite, because there the user typed the name into a dialog
# that asked them about it.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import mimetypes
import os
import shutil
from pathlib import Path

from .ingest import safe_filename

# What a desktop will execute if it is asked to open it. `.desktop` is the one
# that surprises people: it is a text file, it looks inert, and `xdg-open` runs
# its Exec line. The Windows entries are here rather than under a platform test
# because a message is not written for the machine that receives it, and a
# `.scr` arriving on Debian is still worth a question if the store is ever
# opened on Windows.
RISKY_SUFFIXES = frozenset({
    ".desktop", ".sh", ".bash", ".zsh", ".run", ".appimage", ".bin",
    ".py", ".pl", ".rb", ".php", ".jar", ".class",
    ".exe", ".com", ".bat", ".cmd", ".msi", ".scr", ".pif", ".cpl",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1",
    ".lnk", ".reg", ".hta", ".chm",
})

# Where the numbering stops when a directory already holds the name. A message
# with more than this many identically-named parts is not a case to solve
# quietly; it is one to report.
_MAX_DUPLICATES = 999


class AttachmentMissing(Exception):
    """The row is there and the bytes are not. The message is fit to show."""


class AttachmentEscapes(Exception):
    """A stored path that is not under the attachments root. See `stored_file`."""


# ------------------------------------------------------------------- naming
def display_name(row, *, index: int = 0) -> str:
    """What to call this part to a person, and to name a file written out.

    A part with no filename is normal — an inline image referenced only by
    Content-ID usually has none, and so does a bare `application/octet-stream`
    from a program that did not bother. Rather than showing an empty chip, the
    part is named for its position and given the extension its declared type
    implies, which is also what lets the desktop choose a handler for it.
    """
    raw = (row["filename"] or "").strip()
    if raw:
        return safe_filename(raw, fallback=f"part-{index or 1}")

    stem = f"part-{index or 1}"
    suffix = _suffix_for_type(row["content_type"])
    return safe_filename(stem + suffix, fallback=stem)


def _suffix_for_type(content_type: str) -> str:
    """`.png` from `image/png`, and "" when nothing is claimed or known.

    `mimetypes.guess_extension` answers `.jpe` for `image/jpeg` on some
    versions of Python, which is correct and unhelpful; the three types that
    actually arrive in mail are spelled out so the answer is the one a person
    expects to see.
    """
    kind = (content_type or "").split(";")[0].strip().lower()
    known = {"image/jpeg": ".jpg", "text/plain": ".txt",
             "message/rfc822": ".eml", "application/octet-stream": ""}
    if kind in known:
        return known[kind]
    if not kind:
        return ""
    return mimetypes.guess_extension(kind) or ""


def is_risky(name: str) -> bool:
    """Whether opening this would ask the desktop to run something.

    The suffix is taken from the NAME, not from the declared content type: the
    type is the sender's claim and the suffix is what the desktop dispatches
    on, so the suffix is the one that decides what happens.
    """
    return Path(name or "").suffix.lower() in RISKY_SUFFIXES


def human_size(count: int) -> str:
    """The size as the interface says it. Here rather than in a widget so the
    strip, a tooltip and a future message-properties dialog cannot disagree."""
    count = int(count or 0)
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


# ------------------------------------------------------------------ reading
def stored_file(row, root: Path | str) -> Path:
    """The file holding this part's bytes, proved to be inside `root`.

    `ingest.attachment_path` already refused to write anywhere else. This is
    the check that the ROW still says so — a store copied from elsewhere, a
    row edited by hand, or a symbolic link planted in the attachments
    directory. Resolution happens before the comparison, so a link cannot
    point out of the tree and be accepted for where it sits rather than for
    where it goes. `ui/messageview.loadResource` makes the same check for the
    same reason; neither is the other's substitute, because they guard
    different doors.
    """
    stored = (row["stored_path"] or "").strip()
    if not stored:
        raise AttachmentMissing(
            "this part was never downloaded — re-sync the account to fetch it")

    root = Path(root).resolve()
    try:
        path = Path(stored).resolve()
    except OSError as exc:                                   # pragma: no cover
        raise AttachmentMissing(f"could not resolve {stored}: {exc}") from exc
    if path != root and root not in path.parents:
        raise AttachmentEscapes(
            f"attachment path escapes the store: {path} is not under {root}")
    if not path.is_file():
        raise AttachmentMissing(f"the stored copy is gone: {path.name}")
    return path


# ------------------------------------------------------------------ writing
def unique_path(directory: Path | str, name: str) -> Path:
    """`report.pdf`, then `report (2).pdf`, then `report (3).pdf`.

    Contained: the name is put through `safe_filename` first, so a part called
    `../../.bashrc` becomes `.._.._.bashrc` in the directory the user chose and
    not a file above it, and the join is checked afterwards anyway.
    """
    directory = Path(directory).resolve()
    name = safe_filename(name, fallback="attachment")
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""

    for n in range(1, _MAX_DUPLICATES + 1):
        tail = name if n == 1 else f"{stem} ({n}){'.' + suffix if suffix else ''}"
        candidate = (directory / tail).resolve()
        if candidate.parent != directory:
            raise AttachmentEscapes(
                f"save target escapes the chosen directory: {candidate}")
        if not candidate.exists():
            return candidate
    raise AttachmentEscapes(
        f"{directory} already holds {_MAX_DUPLICATES} files called {name}")


def save_as(row, root: Path | str, target: Path | str) -> Path:
    """Write one part to the exact path the user chose in a dialog.

    Overwrites, because the dialog already asked. Contents only — `copyfile`
    does not carry a mode across, so nothing written here can arrive
    executable.
    """
    source = stored_file(row, root)
    target = Path(target).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def save_all(rows, root: Path | str, directory: Path | str) -> list[Path]:
    """Write every non-inline part into a directory the user chose.

    Inline parts are left out for the same reason the strip does not list them:
    a signature logo is part of the message, not a file the person asked for.
    A part whose bytes are missing is SKIPPED rather than failing the rest —
    the caller counts what came back and says so.
    """
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, row in enumerate(rows or (), start=1):
        if row["is_inline"]:
            continue
        try:
            source = stored_file(row, root)
        except AttachmentMissing:
            continue
        target = unique_path(directory, display_name(row, index=index))
        shutil.copyfile(source, target)
        written.append(target)
    return written


def copy_for_opening(row, root: Path | str, cache_dir: Path | str, *,
                     index: int = 0) -> Path:
    """A copy under CACHE, named as a person would name it, ready to open.

    Keyed on the attachment's own id, which is unique across the store, so two
    messages carrying `invoice.pdf` do not open each other's. Rewritten every
    time rather than reused: if the previous copy was edited by whatever opened
    it, the store's copy is the one that is right.
    """
    source = stored_file(row, root)
    holder = Path(cache_dir).expanduser() / str(int(row["id"]))
    holder.mkdir(parents=True, exist_ok=True)
    os.chmod(holder, 0o700)

    target = holder / display_name(row, index=index)
    if target.parent.resolve() != holder.resolve():           # pragma: no cover
        raise AttachmentEscapes(f"open target escapes the cache: {target}")
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
    return target
