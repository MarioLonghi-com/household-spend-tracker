"""Does the packaged desktop app start? (#63)

    python desktop/launch_check.py "desktop/dist/mac-arm64/Household Spend Tracker.app"
    python desktop/launch_check.py "desktop/dist/win-unpacked/Household Spend Tracker.exe"

For the builds Playwright cannot drive: with the fuses on, the app takes no
`--inspect`, so the smoke test cannot attach to it. This starts it on a fresh
ledger folder, as somebody double-clicking it would, and waits for the one
thing only a working app does -- the backend it spawned creating the ledger
database -- then asks it to quit and checks it did. A fused binary whose asar
integrity does not match, or that cannot find its app, never gets that far.

Exits 1 if the app stops first, or the ledger has not appeared in two minutes.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

WAIT = 120


def executable(given: str) -> str:
    path = pathlib.Path(given)
    if path.suffix == ".app":
        return str(path / "Contents" / "MacOS" / path.stem)
    return str(path)


def stop(child: subprocess.Popen) -> int:
    if os.name == "nt":
        # `terminate` on Windows is a hard kill of the one process; taskkill
        # takes the backend child with it and lets nothing linger on a runner.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(child.pid)], check=False, capture_output=True)
        return child.wait(timeout=30)
    # SIGTERM: Electron's `before-quit` runs, and the shell stops the backend.
    child.terminate()
    try:
        return child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()
        return child.wait(timeout=10)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 2
    root = pathlib.Path(tempfile.mkdtemp(prefix="st-launch-"))
    # A backups folder chosen in advance, as the smoke test does, so a first
    # launch writes nothing into Documents.
    (root / "shell.json").write_text(json.dumps({"backupsFolder": str(root / "Backups"), "askedForFolder": True}))
    env = {key: value for key, value in os.environ.items() if not key.startswith("SPENDTRACKER_")}
    env["SPENDTRACKER_DESKTOP_ROOT"] = str(root)
    ledger = root / "Ledger" / "spendtracker.sqlite3"
    started = time.monotonic()
    child = subprocess.Popen([executable(argv[0])], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not ledger.exists():
        if child.poll() is not None:
            sys.stderr.write(f"FAIL  the app stopped on its own (exit {child.returncode}) after {time.monotonic() - started:.0f} s\n")
            sys.stderr.write((root / "shell.log").read_text(errors="replace")[-3000:] if (root / "shell.log").exists() else "no shell.log\n")
            return 1
        if time.monotonic() - started > WAIT:
            sys.stderr.write(f"FAIL  no ledger at {ledger} after {WAIT} s\n")
            sys.stderr.write((root / "shell.log").read_text(errors="replace")[-3000:] if (root / "shell.log").exists() else "no shell.log\n")
            stop(child)
            return 1
        time.sleep(0.5)
    sys.stderr.write(f"ok    the ledger appeared after {time.monotonic() - started:.1f} s\n")
    code = stop(child)
    sys.stderr.write(f"ok    stopped (exit {code})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
