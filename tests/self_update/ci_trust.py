"""A trust policy for CI only (design notes 15.5): images built on the runner, no attestations.

The end-to-end job builds release A from the merge base and B from the change
on the runner, pushes them to a registry that answers as `ghcr.io`, and has
nothing Sigstore could verify. Verification is the one thing replaced: this
policy resolves and "verifies" exactly the digests the job pushed and wrote
into a JSON file, with the revision it built each from, and refuses anything
else. Every other step -- pulls by digest, the label comparison of P5, the
drill, the copy, health, the pin -- is the updater's own code.

This module is never imported by `updater/`, and the job fails if `updater/`
ever names it.
"""

from __future__ import annotations

import json
from pathlib import Path

from updater import verify


class CiTrust:
    """`{"<repository>": {"<version>": {"digest": ..., "revision": ..., "size": ...}}}`."""

    def __init__(self, table: Path) -> None:
        self.table = json.loads(Path(table).read_text())

    def _entry(self, repository: str, version: str) -> dict:
        entry = self.table.get(repository, {}).get(version)
        if not isinstance(entry, dict):
            raise verify.Refused("attestation", f"CI built no {repository}:{version}")
        return entry

    def resolve(self, repository: str, version: str, arch: str) -> verify.Resolved:
        entry = self._entry(repository, version)
        return verify.Resolved(
            repository, version, entry["digest"], int(entry.get("size", 0)), f"linux/{arch}"
        )

    def verify(self, repository: str, digest: str, version: str) -> verify.Verified:
        entry = self._entry(repository, version)
        if entry["digest"] != digest:
            raise verify.Refused("subject", f"CI built {entry['digest']}, not {digest}")
        return verify.Verified(repository, digest, version, entry["revision"], "ci")
