#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# md2txt — render the project's Markdown documents as plain text.
#
# The documents are read far more often than they are edited, and they are read
# in a terminal and a plain editor. Markdown's pipe tables and asterisks are
# noise there. This renders headings as underlined text, tables as aligned
# columns, and reflows prose to a fixed width.
#
# The one non-obvious rule is in the table renderer: when a table is too wide
# for the page, earlier columns are capped and the LAST column wraps onto
# continuation lines. An earlier version truncated instead, which silently
# deleted the end of every sentence in the widest column — a table that looks
# fine and says something different from the source is worse than an ugly one.
#
# © Manish Jagdish Thatte
import pathlib
import re
import sys
import textwrap

WIDTH = 78


def inline(t: str) -> str:
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", t)
    return t


def render_table(rows, width=WIDTH):
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    natural = [max(len(r[c]) for r in rows) for c in range(n)]
    gap, indent = 2, 2

    if indent + sum(natural) + gap * (n - 1) <= width:
        widths = natural
    else:
        # Cap every column but the last; give the last whatever is left and let
        # it wrap. Never truncate — see the header comment.
        cap = max(10, (width - indent - gap * (n - 1)) // (n + 1))
        widths = [min(natural[c], cap) for c in range(n - 1)]
        widths.append(max(18, width - indent - sum(widths) - gap * (n - 1)))

    out = []
    for ri, r in enumerate(rows):
        parts = [textwrap.wrap(r[c], widths[c]) or [""] for c in range(n)]
        for line_no in range(max(len(p) for p in parts)):
            cells = [(parts[c][line_no] if line_no < len(parts[c]) else "").ljust(widths[c])
                     for c in range(n)]
            out.append(" " * indent + (" " * gap).join(cells).rstrip())
        if ri == 0:
            out.append(" " * indent + (" " * gap).join("-" * w for w in widths))
    out.append("")
    return out


def to_text(md: str, width: int = WIDTH) -> str:
    lines = md.split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]

        if ln.startswith("```"):                      # fenced block, verbatim
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                out.append("    " + lines[i])
                i += 1
            i += 1
            continue

        if ln.startswith("#"):                        # heading, underlined
            lvl = len(ln) - len(ln.lstrip("#"))
            txt = inline(ln.lstrip("# ").strip())
            if lvl <= 2:
                txt = txt.upper()
            out += ["", txt, ("=" if lvl == 1 else "-" if lvl == 2 else ".") * len(txt)]
            i += 1
            continue

        if ln.startswith("|"):                        # table
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [inline(c.strip()) for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                out += render_table(rows, width)
            continue

        if ln.strip() in ("---", "***", "___"):
            out += ["", "-" * width, ""]
            i += 1
            continue

        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)      # list item
        if m:
            ind, mark, rest = m.group(1), m.group(2), m.group(3)
            # A list item may span several source lines. Consume the
            # continuations here: if they fall through to the paragraph branch
            # they are re-wrapped as prose and lose the hanging indent, which
            # is how a bullet ends up with one line jammed against the margin.
            i += 1
            while (i < len(lines) and lines[i].strip()
                   and lines[i].startswith((" ", "\t"))
                   and not re.match(r"^\s*([-*]|\d+\.)\s", lines[i])):
                rest += " " + lines[i].strip()
                i += 1
            bullet = "- " if mark in "-*" else f"{mark} "
            first = " " * len(ind) + bullet
            out += textwrap.wrap(inline(rest), width, initial_indent=first,
                                 subsequent_indent=" " * len(first)) or [first.rstrip()]
            continue

        if not ln.strip():
            out.append("")
            i += 1
            continue

        para = []                                     # paragraph, reflowed
        while (i < len(lines) and lines[i].strip()
               and not lines[i].startswith(("#", "|", "```", "-", "*", ">"))
               and not re.match(r"^\s*\d+\.", lines[i])):
            para.append(lines[i].strip())
            i += 1
        if para:
            out += textwrap.wrap(inline(" ".join(para)), width)
        else:
            out.append(inline(ln))
            i += 1

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def main(argv):
    if len(argv) < 2:
        print("usage: md2txt.py FILE.md [FILE.md …]  — writes FILE.txt beside each",
              file=sys.stderr)
        return 2
    for a in argv[1:]:
        src = pathlib.Path(a)
        dst = src.with_suffix(".txt")
        dst.write_text(to_text(src.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"{src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
