"""Password hashing, and the rule that an unknown email costs the same as a
wrong password.

An early return on "no such user" is a timing oracle that tells an attacker
which addresses exist. The verify below does the same work either way, and the
caller must answer with the same sentence in both cases.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..errors import ValidationError
from . import common_passwords

#: argon2-cffi's defaults are argon2id, and current.
_hasher = PasswordHasher()

#: Hashed once at import, so verifying against a user who does not exist costs
#: the same as verifying against one who does.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))

MIN_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True only if the hash exists and matches. Constant work either way."""
    try:
        _hasher.verify(stored_hash if stored_hash is not None else _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return stored_hash is not None


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def complaints(password: str, *, email: str = "") -> list[str]:
    """What is wrong with this password, in words the user can act on.

    Length is the rule that works, with a check against the passwords people
    actually choose (#97, `common_passwords`). Composition rules ("one capital,
    one digit") were retired by NIST 800-63B because they push people toward
    Passw0rd! and nothing else.
    """
    problems: list[str] = []
    if len(password) < MIN_LENGTH:
        problems.append(f"it needs at least {MIN_LENGTH} characters")
    elif common_passwords.is_common(password, min_length=MIN_LENGTH):
        problems.append(
            "it is one of the 100,000 most common passwords, which are the first ones "
            "anybody guessing tries"
        )
    if email and password.strip().lower() == email.strip().lower():
        problems.append("it cannot be your email address")
    return problems


def refuse_weak(password: str, *, email: str = "", first_only: bool = False) -> None:
    """Refuse a password `complaints` finds fault with, with a code (#267).

    ``detail`` is what each caller always said: every complaint after "that
    password will not do: ", or, with ``first_only``, the first complaint
    alone (the profile's change of password). The code names the complaints
    that sentence holds, and the params are numbers, never words: a length,
    the size of the common-password list.
    """
    problems = complaints(password, email=email)
    if not problems:
        return
    if first_only:
        problems = problems[:1]
    detail = problems[0] if first_only else "that password will not do: " + ", and ".join(problems)
    short = len(password) < MIN_LENGTH
    is_email = bool(email) and password.strip().lower() == email.strip().lower()
    both = len(problems) == 2
    if short and both:
        raise ValidationError(
            detail, code="password.too_short_and_is_email", params={"min_length": MIN_LENGTH}
        )
    if short:
        raise ValidationError(detail, code="password.too_short", params={"min_length": MIN_LENGTH})
    if both:
        raise ValidationError(
            detail,
            code="password.common_and_is_email",
            params={"count": common_passwords.ENTRIES},
        )
    if is_email and len(problems) == 1 and problems[0].startswith("it cannot"):
        raise ValidationError(detail, code="password.is_email")
    raise ValidationError(
        detail, code="password.common", params={"count": common_passwords.ENTRIES}
    )
