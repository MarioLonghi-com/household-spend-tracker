"""Prepare: resolve, measure, verify, pull, check -- and no downtime (design notes 4.4, 8.5).

| P1 | the request was validated by `intake` (5.4) |
| P2 | both images of release `<to>` resolved to digests, without pulling |
| P3 | their sizes from the manifests; disk and memory against 8.5's floors |
| P4 | both attestations verified (Part 7); any doubt refuses |
| P5 | both pulled **by digest**; their labels must agree with the attestations |
| P6 | `python -m scripts.upgrade --check --json` from the new image, against this ledger |
| P7 | the prepare report, bound to both digests, valid for 24 hours |

The app keeps serving throughout. Every failure is a `refused` record with
one sentence, *Preparing failed: …*, and leaves the ledger untouched (Part 12,
rows 3 to 6).

**Disk (8.5).** The floor is the images' unpacked size, plus twice the
database, plus 512 MiB, measured with `statvfs` from a one-off on the
ledger's filesystem -- the VM's disk on Docker Desktop and `podman machine`.
A registry gives only compressed sizes, so the unpacked size is estimated as
`UNPACK_FACTOR` times the compressed size of the images not yet on the engine.

**Memory (8.5, C14).** `MemAvailable` from the same one-off: at least 256 MiB
to pull, and -- at preflight -- the app's `mem_limit` plus 128 MiB, counting
768 MiB when the app has no limit.
"""

from __future__ import annotations

import contextlib
import json

from updater import contract, oneoff, shapes, survey, verify, volume
from updater import engine as eng
from updater.site import APP_REPOSITORY, UPDATER_REPOSITORY, Kit, Records
from updater.trust import architecture

REPORT_SECONDS = 24 * 3600
CHECK_SECONDS = 10 * 60
MEASURE_SECONDS = 2 * 60

MIB = 1024 * 1024
#: Compressed to unpacked, for the disk floor. Python images unpack to about
#: two and a half to three times their compressed size; this errs high.
UNPACK_FACTOR = 3
DISK_SPARE = 512 * MIB
PULL_MEMORY = 256 * MIB
MEMORY_SPARE = 128 * MIB
#: C14: the root compose file sets no `mem_limit`; the bundle sets 768m.
DEFAULT_APP_MEMORY = 768 * MIB


class PrepareFailed(Exception):
    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"


def _mb(n: int) -> str:
    return f"{n // MIB} MB"


def disk_where(engine: str) -> str:
    if engine == "docker-desktop":
        return "Delete older update backups under Database, or raise Docker Desktop's disk limit in Settings → Resources."
    if engine == "podman-machine":
        return "Delete older update backups under Database, or give the Podman machine a larger disk."
    return (
        "Delete older update backups under Database, or free space on the disk that holds the containers."
    )


def memory_where(engine: str) -> str:
    if engine == "docker-desktop":
        return "Raise Docker Desktop's memory in Settings → Resources, or close other programs."
    if engine == "podman-machine":
        return "Give the Podman machine more memory (podman machine set --memory), or close other programs."
    return "Free memory on the machine running the containers."


def floor_problem(measured: dict, *, engine: str, new_image_bytes: int, memory_needed: int) -> str | None:
    """The sentence when a floor of 8.5 is not met; None when both are."""
    database = int(measured.get("database") or 0)
    free = measured.get("free")
    need = new_image_bytes * UNPACK_FACTOR + 2 * database + DISK_SPARE
    if isinstance(free, int) and free < need:
        return f"Needs about {_gb(need)} free, there is {_gb(free)}. {disk_where(engine)}"
    available = measured.get("mem_available")
    if isinstance(available, int) and available < memory_needed:
        return f"Needs about {_mb(memory_needed)} of memory free, there is {_mb(available)}. {memory_where(engine)}"
    return None


def app_memory(app_inspect: dict) -> int:
    limit = (app_inspect.get("HostConfig") or {}).get("Memory")
    return limit if isinstance(limit, int) and limit > 0 else DEFAULT_APP_MEMORY


def measure(kit: Kit, app: survey.App, request_id: str) -> dict:
    """The disk, memory and database figures of 8.5, from a one-off of the running image."""
    if not app.image_ref:
        raise PrepareFailed(
            "This instance runs a locally built image. Self-update starts only from a published release."
        )
    body = shapes.oneoff(
        app.inspect,
        app.image_ref,
        "measure",
        request_id,
        ["python", "-c", shapes.MEASURE_SCRIPT],
        ledger_volume=app.ledger_volume,
        ledger_mode="ro",
        image_config=app.image,
    )
    result = kit.runner.run(oneoff.name_for(app.name, "measure", request_id), body, MEASURE_SECONDS)
    try:
        doc = json.loads(result.output.strip().splitlines()[-1]) if result.exit_code == 0 else None
    except (ValueError, IndexError):
        doc = None
    if not isinstance(doc, dict):
        raise PrepareFailed("Preparing failed: the free disk and memory could not be measured.")
    return doc


def present(client: eng.EngineClient, ref: str) -> dict | None:
    try:
        return client.inspect_image(ref)
    except eng.EngineError as e:
        if e.status == 404:
            return None
        raise


def by_digest(repository: str, digest: str) -> str:
    return f"{repository}@{digest}"


def run(kit: Kit, request: contract.Request, records: Records) -> dict:
    """P2-P7 for a validated prepare request. Returns the report, or raises `PrepareFailed`."""
    client = kit.client
    to = str(request.to_version)
    records.say(f"Preparing {to}.", step="P2")
    try:
        app = survey.app(client)
    except survey.NotStarted as e:
        raise PrepareFailed(f"Preparing failed: {e.sentence}") from None

    arch = architecture(client.info())
    try:
        found = {repo: kit.trust.resolve(repo, to, arch) for repo in (APP_REPOSITORY, UPDATER_REPOSITORY)}
    except verify.Refused as e:
        if e.rule == "unreachable":
            raise PrepareFailed("Preparing failed: ghcr.io could not be reached.") from None
        raise PrepareFailed(
            f"Preparing failed: release {to} could not be found on ghcr.io ({e.detail})."
        ) from None

    records.say("Checking free disk and memory.", step="P3")
    missing = sum(r.size for repo, r in found.items() if present(client, by_digest(repo, r.digest)) is None)
    problem = floor_problem(
        measure(kit, app, request.id),
        engine=kit.site.engine,
        new_image_bytes=missing,
        memory_needed=PULL_MEMORY,
    )
    if problem:
        raise PrepareFailed(problem)

    records.say("Verifying where both images came from.", step="P4")
    verified = {}
    for repo, r in found.items():
        try:
            verified[repo] = kit.trust.verify(repo, r.digest, to)
        except verify.Refused as e:
            raise PrepareFailed(
                f"Preparing failed: the image's origin could not be proven: {e.rule}: {e.detail}."
            ) from None

    records.say("Downloading both images.", step="P5")
    for repo, r in found.items():
        ref = by_digest(repo, r.digest)
        try:
            client.pull(ref)
            labels = ((client.inspect_image(ref).get("Config") or {}).get("Labels")) or {}
            verify.labels_agree(verified[repo], labels)
        except verify.Refused:
            for gone in found:
                with contextlib.suppress(eng.EngineError, eng.NotAllowed):
                    client.remove_image(by_digest(gone, found[gone].digest))
            raise PrepareFailed(
                "Preparing failed: the downloaded image is not the one that was verified."
            ) from None
        except eng.EngineError as e:
            raise PrepareFailed(
                f"Preparing failed: the image could not be downloaded ({e.message})."
            ) from None

    records.say("Asking the new version what it would do to this ledger.", step="P6")
    new_ref = by_digest(APP_REPOSITORY, found[APP_REPOSITORY].digest)
    body = shapes.oneoff(
        app.inspect,
        new_ref,
        "check",
        request.id,
        ["python", "-m", "scripts.upgrade", "--check", "--json"],
        ledger_volume=app.ledger_volume,
        image_config=app.image,
    )
    try:
        result = kit.runner.run(oneoff.name_for(app.name, "check", request.id), body, CHECK_SECONDS)
    except oneoff.TimedOut:
        raise PrepareFailed("Preparing failed: the new version's check did not finish.") from None
    try:
        facts = json.loads(result.output) if result.exit_code == 0 else None
    except ValueError:
        facts = None
    if not isinstance(facts, dict) or not isinstance(facts.get("pending"), list):
        raise PrepareFailed("Preparing failed: the new version could not read this ledger.")

    records.say("Writing the report.", step="P7")
    now = kit.clock.now()
    report = contract.PreparedReport(
        id=request.id,
        from_version=str(request.from_version),
        to_version=to,
        digest=found[APP_REPOSITORY].digest,
        updater_digest=found[UPDATER_REPOSITORY].digest,
        expires_at=contract.iso(now + REPORT_SECONDS),
        database_stamp=facts.get("database_stamped"),
        pending=tuple(m for m in facts["pending"] if isinstance(m, dict)),
        attestations={
            "app": verified[APP_REPOSITORY].to_dict(),
            "updater": verified[UPDATER_REPOSITORY].to_dict(),
        },
        sizes={"app": found[APP_REPOSITORY].size, "updater": found[UPDATER_REPOSITORY].size},
    ).to_dict()
    report["code_head"] = facts.get("code_head")
    volume.write_json(kit.site.volume.prepared(request.id), report)
    return report
