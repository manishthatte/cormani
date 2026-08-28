# SPDX-License-Identifier: GPL-3.0-or-later
#
# vCard 3.0 import and export for the address book.
#
# Deliberately small: FN, N, ORG, TITLE, EMAIL, TEL, NOTE — enough to move
# cards between corMani and another program without pulling in a dependency.
# The source file is opened read-only on import.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..store import contacts as contacts_repo


@dataclass
class Card:
    name: str = ""
    org: str = ""
    role: str = ""
    notes: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)


@dataclass
class Report:
    imported: int = 0
    skipped: int = 0


def _unfold(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line.rstrip("\r\n"))
    return out


def _split_field(line: str) -> tuple[str, str, str]:
    """Return (property, params, value) from one unfolded line."""
    if ":" not in line:
        return "", "", ""
    head, value = line.split(":", 1)
    if ";" in head:
        prop, *params = head.split(";")
        return prop.upper(), ";".join(params), value
    return head.upper(), "", value


def _decode(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def _encode(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def parse(text: str) -> list[Card]:
    cards: list[Card] = []
    current: Card | None = None
    for line in _unfold(text.splitlines()):
        prop, _params, value = _split_field(line)
        if prop == "BEGIN" and value.upper() == "VCARD":
            current = Card()
            continue
        if prop == "END" and value.upper() == "VCARD":
            if current is not None:
                cards.append(current)
            current = None
            continue
        if current is None or not prop:
            continue
        value = _decode(value)
        if prop == "FN":
            current.name = value
        elif prop == "N" and not current.name:
            parts = value.split(";")
            current.name = " ".join(p for p in (parts[1], parts[0]) if p)
        elif prop == "ORG":
            current.org = value.split(";")[0]
        elif prop == "TITLE":
            current.role = value
        elif prop == "NOTE":
            current.notes = value
        elif prop == "EMAIL" and value:
            current.emails.append(value)
        elif prop == "TEL" and value:
            current.phones.append(value)
    return cards


def import_file(con: sqlite3.Connection, path: Path, *,
                commit: bool = True) -> Report:
    text = path.read_text(encoding="utf-8", errors="replace")
    report = Report()
    for card in parse(text):
        if not any((card.name, card.org, card.role, card.notes,
                    card.emails, card.phones)):
            report.skipped += 1
            continue
        contact_id = contacts_repo.add_contact(
            con, card.name, org=card.org, role=card.role, notes=card.notes,
            commit=False)
        for address in card.emails:
            contacts_repo.add_handle(con, contact_id,
                                     contacts_repo.KIND_EMAIL, address,
                                     commit=False)
        for phone in card.phones:
            contacts_repo.add_handle(con, contact_id, "phone", phone,
                                     commit=False)
        report.imported += 1
    if commit:
        con.commit()
    return report


def export_file(con: sqlite3.Connection, path: Path, *,
                query: str = "") -> int:
    contacts = contacts_repo.list_contacts(con, query=query)
    lines: list[str] = []
    for contact in contacts:
        lines.append("BEGIN:VCARD")
        lines.append("VERSION:3.0")
        if contact.name:
            lines.append(f"FN:{_encode(contact.name)}")
            parts = contact.name.split(None, 1)
            family = parts[-1] if len(parts) > 1 else ""
            given = parts[0]
            lines.append(f"N:{_encode(family)};{_encode(given)};;;")
        if contact.org:
            lines.append(f"ORG:{_encode(contact.org)}")
        if contact.role:
            lines.append(f"TITLE:{_encode(contact.role)}")
        if contact.notes:
            lines.append(f"NOTE:{_encode(contact.notes)}")
        for handle in contact.handles:
            if handle.kind == contacts_repo.KIND_EMAIL:
                lines.append(f"EMAIL;TYPE=INTERNET:{_encode(handle.value)}")
            elif handle.kind == "phone":
                lines.append(f"TEL;TYPE=VOICE:{_encode(handle.value)}")
        lines.append("END:VCARD")
    path.write_text("\n".join(lines) + ("\n" if lines else ""),
                    encoding="utf-8")
    return len(contacts)
