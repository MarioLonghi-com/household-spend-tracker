"""Does an environment hold exactly what a lock says it should?

    python3 scripts/lock_matches.py requirements.txt installed.txt

`installed.txt` is one `name==version` per line -- `pip freeze`, or the
container's `importlib.metadata` read out by tests.yml's `image` job. The lock
is a `uv pip compile --universal` file, so some of its lines carry an
environment marker (`colorama ... ; sys_platform == 'win32'`); each is
evaluated for the interpreter running this script, which in CI is a Linux
CPython like the image. Exits 1 and names every difference when the two
disagree (#46).

Standard library only, apart from the marker parser: `packaging` when it is
installed, otherwise the copy pip vendors -- so the runner needs nothing
installed for this, and nothing is installed unpinned to run it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:  # pragma: no cover - which one depends on the interpreter
    from packaging.markers import Marker
except ImportError:  # pragma: no cover
    from pip._vendor.packaging.markers import Marker  # type: ignore[no-redef]

# What a virtualenv brings with it rather than what the lock installs.
BOOTSTRAP = {"pip", "setuptools", "wheel"}

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?\s*$")


def canonical(name: str) -> str:
    """PEP 503: `Pillow`, `pillow` and `PILLOW` are one package, as are `a_b` and `a-b`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def locked(text: str, environment: dict[str, str] | None = None) -> dict[str, str]:
    """The lock's pins that apply here, by canonical name.

    `pip` and `setuptools` can be in a lock (pip-audit requires them) and are
    compared like any other line, except by `differences`, which leaves the
    bootstrap tools out on both sides: a venv brings its own.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _PIN.match(line)
        if not match:
            continue
        name, version, marker = match.groups()
        if marker and not Marker(marker).evaluate(environment):
            continue
        found[canonical(name)] = version
    return found


def installed(text: str) -> dict[str, str]:
    """`name==version` lines, by canonical name, without a venv's own tools."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, version = line.strip().partition("==")
        if sep and canonical(name) not in BOOTSTRAP:
            found[canonical(name)] = version
    return found


def differences(lock: dict[str, str], have: dict[str, str]) -> list[str]:
    out = []
    for name in sorted((lock.keys() | have.keys()) - BOOTSTRAP):
        want, got = lock.get(name), have.get(name)
        if want == got:
            continue
        if got is None:
            out.append(f"{name}: locked at {want}, not installed")
        elif want is None:
            out.append(f"{name}: {got} installed, not in the lock")
        else:
            out.append(f"{name}: locked at {want}, {got} installed")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    lock = locked(Path(argv[0]).read_text(encoding="utf-8"))
    have = installed(Path(argv[1]).read_text(encoding="utf-8"))
    wrong = differences(lock, have)
    if wrong:
        print(f"{argv[1]} is not what {argv[0]} locks:", *wrong, sep="\n  ")
        return 1
    print(f"{len(have)} packages installed, exactly as {argv[0]} locks them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
