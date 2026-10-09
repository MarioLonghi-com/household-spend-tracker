"""#264: which engine the launchers use, when Docker and Podman are both on the host.

`docker compose version` reads only the client, so the launchers used to pick
Docker whenever its CLI was installed -- and told an owner whose Podman
machine was running, and held their install, to start Docker. The rule now
is `launch.pick_engine`: the engine that already holds the project, else one
that answers, Docker first in a tie; and only when neither answers, which
are installed and that one must be started.

The shell launcher is run headless against stubs of both CLIs, for every pair
of states, and must do what the rule says. The Windows one cannot run here;
it is held to the rule's sentences.
"""

from __future__ import annotations

import itertools
import subprocess

import pytest

from tests.test_bundle import BAT, LAUNCHER, _launch_headless
from updater import launch

pytestmark = pytest.mark.repo_wide

STATES = launch.ENGINE_STATES
PAIRS = list(itertools.product(STATES, STATES))
ALONG_THE_WAY = {
    launch.IN_BOTH,
    launch.UNCHECKED.format(other="Docker", chosen="Podman"),
    launch.UNCHECKED.format(other="Podman", chosen="Docker"),
}


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
        ("project", "project", "docker"),
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
    assert launch.pick_engine("project", "project").says == (launch.IN_BOTH,)
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
        launch.pick_engine("running", "none")


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
# The Windows launcher: the same sentences, the same order
# --------------------------------------------------------------------------- #


def _sentences() -> set[str]:
    """Every sentence the rule can say or stop with, but Linux's socket permission."""
    found = set()
    for docker, podman in PAIRS:
        if docker == "denied":
            continue
        pick = launch.pick_engine(docker, podman)
        found |= set(pick.says) | ({pick.stop} if pick.stop else set())
    return found


@pytest.mark.parametrize("text", [LAUNCHER, BAT], ids=["sh", "bat"])
def test_each_launcher_carries_every_sentence_of_the_rule(text):
    sentences = _sentences()
    assert len(sentences) == 7
    for sentence in sentences:
        assert sentence in text, sentence


@pytest.mark.parametrize(
    ("text", "order"),
    [
        (
            LAUNCHER,
            [
                'DOCKER_SEEN="$(seen docker)"',
                'PODMAN_SEEN="$(seen podman)"',
                'if [ "$DOCKER_SEEN" = project ]',
                'elif [ "$PODMAN_SEEN" = project ]',
                'elif [ "$DOCKER_SEEN" = answers ]',
                'elif [ "$PODMAN_SEEN" = answers ]',
            ],
        ),
        (
            BAT,
            [
                "call :seen docker DOCKER_SEEN",
                "call :seen podman PODMAN_SEEN",
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
