"""The bundled common-password list: check it, or refresh it when cutting a release.

    python -m scripts.common_passwords              # is the file what the code says?
    python -m scripts.common_passwords --refresh    # take the newest published copy

The list and where it comes from are described in `app/auth/common_passwords.py`
(#97). It is refreshed per release, decided 2026-10-07: `--refresh` asks GitHub
for the newest commit of the file in SecLists, downloads the file at that
commit, refuses it unless it still holds exactly `ENTRIES` lines, then rewrites
`common_passwords.txt` and the `SOURCE_COMMIT` and `SHA256` constants together.
It says whether anything changed; most releases, nothing will have. It writes
and stops -- committing is the release's business.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import sys
import urllib.request

import certifi

from app.auth import common_passwords as cp

_API = "https://api.github.com/repos/danielmiessler/SecLists/commits?per_page=1&path="
_RAW = "https://raw.githubusercontent.com/danielmiessler/SecLists/{commit}/{path}"
_MODULE = cp.LIST.with_name("common_passwords.py")


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "household-spend-tracker"})
    # certifi's roots, not the interpreter's: a python.org build on macOS has
    # none until its "Install Certificates" step has been run.
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=60, context=context) as answer:  # noqa: S310
        return answer.read()


def check() -> int:
    found = cp.digest()
    lines = len(cp.LIST.read_bytes().splitlines())
    print(f"{cp.LIST.name}: {lines} entries, sha256 {found}")
    print(f"source: {cp.SOURCE_URL}")
    if found != cp.SHA256 or lines != cp.ENTRIES:
        print(f"MISMATCH: the code says {cp.ENTRIES} entries, sha256 {cp.SHA256}")
        return 1
    return 0


def refresh() -> int:
    newest = json.loads(_get(_API + cp.SOURCE_PATH))[0]["sha"]
    body = _get(_RAW.format(commit=newest, path=cp.SOURCE_PATH))
    count = len(body.splitlines())
    if count != cp.ENTRIES:
        print(f"refused: the file at {newest} has {count} lines, not {cp.ENTRIES}")
        return 1
    digest = hashlib.sha256(body).hexdigest()
    if (newest, digest) == (cp.SOURCE_COMMIT, cp.SHA256):
        print(f"unchanged: {newest}")
        return 0
    cp.LIST.write_bytes(body)
    source = _MODULE.read_text()
    source = re.sub(r'^SOURCE_COMMIT = "[0-9a-f]+"$', f'SOURCE_COMMIT = "{newest}"', source, flags=re.M)
    source = re.sub(r'^SHA256 = "[0-9a-f]+"$', f'SHA256 = "{digest}"', source, flags=re.M)
    _MODULE.write_text(source)
    same = "the same bytes" if digest == cp.SHA256 else "new bytes"
    print(f"refreshed: {cp.SOURCE_COMMIT[:12]} -> {newest[:12]}, {same}; commit both files")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="take the newest published copy")
    args = parser.parse_args(argv)
    return refresh() if args.refresh else check()


if __name__ == "__main__":
    sys.exit(main())
