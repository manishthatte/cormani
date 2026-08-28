# SPDX-License-Identifier: GPL-3.0-or-later
#
# The sanitiser, which is the only thing between a stranger's markup and the
# widget that draws it.
#
# Most of these are attacks. They are here because an allowlist is only as good
# as the proof that it IS one — a test suite that feeds a sanitiser well-formed
# newsletters proves nothing at all. Where a case has a name in the literature
# it is used, so that the reason for the test survives the person who wrote it.
#
# © Manish Jagdish Thatte
import unittest

from cormani.render.sanitise import (Result, document, plain_document,
                                     sanitise, _safe_style)


class TestScriptRemoval(unittest.TestCase):
    def clean(self, html, **kw):
        return sanitise(html, **kw).html

    def test_a_script_element_goes_with_its_contents(self):
        got = self.clean("<p>before</p><script>alert(1)</script><p>after</p>")
        self.assertNotIn("alert", got)
        self.assertIn("before", got)
        self.assertIn("after", got)

    def test_event_handlers_are_dropped_however_they_are_written(self):
        for markup in ('<p onclick="evil()">x</p>',
                       '<p ONCLICK="evil()">x</p>',
                       '<p onmouseover=evil()>x</p>',
                       '<img src="x" onerror="evil()">',
                       '<body onload="evil()">x</body>',
                       '<div onfocus="evil()" tabindex="1">x</div>'):
            with self.subTest(markup=markup):
                got = self.clean(markup)
                self.assertNotIn("evil", got)
                self.assertNotIn("onclick", got.lower())
                self.assertNotIn("onerror", got.lower())

    def test_the_unclosed_script_swallows_nothing_it_should_not(self):
        got = self.clean("<script>var x = '<p>not really markup</p>';")
        self.assertNotIn("not really markup", got)

    def test_svg_and_math_are_dropped_whole(self):
        # Both are document formats that carry script, not image formats.
        got = self.clean('<svg><script>alert(1)</script></svg>'
                         '<math><mtext>x</mtext></math><p>kept</p>')
        self.assertNotIn("alert", got)
        self.assertIn("kept", got)

    def test_style_contents_are_dropped_not_shown_as_text(self):
        # Unwrapping <style> would print the CSS into the message body.
        got = self.clean("<style>body{color:red}@import url(http://x/)</style><p>hi</p>")
        self.assertNotIn("color:red", got)
        self.assertNotIn("@import", got)
        self.assertIn("hi", got)

    def test_an_iframe_cannot_survive(self):
        got = self.clean('<iframe src="http://evil/"></iframe><p>hi</p>')
        self.assertNotIn("iframe", got)
        self.assertNotIn("evil", got)

    def test_a_form_cannot_survive(self):
        # A message with a working password field in it is a phishing page.
        got = self.clean('<form action="http://evil/"><input name="password">'
                         '<button>Sign in</button></form>')
        for word in ("form", "input", "button", "evil"):
            self.assertNotIn(word, got.lower())

    def test_comments_are_dropped_including_conditional_ones(self):
        got = self.clean("<!--[if IE]><script>alert(1)</script><![endif]--><p>hi</p>")
        self.assertNotIn("alert", got)
        self.assertIn("hi", got)

    def test_what_was_stripped_is_reported(self):
        r = sanitise("<script>x</script><iframe></iframe><p>hi</p>")
        self.assertIn("script", r.stripped)
        self.assertIn("iframe", r.stripped)


class TestLinks(unittest.TestCase):
    def href(self, markup):
        got = sanitise(markup).html
        import re
        m = re.search(r'href="([^"]*)"', got)
        return m.group(1) if m else None

    def test_ordinary_links_survive(self):
        self.assertEqual(self.href('<a href="https://example.org/a">x</a>'),
                         "https://example.org/a")
        self.assertEqual(self.href('<a href="mailto:priya@example.org">x</a>'),
                         "mailto:priya@example.org")

    def test_javascript_urls_are_refused(self):
        for bad in ('javascript:alert(1)', 'JaVaScRiPt:alert(1)',
                    ' javascript:alert(1)', 'java\tscript:alert(1)',
                    'java\nscript:alert(1)', '\x01javascript:alert(1)'):
            with self.subTest(bad=bad):
                self.assertIsNone(self.href(f'<a href="{bad}">x</a>'),
                                  f"{bad!r} survived")

    def test_other_dangerous_schemes_are_refused(self):
        for bad in ("data:text/html,<script>alert(1)</script>", "vbscript:msgbox",
                    "file:///etc/passwd", "about:blank", "chrome://settings"):
            with self.subTest(bad=bad):
                self.assertIsNone(self.href(f'<a href="{bad}">x</a>'))

    def test_the_link_text_survives_even_when_the_href_does_not(self):
        # Losing the words loses the message.
        got = sanitise('<a href="javascript:alert(1)">click here</a>').html
        self.assertIn("click here", got)

    def test_a_bare_fragment_is_dropped(self):
        # There is no document to jump within.
        self.assertIsNone(self.href('<a href="#section">x</a>'))


class TestImages(unittest.TestCase):
    def test_a_remote_image_is_withheld_and_counted(self):
        # A tracking pixel is a disclosure: it tells the sender the message was
        # opened, when, and from which address.
        r = sanitise('<img src="http://tracker.example/pixel.gif" width="1">')
        self.assertEqual(r.blocked_remote, 1)
        self.assertTrue(r.had_remote)
        self.assertNotIn("tracker.example", r.html)

    def test_the_image_keeps_its_place_so_the_layout_holds(self):
        r = sanitise('<img src="http://x/a.png" alt="Our logo" width="600">')
        self.assertIn("<img", r.html)
        self.assertIn('alt="Our logo"', r.html)
        self.assertIn('width="600"', r.html)
        self.assertNotIn("src=", r.html)

    def test_asking_for_remote_content_lets_it_through(self):
        r = sanitise('<img src="https://example.org/a.png">', allow_remote=True)
        self.assertEqual(r.blocked_remote, 0)
        self.assertIn("https://example.org/a.png", r.html)

    def test_an_inline_attachment_resolves_and_discloses_nothing(self):
        r = sanitise('<img src="cid:logo123">',
                     cid_map={"logo123": "file:///store/1/2/1-logo.png"})
        self.assertIn("file:///store/1/2/1-logo.png", r.html)
        self.assertEqual(r.blocked_remote, 0, "it is already on this disk")

    def test_the_angle_brackets_around_a_content_id_are_tolerated(self):
        r = sanitise('<img src="cid:logo123">',
                     cid_map={"<logo123>": "file:///a.png"})
        self.assertIn("file:///a.png", r.html)

    def test_an_unknown_cid_becomes_nothing_rather_than_a_broken_link(self):
        r = sanitise('<img src="cid:missing">', cid_map={})
        self.assertNotIn("cid:", r.html)

    def test_inline_image_data_is_allowed(self):
        tiny = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUg"
                "AAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        r = sanitise(f'<img src="{tiny}">')
        self.assertIn("data:image/png", r.html)
        self.assertEqual(r.blocked_remote, 0)

    def test_data_svg_is_refused_because_svg_carries_script(self):
        r = sanitise('<img src="data:image/svg+xml;base64,PHN2Zz48c2NyaXB0Lz48L3N2Zz4=">')
        self.assertNotIn("svg", r.html)

    def test_data_html_is_refused(self):
        r = sanitise('<img src="data:text/html;base64,PHNjcmlwdD4=">')
        self.assertNotIn("data:", r.html)

    def test_srcset_and_background_never_survive(self):
        # Both fetch, and neither is on the list — which is the whole argument
        # for an allowlist.
        r = sanitise('<img src="cid:a" srcset="http://evil/2x.png 2x">'
                     '<td background="http://evil/bg.png">x</td>')
        self.assertNotIn("evil", r.html)
        self.assertNotIn("srcset", r.html)


class TestStyle(unittest.TestCase):
    def test_ordinary_styling_survives(self):
        # Discarding all CSS makes every HTML message look like a ransom note.
        got = sanitise('<p style="color: #333; font-weight: bold">x</p>').html
        self.assertIn("color: #333", got)
        self.assertIn("font-weight: bold", got)

    def test_url_in_css_is_refused(self):
        # It is a network request wearing a stylesheet.
        self.assertEqual(_safe_style("background-color: url(http://evil/x.png)"), "")
        got = sanitise('<div style="background-color:url(http://evil/x)">x</div>').html
        self.assertNotIn("evil", got)

    def test_expression_and_behavior_are_refused(self):
        self.assertEqual(_safe_style("width: expression(alert(1))"), "")
        self.assertEqual(_safe_style("behavior: url(#default#time2)"), "")

    def test_position_cannot_escape_the_message(self):
        # position:fixed lets a message draw over the rest of the window.
        self.assertEqual(_safe_style("position: fixed; top: 0"), "")
        self.assertEqual(_safe_style("z-index: 99999"), "")

    def test_a_smuggled_second_declaration_is_refused(self):
        self.assertEqual(_safe_style("color: red; position: fixed"), "color: red")

    def test_a_zero_font_size_is_dropped(self):
        # The oldest hidden-text trick: words a filter reads and a person does
        # not. Found on live mail, by Qt warning once per element.
        for bad in ("font-size: 0", "font-size: 0px", "font-size: 0pt",
                    "font-size: -3px", "font-size: 0.0em"):
            self.assertEqual(_safe_style(bad), "", bad)

    def test_an_ordinary_font_size_survives(self):
        self.assertEqual(_safe_style("font-size: 13px"), "font-size: 13px")
        self.assertEqual(_safe_style("font-size: 1.2em"), "font-size: 1.2em")

    def test_a_zero_width_is_still_allowed_because_tables_use_it(self):
        self.assertEqual(_safe_style("width: 0"), "width: 0")

    def test_an_absurd_number_of_declarations_is_bounded(self):
        got = _safe_style("; ".join(["color: red"] * 500))
        self.assertLessEqual(got.count(";"), 32)


class TestStructure(unittest.TestCase):
    def test_unknown_tags_are_unwrapped_and_their_text_kept(self):
        # Throwing the contents away would lose the message itself.
        got = sanitise("<html><body><custom-element>the words</custom-element>"
                       "</body></html>").html
        self.assertIn("the words", got)
        self.assertNotIn("custom-element", got)

    def test_unbalanced_markup_is_closed_off(self):
        got = sanitise("<div><p><b>text").html
        self.assertEqual(got.count("<div"), got.count("</div"))
        self.assertIn("text", got)

    def test_a_stray_closing_tag_is_ignored(self):
        got = sanitise("</div></p>text").html
        self.assertIn("text", got)

    def test_deep_nesting_does_not_run_away(self):
        got = sanitise("<div>" * 500 + "deep" + "</div>" * 500).html
        self.assertIn("deep", got)
        self.assertEqual(got.count("<div"), got.count("</div"))

    def test_text_is_escaped_not_re_interpreted(self):
        got = sanitise("<p>3 &lt; 5 &amp;&amp; 5 &gt; 3</p>").html
        self.assertIn("3 &lt; 5", got)
        self.assertNotIn("<script", got)

    def test_tables_and_lists_survive_because_mail_uses_them(self):
        got = sanitise("<table><tr><td colspan='2'>cell</td></tr></table>"
                       "<ul><li>one</li></ul>").html
        self.assertIn("<table>", got)
        self.assertIn('colspan="2"', got)
        self.assertIn("<li>", got)

    def test_nothing_raises_on_any_of_the_degenerate_inputs(self):
        for bad in ("", "<", "<<<>>>", "<p", "<p attr", "&", "&#x;",
                    "<a href=", "<!--", "<![CDATA[x]]>", "\x00\x01",
                    "<p>" * 10000):
            with self.subTest(bad=bad[:20]):
                r = sanitise(bad)
                self.assertIsInstance(r.html, str)


class TestDocument(unittest.TestCase):
    def test_the_csp_refuses_everything_by_default(self):
        page = document(sanitise("<p>hi</p>"))
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("http:", page.split("</head>")[0].split("Content-Security")[1])

    def test_asking_for_remote_content_widens_the_csp_and_only_then(self):
        blocked = document(sanitise("<p>x</p>"))
        allowed = document(sanitise("<p>x</p>"), allow_remote=True)
        self.assertNotIn("img-src data: file: http:", blocked)
        self.assertIn("http: https:", allowed)

    def test_the_palette_reaches_the_page(self):
        page = document(sanitise("<p>x</p>"),
                        palette={"fg": "#eee8d5", "bg": "#002b36"})
        self.assertIn("#eee8d5", page)
        self.assertIn("#002b36", page)

    def test_a_wide_table_cannot_widen_the_pane(self):
        self.assertIn("max-width: 100%", document(Result()))

    def test_a_plain_message_is_shown_as_what_it_is(self):
        page = plain_document("line one\n  indented\n> quoted")
        self.assertIn("<pre>", page)
        self.assertIn("  indented", page, "plain text's alignment IS its format")

    def test_plain_text_that_looks_like_markup_is_escaped(self):
        page = plain_document("<script>alert(1)</script>")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
