# Contributing to corMani

© Manish Jagdish Thatte

corMani is a private desktop correspondence client (PySide6 + QtWebEngine).
This guide is for anyone working on the tree at `/home/manish/oss/cormani`.

## Before you change anything

- Read `PLAN.md` for the product direction and `CONVENTIONS.txt` for file-size
  and layering rules.
- UI behaviour is specified in `ui.md`; match existing code style in the file
  you touch.
- Secrets (OAuth tokens, app passwords) go to the system keyring — never into
  the repo, config, or database.

## Environment

Install Debian packages listed in `README.md` (`python3-pyside6.*`,
`python3-keyring`, etc.). Run from the repo root:

```bash
python3 -m unittest discover -s tests -t . -q
```

Tests use an in-memory store and fake keyring; they do not need a display for
most cases. Qt tests call `tests.support.qt_app()` once per class.

## Code layout

| Area | Role |
|------|------|
| `cormani/store/` | SQLite schema and queries — no Qt |
| `cormani/ui/` | Widgets, models, window |
| `cormani/imap/`, `cormani/smtp/` | Sync and send |
| `cormani/panels/` | Embedded site panels |
| `tests/` | Unit tests; `tests/support.py` for fixtures |

Keep store logic free of Qt imports. Wire behaviour through hosts (`mailpane`,
`trackhost`, `contacthost`) rather than growing `window.py`.

## Pull requests and commits

- One logical change per commit when possible.
- Run the full test suite before submitting.
- Do not commit credentials, local stores, or `secrets.toml`.

## Questions

For architecture or UX intent, start with `PLAN.md` and `ui.md`. For CLI
behaviour, see `man/cormani.1` and `cormani/cli.py`.
