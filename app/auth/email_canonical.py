"""Folding an email address to the one account it belongs to.

Plus-addressing is stripped everywhere: ``jane+spend@`` and ``jane@`` are one
mailbox at every provider that supports the syntax.

Dot-folding is **not** universal, and applying it everywhere would merge two
people who genuinely own dotted and undotted addresses at the same domain. So
it is applied only to providers that document that they ignore those
characters. Everywhere else, ``john.smith@`` and ``johnsmith@`` are different
mailboxes and this module treats them as such.
"""

from __future__ import annotations

from ..errors import ValidationError

#: Domain -> the characters that provider ignores in the local part.
#:
#: Gmail ignores dots:
#:   https://support.google.com/mail/answer/7436150
#:   "If someone accidentally adds dots to your address when emailing you,
#:   you'll still get that email." Google Workspace custom domains do NOT
#:   behave this way, and cannot be told apart from any other domain.
#:
#: Proton treats dots, hyphens and underscores as transparent:
#:   https://proton.me/support/change-username
#:   "these characters are treated as transparent by our service. That is,
#:   username is the same as user_name and user.name."
FOLDING: dict[str, frozenset[str]] = {
    "gmail.com": frozenset({"."}),
    "googlemail.com": frozenset({"."}),
    "proton.me": frozenset({".", "-", "_"}),
    "protonmail.com": frozenset({".", "-", "_"}),
    "protonmail.ch": frozenset({".", "-", "_"}),
    "pm.me": frozenset({".", "-", "_"}),
}

#: Checked and deliberately absent: Outlook/Hotmail/Live, Yahoo, iCloud and
#: Fastmail all treat dots as significant. Yandex is reported to treat "." and
#: "-" as the *same* character (an equivalence, not a fold), which this table
#: cannot express and which their own documentation does not confirm.

MAX_LENGTH = 254


def canonical(email: str) -> str:
    """The form that decides whether two addresses are one account.

    Stored alongside the address as typed, and the one every lookup uses.
    """
    cleaned = (email or "").strip().lower()
    if not cleaned or cleaned.count("@") < 1:
        raise ValidationError(f"{email!r} is not an email address")
    if len(cleaned) > MAX_LENGTH:
        raise ValidationError("that email address is too long")

    local, _, domain = cleaned.rpartition("@")
    if not local or "." not in domain:
        raise ValidationError(f"{email!r} is not an email address")

    # Sub-addressing: everything from the first plus is a label, not an inbox.
    local = local.split("+", 1)[0]

    for character in FOLDING.get(domain, frozenset()):
        local = local.replace(character, "")

    if not local:
        raise ValidationError(f"{email!r} has no usable name before the @")
    return f"{local}@{domain}"
