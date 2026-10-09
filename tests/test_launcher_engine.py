"""#264, #300: which engine the launchers use, when Docker and Podman are both on the host.

`docker compose version` reads only the client, so the launchers used to pick
Docker whenever its CLI was installed -- and told an owner whose Podman
machine was running, and held their install, to start Docker. Then, with an
install in each, "Docker wins" started a stopped, stale Docker install, which
replaced its updater and collided on the port with the Podman one that was
running (#300). The rule now is `launch.pick_engine`: the engine whose
Spend Tracker is running; else the one that holds the project -- both
holding it, the newer ledger, said out loud, or a stop asking the owner to
remove one; else one that answers, Docker first in a tie; and only when
neither answers, which are installed and that one must be started. Before
anything is changed, the published port must be free or the chosen
install's own (`launch.port_refusal`).

The shell launcher is run headless against stubs of both CLIs, for every pair
of states, and must do what the rule says. The Windows one cannot run here;
it is held to the rule's sentences.
"""

from __future__ import annotations

import itertools
import subprocess

import pytest
import yaml

from tests.test_bundle import BAT, LAUNCHER, ROOT, _launch_headless
from updater import launch

pytestmark = pytest.mark.repo_wide

STATES = launch.ENGINE_STATES
PAIRS = list(itertools.product(STATES, STATES))
ALONG_THE_WAY = {
    launch.IN_BOTH_RUNNING.format(chosen="Docker"),
    launch.IN_BOTH_RUNNING.format(chosen="Podman"),
    launch.IN_BOTH_NEWER.format(chosen="Docker"),
    launch.IN_BOTH_NEWER.format(chosen="Podman"),
    launch.UNCHECKED.format(other="Docker", chosen="Podman"),
    launch.UNCHECKED.format(other="Podman", chosen="Docker"),
}

APP = "ghcr.io/mariolonghi-com/household-spend-tracker"


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("docker", "podman", "engine"),
    [
        # The issue: Docker Desktop installed but stopped, a Podman machine running.
        ("installed", "answers", "podman"),
        ("installed", "project", "podman"),
        # Both run: the one holding the project, whichever it is.
        ("answers", "project", "podman"),
        ("project", "answers", "docker"),
        # #300: the install that is running wins over one that is not,
        # whichever engine each is in.
        ("project", "running", "podman"),
        ("running", "project", "docker"),
        ("running", "answers", "docker"),
        ("installed", "running", "podman"),
        # Both run, neither holds it: Docker, as before.
        ("answers", "answers", "docker"),
        ("answers", "installed", "docker"),
        ("none", "answers", "podman"),
        ("answers", "none", "docker"),
        ("denied", "answers", "podman"),
    ],
)
def test_the_engine_holding_the_project_wins_then_one_that_answers(docker, podman, engine):
    pick = launch.pick_engine(docker, podman)
    assert (pick.engine, pick.product, pick.stop) == (engine, engine.capitalize(), None)


@pytest.mark.parametrize(
    ("docker", "podman", "stop"),
    [
        ("none", "none", launch.NOT_FOUND),
        ("installed", "none", launch.NOT_RUNNING.format(product="Docker")),
        ("none", "installed", launch.NOT_RUNNING.format(product="Podman")),
        ("installed", "installed", launch.NEITHER_RUNNING),
        ("installed", "denied", launch.NEITHER_RUNNING),
        ("denied", "installed", launch.DENIED.format(user="pat")),
        ("denied", "none", launch.DENIED.format(user="pat")),
    ],
)
def test_when_neither_answers_it_says_which_are_installed(docker, podman, stop):
    pick = launch.pick_engine(docker, podman, user="pat")
    assert (pick.engine, pick.stop, pick.says) == (None, stop, ())


def test_what_it_says_on_the_way():
    assert launch.pick_engine("project", "running").says == (
        "Spend Tracker is installed in both Docker and Podman; this uses the one running in Podman.",
    )
    assert launch.pick_engine("running", "answers").says == ()
    assert launch.pick_engine("installed", "answers").says == (
        "Docker is installed but not running, so whether Spend Tracker is already installed there "
        "was not checked; this starts it in Podman.",
    )
    assert launch.pick_engine("answers", "denied").says == (
        launch.UNCHECKED.format(other="Podman", chosen="Docker"),
    )
    # The project is found: nothing elsewhere matters.
    assert launch.pick_engine("installed", "project").says == ()
    assert launch.pick_engine("answers", "answers").says == ()
    with pytest.raises(ValueError):
        launch.pick_engine("started", "none")


def test_both_running_is_said_and_stops():
    pick = launch.pick_engine("running", "running")
    assert (pick.engine, pick.says, pick.stop) == (None, (), launch.BOTH_RUNNING)
    assert "which should not happen" in pick.stop


def _install(release: str | None, started: str | None = None) -> launch.Install:
    return launch.Install(f"{APP}:{release}@sha256:{'a' * 64}" if release else None, started)


@pytest.mark.parametrize(
    ("docker", "podman", "engine"),
    [
        # The issue's two installs, had the Podman one been stopped too.
        ("0.9.1", "0.9.3", "podman"),
        ("0.9.3", "0.9.1", "docker"),
        # Compared as releases, not as text.
        ("0.10.0", "0.9.3", "docker"),
    ],
)
def test_both_holding_it_and_neither_running_takes_the_newer_ledger_and_says_both(docker, podman, engine):
    pick = launch.pick_engine(
        "project",
        "project",
        installs={
            "docker": _install(docker, "2026-10-08T21:14:03.123456789Z"),
            "podman": _install(podman, "2026-10-09 07:02:11.5 +0200 CEST"),
        },
    )
    assert (pick.engine, pick.stop) == (engine, None)
    assert pick.says == (
        f"In Docker: the ledger is at {docker}; it was last started on 2026-10-08 21:14 UTC.",
        f"In Podman: the ledger is at {podman}; it was last started on 2026-10-09 07:02 CEST.",
        launch.IN_BOTH_NEWER.format(chosen=engine.capitalize()),
    )


@pytest.mark.parametrize(
    ("docker", "podman", "why"),
    [
        ("0.9.3", "0.9.3", launch.SAME_RELEASE),
        (None, "0.9.3", launch.UNREAD_RELEASE),
        ("0.9.3", None, launch.UNREAD_RELEASE),
        (None, None, launch.UNREAD_RELEASE),
    ],
)
def test_both_holding_it_at_one_release_or_an_unread_one_stops_and_asks_to_remove_one(docker, podman, why):
    pick = launch.pick_engine(
        "project", "project", installs={"docker": _install(docker), "podman": _install(podman)}
    )
    assert pick.engine is None
    assert pick.stop == launch.IN_BOTH_UNDECIDED.format(why=why)
    assert 'README.txt, under "Installed in both Docker and Podman"' in pick.stop
    assert pick.says == (
        f"In Docker: the ledger is at {docker or 'an unknown release'}; it was last started at an unknown time.",
        f"In Podman: the ledger is at {podman or 'an unknown release'}; it was last started at an unknown time.",
    )


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("2026-10-08T21:14:03.123456789Z", "2026-10-08 21:14 UTC"),
        ("2026-10-09 07:02:11.5 +0200 CEST", "2026-10-09 07:02 CEST"),
        ("2026-10-09T07:02:11+02:00", "2026-10-09 07:02"),
        ("0001-01-01T00:00:00Z", None),
        ("", None),
        (None, None),
        ("soon", None),
    ],
)
def test_when_it_was_last_started_is_read_to_the_minute(raw, text):
    assert launch.started_text(raw) == text


@pytest.mark.parametrize(
    ("chosen", "holder", "said"),
    [
        ("docker", "free", None),
        ("docker", "self", None),
        ("docker", "docker", None),
        (
            "docker",
            "podman",
            "Port 8848 on this computer is in use by the Spend Tracker in Podman, so the one in Docker was "
            "not started, and nothing was changed. Stop the one in Podman, or remove it if you no longer use "
            'it (README.txt, under "Installed in both Docker and Podman", says how), then open this '
            "launcher again.",
        ),
        (
            "podman",
            "program",
            "Port 8848 on this computer is in use by another program, so Spend Tracker was not started in "
            "Podman, and nothing was changed. Close that program, then open this launcher again.",
        ),
    ],
)
def test_a_taken_port_is_said_in_plain_words_naming_the_other_install(chosen, holder, said):
    assert launch.port_refusal(chosen, holder) == said
    if said is None:
        assert launch.port_refusal(chosen, holder, after_up=True) is None
    else:
        after = launch.port_refusal(chosen, holder, after_up=True)
        assert after.startswith(f"{chosen.capitalize()} could not start Spend Tracker because port 8848 ")
        assert "nothing was changed" not in after and "the lines above say why" not in after
    with pytest.raises(ValueError):
        launch.port_refusal("docker", "elsewhere")


def test_the_port_is_the_one_the_bundle_s_compose_file_publishes():
    doc = yaml.safe_load((ROOT / "deploy" / "bundle" / "compose.yaml").read_text())
    assert doc["services"]["app"]["ports"] == [f"127.0.0.1:{launch.PORT}:8848"]
    assert f"PORT={launch.PORT}\n" in LAUNCHER
    assert f'set "PORT={launch.PORT}"' in BAT


# --------------------------------------------------------------------------- #
# The shell launcher, against the rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("docker", "podman"), PAIRS, ids=[f"docker-{d}/podman-{p}" for d, p in PAIRS])
def test_the_shell_launcher_picks_as_the_rule_does(tmp_path, docker, podman):
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    pick = launch.pick_engine(docker, podman, user=user)
    kind = "podman-machine" if pick.engine == "podman" else "docker-desktop"
    status, out, ups, _, calls = _launch_headless(
        tmp_path, ("docker", "podman"), "", kind=kind, states={"docker": docker, "podman": podman}
    )
    asked = {line.split(" ", 1)[0] for line in calls.splitlines() if " run --rm " in line}
    for sentence in pick.says:
        assert sentence in out
    if pick.engine is None:
        assert status == 1, out
        assert pick.stop in out
        assert asked == set() and ups == []
    else:
        assert status == 0, out
        assert asked == {pick.engine}, calls
        assert ups == [f"{pick.engine} compose --env-file .env up -d|"], ups
        assert f"Checking {pick.product} and this folder..." in out
        for sentence in pick.says:
            assert sentence in out
    # And nothing the rule does not say for this pair.
    for sentence in ALONG_THE_WAY - set(pick.says):
        assert sentence not in out


# --------------------------------------------------------------------------- #
# #300: the running install, the newer ledger, the port -- headless
# --------------------------------------------------------------------------- #

DIGEST = "sha256:" + "c" * 64
DOCKER_STARTED = "2026-10-08T21:14:03.123456789Z"
PODMAN_STARTED = "2026-10-09 07:02:11.5 +0200 CEST"


def _run(tmp_path, docker, podman, **env):
    """The shell launcher against both engines' stubs; (status, output, ups, every engine call)."""
    status, out, ups, _, calls = _launch_headless(
        tmp_path, ("docker", "podman"), "", states={"docker": docker, "podman": podman}, extra_env=env
    )
    return status, out, ups, calls


def _changed(calls: str) -> list[str]:
    """The engine calls that change something: the updater asked, a container stopped or removed, `up`."""
    verbs = (" run --rm ", " stop ", " rm ", " rm -f ", " compose --env-file ")
    return [line for line in calls.splitlines() if any(v in f" {line.split('|')[0]} " for v in verbs)]


def test_the_issue_the_running_podman_install_is_used_and_docker_s_is_left_alone(tmp_path):
    """#300 as found: Docker's install stopped at 0.9.1, Podman's running and serving the port."""
    status, out, ups, calls = _run(
        tmp_path, "project", "running", PODMAN_PORT="8848", PORT_TAKEN="1",
        DOCKER_IMAGE=f"{APP}:0.9.1@{DIGEST}",
    )  # fmt: skip
    assert status == 0, out
    assert launch.IN_BOTH_RUNNING.format(chosen="Podman") in out
    assert ups == ["podman compose --env-file .env up -d|"]
    assert [c for c in _changed(calls) if c.startswith("docker ")] == []
    assert "Port 8848" not in out


def _pinned_folder(tmp_path, name: str, release: str, where: str = ".env") -> str:
    folder = tmp_path / name
    (folder / "pin").mkdir(parents=True)
    (folder / where).write_text(
        f"SPENDTRACKER_PUBLIC_URL=http://localhost:8848\nSPENDTRACKER_IMAGE='{APP}:{release}@{DIGEST}'\n"
    )
    return str(folder)


@pytest.mark.parametrize(
    ("docker", "podman", "engine"),
    [("0.9.1", "0.9.3", "podman"), ("0.10.0", "0.9.3", "docker")],
)
def test_both_stopped_the_shell_launcher_says_both_and_takes_the_newer_ledger(
    tmp_path, docker, podman, engine
):
    """Docker's release is read from its folder's pin (`.env`), Podman's from
    `pin/release.env`; the shell's lines are the rule's, word for word."""
    status, out, ups, calls = _run(
        tmp_path, "project", "project",
        DOCKER_DIR=_pinned_folder(tmp_path, "docker-install", docker),
        PODMAN_DIR=_pinned_folder(tmp_path, "podman-install", podman, "pin/release.env"),
        DOCKER_STARTED=DOCKER_STARTED, PODMAN_STARTED=PODMAN_STARTED,
        # What the containers run is not what the ledger is at: the pin wins, as in C5.
        DOCKER_IMAGE=f"{APP}:0.1.0@{DIGEST}", PODMAN_IMAGE=f"{APP}:0.1.0@{DIGEST}",
    )  # fmt: skip
    pick = launch.pick_engine(
        "project",
        "project",
        installs={
            "docker": launch.Install(f"{APP}:{docker}@{DIGEST}", DOCKER_STARTED),
            "podman": launch.Install(f"{APP}:{podman}@{DIGEST}", PODMAN_STARTED),
        },
    )
    assert pick.engine == engine
    assert status == 0, out
    lines = out.splitlines()
    assert [line for line in lines if line in pick.says] == list(pick.says)
    assert ups == [f"{engine} compose --env-file .env up -d|"]
    other = "podman" if engine == "docker" else "docker"
    assert [c for c in _changed(calls) if c.startswith(f"{other} ")] == []


@pytest.mark.parametrize(
    ("docker_image", "podman_image", "why"),
    [
        (f"{APP}:0.9.3@{DIGEST}", f"{APP}:v0.9.3@{DIGEST}", launch.SAME_RELEASE),
        (f"{APP}:0.9.3@{DIGEST}", f"{APP}@{DIGEST}", launch.UNREAD_RELEASE),
    ],
)
def test_both_stopped_at_one_release_the_shell_launcher_stops_and_changes_nothing(
    tmp_path, docker_image, podman_image, why
):
    """No folder to read a pin from: the app containers' images say the release."""
    status, out, ups, calls = _run(
        tmp_path, "project", "project", DOCKER_IMAGE=docker_image, PODMAN_IMAGE=podman_image,
        DOCKER_STARTED=DOCKER_STARTED,
    )  # fmt: skip
    pick = launch.pick_engine(
        "project",
        "project",
        installs={
            "docker": launch.Install(docker_image, DOCKER_STARTED),
            "podman": launch.Install(podman_image),
        },
    )
    assert pick.stop == launch.IN_BOTH_UNDECIDED.format(why=why)
    assert status == 1 and ups == [] and _changed(calls) == [], out
    assert pick.stop in out
    for sentence in pick.says:
        assert sentence in out


def test_both_running_the_shell_launcher_says_so_and_stops(tmp_path):
    status, out, ups, calls = _run(tmp_path, "running", "running")
    assert status == 1 and ups == [] and _changed(calls) == [], out
    assert launch.BOTH_RUNNING in out


@pytest.mark.parametrize(
    ("docker", "podman", "env", "holder", "engine"),
    [
        # Docker's ledger is newer, but Podman's maintenance page holds the port.
        ("project", "project", {"PODMAN_PORT": "8848"}, "podman", "docker"),
        # Something that is not Spend Tracker at all.
        ("answers", "answers", {}, "program", "docker"),
        ("installed", "project", {}, "program", "podman"),
    ],
)
def test_a_taken_port_stops_the_shell_launcher_before_it_changes_anything(
    tmp_path, docker, podman, env, holder, engine
):
    env = {**env, "PORT_TAKEN": "1"}
    if docker == podman == "project":
        env |= {"DOCKER_IMAGE": f"{APP}:0.9.3@{DIGEST}", "PODMAN_IMAGE": f"{APP}:0.9.1@{DIGEST}"}
    status, out, ups, calls = _run(tmp_path, docker, podman, **env)
    assert status == 1, out
    assert launch.port_refusal(engine, holder) in out
    # Nothing replaced, stopped, removed or started; `.env` as it was.
    assert ups == [] and _changed(calls) == [], calls
    assert (
        tmp_path / "Spend Tracker" / ".env"
    ).read_text() == "SPENDTRACKER_PUBLIC_URL=http://localhost:8848\n"


def test_the_port_held_by_the_chosen_install_itself_is_not_a_refusal(tmp_path):
    """Its own app (or maintenance page) holds it: `up` replaces those."""
    status, out, ups, _ = _run(tmp_path, "running", "answers", DOCKER_PORT="8848", PORT_TAKEN="1")
    assert status == 0, out
    assert ups == ["docker compose --env-file .env up -d|"]
    assert "Port 8848" not in out


@pytest.mark.parametrize(
    ("env", "said"),
    [
        (
            {"PORT_TAKEN_AFTER_UP": "1", "PODMAN_PORT": "8848"},
            launch.port_refusal("docker", "podman", after_up=True),
        ),
        ({"PORT_TAKEN_AFTER_UP": "1"}, launch.port_refusal("docker", "program", after_up=True)),
        ({}, launch.UP_FAILED.format(chosen="Docker")),
    ],
)
def test_a_failed_up_names_the_cause_when_it_is_known(tmp_path, env, said):
    status, out, _, _ = _run(tmp_path, "project", "project" if "PODMAN_PORT" in env else "answers",
                             STUB_UP_STATUS="1", DOCKER_IMAGE=f"{APP}:0.9.3@{DIGEST}",
                             PODMAN_IMAGE=f"{APP}:0.9.1@{DIGEST}", **env)  # fmt: skip
    assert status == 1, out
    assert said in out
    if said != launch.UP_FAILED.format(chosen="Docker"):
        assert "the lines above say why" not in out


# --------------------------------------------------------------------------- #
# The Windows launcher: the same sentences, the same order
# --------------------------------------------------------------------------- #


def _sentences() -> set[str]:
    """Every fixed sentence the rule can say or stop with, but Linux's socket permission."""
    found = set()
    for docker, podman in PAIRS:
        if docker == "denied" or docker == podman == "project":
            continue
        pick = launch.pick_engine(docker, podman)
        found |= set(pick.says) | ({pick.stop} if pick.stop else set())
    for why in (launch.SAME_RELEASE, launch.UNREAD_RELEASE):
        found.add(launch.IN_BOTH_UNDECIDED.format(why=why))
    for chosen in ("Docker", "Podman"):
        found.add(launch.IN_BOTH_NEWER.format(chosen=chosen))
    return found


@pytest.mark.parametrize("text", [LAUNCHER, BAT], ids=["sh", "bat"])
def test_each_launcher_carries_every_sentence_of_the_rule(text):
    sentences = _sentences()
    assert len(sentences) == 13
    plain = text.replace('\\"', '"')  # the shell escapes the quotes it says
    undecided = {
        launch.IN_BOTH_UNDECIDED.format(why=why) for why in (launch.SAME_RELEASE, launch.UNREAD_RELEASE)
    }
    for sentence in sentences:
        if text is LAUNCHER and sentence in undecided:
            # The shell spells it once, with $why; both reasons are beside it.
            assert launch.IN_BOTH_UNDECIDED.format(why="$why") in plain
            assert f'why="{launch.SAME_RELEASE}"' in text and f'why="{launch.UNREAD_RELEASE}"' in text
            continue
        assert sentence in plain, sentence


#: How each launcher spells the port, the chosen engine and the other one.
SPELLING = {
    "sh": ("$PORT", "$PRODUCT", "$OTHER_PRODUCT"),
    "bat": ("%PORT%", "%PRODUCT%", "%OTHER_PRODUCT%"),
}


@pytest.mark.parametrize(("text", "kind"), [(LAUNCHER, "sh"), (BAT, "bat")], ids=["sh", "bat"])
def test_each_launcher_carries_the_port_sentences_and_the_install_line(text, kind):
    port, chosen, other = SPELLING[kind]
    for holder in ("program", "other"):
        for after_up in (False, True):
            template = {
                ("program", False): launch.PORT_BY_PROGRAM,
                ("program", True): launch.UP_PORT_BY_PROGRAM,
                ("other", False): launch.PORT_BY_INSTALL,
                ("other", True): launch.UP_PORT_BY_INSTALL,
            }[holder, after_up]
            sentence = template.format(port=port, chosen=chosen, other=other)
            assert sentence in text.replace('\\"', '"'), sentence
    assert launch.UP_FAILED.format(chosen=chosen) in text
    # The port is checked before the updater is asked, so a refusal changes nothing.
    assert (
        text.find("nothing was changed")
        < text.find("updater.launch")
        < text.find("compose --env-file .env up -d")
    )


@pytest.mark.parametrize(
    ("text", "order"),
    [
        (
            LAUNCHER,
            [
                'DOCKER_SEEN="$(seen docker)"',
                'PODMAN_SEEN="$(seen podman)"',
                'if [ "$DOCKER_SEEN" = running ] && [ "$PODMAN_SEEN" = running ]; then',
                'elif [ "$DOCKER_SEEN" = running ]; then',
                'elif [ "$PODMAN_SEEN" = running ]; then',
                'elif [ "$DOCKER_SEEN" = project ] && [ "$PODMAN_SEEN" = project ]; then',
                'elif [ "$DOCKER_SEEN" = project ]; then',
                'elif [ "$PODMAN_SEEN" = project ]; then',
                'elif [ "$DOCKER_SEEN" = answers ]; then',
                'elif [ "$PODMAN_SEEN" = answers ]; then',
            ],
        ),
        (
            BAT,
            [
                "call :seen docker DOCKER_SEEN",
                "call :seen podman PODMAN_SEEN",
                'if "%DOCKER_SEEN%"=="running" if "%PODMAN_SEEN%"=="running" (',
                'if "%DOCKER_SEEN%"=="running" (\n  set "ENGINE=docker"',
                'if "%PODMAN_SEEN%"=="running" (\n  set "ENGINE=podman"',
                'if "%DOCKER_SEEN%"=="project" if "%PODMAN_SEEN%"=="project" goto :both',
                'if "%DOCKER_SEEN%"=="project" (',
                'if "%PODMAN_SEEN%"=="project" (',
                'if "%DOCKER_SEEN%"=="answers" (',
                'if "%PODMAN_SEEN%"=="answers" (',
            ],
        ),
    ],
    ids=["sh", "bat"],
)
def test_each_launcher_asks_in_the_rule_s_order_and_never_by_the_client_alone(text, order):
    at = [text.find(step) for step in order]
    assert -1 not in at and at == sorted(at), list(zip(order, at, strict=True))
    # The old test, `docker compose version` deciding by itself, is gone.
    assert "docker compose version >nul 2>&1 && set" not in text
    assert "command -v docker >/dev/null 2>&1 && docker compose version" not in text


# --------------------------------------------------------------------------- #
# With #262: the engine first, then the broken-container check against it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("docker", "podman", "engine"), [("installed", "project", "podman"), ("project", "answers", "docker")]
)
def test_the_broken_container_check_runs_in_the_engine_the_rule_picked(tmp_path, docker, podman, engine):
    """`updater.launch` (where `clear_broken` lives) runs only in the engine
    `pick_engine` chose, and its stop is the launcher's: nothing is started."""
    said = (
        "The container engine has lost the files of container c9454cf4abcd, so it will not "
        "list Spend Tracker's containers. Remove it, then open this launcher again."
    )
    status, out, ups, _, calls = _launch_headless(
        tmp_path,
        ("docker", "podman"),
        "",
        states={"docker": docker, "podman": podman},
        answer_text=f"SAY={said}\n",
        answer_status=2,
    )
    asked = [line.split(" ", 1)[0] for line in calls.splitlines() if " run --rm " in line]
    assert asked == [engine], calls
    assert status == 1 and ups == [], out
    assert said in out and "Spend Tracker was not started." in out
