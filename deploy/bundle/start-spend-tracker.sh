#!/usr/bin/env bash
# Start Spend Tracker @VERSION@ on this computer: macOS and Linux.
#
# The release zip ships this file twice: as "Start Spend Tracker.command",
# which macOS opens in Terminal on a double-click, and as
# start-spend-tracker.sh for Linux. Double-click it, or run it from this
# folder. It is safe to run again at any time: it is also how Spend Tracker is
# started after a reinstall of Docker or Podman, and how an install whose
# updater no longer works is repaired -- a newer zip's launcher keeps your
# release and replaces only the updater, when its own is newer (C5).
#
# What it does, in order: picks Docker or Podman -- the one Spend Tracker is
# already in, else the one that is running; finds the engine's socket; asks
# the bundle's own updater image what to start
# (updater/launch.py: the engine, the pin, the settings, written into .env,
# and which compose created the project, so the same one runs it again);
# makes sure the containers come back after a restart (podman-restart,
# lingering); on a rootful Linux engine, lets the updater write this folder;
# removes a leftover maintenance page; starts the project; waits for it to
# answer; opens it in the browser. It never uses sudo: when a step needs it,
# it says which command to run and stops.
#
# Built from deploy/bundle/start-spend-tracker.sh by scripts/bundle.py.

set -u

APP_IMAGE='@APP_IMAGE@'
UPDATER_IMAGE='@UPDATER_IMAGE@'
PROJECT='spend-tracker'
URL='http://localhost:8848'
PROBE='http://127.0.0.1:8848/api/health'
HEALTH_TIMEOUT="${SPENDTRACKER_HEALTH_TIMEOUT:-180}"

say() { printf '%s\n' "$*"; }

stop() {
  printf '\n%s\n' "$*" >&2
  if [ -t 0 ]; then
    printf '\nPress Return to close this window.' >&2
    read -r _
  fi
  exit 1
}

# Env-file paths are relative to where compose runs (S1): this folder, always.
cd "$(dirname "$0")" || stop "This launcher cannot open its own folder."
HERE="$(pwd -P)"

OS="$(uname -s)"
if [ "$OS" = Darwin ]; then
  # A double-clicked .command gets a login shell's PATH, which may not reach
  # the CLIs Docker Desktop and Podman Desktop install.
  PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:/opt/podman/bin"
  export PATH
fi

say "Starting Spend Tracker @VERSION@ from $HERE"

# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #

# The rule is updater/launch.py's `pick_engine` (#264), and
# tests/test_launcher_engine.py runs this block against it: an engine that
# already holds the project, else one that answers, Docker first in a tie.
# `docker compose version` reads only the client, so a Docker CLI alone
# says nothing about whether Docker runs.
seen() {
  if ! command -v "$1" >/dev/null 2>&1 || ! "$1" compose version >/dev/null 2>&1; then
    echo none
    return
  fi
  if ! problem="$("$1" info 2>&1 >/dev/null)"; then
    case "$problem" in
      *"permission denied"*) echo denied ;;
      *) echo installed ;;
    esac
    return
  fi
  if [ -n "$("$1" ps -aq --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null)" ]; then
    echo project
  else
    echo answers
  fi
}
DOCKER_SEEN="$(seen docker)"
PODMAN_SEEN="$(seen podman)"

ENGINE=""
if [ "$DOCKER_SEEN" = project ]; then
  ENGINE=docker
  [ "$PODMAN_SEEN" = project ] && say "Spend Tracker is installed in both Docker and Podman; this starts the one in Docker."
elif [ "$PODMAN_SEEN" = project ]; then
  ENGINE=podman
elif [ "$DOCKER_SEEN" = answers ]; then
  ENGINE=docker
  case "$PODMAN_SEEN" in installed|denied)
    say "Podman is installed but not running, so whether Spend Tracker is already installed there was not checked; this starts it in Docker." ;;
  esac
elif [ "$PODMAN_SEEN" = answers ]; then
  ENGINE=podman
  case "$DOCKER_SEEN" in installed|denied)
    say "Docker is installed but not running, so whether Spend Tracker is already installed there was not checked; this starts it in Podman." ;;
  esac
elif [ "$DOCKER_SEEN" = denied ]; then
  stop "Your user cannot reach Docker's socket. Add it to the docker group (sudo usermod -aG docker $(id -un)), log out and back in, then run this again."
elif [ "$DOCKER_SEEN" != none ] && [ "$PODMAN_SEEN" != none ]; then
  stop "Docker and Podman are both installed, and neither is running. Start the one Spend Tracker uses, wait until it says it is running, then open this launcher again."
elif [ "$DOCKER_SEEN" != none ]; then
  stop "Docker is installed but not running. Start it, wait until it says it is running, then open this launcher again."
elif [ "$PODMAN_SEEN" != none ]; then
  stop "Podman is installed but not running. Start it, wait until it says it is running, then open this launcher again."
else
  stop "Spend Tracker runs in Docker Desktop or Podman Desktop, and neither was found. Install one, start it, then open this launcher again."
fi
if [ "$ENGINE" = docker ]; then PRODUCT="Docker"; else PRODUCT="Podman"; fi

# The socket the updater is given. In Docker Desktop and in a podman machine
# the path is the VM's, and /var/run/docker.sock is right for both (S21).
SOCK=/var/run/docker.sock
GID=""
if [ "$OS" = Linux ] && [ "$("$ENGINE" info --format '{{.OperatingSystem}}' 2>/dev/null)" != "Docker Desktop" ]; then
  if [ "$ENGINE" = podman ]; then
    SOCK="$(podman info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null)"
    SOCK="${SOCK#unix://}"
    SOCK="${SOCK#unix:}"
    if [ -n "$SOCK" ] && [ ! -S "$SOCK" ]; then
      # Podman serves its API on a socket only when the unit is on.
      if [ "$(id -u)" = 0 ]; then
        systemctl enable --now podman.socket >/dev/null 2>&1
      else
        systemctl --user enable --now podman.socket >/dev/null 2>&1
      fi
    fi
  else
    endpoint="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)}"
    case "$endpoint" in
      unix://*) SOCK="${endpoint#unix://}" ;;
      "") ;;
      *) stop "The updater talks to the container engine over a unix socket only, and this Docker is set to $endpoint." ;;
    esac
  fi
  [ -S "$SOCK" ] || stop "$PRODUCT's socket was not found at $SOCK. Start $PRODUCT's service, then run this again."
  # The socket's group (S12): `docker` under rootful Docker Engine, not 0.
  GID="$(stat -c %g "$SOCK")"
fi

# --------------------------------------------------------------------------- #
# The folder this project was started from, if not this one: its .env holds
# the pin when a newer zip was unzipped next to the old one.
# --------------------------------------------------------------------------- #

PREVIOUS=""
for id in $("$ENGINE" ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=updater" 2>/dev/null) \
          $("$ENGINE" ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=app" 2>/dev/null); do
  dir="$("$ENGINE" inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id" 2>/dev/null)"
  if [ -n "$dir" ] && [ -d "$dir" ]; then
    if [ "$(cd "$dir" && pwd -P)" != "$HERE" ]; then
      PREVIOUS="$dir"
      say "Spend Tracker was last started from $PREVIOUS; its settings are carried over."
    fi
    break
  fi
done

# --------------------------------------------------------------------------- #
# What to start: asked of the bundle's own updater (updater/launch.py)
# --------------------------------------------------------------------------- #

set -- run --rm --network none --user 0:0 --security-opt label=disable \
  -v "$SOCK:/run/engine.sock" -v "$HERE:/project"
[ -n "$PREVIOUS" ] && set -- "$@" -v "$PREVIOUS:/previous:ro"
set -- "$@" --entrypoint python "$UPDATER_IMAGE" -m updater.launch \
  --bundle-app "$APP_IMAGE" --bundle-updater "$UPDATER_IMAGE" \
  --host-dir "$HERE" --engine-socket "$SOCK"
[ -n "$GID" ] && set -- "$@" --socket-gid "$GID"
[ -n "$PREVIOUS" ] && set -- "$@" --previous /previous

say "Checking $PRODUCT and this folder..."
answer="$("$ENGINE" "$@")"
status=$?

KIND="" PODMAN_RESTART="" LINGER="0" CHGRP="" APP="" UPDATER="" PLACARD="" MADE_BY=""
while IFS= read -r line; do
  case "$line" in
    ENGINE=*) KIND="${line#ENGINE=}" ;;
    PODMAN_RESTART=*) PODMAN_RESTART="${line#PODMAN_RESTART=}" ;;
    LINGER=*) LINGER="${line#LINGER=}" ;;
    CHGRP=*) CHGRP="${line#CHGRP=}" ;;
    APP=*) APP="${line#APP=}" ;;
    UPDATER=*) UPDATER="${line#UPDATER=}" ;;
    PLACARD=*) PLACARD="${line#PLACARD=}" ;;
    COMPOSE=*) MADE_BY="${line#COMPOSE=}" ;;
    SAY=*) say "${line#SAY=}" ;;
  esac
done <<EOF
$answer
EOF

if [ "$status" -ne 0 ] || [ -z "$KIND" ] || [ -z "$PLACARD" ]; then
  if [ -z "$answer" ]; then
    stop "The updater image could not be run. Check the internet connection, then open this launcher again."
  fi
  stop "Spend Tracker was not started."
fi
say "Engine: $KIND. Starting ${APP##*/} with ${UPDATER##*/}."

# --------------------------------------------------------------------------- #
# Which compose (#247): the one that created the project, which
# updater/launch.py reads from its containers' labels. `podman compose` hands
# the work to docker-compose whenever that is installed, and docker-compose
# refuses a stack podman-compose made; Podman takes the provider it runs from
# PODMAN_COMPOSE_PROVIDER. Nothing made yet (a first install): its own choice.
# --------------------------------------------------------------------------- #

case "$MADE_BY" in
  podman-compose)
    provider="$(command -v podman-compose 2>/dev/null)" \
      || stop "This Spend Tracker was created with podman-compose, which was not found. Install it (your system's podman-compose package, or pip install podman-compose), then open this launcher again."
    PODMAN_COMPOSE_PROVIDER="$provider"
    export PODMAN_COMPOSE_PROVIDER
    say "Using podman-compose, which created this Spend Tracker."
    ;;
  docker-compose)
    if [ "$ENGINE" = podman ] && provider="$(command -v docker-compose 2>/dev/null)"; then
      PODMAN_COMPOSE_PROVIDER="$provider"
      export PODMAN_COMPOSE_PROVIDER
      say "Using docker-compose, which created this Spend Tracker."
    fi
    ;;
esac

# --------------------------------------------------------------------------- #
# Coming back after a restart (S2, S20, S21)
# --------------------------------------------------------------------------- #

if [ -n "$PODMAN_RESTART" ]; then
  if [ "$OS" = Darwin ]; then
    command -v podman >/dev/null 2>&1 \
      || stop "Podman's command-line tool was not found, so podman-restart cannot be enabled in its machine. Reinstall Podman Desktop with it, then open this launcher again."
    machine="$(podman machine list --noheading --format '{{.Name}} {{.Running}}' 2>/dev/null | awk '$2 == "true" { print $1; exit }')"
    machine="${machine%\*}"
    [ -n "$machine" ] || stop "No running Podman machine was found. Start it in Podman Desktop, then open this launcher again."
    if [ "$PODMAN_RESTART" = system ]; then
      podman machine ssh "$machine" sudo systemctl enable podman-restart.service >/dev/null 2>&1
    else
      podman machine ssh "$machine" systemctl --user enable podman-restart.service >/dev/null 2>&1
    fi || stop "podman-restart could not be enabled in the machine $machine, so Spend Tracker would not come back after a restart."
  elif [ "$PODMAN_RESTART" = system ]; then
    if ! systemctl is-enabled --quiet podman-restart.service 2>/dev/null; then
      if [ "$(id -u)" = 0 ]; then
        systemctl enable podman-restart.service >/dev/null 2>&1 || stop "podman-restart.service could not be enabled."
      else
        stop "Spend Tracker needs podman-restart.service to come back after a reboot. Run: sudo systemctl enable podman-restart.service -- then run this again."
      fi
    fi
  else
    systemctl --user enable podman-restart.service >/dev/null 2>&1 \
      || stop "podman-restart.service could not be enabled for your user, so Spend Tracker would not come back after a reboot."
  fi
fi

if [ "$LINGER" = 1 ] && [ "$OS" = Linux ]; then
  me="$(id -un)"
  if [ "$(loginctl show-user "$me" --property=Linger --value 2>/dev/null)" != yes ]; then
    loginctl enable-linger "$me" >/dev/null 2>&1 \
      || stop "A rootless engine starts your containers at boot only if your user lingers. Run: sudo loginctl enable-linger $me -- then run this again."
  fi
fi

# R34: under a rootful engine on Linux the updater is uid 65532 carrying the
# socket's group, and it renames .env in this folder and writes pin/.
if [ -n "$CHGRP" ] && [ "$OS" = Linux ]; then
  targets=". .env pin"
  [ -e pin/release.env ] && targets="$targets pin/release.env"
  # shellcheck disable=SC2086 # the list is ours, without spaces
  { chgrp "$CHGRP" $targets && chmod g+w $targets && chmod g+s . pin; } 2>/dev/null \
    || stop "The updater needs to write this folder. Run: sudo chgrp $CHGRP $targets && sudo chmod g+w $targets -- then run this again."
fi

# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #

# The updater's "ahead" page holds the app's port when compose once started an
# older release than the pin (9.2, R30). It goes once the right one runs, but
# `up` would collide with it first.
for key in com.docker.compose.project io.podman.compose.project; do
  for id in $("$ENGINE" ps -aq --filter "label=$key=$PROJECT" --filter "label=com.docker.compose.oneoff=True" --filter "label=$PLACARD" 2>/dev/null); do
    "$ENGINE" rm -f "$id" >/dev/null 2>&1
  done
done

# A handover's standby: the previous updater watches its successor for ten
# minutes after an update and takes back over if the successor goes (6.6, H6).
# This launcher replacing the updater is not the successor failing, and the
# standby took back in the gap and left two updaters running (#169, E15 on
# rootless Podman). So a running `-previous` updater is stopped first.
for id in $("$ENGINE" ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=updater" 2>/dev/null); do
  case "$("$ENGINE" inspect --format '{{.Name}}' "$id" 2>/dev/null)" in
    *-previous) "$ENGINE" stop "$id" >/dev/null 2>&1 ;;
  esac
done

# podman-compose does not replace a running container whose configuration
# changed: `up` tries to remove it without force, fails, prints the error and
# still exits 0, leaving the old updater running (#169, E15 on rootless
# Podman). So under Podman the project's app and updater are stopped and
# removed first. The ledger and the update volume are named volumes and stay;
# a parked -previous or a -next is left as it is.
if [ "$ENGINE" = podman ]; then
  for svc in updater app; do
    for id in $("$ENGINE" ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$svc" 2>/dev/null); do
      name="$("$ENGINE" inspect --format '{{.Name}}' "$id" 2>/dev/null)"
      case "$name" in *-previous|*-next) continue ;; esac
      [ "$("$ENGINE" inspect --format '{{index .Config.Labels "com.docker.compose.oneoff"}}' "$id" 2>/dev/null)" = True ] && continue
      "$ENGINE" stop "$id" >/dev/null 2>&1
      "$ENGINE" rm "$id" >/dev/null 2>&1
    done
  done
fi

"$ENGINE" compose --env-file .env up -d \
  || stop "$PRODUCT could not start Spend Tracker; the lines above say why."

say "Waiting for Spend Tracker to answer..."
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while :; do
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null --max-time 5 "$PROBE" 2>/dev/null && break
  else
    wget -q -O /dev/null -T 5 "$PROBE" 2>/dev/null && break
  fi
  [ "$(date +%s)" -lt "$deadline" ] \
    || stop "Spend Tracker has not answered after $HEALTH_TIMEOUT seconds. In this folder, '$ENGINE compose logs app' shows why."
  sleep 2
done

say "Spend Tracker is running at $URL"
if [ "$OS" = Darwin ]; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 || say "Open $URL in your browser."
else
  say "Open $URL in your browser."
fi
