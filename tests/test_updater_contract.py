"""The request contract and how a request is taken (design notes 5.2-5.4, 15.1).

U1  every refusal writes a `refused` history record with its one sentence
    and makes **zero engine calls**. Each test sends two requests, one
    refused and one valid, and the valid one goes through.
U13 protocol windows: an updater whose window is 1-2 accepts protocol-1 and
    protocol-2 requests; a protocol-1 reader takes a status file with unknown
    keys and an unknown state as `running`.

The engine is a real fake on a socket. Detection talks to it *before* a
request is read -- that is how the updater knows what is running -- and its
call log is cleared there, so every assertion of "no call" covers exactly
the taking, validating and refusing.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace

import pytest

from tests.updater_fake_engine import FakeEngine, Running, engine_fixture
from updater import contract, intake, journal, volume
from updater.contract import Context, RunningApp
from updater.engine import EngineClient, Scope
from updater.volume import Volume

NOW = 1_792_000_000.0  # 2026-10-15, give or take
ME = os.getuid()
PROJECT = "spend-tracker"
DIGEST = "sha256:" + "a" * 64
UPDATER_DIGEST = "sha256:" + "b" * 64
GOOD_HASH = "scrypt$ln=15,r=8,p=1$" + "c2FsdHNhbHRzYWx0c2FsdA" + "$" + "A" * 43
OWNER = journal.Owner(UPDATER_DIGEST, "0.7.1", "spend-tracker-updater-1")


def at(offset: float = 0.0) -> str:
    return contract.iso(NOW + offset)


@pytest.fixture
def world(tmp_path):
    fake = FakeEngine(engine_fixture("podman-machine"), engine_fixture("podman-machine", "info"))
    fake.add_container(
        "spend-tracker-app-1",
        PROJECT,
        labels={
            "org.opencontainers.image.version": "v0.7.1",
            "org.opencontainers.image.revision": "987afefca1d1d372df676e338a3d1c43e9a62552",
        },
    )
    fake.add_container("other-app-1", "another-project", labels={"org.opencontainers.image.version": "9.9.9"})
    with Running(fake) as running:
        client = EngineClient(running.socket_path, Scope(project=PROJECT))
        client.negotiate()
        labels = client.inspect("spend-tracker-app-1")["Labels"]
        app = RunningApp(
            version=contract.label_version(labels["org.opencontainers.image.version"]),
            revision=labels["org.opencontainers.image.revision"][:12],
            published=True,
        )
        ctx = Context(app=app, updater_version="0.7.1")
        vol = Volume(tmp_path / "update")
        vol.init()
        fake.calls.clear()
        yield fake, vol, ctx


def send(vol: Volume, doc: object) -> None:
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    vol.request.write_bytes(raw)


def take(vol: Volume, ctx: Context) -> intake.Outcome:
    out = intake.take(vol, ctx, NOW, owner_uid=ME, me=OWNER)
    assert out is not None
    return out


def report(vol: Volume, **over) -> str:
    rid = str(uuid.uuid4())
    body = contract.PreparedReport(
        id=rid,
        from_version="0.7.1",
        to_version="0.9.0",
        digest=DIGEST,
        updater_digest=UPDATER_DIGEST,
        expires_at=at(24 * 3600),
        database_stamp="2de003489b79",
        pending=(
            {"revision": "b2c3d4e5f6a1", "reversible": "lossy", "title": "drop a thing", "note": ""},
            {"revision": "c3d4e5f6a1b2", "reversible": "clean", "title": "add a thing", "note": ""},
            {"revision": "d4e5f6a1b2c3", "reversible": "undeclared", "title": "old one", "note": ""},
        ),
    ).to_dict()
    body.update(over)
    volume.write_json(vol.prepared(rid), body)
    return rid


def valid(kind: str, vol: Volume, **over) -> dict:
    base = {"protocol": 1, "id": str(uuid.uuid4()), "kind": kind, "created_at": at(-5)}
    if kind != "ping":
        base["requested_by"] = "usr_two"
    if kind in ("prepare", "apply"):
        base.update(from_version="0.7.1", to_version="0.9.0")
    if kind == "update_updater":
        base["to_version"] = "0.8.0"
    if kind in ("apply", "discard"):
        base["prepared_id"] = report(vol)
    if kind == "apply":
        base.update(
            digest=DIGEST,
            updater_digest=UPDATER_DIGEST,
            accepted_lossy=["d4e5f6a1b2c3", "b2c3d4e5f6a1"],
            recovery_hash=GOOD_HASH,
        )
    base.update(over)
    return base


def refused_then_valid(fake, vol, ctx, bad, kind, code, *, good_ctx=None):
    status_before = vol.status.read_bytes() if vol.status.exists() else None
    send(vol, bad)
    out = take(vol, ctx)
    assert not out.accepted and out.refusal.code == code, out.refusal and out.refusal.sentence
    record = json.loads(vol.history(out.record_id).read_text())
    assert record["state"] == "refused"
    assert record["sentence"] == out.refusal.sentence and record["sentence"].endswith(".")
    assert record["code"] == code
    assert vol.taken(out.record_id).exists() and not vol.request.exists()
    status_after = vol.status.read_bytes() if vol.status.exists() else None
    assert status_after == status_before, "a refusal must not touch the status of whatever is running"

    good = valid(kind, vol)
    send(vol, good)
    ok = take(vol, good_ctx or ctx)
    assert ok.accepted, ok.refusal and ok.refusal.sentence
    assert ok.record_id == good["id"]
    assert fake.calls == []
    return record


# --------------------------------------------------------------------------- #
# U1
# --------------------------------------------------------------------------- #

SHAPE_REFUSALS = {
    "a list": (lambda v: [1, 2], "prepare", "not_json"),
    "protocol 2": (lambda v: valid("prepare", v, protocol=2), "prepare", "protocol"),
    "protocol as text": (lambda v: valid("prepare", v, protocol="1"), "prepare", "protocol"),
    "protocol as true": (lambda v: valid("prepare", v, protocol=True), "prepare", "protocol"),
    "an unknown kind": (lambda v: {**valid("prepare", v), "kind": "build"}, "prepare", "kind"),
    "an image key": (lambda v: valid("prepare", v, image="docker.io/evil"), "prepare", "keys"),
    "a path key": (lambda v: valid("discard", v, path="/etc/passwd"), "discard", "keys"),
    "a missing key": (
        lambda v: {k: x for k, x in valid("prepare", v).items() if k != "to_version"},
        "prepare",
        "keys",
    ),
    "a uuid1 id": (lambda v: valid("prepare", v, id=str(uuid.uuid1())), "prepare", "id"),
    "an uppercase id": (lambda v: valid("prepare", v, id=str(uuid.uuid4()).upper()), "prepare", "id"),
    "stale": (lambda v: valid("prepare", v, created_at=at(-11 * 60)), "prepare", "stale"),
    "from the future": (lambda v: valid("prepare", v, created_at=at(120)), "prepare", "future"),
    "a local time": (lambda v: valid("prepare", v, created_at="2026-10-20 21:11:02"), "prepare", "created_at"),
    "a long requested_by": (lambda v: valid("prepare", v, requested_by="u" * 200), "prepare", "requested_by"),
    "a prerelease": (lambda v: valid("prepare", v, to_version="0.9.0-rc1"), "prepare", "version"),
    "other digits": (lambda v: valid("prepare", v, to_version="٠.9.0"), "prepare", "version"),
    "a number version": (lambda v: valid("prepare", v, to_version=9), "prepare", "version"),
    "not what runs": (lambda v: valid("prepare", v, from_version="0.7.0"), "prepare", "from_mismatch"),
    "the same version": (lambda v: valid("prepare", v, to_version="0.7.1"), "prepare", "not_newer"),
    "an older version": (lambda v: valid("apply", v, to_version="0.6.9"), "apply", "not_newer"),
    "no report": (lambda v: valid("apply", v, prepared_id=str(uuid.uuid4())), "apply", "no_report"),
    "an expired report": (
        lambda v: valid("apply", v, prepared_id=report(v, expires_at=at(-1))),
        "apply",
        "report_expired",
    ),
    "a report for another version": (
        lambda v: valid("apply", v, prepared_id=report(v, to_version="0.8.0")),
        "apply",
        "report_mismatch",
    ),
    "another digest": (lambda v: valid("apply", v, digest="sha256:" + "f" * 64), "apply", "digest"),
    "another updater digest": (lambda v: valid("apply", v, updater_digest=DIGEST), "apply", "digest"),
    "a lossy migration left out": (
        lambda v: valid("apply", v, accepted_lossy=["b2c3d4e5f6a1"]),
        "apply",
        "lossy",
    ),
    "a clean migration slipped in": (
        lambda v: valid("apply", v, accepted_lossy=["b2c3d4e5f6a1", "d4e5f6a1b2c3", "c3d4e5f6a1b2"]),
        "apply",
        "lossy",
    ),
    "a weak recovery hash": (
        lambda v: valid("apply", v, recovery_hash=GOOD_HASH.replace("ln=15", "ln=14")),
        "apply",
        "recovery_hash",
    ),
    "an expensive recovery hash": (
        lambda v: valid("apply", v, recovery_hash=GOOD_HASH.replace("ln=15", "ln=20")),
        "apply",
        "recovery_hash",
    ),
    "a recovery code instead of a hash": (
        lambda v: valid("apply", v, recovery_hash="correct-horse-battery-staple"),
        "apply",
        "recovery_hash",
    ),
    "an updater older than the app": (
        lambda v: valid("update_updater", v, to_version="0.7.0"),
        "update_updater",
        "app_newer",
    ),
    "an updater no newer than this one": (
        lambda v: valid("update_updater", v, to_version="0.7.1"),
        "update_updater",
        "updater_not_newer",
    ),
    "a discard naming no report": (
        lambda v: valid("discard", v, prepared_id="../../etc/passwd"),
        "discard",
        "no_report",
    ),
}


@pytest.mark.parametrize("case", sorted(SHAPE_REFUSALS))
def test_a_refusal_is_recorded_with_its_sentence_and_makes_no_engine_call(world, case):
    fake, vol, ctx = world
    make, kind, code = SHAPE_REFUSALS[case]
    refused_then_valid(fake, vol, ctx, make(vol), kind, code)


def test_a_duplicate_id_is_refused_without_overwriting_the_original(world):
    fake, vol, ctx = world
    first = valid("prepare", vol)
    send(vol, first)
    assert take(vol, ctx).accepted
    original = vol.taken(first["id"]).read_bytes()

    again = valid("prepare", vol, id=first["id"])
    record = refused_then_valid(fake, vol, ctx, again, "prepare", "duplicate")
    assert record["request_id"] == first["id"] and record["id"] != first["id"]
    assert vol.taken(first["id"]).read_bytes() == original


def test_a_malformed_id_is_recorded_under_a_fresh_one(world):
    fake, vol, ctx = world
    record = refused_then_valid(fake, vol, ctx, valid("prepare", vol, id="../../history/x"), "prepare", "id")
    assert record["request_id"] == "../../history/x"
    assert contract.is_uuid4(record["id"])
    assert not (vol.root / "history" / "x.json").exists()


def test_not_json_is_refused(world):
    fake, vol, ctx = world
    refused_then_valid(fake, vol, ctx, b'{"protocol": 1, "id": NaN', "ping", "not_json")
    refused_then_valid(fake, vol, ctx, b'{"protocol": NaN}', "ping", "not_json")


CONTEXT_REFUSALS = {
    "a local build": (RunningApp(version=None, revision=None, published=False), "apply", "local_build"),
    "an unpublished image": (RunningApp(version="0.7.1", revision="987afefca1d1", published=False), "prepare", "local_build"),
    "busy": ("busy", "apply", "concurrent"),
    "busy, updater only": ("busy", "update_updater", "concurrent"),
    "outdated, prepare": ("outdated", "prepare", "outdated"),
    "outdated, apply": ("outdated", "apply", "outdated"),
    "no socket": ("permission_denied", "prepare", "socket"),
    "windows containers": ("windows_containers", "update_updater", "socket"),
}


@pytest.mark.parametrize("case", sorted(CONTEXT_REFUSALS))
def test_what_the_updater_knows_refuses_a_well_formed_request(world, case):
    fake, vol, ctx = world
    change, kind, code = CONTEXT_REFUSALS[case]
    if isinstance(change, RunningApp):
        bad_ctx = replace(ctx, app=change)
    elif change == "busy":
        bad_ctx = replace(ctx, busy=True)
    else:
        bad_ctx = replace(ctx, socket=change)
    send(vol, valid(kind, vol))
    out = take(vol, bad_ctx)
    assert out.refusal is not None and out.refusal.code == code
    assert json.loads(vol.history(out.record_id).read_text())["sentence"] == out.refusal.sentence
    refused_then_valid(fake, vol, bad_ctx, valid(kind, vol), kind, code, good_ctx=ctx)


def test_the_local_build_sentence_is_the_designs(world):
    fake, vol, ctx = world
    record = refused_then_valid(
        fake, vol, replace(ctx, app=RunningApp(None, None, False)), valid("prepare", vol), "prepare", "local_build",
        good_ctx=ctx,
    )
    assert record["sentence"] == (
        "This instance runs a locally built image. Self-update starts only from a published release."
    )


def test_an_outdated_updater_still_accepts_update_updater_and_ping(world):
    fake, vol, ctx = world
    outdated = replace(ctx, socket="outdated")
    for kind in ("update_updater", "ping", "discard"):
        send(vol, valid(kind, vol))
        assert take(vol, outdated).accepted
    assert fake.calls == []


def test_ping_is_answered_even_while_busy(world):
    fake, vol, ctx = world
    first, second = valid("ping", vol), valid("ping", vol)
    for doc in (first, second):
        send(vol, doc)
        assert take(vol, replace(ctx, busy=True)).accepted
    for doc in (first, second):
        assert json.loads(vol.history(doc["id"]).read_text())["state"] == "succeeded"
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# The file itself (5.1): O_NOFOLLOW, regular, the app's uid, at most 4 KiB
# --------------------------------------------------------------------------- #


def test_a_symlinked_request_is_refused_and_its_target_left_alone(world, tmp_path):
    fake, vol, ctx = world
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(valid("prepare", vol)))
    os.symlink(target, vol.request)
    out = take(vol, ctx)
    assert out.refusal.code == "unsafe_file" and "symbolic link" in out.refusal.sentence
    assert target.exists() and json.loads(target.read_text())["kind"] == "prepare"
    refused_then_valid(fake, vol, ctx, valid("prepare", vol, protocol=9), "prepare", "protocol")


def test_a_fifo_is_refused_without_blocking(world):
    fake, vol, ctx = world
    os.mkfifo(vol.request)
    out = take(vol, ctx)
    assert out.refusal.code == "unsafe_file"
    assert fake.calls == []


def test_a_request_not_owned_by_the_app_is_refused(world):
    fake, vol, ctx = world
    send(vol, valid("prepare", vol))
    out = intake.take(vol, ctx, NOW, owner_uid=ME + 1)
    assert out.refusal.code == "unsafe_file" and f"uid {ME}" in out.refusal.sentence
    send(vol, valid("prepare", vol))
    assert take(vol, ctx).accepted
    assert fake.calls == []


def test_a_request_over_4_kib_is_refused(world):
    fake, vol, ctx = world
    big = valid("prepare", vol)
    big["requested_by"] = "x" * 5000
    refused = refused_then_valid(fake, vol, ctx, big, "prepare", "unsafe_file")
    assert "4096 bytes" in refused["sentence"]


def test_no_request_is_nothing_to_do(world):
    fake, vol, ctx = world
    assert intake.take(vol, ctx, NOW, owner_uid=ME) is None
    assert list((vol.root / "history").iterdir()) == []


def test_a_half_taken_request_is_answered_on_start(world):
    fake, vol, ctx = world
    good = valid("prepare", vol)
    bad = valid("prepare", vol, to_version="0.1.0")
    for doc, name in ((good, ".intake-aaaa.taken"), (bad, ".intake-bbbb.taken")):
        (vol.root / "journal" / name).write_text(json.dumps(doc))
    outcomes = intake.resume_intake(vol, ctx, NOW, owner_uid=ME)
    assert [o.accepted for o in outcomes] == [True, False]
    assert not list((vol.root / "journal").glob(".intake-*"))
    assert json.loads(vol.history(bad["id"]).read_text())["code"] == "not_newer"
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# What an accepted request leaves
# --------------------------------------------------------------------------- #


def test_an_accepted_apply_journals_step_0_with_the_hash_and_its_owner(world):
    fake, vol, ctx = world
    first, second = valid("apply", vol), valid("prepare", vol)
    send(vol, first)
    take(vol, ctx)
    j = journal.load(vol, first["id"])
    assert (j.step, j.recovery_hash, j.owner) == ("0", GOOD_HASH, OWNER)
    assert json.loads(vol.status.read_text())["state"] == "accepted"
    send(vol, second)
    take(vol, ctx)
    # A prepare has no apply journal; the status moves to it.
    assert journal.load(vol, second["id"]) is None
    assert json.loads(vol.status.read_text())["id"] == second["id"]
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# U13: protocol windows
# --------------------------------------------------------------------------- #


def test_an_updater_with_window_1_to_2_accepts_both_protocols(world):
    fake, vol, ctx = world
    wide = replace(ctx, protocols=(1, 2))
    one, two = valid("prepare", vol, protocol=1), valid("prepare", vol, protocol=2)
    for doc in (one, two):
        send(vol, doc)
        assert take(vol, wide).accepted
    assert {json.loads(vol.taken(d["id"]).read_text())["protocol"] for d in (one, two)} == {1, 2}
    refused_then_valid(fake, vol, wide, valid("prepare", vol, protocol=3), "prepare", "protocol")


@pytest.mark.parametrize("kind", ["ping", "update_updater"])
def test_ping_and_update_updater_are_frozen_at_protocol_1(world, kind):
    fake, vol, ctx = world
    wide = replace(ctx, protocols=(1, 2))
    refused_then_valid(fake, vol, wide, valid(kind, vol, protocol=2), kind, "frozen")


def test_a_protocol_1_reader_ignores_unknown_keys_and_reads_an_unknown_state_as_running():
    newer = {
        "protocol": 1,
        "id": "5d3c0b8e-7f0a-4b8e-9f43-0f6f1a2b9c11",
        "kind": "apply",
        "state": "verifying_the_moon",
        "step": "5b",
        "sentences": ["Backing up.", 7, "Checking the backup."],
        "eta_seconds": 40,
        "phase": {"nested": True},
    }
    known = {**newer, "state": "rolled_back", "id": "6e4d1c9f-8a1b-4c9f-a054-1a7a2b3c0d22"}
    view, other = contract.read_status(newer), contract.read_status(known)
    assert view.state == "running" and other.state == "rolled_back"
    assert view.sentences == ("Backing up.", "Checking the backup.")
    assert (view.step, other.id) == ("5b", known["id"])
    assert contract.read_status(["not", "a", "status"]) is None


# --------------------------------------------------------------------------- #
# The written shapes (5.3)
# --------------------------------------------------------------------------- #


def test_the_heartbeat_carries_every_key_of_5_3():
    beat = contract.Heartbeat(
        updater_version="0.7.1",
        image_digest=UPDATER_DIGEST,
        seen_at=at(),
        engine="docker-desktop",
        engine_version="4.93.0",
        rootless=False,
        layout="loopback",
        socket="ok",
        hook=False,
        busy=False,
        role="current",
        api_version="1.52",
        engine_api="1.40-1.56",
        container="spend-tracker-updater-1",
    ).to_dict()
    assert set(beat) == {
        "protocol", "updater_version", "image_digest", "seen_at", "engine", "engine_version",
        "rootless", "layout", "socket", "hook", "busy", "role", "protocols", "api_version",
        "engine_api", "container",
    }  # fmt: skip
    assert (beat["protocol"], beat["protocols"]) == (1, "1-1")
    with pytest.raises(ValueError):
        contract.Heartbeat(**{**beat, "engine": "lxc"})
    with pytest.raises(ValueError):
        contract.Heartbeat(**{**beat, "socket": "fine"})


def test_history_keeps_the_last_40_log_lines_and_refuses_an_unknown_state():
    lines = tuple(f"line {i}" for i in range(55))
    record = contract.History(
        id="x", kind="apply", state="rolled_back", sentence="Rolled back.", finished_at=at(), log_tail=lines,
        gap_s=3600.0,
    ).to_dict()
    assert record["log_tail"][0] == "line 15" and len(record["log_tail"]) == 40
    assert record["gap_s"] == 3600.0
    with pytest.raises(ValueError):
        contract.History(id="x", kind="apply", state="exploded", sentence=".", finished_at=at())


# --------------------------------------------------------------------------- #
# Recovery requests (Part 11): shapes only
# --------------------------------------------------------------------------- #


def recovery(kind: str, **over) -> dict:
    base = {"protocol": 1, "id": str(uuid.uuid4()), "kind": kind, "created_at": at(-1), "code": "A" * 32}
    base.update({"restore_backup": {"backup": "20261015-210000"}, "start_matching": {"revision": "2de003489b79"},
                 "download_backup": {"backup": "20261015-210000", "include_key": False}}.get(kind, {}))  # fmt: skip
    base.update(over)
    return base


@pytest.mark.parametrize("kind", contract.RECOVERY_KINDS)
def test_every_recovery_kind_has_a_shape(kind):
    got = contract.validate_recovery(recovery(kind), NOW)
    assert got.kind == kind and got.code == "A" * 32


@pytest.mark.parametrize(
    "bad,code",
    [
        (recovery("retry_rollback", path="/"), "keys"),
        ({k: v for k, v in recovery("restore_backup").items() if k != "backup"}, "keys"),
        (recovery("restore_backup", backup="../../secret.key"), "backup"),
        (recovery("start_matching", revision="HEAD"), "revision"),
        (recovery("download_backup", include_key="yes"), "include_key"),
        (recovery("retry_rollback", code="x"), "code"),
        (recovery("retry_rollback", protocol=2), "protocol"),
        ({**recovery("retry_rollback"), "kind": "run_shell"}, "kind"),
        (recovery("retry_rollback", created_at=at(-3600)), "stale"),
    ],
)
def test_a_recovery_request_outside_its_shape_is_refused(bad, code):
    with pytest.raises(contract.Refusal) as e:
        contract.validate_recovery(bad, NOW)
    assert e.value.code == code


def test_a_label_version_drops_one_v_and_nothing_else():
    assert contract.label_version("v0.7.1") == "0.7.1"
    assert contract.label_version("0.8.0") == "0.8.0"
    assert contract.label_version("vv0.7.1") is None
    assert contract.label_version("0.8.0-dev") is None
    assert contract.label_version(None) is None


def test_the_updater_package_imports_the_standard_library_and_itself_only():
    import ast
    import pathlib
    import sys

    package = pathlib.Path(contract.__file__).parent
    imported: dict[str, set[str]] = {}
    for source in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                imported.setdefault(name.split(".")[0], set()).add(source.name)
    foreign = {m: f for m, f in imported.items() if m not in sys.stdlib_module_names and m not in ("updater", "__future__")}
    assert foreign == {}
    assert {"json", "http", "socket", "os"} <= set(imported)
