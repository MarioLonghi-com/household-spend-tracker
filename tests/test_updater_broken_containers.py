"""#262: a power cut right after the updater created its placard and drill.

Both containers exist, but their storage never reached the disk. The engine
then fails every full listing that includes them, and every inspect of them,
with a 500 -- and an updater that listed with `all=1` at start crashed, and
was restarted 830 times, with the app down and nobody told.

Here the fake engine has two such containers (`FakeEngine.broken`), and two
of everything around them: the updater's own placard *and* drill of the
interrupted request, beside another request's placard and the parked app,
which the engine can still describe and which must be left alone.
"""

from __future__ import annotations

import json
import threading

import pytest

from tests.test_updater_launch import BUNDLE_ENV, _answer, _args, _folder, info_of
from tests.updater_fake_engine import FakeEngine, Running, engine_fixture
from tests.updater_world import PROJECT, A, World
from updater import engine as eng
from updater import launch, oneoff
from updater.__main__ import identify
from updater.heartbeat import Beat, Identity
from updater.service import STUCK_MAX_SECONDS, TICK_SECONDS, Stuck


class Killed(BaseException):
    """The power went: nothing after this happened."""


def cut_power_during_the_drill(w: World) -> dict:
    """An apply that reached the drill, then a power cut: every container but the updater stops."""
    service = w.service()
    service.startup()
    report = w.prepared(service)
    w.drill = "stall"
    req = w.apply_request(report)
    w.write_request(req)

    def die():
        raise Killed("5")

    w.time.on_sleep = die
    with pytest.raises(Killed):
        service.tick()
    w.time.on_sleep = None
    for c in w.fake.containers.values():
        if c["Id"] != w.updater_id and c["State"] == "running":
            w.fake.set_state(c, "exited", 137)
    return req


def transient(w: World, req: dict) -> dict[str, str]:
    """This request's placard and drill, by name: their ids."""
    app = w.journal(req["id"]).context["app_name"]
    names = {role: oneoff.name_for(app, role, req["id"]) for role in ("placard", "drill")}
    return {name: w.fake.by_name(name)["Id"] for name in names.values()}


def other_request_placard(w: World) -> str:
    """A placard another request left behind: one of the updater's, but not this request's."""
    return w.fake.add_container(
        f"{w.app_name}-placard-0badc0de",
        PROJECT,
        labels={
            eng.ONEOFF_LABEL: "True",
            eng.ROLE_LABEL: "placard",
            eng.REQUEST_LABEL: "0badc0de-0000-4000-8000-000000000000",
        },
        State="exited",
    )


def test_after_a_power_cut_the_updater_starts_removes_only_its_broken_one_offs_and_rolls_back(tmp_path):
    with World(tmp_path) as w:
        req = cut_power_during_the_drill(w)
        mine = transient(w, req)
        assert len(mine) == 2
        w.fake.broken |= set(mine.values())
        other = other_request_placard(w)
        previous = w.app_id
        assert w.fake.containers[previous]["State"] == "exited"  # parked at step 3

        client = w.kit().client
        with pytest.raises(eng.EngineError) as listing:
            client.containers()
        assert listing.value.status == 500 and "graph driver" in listing.value.message

        # Startup: the updater finds itself, by the listing that still answers.
        me, update_volume = identify(client, mountinfo="", hostname=w.updater_id[:12])
        assert me.container == f"{PROJECT}-updater-1"
        assert me.version == A and me.image_digest.startswith("sha256:")
        assert update_volume == f"{PROJECT}_update"

        before = len(w.fake.calls)
        again = w.service()
        again.startup()

        record = w.history(req["id"])
        assert record["state"] == "rolled_back", record
        assert (
            record["sentence"] == "Rolled back to 0.7.1: the update was interrupted; nothing was migrated."
        )
        assert w.ledger.stamp == A
        [running] = w.running_apps()
        assert running["Id"] == previous and w.version_of(running) == A

        # Removed: this request's two broken one-offs, by name, with force -- and
        # otherwise only what the resume itself created (the find-backup one-off).
        calls = w.fake.calls[before:]
        removed = [c.bare.rsplit("/", 1)[1] for c in calls if c.method == "DELETE"]
        made = {c.bare.split("/")[2] for c in calls if c.method == "POST" and c.bare.endswith("/start")}
        assert sorted(r for r in removed if r not in made) == sorted(mine)
        assert all(
            c.query.get("force") == "1"
            for c in calls
            if c.method == "DELETE" and c.bare.endswith(tuple(mine))
        )
        assert not set(mine.values()) & set(w.fake.containers)
        assert other in w.fake.containers and previous in w.fake.containers
        assert not w.fake.broken
        assert again.problem is None


def test_the_resume_removes_nothing_while_the_listing_answers(tmp_path):
    """Not broken, not removed: a restart with the drill's container intact runs 5.6 as before."""
    with World(tmp_path) as w:
        req = cut_power_during_the_drill(w)
        mine = transient(w, req)
        other = other_request_placard(w)
        before = len(w.fake.calls)
        again = w.service()
        assert again.clear_broken() == []
        removals = [c for c in w.fake.calls[before:] if c.method == "DELETE"]
        assert removals == []
        assert set(mine.values()) <= set(w.fake.containers) and other in w.fake.containers
        again.startup()
        assert w.history(req["id"])["state"] == "rolled_back"
        assert other in w.fake.containers


class Stop(threading.Event):
    """The service's stop event, recording each pause and acting at each."""

    def __init__(self, on_wait) -> None:
        super().__init__()
        self.pauses: list[float] = []
        self.on_wait = on_wait

    def wait(self, timeout=None) -> bool:
        self.pauses.append(timeout)
        if self.on_wait(len(self.pauses)):
            self.set()
        return self.is_set()


def test_an_owner_s_broken_container_is_said_backed_off_from_and_left_for_the_owner(tmp_path):
    with World(tmp_path) as w:
        req = cut_power_during_the_drill(w)
        mine = transient(w, req)
        w.fake.broken |= set(mine.values())
        # Two of the owner's: one broken, one fine. Neither is the updater's to remove.
        theirs = w.fake.add_container(f"{PROJECT}-backup-1", PROJECT, State="exited")
        fine = w.fake.add_container(f"{PROJECT}-notes-1", PROJECT, State="exited")
        w.fake.broken.add(theirs)

        service = w.service()
        beat = Beat(
            w.kit().client,
            w.volume,
            Identity(updater_version=A, image_digest="sha256:" + "1" * 64),
            mountinfo="",
            hostname=w.updater_id[:12],
            problem=lambda: service.problem,
        )
        said: list[dict] = []

        def on_wait(n: int) -> bool:
            beat.tick(w.clock.now())
            said.append(json.loads(w.volume.heartbeat.read_text()))
            if n == 3:
                # The owner reads the sentence and removes the container it names.
                del w.fake.containers[theirs]
                w.fake.broken.discard(theirs)
            return n >= 4

        stop = Stop(on_wait)
        service.run(stop)

        # Three startups that could not go on, each pause longer; then one that did.
        assert stop.pauses[:3] == [TICK_SECONDS, TICK_SECONDS * 2, TICK_SECONDS * 4]
        assert all(p <= STUCK_MAX_SECONDS for p in stop.pauses)
        problem = said[0]["problem"]
        assert problem.startswith("The updater cannot go on:") and theirs in problem
        assert [s["problem"] for s in said[:3]] == [problem] * 3
        assert said[3]["problem"] is None and service.problem is None

        # Its own broken one-offs went at the first try; the owner's two never by the updater.
        assert not set(mine.values()) & set(w.fake.containers)
        assert fine in w.fake.containers
        deleted = {c.bare.rsplit("/", 1)[1] for c in w.fake.calls if c.method == "DELETE"}
        assert theirs not in deleted and fine not in deleted
        # The unfinished apply's status said why, while it waited; then it rolled back.
        assert w.history(req["id"])["state"] == "rolled_back"


def test_a_startup_that_cannot_list_says_so_in_the_apply_s_status_and_raises_stuck(tmp_path):
    with World(tmp_path) as w:
        req = cut_power_during_the_drill(w)
        theirs = w.fake.add_container(f"{PROJECT}-backup-1", PROJECT, State="exited")
        w.fake.broken.add(theirs)
        service = w.service()
        with pytest.raises(Stuck) as stuck:
            service.startup()
        status = json.loads(w.volume.status.read_text())
        assert status["id"] == req["id"] and status["state"] == "running"
        assert status["sentences"][-1] == stuck.value.sentence
        # Earlier sentences are kept, and the same one is not said twice.
        assert len(status["sentences"]) > 1
        with pytest.raises(Stuck):
            service.startup()
        assert json.loads(w.volume.status.read_text())["sentences"] == status["sentences"]
        assert w.history(req["id"]) is None
        assert theirs in w.fake.containers


# --------------------------------------------------------------------------- #
# The engine client's guard
# --------------------------------------------------------------------------- #


def _oneoff(fake: FakeEngine, name: str, role: str = "placard") -> str:
    return fake.add_container(
        name,
        PROJECT,
        labels={eng.ONEOFF_LABEL: "True", eng.ROLE_LABEL: role, eng.REQUEST_LABEL: "r"},
        State="exited",
    )


def test_only_a_broken_one_off_of_the_project_is_removed_unlisted():
    fake = FakeEngine(engine_fixture("docker-desktop"), info_of("docker-desktop"))
    broken_one = _oneoff(fake, "spend-tracker-app-1-placard-11111111")
    healthy_one = _oneoff(fake, "spend-tracker-app-1-drill-22222222", role="drill")
    broken_owner = fake.add_container("spend-tracker-backup-1", PROJECT, State="exited")
    elsewhere = fake.add_container(
        "other-app-placard-33333333",
        "other",
        labels={eng.ONEOFF_LABEL: "True", eng.ROLE_LABEL: "placard", eng.REQUEST_LABEL: "r"},
        State="exited",
    )
    fake.broken |= {broken_one, broken_owner, elsewhere}
    with Running(fake) as running:
        client = eng.EngineClient(running.socket_path, eng.Scope(project=PROJECT))
        client.negotiate()
        assert client.remove_broken_oneoff("spend-tracker-app-1-drill-22222222") is False
        assert client.remove_broken_oneoff(broken_owner) is False
        assert client.remove_broken_oneoff("spend-tracker-backup-1") is False
        assert client.remove_broken_oneoff("other-app-placard-33333333") is False
        with pytest.raises(eng.NotAllowed):
            client.remove_broken_oneoff("../containers/x")
        assert client.remove_broken_oneoff("spend-tracker-app-1-placard-11111111") is True
    assert set(fake.containers) == {healthy_one, broken_owner, elsewhere}
    assert [c.bare for c in fake.calls if c.method == "DELETE"] == [
        "/containers/spend-tracker-app-1-placard-11111111"
    ]


def test_a_running_container_is_still_resolved_while_the_full_listing_fails():
    fake = FakeEngine(engine_fixture("docker-desktop"), info_of("docker-desktop"))
    app = fake.add_container("spend-tracker-app-1", PROJECT)
    parked = fake.add_container("spend-tracker-app-1-previous", PROJECT, State="exited")
    fake.broken.add(_oneoff(fake, "spend-tracker-app-1-placard-11111111"))
    with Running(fake) as running:
        client = eng.EngineClient(running.socket_path, eng.Scope(project=PROJECT))
        client.negotiate()
        assert client.inspect("spend-tracker-app-1")["Id"] == app
        with pytest.raises(eng.EngineError):
            client.inspect("spend-tracker-app-1-previous")
        with pytest.raises(eng.EngineError):
            client.remove(parked, force=True)
    assert parked in fake.containers


# --------------------------------------------------------------------------- #
# The launcher
# --------------------------------------------------------------------------- #


def test_the_launcher_removes_the_updater_s_broken_one_offs_and_goes_on(tmp_path):
    project = _folder(tmp_path, "st", env=BUNDLE_ENV)
    fake = FakeEngine(engine_fixture("docker-desktop"), info_of("docker-desktop"))
    app = fake.add_container("spend-tracker-app-1-previous", PROJECT, State="exited")
    placard = _oneoff(fake, "spend-tracker-app-1-placard-11111111")
    drill = _oneoff(fake, "spend-tracker-app-1-drill-11111111", role="drill")
    fake.broken |= {placard, drill}
    with Running(fake) as running:
        status, out = launch.run(_args(project, running.socket_path))
    assert status == 0, out
    said = [v for k, v in _answer(out) if k == "SAY"]
    assert sorted(said) == sorted(
        f"Removed container {cid[:12]}, one of the updater's own temporary containers, "
        "whose files the container engine had lost."
        for cid in (placard, drill)
    )
    assert set(fake.containers) == {app}
    assert "SPENDTRACKER_ENGINE_SOCKET" in (project / ".env").read_text()


@pytest.mark.parametrize(
    ("fixture", "command"), [("docker-desktop", "docker"), ("podman-machine", "podman")]
)
def test_the_launcher_stops_on_an_owner_s_broken_container_and_names_it(tmp_path, fixture, command):
    project = _folder(tmp_path, "st", env=BUNDLE_ENV)
    before = (project / ".env").read_text()
    fake = FakeEngine(engine_fixture(fixture), info_of(fixture))
    theirs = fake.add_container("spend-tracker-backup-1", PROJECT, State="exited")
    fine = fake.add_container("spend-tracker-app-1", PROJECT)
    fake.broken.add(theirs)
    with Running(fake) as running:
        status, out = launch.run(_args(project, running.socket_path))
    assert status == 2
    [(key, said)] = _answer(out)
    assert key == "SAY" and f"`{command} rm -f {theirs}`" in said and theirs[:12] in said
    assert set(fake.containers) == {theirs, fine}
    assert (project / ".env").read_text() == before
