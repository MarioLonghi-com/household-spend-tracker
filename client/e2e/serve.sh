#!/usr/bin/env bash
# Seed a throwaway instance and serve it, for Playwright's `webServer`.
#
# Its own port and its own data directory, so a dev server on 8848 and a real
# ledger in ./data are both left alone. The port is 8850 unless E2E_PORT names
# another, so a second worktree can run one spec without taking 8850.
set -euo pipefail
cd "$(dirname "$0")/../.."

export SPENDTRACKER_DATA_DIR=./e2e-data
export DATABASE_URL="sqlite:///./e2e-data/e2e.sqlite3"
export SPENDTRACKER_ENV=development
# Passkeys work at http://localhost:8850 and nowhere else here (#121): the
# suite's own address, 127.0.0.1, is an IP and is never offered them.
export SPENDTRACKER_RP_ID=localhost
# A checkout, whatever the machine running it: the Updates section's
# `not_container` case is what the suite expects (#166). Without this a CI
# runner that happens to be a container would read as one with no updater.
export SPENDTRACKER_IN_CONTAINER=0

# The venv locally; whatever interpreter CI installed into, there. Both go
# through `-m uvicorn` rather than the console script, so one variable covers
# the seed and the server.
PY="${PYTHON:-./.venv/bin/python}"

rm -rf ./e2e-data
PYTHONPATH=. "$PY" client/e2e/bootstrap.py

exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "${E2E_PORT:-8850}"
