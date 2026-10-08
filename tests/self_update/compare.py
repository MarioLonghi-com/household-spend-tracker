"""What differs between the previous app container and its copy (C10, E1).

E1 asserts that the new container's inspect differs from the old one's only
in the image and `SPENDTRACKER_AUTO_MIGRATE`. What an image contributes to a
container's inspect -- its labels, its environment -- is taken out of both
sides first, since a new image is the one difference intended.
"""

from __future__ import annotations

from collections.abc import Mapping

from updater import shapes

HOST = (*shapes.HOST_FIELDS, "NetworkMode")
CONFIG = ("User", "ExposedPorts", "Healthcheck", "StopSignal", "Tty", "OpenStdin")


def _own(values: list[str], image: list[str]) -> set[str]:
    return set(values) - set(image)


def _aliases(inspect: Mapping) -> dict[str, list[str]]:
    sid = str(inspect.get("Id", ""))[:12]
    nets = ((inspect.get("NetworkSettings") or {}).get("Networks")) or {}
    return {n: sorted(a for a in (ep or {}).get("Aliases") or [] if a != sid) for n, ep in nets.items()}


def differences(
    previous: Mapping, new: Mapping, old_image: Mapping, new_image: Mapping
) -> dict[str, tuple]:
    """Field -> (previous, new) for every field that differs. `*_image` are the images' `Config`."""
    out: dict[str, tuple] = {}
    ph, nh = previous.get("HostConfig") or {}, new.get("HostConfig") or {}
    for key in HOST:
        if ph.get(key) != nh.get(key):
            out[f"HostConfig.{key}"] = (ph.get(key), nh.get(key))
    if (ph.get("LogConfig") or {}).get("Type") != (nh.get("LogConfig") or {}).get("Type"):
        out["HostConfig.LogConfig"] = (ph.get("LogConfig"), nh.get("LogConfig"))
    pc, nc = previous.get("Config") or {}, new.get("Config") or {}
    for key in CONFIG:
        if pc.get(key) != nc.get(key):
            out[f"Config.{key}"] = (pc.get(key), nc.get(key))
    old_env = _own(pc.get("Env") or [], old_image.get("Env") or [])
    new_env = _own(nc.get("Env") or [], new_image.get("Env") or [])
    for entry in sorted(old_env ^ new_env):
        out[f"Config.Env[{entry.split('=', 1)[0]}]"] = (
            next((e for e in old_env if e.split("=", 1)[0] == entry.split("=", 1)[0]), None),
            next((e for e in new_env if e.split("=", 1)[0] == entry.split("=", 1)[0]), None),
        )
    old_labels = {
        k: v for k, v in (pc.get("Labels") or {}).items() if (old_image.get("Labels") or {}).get(k) != v
    }
    new_labels = {
        k: v for k, v in (nc.get("Labels") or {}).items() if (new_image.get("Labels") or {}).get(k) != v
    }
    for key in sorted(set(old_labels) | set(new_labels)):
        if old_labels.get(key) != new_labels.get(key):
            out[f"Config.Labels[{key}]"] = (old_labels.get(key), new_labels.get(key))
    if _aliases(previous) != _aliases(new):
        out["NetworkSettings.Networks"] = (_aliases(previous), _aliases(new))
    if pc.get("Image") != nc.get("Image"):
        out["Config.Image"] = (pc.get("Image"), nc.get("Image"))
    return out


#: What may differ, and nothing else (C10).
ALLOWED = {"Config.Image", f"Config.Env[{shapes.AUTO_MIGRATE}]"}
