# SPDX-License-Identifier: GPL-3.0-or-later
#
# `--import-thunderbird`: copy another program's mail into corMani's store.
#
# Split from `configure.py` for the reason `calcli.py` and `contactcli.py`
# were: the command has a body, and the parser must stay a parser. This is a
# one-shot setup act rather than day-to-day operation, which is why it sits
# with the "setting up" group in `__main__.py` and not beside `--sync`.
#
# THE SOURCE IS NEVER WRITTEN. `importer.run` opens every mbox read-only; this
# file only resolves which corMani account receives the rows and prints the
# report. CONVENTIONS.txt §7.
#
# `--into` IS REQUIRED. Thunderbird's on-disk account names are server
# directories (`imap.gmail.com`), not addresses, and guessing the mapping would
# put one mailbox's history under another's identity. The account must already
# exist — add it first with `--add-account` or File ▸ Add mail account… .
#
# © Manish Jagdish Thatte
from __future__ import annotations

from pathlib import Path


def import_thunderbird(path: str = "", *, into: str = "",
                       include_junk: bool = False) -> int:
    """Import Thunderbird (or other mbox) mail into one configured account."""
    from . import app
    from . import importer
    from .store import accounts as accounts_repo
    from .store import database

    address = (into or "").strip().lower()
    if not address:
        print("name the corMani account that should receive the mail with "
              "--into ADDRESS")
        return 1
    if "@" not in address:
        print(f"{address!r} is not an email address")
        return 1

    paths = app.current_paths().ensure()
    con = database.open_store(paths.database)
    try:
        account = accounts_repo.find_by_address(con, address)
        if account is None:
            print(f"{address} is not configured — add it first with "
                  f"--add-account or File ▸ Add mail account…")
            return 1

        target = path.strip() or None
        if target:
            print(f"importing from {Path(target).expanduser()}")
        else:
            print("importing from the Thunderbird profiles under this home")
        print(f"into {address}")

        def progress(line: str) -> None:
            print(f"  {line}", flush=True)

        report = importer.run(
            con, account.id, path=target, include_junk=include_junk,
            attachments_root=paths.attachments, progress=progress)
        for note in report.notes:
            print(f"  note: {note}")
        print(report.describe())
        if report.roots:
            print("roots:")
            for root in report.roots:
                print(f"  {root}")
        return 0 if (report.folders or report.skipped or report.new) else 1
    finally:
        con.close()
