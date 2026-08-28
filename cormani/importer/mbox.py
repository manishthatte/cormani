# SPDX-License-Identifier: GPL-3.0-or-later
#
# Reading an mbox file without writing it.
#
# Ported from the prototype's `corrapp/mailsync.py`, which earned its rules
# against a multi-gigabyte Thunderbird store that stayed open during the
# import. Three of them bind here without re-argument:
#
# OPENED 'rb', NEVER THROUGH `mailbox.mbox`. The stdlib class takes locks and
# can rewrite a folder. Thunderbird may be running at the same time; the
# prototype verified byte-identical reads across a sync that overlapped one.
# CONVENTIONS.txt §7 — the mail store of another program is opened read-only,
# without exception.
#
# INCREMENTAL BY BYTE OFFSET, NOT BY MESSAGE-ID. A folder is re-read only from
# the offset of its last complete message, and skipped when size and mtime are
# unchanged. A full first pass costs minutes; every pass after costs
# milliseconds. The offset is also the synthetic UID the store keeps, because
# an imported message has no IMAP UID and UNIQUE (folder_id, uid) still needs
# a stable key.
#
# A MESSAGE BEGINS AT `From ` AFTER A BLANK LINE. That is mboxrd, which is
# what Thunderbird writes. The separator itself is not part of the message;
# body lines that began with `From ` were stored with a leading `>`, and that
# escaping is undone here so `envelope.read` sees the bytes the sender wrote.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from pathlib import Path


def iter_mbox(path: Path, start_offset: int = 0):
    """Yield (start, end, raw_bytes) for each message from `start_offset` onward.

    `start` is the byte position of the message's `From ` separator — also the
    synthetic UID the importer stores. `end` is the position of the next
    separator, or EOF, and is what a later pass resumes from so the last
    message is not read again. The yielded bytes do not include the separator.
    """
    with path.open("rb") as fh:
        fh.seek(start_offset)
        offset, cur_off, buf, prev_blank = start_offset, None, [], True
        for line in fh:
            if line.startswith(b"From ") and prev_blank:
                if cur_off is not None:
                    yield cur_off, offset, _unescape(b"".join(buf))
                cur_off, buf = offset, []
            elif cur_off is not None:
                buf.append(line)
            prev_blank = not line.strip()
            offset += len(line)
        if cur_off is not None:
            yield cur_off, offset, _unescape(b"".join(buf))


def is_separator_at(path: Path, offset: int) -> bool:
    """Whether `offset` still points at a message boundary.

    The resume check: if the file grew and the old resume offset still lands
    on a `From ` line, the bytes after it are new mail. If it does not — the
    folder was compacted, rewritten, or truncated — the caller must start
    again rather than splice into the middle of a message.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.readline().startswith(b"From ")
    except OSError:
        return False


def _unescape(raw: bytes) -> bytes:
    """mboxrd: a body line that began with `From ` was stored as `>From `."""
    if b"\n>From " not in raw and not raw.startswith(b">From "):
        return raw
    lines = raw.splitlines(keepends=True)
    out = []
    for line in lines:
        if line.startswith(b">From "):
            out.append(line[1:])
        else:
            out.append(line)
    return b"".join(out)
