"""One scenario, or several, on this machine's Docker Desktop -- without touching its engine's state.

    python -m tests.self_update.local E3            # or E1,E11 ; or all
    python -m tests.self_update.local E1 --base origin/dev --keep

The scenarios need an engine that resolves `ghcr.io` to a registry of the
job's own, which Docker Desktop's engine cannot be told to do. So this runs
them in a **dind container** on Docker Desktop -- the same way the weekly
canary runs its engines (`engine_box`):

1. `stage registry`: the two registries on the `st-ci` network, nothing
   published but `localhost:5000` for the pushes;
2. `stage build`: releases A (from `--base`, default the merge base with
   `origin/dev`) and B (this working tree, uncommitted changes included),
   and the variants the chosen scenarios need, each with its CI updater;
3. `engine_box --kind docker`: the scenarios inside `docker:dind`.

Leaves behind: the images it built (`spend-tracker-ci*`), the registries
(`st-ci-registry*`, `docker rm -f` them) and `.self-update/` in the checkout.
E6 cannot run here: the engine is the dind container's main process.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tests.self_update import engine_box, stage

ROOT = Path(__file__).resolve().parents[2]
#: The variants each scenario needs (stage.py's table).
NEEDS = {
    "E2": {"B4", "C"},
    "E3": {"B1"},
    "E4": {"B2"},
    "E8": {"B1", "B2"},
    "E11b": {"B3"},
    "E14": {"C"},
    "E15": {"C"},
}
DIND = "docker:29.8.2-dind"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.self_update.local")
    p.add_argument("scenarios", help="E1,E3 ... or all")
    p.add_argument("--base", default=None, help="the commit release A is built from")
    p.add_argument("--image", default=DIND)
    p.add_argument("--out", type=Path, default=ROOT / ".self-update")
    p.add_argument("--no-build", action="store_true", help="reuse what the last run staged")
    p.add_argument("--keep", action="store_true")
    args = p.parse_args(argv)
    only = "" if args.scenarios == "all" else args.scenarios
    wanted = set(only.split(",")) if only else set(NEEDS) | {"E1"}
    variants = set().union(*(NEEDS.get(s, set()) for s in wanted))
    base = (
        args.base
        or subprocess.run(
            ["git", "merge-base", "HEAD", "origin/dev"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    out = args.out.resolve()
    if not args.no_build:
        subprocess.run(["docker", "network", "create", engine_box.NETWORK], capture_output=True)
        stage.main(["registry", "--out", str(out), "--network", engine_box.NETWORK])
        stage.main(
            ["build", "--out", str(out), "--base", base, "--only", ",".join(sorted(variants)) or "none"]
        )
    box = ["--kind", "docker", "--image", args.image, "--staged", str(out), "--leg", "local-dind"]
    box += ["--only", only or ",".join(stage_order()), "--report", str(out / "report.json")]
    if args.keep:
        box.append("--keep")
    return engine_box.main(box)


def stage_order() -> list[str]:
    from tests.self_update.scenarios import SCENARIOS

    return list(SCENARIOS)


if __name__ == "__main__":
    sys.exit(main())
