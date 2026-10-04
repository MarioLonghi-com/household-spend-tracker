#!/usr/bin/env bash
# Seed a throwaway instance and serve it, for Playwright's `webServer`.
#
# Its own port and its own data directory, so a dev server on 8848 and a real
# ledger in ./data are both left alone.
set -euo pipefail
cd "$(dirname "$0")/../.."

export SPENDTRACKER_DATA_DIR=./e2e-data
export DATABASE_URL="sqlite:///./e2e-data/e2e.sqlite3"
export SPENDTRACKER_ENV=development

# The venv locally; whatever interpreter CI installed into, there. Both go
# through `-m uvicorn` rather than the console script, so one variable covers
# the seed and the server.
PY="${PYTHON:-./.venv/bin/python}"

rm -rf ./e2e-data
PYTHONPATH=. "$PY" client/e2e/bootstrap.py

exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8850
