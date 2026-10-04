"""One test per row of the folding table, and one for everyone else.

The table is the kind of thing that gets "tidied" by someone who assumes Gmail's
rule is universal. These are what make that fail.
"""

from __future__ import annotations

import pytest

from app.auth.email_canonical import FOLDING, canonical
from app.errors import ValidationError


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Jane.Doe+spend@Gmail.com", "janedoe@gmail.com"),
        ("j.a.n.e@googlemail.com", "jane@googlemail.com"),
        ("user.name@proton.me", "username@proton.me"),
        ("user_name@protonmail.com", "username@protonmail.com"),
        ("user-name@protonmail.ch", "username@protonmail.ch"),
        ("user.name+tag@pm.me", "username@pm.me"),
    ],
)
def test_providers_that_fold(raw, expected):
    assert canonical(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Dots are significant everywhere else, so these stay distinct.
        ("john.smith@fastmail.com", "john.smith@fastmail.com"),
        ("john.smith@outlook.com", "john.smith@outlook.com"),
        ("john.smith@icloud.com", "john.smith@icloud.com"),
        # A Google Workspace custom domain looks like any other domain, and
        # Google's own help page says dots DO change the address there.
        ("john.smith@example.org", "john.smith@example.org"),
    ],
)
def test_unlisted_domains_keep_their_dots(raw, expected):
    assert canonical(raw) == expected


def test_plus_addressing_is_stripped_everywhere():
    assert canonical("john.smith+bills@fastmail.com") == "john.smith@fastmail.com"


def test_dotted_and_undotted_gmail_are_the_same_account():
    assert canonical("jane.doe@gmail.com") == canonical("janedoe@gmail.com")


def test_dotted_and_undotted_fastmail_are_not():
    assert canonical("john.smith@fastmail.com") != canonical("johnsmith@fastmail.com")


@pytest.mark.parametrize("bad", ["", "nobody", "no@domain", "@example.com", "+only@gmail.com"])
def test_what_is_not_an_address(bad):
    with pytest.raises(ValidationError):
        canonical(bad)


def test_every_folding_domain_is_covered_by_a_test():
    """If a provider joins the table, it needs its own row above."""
    tested = {
        "gmail.com", "googlemail.com", "proton.me", "protonmail.com", "protonmail.ch", "pm.me",
    }
    assert set(FOLDING) == tested, "a domain joined or left FOLDING without a test"
