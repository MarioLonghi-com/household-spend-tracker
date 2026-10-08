"""The release workflow's one approval gate (#96).

`publish` runs in the `release` environment, which carries a required
reviewer in the repository's settings. The other two publishing jobs are not
gated themselves: they are gated *through* `publish`, because they depend on
it. These tests keep it that way -- a publishing job that stopped depending on
`publish` would reach Releases or ghcr.io without anybody's click, and nothing
else would notice.

Each rule is checked against the real workflow, and then against a copy that
breaks it, so a check that passes everything cannot pass here.
"""

from __future__ import annotations

import copy
import pathlib

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
GATE = "publish"
PUBLISHING = ("publish", "publish-image", "publish-release")


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _depends_on(jobs: dict, name: str, target: str) -> bool:
    """Whether `name` needs `target`, directly or through other jobs."""
    seen: set[str] = set()
    todo = _needs(jobs[name])
    while todo:
        current = todo.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            todo.extend(_needs(jobs[current]))
    return False


def problems(jobs: dict) -> list[str]:
    found = []
    if jobs[GATE].get("environment") != "release":
        found.append(f"`{GATE}` does not run in the `release` environment")
    for name in PUBLISHING:
        if jobs[name].get("if") != "github.ref_type == 'tag'":
            found.append(f"`{name}` is not limited to tag pushes")
        if name != GATE and not _depends_on(jobs, name, GATE):
            found.append(f"`{name}` can run without `{GATE}`'s approval")
    gated = sorted(n for n, j in jobs.items() if j.get("environment"))
    if gated != [GATE]:
        found.append(f"jobs with an environment: {gated}, expected only `{GATE}`")
    return found


def test_the_real_workflow_has_one_gate_and_every_publisher_behind_it() -> None:
    jobs = _jobs()
    assert set(PUBLISHING) <= set(jobs)
    assert problems(jobs) == []


def test_a_publisher_that_stops_needing_the_gate_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    jobs["publish-release"]["needs"] = ["build"]
    assert problems(jobs) == ["`publish-release` can run without `publish`'s approval"]


def test_losing_the_environment_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    del jobs[GATE]["environment"]
    assert "`publish` does not run in the `release` environment" in problems(jobs)


def test_a_second_gated_job_is_caught() -> None:
    """Three approvals per release is the failure this design avoids."""
    jobs = copy.deepcopy(_jobs())
    jobs["publish-image"]["environment"] = "release"
    assert problems(jobs) == [
        "jobs with an environment: ['publish', 'publish-image'], expected only `publish`"
    ]


def test_a_publisher_that_runs_on_a_branch_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    del jobs["publish-image"]["if"]
    assert problems(jobs) == ["`publish-image` is not limited to tag pushes"]
