"""`python -m updater.launch`: what the release zip's launchers ask before `compose up` (#167).

The launchers -- `Start Spend Tracker.command`, `Start Spend Tracker.bat` and
`start-spend-tracker.sh`, in `deploy/bundle/` -- stay thin. They keep only what
has to happen on the host: finding `docker compose` or `podman compose`, the
socket's path, `podman-restart`, lingering and `chgrp`. Everything that is a
decision is here, in one dialect, with tests. Which engine to use is the one
decision taken before any container can run, so the launchers carry it
themselves, and `pick_engine` is the rule they are tested against (#264).
Then they run **the bundle's own updater image** once,

    <engine> run --rm --network none --user 0:0 --security-opt label=disable \\
        -v <socket>:/run/engine.sock -v "$PWD:/project" [-v <previous>:/previous:ro] \\
        --entrypoint python <bundle's updater, by digest> -m updater.launch \\
        --bundle-app <ref> --bundle-updater <ref> --host-dir "$PWD" --engine-socket <socket>

and this module

1. detects the engine through the socket, with the same code the updater runs
   (`detect.detect`), and looks up how the updater runs on it
   (`detect.not_root`, 8.2, S12, S18, S20, S21);
2. finds the pin (9.1): the `.env` of the install the project's containers
   were started from (`/previous`, when the zip was unzipped into a new
   folder), else this folder's; in each, `.env` first and `pin/release.env`,
   the updater's record, second;
3. applies **the launcher's rule (C5, 9.2)** in `choose()`, and reads which
   compose created the project from its containers' labels (`compose_of`,
   #247);
4. writes the result into this folder's `.env` -- the pin, when there is one,
   and the per-engine settings compose reads (`SPENDTRACKER_ENGINE_SOCKET`,
   `SPENDTRACKER_SOCKET_GID`, `SPENDTRACKER_UPDATER_USER`) -- every other line
   left as it was, so a later `docker compose up -d` typed by hand starts the
   same thing;
5. prints what the launcher still has to do on the host, one `KEY=value` per
   line (`ANSWER_KEYS`), and the sentences to show (`SAY=`).

It runs as in-container root, which reaches the socket on every engine (S18,
S21) -- and which on a rootful Linux engine is the host's root. So the `.env`
it replaces is given back to the owner the old one had.

Exit status 0: go on. 2: stop, and the `SAY=` lines say why.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from updater import detect, pin, survey
from updater import engine as eng
from updater.contract import label_version, parse_version

PROJECT = "spend-tracker"

#: What `.env` gets besides the pin. compose.yaml reads each one.
SOCKET_KEY = "SPENDTRACKER_ENGINE_SOCKET"
GID_KEY = "SPENDTRACKER_SOCKET_GID"
USER_KEY = "SPENDTRACKER_UPDATER_USER"

#: The lines a launcher reads, in the order they are printed. `SAY` repeats.
ANSWER_KEYS = ("ENGINE", "PODMAN_RESTART", "LINGER", "CHGRP", "APP", "UPDATER", "PLACARD", "COMPOSE", "SAY")

#: The two compose implementations a project can have been created by (#247).
PODMAN_COMPOSE = "podman-compose"
DOCKER_COMPOSE = "docker-compose"
#: Labels podman-compose writes and docker-compose never does. podman-compose
#: writes compose's own `com.docker.compose.*` as well, so those say nothing.
PODMAN_COMPOSE_LABELS = "io.podman.compose."

#: Detections the launcher stops on: nothing about the engine can be decided.
#: `too_old` and `outdated` are said and passed: the app runs, and the
#: updater's own screen explains what it will not do.
STOPS = frozenset(
    {
        "unknown_engine",
        "windows_containers",
        "unreachable",
        "permission_denied",
        "eci_blocked",
        "tcp_socket",
    }
)

_REF = re.compile(
    r"(?P<repo>[^@:]+(?::[0-9]+)?(?:/[^@:]+)*)(?::(?P<tag>[^@]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
)


# --------------------------------------------------------------------------- #
# Which engine (#264)
# --------------------------------------------------------------------------- #

#: What a launcher finds of one engine on the host, worst to best:
#: `none` -- no CLI with `compose`; `installed` -- `<engine> info` fails;
#: `denied` -- it fails with "permission denied"; `answers` -- `info`
#: works; `project` -- and `ps -aq --filter
#: label=com.docker.compose.project=spend-tracker` lists something.
ENGINE_STATES = ("none", "installed", "denied", "answers", "project")
#: The order a tie is settled in.
ENGINES = (("docker", "Docker"), ("podman", "Podman"))

NOT_FOUND = (
    "Spend Tracker runs in Docker Desktop or Podman Desktop, and neither was found. "
    "Install one, start it, then open this launcher again."
)
NOT_RUNNING = (
    "{product} is installed but not running. Start it, wait until it says it is running, "
    "then open this launcher again."
)
NEITHER_RUNNING = (
    "Docker and Podman are both installed, and neither is running. Start the one Spend Tracker "
    "uses, wait until it says it is running, then open this launcher again."
)
DENIED = (
    "Your user cannot reach Docker's socket. Add it to the docker group "
    "(sudo usermod -aG docker {user}), log out and back in, then run this again."
)
IN_BOTH = "Spend Tracker is installed in both Docker and Podman; this starts the one in Docker."
UNCHECKED = (
    "{other} is installed but not running, so whether Spend Tracker is already installed there "
    "was not checked; this starts it in {chosen}."
)


@dataclass(frozen=True)
class EnginePick:
    """The engine a launcher uses, or the sentence it stops with; and what it says on the way."""

    engine: str | None
    product: str | None
    says: tuple[str, ...] = ()
    stop: str | None = None


def pick_engine(docker: str, podman: str, user: str = "$(id -un)") -> EnginePick:
    """The launchers' engine rule (#264), on what each engine's CLI answered.

    1. An engine that already holds the project -- its containers carry
       `com.docker.compose.project=spend-tracker` -- is the one; Docker if
       both do.
    2. Otherwise an engine that answers `info`, Docker first.
    3. Neither answers: say which are installed, and that one must be started.

    `docker compose version` reads only the client, so a Docker CLI on the
    PATH -- Docker Desktop installed but stopped, or Homebrew's `docker` --
    used to win over a Podman machine that was running.

    `start-spend-tracker.sh` and `Start Spend Tracker.bat` carry this rule
    in their own dialects; `tests/test_launcher_engine.py` runs the first
    against it, state by state, and holds the second to its sentences.
    """
    states = {"docker": docker, "podman": podman}
    for name, state in states.items():
        if state not in ENGINE_STATES:
            raise ValueError(f"{name}: {state!r}")
    product = dict(ENGINES)
    other = {"docker": "podman", "podman": "docker"}

    for wanted in ("project", "answers"):
        for name, _ in ENGINES:
            if states[name] != wanted:
                continue
            says: list[str] = []
            if wanted == "project" and states[other[name]] == "project":
                says.append(IN_BOTH)
            if wanted == "answers" and states[other[name]] in ("installed", "denied"):
                says.append(UNCHECKED.format(other=product[other[name]], chosen=product[name]))
            return EnginePick(name, product[name], tuple(says))

    if docker == "denied":
        return EnginePick(None, None, stop=DENIED.format(user=user))
    present = [name for name, _ in ENGINES if states[name] != "none"]
    if len(present) == 2:
        return EnginePick(None, None, stop=NEITHER_RUNNING)
    if present:
        return EnginePick(None, None, stop=NOT_RUNNING.format(product=product[present[0]]))
    return EnginePick(None, None, stop=NOT_FOUND)


# --------------------------------------------------------------------------- #
# The rule (C5)
# --------------------------------------------------------------------------- #


def version_of(ref: str | None) -> tuple[int, int, int] | None:
    """The release a `repo:X.Y.Z@sha256:…` reference names, or None if its tag is no release."""
    m = _REF.fullmatch(ref or "")
    tag = label_version(m.group("tag")) if m else None
    return parse_version(tag) if tag else None


def _text(v: tuple[int, int, int] | None) -> str:
    return ".".join(map(str, v)) if v else "an unknown release"


@dataclass(frozen=True)
class Choice:
    """What compose is to start. `None` leaves compose's default: the bundle's own reference."""

    app: str | None
    updater: str | None
    sentences: tuple[str, ...] = ()

    @property
    def pinned(self) -> bool:
        return self.app is not None or self.updater is not None


def choose(found: Mapping[str, str], bundle_app: str, bundle_updater: str) -> Choice:
    """The launcher's rule (C5, 9.2), on the pin as found and the bundle's two references.

    - **No pin:** the bundle's images -- a first install.
    - **A pinned app:** the pin's, whatever the bundle ships. The ledger is at
      the pin's version; the bundle's app would be refused by `schema_check`,
      and moving it on is the browser's job, with a backup first.
    - **The updater:** the newer of the pin's and the bundle's. Equal, or
      either unreadable, keeps the pin's: never a downgrade, and never a guess.
    """
    pinned_app = found.get(pin.APP_KEY) or None
    pinned_updater = found.get(pin.UPDATER_KEY) or None
    if not pinned_app and not pinned_updater:
        return Choice(None, None)

    said: list[str] = []
    app = pinned_app
    if app and app != bundle_app:
        said.append(
            f"Your ledger is at {_text(version_of(app))}, so that release starts, not the "
            f"{_text(version_of(bundle_app))} in this download. Updates are made in the browser, "
            "under Application."
        )

    updater = pinned_updater or bundle_updater
    if pinned_updater and pinned_updater != bundle_updater:
        mine, theirs = version_of(pinned_updater), version_of(bundle_updater)
        if mine is not None and theirs is not None and theirs > mine:
            updater = bundle_updater
            said.append(f"The updater is replaced: {_text(mine)} by {_text(theirs)} from this download.")
    return Choice(app, updater, tuple(said))


# --------------------------------------------------------------------------- #
# Which compose (#247)
# --------------------------------------------------------------------------- #


def compose_of(listing: list[dict]) -> str | None:
    """The compose implementation that created the project, from its containers' labels.

    `podman compose` hands the work to docker-compose whenever that is
    installed, and docker-compose refuses a stack podman-compose created (its
    network lacks compose's labels). So the launcher must run the one that made
    the project: podman-compose when the project's containers carry
    `io.podman.compose.*` labels, docker-compose otherwise. None when the
    project has no container yet -- a first install -- and the engine's own
    choice stands.

    The app's container decides, then the updater's, then any other the
    updater did not create; the updater's copies keep the labels they copy.
    """
    def rank(c: dict) -> int:
        return {"app": 0, "updater": 1}.get(survey.service_of(c.get("Labels")) or "", 2)

    made = sorted((c for c in listing if not survey.is_oneoff(c)), key=rank)
    if not made:
        return None
    labels = made[0].get("Labels") if isinstance(made[0].get("Labels"), dict) else {}
    return PODMAN_COMPOSE if any(str(k).startswith(PODMAN_COMPOSE_LABELS) for k in labels) else DOCKER_COMPOSE


def project_compose(client: eng.EngineClient) -> str | None:
    """`compose_of` the project's containers, or None when the engine will not list them."""
    try:
        if client.negotiated is None:
            client.negotiate()
        return compose_of(client.containers())
    except (eng.EngineError, eng.EngineUnavailable, eng.NotAllowed, OSError):
        return None


# --------------------------------------------------------------------------- #
# Reading the pin
# --------------------------------------------------------------------------- #


def read_keys(path: Path, keys: tuple[str, ...] = pin.KEYS) -> dict[str, str]:
    """`keys` as an env file sets them, the first assignment of each. Missing file: nothing."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, sep, value = stripped.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if sep and key in keys and key not in out and value:
            out[key] = value
    return out


def find_pin(project: Path, previous: Path | None = None) -> dict[str, str]:
    """The pin, from the first place that holds one (module docstring, step 2).

    `previous` is the folder the project's containers were started from, when
    that is not this one: there the updater has been writing, so it is newer
    than anything copied here. A place counts when it pins the app; a record
    without the app (it is always written with it) is passed over.
    """
    places = []
    if previous is not None:
        places += [previous / ".env", previous / "pin" / "release.env"]
    places += [project / ".env", project / "pin" / "release.env"]
    for place in places:
        keys = read_keys(place)
        if keys.get(pin.APP_KEY):
            return keys
    return {}


# --------------------------------------------------------------------------- #
# The per-engine settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Settings:
    engine: str
    #: `.env` lines compose reads.
    env: dict[str, str]
    podman_restart: str | None
    linger: bool
    #: The gid the project directory and `.env` must be writable by (R34), or None.
    chgrp: str | None


def settings_for(found: detect.Detection, engine_socket: str, socket_gid: str | None) -> Settings:
    """How the updater runs on this engine, as `.env` lines and host steps.

    `socket_gid` is the socket's group as the host sees it (`stat -c %g`,
    S12), wanted only where `detect.not_root` names the host's own group.
    """
    assert found.engine is not None
    rule = detect.not_root(found.engine, found.rootless)
    host_gid = detect.SOCKET_GID in rule.group_add
    if host_gid and not (socket_gid or "").isdigit():
        raise ValueError("the socket's group is needed on this engine and was not given")
    gid = socket_gid if host_gid else "0"
    # R34: the updater renames `.env` in the project directory. Under a rootful
    # engine on Linux it is uid 65532 carrying the socket's group, and the
    # directory is the user's; rootless engines run it as the user, and Desktop
    # file sharing lets anyone write.
    rootful_linux = found.engine in ("docker-engine", "podman") and not found.rootless
    return Settings(
        engine=found.engine,
        env={SOCKET_KEY: engine_socket, GID_KEY: str(gid), USER_KEY: rule.user},
        podman_restart=rule.podman_restart,
        linger=rule.linger,
        chgrp=str(gid) if rootful_linux else None,
    )


# --------------------------------------------------------------------------- #
# Writing `.env`
# --------------------------------------------------------------------------- #


def write_env(project: Path, values: dict[str, str]) -> None:
    """`values` into `project/.env` (pin.set_env), handed back to the old file's owner."""
    env = project / ".env"
    try:
        before = os.lstat(env)
        owner = (before.st_uid, before.st_gid)
    except FileNotFoundError:
        st = os.stat(project)
        owner = (st.st_uid, st.st_gid)
    pin.set_env(project, values)
    if os.geteuid() == 0 and owner != (0, 0):
        # Desktop file sharing maps every owner to the host user anyway.
        with contextlib.suppress(OSError):
            os.chown(env, *owner)


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #


def answer(lines: list[tuple[str, str]]) -> str:
    for key, value in lines:
        if key not in ANSWER_KEYS:
            raise ValueError(key)
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} carries a line break")
    return "".join(f"{k}={v}\n" for k, v in lines)


def run(args: argparse.Namespace, detection: detect.Detection | None = None) -> tuple[int, str]:
    """Steps 1-5 of the module docstring. Returns the exit status and what to print."""
    client = eng.EngineClient(args.socket, eng.Scope(project=PROJECT))
    if detection is None:
        refused = detect.check_socket_target(args.engine_socket)
        detection = refused or detect.detect(client, args.host_dir)
    if detection.engine is None or detection.socket in STOPS:
        return 2, answer([("SAY", detection.sentence or "The container engine could not be identified.")])

    try:
        settings = settings_for(detection, args.engine_socket, args.socket_gid)
    except (KeyError, ValueError) as e:
        return 2, answer([("SAY", f"This container engine cannot run Spend Tracker's updater: {e}.")])

    project = Path(args.project)
    previous = Path(args.previous) if args.previous and Path(args.previous).is_dir() else None
    choice = choose(find_pin(project, previous), args.bundle_app, args.bundle_updater)

    values = dict(settings.env)
    if choice.app:
        values[pin.APP_KEY] = choice.app
    if choice.updater:
        values[pin.UPDATER_KEY] = choice.updater
    write_env(project, values)

    lines = [
        ("ENGINE", settings.engine),
        ("PODMAN_RESTART", settings.podman_restart or ""),
        ("LINGER", "1" if settings.linger else "0"),
        ("CHGRP", settings.chgrp or ""),
        ("APP", choice.app or args.bundle_app),
        ("UPDATER", choice.updater or args.bundle_updater),
        # The label filter that finds the updater's maintenance page (R30),
        # spelt where the updater spells it rather than in three launchers.
        ("PLACARD", f"{eng.ROLE_LABEL}=placard"),
        ("COMPOSE", project_compose(client) or ""),
    ]
    if detection.sentence:
        lines.append(("SAY", detection.sentence))
    lines += [("SAY", s) for s in choice.sentences]
    return 0, answer(lines)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m updater.launch", description=__doc__.splitlines()[0])
    p.add_argument("--bundle-app", required=True, help="the bundle's app image, by digest")
    p.add_argument("--bundle-updater", required=True, help="the bundle's updater image, by digest")
    p.add_argument("--host-dir", required=True, help="this folder as the host names it")
    p.add_argument(
        "--engine-socket", required=True, help="the socket compose is to mount, as the engine names it"
    )
    p.add_argument("--socket-gid", default=None, help="the socket's group on the host (Linux)")
    p.add_argument("--socket", default="/run/engine.sock")
    p.add_argument("--project", default="/project")
    p.add_argument("--previous", default=None, help="the folder the project was started from, if another")
    return p


def main(argv: list[str] | None = None) -> int:
    status, out = run(parser().parse_args(argv))
    sys.stdout.write(out)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
