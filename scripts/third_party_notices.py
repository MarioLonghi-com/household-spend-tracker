"""Write the third-party notices the desktop app shows under Licences (#63).

    python -m scripts.third_party_notices            # build/THIRD-PARTY-NOTICES.txt
    python -m scripts.third_party_notices --check    # is what is there complete?

Run with the Python the backend is frozen from, after the client and the
shell are built: the notices name exactly what ships, read from three places.

1. **The frozen backend**: the packages in `requirements.txt` -- the runtime
   lock the desktop build is held to -- less the ones `desktop/backend.spec`
   excludes from the freeze, each with the licence `pip-licenses` finds in
   the installed distribution.
2. **The web client** and 3. **the shell**: the packages whose modules Vite
   bundled, listed by `client/bundled-packages.ts` into `build/` during each
   build, with the licence `license-checker-rseidelsohn` finds in the same
   `node_modules`. Not the declared dependencies: `@lingui/core` declares the
   Babel macro plugin, so the declared graph reaches Babel and Jest, which
   the bundle does not.

Electron itself is listed with the shell, and Chromium's own notices -- the
`LICENSES.chromium.html` the Electron package carries -- are copied beside
the text, since electron-builder puts that file in a Windows folder and not
in a Mac app.

Refuses rather than writing a thin file: a package with no licence found, an
empty section or a missing input stops the build here, where it can be fixed,
rather than shipping a Licences window that lists half of what is in the app.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
NOTICES = BUILD / "THIRD-PARTY-NOTICES.txt"
CHROMIUM = BUILD / "LICENSES.chromium.html"
RUNTIME_LOCK = ROOT / "requirements.txt"
BACKEND_SPEC = ROOT / "desktop" / "backend.spec"
CLIENT_BUNDLED = BUILD / "client-bundled-packages.json"
SHELL_BUNDLED = BUILD / "shell-bundled-packages.json"
ELECTRON = ROOT / "desktop" / "node_modules" / "electron"

RULE = "=" * 72
THIN = "-" * 72


class Incomplete(SystemExit):
    """A notices file that would not name everything the app ships."""

    def __init__(self, why: str) -> None:
        super().__init__(f"third-party notices: {why}")


# --------------------------------------------------------------------------- #
# What ships
# --------------------------------------------------------------------------- #


def runtime_names(lock: str, environment: dict[str, str] | None = None) -> list[str]:
    """The packages a lock installs here: each pin whose marker, if it has one, holds for this platform.

    The lock is universal (`make lock`), so `colorama ; sys_platform == 'win32'`
    is in it and is not on a Mac. `environment` overrides the marker's view of
    the platform, for the tests.
    """
    found = []
    for match in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==[^\s;\\]+(?:\s*;\s*([^\\]+?))?\s*\\?$", lock, re.MULTILINE):
        name, marker = match.group(1), match.group(2)
        if marker is None or Marker(marker.strip()).evaluate(environment):
            found.append(name)
    return found


def not_frozen(spec: str) -> set[str]:
    """What `backend.spec` tells PyInstaller to leave out: the `excludes=[...]` of its Analysis."""
    for node in ast.walk(ast.parse(spec)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Analysis":
            for keyword in node.keywords:
                if keyword.arg == "excludes":
                    return set(ast.literal_eval(keyword.value))
    raise Incomplete(f"{BACKEND_SPEC} has no Analysis(excludes=[...]) to read")


def normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------- #
# Their licences
# --------------------------------------------------------------------------- #


def python_licences(python: str, names: list[str]) -> list[dict]:
    """pip-licenses over `names` in the given interpreter: name, version, licence, URL, text."""
    command = [
        python, "-m", "piplicenses", "--format=json", "--with-license-file", "--no-license-path",
        "--with-urls", "--packages", *names,
    ]
    found = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    listed = {normalised(one["Name"]) for one in found}
    missing = [name for name in names if normalised(name) not in listed]
    if missing:
        raise Incomplete(f"not installed in {python}, so no licence to read: {', '.join(missing)}")
    return sorted(found, key=lambda one: one["Name"].lower())


def node_licences(start: Path) -> dict[str, dict]:
    """license-checker over a package folder: every installed package, keyed `name@version`."""
    checker = ROOT / "desktop" / "node_modules" / ".bin" / ("license-checker-rseidelsohn.cmd" if os.name == "nt" else "license-checker-rseidelsohn")
    if not checker.exists():
        raise Incomplete(f"{checker} is not installed: npm ci in desktop/")
    done = subprocess.run([str(checker), "--json", "--start", str(start)], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def bundled(listing: Path) -> list[dict]:
    if not listing.exists():
        raise Incomplete(f"{listing} is missing: the build that writes it has not run")
    found = json.loads(listing.read_text(encoding="utf-8"))
    if not found:
        raise Incomplete(f"{listing} lists nothing")
    return found


def licence_text(folder: Path, file: str | None) -> str:
    """The licence file license-checker found, read; a note when it found none but the package says which."""
    if file and Path(file).exists():
        return Path(file).read_text(encoding="utf-8", errors="replace").strip()
    for candidate in sorted(folder.glob("LICEN[CS]E*")) + sorted(folder.glob("licen[cs]e*")):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace").strip()
    return ""


# --------------------------------------------------------------------------- #
# The text
# --------------------------------------------------------------------------- #


def entry(name: str, version: str, licence: str, url: str, text: str) -> str:
    head = f"{name} {version}  --  {licence}" + (f"\n{url}" if url else "")
    body = text.strip() or "(the package names its licence in its metadata; it carries no licence file)"
    return f"{head}\n{THIN}\n{body}\n"


def python_section(found: list[dict]) -> str:
    return "\n".join(
        entry(one["Name"], one["Version"], one["License"], one.get("URL") or "", one.get("LicenseText") or "")
        for one in found
    )


def node_section(packages: list[dict], licences: dict[str, dict]) -> str:
    parts = []
    for one in packages:
        key = f"{one['name']}@{one['version']}"
        known = licences.get(key)
        if known is None:
            raise Incomplete(f"{key} was bundled but license-checker does not list it")
        folder = Path(one["path"])
        text = licence_text(folder, known.get("licenseFile"))
        licence = known.get("licenses") or ""
        if not licence or licence.upper().startswith("UNKNOWN"):
            raise Incomplete(f"{key}: no licence found")
        parts.append(entry(one["name"], one["version"], str(licence), known.get("repository") or "", text))
    return "\n".join(parts)


def electron_entry(licences: dict[str, dict]) -> str:
    manifest = json.loads((ELECTRON / "package.json").read_text(encoding="utf-8"))
    key = f"electron@{manifest['version']}"
    known = licences.get(key)
    if known is None:
        raise Incomplete(f"{key} is not in the shell's node_modules")
    text = licence_text(ELECTRON, known.get("licenseFile"))
    note = (
        "Electron bundles Chromium and Node.js. Their notices, and those of everything\n"
        "they build on, are in LICENSES.chromium.html beside this file."
    )
    return entry("electron", manifest["version"], str(known.get("licenses") or ""), "https://github.com/electron/electron", text) + f"\n{note}\n"


def render(version: str, python: str, client: str, shell: str) -> str:
    for name, section in (("Python", python), ("client", client), ("shell", shell)):
        if not section.strip():
            raise Incomplete(f"the {name} section would be empty")
    return (
        f"Household Spend Tracker {version}: third-party notices\n\n"
        "The app is free software under the GNU Affero General Public License,\n"
        "version 3 or any later version. It is built on the packages below, each\n"
        "under its own licence, reproduced here as its authors ask.\n\n"
        f"{RULE}\nThe backend (Python)\n{RULE}\n\n{python}\n"
        f"{RULE}\nThe web client (JavaScript)\n{RULE}\n\n{client}\n"
        f"{RULE}\nThe desktop shell (JavaScript, Electron)\n{RULE}\n\n{shell}"
    )


def app_version() -> str:
    return json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))["version"]


def write(python: str) -> Path:
    names = [name for name in runtime_names(RUNTIME_LOCK.read_text(encoding="utf-8")) if name not in not_frozen(BACKEND_SPEC.read_text(encoding="utf-8"))]
    backend = python_section(python_licences(python, names))
    client = node_section(bundled(CLIENT_BUNDLED), node_licences(ROOT / "client"))
    shell_licences = node_licences(ROOT / "desktop")
    shell = node_section(bundled(SHELL_BUNDLED), shell_licences) + "\n" + electron_entry(shell_licences)
    chromium = ELECTRON / "dist" / "LICENSES.chromium.html"
    if not chromium.exists():
        raise Incomplete(f"{chromium} is missing: `node node_modules/electron/install.js` in desktop/ fetches it")
    BUILD.mkdir(exist_ok=True)
    shutil.copyfile(chromium, CHROMIUM)
    NOTICES.write_text(render(app_version(), backend, client, shell), encoding="utf-8")
    return NOTICES


def check() -> None:
    """What the packaging step asks before it bundles the notices."""
    for file in (NOTICES, CHROMIUM):
        if not file.exists() or file.stat().st_size == 0:
            raise Incomplete(f"{file} is missing or empty: python -m scripts.third_party_notices first")
    text = NOTICES.read_text(encoding="utf-8")
    for heading in ("The backend (Python)", "The web client (JavaScript)", "The desktop shell (JavaScript, Electron)"):
        if heading not in text:
            raise Incomplete(f"{NOTICES} has no section '{heading}'")
    if f"Household Spend Tracker {app_version()}:" not in text:
        raise Incomplete(f"{NOTICES} was written for another version than {app_version()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check that the notices are there and whole")
    parser.add_argument("--python", default=sys.executable, help="the Python the backend is frozen from (default: this one)")
    args = parser.parse_args(argv)
    if args.check:
        check()
        print(f"{NOTICES}: complete")
        return 0
    written = write(args.python)
    print(f"{written}: {written.stat().st_size:,} bytes, {written.read_text(encoding='utf-8').count(THIN)} packages")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
