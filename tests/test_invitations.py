"""Inviting someone, including another owner.

The property that matters: the owner never holds the new person's credentials.
They send a link; the invitee chooses their own password, enrols their own
authenticator and keeps their own recovery codes -- and an invited owner ends up
with exactly the guarantees the first owner has.
"""

from __future__ import annotations

import time

import pyotp
import pytest

from app.errors import NotFound, ValidationError
from app.models import Role
from app.services import invitations as invite_service

PASSWORD = "a sufficiently long password"
HEADERS = {"sec-fetch-site": "same-origin"}


def _invite(client, *, role="member", email=None, households=None, step_up_token=None) -> dict:
    response = client.post(
        "/api/admin/invitations",
        json={
            "role": role,
            "email": email,
            "household_ids": households or [],
            "step_up_token": step_up_token,
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    # The raw token is no longer sent beside the link -- a one-time credential
    # duplicated in a response body is one more copy to leak, and nothing read
    # it. Tests take it from the link, the way the person clicking it does.
    created["token"] = created["link"].rsplit("/", 1)[-1]
    return created


def _grant(client, secret: str, *, steps_ahead: int = 1) -> str:
    """Both factors, now. The wizard spent the current step's code, so this
    uses a later one inside the drift window."""
    answer = client.post(
        "/api/me/step-up",
        json={
            "password": PASSWORD,
            "code": pyotp.TOTP(secret).at(int(time.time()) + 30 * steps_ahead),
        },
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["token"]


def _accept(client, token: str, *, email: str, name: str) -> dict:
    started = client.post(
        "/api/invite/begin",
        json={"token": token, "email": email, "display_name": name, "password": PASSWORD},
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    body = started.json()

    code = pyotp.TOTP(body["secret"]).at(int(time.time()))
    enrolled = client.post(
        "/api/invite/enrol", json={"blob": body["blob"], "code": code}, headers=HEADERS
    )
    assert enrolled.status_code == 200, enrolled.text

    done = client.post(
        "/api/invite/complete",
        json={"blob": enrolled.json()["blob"], "codes_saved": True},
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    return {"user": done.json(), "secret": body["secret"]}


# --------------------------------------------------------------------------- #
# Service level
# --------------------------------------------------------------------------- #


def test_only_an_owner_can_invite(session, owner, member):
    from app.audit.batch import batch
    from app.models import BatchKind

    with batch(session, kind=BatchKind.admin, actor_id=member.id), pytest.raises(
        ValidationError, match="only an owner"
    ):
        invite_service.create(session, invited_by=member, role=Role.member)


def test_a_link_works_once(session, owner):
    from app.audit.batch import batch
    from app.models import BatchKind

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invitation, token = invite_service.create(session, invited_by=owner)

    assert invite_service.lookup(session, token).id == invitation.id

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invite_service.accept(session, invitation, owner)

    # Deliberately the same answer as an invented link: somebody holding
    # neither should not be able to tell them apart.
    with pytest.raises(NotFound, match="not valid"):
        invite_service.lookup(session, token)


def test_an_expired_link_says_so(session, owner):
    from datetime import timedelta

    from app.audit.batch import batch
    from app.models import BatchKind, utcnow

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invitation, token = invite_service.create(session, invited_by=owner)
        invitation.expires_at = utcnow() - timedelta(minutes=1)

    with pytest.raises(NotFound, match="not valid"):
        invite_service.lookup(session, token)


def test_a_made_up_link_and_a_revoked_one_look_the_same(session, owner):
    """Someone holding neither should not be able to tell them apart."""
    from app.audit.batch import batch
    from app.models import BatchKind

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invitation, token = invite_service.create(session, invited_by=owner)
        invite_service.revoke(session, invitation, by=owner)

    with pytest.raises(NotFound) as revoked:
        invite_service.lookup(session, token)
    with pytest.raises(NotFound) as invented:
        invite_service.lookup(session, "not-a-real-token")
    assert str(revoked.value) == str(invented.value)


def test_undoing_a_revocation_does_not_revive_the_link(session, owner, member):
    """`revoked_at` is captured, so replaying the revoke batch's before-image
    would write NULL back and the link would work again. Undo refuses any batch
    that touched a credential, for the owner and for anybody else."""
    from app.audit.batch import batch
    from app.audit.undo import undo_batch
    from app.errors import Conflict
    from app.models import BatchKind

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invitation, token = invite_service.create(session, invited_by=owner)
    session.commit()
    with batch(session, kind=BatchKind.admin, actor_id=owner.id) as revoking:
        invite_service.revoke(session, invitation, by=owner)
    session.commit()

    for actor in (owner, member):
        with pytest.raises(Conflict, match="credential"):
            undo_batch(session, revoking.id, actor_id=actor.id)
        session.rollback()

    session.expire_all()
    assert invitation.revoked_at is not None
    with pytest.raises(NotFound):
        invite_service.lookup(session, token)


def test_the_token_itself_is_never_stored(session, owner):
    from app.audit.batch import batch
    from app.models import BatchKind

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invitation, token = invite_service.create(session, invited_by=owner)
    assert token not in invitation.token_hash
    assert len(invitation.token_hash) == 64


def test_the_token_hash_never_reaches_the_audit_log(session, owner):
    from sqlalchemy import select

    from app.audit.batch import batch
    from app.models import BatchKind, Change

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invitation, _ = invite_service.create(session, invited_by=owner)

    rows = session.execute(
        select(Change).where(Change.table_name == "invitations")
    ).scalars().all()
    assert rows, "creating an invitation is an audited act"
    for change in rows:
        assert invitation.token_hash not in f"{change.before}{change.after}"
        assert "token_hash" in (change.redacted or [])


# --------------------------------------------------------------------------- #
# Over HTTP, end to end
# --------------------------------------------------------------------------- #


def test_an_owner_can_invite_another_owner(client):
    """The point of an invitation: administering the instance never requires
    holding somebody else's password."""
    from tests.test_api import _setup_owner

    world = _setup_owner(client)
    created = _invite(
        client,
        role="owner",
        email="partner@gmail.com",
        step_up_token=_grant(client, world["secret"]),
    )
    assert created["invitation"]["role"] == "owner"
    assert created["link"].startswith("https://testserver/invite/")

    state = client.get(f"/api/invite/{created['token']}").json()
    assert state["role"] == "owner"
    assert state["invited_by"] == "Jane"

    # A different browser: no session, no trusted device.
    client.cookies.clear()
    result = _accept(client, created["token"], email="partner@gmail.com", name="Partner")
    assert result["user"]["role"] == "owner"

    # They are signed in, and they are an owner: the admin screen answers.
    assert client.get("/api/admin/users").status_code == 200


def test_an_invited_member_is_not_an_owner(client):
    from tests.test_api import _setup_owner

    _setup_owner(client)
    created = _invite(client, role="member")
    client.cookies.clear()
    result = _accept(client, created["token"], email="someone@gmail.com", name="Someone")

    assert result["user"]["role"] == "member"
    refused = client.get("/api/admin/users")
    assert refused.status_code == 403
    assert "only the owner" in refused.json()["detail"]


def test_accepting_puts_them_in_the_households_they_were_invited_to(client):
    from tests.test_api import _setup_owner

    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    created = _invite(client, households=[house["id"]])

    client.cookies.clear()
    _accept(client, created["token"], email="partner@gmail.com", name="Partner")

    theirs = client.get("/api/households").json()
    assert [h["id"] for h in theirs] == [house["id"]]


def test_a_link_cannot_be_used_twice_over_http(client):
    from tests.test_api import _setup_owner

    _setup_owner(client)
    created = _invite(client)
    client.cookies.clear()
    _accept(client, created["token"], email="first@gmail.com", name="First")

    client.cookies.clear()
    again = client.get(f"/api/invite/{created['token']}")
    assert again.status_code == 404
    assert again.json()["detail"] == "that invitation link is not valid"


def test_an_invitee_must_still_enrol_an_authenticator(client):
    """The invited path must not be a weaker way in than the first owner's."""
    from tests.test_api import _setup_owner

    _setup_owner(client)
    created = _invite(client)
    client.cookies.clear()

    started = client.post(
        "/api/invite/begin",
        json={
            "token": created["token"],
            "email": "lazy@gmail.com",
            "display_name": "Lazy",
            "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()

    skipped = client.post(
        "/api/invite/complete", json={"blob": started["blob"], "codes_saved": True}, headers=HEADERS
    )
    assert skipped.status_code == 422
    assert "authenticator" in skipped.json()["detail"]


def test_two_people_cannot_take_the_same_email(client):
    from tests.test_api import _setup_owner

    _setup_owner(client)
    created = _invite(client)
    client.cookies.clear()

    started = client.post(
        "/api/invite/begin",
        json={
            "token": created["token"],
            # The owner's address, spelled differently -- it folds to the same one.
            "email": "jane.doe+other@gmail.com",
            "display_name": "Impostor",
            "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()
    code = pyotp.TOTP(started["secret"]).at(int(time.time()))
    enrolled = client.post(
        "/api/invite/enrol", json={"blob": started["blob"], "code": code}, headers=HEADERS
    ).json()
    refused = client.post(
        "/api/invite/complete", json={"blob": enrolled["blob"], "codes_saved": True}, headers=HEADERS
    )
    assert refused.status_code == 409
    assert "cannot be used for a new account here" in refused.json()["detail"]


def test_a_taken_address_spends_the_link_so_it_cannot_enumerate(client):
    """#211. The refusal has to say *something*, so a live link could be walked
    again and again to learn which addresses have accounts. The first such
    answer now spends it: one address per invitation, and the owner sees the
    invitation gone."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models import Batch, Invitation
    from tests.conftest import _setup_owner

    _setup_owner(client)
    owners_cookies = dict(client.cookies)
    created = _invite(client)
    other = _invite(client)  # a second, untouched invitation
    client.cookies.clear()

    def walk(email: str):
        started = client.post(
            "/api/invite/begin",
            json={
                "token": created["token"],
                "email": email,
                "display_name": "Prober",
                "password": PASSWORD,
            },
            headers=HEADERS,
        )
        if started.status_code != 200:
            return started
        body = started.json()
        enrolled = client.post(
            "/api/invite/enrol",
            json={"blob": body["blob"], "code": pyotp.TOTP(body["secret"]).at(int(time.time()))},
            headers=HEADERS,
        ).json()
        return client.post(
            "/api/invite/complete",
            json={"blob": enrolled["blob"], "codes_saved": True},
            headers=HEADERS,
        )

    taken = walk("jane.doe@gmail.com")
    assert taken.status_code == 409
    assert "now spent" in taken.json()["detail"]

    # The second walk, with an address nobody has, gets nowhere: the link is spent.
    again = walk("fresh@gmail.com")
    assert again.status_code == 404
    assert again.json()["detail"] == "that invitation link is not valid"

    with Session(client.app_module.db_engine) as own:
        spent = own.get(Invitation, created["invitation"]["id"])
        assert spent.revoked_at is not None and spent.accepted_at is None
        assert own.get(Invitation, other["invitation"]["id"]).revoked_at is None
        sources = own.execute(select(Batch.source)).scalars().all()
        assert sources.count({"invitation": "refused: the address already has an account"}) == 1

    # And it has left the owner's list of outstanding links; the other has not.
    client.cookies.clear()
    for name, value in owners_cookies.items():
        client.cookies.set(name, value)
    listed = [one["id"] for one in client.get("/api/admin/invitations").json()]
    assert listed == [other["invitation"]["id"]]


def test_finishing_without_ticking_the_recovery_codes_box_is_refused(client):
    """#211. `codes_saved` was sent and never read: only the page kept the promise."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.models import User
    from tests.conftest import _setup_owner

    _setup_owner(client)
    created = _invite(client)
    client.cookies.clear()
    started = client.post(
        "/api/invite/begin",
        json={
            "token": created["token"],
            "email": "partner@gmail.com",
            "display_name": "Partner",
            "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()
    enrolled = client.post(
        "/api/invite/enrol",
        json={"blob": started["blob"], "code": pyotp.TOTP(started["secret"]).at(int(time.time()))},
        headers=HEADERS,
    ).json()

    for body in ({"blob": enrolled["blob"], "codes_saved": False}, {"blob": enrolled["blob"]}):
        refused = client.post("/api/invite/complete", json=body, headers=HEADERS)
        assert refused.status_code == 422, refused.text
        assert "recovery codes" in refused.json()["detail"]
    with Session(client.app_module.db_engine) as own:
        assert own.execute(select(func.count()).select_from(User)).scalar_one() == 1

    # Ticked, the same blob finishes: the refusal spent nothing.
    done = client.post(
        "/api/invite/complete", json={"blob": enrolled["blob"], "codes_saved": True}, headers=HEADERS
    )
    assert done.status_code == 200, done.text


def test_a_member_cannot_invite_anyone(client):
    from tests.test_api import _setup_owner

    _setup_owner(client)
    created = _invite(client, role="member")
    client.cookies.clear()
    _accept(client, created["token"], email="member@gmail.com", name="Member")

    refused = client.post("/api/admin/invitations", json={"role": "owner"}, headers=HEADERS)
    assert refused.status_code == 403


def test_a_withdrawn_invitation_cannot_still_mint_an_owner(client):
    """The link was revoked while they were filling the form.

    An earlier version re-checked only `accepted_at` at the final step, so a
    withdrawn invitation still created the account -- and because the role
    travels on the invitation, sometimes a second instance administrator.
    """
    from tests.conftest import _setup_owner

    world = _setup_owner(client)
    created = _invite(client, role="owner", step_up_token=_grant(client, world["secret"]))
    invitation_id = created["invitation"]["id"]

    owners_cookies = dict(client.cookies)

    client.cookies.clear()
    started = client.post(
        "/api/invite/begin",
        json={
            "token": created["token"],
            "email": "sneaky@gmail.com",
            "display_name": "Sneaky",
            "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()
    code = pyotp.TOTP(started["secret"]).at(int(time.time()))
    enrolled = client.post(
        "/api/invite/enrol", json={"blob": started["blob"], "code": code}, headers=HEADERS
    ).json()

    # The owner withdraws it before the invitee reaches the last step.
    invitee_cookies = dict(client.cookies)
    client.cookies.clear()
    for name, value in owners_cookies.items():
        client.cookies.set(name, value)
    assert client.delete(f"/api/admin/invitations/{invitation_id}", headers=HEADERS).status_code == 204

    client.cookies.clear()
    for name, value in invitee_cookies.items():
        client.cookies.set(name, value)

    refused = client.post(
        "/api/invite/complete", json={"blob": enrolled["blob"], "codes_saved": True}, headers=HEADERS
    )
    assert refused.status_code == 404
    # Counted as the owner: signed out, this read only ever answered "sign in
    # first", a body of one key, and the assertion held whatever happened (#267).
    client.cookies.clear()
    for name, value in owners_cookies.items():
        client.cookies.set(name, value)
    people = client.get("/api/admin/users")
    assert people.status_code == 200, people.text
    assert [one["email"] for one in people.json()] == ["Jane.Doe@gmail.com"], "no second owner exists"


def test_an_expired_invitation_cannot_be_spent_at_the_last_step(client, monkeypatch):
    from tests.conftest import _setup_owner

    _setup_owner(client)
    created = _invite(client)
    client.cookies.clear()

    started = client.post(
        "/api/invite/begin",
        json={
            "token": created["token"],
            "email": "late@gmail.com",
            "display_name": "Late",
            "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()
    code = pyotp.TOTP(started["secret"]).at(int(time.time()))
    enrolled = client.post(
        "/api/invite/enrol", json={"blob": started["blob"], "code": code}, headers=HEADERS
    ).json()

    # Time passes.
    from datetime import timedelta

    import app.db as db
    from app.audit.batch import batch as open_batch
    from app.models import BatchKind, Invitation, User, utcnow

    with db.SessionLocal() as scratch:
        actor = scratch.execute(__import__("sqlalchemy").select(User)).scalars().first()
        with open_batch(scratch, kind=BatchKind.admin, actor_id=actor.id):
            row = scratch.get(Invitation, created["invitation"]["id"])
            row.expires_at = utcnow() - timedelta(minutes=1)

    refused = client.post(
        "/api/invite/complete", json={"blob": enrolled["blob"], "codes_saved": True}, headers=HEADERS
    )
    assert refused.status_code == 404


def test_presence_does_not_name_people_from_other_households(client):
    """It renders email addresses, and it was the one authenticated route with
    nothing scoping it at all."""
    from tests.conftest import _setup_owner

    _setup_owner(client)
    created = _invite(client, role="member")
    client.cookies.clear()
    _accept(client, created["token"], email="stranger@gmail.com", name="Stranger")

    # The stranger is in no household, so they share none with the owner --
    # and must see themselves and nobody else.
    online = client.get("/api/presence").json()["online"]
    assert [one["display_name"] for one in online] == ["Stranger"], (
        f"presence leaked people from other households: {[o['email'] for o in online]}"
    )


def test_a_rule_cannot_point_at_another_households_payee(client):
    from tests.conftest import _setup_owner

    _setup_owner(client)
    mine = client.post("/api/households", json={"name": "Mine"}, headers=HEADERS).json()
    theirs = client.post("/api/households", json={"name": "Theirs"}, headers=HEADERS).json()

    foreign = client.post(
        f"/api/households/{theirs['id']}/payees", json={"name": "Elsewhere"}, headers=HEADERS
    ).json()

    refused = client.post(
        f"/api/households/{mine['id']}/payee-rules",
        json={"match_type": "contains", "pattern": "X", "payee_id": foreign["id"]},
        headers=HEADERS,
    )
    assert refused.status_code == 404


# --------------------------------------------------------------------------- #
# Making an owner costs both factors (#205)
# --------------------------------------------------------------------------- #


def _count_invitations(client) -> int:
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.models import Invitation

    with Session(client.app_module.db_engine) as own:
        return own.execute(select(func.count()).select_from(Invitation)).scalar_one()


def _role_of(client, user_id: str) -> str:
    from sqlalchemy.orm import Session

    from app.models import User

    with Session(client.app_module.db_engine) as own:
        return own.get(User, user_id).role.value


def test_inviting_an_owner_needs_both_factors_and_not_just_the_cookie(client):
    """A lifted owner cookie could mint a second owner that outlives every
    reset the real owner can make to their own credentials."""
    from tests.conftest import _setup_owner

    world = _setup_owner(client)

    def ask(token):
        return client.post(
            "/api/admin/invitations",
            json={"role": "owner", "step_up_token": token},
            headers=HEADERS,
        )

    assert ask(None).status_code == 401
    assert ask("not-a-real-grant").status_code == 401
    assert _count_invitations(client) == 0, "nothing may be written without a fresh proof"

    token = _grant(client, world["secret"])
    made = ask(token)
    assert made.status_code == 201, made.text
    assert made.json()["invitation"]["role"] == "owner"
    assert _count_invitations(client) == 1

    again = ask(token)
    assert again.status_code == 401, "one grant, one owner"
    assert _count_invitations(client) == 1

    # A member-role link stays on the cookie: a member is contained by
    # household scoping, and cannot mint keys or invite.
    member = _invite(client, role="member")
    assert member["invitation"]["role"] == "member"
    assert _count_invitations(client) == 2


def test_promoting_to_owner_needs_both_factors_and_demoting_does_not(client):
    from tests.conftest import _setup_owner

    world = _setup_owner(client)
    owners_cookies = dict(client.cookies)
    created = _invite(client, role="member")
    client.cookies.clear()
    member = _accept(client, created["token"], email="partner@gmail.com", name="Partner")
    member_id = member["user"]["id"]
    client.cookies.clear()
    for name, value in owners_cookies.items():
        client.cookies.set(name, value)

    def promote(role, token=None):
        return client.post(
            f"/api/admin/users/{member_id}/role",
            json={"role": role, "step_up_token": token},
            headers=HEADERS,
        )

    assert promote("owner").status_code == 401
    assert promote("owner", "not-a-real-grant").status_code == 401
    assert _role_of(client, member_id) == "member"

    token = _grant(client, world["secret"])
    promoted = promote("owner", token)
    assert promoted.status_code == 200, promoted.text
    assert _role_of(client, member_id) == "owner"

    # Taking it away is the safe direction, and costs nothing extra.
    demoted = promote("member")
    assert demoted.status_code == 200, demoted.text
    assert _role_of(client, member_id) == "member"

    # The grant that paid for the first promotion is spent.
    assert promote("owner", token).status_code == 401
    assert _role_of(client, member_id) == "member"
