"""Build a throwaway instance for the end-to-end run, and say how to sign in.

Its own database in its own directory, reset every run: an end-to-end suite
that shares the developer's ledger either destroys it or fails depending on
what happens to be in it, and both are worse than slow.

The credentials go to a JSON file rather than being hardcoded in the spec,
because the seed mints a new TOTP secret every time. The spec computes codes
from that secret the way an authenticator would.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent


def main() -> int:
    from scripts import seed_demo

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = seed_demo.main(["--reset", "--months", "3"])
    output = captured.getvalue()
    sys.stderr.write(output)
    if code != 0:
        return code

    secret = re.search(r"authenticator secret\s*:\s*(\S+)", output)
    if secret is None:
        sys.stderr.write("could not read the authenticator secret out of the seed\n")
        return 1

    (HERE / ".credentials.json").write_text(
        json.dumps(
            {
                "email": seed_demo.DEMO_EMAIL,
                "password": seed_demo.DEMO_PASSWORD,
                "totp_secret": secret.group(1),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
