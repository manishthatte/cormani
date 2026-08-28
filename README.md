# corMani

An installable correspondence client: fifteen email accounts, calendars, and the
messaging sites that have no API, in one window — with a tracking layer
underneath that knows which conversations are owed a reply and which have a
deadline that cannot slip.

© Manish Jagdish Thatte · GPL-3.0-or-later, with a commercial licence available.
See `LICENSE` and `NOTICE`.

The licence is free software from the outset, because the ambition is the Debian
source tree and a licence is expensive to change late.

- `docs/threat-model.txt`, `docs/stage9-audit.txt`, `docs/deps-watch.txt` — security and hardening notes
- `cormani/` — the application

## Google and Microsoft OAuth

Each installation registers its own OAuth client once — a Google Cloud project
or an Azure app registration — and corMani stores the client id and secret in
the system keyring, not in this repository. One registration covers every
account on that provider.

For Google: create a **Desktop app** OAuth client, publish the project to
**Production** (not Testing — refresh tokens otherwise expire every seven
days), and point the consent screen at your own privacy policy and homepage.
For corMani published by maniTLab, the public pages are
[product](https://www.manitlab.org/cormani/),
[privacy](https://www.manitlab.org/privacy/), and
[terms](https://www.manitlab.org/terms/).

Record the registration once:

```
python3 -m cormani --set-oauth google
```

Then add accounts with `--add-account you@example.org --provider google` or
**File ▸ Add mail account…** in the window. Workspace domains such as
`you@yourcompany.example` always need `--provider google` explicitly.

## Running it

From the repository root — the directory holding `pyproject.toml`. The repository
and the package share the name `cormani`, so running from one level up makes
Python find the repository directory instead of the package, and report that
`cormani` "cannot be directly executed".

```
python3 -m cormani --check     # report the installation; needs no display
python3 -m cormani --add-account you@example.org --provider google
python3 -m cormani --import-thunderbird --into you@example.org
python3 -m cormani --sync      # fetch mail and calendars once, and exit
python3 -m cormani --sync calendars           # or one half of it
python3 -m cormani --calendars # what each calendar holds; read-only, offline
python3 -m cormani --filters   # every filter rule, and what it has caught
python3 -m cormani --resync you@example.org   # re-fetch, after a parser fix
python3 -m cormani --reindex   # build the search index and conversations again
python3 -m cormani --install-desktop          # so it can be found and pinned
python3 -m cormani --demo      # the interface over demonstration data
python3 -m cormani             # your mail
python3 -m unittest discover -s tests -t . -q
```

`--demo` opens a separate, disposable store in the cache directory. Your real
store is not opened at all, so the demonstration data cannot reach it.

`--add-account` asks for an app password (or opens a browser for OAuth),
connects, and writes the account only once the server has accepted the
credential — so a failed attempt leaves nothing behind. `--provider` is needed
for anything but a well-known consumer domain, because a custom domain says
nothing about who hosts it.

`--import-thunderbird` copies mail from a Thunderbird profile (or an ImapMail
directory, or a single mbox file) into a configured account named with
`--into`. The source is opened read-only — Thunderbird may stay running — and
a second run only reads bytes appended since the last. Junk and Trash are
skipped unless `--include-junk` is given.

The window does the same thing from **File ▸ Add mail account…**, and it is the
same code: the dialog fills in the provider and its hostnames as you type the
address, refuses what it can tell is wrong before touching the network, and
hands the form to `--add-account` on a worker thread. **File ▸ OAuth
registration…** records this installation's own client id, which one Google
Cloud project or Azure app registration provides for every account on that
provider. Both go to the system keyring and nowhere else.

New mail is announced through the desktop's notification service — the same
path calendar reminders already use — unless a filter filed it, marked it
read, or asked for silence. Closing the window hides to the system tray when
one is there; **File ▸ Quit** ends the process.

Filters run on mail as it arrives, in the Inbox of the account they name.
A rule is conditions and actions — move, tag, flag, mark, put on the tracking
board, or say nothing about it — and every change it makes is queued for the
server exactly as a click would be, because a message filed here and left in
the Inbox there is a message the next sync files again, for ever. Write them in
**Tools ▸ Message filters**; `Ctrl+Shift+F`. A rule can be tried before it is
saved, against the mail already here, which is the only honest way to check a
guess about a pattern in mail nobody has read.

`--filters` exists because a filter is invisible when it works and invisible
when it does not: mail a rule moved and mail no rule looked at are both just
mail in a folder. It prints every rule in the order they run, with the number
of times each has matched anything — so a rule that stopped matching, because a
correspondent changed their address, can be told apart from one that had
nothing to match.

`--check` also reports the tracking layer — what is being pursued, what is
owed, and every deadline within a month — and the site panels: what the
embedded browser is, how current its security patches are, which sites are
turned on, and where each site's session is kept.

The messaging sites appear as **panels**, because WhatsApp, LinkedIn, X and
Facebook publish no interface for personal conversations — not restricted, not
paid, absent. So corMani shows the web application a person signs into and does
nothing else with it: no reverse-engineered protocol client, no automation that
reads or sends a message, both of which get the account banned. The one
exception is the unread count, read from the number already in the page's
title so the rail can carry a badge. Each site has its own cookies, its own
cache and its own signed-in session, and cannot see another's; a panel holds no
connection to the message store. The panels are optional and are meant to stay
that way — the embedded browser comes from the distribution rather than being
bundled, so it ages, and mail and calendars must be unaffected the day a site
refuses it. Sign a site out from the **Panels** menu.

Tracking is what corMani is for. A *thread* is a conversation you are pursuing:
a state, a next action, a nudge cadence, possibly a deadline, and a timeline of
everything that happened on it across every channel — including the telephone
calls and meetings a mailbox cannot hold. `Ctrl+Shift+O` opens it in a tab.
**Owed** is a fact rather than a guess — they answered last and you did not —
and a call logged on the thread answers it, because it did. A deadline is never
folded into a nudge: no amount of polite reminding satisfies a filing date.
Mail that belongs to no thread waits in a queue beside the board rather than
being dropped.

`--sync` is how a first import is actually run. It takes hours for a large
account, is resumable by design, and exits non-zero if any account failed in
either half. The same work happens on F5 in the window, off the interface
thread — F5 means everything, now, and so does this. Name `mail` or `calendars`
to run one half, which is worth having while a first mail import is still
going.

Dependencies are Debian packages, never `pip` into a virtualenv:
`python3-pyside6.qtwidgets`, `python3-pyside6.qtwebenginewidgets`,
`python3-pyside6.qtsvg`, `python3-pyside6.qtprintsupport`, `python3-keyring`.

© Manish Jagdish Thatte
