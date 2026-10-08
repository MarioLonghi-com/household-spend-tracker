"""Every container the updater creates, as a create body (design notes 4.2, 6.2, 6.4, 8.4, C10).

Five shapes, and nothing else:

- **The new app** (step 7): a copy of the previous app container.
- **A ledger one-off** (`check`, `drill`, `restore`, `measure`, `find-backup`,
  `prune`): the app's image, the ledger volume, `network_mode: none`, the
  app's user, a read-only root and the app's memory limit (4.1).
- **The maintenance page** (6.4): the old image, the app's network and port
  binding, the `update` volume read-write and the ledger read-only.
- **The port probe** (8.4): the app's image on the default bridge, no mounts.
- The successor updater (6.6) is #162's.

The engine client's create guard (`engine.guard_create`) checks the same
shapes again, from the other side: whatever this module builds, the guard is
what decides it may be sent.

## The copy is an allowlist mapping, not a round trip (C10)

Inspect output is not create input, and Docker's and Podman's inspect differ.
So `copy_app` names every field it carries, and anything not named is left
to the engine and the new image. Exactly two things change: the image (by
digest) and `SPENDTRACKER_AUTO_MIGRATE`, forced to `0`.

What is carried, and the cases that needed a rule:

- **`HostConfig.Binds` verbatim** (S15): Compose writes the named ledger
  volume there; Podman adds its own mount options, which it accepts back.
  `HostConfig.Mounts` too, when a compose file used the long syntax.
- **Networks**, from `NetworkSettings.Networks`, with their aliases. Docker
  lists `[<container name>, <service>]`; Podman adds the container's short id,
  which belongs to the old container and is dropped. Podman reports
  `NetworkMode: bridge` while the container sits on the project's network; a
  `bridge` or `default` mode with exactly one named network is written as that
  network, which both engines accept.
- **The sidecar layout**: `NetworkMode: container:<sidecar id as it is now>`
  (8.4), never the id the old container was created with, and no hostname,
  which an engine refuses beside a container network mode.
- **`Config.Healthcheck`** and every compose label, `depends_on` included (S16),
  under either naming scheme (S5).
- **Port bindings, limits, restart policy, user, read-only root, tmpfs,
  security options, dropped capabilities, groups, logging, DNS and hosts.**
- **What came from the image is not carried**: labels and environment entries
  equal to the old image's own (`org.opencontainers.image.version` above all,
  which would otherwise pin the old version onto the new image), and the old
  image's entrypoint, command and working directory. When the old image's
  configuration is unknown, OCI and Chainguard labels are dropped and the rest
  is carried as it is.
- **What the engine made for the old container is not carried**: `HOSTNAME`
  and Podman's `container=` and `HOME` environment entries, and a hostname
  that is the old container's short id.

A container in a Podman pod cannot be copied: a container created through
the Docker-compatible API cannot join the pod (S19). `in_pod` says so, and
the caller refuses with `POD_SENTENCE` before anything is stopped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from updater.engine import (
    ONEOFF_LABEL,
    PROJECT_LABELS,
    REQUEST_LABEL,
    ROLE_LABEL,
)

AUTO_MIGRATE = "SPENDTRACKER_AUTO_MIGRATE"
LEDGER_PATH = "/var/lib/spend-tracker"
UPDATE_PATH = "/var/lib/spend-tracker-update"
APP_PORT = 8848

#: The suffix of the stopped app's name during and after an update (4.2 step 3).
PREVIOUS_SUFFIX = "-previous"
MAINTENANCE_SUFFIX = "-maintenance"

POD_SENTENCE = (
    "The app runs in a Podman pod, which a container the updater creates cannot join. "
    "Add `x-podman: { in_pod: false }` at the top of the compose file and start it again "
    "with the launcher."
)

#: Labels that come from the image, dropped when the image's own are unknown.
_IMAGE_LABEL_PREFIXES = ("org.opencontainers.image.", "dev.chainguard.")
#: Environment the engine writes for a container, not the compose file.
_ENGINE_ENV = ("HOSTNAME",)
_PODMAN_ENV = ("container", "HOME")

#: HostConfig fields carried as they are.
HOST_FIELDS = (
    "Binds",
    "Mounts",
    "PortBindings",
    "PublishAllPorts",
    "RestartPolicy",
    "Memory",
    "MemoryReservation",
    "MemorySwap",
    "NanoCpus",
    "CpuShares",
    "CpuQuota",
    "CpuPeriod",
    "CpusetCpus",
    "PidsLimit",
    "ReadonlyRootfs",
    "Tmpfs",
    "SecurityOpt",
    "CapDrop",
    "CapAdd",
    "GroupAdd",
    "Init",
    "ExtraHosts",
    "Dns",
    "DnsOptions",
    "DnsSearch",
    "ShmSize",
    "Ulimits",
    "Sysctls",
    "OomScoreAdj",
)
#: Config fields carried as they are.
CONFIG_FIELDS = (
    "User",
    "ExposedPorts",
    "Healthcheck",
    "StopSignal",
    "StopTimeout",
    "Tty",
    "OpenStdin",
    "Domainname",
)


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def name_of(inspect: Mapping) -> str:
    return str(inspect.get("Name") or "").lstrip("/")


def short_id(inspect: Mapping) -> str:
    return str(inspect.get("Id") or "")[:12]


def in_pod(inspect: Mapping) -> bool:
    """S19: Podman's `Pod` is a non-empty id when the container is in one."""
    return bool(inspect.get("Pod"))


def is_podman(inspect: Mapping) -> bool:
    env = _dict(inspect.get("Config")).get("Env") or []
    labels = _dict(_dict(inspect.get("Config")).get("Labels"))
    return "container=podman" in env or any(k.startswith("io.podman.") for k in labels)


def env_dict(env: Sequence[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in env or []:
        if isinstance(entry, str):
            key, _, value = entry.partition("=")
            out[key] = value
    return out


def own_env(inspect: Mapping, image_config: Mapping | None) -> list[str]:
    """The container's environment without what the image or the engine put there."""
    env = [e for e in _dict(inspect.get("Config")).get("Env") or [] if isinstance(e, str)]
    image_env = set(_dict(image_config).get("Env") or []) if image_config is not None else set()
    podman = is_podman(inspect)
    out = []
    for entry in env:
        key = entry.partition("=")[0]
        if key in _ENGINE_ENV or (podman and key in _PODMAN_ENV):
            continue
        if entry in image_env:
            continue
        out.append(entry)
    return out


def with_env(env: Sequence[str], key: str, value: str) -> list[str]:
    """`env` with `key` set to `value`: replaced where it was, appended if not."""
    out, done = [], False
    for entry in env:
        if entry.partition("=")[0] == key:
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(entry)
    if not done:
        out.append(f"{key}={value}")
    return out


def own_labels(inspect: Mapping, image_config: Mapping | None) -> dict[str, str]:
    """The container's labels without the image's (C10)."""
    labels = dict(_dict(_dict(inspect.get("Config")).get("Labels")))
    if image_config is not None:
        image_labels = _dict(_dict(image_config).get("Labels"))
        return {k: v for k, v in labels.items() if image_labels.get(k) != v}
    return {k: v for k, v in labels.items() if not k.startswith(_IMAGE_LABEL_PREFIXES)}


def project_labels(inspect: Mapping) -> dict[str, str]:
    """The project label(s) the app carries, in whichever scheme compose wrote (S5)."""
    labels = _dict(_dict(inspect.get("Config")).get("Labels"))
    return {k: labels[k] for k in PROJECT_LABELS if isinstance(labels.get(k), str)}


def _endpoints(inspect: Mapping) -> dict[str, dict]:
    sid = short_id(inspect)
    out = {}
    for net, ep in _dict(_dict(inspect.get("NetworkSettings")).get("Networks")).items():
        ep = _dict(ep)
        entry: dict = {}
        aliases = [a for a in ep.get("Aliases") or [] if isinstance(a, str) and a != sid]
        if aliases:
            entry["Aliases"] = aliases
        ipam = _dict(ep.get("IPAMConfig"))
        ipam = {k: v for k, v in ipam.items() if v}
        if ipam:
            entry["IPAMConfig"] = ipam
        out[net] = entry
    return out


def network_of(inspect: Mapping) -> tuple[str, dict[str, dict]]:
    """`(NetworkMode, EndpointsConfig)` for a container joining the app's networks."""
    mode = str(_dict(inspect.get("HostConfig")).get("NetworkMode") or "")
    endpoints = _endpoints(inspect)
    if mode in ("", "bridge", "default"):
        named = [n for n in endpoints if n not in ("bridge", "podman")]
        if len(named) == 1:
            mode = named[0]
    return mode, endpoints


def copy_app(
    previous: Mapping,
    image: str,
    *,
    image_config: Mapping | None = None,
    sidecar_id: str | None = None,
) -> dict:
    """The create body of the new app: `previous`, with the image and AUTO_MIGRATE changed (C10).

    `image_config` is the *old* image's `Config` (R27), so what came from the
    image is left to the new one. `sidecar_id` is the sidecar's id as it is
    now, for the sidecar layout.
    """
    cfg = _dict(previous.get("Config"))
    old_host = _dict(previous.get("HostConfig"))
    body: dict = {"Image": image}
    for key in CONFIG_FIELDS:
        if cfg.get(key) not in (None, "", [], {}):
            body[key] = cfg[key]
    image_cfg = _dict(image_config) if image_config is not None else None
    for key in ("Entrypoint", "Cmd", "WorkingDir"):
        value = cfg.get(key)
        if value in (None, "", []):
            continue
        if image_cfg is not None and image_cfg.get(key) == value:
            continue
        body[key] = value
    body["Env"] = with_env(own_env(previous, image_config), AUTO_MIGRATE, "0")
    body["Labels"] = own_labels(previous, image_config)

    host: dict = {}
    for key in HOST_FIELDS:
        value = old_host.get(key)
        if value is not None:
            host[key] = value
    log = _dict(old_host.get("LogConfig"))
    if log.get("Type"):
        host["LogConfig"] = {"Type": log["Type"], "Config": _dict(log.get("Config"))}

    mode, endpoints = network_of(previous)
    if mode.startswith(("container:", "service:")):
        if not sidecar_id:
            raise ValueError("the sidecar layout needs the sidecar's id as it is now")
        host["NetworkMode"] = f"container:{sidecar_id}"
        host.pop("PortBindings", None)
        host.pop("PublishAllPorts", None)
        body.pop("Domainname", None)
    else:
        host["NetworkMode"] = mode
        if endpoints:
            body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
        hostname = cfg.get("Hostname")
        if isinstance(hostname, str) and hostname and hostname != short_id(previous):
            body["Hostname"] = hostname
    body["HostConfig"] = host
    return body


# --------------------------------------------------------------------------- #
# One-offs
# --------------------------------------------------------------------------- #


def volume_at(inspect: Mapping, destination: str) -> str | None:
    """The named volume mounted at `destination`, from `Mounts`."""
    for m in inspect.get("Mounts") or []:
        if isinstance(m, dict) and m.get("Destination") == destination and m.get("Type") == "volume":
            name = m.get("Name")
            return name if isinstance(name, str) else None
    return None


def _oneoff_labels(app: Mapping, role: str, request_id: str) -> dict[str, str]:
    return {**project_labels(app), ONEOFF_LABEL: "True", ROLE_LABEL: role, REQUEST_LABEL: request_id}


def oneoff(
    app: Mapping,
    image: str,
    role: str,
    request_id: str,
    command: Sequence[str],
    *,
    ledger_volume: str,
    ledger_mode: str = "rw",
    update_volume: str | None = None,
    image_config: Mapping | None = None,
) -> dict:
    """A one-off of the app's image against the ledger (4.1): no network, the app's user and limit."""
    cfg = _dict(app.get("Config"))
    host = _dict(app.get("HostConfig"))
    binds = [f"{ledger_volume}:{LEDGER_PATH}:{ledger_mode}"]
    if update_volume:
        binds.append(f"{update_volume}:{UPDATE_PATH}:rw")
    body: dict = {
        "Image": image,
        "Entrypoint": [command[0]],
        "Cmd": list(command[1:]),
        "Env": with_env(own_env(app, image_config), AUTO_MIGRATE, "0"),
        "Labels": _oneoff_labels(app, role, request_id),
        "HostConfig": {
            "NetworkMode": "none",
            "Binds": binds,
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": ""},
            "CapDrop": ["ALL"],
            "SecurityOpt": list(host.get("SecurityOpt") or ["no-new-privileges"]),
            "RestartPolicy": {"Name": "no"},
        },
    }
    if cfg.get("User"):
        body["User"] = cfg["User"]
    if host.get("Memory"):
        body["HostConfig"]["Memory"] = host["Memory"]
    return body


def placard(
    app: Mapping,
    old_image: str,
    request_id: str,
    *,
    ledger_volume: str,
    update_volume: str,
    sidecar_id: str | None,
    recovery: bool = False,
    image_config: Mapping | None = None,
) -> dict:
    """The maintenance page (6.4): the old image, where the app was listening.

    It runs `python -m scripts.placard`, which is #163's. Until that module
    exists the container starts and exits, and step 4 is logged, not fatal.
    """
    cfg = _dict(app.get("Config"))
    host = _dict(app.get("HostConfig"))
    command = ["python", "-m", "scripts.placard", *(["--recovery"] if recovery else [])]
    body: dict = {
        "Image": old_image,
        "Entrypoint": [command[0]],
        "Cmd": command[1:],
        "Env": with_env(own_env(app, image_config), AUTO_MIGRATE, "0"),
        "Labels": _oneoff_labels(app, "placard", request_id),
        "HostConfig": {
            "Binds": [f"{update_volume}:{UPDATE_PATH}:rw", f"{ledger_volume}:{LEDGER_PATH}:ro"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": ""},
            "CapDrop": ["ALL"],
            "SecurityOpt": list(host.get("SecurityOpt") or ["no-new-privileges"]),
            "RestartPolicy": {"Name": "no"},
        },
    }
    if cfg.get("User"):
        body["User"] = cfg["User"]
    if host.get("Memory"):
        body["HostConfig"]["Memory"] = host["Memory"]
    mode, endpoints = network_of(app)
    if mode.startswith(("container:", "service:")):
        if not sidecar_id:
            raise ValueError("the sidecar layout needs the sidecar's id as it is now")
        body["HostConfig"]["NetworkMode"] = f"container:{sidecar_id}"
    else:
        body["HostConfig"]["NetworkMode"] = mode
        if host.get("PortBindings"):
            body["HostConfig"]["PortBindings"] = host["PortBindings"]
        if endpoints:
            body["NetworkingConfig"] = {
                "EndpointsConfig": {
                    net: {k: v for k, v in ep.items() if k != "Aliases"} for net, ep in endpoints.items()
                }
            }
    return body


#: The gateway name per engine that runs in a VM (8.4, S6). Elsewhere a port
#: published on 127.0.0.1 is not reachable from a bridge container (S13, and
#: spike 3 on rootful Podman too), so the binding is checked by inspection.
GATEWAYS = {"docker-desktop": "host.docker.internal", "podman-machine": "host.containers.internal"}


def published_port(inspect: Mapping) -> int | None:
    """The host port the app's 8848 is published on, from its bindings."""
    bindings = _dict(_dict(inspect.get("HostConfig")).get("PortBindings")).get(f"{APP_PORT}/tcp") or []
    for b in bindings:
        port = _dict(b).get("HostPort")
        if isinstance(port, str) and port.isdigit():
            return int(port)
    return None


def port_probe(app: Mapping, image: str, request_id: str, gateway: str, port: int) -> dict:
    """The second health probe of 8.4: the app's image on the default bridge, asking the gateway."""
    cfg = _dict(app.get("Config"))
    body = {
        "Image": image,
        "Entrypoint": ["python"],
        "Cmd": ["-c", HEALTH_SCRIPT, f"http://{gateway}:{port}/api/health", f"localhost:{port}"],
        "Labels": _oneoff_labels(app, "probe", request_id),
        "HostConfig": {
            "NetworkMode": "bridge",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "RestartPolicy": {"Name": "no"},
        },
    }
    if cfg.get("User"):
        body["User"] = cfg["User"]
    return body


# --------------------------------------------------------------------------- #
# The fixed programs one-offs and probes run. Arguments are validated values
# (stamps, a URL built from constants and an integer port); never a request's.
# --------------------------------------------------------------------------- #

#: Fetch a health URL, optionally with a `Host` header (S6); print the body.
HEALTH_SCRIPT = (
    "import sys,urllib.request\n"
    "r=urllib.request.Request(sys.argv[1])\n"
    "if len(sys.argv)>2: r.add_header('Host',sys.argv[2])\n"
    "b=urllib.request.urlopen(r,timeout=5)\n"
    "sys.stdout.write(b.read(4096).decode());sys.exit(0 if b.status==200 else 1)\n"
)

#: Free disk under the ledger, available memory, and the database's size (8.5).
MEASURE_SCRIPT = (
    "import json,os\n"
    "d='/var/lib/spend-tracker';s=os.statvfs(d);m=None\n"
    "for l in open('/proc/meminfo'):\n"
    "  if l.startswith('MemAvailable:'): m=int(l.split()[1])*1024\n"
    "db=sum(os.path.getsize(os.path.join(d,f)) for f in os.listdir(d) if f.startswith('spendtracker.sqlite3'))\n"
    "print(json.dumps({'free':s.f_bavail*s.f_frsize,'mem_available':m,'database':db}))\n"
)

#: 5.6's "no drill report": a backup folder made since the drill started, verified.
FIND_BACKUP_SCRIPT = (
    "import json,os,sys\n"
    "from scripts import backup\n"
    "root='/var/lib/spend-tracker/backups';since=float(sys.argv[1]);found=None\n"
    "names=sorted(os.listdir(root)) if os.path.isdir(root) else []\n"
    "for n in reversed(names):\n"
    "  p=os.path.join(root,n)\n"
    "  if os.path.isdir(p) and os.path.getmtime(p)>=since:\n"
    "    try: backup.verify(__import__('pathlib').Path(p)); found=p\n"
    "    except BaseException: pass\n"
    "    break\n"
    "print(json.dumps({'folder':found}))\n"
)

#: B9: remove the named update backups, and nothing whose name is not a stamp.
PRUNE_SCRIPT = (
    "import json,os,re,shutil,sys\n"
    "root='/var/lib/spend-tracker/backups';gone=[]\n"
    "for n in sys.argv[1:]:\n"
    "  p=os.path.join(root,n)\n"
    "  if re.fullmatch(r'[0-9]{8}-[0-9]{6}',n) and os.path.isdir(p) and not os.path.islink(p):\n"
    "    shutil.rmtree(p); gone.append(n)\n"
    "print(json.dumps({'removed':gone}))\n"
)

BACKUP_STAMP = re.compile(r"[0-9]{8}-[0-9]{6}")
