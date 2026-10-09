"""The operator's tools for the ledger: which one, is it healthy, start again,
and a member who cannot get past the code step.

`deploy/TROUBLESHOOTING.md` is the page these back. Each test asserts what the
tool did to the files or the rows, not that it ran: a reset that exits 0 and
leaves the ledger where it was is the failure worth catching.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import secrets
import sqlite3
import time
from pathlib import Path

import pyotp
import pytest

from tests.conftest import _setup_owner

PARTNER = "partner@example.com"


@pytest.fixture(autouse=True)
def _one_key(monkeypatch, client):
    """Seal with the key on disk, as a real instance does (test_backup_download)."""
    from app import config
    from app.auth import crypto

    monkeypatch.setattr(crypto, "settings", config.settings)


def _another_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _ledger(client) -> Path:
    from app import config

    return Path(config.settings.database_url.split("///", 1)[-1])


def _two_members(client) -> dict:
    """The owner from the real wizard, and a second member with a real
    authenticator and recovery codes of their own -- two of everything, so a
    reset aimed at one is seen to leave the other alone."""
    import app.db as db
    from app.audit.batch import batch
    from app.auth import crypto, passwords, sessions, totp
    from app.models import BatchKind, RecoveryCode, Role, User

    world = _setup_owner(client)
    owner_id = world["user"]["id"]
    partner_secret = totp.new_secret()
    with db.session_scope() as session:
        with batch(session, kind=BatchKind.admin, actor_id=owner_id):
            partner = User(
                email=PARTNER,
                email_canonical=PARTNER,
                display_name="Partner",
                password_hash=passwords.hash_password("another long password"),
                role=Role.member,
                totp_secret=b"",
            )
            session.add(partner)
            session.flush()
            partner.totp_secret = crypto.seal_totp_secret(partner_secret, user_id=partner.id)
            for code in ("aaaaa11111", "bbbbb22222"):
                session.add(RecoveryCode(user_id=partner.id, code_hash=passwords.hash_password(code)))
        owner = session.get(User, owner_id)
        sessions.issue(session, owner)
        sessions.issue(session, partner)
        partner_id = partner.id
    return {**world, "owner_id": owner_id, "partner_id": partner_id, "partner_secret": partner_secret}


def _rows(client, sql: str, *args) -> list[tuple]:
    with sqlite3.connect(_ledger(client)) as conn:
        return conn.execute(sql, args).fetchall()


# --------------------------------------------------------------------------- #
# Does this key open this ledger?
# --------------------------------------------------------------------------- #


def test_the_key_check_names_exactly_the_members_a_key_cannot_open(client):
    from app import config
    from app.auth import keycheck

    _two_members(client)
    right = keycheck.check(_ledger(client), config.settings.secret_key)
    assert (right.enrolled, right.refused) == (2, ())

    wrong = keycheck.check(_ledger(client), _another_key())
    assert wrong.opened == 0
    assert set(wrong.refused) == {"Jane.Doe@gmail.com", PARTNER}


def test_the_boot_says_which_ledger_and_warns_when_its_key_does_not_open_it(client, monkeypatch):
    from app import config

    main = client.app_module
    _two_members(client)
    heard: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: heard.append(record.getMessage())
    logging.getLogger("spendtracker").addHandler(handler)
    try:
        main.announce_ledger()
        assert any("0 household(s), 2 member(s)" in line for line in heard), heard
        assert not any("does not open" in line for line in heard)

        heard.clear()
        monkeypatch.setattr(
            main, "settings", dataclasses.replace(config.settings, secret_key=_another_key())
        )
        main.announce_ledger()
    finally:
        logging.getLogger("spendtracker").removeHandler(handler)
    warned = [line for line in heard if "does not open" in line]
    assert len(warned) == 1 and "2 of the 2" in warned[0], heard
    # Where the operator hears it first, so it says what waiting costs: the
    # recovery code takes trust and keys the original key will not return.
    assert "has not re-enrolled" in warned[0]
    assert "agent keys" in warned[0] and "does not restore them" in warned[0]
    assert f"recovery mode: secret.key in {config.settings.data_dir} does not open" in warned[0]
    assert "SPENDTRACKER_SECRET_KEY" not in warned[0]


def test_a_key_from_the_environment_is_named_as_the_environment(client, monkeypatch, tmp_path):
    """`SPENDTRACKER_SECRET_KEY` wins over `secret.key`, which is then never
    read. The boot log used to blame "secret.key in <data dir>" all the same,
    so an operator copied the right key into the file and restarted, and
    recovery mode carried on. The settings now say where the key came from,
    and the warning names the variable and says the file is not read."""
    from app import config

    env_key, file_key = _another_key(), _another_key()
    data = tmp_path / "keyed-by-env"
    data.mkdir()
    (data / "secret.key").write_text(file_key)
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(data))
    monkeypatch.setenv("SPENDTRACKER_SECRET_KEY", env_key)
    from_env = config.Settings.from_env()
    assert (from_env.secret_key, from_env.secret_key_from_env) == (env_key, True)
    assert from_env.secret_key_source == "SPENDTRACKER_SECRET_KEY"
    assert (data / "secret.key").read_text() == file_key, "the file is neither read nor written"

    monkeypatch.delenv("SPENDTRACKER_SECRET_KEY")
    from_file = config.Settings.from_env()
    assert (from_file.secret_key, from_file.secret_key_from_env) == (file_key, False)
    assert from_file.secret_key_source == f"secret.key in {data.resolve()}"

    main = client.app_module
    _two_members(client)
    heard: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: heard.append(record.getMessage())
    logging.getLogger("spendtracker").addHandler(handler)
    try:
        monkeypatch.setattr(
            main,
            "settings",
            dataclasses.replace(config.settings, secret_key=env_key, secret_key_from_env=True),
        )
        main.announce_ledger()
    finally:
        logging.getLogger("spendtracker").removeHandler(handler)
    (warned,) = [line for line in heard if "does not open" in line]
    assert warned.startswith("recovery mode: SPENDTRACKER_SECRET_KEY does not open 2 of the 2")
    assert "setting SPENDTRACKER_SECRET_KEY back to the original key ends recovery mode" in warned
    assert f"secret.key in {config.settings.data_dir} is not read" in warned
    assert "putting the original key back" not in warned


# --------------------------------------------------------------------------- #
# A new authenticator, from the server
# --------------------------------------------------------------------------- #


def test_the_operator_reset_replaces_one_members_factor_and_nothing_of_the_others(client, capsys):
    from app.auth import crypto, passwords
    from scripts import reset_authenticator

    world = _two_members(client)
    owner_before = _rows(
        client, "SELECT totp_secret FROM users WHERE id = ?", world["owner_id"]
    )[0][0]
    owner_codes = _rows(
        client, "SELECT code_hash FROM recovery_codes WHERE user_id = ?", world["owner_id"]
    )
    owner_sessions = _rows(client, "SELECT count(*) FROM sessions WHERE user_id = ?", world["owner_id"])
    assert owner_sessions[0][0] >= 1

    def answer(_prompt: str) -> str:
        printed = capsys.readouterr().out
        secret = printed.split("type this key in by hand:\n  ", 1)[1].split()[0]
        answer.secret = secret
        return pyotp.TOTP(secret).at(time.time())

    assert reset_authenticator.main([PARTNER.upper()], ask=answer) == 0
    out = capsys.readouterr().out

    (sealed,) = _rows(client, "SELECT totp_secret FROM users WHERE id = ?", world["partner_id"])[0]
    assert crypto.open_totp_secret(sealed, user_id=world["partner_id"]) == answer.secret
    assert answer.secret != world["partner_secret"]

    # Ten new codes, the old two gone, and every printed one is a real one.
    printed = out.split("this terminal:\n", 1)[1].split()
    stored = [row[0] for row in _rows(
        client, "SELECT code_hash FROM recovery_codes WHERE user_id = ?", world["partner_id"]
    )]
    assert len(printed) == len(stored) == 10
    assert all(any(passwords.verify_password(h, code) for h in stored) for code in printed)
    assert not any(passwords.verify_password(h, "aaaaa11111") for h in stored)

    # Their session ended; the owner's did not, nor their factor, nor their codes.
    assert _rows(client, "SELECT count(*) FROM sessions WHERE user_id = ?", world["partner_id"]) == [(0,)]
    assert _rows(
        client, "SELECT count(*) FROM sessions WHERE user_id = ?", world["owner_id"]
    ) == owner_sessions
    assert _rows(client, "SELECT totp_secret FROM users WHERE id = ?", world["owner_id"])[0][0] == owner_before
    assert _rows(
        client, "SELECT code_hash FROM recovery_codes WHERE user_id = ?", world["owner_id"]
    ) == owner_codes

    # One audited act, attributed. The secret is a redacted column, so the user
    # row records no change -- as for re-enrolment from the screen -- and the
    # recovery codes are what the act leaves in the log.
    batches = _rows(
        client, "SELECT id, kind, source FROM batches WHERE actor_id = ? AND kind = 'admin'",
        world["partner_id"],
    )
    assert len(batches) == 1 and "scripts.reset_authenticator" in batches[0][2]
    changed = {row[0] for row in _rows(
        client, "SELECT table_name FROM changes WHERE batch_id = ?", batches[0][0]
    )}
    assert "recovery_codes" in changed


def test_a_wrong_code_three_times_changes_nothing(client):
    from scripts import reset_authenticator

    world = _two_members(client)
    before = _rows(client, "SELECT totp_secret, totp_last_counter FROM users ORDER BY id")
    codes = _rows(client, "SELECT count(*) FROM recovery_codes WHERE user_id = ?", world["partner_id"])

    assert reset_authenticator.main([PARTNER], ask=lambda _: "000000") == 1
    assert _rows(client, "SELECT totp_secret, totp_last_counter FROM users ORDER BY id") == before
    assert _rows(
        client, "SELECT count(*) FROM recovery_codes WHERE user_id = ?", world["partner_id"]
    ) == codes == [(2,)]


def test_an_address_nobody_signs_in_with_changes_nothing(client):
    from scripts import reset_authenticator

    _two_members(client)
    before = _rows(client, "SELECT count(*) FROM batches")
    assert reset_authenticator.main(["nobody@example.com"], ask=lambda _: "") == 2
    assert _rows(client, "SELECT count(*) FROM batches") == before


def test_after_the_key_is_replaced_the_reset_is_what_lets_a_member_back_in(client, monkeypatch):
    """The lost-key case end to end: a new key opens nobody, and resetting one
    member makes the key open exactly that member and still not the other."""
    from app import config
    from app.auth import crypto, keycheck
    from scripts import reset_authenticator

    _two_members(client)
    replaced = dataclasses.replace(config.settings, secret_key=_another_key())
    monkeypatch.setattr(crypto, "settings", replaced)
    assert keycheck.check(_ledger(client), replaced.secret_key).opened == 0

    seen: dict = {}

    def code_for_new_secret(_prompt: str) -> str:
        return pyotp.TOTP(seen["secret"]).at(time.time())

    from app.auth import totp

    real_new_secret = totp.new_secret

    def remembered() -> str:
        seen["secret"] = real_new_secret()
        return seen["secret"]

    monkeypatch.setattr(totp, "new_secret", remembered)
    assert reset_authenticator.main([PARTNER], ask=code_for_new_secret) == 0

    after = keycheck.check(_ledger(client), replaced.secret_key)
    assert (after.enrolled, after.refused) == (2, ("Jane.Doe@gmail.com",))


def test_after_the_key_is_replaced_a_recovery_code_lets_a_member_back_in_on_their_own(
    client, monkeypatch, clock
):
    """Recovery codes are hashed, not sealed: a new key leaves them working (#283).

    With no operator step, the member signs in with one code through the real
    sign-in, enrols a new authenticator on the profile screen with another, and
    the new key opens exactly them -- the other member stays refused, their
    sealed secret untouched, and each code used is spent.
    """
    from fastapi.testclient import TestClient

    from app import config
    from app.auth import crypto, keycheck
    from tests.conftest import HEADERS

    world = _two_members(client)
    owner_sealed = _rows(client, "SELECT totp_secret FROM users WHERE id = ?", world["owner_id"])
    replaced = dataclasses.replace(config.settings, secret_key=_another_key())
    monkeypatch.setattr(crypto, "settings", replaced)
    assert keycheck.check(_ledger(client), replaced.secret_key).opened == 0

    partner = TestClient(client.app_module.app, base_url="https://testserver")
    first = partner.post(
        "/api/session",
        json={"email": PARTNER, "password": "another long password"},
        headers=HEADERS,
    )
    assert first.status_code == 200 and first.json()["needs_code"], first.text
    back = partner.post("/api/session/recovery", json={"code": "aaaaa11111"}, headers=HEADERS)
    assert back.status_code == 200 and back.json()["authenticated"], back.text

    offer = partner.post(
        "/api/me/authenticator",
        json={"current_password": "another long password"},
        headers=HEADERS,
    ).json()
    done = partner.post(
        "/api/me/authenticator/confirm",
        json={
            "token": offer["token"],
            "code": pyotp.TOTP(offer["secret"]).at(clock()),
            # The old authenticator cannot be opened, so the second code proves it.
            "current_code": "bbbbb22222",
        },
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text

    after = keycheck.check(_ledger(client), replaced.secret_key)
    assert (after.enrolled, after.refused) == (2, ("Jane.Doe@gmail.com",))
    assert _rows(client, "SELECT totp_secret FROM users WHERE id = ?", world["owner_id"]) == owner_sealed
    assert _rows(
        client,
        "SELECT count(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL",
        world["partner_id"],
    ) == [(0,)]

    # The spent code does not open the door a second time.
    again = TestClient(client.app_module.app, base_url="https://testserver")
    again.post(
        "/api/session",
        json={"email": PARTNER, "password": "another long password"},
        headers=HEADERS,
    )
    refused = again.post("/api/session/recovery", json={"code": "aaaaa11111"}, headers=HEADERS)
    assert refused.status_code == 401, refused.text


# --------------------------------------------------------------------------- #
# The doctor
# --------------------------------------------------------------------------- #


def test_the_doctor_passes_a_healthy_ledger_and_names_it(client, capsys, tmp_path):
    from scripts import doctor

    _two_members(client)
    assert doctor.main(["--backups", str(tmp_path / "none")]) == 0
    out = capsys.readouterr().out
    assert "ok    schema" in out
    assert "2 member(s)" in out
    assert "ok    secret.key      opens all 2 authenticator(s)" in out
    assert f"{PARTNER} 2" in out


def test_the_doctor_fails_a_key_that_does_not_open_the_ledger_and_says_whose(
    client, capsys, monkeypatch, tmp_path
):
    from app import config
    from scripts import doctor

    _two_members(client)
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, secret_key=_another_key())
    )
    assert doctor.main(["--backups", str(tmp_path / "none")]) == 1
    failed = [line for line in capsys.readouterr().out.splitlines() if "FAIL" in line]
    assert len(failed) == 1
    assert "opens 0 of 2" in failed[0] and PARTNER in failed[0] and "Jane.Doe@gmail.com" in failed[0]


def test_the_doctor_warns_about_a_member_with_no_recovery_codes_left(client, capsys, tmp_path):
    from scripts import doctor

    world = _two_members(client)
    with sqlite3.connect(_ledger(client)) as conn:
        conn.execute(
            "UPDATE recovery_codes SET used_at = '2026-01-01' WHERE user_id = ?",
            (world["partner_id"],),
        )
    assert doctor.main(["--backups", str(tmp_path / "none")]) == 0
    warned = [line for line in capsys.readouterr().out.splitlines() if "WARN  recovery codes" in line]
    assert len(warned) == 1 and PARTNER in warned[0] and "Jane" not in warned[0]


# --------------------------------------------------------------------------- #
# Starting again
# --------------------------------------------------------------------------- #


def _stop_the_app(client) -> None:
    """Stop the app before moving its ledger, as `reset` tells an operator to.

    `port_is_busy` is stubbed below, so nothing else stops it. Left running,
    the app's housekeeping sweep -- started at boot on a worker thread, and
    running ANALYZE and a WAL checkpoint -- could still hold the ledger while
    `reset` backed it up and renamed it with its `-wal` and `-shm` -- the
    likeliest source of the disk I/O error this test hit now and then under
    load (#108), though no failure was ever caught with its traceback.
    Leaving the client's context runs the app's shutdown, which waits for
    that sweep to finish; the fixture's own exit afterwards does nothing.
    """
    import app.db as db

    client.__exit__(None, None, None)
    db.engine.dispose()


def test_reset_backs_up_then_moves_the_ledger_and_key_aside(client, monkeypatch, tmp_path):
    from scripts import backup, reset, upgrade

    _two_members(client)
    monkeypatch.setattr(upgrade, "port_is_busy", lambda _port: False)
    live = _ledger(client)
    key = live.parent / "secret.key"
    key_text = key.read_text()
    _stop_the_app(client)

    assert reset.main(["--into", str(tmp_path / "bk")], ask=lambda _: "reset") == 0

    assert not live.exists() and not key.exists()
    (aside,) = live.parent.glob(f"{live.name}.before-reset-*")
    (key_aside,) = live.parent.glob("secret.key.before-reset-*")
    assert key_aside.read_text() == key_text
    with sqlite3.connect(aside) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (2,)

    (taken,) = (tmp_path / "bk").iterdir()
    assert backup.verify(taken)["rows"]["users"] == 2


def test_reset_without_the_word_moves_nothing(client, monkeypatch, tmp_path):
    from scripts import reset, upgrade

    _two_members(client)
    monkeypatch.setattr(upgrade, "port_is_busy", lambda _port: False)
    _stop_the_app(client)

    assert reset.main(["--into", str(tmp_path / "bk")], ask=lambda _: "yes") == 1
    assert _rows(client, "SELECT count(*) FROM users") == [(2,)]
    assert not list(_ledger(client).parent.glob("*.before-reset-*"))


def test_reset_refuses_while_the_app_answers(client, monkeypatch, tmp_path):
    from scripts import reset, upgrade

    _two_members(client)
    monkeypatch.setattr(upgrade, "port_is_busy", lambda _port: True)
    monkeypatch.setenv("SPENDTRACKER_IN_CONTAINER", "0")
    with pytest.raises(SystemExit, match="Stop it"):
        reset.main(["--into", str(tmp_path / "bk")], ask=lambda _: "reset")
    assert _rows(client, "SELECT count(*) FROM users") == [(2,)]
    assert not (tmp_path / "bk").exists()



# --------------------------------------------------------------------------- #
# The break-glass block in TROUBLESHOOTING.md, run as written
# --------------------------------------------------------------------------- #


def _break_glass() -> tuple[str, str]:
    """The break-glass subsection of "Getting back into an account" in
    TROUBLESHOOTING.md (under part 3, an owner's reset): its prose, and the
    Python an operator pastes, taken out of the page itself so the test runs
    what the page says."""
    import re

    page = (Path(__file__).resolve().parent.parent / "deploy" / "TROUBLESHOOTING.md").read_text()
    part = page[page.index("#### When no owner can: break glass") : page.index("### 4.")]
    block = re.search(r"python - you@example\.com <<'EOF'\n(.*?)\nEOF\n", part, re.S)[1]
    prose = " ".join(re.sub(r"```.*?```", "", part, flags=re.S).replace("*", "").split())
    return prose, block


def _paste(block: str, email: str, monkeypatch, capsys) -> tuple[str, object]:
    """Run the block the way `python - <email>` does. Its output, and the
    argument it exited with, or None if it ran to the end."""
    import sys

    monkeypatch.setattr(sys, "argv", ["-", email])
    try:
        exec(compile(block, "TROUBLESHOOTING.md", "exec"), {"__name__": "__main__"})
    except SystemExit as stopped:
        return capsys.readouterr().out, stopped.code
    return capsys.readouterr().out, None


def test_the_break_glass_block_reads_a_secret_and_says_why_it_cannot_read_a_cleared_one(
    client, monkeypatch, capsys
):
    """A reset link clears the secret, and a member who cannot sign in is when
    an operator reaches for this block. It used to stop on a bare TypeError
    the page did not explain. The other member's secret still reads."""
    import app.db as db
    from app.audit.batch import batch
    from app.models import BatchKind, User
    from app.services import account_resets

    world = _two_members(client)
    with db.session_scope() as session:
        owner = session.get(User, world["owner_id"])
        with batch(session, kind=BatchKind.admin, actor_id=owner.id):
            account_resets.issue(session, owner, password=False, authenticator=True, by=None)
    prose, block = _break_glass()

    out, stopped = _paste(block, PARTNER, monkeypatch, capsys)
    assert stopped is None and out.startswith("otpauth://")
    assert f"secret={world['partner_secret']}" in out

    out, stopped = _paste(block, "Jane.Doe@gmail.com", monkeypatch, capsys)
    assert out == ""
    assert stopped == "there is no authenticator to read: a reset link cleared it"
    # And the words around the block say what that means and what to do.
    assert f'"{stopped}" means this member\'s account was reset' in prose
