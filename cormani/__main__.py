# SPDX-License-Identifier: GPL-3.0-or-later
#
# The command line: arguments in, one call out.
#
# Everything with a body is in `cli.py`. This file is the parser, and it stays
# that way — the moment a command grows a paragraph of its own it belongs next
# to the others rather than here.
#
# WHY UNKNOWN ARGUMENTS ARE NOT SIMPLY PASSED ALONG. Qt reads its own switches
# from `argv` — `-style`, `-platform`, `-qwindowgeometry` — so some arguments
# must survive this parser untouched. `parse_known_args` does that and does one
# thing more: it swallows MISSPELLED and UNIMPLEMENTED options too, and hands
# them to Qt, which ignores them in turn. `--add-acount` then opens a window
# and appears to have worked, which is how a person spends an evening wondering
# why nothing was added.
#
# The rule that fixes it is the one both conventions already imply: Qt's
# switches take a single dash, corMani's take two. Anything unrecognised with
# two dashes is an error; anything with one is Qt's business.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import argparse
import sys

from . import APP_NAME, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cormani",
        description="A correspondence client: mail, calendar, and the sites "
                    "that have no API.",
        epilog="Run from the repository root, or install with pip. Qt's own "
               "switches (-style, -platform) are passed through.")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {__version__}")
    parser.add_argument("--check", action="store_true",
                        help="report the state of this installation and exit, "
                             "without opening a window")
    parser.add_argument("--sync", nargs="?", const="all",
                        choices=("all", "mail", "calendars"), metavar="WHAT",
                        help="fetch once and exit, without opening a window: "
                             "mail and calendars both, which is what F5 does. "
                             "A first mail import takes hours, so say "
                             "'calendars' or 'mail' to run one half of it. A "
                             "first import is the reason this exists — it runs "
                             "in a terminal and can be watched")
    parser.add_argument("--calendars", nargs="?", const="", metavar="ADDRESS",
                        help="report every calendar this store knows about — "
                             "what is in it, what window has been fetched, and "
                             "whether the next pass will be incremental — and "
                             "exit. Read-only and offline, like --check. Name "
                             "an address to see only that account's")
    parser.add_argument("--filters", nargs="?", const="", metavar="ADDRESS",
                        help="report every filter rule, in the order they run "
                             "— what each one looks at, what it does, and how "
                             "often it has matched anything — and exit. "
                             "Read-only and offline, like --check. Name an "
                             "address to see only the rules that run against "
                             "it")
    parser.add_argument("--contacts", nargs="?", const="", metavar="QUERY",
                        help="report the address book — everybody, their "
                             "handles, and which cards look like the same "
                             "person twice. Give a QUERY to narrow it to one "
                             "name, organisation or address, which also turns "
                             "on the per-contact mail counts")
    parser.add_argument("--searches", action="store_true",
                        help="report every saved search — what each one asks "
                             "for, how much mail it holds, and whether it "
                             "still names a folder that exists — and exit. "
                             "Read-only and offline, like --check")
    parser.add_argument("--quiet", action="store_true",
                        help="with --sync, print only the per-account summary")
    parser.add_argument("--reindex", action="store_true",
                        help="build the derived data again from the messages "
                             "already stored — the search index and the "
                             "conversations — and exit. For when --check "
                             "reports that some of them are not searchable or "
                             "not placed; nothing is fetched and nothing is "
                             "lost")
    parser.add_argument("--demo", action="store_true",
                        help="open a disposable store of demo data instead of "
                             "your mail. It lives in the cache directory; "
                             "delete that file to reset it")

    group = parser.add_argument_group("setting up an account")
    group.add_argument("--add-account", metavar="ADDRESS",
                       help="add one account, prove the credential works, and "
                            "only then write anything. Asks for an app "
                            "password, or opens a browser for OAuth")
    group.add_argument("--provider", choices=("google", "microsoft", "imap"),
                       help="required unless the address is at a well-known "
                            "consumer domain. A custom domain says nothing — "
                            "a Google Workspace address looks like any other")
    group.add_argument("--auth", choices=("oauth2", "password"),
                       help="how to authenticate. Defaults to OAuth where the "
                            "provider offers it. Google still accepts an app "
                            "password for mail; Microsoft accepts none")
    group.add_argument("--name", metavar="TEXT", default="",
                       help="the display name for this account in the rail")
    group.add_argument("--imap-host", metavar="HOST", default="")
    group.add_argument("--imap-port", metavar="PORT", type=int, default=0)
    group.add_argument("--smtp-host", metavar="HOST", default="")
    group.add_argument("--smtp-port", metavar="PORT", type=int, default=0)
    group.add_argument("--resync", metavar="ADDRESS",
                       help="throw away one account's cached messages and "
                            "fetch them again. For when a parser has been "
                            "fixed since they were stored; the server is the "
                            "truth and nothing is lost")
    group.add_argument("--set-oauth", metavar="PROVIDER",
                       choices=("google", "microsoft"),
                       help="record this installation's OAuth client id and "
                            "secret for a provider. One registration covers "
                            "every account on it")
    group.add_argument("--import-thunderbird", nargs="?", const="",
                       metavar="PATH",
                       help="copy mail from a Thunderbird profile (or an "
                            "ImapMail directory, or a single mbox file) into "
                            "a corMani account. The source is opened "
                            "read-only. Requires --into. With no PATH, looks "
                            "under ~/.thunderbird")
    group.add_argument("--into", metavar="ADDRESS", default="",
                       help="with --import-thunderbird, the configured "
                            "account that receives the imported mail")
    group.add_argument("--include-junk", action="store_true",
                       help="with --import-thunderbird, also import Junk and "
                            "Trash folders (skipped by default)")
    desktop = parser.add_argument_group("appearing in the desktop")
    desktop.add_argument("--install-desktop", action="store_true",
                         help="put corMani in this user's applications list, "
                              "with its icon, so it can be started from the "
                              "overview and pinned to the dash")
    desktop.add_argument("--uninstall-desktop", action="store_true",
                         help="take it out of the applications list again")

    return parser


def _reject_unknown(parser: argparse.ArgumentParser, rest: list) -> None:
    """Anything with two dashes that this parser did not recognise is an error.

    Single-dash arguments pass through to Qt untouched; see the note at the top
    of this file for why the distinction is drawn here rather than left to
    `parse_known_args` alone.
    """
    unknown = [a for a in rest if a.startswith("--")]
    if unknown:
        parser.error(f"unrecognised option: {unknown[0]}")


def main(argv: list[str] | None = None) -> int:
    try:
        return _dispatch(argv)
    except KeyboardInterrupt:
        # Interrupting a password prompt is how a person says "not now". A
        # stack trace makes it look as though something broke, and buries the
        # one fact that matters: nothing was written. 130 is the shell's own
        # convention for a command ended by SIGINT.
        print("\ninterrupted — nothing was changed", file=sys.stderr)
        return 130


def _dispatch(argv: list[str] | None) -> int:
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)
    _reject_unknown(parser, rest)

    from . import calcli, cli, configure, contactcli, importcli, rulecli, viewcli

    if args.check:
        return cli.check()

    # `is not None` and not truthiness: the value is an ADDRESS and the empty
    # string is the meaning "every account", which is what a bare --calendars
    # supplies. Testing it for truth would make the bare form fall through to
    # opening a window, which is the failure this file's header is about.
    if args.calendars is not None:
        return calcli.calendars(args.calendars)

    # `is not None` and not truthiness, for the reason --calendars is: the
    # value is an ADDRESS and "" means every account.
    if args.filters is not None:
        return rulecli.filters(args.filters)

    # A plain flag and not an optional ADDRESS, unlike --filters and
    # --calendars: a saved search is not scoped to an account. Its own scope
    # may name one, but that is a property of the search rather than a way of
    # narrowing the list, and a --searches ADDRESS would have to invent a
    # meaning for "every account" views that nobody asked for.
    if args.searches:
        return viewcli.searches()

    # `is not None` and not truthiness, for the reason --calendars and
    # --filters are: the value is a QUERY and "" means everybody, which is what
    # a bare --contacts supplies. Testing it for truth would make the bare form
    # fall through to opening a window.
    if args.contacts is not None:
        return contactcli.contacts(args.contacts)

    if args.set_oauth:
        return configure.set_oauth(args.set_oauth)

    # `is not None`: a bare --import-thunderbird means "discover under home",
    # which is the empty string, and must not fall through to the window.
    if args.import_thunderbird is not None:
        return importcli.import_thunderbird(
            args.import_thunderbird, into=args.into,
            include_junk=args.include_junk)

    if args.add_account:
        return configure.add_account(
            args.add_account, provider=args.provider or "",
            auth=args.auth or "", display_name=args.name,
            imap_host=args.imap_host, imap_port=args.imap_port,
            smtp_host=args.smtp_host, smtp_port=args.smtp_port)


    if args.install_desktop:
        return configure.install_desktop()

    if args.uninstall_desktop:
        return configure.uninstall_desktop()

    if args.reindex:
        return cli.reindex()

    if args.resync:
        return cli.resync(args.resync)

    if args.sync:
        return cli.sync(verbose=not args.quiet, what=args.sync)

    from .app import run
    return run([sys.argv[0], *rest], demo=args.demo)


if __name__ == "__main__":
    sys.exit(main())
