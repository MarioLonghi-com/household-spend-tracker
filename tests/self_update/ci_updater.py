"""The CI updater's entry point: `python -m updater`, with the test-only trust policy.

    python -m tests.self_update.ci_updater [the arguments of python -m updater]

Only `tests/self_update/ci-updater.Dockerfile` runs this, in an image built on
top of the real updater image by the end-to-end job (design notes 15.5). It is
`updater.__main__.serve` with `CiTrust` in place of `Sigstore`, reading the
digests the job pushed from `ci-trust.json` in the project directory -- the
one host directory the updater and every successor it starts already bind
(`/project`), so a handover carries it without a new mount.

`SPENDTRACKER_CI_BREAK_SUCCESSOR=1`, set in one variant image's environment
by the job, makes that image fail its self-check when it is started as a
successor (H3): it is pointed at an engine socket that does not exist. The
updater that started it must then stay current (E11, E13).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tests.self_update.ci_trust import CiTrust
from updater import __main__ as entry

TABLE = "ci-trust.json"
BREAK = "SPENDTRACKER_CI_BREAK_SUCCESSOR"


def arguments(argv: list[str]) -> list[str]:
    if os.environ.get(BREAK) == "1" and "--successor" in argv:
        return [*argv, "--socket", "/nonexistent/engine.sock"]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = entry.parser().parse_args(arguments(list(sys.argv[1:] if argv is None else argv)))
    table = Path(args.project_dir) / TABLE
    if not table.is_file():
        print(f"the CI updater needs {table}, written by the end-to-end job", file=sys.stderr)
        return 2
    entry.serve(args, CiTrust(table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
