"""Build the desktop app, start to finish, on the machine it is for (#61).

    python -m scripts.desktop_build mac      # macOS, unsigned, to run here
    python -m scripts.desktop_build mas      # the Mac App Store build, unsigned, to run here
    python -m scripts.desktop_build store    # the Mac App Store build to upload (#63)
    python -m scripts.desktop_build win      # Windows: an MSIX and a plain folder

Run with the Python that should be frozen: a venv made from
`requirements-desktop.txt` (the runtime lock plus PyInstaller). There is no
cross-building -- PyInstaller freezes the interpreter it runs in -- so the Mac
builds are made on an Apple Silicon Mac and the Windows one on Windows x64.

In order, each step's output the next one's input:

1. **the client**, built into `app/static/dist`, which the backend serves;
2. **the build stamp**, `app/build.json`, so `/api/health` names a commit with
   no `.git` beside it. Kept as it is when there is no git to ask (a copy of
   the tree on a build machine), and refused when there is neither;
3. **the backend**, frozen with `desktop/backend.spec` into
   `build/backend/spend-tracker-backend`;
4. **its smoke test** (`desktop/smoke_backend.py`): started from `/` on a fresh
   ledger, set up, a household with an accented name, a clean exit. A build
   missing a data file fails here rather than in somebody's hands;
5. **the shell**, `desktop/build`;
6. **the package**, by electron-builder, into `desktop/dist`.

`--from N` starts at step N, for the second run of a build being fixed.
Signing is not here: `desktop/scripts/sign-mas.sh` for a sandboxed run on
this Mac (with a development profile) or for the store (with the App Store
profile and "Apple Distribution"), then `desktop/scripts/package-mas.sh` for
the installer package the store takes; `desktop/scripts/sign-msix.ps1` for a
test install on Windows. `desktop/README.md` has the store steps (#63).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
FROZEN = ROOT / "build" / "backend" / "spend-tracker-backend"

TARGETS = {
    "mac": ["--mac", "dir", "--arm64"],
    "mas": ["--mac", "mas-dev", "--arm64", "-c.mac.identity=null"],
    # The same app, built for distribution rather than for a development
    # profile: electron-builder's `mas` target, into dist/mas-arm64.
    "store": ["--mac", "mas", "--arm64", "-c.mac.identity=null"],
    "win": ["--win", "appx", "dir", "--x64"],
}


def _npm() -> str:
    found = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if found is None:
        raise SystemExit("npm is not on PATH (make preflight says which Node this needs)")
    return found


def _run(step: str, command: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    sys.stderr.write(f"\n== {step}\n   {' '.join(command)}\n")
    sys.stderr.flush()
    finished = subprocess.run(command, cwd=cwd, check=False, env=env)
    if finished.returncode != 0:
        raise SystemExit(f"{step} failed (exit {finished.returncode})")


def _stamp(python: str) -> None:
    sys.stderr.write("\n== the build stamp\n")
    finished = subprocess.run([python, "-m", "scripts.build_stamp"], cwd=ROOT, check=False)
    if finished.returncode == 0:
        return
    if (ROOT / "app" / "build.json").exists():
        sys.stderr.write("   no git here: keeping the app/build.json already in the tree\n")
        return
    raise SystemExit("no git to ask and no app/build.json: the build would not know its commit")


def _build_number(target: str) -> list[str]:
    """The store build's CFBundleVersion, when it must differ from the version.

    Apple takes one upload per build number: the first of a version can be the
    version itself (electron-builder's default), and a second upload of the same
    version -- a build fixed after App Review, say -- needs
    `SPENDTRACKER_STORE_BUILD=2` and so on. Only the store target reads it.
    """
    number = os.environ.get("SPENDTRACKER_STORE_BUILD")
    if target != "store" or not number:
        return []
    if not number.isdigit():
        raise SystemExit("SPENDTRACKER_STORE_BUILD is a whole number: 2, 3, ...")
    return [f"-c.buildVersion={number}"]


def build(target: str, *, python: str, start: int = 1) -> None:
    npm = _npm()
    executable = FROZEN / ("spend-tracker-backend.exe" if os.name == "nt" else "spend-tracker-backend")
    steps = [
        lambda: _run("the client", [npm, "run", "build"], ROOT / "client"),
        lambda: _stamp(python),
        lambda: _run(
            "the backend, frozen",
            [
                python, "-m", "PyInstaller", str(DESKTOP / "backend.spec"), "--noconfirm",
                "--distpath", str(FROZEN.parent), "--workpath", str(ROOT / "build" / "pyinstaller"),
            ],
        ),
        lambda: _run("its smoke test", [python, str(DESKTOP / "smoke_backend.py"), str(executable)]),
        lambda: _run("the shell", [npm, "run", "build"], DESKTOP),
        lambda: _run(
            f"the {target} package",
            [npm, "exec", "--", "electron-builder", *TARGETS[target], *_build_number(target), "--publish", "never"],
            DESKTOP,
            # Not signed with whatever identity the keychain happens to hold:
            # electron-builder signs ad hoc instead, which is what an Apple
            # Silicon Mac needs to run it, and CI does the same.
            {"CSC_IDENTITY_AUTO_DISCOVERY": "false", **os.environ},
        ),
    ]
    for number, step in enumerate(steps, start=1):
        if number >= start:
            step()
    sys.stderr.write(f"\nbuilt: {DESKTOP / 'dist'}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("--from", dest="start", type=int, default=1, help="start at this step (1-6)")
    parser.add_argument("--python", default=sys.executable, help="the Python to freeze (default: this one)")
    args = parser.parse_args(argv)
    if args.target in {"mac", "mas", "store"} and sys.platform != "darwin":
        raise SystemExit("the Mac builds are made on a Mac")
    if args.target == "win" and os.name != "nt":
        raise SystemExit("the Windows build is made on Windows")
    build(args.target, python=args.python, start=args.start)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
