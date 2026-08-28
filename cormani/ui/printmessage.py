# SPDX-License-Identifier: GPL-3.0-or-later
#
# Print the message being read.
#
# The reading pane already chose plain text over a browser for security; printing
# uses the same body the pane shows, laid out on paper through Qt's print
# support rather than through a second HTML engine.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from PySide6.QtGui import QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import QWidget


def print_message(*, subject: str, correspondent: str, when: str, body_html: str,
                  body_plain: str, parent: QWidget | None = None) -> bool:
    """Offer a print dialog for one message. Returns whether printing ran."""
    document = QTextDocument()
    plain = body_plain.strip() or body_html
    document.setHtml(
        f"<h2>{_escape(subject)}</h2>"
        f"<p><b>{_escape(correspondent)}</b> — {_escape(when)}</p>"
        f"<hr>"
        f"{body_html if body_html.strip() else _plain_to_html(plain)}")
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    dialog = QPrintDialog(printer, parent)
    dialog.setWindowTitle("Print message")
    if dialog.exec() != QPrintDialog.DialogCode.Accepted:
        return False
    document.print_(printer)
    return True


def export_pdf(*, subject: str, correspondent: str, when: str, body_html: str,
               body_plain: str, path: str, parent: QWidget | None = None) -> bool:
    """Write the message to a PDF file."""
    from PySide6.QtGui import QPageLayout, QPageSize
    from PySide6.QtCore import QMarginsF

    document = QTextDocument()
    plain = body_plain.strip() or body_html
    document.setHtml(
        f"<h2>{_escape(subject)}</h2>"
        f"<p><b>{_escape(correspondent)}</b> — {_escape(when)}</p>"
        f"<hr>"
        f"{body_html if body_html.strip() else _plain_to_html(plain)}")
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    layout = QPageLayout(QPageSize(QPageSize.PageSizeId.A4),
                         QPageLayout.Orientation.Portrait,
                         QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter)
    printer.setPageLayout(layout)
    document.print_(printer)
    return True


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _plain_to_html(text: str) -> str:
    return "<pre>" + _escape(text) + "</pre>"
