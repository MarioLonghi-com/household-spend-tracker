"""The credential an agent holds: issuing it, using it, and killing it.

Every test here asserts a row, a count or a sentence -- never that a call
returned. The one that matters most is `test_the_sweep_of_an_audited_table...`:
`agent_keys` is the first table in housekeeping that is in the audit log, and
the mistake available is to copy one of the five bulk deletes above it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.errors import Forbidden, NotFound, ValidationError
from app.models import AgentKey, AgentScope, BatchKind, Change, utcnow
from app.services import agent_keys as service
from app.services import households as household_service


@pytest.fixture()
def key(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        row, token = service.issue(
            session, user=owner, household=household, label="receipt filer",
            agent_name="Claude Desktop",
        )
    session.commit()
    return row, token


# --------------------------------------------------------------------------- #
# Issuing
# --------------------------------------------------------------------------- #


def test_the_token_is_shown_once_and_never_stored(session, owner, household, key):
    row, token = key

    assert token.startswith(service.PREFIX), "a leaked key has to be greppable"
    assert token[len(service.PREFIX):] not in (row.token_hash, "")
    assert row.token_hash != token
    # And the hash is what the lookup matches, so the value really is enough.
    assert service.lookup(session, token) is row


def test_issuing_lands_in_history_with_the_token_redacted(session, owner, household, key):
    row, _token = key

    changes = list(
        session.execute(select(Change).where(Change.table_name == "agent_keys")).scalars()
    )
    assert len(changes) == 1, "issuing a credential is an act, and belongs in History"
    written = changes[0].after
    assert written["label"] == "receipt filer"
    # The two redacted columns are omitted from the image entirely, rather than
    # masked -- a "***" placeholder is what an undo would write back.
    assert "token_hash" not in written
    assert "last_used_at" not in written


def test_the_default_key_is_read_only_and_cannot_commit(session, owner, household, key):
    row, _ = key
    assert row.scope is AgentScope.read
    assert row.may_commit is False
    assert row.scope.granted == ["read"]


def test_write_implies_read_without_storing_both(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        row, _ = service.issue(
            session, user=owner, household=household, label="importer",
            scope=AgentScope.write,
        )
    assert row.scope is AgentScope.write
    assert row.scope.granted == ["read", "write"], "the implication is ours to apply"
    assert row.scope.may_write is True


def test_a_read_only_key_that_may_commit_is_refused(session, owner, household):
    """A flag that reads as true and behaves as false is worse than no flag."""
    with (
        pytest.raises(ValidationError, match="nothing to commit"),
        batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id),
    ):
        service.issue(
            session, user=owner, household=household, label="confused",
            scope=AgentScope.read, may_commit=True,
        )


def test_a_key_cannot_be_issued_into_a_household_you_are_not_in(
    session, owner, other_household
):
    """404, not 403 -- the rule the whole app keeps, kept here too."""
    before = session.execute(select(func.count()).select_from(AgentKey)).scalar_one()
    with (
        pytest.raises(NotFound) as refused,
        batch(session, kind=BatchKind.admin, actor_id=owner.id),
    ):
        service.issue(
            session, user=owner, household=other_household, label="somebody else's",
        )
    assert "no such household" in str(refused.value)
    session.rollback()
    assert session.execute(select(func.count()).select_from(AgentKey)).scalar_one() == before


def test_a_key_must_say_what_it_is_for(session, owner, household):
    with (
        pytest.raises(ValidationError, match="label"),
        batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id),
    ):
        service.issue(session, user=owner, household=household, label="   ")


def test_a_key_cannot_outlive_a_year(session, owner, household):
    with (
        pytest.raises(ValidationError, match="365"),
        batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id),
    ):
        service.issue(
            session, user=owner, household=household, label="forever", days=366,
        )


def test_the_default_life_is_ninety_days_and_is_fixed_at_issue(session, owner, household, key):
    row, _ = key
    assert (row.expires_at - row.created_at).days == service.DEFAULT_DAYS


# --------------------------------------------------------------------------- #
# Looking one up
# --------------------------------------------------------------------------- #


def test_every_way_a_key_can_be_no_good_answers_the_same(session, owner, household, key):
    row, token = key

    assert service.lookup(session, None) is None
    assert service.lookup(session, "") is None
    assert service.lookup(session, "not even prefixed") is None
    assert service.lookup(session, service.PREFIX + "invented") is None
    assert service.lookup(session, token) is row


def test_a_revoked_key_stops_working_at_once(session, owner, household, key):
    row, token = key
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        service.revoke(session, row, by=owner)

    assert service.lookup(session, token) is None, "revocation is a row read, so it is instant"


def test_an_expired_key_stops_working(session, owner, household, key):
    row, token = key
    assert service.lookup(session, token, now=row.expires_at + timedelta(seconds=1)) is None


def test_a_disabled_members_keys_die_with_them(session, owner, household, key):
    """What `deps.current_user` already does for their sessions."""
    row, token = key
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        owner.disabled_at = utcnow()
    session.flush()

    assert service.lookup(session, token) is None


def test_an_evicted_members_keys_die_with_their_membership(session, owner, member, household):
    """Eviction has to close the programmatic door as well as the browser one.

    It used to close only the browser's: `deps.current_household` joins
    `household_members`, so a cookie lost access at once, while `current_agent`
    resolved the household straight off `key.household_id` and a removed member
    kept full read and write access for the rest of the key's life -- up to a
    year. The owner could not even revoke it: `revoke` refuses anybody but the
    key's issuer, who is the person just evicted.
    """
    with batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=household.id):
        row, token = service.issue(
            session, user=member, household=household, label="the evicted one"
        )
    session.commit()
    assert service.lookup(session, token) is row, "works while they are a member"

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        household_service.remove_member(
            session, household_id=household.id, user_id=member.id, removed_by=owner
        )
    session.commit()

    assert service.lookup(session, token) is None
    # And the row is still there to be swept on its own schedule -- the key
    # stops working because membership is checked, not because it was deleted.
    assert session.get(AgentKey, row.id) is not None


def test_a_key_for_a_household_the_owner_is_still_in_is_untouched(
    session, owner, member, household, other_household
):
    """The guard is keyed on the pair, not on "this person was evicted somewhere"."""
    with batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=household.id):
        _row, kept = service.issue(
            session, user=member, household=household, label="the one that stays"
        )
    session.commit()

    with batch(
        session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id
    ):
        _other, evicted = service.issue(
            session, user=member, household=other_household, label="the one that goes"
        )
    session.commit()

    # Thrown out of the second household only.
    with batch(
        session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id
    ):
        household_service.add_member(
            session, household_id=other_household.id, user_id=owner.id, added_by=owner
        )
        household_service.remove_member(
            session, household_id=other_household.id, user_id=member.id, removed_by=owner
        )
    session.commit()

    assert service.lookup(session, evicted) is None
    assert service.lookup(session, kept) is not None, "the other household is not affected"


def test_only_the_person_who_issued_a_key_can_revoke_it(session, owner, member, household, key):
    row, token = key
    with pytest.raises(Forbidden):
        service.revoke(session, row, by=member)
    assert service.lookup(session, token) is row, "and it still works"


# --------------------------------------------------------------------------- #
# Recording its use without opening a batch
# --------------------------------------------------------------------------- #


def test_touching_last_used_at_writes_no_change_row_and_needs_no_batch(
    session, owner, household, key
):
    """The mechanism the whole read path depends on.

    `last_used_at` is redacted, so `snapshot.images()` omits it from both
    images, so the hook sees `before == after` and writes nothing -- and a write
    that produces no change row demands no open batch. Without this, every GET
    an agent makes would have to open one.
    """
    row, _ = key
    before = session.execute(select(func.count()).select_from(Change)).scalar_one()

    service.touch(session, row)      # deliberately outside any batch
    session.flush()
    session.commit()

    assert row.last_used_at is not None, "it really did move"
    after = session.execute(select(func.count()).select_from(Change)).scalar_one()
    assert after == before, "and it left no trace in the log"


def test_touching_is_sparing_so_a_polling_agent_does_not_write_every_read(
    session, owner, household, key
):
    row, _ = key
    service.touch(session, row)
    first = row.last_used_at

    service.touch(session, row, now=first + timedelta(seconds=service.TOUCH_SECONDS - 1))
    assert row.last_used_at == first, "inside the window it must not write"

    service.touch(session, row, now=first + timedelta(seconds=service.TOUCH_SECONDS + 1))
    assert row.last_used_at > first
