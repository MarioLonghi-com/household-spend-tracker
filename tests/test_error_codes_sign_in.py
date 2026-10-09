"""Sign-in, setup, recovery, passkeys and invitations carry codes (#267).

The rule from `test_error_codes.py`, on the second wave: ``detail`` is the
sentence it always was, byte for byte, and beside it a ``code`` with raw
``params`` -- numbers as numbers, an address as typed. An agent's answer does
not change at all.
"""

from __future__ import annotations

import pytest

import app.auth.setup as setup_service
from app.auth import passwords
from app.errors import ValidationError
from tests.conftest import HEADERS, PASSWORD, _setup_owner

EMAIL = "Jane.Doe@gmail.com"


# --------------------------------------------------------------------------- #
# The password complaints
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("password", "email", "first_only", "detail", "code", "params"),
    [
        (
            "short",
            "",
            False,
            "that password will not do: it needs at least 12 characters",
            "password.too_short",
            {"min_length": 12},
        ),
        (
            "password1234",
            "",
            False,
            "that password will not do: it is one of the 100,000 most common passwords, "
            "which are the first ones anybody guessing tries",
            "password.common",
            {"count": 100_000},
        ),
        (
            "jane.doe@gmail.com",
            EMAIL,
            False,
            "that password will not do: it cannot be your email address",
            "password.is_email",
            {},
        ),
        (
            "a@b.co",
            "A@b.co",
            False,
            "that password will not do: it needs at least 12 characters, and it cannot be "
            "your email address",
            "password.too_short_and_is_email",
            {"min_length": 12},
        ),
        # The profile says the first complaint alone, and its code says only that.
        ("a@b.co", "A@b.co", True, "it needs at least 12 characters", "password.too_short", {"min_length": 12}),
    ],
)
def test_each_password_complaint_has_its_code(password, email, first_only, detail, code, params):
    with pytest.raises(ValidationError) as refused:
        passwords.refuse_weak(password, email=email, first_only=first_only)
    assert (str(refused.value), refused.value.code, refused.value.params) == (detail, code, params)


def test_a_good_password_is_not_refused():
    assert passwords.refuse_weak("a sufficiently long one", email=EMAIL) is None


def test_setup_refuses_a_short_password_with_the_same_sentence_and_a_code(client):
    answer = client.post(
        "/api/setup/begin",
        json={
            "token": setup_service.current_setup_token(),
            "email": EMAIL,
            "display_name": "Jane",
            "password": "too short",
        },
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {
        "detail": "that password will not do: it needs at least 12 characters",
        "code": "password.too_short",
        "params": {"min_length": 12},
    }


def test_a_profile_password_that_is_the_address_is_refused_with_a_code(client):
    _setup_owner(client)
    answer = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": "jane.doe@gmail.com"},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {
        "detail": "it cannot be your email address",
        "code": "password.is_email",
        "params": {},
    }


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #


def test_a_wrong_password_carries_a_code_and_the_same_sentence(client):
    _setup_owner(client)
    refused = client.post(
        "/api/session", json={"email": "janedoe@gmail.com", "password": "wrong"}, headers=HEADERS
    )
    unknown = client.post(
        "/api/session", json={"email": "nobody@gmail.com", "password": "wrong"}, headers=HEADERS
    )
    expected = {"detail": "that email and password do not match", "code": "auth.refused", "params": {}}
    assert (refused.status_code, refused.json()) == (401, expected)
    assert unknown.json() == refused.json(), "an unknown address still answers exactly alike"


def test_signed_out_says_so_with_a_code(client):
    _setup_owner(client)
    client.cookies.clear()
    answer = client.get("/api/me")
    assert (answer.status_code, answer.json()) == (
        401,
        {"detail": "sign in first", "code": "auth.sign_in_first", "params": {}},
    )


def test_a_malformed_address_goes_out_as_typed(client):
    answer = client.post(
        "/api/setup/begin",
        json={
            "token": setup_service.current_setup_token(),
            "email": "not-an-address",
            "display_name": "Jane",
            "password": PASSWORD,
        },
        headers=HEADERS,
    )
    assert answer.status_code == 422, answer.text
    assert answer.json() == {
        "detail": "'not-an-address' is not an email address",
        "code": "email.not_an_address",
        "params": {"email": "not-an-address"},
    }


def test_the_rate_limit_sends_its_wait_in_seconds_as_a_number(client):
    _setup_owner(client)
    # A browser this account trusts has a ceiling of its own (#203); a stranger's has five.
    client.cookies.clear()
    answers = [
        client.post("/api/session", json={"email": EMAIL, "password": "wrong"}, headers=HEADERS)
        for _ in range(12)
    ]
    limited = next(one for one in answers if one.status_code == 429)
    body = limited.json()
    assert body["code"] in {"auth.too_many_for_account", "auth.too_many_attempts", "auth.too_many_from_here"}
    seconds = body["params"]["seconds"]
    assert isinstance(seconds, int) and seconds > 0
    assert f"Try again in {seconds} seconds." in body["detail"]
    assert limited.headers["Retry-After"] == str(seconds)


def test_a_setup_after_setup_is_refused_with_a_code(client):
    _setup_owner(client)
    again = client.post(
        "/api/setup/begin",
        json={"token": "x", "email": "someone@example.com", "display_name": "S", "password": PASSWORD},
        headers=HEADERS,
    )
    assert again.status_code == 409
    assert (again.json()["code"], again.json()["params"]) == ("setup.already_done", {})
    assert again.json()["detail"] == "this instance has already been set up"


# --------------------------------------------------------------------------- #
# Agents: unchanged
# --------------------------------------------------------------------------- #


def test_an_agent_with_no_usable_key_gets_the_body_it_always_got(client):
    _setup_owner(client)
    client.cookies.clear()
    answer = client.get("/api/agent/v1/manifest", headers={"Authorization": "Bearer stk_not_a_real_key"})
    assert answer.status_code == 401
    assert "code" not in answer.json() and "params" not in answer.json()
