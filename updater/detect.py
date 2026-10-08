"""Which engine this is, what it refuses, and how the updater runs on it (design notes 8.1-8.3).

Detection runs at start and every five minutes (`DETECT_EVERY_SECONDS`) from
three things: `GET /version`, `GET /info`, and what the updater knows about
its own socket and its own container. It makes no call outside the detection
set of `engine.DETECTION_ONLY`, except listing and inspecting the updater's
own project to find its own container.

**The rules (S11).**

- *Podman* is `Components[0].Name == "Podman Engine"`. Its own version is
  `Components[0].Version`, and that is what "Podman 4.4 or newer" is about;
  the compat window is the top-level `ApiVersion`/`MinAPIVersion` (R6).
- *Docker* is `Components[0].Name == "Engine"`. It is *Docker Desktop* when
  `Platform.Name` starts with `Docker Desktop` or `/info.OperatingSystem` is
  `Docker Desktop`; otherwise it is Docker Engine.
- Anything else is an `unknown_engine`, refused with its name.
- `rootless` and `selinux` are the `name=rootless` / `name=selinux` entries of
  `/info.SecurityOptions` (Podman also says `Rootless: true`).
- `OSType: windows` is Docker Desktop in Windows containers mode, refused.

**podman machine against Podman on Linux.** The engine never says it runs in
a VM: a machine's `/info` reads `OperatingSystem: fedora`, the same as a
Fedora workstation's, and its kernel is a Fedora kernel. The socket path does
not tell them apart either: `/run/user/<uid>/podman/podman.sock` is the
rootless socket on any Linux host, and inside a machine `/var/run/docker.sock`
is a symlink to that same socket, so the bundle mounts the same path on both
(S4). Two signals do, and either one is enough:

1. **The storage root is `core`'s.** `podman machine` runs Fedora CoreOS
   (`podman-machine-os`), whose only user is `core` and whose homes live under
   ostree's `/var/home`. A rootless machine's `/info.DockerRootDir` is
   therefore `/var/home/core/.local/share/containers/storage` -- the recorded
   fixture's. A Linux host's rootless storage is under its own user's home.
2. **The project lives on another operating system.** Compose runs on the
   host, and writes the project's directory into the updater's own
   `com.docker.compose.project.working_dir` label. A macOS path (`/Users/…`,
   `/Volumes/…`, `/private/…`) or a Windows one (`C:\\…`) under a Podman
   engine can only be a machine. This is what catches a **rootful** machine,
   whose storage is `/var/lib/containers/storage` like any rootful host's.

Neither signal, under Podman, is Podman on Linux. A Fedora Silverblue host
whose user is literally called `core` would read as a machine; that costs only
the memory sentence pointing at `podman machine set --memory`.

**Not root (8.2, S2, S4).** `not_root()` is the per-engine rule as data, for
the compose file and the launchers (#164, #167) to read rather than restate.

**`podman-restart` (S2).** The updater cannot see systemd inside the host or
the machine. It infers: Podman's `/info.Uptime` gives when the host (or the
machine VM) booted. If the updater's own container was *created* before that
boot and *started* within `RESTART_WINDOW_SECONDS` after it, something started
an existing container at boot, and on Podman only `podman-restart.service`
does that. Then it reports `enabled`. Any other case -- created this boot,
started long after it, no uptime -- is `unknown`, never `disabled`: the
absence of evidence is not evidence, and a hand-run `compose up` right after a
boot is indistinguishable from the unit.
"""

from __future__ import annotations

import re
import socket as _socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from updater import engine as eng
from updater.contract import FROZEN_PROTOCOL, RunningApp, label_version

DETECT_EVERY_SECONDS = 5 * 60

#: 8.3: the oldest Podman self-update runs on, by Podman's own version.
PODMAN_FLOOR: tuple[int, int] = (4, 4)

#: How soon after a boot a start counts as the boot's doing (podman-restart).
RESTART_WINDOW_SECONDS = 10 * 60

#: The labels a local build lacks (A6), and the protocol an app image writes (C4).
VERSION_LABEL = "org.opencontainers.image.version"
REVISION_LABEL = "org.opencontainers.image.revision"
APP_REPOSITORY = eng.REPOSITORIES[0]
#: `com.github.<owner>.spend-tracker.updater-protocol` (6.6), the owner being
#: the ghcr namespace of the repository's images.
PROTOCOL_LABEL = f"com.github.{APP_REPOSITORY.split('/')[1]}.spend-tracker.updater-protocol"

WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"

#: The refusal sentences of 8.3, as amended by S8 (ECI is Docker Business only).
#: One per token; `too_old` and `unknown_engine` are filled in by `sentence()`.
SENTENCES: dict[str, str] = {
    "permission_denied": "The updater cannot use the container engine: permission denied on its socket.",
    "eci_blocked": (
        "Docker Desktop's Enhanced Container Isolation stops containers using the Docker socket; "
        "add the updater image to enhancedContainerIsolation.dockerSocketMount.imageList.images "
        "in admin-settings.json (deploy/DOCKER.md, \"Enhanced Container Isolation\", Docker Business only), "
        "or ask whoever manages Docker Desktop to."
    ),
    "windows_containers": "Docker Desktop is in Windows containers mode. Switch to Linux containers.",
    "tcp_socket": "The updater talks to the container engine over a unix socket only, not over TCP.",
    "too_old": "{name} {version} is too old for self-update; {floor} or newer is needed.",
    "unknown_engine": "The updater does not recognise the container engine {name}, so it does nothing with it.",
    "unreachable": "The container engine's socket does not answer.",
    "outdated": "The updater is too old for this container engine. Update the updater first, then try again.",
}

#: The one setting to fix a permission refusal (8.3), by where the updater runs.
PERMISSION_FIXES = {
    "server": "Set SPENDTRACKER_SOCKET_GID to the group that owns the engine socket, then start the updater again.",
    "personal": "Reinstall with the current bundle.",
}


def sentence(token: str, **params: str) -> str:
    return SENTENCES[token].format(**params)


# --------------------------------------------------------------------------- #
# What detection found
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Detection:
    """One detection. `engine` is None when the engine could not be identified."""

    socket: str
    sentence: str | None
    engine: str | None = None
    engine_version: str = ""
    rootless: bool = False
    selinux: bool = False
    os_type: str = ""
    api_version: str = ""
    engine_api: str = ""
    #: Detection's hint at the layout of the host, not the app's layout (that
    #: is `layout_of`): whether the engine runs in a VM (Desktop, machine).
    vm: bool = False
    #: How long the host (or the machine VM) has been up, where the engine
    #: says: Podman's `/info.Uptime`. What `podman_restart` infers from.
    uptime_s: float | None = None

    @property
    def refused(self) -> bool:
        return self.socket not in ("ok", "outdated")


def _security_names(info: Mapping) -> set[str]:
    names = set()
    for entry in info.get("SecurityOptions") or []:
        if not isinstance(entry, str):
            continue
        for part in entry.split(","):
            key, _, value = part.partition("=")
            if key == "name":
                names.add(value)
    return names


def _numbers(version: str | None) -> tuple[int, ...] | None:
    m = re.match(r"v?([0-9]+)\.([0-9]+)", version or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


_FOREIGN_HOST_PATH = re.compile(r"(/Users/|/Volumes/|/private/|[A-Za-z]:[\\/])")


def _podman_in_a_machine(info: Mapping, working_dir: str | None) -> bool:
    """The two signals of the module docstring. Either is enough."""
    root = info.get("DockerRootDir")
    if isinstance(root, str) and root.startswith("/var/home/core/"):
        return True
    return isinstance(working_dir, str) and bool(_FOREIGN_HOST_PATH.match(working_dir))


def identify(version_doc: Mapping, info_doc: Mapping | None = None, working_dir: str | None = None) -> Detection:
    """Pure: the detection from a `/version` answer, an `/info` answer and the project's directory."""
    info = info_doc or {}
    try:
        negotiated = eng.negotiate(version_doc)
    except eng.EngineUnavailable:
        name = _platform(version_doc) or "that answered without an API version"
        return Detection("unknown_engine", sentence("unknown_engine", name=name))
    os_type = str(info.get("OSType") or version_doc.get("Os") or "")
    security = _security_names(info)
    rootless = "rootless" in security or info.get("Rootless") is True
    selinux = "selinux" in security
    common = dict(
        engine_version=negotiated.engine_version or "",
        rootless=rootless,
        selinux=selinux,
        os_type=os_type,
        api_version=eng.api_text(negotiated.version),
        engine_api=negotiated.engine_api,
        uptime_s=parse_uptime(info.get("Uptime")),
    )

    if negotiated.component == "Podman Engine":
        machine = _podman_in_a_machine(info, working_dir)
        engine, vm = ("podman-machine" if machine else "podman"), machine
    elif negotiated.component == "Engine":
        desktop = (negotiated.platform or "").startswith("Docker Desktop") or info.get(
            "OperatingSystem"
        ) == "Docker Desktop"
        engine, vm = ("docker-desktop" if desktop else "docker-engine"), desktop
        if desktop:
            # The heartbeat's `engine_version` is the product's, as 5.3's
            # example has it ("4.48.0"), not the Engine inside it.
            m = re.search(r"Docker Desktop ([0-9]+\.[0-9]+\.[0-9]+)", negotiated.platform or "")
            if m:
                common["engine_version"] = m.group(1)
    else:
        name = negotiated.component or _platform(version_doc) or "with no name"
        return Detection("unknown_engine", sentence("unknown_engine", name=name), **common)

    found = dict(engine=engine, vm=vm, **common)
    if os_type == "windows":
        return Detection("windows_containers", sentence("windows_containers"), **found)
    if engine.startswith("podman"):
        podman = _numbers(negotiated.engine_version)
        if podman is None or podman < PODMAN_FLOOR:
            return Detection(
                "too_old",
                sentence(
                    "too_old",
                    name="Podman",
                    version=".".join(map(str, podman)) if podman else "of this version",
                    floor=".".join(map(str, PODMAN_FLOOR)),
                ),
                **found,
            )
    if negotiated.state == "too_old":
        return Detection(
            "too_old",
            sentence(
                "too_old",
                name="Docker Engine API" if engine.startswith("docker") else "Engine API",
                version=eng.api_text(negotiated.engine_max),
                floor=eng.api_text(eng.TESTED_FLOOR),
            ),
            **found,
        )
    if negotiated.state == "outdated":
        return Detection("outdated", sentence("outdated"), **found)
    return Detection("ok", None, **found)


def _platform(version_doc: Mapping) -> str | None:
    platform = version_doc.get("Platform")
    name = platform.get("Name") if isinstance(platform, dict) else None
    return name if isinstance(name, str) and name else None


# --------------------------------------------------------------------------- #
# Refusals that come before any answer
# --------------------------------------------------------------------------- #

_TCP = re.compile(r"(tcp|http|https|ssh)://", re.IGNORECASE)
_HOST_PORT = re.compile(r"[A-Za-z0-9.\-\[\]:]+:[0-9]{1,5}")


def check_socket_target(target: str) -> Detection | None:
    """A `tcp://`, `http(s)://`, `ssh://` or `host:port` engine address is refused (8.3).

    `unix://` and a plain path are what the updater accepts. Returns the
    refusal, or None.
    """
    t = target.strip()
    if t.startswith("unix://"):
        return None
    if _TCP.match(t) or (not t.startswith("/") and _HOST_PORT.fullmatch(t)):
        return Detection("tcp_socket", sentence("tcp_socket"))
    return None


_ECI = re.compile(r"enhanced container isolation|\bECI\b", re.IGNORECASE)


def from_error(exc: BaseException) -> Detection:
    """The refusal an engine failure means. ECI's denials name it in the message."""
    if isinstance(exc, eng.EngineError) and _ECI.search(exc.message):
        return Detection("eci_blocked", sentence("eci_blocked"))
    if isinstance(exc, eng.EngineUnavailable) and exc.socket_state in ("permission_denied", "unreachable"):
        return Detection(exc.socket_state, sentence(exc.socket_state))
    if isinstance(exc, PermissionError):
        return Detection("permission_denied", sentence("permission_denied"))
    return Detection("unreachable", sentence("unreachable"))


def detect(client: eng.EngineClient, working_dir: str | None = None) -> Detection:
    """`GET /version` (negotiating), then `GET /info`, and the verdict.

    Never raises for an engine that answers badly or not at all: that is a
    refusal, and the heartbeat carries it.
    """
    try:
        client.negotiate()
    except eng.EngineUnavailable as e:
        if e.socket_state == "unknown_engine":
            return identify(client.version_doc or {})
        return from_error(e)
    except (eng.EngineError, OSError) as e:
        return from_error(e)
    try:
        info = client.info()
    except (eng.EngineError, eng.EngineUnavailable, OSError) as e:
        return from_error(e)
    return identify(client.version_doc or {}, info if isinstance(info, dict) else {}, working_dir)


# --------------------------------------------------------------------------- #
# The not-root rule, per engine (8.2, S2, S4)
# --------------------------------------------------------------------------- #

#: Stands for the host's own group of the socket (`getent group docker`),
#: which compose reads from SPENDTRACKER_SOCKET_GID.
SOCKET_GID = "${SPENDTRACKER_SOCKET_GID}"
UPDATE_GROUP = "65532"


@dataclass(frozen=True)
class NotRoot:
    """How the updater service runs on one engine, for the compose file and launchers."""

    engine: str
    rootless: bool
    #: compose `user:`.
    user: str
    #: compose `group_add:`. The volume's group (C11) is always last.
    group_add: tuple[str, ...]
    #: Whether the launcher must enable `podman-restart.service`, and in
    #: which systemd scope (`system` or `user`); None where a daemon restarts.
    #: It is off by default in podman machine and on Fedora alike (S2).
    podman_restart: str | None
    #: Whether the launcher must enable lingering (`loginctl enable-linger`)
    #: so a rootless engine's containers come back at boot. The updater can
    #: fix neither this nor podman-restart; it can only report.
    linger: bool
    #: The socket's host path, the default for SPENDTRACKER_ENGINE_SOCKET.
    socket: str
    #: True when a spike observed this row; False while it is written from docs.
    confirmed: bool
    #: `security_opt: label=disable`, carried on every engine (S4): SELinux
    #: needs it to open the socket, and everywhere else it is a no-op.
    label_disable: bool = True


_RULES: dict[tuple[str, bool], NotRoot] = {
    # Observed on Debian 13: the socket is root:docker 660; the docker group
    # reaches it and group 0 does not.
    ("docker-engine", False): NotRoot(
        "docker-engine", False, "65532:65532", (SOCKET_GID, UPDATE_GROUP), None, False,
        "/var/run/docker.sock", True,
    ),
    # B11. Observed on Debian 13: the user's socket is mode 1660 and only
    # in-container uid 0 -- the unprivileged host user -- reaches it; 65532
    # fails with group 0 and with the socket's mapped group.
    ("docker-engine", True): NotRoot(
        "docker-engine", True, "0:0", (UPDATE_GROUP,), None, True,
        "${XDG_RUNTIME_DIR}/docker.sock", True,
    ),
    # Observed on macOS: the VM's socket is 0:0 660 (S7).
    ("docker-desktop", False): NotRoot(
        "docker-desktop", False, "65532:65532", ("0", UPDATE_GROUP), None, False,
        "/var/run/docker.sock", True,
    ),
    # Observed on Fedora 44, SELinux enforcing: root:root 660, reached by
    # 65532 with group 0 only under label=disable.
    ("podman", False): NotRoot(
        "podman", False, "65532:65532", ("0", UPDATE_GROUP), "system", False,
        "/run/podman/podman.sock", True,
    ),
    # Observed on Fedora 44: 65532 reaches the socket with group 0 and
    # label=disable, but cannot write the user's project directory (it maps
    # into the subuid range), so in-container uid 0 -- the user -- it is.
    ("podman", True): NotRoot(
        "podman", True, "0:0", (UPDATE_GROUP,), "user", True,
        "/run/user/${UID}/podman/podman.sock", True,
    ),
    # S21, observed on macOS with a machine made as Podman Desktop makes one
    # (rootful, its default): the socket is /run/podman/podman.sock, root:root
    # 660, and `/var/run/docker.sock` links to it, so the bundle's default
    # mount is right here too. 65532 with group 0 reaches it under
    # label=disable. podman-restart is off, system and user; the **system**
    # unit, enabled, brings `unless-stopped` back after a machine restart.
    ("podman-machine", False): NotRoot(
        "podman-machine", False, "65532:65532", ("0", UPDATE_GROUP), "system", False,
        "/var/run/docker.sock", True,
    ),
    # S4, observed on macOS: unlike Linux, the project directory is shared
    # over VirtioFS and 65532 writes it; `/var/run/docker.sock` in the
    # machine is a symlink to the user socket. Lingering is on in the machine.
    ("podman-machine", True): NotRoot(
        "podman-machine", True, "65532:65532", ("0", UPDATE_GROUP), "user", False,
        "/var/run/docker.sock", True,
    ),
}  # fmt: skip


def not_root(engine: str, rootless: bool) -> NotRoot:
    """The rule for an engine. Docker Desktop has no rootless mode of its own."""
    key = (engine, rootless and engine != "docker-desktop")
    if key not in _RULES:
        raise KeyError(f"no not-root rule for {engine} (rootless={rootless})")
    return _RULES[key]


# --------------------------------------------------------------------------- #
# podman-restart, inferred (S2)
# --------------------------------------------------------------------------- #

_UPTIME = re.compile(r"(?:([0-9]+)h\s*)?(?:([0-9]+)m\s*)?(?:([0-9]+(?:\.[0-9]+)?)s)?")


def parse_uptime(value: object) -> float | None:
    """Podman's `/info.Uptime`, `"0h 25m 19.00s"`, in seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    m = _UPTIME.fullmatch(value.strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def parse_engine_time(value: object) -> float | None:
    """An engine timestamp -- nanoseconds and any offset -- as a POSIX time."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})", value)
    if not m:
        return None
    fraction = (m.group(2) or ".0")[:7]
    offset = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        return datetime.fromisoformat(m.group(1) + fraction + offset).timestamp()
    except ValueError:
        return None


def podman_restart(detection: Detection, own: Mapping, now: float) -> str:
    """`enabled`, `unknown`, or `not_applicable` off Podman. See the module docstring.

    `own` is the updater's own container inspect (`Created`, `State.StartedAt`).
    """
    if not (detection.engine or "").startswith("podman"):
        return "not_applicable"
    uptime = detection.uptime_s
    created = parse_engine_time(own.get("Created"))
    started = parse_engine_time((own.get("State") or {}).get("StartedAt") if isinstance(own.get("State"), dict) else None)
    if uptime is None or created is None or started is None:
        return "unknown"
    boot = now - uptime
    if created < boot and boot <= started <= boot + RESTART_WINDOW_SECONDS:
        return "enabled"
    return "unknown"


# --------------------------------------------------------------------------- #
# The updater's own container
# --------------------------------------------------------------------------- #

#: A container's 64-hex id in a path the engine bind-mounts into it:
#: `/var/lib/docker/containers/<id>/hostname` (Docker) or
#: `…/overlay-containers/<id>/userdata/hostname` (Podman, rootful or rootless).
_ID_IN_MOUNT = re.compile(r"containers/([0-9a-f]{64})/")
_SHORT_ID = re.compile(r"[0-9a-f]{12,64}")


def own_ids(mountinfo: str, hostname: str) -> tuple[list[str], str | None]:
    """Full ids from `/proc/self/mountinfo`, and the hostname if it is an id prefix.

    The mounted `/etc/hostname`, `/etc/hosts` and `/etc/resolv.conf` come from
    the engine's per-container directory, whose path carries the full id, on
    Docker and on Podman alike. The hostname is the short id by default on
    both, including podman-compose's pods (their UTS namespace is private),
    but a compose `hostname:` replaces it, so it is only the fallback.
    """
    full: list[str] = []
    for line in mountinfo.splitlines():
        if not any(f" /etc/{name} " in line for name in ("hostname", "hosts", "resolv.conf")):
            continue
        for match in _ID_IN_MOUNT.finditer(line):
            if match.group(1) not in full:
                full.append(match.group(1))
    return full, hostname if _SHORT_ID.fullmatch(hostname or "") else None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def find_own_container(
    listing: Iterable[Mapping], mountinfo: str | None = None, hostname: str | None = None
) -> Mapping | None:
    """Which of the project's containers this process runs in, or None.

    `listing` is `EngineClient.containers()`: the project only. The name is
    then read from the engine, never built: Docker Compose names it
    `<project>-updater-1`, podman-compose `<project>_updater_1`, and after a
    handover it is whatever the rename made it.
    """
    mountinfo = _read("/proc/self/mountinfo") if mountinfo is None else mountinfo
    hostname = _socket.gethostname() if hostname is None else hostname
    full, short = own_ids(mountinfo, hostname)
    containers = [c for c in listing if isinstance(c.get("Id"), str)]
    for cid in full:
        for c in containers:
            if c["Id"] == cid:
                return c
    if short:
        matches = [c for c in containers if c["Id"].startswith(short)]
        if len(matches) == 1:
            return matches[0]
    return None


def container_name(c: Mapping) -> str:
    names = c.get("Names") or ([c["Name"]] if isinstance(c.get("Name"), str) else [])
    return str(names[0]).lstrip("/") if names else str(c.get("Id", ""))[:12]


def working_dir_of(c: Mapping) -> str | None:
    labels = c.get("Labels") or (c.get("Config") or {}).get("Labels") or {}
    value = labels.get(WORKING_DIR_LABEL) if isinstance(labels, dict) else None
    return value if isinstance(value, str) else None


# --------------------------------------------------------------------------- #
# The app container: a local build (A6), and the layout
# --------------------------------------------------------------------------- #


def running_app(inspect: Mapping, repo_digests: Iterable[str] = ()) -> RunningApp:
    """The app as `contract.Context` needs it, from its container's inspect.

    A local build (A6) is an image with no `org.opencontainers.image.version`
    or `revision` label, or one whose digest is not a ghcr digest of this
    repository's app image. The digest is read from the container's image
    reference when it was started by digest, or from the image's
    `RepoDigests` -- which the caller passes, because the engine client
    inspects images by repository digest only.
    """
    config = inspect.get("Config") if isinstance(inspect.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    version = label_version(labels.get(VERSION_LABEL))
    revision = labels.get(REVISION_LABEL)
    revision = revision if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{7,40}", revision) else None
    refs = [config.get("Image"), *repo_digests]
    published = False
    for ref in refs:
        m = eng.IMAGE_BY_DIGEST.fullmatch(ref) if isinstance(ref, str) else None
        if m and m.group(1) == APP_REPOSITORY:
            published = True
    protocol = labels.get(PROTOCOL_LABEL)
    protocol = int(protocol) if isinstance(protocol, str) and protocol.isdigit() else FROZEN_PROTOCOL
    return RunningApp(version=version, revision=revision, published=published, protocol=protocol)


def layout_of(app_inspect: Mapping) -> str:
    """`sidecar` when the app's network is another container's, else `loopback` (5.3)."""
    host = app_inspect.get("HostConfig") if isinstance(app_inspect.get("HostConfig"), dict) else {}
    mode = host.get("NetworkMode") if isinstance(host.get("NetworkMode"), str) else ""
    return "sidecar" if mode.startswith(("container:", "service:")) else "loopback"
