"""Did every job of this run pass, or was it not owed? The `ci-ok` verdict (#407).

    NEEDS='${{ toJSON(needs) }}' python -m scripts.ci_ok

`ci-ok` is the one check the rulesets require, so it is the only thing that
stands between a pull request and a merge. It used to fail on a `needs` result
of `failure` or `cancelled` and pass everything else. On two runs of #405
GitHub abandoned the `changes` job before it started: its result was
`abandoned`, every job after it was skipped because of that, and `ci-ok` said
ok over a run that had tested nothing.

## What it accepts

- **Only `success` or `skipped`.** An allow-list, not a deny-list: `abandoned`,
  an empty result, or any word GitHub adds later fails.
- **`changes` succeeded.** Every skip in `tests.yml` is decided by `changes`
  (the path filter and the green-tree record), so a skip is on purpose only
  when `changes` actually ran. `tests/test_ci_ok.py` keeps every job of the
  workflow in `ci-ok`'s `needs`, so a job skipped because something upstream
  did not succeed always comes with that upstream job's own bad result.

It prints every job's result, then every problem at once.
"""

from __future__ import annotations

import json
import os
import sys

PASSING = frozenset({"success", "skipped"})
#: The job whose outputs decide every skip in the workflow.
GATE = "changes"


def problems(needs: dict) -> list[str]:
    """What is wrong with these `needs` results; empty when the run passed."""
    if not needs:
        return ["no job results at all"]
    found = []
    for name, job in sorted(needs.items()):
        result = (job or {}).get("result") or ""
        if result not in PASSING:
            found.append(f"{name}: {result or 'no result'}")
    if GATE not in needs:
        found.append(f"{GATE}: not among the jobs ci-ok needs")
    elif (gate := (needs[GATE] or {}).get("result")) == "skipped":
        # A skipped gate decided nothing: whatever it skipped was not judged.
        found.append(f"{GATE}: {gate}, so no skip after it was decided")
    return found


def main() -> int:
    raw = os.environ.get("NEEDS", "")
    try:
        needs = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as error:
        print(f"not ok: NEEDS is not JSON ({error})")
        return 1
    for name, job in sorted(needs.items()):
        print(f"{name}: {(job or {}).get('result') or 'no result'}")
    found = problems(needs)
    if found:
        print("not ok: " + "; ".join(found))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
