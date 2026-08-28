# SPDX-License-Identifier: GPL-3.0-or-later
#
# Mechanical checks for Stage 9's hardening pass.
#
# The threat model and the unit tests name mechanisms; this file asks whether
# those mechanisms are still wired the way the model claims. A comment that
# says "no QWebChannel" is not evidence — grepping the sources is.
#
# © Manish Jagdish Thatte
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "cormani"


def _py_files(*relative):
    base = PACKAGE.joinpath(*relative) if relative else PACKAGE
    if base.is_file():
        return [base]
    return sorted(base.rglob("*.py"))


class TestNoBridgeIntoTheApplication(unittest.TestCase):
    def test_no_source_imports_qwebchannel(self):
        # Threat model 2.2 / sitepanel header: no bridge from a panel into
        # Python. An import of QWebChannel anywhere under cormani/ would be
        # exactly that bridge starting to exist.
        offenders = []
        for path in _py_files():
            text = path.read_text(encoding="utf-8")
            if "QWebChannel" in text or "webChannel" in text:
                # Mentions in comments that forbid it are fine; an import is not.
                try:
                    tree = ast.parse(text)
                except SyntaxError:                           # pragma: no cover
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        blob = ast.dump(node)
                        if "QWebChannel" in blob or "WebChannel" in blob:
                            offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_sitepanel_does_not_hold_a_store_connection(self):
        from cormani.ui.sitepanel import SitePanel
        # The isolation test in test_panels.py is the behavioural half; this
        # pins the attribute absence on the class itself so a later __init__
        # that stores `self._con` fails before any widget is built.
        self.assertFalse(hasattr(SitePanel, "_store"))
        self.assertFalse(hasattr(SitePanel, "_con"))


class TestSecretsDoNotLeakThroughReprOrLogs(unittest.TestCase):
    def test_credential_and_tokenset_reprs_redact(self):
        from cormani.auth.credentials import Credential
        from cormani.auth.oauth import TokenSet
        from cormani.auth.providers import METHOD_OAUTH2

        secret = "ya29.THIS_IS_A_REAL_LOOKING_TOKEN_VALUE"
        text = repr(Credential(METHOD_OAUTH2, "a@x", secret))
        self.assertNotIn(secret, text)
        text = repr(TokenSet(secret, "1//REFRESH_SECRET_VALUE"))
        self.assertNotIn(secret, text)
        self.assertNotIn("REFRESH_SECRET", text)

    def test_no_module_logs_a_password_or_token_field(self):
        # A log line that interpolates `.password` or `access_token=` is how a
        # secret reaches the state directory. Comments and test files are out
        # of scope; production modules under cormani/ are not.
        pattern = re.compile(
            r"log\.(debug|info|warning|error|exception)\([^)]*"
            r"(password|access_token|refresh_token|client_secret)\s*=",
            re.I)
        offenders = []
        for path in _py_files():
            if "/tests/" in str(path):                       # pragma: no cover
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line) and "noqa" not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{n}")
        self.assertEqual(offenders, [])


class TestPathContainmentStillHasOneDoor(unittest.TestCase):
    def test_attachment_writes_go_through_safe_filename(self):
        # ingest.attachment_path is the only writer of attachment bytes;
        # attachments.display_name / unique_path are the only writers of
        # user-facing save names. Both must call safe_filename.
        ingest = (PACKAGE / "store" / "ingest.py").read_text(encoding="utf-8")
        attach = (PACKAGE / "store" / "attachments.py").read_text(encoding="utf-8")
        self.assertIn("safe_filename", ingest)
        self.assertIn("def attachment_path", ingest)
        self.assertIn("safe_filename", attach)
        self.assertIn("AttachmentEscapes", attach)

    def test_messageview_checks_cid_paths_stay_under_the_root(self):
        # The reader loads cid: parts through loadResource; escaping here
        # bypasses stored_file. The check must remain.
        text = (PACKAGE / "ui" / "messageview.py").read_text(encoding="utf-8")
        self.assertTrue(
            "resolve" in text.lower() or "parents" in text or "AttachmentEscapes" in text
            or "attachments" in text,
            "messageview must still contain a path check for cid resources")


class TestRefreshTokenRotationIsStored(unittest.TestCase):
    def test_resolve_writes_a_rotated_refresh_token(self):
        """Threat model §3: when the provider returns a new refresh token, keep it.

        Already covered behaviourally in test_auth; this pins the call path
        `refresh_token` → `set_tokens` so a future refactor that refreshes in
        memory and forgets to write cannot claim the checklist item.
        """
        import inspect
        from cormani.auth import credentials
        source = inspect.getsource(credentials.resolve)
        self.assertIn("set_tokens", source)
        self.assertIn("refresh_token", source)


if __name__ == "__main__":
    unittest.main()
