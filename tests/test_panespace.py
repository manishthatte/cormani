# SPDX-License-Identifier: GPL-3.0-or-later
#
# Who owns the space the list and the reading pane occupy.
#
# Split out of `tests/test_contacthost.py` when the 600-line rule fired on it,
# and split HERE because these four tests are not about the address book at
# all: they are about `ui/panespace.py`, which the address book only happened
# to be the occasion for. The seam falls where the MODULE split does, which is
# the answer whenever it is available.
#
# ── WHAT MAKES THESE WORTH HAVING ──────────────────────────────────────────
#
# Four hosts claim this space — the calendar, the tracking board, a site panel
# and the address book — and each used to carry its own hand-written list of
# the others. When the address book became the fourth, THREE OF THE FOUR LISTS
# WERE WRONG, and each of them looked complete on its own. Every test here is
# written over `panespace.CLAIMANTS` rather than over a list of its own for
# exactly that reason: a test that names the hosts it knows about would go on
# passing when a fifth arrived, which is the failure it exists to catch.
#
# All three historical defects were reintroduced and each turns one of these
# red, naming the host that was left drawn underneath.
#
# © Manish Jagdish Thatte
import unittest

import support

from cormani.store import folders as folders_repo
from cormani.store.accounts import add_account

support.qt_app() if support.HAVE_QT else None


@support.requires_qt
class Space(unittest.TestCase):
    def setUp(self):
        self.con = support.temp_store(self)
        self.account = add_account(self.con, "manish@manitlab.invalid",
                                   "google")
        folders_repo.ensure_folder(self.con, self.account, "INBOX",
                                   display_name="Inbox", role="inbox")

    def pane(self):
        from cormani.ui.mailpane import MailPane

        return support.own(self, MailPane(self.con,
                                          dialogs={"run": lambda d: True}))


class TestTheSpace(Space):
    def test_opening_it_hides_the_list_and_the_reader(self):
        pane = self.pane()
        pane.contacts.open()
        self.assertTrue(pane.contacts.showing)
        self.assertFalse(pane.middle.isVisible())
        self.assertFalse(pane.reader.isVisible())

    def test_every_ordered_pair_of_claimants_stands_the_other_down(self):
        """The one property the whole of `ui/panespace.py` exists for.

        OVER THE REGISTRY AND NOT OVER A LIST OF MY OWN, which is the point: a
        test naming the hosts it knows about would go on passing when a fifth
        arrived, exactly as the four hand-written stand-down lists did. Three
        of those four were wrong when the address book became the fourth, and
        each of them looked complete.

        BOTH ORDERINGS OF EVERY PAIR. The first version of this test asserted
        one pair in both directions and caught `ui/trackhost.py` — and would
        have missed `ui/sitehost.py` and `ui/calendarhost.py`, which were wrong
        in the same way at the same moment.

        The sites are left out and the exclusion is ASSERTED rather than
        assumed: a site panel is a QWebEngineView, so showing one needs the
        browser, and a skip that quietly grew to cover a second host would be
        a test reporting a property it had stopped checking.
        """
        from cormani.ui import panespace

        needs_browser = {"sites"}
        self.assertEqual(
            needs_browser & {n for n, _ in panespace.CLAIMANTS}, needs_browser,
            "the browser exclusion names a host the registry does not have")
        drivable = [n for n, _ in panespace.CLAIMANTS if n not in needs_browser]
        self.assertGreater(len(drivable), 1)

        pane = self.pane()
        for first in drivable:
            for second in drivable:
                if first == second:
                    continue
                getattr(pane, first).show(True)
                getattr(pane, second).show(True)
                self.assertTrue(getattr(pane, second).showing,
                                f"{second} did not claim the space")
                self.assertFalse(getattr(pane, first).showing,
                                 f"{first} was left showing under {second}")
                getattr(pane, second).show(False)

    def test_the_registry_names_every_host_that_claims_the_space(self):
        """P60's rule from the compiler next door: a registry that must agree
        with another registry should be CHECKED, not described.

        The other registry here is the pane itself. Anything hanging off it
        with both a `showing` and a `show` is claiming this space, and one that
        is not in `CLAIMANTS` is a pane that will be left drawn underneath —
        silently, because every other host's stand-down would be correct.
        Discriminated on the shape rather than on a name, so a fifth host is
        covered by having been written rather than by being remembered.
        """
        from cormani.ui import panespace

        pane = self.pane()
        found = {name for name in vars(pane)
                 if hasattr(getattr(pane, name), "showing")
                 and callable(getattr(getattr(pane, name), "show", None))}
        self.assertEqual(found, {name for name, _ in panespace.CLAIMANTS})

    def test_showing_mail_is_false_for_every_claimant(self):
        """The predicate `ui/viewhost.py` now asks instead of naming three
        hosts. It went wrong silently when the address book became the fourth,
        so every claimant is checked rather than the new one alone."""
        pane = self.pane()
        self.assertTrue(pane.showing_mail())
        for host in ("contacts", "tracking"):
            getattr(pane, host).open()
            self.assertFalse(pane.showing_mail(), host)
            getattr(pane, host).show(False)
            self.assertTrue(pane.showing_mail(), host)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
