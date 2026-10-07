"""Two members with passkeys, for the passkey tests (#120, #121).

Fixtures and helpers shared by `test_passkeys_register.py` and
`test_passkey_sign_in.py`, registered for the suite in `conftest.py`.
`passkey_world` has two of everything (CLAUDE.md): two members, two passkeys
each -- one synced, one device-bound -- on two RP IDs, the instance's own and
one a renamed host left behind.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import HEADERS, PASSWORD
from tests.soft_authenticator import SoftAuthenticator

HERE = "testserver"
ORIGIN = f"https://{HERE}"
OLD_HOST = "old.example.ts.net"
DEVICE_BOUND = bytes(16)  # an authenticator that will not say what it is


@pytest.fixture()
def passkeys_on(monkeypatch, client):
    """The suite's own address as the public URL, so passkeys are available
    to the test client, as they would be at a tailnet name."""
    import app.config as config

    monkeypatch.setenv("SPENDTRACKER_PUBLIC_URL", ORIGIN)
    monkeypatch.delenv("SPENDTRACKER_RP_ID", raising=False)
    monkeypatch.setattr(config, "settings", config.Settings.from_env())
    assert config.settings.rp_id == HERE


def _ledger() -> Path:
    from app import config

    return Path(config.settings.database_url.split("///", 1)[-1])


def _rows(client, model, *where):
    with Session(client.app_module.db_engine) as own:
        return list(own.execute(select(model).where(*where)).scalars())


def step_up(browser, secret: str, clock) -> str:
    answer = browser.post(
        "/api/me/step-up",
        json={"password": PASSWORD, "code": pyotp.TOTP(secret).at(clock())},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["token"]


def options(browser, secret: str, clock) -> dict:
    answer = browser.post(
        "/api/me/passkeys/options",
        json={"step_up_token": step_up(browser, secret, clock)},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def register(browser, secret, clock, authenticator: SoftAuthenticator, *, label=None) -> dict:
    made = authenticator.create(options(browser, secret, clock), origin=ORIGIN)
    answer = browser.post(
        "/api/me/passkeys", json={"credential": made, "label": label}, headers=HEADERS
    )
    assert answer.status_code == 201, answer.text
    return answer.json()


@pytest.fixture()
def passkey_world(client, clock, passkeys_on):
    """Two members, two passkeys each, on two RP IDs."""
    from tests.test_recovery_codes_regenerate import _two_members

    people = _two_members(client)
    owner = {**people["owner"], "client": client}
    member = people["member"]
    for person in (owner, member):
        person["synced"] = SoftAuthenticator()
        person["bound"] = SoftAuthenticator(aaguid=DEVICE_BOUND, synced=False)
        person["passkeys"] = [
            register(person["client"], person["secret"], clock, person["synced"]),
            register(person["client"], person["secret"], clock, person["bound"], label="Work laptop"),
        ]
    # What a restore onto a renamed host leaves: rows made under the old name.
    with sqlite3.connect(_ledger()) as conn:
        for person in (owner, member):
            conn.execute(
                "UPDATE passkeys SET rp_id = ? WHERE id = ?", (OLD_HOST, person["passkeys"][1]["id"])
            )
    return {"owner": owner, "member": member}


