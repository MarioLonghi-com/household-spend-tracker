# Contributing

This repository is worked on by people and by programs, often at the same time,
often on the same machine. Most of what follows exists because two workers in
one checkout stepped on each other and it took a while to see why.

`CLAUDE.md` holds the rules about *the code* — money is integer minor units, no
write reaches an audited table outside a batch, fixtures have two of everything.
Read it before you change anything. This file is about *the work*: what to call
things, where to put them, and how to hand something over.

---

## 1. Names say what a thing is

The problem this solves is real and it happened here. `~/code/household-spend-tracker`
is a clone. `~/code/household-spend-tracker-dev` looks like a second clone, or a
folder of dev notes, and is neither: it is a **worktree** of the first one,
checked out on the `dev` branch. Nothing in the name says so. You find out by
running `git worktree list`, which you only run if you already suspect.

So every name carries a prefix that says what kind of thing it is, before it says
which thing.

| Prefix | What it is | Where it lives | Example |
|---|---|---|---|
| *(none)* | **the** clone — one per repo, the one you return to | `~/code/` | `household-spend-tracker` |
| `br/` | a branch | in git | `br/feat-reports-income-expense` |
| `wt-` | a worktree directory | `~/code/` | `wt-household-spend-tracker-feat-reports` |
| `tmp-` | scratch. Deletable without asking | `~/code/` | `tmp-household-spend-tracker-csv-samples` |
| `arch-` | frozen. Reference only, never pushed to | `~/code/` | `arch-household-spend-tracker-prototype` |

Three rules make the table usable.

**The bare name is the clone, and there is only one.** `household-spend-tracker` with no
prefix is the checkout you clone once and keep. Everything else derived from it
announces itself. A directory with no prefix that is *not* the clone is the bug
this scheme exists to prevent.

**A worktree directory is named after its branch.** Take the branch, drop the
`br/`, prefix `wt-<repo>-`. So `br/feat-reports-income-expense` lives in
`wt-household-spend-tracker-feat-reports-income-expense`, and you may shorten the tail as
long as it stays unambiguous — `wt-household-spend-tracker-feat-reports` is fine while it
is the only reports worktree. The branch is the source of truth; `git worktree
list` is what resolves a short name back to it.

**Trunks keep their bare names.** `main` and `dev` are not working branches and
they are not renamed. `main` is in the CI trigger
(`.github/workflows/tests.yml` fires on `push` to `main`), in every clone, and
in the remote's default. A `br/` prefix on it would buy clarity nobody needed
and break three things that did.

### Branch names

    br/<kind>-<slug>

`kind` is one of `feat`, `fix`, `chore`, `docs` or `exp`. `exp` is the one worth
having: it means *this may be thrown away*, and it tells a reviewer not to ask
for tests on a spike.

    br/feat-reports-income-expense
    br/fix-csrf-audit-window
    br/chore-ci-on-main
    br/docs-contributing
    br/exp-sqlite-fts-payees

The slug is what changed, in words, not a ticket number. `br/fix-issue-16` needs
a second tab open to mean anything; `br/fix-node-arch-mismatch` does not. If
there *is* an issue, it goes in the commit body and the PR title, where there is
room for both.

> The older branches — `feature/reports-income-expense`, `chore/ci-on-main` —
> predate this and are not being renamed. A merged branch's name is history.
> New work uses `br/`.

### Anything else you put in `~/code/`

Name it for what it relates to *and* what it is: `tmp-household-spend-tracker-csv-samples`,
not `csv-samples`; `arch-household-spend-tracker-prototype`, not `old`. Six months later
the only thing that stops you deleting the wrong directory is its name.

---

## 2. Working in parallel

Two workers in one checkout is the failure mode, and it is quiet. One switches
branch; the other's `--reload` server picks up the new `app/`, fails
`schema_check.verify` at startup and exits 144. The second worker sees a dead
server and no reason for it — it was working a minute ago, and nothing *they*
did changed.

**So: one worker, one worktree.** Not one branch — one worktree. Branches are
cheap; the thing that has to be exclusive is the working directory.

```bash
# from the clone, or from any worktree of it
git worktree add -b br/feat-your-thing \
    ../wt-household-spend-tracker-feat-your-thing dev
cd ../wt-household-spend-tracker-feat-your-thing
make install-py
```

- `make install-py` — each worktree needs its own .venv

`.venv/`, `client/node_modules/` and `app/static/dist/` are gitignored, so each
worktree gets its own for free. **The database does not.** `app/config.py`
(`resolve_data_dir`) looks for `SPENDTRACKER_DATA_DIR`, then an existing
`./data`, and otherwise falls back to `~/.local/share/spend-tracker` -- and a
fresh worktree has no `./data`. Every fresh worktree therefore opens the *same*
ledger, the one the clone is probably serving on 8848, and `make seed` in any
of them wipes it for everybody.

So give a worktree its own before running anything that touches the database:

```bash
mkdir data
```

An existing `./data` wins over the shared default, and `make upgrade-check`
prints which directory it resolved if you want to be sure.

The other thing that does not isolate itself is the **port**.

| | Port | Data |
|---|---|---|
| the clone | 8848 | whatever `make upgrade-check` says |
| a worktree | pick one from 8850 up | its own `data/`, which you made |

```bash
make dev PORT=8851
```

`dev`, `api` and `lan` honour it. **`make web` does not** — Vite binds 5173 and
proxies to a hardcoded `127.0.0.1:8848` (`client/vite.config.ts`), so only one
worktree at a time can use the hot-reload pair. Everyone else runs `make dev`,
which builds the client and serves it from the API on their own port. If two
people need Vite at once, that config needs to learn an environment variable
first.

There is no central register of who has which port, because a table of ports on
one person's laptop is a file that is wrong by the end of the week. `lsof -i
:8851` answers the question in less time than reading a table would.

### Renaming one

`git worktree move` keeps git's bookkeeping straight — it rewrites the `.git`
pointer file and the `gitdir` beside it, so `git worktree list` is correct
immediately.

```bash
git worktree move ../old-name ../wt-household-spend-tracker-new-name
make install-py
```

- `make install-py` — NOT optional -- see below

**It breaks the venv, silently until the next run.** A Python venv writes its
own absolute path into the shebang of every script in `.venv/bin`, so after the
move `./.venv/bin/uvicorn` still starts `#!/…/old-name/.venv/bin/python3.14`
and dies with `bad interpreter: no such file or directory`. Nothing warns you
at move time; you find out when the server will not start. Deleting `.venv` and
running `make install-py` is the whole fix. `client/node_modules` survives the
move — npm does not hardcode paths the same way.

Anything else holding the old path needs the same treatment: a shell sitting in
the directory keeps working (its cwd follows the inode) but its `$PWD` is now a
lie, so `cd $(pwd -P)` before trusting a relative path.

When you are finished:

```bash
git worktree remove ../wt-household-spend-tracker-feat-your-thing
```

Deleting the directory by hand leaves git's bookkeeping behind; `git worktree
prune` cleans up after you if you already did.

### If you must share a checkout

Sometimes you are dropped into one that is already in use. Then:

- Treat a working server as unverified. It may have died on someone else's
  branch switch. Check before you conclude anything from it.
- Never bare `git stash` / `git stash pop` — the stash stack is shared across
  every worktree of the repo, and pop takes whatever is on top, which may be
  someone else's. Use `git stash push -u -m "<a tag only you would use>"`,
  find your entry by that tag, and `git stash apply <sha>`. A throwaway WIP
  commit is simpler and safer than either.
- Only the branch that *owns* a migration can downgrade past it. Run `alembic
  downgrade` from the checkout holding the revision file, pointed at a copy via
  `DATABASE_URL=sqlite:////abs/path.sqlite3`.

---

## 3. How work flows

    main  ←  dev  ←  br/*

Branch off `dev`. Open the PR into `dev`. `dev` → `main` is a separate merge and
it is the one that deploys.

**CI runs on every pull request into `dev` or `main`**, and again on the push
that lands one on `main`. On a pull request, each job runs only when the change
touches what that job is the check for: lint and the full pytest suite for
Python and the requirements; the client's typecheck, vitest and build for
`client/`; the browser tests for anything a browser could see; the upgrade
rehearsal -- which also proves the app boots on the runtime requirements alone
-- for backend code and migrations; the container builds for the Dockerfile and
what it installs; the dependency audit for the manifests. The lists are the
`changes` job at the top of `.github/workflows/tests.yml`. The tests marked
`repo_wide` (data hygiene, pasted secrets, client/server agreement, the
version's five copies) run on every pull request, whatever it touches, with
the client's dependencies installed so the money-rounding comparison runs
rather than skips. A push to `main`, the Monday schedule and a hand-started run
owe everything.

**One tree, one full run.** When every job passes, `ci-ok` records the tree
(`git rev-parse HEAD^{tree}`) as an artifact named `green-<tree>`, and the next
run on an identical tree -- the `dev` → `main` release PR, the push that lands
it -- skips the suite, the browser tests and the image, and says so in its
summary. The rehearsal still runs on the release PR, because there its "before"
is the last release. `release.yml` refuses to publish a tag whose tree has no
such record.

**`ci-ok` is the one check to require.** It waits for every job, fails on any
failure or cancellation, and reads a skip as "not owed", so gating or renaming
a job never leaves a required check waiting for a run that will not come.

The suite runs as `pytest -n auto --dist loadfile` (pytest-xdist). Each worker
gets its own `SPENDTRACKER_DATA_DIR` under the one it was given, and
`tests/conftest.py` hashes passwords with test-grade argon2 -- real argon2id,
low work factor -- because the setup wizard most HTTP tests walk used to spend
most of the suite's time hashing. The same invocation works locally in a venv
with `requirements-dev.txt` installed.

A pull request into `main` also runs `release-ready`, which checks that it is a
release: the version moved, the CHANGELOG has a dated section for it, and every
new migration says what rolling it back loses. See the README, *Cutting a
release*.

**If you add a test that reads a file outside the backend** -- a client source,
a doc, the Dockerfile -- mark the module `pytestmark = pytest.mark.repo_wide`.
Otherwise a pull request that changes only that file skips the test that guards
it.

A newer push to a pull request cancels the run on the older one. Dependabot no
longer rebases its pull requests by itself when `dev` moves; comment
`@dependabot rebase` on one when you want to merge it. It opens one grouped
pull request per ecosystem a week, on Monday, for releases at least seven days
old; security updates arrive on their own whenever they are published.

### The self-update job

`self-update` in `tests.yml` -- one of the jobs `ci-ok` requires -- runs when a change touches the updater, `deploy/`,
the drill and restore scripts, the maintenance page, the migrations, the
Dockerfile or a compose file. It builds release A from the merge base and B
from the change -- plus a few variants of B built `FROM` it, such as one whose
migration fails after writing a row -- pushes them to a registry that answers
as `ghcr.io` on the runner, and updates a running A through **the updater in
its own container**, as compose starts it. What it proves is what an owner
would see: after each of E1-E15 (`tests/self_update/scenarios.py`) it reads
row counts, the ledger's stamp, which digest each container runs, a
container's `StartedAt` and the pin in `.env`. It runs on rootful Docker
(loopback and the Tailscale sidecar layout, with a stand-in for Tailscale),
rootless Podman, and Docker on arm64. Rootless Docker is in the manual
matrix instead: its daemon cannot make its bridge network on a hosted runner.

The only thing replaced is verification: nothing built on a runner has an
attestation. The CI updater image (`tests/self_update/ci-updater.Dockerfile`)
is the release's real updater image with a test-only trust policy added on
top. `release.yml` never builds or names it, and
`tests/test_self_update_ci.py` fails if it ever does.

**One scenario on your own machine**, with Docker Desktop running:

```bash
.venv/bin/python -m tests.self_update.local E3
.venv/bin/python -m tests.self_update.local E3 --no-build
```

- `local E3` -- A from the merge base with `origin/dev`, B from your working tree, uncommitted changes included; `E1,E11` or `all` work too
- `--no-build` -- reuse the releases the last run built

It runs the scenarios inside a `docker:dind` container, so Docker Desktop's
own engine, images and containers are left alone; it leaves behind the images
it built (`spend-tracker-ci*`), two registry containers (`st-ci-registry*`)
and `.self-update/` in the checkout. E6, which restarts the engine, cannot run
there. A scenario the updater is known not to pass yet is listed in
`KNOWN_GAPS` in `scenarios.py`: it runs and prints, and does not fail the job
(the list is empty). A leg whose engine or layout needs a fix in release A's
updater -- the merge base's -- names the fixing commit as its `a_floor` in
the matrix: while the merge base predates it, A is built from that commit
instead, and the run says so in a notice. The engine canary does the same.

**The engine canary** (`engine-canary.yml`, weekly, never a pull-request
check) runs an update and the handover on the newest Docker Engine stable and
test-channel releases and the newest Podman, because an engine updates on a
laptop without anybody deciding to -- Docker Engine 29 once raised its API
floor and broke every client that pinned an older one. It records each
engine's version and API window, and a failure opens one issue labelled
`docker` (or comments on the open one). The fix ships in a release, and the
updater going first brings it to every instance.

Two more workflows run beside `tests.yml`, and neither is a pull-request check
on `dev`: `codeql.yml` (Python and TypeScript, on the release PR, on `main` and
weekly) and `scorecard.yml` (OpenSSF Scorecard, weekly and on pushes to `dev` --
the default branch, the only one it will score). Their findings land in the
Security tab.

---

## 4. Commits

**No tool-generated trailers.** No `Co-Authored-By:` naming an assistant or a
coding tool, no "Generated with" line, in commit messages or in PR
descriptions. The person who commits is the author of record, and answers for
the change. 46 commits had to be rewritten once to take these back out.

This applies to agents most of all, because several coding harnesses add such a
trailer *by default* and will keep doing so unless told not to. If your tooling
appends one, strip it before you commit. People are different: if two of you
wrote a change together, `Co-Authored-By:` for the human co-author is welcome.

Write the subject as a sentence about what changed, not a label:

    A week is the date of its Monday, not a week number
    A lapsed key is kept as long as a revoked one

not `fix: week calculation`. The body is where the reasoning and the issue
number go, and it is worth more than the subject — it is the only place that can
say *why the obvious thing was wrong*.

**A fix is new code.** It gets reviewed like new code, with a fixture that has
two of everything.

---

## 5. Before you push

`make hooks` installs `.githooks/pre-commit` — ruff, the bulk-statement grep,
and the client typecheck plus vitest when `client/` changed. It takes seconds,
which is the entire design: a gate that costs minutes gets `--no-verify`'d
within a day and then guards nothing. The `pre-push` hook is gone.

Run the rest yourself:

```bash
make lint
make test
make e2e
```

- `make lint` — ruff, the audit grep, client typecheck
- `make test` — the suite with its coverage gate, then the client's
- `make e2e` — a real browser, desktop and phone — before anything touching the UI

**Changed a Python dependency? Edit the `.in`, then run `make lock`.**
`requirements.in` and `requirements-dev.in` hold the floors and the reasons;
`requirements.txt` and `requirements-dev.txt` are generated from them, every
package pinned and hashed, and every install reads those with
`--require-hashes`. Never edit a `.txt` by hand: CI's `lock` job re-runs the
compile and fails the pull request when the result differs. `make lock` keeps
existing pins; `make lock UPGRADE=1` moves everything to the newest the floors
allow.

**Know which ledger the tests will touch.** `tests/conftest.py` gives the suite
a fresh temporary data directory *unless `SPENDTRACKER_DATA_DIR` is already set*,
in which case it uses that one. So never run the suite from a shell where that
variable points at a real ledger. Unset it, or point it somewhere disposable:

```bash
export SPENDTRACKER_DATA_DIR="$(mktemp -d)"
make test
```

`make e2e` seeds and serves its own throwaway instance on 8850, in
`e2e-data/` at the top of the checkout, whatever the variable says.
`E2E_PORT=8862` moves it to another port, so a worktree can run one spec
(`cd client && E2E_PORT=8862 npx playwright test e2e/locales.spec.ts`) while
8850 is busy. Its client build is a **QA build** (`vite build --mode qa`), the
only kind that can show the draft languages (#271): run `make dev` or
`npm run build` before serving that `app/static/dist` to anyone.

`npm run build` is the client check, not `tsc --noEmit`: the latter has passed a
file containing an undefined name.

And the test that is not a test: **asserting a 200, or that a mechanism fired,
is not asserting what it did to the data.** Four of the previous build's
nineteen bugs hid behind exactly that.

**Changed what an import does? Change the page that explains it** in the same
commit: `client/src/screens/importGuide/en.tsx`, *Admin → How import works*
(and the same page in any other language it has been written in).
`tests/test_import_guide.py` fails on an outcome, a state word, a format or an
identifier kind it does not mention -- but only a person notices a sentence
that has stopped being true.

**Extracted a short or ambiguous message? Give the translator a note:**
`t({ message, comment })` or `<Trans comment="…">`, one line of English saying
what it is -- the rule is in `client/src/locales/README.md`, and
`catalogs.test.ts` refuses a bare one- or two-word message.

---

## 6. If you are a program

Everything above applies to you. A few things that only bite agents:

- **Take your own worktree.** Do not share a checkout with a session you cannot
  see. `git worktree list` shows you who is already here.
- **Strip your harness's attribution trailer** before committing. See §4.
- **Say which worktree and which port you used** when you report back. The next
  session cannot see your shell.
- **If the doc and the code disagree, both are suspect until one is proven.**
  Including this document.

To drive the running app rather than the repository, `agent/README.md` is the
HTTP API — keys, conventions, and the three mistakes a language model reliably
makes against it.

---

## 7. Where to file what, and on what terms

- **A bug or a feature request:** an issue, from the templates. Describe the
  shape of a statement rather than attaching one: no real names, IBANs, card
  numbers or amounts. A synthetic file of the same shape is always welcome.
  `tests/test_data_hygiene.py` holds the fixtures to the same rule.
- **A vulnerability:** never a public issue. See [`SECURITY.md`](SECURITY.md).
- **A change:** a pull request into `dev`, with a line under `## Unreleased`
  in `CHANGELOG.md`.

Issue numbers written as `` `#NNN` `` in `CHANGELOG.md`, and bare `#NNN` in code
comments, below this repository's first public issue refer to the project's
original private tracker. They are kept as provenance; they are not links.

**Contributions are accepted under the project's licence,**
AGPL-3.0-or-later (see [`LICENSE`](LICENSE)). By opening a pull request you
agree that your change is published under it.

Everyone taking part is expected to follow the
[code of conduct](CODE_OF_CONDUCT.md).
