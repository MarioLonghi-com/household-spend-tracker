"""The endpoint an open page polls to notice changes made elsewhere (#369).

Two users and two households. Every test asserts the `seq` the endpoint
answers, or who it says made the change, or that the poll wrote nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.config import settings
from app.models import (
    AgentScope,
    Batch,
    BatchKind,
    Change,
    Household,
    HouseholdMember,
    Role,
    User,
    WebSession,
    utcnow,
)
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner


def _latest(client, house, since=None):
    params = {} if since is None else {"since": since}
    return client.get(
        f"/api/households/{house}/changes/latest", params=params, headers=HEADERS
    )


@pytest.fixture()
def world(client):
    made = _setup_owner(client)
    home = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()["id"]
    away = client.post("/api/households", json={"name": "Away"}, headers=HEADERS).json()["id"]
    engine = client.app_module.db_engine
    with Session(engine, expire_on_commit=False) as own:
        owner = own.get(User, made["user"]["id"])
        partner = User(
            email="partner@example.com", email_canonical="partner@example.com",
            display_name="Partner", password_hash="argon2-placeholder", role=Role.member,
            totp_secret=b"sealed-placeholder",
        )
        own.execute(text("PRAGMA defer_foreign_keys=ON"))
        with batch(own, kind=BatchKind.setup, actor_id=owner.id, own_transaction=False):
            own.add(partner)
        own.commit()
        with batch(own, kind=BatchKind.admin, actor_id=owner.id, household_id=home):
            own.add(HouseholdMember(
                household_id=home, user_id=partner.id, added_by_id=owner.id, added_at=utcnow()
            ))
        # A household the owner is not in at all.
        with batch(own, kind=BatchKind.admin, actor_id=partner.id) as making:
            theirs = Household(name="Partner's own", base_currency="GBP")
            own.add(theirs)
            own.flush()
            own.add(HouseholdMember(
                household_id=theirs.id, user_id=partner.id, added_by_id=partner.id,
                added_at=utcnow(),
            ))
            assert making.id
        own.commit()
    return {
        "client": client, "engine": engine, "home": home, "away": away, "theirs": theirs.id,
        "owner_id": owner.id, "partner_id": partner.id,
    }


def _as(world, actor_id, house, *, key_id=None):
    """A change in `house` by `actor_id`: a new account, written in a batch."""
    from app.models import Account, AccountType

    with Session(world["engine"]) as own:
        with batch(own, kind=BatchKind.manual, actor_id=actor_id, household_id=house) as made:
            own.add(Account(household_id=house, name=f"by {actor_id[:6]}",
                            type=AccountType.checking, currency="EUR"))
            if key_id:
                made.agent_key_id = key_id
        own.commit()


def test_a_change_moves_the_seq_and_says_who_made_it(world):
    client = world["client"]
    start = _latest(client, world["home"]).json()["seq"]
    assert start > 0, "making the household already wrote its categories"

    # The owner, in a browser: mine.
    client.post(
        f"/api/households/{world['home']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    )
    after_mine = _latest(client, world["home"], since=start).json()
    assert after_mine["seq"] > start
    assert (after_mine["mine"], after_mine["elsewhere"]) == (True, False)

    # The other member: elsewhere.
    _as(world, world["partner_id"], world["home"])
    after_theirs = _latest(client, world["home"], since=after_mine["seq"]).json()
    assert after_theirs["seq"] > after_mine["seq"]
    assert (after_theirs["mine"], after_theirs["elsewhere"]) == (False, True)

    # Nothing new: the same seq and neither flag.
    still = _latest(client, world["home"], since=after_theirs["seq"]).json()
    assert still == {"seq": after_theirs["seq"], "elsewhere": False, "mine": False}


def test_an_agent_acting_for_the_owner_counts_as_elsewhere(world):
    client = world["client"]
    with Session(world["engine"], expire_on_commit=False) as own:
        owner = own.get(User, world["owner_id"])
        house = own.get(Household, world["home"])
        with batch(own, kind=BatchKind.admin, actor_id=owner.id, household_id=house.id):
            key, _token = key_service.issue(
                own, user=owner, household=house, label="tidier", scope=AgentScope.write
            )
        own.commit()
    start = _latest(client, world["home"]).json()["seq"]

    _as(world, world["owner_id"], world["home"], key_id=key.id)

    answer = _latest(client, world["home"], since=start).json()
    assert answer["seq"] > start
    assert answer["elsewhere"] is True, "an agent's change is not this tab's"
    assert answer["mine"] is False


def test_a_change_in_another_household_never_moves_this_ones(world):
    client = world["client"]
    home = _latest(client, world["home"]).json()["seq"]

    _as(world, world["owner_id"], world["away"])
    _as(world, world["partner_id"], world["theirs"])

    assert _latest(client, world["home"]).json()["seq"] == home
    assert _latest(client, world["home"], since=home).json() == {
        "seq": home, "elsewhere": False, "mine": False,
    }
    assert _latest(client, world["away"]).json()["seq"] > home


def test_a_household_the_user_is_not_in_is_a_404(world):
    client = world["client"]
    refused = _latest(client, world["theirs"])
    invented = _latest(client, "0" * 32)
    assert refused.status_code == invented.status_code == 404
    assert "seq" not in refused.text


def test_the_poll_writes_nothing_and_keeps_no_session_alive(world):
    """No session touch, no batch, no change row: polling is not activity.

    The stored `last_seen_at` is put an hour back, well past the touch window,
    so any request that touches would move it -- which an ordinary page read
    is shown doing afterwards, so the assertion is about the poll and not
    about a quiet clock.
    """
    client, engine = world["client"], world["engine"]
    assert settings.session_touch_seconds < 3600

    def seen():
        with Session(engine) as own:
            return own.execute(
                select(WebSession.last_seen_at).where(WebSession.user_id == world["owner_id"])
            ).scalar_one()

    def counts():
        with Session(engine) as own:
            return (
                own.execute(select(func.count()).select_from(Batch)).scalar_one(),
                own.execute(select(func.count()).select_from(Change)).scalar_one(),
            )

    # Make the stored time old, so any touch at all would move it.
    with engine.begin() as conn:
        conn.execute(
            WebSession.__table__.update().values(last_seen_at=utcnow() - timedelta(hours=1))
        )
    before = seen()
    written = counts()

    for _ in range(5):
        assert _latest(client, world["home"], since=0).status_code == 200
    assert seen() == before, "a poll touched the session"
    assert counts() == written

    client.get(f"/api/households/{world['home']}/accounts", headers=HEADERS)
    assert seen() > before, "an ordinary read touches it, so the check above is real"


def test_the_poll_still_needs_a_signed_in_session(world):
    client = world["client"]
    client.cookies.clear()
    assert _latest(client, world["home"]).status_code == 401
