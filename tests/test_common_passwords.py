"""New passwords are checked against the 100,000 most common (#97).

Every path that sets a password -- the setup wizard, changing your own, a
reset link -- goes through `passwords.complaints`, so the rule is tested there
and then through the routes, asserting the stored hash did or did not move.
"""

from __future__ import annotations

import pathlib

import pytest

from app.auth import common_passwords, passwords
from tests.conftest import HEADERS, PASSWORD, _setup_owner

COMMON = "it is one of the 100,000 most common passwords"


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #


def test_the_list_is_the_published_one_byte_for_byte():
    raw = common_passwords.LIST.read_bytes()
    assert common_passwords.digest() == common_passwords.SHA256
    assert len(raw.splitlines()) == common_passwords.ENTRIES == 100_000
    assert b"\r" not in raw
    assert common_passwords.SOURCE_COMMIT in common_passwords.SOURCE_URL
    assert common_passwords.SOURCE_URL.startswith("https://raw.githubusercontent.com/danielmiessler/SecLists/")


# Reads the README: run on every pull request, so a README-only change meets it.
@pytest.mark.repo_wide
def test_its_source_and_licence_are_recorded_where_people_look():
    root = pathlib.Path(__file__).resolve().parent.parent
    doc = common_passwords.__doc__ or ""
    assert "public domain" in doc and "MIT" in doc and "SecLists" in doc
    readme = (root / "README.md").read_text()
    assert "app/auth/common_passwords.txt" in readme and "public domain" in readme


def test_the_hygiene_scan_would_flag_it_so_the_exemption_is_narrow():
    """The exemption is not decoration: the list trips the name and marker
    detectors, and only these exact bytes at this exact path are let through."""
    from tests import test_data_hygiene as hygiene

    lines = common_passwords.LIST.read_text(encoding="utf-8").splitlines()
    assert any(hygiene.real_names_in(line) or hygiene.private_markers_in(line) for line in lines)
    assert hygiene.vendored(common_passwords.LIST)
    assert not hygiene.vendored(common_passwords.LIST.with_name("passwords.py"))


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("password", ["qwertyuiopasdfgh", "1q2w3e4r5t6y", "QwertyQwerty", "123456QWERTY"])
def test_a_common_long_password_is_refused_in_words(password):
    assert passwords.complaints(password) == [
        "it is one of the 100,000 most common passwords, which are the first ones "
        "anybody guessing tries"
    ]


@pytest.mark.parametrize("password", [PASSWORD, "qwertyuiopasdfgh!", "an entirely different long password"])
def test_an_uncommon_long_password_passes(password):
    assert passwords.complaints(password) == []


def test_a_short_common_password_is_refused_for_its_length_alone():
    assert passwords.complaints("password") == ["it needs at least 12 characters"]
    assert not common_passwords.is_common("password", min_length=passwords.MIN_LENGTH)


def test_only_the_entries_long_enough_to_matter_are_held():
    held = common_passwords._at_least(passwords.MIN_LENGTH)
    assert 0 < len(held) < 1_000
    assert all(len(entry) >= passwords.MIN_LENGTH for entry in held)


# --------------------------------------------------------------------------- #
# Through the routes, asserting what was stored
# --------------------------------------------------------------------------- #


def _hash_of(client, user_id: str) -> str:
    from sqlalchemy.orm import Session

    from app.models import User

    with Session(client.app_module.db_engine) as own:
        return own.get(User, user_id).password_hash


def test_changing_to_a_common_password_is_refused_and_changes_nothing(client):
    world = _setup_owner(client)
    before = _hash_of(client, world["user"]["id"])

    refused = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": "QWERTYuiopASDFGH"},
        headers=HEADERS,
    )
    assert refused.status_code == 422
    assert COMMON in refused.json()["detail"]
    assert _hash_of(client, world["user"]["id"]) == before

    changed = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": "qwertyuiopasdfgh plus a tail"},
        headers=HEADERS,
    )
    assert changed.status_code == 200, changed.text
    after = _hash_of(client, world["user"]["id"])
    assert after != before
    assert passwords.verify_password(after, "qwertyuiopasdfgh plus a tail")


def test_the_setup_wizard_refuses_a_common_password(tmp_path, monkeypatch):
    from app.auth import setup
    from app.errors import ValidationError

    monkeypatch.setattr(setup, "setup_token_path", lambda: tmp_path / "setup-token")
    token = setup.rotate_setup_token()
    with pytest.raises(ValidationError, match="most common passwords"):
        setup.begin(token, email="a@example.com", display_name="A", password="1qaz2wsx3edc")
    blob = setup.begin(token, email="a@example.com", display_name="A", password="1qaz2wsx3edc4rfv5tgb!")
    assert passwords.verify_password(blob.password_hash, "1qaz2wsx3edc4rfv5tgb!")


def test_the_release_check_of_the_list_passes(capsys):
    """`python -m scripts.common_passwords` with no argument: what a release
    runs before `--refresh`, and what says the file and the code agree."""
    from scripts import common_passwords as script

    assert script.main([]) == 0
    said = capsys.readouterr().out
    assert "100000 entries" in said and common_passwords.SHA256 in said
