# SPDX-License-Identifier: GPL-3.0-or-later
#
# The sanitiser against a corpus, not against the cases imagined while writing
# it — PLAN.txt Stage 9.
#
# `tests/test_sanitise.py` is the unit suite: named attacks, one property each.
# This file is the other half: every `.html` under `tests/corpus/sanitise/` is
# fed to `sanitise`, and a small set of universal refusals is asserted, plus
# per-file expectations for the ones that name a specific outcome. Adding a
# sample is dropping a file in the directory; forgetting to assert it is how a
# corpus stops being evidence.
#
# © Manish Jagdish Thatte
import unittest
from pathlib import Path

from cormani.render.sanitise import document, sanitise

CORPUS = Path(__file__).resolve().parent / "corpus" / "sanitise"

# Substrings that must never appear in sanitised output, whatever the sample.
_FORBIDDEN = (
    "<script", "</script", "javascript:", "vbscript:",
    "onerror=", "onclick=", "onload=", "onmouseover=",
    "<iframe", "<form", "<input", "<base", "<meta",
    "@import", "expression(",
)


def _samples():
    return sorted(CORPUS.glob("*.html"))


class TestCorpusExists(unittest.TestCase):
    def test_the_corpus_is_not_empty(self):
        # An empty directory would make every "for sample in samples" loop
        # vacuously green — the same shape as a skip that overlaps the failure.
        self.assertGreaterEqual(len(_samples()), 8)


class TestUniversalRefusals(unittest.TestCase):
    def test_every_sample_is_sanitisable_and_refuses_the_obvious(self):
        self.assertTrue(_samples(), "corpus missing")
        for path in _samples():
            with self.subTest(sample=path.name):
                raw = path.read_text(encoding="utf-8", errors="replace")
                result = sanitise(raw)
                lower = result.html.lower()
                for bad in _FORBIDDEN:
                    self.assertNotIn(bad, lower, f"{path.name} kept {bad!r}")
                # The document wrapper's CSP is the third barrier.
                page = document(result)
                self.assertIn("default-src 'none'", page)


class TestNamedSamples(unittest.TestCase):
    def sample(self, name: str) -> str:
        return (CORPUS / name).read_text(encoding="utf-8")

    def test_xss_script_keeps_the_words(self):
        r = sanitise(self.sample("xss-script.html"))
        self.assertIn("kept", r.html)
        self.assertNotIn("alert", r.html)

    def test_remote_pixel_is_counted_and_withheld(self):
        r = sanitise(self.sample("remote-pixel.html"))
        self.assertGreaterEqual(r.blocked_remote, 1)
        self.assertNotIn("tracker.evil.example", r.html)
        self.assertIn("body", r.html)

    def test_cid_resolves_when_mapped(self):
        r = sanitise(self.sample("cid-inline.html"),
                     cid_map={"logo@mail": "file:///store/1-logo.png"})
        self.assertIn("file:///store/1-logo.png", r.html)
        self.assertNotIn("cid:missing", r.html)

    def test_newsletter_keeps_the_article_link(self):
        r = sanitise(self.sample("newsletter-mixed.html"))
        self.assertIn("https://example.org/article", r.html)
        self.assertNotIn("metrics.example", r.html)
        self.assertGreaterEqual(r.blocked_remote, 1)

    def test_phishing_form_leaves_the_lure_text(self):
        r = sanitise(self.sample("phishing-form.html"))
        self.assertIn("verify", r.html.lower())
        self.assertNotIn("evil.example", r.html)

    def test_css_exfil_drops_position_escape(self):
        r = sanitise(self.sample("css-exfil.html"))
        self.assertNotIn("evil.example", r.html)
        self.assertNotIn("position", r.html.lower())
        self.assertNotIn("fixed", r.html.lower())


class TestCorpusIsWiredIntoTheSuite(unittest.TestCase):
    def test_every_html_file_has_a_named_or_universal_check(self):
        """A file nobody asserts is a sample that can rot unnoticed.

        Universal refusals cover every file. Named checks cover the ones whose
        interesting property is not in `_FORBIDDEN`. This test only checks that
        the directory's members are exactly the set this module knows about —
        add a file, add its name here (or accept universal-only coverage).
        """
        known = {p.name for p in _samples()}
        # Universal coverage is enough for every member; this pins the set so a
        # silent deletion of half the corpus fails rather than shrinking quietly.
        self.assertEqual(known, {
            "xss-script.html", "xss-svg.html", "xss-js-url.html",
            "remote-pixel.html", "css-exfil.html", "cid-inline.html",
            "phishing-form.html", "redirect-meta.html", "data-svg.html",
            "newsletter-mixed.html",
        })


if __name__ == "__main__":
    unittest.main()
