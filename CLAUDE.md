# household-spend-tracker

A self-hosted, multi-currency spend tracker for one household.

The design notes behind this build are private. The standing rules below are
their public summary, and where a comment cites a decision, the rule it led to
is written next to it.

**`CONTRIBUTING.md` is the companion to this file.** This one is about the
code; that one is about the work -- what branches, worktrees and local
directories are called, and how to take a worktree of your own rather than
sharing a checkout with a session you cannot see.

## Standing rules

- **Money is integer minor units.** Never a float, anywhere, ever. `app/money.py`
  owns every conversion.
- **A `DomainError` carries a `code`; `params` are raw values, never formatted
  text.** `detail` stays the English sentence; the code, registered in
  `app/error_codes.py`, is what a translated screen reads. Money goes as minor
  units beside its currency code, a date as ISO, an enum as its value.
  `tests/test_error_codes.py` fails if the number of raises without a code
  goes up -- convert a site, lower the number.
- **Store deliberate acts. Compute consequences.** One stored number per concept.
- **A test that does not assert a changed value is not a test.** Asserting a 200,
  or that a mechanism fired, is not asserting what it did to the data. Four of
  the previous build's nineteen bugs hid behind exactly that.
- **A fix is new code.** It gets reviewed like new code, with a fixture that has
  two of everything.
- **Fixtures have two of everything.** Two users, two households, two accounts,
  two currencies. One card in the fixture is why a refund once funded the wrong
  card; one currency is why three reports added EUR to GBP.
- **If the doc and the code disagree, both are suspect until one is proven.**
- **Commits carry no tool-generated trailer.** No `Co-Authored-By:` naming an
  assistant or a coding tool, no "Generated with" line, in commit messages or
  PR descriptions. The person who commits is the author of record. 46 commits
  had to be rewritten once to take these back out -- do not put them back.
  Human co-authors are welcome to credit each other with `Co-Authored-By:`.

## Rules specific to this build

- **No write reaches an audited table outside a batch.** The `before_flush` hook
  raises if you try. Open one with `with batch(session, kind=..., actor_id=...)`.
- **No bulk statements against audited tables** — `session.execute(update(...))`
  bypasses ORM events, so the audit never sees it. Go through loaded objects.
  CI greps for this.
- **Hard deletes only.** There is no `deleted` flag; the audit log's before-image
  is how a row comes back. Every query that filters on a tombstone is a bug.
- **404, not 403**, for a household a user is not a member of. They should not
  learn the id is real.
- **Every new model is either audited or explicitly excluded.** A test fails
  until you decide.
- **Every new table is also classified for the `/db` snapshot** -- `PURGE`,
  `REDACTIONS` or `CARRIED_WHOLE` in `scripts/db_view.py`. `make snapshot`
  refuses to build while one is unaccounted for, because the alternative is
  copying it into a browsable file exactly as it sits in the ledger.
- **A retention window in a docstring is not a retention window.** Anything
  with an expiry gets swept by `app/auth/housekeeping.py`, which runs on a
  timer from `lifespan`. `ratelimit.prune()` documented thirty days and had no
  caller for the life of the build.
- **Fixtures carry nobody real,** and `tests/test_data_hygiene.py` is what says
  so rather than a docstring. It reads the PDFs and the spreadsheets as text,
  because a PDF keeps its strings in a compressed stream and that is exactly
  how two real ones survived every grep aimed at them.
- **Every list sorts at its column headers.** Not a per-screen decision --
  `SortHeading` in `client/src/components/bits.tsx` is generic over the key so
  a screen sorting rows in hand and one sorting through the server share one
  arrow. Only a cell holding buttons rather than a fact about the row is
  exempt. Sort on the meaning, not the glyph: the country column sorts by
  country name, not by the flag. Money across mixed currencies sorts by
  currency first, then figure.
- **An enum value with no designed behaviour is a bug with a menu item.** The
  previous build shipped twelve account types with three behaviours and five
  loan types it never modelled.

## Layout

    app/models/    base, domain, auth, audit
    app/audit/     registry, hook, snapshot, batch, undo, guard
    app/auth/      passwords, totp, sessions, devices, crypto, setup,
                   ratelimit, email_canonical
    app/services/  the domain logic; routers stay thin
    statements/    reading statement files -- a library, with no import of app/
    app/api/       deps.py holds current_user / current_household / require_owner
    client/        Vite + React + TS, built into app/static/dist
    migrations/    Alembic, from the first commit
    updater/       the self-updater's own container: file contract, journal,
                   restricted engine client -- standard library, no import of app/

## Security posture, in one place

- Responses carry CSP, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  no-referrer` and a `Permissions-Policy`, set by the outermost middleware in
  `main.py` so a refusal carries them too. `/db` and the dev-only doc viewers
  are left out of the CSP because they render HTML this app did not write.
- **`/snap` is the one path whose `Permissions-Policy` differs**, and the only
  one allowed `geolocation=(self)`. It has the location toggle on it; every
  other path on the origin, the whole SPA included, still sends
  `geolocation=()`. The paths are listed exactly in `main.py`, not matched by
  prefix, so a future `/snapshot` does not quietly inherit the permission.
- The CSP allows `style-src 'unsafe-inline'` **on purpose**: `theme.ts` paints
  a household's palette into a `<style>` element. Those values are validated as
  hex before they are stored, so do not relax that validation.
- HSTS is production-only. Pinning a dev browser to HTTPS on localhost costs an
  afternoon and protects nothing.

## Where the checks run

**CI runs on pushes to `main` and on pull requests into `main` or `dev`.**
`.github/workflows/tests.yml` runs ruff, the bulk-statement grep, the full
pytest suite (under pytest-xdist; the migration check is part of it), the seed
smoke test, the client typecheck, vitest and build, Playwright end-to-end at
two widths, a dependency audit and review, the **upgrade rehearsal**, and the
container image on both of its bases -- each only when the change touches what
it checks, and the heavy ones only once per tree (the `green-<tree>` record).
**`ci-ok` is the single check a ruleset should require.** A pull request into
`main` also runs **`release-ready`** (`scripts/release_check.py`): the version
moved past the last tag, the CHANGELOG has a dated section for it with nothing
left under Unreleased, and every migration added since `main` is named in it.
Anything merged into `dev` belongs under `## Unreleased` in the same change.

**There is one git hook, and it is the fast one.** `make hooks` installs
`.githooks/pre-commit`: ruff, the bulk-statement grep, and the client typecheck
plus `vitest` when `client/` changed. Seconds. Anything slow added here will get
the hook bypassed -- it belongs in CI instead. It takes `--no-verify`.

The `pre-push` hook is **gone**. It ran the whole suite on every push to every
branch, which is minutes each time, and the checks it ran now run in CI.

> [!important]
> The `push: main` trigger fires *after* the merge, so on its own it reports on
> `main` rather than protecting it. The `pull_request` triggers are what
> protect. Branch protection is a repository setting, not a workflow one: a
> ruleset on `main` requiring the `ci-ok` and `release-ready` checks is what
> stops a direct push.

### The upgrade rehearsal is the one that is easy to skip

`rehearsal` builds a real database with the *previous* commit's code,
records every table's row count, upgrades to the change under review, and
**asserts the counts did not go down**. `alembic upgrade head` exiting 0 says
the migration ran; it says nothing about whether the ledger survived it. Same
standing rule as everywhere else: a test that does not assert a changed value
is not a test.

**Every migration declares `Reversible: clean|lossy -- <what>`** in its module
docstring, and `tests/test_upgrade_drill.py` fails if one does not.
`make upgrade-check` reads those declarations and is what tells an operator
whether they can roll back. One silently undeclared migration in a list of
fifteen is exactly the one somebody skims past.

**`npm run build` is the client check, not `tsc --noEmit`** -- the latter has
passed a file containing an undefined name.

## Commands

    make preflight   check the toolchain: Python >= 3.12, Node ^22.22.2, ^24.15 or >= 26
    make install     venv + deps, both Python and npm
    make install-py  the venv alone -- the API and the tests need no Node
    make dev         builds the client, then uvicorn on 8848
    make lan         the same, bound to this machine's LAN address
    make api         uvicorn alone; make web  vite on 5173
    make test        pytest with coverage threshold
    make lint        ruff, the bulk-statement grep, and the client typecheck
    make hooks       install the pre-commit hook (see above)
    make migrate     alembic upgrade head
    make seed        a demo household, with credentials printed
    make serve       the production target: no reloader, loopback kept
    make version     print the three version files; BUMP=minor moves them
    make backup      a verified VACUUM INTO copy, plus secret.key
    make upgrade-check   what an upgrade would do. Writes nothing.
    make upgrade     backup, maintenance page, migrate, verify, log
    make restore     FROM=backups/<stamp>, a downloaded .zip or a .sqlite3 [KEY=]
    make audit       pip-audit and npm audit against what is pinned
    make lock        requirements*.txt from requirements*.in, pinned and hashed

**Never back up by copying the database file.** WAL mode keeps recent writes in
`spendtracker.sqlite3-wal`; the main file has been 4 KB while the WAL held
1.7 MB. `make backup` runs `VACUUM INTO` and then reopens the copy to prove it.

**The audit log does not see migrations.** Alembic runs DDL on a plain
connection -- no ORM session, no `before_flush`, no `changes` rows. The backup
is the only thing between a bad migration and a lost ledger, and this project's
strongest feature invites exactly the wrong assumption there. See
`deploy/UPGRADING.md`.

`NODE=` governs every one of these that needs node -- it is resolved to its
directory and put on `PATH`, so it reaches `npm` and `npx` too.
