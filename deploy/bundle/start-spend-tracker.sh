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
# What it does, in order: finds `docker compose` or `podman compose`; finds the
# engine's socket; asks the bundle's own updater image what to start
# (updater/launch.py: the engine, the pin, the settings, written into .env);
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
PLACARD_LABEL='com.github.mariolonghi-com.spend-tracker.updater-role=placard'

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

ENGINE=""
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ENGINE=docker
  PRODUCT="Docker"
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
  ENGINE=podman
  PRODUCT="Podman"
else
  stop "Spend Tracker runs in Docker Desktop or Podman Desktop, and neither was found. Install one, start it, then open this launcher again."
fi

if ! problem="$("$ENGINE" info 2>&1 >/dev/null)"; then
  case "$problem" in
    *"permission denied"*)
      stop "Your user cannot reach $PRODUCT's socket. Add it to the docker group (sudo usermod -aG docker $(id -un)), log out and back in, then run this again." ;;
    *)
      stop "$PRODUCT is installed but not running. Start it, wait until it says it is running, then open this launcher again." ;;
  esac
fi

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

KIND="" PODMAN_RESTART="" LINGER="0" CHGRP="" APP="" UPDATER=""
while IFS= read -r line; do
  case "$line" in
    ENGINE=*) KIND="${line#ENGINE=}" ;;
    PODMAN_RESTART=*) PODMAN_RESTART="${line#PODMAN_RESTART=}" ;;
    LINGER=*) LINGER="${line#LINGER=}" ;;
    CHGRP=*) CHGRP="${line#CHGRP=}" ;;
    APP=*) APP="${line#APP=}" ;;
    UPDATER=*) UPDATER="${line#UPDATER=}" ;;
    SAY=*) say "${line#SAY=}" ;;
  esac
done <<EOF
$answer
EOF

if [ "$status" -ne 0 ] || [ -z "$KIND" ]; then
  if [ -z "$answer" ]; then
    stop "The updater image could not be run. Check the internet connection, then open this launcher again."
  fi
  stop "Spend Tracker was not started."
fi
say "Engine: $KIND. Starting ${APP##*/} with ${UPDATER##*/}."

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
  for id in $("$ENGINE" ps -aq --filter "label=$key=$PROJECT" --filter "label=com.docker.compose.oneoff=True" --filter "label=$PLACARD_LABEL" 2>/dev/null); do
    "$ENGINE" rm -f "$id" >/dev/null 2>&1
  done
done

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
