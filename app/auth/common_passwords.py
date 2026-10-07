"""The 100,000 most common passwords, shipped with the app (#97).

Length is the rule that works, and a breached-password check is the other half
of it (NIST 800-63B): `qwertyuiopasdfgh` is long and is among the first things
anybody guessing tries. The app never calls out, so a k-anonymity lookup
against an online service is not an option; the list ships with it.

## The list

`common_passwords.txt`, byte for byte as published: the top 100,000 of the
ten million passwords Mark Burnett compiled from public breaches and released
**into the public domain** in 2015 (xato.net, "Today I Am Releasing Ten
Million Passwords"), as distributed in SecLists by Daniel Miessler (MIT
licence) at `Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt`.
`SOURCE_URL` pins the commit it was taken from and `SHA256` the bytes;
`tests/test_common_passwords.py` fails if the file drifts from either, and
`tests/test_data_hygiene.py` exempts exactly these bytes from its name scan --
a password list is full of first names, none of them anybody's data.

It is refreshed when a release is cut: `python -m scripts.common_passwords
--refresh` takes the newest commit of that file, rewrites both, and says
whether anything changed.

## The check

Compared case-insensitively, the whole password against each entry. Only
entries at least as long as the length rule are held in memory -- a shorter
one is refused for its length already -- which is a few hundred strings, read
once, rather than a hundred thousand.
"""

from __future__ import annotations

import functools
import hashlib
import pathlib

#: The SecLists commit the list was taken from, and the file's path in it.
SOURCE_COMMIT = "c205c36a445bff37f8e58a9ec829105cd4975c58"
SOURCE_PATH = "Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt"
SOURCE_URL = (
    f"https://raw.githubusercontent.com/danielmiessler/SecLists/{SOURCE_COMMIT}/{SOURCE_PATH}"
)
#: SHA-256 of `common_passwords.txt` exactly as published at that commit.
SHA256 = "1472aafa2561df5e3293aee252aee3ca660c12b399a283cf808bb01b39be388b"
#: How many it holds. The decision was the top 100,000 (2026-10-07).
ENTRIES = 100_000

LIST = pathlib.Path(__file__).with_name("common_passwords.txt")


def digest(path: pathlib.Path = LIST) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@functools.cache
def _at_least(min_length: int) -> frozenset[str]:
    return frozenset(
        line.casefold()
        for line in LIST.read_text(encoding="utf-8").splitlines()
        if len(line) >= min_length
    )


def is_common(password: str, *, min_length: int) -> bool:
    """Is this, ignoring case, one of the list's passwords of `min_length` or more?"""
    return len(password) >= min_length and password.casefold() in _at_least(min_length)
