"""Currency codes are ISO 4217's, and a ledger already holding another keeps working (#110).

`[A-Z]{3}` was the whole check, so `GPB` opened an account in a currency that
does not exist. New codes are now checked against ISO 4217. A code the
household already holds -- from before this check -- is accepted again, so no
existing ledger is refused anything it could do yesterday. Two households and
two currencies, so "already holds" is seen to be per household.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app import currencies
from app.audit.batch import batch
from app.errors import ValidationError
from app.models import Account, AccountType, BatchKind
from app.services import accounts as account_service
from app.services import households as household_service


def test_both_lists_are_three_capital_letters_and_do_not_overlap():
    for code in currencies.ASSIGNED:
        assert re.fullmatch(r"[A-Z]{3}", code), code
    assert not currencies.CURRENT & currencies.WITHDRAWN
    # The ones this app's own money module knows how to format are all in it.
    from app.money import _EXPONENTS

    assert set(_EXPONENTS) <= currencies.CURRENT


@pytest.mark.parametrize("code", ["EUR", "GBP", "JPY", "SEK", "BRL", "XAU", "HRK", "DEM"])
def test_assigned_codes_are_accepted(code):
    currencies.check_new(code)


@pytest.mark.parametrize("code", ["GPB", "EUO", "ABC", "BTC"])
def test_a_code_iso_never_assigned_is_refused_with_a_code(code):
    with pytest.raises(ValidationError, match="is not an ISO 4217 currency code") as refused:
        currencies.check_new(code)
    assert refused.value.code == "currency.not_iso_4217"
    assert refused.value.params == {"code": code}


def _open(session, owner, household, currency):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        return account_service.create_account(
            session, household=household, name=f"Account in {currency}",
            type=AccountType.checking, currency=currency,
        )


def test_a_typo_opens_no_account(session, owner, household, accounts):
    before = session.execute(select(Account.id)).scalars().all()
    with pytest.raises(ValidationError, match="GPB is not an ISO 4217 currency code"):
        _open(session, owner, household, "gpb")
    session.rollback()
    assert session.execute(select(Account.id)).scalars().all() == before
    assert _open(session, owner, household, "sek").currency == "SEK"


def _grandfather(session, owner, row, field: str, code: str) -> None:
    """A code stored before the check existed: written past the service, as
    the model itself never checked it."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id,
               household_id=getattr(row, "household_id", None) or row.id):
        setattr(row, field, code)
    session.commit()


def test_a_code_the_household_already_holds_is_accepted_again(
    session, owner, household, other_household, member, accounts
):
    _grandfather(session, owner, accounts["pounds"], "currency", "XBT")

    again = _open(session, owner, household, "XBT")
    assert again.currency == "XBT"
    assert accounts["pounds"].currency == "XBT"

    # Per household: the other one never held it, so it is new there.
    with (
        pytest.raises(ValidationError, match="XBT is not an ISO 4217"),
        batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id),
    ):
        account_service.create_account(
            session, household=other_household, name="Elsewhere",
            type=AccountType.checking, currency="XBT",
        )


def test_a_household_keeps_its_old_base_currency_and_cannot_take_a_new_typo(
    session, owner, household, accounts
):
    _grandfather(session, owner, household, "base_currency", "XBT")

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        household_service.update_household(session, household, base_currency="XBT")
    assert household.base_currency == "XBT"

    with (
        pytest.raises(ValidationError, match="EUO is not an ISO 4217"),
        batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id),
    ):
        household_service.update_household(session, household, base_currency="EUO")
    session.rollback()
    session.refresh(household)
    assert household.base_currency == "XBT"

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        household_service.update_household(session, household, base_currency="GBP")
    assert household.base_currency == "GBP"


def test_a_new_household_needs_an_assigned_code(session, owner):
    with (
        pytest.raises(ValidationError, match="GPB is not an ISO 4217"),
        batch(session, kind=BatchKind.admin, actor_id=owner.id),
    ):
        household_service.create_household(
            session, name="Typo", creator=owner, base_currency="GPB"
        )
