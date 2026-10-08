"""The self-updater's image, its compose service and its release (#164).

Design notes 6.3 (the service), 6.6 and C4 (the protocol labels), C11 (the
`update` volume shared by group), C15 (the image lines), S19 (no pod under
podman-compose), 7.1 and C6-C7 (the release). Three readers -- the
Dockerfile's stages, both compose files, release.yml -- each with a
`problems()` that names every rule a file breaks. Each is checked against the
real file, which must have none, and then against a copy broken one rule at a
time, which must name exactly that rule: a check that passes everything
cannot pass here.

What the image *does* when it runs -- uid 65532, the fresh volume at 2770,
nothing listening (E10) -- is checked by starting it, in tests.yml's and
release.yml's `image` jobs (tests/self_update/updater_smoke.py), because no
file says it.
"""

from __future__ import annotations

import copy
import pathlib
import re
import shlex

import pytest
import yaml

from app.services import updates
from updater import contract, detect, handover
from updater import engine as eng

# Reads the Dockerfile, both compose files and a workflow: every pull request.
pytestmark = pytest.mark.repo_wide

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE_FILES = {
    "root": ROOT / "compose.yaml",
    "tailnet": ROOT / "deploy" / "tailnet" / "compose.yaml",
}
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

APP_REPO, UPDATER_REPO = eng.REPOSITORIES
RUNS_AS = "65532:65532"
SOCKET = "${SPENDTRACKER_ENGINE_SOCKET:-/var/run/docker.sock}:/run/engine.sock"


# --------------------------------------------------------------------------- #
# The Dockerfile
# --------------------------------------------------------------------------- #


def stages(text: str) -> dict[str, list[tuple[str, str]]]:
    """Each named stage's instructions, as (INSTRUCTION, rest) with continuations joined."""
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#") and not pending:
            continue
        if raw.lstrip().startswith("#"):  # a comment inside a continuation
            continue
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        line = (pending + raw).strip()
        pending = ""
        if line:
            lines.append(line)
    found: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for line in lines:
        word, _, rest = line.partition(" ")
        if word.upper() == "FROM":
            m = re.search(r"\bAS\s+(\S+)\s*$", rest, re.I)
            current = found.setdefault(m.group(1) if m else f"#{len(found)}", [])
            current.append(("FROM", rest.strip()))
        elif current is not None:
            current.append((word.upper(), rest.strip()))
    return found


def labels(stage: list[tuple[str, str]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for word, rest in stage:
        if word == "LABEL":
            for pair in shlex.split(rest):
                key, _, value = pair.partition("=")
                found[key] = value
    return found


def _runs(stage: list[tuple[str, str]]) -> str:
    return "\n".join(rest for word, rest in stage if word == "RUN")


def dockerfile_problems(text: str) -> list[str]:
    found: list[str] = []
    all_stages = stages(text)
    names = list(all_stages)
    if names[-1:] != ["runtime"]:
        found.append("the app's `runtime` is not the last stage, so an untargeted build is not the app")
    app = all_stages.get("runtime", [])
    upd = all_stages.get("updater", [])
    deps = all_stages.get("updater-deps", [])
    if not upd:
        return [*found, "there is no `updater` stage"]

    if ("FROM", "${PY_RUN} AS updater") not in upd:
        found.append("the updater does not stand on the app's runtime base")
    if ("FROM", "${PY_BASE} AS updater-deps") not in deps:
        found.append("the updater's venv is not built on the app's -dev base")
    if "--require-hashes -r requirements-updater.txt" not in _runs(deps):
        found.append("the updater's venv is not installed from its hashed lock")
    if "pip uninstall" not in _runs(deps):
        found.append("pip stays in the updater's venv")
    from_context = [rest for word, rest in upd if word == "COPY" and "--from=" not in rest]
    if from_context != ["updater/ ./updater/"]:
        found.append(f"the updater image copies more than updater/ from the context: {from_context}")
    if [rest for word, rest in upd if word == "RUN"]:
        found.append("the updater's runtime stage runs a command, so it needs a shell")
    if ("USER", RUNS_AS) not in upd:
        found.append(f"the updater does not run as {RUNS_AS}")
    if ("ENTRYPOINT", '["python", "-m", "updater"]') not in upd:
        found.append("the updater's entry point is not `python -m updater`")
    if any(word in ("EXPOSE", "HEALTHCHECK") for word, _ in upd):
        found.append("the updater image declares a port or a probe; it listens on nothing")
    window = handover.protocol_window(labels(upd))
    if window != contract.PROTOCOLS:
        found.append(f"the updater's protocols label is {window}, not {contract.PROTOCOLS}")
    if labels(upd).get("org.opencontainers.image.source") != labels(app).get("org.opencontainers.image.source"):
        found.append("the updater's OCI source is not the app's")
    if labels(app).get(detect.PROTOCOL_LABEL) != str(updates.PROTOCOL):
        found.append(f"the app's protocol label is not {updates.PROTOCOL}")

    for stage, path in (("updater-deps", "/mounts/update"), ("deps", "/mounts/var/lib/spend-tracker-update")):
        made = _runs(all_stages.get(stage, []))
        if f"chown {RUNS_AS} {path}" not in made or f"chmod 2770 {path}" not in made:
            found.append(f"{path} is not made {RUNS_AS}, mode 2770, in `{stage}`")
    for stage, source in ((upd, "updater-deps"), (app, "deps")):
        if ("COPY", f"--from={source} /mounts/ /") not in stage:
            found.append(f"the mount points are not copied from `{source}` as a tree")
    return found


def test_the_dockerfile_builds_the_updater_as_the_design_says():
    assert dockerfile_problems(DOCKERFILE.read_text()) == []


def test_the_label_names_are_the_ones_the_updater_reads():
    """Written out in the Dockerfile, built from the ghcr owner in updater/."""
    assert detect.PROTOCOL_LABEL == "com.github.mariolonghi-com.spend-tracker.updater-protocol"  # hygiene: the repo's own address
    found = stages(DOCKERFILE.read_text())
    assert labels(found["runtime"])[detect.PROTOCOL_LABEL] == "1"
    assert labels(found["updater"])[handover.PROTOCOLS_LABEL] == "1-1"
    # And the app image does not claim a window, nor the updater a protocol.
    assert handover.PROTOCOLS_LABEL not in labels(found["runtime"])
    assert detect.PROTOCOL_LABEL not in labels(found["updater"])


@pytest.mark.parametrize(
    ("old", "new", "rule"),
    [
        ('updater-protocols="1-1"', 'updater-protocols="1-2"', "the updater's protocols label is (1, 2), not (1, 1)"),
        ('updater-protocol="1"', 'updater-protocol="2"', "the app's protocol label is not 1"),
        ('ENTRYPOINT ["python", "-m", "updater"]', 'ENTRYPOINT ["python", "-m", "app"]', "the updater's entry point is not `python -m updater`"),
        ("COPY updater/ ./updater/", "COPY updater/ ./updater/\nCOPY scripts/ ./scripts/", "the updater image copies more than updater/ from the context: ['updater/ ./updater/', 'scripts/ ./scripts/']"),
        ("chmod 2770 /mounts/update", "chmod 0755 /mounts/update", f"/mounts/update is not made {RUNS_AS}, mode 2770, in `updater-deps`"),
        (" && /venv/bin/python -m pip uninstall --yes --quiet pip", "", "pip stays in the updater's venv"),
    ],
)  # fmt: skip
def test_each_dockerfile_rule_is_caught(old, new, rule):
    text = DOCKERFILE.read_text()
    assert text.count(old) == 1, old
    assert dockerfile_problems(text.replace(old, new)) == [rule]


def test_a_dockerfile_whose_last_stage_is_the_updater_is_caught():
    text = DOCKERFILE.read_text()
    moved = text + '\nFROM ${PY_RUN} AS updater-last\nENTRYPOINT ["python", "-m", "updater"]\n'
    assert "the app's `runtime` is not the last stage" in dockerfile_problems(moved)[0]


def test_a_port_in_the_updater_stage_is_caught():
    text = DOCKERFILE.read_text()
    broken = text.replace("USER 65532:65532\nENTRYPOINT [\"python\", \"-m\", \"updater\"]", "EXPOSE 9000\nUSER 65532:65532\nENTRYPOINT [\"python\", \"-m\", \"updater\"]")
    assert broken != text
    assert dockerfile_problems(broken) == ["the updater image declares a port or a probe; it listens on nothing"]


# --------------------------------------------------------------------------- #
# The compose files
# --------------------------------------------------------------------------- #


def _compose(which: str) -> dict:
    return yaml.safe_load(COMPOSE_FILES[which].read_text())


def _mounts(service: dict) -> list[str]:
    return [v if isinstance(v, str) else f"{v.get('source')}:{v.get('target')}" for v in service.get("volumes") or []]


def _holds_socket(service: dict) -> bool:
    return any("docker.sock" in m or "podman.sock" in m or m.endswith(":/run/engine.sock") for m in _mounts(service))


def compose_problems(doc: dict, which: str) -> list[str]:
    found: list[str] = []
    services = doc.get("services") or {}
    app, upd = services.get("app") or {}, services.get("updater")
    if upd is None:
        return ["there is no updater service"]
    if (doc.get("x-podman") or {}).get("in_pod") is not False:
        found.append("x-podman.in_pod is not false, so podman-compose makes a pod")

    if app.get("image") != f"${{SPENDTRACKER_IMAGE:-{APP_REPO}:${{SPENDTRACKER_VERSION:-latest}}}}":
        found.append(f"the app's image line is {app.get('image')!r}")
    if upd.get("image") != f"${{SPENDTRACKER_UPDATER_IMAGE:-{UPDATER_REPO}:${{SPENDTRACKER_VERSION:-latest}}}}":
        found.append(f"the updater's image line is {upd.get('image')!r}")
    if which == "tailnet" and any("build" in s for s in services.values()):
        found.append("the tailnet file builds an image; it must pull the release")
    if which == "root" and (upd.get("build") or {}).get("target") != "updater":
        found.append("the root file's updater does not fall back to the `updater` target")

    for key in ("ports", "expose", "network_mode", "network"):
        if key in upd:
            found.append(f"the updater has `{key}`")
    if "update:/var/lib/spend-tracker-update" not in _mounts(app):
        found.append("the app does not mount the update volume")
    if _mounts(upd) != ["update:/update", SOCKET, ".:/project"]:
        found.append(f"the updater mounts {_mounts(upd)}")
    if "update" not in (doc.get("volumes") or {}):
        found.append("there is no top-level `update` volume")
    holders = sorted(name for name, s in services.items() if _holds_socket(s))
    if holders != ["updater"]:
        found.append(f"the engine socket is mounted by {holders}")
    projects = sorted(name for name, s in services.items() if any(m.split(":")[0] == "." for m in _mounts(s)))
    if projects != ["updater"]:
        found.append(f"the project directory is mounted by {projects}")

    wanted = {
        "restart": "unless-stopped",
        "user": "${SPENDTRACKER_UPDATER_USER:-65532:65532}",
        "group_add": ["${SPENDTRACKER_SOCKET_GID:-0}", "65532"],
        "read_only": True,
        "tmpfs": ["/tmp"],
        "security_opt": ["no-new-privileges:true", "label=disable"],
        "cap_drop": ["ALL"],
        "mem_limit": "128m",
    }
    for key, value in wanted.items():
        if upd.get(key) != value:
            found.append(f"the updater's {key} is {upd.get(key)!r}, not {value!r}")
    return found


@pytest.mark.parametrize("which", sorted(COMPOSE_FILES))
def test_both_compose_files_run_the_updater_as_the_design_says(which):
    assert compose_problems(_compose(which), which) == []


def test_the_sidecar_layout_keeps_the_updater_out_of_the_tailnet():
    services = _compose("tailnet")["services"]
    assert services["app"]["network_mode"] == "service:tailscale"
    assert "network_mode" not in services["updater"]
    assert sorted(services) == ["app", "tailscale", "updater"]


def _break(which: str, edit) -> list[str]:
    doc = copy.deepcopy(_compose(which))
    edit(doc)
    return compose_problems(doc, which)


@pytest.mark.parametrize("which", sorted(COMPOSE_FILES))
def test_each_compose_rule_is_caught(which):
    s = "services"

    def sidecar(d):
        d[s]["updater"]["network_mode"] = "service:tailscale"

    def ported(d):
        d[s]["updater"]["ports"] = ["127.0.0.1:9000:9000"]

    def pod(d):
        d["x-podman"]["in_pod"] = True

    def app_socket(d):
        d[s]["app"]["volumes"].append(SOCKET)

    def app_project(d):
        d[s]["app"]["volumes"].append(".:/project")

    def no_update(d):
        d[s]["app"]["volumes"].remove("update:/var/lib/spend-tracker-update")

    def root_user(d):
        d[s]["updater"]["user"] = "0:0"

    def no_label(d):
        d[s]["updater"]["security_opt"] = ["no-new-privileges:true"]

    def pinned_app(d):
        d[s]["app"]["image"] = f"{APP_REPO}:${{SPENDTRACKER_VERSION:-latest}}"

    assert _break(which, sidecar) == ["the updater has `network_mode`"]
    assert _break(which, ported) == ["the updater has `ports`"]
    assert _break(which, pod) == ["x-podman.in_pod is not false, so podman-compose makes a pod"]
    assert _break(which, app_socket) == ["the engine socket is mounted by ['app', 'updater']"]
    assert _break(which, app_project) == ["the project directory is mounted by ['app', 'updater']"]
    assert _break(which, no_update) == ["the app does not mount the update volume"]
    assert _break(which, root_user) == [
        "the updater's user is '0:0', not '${SPENDTRACKER_UPDATER_USER:-65532:65532}'"
    ]
    assert _break(which, no_label) == [
        "the updater's security_opt is ['no-new-privileges:true'], not ['no-new-privileges:true', 'label=disable']"
    ]
    assert _break(which, pinned_app) == [f"the app's image line is '{APP_REPO}:${{SPENDTRACKER_VERSION:-latest}}'"]


def test_a_build_in_the_tailnet_file_is_caught_and_the_roots_is_required():
    def build_app(d):
        d["services"]["app"]["build"] = {"context": "../..", "dockerfile": "Dockerfile"}

    def no_fallback(d):
        del d["services"]["updater"]["build"]

    assert _break("tailnet", build_app) == ["the tailnet file builds an image; it must pull the release"]
    assert _break("root", no_fallback) == ["the root file's updater does not fall back to the `updater` target"]


# --------------------------------------------------------------------------- #
# The release
# --------------------------------------------------------------------------- #


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).startswith(action + "@")


def release_problems(doc: dict) -> list[str]:
    found: list[str] = []
    jobs = doc["jobs"]
    if doc.get("env", {}).get("UPDATER_IMAGE") != UPDATER_REPO:
        found.append(f"UPDATER_IMAGE is {doc.get('env', {}).get('UPDATER_IMAGE')!r}, not {UPDATER_REPO}")

    builds = [s["with"] for s in _steps(jobs["image"]) if _uses(s, "docker/build-push-action")]
    updater_builds = [w for w in builds if w.get("target") == "updater"]
    if len(updater_builds) != 1:
        found.append(f"`image` builds the updater {len(updater_builds)} times")
    else:
        w = updater_builds[0]
        if w.get("platforms") != "linux/${{ matrix.arch }}":
            found.append("the updater is not built per platform")
        if "type=oci,dest=updater-${{ matrix.arch }}.tar" not in str(w.get("outputs")):
            found.append("the updater is not archived for the push")
        if "org.opencontainers.image.version=${{ needs.build.outputs.version }}" not in str(w.get("labels")):
            found.append("the updater's version label is not the bare version")
    if not any("updater_smoke.py" in str(s.get("run")) for s in _steps(jobs["image"])):
        found.append("`image` does not run the updater's smoke check")

    attested = [s["with"].get("subject-name") for s in _steps(jobs["publish-image"])
                if _uses(s, "actions/attest-build-provenance")]  # fmt: skip
    if attested != ["${{ env.IMAGE }}", "${{ env.UPDATER_IMAGE }}"]:
        found.append(f"`publish-image` attests {attested}")
    runs = "\n".join(str(s.get("run", "")) for s in _steps(jobs["publish-image"]))
    for needle, rule in (
        ('repo="$UPDATER_IMAGE"', "`publish-image` does not push the updater"),
        ('index updater "$UPDATER_IMAGE"', "`publish-image` does not index the updater"),
        ('gh attestation verify "oci://${UPDATER_IMAGE}', "`publish-image` does not verify the updater's attestation"),
        ('public "$UPDATER_IMAGE"', "`publish-image` does not pull the updater anonymously"),
        ("make it public at", "the anonymous pull does not say how to make a private package public"),
    ):
        if needle not in runs:
            found.append(rule)
    moved = "\n".join(str(s.get("run", "")) for s in _steps(jobs["publish-release"]))
    if '-t "${UPDATER_IMAGE}:latest"' not in moved or 'resolves "$UPDATER_IMAGE"' not in moved:
        found.append("`publish-release` does not move and check the updater's floating tags")
    if "updater-digest" not in (jobs["publish-image"].get("outputs") or {}):
        found.append("`publish-image` does not hand the updater's digest on")
    return found


def test_every_release_builds_attests_and_publishes_the_updater():
    assert release_problems(yaml.safe_load(RELEASE.read_text())) == []


def test_each_release_rule_is_caught():
    real = yaml.safe_load(RELEASE.read_text())

    def broken(edit) -> list[str]:
        doc = copy.deepcopy(real)
        edit(doc)
        return release_problems(doc)

    def no_attest(d):
        steps = d["jobs"]["publish-image"]["steps"]
        steps[:] = [s for s in steps if not (_uses(s, "actions/attest-build-provenance")
                                             and "UPDATER" in s["with"]["subject-name"])]  # fmt: skip

    def no_anonymous(d):
        for s in d["jobs"]["publish-image"]["steps"]:
            if "run" in s:
                s["run"] = s["run"].replace('public "$UPDATER_IMAGE" "$UPDATER"\n', "")

    def other_repo(d):
        d["env"]["UPDATER_IMAGE"] = "ghcr.io/somebody/updater"

    assert broken(no_attest) == ["`publish-image` attests ['${{ env.IMAGE }}']"]
    assert broken(no_anonymous) == ["`publish-image` does not pull the updater anonymously"]
    assert broken(other_repo) == [f"UPDATER_IMAGE is 'ghcr.io/somebody/updater', not {UPDATER_REPO}"]
