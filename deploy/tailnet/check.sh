#!/usr/bin/env bash
# Is the sidecar deployment healthy? Run on the server, from anywhere:
#
#   deploy/tailnet/check.sh
#
# Reads; changes nothing. Prints `ok`, `WARN` or `FAIL` per check and exits 1
# if anything failed. The questions are the ones deploy/DOCKER.md section 3
# asks one command at a time: is .env filled in and private, are both
# containers up, does the node's tailnet name match SPENDTRACKER_PUBLIC_URL,
# is the version running the one .env names, and is exactly one app on the
# ledger volume. Ends with scripts.doctor, which asks the ledger itself.
#
# Never prints .env or the auth key.

set -u
cd "$(dirname "$0")" || exit 2

failed=0
ok()   { printf '  ok    %-16s%s\n' "$1" "$2"; }
warn() { printf '  WARN  %-16s%s\n' "$1" "$2"; }
fail() { printf '  FAIL  %-16s%s\n' "$1" "$2"; failed=$((failed + 1)); }

# A value from .env, without sourcing it: sourcing runs whatever is in it.
setting() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }

echo "Spend Tracker sidecar check, $(pwd)"

if ! docker compose version >/dev/null 2>&1; then
    fail "docker" "docker compose v2 is not available to this user"
    exit 1
fi

if [ ! -f .env ]; then
    fail ".env" "missing: cp .env.example .env && chmod 600 .env, then fill it in"
    exit 1
fi
mode=$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env)
if [ "$mode" = "600" ]; then ok ".env" "present, mode 600"; else fail ".env" "mode $mode: chmod 600 .env (it holds the auth key)"; fi
if grep -q 'REPLACE-ME' .env; then fail ".env" "still has a REPLACE-ME placeholder"; fi

public=$(setting SPENDTRACKER_PUBLIC_URL)
wanted=$(setting SPENDTRACKER_VERSION)

for service in tailscale app; do
    id=$(docker compose ps -q "$service" 2>/dev/null)
    if [ -z "$id" ]; then
        fail "$service" "not running: docker compose up -d"
        continue
    fi
    state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id")
    case "$state" in
        "running healthy" | "running ") ok "$service" "$state" ;;
        "running starting") warn "$service" "running, health check still starting" ;;
        *) fail "$service" "$state: docker compose logs $service" ;;
    esac
done

health=$(docker compose exec -T app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8848/api/health', timeout=4).read().decode())" 2>/dev/null)
running=$(printf '%s' "$health" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')
if [ -z "$running" ]; then
    fail "health" "the app does not answer on 127.0.0.1:8848 inside its network"
elif [ -n "$wanted" ] && [ "$running" != "$wanted" ]; then
    warn "version" "running $running, but .env names $wanted: build the checked-out tag"
else
    ok "version" "$running"
fi
case "$health" in *'"setup_required":true'* | *'"setup_required": true'*)
    warn "setup" "not finished: the one-time token is in docker compose logs app" ;;
esac

name=$(docker compose exec -T tailscale tailscale status --json --peers=false 2>/dev/null \
    | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -1)
name=${name%.}
host=${public#https://}
host=${host%%/*}
if [ -z "$name" ]; then
    fail "tailnet" "the sidecar has no tailnet name: docker compose logs tailscale"
elif [ "$name" != "$host" ]; then
    fail "tailnet" "the node is $name but SPENDTRACKER_PUBLIC_URL says $host (deploy/TROUBLESHOOTING.md, spend-tracker-1)"
else
    ok "tailnet" "$name"
fi

if docker compose exec -T tailscale tailscale serve status 2>/dev/null | grep -q '127.0.0.1:8848'; then
    ok "serve" "https on 443 to 127.0.0.1:8848"
else
    fail "serve" "no serve config proxying to 127.0.0.1:8848: check serve.json and TS_SERVE_CONFIG"
fi

apps=$(docker ps --filter volume=spend-tracker_ledger --format '{{.Names}}' | grep -c .)
if [ "$apps" -eq 1 ]; then
    ok "ledger volume" "one container on spend-tracker_ledger"
else
    fail "ledger volume" "$apps containers on spend-tracker_ledger; one app per ledger (deploy/TROUBLESHOOTING.md)"
fi

echo
echo "The ledger itself:"
# `run` with ./backups mounted rather than `exec`, so the doctor sees the
# backups on this server and not only the ones inside the volume. Mounted only
# when it exists: Docker creates a missing bind source as root.
mount=()
if [ -d backups ]; then mount=(-v "$PWD/backups:/backups:ro"); fi
if ! docker compose run --rm -T ${mount[@]+"${mount[@]}"} --entrypoint python app \
        -m scripts.doctor --backups /backups; then
    failed=$((failed + 1))
fi

echo
if [ "$failed" -gt 0 ]; then
    echo "$failed check(s) failed."
    exit 1
fi
echo "Nothing failed."
