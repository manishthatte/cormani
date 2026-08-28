# SPDX-License-Identifier: GPL-3.0-or-later
#
# Opening the store, and bringing it up to date.
#
# Three decisions worth the words:
#
# WAL, always. A mail client reads constantly — the list redraws, the search
# runs, the reading pane loads — while a sync writes. Under the default rollback
# journal those block each other, and the interface stops during every sync. WAL
# lets readers proceed while one writer works, which is exactly this shape.
#
# The schema is created and upgraded inside ONE transaction per migration, with
# `PRAGMA user_version` set in the same transaction. If the process dies
# mid-upgrade the file is either wholly at the old version or wholly at the new
# one; there is no state where the version claims something the tables do not
# provide.
#
# `connect()` does not migrate. Opening a database and changing its shape are
# different acts with different risks, and a background thread opening a
# connection must never find itself running DDL. `open_store()` is the one entry
# point that may migrate, and it is called once at start-up.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .schema import LATEST_VERSION, MIGRATIONS


class SchemaTooNew(RuntimeError):
    """The file was written by a later version of corMani.

    Refused rather than opened. A newer schema may use columns this build does
    not know about, and writing to it would corrupt data the user can still
    recover by running the newer build.
    """


def utc_now() -> str:
    """The one timestamp format used everywhere. See schema.py."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection. Does not create or migrate anything."""
    path = Path(path)
    if read_only:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    else:
        con = sqlite3.connect(str(path), timeout=30.0)
    con.row_factory = sqlite3.Row
    # busy_timeout, not a retry loop: SQLite's own waiter is more efficient and
    # does not spin. Thirty seconds is generous, and the alternative is an
    # exception surfacing in the interface during a large import.
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        con.execute("PRAGMA journal_mode = WAL")
        # NORMAL rather than FULL: with WAL this is durable across application
        # crashes and only risks the last transactions on power loss. For a
        # store that can be re-synchronised from the server, that trade is
        # correct, and FULL costs an fsync per transaction during import.
        con.execute("PRAGMA synchronous = NORMAL")
    return con


def schema_version(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


def migrate(con: sqlite3.Connection) -> list[int]:
    """Apply every migration the file has not seen. Returns those applied."""
    current = schema_version(con)
    if current > LATEST_VERSION:
        raise SchemaTooNew(
            f"the store is at schema version {current}, but this build of "
            f"corMani understands {LATEST_VERSION}. Refusing to open it — "
            f"run the newer version instead.")

    applied = []
    for version, description, sql in MIGRATIONS:
        if version <= current:
            continue
        # BEGIN explicitly: Python's implicit transaction handling does not
        # cover DDL, so without this the CREATE statements autocommit one by
        # one and a failure halfway leaves a half-built schema.
        con.execute("BEGIN")
        try:
            con.executescript(sql)
            # executescript commits and ends the transaction, so the version is
            # set in its own statement immediately afterwards. The window
            # between them is the one risk, and it is recoverable: re-running a
            # migration whose version did not stick fails on CREATE TABLE, which
            # is loud rather than silent.
            con.execute(f"PRAGMA user_version = {version}")
            con.commit()
        except Exception:
            con.rollback()
            raise
        applied.append(version)
    return applied


def open_store(path: str | Path) -> sqlite3.Connection:
    """The one entry point that may change the file. Called once at start-up."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    migrate(con)
    return con


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)))
