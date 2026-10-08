# Changelog

Every entry carries a **Reversible** field, because the question an operator
has at 23:00 is not what changed — it is whether they can go back.

| Value | What it means |
|---|---|
| `none` | no migration in this release. Check out the old tag and restart. |
| `clean` | migrations ran and every one of them can be undone without loss. |
| `lossy` | at least one migration cannot be undone. The only faithful way back is restoring the backup, and everything written since the upgrade goes with it. The entry names what is destroyed. |

Run `make upgrade-check` against your own instance before upgrading. It reads
the same declarations out of the migrations themselves and reports only the
ones between your database and the release you are moving to.

The three release classes are the ones the review asked for, mapped onto
SemVer: **major** is a code or UI overhaul or a backend restructure, **minor**
is a new feature or a functional change to one, and **technical** is a
dependency bump, a security fix, patching or a bug fix.

Issue numbers written in backticks (`` `#NNN` ``) refer to the project's
original private tracker and are kept for the record; they are not issues in
this repository.

**Public releases start at 0.6.2**, the first one tagged and published from
this repository. The sections from 0.6.1 down were released from the project's
earlier, private repository and are kept here as history: they have no tag
and no release on this one, deliberately, because a tag would point at
history this repository does not have.

---

## Unreleased

### Fixed

- **Safari can sign in at `http://localhost`.** The cookies carried the
  `__Host-` prefix, which Safari refuses on plain-HTTP `localhost` while
  keeping an unprefixed `Secure` cookie, so the container on your own
  computer answered the sign-in, lost the cookie and showed the sign-in
  screen again, with nothing in any log. At `localhost`, `127.0.0.1` and
  `[::1]` the cookies are now named without the prefix and are still
  `Secure`; every other address, the tailnet included, keeps it. HSTS is no
  longer sent over plain HTTP to those three, where browsers ignore it.
  After upgrading, expect to sign in at `localhost` once more, code
  included, in any browser: the old names are no longer read there. (#196)

### Changed

- **A fresh install starts without `SPENDTRACKER_AUTO_MIGRATE=1`.** A first
  `docker compose up -d` against a new volume used to be refused until you
  passed the flag once from a terminal. A database with no tables at all --
  or none yet -- is now migrated on its first start, with one line in the log
  saying so, because there is nothing in it to lose. Every other mismatch is
  still refused, including a database with tables but no migration stamp and
  one stamped at a revision the code does not know. The flag keeps its
  default (off) and its meaning: a deliberate migration of an existing ledger,
  which `make upgrade` does with a backup first (#167).

- **The recovery code for an update is issued by `POST`**
  (`/api/admin/application/update/recovery-code`), not `GET`. Issuing one
  replaces the code held for the prepared update, so a cross-site `GET` could
  rotate it and make *Update* fail; as a `POST` it is behind the Origin check.
- **The updater's heartbeat sentence reaches the screen**: `GET
  /api/admin/application/update` carries `heartbeat.socket_sentence`.

- **Short messages carry a note for the translator.** Every message of one
  or two words, and any whose English alone is ambiguous, says in one line
  what it is -- a button, a column heading, a state, which sense of
  "Balance" -- through Lingui's own `comment`, so it reaches every
  language's catalog. "New" and the authenticator's "Set up" now have a
  context of their own, because they need different words in other
  languages. The rule is in `client/src/locales/README.md`, and the catalog
  tests refuse a bare short message. English is unchanged. (#228)

- **"Check the repository" reads published releases, not tags,** and returns
  every release newer than the running one, newest first, each with its
  notes: the release's CHANGELOG section where the release body leads with
  it, and an older release's body as it is. Drafts, prereleases and any tag
  that is not `vX.Y.Z` are never offered -- a tag can exist with no image
  behind it, as 0.3.1's did. Still one request, only when the button is
  pressed, saying nothing about the instance. (#165)

- **The remaining screens' words go into the catalogs one screen at a time**,
  starting with Categories. English is unchanged; each screen has a test
  that renders it in the `en-XA` pseudo-locale and finds no English left.
  (#56)

- **The transfer panel's words are in the catalogs.** English is unchanged;
  a test renders it in the `en-XA` pseudo-locale and finds no English left.
  (#55)

- **Draft translations of the first screens** in pt-BR, es-ES and sv-SE:
  every message extracted so far, each marked `#, fuzzy` until a native
  speaker reviews it (#58). None is served or selectable; a test checks every
  catalog entry is valid ICU and keeps the English placeholders. (#175, #176,
  #177)

- **The first screens' words are in the catalogs:** the shell and its menu,
  signing in, step-up, recovery codes, resetting a sign-in, the profile and
  passkeys, setting up the instance and accepting an invitation, plus the
  shared panel, hint and dialog furniture and the sign-in-changes notice.
  Sentences built from fragments are whole sentences now, one per case, so
  each can be translated as it is read. English is unchanged, and a test
  renders each of these screens in the `en-XA` pseudo-locale and finds no
  English left. (#54)

- **The client's words can come from translation catalogs** (Lingui 6,
  `client/src/locales/`). The menu, the sort headings' tooltip and the
  "try again in" wait are the first messages extracted; a refusal that
  carries a code shows the catalog's message in another language and the
  server's sentence in English, as before. English is the only language
  served and the language picker in Profile → Appearance stays hidden; the
  `en-XA` pseudo-locale is reachable for CI and development, and one
  Playwright pass runs in it at phone width. CI fails when the catalogs are
  behind the source. Nothing an English reader sees changes. (#53)

### Added

- **The Updates section on Admin → Application** (#166), where *Is there a
  newer version?* was. It says which case this instance is in: a checkout
  (update from the terminal), a container with no updater (how to start it,
  naming the container the heartbeat named), an updater the engine refuses
  (the updater's own sentence), one the engine has outgrown (*Update the
  updater*), or a working one. A check offers the newest release with every
  skipped release's notes as plain text, and any other newer release from a
  menu; *Update the updater only* when a newer updater exists. Preparing shows
  the updater's progress every two seconds. The confirmation lists the
  migrations with one tick-box per migration a downgrade cannot undo, shows a
  one-time recovery code with *Download as a file* and *I have saved it*, and
  asks for the password and a code; *Update* stays disabled until every box is
  ticked. While it updates a full-width panel watches `/api/health`, reloads
  when the app is back, and after 30 minutes points at the recovery page. The
  outcome stays at the top of the section until dismissed.
- **Update backups are listed under Database** with their version and
  migration, apart from the backups made by hand. Each downloads; only those
  older than the newest five can be deleted, and the server still refuses
  the five.

- **The app's side of self-update, API only** (#165). Owner-only endpoints
  under `/admin/application/update` -- a member gets 403 and nothing is
  written: the updater's heartbeat, status, current prepare report, newest
  outcome and update backups (`GET`, no outbound request); `prepare`; a
  one-time recovery code for the confirmation; `apply`, which spends a
  step-up grant first and must accept exactly the report's lossy migrations;
  `discard`; `updater`, to replace the updater only, with an optional newer
  release; and dismissing an outcome. Each writes one request into the
  shared `update` volume (`SPENDTRACKER_UPDATE_DIR`, default
  `/var/lib/spend-tracker-update` in the container), atomically, group-shared,
  never over a request not yet taken; the app checks what it can first, so a
  refusal is a sentence at once. The recovery code is 140 random bits shown
  once; only its scrypt hash travels, and the code is never written or
  logged. Prepared, confirmed (with the lossy migrations accepted),
  discarded and deleted-backup events are logged at WARNING with the owner's
  email. The backups listing includes update backups (folders) with their
  size, version and revision, and the newest five cannot be deleted (409).
  The Updates section of the screen follows in #166.

- **The self-updater's core, not yet wired to anything** (#158). A new
  top-level package, `updater/`, standard library only: the file contract
  between the app and the updater in the shared `update` volume (requests,
  heartbeat, status, prepare reports, history), strict request validation that
  refuses anything but a published release newer than the running one and
  makes no engine call when it refuses, a journal that records which step of
  an apply has started and which updater owns it, deadlines that do not count
  time the machine slept, and an engine client that can make only a listed set
  of calls, negotiates the engine's API version, and refuses privileged
  containers, host mounts and host networking whoever asks. Tested against a
  recording fake engine on a real unix socket and `/version` answers recorded
  from Docker Desktop and Podman. No image, compose service or screen uses it
  yet.
- **The self-updater knows which engine it is on** (#160). `updater/detect.py`
  tells Docker Engine, Docker Desktop, Podman and `podman machine` apart from
  the engine's `/version` and `/info` and the project's own directory, with
  rootless and SELinux, and refuses with one sentence each: permission denied
  on the socket, Enhanced Container Isolation, Windows containers, a TCP
  socket, Podman older than 4.4, an API older than the tested window, and an
  engine it does not know; an engine newer than that window is `outdated`. It
  also reads a local build from the app container's labels and digest, holds
  the not-root rule for each engine as data for the compose file and the
  launchers, and infers whether `podman-restart` is on. `updater/heartbeat.py`
  writes `updater.json` every 30 seconds with the negotiated API version, the
  engine's window and the updater's real container name, under Docker
  Compose's and podman-compose's naming alike. Nothing runs it yet.

- **The updater can prove where an image came from before pulling it**
  (#159). `updater/verify.py` reads the build attestation `release.yml` pushed
  beside the app or updater image -- anonymously, from the registry, every
  digest recomputed -- and checks with sigstore that this repository's release
  workflow built exactly that digest for tag `vX.Y.Z` on a GitHub-hosted
  runner, by repository and owner id rather than name, with no prerelease
  suffix. Signature, certificate chain and transparency-log proof are checked
  from the bundle alone; when Sigstore's trust repository cannot be reached it
  falls back to the trust root committed beside it and records which one
  verified. Any doubt is a refusal and nothing skips it. sigstore lives in a
  lock of its own, `requirements-updater.txt`, never in the app's runtime
  lock. Tested offline against the real 0.7.0, 0.7.1 and 0.8.0 bundles and
  tampered copies of them. Nothing calls it yet.

- **The self-updater can prepare, apply and roll back an update** (#161).
  *Prepare* resolves both images of the release to digests without pulling,
  checks free disk and memory, verifies both attestations, pulls them by
  digest, compares their labels with what was verified, and asks the new
  image's `scripts.upgrade --check --json` what it would do to this ledger,
  with the app serving throughout. *Apply* runs the steps of the design one
  journal entry at a time: it stops the app and parks it as
  `<name>-previous`, runs the drill from the new image (backup first, then
  the migration), starts the new version as a copy of the previous container
  that changes only the image and `SPENDTRACKER_AUTO_MIGRATE=0`, checks
  health from where requests arrive (the Tailscale sidecar, or the published
  port), writes the pin into the project's `.env` and `pin/release.env`, and
  prunes update backups beyond the newest five. A failed migration or health
  check **rolls back on its own**: the backup restored with the old image,
  the old version started again under its name; after three failed attempts
  the update needs recovery and nothing serves the ledger. After a crash, a
  laptop sleeping or the engine restarting, the updater resumes from its
  journal and never runs the drill twice. The Tailscale sidecar is never
  stopped or restarted, and an app in a Podman pod is refused with a
  sentence. The handover to a newer updater (#162) and the maintenance page
  (#163) are not built yet: the updater carries on without them. CI gains a
  `self-update` job, advisory for now, that updates release A to B through
  the updater against a real Docker Engine. No image or compose service runs
  the updater yet (#164).
## 0.8.0 — 2026-10-08

**Reversible: lossy** — one migration.

- `2de003489b79` — lossy: adds the `passkeys` and `webauthn_challenges` tables
  and `users.webauthn_user_handle` (#120). Rolling it back drops every
  registered passkey. Members then sign in with password + code, as before
  passkeys existed, and register their passkeys again after upgrading back.
  The sign-in challenges it drops expire within minutes anyway.

Passkeys: registering them, signing in with one, and one "Sign-in methods"
section in your account to manage them. Alongside them, security and
data-integrity fixes, a locked and hashed Python dependency set, and the
published image as what `compose.yaml` runs.

### Security

- **The `sql` logging style no longer prints the ledger to the console.**
  Turning it on set SQLAlchemy's `echo`, which attaches SQLAlchemy's own
  handler writing every statement and its values to standard output -- that
  is, to `docker logs` -- below the guard meant to keep them in `sql.log`
  alone. The style now works by log level, never `echo`, and takes off any
  such handler it finds. Found through a test that only failed when run on its
  own. (#108)

- **The container's Chainguard bases are pinned by digest.** The free tier
  has only a moving `:latest`, so the image named an input that changed under
  it. Both bases are now their own pinned stages, which Dependabot's weekly
  Docker check can move; `PY_BASE=`/`PY_RUN=` still build on any other base.
  The README says what the release attestation does and does not cover. (#95)

- **A new password is checked against the 100,000 most common.** Length was
  the only rule, so `qwertyuiopasdfgh` passed it. The setup wizard, changing
  your password and a reset link now refuse any of the top 100,000 passwords
  of a public-domain breach corpus, ignoring case, and say why. The list ships
  with the app -- it never calls out -- and is refreshed when a release is cut
  (`python -m scripts.common_passwords --refresh`). Existing passwords are not
  checked. (#97)

- **A YNAB key is gone from the browser's memory when the one-time import
  closes.** The wizard dropped it from its own state, but the query library
  kept each finished call -- key included -- for five minutes after the
  wizard closed. Those calls are now discarded as soon as it does. (#93)

- **The Content Security Policy no longer allows `data:` images.** Nothing
  in the client uses one (the QR code is SVG), so `img-src` is `'self' blob:`.
  (#94)

- **`/snap` paints the household's accent only when it is a `#rrggbb` colour.**
  The server already validates it before storing it; the capture page now
  checks it again before setting the header's background, as the app's own
  theme does. Defence in depth. (#92)

### Fixed

- **A currency code has to be a real one.** Only the shape was checked, so a
  typo like `GPB` opened an account in a currency that does not exist. A new
  account or a household's currency must now be an ISO 4217 code: a current
  one, or one withdrawn since 1999 such as `HRK` or `DEM`, for accounts with
  history. A code the household already holds from before this check is still
  accepted, so no existing ledger stops working. Nothing stored changes. (#110)

- **An agent's oversized receipt batch is answered `413`, not a dropped
  connection.** The app refused a body over its limit from the declared
  length and closed the socket at once. Most clients write the whole body
  before reading the answer, so they saw a broken pipe, which looks like a
  network fault and does not say whether anything was stored. A body up to
  five times over its limit is now read and discarded first, so the client
  reads the sentence. Nothing in it is kept. `agent/README.md` and the route's
  OpenAPI description give the batch's whole-request limit of 32 MB, and
  `deploy/DOCKER.md` says a reverse proxy needs a body limit at least as
  high. (#40)

- **A burst of requests can no longer use more memory than a small host
  has.** Each SQLite connection had a 32 MiB page cache whatever the machine,
  and the connection pool is unbounded on purpose. On a 95 MiB ledger, ten
  connections reading at once held 456 MiB. SQLite now has a process-wide soft
  heap limit of an eighth of the memory the process may use (the container's
  limit, or the machine's), and each connection's cache is a sixty-fourth of
  it, between 2 and 32 MiB. The same ten connections measured 178 MiB. (#102)

- **A Spanish statement whose date column is headed `F. Valor` imports.**
  Spanish banks abbreviate *fecha* to "F.". "F. Valor" matched no date name
  and did match the amount name "valor", so the file had no date column and
  its dates were taken as the amount. A header of "F." followed by a word is
  now a date. Three synthetic statements are kept as regression fixtures:
  this header; `1,234` beside `1,234.56`; and a blank debit cell next to a
  balance column. A new test checks that undoing an import gives a
  hand-entered row it absorbed back exactly as it was. (#90)

- **A register load is one request for its rows, not two.** The count beside
  "Needs a category" was a second `GET …/transactions` fired in the same tick
  as the register's own, and `access.log` drops the query string, so every
  load and every refresh showed up as two identical requests a few
  milliseconds apart -- each one running the filter, the count and the
  lookups again. The register's answer now carries the count
  (`needs_category`); the badge asks on its own only when nothing is ticked
  and the register is not asked at all. (#101)

- **The test that every household-scoped route checks membership was
  checking nine routes.** Its walk of the route table predated how this
  FastAPI version nests included routers, so it found only the routes declared
  on the app itself and passed by looking at almost nothing. It now walks all
  of them -- every non-agent route passed -- and holds agent routes to their
  own check (`current_agent`, then the key's household). The empty-household
  test walks the same table instead of a hand-kept list of eleven paths, so
  reports, receipts, transfers and categories are covered. (#106)

- **An agent's import row can say its currency, and one in another currency
  is refused.** Rows posted to `POST /imports` had no currency field, so the
  check a statement file's currency column gets (0.5.0) never ran for them:
  a row an agent pulled from a yen account and sent to a euro one was
  recorded as the same figure in euros. A row may now carry `currency`; one
  that is not the account's is rejected with the sentence a file's row gets,
  and an import whose every row names another currency is refused. Rows that
  leave it out are read in the account's currency, as before. (#86)

- **"Link all" links everything that is strong, not just what was strong
  before it started.** A link made because a row names the other account
  makes those two accounts' history, and that history makes their other
  pairs strong -- but "Link all", and the link on commit at import, asked
  once and stopped, so a second press of "Link all" found more. Both now link
  until nothing new is strong, still as one batch and one undo, and "Link
  all" says how many it linked in all. (#88)

- **"Make new codes" tells a member whose authenticator the server can no
  longer check what to do.** After `secret.key` was replaced, it answered with
  the generic "that password and authenticator code do not prove it is you".
  It now says what a step-up says: the key was replaced, a recovery code does
  not stand in here, and setting up a new authenticator is the way on. (#98)

- **An amount typed as "1,234" is a thousand again, not 1.23.** Amount fields
  read the last separator as the decimal mark, so a thousands comma with no
  decimals was taken as a decimal comma and the third digit rounded away -- a
  transaction, transfer or opening balance saved a thousand times too small,
  with no warning. A lone `,` or `.` before exactly three digits now means
  what it means in the browser's number format ("1,234" is a thousand in
  English, "1.234" is one in German); a mark that format does not use either
  way is refused rather than guessed. "1,234.56", "1.234,56" and "12,34" read
  as before. An amount put back into a box for editing uses the same decimal
  mark, so a three-decimal currency round-trips. (#45)

- **A statement amount written `12.50 DR` imports as money out.** The letters
  were dropped as decoration, so a debit came in as money in. `DR` after the
  figure is now a minus and `CR` a plus, in CSV, spreadsheet and PDF
  statements, with or without a space and in either case; one that also
  carries a minus sign or brackets is refused as signed twice. The import
  guide says so. (#84)

### Changed

- **An invariant suite over randomised ledgers.** Twelve seeds each build a
  ledger in two households: rows, transfers within and across currencies,
  edits, splits and deletes. The suite then holds four things true of any
  ledger: every balance is the sum of its rows, by every route that reports
  one; transfer pairs point at each other and net to zero within a currency;
  undoing a run of acts gives back every column of every row; and no total
  crosses currencies. A failing seed is reproduced by its number. (#107)

- **The database file is looked after, not only its rows.** Every
  housekeeping sweep now ends with a `wal_checkpoint(TRUNCATE)`, so the
  `-wal` file goes back to zero instead of staying at the size the biggest
  import ever left it, and it runs `VACUUM` when more than half the file is
  free pages, such as after a household is deleted or a large import is
  undone. Planner statistics are refreshed straight after any commit that
  writes 1,000 rows or more, and after `make restore`, rather than waiting up
  to six hours for the next sweep. (#103)

- **The container image is built for linux/amd64 and linux/arm64, and the
  release is published last.** The image was amd64 only, so Docker Desktop on
  an Apple-silicon Mac ran it emulated; each platform is now built and
  smoke-tested natively and the two are joined into one index, whose digest
  the provenance attestation names. The GitHub release is created as a draft,
  the image pushed, attested and pulled back with no credentials to prove the
  package is public, and only then does the release go public and `X.Y` and
  `latest` move -- a run that fails halfway leaves a draft, not a release
  with no image behind it. The `org.opencontainers.image.version` label is
  the bare `X.Y.Z`, the number `/api/health` reports, and the release body
  leads with the version's CHANGELOG section. (#181)

- **The register loads five hundred rows at a time.** It used to ask for
  everything the filter matched, up to 25,000 rows, and refetch all of it
  after every edit. It now asks for the first 500, says how many the filter
  matched and how many are loaded, and asks for the next 500 when you reach
  the end of what is there. Sorting at a column heading is still done by the
  server, from the first page. The heading's tick box selects the rows that
  are loaded. (#100)

- **The client asks one module which locale it is in** (`lib/locale.ts`):
  the words stay English, and numbers, money and dates follow the browser's
  own formatting locale as they always did. Money is formatted from its
  digits rather than a divided float, sorting by name goes through one
  collator, and the labels for account types, import outcomes, roles and the
  YNAB import's steps live in `lib/labels.ts`. Typed amounts now also read
  the minus sign, spaces and apostrophes other locales write. Nothing an
  English reader sees changes; tests compare the old and new output. (#52)

- **Small fixes left from reviews** (#110): History headlines a bulk delete
  of receipts as *Bulk delete*, not *Bulk edit*; an account update that sends
  a country or statement product together with its clear flag is refused, as
  the note and the bank already were, rather than the flag winning in silence;
  `make version` works in a worktree without a `.venv`; the accounts filter's
  grouping is one shared copy for the register and the income-and-expense
  report; opening a screen from `?open=` no longer depends on React running
  the reader once; and three deprecation warnings are gone from the test run.

- **A receipt photo over 4 MB from an agent is told how to shrink it.** The
  `413` from the agent receipt routes pointed at the multipart route, which no
  key can use. It now says to shrink the photo below 4 MB as JPEG or AVIF,
  keeping its EXIF `DateTimeOriginal` and GPS, and names the section of
  `agent/README.md` that says how. The 4 MB ceiling is unchanged. (#39)

- **Five statements in the docs and comments now match the code** (#109):
  CLAUDE.md names `ci-ok`, not a `tests` check, as what a ruleset requires;
  the setup router says it answers `409` once set up, not `404`; `/db`'s
  docstring says it shows an owner every household's ledger, which is more
  than the admin screen; `scripts/db_view.py` says receipts are carried whole,
  GPS and EXIF included, as are payee rule patterns; and old-tracker issue
  numbers in the `Makefile` and `tests.yml` are marked as such.

- **`agent/README.md` fills three gaps an agent found by trial:** the range
  and default of `window_days` on `/transactions/match` (0 to 14, default 4),
  that the register's `amount` filter matches the figure without its sign,
  and the `receipts/binary` door with its query parameters, its 4 MB ceiling
  and that it takes no `extracted`. Tests hold each to the code. (#41)

- **The README says CI tests Python 3.12**, and no longer claims 3.14 works:
  every CI job runs 3.12, and nothing tests 3.14. (#105)

- **The CHANGELOG says public releases start at 0.6.2**, and that the
  sections below it are history from the earlier private repository, with no
  tag or release here. (#117)

- **`compose.yaml` runs the published image.** It names
  `ghcr.io/mariolonghi-com/household-spend-tracker` at the release
  `SPENDTRACKER_VERSION` in `.env` says, and `docker compose pull` fetches it;
  `build:` stays as the fallback, and a local build is told to call itself
  `local`. The README and `deploy/DOCKER.md` start, upgrade and roll back that
  way. The Tailscale sidecar setup still builds from its checkout. `/llms.txt`
  names the source repository and its licence, and the pull request template
  asks for the design doc to be re-read against the code. (#111)

- **The import guide says what happens to a statement line whose row you
  deleted:** it is new again, so the next statement that carries it -- or the
  same file sent again with *Import it anyway* -- brings it back. Undo in
  History is the other way back. Nothing about importing changed; it is now
  written down and tested. (#91)

- **Why any member may import accounts from a file is written down**, beside
  the route, with a test: it only adds accounts, each in the audit log, and
  History undoes the whole file. Nothing about who may run it changed. (#115)

- **A release waits for one approval before anything is published.**
  `release.yml`'s `publish` job runs in a `release` environment, and the other
  two publishing jobs depend on it, so once the repository gives that
  environment a required reviewer, a pushed `v*` tag builds and smoke-tests as
  before and then waits for a single click before anything reaches Releases
  or ghcr.io. `tests/test_release_workflow.py` fails if a publishing job stops
  depending on the gated one. Until the reviewer is set, it behaves as it did.
  (#96)

- **OpenSSF Scorecard runs on pushes to `dev` and weekly, not on `main`.**
  The action only scores the default branch, which is `dev`, so on `main` it
  failed every release without measuring anything. (#36)

- **A receipt's free-text field is now headed "Receipt notes"**, on the
  Receipts screen and on `/snap`. It used to say "Note". A transaction's field
  is a *memo*, and the old heading read as if the two were the same thing.
  Only the wording changed: it is still the receipt's `note` field, and the API
  and the agent are unchanged. (#114)

- **The Python dependencies are locked, with hashes.** `requirements.in` and
  `requirements-dev.in` hold the floors you edit; `make lock` compiles them
  with `uv pip compile --universal --generate-hashes` into `requirements.txt`
  and `requirements-dev.txt`, every package pinned exactly, transitive ones
  included. `make install-py`, `make install-prod`, the container image and CI
  install them with `--require-hashes`, so two installs of one tag get the same
  set. CI fails a pull request whose locks do not match its `.in` files, and
  checks that the image holds exactly the runtime lock. `starlette` and
  `certifi`, which the app imports directly, are now declared. `make audit`
  audits the locks as written, a release carries a CycloneDX SBOM of the
  runtime set, and Dependabot reads the locks through its `uv` ecosystem. The
  client's `tsx`, which one test ran unpinned through `npx`, is a dev
  dependency. **For an operator:** nothing to do beyond the usual
  `make install-prod`. (#46)

- **One "Sign-in methods" section in your account.** Password,
  authenticator, passkeys and recovery codes are now rows of one section
  instead of four separate blocks. Each row says its state in words, such as
  "Set", "Needs setting up again" or "7 of 10 left", and offers its own
  action. A line at the top says what currently gets you in. Keys for
  programs stay a separate section.
  - **Your passkeys** are listed with their name, whether each is synced or on
    this device only, when it was added and last used, and "this device" on
    the one you signed in with. The list sorts at its headers.
  - **Renaming** is done in place, and **removing** asks once and says what
    you can still sign in with.
  - **A passkey made for another host name** is marked as such and can only
    be removed.
  - **Adding a passkey** asks for your password and code in the same panel,
    then hands over to the browser's prompt. The new passkey appears
    highlighted, with its name ready to edit.
  - **Where passkeys cannot work** there is no Add button, only one line
    saying why.
  (#122)

### Added

- **An agent key can read a stored receipt back.** Receipts in the agent API
  now carry their `note`, and there are new read-scope routes for one
  receipt, its stored file and its thumbnail:
  `GET /api/agent/v1/receipts/{id}`, `…/file` and `…/thumbnail`. The listing
  also takes `transaction_id=` to go from a row to its receipts. A note sent
  at upload used to be write-only, and an agent summarising receipts filed
  the day before had nothing to read but its own claim. Another household's
  receipt is a `404`, and every read is in the request log. (#44)

- **A weekly upgrade rehearsal on a bench-sized ledger** (`bench.yml`,
  Mondays and by hand). It is not a pull-request check. It builds the demo
  seed with the last release's code, grows it to about 100 MiB with
  `scripts/bench_ledger.py`, and times `alembic upgrade head`, a backup and
  the `/db` snapshot. It still fails if a row is lost. On a 93 MiB,
  129,024-row ledger, the slowest migration in the project's history (one
  that rebuilds `transactions`) took 5.8 s, and the whole chain about 25 s.
  The upgrade from 0.7.1 took under 2 s. (#104)

- **The upgrade drill can be driven by a program.** `python -m scripts.upgrade
  --check --json` prints what an upgrade would do as one JSON document: the
  deployed version and commit, the database's stamp, the code's head, and each
  pending migration with its `Reversible:` verdict. `--yes --report PATH`
  writes the outcome of a real run -- the backup folder and whether it
  verified, the stamp and every counted table before and after, the
  `secret.key` check, the exit status and the log -- whatever the exit. And
  the exit status now says what happened: a `secret.key` that does not open
  the migrated ledger exits 5 and a table with fewer rows than the backup
  counted exits 6, where both used to print a warning and exit 0, which a
  person reading the output catches and an updater would not. The codes are
  listed in the script's docstring. A test proves `--check` against a live WAL
  ledger leaves the database and its `-wal` byte for byte as they were. (#155)

- **The groundwork for passkeys: `SPENDTRACKER_RP_ID`, and whether an instance
  can offer them.** Nothing on the sign-in screen changes yet. The new
  setting is the host name passkeys will be bound to. It defaults to the host
  of `SPENDTRACKER_PUBLIC_URL`, and it is never taken from the request. Boot
  refuses any other value, because a passkey only ever works for the name it
  was made under: a wider name such as the whole tailnet's is refused, and
  `localhost` is accepted in development only. `GET
  /api/session/passkey/state` says whether this request may be offered
  passkeys, or why not: no public URL set, an IP address, the app opened at
  another address, or plain HTTP. The `Permissions-Policy` now names the two
  passkey features, allowed on this origin and refused on `/snap`. The
  `webauthn` library is added, locked. **For an operator:** nothing to do. If
  `SPENDTRACKER_PUBLIC_URL` is set, leave `SPENDTRACKER_RP_ID` unset. (#119)

- **A member can register passkeys, and list, rename and remove them**,
  through the API so far. The screens come with #122. Adding a passkey costs
  a fresh password and authenticator code, the same step-up that issuing an
  agent key costs. The passkey has to be discoverable and must verify the
  user. Each one records the host name it was made for. `make doctor`,
  `make upgrade-check` and `make restore` now name any passkeys made for
  another host name than this instance's, which is what a renamed machine or
  a restore onto another host leaves behind. Adding, renaming and removing a
  passkey are in History. Undo never brings one back. **Migration
  `2de003489b79`** adds the `passkeys` and `webauthn_challenges` tables and
  `users.webauthn_user_handle`. **Reversible: lossy**: rolling it back drops
  every registered passkey, and members then sign in with password + code as
  before. (#120)

- **Signing in with a passkey.** Where passkeys can work, the sign-in screen
  offers "Sign in with a passkey", and the email field suggests your passkeys
  itself in browsers that support that. Both the server and the browser
  must agree that passkeys can work here; anywhere else the screen is as it
  was. A passkey is both factors at once, so it signs you in with no code
  step and does not mark the browser as trusted. Every refusal says the same
  thing: an unknown passkey, a disabled member, a passkey made for another
  host name, a replayed challenge and a bad signature all read alike.
  Failed passkey sign-ins have their own rate limit. **What else changes:**
  - **Account resets remove the account's passkeys.** That covers an owner's
    reset link, `scripts.reset_account` and `scripts.reset_authenticator`.
  - **A recovery code leaves passkeys in place**, and the screen now says how
    many still work, so you can remove one that was on a lost phone.
  - **A password change leaves passkeys in place**, as it leaves agent keys.
  (#121)

- **Refusals can carry a stable code and raw values beside the sentence.**
  A converted refusal answers `{"detail", "code", "params"}`: `detail` is the
  same English sentence as before, `code` a name from `app/error_codes.py`,
  and `params` the raw values -- money as minor units with its currency, dates
  as ISO -- so a translated screen can say it in its own words and format.
  Ten refusals are converted (the exact money parser, transfers to the same
  account or across currencies, a reconciliation that does not balance or
  holds a later row, a split that does not add up); the rest follow with the
  translations. Agent answers are unchanged: they carry no code yet. A test
  stops new refusals arriving without one. (#65)

### Documentation

- **The README shows the register**, from the demo household `make seed`
  creates, so every name and figure in it is invented. There is also a
  `CITATION.cff`. The data-hygiene test now lets screenshots live under
  `docs/screenshots/` if they are PNGs with no metadata chunks. It skips
  `CITATION.cff`'s two author lines, as it already skipped the copyright
  line. (#111)

- **Passkeys for operators:** the README section *Passkeys, and choosing the
  host name first* says how an instance can be reached for passkeys to work,
  and what `SPENDTRACKER_RP_ID` defaults to and refuses. It also says why the
  host name has to be chosen before anyone registers a passkey, and what
  household devices need, including that signing in on a laptop with a phone
  needs Bluetooth and internet on both. `deploy/DOCKER.md` and
  `deploy/UPGRADING.md` each add a paragraph on what changes the name and
  what to do afterwards. (#123)

- **A glossary for the first translations:** `client/src/locales/GLOSSARY.md`
  holds one draft rendering per term in pt-BR, es-ES and sv-SE, the register
  each language uses, and how each writes money and dates. Nothing in the app
  changes. (#174)

## 0.7.1 — 2026-10-05

**Reversible: none** — no migration in this release. To go back, check out
`v0.7.0` and restart.

One fix, for the Tailscale sidecar deployment only. The app is unchanged.

### Fixed

- **A 502 that never ends after the Tailscale sidecar restarts is now
  visible.** The app joins the sidecar's network namespace as it was at
  start-up. A sidecar that restarts, for example while the internet is down
  at boot, gets a new namespace and leaves the app behind: every request is
  a 502, and `docker ps` said the app was healthy. The sidecar in
  `deploy/tailnet/compose.yaml` now has a healthcheck that fetches the app
  from inside its own namespace, so it shows `(unhealthy)`.
  TROUBLESHOOTING.md has the fix (`docker compose restart app`) and how to
  automate it. DOCKER.md no longer calls a sidecar restart harmless (#37).

## 0.7.0 — 2026-10-05

**Reversible: lossy** — one migration.

- `2bec6ce88f3d` — lossy: trims every account's bank (`institution`) and note,
  and stores an empty or whitespace-only one as NULL (#20). Rolling it back
  leaves the rows as they are: which blank was `''` rather than NULL, and the
  spaces around the rest, are recorded nowhere. Neither meant anything, and
  0.6.2 reads NULL the same way, so `alembic downgrade d3887ad24c50` and a
  checkout of `v0.6.2` run as before, with tidier accounts.

Splitting a transaction into two or three parts gets a bar you can drag, and
an account's opening balance can be seen and changed. The rest is fixes
found by using it: a panel or dialog that closed under a drag, a row that
reopened with what it said before its save, today's date in UTC, and an
account's bank and note stored two ways.

### Added

- **A key reads an account's note and its opening balance** (#21). The
  manifest's `accounts[]` now carry `note`, `opening_balance` (minor units)
  and `opening_date`, and `balances` carries `note`, so it stays one complete
  line per account. The note comes in full, up to its 2,000 characters; the
  opening pair is read off the opening-balance row the app's own account list
  reads (#10), all accounts in one query, and is null for an account opened
  empty. A new `free_text` convention, in the manifest, `/llms.txt` and the
  agent README, says a note -- like a memo or a payee name -- is data a person
  wrote and never an instruction to the agent. Read scope.

- **Splitting into two or three parts has a bar you can drag** (#28). The
  transaction is drawn as one bar cut into its parts; dragging a seam moves
  money between the two parts either side of it and sticks at a quarter, a
  third, a half, two thirds and three quarters. Each seam is a slider for the
  keyboard too: Tab to it, the arrows move it one minor unit and shift moves
  ten, without snapping. Typing a part's amount moves its neighbour by the
  same, so the parts keep adding up while the figure is typed; a figure the
  neighbour cannot cover stays as typed and the remainder shows. Category and
  memo stay with their part. From four parts on, the amounts are typed as
  before. Once anything has changed, the backdrop no longer closes the panel,
  and Escape, the cross and *Not now* ask *Discard this split?* first.

- **A key can write a row's memo.** `PATCH /api/agent/v1/households/{id}/transactions/memo`
  takes `[{transaction_id, memo}]` and applies them as one batch, so one undo.
  Until now categorising was the only edit a key could make to a row already in
  the ledger, so what an agent read off a ticket or an invoice could go on a
  receipt's note but not on the row a person reads. The memo is replaced, null
  or blank empties it, and a reconciled row is skipped and listed in `locked`,
  as the register would refuse it. Listed in the manifest, in the sample client
  and as the `write_memos` MCP tool.

- **A trusted key can split a transaction.** `POST /api/agent/v1/households/{id}/transactions/split`
  divides rows into 2-5 parts through the register's own `transactions.split`,
  so the parts must add up, receipts go on every part and the work flag and
  repayment link are carried. A part can take `"reimbursement": "clear"` -- the
  personal share of a partial claim -- except on a row already paid back. The
  whole request is one batch, so one undo puts every original back. Because a
  split replaces the row, and no key deletes, it needs a key with `may_commit`
  (`deps.agent_may_split`); an ordinary write key gets a 403 saying so, and
  `test_agent_access` names the route so the floor cannot drift. Listed in the
  manifest, the sample client and the `split_transactions` MCP tool.

- **The New account panel asks for the bank and a note** (#12). The API
  always took `institution` and `note` on create, but the panel never
  collected them, so the only way to record the bank was to make the account
  and open it again. Both are sent trimmed, and a blank one as null. The edit
  panel now asks in the same order -- country, then bank, then note -- and
  both hold the bank to 120 characters and the note to 2,000, the API's own
  limits.

- **A staged line can be marked uncategorised on purpose** (#9). The import
  preview's category cell offers *Uncategorised* beside the categories, as a
  third answer next to a category and an empty box. The empty box still hands
  the line back to the suggestion; *Uncategorised* commits the row with no
  category, and neither the payee's usual category nor the bank's wording for
  interest, investments and fees is consulted for it -- nor is a category made
  for that wording. The cell tells the four states apart: a chosen category, a
  muted suggestion, *uncategorised (chosen)* and plain *uncategorised*. Kept on
  the staged line, so it survives a reload and a preview reopened from the
  queue, and the "rest of this payee" offer spreads it within the file (without
  a rule to set). The agent import route takes the same thing as
  `uncategorised: true` on a row, never together with `category_id`. No
  migration: the choice lives in the line's `parsed`, as the typed memo does.

- **The account panel shows and edits the opening balance and its date**
  (#10). Neither is a column, so no migration: both are read off the
  account's opening-balance row, found by its payee's `system` mark, for
  every account in one query, with a link that opens that row in the
  register. A change is made to that row in the same batch as the rest of
  the save, so one undo puts it all back, and the row stays reconciled with
  its system payee -- no unlocking it by hand. No date in the future, as on
  creation. A figure on an account opened empty writes the row, dated as
  given or at the account's oldest transaction; zero deletes it, and undo
  restores it; a date alone on an account with no row is refused, since
  there is nowhere to keep it. An opening date after the account's oldest
  other transaction is saved with a warning rather than refused, because the
  balance before that date then leaves the opening figure out -- `warnings`
  on the account, said wherever the account is read. A change that moves
  the figure or the date is refused while a recorded reconciliation is
  dated on or after the earlier of the old and new opening dates, since the
  opening row is part of the floor that statement balanced on; undo the
  reconciliation first. And the panel edits past the lock only on the row
  the app wrote as the opening balance -- born reconciled, by its first entry
  in the audit log, never since ticked by a reconciliation, and not a
  transfer leg. With two opening-balance rows on one account the earliest is
  the one shown, and if a person gave that payee to an ordinary row, the
  panel refuses and points at the register.

### Changed

- **A self-built image can say which commit it runs.** The Application screen
  and `/api/health` read the commit from `app/build.json`, which
  `scripts.build_stamp` writes from git. CI and the release workflow ran it;
  `deploy/DOCKER.md` never told anyone else to, so every image built by
  following it said *unknown*. Every build command there, in `UPGRADING.md`
  and at the top of the sidecar compose file is now preceded by the stamp, and
  a new section, *Naming what runs*, explains it alongside `SPENDTRACKER_ENV`,
  which was not documented in DOCKER.md at all.
- **The memory the app needs is written down, and it is more than the docs
  said.** `deploy/DOCKER.md` said "about 1 GB of RAM". The app container needs
  `mem_limit: 768m` as a floor (it peaks near 500 MiB and keeps its high-water
  mark), and a machine that runs only this needs 1.5 GB. Below that the kernel
  kills the app mid-request and the browser shows a 502 for a second.
  `deploy/TROUBLESHOOTING.md` has a new section saying how to recognise it and
  what to change. Measurements in #14.

### Fixed

- **A drag that ends outside a confirmation dialog leaves it open too.** #27
  fixed this for the side panels; the confirmation box (*Reset sign-in?*,
  *Discard this split?* and the rest) still closed on a click its backdrop
  received from a press that began inside it. It now follows the same rule:
  only a press that began on the backdrop closes it.

- **A drag that ends outside a panel no longer closes it** (#27). A click
  goes to the nearest element holding both the press and the release, so
  selecting text in a panel's field and letting go past its edge was a click
  on the backdrop, and the panel closed with what had been typed in it. The
  backdrop now closes a panel only when the press began on the backdrop too.
  Every side panel had it.

- **A row reopened straight after a save shows what was saved** (#29). The
  transaction panel saves on the way out of a field and only asked the
  register behind it to refetch; until that answer came back, closing the
  panel and reopening the row opened it on the values from before the save.
  The panel writes every field on its next save, so one more edit there put
  the old memo back over the new one. The server's answer to the save now goes
  into the register's cached rows at once, and the refetch still follows. The
  browser test that caught it on the phone run waits for the save's response
  before closing the panel.

- **Picking *Uncategorised* on a staged line means uncategorised** (#19). In a
  household with a category of its own called, say, *Other: Uncategorised*,
  the import preview's category cell read the words in the box and matched
  that category first: choosing *Uncategorised* sent the category instead, and
  merely opening a line already marked uncategorised and leaving it replaced
  the decision with that category. The cell now commits the option picked from
  the list rather than its label, sends nothing when the box is left as it
  opened, and reads the words *Uncategorised* typed in full as the
  no-category answer. The household's own category is still a pick from the
  list away, or typed by its full name.

- **"Today" is the local date on every screen** (#23). The transfer,
  reconcile and quick-entry panels, a duplicated row's date, and how long a
  work expense has waited all took today from `toISOString()`, which is UTC:
  between midnight and 02:00 in Madrid (01:00 in winter) that is still
  yesterday, so a new transfer or entry was pre-filled with yesterday and a
  duplicate was dated yesterday, while the server's `date.today()` is local.
  They now share the account panel's `localToday()`, moved to
  `client/src/lib/time.ts`.

- **An account's bank and note are stored trimmed, and an empty one as
  nothing** (#20). The edit panel sent both as typed, so `"  Bank "` kept its
  spaces and an emptied field was stored as `""` -- "no bank" had two
  spellings, and a filter or an export asking for null missed one. The
  server now trims both on create and on edit and stores a blank one as
  null, whatever a client sends. On `PATCH /api/accounts/{id}` null still
  means "leave it alone", so emptying one has its own word,
  `clear_institution` / `clear_note`, as `clear_country` does; a value sent
  with its clear flag is a 422. The 120- and 2,000-character limits count the
  value as sent, as the panel's own limit does. The panel sends both trimmed
  and the clear flag when a field that had a value is emptied, and a panel
  kept open after a save now shows what was stored rather than what was
  typed. Migration `2bec6ce88f3d` folds the rows already written: every bank
  and note trimmed, and an empty or whitespace-only one set to NULL. It is
  **lossy** in name only -- which blank was `''` rather than NULL, and the
  spaces around the rest -- and its downgrade leaves the rows as they are.
  Like every migration it is not in the audit log.

## 0.6.2 — 2026-10-04

**Reversible: none** — no migration in this release. To go back, check out
this repository's first commit, which is 0.6.1, and restart.

The first release published from this repository; 0.6.1 was its starting
point and was not tagged here.

### Fixed

- **The upgrade rehearsal runs on a repository's first commit.** On a push it
  rehearsed from `HEAD^`, falling back to `HEAD` when there is no parent -- but
  on a root commit `git rev-parse HEAD^` fails *and* prints `HEAD^`, so the step
  wrote two lines where GitHub expects one value and `rehearsal` failed with
  "Invalid format". This repository starts from one commit, so `main` could not
  go green and `release.yml` had no tree it would publish. A root commit now
  rehearses from itself. No application code changed.

## 0.6.1 — 2026-10-02

**Reversible: none** — no migration in this release. Check out `v0.6.0` and
restart to go back.

### Fixed

- **CI's runners pass the junk-statement timing tests.** The two tests from
  `#222` asserted under 2 s. A shared runner, under xdist and coverage, took
  2.6–5.3 s for what a laptop does in half a second, so `tests` failed on
  every run and 0.6.0's tag could not be published. The limit is now 10 s,
  still a fraction of the 43 s the defect took. No application code changed.

## 0.6.0 — 2026-10-02

**Reversible: lossy** — four migrations, applied in this order.

- `25e73951a565` — clean: recomputes `payees.name_folded` from the unchanged
  name, and the downgrade recomputes the old key the same way, giving the
  payee that took a shared key its own old one back (`#268`).
- `ef3af4e09c4a` — lossy: adds the nullable `transactions.import_alt_ids`.
  Rolling it back drops the keys kept there, so a repeat One-time Import
  offers a row a statement absorbed as a possible duplicate again, and exact
  matches fall back to the four-day rule (`#264`).
- `541128a33fd4` — lossy: adds `account_resets` and makes `users.password_hash`
  and `users.totp_secret` nullable. Rolling it back drops every pending reset
  link, which is the only way back into its account. **An account whose
  password a reset had cleared cannot be signed into on the older version at
  all** -- nothing there sets a password for somebody who cannot sign in --
  until the backup is restored, or this version is back and a new link is
  followed; that includes an only owner. An account whose authenticator was
  cleared gets an empty secret no key opens and needs
  `scripts.reset_authenticator`, which is enough only if its password was not
  reset too. Have every pending link followed before rolling back (#284).
- `d3887ad24c50` — clean: adds the index `ix_batches_started_at`, so the
  owners' notice of recent sign-in changes reads the last fourteen days of
  the audit log rather than all of it. Rolling it back drops the index and
  loses nothing (#286).

### Added

- **Account reset links** (#284). A one-time link for one account, with two
  switches, password and authenticator; issuing it ends the account's
  sessions, trusted browsers and agent keys and resets what was chosen, and
  `/reset/<token>` names who reset it, sets a new password and/or enrols a new
  authenticator, and shows ten new recovery codes once. An account whose
  authenticator was cleared is refused at the code step. Issuing comes with
  the owner screen and the server command.
- **`scripts.reset_account`, the server command** (#285): a reset link for any
  account, `--make-owner` to promote one or, with `--name`, create one,
  `--enable` to enable a disabled one, and `--household` to add it to a
  household; one audited batch, the link printed once.
- **Owners reset sign-in from Admin → People** (#286): *Reset sign-in…* on
  any account but your own, another owner's included and with no extra check,
  shows the link once to hand over; *Pending reset links* (lapsed ones marked,
  Withdraw) and *Recent sign-in changes* -- every reset, replaced
  authenticator and new owner of the last 14 days, read from the audit log --
  with a notice in the app for what somebody else did.

- **Recovery mode** when `secret.key` does not open a member's authenticator
  (#287): computed, never stored. Sign-in says the key was replaced as soon as
  the password is right and asks for a recovery code, at a trusted browser
  too, which goes straight on to a new authenticator without a second one --
  or, put off, from the profile later, in the same tab within a day; until
  then every screen tells that member so, a reload of the sign-in screen
  included, with a button to the profile. That
  recovery code revokes the member's sessions, trusted browsers and agent keys
  as one always has, and the original key put back returns none of them:
  SECURITY.md and the README no longer say a lost key costs nothing else, and
  the boot log says to look for the key first. A step-up says the key was
  replaced instead of failing on it, and that a new authenticator, not a
  recovery code, is the way on there. Owners see a banner counting those
  members, leaving out disabled ones, who cannot sign in to be asked, as the
  boot log and `make doctor` do, and asking again every minute, so it goes
  without a reload once nobody is locked out. The banner and the boot log name
  `SPENDTRACKER_SECRET_KEY` rather than `secret.key` when the key came from
  that variable, since the file is then never read. The old sealed secrets
  are kept until each member re-enrols, so putting the original key back
  ends it for everybody who has not re-enrolled -- one who has holds a secret
  only the replacement opens, and is asked for a recovery code once more.

- **Operator tools for the ledger.** `make doctor` (`scripts.doctor`): which
  ledger is open, schema, integrity, whether `secret.key` opens every
  authenticator, recovery codes left, newest backup; read-only, exits 1 on a
  failure. `make reset` (`scripts.reset`): a verified backup, then the ledger
  and key moved aside for an empty start. `scripts.reset_authenticator`: a new
  authenticator and ten new recovery codes for a member, from the server, in
  one audited batch, stored only after a working code. `deploy/tailnet/check.sh`:
  the sidecar's containers, tailnet name, version and volume, then the doctor.
- **The log says which ledger it opened**, and warns when `secret.key` does not
  open its authenticators.
- **A member can make new recovery codes from the profile screen** (#289), with
  their password and a code from their current authenticator (not a recovery
  code); every old code goes, ten new ones are shown once, nobody is signed out.

- **A One-time Import through YNAB's API checks each account against YNAB's
  balance** (`#266`). What the import wrote, plus what it left out on purpose
  (starting balances skipped, rows outside the date range, duplicates
  skipped), is compared with the account's `balance` from YNAB, converted
  exactly from thousandths into the currency's own minor units -- so yen are
  whole yen. When they differ, the preview and the report name the account
  with both figures and the difference; when they agree, nothing is said. A
  balance the currency cannot hold exactly is named as unchecked, with why.
  Every account the import writes into is checked, created or existing: the
  figure adds up YNAB's rows, not the ledger's, so an account's own history
  does not move it. An import from the `Register.csv`, which has no
  balances, says nothing.

### Changed

- **A One-time Import through YNAB's API keeps the bank's own text** (`#265`).
  YNAB's `import_payee_name_original` -- what the bank wrote, before YNAB's
  rename rules -- is stored as the row's bank text, so Payee Naming Rules can
  suggest rules from years of YNAB history before the first statement import.
  The payee is still the name kept in YNAB. A row with no bank text (typed into
  YNAB by hand, or any row from the `Register.csv`, which has only clean names)
  stores none, where before it stored YNAB's clean name as if a bank had sent
  it. A part of a split takes its parent's bank text only when it kept the
  parent's payee, so one bank line is never spread across two payees for the
  suggestions to fold together. The report says how many bank lines brought
  their text (a split counts once) and links to the rules. YNAB's own
  `import_id` is read and carried with each row, and statements now match on
  it (`#264`, under Fixed). **A One-time
  Import made before this change still holds YNAB's clean names as bank text**,
  and the suggestions read them as such: undo that import in History and run it
  again from the API to store the bank's text instead.
- **Payee names and rules ignore accents, dash style and invisible spaces**
  (`#268`). `payees.fold` now applies NFKD and drops the combining marks, reads
  every Unicode dash (U+2010–U+2015, U+2212, U+FE63, U+FF0D) as `-` and
  no-break, narrow no-break and zero-width spaces, joiners and the BOM as a
  space, before the whitespace collapse and casefold it always had. So
  `Café Sol` and `CAFE SOL`, or a sort code written with hyphens and with en
  dashes, are one payee, in statement imports and the One-time Import alike.
  `contains`, `equals` and `prefix` rules compare both sides folded, and a
  literal rewrite cuts the accented spelling it matched, on whole characters
  only (`STRAS` does not cut half of `Straße`). A `regex` rule's
  pattern is never folded -- casefolding `\D` gives `\d` -- but it is now
  tried against the folded text as well as the bank's own words, raw first.
- **Payees that are now one name are listed, not merged.** The migration
  updates each payee's stored key; where two payees of one household now
  share one, it goes to the one with the most transactions (then the lowest
  id) and the others keep their old key. Payee lookups resolve through the
  same fold as the rules, so a third spelling finds that payee rather than
  becoming a third one. The groups appear on the Payees screen under
  *Spellings of one name* (`GET /households/{id}/payee-collisions`), sortable
  at their headings, and are merged only when somebody chooses the spelling to
  keep, through the existing payee merge, which brings the survivor's key up
  to date. A merge that fails part-way shows the error and offers only the
  spellings still there.
- **A committed One-time Import says how many rule suggestions are waiting**
  (`#269`). Once it commits, the report counts the household's groups of bank
  strings that look like one payee each -- the suggestions on Payee Naming
  Rules, all of them rather than the twenty the screen lists -- and says so in
  the same note, behind the same link: "12 groups of bank strings look like
  one payee each; make rules?". An import that brought no bank text, such as
  the `Register.csv`, says instead that rules can be suggested after the first
  statement import, unless earlier statements already left some. Over twenty,
  it adds "(the 20 largest are listed)", since the screen lists only those.
  The count is the Rules screen's own grouped query, run once per commit and
  never on a preview, and it is `rule_suggestions` in the report; if counting
  fails, the import stays saved and the report leaves the count out.

- **The sidecar deployment guide explains each step, and a troubleshooting
  page covers the ledger.** `deploy/DOCKER.md` section 3 now states the goal
  of the Tailscale access rule before its syntax and links Tailscale's own
  pages, says how to find the tailnet name and the version, explains what each
  command on the server does (with `nano` for `.env`), adds a check after each
  step and ends with a status report. New `deploy/TROUBLESHOOTING.md`: where
  the ledger is for each route, which ledger is open, why a fresh clone opens
  the old one, a deliberate reset, `secret.key`, getting back into an account,
  and two apps on one volume. Documentation only.

### Fixed

- **Income vs Expense opens in a tenth of a second, not half a minute** (#297).
  The check for "is this row a work expense's repayment?" ran once per row and,
  depending on the database's statistics, searched the household's whole
  ledger each time, so a 10,000-row ledger took 14 s per query and three
  queries per report. It now reads the repayments once, which stays fast
  whatever statistics the database holds. Its drill-through paid the same. No
  figure changes.
- **`make upgrade-check` prints the data directory**, as README.md and
  UPGRADING.md always said it did. It printed it only during a real upgrade.
- **The docs no longer say `secret.key` encrypts the recovery codes** (`#283`).
  They are hashes: a lost key leaves them working, and a test now signs a
  member in with one under a replaced key and re-enrols them.

- **A Windows-1252 statement keeps its `€` and curly quotes** (`#258`). ISO-8859-1
  decodes every byte, and it was tried before cp1252, so cp1252 was never
  reached and the bytes it uses for `€`, `“ ”`, `‘ ’`, `–` and `…` arrived in
  payee names as control characters, with no warning. cp1252 is now the
  reader of last resort, and the five bytes it leaves undefined are read as
  Latin-1 reads them one at a time, so a stray one no longer sends the whole
  file back to Latin-1; the preview warns when any such control character is
  in the text.
- **A UTF-16 statement can be imported** (`#263`). A file with a UTF-16 (or
  UTF-32) byte-order mark is read in that encoding instead of as Latin-1 with
  a NUL between every character, which found no columns and blamed the header
  row. An OFX file in UTF-16 is recognised as OFX. UTF-16 or UTF-32
  *without* a byte-order mark is refused with a sentence asking for the file
  saved again as UTF-8, rather than guessed at.
- **A debit written `12.50-` is read as a debit, not skipped** (`#261`). A
  trailing minus made the amount unreadable, it was read as zero, and the row
  was skipped as "this line moves no money". A sign at either end is now read,
  in both decimal conventions (`12.50-`, `1.234,56-`). A value signed at both
  ends (`-12.50-`, `(-12.50)`), and any money cell that holds something other
  than a number, is now a rejected line naming the cell and the column rather
  than a silent zero -- including the fee column. A running balance moves no
  money, so an unreadable one leaves that row without a balance and the
  preview warns about the column instead.
- **A minus sign that is not ASCII no longer turns a debit into money in**
  (`#262`). U+2212 MINUS SIGN, the figure, en and em dashes, and the small and
  fullwidth hyphen-minus were dropped as decoration. They are read as `-` in
  CSV, spreadsheet and PDF amounts, the PDF reader places such a number in its
  column as it does an ASCII-signed one, and a file signed only that way is no
  longer warned about as "every amount is positive".
- **A fresh YNAB Register.csv no longer asks about every row it already
  imported** (`#267`). A row's id was its line number and its text, so one
  transaction added above shifted every later id, and a repeat One-time Import
  offered each earlier row as a possible duplicate. The id is now the row's
  account, date, outflow, inflow, payee, category and memo, plus which of its
  identical twins in the file it is, so two identical rows stay two. Amounts
  count by their figure, not their text, so a plan re-exported with another
  currency format (`£2.50` or `2.50 £`, `€1.00` or `€1,00`) keeps its ids.
  The old line-number id is still recognised, so a ledger imported before this
  change is skipped as already imported on a repeat run of the same file. Two
  gaps remain. A row whose date was edited in YNAB gets a new id, so it is
  offered as a duplicate when within 3 days of the old date and imported a
  second time when further away. And a ledger imported before this change,
  re-exported with a row added above, still asks about each earlier row once.
- **A statement row in another currency is no longer imported as the
  account's** (`#260`). A `Currency` column (also `Moneda`, `Divisa`, `CCY`)
  and an OFX file's `CURDEF` -- or a transaction's own `<CURRENCY>` -- are now
  read and compared with the account's currency. A USD row staged into a EUR
  account used to become the same figure in euros. Now it is not imported: it
  is *skipped* when the file also holds rows in the account's currency (a
  multi-currency export, whose other rows belong to another account), and
  *rejected* otherwise -- and always rejected in an OFX file, which is one
  account, with the bank's `CURRATE` named but not used -- with a reason naming
  both currencies. A file whose every row is in another currency is refused
  with a sentence. A column naming what a purchase cost before conversion
  ("Original Currency", "Moneda origen", "Local Currency", "Transaction
  Currency") is not taken as the row's currency; among several candidates the
  one named for the account, bill or settlement wins, and if none does, no
  column is used and the preview says so. A cell that is not a three-letter
  code states nothing.
- **A statement of whole thousands no longer imports a thousand times too
  small** (`#259`). When every amount reads either way (`1.500`, `-2.000`),
  the decimal separator fell back to `.` without a word, so a Spanish rent
  account read EUR 1,500 as EUR 1.50. The account's country now breaks the tie
  (a comma for Spain, a point for the United Kingdom, from a short table in
  `app/countries.py`; a point when the account has no country or one not in
  it), and the preview says the separator was assumed, which one and why. A
  file that settles it itself is unaffected: an amount with cents, a balance
  column with cents, or a separator written twice (`1.500.000`), which is now
  read as the thousands one. When only the fee or balance column settles it
  and that disagrees with the account's country, the column is followed and
  the preview says so.
- **A reopened import still shows what reading the file could not settle.**
  The "the decimal separator was assumed" sentence and the older date-format
  warnings reached only the upload response; they were never kept on the
  batch, so reopening a staged import from the queue dropped them. They are
  now stored with the import and shown every time its preview is opened.
- **A statement imported after a One-time Import no longer adds every line
  again** (`#264`). Statement matching only looked at rows typed in by hand --
  no `import_id` -- and every One-time Import row carries `ynab:<ref>`, so each
  overlapping line was `created` and the account held the money twice. A row a
  One-time Import brought in is now offered as a twin like a typed one (same
  amount, up to four days apart), unless it is reconciled or a statement has
  already absorbed it. Matched that way -- by amount and date, not by the
  bank's key -- the preview says the row is from the YNAB history and asks for
  a check, because a new purchase of the same amount the day after the history
  ends looks just like its last row. Absorbing it gives the row the statement's key and keeps
  the `ynab:` key beside it, so running the One-time Import again still skips
  the row as its own instead of offering it as a possible duplicate. For a row
  YNAB had itself imported from the bank, YNAB's key
  (`YNAB:<milliunits>:<date>:<n>`) is stored in our statement key's shape
  (`ST:<minor>:<date>:<n-1>`), and the statement line with that key matches the
  row **exactly** -- even when its date was moved in YNAB beyond the four days.
  Exact keys are settled for the whole file before any line is matched by
  date, so an earlier line of the same amount cannot take that row first.
  A split's parts carry their parent's key but are not the bank's line, so they
  never take it. Undoing the statement import puts the `ynab:` key back.
  **One migration, `ef3af4e09c4a` -- lossy**: it adds the nullable
  `transactions.import_alt_ids`, and rolling it back drops the keys kept there.
  After a downgrade, a repeat One-time Import offers a row a statement absorbed
  as a possible duplicate again, and exact matches fall back to the four-day
  rule. A One-time Import made before this change has no YNAB keys stored, so
  its rows match by the four-day rule only; undo it and run it again from the
  API to store them.

## 0.5.1 — 2026-10-01

**Reversible: none** — no migration in this release. Check out `v0.5.0` and
restart to go back.

### Added

- **The container can run behind its own Tailscale node.** `deploy/tailnet/`
  holds a two-container compose file (the Tailscale image pinned, the app in
  its network namespace, no published port), the `serve.json` that proxies 443
  to loopback, and an `.env.example` for the auth key; the real `.env` is
  git-ignored. `deploy/DOCKER.md` is rewritten around the three ways to run
  it, chosen once at the top: on your own computer, on a server with Tailscale
  on the host, and on a server with Tailscale as a sidecar.

### Changed

- **`deploy/entrypoint.py` reads `SPENDTRACKER_HOST`** (default `0.0.0.0`, as
  before). In a shared network namespace that namespace is the tailnet node,
  so the hard-coded `0.0.0.0` would have put plain HTTP on `<node>.ts.net:8848`
  beside the HTTPS on 443. The sidecar compose file sets it to `127.0.0.1`.
- **Dependency floors raised** to what Dependabot proposed: alembic 1.20.0,
  uvicorn 0.54.0, regex 2026.9.10, xlrd 2.0.2, pdfplumber 0.11.10, Pillow
  12.3.0, pypdfium2 5.13.0 and pillow-heif 1.8.0; for development, pytest
  9.1.1, httpx 0.28.1, ruff 0.16.9, pip-audit 2.10.1 and datasette 0.65.5. CI's
  `actions/upload-artifact` moves from v5.0.0 to v7.0.1, the version the
  release workflow already used.

### Fixed

- **`deploy/DOCKER.md` passes the tree's own checks again.** It cloned the old
  repository's address, named a home directory in the restore example, and
  put `# comments` after commands in three shell blocks, which zsh reads as
  arguments when the block is pasted.

## 0.5.0 — 2026-10-01

**Reversible: clean** — one migration.
- `89c099d8239c` — clean: drops a column nothing reads and two indexes, and
  loses nothing (`#229`, `#238`).

### Security

- A mistyped password or code in a step-up form no longer signs you out. Those 401s now carry `X-Refused: proof`, and the client ends the session only on a 401 without it. Found while combining the review fixes, where step-up forms appear on three screens.
- **A sign-in's email and password are bounded** (`#202`): the address at 254
  characters and the password at 1,024, answered 422 before anything is
  recorded. A refused sign-in is written to `login_attempts` before the
  password is judged, and an unbounded address let a stranger park about a
  megabyte there per request. An address that will not fold is still counted,
  cut to the column's length.
- **A trusted browser signs in through a stranger's lockout** (`#203`). Five
  wrong passwords against a named account, or fifty across invented addresses
  from one place, used to lock the owner's own trusted laptop out too. A
  browser holding a live trusted-device record for the account it names has
  already proved the second factor, so it is now judged only against a
  ceiling of its own (twenty failures in the window) rather than the three
  ordinary arms; its failures are still recorded, and a stolen device cookie
  still cannot guess without limit. `compose.yaml` sets `FORWARDED_ALLOW_IPS`
  to the Docker bridge range, so behind `tailscale serve` the per-address arms
  are per tailnet peer again rather than one address for everyone.
- **A backup with `secret.key` in it asks for your password and a code**
  (`#204`). The zip then holds every password hash and the key that opens every
  member's authenticator, which is more than an agent key buys, and it was
  guarded one step lower: an owner's session cookie was enough. The keyed
  download is now `POST /api/admin/application/backups/{name}/download` with
  `include_key` and a step-up grant, spent before the file is located; the
  plain `GET` link serves the zip without the key only, and refuses
  `?include_key=true` in words. With the box ticked, the Backups list's
  Download opens the save panel, which asks for both factors and fetches the
  zip itself.
- **Making an owner asks for your password and a code** (`#205`). Inviting
  somebody as an owner, or promoting a member to owner, needed only an owner's
  session cookie, so a lifted cookie could mint a second owner account that
  survives every reset the real owner can make to their own credentials. Both
  now spend a step-up grant (`step_up_token` on `POST /api/admin/invitations`
  with `role: owner`, and on `POST /api/admin/users/{id}/role` to owner). A
  member-role invitation and a demotion do not. On the Admin screen, choosing
  Owner in a person's role opens a dialog asking for both, and the invitation
  panel asks for them when the role is Owner.
- **A pending sign-in and a step-up grant are spent once even by two requests
  at once** (`#206`). Both were read and then deleted, and two concurrent
  requests could both read the row before either deleted it: one stolen
  pending sign-in bought two code guesses, one grant two keys. Each is now a
  single `DELETE ... RETURNING` whose row count is the verdict, the
  compare-and-set the authenticator code already used.
- **`SPENDTRACKER_PUBLIC_URL` says where the instance lives** (`#207`).
  Invitation links, `/llms.txt` and the agent descriptor are built from it
  when it is set, instead of from the request -- which behind an untrusted
  proxy came out as `http://` and the proxy's address, a link `tailscale
  serve` does not answer. A value that is not an origin stops the boot. The
  README's new *Behind a proxy* section says what `FORWARDED_ALLOW_IPS` should
  be for the container, for `make serve` behind `tailscale serve`, and for
  `make serve` with nothing in front, where any local user could otherwise
  name their own address to the rate limiter.
- **A code or setup token with a non-ASCII character is refused, not a 500**
  (`#208`). `12345é` at any code prompt, or `é` as the setup token on a fresh
  instance, raised inside the constant-time comparison and left a traceback
  in the log. Both are now compared as bytes.
- **The cookies are named `__Host-st_session`, `__Host-st_device` and
  `__Host-st_pending`** whenever they are `Secure` (`#209`), and keep their bare
  names on an instance that has turned `Secure` off. Cookies are not isolated
  by port, so another service on the same host could plant its own session
  cookie and land the victim in its account; the prefix makes the browser
  refuse one that was not set securely, at `/`, with no domain. The name and
  the flag are one decision in `app/auth/cookies.py`, and every reader asks it.
  **After upgrading, everyone signs in once more**, and a trusted browser asks
  for a code once: the old cookies are no longer read.
- **Redeeming a recovery code revokes your live agent keys** (`#210`), as well
  as every session and trusted browser. A key minted from one of those
  browsers is in the same doubt when the phone is gone, and it is the quiet
  credential: it goes on working from wherever it was pasted. The sign-in says
  how many it revoked (`keys_revoked` on the answer) before going on, and the
  keys screen shows them revoked. Changing a password still leaves keys alone,
  by decision, and still says how many are live.
- **Auth housekeeping** (`#211`). An invitation link whose last step is refused
  because the address already has an account is now spent, so a link can
  test one address rather than as many as its holder likes; the owner sees it
  leave the list of outstanding invitations, and the log says why. Accepted,
  withdrawn and expired invitations are deleted thirty days after they ended,
  and their address is no longer copied into the audit log. Finishing setup or
  an invitation without ticking "I have stored my recovery codes" is refused
  by the server too, not only by the page. `config.py` no longer claims that
  losing `secret.key` ends every session; `deploy/DOCKER.md` says the
  container log holds the setup token until setup is done.
---

### Changed

- **No real statement excerpts or private references are left in the tree**
  (`#200`). The two-column PDF fixture is regenerated with an invented pension
  fund, amount and balances; the parser's docstrings and tests use an invented
  payee and document number; the receipt-corpus narration, first names, links
  into private notes and one machine's directory layout are reworded.
- **The project moves to `MarioLonghi-com/household-spend-tracker`** (`#227`).
  The repository address, the version check, the backup zip's instructions and
  the one-time import's issue link point there. Package names become
  `household-spend-tracker`; the display name stays "Spend Tracker", and every
  runtime identifier (data directory, database file, `SPENDTRACKER_*`, `stk_`,
  cookie names, sealed-secret derivation) is unchanged, so an existing install
  upgrades in place. `compose.yaml` pins `name: spend-tracker` so the ledger
  volume keeps its name whatever the checkout's directory is called
  (`deploy/UPGRADING.md`), and the image carries OCI labels. Issue numbers in
  this file are wrapped in backticks, and `SECURITY.md` states the supported
  versions and data paths as they are today.
- **The data-hygiene test guards more than names and IBANs** (`#201`). It
  accepts the new repository address and fails on the old one, and it now
  also catches first names and personal email addresses (as salted digests),
  pointers into private notes, the shapes of real secrets, binaries outside the
  fixtures and icons, PDF and XLS metadata, and unlisted account numbers beside
  an account word. Each detector is shown firing in its mutation test.

### Added

- **Community files for the public repository** (`#228`): a code of conduct,
  issue forms (bug, feature, agent feedback) that ask for no real data, a
  pull-request checklist, `CODEOWNERS`, release-note categories,
  `.editorconfig` and `.gitattributes`. `CONTRIBUTING.md` says how to run the
  suite away from a real ledger, where to file what, and that contributions
  are accepted under AGPL-3.0-or-later. The README gains badges and one
  "what it is, and what it is not" section; `make image` and `make compose`,
  which never had rules, leave `.PHONY`.
- **The test suite runs in parallel, and hashes passwords at test cost**
  (`#241`). `tests/conftest.py` swaps in a low-work-factor argon2id hasher
  (and re-makes the dummy hash with it, so "no such user" still costs what a
  wrong password does), gives each pytest-xdist worker its own data
  directory, and turns off fsync on the throwaway databases; the `client`
  fixture disposes of its engine. `pytest-xdist` is in `requirements-dev.txt`.
  No change to `app/`.
- **CI reshaped for the public repository** (`#242`). `tests.yml` runs in tiers
  behind a path gate, runs the suite under xdist, runs a tree in full once
  (`green-<tree>` records) and reports one required check, `ci-ok`.
  `release.yml` refuses a tag whose tree has not passed, names the tarball
  `household-spend-tracker-<version>.tar.gz` with an unversioned
  `household-spend-tracker.tar.gz` beside it, publishes the image to
  `ghcr.io/mariolonghi-com/household-spend-tracker`, and attests both. New:
  CodeQL, OpenSSF Scorecard, dependency review. Dependabot opens one grouped
  pull request per ecosystem a week, after a seven-day cooldown.

### Fixed

- **Running the migrations in-process no longer silences the app's logging.** `migrations/env.py` called `fileConfig` with its default, which disables every logger that already exists. Found while cutting this release: the log tests failed whenever xdist put a migration test before them on the same worker.
- **Committing an import a second time marked it failed** (`#212`). A
  double-click, a retried request or an agent key committing an applied import
  was refused, but the refusal left the batch `failed`, which History cannot
  undo while its rows stay in the register. Opening an applied import rewrote
  every line as a duplicate of itself and cut its rows off from *Where did this
  come from?*. The import routes now refuse anything but a staged import before
  touching it, and the batch keeps its status.
- **History credited an import to whoever staged it, not whoever applied
  it** (`#213`). When a key, or another member, commits a staged import, the
  batch now records who applied it: History names the key beside the person
  ("via Ledger Bot (bookkeeper), which applied it"), the sentence ends
  "Applied by Bob.", and the agent filter and the per-key filter find it.
- **An agent key could attach a statement to any batch in its household**
  (`#214`), a typed edit, a key issuance or somebody else's import included.
  `POST /api/agent/v1/imports/{id}/document` now takes only an import that key
  or its person staged, staged or applied, and only once.
- **The agent's categorise route changed reconciled rows** (`#215`) that the
  register refuses to edit. A reconciled row is now left alone and listed in
  the answer's new `locked`, the way a transfer leg already is.
- **The test guarding what an agent key may never reach missed some routes**
  (`#217`): membership, keys, `/admin`, the one-time import and creating a
  household are now named in it, and it fails if a name stops matching a real
  route. No agent route crossed the line. The reconciliation notes no longer
  claim a row can only be locked by a reconciliation: the register's panel
  can lock one by hand, and they now say so.
- **An OFX amount of `NaN` turned a statement upload into a server error**
  (`#218`). `NaN`, `sNaN` and `Infinity` in `<TRNAMT>` are now one rejected
  line, "no usable amount", and the rest of the file stages.
- **Searching for `%` or `_` matched every row** (`#239`), in the register's
  search and the agent's id-prefix lookup. Both now treat what was typed as
  text, so "100%" finds "100% cotton" and "ord_" finds only ids starting with
  it.
- A currency code that is three characters but not three letters (`€€€`,
  `12A`) is refused with a 422 on households and accounts; money already
  stored with such a code renders as plain text, and a screen that fails to
  render shows the error under a working menu instead of blanking the page (`#193`).
- API responses that set no caching of their own now send `Cache-Control:
  no-store`, so the ledger is not left in a shared browser's disk cache after
  sign-out. Receipt images keep their long cache (`#194`).
- Receipt previews dropped or pasted into a transaction or the Receipts screen
  are released when the upload finishes or the panel closes, instead of staying
  in memory until the tab is closed (`#195`).
- A refusal that is not JSON -- an HTML 502 from the proxy while the server
  restarts, or an HTML 401 -- still signs the browser out on a 401 and shows
  the status instead of `Unexpected token '<'`; uploads the same (`#196`).
- The household named in an `?open=…&household=…` link is accepted only as an
  id, and the receipts badge asks about the household actually on screen,
  encoded, rather than whatever the link carried (`#197`).
- A shared browser forgets more at sign-out (`#198`): `/snap`'s queue of
  photos waiting to upload and the household it last used are cleared, and a
  parked photo is sent only by the person who took it; the register's and the
  reimbursements report's remembered filters, search included, are forgotten
  at sign-out and when somebody else signs in (appearance, menu and column
  widths stay); the authenticator secret leaves the enrolment screens once the
  code verifies; and the sign-in, setup and invitation fields carry
  `autocomplete` hints (`username`, `new-password`, `one-time-code`).
- Deleting a transaction, a category or a category group, detaching a
  receipt from a transaction, and disabling a person, changing their role or
  revoking an invitation on the Admin screen now ask first, naming what they
  act on (`#199`).
- The YNAB client and the upstream version check follow no redirect, so the
  YNAB token can no longer be forwarded to another host, and the YNAB answer
  is read against a two-minute deadline rather than only a per-read timeout
  (`#219`).
- A crafted amount cell in a YNAB `Register.csv` no longer ties up a CPU
  worker: the trailing-symbol strip is a loop rather than a quadratic regex,
  and an amount longer than 64 characters is refused (`#220`).
- A statement file of junk rows is refused in a moment instead of holding a
  CPU worker for minutes: dates try the sniffed format and ISO before the long
  list, more than 1,000 rows with no readable date refuse the file, and so do
  more than 200,000 rows (`#222`).
- A malformed answer from YNAB's API -- a string or a boolean where an amount
  belongs, a record with no id, JSON nested too deep to parse -- is refused
  with "YNAB's answer could not be read" instead of a server error; the shapes
  are checked once, where the answer comes in (`#223`).
- A request that fails validation no longer has each failing value echoed
  back in the 422 (the YNAB token among them), and a statement, register or
  accounts file that cannot be read is refused with a fixed sentence rather
  than the reader library's own message, which is logged at DEBUG instead
  (`#224`).
- "Check upstream" reads at most 1 MiB of GitHub's answer, and `make restore`
  refuses a backup zip whose members inflate past a ceiling (16 GiB for the
  database, 64 KiB for the others), removing what it had written (`#225`).
- `make seed` (`seed_demo --reset`) refuses a database that holds anyone but
  the demo owner, printing the path it resolved; `--force` goes ahead only
  after that path is typed back (`#226`).
- **Only the owner can undo a one-time import** (`#216`), as only the owner can
  run one; a member asking is refused with a 403 that says so. Undoing that
  undo -- which is the import again -- is the owner's too. Every other batch,
  ordinary statement imports included, stays undoable by any member.
- **A second commit of the same one-time import is a 409, not a 500** (`#235`).
  Two commits of one plan at once -- a double click, a retry after a proxy
  timeout -- used to end in an unexplained server error for the second. It is
  now told the rows were imported by another request a moment ago. Rows are
  written 500 to a savepoint, so a refusal the database makes at write time
  fails that row alone, as a service refusal already did, rather than the
  whole import.

### Performance

- **Statement text is cut to its column on import, and two regexes read long
  runs in linear time** (`#221`). A payee is kept to 300 characters and a memo
  to 500; one planted 128 KiB cell used to make every open of the Identifiers
  and Payee-suggestions screens take minutes.
- **Re-applying payee rules runs the rules once per bank string, and Apply
  no longer works the plan out a second time** (`#231`). At 40,400 rows a plan
  took 4.7–6.6 s and 122 MiB, twice per click; it is now 0.2–0.6 s and
  18 MiB, and the Apply after a preview reuses it while nothing has been
  written. A plan moving more than 32,766 rows no longer fails with "too many
  SQL variables"; the same limit is lifted from the reimbursement claims
  report and the reimbursement check in transfer matching.
- **Suggested identifiers read each merchant once and the newest 5,000
  texts, and count pairs only when asked** (`#232`). A per-row reference is
  folded off before a text is read (never a run of four digits, so account
  and card numbers survive), and the list is served without running the
  transfer matcher; the Accounts screen shows it at once and fills in *Would
  link* from a second request, which the Transfers screen's line uses too. At
  40,000 rows the Accounts screen's request went from ~1.4 s of CPU to ~0.15
  s. *Mentions* now counts rows among the newest 5,000 distinct texts.
- **The audit log is written in batches** (`#229`). Every change row was its
  own `INSERT … RETURNING` round trip, because `changes.seq` is made by the
  database; a flush that logs 500 transactions now sends one INSERT for the
  log instead of 500. The order undo replays in is unchanged.
- **Undoing an import that created its account no longer reads the log once
  per row** (`#230`). The check for rows a later batch added asked which batch
  made each transaction separately -- 208 reads of the log to undo a 200-row
  one-time import, 2,008 for 2,000. It is now one grouped read per table and
  per 500 rows: 9 reads for either.
- **An undo is replayed a chunk at a time, and the undo confirmation shows a
  page of rows** (`#234`). Undo read every change row of the batch, with both
  row images, before replaying any; it now reads and flushes 2,000 at a time
  inside the same transaction. The confirmation's *Every row it changed* lists
  the first 200 and says how many there are in all; the count it asks you to
  confirm is still the whole batch. `GET …/batches/{id}` takes `limit`
  (default 200, at most 1,000) and `offset`, and describes a large batch from
  a count per table rather than from every row.
- **Six reads stop walking more than they need** (`#238`). The members list's
  count of transactions each person entered reads the household's own log
  through a new `ix_changes_household_table_row`, not every household's. The
  History list decides which single edits to hide one batch at a time instead
  of counting every change on the instance per page, and says a bulk edit from
  a count rather than from its rows' images. The transaction panel's *Where
  did this come from?* finds its import line through a new
  `ix_import_lines_transaction_id`. The flow reports (Income vs Expense and
  what builds on them) start from the household rather than from the
  transfer-id index, which the planner had preferred once statistics existed.
  The agent's insights totals filter the rows they add up directly rather than
  through a list of ids. The database runs with `synchronous=NORMAL`, SQLite's
  setting for WAL -- a commit no longer waits for the disk, a crash of the
  app loses nothing, and a power cut can lose the last moments but not the
  file -- a 32 MiB page cache per connection and temporary tables in memory.
  The three single-column `transactions` indexes the review suggested dropping
  stay: nothing showed them unused.
- **A statement upload no longer freezes the server while it stages** (`#233`).
  The database half of four `async` routes -- statement staging, the accounts
  CSV import, the receipt store and the recogniser's account match -- now runs
  on a threadpool worker, so `/api/health`, the register and sign-in answer
  while a 3,000-line statement is being staged.
- **Imports, bulk edits and reimbursement links hold the write lock for far
  less** (`#236`). The one-time import reads the payees it needs once and makes
  the new ones without a flush each, and links transfers with one read of
  payments and rejections for all pairs; a statement of distinct descriptors
  no longer looks up, flushes and asks the history of each new payee; a bulk
  edit, the agent's reimbursement PATCH and linking many expenses to one
  payment flush once instead of per row. Measured: a 500-row one-time import
  2,472 -> 1,032 statements, a 290-row bulk edit 1,750 -> 302, linking 200
  expenses 802 -> 204; what remains per row is the audit log's change insert.
- **The reconcile worksheet, the categorisation review and the register
  build far less to answer** (`#237`). The worksheet reads its five columns
  instead of a `Transaction` per row, and asked without a date it now stops 45
  days after the last statement (the screen always sends one). The agent's
  categorisation review groups by payee and category in SQL and fetches at
  most `limit` rows of each list, with its totals, from one windowed read
  (3.3 MiB -> 0.2 MiB at 5,000 rows). The register reads its page once,
  running balance included, and writes the JSON straight from the columns
  rather than validating every row into a model and serialising it again
  (17.2 -> 7.1 MiB at 5,000 rows; byte-identical output). It still returns
  every matching row by default: the screen does not page.
- **Six smaller efficiency fixes** (`#240`). A payee merge rewrites its rows 500
  at a time (25 -> under 20 MiB for 2,000 rows, every move still logged); the
  setup gate asks the database once after setup instead of on every `/api`
  request, and off the event loop; transfer pairing compares a row only with
  the same-amount rows inside the five-day window, and reads what each row's
  text names once; the YNAB wizard's category suggestion skips categories that
  cannot score high enough before scoring them in full; a repeat one-time
  import reads the ledger's rows as columns; and a write refused because the
  database is locked by another one is a 409 "try again", app-wide, not a 500.

## 0.4.0 — 2026-09-30

**Reversible: none** — no migration in this release. Check out `v0.3.1` and
restart to go back.

### Added

- **One-time Import from YNAB** (`#183`). A household owner brings a YNAB
  plan's history in once, from the export (the zip or its `Register.csv`; a
  `Plan.csv` on its own is refused) or from YNAB's API with a personal access
  token that is used for that request and never stored, logged or put in the
  audit log. YNAB accounts map one to one onto existing accounts in the plan's
  currency, new ones or nothing; categories map many to one, with *Ready to
  Assign* and *Uncategorized* fixed to no category. Transfers with both sides
  imported arrive linked; splits arrive as separate rows; flags can go into
  the memo; everything arrives uncleared. A dry run lists the rows that look
  like ones already in the ledger, to skip or import each. The import is
  partial -- a bad row is reported, not fatal -- and is one batch, so one undo
  in History takes it all back out, the accounts, categories and payees it
  made included. History reads "One-time Import · YNAB (CSV)" or "(API)".
  A row it brought in says so in the register's *Where did this come from?*
  (the workflow, the file or plan, the date) instead of "entered by hand".
  The Import screen points at the One-time Import until one has been done in
  the household (an undone one does not count), through a new read-only
  `GET /api/households/{id}/one-time-import/history`, and links to *How
  import works*, which has a section on it.
- **A category group can be renamed or deleted from its heading** (`#184`). On
  the Categories screen, pressing a group's name opens it: change the label, or
  delete the group. Only an empty group can be deleted — archived categories
  count, though the screen hides them — and a refusal says so in the alert at
  the top of the panel. The delete is an entry in History, and Undo there puts
  the group back as it was.

### Changed

- **Tab titles name the page and the household** (`#189`): "Spend Tracker -
  Accounts - Casa Doe", the page as the menu names it. The household's own page
  says its name once; Sign in, Set up, Invitation and New household name
  themselves; `/snap` adds the household once it knows it.
- **Transactions: a Categories filter replaces the "Needs a category" checkbox**
  (`#188`). It is a grouped multi-select like Accounts: tick categories one by one
  or a whole group from its heading. Its first tick, **Needs a category**, is
  the old backlog: rows with no category, transfers excluded, with a count
  beside it. Ticks add up, so Needs a category plus Groceries shows both. To
  get the backlog alone, press Select none; it still sorts oldest first. The
  register API takes a repeated `category_id` and a `categorised` flag, which
  combine with `uncategorised` as alternatives. A filter popover near the right
  edge of the screen now opens leftwards, unless that would put it under the
  sidebar (`#160`).

- **The household's part of the menu is reordered** (`#185`): Accounts,
  Transfers, Categories, then a *Payee* heading -- words, not a link -- over
  Payee Merge (the payee list, formerly "Payee"), Payee Categorisation and
  Payee Naming Rules. At both widths; nothing was added or removed.

### Fixed

- **Undo could fail with "FOREIGN KEY constraint failed"** when a batch had
  created both an account and its "Transfer : …" payee (found on `#183`). Nothing
  told the database session that the payee must be deleted before the account,
  so the order was left to chance. Both are now related in the model, and a test
  checks that every restricting key on an audited table is known.

---

## 0.3.1 — 2026-09-30

**Reversible: none** — no migration in this release. Check out `v0.3.0` and
restart to go back.

### Changed

- **CI runs each job on a pull request only when the change touches what it
  checks** (`#147`, `#159`, `#174`). The tests marked `repo_wide`, the browser tests
  and `release-ready` still run on every pull request; a push to `main`, the
  Monday schedule and any change to the workflow itself run everything.
  Dependabot no longer rebases its pull requests until asked with
  `@dependabot rebase`.

- **Example data no longer comes from a real ledger.** Merchant names, order
  and booking references and a card's issuer prefix in tests, docstrings, the
  demo seed and two UI hints were copied from real statements; they are now
  made up with the same shape, so every parser and matcher sees what it saw
  before. `test_data_hygiene` keeps its name denylist as salted digests rather
  than the names themselves. Nothing the app runs changed.

---

## 0.3.0 — 2026-09-30

**Reversible: none** — no migration in this release. Check out `v0.2.1` and
restart to go back.

### Added

- **The desktop menu folds away** (`#161`). A narrow rail between the menu and
  the page hides the menu and gives the page the full width; pressing it again
  brings the menu back. The choice is remembered per browser and holds while
  moving between screens. On a phone nothing changes: the menu is still the
  drawer.

- **Accounts can be imported from a CSV file, and the template downloaded**
  (`#146`). The template is the header row and nothing else: `name` and `type`
  are required; `currency` (blank means the household's), `institution`,
  `country`, `opening_balance`, `opening_date` (YYYY-MM-DD), `iban` and `note`
  are not. Comma, semicolon or tab, in UTF-8 or Windows-1252. Every row goes
  through the same checks as the form, and a preview says what is wrong with
  each line by its line number before anything is written. **All or none**: one
  row that cannot be imported refuses the file, because a half-imported list is
  one whose corrected second attempt duplicates the first. Amounts are read
  exactly — `12.345` in euros and `1,234.56` are refused rather than rounded or
  guessed. The import is one entry in History, *Account import*, and one undo
  takes back the accounts, their opening balances and their IBANs. It starts
  from **Import from a file**, beside *Add an account* on the Accounts screen.

### Changed

- **The menu is tighter and only its pages scroll.** Rows are a little closer
  together, headers and pages are both 14px, and the menu has no border of its
  own beside the fold rail. When it does have to scroll, the
  scrollbar is thin and only shows while the pointer is over it, and who is
  here and Sign out stay pinned at the bottom.

- **How receipts are stored is two choices now, each with its size** (`#162`).
  The Household page's checkbox sat under its label rather than beside it, and
  it only said what keeping originals costs once it was ticked. It is now two
  cards -- *Readable copies only* (about 33 KB a receipt, recommended) and
  *Also keep the original file* (about 2 MB, roughly 70x the space) -- with one
  short warning when the heavy one is chosen. What is saved is unchanged.

- **Snap a Receipt opens in a new tab** (`#168`). The menu link used to take the
  ledger's own tab away to `/snap`; the app now stays where it was.

### Fixed

- **The Transactions accounts filter opens over the register** (`#160`). It
  hung off the right edge of its button, so from the left of the filter line
  it opened leftwards under the menu on a desktop and off the screen on a
  phone. It now opens rightwards, the way the income-and-expense report's
  pickers already did.

---

## 0.2.1 — 2026-09-29

**Reversible: none** — no migration in this release. Check out `v0.2.0` and
restart to go back.

### Fixed

- **The release check's own test failed in CI's checkout.** It walked this
  repository back to its first commit, and CI clones one commit deep, so there
  HEAD *is* the root and no migration looked added. It passed on every full
  clone and failed on the 0.2.0 release PR. It now builds a two-commit history
  of its own. Nothing the app runs changed.

---

## 0.2.0 — 2026-09-29

The first tagged release. Everything since `0.1.0`, which `main` carried but
was never released.

**Reversible: lossy** — eight migrations, in the order they run. Five of them
lose something if rolled back; restoring the backup is the only faithful way
back past any of those.

- `b4c9e1d70a25` — **lossy**: every rewrite rule a household has written. No
  transaction and no payee is touched: a rule is a recipe, never a row of
  money (`#58`).
- `c8a1f6b30d47` — clean: only which households had turned keep-original on.
  No receipt is touched, and the environment variable goes back to being the
  only answer (`#62`).
- `a3f1c9d27e40` — **lossy**: drops every account identifier typed in since
  (`#66`).
- `b7d2e5a91c63` — **lossy**: drops each account's chosen statement product
  (`#68`).
- `c3e8a1f05d72` — clean: drops two indexes and loses nothing (`#100`).
- `db3cf4a5f9d3` — **lossy**: drops how each transfer was linked and every
  pair marked *not a transfer*; the links themselves stay (`#131`).
- `ecc9b154764f` — **lossy**: forgets every ignored identifier suggestion, so
  they are offered again; nothing in the ledger changes (`#130`).
- `a4c7e19d2b86` — **lossy**: drops every work-expense flag and every link to
  the payment that repaid it; the transactions themselves stay.

### Added

- **Work expenses, the payment that repaid them, and a Reimbursements
  report** (`#142`). A row of money out can be flagged as something work
  should pay back, linked to the payment that did, or written off — from the
  transaction panel, or for a whole selection from the bulk banner, where
  *Link as reimbursement* ties many expenses to one payment in one undoable
  act. Flagging works on a reconciled row, because whether work pays is
  decided long after the statement is ticked off. The register filters by it
  and marks a flagged row with a W. The report, under Reports, says what work
  owes, what came back, what was written off and what arrived unmatched,
  with the outstanding rows, each payment with what it repaid, and a
  by-month table; several currencies at once, each on its own line and never
  added together, since the ledger stores no rates. A row opens its details,
  and the screen remembers its currencies, dates and sorts. Income v Expense
  now leaves work expenses and their repayments out, and says how many: money
  that comes back is not spending. The transfer matcher leaves them alone
  too, because an employer repaying a card purchase looks exactly like a
  transfer on amount and date.
- **An agent has five jobs it can do here** (`#134`): file receipts against
  rows, review categorisation, flag work expenses read off a corporate
  portal, report across countries and currencies, and link the transfers the
  matcher missed. New routes match evidence to rows without storing
  anything (amounts compared exactly and only in their own currency), read
  the register, review categorisation per payee, run the reports, list the
  rates the household actually got, flag work expenses and link or rule out
  transfer pairs. A pair an agent links is recorded as the agent's, never
  counts as history, and is listed for a person to keep or unlink. An agent
  still cannot unlink or delete anything. `agent/README.md` and `/llms.txt`
  are organised around the five jobs, and the sample client and MCP server
  offer a tool per job.
- **The agent API explains itself** (`#36`–`#50`, `#57`). An unknown `/api/` path
  is a JSON 404 rather than the web app; the manifest says how to
  authenticate and what every route returns; a key sent in the wrong header
  is told so; `/.well-known/spend-tracker-agent.json` describes the software.
  A staged import says what it noticed — rows it skipped as duplicates, card
  repayments landing as income, a declared row count or total that does not
  match — in a few hundred bytes instead of the request handed back. Rows can
  be found by external id, an import row can carry its category, rows
  already in the ledger can be recategorised in one batch, receipts can go
  up as a binary body or a batch, and an import can keep the document its
  rows were read from. An import an agent staged and nobody committed expires
  after seven days. History and the transaction panel say which key a person
  acted through.
- **Account identifiers, and statements that find their own account** (`#66`,
  `#67`). An account can hold its IBAN, account and card numbers, the names a
  bank uses for it and the tag its bank puts in a download's name; a
  household can say how banks spell each holder. The Import screen picks the
  account a file names, or says when the chosen one is not it.
- **Transfers are found, linked and kept out of the report** (`#70`, `#71`). Two
  rows from two statements become the legs of one transfer, each keeping its
  own date and bank words, with the rate stored across currencies. Pairs a
  descriptor or past links vouch for link at import; the rest wait on a
  Transfers screen, where several can now be ticked and linked in one go
  (`#126`).
- **Statements that hold several accounts, charge fees and overlap** (`#68`,
  `#69`). An account takes one product of a mixed file (Revolut's checking and
  savings pockets), so the other rows are skipped rather than landing in the
  wrong account. A fee becomes its own row under *Bank fees*, pending rows
  wait until they settle, and the running balance is checked, both within
  the file and against where the ledger left off.
- **How import works** (`#73`), under Admin: every path a statement can take,
  in the preview's own words. A test fails when the importer grows an outcome
  the page does not explain.
- **Suggested identifiers** (`#130`). The Accounts screen lists identifiers the
  register already uses and nobody has added — words on the far side of
  transfers a name or a person linked, IBANs and card numbers that pass their
  check digits, masked card numbers, UK `A/C` and sort-code numbers, Spanish
  CCC and contract numbers, quoted pocket names, an account's own name
  without its bank, and the tag every statement file of an account carries —
  each with how many rows mention it and how many transfer pairs adding it
  would link, counted by the matcher with nothing written. Add or Ignore; an
  ignored one is not offered again. The Transfers screen says how many pairs
  they would link between them, and the Import screen offers an unrecognised
  file's tag for the account a person picks. Never added by itself.
- **Payee rules you can check before trusting** (`#59`, `#60`, `#61`). The rule
  panel says what a pattern is matched against and shows what it would
  match on this ledger; payees that differ only by a reference number are
  grouped and offered as rules, largest first; and a new rule can be
  re-applied to rows already in the ledger, previewed first, one undo for
  all of it, leaving rows somebody corrected by hand alone.
- **A payee rule can rewrite instead of naming a payee** (`#58`). `SQ *`,
  `PAGO MOVIL` and `COMPRA INTERNET` are payment rails, not shops, so there is
  no one payee to map them to. A rewrite rule takes the rail off and the
  mapping rules run against what is left, which composes: one `COMPRA INTERNET`
  strip rule plus an existing `AMAZON` rule covers the whole family.
- **Whether receipts keep the original upload is a household setting** (`#62`),
  defaulted off — which is what every install already did.
  `SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL` still works and is now a floor.
  Greyscale and 1-bit conversion were measured and **not** built: greyscale
  saves 2.3%, and the thresholding that would save more either destroys the
  photograph or comes out larger than colour.
- **The register filters by amount, Cleared, Source and *Needs a category***
  (`#54`, `#123`). One amount box matches every money column in every currency;
  *Needs a category* sorts oldest first, because a backlog is worked forward.
  The date range steps back and forward by its own length, and a skip link
  jumps the filters to the rows (`#53`).
- **The register remembers its filters and sort** per browser and household,
  and gains a select-all box, a text-size step, a column showing either leg
  of a transfer, and columns that widen when their heading is dragged (`#74`,
  `#143`). A filter a report sends you to is shown over the remembered ones
  and never written into them.
- **The import preview numbers the file the way its Line column does** (`#128`).
- **A backup can be downloaded, saved to Drive or Dropbox, and deleted** (`#133`).
  Each backup on the Application screen downloads as a zip laid out like a
  `make backup` folder, with a manifest and a README telling whoever opens it
  next what it is, how to read it and how to restore it. `secret.key` goes in
  only when the box is ticked. *Save to Drive/Dropbox* uses no keys: the share
  sheet, saving straight into a synced folder, or download-then-upload,
  whichever the browser has. Delete asks first, and says so when it is the
  newest or the only backup.
- **`make restore` takes what the screen makes** (`#133`): a folder, a zip or a
  bare `.sqlite3`, with `KEY=` for one that carries no key. It now opens a real
  authenticator secret with the key before moving anything, and refuses the
  wrong key unless told `--without-key`.
- **The Application screen and `/api/health` say which commit is running.**
  The version cannot tell two builds of 0.2.0 apart. The screen shows the
  short SHA, branch, commit time and whether tracked files had changed at
  start; `/api/health`, which answers without a session, gives the short SHA
  only. A release tarball or image without `.git` carries it in
  `app/build.json`.
- **The Application screen answers "which process, which database"** (`#51`,
  `#52`): process id, SQLite version and journal mode, the database path, each
  with its own actions beside it. The log view opens at its newest line.
- **Three log files** — `app.log`, `access.log` and `sql.log` — so the one
  that can hold the ledger in plain text is never the one somebody is asked
  to send. Rotated files are named for when they were closed, lines carry
  milliseconds and a UTC offset, and turning logging down is recorded in
  every file before it goes quiet.
- **The upgrade drill** (`#63`): `make upgrade-check`, `make backup`,
  `make restore`, `make upgrade`, and `deploy/UPGRADING.md`. The backup runs
  `PRAGMA integrity_check`, reopens the copy and counts its rows; the upgrade
  opens a real sealed TOTP secret with `secret.key` afterwards, because the
  wrong key leaves every row readable and every authenticator refused.
  `--keep N` prunes after verifying, and `--method backup` preserves page
  numbering for an off-site copy that deduplicates.
- **A container** (`#63`): `Dockerfile`, `compose.yaml` and
  `deploy/DOCKER.md`, Chainguard base by default with a `PY_BASE=` /
  `PY_RUN=` bypass. It runs as non-root on a read-only root, and the
  entrypoint says when a volume is owned by the wrong user and repairs it
  with `--fix-ownership`. CI starts the image against a fresh volume on both
  bases, not just builds it.
- **`make serve`** (`#63`) — uvicorn without `--reload`, keeping the loopback
  bind that makes `tailscale serve` the only way in. **`make install-prod`**
  installs the runtime dependencies alone (a first step toward #46).
- **`make version`** (`#63`), moving every copy of the version together — the
  three files and, since 0.2.0, the two in `client/package-lock.json` — with
  `tests/test_version.py` failing when they drift.
- **A pull request into `main` has to be a release.** CI's `release-ready`
  job refuses one until the version is past the newest tag, the CHANGELOG
  has a dated section for it with nothing left under Unreleased, and every
  migration added since `main` is named in it; the release job checks the
  tag against the CHANGELOG too. README → *Cutting a release*.
- **`make audit`** (`#63`) and a CI job for it, on every change and weekly.

### Changed

- **Transfer matching weighs its evidence** (`#125`, `#127`, `#131`). A row with a
  category is not a transfer: never paired, never waiting, never linked on
  import, and the Waiting list can categorise rows one at a time or ticked
  together. Among same-day rivals, the pair whose two descriptions share the
  most words wins; joining words and the Spanish transfer words do not
  count. Each link records how it was made, and only a link a name or a
  person vouched for counts as history for the next; money out of a card,
  and a row naming someone who has paid the household, are never linked
  without asking. An unlinked pair, or one marked *Not a transfer*, is not
  offered again, and *Linked by history only* lists the links nothing
  vouches for, with Unlink and Keep.
- **A holder's name matches however the bank writes it** (`#132`): reordered,
  with middle names dropped, as initials, or cut off at the bank's field
  width — including when a memo follows the cut. The Accounts screen shows
  what each name catches. A holder only ever makes a pair *suggested*.
- **A transfer leg has no category** (`#124`). The server refuses one, and a
  bulk *Set the category* skips legs and says how many.
- **The register is called Transactions**, as the menu always said (`#143`).
  *Add transaction* opens a menu holding the transfer form, the currency
  toggle sits by the heading, the accounts picker is labelled, and while
  rows are selected their sums and bulk acts are pinned to the bottom of the
  screen (`#144`). Every filter is one height and one type size, 16px on a
  phone so iOS does not zoom on focus (`#145`).
- **Payee Naming Rules and Payee Categorisation are two screens** (`#65`), so
  the rules are no longer under hundreds of payees.
- **The agent's receipt link refuses to move a receipt** already on another
  row unless asked with `move: true` (`#134`), and the agent's receipt batch
  has a 32 MB whole-body ceiling (`#83`). Both are contract changes for agents.
- **Large ledgers are faster** (`#100`–`#105`). The ledger is indexed by payee
  and by transfer account, and housekeeping runs `ANALYZE`; committing,
  staging and undoing an import, the History list and the transfer sweep
  each read what they need once rather than per row.
- **The default data directory is outside the checkout** (`#63`):
  `~/.local/share/spend-tracker`. An existing `./data` still works and is not
  moved. `git clean -xdf` deletes ignored files, `data/` is ignored, and that
  command used to take the ledger and `secret.key` with it.
- **`make lan` keeps the API docs off** (`#95`). It serves as production; `make
  lan DOCS=1` opens `/api/docs` to the network and says so.
- CI now checks pull requests into `dev` as well as `main`, and no longer
  leaves a usable token in `.git/config` for every step after checkout. The
  release job that holds a write token installs and runs nothing, and the
  Node build stage is pinned by digest (a first step toward #46).
- **Dependencies** (`#135`–`#141`, applied on `dev` rather than the `main`
  branches Dependabot opened them against): uvicorn >=0.53.0, SQLAlchemy
  >=2.0.54 (resolves to 2.1), python-multipart >=0.0.32, argon2-cffi >=25.1.0,
  pytest-cov >=7.1.0; client @tanstack/react-query 5.104, jsdom 30.
  Dependabot now targets `dev`.
- **Node must be ^22.22.2, ^24.15 or >= 26**, not ^20.19: jsdom 30 refuses
  older versions and the client tests then fail. `make preflight` checks it.

### Fixed

- **Re-proving it is you** (`#78`–`#82`, `#116`). Replacing the authenticator needs
  a code from the current one (or a recovery code), not just the password.
  Changing the password also forgets trusted browsers, pending sign-ins and
  step-up grants, and says how many agent keys are still live. Password
  re-checks on the Profile screen are rate limited. One authenticator code is
  spent once even when two requests race for it, a burst of wrong passwords
  gets no more than the budget, and a burst of failed sign-ins no longer
  runs the connection pool dry and stalls every request.
- **Household scope and undo** (`#76`, `#77`, `#97`–`#99`). An import or receipt
  replacement can only touch the household in the URL, and the audit hook
  refuses a change stamped with another. Undo refuses to bring back a revoked
  agent key or invitation, or a receipt whose bytes have been swept.
  Deleting a category clears payee defaults as logged changes, so undo
  restores them. Account names differing only by case are a conflict, not a
  500 on the next transfer.
- **Hostile or oversized files** (`#83`–`#90`, `#119`). A request body over its
  route's ceiling is refused before anything reads it. Parsing and image
  work run on a two-wide pool off the event loop, so one slow file no longer
  stalls every request. Receipts are decoded once at display size and PDFs
  sized before they render; PDF statements have page and work limits, a
  sparse `.xls` is read by its cells, unclosed OFX tags read in linear time,
  and an oversized cell or figure is a sentence, not a 500.
- **What leaves the box** (`#91`–`#94`). The `/db` snapshot no longer carries full
  account numbers in the audit log or import lines. The ledger, backups,
  snapshot and logs are created readable by their owner alone, with a
  startup warning for anything that is not. `access.log` no longer records
  invitation tokens or query strings, and uvicorn's console still prints the
  cleaned line. Requests naming a host not in `SPENDTRACKER_ALLOWED_HOSTS`
  are refused (the default admits localhost, `*.ts.net` and any IP literal),
  discovery documents are no longer cached, and the upgrade placard escapes
  its log.
- **The browser** (`#106`–`#108`). A session that ends takes its cached data
  with it, so the next person at the tab never sees the last one's ledger.
  Search waits for a pause in typing and cancels what it overtakes; the
  import preview renders a window of a long file; theme colours are checked
  again before they reach the stylesheet; `/snap` frees its photos.
- **The setup token reaches the console.** The app's own log never did, so
  `docker compose logs` could not show the token a first run prints; the
  setup screen now names the real token path and the command that reads it.
- **The running balance is withheld under the Cleared filter** (`#54`), rather
  than summing a subset and calling it the account's balance.
- **A money column keeps its currency groups still when it turns round**
  (`#129`): base currency first, then the others A to Z, then the figure.
- **Opening a category field lists every category** (`#143`), not the first
  eight.

### Known gaps

- Branch protection, CodeQL, Scorecard and artifact provenance all need the
  repository to be public or on a paid plan. Dependabot alerts need switching
  on in the repository settings. None of these is a commit.
- The Chainguard base is not pinned by digest. The free tier is `:latest` only,
  so the tag moves; pinning wants a digest Dependabot then maintains.
- Python dependencies have no lockfile with hashes yet (done since in #46).

---

## 0.1.0 — never tagged

The number `main` carried until 0.2.0. No release was cut under it, so there
is nothing to download; everything built since is in 0.2.0.
