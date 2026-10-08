"""The pre-update hook: the updater's half of the handshake, and the host's runner (design notes 6.5, A3).

The updater's half runs through the real request path against the simulated
installation of `tests/updater_world.py`: a request file, `Service.tick`,
intake, preflight, then step 2. Where a test needs the host to answer, it is
the **real** `deploy/updater/host-hook/hook-runner`, run from the fake clock's
sleep exactly as the path unit would run it between two of the updater's
polls. Edge cases the runner never produces (a symlinked or oversized result)
are planted by hand.

Every failure must leave the update *not started* with nothing touched: the
app container never stopped, renamed or replaced, and the ledger never
drilled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.updater_world import A, B, World
from updater import hook, volume

RUNNER = Path(__file__).resolve().parent.parent / "deploy" / "updater" / "host-hook" / "hook-runner"
FAILED = "Not started: the pre-update hook failed."


@pytest.fixture
def world(tmp_path):
    with World(tmp_path) as w:
        yield w


def touched(w: World, cid: str) -> list:
    """Every mutating call naming a container."""
    return [c for c in w.fake.calls if c.method != "GET" and cid in c.bare]


def steps(w: World, rid: str) -> list[str]:
    return [s["step"] for s in w.journal(rid).started]


def nothing_changed(w: World) -> None:
    app = w.by_name(w.app_name)
    assert app is not None and app["Id"] == w.app_id and app["State"] == "running"
    assert touched(w, w.app_id) == []
    assert w.ledger.stamp == A and w.ledger.drills == 0


def command(tmp_path: Path, body: str, name: str = "pre-update-hook") -> tuple[Path, Path]:
    """A hook command, and the file it appends one line to each time it runs."""
    marker = tmp_path / f"{name}.ran"
    path = tmp_path / name
    path.write_text(
        "#!/bin/sh\n"
        f'echo "$SPENDTRACKER_HOOK_FROM $SPENDTRACKER_HOOK_TO $SPENDTRACKER_HOOK_ID" >> "{marker}"\n' + body
    )
    os.chmod(path, 0o700)
    return path, marker


def run_runner(hook_dir: Path, cmd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(RUNNER), "--dir", str(hook_dir), "--command", str(cmd)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def hook_dir(tmp_path: Path, config: dict | None = None) -> Path:
    d = tmp_path / "hook"
    d.mkdir()
    if config is not None:
        (d / "hook.json").write_text(json.dumps(config))
    return d


def apply_with(w: World, hdir: Path | None, on_sleep=None) -> tuple[dict, dict]:
    """Prepare, then apply with the hook directory mounted; `on_sleep(rid)` runs between polls."""
    service = w.service(hook_dir=hdir)
    service.startup()
    report = w.prepared(service)
    req = w.apply_request(report)
    if on_sleep is not None:
        w.time.on_sleep = lambda: on_sleep(req["id"])
    w.write_request(req)
    service.tick()
    w.time.on_sleep = None
    record = w.history(req["id"])
    assert record is not None
    return req, record


def path_unit(hdir: Path, cmd: Path):
    """What the systemd path unit does: run the runner whenever a request is waiting."""

    def fire(rid: str) -> None:
        if (hdir / f"{rid}.request").exists():
            run_runner(hdir, cmd)

    return fire


# --------------------------------------------------------------------------- #
# Not configured: skipped, the update proceeds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mounted", [False, True], ids=["no-mount", "mount-without-hook.json"])
def test_no_hook_configured_is_skipped_and_the_update_runs(world, tmp_path, mounted):
    hdir = hook_dir(tmp_path) if mounted else None
    req, record = apply_with(world, hdir)

    assert record["state"] == "succeeded"
    assert world.ledger.stamp == B and world.ledger.drills == 1
    assert "2" not in steps(world, req["id"])
    assert world.journal(req["id"]).context["hook"] == "skipped"
    if hdir is not None:
        # Nothing was asked of a host that never configured a hook.
        assert sorted(p.name for p in hdir.iterdir()) == []


# --------------------------------------------------------------------------- #
# Through the real runner
# --------------------------------------------------------------------------- #


def test_a_hook_that_succeeds_runs_once_with_the_versions_and_the_update_proceeds(world, tmp_path):
    hdir = hook_dir(tmp_path, {"timeout_seconds": 120})
    cmd, marker = command(tmp_path, "echo snapshot spend-pre-0_8_0 taken\n")
    req, record = apply_with(world, hdir, path_unit(hdir, cmd))

    assert record["state"] == "succeeded"
    assert world.ledger.stamp == B and world.ledger.drills == 1
    assert marker.read_text() == f"{A} {B} {req['id']}\n"
    assert "2" in steps(world, req["id"])
    remembered = world.journal(req["id"]).context["hook"]
    assert remembered["ok"] is True and remembered["exit"] == 0
    assert "spend-pre-0_8_0 taken" in remembered["output"]
    assert not (hdir / f"{req['id']}.request").exists() and (hdir / f"{req['id']}.taken").exists()


def test_a_hook_that_exits_non_zero_is_not_started_and_touches_nothing(world, tmp_path):
    hdir = hook_dir(tmp_path, {"timeout_seconds": 120})
    cmd, marker = command(tmp_path, "echo 'snapshot failed: storage local-lvm is full' >&2\nexit 3\n")
    req, record = apply_with(world, hdir, path_unit(hdir, cmd))

    assert record["state"] == "not_started" and record["sentence"] == FAILED
    assert record["failed_step"] == "2"
    nothing_changed(world)
    assert steps(world, req["id"]) == ["0", "1", "2"]
    # The reason and the hook's output tail are in the history.
    (note,) = record["notes"]
    assert note.startswith("The pre-update hook exited with status 3.")
    assert "snapshot failed: storage local-lvm is full" in note
    assert marker.read_text().count("\n") == 1


# --------------------------------------------------------------------------- #
# No result: a timeout, or no runner at all
# --------------------------------------------------------------------------- #


def take_only(hdir: Path):
    """A runner that picks the request up and never finishes."""

    def fire(rid: str) -> None:
        request = hdir / f"{rid}.request"
        if request.exists():
            os.link(request, hdir / f"{rid}.taken")
            request.unlink()

    return fire


def test_a_hook_that_runs_past_its_timeout_is_not_started(world, tmp_path):
    hdir = hook_dir(tmp_path, {"timeout_seconds": 45})
    started = {}

    def fire(rid: str) -> None:
        if (hdir / f"{rid}.request").exists():
            started["t"] = world.time.monotonic()
        take_only(hdir)(rid)

    req, record = apply_with(world, hdir, fire)

    assert record["state"] == "not_started" and record["sentence"] == FAILED
    assert record["notes"] == ["The pre-update hook did not finish within 45 seconds."]
    nothing_changed(world)
    # It waited the configured 45 s, not the 300 s default nor the 60 s pickup.
    waited = world.time.monotonic() - started["t"]
    assert 44 <= waited <= 50


def test_a_configured_hook_with_no_runner_is_not_started_after_the_pickup_window(world, tmp_path):
    hdir = hook_dir(tmp_path, {"timeout_seconds": 600})
    polls = []

    def fire(rid: str) -> None:
        if (hdir / f"{rid}.request").exists():
            polls.append(world.time.monotonic())

    req, record = apply_with(world, hdir, fire)

    assert record["state"] == "not_started" and record["sentence"] == FAILED
    (note,) = record["notes"]
    assert note.startswith(f"No hook runner picked the request up within {hook.PICKUP_SECONDS} seconds")
    nothing_changed(world)
    # Gave up after the pickup window, not the 600 s timeout -- and withdrew
    # the request, so a runner enabled later does not act on a dead update.
    assert polls[-1] - polls[0] < hook.PICKUP_SECONDS + 5
    assert not (hdir / f"{req['id']}.request").exists()


@pytest.mark.parametrize("phase", ["before-pickup", "while-running"])
def test_a_gap_during_the_wait_does_not_time_the_hook_out(world, tmp_path, phase):
    """The VM sleeps for two hours mid-wait; only time it was running counts (8.6)."""
    hdir = hook_dir(tmp_path, {"timeout_seconds": 30})
    polls = {"n": 0}

    def fire(rid: str) -> None:
        waiting = (hdir / f"{rid}.request").exists() or (hdir / f"{rid}.taken").exists()
        if not waiting or (hdir / f"{rid}.result").exists():
            return
        polls["n"] += 1
        n = polls["n"]
        if phase == "before-pickup" and n == 2:
            world.time.doze(2 * 3600)
        if n == 5:
            take_only(hdir)(rid)
        if phase == "while-running" and n == 7:
            world.time.doze(2 * 3600)
        if n == 12:
            volume.write_json(hdir / f"{rid}.result", {"id": rid, "exit": 0, "output": "done\n"})

    req, record = apply_with(world, hdir, fire)

    assert record["state"] == "succeeded", record
    assert world.ledger.stamp == B
    assert world.journal(req["id"]).context["hook"]["ok"] is True
    assert record["gap_s"] >= 2 * 3600


# --------------------------------------------------------------------------- #
# A result the updater will not read
# --------------------------------------------------------------------------- #


def plant(hdir: Path, kind: str):
    """A result that says exit 0 -- so only its refusal can stop the update."""

    def fire(rid: str) -> None:
        result = hdir / f"{rid}.result"
        if result.exists() or result.is_symlink():
            return
        good = {"id": rid, "exit": 0, "output": "ok\n"}
        if kind == "symlink":
            elsewhere = hdir.parent / "elsewhere.json"
            elsewhere.write_text(json.dumps(good))
            result.symlink_to(elsewhere)
        elif kind == "oversized":
            result.write_text(json.dumps({**good, "output": "x" * (hook.MAX_RESULT_BYTES + 1)}))
        elif kind == "another-id":
            result.write_text(json.dumps({**good, "id": str(uuid.uuid4())}))
        elif kind == "no-exit":
            result.write_text(json.dumps({"id": rid, "exit": True}))

    return fire


@pytest.mark.parametrize(
    ("kind", "why"),
    [
        ("symlink", "is a symbolic link"),
        ("oversized", f"is larger than {hook.MAX_RESULT_BYTES} bytes"),
        ("another-id", "is about another request"),
        ("no-exit", "has no exit status"),
    ],
)
def test_a_result_the_updater_will_not_read_is_not_started(world, tmp_path, kind, why):
    hdir = hook_dir(tmp_path, {"timeout_seconds": 60})
    req, record = apply_with(world, hdir, plant(hdir, kind))

    assert record["state"] == "not_started" and record["sentence"] == FAILED
    (note,) = record["notes"]
    assert note.startswith("The pre-update hook's result was refused:") and why in note
    nothing_changed(world)


@pytest.mark.parametrize(
    ("config", "why"),
    [
        ("{not json", "hook.json is not JSON."),
        ('{"timeout_seconds": "soon"}', "timeout_seconds in hook.json is not a whole number"),
        ('{"timeout_seconds": 86400}', "timeout_seconds in hook.json is not a whole number"),
    ],
)
def test_a_configured_but_broken_hook_is_not_started_and_asks_nothing(world, tmp_path, config, why):
    hdir = hook_dir(tmp_path)
    (hdir / "hook.json").write_text(config)
    req, record = apply_with(world, hdir)

    assert record["state"] == "not_started" and record["sentence"] == FAILED
    (note,) = record["notes"]
    assert why in note
    nothing_changed(world)
    assert not (hdir / f"{req['id']}.request").exists()


def test_a_symlinked_hook_json_is_a_broken_configuration_not_none(tmp_path):
    hdir = hook_dir(tmp_path)
    real = tmp_path / "real.json"
    real.write_text('{"timeout_seconds": 10}')
    (hdir / "hook.json").symlink_to(real)
    config = hook.configured(hdir)
    assert config is not None and config.problem is not None and "symbolic link" in config.problem


def test_the_request_names_the_versions_and_replaces_a_planted_symlink(world, tmp_path):
    hdir = hook_dir(tmp_path, {"timeout_seconds": 5})
    rid = str(uuid.uuid4())
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    (hdir / f"{rid}.request").symlink_to(victim)

    def answer(_seconds: float) -> None:
        volume.write_json(hdir / f"{rid}.result", {"id": rid, "exit": 0, "output": ""})

    outcome = hook.run(hdir, hook.Config(timeout=5), rid, A, B, clock=world.clock, sleep=answer)

    assert outcome.ok and outcome.reason == "succeeded"
    assert victim.read_text() == "untouched\n"
    assert not (hdir / f"{rid}.request").is_symlink()
    assert json.loads((hdir / f"{rid}.request").read_text()) == {"id": rid, "from": A, "to": B}


# --------------------------------------------------------------------------- #
# The runner on its own
# --------------------------------------------------------------------------- #


def request(hdir: Path, rid: str, frm: str = A, to: str = B) -> None:
    (hdir / f"{rid}.request").write_text(json.dumps({"id": rid, "from": frm, "to": to}))


def result_of(hdir: Path, rid: str) -> dict:
    return json.loads((hdir / f"{rid}.result").read_text())


def test_the_runner_runs_the_command_with_the_versions_and_writes_its_result(tmp_path):
    hdir = hook_dir(tmp_path)
    cmd, marker = command(tmp_path, "echo hello from the host\nexit 0\n")
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    request(hdir, first, A, B)
    request(hdir, second, B, "0.9.0")

    run_runner(hdir, cmd)

    assert sorted(marker.read_text().splitlines()) == sorted([f"{A} {B} {first}", f"{B} 0.9.0 {second}"])
    for rid in (first, second):
        res = result_of(hdir, rid)
        assert res["exit"] == 0 and res["output"] == "hello from the host\n" and res["id"] == rid
        assert not (hdir / f"{rid}.request").exists() and (hdir / f"{rid}.taken").exists()


@pytest.mark.parametrize(
    "bad",
    ["0.8.0; touch /tmp/owned", "１.２.３", "0.8", "v0.8.0", 8],
    ids=["shell", "fullwidth-digits", "two-parts", "prefixed", "not-a-string"],
)
def test_the_runner_refuses_a_malformed_version_without_running_anything(tmp_path, bad):
    hdir = hook_dir(tmp_path)
    cmd, marker = command(tmp_path, "exit 0\n")
    rid = str(uuid.uuid4())
    (hdir / f"{rid}.request").write_text(json.dumps({"id": rid, "from": A, "to": bad}))

    run_runner(hdir, cmd)

    res = result_of(hdir, rid)
    assert res["exit"] != 0 and "'to' is not a version" in res["refused"]
    assert not marker.exists()


def test_the_runner_never_runs_one_id_twice(tmp_path):
    hdir = hook_dir(tmp_path)
    cmd, marker = command(tmp_path, "exit 0\n")
    rid, other = str(uuid.uuid4()), str(uuid.uuid4())
    request(hdir, rid)
    run_runner(hdir, cmd)
    first = result_of(hdir, rid)

    # The same id again (a replay, or a stray copy), next to a fresh one.
    request(hdir, rid, B, "0.9.0")
    request(hdir, other)
    run_runner(hdir, cmd)

    assert marker.read_text().splitlines() == [f"{A} {B} {rid}", f"{A} {B} {other}"]
    assert result_of(hdir, rid) == first
    # Cleared, so the path unit does not trigger again on it.
    assert sorted(p.name for p in hdir.glob("*.request")) == []


def test_the_runner_skips_an_id_already_taken_even_without_a_result(tmp_path):
    """A runner that died mid-command left `<id>.taken`; a second run must not snapshot again."""
    hdir = hook_dir(tmp_path)
    cmd, marker = command(tmp_path, "exit 0\n")
    rid = str(uuid.uuid4())
    (hdir / f"{rid}.taken").write_text("{}")
    request(hdir, rid)

    run_runner(hdir, cmd)

    assert not marker.exists() and not (hdir / f"{rid}.result").exists()
    assert not (hdir / f"{rid}.request").exists()


@pytest.mark.parametrize("unsafe", ["group-writable", "symlink", "missing"])
def test_the_runner_refuses_an_unsafe_command_and_says_so(tmp_path, unsafe):
    hdir = hook_dir(tmp_path)
    cmd, marker = command(tmp_path, "exit 0\n")
    if unsafe == "group-writable":
        os.chmod(cmd, 0o770)
    elif unsafe == "symlink":
        link = tmp_path / "linked-hook"
        link.symlink_to(cmd)
        cmd = link
    else:
        cmd = tmp_path / "nothing-here"
    rid = str(uuid.uuid4())
    request(hdir, rid)

    run_runner(hdir, cmd)

    res = result_of(hdir, rid)
    assert res["exit"] == 125 and res["refused"]
    assert not marker.exists()


def test_the_runner_refuses_a_symlinked_request_and_one_whose_id_disagrees(tmp_path):
    hdir = hook_dir(tmp_path)
    cmd, marker = command(tmp_path, "exit 0\n")
    linked, liar = str(uuid.uuid4()), str(uuid.uuid4())
    target = tmp_path / "planted.json"
    target.write_text(json.dumps({"id": linked, "from": A, "to": B}))
    (hdir / f"{linked}.request").symlink_to(target)
    (hdir / f"{liar}.request").write_text(json.dumps({"id": str(uuid.uuid4()), "from": A, "to": B}))
    (hdir / "not-an-id.request").write_text("{}")

    run_runner(hdir, cmd)

    assert "symbolic link" in result_of(hdir, linked)["refused"]
    assert "does not match" in result_of(hdir, liar)["refused"]
    assert not marker.exists()
    assert sorted(p.name for p in hdir.glob("*.request")) == []


def test_the_runner_stops_a_command_at_the_configured_timeout(tmp_path):
    hdir = hook_dir(tmp_path, {"timeout_seconds": 1})
    cmd, _ = command(tmp_path, "echo starting\nsleep 30\necho never\n")
    rid = str(uuid.uuid4())
    request(hdir, rid)

    run_runner(hdir, cmd)

    res = result_of(hdir, rid)
    assert res["exit"] == 124
    assert "starting" in res["output"] and "never" not in res["output"]
    assert "stopped after 1 seconds" in res["output"]
    assert res["finished_at"] - res["started_at"] < 15


def test_the_runner_keeps_only_the_last_4_kib_of_output(tmp_path):
    hdir = hook_dir(tmp_path)
    cmd, _ = command(tmp_path, "i=0; while [ $i -lt 2000 ]; do echo line-$i; i=$((i+1)); done\nexit 7\n")
    rid = str(uuid.uuid4())
    request(hdir, rid)

    run_runner(hdir, cmd)

    res = result_of(hdir, rid)
    assert res["exit"] == 7
    assert len(res["output"].encode()) <= hook.MAX_OUTPUT_BYTES
    assert res["output"].endswith("line-1999\n") and "line-0\n" not in res["output"]
