"""The `ci-ok` verdict passes only a run that did the work (#407).

The first case is the run GitHub abandoned while #405 was being built: its
`needs` are copied from that run's `ci-ok` log, which said ok.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import yaml

from scripts.ci_ok import problems

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"

#: `ci-ok`'s needs on run 42672482324, attempt 1 (#405), which passed.
ABANDONED_405 = {
    "changes": "abandoned",
    "release-ready": "skipped",
    "lint": "skipped",
    "repo-wide": "abandoned",
    "pytest": "skipped",
    "pytest-desktop-os": "skipped",
    "desktop-app": "skipped",
    "client": "skipped",
    "rehearsal": "skipped",
    "audit": "skipped",
    "dependency-review": "abandoned",
    "image": "skipped",
    "lock": "skipped",
    "self-update": "skipped",
}


def _needs(results: dict[str, str]) -> dict:
    return {name: {"result": result, "outputs": {}} for name, result in results.items()}


def _run(needs_json: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.ci_ok"],
        cwd=ROOT,
        env={"NEEDS": needs_json, "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_abandoned_run_from_405_fails():
    found = problems(_needs(ABANDONED_405))
    assert found == [
        "changes: abandoned",
        "dependency-review: abandoned",
        "repo-wide: abandoned",
    ]


def test_a_run_of_successes_and_deliberate_skips_passes():
    results = dict.fromkeys(ABANDONED_405, "skipped")
    results.update(changes="success", lint="success", pytest="success")
    assert problems(_needs(results)) == []


def test_every_result_outside_the_allow_list_fails():
    for result in ("failure", "cancelled", "abandoned", "", None, "neutral", "timed_out"):
        needs = _needs({"changes": "success", "pytest": "success"})
        needs["pytest"]["result"] = result
        assert problems(needs) == [f"pytest: {result or 'no result'}"], result


def test_a_skip_is_only_deliberate_when_changes_ran():
    needs = _needs({"changes": "skipped", "pytest": "skipped"})
    assert problems(needs) == ["changes: skipped, so no skip after it was decided"]


def test_changes_missing_or_nothing_at_all_fails():
    assert problems(_needs({"pytest": "success"})) == ["changes: not among the jobs ci-ok needs"]
    assert problems({}) == ["no job results at all"]
    assert problems({"changes": None, "lint": {}}) == ["changes: no result", "lint: no result"]


def test_the_script_exits_non_zero_and_names_every_problem():
    bad = _run(json.dumps(_needs(ABANDONED_405)))
    assert bad.returncode == 1
    assert bad.stdout.splitlines()[-1] == (
        "not ok: changes: abandoned; dependency-review: abandoned; repo-wide: abandoned"
    )
    good = _run(json.dumps(_needs({"changes": "success", "lint": "skipped"})))
    assert (good.returncode, good.stdout.splitlines()) == (0, ["changes: success", "lint: skipped", "ok"])
    assert _run("").returncode == 1
    assert _run("{not json").returncode == 1


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_ci_ok_needs_every_job_in_the_workflow():
    jobs = _jobs()
    needs = jobs["ci-ok"]["needs"]
    assert sorted(needs) == sorted(name for name in jobs if name != "ci-ok")


def test_ci_ok_always_runs_and_hands_its_needs_to_the_script():
    job = _jobs()["ci-ok"]
    assert job["if"] == "always()"
    verdict = [step for step in job["steps"] if "scripts.ci_ok" in step.get("run", "")]
    assert len(verdict) == 1
    assert verdict[0]["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    # Nothing in the verdict may be decided by an inline deny-list again.
    assert not any("cancelled" in step.get("run", "") for step in job["steps"])


def test_pushes_to_dev_queue_and_pull_requests_cancel():
    """A newer push to `dev` must not cancel the deferred tier already running."""
    concurrency = yaml.safe_load(WORKFLOW.read_text())["concurrency"]
    assert "'dev'" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == (
        "${{ !(github.event_name == 'push' && github.ref == 'refs/heads/dev') }}"
    )
