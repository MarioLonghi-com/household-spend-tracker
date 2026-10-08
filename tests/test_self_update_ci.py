"""The CI-only updater image stays CI-only (design notes 15.5, V5; #169).

The end-to-end self-update job runs the updater in its container with a
test-only trust policy, which "verifies" whatever digests the job pushed. It
is built `FROM` the release's real updater image by
`tests/self_update/ci-updater.Dockerfile`, as `spend-tracker-ci-updater`, and
pushed only to the registry that answers as ghcr.io on the job's own runner.
If that image, or the policy, ever reached a published release, every
instance it updated would accept any image at all. So:

- **release.yml never builds, tags, pushes or names it**: not the Dockerfile,
  not the image name, not the policy's modules, and no build in it takes a
  Dockerfile or a context from under `tests/`;
- **the published updater target carries no `tests/`**: none of the stages
  it is built from copies anything from there, and its entry point is
  `python -m updater`, which builds only `trust.Sigstore`;
- **the CI image is the real one plus the policy**: `FROM` the image it is
  given, adding only `tests/self_update`'s policy and entry point;
- the job's driver pushes only to the loopback registry.

Each rule is checked against the real files, then against a copy that breaks
it, which must be named: a check that passes everything cannot pass here.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from tests.self_update import stage
from tests.test_updater_image import stages

pytestmark = pytest.mark.repo_wide

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
DOCKERFILE = ROOT / "Dockerfile"
CI_DOCKERFILE = ROOT / "tests" / "self_update" / "ci-updater.Dockerfile"

#: What release.yml may never mention.
FORBIDDEN = (
    "ci-updater.Dockerfile",
    "spend-tracker-ci-updater",
    "ci_updater",
    "ci_trust",
    "CiTrust",
    "ci-trust.json",
)


def release_problems(text: str) -> list[str]:
    found = [f"release.yml names {word!r}" for word in FORBIDDEN if word in text]
    doc = yaml.safe_load(text)
    for name, job in (doc.get("jobs") or {}).items():
        for i, step in enumerate(job.get("steps") or []):
            where = f"{name} step {step.get('name') or i}"
            given = step.get("with") or {}
            for key in ("file", "context"):
                value = str(given.get(key) or "")
                if value.lstrip("./").startswith("tests"):
                    found.append(f"{where} builds with {key} {value!r}")
            run = str(step.get("run") or "")
            for m in re.finditer(r"docker\s+(?:buildx\s+)?build\b[^\n]*", run):
                if re.search(r"(?:-f|--file)[ =]\.?/?tests/", m.group(0)) or re.search(
                    r"\s\.?/?tests/\S*\s*$", m.group(0)
                ):
                    found.append(f"{where} runs a build from tests/: {m.group(0)!r}")
    return found


def updater_target_problems(text: str) -> list[str]:
    found: list[str] = []
    all_stages = stages(text)
    chain, todo = [], ["updater"]
    while todo:
        name = todo.pop()
        if name in chain or name not in all_stages:
            continue
        chain.append(name)
        for word, rest in all_stages[name]:
            m = re.search(r"--from=(\S+)", rest) if word == "COPY" else None
            if m:
                todo.append(m.group(1))
            if word == "FROM":
                todo.append(rest.split()[0])
    for name in chain:
        for word, rest in all_stages[name]:
            if word in ("COPY", "ADD") and "--from=" not in rest:
                sources = [s for s in rest.split()[:-1] if not s.startswith("--")]
                if any(s.lstrip("./").startswith("tests") for s in sources):
                    found.append(f"`{name}`, which the updater is built from, copies {rest!r}")
    if ("ENTRYPOINT", '["python", "-m", "updater"]') not in all_stages.get("updater", []):
        found.append("the published updater's entry point is not `python -m updater`")
    return found


def ci_image_problems(text: str) -> list[str]:
    found: list[str] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    froms = [ln for ln in lines if ln.upper().startswith("FROM ")]
    if froms != ["FROM ${UPDATER_IMAGE}"]:
        found.append(f"the CI image is not built FROM the updater image it is given: {froms}")
    for ln in lines:
        word = ln.split()[0].upper()
        if word in ("COPY", "ADD"):
            sources = ln.split()[1:-1]
            if not all(s.startswith("tests/") for s in sources):
                found.append(f"the CI image adds more than the test-only policy: {ln!r}")
        elif word in ("RUN", "USER", "VOLUME", "EXPOSE"):
            found.append(f"the CI image changes the real image beyond the policy: {ln!r}")
    if 'ENTRYPOINT ["python", "-m", "tests.self_update.ci_updater"]' not in lines:
        found.append("the CI image's entry point does not select the test-only policy")
    return found


# --------------------------------------------------------------------------- #


def test_release_never_builds_or_names_the_ci_updater():
    assert release_problems(RELEASE.read_text()) == []


def test_the_published_updater_target_carries_no_tests():
    assert updater_target_problems(DOCKERFILE.read_text()) == []


def test_the_ci_image_is_the_real_updater_plus_the_policy():
    assert ci_image_problems(CI_DOCKERFILE.read_text()) == []


def test_the_driver_pushes_only_to_the_loopback_registry():
    assert stage.PUSH.startswith("localhost:")
    assert "spend-tracker-ci-updater" not in (ROOT / ".github" / "workflows" / "tests.yml").read_text()


@pytest.mark.parametrize(
    "insert, named",
    [
        (
            "      - run: docker build -f tests/self_update/ci-updater.Dockerfile .\n",
            "ci-updater.Dockerfile",
        ),
        ("      - run: docker push spend-tracker-ci-updater:1.2.3\n", "spend-tracker-ci-updater"),
        ("      - run: python -m tests.self_update.ci_updater\n", "ci_updater"),
        ("      - run: docker build -f tests/other.Dockerfile .\n", "runs a build from tests/"),
    ],
)
def test_release_problems_name_each_break(insert, named):
    text = RELEASE.read_text()
    first_steps = text.index("    steps:\n") + len("    steps:\n")
    broken = text[:first_steps] + insert + text[first_steps:]
    problems = release_problems(broken)
    assert any(named in p for p in problems), problems


def test_release_problems_name_a_build_action_from_tests():
    doc = yaml.safe_load(RELEASE.read_text())
    job = next(j for j in doc["jobs"].values() if j.get("steps"))
    job["steps"].insert(
        0, {"uses": "docker/build-push-action@x", "with": {"file": "tests/self_update/x.Dockerfile"}}
    )
    assert any("builds with file" in p for p in release_problems(yaml.safe_dump(doc)))


def test_updater_target_problems_name_a_copy_from_tests():
    text = DOCKERFILE.read_text()
    broken = text.replace(
        "COPY updater/ ./updater/", "COPY updater/ ./updater/\nCOPY tests/self_update/ ./tests/self_update/"
    )
    assert broken != text
    assert any("copies" in p and "tests/self_update" in p for p in updater_target_problems(broken))
    broken = text.replace("COPY requirements-updater.txt ./", "COPY requirements-updater.txt tests/ ./")
    assert broken != text
    assert any("updater-deps" in p for p in updater_target_problems(broken))


def test_ci_image_problems_name_each_break():
    text = CI_DOCKERFILE.read_text()
    assert any(
        "FROM" in p for p in ci_image_problems(text.replace("FROM ${UPDATER_IMAGE}", "FROM python:3.12"))
    )
    assert any("more than" in p for p in ci_image_problems(text + "COPY app/ ./app/\n"))
    assert any("beyond" in p for p in ci_image_problems(text + "RUN echo\n"))
    assert any(
        "entry point" in p
        for p in ci_image_problems(text.replace("tests.self_update.ci_updater", "updater"))
    )
