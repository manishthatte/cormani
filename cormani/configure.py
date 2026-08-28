# SPDX-License-Identifier: GPL-3.0-or-later
#
# Setting corMani up: an account, a provider registration, a desktop entry.
#
# Split from cli.py because the two are different jobs. `cli.py` OPERATES a
# corMani that is already configured — report, fetch, re-fetch — and everything
# here is what has to happen once, before any of that means anything.
#
# ASKING IS INJECTED, NEVER CALLED DIRECTLY. `ask` and `ask_secret` are
# parameters so that the whole of setting up can be tested without a terminal —
# and so that the Qt dialog calls the SAME functions with its own prompts
# rather than reimplementing them. It does: `ui/accountsetup.py` runs
# `add_account` on a worker thread with the form's values behind those two
# names, which is why the window cannot drift from the command line about what
# adding an account MEANS.
#
# SO IS SAYING. `out` is the third of the same kind, added when the dialog
# arrived: everything here reports by printing, and a window cannot read that
# without redirecting the whole process's stdout — which one thread may not do
# to the others. A caller that passes `out` gets the same lines, in order, as a
# callback.
#
# THEY DEFAULT TO None AND ARE RESOLVED INSIDE THE FUNCTION. Writing
# `ask_secret=_ask_secret` in the signature binds the default once, at import,
# so patching the module attribute has NO effect — and a test that believes it
# has replaced the prompt silently blocks on the real terminal instead. That
# happened; hence this.
#
# A PASSWORD IS VERIFIED BEFORE ANYTHING IS WRITTEN. It is held in memory, used
# to connect, and only stored once the server has accepted it. A failed setup
# therefore leaves NOTHING behind: no half-made account row, and no credential
# in the keyring for an address that does not work.
#
# © Manish Jagdish Thatte
from __future__ import annotations

import getpass
import re
from pathlib import Path

# Consumer domains where the provider is not a guess. A custom domain says
# nothing — owner@manitlab.example is Google Workspace and manitlab.example is not in
# any list — so inference stops here and `--provider` is asked for instead.
_KNOWN_DOMAINS = {
    "gmail.com": "google", "googlemail.com": "google",
    "hotmail.com": "microsoft", "outlook.com": "microsoft",
    "live.com": "microsoft", "msn.com": "microsoft",
    "hotmail.co.uk": "microsoft", "outlook.in": "microsoft",
}


def _ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    return input(shown).strip() or default


def _ask_secret(prompt: str) -> str:
    """Never echoed, never logged, never kept anywhere but the keyring."""
    return getpass.getpass(f"{prompt}: ").strip()


def _say(line: str) -> None:
    """One line of commentary, on the terminal.

    Flushed, because the lines worth having are the ones printed BEFORE
    something slow: "connecting to …" says nothing once the connection has
    already been made or refused.
    """
    print(line, flush=True)


def _sink(out):
    """Where this run's commentary goes.

    A PARAMETER FOR THE SAME REASON THE PROMPTS ARE. The Qt dialog runs
    `add_account` on a worker thread and shows the commentary in the window; a
    function that could only report by printing would have to be reimplemented
    behind that window, or have the whole process's stdout redirected
    underneath it — which is not something one thread may do to the others.
    Defaults to None and is resolved inside the function, so that patching the
    module attribute works; see the note at the top of this file about binding
    a default at import.
    """
    return out if out is not None else _say


def infer_provider(address: str) -> str:
    """The provider a consumer address obviously belongs to, or empty.

    Empty rather than a guess: a wrong provider means the wrong hostnames and
    the wrong authentication mechanism, and the resulting failure looks like a
    rejected password. Saying "I do not know" costs one command-line flag.
    """
    domain = address.rpartition("@")[2].strip().lower()
    return _KNOWN_DOMAINS.get(domain, "")


def set_oauth(provider_name: str, *, client_id: str = "", client_secret: str = "",
              ask=None, ask_secret=None, out=None) -> int:
    """Record this installation's own OAuth registration for a provider.

    ONE registration per PROVIDER, not per account: a single Google Cloud
    project and a single Azure app registration cover all accounts on that
    provider. It goes to the keyring, which is why no client id or
    secret appears anywhere in this repository — and why the repository can be
    published while the registration cannot.
    """
    from .auth import credentials, providers

    ask, ask_secret = ask or _ask, ask_secret or _ask_secret
    say = _sink(out)
    provider = providers.get(provider_name)
    if not provider.supports_oauth:
        say(f"{provider.label} does not use OAuth; there is nothing to record")
        return 1

    say(f"OAuth registration for {provider.label}.")
    if provider.name == "google":
        say("  The project must be published to Production, not left in")
        say("  Testing, or its refresh tokens expire every seven days.")
        say("  Unverified is fine. The application type is 'Desktop app'.")
    client_id = client_id or ask("  Client ID")
    if not client_id:
        say("nothing recorded")
        return 1
    # Prompted with getpass even though a desktop client's "secret" ships in
    # every copy and is therefore not one. It is still a credential-shaped
    # string, and a terminal's scrollback is a place credentials should not be.
    client_secret = client_secret or ask_secret("  Client secret (blank if none)")

    credentials.set_registration(provider.name, client_id, client_secret)
    say(f"recorded for {provider.label}; it covers every {provider.label} "
          f"account on this machine")
    return 0


# ------------------------------------------------------------ add an account
def add_account(address: str, *, provider: str = "", auth: str = "",
                display_name: str = "", imap_host: str = "", imap_port: int = 0,
                smtp_host: str = "", smtp_port: int = 0,
                ask=None, ask_secret=None, open_browser=None,
                out=None) -> int:
    """Add one account, prove it works, and only then write anything.

    The order is the point. The credential is obtained, held in memory, and
    used to connect and list the folders; the account row and the stored secret
    come afterwards. A setup that fails therefore leaves NOTHING behind — no
    half-made account, and no keyring entry for an address that does not work.
    Anything else and the next `--sync` inherits a broken account that has to
    be found and removed by hand.
    """
    from .auth import credentials, providers
    from .auth.credentials import NotConfigured
    from .app import current_paths
    from .auth.providers import METHOD_OAUTH2, METHOD_PASSWORD
    from .imap import folders as folder_sync
    from .imap.client import Connection
    from .imap.errors import ImapError, describe
    from .store import accounts as accounts_repo
    from .store import database

    ask, ask_secret = ask or _ask, ask_secret or _ask_secret
    say = _sink(out)
    address = (address or "").strip().lower()
    if "@" not in address:
        say(f"{address!r} is not an email address")
        return 1

    if not provider:
        provider = infer_provider(address)
        if not provider:
            say(f"cannot tell which provider {address} belongs to — a custom "
                  f"domain says nothing. Pass --provider google, microsoft or imap")
            return 1
        say(f"provider: {provider} (from the address)")
    spec = providers.get(provider)

    # `current_paths` and not `Paths()`: an account added here must land in the
    # store the WINDOW opens, and `data_dir` is what decides which that is.
    paths = current_paths().ensure()
    con = database.open_store(paths.database)
    try:
        if accounts_repo.find_by_address(con, address) is not None:
            say(f"{address} is already configured")
            return 1

        host = imap_host or spec.imap_host or ask("IMAP host")
        if not host:
            say("an IMAP host is needed")
            return 1
        port = int(imap_port or spec.imap_port or 993)

        method = auth or providers.default_method(provider)
        if method == METHOD_PASSWORD and not spec.allows_password:
            say(f"{spec.label} no longer accepts an app password; "
                  f"use --auth oauth2")
            return 1

        try:
            credential = _obtain(address, spec, method, ask_secret,
                                 open_browser, say)
        except NotConfigured as exc:
            say(str(exc))
            return 1
        except EOFError:
            # No terminal to ask on — a pipe, a cron job, a hook. Saying
            # "sign-in failed" here sends someone to look at their password.
            say("no terminal to ask on; run this from a shell")
            return 1
        except Exception as exc:
            say(f"sign-in failed: {describe(exc)}")
            return 1

        say(f"connecting to {host}:{port} …")
        try:
            connection = Connection.connect(host, port, address=address)
        except ImapError as exc:
            say(f"could not connect: {describe(exc)}")
            return 1
        try:
            credentials.authenticate(connection, credential)
            mailboxes = connection.list_mailboxes()
        except ImapError as exc:
            say(f"the server refused: {describe(exc)}")
            return 1
        finally:
            connection.logout()

        # Accepted. Now, and only now, is anything written.
        if method == METHOD_PASSWORD:
            credentials.set_password(address, credential.secret)
        account_id = accounts_repo.add_account(
            con, address, provider, display_name=display_name,
            imap_host=host, imap_port=port,
            smtp_host=smtp_host or spec.smtp_host,
            smtp_port=int(smtp_port or spec.smtp_port or 587),
            auth_method=method)

        # THE ACCOUNT IS ALREADY ADDED BY THIS POINT, so a failure here is not
        # a failure to add it. The listing is a second connection and can fail
        # on its own — a server that dropped the first one, a network that went
        # away between the two — and letting the exception out would report a
        # complete account as an error, which is the one thing worse than not
        # listing its folders. The next sync lists them again.
        try:
            report = folder_sync.sync_folders(
                con, _reconnect(host, port, address, credential), account_id)
        except ImapError as exc:
            say(f"added {address}, but its folders could not be listed: "
                f"{describe(exc)}")
            say("")
            say("now run:  python3 -m cormani --sync")
            return 0
        roles = {path: role for path, role in report.roles.items()}
        say(f"added {address} — {len(mailboxes)} mailboxes, "
              f"{len(roles)} with a role")
        for path, role in sorted(roles.items(), key=lambda kv: kv[1]):
            say(f"    {role:<8} {path}")
        # `say("")` and not `say()`: the sink takes one argument, always. A
        # blank line is a line. The Qt dialog's sink is a signal's `emit`,
        # which raises on the wrong arity — and it raised AFTER the account had
        # been written, so the attempt reported failure for something that had
        # already succeeded.
        say("")
        say("now run:  python3 -m cormani --sync")
        return 0
    finally:
        con.close()


def _obtain(address, spec, method, ask_secret, open_browser, say):
    """The credential, in memory. Nothing is stored by this function.

    Except for OAuth, where the token exchange stores what it receives — those
    tokens are valid whatever happens next, and making the browser flow run a
    second time to recover from an unrelated IMAP failure would be unkind.
    """
    from .auth import credentials
    from .auth.credentials import Credential
    from .auth.providers import METHOD_OAUTH2, METHOD_PASSWORD

    if method == METHOD_PASSWORD:
        say(f"An app password for {address}.")
        if spec.name == "google":
            say("  Google calls these App Passwords, at myaccount.google.com"
                  " → Security. Sixteen letters, spaces ignored.")
        secret = ask_secret("  App password")
        if not secret:
            raise credentials.NotConfigured("no password given")
        return Credential(method=METHOD_PASSWORD, user=address,
                          secret=secret.replace(" ", ""))

    say(f"Opening a browser to authorise {address} with {spec.label}…")
    tokens = credentials.sign_in(address, spec.name, open_browser=open_browser)
    return Credential(method=METHOD_OAUTH2, user=address,
                      secret=tokens.access_token)


def _reconnect(host, port, address, credential):
    """A second connection, for the folder listing that follows verification.

    Separate because the first one has been logged out — deliberately, so that
    the verification really is a complete connect-authenticate-list-disconnect
    rather than a session left open and reused.
    """
    from .auth import credentials as auth
    from .imap.client import Connection

    connection = Connection.connect(host, port, address=address)
    auth.authenticate(connection, credential)
    return connection


# ------------------------------------------------ appearing in the desktop
#
# Three things have to line up before a window can be pinned, and two of them
# are easy to get wrong in a way that LOOKS like it worked:
#
#   the .desktop entry     so the application exists to the shell at all
#   the icon, in hicolor   so `Icon=cormani` resolves to something
#   the Wayland app_id     so the RUNNING WINDOW is recognised as that entry
#
# The third is the one that bites. `StartupWMClass` is an X11 mechanism and
# Wayland ignores it; GNOME matches on the xdg-shell app_id, which Qt takes
# from `QGuiApplication.setDesktopFileName` — see app.py. Without it the
# launcher and the window are two unrelated things: the icon appears in the
# grid, launches correctly, and then a SECOND anonymous entry appears in the
# dash which cannot be pinned and does not go away.
_DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=corMani
GenericName=Correspondence Client
Comment=Mail, calendar and correspondence across every channel you use
Exec={exec_line}
{path_line}Icon={icon}
Terminal=false
Categories=Network;Email;
Keywords=email;mail;imap;smtp;calendar;correspondence;messaging;
StartupNotify=true
StartupWMClass=cormani
MimeType=x-scheme-handler/mailto;message/rfc822;text/x-vcard;text/calendar;
"""


def _exec_line() -> tuple:
    """How to start corMani from a launcher, and from which directory.

    An installed `cormani` on PATH is preferred, because that is what a
    packaged corMani looks like and it does not depend on where the source
    happens to live. Where there is none — pip is not installed on this
    machine, so today there is none — the module is run directly, with `Path=`
    setting the working directory, since `python3 -m cormani` from anywhere
    else finds the repository directory instead of the package.
    """
    import shutil
    import sys

    found = shutil.which("cormani")
    if found:
        return f"{found} %u", ""
    root = Path(__file__).resolve().parent.parent
    return f"{sys.executable} -m cormani %u", f"Path={root}\n"


def install_desktop(*, paths=None) -> int:
    """Put corMani in the applications list, so it can be pinned.

    Per-user, under XDG_DATA_HOME. Installing into /usr needs root, and
    something a person runs from their own source tree should not require it.
    """
    from .platform.paths import Paths
    from .platform.runtime import resource

    paths = paths or Paths()
    exec_line, path_line = _exec_line()

    icon_source = resource("cormani.svg")
    icon_name = "cormani"
    if icon_source is None:
        # An absolute path in `Icon=` is legal and is the honest fallback: the
        # entry still works, it simply cannot be re-themed.
        print("warning: data/cormani.svg was not found; the entry will have "
              "no icon")
        icon_name = "application-x-executable"
    else:
        paths.icon_theme.mkdir(parents=True, exist_ok=True)
        target = paths.icon_theme / "cormani.svg"
        target.write_bytes(icon_source.read_bytes())
        print(f"icon    {target}")

    paths.applications.mkdir(parents=True, exist_ok=True)
    entry = paths.applications / "cormani.desktop"
    entry.write_text(_DESKTOP_ENTRY.format(
        exec_line=exec_line, path_line=path_line, icon=icon_name),
        encoding="utf-8")
    entry.chmod(0o755)
    print(f"entry   {entry}")
    print(f"        Exec={exec_line}")

    _refresh_caches(paths)
    print()
    print("corMani is now in the applications list. Start it from there — not "
          "from this terminal —")
    print("then right-click it in the dash and choose Pin to Dash.")
    return 0


# The marker that says corMani wrote this file. An index.theme somebody else
# installed is never touched; one of ours is refreshed, because a directory
# that appears later must be added to it or every icon in that directory
# becomes invisible.
_INDEX_MARKER = "# Written by corMani. Safe to delete or replace."

_SIZE_DIR = re.compile(r"^(\d+)x\1$")


def _theme_sections(root: Path) -> list:
    """Every icon directory in the tree, as (relative path, Size, Type).

    ENUMERATED AND NOT ASSUMED, and that distinction cost VSCodium its icon
    for ten minutes. GTK's lookup consults `Directories=` to learn which
    subdirectories are part of the theme and at what size; a directory missing
    from that list is a directory whose icons cannot be found, however
    correctly they are installed and however completely
    `gtk-update-icon-cache` indexed them. An index.theme naming only the
    directory corMani installs into therefore HIDES everything anybody else
    put there.
    """
    sections = []
    for context in sorted(p for p in root.iterdir() if p.is_dir()):
        for kind in sorted(p for p in context.iterdir() if p.is_dir()):
            relative = f"{context.name}/{kind.name}"
            match = _SIZE_DIR.match(context.name)
            if match:
                sections.append((relative, int(match.group(1)), "Fixed"))
            elif context.name == "scalable":
                sections.append((relative, 48, "Scalable"))
    return sections


def _index_theme(root: Path) -> str:
    sections = _theme_sections(root)
    lines = [_INDEX_MARKER, "[Icon Theme]", "Name=Hicolor",
             "Comment=Fallback icon theme",
             "Directories=" + ",".join(name for name, _s, _t in sections), ""]
    for name, size, kind in sections:
        lines += [f"[{name}]", f"Size={size}", "Context=Applications",
                  f"Type={kind}"]
        if kind == "Scalable":
            lines += ["MinSize=8", "MaxSize=512"]
        lines.append("")
    return "\n".join(lines)


def _ensure_icon_theme(root: Path) -> str:
    """Give the per-user hicolor tree a correct `index.theme`.

    Without one the directory is not a valid icon theme, and
    `gtk-update-icon-cache` refuses to build a cache for it — which is how an
    installation ends up with a STALE cache it cannot replace. The system's own
    /usr/share/icons/hicolor/index.theme does not help: GTK reads the index
    from the same directory as the icons it is looking at.

    An index.theme somebody ELSE wrote is left alone; one of ours is rewritten
    every time, so that a directory added since is in it.
    """
    root.mkdir(parents=True, exist_ok=True)
    index = root / "index.theme"
    if index.exists():
        existing = index.read_text(encoding="utf-8", errors="replace")
        if _INDEX_MARKER not in existing:
            return ""
        wanted = _index_theme(root)
        if existing == wanted:
            return ""
        index.write_text(wanted, encoding="utf-8")
        return "refreshed"
    index.write_text(_index_theme(root), encoding="utf-8")
    return "created"


def _refresh_caches(paths) -> None:
    """Ask the desktop to notice, and SAY whether it did.

    GNOME rescans these directories by itself, so the commands only make it
    immediate — but the icon cache is not merely an optimisation and the
    difference cost an evening. GTK treats an existing `icon-theme.cache` as
    AUTHORITATIVE: a cache older than the icon beside it does not fall back to
    scanning the directory, it reports that the icon is not there. Installing
    the icon and failing to rebuild the cache therefore leaves an application
    with NO icon and every file correctly in place.

    So a failure is reported rather than swallowed — CONVENTIONS.txt §8 — and
    a cache that could not be rebuilt is DELETED, because no cache is better
    than a stale one. Shelling out with an argument array rather than a string
    is §7.
    """
    import subprocess

    root = paths.icon_theme.parent.parent
    wrote = _ensure_icon_theme(root)
    if wrote:
        print(f"theme   {root / 'index.theme'}  ({wrote}, "
              f"{len(_theme_sections(root))} directories)")

    try:
        subprocess.run(["update-desktop-database", str(paths.applications)],
                       check=False, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass

    cache = root / "icon-theme.cache"
    try:
        done = subprocess.run(["gtk-update-icon-cache", "-qf", str(root)],
                              check=False, capture_output=True, timeout=20)
        ok = done.returncode == 0
        detail = (done.stderr or b"").decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        ok, detail = False, str(exc)

    if ok:
        print(f"cache   {cache}")
        return
    # The stale one is worse than none: it is what the shell believes.
    if cache.exists():
        cache.unlink()
        print(f"cache   removed {cache} — it was stale and could not be "
              f"rebuilt")
    else:
        print("cache   not built (gtk-update-icon-cache is absent)")
    if detail:
        print(f"        {detail.splitlines()[0]}")
    print("        The icon still resolves by directory scan; this only "
          "makes it slower.")


def uninstall_desktop(*, paths=None) -> int:
    """Take it out of the applications list again."""
    from .platform.paths import Paths

    paths = paths or Paths()
    removed = []
    for target in (paths.applications / "cormani.desktop",
                   paths.icon_theme / "cormani.svg"):
        try:
            target.unlink()
            removed.append(str(target))
        except OSError:
            pass
    for line in removed:
        print(f"removed {line}")
    if not removed:
        print("nothing was installed")
    _refresh_caches(paths)
    return 0
