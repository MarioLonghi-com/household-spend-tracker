# household-spend-tracker

[![tests](https://github.com/MarioLonghi-com/household-spend-tracker/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/MarioLonghi-com/household-spend-tracker/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/MarioLonghi-com/household-spend-tracker)](https://github.com/MarioLonghi-com/household-spend-tracker/releases/latest)
[![licence: AGPL-3.0-or-later](https://img.shields.io/badge/licence-AGPL--3.0--or--later-blue)](LICENSE)
[![python: 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)

**Spend Tracker** is a self-hosted spend tracker for one household.
Multi-currency, fully audited, and meant to be reachable only over a tailnet.

## What it is, and what it is not

It is a ledger for one household's accounts and transactions, and the
machinery for getting transactions in: manual entry, statement import (CSV,
OFX, PDF and XLS) with duplicate control, payee rules, transfers found and
linked across accounts, receipts, work expenses and their reimbursements,
reports, a one-time import from YNAB, and an HTTP API a program can use with a
key a person issued. There is no budget layer.

It is **not** a hosted service, not a bank connection (it reads the statement
files you download, and never logs in anywhere), and not built for the open
internet: the threat model assumes a private network in front of it. See
[Security](#security).

Two things ship with it rather than after it, because neither can be retrofitted
honestly:

- **Every change is attributable and reversible.** Nothing reaches an audited
  table outside a *batch* — an import, a bulk edit, one keystroke in the
  register. Each batch records what it did, row by row, with the before and
  after images that make "undo that import" work.
- **No default credentials.** A fresh instance has no accounts and cannot be
  signed into; the setup wizard creates the first one and will not finish
  without a working authenticator.

## Documentation

This README covers running and deploying the app. The rest of the written
documentation:

| File | What it covers |
| --- | --- |
| [`docs/DATA-MODEL.md`](docs/DATA-MODEL.md) | How the data is stored: the groups of tables and the rules every table follows. |
| [`deploy/DOCKER.md`](deploy/DOCKER.md) | Running it in a container, including seeding a demo. |
| [`deploy/TROUBLESHOOTING.md`](deploy/TROUBLESHOOTING.md) | Where the ledger is, which one is open, starting again, and getting back into an account. |
| [`deploy/UPGRADING.md`](deploy/UPGRADING.md) | The upgrade drill: backup, migrate, verify, and how to roll back. |
| [`agent/README.md`](agent/README.md) | Letting a program use the ledger over HTTP with an agent key. |
| [`statements/README.md`](statements/README.md) | The statement-reading library and its command-line tool. |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed in each release, and whether each change can be reversed. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How work is named, branched and handed over. |
| [`CLAUDE.md`](CLAUDE.md) | The standing rules for changing the code. |
| [`SECURITY.md`](SECURITY.md) | How to report a vulnerability. |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | How people taking part are expected to behave, and how to report it when they do not. |

## What you need

| | | |
|---|---|---|
| **Python** | >= 3.12 | CI runs 3.12; 3.14 works |
| **Node** | ^22.22.2, ^24.15 or >= 26 | jsdom 30's floor (vite 8 needs less); CI runs 22, and so does `.nvmrc` |
| SQLite | bundled with Python | nothing to install |

`make preflight` checks both and says what is wrong in a sentence. It is run for
you by `make install`.

**Node is only for the SPA.** The API, the migrations, the seed and the whole
test suite need Python and nothing else:

```bash
make install-py && make seed && make api
```

## Running it

```bash
make install
make seed
make dev
```

- `make install` — Python venv + client/node_modules
- `make seed` — a demo household, and credentials printed to sign in with
- `make dev` — builds the client, then serves on http://localhost:8848

Use `make migrate` instead of `make seed` if you would rather start empty and
walk the setup wizard yourself — it prints a setup token to the log on first
boot.

`make dev` builds the client into `app/static/dist` before starting the API.
That directory is gitignored, so on a fresh clone there is no UI until something
builds it and `/` answers 404 — the server logs a warning saying exactly that.
`make api` and `make web` run the two separately when you want Vite's hot reload
on 5173.

### From another machine on the same network

```bash
make lan
```

- `make lan` — binds 0.0.0.0:8848 and prints the address to use

For a phone at the till on `/snap`, or a laptop on the sofa. It is `make dev`
with four changes that have to happen together, which is why it is a target
rather than a note telling you to add `--host`:

- **`--host 0.0.0.0`** — every interface, not just loopback.
- **`SPENDTRACKER_COOKIE_SECURE=off`** — a browser will not *store* a `Secure`
  cookie from `http://192.168.1.50:8848`. `http://localhost` is a trustworthy
  origin; a LAN address is not. Without this the sign-in answers 200, the
  cookie is silently dropped, and you land back on the sign-in screen with
  nothing in any log to say why.
- **no `--reload`** — this one is meant to be left running while people use it.
- **no API docs** — it runs as production, so `/api/docs`, `/api/redoc` and
  `/api/openapi.json` are not mounted: they hand the whole route inventory to
  anyone on the network without signing in. `make lan DOCS=1` if you want them
  there for an evening.

> [!WARNING]
> `make lan` is **plain HTTP**. The password and the session cookie cross the
> network in clear, and anything else on that network can read them. That is a
> reasonable trade on a home network you control, for an evening, and a bad one
> anywhere else. It is not a deployment mode: for that use `tailscale serve`,
> which terminates TLS and leaves the cookies `Secure` where they belong.

`SPENDTRACKER_COOKIE_SECURE` defaults to on and is never set by any other
target. An instance with it off also stops sending HSTS, because pinning every
browser on the network to HTTPS for a year, at an address that only answers
plain HTTP, would make the app unreachable at the address it just advertised.

### Which host names it answers to

Every request's `Host` header is checked against
**`SPENDTRACKER_ALLOWED_HOSTS`**, a comma-separated list, and anything else gets
`400 Invalid host header`. Without the check a page on a domain somebody else
controls could re-point its own DNS at this machine (DNS rebinding) and talk to
the app as though it were same-origin — and `/llms.txt` and the agent
descriptor would render their URLs from whatever `Host` it sent.

Unset, the default covers every way the app is normally reached:

| Entry | Covers |
| --- | --- |
| `localhost`, `127.0.0.1`, `[::1]` | `make dev`, `make serve`, the container healthcheck |
| `ip` | any address typed as an IP literal: `make lan`, a tailnet `100.x` address, a container published on the host's address |
| `*.ts.net` | `tailscale serve`, the intended deployment |
| `testserver` | the test suite's client |

`ip` is safe to allow because rebinding needs a *name* the attacker controls; a
browser pointed at an address sends that address. Reaching the app by any other
name — a `.local` mDNS name, a reverse proxy's own domain — means setting the
variable, which **replaces** the default list rather than adding to it:

```bash
SPENDTRACKER_ALLOWED_HOSTS="localhost,127.0.0.1,ip,*.ts.net,spend.example.org"
```

`*.example.org` matches any subdomain. `*` on its own switches the check off,
which is not a thing to do on anything reachable from a browser.

### Behind a proxy

Two settings decide what the app believes about where a request came from and
where it lives. Both matter to the sign-in rate limits, which count failures
per address, and to the links the app hands out.

**`FORWARDED_ALLOW_IPS`** is uvicorn's own: the peers whose `X-Forwarded-For`
and `X-Forwarded-Proto` it believes. Its default is loopback.

| How it runs | Set it to | Why |
| --- | --- | --- |
| the container (`compose.yaml`) | already set: `172.16.0.0/12` | requests arrive from the Docker bridge, not loopback; trusting the bridge makes each tailnet peer its own address |
| `make serve` with `tailscale serve` in front | `127.0.0.1` (the default) | `tailscale serve` connects from loopback and sets the header |
| `make serve` with nothing in front | `FORWARDED_ALLOW_IPS=""`, or run uvicorn with `--no-proxy-headers` | otherwise any local user can send `X-Forwarded-For` and pick a fresh address for every guess |

**Never `*`.** That lets every caller name its own address, which is the rate
limit switched off.

**`SPENDTRACKER_PUBLIC_URL`** is the origin people type, such as
`https://spend.your-tailnet.ts.net`. Invitation links, `/llms.txt` and the
agent descriptor are built from it. Unset, they are built from the request,
which behind a proxy that is not trusted comes out as `http://` and the
proxy's address: an invitation link `tailscale serve` does not answer. A value
that is not an `http(s)://host[:port]` origin stops the app at boot.

> [!IMPORTANT]
> **Back up `secret.key` with the database.** It sits beside
> `spendtracker.sqlite3` in the data directory (see below). It is generated on first
> boot and it encrypts every TOTP secret. Lose it and every household member
> must re-enrol their authenticator: each signs in with a recovery code -- a
> trusted browser does not skip it -- and sets up a new one, and anyone with no
> code left is given one from the server with `scripts.reset_authenticator`.
> That recovery code signs the member out everywhere, forgets their trusted
> browsers and revokes their agent keys, and the original key turning up later
> does not bring those back. Passwords, recovery codes and the ledger are not
> encrypted with the key, and losing it costs none of them.

> [!IMPORTANT]
> **Do not back up `spendtracker.sqlite3` by copying it.** The database runs in
> WAL mode, so recent writes live in `spendtracker.sqlite3-wal` until a
> checkpoint folds them in. In the previous build the main file was 4 KB while
> the `-wal` file held 1.7 MB — copying the one file would have backed up an
> empty database and nobody would have found out until they needed it. Take a
> consistent copy instead:
>
> ```bash
> make backup
> ```
>
> which also keeps `secret.key` beside the copy.

## Deploying your own

Three routes. They are not exclusive, and the container is the recommended one
because it makes the upgrade story trivial and separates data from code by
force rather than by documentation.

### A container

```bash
SPENDTRACKER_AUTO_MIGRATE=1 docker compose up -d
docker compose up -d
```

- `SPENDTRACKER_AUTO_MIGRATE=1 docker compose up -d` — first run only
- `docker compose up -d` — every time after

**[`deploy/DOCKER.md`](deploy/DOCKER.md) is the full guide** — seeding a demo
database, attaching a ledger you already have, backing up, upgrading, and what
to do when it will not start. Every command on it has been run against a real
daemon on both base images.

No toolchain at all — not Python, not Node, not a matching version of either.
The ledger lives on a named volume at `/var/lib/spend-tracker`, and the port is
published to **`127.0.0.1` only**.

That last part is deliberate and worth not undoing. `tailscale serve` proxies
from your tailnet into loopback and terminates TLS there, so binding loopback
is exactly what makes this unreachable except through the thing holding the
certificate. Publishing `8848:8848` instead would put your ledger on every
interface in plain HTTP — and because the cookie is `Secure`, the browser would
refuse to store it and the sign-in would *loop silently* rather than fail
visibly. **Never Tailscale Funnel**: that is the public internet.

The image is built on Chainguard's Wolfi-based Python, whose runtime variant
carries no shell and no package manager. If you would rather not:

```bash
docker compose build --build-arg PY_BASE=python:3.12-slim \
                     --build-arg PY_RUN=python:3.12-slim
```

Both are built in CI, so the bypass is tested rather than promised.

### A release tarball

Needs Python 3.12+ and nothing else — the client is prebuilt inside, so there
is no Node to install:

```bash
curl -L https://github.com/MarioLonghi-com/household-spend-tracker/releases/latest/download/household-spend-tracker.tar.gz | tar xz
cd household-spend-tracker-*
make install-prod && make migrate && make serve
```

`make install-prod` installs the runtime dependencies and nothing else — no
test, lint or audit tools in a deployment's virtualenv. `make install-py` is
the contributor's version, and the one to use if you want the `/db` snapshot
browser, which needs Datasette.

### A clone

What contributors use. Needs Python **and** Node, because `app/static/dist` is
gitignored and a fresh clone has no UI until it is built.

```bash
make install && make migrate && make seed && make serve
```

`make serve` is the production target: no `--reload`, and the loopback bind
kept. `make dev` is the one with the reloader.

### Where your data lives

```bash
make upgrade-check
```

- `make upgrade-check` — prints the data directory it resolved

Two files matter, and exactly two: `spendtracker.sqlite3` (with its `-wal` and
`-shm`) and `secret.key`. The second encrypts every TOTP secret — lose it and
every member re-enrols their authenticator. Recovery codes are hashes, not
encrypted with it, so they still sign people in; see
[`deploy/TROUBLESHOOTING.md`](deploy/TROUBLESHOOTING.md#4-secretkey-is-lost-or-wrong).

The default is `~/.local/share/spend-tracker`, **outside the checkout**.

> [!warning]
> `git clean -xdf` deletes ignored files, and `data/` is ignored.
> That command is the standard reflex for "my build is in a weird state, start
> clean", and in the old layout it took the ledger, the WAL and `secret.key`
> with it. If you have an existing `./data`, it still works and is not moved —
> but it is inside the blast radius of that command and the new default is not.

**Everything in it belongs to the account the app runs as, and nobody else.**
The process runs with a `0077` umask, so the database and its WAL, the logs,
the snapshot and every backup are created `0600` and their directories `0700`.
The data directory is tightened to `0700` at startup, and `./backups` whenever
a backup is taken, even if they already existed. Files an older version made keep their modes — the app
names them in the log at startup rather than changing them behind your back:

```bash
chmod -R go-rwx ~/.local/share/spend-tracker backups
```

### Backing up, and upgrading

```bash
make backup
make upgrade-check
make upgrade
make restore FROM=backups/20260101-120000
make restore FROM=spendtracker-20260101T120000Z.zip KEY=/path/to/secret.key
```

The second shape is a backup downloaded from *Application management*. The
zip carries a README that says the same thing, for whoever finds it later.
`KEY=` is only needed when the zip was made without the key and is being
restored somewhere other than the instance it came from.

- `make backup` — verified: it reopens the copy
- `make upgrade-check` — what an upgrade would do. Writes nothing.
- `make upgrade` — backup, maintenance page, migrate, verify

**Do not back up by copying the database file.** SQLite is in WAL mode, so
recent writes sit in `spendtracker.sqlite3-wal` until a checkpoint folds them
in — in the previous build the main file was 4 KB while the WAL held 1.7 MB.
`make backup` runs `VACUUM INTO`, which reads across both, and then reopens the
copy and counts its rows before reporting success.

It also runs `PRAGMA integrity_check`, and after an upgrade it opens one real
sealed TOTP secret with `secret.key` — because the wrong key leaves every row
readable and every authenticator refused, and nothing says so until somebody
tries to sign in.

For a rolling three, and for an off-site copy that deduplicates:

```bash
python -m scripts.backup --keep 3
python -m scripts.backup --method backup --into /mnt/offsite
```

`VACUUM INTO` repacks the file, so an incremental sync stores all of it again
every week — 2.5 MB of new receipts costing 666 MB of transfer, measured.
`--method backup` preserves page numbering and reuses 98.5% of blocks.

`make upgrade-check` names every migration between your database and this code
and says whether rolling each one back would lose anything. Read it before you
upgrade, not after.

**[`deploy/UPGRADING.md`](deploy/UPGRADING.md) is the full drill**, including
the three rollback cases and the one thing that surprises people: the audit
log, which makes every other mistake in this app undoable, does not see
migrations at all. Alembic runs on a plain connection with no ORM session and
no `before_flush` hook. The backup is the only thing between a bad migration
and a lost ledger.

### Versions

`MAJOR.MINOR.PATCH`, where the three map onto the classes this project
releases by:

| | |
|---|---|
| **major** | a code overhaul, a UI overhaul, a backend restructure, or any change a downgrade cannot undo |
| **minor** | a new feature, or a functional change to one that exists |
| **technical** | a dependency bump, a security fix, patching, a bug fix |

```bash
make version
make version BUMP=technical
```

- `make version` — what the three version files say
- `make version BUMP=technical` — move all three together

**Before 1.0.0, a change a downgrade cannot undo is a minor, not a major.** The
0.x series is the one where the shape is still settling; the CHANGELOG's
`Reversible: lossy` line is what tells an operator, not the first digit. From
1.0.0 the table above holds as written.

`curl -s localhost:8848/api/health` is what proves which code is running after
an upgrade: `version` names the release and `commit` the exact commit, which
is what tells two builds between releases apart. The version only proves it
because `tests/test_version.py` fails when the three files drift apart.

#### Cutting a release

`main` only moves at a release, so a pull request from `dev` into `main` is
one, and CI's `release-ready` job refuses it until it looks like one
(`scripts/release_check.py`):

1. On a branch off `dev`: `make version BUMP=minor` (or `technical`, `major`).
2. In `CHANGELOG.md`, retitle `## Unreleased` to `## X.Y.Z — YYYY-MM-DD` and
   put an empty `## Unreleased` above it. The section needs a
   `**Reversible: none|clean|lossy**` line naming **every migration added since
   the last release** -- the check lists any it forgets.
3. Merge that into `dev`, then open the pull request `dev` → `main`. When it is
   green, merge it.
4. Tag the merge on `main` and push the tag. `release.yml` checks that
   `tests.yml` already passed on exactly this tree, checks the tag against the
   version and the CHANGELOG, builds the tarball and the container image,
   attests both, and publishes them -- the tarball to the release, the image
   to `ghcr.io/mariolonghi-com/household-spend-tracker`.

   ```bash
   git tag -a vX.Y.Z -m "X.Y.Z" origin/main && git push origin vX.Y.Z
   ```

Run the same check locally before opening the pull request:

```bash
./.venv/bin/python -m scripts.release_check --since origin/main
```

## Looking at the database

What the tables are for, and the rules they follow, is in
[`docs/DATA-MODEL.md`](docs/DATA-MODEL.md).

```bash
make db-view
```

Two ways in, both over the same **redacted snapshot** rather than the live file.

**In the app, at `/db`** — behind the session check, so it needs the same
password, the same authenticator and the same 30-day device trust as the
ledger itself, and is therefore safe on the tailnet. Owner only: raw tables
have no notion of household, so `transactions` is every household at once and
the 404-not-403 scoping the API relies on does not apply. It mounts only once a
snapshot exists, so a deployment that never runs `make snapshot` never serves
it; `SPENDTRACKER_DB_VIEW=off` turns it off outright.

**On its own port**, for poking at the file without running the app:

```bash
make db-view
```

Datasette on <http://127.0.0.1:8899>, **loopback only** — on its own it has no
authentication at all and will run arbitrary read-only SQL for whoever reaches
it. Do not give it `--host 0.0.0.0`; use `/db` for that.

Either way it is the snapshot, for three reasons:

- The live file is not the whole database. WAL mode keeps recent writes in
  `spendtracker.sqlite3-wal` until a checkpoint, so a viewer opened on the main
  file alone shows stale data with no error at all.
- The live file is being written to, and a viewer that opens it read-write
  competes with the server for the lock.
- It holds credential material. `scripts/db_view.py` blanks every password
  hash, TOTP secret, recovery-code hash and session, device and invitation
  token before the snapshot is written, so what is served cannot leak one
  however it is reached — including through Datasette's own SQL console, which
  is why the file is redacted rather than a page being hidden. The ledger
  itself is still there; that is the point of looking.

Every table is classified — emptied, blanked, or carried whole — and
`make snapshot` **refuses to run** while one is unaccounted for, rather than
copying a table nobody has thought about into a file you are about to point a
browser at. The sign-in attempt log is one of the emptied ones: it is a record
of what happened at the door, not of the ledger, and it is the only table that
carries an email address and an IP for every attempt made.

**Since receipts, the snapshot also holds locations.** A photograph taken at a
till usually carries a GPS fix, and those coordinates are parsed into
`receipts` and carried whole — so one `SELECT` ordered by date produces a
movement history for whoever photographed them. They are not redacted, because
`/db` is owner-only behind the same password, authenticator and device trust as
the ledger, and anybody who can read the snapshot can open the receipt itself
in the app with a map link beside it; blanking them would protect nothing and
would make `/db` disagree with the app about what the row says. The picture
itself is never in the file — `receipt_blobs` is one of the emptied tables. If
that trade is not one you want to make, `SPENDTRACKER_DB_VIEW=off` is the
control.

`make snapshot` builds the file without starting a server. The viewer needs
`datasette`, which is a development dependency and not a runtime one — on a
production install `/db` says so in a sentence rather than failing.

## Tests

```bash
make test
make e2e
```

- `make test` — pytest with the coverage gate, then the client's units
- `make e2e` — a real browser, desktop and phone

`make e2e` builds the client, seeds a **throwaway** instance in `./e2e-data`,
serves it on 8850 and drives Chromium against it. It never touches `./data`.

Every spec runs twice, once at each width. That is not thoroughness for its own
sake: the nav is a drawer on a phone and a sidebar on a desktop, and the phone
layout was unusable for the life of the build without a single test noticing,
because nothing had ever looked at it.

> [!note]
> The sign-in helper retries once, in the next 30-second window, and that is
> two deliberate protections rather than flake. `make seed` finishes the setup
> wizard by presenting a real TOTP code, which **burns that timestep**; and a
> wrong code spends the single-use pending sign-in, so the retry has to start
> again from the password rather than resubmit. Both stay in the path every
> test goes through.

## Letting a program use it

A script, an automation or an AI assistant can read this ledger over plain
HTTP, with a key a household member issues from **Your account → Keys for
programs**. The token is shown once and starts `stk_`.

    curl -H "Authorization: Bearer stk_..." https://your-instance/api/agent/v1/manifest

The app speaks HTTP and JSON and knows about no vendor and no protocol, so
anything that can set a header is a first-class client. [`agent/README.md`](agent/README.md)
has the whole thing with real requests and responses, and
[`agent/mcp/`](agent/mcp/) is a working sample — a standard-library client plus
an MCP server in about a hundred lines, importing nothing from the app.

A running instance also serves **`/llms.txt`** to anybody, with no key: what
this is, how a person issues one, and the conventions worth knowing before
writing anything. It is the only thing served to a caller holding nothing.

No key can delete, undo, issue another key, or touch authentication — those
routes ask for a signed-in person. Everything a key does appears in History
under both the person's name and the key's.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md) — how work is named and handed over, and
how several people or programs work on this at once without standing on each
other. [`CLAUDE.md`](CLAUDE.md) is the companion piece, and it is about the code
rather than the work. Taking part means following the
[code of conduct](CODE_OF_CONDUCT.md).

## Security

Reporting a vulnerability: [`SECURITY.md`](SECURITY.md). In short, this is
designed for a tailnet and never for the open internet, and that assumption is
load-bearing rather than incidental.

## Licence

**GNU Affero General Public License v3.0 or later.** The full text is in
[`LICENSE`](LICENSE).

Copyright (C) 2026 Mario Longhi.

The Affero clause is the reason this licence and not plain GPL: section 13 says
that if you run a **modified** version and let other people use it over a
network, those users must be offered the source of the version they are talking
to. Running it unmodified for your own household — which is what this is for —
asks nothing of you. Publishing a fork and inviting others onto it does.
