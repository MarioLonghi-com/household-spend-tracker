"""Prepare: resolve, measure, verify, pull, check -- and no downtime (design notes 4.4, 8.5).

| P1 | the request was validated by `intake` (5.4) |
| P2 | both images of release `<to>` resolved to digests, without pulling |
| P3 | their sizes from the manifests; disk and memory against 8.5's floors |
| P4 | both attestations verified (Part 7); any doubt refuses |
| P5 | both pulled **by digest**; their labels must agree with the attestations |
| P6 | `python -m scripts.upgrade --check --json` from the new image, against this ledger |
| P7 | the prepare report, bound to both digests, valid for 24 hours |

`updater_only` is the short prepare of an `update_updater` request (C2,
6.6): P2, P4 and P5 for the updater image alone, then its protocol window
read from the pulled image's labels -- which intake cannot see (R8) -- and
refused unless it includes the running app's protocol.

The app keeps serving throughout. Every failure is a `refused` record with
one sentence, *Preparing failed: …*, and leaves the ledger untouched (Part 12,
rows 3 to 6).

**Disk (8.5).** The floor is the images' unpacked size, plus twice the
database, plus 512 MiB, measured with `statvfs` from a one-off on the
ledger's filesystem -- the VM's disk on Docker Desktop and `podman machine`.
A registry gives only compressed sizes, so the unpacked size is estimated as
`UNPACK_FACTOR` times the compressed size of the images not yet on the engine.

A refusal says the shortfall, so its two figures never read as equal (#330):
the need is rounded up, the free space down, and the megabytes to free are
named, with what the need is made of. It offers *delete older update
backups* only when the same one-off found more than `KEEP_BACKUPS` of them
on the disk -- the newest five cannot be deleted (B9) -- and says how much
the older ones hold. Its record carries the code `disk_short`, by which the
screen links to *Freeing disk for an update* in `deploy/TROUBLESHOOTING.md`.

**Memory (8.5, C14).** `MemAvailable` from the same one-off: at least 256 MiB
to pull, and -- at preflight -- the app's `mem_limit` plus 128 MiB, counting
768 MiB when the app has no limit.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from dataclasses import dataclass

from updater import contract, handover, oneoff, shapes, survey, verify, volume
from updater import engine as eng
from updater.journal import Owner
from updater.site import APP_REPOSITORY, UPDATER_REPOSITORY, Kit, Records
from updater.trust import architecture

REPORT_SECONDS = 24 * 3600
CHECK_SECONDS = 10 * 60
MEASURE_SECONDS = 2 * 60

MIB = 1024 * 1024
GIB = 1024 * MIB
#: How many update backups are protected (A12, B9): the newest five.
KEEP_BACKUPS = 5
#: The history record's code for a refusal for want of disk (#330).
DISK_SHORT = "disk_short"
MEMORY_SHORT = "memory_short"
#: Compressed to unpacked, for the disk floor. Python images unpack to about
#: two and a half to three times their compressed size; this errs high.
UNPACK_FACTOR = 3
DISK_SPARE = 512 * MIB
PULL_MEMORY = 256 * MIB
MEMORY_SPARE = 128 * MIB
#: C14: the root compose file sets no `mem_limit`; the bundle sets 768m.
DEFAULT_APP_MEMORY = 768 * MIB


class PrepareFailed(Exception):
    def __init__(self, sentence: str, code: str | None = None) -> None:
        super().__init__(sentence)
        self.sentence = sentence
        self.code = code


@dataclass(frozen=True)
class Problem:
    """A floor of 8.5 not met: the owner's sentence, and the record's code for it."""

    code: str
    sentence: str


def _gb_up(n: int) -> str:
    return f"{math.ceil(n * 100 / GIB) / 100:.2f} GB"


def _gb_down(n: int) -> str:
    return f"{math.floor(n * 100 / GIB) / 100:.2f} GB"


def _gb(n: int) -> str:
    return f"{n / GIB:.2f} GB"


def _mb_up(n: int) -> str:
    return f"{-(-n // MIB)} MB"


def _mb(n: int) -> str:
    return f"{n // MIB} MB"


def disk_where(engine: str) -> str:
    if engine == "docker-desktop":
        return "raise Docker Desktop's disk limit in Settings → Resources"
    if engine == "podman-machine":
        return "give the Podman machine a larger disk"
    return "free space on the disk that holds the containers"


def memory_where(engine: str) -> str:
    if engine == "docker-desktop":
        return "Raise Docker Desktop's memory in Settings → Resources, or close other programs."
    if engine == "podman-machine":
        return "Give the Podman machine more memory (podman machine set --memory), or close other programs."
    return "Free memory on the machine running the containers."


def update_backup_names(vol: volume.Volume) -> set[str]:
    """The backup folders the updater's drills took, by folder name: the ones a history record names (R22)."""
    names = set()
    for path in (vol.root / "history").glob("*.json"):
        if not contract.is_uuid4(path.name[: -len(".json")]):
            continue
        doc = volume.read_own_json(path) or {}
        if isinstance(doc.get("backup"), str):
            names.add(os.path.basename(doc["backup"]))
    return {n for n in names if shapes.BACKUP_STAMP.fullmatch(n)}


def deletable_backups(measured: dict, update_backups: set[str] | frozenset[str]) -> dict[str, int]:
    """The update backups on the disk beyond the newest five, with their sizes: what *Delete* may remove.

    From the measuring one-off's listing of `backups/`, which is what is on
    the disk; the history alone also names backups already pruned.
    """
    listed = measured.get("backups")
    if not isinstance(listed, dict):
        return {}
    found = {
        name: size
        for name, size in listed.items()
        if name in update_backups and isinstance(size, int) and not isinstance(size, bool) and size >= 0
    }
    older = sorted(found, reverse=True)[KEEP_BACKUPS:]
    return {name: found[name] for name in older}


def disk_sentence(
    need: int, free: int, *, images: int, database: int, engine: str, deletable: dict[str, int]
) -> str:
    """The refusal for want of disk (#330): both figures, the shortfall, the parts, and what can be done."""
    parts = []
    if images:
        parts.append(f"{_gb(images)} for the new images")
    parts.append(f"{_gb(2 * database)} for two copies of the database")
    parts.append(f"{_gb(DISK_SPARE)} to spare")
    made_of = ", ".join(parts[:-1]) + " and " + parts[-1]
    where = disk_where(engine)
    if deletable:
        count = len(deletable)
        what = "the update backup" if count == 1 else f"the {count} update backups"
        remedy = (
            f"Delete {what} older than the newest {KEEP_BACKUPS} under Database, which frees about "
            f"{_gb(sum(deletable.values()))}, or {where}."
        )
    else:
        remedy = where[0].upper() + where[1:] + "."
    return (
        f"Needs about {_gb_up(need)} free, there is {_gb_down(free)}: free at least "
        f"{_mb_up(need - free)} more. That is {made_of}. {remedy}"
    )


def floor_problem(
    measured: dict,
    *,
    engine: str,
    new_image_bytes: int,
    memory_needed: int,
    update_backups: set[str] | frozenset[str] = frozenset(),
) -> Problem | None:
    """What is short when a floor of 8.5 is not met; None when both are.

    `update_backups` names the folders that are update backups (the history's,
    `update_backup_names`), so the sentence offers deleting only those.
    """
    database = int(measured.get("database") or 0)
    free = measured.get("free")
    images = new_image_bytes * UNPACK_FACTOR
    need = images + 2 * database + DISK_SPARE
    if isinstance(free, int) and free < need:
        sentence = disk_sentence(
            need,
            free,
            images=images,
            database=database,
            engine=engine,
            deletable=deletable_backups(measured, update_backups),
        )
        return Problem(DISK_SHORT, sentence)
    available = measured.get("mem_available")
    if isinstance(available, int) and available < memory_needed:
        return Problem(
            MEMORY_SHORT,
            f"Needs about {_mb_up(memory_needed)} of memory free, there is {_mb(available)}. "
            f"{memory_where(engine)}",
        )
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


def verified_app_ref(kit: Kit, recorded: str, image: dict, version: str) -> str:
    """A previous app image by the digest a release of `version` attests, or `verify.Refused`.

    An update records the reference the app ran by (`survey.published_ref`),
    and on Podman, pulled by a multi-arch index, that can be the platform
    manifest's digest: the engine lists both in `RepoDigests`, in no order
    anybody promises (#287), and a release attests the index alone. So no
    single digest is taken on trust or by position. The recorded one is tried
    first, then every one the engine lists for `image` in the app repository,
    as the updater's own identity does (`handover.own_digests`); each must
    pass `verify` and agree with the image's labels in full, and the first
    that does is the one to run and pin by. With none, the first refusal.
    Going back (`goback`) and recovery's restore and start (`recovery`) ask
    this; it met the E8 drill about every other run on rootless Podman (#421).
    """
    listed = [
        m.group(2)
        for r in image.get("RepoDigests") or []
        if (m := eng.IMAGE_BY_DIGEST.fullmatch(r) if isinstance(r, str) else None) and m.group(1) == APP_REPOSITORY
    ]
    refusal: verify.Refused | None = None
    for digest in dict.fromkeys([recorded.split("@", 1)[1], *listed]):
        try:
            verified = kit.trust.verify(APP_REPOSITORY, digest, version)
            verify.labels_agree(verified, (image.get("Config") or {}).get("Labels"))
        except verify.Refused as e:
            refusal = refusal or e
            continue
        return by_digest(APP_REPOSITORY, digest)
    assert refusal is not None
    raise refusal


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
        update_backups=update_backup_names(kit.site.volume),
    )
    if problem:
        raise PrepareFailed(problem.sentence, problem.code)

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


def updater_only(kit: Kit, request: contract.Request, records: Records) -> Owner:
    """`update_updater`'s own prepare (C2, R8): the updater image of `<to>`, resolved,
    verified, pulled, and speaking the running app's protocol. Returns the successor.

    Uses only calls an `outdated` engine client still allows (C3): `/info`,
    the project listing and inspections, the pull and image inspect.
    """
    client = kit.client
    to = str(request.to_version)
    failed = "Updating the updater failed: "
    records.say(f"Preparing the updater of {to}.", step="P2")
    try:
        app = survey.app(client, pod_ok=True)
    except survey.NotStarted as e:
        raise PrepareFailed(failed + e.sentence) from None
    try:
        found = kit.trust.resolve(UPDATER_REPOSITORY, to, architecture(client.info()))
    except verify.Refused as e:
        if e.rule == "unreachable":
            raise PrepareFailed(failed + "ghcr.io could not be reached.") from None
        raise PrepareFailed(
            failed + f"the updater of {to} could not be found on ghcr.io ({e.detail})."
        ) from None
    records.say("Verifying where the updater image came from.", step="P4")
    try:
        verified = kit.trust.verify(UPDATER_REPOSITORY, found.digest, to)
    except verify.Refused as e:
        raise PrepareFailed(
            failed + f"the image's origin could not be proven: {e.rule}: {e.detail}."
        ) from None
    records.say("Downloading the updater image.", step="P5")
    ref = by_digest(UPDATER_REPOSITORY, found.digest)
    try:
        client.pull(ref)
        labels = ((client.inspect_image(ref).get("Config") or {}).get("Labels")) or {}
        verify.labels_agree(verified, labels)
    except verify.Refused:
        with contextlib.suppress(eng.EngineError, eng.NotAllowed):
            client.remove_image(ref)
        raise PrepareFailed(failed + "the downloaded image is not the one that was verified.") from None
    except eng.EngineError as e:
        raise PrepareFailed(failed + f"the image could not be downloaded ({e.message}).") from None
    window = handover.protocol_window(labels)
    protocol = app.running_app.protocol  # type: ignore[attr-defined]
    if window is None or not window[0] <= protocol <= window[1]:
        with contextlib.suppress(eng.EngineError, eng.NotAllowed):
            client.remove_image(ref)
        reads = (
            f"reads protocols {window[0]} to {window[1]}"
            if window
            else "does not say which protocols it reads"
        )
        raise PrepareFailed(
            failed + f"the updater of {to} {reads}, and the app writes protocol {protocol}."
        )
    return Owner(image_digest=found.digest, version=to, container="")
