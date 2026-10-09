"""Build the release zip for a personal computer: `spend-tracker-<version>-compose.zip` (#167).

    python -m scripts.bundle --version 0.9.0 \\
        --app-digest sha256:… --updater-digest sha256:… --zip dist/
    python -m scripts.bundle --check dist/spend-tracker-0.9.0-compose.zip

The zip unpacks to one folder, `spend-tracker-<version>/`, holding

    compose.yaml                   both images by digest, no `build:` (C15)
    .env                           SPENDTRACKER_PUBLIC_URL, nothing secret (C13)
    pin/                           empty; the updater's record goes here
    README.txt
    Start Spend Tracker.command    macOS   } the same script, deploy/bundle/
    start-spend-tracker.sh         Linux   } start-spend-tracker.sh
    Start Spend Tracker.bat        Windows (Docker Desktop tested; Podman untested), CRLF

from the templates in `deploy/bundle/`, with `@VERSION@`, `@APP_IMAGE@` and
`@UPDATER_IMAGE@` filled in. The images are the index digests the release
pushed, so release.yml builds the real zip after `publish-image` and before
the draft goes public; the `build` job builds one with stand-in digests on
every run, dry runs included, so a broken template fails before anything is
published.

**The executable bit is the point of `--check`** (C16): macOS refuses to open
a `.command` that lost it, with a message that says nothing useful. Each entry
records its Unix mode, and `--check` reads them back.

`--app-image` and `--updater-image` take whole references instead of digests,
for a bundle of locally built images (the end-to-end run on a Mac); `--check`
refuses such a zip unless `--local` is given.

Standard library only: release.yml runs this without installing anything.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "deploy" / "bundle"

APP_REPO = "ghcr.io/mariolonghi-com/household-spend-tracker"
UPDATER_REPO = "ghcr.io/mariolonghi-com/household-spend-tracker-updater"

VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PLACEHOLDER = re.compile(r"@(VERSION|APP_IMAGE|UPDATER_IMAGE)@")


@dataclass(frozen=True)
class Entry:
    name: str
    template: str
    mode: int
    crlf: bool = False


#: What the folder holds, in the order the zip lists it. `pin/` is added as a directory.
ENTRIES = (
    Entry("compose.yaml", "compose.yaml", 0o644),
    Entry(".env", "env", 0o644),
    Entry("README.txt", "README.txt", 0o644),
    Entry("Start Spend Tracker.command", "start-spend-tracker.sh", 0o755),
    Entry("start-spend-tracker.sh", "start-spend-tracker.sh", 0o755),
    Entry("Start Spend Tracker.bat", "Start Spend Tracker.bat", 0o644, crlf=True),
)
PIN_DIR = "pin/"
PIN_MODE = 0o775
EXECUTABLE = tuple(e.name for e in ENTRIES if e.mode & 0o111)


def folder(version: str) -> str:
    return f"spend-tracker-{version}"


def zip_name(version: str) -> str:
    return f"spend-tracker-{version}-compose.zip"


def render(text: str, values: dict[str, str]) -> str:
    out = PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
    if PLACEHOLDER.search(out):  # pragma: no cover - a value carrying a placeholder
        raise ValueError("a placeholder survived rendering")
    return out


def files(version: str, app_image: str, updater_image: str) -> dict[str, tuple[bytes, int]]:
    """Each file of the folder: its bytes and its mode."""
    values = {"VERSION": version, "APP_IMAGE": app_image, "UPDATER_IMAGE": updater_image}
    out = {}
    for e in ENTRIES:
        text = render((TEMPLATES / e.template).read_text(encoding="utf-8"), values)
        if e.crlf:
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        out[e.name] = (text.encode("utf-8"), e.mode)
    return out


def _info(name: str, mode: int, when: tuple[int, ...]) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=when)  # type: ignore[arg-type]
    info.create_system = 3  # Unix: the mode in external_attr is read back
    kind = stat.S_IFDIR if name.endswith("/") else stat.S_IFREG
    info.external_attr = ((kind | mode) & 0xFFFF) << 16
    if kind == stat.S_IFDIR:
        info.external_attr |= 0x10  # MS-DOS directory flag
    info.compress_type = zipfile.ZIP_STORED if kind == stat.S_IFDIR else zipfile.ZIP_DEFLATED
    return info


def build(
    version: str, app_image: str, updater_image: str, out_dir: Path, epoch: int | None = None
) -> Path:
    """Write the zip into `out_dir`; returns its path."""
    when = time.localtime(epoch if epoch is not None else time.time())[:6]
    when = (max(when[0], 1980), *when[1:])
    top = folder(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / zip_name(version)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(_info(f"{top}/", 0o755, when), b"")
        for name, (data, mode) in files(version, app_image, updater_image).items():
            z.writestr(_info(f"{top}/{name}", mode, when), data)
        z.writestr(_info(f"{top}/{PIN_DIR}", PIN_MODE, when), b"")
    return path


def image(repo: str, version: str, digest: str) -> str:
    if not DIGEST.fullmatch(digest):
        raise SystemExit(f"{digest!r} is not a sha256 digest")
    return f"{repo}:{version}@{digest}"


# --------------------------------------------------------------------------- #
# --check
# --------------------------------------------------------------------------- #


def problems(path: Path, local: bool = False) -> list[str]:
    """What is wrong with a built zip. Empty is good."""
    found: list[str] = []
    with zipfile.ZipFile(path) as z:
        infos = {i.filename: i for i in z.infolist()}
        tops = {n.split("/", 1)[0] for n in infos}
        if len(tops) != 1:
            return [f"the zip holds {len(tops)} top-level entries, not one folder"]
        top = tops.pop()
        m = re.fullmatch(r"spend-tracker-(.+)", top)
        version = m.group(1) if m else ""
        if not VERSION.fullmatch(version):
            found.append(f"the folder {top!r} names no release")
        if path.name != zip_name(version):
            found.append(f"the zip is called {path.name!r}, not {zip_name(version)!r}")
        expected = {f"{top}/", f"{top}/{PIN_DIR}", *(f"{top}/{e.name}" for e in ENTRIES)}
        if set(infos) != expected:
            found.append(
                f"entries differ: missing {sorted(expected - set(infos))}, extra {sorted(set(infos) - expected)}"
            )
        for e in ENTRIES:
            info = infos.get(f"{top}/{e.name}")
            if info is None:
                continue
            mode = (info.external_attr >> 16) & 0o777
            if info.create_system != 3 or mode != e.mode:
                found.append(f"{e.name} is mode {oct(mode)}, not {oct(e.mode)}")
        pin = infos.get(f"{top}/{PIN_DIR}")
        if pin is not None and not stat.S_ISDIR(pin.external_attr >> 16):
            found.append("pin/ is not a directory")

        compose = z.read(f"{top}/compose.yaml").decode() if f"{top}/compose.yaml" in infos else ""
        env = z.read(f"{top}/.env").decode() if f"{top}/.env" in infos else ""
        launchers = [z.read(f"{top}/{n}").decode() for n in EXECUTABLE if f"{top}/{n}" in infos]

    for text, what in [(compose, "compose.yaml"), (env, ".env"), *((t, "a launcher") for t in launchers)]:
        if PLACEHOLDER.search(text):
            found.append(f"{what} still holds a placeholder")
    if re.search(r"^\s*build:", compose, re.MULTILINE):
        found.append("compose.yaml has a build: section")
    if "mem_limit: 768m" not in compose:
        found.append("compose.yaml does not limit the app to 768m")
    if "SPENDTRACKER_PUBLIC_URL=http://localhost:8848" not in env.splitlines():
        found.append(".env does not set SPENDTRACKER_PUBLIC_URL=http://localhost:8848")
    images = re.findall(r"image: \$\{SPENDTRACKER_(?:UPDATER_)?IMAGE:-([^}]+)\}", compose)
    if len(images) != 2:
        found.append(f"compose.yaml names {len(images)} images, not 2")
    for ref in images:
        if not local and not re.fullmatch(rf"[^@\s]+:{re.escape(version)}@sha256:[0-9a-f]{{64}}", ref):
            found.append(f"{ref} is not this release by digest")
        if any(f"'{ref}'" not in t for t in launchers):
            found.append(f"a launcher does not name {ref}")
    return found


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.bundle", description=__doc__.splitlines()[0])
    p.add_argument("--version")
    p.add_argument("--app-digest")
    p.add_argument("--updater-digest")
    p.add_argument("--app-image", help="a whole reference, for a bundle of local images")
    p.add_argument("--updater-image", help="a whole reference, for a bundle of local images")
    p.add_argument("--zip", type=Path, help="the directory to write the zip into")
    p.add_argument("--check", type=Path, help="a built zip to check")
    p.add_argument("--local", action="store_true", help="--check: accept images that are not by digest")
    args = p.parse_args(argv)

    if args.check:
        found = problems(args.check, local=args.local)
        for line in found:
            print(f"{args.check.name}: {line}", file=sys.stderr)
        if not found:
            print(f"{args.check.name}: ok")
        return 1 if found else 0

    if not (args.version and VERSION.fullmatch(args.version) and args.zip):
        p.error("--version X.Y.Z and --zip are required to build")
    app = args.app_image or image(APP_REPO, args.version, args.app_digest or "")
    updater = args.updater_image or image(UPDATER_REPO, args.version, args.updater_digest or "")
    stamp = os.environ.get("SOURCE_DATE_EPOCH", "")
    epoch = int(stamp) if stamp.isdigit() else None
    print(build(args.version, app, updater, args.zip, epoch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
