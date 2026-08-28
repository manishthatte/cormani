# SPDX-License-Identifier: GPL-3.0-or-later
#
# Finding Thunderbird's mail folders on disk.
#
# A Thunderbird profile keeps IMAP accounts under `ImapMail/<server>/` and
# local folders under `Mail/`. Each folder is an mbox file beside a `.msf`
# summary; directories ending `.sbd` hold children. None of that layout is
# documented as a stable API, and none of it is written here — only walked.
#
# THE ACCOUNT NAME ON DISK IS NOT AN EMAIL ADDRESS. `imap.gmail.com` and
# `imap.gmail-1.com` are server directories Thunderbird minted; which address
# each holds is in `prefs.js`. The importer does not parse prefs: the caller
# names the corMani account with `--into`, and this module only finds the
# files. Guessing the mapping from a hostname would silently put one person's
# mail under another's account.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import re
from pathlib import Path

from ..store import folders as folders_repo

# Folders a first import usually skips. Junk and Trash are recoverable from
# the server on the next sync; importing them once doubles the noise in the
# rail for no lasting gain. Named rather than hard-coded so a caller that
# wants everything can pass include_junk=True.
_SKIP = re.compile(r"(^|/)(spam|junk|trash|deleted|deleted items)(/|$)", re.I)

_SENT = re.compile(r"(^|/)(sent|sent items|sent mail)(/|$)", re.I)
_DRAFTS = re.compile(r"(^|/)(drafts)(/|$)", re.I)
_ARCHIVE = re.compile(r"(^|/)(archive|all mail)(/|$)", re.I)
_INBOX = re.compile(r"(^|/)(inbox)(/|$)", re.I)


def discover_mail_roots(home: Path | None = None) -> list[Path]:
    """Every Thunderbird ImapMail/Mail directory under the usual profiles."""
    home = Path(home or Path.home())
    out: list[Path] = []
    for pattern in (".thunderbird/*/ImapMail", ".thunderbird/*/Mail",
                    ".mozilla-thunderbird/*/ImapMail",
                    ".mozilla-thunderbird/*/Mail"):
        out.extend(sorted(p for p in home.glob(pattern) if p.is_dir()))
    return out


def resolve_roots(path: str | Path | None, *, home: Path | None = None) -> list[Path]:
    """What the user named, widened to the mail roots it contains.

    A profile directory, an ImapMail directory, a single account directory, a
    single mbox file, or nothing (discover) all become a list of directories to
    walk — or, for a bare mbox, a one-file synthetic root handled by
    `folder_files`.
    """
    if path is None or str(path).strip() == "":
        return discover_mail_roots(home)
    target = Path(path).expanduser().resolve()
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    # A profile: look inside for ImapMail / Mail.
    nested = [target / name for name in ("ImapMail", "Mail") if (target / name).is_dir()]
    if nested:
        return nested
    # Already a mail root, or an account directory under one.
    return [target]


def folder_files(roots, *, include_junk: bool = False):
    """Yield (path, account_key, label) for every mbox under the roots.

    `account_key` is the first path segment under an ImapMail/Mail root — the
    Thunderbird server directory name — or the file's stem when the root is a
    single mbox. `label` is the folder path a person would recognise
    (`INBOX`, `Archive/2024`).
    """
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root, root.stem, root.name
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not _is_mbox(path):
                continue
            try:
                if path.stat().st_size == 0:
                    continue
            except OSError:
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:                               # pragma: no cover
                continue
            parts = rel.parts
            # Under ImapMail the first segment is the server directory; under
            # an account directory passed directly, every part is the label.
            if root.name in ("ImapMail", "Mail") and len(parts) >= 2:
                account, folder_parts = parts[0], parts[1:]
            else:
                account, folder_parts = root.name, parts
            label = "/".join(
                p[:-4] if p.endswith(".sbd") else p for p in folder_parts) or path.name
            if not include_junk and _SKIP.search(label):
                continue
            yield path, account, label


def role_for(label: str) -> str:
    """The RFC 6154 role a Thunderbird folder label most likely is."""
    if _INBOX.search(label):
        return folders_repo.ROLE_INBOX
    if _SENT.search(label):
        return folders_repo.ROLE_SENT
    if _DRAFTS.search(label):
        return folders_repo.ROLE_DRAFTS
    if _ARCHIVE.search(label):
        return folders_repo.ROLE_ARCHIVE
    if _SKIP.search(label):
        # Only reached when include_junk was True.
        if re.search(r"junk|spam", label, re.I):
            return folders_repo.ROLE_JUNK
        return folders_repo.ROLE_TRASH
    return ""


def store_path(account_key: str, label: str) -> str:
    """The folder.path written into corMani's store for an imported mbox.

    Under `LOCAL_PREFIX` so the IMAP reconciler never SELECTs it, and so a
    later sync of the same account cannot collide with a server path that
    happens to share the label.
    """
    safe_account = account_key.replace("\\", "_").replace("/", "_")
    safe_label = label.replace("\\", "/")
    return f"{folders_repo.LOCAL_PREFIX}Thunderbird/{safe_account}/{safe_label}"


def _is_mbox(path: Path) -> bool:
    if not path.is_file():
        return False
    name = path.name
    if name.endswith(".msf") or path.suffix in (".dat", ".sqlite", ".sqlite-wal",
                                                 ".sqlite-shm", ".json"):
        return False
    # A directory's companion summary is `Name.msf`; the mbox itself has no
    # suffix. `.sbd` is a directory of children, not a file.
    if path.suffix == ".sbd":
        return False
    return True
