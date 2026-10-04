"""Account reset links (#284).

What matters is what a reset does to the rows, so every test here reads them
back: the reset account's password hash, secret, recovery codes, sessions,
trusted browsers and agent keys -- and the same columns of the *other*
account, which a reset aimed at one must leave exactly as they were. Two
users, two households, a key in each.

The service is driven directly where it can be. The HTTP client is used only
for the contract -- the public routes, and the sign-in that has to refuse an
account whose authenticator was cleared.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pyotp
import pytest
from sqlalchemy import event, func, select

from app.audit.batch import batch
from app.auth import crypto, devices, passwords, sessions, tokens, totp
from app.errors import Conflict, NotFound, ValidationError
from app.models import (
    AccountReset,
    BatchKind,
    Change,
    RecoveryCode,
    TrustedDevice,
    User,
    WebSession,
    utcnow,
)
from app.services import account_resets as reset_service
from app.services import agent_keys
from tests.conftest import HEADERS, _setup_owner

OLD_PASSWORD = "the password before the reset"
NEW_PASSWORD = "the password after the reset"


# --------------------------------------------------------------------------- #
# The service, on the in-memory fixture
# --------------------------------------------------------------------------- #


def _count(session, model, user_id: str) -> int:
    return session.execute(
        select(func.count()).select_from(model).where(model.user_id == user_id)
    ).scalar_one()


@pytest.fixture()
def world(session, owner, member, household, other_household) -> dict:
    """Both users fully credentialled: a real password, a real sealed secret,
    two recovery codes, a session, a trusted browser and a live key each, in
    two different households."""
    secrets_ = {}
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        for user in (owner, member):
            secrets_[user.id] = totp.new_secret()
            user.password_hash = passwords.hash_password(OLD_PASSWORD)
            user.totp_secret = crypto.seal_totp_secret(secrets_[user.id], user_id=user.id)
            user.totp_last_counter = 1
            for code in ("aaaaa11111", "bbbbb22222"):
                session.add(RecoveryCode(user_id=user.id, code_hash=passwords.hash_password(code)))
    for user, house in ((owner, household), (member, other_household)):
        with batch(session, kind=BatchKind.admin, actor_id=user.id, household_id=house.id):
            agent_keys.issue(session, user=user, household=house, label="filer")
        sessions.issue(session, user)
        devices.issue(session, user)
    session.commit()
    return {"owner": owner, "member": member, "secrets": secrets_}


def _snapshot(session, user: User) -> dict:
    session.refresh(user)
    return {
        "password_hash": user.password_hash,
        "totp_secret": user.totp_secret,
        "totp_last_counter": user.totp_last_counter,
        "codes": _count(session, RecoveryCode, user.id),
        "sessions": _count(session, WebSession, user.id),
        "devices": _count(session, TrustedDevice, user.id),
        "live_keys": sum(1 for key in agent_keys.for_user(session, user) if key.live()),
    }


def _issue(session, user, *, by, password=True, authenticator=True):
    actor = by.id if by is not None else user.id
    with batch(session, kind=BatchKind.admin, actor_id=actor):
        reset, token = reset_service.issue(
            session, user, password=password, authenticator=authenticator, by=by
        )
    session.commit()
    return reset, token


@pytest.mark.parametrize(("password", "authenticator"), [(True, True), (True, False), (False, True)])
def test_issuing_shuts_that_account_and_only_that_one(session, world, password, authenticator):
    owner, member = world["owner"], world["member"]
    owner_before = _snapshot(session, owner)
    member_before = _snapshot(session, member)
    assert member_before["sessions"] == member_before["devices"] == member_before["live_keys"] == 1

    reset, token = _issue(session, member, by=owner, password=password, authenticator=authenticator)

    after = _snapshot(session, member)
    # Whatever the switches, everything the old credentials bought has ended.
    assert (after["sessions"], after["devices"], after["live_keys"]) == (0, 0, 0)
    if password:
        assert member_before["password_hash"] is not None
        assert after["password_hash"] is None
        assert not passwords.verify_password(after["password_hash"], OLD_PASSWORD)
    else:
        assert after["password_hash"] == member_before["password_hash"]
    if authenticator:
        assert after["totp_secret"] is None and after["totp_last_counter"] is None
        assert after["codes"] == 0
    else:
        assert after["totp_secret"] == member_before["totp_secret"]
        assert after["codes"] == 2

    # The other account: not one column moved.
    assert _snapshot(session, owner) == owner_before

    row = session.get(AccountReset, reset.id)
    assert (row.user_id, row.created_by_id) == (member.id, owner.id)
    assert (row.password, row.authenticator) == (password, authenticator)
    assert row.token_hash == tokens.fingerprint(token) and token not in row.token_hash
    assert row.expires_at > utcnow() + timedelta(hours=reset_service.VALID_HOURS - 1)


def test_a_reset_from_the_server_names_nobody(session, world):
    reset, _ = _issue(session, world["member"], by=None, password=True, authenticator=False)
    assert session.get(AccountReset, reset.id).created_by_id is None


def test_a_reset_that_resets_nothing_and_one_from_a_member_are_refused(session, world):
    owner, member = world["owner"], world["member"]
    before = _snapshot(session, owner)
    with pytest.raises(ValidationError, match="choose what to reset"):
        _issue(session, owner, by=owner, password=False, authenticator=False)
    session.rollback()
    with pytest.raises(ValidationError, match="only an owner"):
        _issue(session, owner, by=member)
    session.rollback()
    assert _snapshot(session, owner) == before
    assert session.execute(select(func.count()).select_from(AccountReset)).scalar_one() == 0


def test_a_second_link_replaces_the_first_and_keeps_what_it_reset(session, world):
    """The first cleared the authenticator; a second asked only for the
    password must still enrol one, or it leads to an account with no way in."""
    owner, member = world["owner"], world["member"]
    first, first_token = _issue(session, member, by=owner, password=False, authenticator=True)
    second, second_token = _issue(session, member, by=owner, password=True, authenticator=False)

    rows = session.execute(select(AccountReset)).scalars().all()
    assert [row.id for row in rows] == [second.id]
    assert (rows[0].password, rows[0].authenticator) == (True, True)
    with pytest.raises(NotFound):
        reset_service.lookup(session, first_token)
    assert reset_service.lookup(session, second_token).id == second.id


def _gone(session, engine, reset, gone: str, *, by) -> None:
    """Take a link away the two ways that leave its account shut."""
    from app.auth import housekeeping

    if gone == "withdrawn":
        with batch(session, kind=BatchKind.admin, actor_id=by.id):
            reset_service.withdraw(session, reset, by=by)
        session.commit()
    else:
        later = utcnow() + timedelta(hours=reset_service.VALID_HOURS) + reset_service.RETENTION
        assert housekeeping._sweep_account_resets(engine, now=later + timedelta(days=1)) == 1
    assert session.execute(select(func.count()).select_from(AccountReset)).scalar_one() == 0


@pytest.mark.parametrize("gone", ["withdrawn", "swept"])
@pytest.mark.parametrize(
    ("first", "then"),
    [
        ({"password": True, "authenticator": False}, {"password": False, "authenticator": True}),
        ({"password": False, "authenticator": True}, {"password": True, "authenticator": False}),
    ],
    ids=["password-then-authenticator", "authenticator-then-password"],
)
def test_a_link_after_one_that_is_gone_still_resets_what_that_one_did(
    session, engine, world, clock, gone, first, then
):
    """With the first link withdrawn or swept there is no row left to say what
    it reset -- only the account, whose password or secret is still NULL. A
    second link for the other credential alone used to set that one and
    leave the account with no way in: a password nobody knows, or no
    authenticator and no recovery codes."""
    owner, member = world["owner"], world["member"]
    owner_before = _snapshot(session, owner)
    reset, _ = _issue(session, member, by=owner, **first)
    _gone(session, engine, reset, gone, by=owner)

    second, token = _issue(session, member, by=owner, **then)
    assert (second.password, second.authenticator) == (True, True)

    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(int(time.time()))
    for missing, only in (
        ("choose a new password", {"new_password": None, "totp_secret": secret, "totp_code": code}),
        ("enrol a new authenticator first", {"new_password": NEW_PASSWORD, "totp_secret": None, "totp_code": None}),
    ):
        with pytest.raises(ValidationError, match=missing), batch(session, kind=BatchKind.admin, actor_id=member.id):
            reset_service.redeem(session, reset_service.lookup(session, token), **only)
        session.rollback()

    with batch(session, kind=BatchKind.admin, actor_id=member.id):
        codes = reset_service.redeem(
            session, reset_service.lookup(session, token), new_password=NEW_PASSWORD, totp_secret=secret, totp_code=code
        )
    session.commit()
    after = _snapshot(session, member)
    assert passwords.verify_password(after["password_hash"], NEW_PASSWORD)
    assert crypto.open_totp_secret(after["totp_secret"], user_id=member.id) == secret
    assert len(codes) == 10 and after["codes"] == 10
    assert _snapshot(session, owner) == owner_before


def test_redeeming_both_sets_them_and_spends_the_link(session, world, clock):
    owner, member = world["owner"], world["member"]
    owner_before = _snapshot(session, owner)
    reset, token = _issue(session, member, by=owner)
    new_secret = totp.new_secret()
    now = int(time.time())

    with batch(session, kind=BatchKind.admin, actor_id=member.id):
        codes = reset_service.redeem(
            session,
            reset_service.lookup(session, token),
            new_password=NEW_PASSWORD,
            totp_secret=new_secret,
            totp_code=pyotp.TOTP(new_secret).at(now),
        )
    session.commit()

    session.refresh(member)
    assert passwords.verify_password(member.password_hash, NEW_PASSWORD)
    assert crypto.open_totp_secret(member.totp_secret, user_id=member.id) == new_secret
    # The code that proved the pairing is spent.
    assert member.totp_last_counter == now // totp.STEP_SECONDS
    assert not totp.verify_and_consume(member, pyotp.TOTP(new_secret).at(now))
    assert len(codes) == 10 and len(set(codes)) == 10
    stored = session.execute(select(RecoveryCode).where(RecoveryCode.user_id == member.id)).scalars().all()
    assert len(stored) == 10
    assert any(passwords.verify_password(row.code_hash, codes[0]) for row in stored)
    assert session.get(AccountReset, reset.id) is None
    with pytest.raises(NotFound):
        reset_service.lookup(session, token)
    assert _snapshot(session, owner) == owner_before


def test_redeeming_a_password_only_link_leaves_the_authenticator_and_its_codes(session, world):
    member = world["member"]
    reset, _ = _issue(session, member, by=world["owner"], password=True, authenticator=False)
    secret_before = _snapshot(session, member)["totp_secret"]

    with batch(session, kind=BatchKind.admin, actor_id=member.id):
        codes = reset_service.redeem(
            session, reset, new_password=NEW_PASSWORD, totp_secret=None, totp_code=None
        )
    session.commit()

    after = _snapshot(session, member)
    assert codes == []
    assert passwords.verify_password(after["password_hash"], NEW_PASSWORD)
    assert after["totp_secret"] == secret_before and after["codes"] == 2
    assert session.get(AccountReset, reset.id) is None


@pytest.mark.parametrize(
    ("new_password", "code", "complaint"),
    [
        ("short", "right", "at least 12 characters"),
        (None, "right", "choose a new password"),
        (NEW_PASSWORD, "000000", "that code is not right"),
    ],
)
def test_a_refused_redemption_changes_nothing(session, world, new_password, code, complaint):
    member = world["member"]
    reset, token = _issue(session, member, by=world["owner"])
    before = _snapshot(session, member)
    secret = totp.new_secret()
    if code == "right":
        code = pyotp.TOTP(secret).at(int(time.time()))

    with (
        pytest.raises(ValidationError, match=complaint),
        batch(session, kind=BatchKind.admin, actor_id=member.id),
    ):
        reset_service.redeem(session, reset, new_password=new_password, totp_secret=secret, totp_code=code)
    session.rollback()

    assert _snapshot(session, member) == before
    assert reset_service.lookup(session, token).id == reset.id


def test_no_code_is_right_for_an_account_whose_authenticator_was_cleared(session, world):
    """`check_code` refuses such an account before it gets here; this is the
    line behind it, for any other caller. The code its old authenticator
    shows now is refused and burns nothing, while the other account's own
    code at the same instant is still right."""
    owner, member = world["owner"], world["member"]
    _issue(session, member, by=owner, password=False, authenticator=True)
    session.refresh(member)
    now = int(time.time())

    assert totp.verify_and_consume(member, pyotp.TOTP(world["secrets"][member.id]).at(now)) is False
    assert member.totp_last_counter is None
    assert totp.verify_and_consume(owner, pyotp.TOTP(world["secrets"][owner.id]).at(now)) is True
    assert owner.totp_last_counter == now // totp.STEP_SECONDS


def test_withdrawing_deletes_the_link_and_leaves_the_account_shut(session, world):
    owner, member = world["owner"], world["member"]
    reset, token = _issue(session, member, by=owner)
    shut = _snapshot(session, member)

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        reset_service.withdraw(session, reset, by=owner)
    session.commit()

    assert reset_service.pending(session) == []
    with pytest.raises(NotFound):
        reset_service.lookup(session, token)
    assert _snapshot(session, member) == shut


def test_an_expired_link_is_not_found_but_still_pending(session, world):
    member = world["member"]
    reset, token = _issue(session, member, by=world["owner"])
    with batch(session, kind=BatchKind.admin, actor_id=member.id):
        reset.expires_at = utcnow() - timedelta(minutes=1)
    session.commit()

    with pytest.raises(NotFound, match="not valid"):
        reset_service.lookup(session, token)
    assert [row.id for row in reset_service.pending(session)] == [reset.id]


def test_the_audit_log_never_carries_the_token_hash(session, world):
    owner, member = world["owner"], world["member"]
    reset, token = _issue(session, member, by=owner)
    digest = tokens.fingerprint(token)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        reset_service.withdraw(session, reset, by=owner)
    session.commit()

    logged = session.execute(select(Change).where(Change.table_name == "account_resets")).scalars().all()
    assert sorted(c.op for c in logged) == ["delete", "insert"]
    for change in logged:
        assert "token_hash" in (change.redacted or [])
        assert digest not in f"{change.before}{change.after}"
    # And the user's own change row does not carry the password hash or secret.
    user_changes = (
        session.execute(select(Change).where(Change.table_name == "users", Change.row_id == member.id))
        .scalars()
        .all()
    )
    assert user_changes, "the reset of the password was not logged at all"
    for change in user_changes:
        assert "password_hash" not in (change.after or {})
        assert "totp_secret" not in (change.after or {})


def test_the_access_log_does_not_keep_a_reset_token():
    from app.logging_setup import RedactRequestLines

    token = "Zq9xTokenThatResetsAnAccount-_4f"
    clean = RedactRequestLines()._clean
    for path in (f"/reset/{token}", f"/api/reset/{token}", f"/api/reset/{token}/authenticator"):
        line = clean(f'"POST {path}?x=1 HTTP/1.1" 200')
        assert token not in line and "<token>" in line, line


# --------------------------------------------------------------------------- #
# The contract: the public routes, and the door that has to stay shut
# --------------------------------------------------------------------------- #

PARTNER = "partner@example.com"


def _partner(client) -> dict:
    """The wizard's owner, and a second member with a real authenticator and
    recovery codes of their own."""
    import app.db as db
    from app.models import Role

    world = _setup_owner(client)
    owner_id = world["user"]["id"]
    secret = totp.new_secret()
    with db.session_scope() as session:
        with batch(session, kind=BatchKind.admin, actor_id=owner_id):
            partner = User(
                email=PARTNER,
                email_canonical=PARTNER,
                display_name="Partner",
                password_hash=passwords.hash_password(OLD_PASSWORD),
                role=Role.member,
                totp_secret=b"",
            )
            session.add(partner)
            session.flush()
            partner.totp_secret = crypto.seal_totp_secret(secret, user_id=partner.id)
            for code in ("aaaaa11111", "bbbbb22222"):
                session.add(RecoveryCode(user_id=partner.id, code_hash=passwords.hash_password(code)))
        partner_id = partner.id
    return {**world, "owner_id": owner_id, "partner_id": partner_id, "partner_secret": secret}


def _issue_over_the_ledger(user_id: str, by_id: str | None, **switches) -> str:
    import app.db as db

    with db.session_scope() as session:
        user = session.get(User, user_id)
        by = session.get(User, by_id) if by_id else None
        with batch(session, kind=BatchKind.admin, actor_id=by_id or user_id):
            _, token = reset_service.issue(session, user, by=by, **switches)
    return token


def _fresh_client(client):
    """The same app with no cookies, so nobody is signed in."""
    client.cookies.clear()
    return client


def test_a_cleared_authenticator_is_refused_at_the_code_and_the_recovery_door(client):
    world = _partner(client)
    _issue_over_the_ledger(world["partner_id"], world["owner_id"], password=False, authenticator=True)
    _fresh_client(client)

    first = client.post("/api/session", json={"email": PARTNER, "password": OLD_PASSWORD}, headers=HEADERS)
    assert first.status_code == 200 and first.json()["needs_code"] is True, first.text
    assert first.json()["authenticated"] is False

    code = client.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(world["partner_secret"]).at(int(time.time())), "trust_this_browser": True},
        headers=HEADERS,
    )
    assert code.status_code == 401
    assert "reset link" in code.json()["detail"]

    client.post("/api/session", json={"email": PARTNER, "password": OLD_PASSWORD}, headers=HEADERS)
    recovery = client.post("/api/session/recovery", json={"code": "aaaaa11111"}, headers=HEADERS)
    assert recovery.status_code == 401 and "reset link" in recovery.json()["detail"]
    assert client.get("/api/me").status_code == 401


def test_a_trusted_browser_does_not_stand_in_for_an_authenticator_that_was_cleared(client):
    """A reset revokes every trusted browser; the sign-in also refuses to let
    one skip the code for an account with no authenticator, in case a
    revocation ever misses a row. One browser trusted for both members, one
    member's authenticator then cleared with that trust left in place: the
    other still skips the code, and this one is asked for a code it cannot
    give rather than handed a session on its password alone."""
    import app.db as db
    from tests.conftest import PASSWORD

    world = _partner(client)  # the wizard trusted this browser for the owner
    client.delete("/api/session", headers=HEADERS)
    client.post("/api/session", json={"email": PARTNER, "password": OLD_PASSWORD}, headers=HEADERS)
    trusting = client.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(world["partner_secret"]).at(int(time.time())), "trust_this_browser": True},
        headers=HEADERS,
    )
    assert trusting.status_code == 200, trusting.text
    client.delete("/api/session", headers=HEADERS)

    with db.session_scope() as session, batch(session, kind=BatchKind.admin, actor_id=world["owner_id"]):
        partner = session.get(User, world["partner_id"])
        partner.totp_secret = None
        partner.totp_last_counter = None
    assert {
        user_id: _count_over_the_ledger(TrustedDevice, user_id)
        for user_id in (world["owner_id"], world["partner_id"])
    } == {world["owner_id"]: 1, world["partner_id"]: 1}, "precondition: trusted for both"

    held_back = client.post("/api/session", json={"email": PARTNER, "password": OLD_PASSWORD}, headers=HEADERS)
    assert held_back.status_code == 200, held_back.text
    assert (held_back.json()["authenticated"], held_back.json()["needs_code"]) == (False, True)
    assert client.get("/api/me").status_code == 401
    assert _count_over_the_ledger(WebSession, world["partner_id"]) == 0

    waved_through = client.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD}, headers=HEADERS
    )
    assert (waved_through.json()["authenticated"], waved_through.json()["needs_code"]) == (True, False)
    assert client.get("/api/me").json()["id"] == world["owner_id"]


def _count_over_the_ledger(model, user_id: str) -> int:
    import app.db as db

    with db.session_scope() as session:
        return _count(session, model, user_id)


def test_a_reset_password_no_longer_signs_in(client):
    world = _partner(client)
    _issue_over_the_ledger(world["partner_id"], world["owner_id"], password=True, authenticator=False)
    _fresh_client(client)
    refused = client.post(
        "/api/session", json={"email": PARTNER, "password": OLD_PASSWORD}, headers=HEADERS
    )
    assert refused.status_code == 401


def test_following_the_link_end_to_end(client, clock):
    world = _partner(client)
    token = _issue_over_the_ledger(
        world["partner_id"], world["owner_id"], password=True, authenticator=True
    )
    _fresh_client(client)

    state = client.get(f"/api/reset/{token}")
    assert state.status_code == 200, state.text
    body = state.json()
    assert (body["email"], body["display_name"], body["reset_by"]) == (PARTNER, "Partner", "Jane")
    assert (body["password"], body["authenticator"]) == (True, True)

    offer = client.post(f"/api/reset/{token}/authenticator", headers=HEADERS).json()
    assert offer["otpauth_uri"].startswith("otpauth://") and offer["secret"] in offer["otpauth_uri"]

    done = client.post(
        f"/api/reset/{token}",
        json={
            "password": NEW_PASSWORD,
            "blob": offer["blob"],
            "code": pyotp.TOTP(offer["secret"]).at(int(time.time())),
        },
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    assert len(done.json()["recovery_codes"]) == 10
    # Nothing signed anybody in.
    assert client.get("/api/me").status_code == 401
    # Spent.
    assert client.get(f"/api/reset/{token}").status_code == 404

    # And the account opens with what the link set, and only that.
    clock(1)
    signed = client.post("/api/session", json={"email": PARTNER, "password": NEW_PASSWORD}, headers=HEADERS)
    assert signed.json()["needs_code"] is True, signed.text
    stale = client.post(
        "/api/session/code",
        json={
            "code": pyotp.TOTP(world["partner_secret"]).at(int(time.time())),
            "trust_this_browser": False,
        },
        headers=HEADERS,
    )
    assert stale.status_code == 401
    client.post("/api/session", json={"email": PARTNER, "password": NEW_PASSWORD}, headers=HEADERS)
    fresh = client.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(offer["secret"]).at(int(time.time())), "trust_this_browser": False},
        headers=HEADERS,
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["user"]["id"] == world["partner_id"]


def test_a_link_from_the_server_says_so(client):
    world = _partner(client)
    token = _issue_over_the_ledger(world["partner_id"], None, password=True, authenticator=False)
    _fresh_client(client)
    body = client.get(f"/api/reset/{token}").json()
    assert body["reset_by"] is None and (body["password"], body["authenticator"]) == (True, False)


def test_an_offer_from_one_link_does_not_complete_another(client):
    world = _partner(client)
    owner_token = _issue_over_the_ledger(
        world["owner_id"], world["owner_id"], password=False, authenticator=True
    )
    partner_token = _issue_over_the_ledger(
        world["partner_id"], world["owner_id"], password=False, authenticator=True
    )
    _fresh_client(client)
    offer = client.post(f"/api/reset/{owner_token}/authenticator", headers=HEADERS).json()

    refused = client.post(
        f"/api/reset/{partner_token}",
        json={"blob": offer["blob"], "code": pyotp.TOTP(offer["secret"]).at(int(time.time()))},
        headers=HEADERS,
    )
    assert refused.status_code == 422
    assert client.get(f"/api/reset/{partner_token}").status_code == 200


def test_an_unknown_or_expired_link_is_a_404_on_every_route(client):
    import app.db as db

    world = _partner(client)
    token = _issue_over_the_ledger(
        world["partner_id"], world["owner_id"], password=True, authenticator=True
    )
    with db.session_scope() as session:
        row = session.execute(select(AccountReset)).scalar_one()
        with batch(session, kind=BatchKind.admin, actor_id=world["owner_id"]):
            row.expires_at = utcnow() - timedelta(seconds=1)
    _fresh_client(client)

    for presented in (token, "not-a-real-token"):
        assert client.get(f"/api/reset/{presented}").status_code == 404
        assert client.post(f"/api/reset/{presented}/authenticator", headers=HEADERS).status_code == 404
        answer = client.post(f"/api/reset/{presented}", json={"password": NEW_PASSWORD}, headers=HEADERS)
        assert answer.status_code == 404
        assert answer.json()["detail"] == "that reset link is not valid"


# --------------------------------------------------------------------------- #
# Two requests at once: a link is spent by its DELETE's row count
# --------------------------------------------------------------------------- #

OTHER_PASSWORD = "somebody else's password, also long"


def _credentials(user_id: str) -> dict:
    """What a reset can change on one account, read back on a fresh session."""
    import app.db as db

    with db.session_scope() as session:
        user = session.get(User, user_id)
        codes = session.execute(select(RecoveryCode.code_hash).where(RecoveryCode.user_id == user_id))
        resets = session.execute(select(AccountReset.id).where(AccountReset.user_id == user_id))
        return {
            "password_hash": user.password_hash,
            "totp_secret": user.totp_secret,
            "code_hashes": sorted(codes.scalars()),
            "resets": list(resets.scalars()),
        }


def _failed_batches(actor_id: str) -> int:
    import app.db as db
    from app.models import Batch, BatchStatus

    with db.session_scope() as session:
        return session.execute(
            select(func.count())
            .select_from(Batch)
            .where(Batch.actor_id == actor_id, Batch.status == BatchStatus.failed)
        ).scalar_one()


def _now(secret: str) -> str:
    return pyotp.TOTP(secret).at(int(time.time()))


def test_a_link_followed_twice_at_once_is_spent_once(client):
    """Both requests look the link up before either writes -- `lookup` is a
    plain SELECT, so nothing stops them. The second used to overwrite the
    first's password, secret and codes with only a SAWarning to show for it;
    now its DELETE matches no row, and everything it wrote goes back."""
    import app.db as db

    world = _partner(client)
    partner_id, owner_id = world["partner_id"], world["owner_id"]
    token = _issue_over_the_ledger(partner_id, owner_id, password=True, authenticator=True)
    # The other account has a link of its own, which none of this may touch.
    _issue_over_the_ledger(owner_id, owner_id, password=False, authenticator=True)
    owner_before = _credentials(owner_id)
    secret, other_secret = totp.new_secret(), totp.new_secret()

    first, second = db.SessionLocal(), db.SessionLocal()
    try:
        mine = reset_service.lookup(first, token)
        theirs = reset_service.lookup(second, token)
        with batch(first, kind=BatchKind.admin, actor_id=partner_id):
            codes = reset_service.redeem(
                first, mine, new_password=NEW_PASSWORD, totp_secret=secret, totp_code=_now(secret)
            )
        with (
            pytest.raises(NotFound, match="that reset link is not valid"),
            batch(second, kind=BatchKind.admin, actor_id=partner_id),
        ):
            reset_service.redeem(
                second,
                theirs,
                new_password=OTHER_PASSWORD,
                totp_secret=other_secret,
                totp_code=_now(other_secret),
            )
    finally:
        first.close()
        second.close()

    after = _credentials(partner_id)
    assert passwords.verify_password(after["password_hash"], NEW_PASSWORD)
    assert not passwords.verify_password(after["password_hash"], OTHER_PASSWORD)
    assert crypto.open_totp_secret(after["totp_secret"], user_id=partner_id) == secret
    # Ten, all the first person's -- not twenty, half of them the loser's.
    assert len(after["code_hashes"]) == 10
    assert any(passwords.verify_password(stored, codes[0]) for stored in after["code_hashes"])
    assert after["resets"] == []
    assert _failed_batches(partner_id) == 1
    assert _credentials(owner_id) == owner_before


def test_two_posts_of_one_link_get_one_200_and_one_404(client, monkeypatch):
    """The same race over HTTP. The first request's lookup lets the second run
    from start to finish before it returns, which is the window two browsers
    hit by chance: both have read the link, neither has written."""
    world = _partner(client)
    partner_id = world["partner_id"]
    token = _issue_over_the_ledger(partner_id, world["owner_id"], password=True, authenticator=True)
    _fresh_client(client)
    offers = [client.post(f"/api/reset/{token}/authenticator", headers=HEADERS).json() for _ in range(2)]

    def completing(offer: dict, password: str) -> dict:
        return {"password": password, "blob": offer["blob"], "code": _now(offer["secret"])}

    real_lookup = reset_service.lookup
    overtaking: dict = {}

    def lookup_then_be_overtaken(session, presented):
        reset = real_lookup(session, presented)
        if not overtaking:
            overtaking["running"] = True
            overtaking["answer"] = client.post(
                f"/api/reset/{token}", json=completing(offers[1], OTHER_PASSWORD), headers=HEADERS
            )
        return reset

    monkeypatch.setattr(reset_service, "lookup", lookup_then_be_overtaken)
    overtaken = client.post(f"/api/reset/{token}", json=completing(offers[0], NEW_PASSWORD), headers=HEADERS)
    monkeypatch.setattr(reset_service, "lookup", real_lookup)

    winner = overtaking["answer"]
    assert winner.status_code == 200, winner.text
    assert overtaken.status_code == 404, overtaken.text
    assert overtaken.json()["detail"] == "that reset link is not valid"

    after = _credentials(partner_id)
    assert passwords.verify_password(after["password_hash"], OTHER_PASSWORD)
    assert not passwords.verify_password(after["password_hash"], NEW_PASSWORD)
    assert crypto.open_totp_secret(after["totp_secret"], user_id=partner_id) == offers[1]["secret"]
    shown = winner.json()["recovery_codes"]
    assert len(shown) == 10 and len(after["code_hashes"]) == 10
    assert any(passwords.verify_password(stored, shown[0]) for stored in after["code_hashes"])
    assert after["resets"] == []


def test_withdrawing_a_link_somebody_has_just_followed_is_refused(client):
    """The owner's screen read the link; before the withdrawal reached the
    ledger, somebody followed it. "Withdrawn" would be a lie about an account
    that is now open with whatever that somebody chose."""
    import app.db as db

    world = _partner(client)
    partner_id, owner_id = world["partner_id"], world["owner_id"]
    token = _issue_over_the_ledger(partner_id, owner_id, password=True, authenticator=False)
    owners_view, followers = db.SessionLocal(), db.SessionLocal()
    try:
        seen = reset_service.lookup(owners_view, token)
        followed = reset_service.lookup(followers, token)
        with batch(followers, kind=BatchKind.admin, actor_id=partner_id):
            reset_service.redeem(
                followers, followed, new_password=NEW_PASSWORD, totp_secret=None, totp_code=None
            )

        owner = owners_view.get(User, owner_id)
        with (
            pytest.raises(Conflict, match="used or replaced a moment ago"),
            batch(owners_view, kind=BatchKind.admin, actor_id=owner_id),
        ):
            reset_service.withdraw(owners_view, seen, by=owner)
    finally:
        owners_view.close()
        followers.close()

    after = _credentials(partner_id)
    assert passwords.verify_password(after["password_hash"], NEW_PASSWORD)
    assert after["resets"] == [] and len(after["code_hashes"]) == 2
    assert (_failed_batches(owner_id), _failed_batches(partner_id)) == (1, 0)


def test_replacing_a_link_somebody_has_just_followed_is_refused(client):
    """`issue` reads the pending link, then deletes it. Somebody follows it in
    between; the replacement must not go ahead as though nothing happened."""
    import app.db as db

    world = _partner(client)
    partner_id, owner_id = world["partner_id"], world["owner_id"]
    token = _issue_over_the_ledger(partner_id, owner_id, password=True, authenticator=True)
    secret = totp.new_secret()
    owners_view, followers = db.SessionLocal(), db.SessionLocal()
    try:
        followed = reset_service.lookup(followers, token)

        def followed_first(*_args) -> None:
            with batch(followers, kind=BatchKind.admin, actor_id=partner_id):
                reset_service.redeem(
                    followers,
                    followed,
                    new_password=NEW_PASSWORD,
                    totp_secret=secret,
                    totp_code=_now(secret),
                )

        owner = owners_view.get(User, owner_id)
        partner = owners_view.get(User, partner_id)
        with (
            pytest.raises(Conflict, match="used or replaced a moment ago"),
            batch(owners_view, kind=BatchKind.admin, actor_id=owner_id),
        ):
            # After `issue` has read the pending link, before it deletes it.
            event.listen(owners_view, "before_flush", followed_first, once=True)
            reset_service.issue(owners_view, partner, password=True, authenticator=False, by=owner)
    finally:
        owners_view.close()
        followers.close()

    after = _credentials(partner_id)
    assert passwords.verify_password(after["password_hash"], NEW_PASSWORD)
    assert crypto.open_totp_secret(after["totp_secret"], user_id=partner_id) == secret
    assert len(after["code_hashes"]) == 10
    # And no replacement link: the refusal took the whole issue with it.
    assert after["resets"] == []
    assert _failed_batches(owner_id) == 1
