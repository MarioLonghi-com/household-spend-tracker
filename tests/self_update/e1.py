"""E1 of the end-to-end job (design notes 15.5): A -> B, loopback layout, Docker Engine.

    sudo python -m tests.self_update.e1 --table digests.json --update-root <mountpoint> \\
        --project-dir <dir> --from 98.0.0 --to 99.0.0

The updater runs **as a Python process on the runner** against the runner's
Docker socket -- no updater image exists yet (#164) -- with the test-only
trust policy of `ci_trust`. As root, because the `update` volume is read and
written at its mountpoint under `/var/lib/docker`. Requests are written owned
by uid 65532, as the app writes them.

It sends a prepare and an apply through the updater's real request path
(`Service.tick`), then asserts E1:

- health from where requests arrive (the published port) says B, at B's commit;
- the ledger's stamp is B's code head;
- every row count is at least what it was before;
- the drill's backup verifies, read by B's own `scripts.backup`;
- the previous container is kept, stopped, as `<name>-previous`;
- the pin (`.env` and `pin/release.env`) names B's app digest;
- the new container differs from the previous one only in its image and
  `SPENDTRACKER_AUTO_MIGRATE` (C10).

Exit 0 when all hold. Whatever happens, it prints what it found.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from tests.self_update.ci_trust import CiTrust
from tests.self_update.compare import ALLOWED, differences
from updater import contract, detect, pin, volume
from updater import engine as eng
from updater.journal import Owner
from updater.service import Service
from updater.site import APP_REPOSITORY, Kit, Site
from updater.volume import Volume

PROJECT = "spend-tracker"
APP_UID = 65532
#: A well-formed recovery-code hash; the code itself is never needed here.
RECOVERY_HASH = "scrypt$ln=15,r=8,p=1$" + "c2FsdHNhbHRzYWx0c2FsdA" + "$" + "A" * 43

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)


def write_request(vol: Volume, doc: dict) -> None:
    volume.write_json(vol.request, doc)
    os.chown(vol.request, APP_UID, APP_UID)


def base(kind: str) -> dict:
    return {
        "protocol": 1,
        "id": str(uuid.uuid4()),
        "kind": kind,
        "created_at": contract.iso(time.time()),
        "requested_by": "usr_ci",
    }


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def dump(vol: Volume, *ids: str) -> None:
    for rid in ids:
        for label, path in (
            ("history", vol.history(rid)),
            ("journal", vol.journal(rid)),
            ("prepared", vol.prepared(rid)),
        ):
            doc = volume.read_own_json(path)
            if doc is not None:
                print(f"--- {label}/{rid}.json\n{json.dumps(doc, indent=2)[:6000]}")
    print(docker("ps", "-a", "--format", "table {{.Names}}\t{{.Image}}\t{{.Status}}"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--table", type=Path, required=True)
    p.add_argument("--update-root", type=Path, required=True)
    p.add_argument("--update-volume", default=f"{PROJECT}_update")
    p.add_argument("--project-dir", type=Path, required=True)
    p.add_argument("--socket", default="/var/run/docker.sock")
    p.add_argument("--from", dest="frm", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--port", type=int, default=8848)
    args = p.parse_args(argv)

    client = eng.EngineClient(args.socket, eng.Scope(project=PROJECT))
    client.negotiate()
    found = detect.detect(client)
    print(
        f"engine {found.engine} {found.engine_version}, API {found.api_version} ({found.engine_api}), {found.socket}"
    )
    vol = Volume(args.update_root)
    site = Site(
        project=PROJECT,
        volume=vol,
        update_volume=args.update_volume,
        project_dir=args.project_dir,
        me=Owner(image_digest="", version="0.0.0", container=""),
        engine=found.engine or "docker-engine",
    )
    trust = CiTrust(args.table)
    service = Service(Kit(client=client, site=site, trust=trust), owner_uid=APP_UID)
    service.startup()

    before = client.inspect(f"{PROJECT}-app-1")
    old_image = client.inspect_image_id(before["Image"])

    print("== prepare")
    prepare = {**base("prepare"), "from_version": args.frm, "to_version": args.to}
    write_request(vol, prepare)
    started = time.monotonic()
    service.tick()
    print(f"   {time.monotonic() - started:.1f} s")
    prepared = volume.read_own_json(vol.prepared(prepare["id"]))
    record = volume.read_own_json(vol.history(prepare["id"])) or {}
    check(record.get("state") == "succeeded", f"prepare succeeded: {record.get('sentence')}")
    if prepared is None:
        dump(vol, prepare["id"])
        return 1

    print("== apply")
    apply = {
        **base("apply"),
        "from_version": args.frm,
        "to_version": args.to,
        "prepared_id": prepared["id"],
        "digest": prepared["digest"],
        "updater_digest": prepared["updater_digest"],
        "accepted_lossy": sorted(contract.lossy_revisions(prepared)),
        "recovery_hash": RECOVERY_HASH,
    }
    write_request(vol, apply)
    started = time.monotonic()
    service.tick()
    print(f"   {time.monotonic() - started:.1f} s")
    record = volume.read_own_json(vol.history(apply["id"])) or {}
    check(record.get("state") == "succeeded", f"apply succeeded: {record.get('sentence')}")
    for note in record.get("notes") or []:
        print(f"   note: {note}")

    print("== E1")
    request = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/api/health", headers={"Host": f"localhost:{args.port}"}
    )
    try:
        answer = json.loads(urllib.request.urlopen(request, timeout=10).read())
    except OSError as e:
        answer = {"error": str(e)}
    revision = trust.table[APP_REPOSITORY][args.to]["revision"]
    check(answer.get("version") == args.to, f"health on the published port says {answer.get('version')}")
    check(revision.startswith(str(answer.get("commit"))), f"health names commit {answer.get('commit')}")

    drill = volume.read_own_json(vol.work(apply["id"]) / "drill.json") or {}
    new_ref = f"{APP_REPOSITORY}@{prepared['digest']}"
    head = docker(
        "run", "--rm", "--network", "none", "--entrypoint", "python", new_ref,
        "-c", "from scripts.upgrade import chain; print(chain()[-1].revision)",
    ).strip()  # fmt: skip
    check((drill.get("stamp") or {}).get("after") == head, f"the ledger is stamped at B's head {head}")
    rows = drill.get("rows") or {}
    lost = {t: (n, (rows.get("after") or {}).get(t)) for t, n in (rows.get("before") or {}).items()
            if (rows.get("after") or {}).get(t, -1) < n}  # fmt: skip
    check(
        bool(rows.get("before")) and not lost,
        f"no table lost rows ({len(rows.get('before') or {})} tables) {lost}",
    )
    folder = (drill.get("backup") or {}).get("folder")
    verified = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-v", f"{PROJECT}_ledger:/var/lib/spend-tracker:ro",
         "--entrypoint", "python", new_ref, "-c",
         "import pathlib,sys; from scripts import backup; backup.verify(pathlib.Path(sys.argv[1])); print('verified')",
         str(folder)],
        capture_output=True, text=True,
    )  # fmt: skip
    check(verified.returncode == 0 and "verified" in verified.stdout, f"the backup {folder} verifies")
    check(record.get("backup") == folder, "the history names the backup")

    parked = client.inspect(f"{PROJECT}-app-1-previous")
    check(
        parked["Id"] == before["Id"] and not parked["State"]["Running"],
        "the previous container is kept, stopped",
    )

    pinned = pin.read(args.project_dir)
    want = pin.image_ref(APP_REPOSITORY, args.to, prepared["digest"])
    check(pinned.get(pin.APP_KEY) == want, f".env pins {pinned.get(pin.APP_KEY)}")
    record_file = (args.project_dir / "pin" / "release.env").read_text()
    check(f"{pin.APP_KEY}={want}" in record_file, "pin/release.env records it")

    after = client.inspect(f"{PROJECT}-app-1")
    new_image = client.inspect_image(new_ref)
    diff = differences(before, after, old_image.get("Config") or {}, new_image.get("Config") or {})
    extra = {k: v for k, v in diff.items() if k not in ALLOWED}
    check(not extra, f"the copy differs only in the image and AUTO_MIGRATE: {sorted(diff)}")
    for key, (was, now) in extra.items():
        print(f"     {key}: {was!r} -> {now!r}")
    env = dict(e.split("=", 1) for e in after["Config"]["Env"])
    check(env.get("SPENDTRACKER_AUTO_MIGRATE") == "0", "the new container has AUTO_MIGRATE=0")

    if failures:
        dump(vol, prepare["id"], apply["id"])
        print(f"\n{len(failures)} E1 assertion(s) failed.")
        return 1
    print("\nE1 holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
