"""Operator-facing scripts: seeding, snapshots, backup, restore, upgrade."""

from __future__ import annotations

import os
import pathlib


def in_a_container() -> bool:
    """Whether this is running inside the app's own image.

    Used only to choose which *instructions* to print. Telling somebody in a
    container to run `make serve` is worse than saying nothing: there is no
    Makefile in the image, and the command they need is
    `docker compose up -d` against a service they have just stopped.

    `/.dockerenv` is the conventional marker and Docker still writes it.
    `SPENDTRACKER_IN_CONTAINER` is the override, for anything that runs the
    image under a runtime that does not.
    """
    said = (os.environ.get("SPENDTRACKER_IN_CONTAINER") or "").strip().lower()
    if said in {"1", "true", "yes", "on"}:
        return True
    if said in {"0", "false", "no", "off"}:
        return False
    return pathlib.Path("/.dockerenv").exists()
