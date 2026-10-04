"""Write `app/build.json`: the commit, for a build that will not carry `.git`.

    python -m scripts.build_stamp

The release tarball and the container image are both assembled from a checkout
and shipped without its `.git`, so the running app has nobody to ask which
commit it is. This asks git now, while there is one, and leaves the answer
where `app/build.py` looks for it.

Refuses rather than writing an empty stamp: a build that says "unknown"
honestly is better than one whose stamp step silently did nothing, and CI is
where that should fail.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

from app import build


def main() -> int:
    found = build.from_git()
    if found is None:
        print(f"git cannot say which commit {build.ROOT} is; no stamp written", file=sys.stderr)
        return 1
    stamp = {key: value for key, value in asdict(found).items() if key != "source"}
    build.STAMP.write_text(json.dumps(stamp, indent=2) + "\n")
    print(f"{build.STAMP}: {found.commit} ({found.branch or 'detached'})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
