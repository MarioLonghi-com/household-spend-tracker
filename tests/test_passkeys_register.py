"""Registering, listing, renaming and removing passkeys (#120).

Every passkey here is made by `tests/soft_authenticator.py`, a real P-256
authenticator in software, and goes through the real ceremony: a step-up
grant, the options, the browser's answer, `webauthn`'s verification. The
tests assert what landed in the database -- the row, its RP ID, its counter
and flags, the member's user handle -- and, for every refusal, that nothing
did.

The fixture has two of everything (CLAUDE.md): two members, two passkeys
each, one synced and one device-bound, on two RP IDs -- the instance's own and
one a renamed host left behind.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import Batch, Change, Passkey, User, WebAuthnChallenge, utcnow
from tests.conftest import HEADERS
from tests.passkey_world import (
    DEVICE_BOUND,
    HERE,
    OLD_HOST,
    ORIGIN,
    _ledger,
    _rows,
    options,
    register,
    step_up,
)
from tests.soft_authenticator import ICLOUD_KEYCHAIN, SoftAuthenticator, b64url
from tests.test_recovery_codes_regenerate import _two_members

# --------------------------------------------------------------------------- #
# Registering
# --------------------------------------------------------------------------- #


def test_a_registered_passkey_is_stored_as_the_authenticator_made_it(client, clock, passkeys_on):
    people = _two_members(client)
    owner_id = people["owner"]["user"]["id"]
    authenticator = SoftAuthenticator()

    offered = options(client, people["owner"]["secret"], clock)
    assert offered["rp"] == {"id": HERE, "name": "Spend Tracker"}
    assert offered["authenticatorSelection"]["residentKey"] == "required"
    assert offered["authenticatorSelection"]["userVerification"] == "required"
    [user] = _rows(client, User, User.id == owner_id)
    # The handle is random, not the id and not the email, and it is what the
    # options carry.
    assert offered["user"]["id"] == b64url(user.webauthn_user_handle)
    assert len(user.webauthn_user_handle) == 32
    assert owner_id.encode() not in user.webauthn_user_handle
    assert offered["user"]["name"] == "Jane.Doe@gmail.com"

    made = authenticator.create(offered, origin=ORIGIN)
    answer = client.post("/api/me/passkeys", json={"credential": made}, headers=HEADERS)
    assert answer.status_code == 201, answer.text

    [row] = _rows(client, Passkey)
    assert (row.user_id, row.credential_id, row.rp_id) == (owner_id, made["id"], HERE)
    assert (row.sign_count, row.backup_eligible, row.backed_up) == (0, True, True)
    assert row.aaguid == "fbfc3007-154e-4ecc-8c0b-6e020557d7bd"
    assert row.label == "iCloud Keychain"
    assert row.transports == ["internal", "hybrid"]
    assert row.last_used_at is None
    assert answer.json() == {
        "id": row.id,
        "label": "iCloud Keychain",
        "created_at": answer.json()["created_at"],
        "last_used_at": None,
        "synced": True,
        "rp_id": HERE,
        "usable_here": True,
        "aaguid": "fbfc3007-154e-4ecc-8c0b-6e020557d7bd",
    }
    # Spent: nothing is left to answer twice.
    assert _rows(client, WebAuthnChallenge) == []


def test_only_known_transports_are_kept(client, clock, passkeys_on):
    people = _two_members(client)
    made = SoftAuthenticator().create(options(client, people["owner"]["secret"], clock), origin=ORIGIN)
    made["response"]["transports"] = ["usb", "x" * 5000, 7, "internal", "usb"]
    assert client.post("/api/me/passkeys", json={"credential": made}, headers=HEADERS).status_code == 201
    [row] = _rows(client, Passkey)
    assert row.transports == ["usb", "internal"]


def test_a_device_bound_passkey_of_an_unnamed_provider_says_so(client, clock, passkeys_on):
    people = _two_members(client)
    made = register(client, people["owner"]["secret"], clock, SoftAuthenticator(aaguid=DEVICE_BOUND, synced=False))
    [row] = _rows(client, Passkey)
    assert (row.label, row.backup_eligible, row.backed_up) == ("Passkey", False, False)
    assert made["synced"] is False


def test_the_second_passkey_options_exclude_the_first(client, clock, passkeys_on):
    people = _two_members(client)
    first = register(client, people["owner"]["secret"], clock, SoftAuthenticator())
    [row] = _rows(client, Passkey, Passkey.id == first["id"])
    offered = options(client, people["owner"]["secret"], clock)
    assert [one["id"] for one in offered["excludeCredentials"]] == [row.credential_id]
    # The handle does not change on a second registration.
    assert len({tuple(_rows(client, User, User.webauthn_user_handle.is_not(None)))[0].webauthn_user_handle}) == 1


def test_adding_a_passkey_without_a_step_up_grant_is_refused(client, clock, passkeys_on):
    people = _two_members(client)
    for token in ("", "not-a-grant"):
        answer = client.post("/api/me/passkeys/options", json={"step_up_token": token}, headers=HEADERS)
        assert answer.status_code == 401, answer.text
    # A grant is single-use: spent on the first options, refused on the second.
    token = step_up(client, people["owner"]["secret"], clock)
    assert client.post("/api/me/passkeys/options", json={"step_up_token": token}, headers=HEADERS).status_code == 200
    assert client.post("/api/me/passkeys/options", json={"step_up_token": token}, headers=HEADERS).status_code == 401
    # Another member's grant is no use either.
    theirs = step_up(people["member"]["client"], people["member"]["secret"], clock)
    assert client.post("/api/me/passkeys/options", json={"step_up_token": theirs}, headers=HEADERS).status_code == 401
    assert _rows(client, Passkey) == []
    owner = _rows(client, User, User.id == people["owner"]["user"]["id"])[0]
    member = _rows(client, User, User.id == people["member"]["user"]["id"])[0]
    assert member.webauthn_user_handle is None
    assert owner.webauthn_user_handle is not None  # the one options call that was paid for


def test_a_replayed_challenge_is_refused(client, clock, passkeys_on):
    people = _two_members(client)
    offered = options(client, people["owner"]["secret"], clock)
    first = SoftAuthenticator().create(offered, origin=ORIGIN)
    assert client.post("/api/me/passkeys", json={"credential": first}, headers=HEADERS).status_code == 201

    # The same answer again, and a second authenticator answering the same
    # options: both present a challenge that is already spent.
    again = client.post("/api/me/passkeys", json={"credential": first}, headers=HEADERS)
    second = SoftAuthenticator().create(offered, origin=ORIGIN)
    other = client.post("/api/me/passkeys", json={"credential": second}, headers=HEADERS)
    assert (again.status_code, other.status_code) == (422, 422)
    assert other.json()["detail"] == "that passkey could not be registered here. Start again."
    assert [row.credential_id for row in _rows(client, Passkey)] == [first["id"]]


def test_a_challenge_issued_to_one_member_is_no_use_to_another(client, clock, passkeys_on):
    people = _two_members(client)
    offered = options(client, people["owner"]["secret"], clock)
    made = SoftAuthenticator().create(offered, origin=ORIGIN)
    answer = people["member"]["client"].post("/api/me/passkeys", json={"credential": made}, headers=HEADERS)
    assert answer.status_code == 422
    assert _rows(client, Passkey) == []
    # And it was spent by the attempt, so the owner cannot use it afterwards.
    assert client.post("/api/me/passkeys", json={"credential": made}, headers=HEADERS).status_code == 422


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param({"origin": "https://evil.example"}, id="another origin"),
        pytest.param({"user_verifies": False}, id="no user verification"),
    ],
)
def test_an_answer_that_does_not_verify_stores_nothing(client, clock, passkeys_on, spoil):
    people = _two_members(client)
    offered = options(client, people["owner"]["secret"], clock)
    authenticator = SoftAuthenticator(user_verifies=spoil.get("user_verifies", True))
    made = authenticator.create(offered, origin=spoil.get("origin", ORIGIN))
    answer = client.post("/api/me/passkeys", json={"credential": made}, headers=HEADERS)
    assert answer.status_code == 422
    assert _rows(client, Passkey) == []


def test_an_expired_challenge_is_refused_and_swept(client, clock, passkeys_on):
    from app.auth import housekeeping

    people = _two_members(client)
    stale = options(client, people["owner"]["secret"], clock)
    with sqlite3.connect(_ledger()) as conn:
        conn.execute(
            "UPDATE webauthn_challenges SET expires_at = ?",
            ((utcnow() - timedelta(seconds=1)).isoformat(sep=" "),),
        )
    # A second member's challenge, still live, has to survive the sweep.
    options(people["member"]["client"], people["member"]["secret"], clock)

    removed = housekeeping.sweep(client.app_module.db_engine)
    assert removed["webauthn_challenges"] == 1
    [left] = _rows(client, WebAuthnChallenge)
    assert left.user_id == people["member"]["user"]["id"]

    made = SoftAuthenticator().create(stale, origin=ORIGIN)
    assert client.post("/api/me/passkeys", json={"credential": made}, headers=HEADERS).status_code == 422
    assert _rows(client, Passkey) == []


def test_an_expired_challenge_is_refused_before_the_sweep_reaches_it(client, clock, passkeys_on):
    people = _two_members(client)
    stale = options(client, people["owner"]["secret"], clock)
    with sqlite3.connect(_ledger()) as conn:
        conn.execute(
            "UPDATE webauthn_challenges SET expires_at = ?",
            ((utcnow() - timedelta(seconds=1)).isoformat(sep=" "),),
        )
    made = SoftAuthenticator().create(stale, origin=ORIGIN)
    assert client.post("/api/me/passkeys", json={"credential": made}, headers=HEADERS).status_code == 422
    assert _rows(client, Passkey) == []
    assert _rows(client, WebAuthnChallenge) == []


def test_nothing_is_offered_or_registered_where_passkeys_are_unavailable(client, clock):
    """No public URL: the options are refused with the reason, and the grant
    is not spent on a request that could only fail."""
    people = _two_members(client)
    token = step_up(client, people["owner"]["secret"], clock)
    answer = client.post("/api/me/passkeys/options", json={"step_up_token": token}, headers=HEADERS)
    assert answer.status_code == 409
    assert answer.json()["detail"].startswith("Passkeys are not set up on this server")
    assert _rows(client, WebAuthnChallenge) == []
    assert _rows(client, User, User.webauthn_user_handle.is_not(None)) == []


# --------------------------------------------------------------------------- #
# Listing, renaming, removing
# --------------------------------------------------------------------------- #


def test_each_member_lists_their_own_passkeys_and_which_work_here(client, passkey_world):
    for who in ("owner", "member"):
        person = passkey_world[who]
        listed = person["client"].get("/api/me/passkeys", headers=HEADERS).json()
        assert {one["id"] for one in listed} == {one["id"] for one in person["passkeys"]}
        by_id = {one["id"]: one for one in listed}
        synced, bound = (by_id[one["id"]] for one in person["passkeys"])
        assert (synced["label"], synced["synced"], synced["rp_id"], synced["usable_here"]) == (
            "iCloud Keychain", True, HERE, True,
        )
        assert (bound["label"], bound["synced"], bound["rp_id"], bound["usable_here"]) == (
            "Work laptop", False, OLD_HOST, False,
        )


def test_a_member_renames_their_own_passkey(client, passkey_world):
    target = passkey_world["owner"]["passkeys"][0]["id"]
    answer = client.patch(f"/api/me/passkeys/{target}", json={"label": "  Phone   in my pocket "}, headers=HEADERS)
    assert answer.status_code == 200, answer.text
    assert answer.json()["label"] == "Phone in my pocket"
    [row] = _rows(client, Passkey, Passkey.id == target)
    assert row.label == "Phone in my pocket"
    assert client.patch(f"/api/me/passkeys/{target}", json={"label": "   "}, headers=HEADERS).status_code == 422
    assert _rows(client, Passkey, Passkey.id == target)[0].label == "Phone in my pocket"


def test_a_member_removes_their_own_passkey_and_only_that_one(client, passkey_world):
    target = passkey_world["owner"]["passkeys"][0]["id"]
    assert client.delete(f"/api/me/passkeys/{target}", headers=HEADERS).status_code == 204
    left = {row.id for row in _rows(client, Passkey)}
    assert target not in left
    assert left == {
        passkey_world["owner"]["passkeys"][1]["id"],
        *(one["id"] for one in passkey_world["member"]["passkeys"]),
    }


def test_member_a_cannot_list_rename_or_remove_member_bs_passkey(client, passkey_world):
    theirs = passkey_world["member"]["passkeys"][0]["id"]
    [before] = _rows(client, Passkey, Passkey.id == theirs)

    listed = client.get("/api/me/passkeys", headers=HEADERS).json()
    assert theirs not in {one["id"] for one in listed}
    renamed = client.patch(f"/api/me/passkeys/{theirs}", json={"label": "mine now"}, headers=HEADERS)
    removed = client.delete(f"/api/me/passkeys/{theirs}", headers=HEADERS)
    unknown = client.delete("/api/me/passkeys/0123456789abcdef0123456789abcdef", headers=HEADERS)
    # 404, not 403, and the same 404 as an id that was never real.
    assert (renamed.status_code, removed.status_code) == (404, 404)
    assert renamed.json() == removed.json() == unknown.json() == {
        "detail": "no such passkey",
        "code": "passkey.not_found",
        "params": {},
    }

    [after] = _rows(client, Passkey, Passkey.id == theirs)
    assert (after.label, after.user_id) == (before.label, before.user_id) == ("iCloud Keychain", passkey_world["member"]["user"]["id"])


def test_history_records_a_passkey_and_undo_never_brings_one_back(client, passkey_world):
    """Audited, without the public key, and refused by undo like every other
    credential."""
    target = passkey_world["owner"]["passkeys"][0]["id"]
    assert client.delete(f"/api/me/passkeys/{target}", headers=HEADERS).status_code == 204
    changes = _rows(client, Change, Change.table_name == "passkeys", Change.row_id == target)
    assert [c.op for c in changes] == ["insert", "delete"]
    for change in changes:
        image = change.after or change.before
        assert image["label"] == "iCloud Keychain"
        assert not {"public_key", "sign_count", "last_used_at", "backed_up"} & set(image)

    from app.audit import undo

    removal = changes[-1].batch_id
    [row] = _rows(client, Batch, Batch.id == removal)
    with Session(client.app_module.db_engine) as own, pytest.raises(Exception, match="credential"):
        undo._refuse_if_credentials(own, row.id)
    assert _rows(client, Passkey, Passkey.id == target) == []


# --------------------------------------------------------------------------- #
# A host name that changed: the doctor, upgrade-check and restore say so
# --------------------------------------------------------------------------- #


def test_the_doctor_reports_passkeys_made_for_another_host(client, passkey_world, capsys, tmp_path):
    from scripts import doctor

    doctor.main(["--backups", str(tmp_path / "none")])
    warned = [line for line in capsys.readouterr().out.splitlines() if "passkeys" in line]
    assert warned == [
        f"  WARN  passkeys        2 passkeys registered for {OLD_HOST}, this instance is {HERE}; "
        "their members sign in with password + code and register again"
    ]


def test_the_doctor_is_quiet_when_every_passkey_belongs_here(client, clock, passkeys_on, capsys, tmp_path):
    from scripts import doctor

    people = _two_members(client)
    register(client, people["owner"]["secret"], clock, SoftAuthenticator())
    doctor.main(["--backups", str(tmp_path / "none")])
    lines = [line for line in capsys.readouterr().out.splitlines() if "passkeys" in line]
    assert lines == [f"  ok    passkeys        1 registered, all for {HERE}"]


def test_upgrade_check_names_passkeys_made_for_another_host(client, passkey_world, monkeypatch):
    from scripts import upgrade

    monkeypatch.setattr(upgrade, "published", lambda: (None, None))
    say = upgrade.Step()
    upgrade.report(say)
    said = [line.split("  ", 1)[1] for line in say.lines if "passkeys" in line]
    assert said == [f"    passkeys         2 passkeys registered for {OLD_HOST}, this instance is {HERE}"]


def test_with_passkeys_switched_off_every_one_is_reported(client, passkey_world, monkeypatch):
    import app.config as config
    from app.auth import passkeys

    monkeypatch.delenv("SPENDTRACKER_PUBLIC_URL")
    monkeypatch.setattr(config, "settings", config.Settings.from_env())
    assert passkeys.stranded(passkeys.hosts_in(_ledger()), config.settings.rp_id) == [
        f"2 passkeys registered for {OLD_HOST}, this instance is not set up for passkeys",
        f"2 passkeys registered for {HERE}, this instance is not set up for passkeys",
    ]


def test_a_ledger_older_than_passkeys_reports_none(tmp_path):
    from app.auth import passkeys

    older = tmp_path / "older.sqlite3"
    sqlite3.connect(older).close()
    assert passkeys.hosts_in(older) == {}
    assert ICLOUD_KEYCHAIN.hex() == "fbfc3007154e4ecc8c0b6e020557d7bd"
