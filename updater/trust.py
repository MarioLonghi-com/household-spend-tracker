"""What the orchestration asks of the registry and the attestations (Part 7, 4.4 P2-P5).

Two questions, asked through one small interface so prepare and apply never
reach past it:

- `resolve`: which digest does release `X.Y.Z` of a repository name, and how
  big is it -- without pulling (P2, P3);
- `verify`: is that digest this repository's release `X.Y.Z` (P4, and again
  at preflight).

`Sigstore` is the only implementation the updater carries. It is
`updater.verify` and nothing else: no switch, variable or argument turns it
off (7.5). The end-to-end job in CI (15.5) builds its images on the runner,
where no attestation can exist, and replaces this with a test-only policy
from an entry point under `tests/`, which a CI grep keeps out of `updater/`.
"""

from __future__ import annotations

from typing import Protocol

from updater import verify


class Trust(Protocol):
    def resolve(self, repository: str, version: str, arch: str) -> verify.Resolved: ...

    def verify(self, repository: str, digest: str, version: str) -> verify.Verified: ...


class Sigstore:
    """Resolution against ghcr.io and verification with sigstore-python (7.4)."""

    def __init__(self, registry: verify.Registry | None = None) -> None:
        self.registry = registry

    def resolve(self, repository: str, version: str, arch: str) -> verify.Resolved:
        return verify.resolve(repository, version, arch, self.registry)

    def verify(self, repository: str, digest: str, version: str) -> verify.Verified:
        if self.registry is None:
            return verify.verify(repository, digest, version)
        registry = self.registry
        return verify.verify(
            repository, digest, version, fetch=lambda r, d: verify.fetch_bundles(r, d, registry)
        )


def architecture(info: dict) -> str:
    """The OCI architecture of the engine's machine, from `/info.Architecture`."""
    machine = str((info or {}).get("Architecture") or "").lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, "amd64")
