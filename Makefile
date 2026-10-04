.PHONY: help preflight install install-py install-prod install-client client dev api lan web test e2e lint \
        migrate revision seed snapshot db-view clean hooks serve version backup restore \
        upgrade upgrade-check doctor reset audit

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

PYTHON ?= python3
NODE   ?= node

# One instance per worktree, and they cannot all have 8848. .venv and
# node_modules isolate themselves; the database only does if the worktree has
# its own ./data -- without one it falls back to ~/.local/share/spend-tracker,
# which every fresh worktree shares (app/config.py, resolve_data_dir). See
# CONTRIBUTING.md.
PORT   ?= 8848

# NODE= has to govern npm and npx too, and the only thing that achieves that is
# PATH. Parameterising them separately (NPM ?= npm) looks equivalent and is not:
# npm's own shim starts `#!/usr/bin/env node`, so even the right npm re-execs
# whichever node PATH finds first -- which, on the machine in #16, is the broken
# x86_64 one. Resolving NODE to its directory and putting that first means one
# knob covers install-client, client, web, test and lint.
#
# Empty when NODE cannot be found, so PATH is left alone and preflight is what
# reports the problem. `cd && pwd` rather than dirname alone: it normalises a
# relative NODE= into something still valid inside a recipe's `cd client`.
NODE_BIN := $(shell if command -v $(NODE) >/dev/null 2>&1; then \
	cd "$$(dirname "$$(command -v $(NODE))")" && pwd; fi)
ifneq ($(NODE_BIN),)
export PATH := $(NODE_BIN):$(PATH)
endif

# Every prerequisite this repo has, checked in one place and reported in a
# sentence somebody can act on. The `$(NODE) -v` probe is the one that earns its
# keep: a leftover x86_64 node on an arm64 Mac passes `command -v` and then
# fails with "env: node: Bad CPU type in executable", which names neither npm,
# nor the client, nor the architecture. Reported from a fresh macOS clone in
# issue #1, where diagnosing it took `file $(which node)` plus `uname -m`.
preflight:  ## check the toolchain before anything tries to use it
	@command -v $(PYTHON) >/dev/null 2>&1 \
		|| { echo "python3 not found. This needs Python >= 3.12."; exit 1; }
	@$(PYTHON) -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
		|| { echo "python3 is $$($(PYTHON) -V 2>&1), and this needs >= 3.12."; exit 1; }
	@command -v $(NODE) >/dev/null 2>&1 \
		|| { echo "node not found. This needs Node ^22.22.2, ^24.15 or >= 26 (CI uses 22; see .nvmrc)."; \
		     echo "The API and the tests do not need it: make install-py"; exit 1; }
	@$(NODE) -v >/dev/null 2>&1 \
		|| { echo "node is installed but will not run -- usually an architecture mismatch."; \
		     echo "  this machine : $$(uname -m)"; \
		     echo "  that node    : $$(file -b $$(command -v $(NODE)) 2>/dev/null || echo unknown)"; \
		     echo "Install a Node matching this machine, or skip it: make install-py"; exit 1; }
	@echo "toolchain ok: $$($(PYTHON) -V 2>&1), node $$($(NODE) -v) from $(NODE_BIN)"

install: install-py install-client  ## everything, Python and npm both

# Split because the halves fail independently. When npm ci fails, make exits
# non-zero and it looks like nothing worked -- while the Python side has
# finished and the API and the whole test suite are perfectly usable.
install-py:  ## the venv alone: enough for the API and the tests, no Node needed
	@command -v $(PYTHON) >/dev/null 2>&1 \
		|| { echo "python3 not found. This needs Python >= 3.12."; exit 1; }
	@$(PYTHON) -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
		|| { echo "python3 is $$($(PYTHON) -V 2>&1), and this needs >= 3.12."; exit 1; }
	$(PYTHON) -m venv .venv
	./.venv/bin/pip install -q -r requirements-dev.txt

# What a deployment installs: the runtime dependencies and nothing else. No
# pytest, no ruff, no pip-audit, no datasette -- every package in the venv is
# one more thing an advisory can be about, and none of those serve a request.
# The one visible difference is /db, the snapshot browser, which needs
# datasette and says so when it is missing; `make install-py` adds it.
#
# A lockfile with hashes is the Phase 4 release artefact, not this target.
install-prod:  ## runtime dependencies only, for a deployment (no test or lint tools)
	@command -v $(PYTHON) >/dev/null 2>&1 \
		|| { echo "python3 not found. This needs Python >= 3.12."; exit 1; }
	@$(PYTHON) -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
		|| { echo "python3 is $$($(PYTHON) -V 2>&1), and this needs >= 3.12."; exit 1; }
	$(PYTHON) -m venv .venv
	./.venv/bin/pip install -q -r requirements.txt

install-client: preflight  ## node_modules for the SPA
	cd client && npm ci

client:  ## build the client into app/static/dist
	cd client && npm run build

# app/static/dist is gitignored, so a fresh clone has no UI until this runs --
# main.py only mounts the SPA when the directory exists, and `/` 404s until then.
dev: client  ## build the client, then run the API on localhost:8848 (override PORT=)
	SPENDTRACKER_ENV=development ./.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port $(PORT)

api:  ## the API alone, without rebuilding the client
	SPENDTRACKER_ENV=development ./.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port $(PORT)

# Reachable from the other machines on this network: a phone for /snap, a
# laptop on the sofa. Three things have to change together, which is why this
# is a target and not a note in the README telling somebody to add --host.
#
#   --host 0.0.0.0    bind every interface, not just loopback
#   COOKIE_SECURE=off the browser will not store a `Secure` cookie from
#                     http://192.168.x.x, so without this the sign-in succeeds
#                     and then loops, silently. See app/config.py.
#   --reload off      the reloader watches the tree and this one is meant to be
#                     left running while people use it
#   no API docs       SPENDTRACKER_ENV=development is what mounts /api/docs,
#                     /api/redoc and /api/openapi.json, anonymously -- the whole
#                     route inventory, to everything on the network. So this
#                     runs as production unless asked: `make lan DOCS=1`.
#
# No TLS, so it is plain HTTP across the LAN: the password and the session
# cookie are readable by anything on the wire. That is a fine trade on a home
# network you control and a bad one anywhere else -- for the real deployment
# use `tailscale serve`, which terminates TLS and leaves the cookies `Secure`.
lan: client  ## build the client, then serve on this machine's LAN address (DOCS=1 for /api/docs)
	@echo "Reachable on this network at:"
	@addr=$$(ipconfig getifaddr $$(route -n get default 2>/dev/null \
		| awk '/interface:/{print $$2}') 2>/dev/null); \
	  if [ -n "$$addr" ]; then echo "    http://$$addr:$(PORT)"; \
	  else echo "    (no address on the default route -- check ifconfig)"; fi
	@if [ "$(DOCS)" = "1" ]; then \
	  echo "DOCS=1: /api/docs, /api/redoc and /api/openapi.json are open to this whole network."; fi
	@echo
	SPENDTRACKER_ENV=$(if $(filter 1,$(DOCS)),development,production) SPENDTRACKER_COOKIE_SECURE=off \
		./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

# Does not follow PORT: the proxy target is hardcoded in client/vite.config.ts
# and vite binds 5173, so this is the one mode two worktrees cannot both run.
web:  ## vite on 5173, proxying /api to 8848 (not PORT-aware)
	cd client && npm run dev

# The production run target. Three differences from `dev`, and each one is the
# point rather than a detail:
#
#   no --reload       the reloader is a development supervisor. It watches the
#                     filesystem and restarts on every write, which is not how
#                     a service stays up -- and on a machine several sessions
#                     share, an unrelated commit has killed a running instance
#                     more than once.
#   SPENDTRACKER_ENV  unset, so it defaults to production: no OpenAPI schema,
#                     no /api/docs, no /api/redoc. Those hand an
#                     unauthenticated visitor the whole route inventory.
#   127.0.0.1 kept    deliberately. `tailscale serve` proxies from the tailnet
#                     into loopback, so binding loopback is exactly what makes
#                     this unreachable except through the thing terminating
#                     TLS. Do not "fix" this to 0.0.0.0; `make lan` exists for
#                     the times you want that, and says what it costs.
serve: client  ## run it the way a deployment should: no reloader, loopback only
	./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $(PORT)

test:  ## run the whole suite with the coverage gate, then the client's
	./.venv/bin/python -m pytest
	cd client && npm test

# Separate from `test` on purpose: it builds the client, seeds a throwaway
# instance, starts a server and drives a real browser, which is a different
# order of cost from a unit run. It is also the only thing that looks at the
# phone layout, so it is not optional -- `client/playwright.config.ts` runs
# every spec twice, once at each width.
e2e:  ## end to end in a real browser, desktop and phone
	cd client && npm run e2e

lint:  ## ruff, the bulk-statement grep, and the client's typecheck
	./.venv/bin/ruff check app/ statements/ tests/ scripts/
	@./scripts/no-bulk-statements.sh
	cd client && npx tsc -b --noEmit

# One hook, and it is the fast one. The suite and the seed moved to CI, which
# runs them on a push to `main` -- see .github/workflows/tests.yml. A gate that
# costs minutes on every push to every branch is a gate that gets bypassed, and
# a hook nobody runs protects nothing.
hooks:  ## install the git hook that runs before every commit
	git config core.hooksPath .githooks
	@echo "pre-commit: ruff, audit guard, client types   (seconds)"
	@echo "skip it with --no-verify when you mean to"
	@echo "the suite and the seed now run in CI, on a push to main"

snapshot:  ## a consistent, redacted copy of the database, beside it in the data directory
	PYTHONPATH=. ./.venv/bin/python scripts/db_view.py

# --------------------------------------------------------------------------- #
# Releasing, and keeping a deployment alive across one
# --------------------------------------------------------------------------- #

# The version lives in three files and the one /api/health reports is the one
# nobody thinks to edit. `make version` with no argument prints all three;
# `make version BUMP=minor` moves them together. tests/test_version.py is what
# makes that a rule rather than a habit.
#
#   major      a code overhaul, a UI overhaul, a backend restructure, or any
#              change a downgrade cannot undo without losing rows
#   minor      a new feature, or a functional change to one that exists
#   technical  a dependency bump, a security fix, patching, a bug fix
version:  ## print the version; `make version BUMP=minor` to move it
	@./.venv/bin/python -m scripts.version $(BUMP)

# VACUUM INTO, never a file copy: WAL mode keeps recent writes in
# `spendtracker.sqlite3-wal` until a checkpoint folds them in, and in the
# previous build the main file was 4 KB while the WAL held 1.7 MB. It also
# reopens the copy and counts its rows before reporting success -- a backup
# that was never reopened is a belief.
backup:  ## a verified copy of the ledger and secret.key, under ./backups
	./.venv/bin/python -m scripts.backup

# FROM is a folder from `make backup`, a .zip downloaded from the Application
# screen, or a bare .sqlite3 from its backups directory. KEY= is the secret.key
# for one that does not carry its own; without it the key already here is kept,
# and restore checks it opens the backup's authenticator secrets either way.
restore:  ## put one back: make restore FROM=backups/20260923-024814 [KEY=path]
	@test -n "$(FROM)" || { echo "which backup? make restore FROM=backups/<stamp>"; exit 2; }
	./.venv/bin/python -m scripts.restore "$(FROM)" --port $(PORT) $(if $(KEY),--key "$(KEY)")

upgrade-check:  ## what an upgrade would do. Writes nothing.
	@./.venv/bin/python -m scripts.upgrade --check

doctor:  ## which ledger, and is it healthy. Writes nothing.
	@./.venv/bin/python -m scripts.doctor

# Backs up, reads the backup back, asks for `reset`, then moves the ledger and
# secret.key aside. Refuses while the app answers on the port.
reset:  ## start again with an empty ledger, keeping the old one aside
	@./.venv/bin/python -m scripts.reset

# Backs up, holds the port with a maintenance page, migrates, proves the result
# opens, and writes a log of all of it. It does not stop your service and does
# not `git pull`: which commit to deploy is a decision, and a script that makes
# it while also migrating a database is a script whose mistakes are two deep.
upgrade:  ## the drill: backup, placard, migrate, verify, log
	./.venv/bin/python -m scripts.upgrade --port $(PORT)

# What found the vite advisories in the 2026-09-19 review, which was done by
# hand because no automation would have said anything. Both halves run even
# when the first one finds something, because "python is clean" is not an
# answer to "is the client clean".
audit:  ## known advisories against the pinned dependencies, Python and npm
	-./.venv/bin/python -m pip_audit -r requirements.txt -r requirements-dev.txt
	-cd client && npm audit --audit-level=high

# Standalone, for poking at the file without running the app. 127.0.0.1 on
# purpose: on its own Datasette has no authentication and will happily run
# arbitrary read-only SQL for whoever reaches it. For the tailnet use the app's
# own /db instead, which is behind the session check.
# The script builds the snapshot and then serves it, rather than this target
# naming the file: the snapshot lives in the resolved data directory, and a
# path written here went stale the day that directory moved (#64).
db-view:  ## browse the database at http://127.0.0.1:8899 (loopback only)
	PYTHONPATH=. ./.venv/bin/python scripts/db_view.py --serve

seed:  ## wipe the database and build a demo household
	./.venv/bin/python -m scripts.seed_demo --reset

migrate:  ## bring the database up to head
	./.venv/bin/alembic upgrade head

revision:  ## autogenerate a migration: make revision m="what changed"
	./.venv/bin/alembic revision --autogenerate -m "$(m)"

clean:  ## remove caches
	rm -rf .pytest_cache .ruff_cache .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
