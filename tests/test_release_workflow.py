"""The release workflow's one approval gate (#96).

`publish` runs in the `release` environment, which carries a required
reviewer in the repository's settings. The other two publishing jobs are not
gated themselves: they are gated *through* `publish`, because they depend on
it. These tests keep it that way -- a publishing job that stopped depending on
`publish` would reach Releases or ghcr.io without anybody's click, and nothing
else would notice.

The Mac App Store upload (#351) is a publisher like the others, behind the
same gate. Its secrets sit in an `app-store` environment with no reviewer --
a tag-only store for secrets, not a second gate -- and that environment is
allowed on that job alone. The job that builds the app runs `npm ci` and
must never see a secret.

Each rule is checked against the real workflow, and then against a copy that
breaks it, so a check that passes everything cannot pass here.
"""

from __future__ import annotations

import copy
import pathlib

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
GATE = "publish"
PUBLISHING = ("publish", "publish-image", "publish-bundle", "publish-release", "publish-mac-store")
#: The environments a job may run in, and the one job each is for.
ENVIRONMENTS = {GATE: "release", "publish-mac-store": "app-store"}
#: Jobs that install packages, and so must hold nothing secret.
BUILDERS = ("build", "image", "store-build")


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
    # The release zip names the images by the digests `publish-image` pushed,
    # and the release must not go public without it (#167).
    if "publish-bundle" in jobs:
        if not _depends_on(jobs, "publish-bundle", "publish-image"):
            found.append("`publish-bundle` can run before the images are pushed")
        if not _depends_on(jobs, "publish-release", "publish-bundle"):
            found.append("`publish-release` can make the release public without its zip")
    for name, job in jobs.items():
        wanted = ENVIRONMENTS.get(name)
        if job.get("environment") != wanted:
            found.append(f"`{name}` runs in environment {job.get('environment')!r}, expected {wanted!r}")
    # The App Store upload is not uploaded for a release that did not go out.
    if "publish-mac-store" in jobs and not _depends_on(jobs, "publish-mac-store", "publish-release"):
        found.append("`publish-mac-store` can upload before the release is public")
    for name in BUILDERS:
        if name in jobs and "secrets." in yaml.safe_dump(jobs[name]):
            found.append(f"`{name}` installs packages and reads a secret")
    return found


def test_the_real_workflow_has_one_gate_and_every_publisher_behind_it() -> None:
    jobs = _jobs()
    assert set(PUBLISHING) <= set(jobs)
    assert problems(jobs) == []


def test_a_publisher_that_stops_needing_the_gate_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    jobs["publish-release"]["needs"] = ["build"]
    assert problems(jobs) == [
        "`publish-release` can run without `publish`'s approval",
        # And the upload behind it loses the gate with it (#351).
        "`publish-mac-store` can run without `publish`'s approval",
        "`publish-release` can make the release public without its zip",
    ]


def test_losing_the_environment_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    del jobs[GATE]["environment"]
    assert "`publish` does not run in the `release` environment" in problems(jobs)


def test_a_second_gated_job_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    jobs["publish-image"]["environment"] = "release"
    assert problems(jobs) == [
        "`publish-image` runs in environment 'release', expected None"
    ]


def test_the_app_store_environment_belongs_to_the_upload_alone() -> None:
    """#351: `app-store` is where the signing secrets live. On any other job --
    or the release gate moved onto the upload -- it is caught."""
    jobs = copy.deepcopy(_jobs())
    jobs["publish-image"]["environment"] = "app-store"
    assert problems(jobs) == [
        "`publish-image` runs in environment 'app-store', expected None"
    ]
    jobs = copy.deepcopy(_jobs())
    jobs["publish-mac-store"]["environment"] = "release"
    assert problems(jobs) == [
        "`publish-mac-store` runs in environment 'release', expected 'app-store'"
    ]


def test_an_upload_before_the_release_is_public_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    jobs["publish-mac-store"]["needs"] = ["build", "store-build", "publish"]
    assert problems(jobs) == ["`publish-mac-store` can upload before the release is public"]
    jobs["publish-mac-store"]["needs"] = ["build", "store-build"]
    assert "`publish-mac-store` can run without `publish`'s approval" in problems(jobs)


def test_an_upload_on_a_branch_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    del jobs["publish-mac-store"]["if"]
    assert problems(jobs) == ["`publish-mac-store` is not limited to tag pushes"]


def test_a_builder_that_reads_a_secret_is_caught() -> None:
    """The job that runs `npm ci` must not hold the signing secrets: anything a
    compromised dependency ran would run beside them."""
    jobs = copy.deepcopy(_jobs())
    jobs["store-build"]["env"]["P12"] = "${{ secrets.MAS_CERTIFICATES_P12 }}"
    assert problems(jobs) == ["`store-build` installs packages and reads a secret"]


def test_a_publisher_that_runs_on_a_branch_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    del jobs["publish-image"]["if"]
    assert problems(jobs) == ["`publish-image` is not limited to tag pushes"]


def test_a_zip_built_before_the_images_are_pushed_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    jobs["publish-bundle"]["needs"] = ["build", "publish"]
    assert problems(jobs) == ["`publish-bundle` can run before the images are pushed"]


def test_a_release_that_goes_public_without_its_zip_is_caught() -> None:
    jobs = copy.deepcopy(_jobs())
    jobs["publish-release"]["needs"] = ["build", "publish-image"]
    assert problems(jobs) == ["`publish-release` can make the release public without its zip"]


def _run(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_the_zip_is_checked_attested_and_attached_with_the_pushed_digests() -> None:
    job = _jobs()["publish-bundle"]
    run = _run(job)
    assert "needs.publish-image.outputs.digest" in str(job)
    assert "needs.publish-image.outputs.updater-digest" in str(job)
    assert "python -m scripts.bundle --check" in run
    assert "Start Spend Tracker.command" in run and "zipinfo" in run
    assert "gh release upload" in run
    attest = [s for s in job["steps"] if "attest-build-provenance" in s.get("uses", "")]
    assert [s["with"]["subject-path"] for s in attest] == ["dist/*-compose.zip"]
    assert "npm" not in run


def test_every_run_builds_and_checks_a_stand_in_zip() -> None:
    build = _jobs()["build"]
    step = next(
        s for s in build["steps"] if s.get("name") == "the release zip builds, from stand-in digests"
    )
    assert "if" not in step
    assert "python -m scripts.bundle --check" in step["run"]
