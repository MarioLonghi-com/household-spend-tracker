"""Browser recovery, the updater's side (design notes Part 11, 9.2; U10).

Every test drives the real request path: a recovery request written into the
volume as the page writes it, `Service.tick` or `Recovery.handle`, the
restricted client and the recording fake engine with the behaviour of
`tests/updater_world.py`. The recovery hash is the app's own
(`app.services.updates.hash_recovery_code`), so a test that accepts a code
also proves the two sides canonicalise alike (R20).

U10: a right code is accepted once per action; a code from a settled update,
a wrong code and the sixth wrong code within 15 minutes are refused with no
engine call; the hash, never the code, is all that is ever written -- every
file under the test's directory is read back for the code in each of its
canonical-form variants.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.services import updates
from tests.updater_world import APP, HEAD, A, B, C, World, digest, image_doc, platform_digest, ref
from updater import contract, pin, recovery, volume
from updater import engine as eng
from updater.apply import previous_name

#: Seven groups of four Crockford symbols, as the app makes them; with a 0 and
#: a 1, so the O and I/L readings have something to stand for.
CODE = "7K2M-9QXR-4HJT-0WVB-PN3D-8FGC-6YZ1"
OTHER = "8K2M-9QXR-4HJT-0WVB-PN3D-8FGC-6YZ1"
#: What the owner might type instead, all the same code (R20).
TYPED = (
    CODE.lower(),
    CODE.replace("-", ""),
    CODE.replace("0", "O"),
    CODE.replace("1", "L").lower(),
    CODE.replace("1", "I"),
)
HASH = updates.hash_recovery_code(CODE)


@pytest.fixture
def world(tmp_path):
    with World(tmp_path) as w:
        yield w


def apply(w: World, service, to: str = B, frm: str = A, code_hash: str = HASH) -> tuple[dict, dict]:
    report = w.prepared(service, to, frm)
    req = {**w.apply_request(report), "recovery_hash": code_hash}
    w.write_request(req)
    service.tick()
    record = w.history(req["id"])
    assert record is not None
    return req, record


def needing_recovery(w: World, **world_settings) -> tuple:
    """An apply whose rollback failed three times: the restore never works."""
    service = w.service()
    service.startup()
    w.drill = world_settings.pop("drill", "migration-fails")
    w.restore_fails = world_settings.pop("restore_fails", 3)
    for k, v in world_settings.items():
        setattr(w, k, v)
    req, record = apply(w, service)
    assert record["state"] == "needs_recovery", record
    return service, req


def ask(w: World, update_id: str, kind: str, code: str = CODE, **fields) -> dict:
    """What the page does: one request into recovery/request.json."""
    volume.write_json(
        w.volume.recovery_request,
        {
            "protocol": 1,
            "id": update_id,
            "kind": kind,
            "created_at": contract.iso(w.clock.now()).replace("Z", f".{uuid.uuid4().int % 10**6:06d}Z"),
            "code": code,
            **fields,
        },
    )


def answer(w: World) -> dict:
    found = volume.read_own_json(w.volume.recovery_answer)
    assert found is not None
    return found


def recover(w: World, service, update_id: str, kind: str, code: str = CODE, **fields) -> dict:
    ask(w, update_id, kind, code, **fields)
    service.tick()
    return answer(w)


def actions(w: World, update_id: str) -> list[dict]:
    return list((w.history(update_id) or {}).get("recovery") or [])


def attempts(w: World) -> dict:
    return volume.read_own_json(w.volume.recovery_attempts) or {}


def every_file(root: Path) -> list[Path]:
    return [Path(d) / f for d, _, files in os.walk(root) for f in files]


def assert_code_written_nowhere(root: Path, *codes: str) -> None:
    variants = set()
    for code in codes:
        canonical = updates.canonical_code(code)
        for v in (code, code.lower(), code.replace("-", ""), canonical, canonical.lower(), *TYPED):
            variants.add(v.encode())
    files = every_file(root)
    assert files, "the test produced no files to search"
    for path in files:
        data = path.read_bytes()
        for v in variants:
            assert v not in data, f"{v!r} reached {path}"


# --------------------------------------------------------------------------- #
# The code (U10)
# --------------------------------------------------------------------------- #


def test_the_canonical_form_is_the_apps_exactly():
    for typed in (*TYPED, CODE, " " + CODE + " ", "abc-def-ilo-0011"):
        assert recovery.canonical(typed) == updates.canonical_code(typed)
    assert recovery.code_matches(CODE, HASH)
    assert not recovery.code_matches(OTHER, HASH)


def test_a_right_code_opens_recovery_once_per_request_in_any_typed_form(world):
    service, req = needing_recovery(world)
    for n, typed in enumerate(TYPED, start=1):
        got = recover(world, service, req["id"], "open", typed)
        assert got["state"] == "done", got.get("sentence") or got
        assert [a["kind"] for a in actions(world, req["id"])] == ["open"] * n
        # The request was taken: a second tick does nothing more.
        service.tick()
        assert len(actions(world, req["id"])) == n
    assert not os.path.lexists(world.volume.recovery_request)
    assert attempts(world).get("wrong", 0) == 0


def test_a_wrong_code_is_refused_counted_and_makes_no_engine_call(world):
    service, req = needing_recovery(world)
    before = len(world.fake.calls)
    ask(world, req["id"], "retry_rollback", OTHER)
    assert service.recovery.handle() == "refused"
    got = answer(world)
    assert got["state"] == "refused" and got["code"] == "wrong_code"
    assert len(world.fake.calls) == before
    assert attempts(world)["wrong"] == 1
    assert actions(world, req["id"])[-1]["result"] == "refused"
    # Nothing moved: still needs recovery, the ledger as it was.
    assert world.history(req["id"])["state"] == "needs_recovery"
    assert world.ledger.stamp == "half-migrated"


def test_the_sixth_code_within_15_minutes_is_refused_even_when_right(world):
    service, req = needing_recovery(world)
    before = len(world.fake.calls)
    for n in range(1, 6):
        ask(world, req["id"], "open", OTHER)
        service.recovery.handle()
        assert attempts(world)["wrong"] == n
    assert attempts(world)["refused_until"] is not None
    ask(world, req["id"], "retry_rollback", CODE)
    assert service.recovery.handle() == "refused"
    got = answer(world)
    assert got["code"] == "refused_for_now" and "15 more minutes" in got["sentence"]
    assert len(world.fake.calls) == before
    # Refused unread: the count did not grow, so a flood cannot lengthen it.
    assert attempts(world)["wrong"] == 5
    world.time.t += 15 * 60 + 1
    assert recover(world, service, req["id"], "open", CODE)["state"] == "done"
    assert attempts(world)["wrong"] == 0


def test_each_further_five_wrong_codes_doubles_the_refusal(world):
    service, req = needing_recovery(world)
    for _round, minutes in ((1, 15), (2, 30), (3, 60)):
        for _ in range(5):
            ask(world, req["id"], "open", OTHER)
            service.recovery.handle()
        until = contract.parse_iso(attempts(world)["refused_until"])
        assert round((until - world.clock.now()) / 60) == minutes
        world.time.t += minutes * 60 + 1
    assert attempts(world)["wrong"] == 15
    assert [recovery.refusal_seconds(n) for n in (4, 5, 6, 10, 15)] == [None, 900, None, 1800, 3600]


def test_a_code_from_a_settled_update_opens_nothing(world):
    service = world.service()
    service.startup()
    req, record = apply(world, service)
    assert record["state"] == "succeeded"
    assert world.journal(req["id"]).recovery_hash is None
    before = len(world.fake.calls)
    ask(world, req["id"], "open", CODE)
    assert service.recovery.handle() == "refused"
    assert answer(world)["code"] == "settled"
    assert len(world.fake.calls) == before
    # Not a wrong code: nothing was checked, nothing counted.
    assert attempts(world).get("wrong", 0) == 0


def test_requests_of_another_shape_or_for_no_update_are_refused_unread(world):
    service, req = needing_recovery(world)
    before = len(world.fake.calls)
    ask(world, req["id"], "start_matching", CODE, revision="../../etc")
    service.recovery.handle()
    assert answer(world)["code"] == "revision"
    ask(world, req["id"], "retry_rollback", CODE, image=ref(APP, C))
    service.recovery.handle()
    assert answer(world)["code"] == "keys"
    ask(world, str(uuid.uuid4()), "open", CODE)
    service.recovery.handle()
    assert answer(world)["code"] == "unknown"
    assert len(world.fake.calls) == before
    assert attempts(world).get("wrong", 0) == 0


def test_a_request_that_is_a_symlink_is_refused_and_its_target_left_alone(world, tmp_path):
    service, req = needing_recovery(world)
    target = tmp_path / "elsewhere.json"
    target.write_text('{"protocol": 1}')
    os.symlink(target, world.volume.recovery_request)
    assert service.recovery.handle() == "refused"
    assert answer(world)["code"] == "unsafe_file"
    assert target.read_text() == '{"protocol": 1}'


def test_the_hash_never_the_code_is_all_that_is_ever_written(world, tmp_path):
    service, req = needing_recovery(world)
    for typed in TYPED:
        recover(world, service, req["id"], "open", typed)
    for _ in range(3):
        recover(world, service, req["id"], "open", OTHER)
    recover(world, service, req["id"], "download_diagnostics")
    world.restore_fails = 0
    recover(world, service, req["id"], "retry_rollback")
    assert world.history(req["id"])["state"] == "rolled_back"
    recover(world, service, req["id"], "open")  # settled now
    assert_code_written_nowhere(tmp_path, CODE, OTHER)
    # And the hash was there until the update settled, then gone.
    assert world.journal(req["id"]).recovery_hash is None


# --------------------------------------------------------------------------- #
# The actions (11.3), each through the fake world
# --------------------------------------------------------------------------- #


def test_retry_the_rollback_puts_the_previous_version_back_and_settles(world):
    service, req = needing_recovery(world)
    world.restore_fails = 0
    got = recover(world, service, req["id"], "retry_rollback")
    assert got["state"] == "done"
    record = world.history(req["id"])
    assert record["state"] == "rolled_back" and record["sentence"].startswith(f"Rolled back to {A}")
    assert world.ledger.stamp == A
    assert [world.version_of(c) for c in world.running_apps()] == [A]
    assert world.journal(req["id"]).recovery_hash is None
    assert [(a["kind"], a["result"]) for a in record["recovery"]] == [("retry_rollback", "done")]
    world.no_running_app_at_a_mismatched_stamp()


def test_a_retry_that_fails_again_stays_in_recovery_with_the_page_back(world):
    service, req = needing_recovery(world)
    placards_before = sum(1 for c in world.fake.calls if c.bare == "/containers/create" and "placard" in c.query.get("name", ""))
    world.restore_fails = 1
    got = recover(world, service, req["id"], "retry_rollback")
    assert got["state"] == "failed" and "still open" in got["sentence"]
    record = world.history(req["id"])
    assert record["state"] == "needs_recovery"
    assert [(a["kind"], a["result"]) for a in record["recovery"]] == [("retry_rollback", "failed")]
    assert world.journal(req["id"]).recovery_hash is not None
    placards = sum(1 for c in world.fake.calls if c.bare == "/containers/create" and "placard" in c.query.get("name", ""))
    assert placards == placards_before + 1
    assert world.running_apps() == []
    # The restore's own words are kept for the page.
    assert "the copy failed" in "\n".join(world.journal(req["id"]).context["restore_log"])


def two_updates_the_second_needing_recovery(w: World) -> tuple:
    service = w.service()
    service.startup()
    first, record = apply(w, service)
    assert record["state"] == "succeeded" and w.ledger.stamp == B
    w.drill = "migration-fails"
    w.restore_fails = 3
    second, record = apply(w, service, to=C, frm=B)
    assert record["state"] == "needs_recovery"
    return service, first, second


def test_restore_a_different_backup_with_the_version_that_took_it(world):
    service, first, second = two_updates_the_second_needing_recovery(world)
    older = os.path.basename(world.journal(first["id"]).context["backup"])
    newer = os.path.basename(world.journal(second["id"]).context["backup"])
    assert world.ledger.backups[older] == A and world.ledger.backups[newer] == B
    assert ref(APP, A) in world.fake.images  # still on the machine: the second update did not finish
    world.restore_fails = 0
    got = recover(world, service, second["id"], "restore_backup", backup=older)
    assert got["state"] == "done", got.get("sentence") or got
    record = world.history(second["id"])
    assert record["state"] == "recovered" and older in record["sentence"]
    assert world.ledger.stamp == A
    assert [world.version_of(c) for c in world.running_apps()] == [A]
    assert pin.read(world.project_dir)[pin.APP_KEY].startswith(f"{APP}:{A}@")
    assert world.journal(second["id"]).recovery_hash is None
    world.no_running_app_at_a_mismatched_stamp()


def test_a_backup_recorded_by_the_platform_digest_is_restored_by_the_digest_that_verifies(world):
    """Podman lists the platform manifest's digest beside the index digest a
    release attests (#287), and the update recorded the one the app ran by.
    Restoring that update's backup verified the recorded digest alone and
    refused -- "no longer verifies (subject)" -- about every other E8 run on
    the rootless Podman leg (#421). Every digest the image carries is tried."""
    service, first, second = two_updates_the_second_needing_recovery(world)
    older = os.path.basename(world.journal(first["id"]).context["backup"])
    platform = f"{APP}@{platform_digest(APP, A)}"
    world.fake.images[ref(APP, A)]["RepoDigests"] = [platform, ref(APP, A)]
    world.fake.images[platform] = world.fake.images[ref(APP, A)]
    # The first update ran from A and recorded it by the platform's digest; the
    # second ran from B, and its journal is left as it is.
    path = world.volume.journal(first["id"])
    doc = volume.read_own_json(path)
    doc["context"]["old_ref"] = platform
    volume.write_json(path, doc)
    world.restore_fails = 0
    got = recover(world, service, second["id"], "restore_backup", backup=older)
    assert got["state"] == "done", got.get("sentence") or got
    record = world.history(second["id"])
    assert record["state"] == "recovered" and older in record["sentence"]
    assert world.ledger.stamp == A
    running = world.running_apps()
    assert [world.version_of(c) for c in running] == [A]
    # Started and pinned by the digest that verifies, not the one recorded.
    assert world.fake.inspect_of(running[0])["Config"]["Image"] == ref(APP, A)
    assert pin.read(world.project_dir)[pin.APP_KEY] == f"{APP}:{A}@{digest(APP, A)}"
    assert world.journal(second["id"]).recovery_hash is None


#: Podman lists a multi-arch image's index and platform digests in either
#: order (#287); an update records whichever `survey.published_ref` met first.
PODMAN_ORDERS = ["index_first", "platform_first"]


def recorded_as(w: World, update_id: str, order: str) -> str:
    """The old image as the update recorded it: the platform's digest when Podman listed it first."""
    old_ref = w.journal(update_id).context["old_ref"]
    assert old_ref == (f"{APP}@{platform_digest(APP, A)}" if order == "platform_first" else ref(APP, A))
    return old_ref


def started_and_pinned_by_the_index(w: World, *, came_home: bool = False) -> None:
    """A serving, pinned by the digest its release attests whatever was recorded,
    and -- unless the parked container itself came home, as it was -- run by it."""
    running = w.running_apps()
    assert [w.version_of(c) for c in running] == [A]
    seen = w.fake.inspect_of(running[0])
    assert seen["Image"] == w.fake.images[ref(APP, A)]["Id"]
    if not came_home:
        assert seen["Config"]["Image"] == ref(APP, A)
    assert pin.read(w.project_dir)[pin.APP_KEY] == f"{APP}:{A}@{digest(APP, A)}"


@pytest.mark.parametrize("order", PODMAN_ORDERS)
def test_a_rollback_on_podman_puts_back_the_previous_version_in_either_digest_order(tmp_path, order):
    """R2-R4 run the recorded reference, which needs no attestation: it is the
    image the app ran by a moment ago. Either digest names that image."""
    with World(tmp_path, multiarch=order) as w:
        service = w.service()
        service.startup()
        w.drill = "migration-fails"
        req, record = apply(w, service)
        assert record["state"] == "rolled_back", record
        recorded_as(w, req["id"], order)
        assert w.ledger.stamp == A
        assert [w.version_of(c) for c in w.running_apps()] == [A]
        w.no_running_app_at_a_mismatched_stamp()


@pytest.mark.parametrize("order", PODMAN_ORDERS)
def test_restoring_a_backup_on_podman_verifies_by_the_index_digest_in_either_order(tmp_path, order):
    """Recovery re-verifies the image that took the backup (11.3). It tries
    every digest the engine lists for it, never only the first (#421): in
    `platform_first` the recorded one is the platform's, which no
    attestation names, and the restore was refused "(subject)"."""
    with World(tmp_path, multiarch=order) as w:
        service, first, second = two_updates_the_second_needing_recovery(w)
        recorded_as(w, first["id"], order)
        older = os.path.basename(w.journal(first["id"]).context["backup"])
        w.restore_fails = 0
        got = recover(w, service, second["id"], "restore_backup", backup=older)
        assert got["state"] == "done", got.get("sentence") or got
        record = w.history(second["id"])
        assert record["state"] == "recovered" and older in record["sentence"]
        assert w.ledger.stamp == A
        started_and_pinned_by_the_index(w)
        assert w.journal(second["id"]).recovery_hash is None


@pytest.mark.parametrize("order", PODMAN_ORDERS)
def test_starting_the_matching_version_on_podman_verifies_by_the_index_digest_in_either_order(tmp_path, order):
    with World(tmp_path, multiarch=order) as w:
        service, req = needing_recovery(w, restore_fails=0, broken={A})
        recorded_as(w, req["id"], order)
        assert w.ledger.stamp == A and w.running_apps() == []
        w.broken.clear()
        got = recover(w, service, req["id"], "start_matching", revision=HEAD[A])
        assert got["state"] == "done", got.get("sentence") or got
        assert w.history(req["id"])["state"] == "recovered"
        # The parked A is A's image by either digest, so it comes home rather
        # than being made again.
        assert w.by_name(previous_name(w.app_name)) is None
        started_and_pinned_by_the_index(w, came_home=True)
        w.no_running_app_at_a_mismatched_stamp()


@pytest.mark.parametrize("order", PODMAN_ORDERS)
def test_a_digest_no_attestation_names_is_still_refused_in_either_order(tmp_path, order):
    """Trying every digest widens nothing: with the release's attestation
    gone (forged here), neither digest verifies and A is not started."""
    with World(tmp_path, multiarch=order) as w:
        service, req = needing_recovery(w, restore_fails=0, broken={A})
        w.broken.clear()
        w.forged.add((APP, A))
        got = recover(w, service, req["id"], "start_matching", revision=HEAD[A])
        assert got["state"] == "failed" and f"{A} no longer verifies" in got["sentence"], got
        assert w.history(req["id"])["state"] == "needs_recovery"
        assert w.running_apps() == []


def test_a_backup_whose_version_is_gone_is_not_restored(world):
    service, first, second = two_updates_the_second_needing_recovery(world)
    older = os.path.basename(world.journal(first["id"]).context["backup"])
    del world.fake.images[ref(APP, A)]  # pruned by hand, say
    restores = sum(1 for c in world.fake.calls if c.bare == "/containers/create" and "-restore-" in c.query.get("name", ""))
    world.restore_fails = 0
    got = recover(world, service, second["id"], "restore_backup", backup=older)
    assert got["state"] == "failed" and "no longer on this machine" in got["sentence"]
    assert world.history(second["id"])["state"] == "needs_recovery"
    assert world.ledger.stamp == "half-migrated"
    assert sum(1 for c in world.fake.calls if c.bare == "/containers/create" and "-restore-" in c.query.get("name", "")) == restores


def test_a_backup_that_is_not_an_update_backup_is_refused(world):
    service, req = needing_recovery(world)
    got = recover(world, service, req["id"], "restore_backup", backup="20200101-000000")
    assert got["state"] == "refused" and got["code"] == "backup"
    assert world.history(req["id"])["state"] == "needs_recovery"


def test_start_the_version_that_matches_the_ledger_old_or_new(world):
    # The ledger was restored to A but A did not answer (row 23); then it does.
    service, req = needing_recovery(world, restore_fails=0, broken={A})
    assert world.ledger.stamp == A and world.running_apps() == []
    world.broken.clear()
    got = recover(world, service, req["id"], "start_matching", revision=HEAD[A])
    assert got["state"] == "done", got.get("sentence") or got
    assert world.history(req["id"])["state"] == "recovered"
    assert [world.version_of(c) for c in world.running_apps()] == [A]
    world.no_running_app_at_a_mismatched_stamp()


def test_start_matching_the_new_version_when_the_migration_did_land(tmp_path):
    with World(tmp_path) as w:
        # The drill succeeded, B did not answer, and putting A back failed three times.
        service, req = needing_recovery(w, drill="ok", restore_fails=3, broken={B})
        assert w.ledger.stamp == B
        before = [c.query.get("name") for c in w.fake.calls if c.bare == "/containers/create"]
        got = recover(w, service, req["id"], "start_matching", revision=HEAD[A])
        assert got["state"] == "failed" and HEAD[B] in got["sentence"]
        created = [c for c in w.fake.calls if c.bare == "/containers/create"][len(before) :]
        assert not any(c.query.get("name") == w.app_name for c in created), "A was started on B's ledger"
        w.broken.clear()
        got = recover(w, service, req["id"], "start_matching", revision=HEAD[B])
        assert got["state"] == "done", got.get("sentence") or got
        assert [w.version_of(c) for c in w.running_apps()] == [B]
        assert pin.read(w.project_dir)[pin.APP_KEY].startswith(f"{APP}:{B}@")
        for c in [c for c in w.fake.calls if c.bare == "/containers/create"]:
            env = c.body.get("Env") or []
            assert "SPENDTRACKER_AUTO_MIGRATE=1" not in env, "never a migration"


def test_the_downloads_are_accepted_for_update_backups_only_and_recorded(world):
    service, req = needing_recovery(world)
    stamp = os.path.basename(world.journal(req["id"]).context["backup"])
    before = len(world.fake.calls)
    assert recover(world, service, req["id"], "download_backup", backup=stamp, include_key=False)["state"] == "done"
    assert recover(world, service, req["id"], "download_backup", backup=stamp, include_key=True)["state"] == "done"
    assert recover(world, service, req["id"], "download_backup", backup="19990101-000000", include_key=False)["state"] == "refused"
    assert recover(world, service, req["id"], "download_diagnostics")["state"] == "done"
    assert len(world.fake.calls) == before
    sentences = [a["sentence"] for a in actions(world, req["id"])]
    assert sentences[0].endswith("without secret.key.") and sentences[1].endswith("with secret.key.")


def test_leave_it_to_me_marks_the_record_and_closes_the_engine_actions(world):
    service, req = needing_recovery(world)
    got = recover(world, service, req["id"], "leave_for_operator")
    assert got["state"] == "done"
    assert world.history(req["id"])["state"] == "left_for_operator"
    world.restore_fails = 0
    got = recover(world, service, req["id"], "retry_rollback")
    assert got["state"] == "refused" and got["code"] == "left"
    assert world.ledger.stamp == "half-migrated" and world.running_apps() == []
    assert recover(world, service, req["id"], "download_diagnostics")["state"] == "done"
    assert world.journal(req["id"]).recovery_hash is not None


def test_needs_recovery_names_the_update_for_the_page(world):
    service, req = needing_recovery(world)
    mode = volume.read_own_json(world.volume.recovery_mode)
    assert mode["mode"] == "recovery" and mode["id"] == req["id"]


# --------------------------------------------------------------------------- #
# 11.1: an hour unfinished
# --------------------------------------------------------------------------- #


def stuck_at_the_drill(w: World) -> tuple:
    service = w.service()
    service.startup()
    report = w.prepared(service)

    def engine_goes(c):
        w.drill = "hang"
        w.time.on_sleep = lambda: setattr(w.fake, "gone", True)

    w.on_drill = engine_goes
    req = {**w.apply_request(report), "recovery_hash": HASH}
    w.write_request(req)
    service.tick()
    w.time.on_sleep = None
    w.on_drill = None
    w.fake.gone = False
    assert w.history(req["id"]) is None and w.journal(req["id"]).step == "5"
    return service, req


def placards(w: World) -> list:
    return [c for c in w.fake.calls if c.bare == "/containers/create" and (c.body.get("Labels") or {}).get(eng.ROLE_LABEL) == "placard"]


def test_an_apply_unfinished_for_an_hour_opens_recovery(world):
    service, req = stuck_at_the_drill(world)
    before = len(placards(world))
    j = world.journal(req["id"])
    world.time.t += 30 * 60
    assert service.recovery.watch_stuck([j]) == []
    world.time.t += 31 * 60
    assert service.recovery.watch_stuck([world.journal(req["id"])]) == [req["id"]]
    made = placards(world)[before:]
    assert len(made) == 1 and made[0].body["Cmd"] == ["-m", "scripts.placard", "--recovery"]
    assert volume.read_own_json(world.volume.recovery_mode)["id"] == req["id"]
    # Once: the next pass leaves it.
    assert service.recovery.watch_stuck([world.journal(req["id"])]) == []
    # The code opens it, and leaving it to a terminal stops the journal being
    # resumed. (`handle`, not `tick`: a tick would resume this journal first.)
    for kind in ("open", "leave_for_operator"):
        ask(world, req["id"], kind)
        assert service.recovery.handle() == "done", answer(world)
    record = world.history(req["id"])
    assert record["state"] == "left_for_operator"
    assert [a["kind"] for a in record["recovery"]] == ["open", "leave_for_operator"]
    assert service.unfinished() == []


# --------------------------------------------------------------------------- #
# 9.2: compose started an older image
# --------------------------------------------------------------------------- #


def test_an_app_started_older_than_the_pin_gets_the_ledger_ahead_page(world):
    service = world.service()
    service.startup()
    req, record = apply(world, service)
    assert record["state"] == "succeeded"
    # Compose, not reading the pin, starts A again on B's ledger: it refuses and exits.
    app = world.by_name(world.app_name)
    a_image = world.fake.images[ref(APP, A)] if ref(APP, A) in world.fake.images else image_doc(APP, A)
    world.fake.images[ref(APP, A)] = a_image
    world.fake.inspect_of(app)["Image"] = a_image["Id"]
    world.fake.inspect_of(app)["Config"]["Image"] = ref(APP, A)
    world.fake.set_state(app, "exited", exit_code=3)
    before = len(placards(world))
    service.recovery._ahead_checked = float("-inf")
    assert service.recovery.watch_ahead() == "opened"
    made = placards(world)[before:]
    assert len(made) == 1 and made[0].body["Image"] == ref(APP, A)
    assert made[0].body["Cmd"] == ["-m", "scripts.placard", "--recovery"]
    mode = volume.read_own_json(world.volume.recovery_mode)
    assert mode["mode"] == "ledger_ahead" and mode["id"] is None
    # No code exists: nothing opens recovery.
    ask(world, req["id"], "open")
    service.recovery.handle()
    assert answer(world)["code"] == "settled"
    # Running the launcher brings B back: the page goes.
    world.fake.inspect_of(app)["Image"] = world.fake.images[ref(APP, B)]["Id"]
    world.fake.inspect_of(app)["Config"]["Image"] = ref(APP, B)
    world.fake.set_state(app, "running")
    service.recovery._ahead_checked = float("-inf")
    assert service.recovery.watch_ahead() == "cleared"
    assert world.by_name(f"{world.app_name}-placard-ahead") is None


def test_an_app_at_the_pins_version_that_stopped_cleanly_gets_no_page(world):
    service = world.service()
    service.startup()
    apply(world, service)
    app = world.by_name(world.app_name)
    world.fake.set_state(app, "exited", exit_code=0)
    service.recovery._ahead_checked = float("-inf")
    assert service.recovery.watch_ahead() is None
    assert world.by_name(f"{world.app_name}-placard-ahead") is None
