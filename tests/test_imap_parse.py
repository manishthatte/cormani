# SPDX-License-Identifier: GPL-3.0-or-later
#
# The wire parsers, against the shapes real servers actually send.
#
# Every case here is one that has a plausible wrong implementation: an item
# after a literal, a mailbox name that is not ASCII, a month name read through
# a locale, a UID set long enough to be refused. A test that guards the class
# of a bug is worth more than one that guards the instance — CONVENTIONS.txt §9.
#
# © Manish Jagdish Thatte
import datetime as dt
import unittest

from cormani.imap import parse


class TestModifiedUTF7(unittest.TestCase):
    def test_ascii_is_unchanged(self):
        self.assertEqual(parse.decode_mutf7("INBOX/Lists"), "INBOX/Lists")

    def test_ampersand_is_escaped_as_itself(self):
        self.assertEqual(parse.decode_mutf7("R&-D"), "R&D")
        self.assertEqual(parse.encode_mutf7("R&D"), "R&-D")

    def test_non_ascii_round_trips(self):
        for text in ("Entwürfe", "Прочитанные", "日本語", "café & bar", "Ø"):
            wire = parse.encode_mutf7(text)
            self.assertTrue(all(" " <= c <= "~" for c in wire),
                            f"{wire!r} must be pure ASCII on the wire")
            self.assertEqual(parse.decode_mutf7(wire), text)

    def test_the_known_wire_forms(self):
        # From RFC 3501 §5.1.3 and from Gmail's own folder names.
        self.assertEqual(parse.decode_mutf7("~peter/mail/&U,BTFw-/&ZeVnLIqe-"),
                         "~peter/mail/台北/日本語")
        self.assertEqual(parse.decode_mutf7("&AOk-t&AOk-"), "été")

    def test_base64_uses_comma_not_slash(self):
        # The one character that differs from ordinary base64. A name encoded
        # with `/` would be a name the server cannot find.
        wire = parse.encode_mutf7("台北")
        self.assertNotIn("/", wire)

    def test_a_malformed_name_is_returned_not_raised(self):
        # A server sending nonsense is not a reason to hide a mailbox.
        self.assertEqual(parse.decode_mutf7("broken&"), "broken&")
        self.assertEqual(parse.decode_mutf7("&!!!-x"), "&!!!-x")


class TestFetch(unittest.TestCase):
    def test_a_response_with_no_literal(self):
        got = parse.parse_fetch([b"1 (UID 101 FLAGS (\\Seen \\Flagged))"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].seq, 1)
        self.assertEqual(got[0].uid, 101)
        self.assertEqual(got[0].flags, ("\\Seen", "\\Flagged"))

    def test_empty_flags_are_empty_not_missing(self):
        got = parse.parse_fetch([b"1 (UID 101 FLAGS ())"])
        self.assertEqual(got[0].flags, ())

    def test_a_literal_body_is_kept_whole(self):
        body = b"Subject: hi\r\n\r\nbody line one\r\nbody line two\r\n"
        data = [(b"1 (UID 101 RFC822.SIZE %d BODY[] {%d}" % (len(body), len(body)), body),
                b")"]
        got = parse.parse_fetch(data)
        self.assertEqual(got[0].uid, 101)
        self.assertEqual(got[0].size, len(body))
        self.assertEqual(got[0].body, body)

    def test_an_attribute_after_the_literal_is_not_lost(self):
        # THE case this parser exists for. Servers order FETCH items as they
        # please, and imaplib hands the tail back as a separate item.
        body = b"Subject: hi\r\n\r\nx\r\n"
        data = [(b"1 (UID 101 BODY[] {%d}" % len(body), body),
                b" FLAGS (\\Seen) INTERNALDATE \"25-Aug-2026 10:00:00 +0000\")"]
        got = parse.parse_fetch(data)
        self.assertEqual(got[0].flags, ("\\Seen",))
        self.assertEqual(got[0].internaldate, "2026-08-25T10:00:00+00:00")
        self.assertEqual(got[0].body, body)

    def test_several_messages_in_one_response(self):
        a, b = b"first\r\n", b"second\r\n"
        data = [(b"1 (UID 101 BODY[] {%d}" % len(a), a), b")",
                (b"2 (UID 102 BODY[] {%d}" % len(b), b), b")",
                b"3 (UID 103 FLAGS (\\Deleted))"]
        got = parse.parse_fetch(data)
        self.assertEqual([f.uid for f in got], [101, 102, 103])
        self.assertEqual(got[0].body, a)
        self.assertEqual(got[1].body, b)
        self.assertEqual(got[2].flags, ("\\Deleted",))

    def test_a_body_containing_protocol_characters_is_not_re_parsed(self):
        # A literal may hold unbalanced parentheses and quotes. Re-scanning it
        # as protocol text would derail every response after it.
        body = b'Subject: )")((\r\n\r\nUID 999 FLAGS (\\Seen)\r\n'
        data = [(b"1 (UID 101 BODY[] {%d}" % len(body), body), b" FLAGS (\\Answered))"]
        got = parse.parse_fetch(data)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].uid, 101, "the body must not supply the UID")
        self.assertEqual(got[0].body, body)
        self.assertEqual(got[0].flags, ("\\Answered",))

    def test_a_sectioned_body_is_a_different_key_from_the_whole_message(self):
        head = b"From: a@example.org\r\n\r\n"
        data = [(b"1 (UID 101 BODY[HEADER] {%d}" % len(head), head), b")"]
        got = parse.parse_fetch(data)
        self.assertEqual(got[0].header, head)
        self.assertIsNone(got[0].body, "BODY[HEADER] is not the whole message")

    def test_a_header_fields_section_is_one_item_name(self):
        # `BODY[HEADER.FIELDS (DATE FROM)]` contains spaces and parentheses and
        # is nonetheless a single attribute name.
        head = b"Date: x\r\nFrom: y\r\n\r\n"
        data = [(b"1 (UID 7 BODY[HEADER.FIELDS (DATE FROM)] {%d}" % len(head), head),
                b")"]
        got = parse.parse_fetch(data)
        self.assertEqual(got[0].uid, 7)
        self.assertIn("BODY[HEADER.FIELDS (DATE FROM)]", got[0].items)

    def test_modseq_arrives_parenthesised(self):
        got = parse.parse_fetch([b"1 (UID 101 MODSEQ (12345) FLAGS (\\Seen))"])
        self.assertEqual(got[0].modseq, 12345)

    def test_missing_items_report_absent_rather_than_guessing(self):
        got = parse.parse_fetch([b"1 (FLAGS (\\Seen))"])
        self.assertIsNone(got[0].uid)
        self.assertIsNone(got[0].internaldate)
        self.assertIsNone(got[0].body)
        self.assertEqual(got[0].size, 0)

    def test_nil_is_none_not_the_string(self):
        got = parse.parse_fetch([b"1 (UID 5 ENVELOPE NIL)"])
        self.assertIsNone(got[0].items["ENVELOPE"])


class TestListLines(unittest.TestCase):
    def test_a_quoted_name_with_a_delimiter(self):
        box = parse.parse_list_line(
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"')
        self.assertEqual(box.path, "[Gmail]/Sent Mail")
        self.assertEqual(box.delimiter, "/")
        self.assertEqual(box.attributes, ("\\HasNoChildren", "\\Sent"))
        self.assertTrue(box.selectable)

    def test_an_unquoted_name(self):
        box = parse.parse_list_line(b'(\\HasNoChildren) "." INBOX')
        self.assertEqual(box.path, "INBOX")
        self.assertEqual(box.delimiter, ".")

    def test_a_nil_delimiter_is_empty_not_the_word(self):
        box = parse.parse_list_line(b'(\\Noselect) NIL ""')
        self.assertIsNone(box, "a nameless mailbox is not a mailbox")
        box = parse.parse_list_line(b'(\\Noselect) NIL Root')
        self.assertEqual(box.delimiter, "")
        self.assertFalse(box.selectable)

    def test_the_stored_path_is_the_wire_form_and_the_label_is_decoded(self):
        # folder.path is the key used to select the mailbox again; a tidied
        # name is a mailbox that can no longer be opened.
        box = parse.parse_list_line(b'(\\HasNoChildren \\Drafts) "/" "Entw&APw-rfe"')
        self.assertEqual(box.path, "Entw&APw-rfe")
        self.assertEqual(box.display_name, "Entwürfe")

    def test_a_junk_line_is_skipped_not_raised(self):
        self.assertIsNone(parse.parse_list_line(b"* OK still here"))
        self.assertIsNone(parse.parse_list_line(b""))


class TestInternaldate(unittest.TestCase):
    def test_utc(self):
        self.assertEqual(parse.parse_internaldate('"25-Aug-2026 10:00:00 +0000"'),
                         "2026-08-25T10:00:00+00:00")

    def test_an_offset_is_converted_not_dropped(self):
        # This machine is at UTC+05:30 and the store keeps UTC.
        self.assertEqual(parse.parse_internaldate('"25-Aug-2026 10:00:00 +0530"'),
                         "2026-08-25T04:30:00+00:00")
        self.assertEqual(parse.parse_internaldate('"25-Aug-2026 10:00:00 -0800"'),
                         "2026-08-25T18:00:00+00:00")

    def test_a_single_digit_day(self):
        self.assertEqual(parse.parse_internaldate('" 1-Jan-2026 00:00:00 +0000"'),
                         "2026-01-01T00:00:00+00:00")

    def test_month_names_are_english_regardless_of_locale(self):
        # %b would read these through the process locale and fail on a machine
        # set to French. Every month, explicitly.
        for n, name in enumerate(parse._MONTHS, start=1):
            got = parse.parse_internaldate(f'"15-{name}-2026 12:00:00 +0000"')
            self.assertEqual(got, f"2026-{n:02d}-15T12:00:00+00:00")

    def test_nonsense_is_none_not_a_guess(self):
        for bad in (None, b"", b"not a date", b'"32-Xxx-2026 00:00:00 +0000"',
                    b'"31-Feb-2026 00:00:00 +0000"'):
            self.assertIsNone(parse.parse_internaldate(bad), repr(bad))

    def test_search_date_uses_the_same_english_table(self):
        self.assertEqual(parse.search_date(dt.date(2026, 1, 1)), "1-Jan-2026")
        self.assertEqual(parse.search_date(dt.date(2026, 12, 31)), "31-Dec-2026")


class TestStatusAndUids(unittest.TestCase):
    def test_select_values_are_found_in_any_shape(self):
        got = parse.status_values({
            "UIDVALIDITY": [b"1466687"],
            "UIDNEXT": [b"9541"],
            "HIGHESTMODSEQ": [b"310241"],
            "EXISTS": [b"237"],
        })
        self.assertEqual(got["UIDVALIDITY"], 1466687)
        self.assertEqual(got["UIDNEXT"], 9541)
        self.assertEqual(got["HIGHESTMODSEQ"], 310241)
        self.assertEqual(got["EXISTS"], 237)

    def test_values_inside_an_ok_response_code(self):
        got = parse.status_values([b"[UIDVALIDITY 3857529045] UIDs valid",
                                   b"[UIDNEXT 4392] Predicted next UID"])
        self.assertEqual(got["UIDVALIDITY"], 3857529045)
        self.assertEqual(got["UIDNEXT"], 4392)

    def test_a_search_result_across_several_items(self):
        self.assertEqual(parse.parse_uid_list([b"1 2 3", b"10 11"]),
                         [1, 2, 3, 10, 11])
        self.assertEqual(parse.parse_uid_list([b""]), [])
        self.assertEqual(parse.parse_uid_list([None]), [])

    def test_uid_sets_collapse_into_ranges(self):
        # A first Gmail sync fetches flags for tens of thousands of UIDs, and
        # one per comma builds a command line servers refuse.
        self.assertEqual(parse.uid_ranges([1, 2, 3, 4, 5]), "1:5")
        self.assertEqual(parse.uid_ranges([1, 3, 5]), "1,3,5")
        self.assertEqual(parse.uid_ranges([1, 2, 3, 9, 20, 21, 22]), "1:3,9,20:22")
        self.assertEqual(parse.uid_ranges([]), "")
        self.assertEqual(parse.uid_ranges([7]), "7")
        self.assertEqual(parse.uid_ranges([3, 1, 2, 2]), "1:3")

    def test_a_long_run_becomes_short(self):
        self.assertEqual(parse.uid_ranges(range(1, 50001)), "1:50000")


if __name__ == "__main__":
    unittest.main()
