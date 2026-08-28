# SPDX-License-Identifier: GPL-3.0-or-later
#
# Reading real messages, including the malformed ones.
#
# The governing requirement is that NOTHING here raises. A sync that stops on
# one bad message never reaches the ten thousand behind it, so every case below
# that looks like abuse is a message someone has actually sent.
#
# © Manish Jagdish Thatte
import unittest

from cormani.imap import envelope


def build(headers: str, body: str = "", *, crlf: bool = True) -> bytes:
    raw = headers.strip("\n") + "\n\n" + body
    return raw.replace("\n", "\r\n").encode("utf-8") if crlf else raw.encode("utf-8")


class TestHeaders(unittest.TestCase):
    def test_a_plain_message(self):
        e = envelope.read(build(
            "From: Priya Raman <priya@example.org>\n"
            "To: owner@manitlab.example\n"
            "Subject: Quarterly figures\n"
            "Message-ID: <a1@example.org>\n"
            "Date: Tue, 25 Aug 2026 10:00:00 +0000",
            "Numbers attached.\n"))
        self.assertEqual(e.from_name, "Priya Raman")
        self.assertEqual(e.from_addr, "priya@example.org")
        self.assertEqual(e.subject, "Quarterly figures")
        self.assertEqual(e.message_id, "<a1@example.org>")
        self.assertEqual(e.date_at, "2026-08-25T10:00:00+00:00")
        self.assertEqual(e.body_text, "Numbers attached.")
        self.assertEqual(e.preview, "Numbers attached.")
        self.assertFalse(e.has_attachment)

    def test_encoded_words_are_decoded(self):
        e = envelope.read(build(
            "From: =?utf-8?q?Bj=C3=B6rn_Andr=C3=A9?= <bjorn@example.se>\n"
            "Subject: =?utf-8?B?UmU6IEludm9pY2Ug4oKsMTAw?=", "x"))
        self.assertEqual(e.from_name, "Björn André")
        self.assertEqual(e.subject, "Re: Invoice €100")
        self.assertEqual(e.subject_base, "Invoice €100")

    def test_abutting_encoded_words_do_not_gain_a_space(self):
        # Split encoded words abut by design. Joining the decoded pieces with a
        # space inserts one the sender did not write.
        e = envelope.read(build(
            "Subject: =?utf-8?q?Quarterly_?= =?utf-8?q?figures?=", "x"))
        self.assertEqual(e.subject, "Quarterly figures")
        e = envelope.read(build("Subject: =?utf-8?B?4pyT?==?utf-8?B?4pyT?=", "x"))
        self.assertEqual(e.subject, "✓✓")

    def test_a_folded_subject_becomes_one_line(self):
        e = envelope.read(build("Subject: a very long subject that the sender\n"
                                "  folded across two lines", "x"))
        self.assertEqual(e.subject,
                         "a very long subject that the sender folded across two lines")

    def test_a_charset_this_build_does_not_have_is_not_fatal(self):
        e = envelope.read(build("Subject: =?x-unknown-charset?q?hello?=", "x"))
        self.assertIn("hello", e.subject)

    def test_unknown_8bit_is_a_charset_name_not_a_codec(self):
        e = envelope.read(build("Subject: =?unknown-8bit?q?caf=E9?=", "x"))
        self.assertTrue(e.subject)                       # decoded, not raised

    def test_address_lists_keep_the_rfc_form(self):
        e = envelope.read(build(
            "To: Priya Raman <priya@example.org>, bare@example.org\n"
            "Cc: =?utf-8?q?Ren=C3=A9?= <rene@example.fr>", "x"))
        self.assertEqual(e.to_addrs,
                         "Priya Raman <priya@example.org>, bare@example.org")
        self.assertEqual(e.cc_addrs, "René <rene@example.fr>")

    def test_a_display_name_with_a_comma_survives_the_round_trip(self):
        # One recipient must not become two. The store keeps the RFC form and
        # `store.messages.display_name` parses it back; the two have to agree.
        from cormani.store.messages import display_name
        e = envelope.read(build(
            'To: "Raman, Priya" <priya@example.org>, bare@example.org', "x"))
        self.assertEqual(e.to_addrs,
                         '"Raman, Priya" <priya@example.org>, bare@example.org')
        self.assertEqual(display_name(e.to_addrs), "Raman, Priya")

    def test_a_non_ascii_name_is_stored_as_text_not_re_encoded(self):
        # formataddr would put `=?utf-8?b?...?=` back in the column, which is a
        # wire form. Encoding belongs in stage 4's composer.
        from cormani.store.messages import display_name
        e = envelope.read(build("To: =?utf-8?q?Ren=C3=A9?= <rene@example.fr>", "x"))
        self.assertEqual(e.to_addrs, "René <rene@example.fr>")
        self.assertEqual(display_name(e.to_addrs), "René")

    def test_missing_headers_are_empty_not_none(self):
        e = envelope.read(build("Subject: only a subject", "x"))
        self.assertEqual(e.from_addr, "")
        self.assertEqual(e.to_addrs, "")
        self.assertEqual(e.references, "")
        self.assertIsNone(e.date_at)

    def test_a_date_with_no_offset_is_read_as_utc(self):
        e = envelope.read(build("Date: Tue, 25 Aug 2026 10:00:00", "x"))
        self.assertEqual(e.date_at, "2026-08-25T10:00:00+00:00")

    def test_a_date_with_an_offset_is_converted(self):
        e = envelope.read(build("Date: Tue, 25 Aug 2026 10:00:00 +0530", "x"))
        self.assertEqual(e.date_at, "2026-08-25T04:30:00+00:00")

    def test_a_nonsense_date_is_none_rather_than_a_guess(self):
        for bad in ("Date: yesterday", "Date: ", "Date: 32 Xxx 9999"):
            self.assertIsNone(envelope.read(build(bad, "x")).date_at, bad)

    def test_the_references_chain_is_kept_in_order(self):
        e = envelope.read(build(
            "References: <a@x> <b@x>\n  <c@x>\n"
            "In-Reply-To: <c@x>", "x"))
        self.assertEqual(e.references, "<a@x> <b@x> <c@x>")
        self.assertEqual(e.in_reply_to, "<c@x>")


class TestSubjectPrefixes(unittest.TestCase):
    def test_repeated_prefixes_go_in_one_pass(self):
        self.assertEqual(envelope.strip_subject("Re: Fwd: Re: Invoice"), "Invoice")

    def test_the_languages_the_correspondence_is_in(self):
        for prefixed in ("Re: x", "RE: x", "Fwd: x", "FW: x", "AW: x", "SV: x",
                         "Re[2]: x", "re : x"):
            self.assertEqual(envelope.strip_subject(prefixed), "x", prefixed)

    def test_a_word_that_merely_ends_in_a_colon_is_left_alone(self):
        self.assertEqual(envelope.strip_subject("Report: Q3"), "Report: Q3")
        self.assertEqual(envelope.strip_subject("Reminder: pay"), "Reminder: pay")

    def test_an_empty_subject_stays_empty(self):
        self.assertEqual(envelope.strip_subject(""), "")
        self.assertEqual(envelope.strip_subject("Re: "), "")


class TestBodies(unittest.TestCase):
    def test_multipart_alternative_keeps_both_and_prefers_the_last(self):
        raw = build(
            'Subject: both\n'
            'Content-Type: multipart/alternative; boundary="B"',
            "--B\n"
            "Content-Type: text/plain; charset=utf-8\n\n"
            "the plain one\n"
            "--B\n"
            "Content-Type: text/html; charset=utf-8\n\n"
            "<p>the <b>rich</b> one</p>\n"
            "--B--\n")
        e = envelope.read(raw)
        self.assertEqual(e.body_text, "the plain one")
        self.assertIn("<b>rich</b>", e.body_html)
        self.assertEqual(e.preview, "the plain one")

    def test_an_html_only_message_still_gets_text_and_a_preview(self):
        # Otherwise the third line of the row is blank and the message is
        # invisible to search.
        raw = build('Subject: html only\nContent-Type: text/html; charset=utf-8',
                    "<html><head><style>p{color:red}</style></head>"
                    "<body><p>Dear Manish,</p><p>the &pound;40 is paid.</p></body></html>")
        e = envelope.read(raw)
        self.assertIn("Dear Manish", e.body_text)
        self.assertIn("£40 is paid", e.body_text)
        self.assertNotIn("color:red", e.body_text, "style contents are not text")
        self.assertNotIn("<p>", e.body_text)
        self.assertTrue(e.preview.startswith("Dear Manish"))
        self.assertIn("<p>", e.body_html, "the original HTML is kept for stage 3")

    def test_a_plain_part_that_is_really_markup_is_stripped(self):
        # Observed on live mail: a marketing mailer put 72 KB containing 926
        # tags into the plain half of a multipart/alternative. Left alone it
        # puts raw markup on the list row and `div` and `td` in the index.
        markup = ("<div><p>Dear customer</p>" + "<td><span>cell</span></td>" * 20
                  + "<br><a href='x'>link</a></div>")
        raw = build('Subject: mislabelled\nContent-Type: multipart/alternative; '
                    'boundary="B"',
                    f"--B\nContent-Type: text/plain\n\n{markup}\n"
                    f"--B\nContent-Type: text/html\n\n"
                    f"<html><body><p>Dear customer</p><p>the real one</p></body></html>\n"
                    "--B--\n")
        e = envelope.read(raw)
        self.assertNotIn("<td>", e.body_text)
        self.assertNotIn("<div", e.body_text)
        self.assertIn("Dear customer", e.body_text)
        self.assertIn("the real one", e.body_text,
                      "rendered from the real HTML part, which is the better source")
        self.assertFalse(e.preview.lstrip().startswith("<"))

    def test_a_plain_only_message_of_markup_is_stripped_from_itself(self):
        markup = "<div>" + "<p>line</p>" * 12 + "</div>"
        e = envelope.read(build("Content-Type: text/plain", markup))
        self.assertNotIn("<p>", e.body_text)
        self.assertIn("line", e.body_text)
        self.assertEqual(e.body_html, "",
                         "the sender declared plain; we do not promote it to HTML")

    def test_line_endings_are_normalised_to_one_convention(self):
        # The wire sends CRLF and a body rendered from HTML has LF, so without
        # this the column holds both depending on where the text came from —
        # measured at 58 of 77 on a live account.
        e = envelope.read(build("Subject: s", "one\ntwo\nthree\n"))
        self.assertEqual(e.body_text, "one\ntwo\nthree")
        self.assertNotIn("\r", e.body_text)

    def test_the_original_html_keeps_its_own_line_endings(self):
        # It is the document stage 3 renders, and it arrived that way.
        raw = build("Content-Type: text/html", "<p>one</p>\n<p>two</p>")
        self.assertIn("\r\n", envelope.read(raw).body_html)

    def test_genuine_plain_text_is_never_rewritten(self):
        # 21 of 77 live messages contained angle-bracket text — quoted
        # addresses, mostly. Being wrong here silently rewrites someone's mail.
        for body in (
                "On Monday, Priya <priya@example.org> wrote:\n> yes\n",
                "The condition is a < b > c, which holds.\n",
                "See <https://example.org/a> and <https://example.org/b>.\n",
                "Reply to <owner@manitlab.example> or <admin@idlidu.example>.\n",
                "Use <Ctrl> and <Alt> together.\n"):
            with self.subTest(body=body[:30]):
                e = envelope.read(build("Subject: plain", body))
                self.assertEqual(e.body_text, body.strip())

    def test_a_few_tags_in_prose_are_left_alone(self):
        # Someone writing ABOUT html. Eight is the threshold; this is under it.
        body = "Wrap it in <p> and <b>, then close </b> and </p>.\n"
        self.assertFalse(envelope.looks_like_html(body))
        self.assertEqual(envelope.read(build("Subject: s", body)).body_text,
                         body.strip())

    def test_a_full_document_is_recognised_however_few_tags(self):
        self.assertTrue(envelope.looks_like_html("<html><body>hi</body></html>"))
        self.assertTrue(envelope.looks_like_html("<!DOCTYPE html>\n<p>hi"))

    def test_script_contents_never_reach_the_text(self):
        e = envelope.read(build("Content-Type: text/html",
                                "<body>ok<script>alert('x')</script></body>"))
        self.assertNotIn("alert", e.body_text)

    def test_a_quoted_reply_previews_the_reply_not_the_quotation(self):
        e = envelope.read(build("Subject: Re: x",
                                "Yes, Friday works.\n\n"
                                "> On Monday you wrote:\n"
                                "> a long quotation\n"
                                "> that would otherwise be the preview\n"))
        self.assertEqual(e.preview, "Yes, Friday works.")

    def test_a_message_that_is_only_a_quotation_still_previews(self):
        e = envelope.read(build("Subject: x", "> nothing but this\n"))
        self.assertEqual(e.preview, "> nothing but this")

    def test_the_preview_is_bounded(self):
        e = envelope.read(build("Subject: x", "word " * 500))
        self.assertLessEqual(len(e.preview), 200)

    def test_a_windows_charset_body_is_read(self):
        raw = (b"Subject: cp1252\r\nContent-Type: text/plain; charset=windows-1252"
               b"\r\n\r\nsmart \x93quotes\x94 and an en\x96dash\r\n")
        e = envelope.read(raw)
        self.assertIn("“quotes”", e.body_text)

    def test_a_body_whose_declared_charset_is_a_lie_is_still_read(self):
        raw = (b"Subject: mislabelled\r\nContent-Type: text/plain; charset=us-ascii"
               b"\r\n\r\ncaf\xc3\xa9 na\xc3\xafve\r\n")
        e = envelope.read(raw)
        self.assertTrue(e.body_text, "a mislabelled body must not come back empty")

    def test_deeply_nested_multipart_finds_the_text_at_the_bottom(self):
        raw = build(
            'Subject: nested\nContent-Type: multipart/mixed; boundary="OUT"',
            '--OUT\n'
            'Content-Type: multipart/related; boundary="MID"\n\n'
            '--MID\n'
            'Content-Type: multipart/alternative; boundary="IN"\n\n'
            '--IN\n'
            'Content-Type: text/plain\n\n'
            'buried four deep\n'
            '--IN--\n'
            '--MID--\n'
            '--OUT--\n')
        self.assertEqual(envelope.read(raw).body_text, "buried four deep")


class TestAttachments(unittest.TestCase):
    def _with_attachment(self, disposition: str, extra: str = "") -> bytes:
        return build(
            'Subject: with a file\nContent-Type: multipart/mixed; boundary="B"',
            "--B\n"
            "Content-Type: text/plain\n\n"
            "See attached.\n"
            "--B\n"
            "Content-Type: application/pdf; name=\"invoice.pdf\"\n"
            f"Content-Disposition: {disposition}\n"
            f"{extra}"
            "Content-Transfer-Encoding: base64\n\n"
            "cGRmIGJ5dGVz\n"
            "--B--\n")

    def test_an_attachment_is_found_with_its_bytes(self):
        e = envelope.read(self._with_attachment('attachment; filename="invoice.pdf"'))
        self.assertEqual(e.body_text, "See attached.")
        self.assertEqual(len(e.parts), 1)
        part = e.parts[0]
        self.assertEqual(part.filename, "invoice.pdf")
        self.assertEqual(part.content_type, "application/pdf")
        self.assertEqual(part.payload, b"pdf bytes")
        self.assertEqual(part.size_bytes, 9)
        self.assertEqual(part.part_number, "2")
        self.assertTrue(e.has_attachment)

    def test_an_inline_image_does_not_earn_a_paperclip(self):
        # An HTML signature logo would otherwise put one on every row.
        raw = build(
            'Subject: signature\nContent-Type: multipart/related; boundary="B"',
            "--B\nContent-Type: text/html\n\n<p>hi <img src=\"cid:logo\"></p>\n"
            "--B\n"
            "Content-Type: image/png\n"
            "Content-ID: <logo>\n"
            "Content-Disposition: inline; filename=\"logo.png\"\n"
            "Content-Transfer-Encoding: base64\n\n"
            "cG5n\n"
            "--B--\n")
        e = envelope.read(raw)
        self.assertEqual(len(e.parts), 1)
        self.assertTrue(e.parts[0].is_inline)
        self.assertEqual(e.parts[0].content_id, "logo")
        self.assertFalse(e.has_attachment)

    def test_an_encoded_filename_is_decoded(self):
        raw = build(
            'Content-Type: multipart/mixed; boundary="B"',
            "--B\nContent-Type: text/plain\n\nx\n"
            "--B\n"
            "Content-Type: application/pdf\n"
            "Content-Disposition: attachment;\n"
            " filename=\"=?utf-8?q?Rechnung_M=C3=A4rz.pdf?=\"\n\n"
            "bytes\n--B--\n")
        self.assertEqual(envelope.read(raw).parts[0].filename, "Rechnung März.pdf")

    def test_a_forwarded_message_is_one_attachment_not_a_subtree(self):
        raw = build(
            'Subject: Fwd: original\nContent-Type: multipart/mixed; boundary="B"',
            "--B\nContent-Type: text/plain\n\nSee below.\n"
            "--B\n"
            "Content-Type: message/rfc822\n"
            "Content-Disposition: attachment; filename=\"original.eml\"\n\n"
            "From: someone@example.org\n"
            "Subject: the inner subject\n\n"
            "inner body\n"
            "--B--\n")
        e = envelope.read(raw)
        self.assertEqual(e.subject, "Fwd: original")
        self.assertEqual(e.body_text, "See below.",
                         "the inner body must not become the outer one")
        self.assertEqual(len(e.parts), 1)
        self.assertEqual(e.parts[0].content_type, "message/rfc822")

    def test_nested_part_numbers_follow_the_imap_scheme(self):
        raw = build(
            'Content-Type: multipart/mixed; boundary="OUT"',
            '--OUT\nContent-Type: multipart/alternative; boundary="IN"\n\n'
            '--IN\nContent-Type: text/plain\n\ntext\n'
            '--IN\nContent-Type: text/html\n\n<p>html</p>\n'
            '--IN--\n'
            '--OUT\nContent-Type: application/pdf\n'
            'Content-Disposition: attachment; filename="f.pdf"\n\nbytes\n'
            '--OUT--\n')
        self.assertEqual(envelope.read(raw).parts[0].part_number, "2")


class TestNothingRaises(unittest.TestCase):
    def test_the_degenerate_inputs(self):
        for raw in (b"", b"\r\n", b"not a message at all",
                    b"Subject: no body\r\n", b"\x00\x01\x02\xff\xfe",
                    b"From: <<<>>>\r\n\r\nbody",
                    b"Content-Type: multipart/mixed; boundary=\r\n\r\n--\r\n"):
            with self.subTest(raw=raw[:20]):
                e = envelope.read(raw)                   # must not raise
                self.assertIsInstance(e.subject, str)
                self.assertIsInstance(e.body_text, str)
                self.assertIsInstance(e.parts, tuple)

    def test_an_unterminated_multipart(self):
        raw = build('Content-Type: multipart/mixed; boundary="B"',
                    "--B\nContent-Type: text/plain\n\nthe only part\n")
        e = envelope.read(raw)
        self.assertIn("the only part", e.body_text)

    def test_size_is_the_bytes_on_the_wire(self):
        raw = build("Subject: x", "body")
        self.assertEqual(envelope.read(raw).size_bytes, len(raw))

    def test_the_envelope_carries_the_chain_but_does_not_thread(self):
        # An envelope reports what the message SAID. Which conversation it
        # joins depends on what the store already holds, so store/threads.py
        # decides it — see the note where `thread_key` used to be.
        e = envelope.read(build("Subject: Re: Quarterly Figures\n"
                                "In-Reply-To: <a@x>\n"
                                "References: <root@x> <a@x>", "x"))
        self.assertEqual(e.in_reply_to, "<a@x>")
        self.assertEqual(e.references, "<root@x> <a@x>")
        self.assertEqual(e.subject_base, "Quarterly Figures")
        self.assertFalse(hasattr(e, "thread_key"))


if __name__ == "__main__":
    unittest.main()
