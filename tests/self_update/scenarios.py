"""The end-to-end self-update scenarios, E1-E16 (design notes 15.5; E16 is #275).

    python -m tests.self_update.scenarios --staged DIR --project-dir DIR --leg docker \\
        --socket /var/run/docker.sock --compose "docker compose" [--layout loopback|sidecar] \\
        [--only E1,E3] [--restart "sudo systemctl restart docker"] \\
        [--updater-user 65532:65532] [--socket-gid 989] [--skip E6="why"]

Each scenario asserts what the update did to what an owner has -- row
counts, the ledger's stamp, which digest each container runs, a container's
id and `StartedAt`, the pin's text -- not that a mechanism ran. A scenario
that needs the state another leaves (E11, E12, E13 after E1) reuses it in the
same run, or makes it first when run alone.

`DIR` (staged) is what `tests.self_update.stage build` wrote. The updater runs
in its container throughout: the CI updater image, which is the release's
real updater image plus the test-only trust policy (`ci-updater.Dockerfile`).

Prints each assertion, a table of results and timings, and exits 1 if any
scenario failed. A scenario a leg cannot run is reported as skipped, with the
leg's reason.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tests.self_update import api
from tests.self_update.compare import ALLOWED, differences
from tests.self_update.world import (
    APP_REPO,
    LEDGER,
    LEDGER_VOLUME,
    ROOT,
    UPDATER_REPO,
    Failure,
    Leg,
    Stack,
    new_code,
)
from updater.engine import REQUEST_LABEL, ROLE_LABEL

A, B, B1, B2, B3, B4, C = "98.0.0", "99.0.0", "99.0.1", "99.0.2", "99.0.3", "99.1.0", "100.0.0"
SMOKE = Path(__file__).with_name("updater_smoke.py")


@dataclass
class Result:
    name: str
    title: str
    checks: list[tuple[bool, str]] = field(default_factory=list)
    skipped: str | None = None
    error: str | None = None
    seconds: float = 0.0

    def check(self, ok: bool, what: str) -> bool:
        print(("  ok    " if ok else "  FAIL  ") + what, flush=True)
        self.checks.append((bool(ok), what))
        return bool(ok)

    @property
    def passed(self) -> bool:
        return (
            self.skipped is None
            and self.error is None
            and all(ok for ok, _ in self.checks)
            and bool(self.checks)
        )


# --------------------------------------------------------------------------- #
# What an update leaves behind
# --------------------------------------------------------------------------- #


@dataclass
class Before:
    """What the scenario recorded before sending the apply."""

    app: dict
    app_image: dict
    sidecar: dict | None
    backups: list[str]
    ledger: dict


def before(s: Stack) -> Before:
    app = s.engine.inspect(s.app_name)
    sidecar = s.engine.inspect(s.standin) if s.standin else None
    return Before(
        app=app,
        app_image=s.engine.image(app["Image"]),
        sidecar=sidecar,
        backups=s.vol.backups(),
        ledger=s.vol.ledger(),
    )


def drill(s: Stack, rid: str) -> dict:
    return s.vol.read(f"work/{rid}/drill.json") or {}


def pinned(s: Stack) -> dict[str, str]:
    out = {}
    for line in (s.dir / ".env").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("SPENDTRACKER_IMAGE", "SPENDTRACKER_UPDATER_IMAGE"):
            out[key.strip()] = value.strip()
    return out


def backup_verifies(s: Stack, version: str, folder: str) -> bool:
    """Read by `version`'s own `scripts.backup`, in a one-off of its image, the ledger read-only."""
    name = "st-ci-verify"
    s.engine.run(
        name,
        s.app_ref(version),
        ["-c", "import pathlib,sys; from scripts import backup; backup.verify(pathlib.Path(sys.argv[1])); print('verified')",
         folder],
        entrypoint=["python"],
        binds=[f"{LEDGER_VOLUME}:/var/lib/spend-tracker:ro"],
    )  # fmt: skip
    try:
        code = s.engine.wait_exit(name, 120)
        out = s.engine.logs(name)
    finally:
        s.engine.remove(name)
    return code == 0 and "verified" in out


def check_updated(
    r: Result,
    s: Stack,
    b: Before,
    req: dict,
    record: dict,
    to: str,
    *,
    updater_to: str | None,
    engine_restarted: bool = False,
) -> None:
    """E1's assertions, for an update to `to`. `updater_to`: the release whose updater should now run.

    `engine_restarted`: the engine restarted mid-update (E6), which restarts
    every container -- the sidecar too, same container, new `StartedAt`."""
    rid = req["id"]
    #: Whose code ran the apply: A's updater's, when nothing handed it over.
    owner = (s.journal(rid).get("owner") or {}).get("image_digest")
    r.check(record.get("state") == "succeeded", f"the apply succeeded: {record.get('sentence')}")
    health = s.health()
    r.check(health.get("version") == to, f"health from where requests arrive says {health.get('version')}")
    revision = s.releases[to]["revision"]
    r.check(
        revision.startswith(str(health.get("commit") or "-")), f"health names commit {health.get('commit')}"
    )
    report = drill(s, rid)
    now = s.vol.ledger()
    r.check(now["stamp"] == s.head(to), f"the ledger is stamped {now['stamp']}, {to}'s head {s.head(to)}")
    r.check(
        (report.get("stamp") or {}).get("after") == s.head(to), "the drill's report says the same stamp"
    )
    rows = report.get("rows") or {}
    lost = {
        t: (n, now["counts"].get(t))
        for t, n in (rows.get("before") or {}).items()
        if now["counts"].get(t, -1) < n
    }
    r.check(
        bool(rows.get("before")) and not lost,
        f"no table has fewer rows than before ({len(rows.get('before') or {})} tables) {lost}",
    )
    folder = (report.get("backup") or {}).get("folder")
    r.check(
        bool(folder) and backup_verifies(s, to, str(folder)),
        f"the backup {folder} verifies, read by {to}'s code",
    )
    r.check(record.get("backup") == folder, "the history names that backup")
    # By id: if the apply did not succeed, the name is the app's again.
    previous = s.engine.inspect(b.app["Id"])
    r.check(
        previous["Name"].lstrip("/") == s.app_name + "-previous" and not previous["State"]["Running"],
        f"the previous container is kept, stopped, as -previous ({previous['Name'].lstrip('/')})",
    )
    policy = ((previous.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    unable = [n for n in record.get("notes") or [] if "could not be set to `no`" in n]
    if owner == s.releases[A]["updater"] and contains(s.releases[A]["revision"], PARKING) is False:
        print(f"   (A's updater ran the apply and predates {PARKING[:7]}: restart policy {policy} not checked)")
    else:
        r.check(
            policy == "no" or bool(unable),
            f"and parked with restart policy no ({policy}{'; the engine could not: ' + unable[0] if unable else ''})",
        )
    pin = pinned(s)
    want = f"{APP_REPO}:{to}@{s.releases[to]['app']}"
    r.check(pin.get("SPENDTRACKER_IMAGE") == want, f".env pins the app at {pin.get('SPENDTRACKER_IMAGE')}")
    if updater_to:
        want_u = f"{UPDATER_REPO}:{updater_to}@{s.releases[updater_to]['updater']}"
        r.check(
            pin.get("SPENDTRACKER_UPDATER_IMAGE") == want_u,
            f".env pins the updater at {pin.get('SPENDTRACKER_UPDATER_IMAGE')}",
        )
    record_file = (
        (s.dir / "pin" / "release.env").read_text() if (s.dir / "pin" / "release.env").exists() else ""
    )
    r.check(f"SPENDTRACKER_IMAGE={want}" in record_file, "pin/release.env records the app's pin")
    after = s.engine.inspect(s.app_name)
    r.check(after["Image"] == s.image_id(s.app_ref(to)), f"{s.app_name} runs {to}'s digest")
    diff = differences(
        b.app, after, b.app_image.get("Config") or {}, s.engine.image(after["Image"]).get("Config") or {}
    )
    extra = {k: v for k, v in diff.items() if k not in ALLOWED}
    if (
        "Config.Healthcheck" in extra
        and owner == s.releases[A]["updater"]
        and contains(s.releases[A]["revision"], PODMAN4_HEALTHCHECK) is False
    ):
        # A's updater made this copy, and predates the fix for Podman 4's
        # compat create splitting a CMD healthcheck on every space.
        print(f"   Config.Healthcheck: A's updater made the copy and predates {PODMAN4_HEALTHCHECK[:7]}; set aside")
        extra.pop("Config.Healthcheck")
    raised = extra.get("HostConfig.OomScoreAdj")
    if s.leg.name.startswith("podman-rootless") and raised and (raised[0] or 0) < (raised[1] or 0):
        # Rootless Podman: a container created through the API service gets
        # the service's own oom_score_adj (systemd gives user services 200),
        # and an unprivileged process cannot lower it again; compose's CLI ran
        # from a shell at 0. The engine's, not the copy's (#169).
        print(f"   HostConfig.OomScoreAdj {raised[0]} -> {raised[1]}: rootless Podman's API service; set aside")
        extra.pop("HostConfig.OomScoreAdj")
    r.check(
        not extra,
        f"the copy differs from the previous container only in its image and AUTO_MIGRATE: {sorted(diff)}",
    )
    for key, (was, now_) in extra.items():
        print(f"     {key}: {was!r} -> {now_!r}")
    env = dict(e.split("=", 1) for e in after["Config"]["Env"])
    r.check(
        env.get("SPENDTRACKER_AUTO_MIGRATE") == "0", "the new container has SPENDTRACKER_AUTO_MIGRATE=0"
    )
    if b.sidecar is not None:
        check_sidecar(r, s, b, after, engine_restarted)


def check_sidecar(r: Result, s: Stack, b: Before, app: dict, engine_restarted: bool) -> None:
    """The sidecar layout: the sidecar untouched, and the app in its namespace as it is now (8.4)."""
    assert b.sidecar is not None
    side = s.engine.inspect(b.sidecar["Id"])
    if engine_restarted:
        # An engine restart restarts every container: the sidecar keeps its
        # id and gets a new StartedAt, which is not the updater's doing.
        r.check(side["State"]["Running"], "the sidecar is the same container, running again")
    else:
        r.check(
            side["State"]["StartedAt"] == b.sidecar["State"]["StartedAt"] and side["State"]["Running"],
            "the sidecar is untouched: same container, same StartedAt, running",
        )
    mode = (app.get("HostConfig") or {}).get("NetworkMode")
    r.check(mode == f"container:{side['Id']}", f"the app is in the sidecar's namespace as it is now ({mode})")


def check_rolled_back(
    r: Result, s: Stack, b: Before, req: dict, record: dict, frm: str, *, engine_restarted: bool = False
) -> None:
    """E3's common part: back at `frm`, its stamp, the rows as the drill's backup counted them."""
    r.check(record.get("state") == "rolled_back", f"the apply rolled back: {record.get('sentence')}")
    health = s.health()
    r.check(health.get("version") == frm, f"health from where requests arrive says {health.get('version')}")
    now = s.vol.ledger()
    r.check(now["stamp"] == s.head(frm), f"the ledger is stamped {now['stamp']}, {frm}'s head")
    drilled = (drill(s, req["id"]).get("rows") or {}).get("before") or {}
    differ = {t: (n, now["counts"].get(t)) for t, n in drilled.items() if now["counts"].get(t) != n}
    r.check(not differ, f"every table the drill's backup counted has exactly that many rows {differ}")
    was = b.ledger["counts"]
    moved = {
        t: (was.get(t), now["counts"].get(t))
        for t in set(was) | set(now["counts"])
        if was.get(t) != now["counts"].get(t)
    }
    r.check(
        not moved, f"every table has exactly the rows it had before the apply ({len(was)} tables) {moved}"
    )
    after = s.engine.inspect(s.app_name)
    r.check(after["Image"] == s.image_id(s.app_ref(frm)), f"{s.app_name} runs {frm}'s digest")
    want = ((b.app.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    got = ((after.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    took_back = any("took back over" in n for n in record.get("notes") or [])
    if took_back and contains(s.releases[A]["revision"], PARKING) is False:
        # 6.6: the successor died and A's updater rolled back -- code from
        # before the parking, which does not know to undo it.
        r.check(after["Id"] == b.app["Id"], "the previous container is back under its name")
        print(f"   (A's updater took the rollback back and predates {PARKING[:7]}: restart policy {got} not checked)")
    else:
        r.check(
            after["Id"] == b.app["Id"] and got == want,
            f"the previous container is back under its name, with its own restart policy ({got}, was {want})",
        )
    if b.sidecar is not None:
        check_sidecar(r, s, b, after, engine_restarted)


def restored_aside(s: Stack) -> list[str]:
    return [n for n in s.vol.listdir("/l") or [] if ".before-restore-" in n]


#: 127.0.0.11, as /proc/net/tcp writes it: Docker's embedded DNS resolver.
DOCKER_DNS = "0B00007F:"


def fresh_beat(s: Stack, after: str | None) -> dict:
    """The heartbeat written after `after` (an ISO time): one beat is every 30 s."""
    mark = (after or "")[:19]
    return s.wait_for(
        lambda: (lambda b: b if b and str(b.get("seen_at", ""))[:19] > mark else None)(
            s.vol.read("updater.json")
        ),
        "a heartbeat written after the outcome",
        75,
        every=2,
    )


def updater_now(s: Stack) -> dict:
    """The canonical updater container, inspected."""
    return s.engine.inspect(s.updater_name)


# --------------------------------------------------------------------------- #
# The scenarios
# --------------------------------------------------------------------------- #


class Run:
    def __init__(self, stack: Stack, leg: Leg) -> None:
        self.s = stack
        self.leg = leg
        #: What E1 left, for E10-E13 to read without updating again.
        self.e1: dict | None = None

    # ---- E1 and what reads its state ------------------------------------ #

    def ensure_e1(self, r: Result) -> dict:
        if self.e1 is None:
            print("   (E1's update first)")
            self.E1(r, check=False)
        assert self.e1 is not None
        return self.e1

    def E1(self, r: Result, check: bool = True) -> None:
        s = self.s
        s.up(A)
        listening_before = self.listeners(s.updater_name)
        b = before(s)
        req, record = s.update(A, B)
        self.e1 = {"before": b, "req": req, "record": record, "listening_before": listening_before}
        if check:
            check_updated(r, s, b, req, record, B, updater_to=B)
        else:
            r.check(
                record.get("state") == "succeeded", f"(setup) A -> B succeeded: {record.get('sentence')}"
            )

    def E11(self, r: Result) -> None:
        """The handover, after E1: B's updater is the updater, A's is parked, one heartbeat is current."""
        s = self.s
        e1 = self.ensure_e1(r)
        u = updater_now(s)
        r.check(u["Image"] == s.image_id(s.updater_ref(B)), f"{s.updater_name} runs B's updater digest")
        r.check(u["State"]["Running"], f"{s.updater_name} is running")
        prev = s.engine.inspect(s.updater_name + "-previous")
        r.check(
            prev["Image"] == s.image_id(s.updater_ref(A)), f"{s.updater_name}-previous holds A's updater"
        )
        beat = fresh_beat(s, after=e1["record"].get("finished_at"))
        r.check(
            beat.get("role") == "current" and beat.get("image_digest") == s.releases[B]["updater"],
            f"the one heartbeat says current, with B's digest ({beat.get('role')}, "
            f"{str(beat.get('image_digest'))[:19]}, {beat.get('container')})",
        )
        lock = (s.vol.read("updater.lock") or {}).get("holder") or {}
        r.check(lock.get("image_digest") == s.releases[B]["updater"], "updater.lock names B's updater")
        if s.releases[B].get("updater_index"):
            # Pulled by a multi-arch index's digest (stage.py --index): what
            # this leg is for is an engine listing two digests for it (#287).
            listed = [
                d.split("@", 1)[1]
                for d in s.engine.image(s.updater_ref(B)).get("RepoDigests") or []
                if d.startswith(UPDATER_REPO + "@")
            ]
            r.check(
                s.releases[B]["updater"] in listed and len(set(listed)) >= 2,
                f"the engine lists B's updater by the index digest and another ({', '.join(d[:19] for d in listed)})",
            )
        rid = e1["req"]["id"]
        hand = s.vol.read(f"handover/{rid}.request") or {}
        r.check(
            hand.get("outcome") in (None, "done") and s.vol.exists(f"/u/handover/{rid}.go"),
            f"the handover went: `go` written, nothing failed or taken back ({hand.get('outcome')})",
        )

    def E11b(self, r: Result) -> None:
        """A -> B3, whose updater fails its self-check: A's updater stays current and runs the apply.

        The second half of both E11 and E13, on a fresh stack."""
        s = self.s
        s.up(A)
        b = before(s)
        req, record = s.update(A, B3)
        check_updated(r, s, b, req, record, B3, updater_to=None)
        u = updater_now(s)
        r.check(u["Image"] == s.image_id(s.updater_ref(A)), f"{s.updater_name} still runs A's updater")
        beat = fresh_beat(s, after=record.get("finished_at"))
        r.check(
            beat.get("role") == "current" and beat.get("image_digest") == s.releases[A]["updater"],
            f"the heartbeat says A's updater is current ({beat.get('role')}, {str(beat.get('image_digest'))[:19]})",
        )
        pin = pinned(s)
        r.check(
            s.releases[B3]["updater"] not in pin.get("SPENDTRACKER_UPDATER_IMAGE", ""),
            "the pin does not name B3's updater",
        )
        hand = s.vol.read(f"handover/{req['id']}.request") or {}
        r.check(
            hand.get("outcome") == "failed",
            f"the 2a handover's journal says failed ({hand.get('outcome')})",
        )
        j = s.journal(req["id"])
        owners = {str((o.get("owner") or {}).get("image_digest")) for o in j.get("owners") or []}
        owner = (j.get("owner") or {}).get("image_digest")
        r.check(
            owner == s.releases[A]["updater"] and owners <= {s.releases[A]["updater"]},
            "the journal names A's updater as the apply's only owner",
        )
        running = [s.name(c) for c in s.running("updater")]
        r.check(running == [s.updater_name], f"only A's updater runs: {running}")

    def E13(self, r: Result) -> None:
        """Updater first, after E1: the handover completed before the app stopped; B's updater ran the rest."""
        s = self.s
        e1 = self.ensure_e1(r)
        j = s.journal(e1["req"]["id"])
        started = {e.get("step"): e.get("at") for e in j.get("started") or [] if isinstance(e, dict)}
        trail = j.get("owners") or []
        to_b = next(
            (o for o in trail if (o.get("owner") or {}).get("image_digest") == s.releases[B]["updater"]),
            None,
        )
        r.check(
            to_b is not None and to_b.get("from_step") == "2a",
            f"the journal hands the apply to B's updater at 2a: {to_b}",
        )
        r.check(
            "2a" in started and "3" in started and str(started["2a"]) <= str(started["3"]),
            f"step 2a started before step 3, which stops the app: {started.get('2a')}, {started.get('3')}",
        )
        if to_b is not None and "3" in started:
            r.check(
                str(to_b.get("at")) <= str(started["3"]),
                f"B's updater owned the apply before the app stopped ({to_b.get('at')} <= {started['3']})",
            )
        r.check(
            (j.get("owner") or {}).get("image_digest") == s.releases[B]["updater"],
            "the journal's owner when it finished is B's updater",
        )
        r.check(
            not (j.get("context") or {}).get("handover_id"), "and step 10 had no handover of its own to do"
        )

    def E10(self, r: Result) -> None:
        """Nothing listens in the updater's namespace: A's, and B's after the handover.

        Docker's embedded resolver for a user-defined network listens on
        127.0.0.11 inside every container on it -- the engine's socket in the
        container's namespace, not the updater's -- and is set aside, by name."""
        e1 = self.ensure_e1(r)
        r.check(e1["listening_before"] == [], f"A's updater listens on nothing: {e1['listening_before']}")
        now = self.listeners(self.s.updater_name)
        r.check(now == [], f"B's updater, after the handover, listens on nothing: {now}")

    def listeners(self, container: str) -> list[str]:
        code, out = self.s.engine.exec(
            container, ["python", "-c", SMOKE.read_text()], env=["SMOKE_LISTENERS_ONLY=1"]
        )
        if code != 0:
            raise Failure(f"the listener check did not run in {container}: {out[-500:]}")
        found = json.loads(out.strip().splitlines()[-1])
        resolver = [
            x for x in found if x.split()[0] in ("tcp", "udp") and x.split()[1].startswith(DOCKER_DNS)
        ]
        if resolver:
            print(
                f"   {container}: Docker's embedded resolver at 127.0.0.11 ({len(resolver)} sockets), set aside"
            )
        return [x for x in found if x not in resolver]

    def E16(self, r: Result) -> None:
        """The sidecar restarts on its own: the updater rejoins the app to its new namespace (#275).

        After E1, so the updater is B's, built from the change. Bounded by the
        updater's check interval, the app's stop and its start."""
        s = self.s
        self.ensure_e1(r)
        s.wait_for(lambda: s.health().get("version") == B, "health before the restart", 120)
        app = s.engine.inspect(s.app_name)
        seen = set(s.vol.listdir("/u/history") or [])
        s.engine.stop(s.standin)
        s.engine.start(s.standin)
        side = s.engine.inspect(s.standin)
        stranded = s.health().get("version")
        print(f"   right after the sidecar's restart, health says {stranded!r} (stranded unless rejoined)")
        started = time.monotonic()
        health = s.wait_for(
            lambda: (lambda h: h if h.get("version") == B else None)(s.health()), "the app to answer again", 180
        )
        took = time.monotonic() - started
        r.check(health.get("version") == B, f"the app answers inside the sidecar again, after {took:.0f} s")
        after = s.engine.inspect(s.app_name)
        r.check(after["Id"] == app["Id"], "the same app container, restarted rather than recreated")
        r.check(
            after["State"]["StartedAt"] > side["State"]["StartedAt"] > app["State"]["StartedAt"],
            f"the app started after the sidecar's restart ({after['State']['StartedAt']} > "
            f"{side['State']['StartedAt']})",
        )
        r.check(
            after["HostConfig"]["NetworkMode"] == f"container:{side['Id']}",
            "the app is in the sidecar's namespace as it is now",
        )
        records = [
            d
            for n in sorted(set(s.vol.listdir("/u/history") or []) - seen)
            if (d := s.vol.read(f"history/{n}") or {}).get("kind") == "rejoin"
        ]
        r.check(
            len(records) == 1 and records[0].get("state") == "succeeded",
            f"one rejoin is recorded: {[d.get('sentence') for d in records]}",
        )
        beat = s.vol.read("updater.json") or {}
        r.check(not beat.get("problem"), f"the heartbeat reports no problem ({beat.get('problem')})")

    def E12(self, r: Result) -> None:
        """`compose up -d` again, reading the pin, with the parked containers present."""
        s = self.s
        self.ensure_e1(r)
        parked = [s.name(c) for c in s.project_containers() if s.name(c).endswith(("-previous", "-next"))]
        print(f"   parked before compose up: {parked}")
        s.compose("up", "-d", "--no-build")
        time.sleep(5)
        pin = pinned(s)
        app_ref = pin["SPENDTRACKER_IMAGE"].split(":", 2)
        apps = s.wait_for(lambda: s.running("app"), "an app container", 120)
        upd = s.running("updater")
        # docker compose removes the parked containers of a service (S5);
        # podman-compose leaves them, and the previous updater is standing by
        # for ten minutes after E1's handover (6.6, H6) -- running, and not
        # the updater. It is set aside only while the handover says so.
        standing_by = [
            n for n in (s.vol.listdir("/u/handover") or []) if n.endswith(".request")
            and (lambda d: d.get("step") == "H6" and d.get("outcome") is None)(s.vol.read(f"handover/{n}") or {})
        ]  # fmt: skip
        if standing_by:
            parked = [c for c in upd if s.name(c).endswith("-previous")]
            if parked:
                print(f"   {[s.name(c) for c in parked]} standing by (H6, {standing_by[0]}); set aside")
            upd = [c for c in upd if c not in parked]
        beat = s.vol.read("updater.json") or {}
        r.check(
            beat.get("role") == "current"
            and beat.get("image_digest") == pin["SPENDTRACKER_UPDATER_IMAGE"].split("@", 1)[1],
            f"the heartbeat is the pin's updater, current ({beat.get('role')}, {str(beat.get('image_digest'))[:19]})",
        )
        r.check(len(apps) == 1, f"exactly one app container runs: {[s.name(c) for c in apps]}")
        r.check(len(upd) == 1, f"exactly one updater container runs: {[s.name(c) for c in upd]}")
        app_id = s.image_id(f"{APP_REPO}@{pin['SPENDTRACKER_IMAGE'].split('@', 1)[1]}")
        upd_id = s.image_id(f"{UPDATER_REPO}@{pin['SPENDTRACKER_UPDATER_IMAGE'].split('@', 1)[1]}")
        r.check(
            all(s.engine.inspect(c["Id"])["Image"] == app_id for c in apps), "the app runs the pin's digest"
        )
        r.check(
            all(s.engine.inspect(c["Id"])["Image"] == upd_id for c in upd),
            "the updater runs the pin's digest",
        )
        r.check(app_ref[1].startswith(B), f"the pin is B's ({pin['SPENDTRACKER_IMAGE']})")
        health = s.wait_for(lambda: (lambda h: h if h.get("version") else None)(s.health()), "health", 120)
        r.check(health.get("version") == B, f"health says {health.get('version')}")

    # ---- the rest, each on a fresh stack -------------------------------- #

    def E2(self, r: Result) -> None:
        """A -> C over B4: both releases' migrations, in order."""
        s = self.s
        s.up(A)
        b = before(s)
        req, record = s.update(A, C)
        check_updated(r, s, b, req, record, C, updater_to=C)
        chain_a, chain_c = s.releases[A]["chain"], s.releases[C]["chain"]
        want = chain_c[len(chain_a) :]
        log = "\n".join(str(x) for x in drill(s, req["id"]).get("log") or [])
        ran = re.findall(r"Running upgrade (\w*) -> (\w+)", log)
        r.check(
            [to for _, to in ran] == want, f"the migrations ran in order: {[to for _, to in ran]} == {want}"
        )
        b4 = s.releases[B4]["chain"][-1]
        r.check(b4 in want and want[-1] == s.head(C), f"B4's migration {b4} and C's {s.head(C)} both ran")
        tables = s.vol.ledger()["counts"]
        r.check("ci_release_b4" in tables and "ci_release_c" in tables, "both releases' tables exist")

    def E3(self, r: Result) -> None:
        """Rollback on a failed migration that committed a sentinel row first."""
        s = self.s
        s.up(A)
        b = before(s)
        req, record = s.update(A, B1)
        check_rolled_back(r, s, b, req, record, A)
        rows = s.vol.query("SELECT count(*) FROM login_attempts WHERE email_canonical = ?", [s.sentinel])
        r.check(rows == [[0]], f"the sentinel row is absent: {rows}")
        aside = restored_aside(s)
        r.check(bool(aside), f"what the restore replaced is kept beside it: {aside}")

    def E4(self, r: Result) -> None:
        """Rollback on health 500: B2's new column is absent."""
        s = self.s
        s.up(A)
        b = before(s)
        req, record = s.update(A, B2)
        check_rolled_back(r, s, b, req, record, A)
        table, column = s.probe_column
        r.check(column not in s.vol.columns(table), f"{table}.{column}, which B2 added, is absent")
        r.check(record.get("failed_step") == "8", f"it failed at health, step {record.get('failed_step')}")

    def interrupted(
        self, r: Result, act: Callable[[str], None], what: str, engine_restarted: bool = False
    ) -> None:
        """E5 and E6: interrupt the drill, then E1 or E3 holds, and the drill ran once."""
        s = self.s
        s.up(A)
        b = before(s)
        req, record = s.update(A, B, during=act)
        state = record.get("state")
        print(f"   after {what}: {state}")
        if state == "succeeded":
            check_updated(r, s, b, req, record, B, updater_to=None, engine_restarted=engine_restarted)
        else:
            check_rolled_back(r, s, b, req, record, A, engine_restarted=engine_restarted)
        made = sorted(set(s.vol.backups()) - set(b.backups))
        if made or state == "succeeded":
            r.check(len(made) == 1, f"the drill ran once: one backup folder for the request {made}")
        else:
            # The interruption took the drill before it had backed up (5.6,
            # "5, no drill report", no newer backup): R3, nothing migrated,
            # and the drill was not run again to make one.
            r.check(
                not record.get("backup") and "nothing was migrated" in str(record.get("sentence")),
                f"the drill was taken before its backup: rolled back, nothing migrated, not run again "
                f"({record.get('sentence')})",
            )

    def E5(self, r: Result) -> None:
        s = self.s

        def kill(rid: str) -> None:
            s.wait_step(rid, ("5",))
            target = self.owner_container(rid)
            print(f"   killing {s.name(target)} (the apply's owner) at step 5")
            s.engine.kill(target["Id"])
            time.sleep(3)
            print(f"   starting {s.name(target)} again")
            s.engine.start(target["Id"])

        self.interrupted(r, kill, "the updater was killed and started again")

    def owner_container(self, rid: str) -> dict:
        """The running updater container that owns the apply, by its image (a handover renames)."""
        s = self.s
        owner = (s.journal(rid).get("owner") or {}).get("image_digest")
        ref = f"{UPDATER_REPO}@{owner}"
        return next(
            c for c in s.project_containers()
            if c.get("State") == "running" and s.engine.inspect(c["Id"])["Image"] == s.image_id(ref)
        )  # fmt: skip

    def E6(self, r: Result) -> None:
        s = self.s
        assert self.leg.restart is not None

        def restart(rid: str) -> None:
            s.wait_step(rid, ("5",))
            print(f"   restarting the engine at step 5: {' '.join(self.leg.restart)}")
            import subprocess

            subprocess.run(self.leg.restart, check=True)
            s.engine.wait_answering()

        self.interrupted(r, restart, "the engine restarted", engine_restarted=True)

    def E7(self, r: Result) -> None:
        """`-previous` started by hand -- Docker Desktop's Start button -- twice, and E1 holds.

        **Once as soon as it is parked** (step 3). Before #169's fix it held the
        port (or, in the sidecar layout, 8848 in the shared namespace) when the
        new app needed it, and `unless-stopped` kept it coming back. Step 3 now
        parks it with restart policy `no`, and since #246 the check before
        step 5 stops it before the drill starts; steps 7 and 8 would stop it
        once and start the new app again.

        **Once while the drill runs** (#246): the old app against the ledger
        the drill is backing up and migrating. The drill's poll stops it at
        once. The drill is created only after the check before step 5, so
        clicking once it exists reaches the poll's check and no other.

        Both clicks are made with the apply's owner paused -- and the second
        with the drill paused too, when it still runs -- so the person is
        always quicker than the updater, as the scenario needs: a person, not
        a loop racing the updater."""
        s = self.s
        s.up(A)
        b = before(s)
        seen: dict = {"clicks": 0, "refused": [], "ran": False, "drill_ran": False, "drill_paused": False}

        def press(since: str) -> bool:
            """Start pressed on -previous; True once it has started since `since`."""
            seen["clicks"] += 1
            with contextlib.suppress(api.Unreachable):
                try:
                    # By id: the same container, whether the rename has happened yet or not.
                    s.engine.start(b.app["Id"])
                except api.Failed as e:
                    seen["refused"].append(str(e).split("/start: ", 1)[-1][:200])
            with contextlib.suppress(api.Failed, api.Unreachable):
                return s.engine.inspect(b.app["Id"])["State"].get("StartedAt") != since
            return False

        def started_at() -> str:
            return str(s.engine.inspect(b.app["Id"])["State"].get("StartedAt"))

        def stopped() -> bool:
            return not s.engine.inspect(b.app["Id"])["State"].get("Running")

        def click(rid: str) -> None:
            s.wait_step(rid, ("3",))
            owner = self.owner_container(rid)["Id"]
            s.wait_for(stopped, "step 3 to stop the app", 120, every=0.02)
            # The person is quicker than the updater: the apply's owner is
            # paused the moment the app has stopped, for the click, so the
            # maintenance page has not taken the port yet. Clicking only once
            # `-previous` appeared landed behind the page most of the time.
            since = b.app["State"]["StartedAt"]
            s.engine.pause(owner)
            try:
                # Podman can refuse a start for a moment after a stop, while it
                # cleans the container up: the updater waits, so clicking again
                # costs nothing.
                for _ in range(30):
                    seen["ran"] = press(since)
                    if seen["ran"]:
                        break
                    time.sleep(0.1)
            finally:
                s.engine.unpause(owner)
            while not seen["ran"] and s.journal(rid).get("step") in ("3", "4"):
                time.sleep(0.5)
                seen["ran"] = press(since)
            click_during_the_drill(rid)

        def the_drill(rid: str) -> dict | None:
            for c in s.engine.containers(f"{REQUEST_LABEL}={rid}"):
                if (c.get("Labels") or {}).get(ROLE_LABEL) == "drill":
                    return c
            return None

        def click_during_the_drill(rid: str) -> None:
            # Not `wait_for`, which waits out a Failure: an apply that ended
            # before its drill fails the scenario now, with its record dumped.
            deadline, looked, drill_c = time.monotonic() + 600, 0, None
            while drill_c is None:
                with contextlib.suppress(api.Unreachable, api.Failed):
                    drill_c = the_drill(rid)
                looked += 1
                if drill_c is None and looked % 50 == 0 and s.vol.exists(f"/u/history/{rid}.json"):
                    raise Failure("the apply ended before its drill was created")
                if drill_c is None and time.monotonic() > deadline:
                    raise Failure("timed out after 600 s waiting for the drill to be created")
                if drill_c is None:
                    time.sleep(0.02)
            owner = self.owner_container(rid)["Id"]
            # The drill first, while it still runs: the click then lands while
            # the drill holds the ledger, however quick the drill is.
            with contextlib.suppress(api.Failed, api.Unreachable):
                s.engine.pause(drill_c["Id"])
                seen["drill_paused"] = True
            s.engine.pause(owner)
            try:
                s.wait_for(stopped, "-previous to be stopped before the second click", 60, every=0.1)
                since = started_at()
                for _ in range(30):
                    seen["drill_ran"] = press(since)
                    if seen["drill_ran"]:
                        break
                    time.sleep(0.1)
            finally:
                s.engine.unpause(owner)
            try:
                if seen["drill_ran"] and seen["drill_paused"]:
                    # The updater's next poll stops it; the drill is still frozen.
                    s.wait_for(stopped, "the updater to stop -previous during the drill", 120, every=0.1)
            finally:
                if seen["drill_paused"]:
                    with contextlib.suppress(api.Failed, api.Unreachable):
                        s.engine.unpause(drill_c["Id"])

        req, record = s.update(A, B, during=click)
        print(
            f"   {seen['clicks']} clicks, ran at step 3: {seen['ran']}, during the drill: {seen['drill_ran']} "
            f"(drill paused: {seen['drill_paused']}), refused: {sorted(set(seen['refused']))[:2]}"
        )
        r.check(seen["ran"], f"-previous was started by hand at step 3 ({seen['clicks']} clicks)")
        r.check(seen["drill_ran"], "-previous was started by hand again while the drill ran")
        guard = (s.journal(req["id"]).get("context") or {}).get("ledger_holders_stopped") or []
        print(f"   stopped by the drill's guard: {guard}")
        if seen["drill_paused"]:
            r.check(
                any(h.get("id") == b.app["Id"] and h.get("during_drill") for h in guard),
                "the updater stopped -previous while the drill held the ledger (#246)",
            )
        note = [n for n in record.get("notes") or [] if "started while the update ran" in n]
        print(f"   the updater's notes: {note or '(none: -previous had exited by itself)'}")
        check_updated(r, s, b, req, record, B, updater_to=B)

    def E8(self, r: Result) -> None:
        """Corrupt backup and failed health -> needs_recovery; recovery over HTTP through the page."""
        s = self.s
        s.up(A)
        print("   an earlier update that rolled back, for an older verified backup")
        _, first = s.update(A, B1)
        r.check(
            first.get("state") == "rolled_back", f"(setup) A -> B1 rolled back: {first.get('sentence')}"
        )
        older = str(first.get("backup") or "")
        stamp = older.rstrip("/").rsplit("/", 1)[-1]
        manifest = s.vol.read_abs(f"/l/backups/{stamp}/manifest.json")
        s.vol.write(
            "INSERT INTO login_attempts (id, email_canonical, ip, kind, ok, at) "
            "VALUES ('00000000000000000000000000000002', 'ci-after@example.test', NULL, 'password', 0, '2026-01-02 00:00:00')"
        )
        code = new_code()
        corrupted: dict = {}

        def corrupt(rid: str) -> None:
            s.wait_step(rid, ("8",))
            folder = str((s.journal(rid).get("context") or {}).get("backup") or "")
            name = folder.rstrip("/").rsplit("/", 1)[-1]
            path = f"/l/backups/{name}/spendtracker.sqlite3"
            print(f"   corrupting {path} while health fails")
            s.vol.corrupt(path)
            corrupted["path"] = path

        _, record = s.update(A, B2, code=code, during=corrupt)
        r.check(
            record.get("state") == "needs_recovery", f"the update needs recovery: {record.get('sentence')}"
        )
        r.check(not s.running("app"), "no app container runs")
        r.check(s.vol.exists(LEDGER) and s.vol.exists(corrupted.get("path", "/nonexistent")),
                "the ledger and the corrupt backup both still exist")  # fmt: skip
        page = s.wait_for(lambda: (lambda a: a if a["status"] == 200 else None)(s.http("GET", "/recovery")),
                          "the recovery page", 120)  # fmt: skip
        r.check("Recovery code" in page["body"], "the maintenance page offers recovery")
        wrong = new_code()
        answer = s.http("POST", "/recovery/open", form={"code": wrong})
        cookies = [v for k, v in answer["headers"] if k.lower() == "set-cookie"]
        attempts = s.vol.read("recovery/attempts.json") or {}
        r.check(
            answer["status"] == 200 and not cookies,
            f"a wrong code is refused ({answer['status']}, no session)",
        )
        r.check(attempts.get("wrong") == 1, f"and counted: {attempts}")
        answer = s.http("POST", "/recovery/open", form={"code": code.lower()})
        cookies = [v for k, v in answer["headers"] if k.lower() == "set-cookie"]
        r.check(
            answer["status"] == 303 and bool(cookies), f"the right code opens recovery ({answer['status']})"
        )
        if not cookies:
            return
        cookie = cookies[0].split(";", 1)[0]
        dash = s.http("GET", "/recovery", cookie=cookie)
        csrf = re.search(r'name=csrf value="([^"]+)"', dash["body"])
        offered = re.findall(r'name=backup value="([0-9]{8}-[0-9]{6})"', dash["body"])
        r.check(stamp in offered, f"the older backup {stamp} is offered: {sorted(set(offered))}")
        if not csrf:
            r.check(False, "the dashboard carries its form token")
            return
        act = s.http(
            "POST",
            "/recovery/act",
            form={"csrf": csrf.group(1), "kind": "restore_backup", "backup": stamp},
            cookie=cookie,
        )
        r.check(act["status"] == 202, f"restoring that backup is accepted ({act['status']})")
        health = s.wait_for(
            lambda: (lambda h: h if h.get("version") == A else None)(s.health()),
            "A to answer",
            600,
            every=3,
        )
        r.check(health.get("version") == A, "A answers again")
        now = s.vol.ledger()
        want = (manifest or {}).get("rows") or {}
        differ = {t: (n, now["counts"].get(t)) for t, n in want.items() if now["counts"].get(t) != n}
        r.check(bool(want) and not differ, f"the ledger has that backup's counts {differ}")
        r.check(now["stamp"] == s.head(A), f"stamped at A's head ({now['stamp']})")

    def E9(self, r: Result) -> None:
        """Stale and duplicate requests: refused, and the app untouched."""
        s = self.s
        s.up(A)
        app = s.engine.inspect(s.app_name)
        stale = {**s.base("prepare", created=time.time() - 11 * 60), "from_version": A, "to_version": B}
        s.send(stale)
        record = s.outcome(stale["id"], 120)
        r.check(
            record.get("state") == "refused" and record.get("code") == "stale",
            f"a stale request is refused: {record.get('code')}",
        )
        first = {**s.base("prepare"), "from_version": A, "to_version": B}
        s.send(first)
        r.check(s.outcome(first["id"], 600).get("state") == "succeeded", "(setup) a fresh prepare succeeds")
        again = {**first, "created_at": s.base("prepare")["created_at"]}
        s.send(again)
        found = s.wait_for(
            lambda: next(
                (h for h in (s.vol.read(f"history/{n}") for n in s.vol.listdir("/u/history") or [])
                 if h and h.get("request_id") == first["id"]), None),
            "the duplicate's record", 120)  # fmt: skip
        r.check(
            found.get("state") == "refused" and found.get("code") == "duplicate",
            f"a duplicate is refused: {found.get('code')}",
        )
        r.check(found.get("id") != first["id"], "and filed under its own id, not over the original's")
        original = s.vol.read(f"history/{first['id']}.json") or {}
        r.check(original.get("state") == "succeeded", "the original's record is unchanged")
        now = s.engine.inspect(s.app_name)
        r.check(
            now["Id"] == app["Id"] and now["State"]["StartedAt"] == app["State"]["StartedAt"],
            "the app container is the same, never restarted",
        )
        r.check(s.health().get("version") == A, "the app still answers as A")

    def E14(self, r: Result) -> None:
        """Updater only: the updater moves to C's, the app is not touched."""
        s = self.s
        s.up(A)
        app = s.engine.inspect(s.app_name)
        req = {**s.base("update_updater"), "to_version": C}
        s.send(req)
        record = s.outcome(req["id"], 600)
        r.check(record.get("state") == "succeeded", f"update_updater succeeded: {record.get('sentence')}")
        u = updater_now(s)
        r.check(u["Image"] == s.image_id(s.updater_ref(C)), f"{s.updater_name} runs C's updater digest")
        now = s.engine.inspect(s.app_name)
        r.check(now["Id"] == app["Id"], "the app container is the same container")
        r.check(
            now["State"]["StartedAt"] == app["State"]["StartedAt"], "and was never restarted (StartedAt)"
        )
        r.check(s.health().get("version") == A, "the app still answers as A")
        pin = pinned(s)
        r.check(
            pin.get("SPENDTRACKER_UPDATER_IMAGE") == f"{UPDATER_REPO}:{C}@{s.releases[C]['updater']}",
            "the pin names C's updater",
        )
        r.check("SPENDTRACKER_IMAGE" not in pin, f"and pins no app: {pin.get('SPENDTRACKER_IMAGE')}")

    def E15(self, r: Result) -> None:
        """The repair launcher (C5): a pin naming B, a bundle naming C, the launcher run headless.

        The release zip for C is built by `scripts.bundle` and unzipped into a
        new folder beside the install, as an owner would; its Linux launcher
        runs with no terminal. The app must start at the pin's B (the ledger is
        at B), and the updater at C's, the newer of the two."""
        import subprocess
        import zipfile

        s = self.s
        s.up(A)
        _, record = s.update(A, B)
        r.check(record.get("state") == "succeeded", f"(setup) A -> B, which pins B: {record.get('sentence')}")
        out = s.dir.parent / f"{s.dir.name}-zip"
        if out.exists():
            subprocess.run(["rm", "-rf", str(out)], check=True)
        out.mkdir(parents=True)
        subprocess.run(
            [sys.executable, "-m", "scripts.bundle", "--version", C, "--app-digest", s.releases[C]["app"],
             "--updater-digest", s.releases[C]["updater"], "--zip", str(out)],
            cwd=ROOT, check=True,
        )  # fmt: skip
        archive = next(out.glob("*.zip"))
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                path = Path(z.extract(info, out))
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    path.chmod(mode)
        folder = next(p_ for p_ in out.iterdir() if p_.is_dir())
        # Test-only: the CI updater the launcher starts verifies against the job's table.
        (folder / "ci-trust.json").write_text(json.dumps(s.table))
        env = dict(os.environ)
        if self.leg.compose[0].startswith("podman"):
            env.pop("DOCKER_HOST", None)
            # The launcher takes `docker compose` when there is one, and the
            # runner has Docker beside Podman: an owner on rootless Podman has
            # no `docker`, so this leg hides it (first E15 run on this leg
            # started the bundle on the runner's Docker instead).
            shim = s.dir.parent / f"{s.dir.name}-no-docker"
            shim.mkdir(exist_ok=True)
            (shim / "docker").write_text("#!/bin/sh\nexit 127\n")
            (shim / "docker").chmod(0o755)
            env["PATH"] = f"{shim}:{env.get('PATH', '')}"
            # `podman compose` takes Docker's compose plugin over
            # podman-compose when both are installed, as on the runner, and
            # docker-compose refuses the stack podman-compose made ("incorrect
            # label com.docker.compose.network"). The launcher now runs the
            # compose that made the project (#247), so nothing is set here:
            # this leg is that machine with both installed.
            env.pop("PODMAN_COMPOSE_PROVIDER", None)
        else:
            env["DOCKER_HOST"] = f"unix://{self.leg.socket}"
        env["SPENDTRACKER_HEALTH_TIMEOUT"] = "240"
        done = subprocess.run(
            ["sh", str(folder / "start-spend-tracker.sh")], cwd=folder, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=900,
        )  # fmt: skip
        print("   " + (done.stdout + done.stderr).strip().replace("\n", "\n   ")[-4000:])
        r.check(done.returncode == 0, f"the launcher, run headless, exits 0 ({done.returncode})")
        if self.leg.compose[0].startswith("podman"):
            r.check(
                "Using podman-compose, which created this Spend Tracker." in done.stdout,
                "the launcher chose podman-compose, which made the stack (#247)",
            )
        apps = s.running("app")
        upds = s.running("updater")
        r.check(
            len(apps) == 1 and s.engine.inspect(apps[0]["Id"])["Image"] == s.image_id(s.app_ref(B)),
            f"the app runs the pin's B, not the bundle's C: {[s.name(c) for c in apps]}",
        )
        r.check(
            len(upds) == 1 and s.engine.inspect(upds[0]["Id"])["Image"] == s.image_id(s.updater_ref(C)),
            f"the updater runs C's, the newer of the pin's and the bundle's: {[s.name(c) for c in upds]}",
        )
        health = s.health()
        r.check(health.get("version") == B, f"health says {health.get('version')}")
        keys = {k: v for k, _, v in (ln.partition("=") for ln in (folder / ".env").read_text().splitlines())}
        r.check(
            keys.get("SPENDTRACKER_IMAGE", "").endswith(s.releases[B]["app"]),
            "the new folder's .env carries the pin's app forward",
        )


#: Scenarios that fail today for a reason in the updater, not in the test: each
#: still runs and prints every assertion, and its failure does not fail the
#: leg. When one passes, the summary says so -- take it out of here then.
KNOWN_GAPS: dict[str, str] = {}

#: The commit that parks -previous with restart policy `no` (#169).
PARKING = "858eb410e39016630daa5d818cd53ed0bdc6a244"
#: The commit that sends Podman 4 a CMD healthcheck it does not split (#169).
PODMAN4_HEALTHCHECK = "9de23ffb04e9cadc868a7409306a7e4da6a35eb8"


@functools.cache
def contains(revision: str, commit: str) -> bool | None:
    """Whether `revision` has `commit` in its history; None when git cannot say."""
    import subprocess

    try:
        done = subprocess.run(
            # safe.directory: inside an engine box the checkout is another uid's.
            ["git", "-c", "safe.directory=*", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, revision],
            capture_output=True,
        )
    except OSError:
        return None
    return {0: True, 1: False}.get(done.returncode)

SCENARIOS = {
    # One stack: E1, then what reads the state E1 left.
    "E1": "A -> B",
    "E10": "no listener in the updater's namespace",
    "E11": "the handover",
    "E13": "updater first (C1)",
    "E12": "compose up -d again, with the parked containers present",
    "E16": "the sidecar restarts: the app rejoins it (#275)",
    "E11b": "E11 and E13 with a successor that fails its self-check",
    "E2": "skip: A -> C over B4",
    "E3": "rollback: a migration fails after a sentinel row",
    "E4": "rollback: health answers 500",
    "E5": "the updater killed mid-drill, started again",
    "E6": "the engine restarted mid-drill",
    "E7": "-previous started by hand at step 3 and during the drill",
    "E8": "corrupt backup, failed health: recovery over HTTP",
    "E9": "stale and duplicate requests",
    "E14": "updater only (C2)",
    "E15": "the repair launcher (C5)",
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.self_update.scenarios")
    p.add_argument("--staged", type=Path, required=True)
    p.add_argument("--project-dir", type=Path, required=True)
    p.add_argument("--leg", required=True)
    p.add_argument("--socket", required=True)
    p.add_argument("--compose", default="docker compose")
    p.add_argument("--layout", default="loopback", choices=("loopback", "sidecar"))
    p.add_argument("--restart", default="")
    p.add_argument("--updater-user", default="65532:65532")
    p.add_argument("--socket-gid", default="0")
    p.add_argument("--only", default="")
    p.add_argument("--skip", action="append", default=[], help='E6="why this leg cannot"')
    p.add_argument("--report", type=Path, default=None, help="write the results as JSON here")
    p.add_argument("--version-out", type=Path, default=None, help="write the engine's GET /version here")
    args = p.parse_args(argv)

    skips = dict(item.split("=", 1) for item in args.skip)
    leg = Leg(
        name=args.leg,
        socket=args.socket,
        compose=shlex.split(args.compose),
        restart=shlex.split(args.restart) or None,
        updater_user=args.updater_user,
        socket_gid=args.socket_gid,
        layout=args.layout,
        skips=skips,
    )
    if leg.layout != "sidecar":
        leg.skips.setdefault("E16", "only the sidecar layout has a sidecar to restart")
    if leg.layout == "sidecar":
        leg.skips.setdefault("E15", "the release zip is a personal computer's loopback layout, not a server's sidecar")
    if leg.restart is None:
        leg.skips.setdefault("E6", "no way to restart this engine was given")
    engine = api.Engine(args.socket)
    engine.wait_answering(30)
    version = engine.version()
    if args.version_out:
        args.version_out.write_text(json.dumps(version, indent=2) + "\n")
    print(f"leg {leg.name}, {leg.layout}: {version.get('Platform', {}).get('Name') or version.get('Components', [{}])[0].get('Name')} "
          f"{version.get('Version')}, API {version.get('MinAPIVersion')}-{version.get('ApiVersion')}")  # fmt: skip
    stack = Stack(leg, engine, args.project_dir.resolve(), args.staged.resolve())
    # The engine resolves ghcr.io to the job's registry only once its own
    # resolver has read /etc/hosts again (Go caches it for 5 s): the first
    # compose up once went to the real ghcr.io and was `denied`. Pulling A by
    # digest until it comes is the wait, and saves the first `up` the pull.
    for ref in (stack.app_ref(A), stack.updater_ref(A)):
        Stack.wait_for(lambda ref=ref: engine.pull(ref) is None, f"the registry to serve {ref}", 120, every=3)
    run = Run(stack, leg)
    wanted = [n for n in SCENARIOS if not args.only or n in args.only.split(",")]
    results: list[Result] = []
    for name in wanted:
        r = Result(name, SCENARIOS[name])
        results.append(r)
        print(f"\n== {name}: {r.title}", flush=True)
        if name in leg.skips:
            r.skipped = leg.skips[name]
            print(f"   skipped on {leg.name}: {r.skipped}")
            continue
        started = time.monotonic()
        try:
            getattr(run, name)(r)
        except Exception as e:  # noqa: BLE001 -- a scenario's crash is its failure, the next one still runs
            r.error = f"{type(e).__name__}: {e}"
            print(f"  ERROR {r.error}", flush=True)
        r.seconds = time.monotonic() - started
        if r.skipped:
            print(f"   skipped: {r.skipped}")
        elif not r.passed:
            stack.dump_all()
    with contextlib.suppress(Exception):
        stack.down()

    print(f"\n{'':4}{'scenario':<6} {'result':<10} {'s':>5}  title")
    for r in results:
        extra = f"  -- {r.skipped}" if r.skipped else ""
        print(f"    {r.name:<6} {verdict(r):<10} {r.seconds:5.0f}  {r.title}{extra}")
        if r.name in KNOWN_GAPS:
            print(f"{'':29}known gap: {KNOWN_GAPS[r.name]}")
    if args.report:
        args.report.write_text(json.dumps([
            {"scenario": r.name, "title": r.title, "skipped": r.skipped, "error": r.error, "seconds": round(r.seconds),
             "passed": r.passed, "result": verdict(r), "failed": [w for ok, w in r.checks if not ok]} for r in results], indent=2))  # fmt: skip
    return 0 if all(r.passed or r.skipped or r.name in KNOWN_GAPS for r in results) else 1


def verdict(r: Result) -> str:
    if r.skipped:
        return "skipped"
    if r.name in KNOWN_GAPS:
        return "gap closed" if r.passed else "known gap"
    return "passed" if r.passed else "FAILED"


if __name__ == "__main__":
    sys.exit(main())
