# SPDX-License-Identifier: GPL-3.0-or-later
#
# vCard import and export.
#
# © Manish Jagdish Thatte
import tempfile
import unittest
from pathlib import Path

from test_cli import Fixture

from cormani.importer import vcard as vcard_mod
from cormani.store import contacts as contacts_repo


class TestVCard(Fixture):
    def test_round_trip(self):
        con = self.store()
        contact_id = contacts_repo.add_contact(
            con, "Ada Lovelace", org="Analytical Engines", role="Mathematician",
            notes="First programmer")
        contacts_repo.add_handle(con, contact_id, contacts_repo.KIND_EMAIL,
                                 "ada@example.org")
        contacts_repo.add_handle(con, contact_id, "phone", "+44 20 7946 0958")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.vcf"
            self.assertEqual(vcard_mod.export_file(con, path), 1)
            text = path.read_text(encoding="utf-8")
            self.assertIn("BEGIN:VCARD", text)
            self.assertIn("Ada Lovelace", text)
            self.assertIn("ada@example.org", text)

            con.execute("DELETE FROM handle")
            con.execute("DELETE FROM contact")
            con.commit()
            report = vcard_mod.import_file(con, path)
            self.assertEqual(report.imported, 1)
            people = contacts_repo.list_contacts(con)
            self.assertEqual(len(people), 1)
            self.assertEqual(people[0].name, "Ada Lovelace")
            self.assertEqual(people[0].org, "Analytical Engines")
            self.assertEqual(people[0].address, "ada@example.org")

    def test_import_cli(self):
        con = self.store()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.vcf"
            path.write_text(
                "BEGIN:VCARD\nVERSION:3.0\nFN:Test Person\n"
                "EMAIL:test@example.com\nEND:VCARD\n",
                encoding="utf-8")
            code, text = self.run_cli("--import-vcard", str(path))
            self.assertEqual(code, 0)
            self.assertIn("imported 1 contact", text)
            self.assertEqual(len(contacts_repo.list_contacts(con)), 1)

    def test_export_cli(self):
        con = self.store()
        contacts_repo.add_contact(con, "Export Me")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.vcf"
            code, text = self.run_cli("--export-vcard", str(path))
            self.assertEqual(code, 0)
            self.assertIn("exported 1 contact", text)
            self.assertIn("Export Me", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
