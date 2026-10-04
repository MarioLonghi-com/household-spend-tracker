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

---

## Unreleased

### Added

- **The New account panel asks for the bank and a note** (#12). The API
  always took `institution` and `note` on create, but the panel never
  collected them, so the only way to record the bank was to make the account
  and open it again. Both are sent trimmed, and a blank one as null. The edit
  panel now asks in the same order -- country, then bank, then note -- and
  both hold the bank to 120 characters and the note to 2,000, the API's own
  limits.

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
  installs the runtime dependencies alone (`#96`).
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
  Node build stage is pinned by digest (`#96`).
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
- Python dependencies have no lockfile with hashes yet (`#96`).

---

## 0.1.0 — never tagged

The number `main` carried until 0.2.0. No release was cut under it, so there
is nothing to download; everything built since is in 0.2.0.
