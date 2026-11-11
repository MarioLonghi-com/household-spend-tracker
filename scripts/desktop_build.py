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
6. **the third-party notices** (`scripts/third_party_notices.py`), from what
   steps 1, 3 and 5 bundled, into `build/` for the Licences window (#63);
7. **the package**, by electron-builder, into `desktop/dist`. Refused without
   the notices, with Electron's fuses flipped on the way for a store build
   (`desktop/scripts/fuses.cjs`), and checked to carry the notices and
   Chromium's own licence file;
8. **the fuses, read back** from the packaged binary: a store build whose
   fuses did not take fails here, and so does a smoke-test build whose fuses
   are on, since the smoke test could not drive it.

`--from N` starts at step N, for the second run of a build being fixed.
`SPENDTRACKER_FUSES=on|off` overrides which builds get the fuses: on for
`store` and `win`, off for `mac` and `mas` by default, because the end-to-end
smoke test needs the debugging port the fuses close. Signing is not here:
`desktop/scripts/sign-mas.sh` for a sandboxed run on this Mac (with a
development profile) or for the store (with the App Store profile and "Apple
Distribution"), then `desktop/scripts/package-mas.sh` for the installer
package the store takes; `desktop/scripts/sign-msix.ps1` for a test install
on Windows. `desktop/README.md` has the store steps (#63).
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
# electron-builder.yml's productName: what the packaged app is called on disk.
PRODUCT = "Household Spend Tracker"

TARGETS = {
    "mac": ["--mac", "dir", "--arm64"],
    "mas": ["--mac", "mas-dev", "--arm64", "-c.mac.identity=null"],
    # The same app, built for distribution rather than for a development
    # profile: electron-builder's `mas` target, into dist/mas-arm64.
    "store": ["--mac", "mas", "--arm64", "-c.mac.identity=null"],
    "win": ["--win", "appx", "dir", "--x64"],
}

# Where electron-builder leaves each target's app, relative to desktop/dist:
# the bundle on a Mac, the executable inside the plain folder on Windows,
# which is also the one the MSIX was packed from.
PACKAGED = {
    "mac": Path("mac-arm64") / f"{PRODUCT}.app",
    "mas": Path("mas-dev-arm64") / f"{PRODUCT}.app",
    "store": Path("mas-arm64") / f"{PRODUCT}.app",
    "win": Path("win-unpacked") / f"{PRODUCT}.exe",
}

# The targets whose fuses go on unless SPENDTRACKER_FUSES says otherwise: the
# two the stores take. `mac` and `mas` stay open for the smoke test.
FUSED_BY_DEFAULT = {"store", "win"}


def _npm() -> str:
    found = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if found is None:
        raise SystemExit("npm is not on PATH (make preflight says which Node this needs)")
    return found


def _node() -> str:
    found = shutil.which("node")
    if found is None:
        raise SystemExit("node is not on PATH (make preflight says which Node this needs)")
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


def fuses(target: str, env: dict[str, str] | None = None) -> str:
    """"on" or "off": SPENDTRACKER_FUSES when set, else by target (#63)."""
    given = (env if env is not None else os.environ).get("SPENDTRACKER_FUSES", "").strip().lower()
    if given in {"on", "off"}:
        return given
    if given:
        raise SystemExit(f'SPENDTRACKER_FUSES is "{given}": on or off')
    return "on" if target in FUSED_BY_DEFAULT else "off"


def _package(target: str, python: str, npm: str, mode: str) -> None:
    # The notices are an extra resource: a build without them would be refused
    # by electron-builder for a missing file, or, worse, ship an empty one.
    _run("the notices, checked", [python, "-m", "scripts.third_party_notices", "--check"])
    _run(
        f"the {target} package, fuses {mode}",
        [npm, "exec", "--", "electron-builder", *TARGETS[target], *_build_number(target), "--publish", "never"],
        DESKTOP,
        # Not signed with whatever identity the keychain happens to hold:
        # electron-builder signs ad hoc instead, which is what an Apple
        # Silicon Mac needs to run it, and CI does the same. The fuses are
        # decided here and only read in the afterPack hook, so a stray value
        # in the environment cannot leave a store build open.
        {**os.environ, "CSC_IDENTITY_AUTO_DISCOVERY": "false", "SPENDTRACKER_FUSES": mode},
    )


def _shipped(target: str, packaged: Path) -> None:
    """The licence files the Licences window and the notices point at, in the package (#63).

    electron-builder keeps Chromium's `LICENSES.chromium.html` from the Electron
    download -- in Contents/Resources on a Mac, beside the executable on
    Windows -- and the notices are an extra resource. The notices say where
    both are, so a package without either is refused here.
    """
    resources = packaged / "Contents" / "Resources" if target != "win" else packaged.parent / "resources"
    expected = [resources / "THIRD-PARTY-NOTICES.txt", (resources if target != "win" else packaged.parent) / "LICENSES.chromium.html"]
    missing = [str(file) for file in expected if not file.is_file() or file.stat().st_size == 0]
    if missing:
        raise SystemExit("the package is missing, or ships empty: " + ", ".join(missing))
    sys.stderr.write("   shipped: " + ", ".join(file.name for file in expected) + "\n")


def build(target: str, *, python: str, start: int = 1) -> None:
    npm = _npm()
    mode = fuses(target)
    executable = FROZEN / ("spend-tracker-backend.exe" if os.name == "nt" else "spend-tracker-backend")
    packaged = DESKTOP / "dist" / PACKAGED[target]
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
        lambda: _run("the third-party notices", [python, "-m", "scripts.third_party_notices"]),
        lambda: (_package(target, python, npm, mode), _shipped(target, packaged)),
        lambda: _run(f"the fuses, read back ({mode})", [_node(), str(DESKTOP / "scripts" / "fuses.cjs"), "check", str(packaged), mode]),
    ]
    for number, step in enumerate(steps, start=1):
        if number >= start:
            step()
    sys.stderr.write(f"\nbuilt: {packaged} (fuses {mode})\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("--from", dest="start", type=int, default=1, help="start at this step (1-8)")
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
