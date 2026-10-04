"""Counts behind the category and payee screens.

Two `GET`s, and nothing here opens a batch: a count is a question, not an act,
so there is nothing to audit and nothing to undo -- the same reasoning written
out at the top of `reporting.py`.

Scoping is `CurrentHousehold`, so a household the caller is not a member of
answers **404** rather than 403, like everything else. Neither route takes any
other parameter: both answer for the whole household, because the screens they
feed are management lists rather than reports over a window.

The response models live **here** rather than in `app/schemas.py`. They are read
by these two routes and by nothing else, and `schemas.py` is edited by
everything.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...services import stats as stats_service
from ..deps import CurrentHousehold, SessionDep

router = APIRouter(tags=["stats"])


class TallyOut(BaseModel):
    """One line of a breakdown.

    `key` is the id of the payee or category, and **null for the unset bucket**:
    the transactions with no payee, or with no category. `name` is filled in for
    those too ("No payee", "Uncategorised"), so a client never has to invent a
    label for a null.
    """

    key: str | None
    name: str
    transaction_count: int


class CategoryStatOut(BaseModel):
    category_id: str
    #: Transactions carrying this category.
    transaction_count: int
    #: Distinct payees among them. "No payee" is not one of them.
    payee_count: int
    #: The busiest few, biggest first. May include a null-keyed "No payee" line.
    payees: list[TallyOut]
    #: Real payees `payees` left out. Zero when the list is complete.
    more_payees: int


class CategoryStatsOut(BaseModel):
    """Only categories something is filed under.

    A category with no transactions is absent rather than zeroed: the caller has
    the category tree already, and a row of zeros would say nothing it does not
    know. The client reads a missing id as none of everything.
    """

    categories: list[CategoryStatOut]


class PayeeStatOut(BaseModel):
    payee_id: str
    #: Transactions naming this payee, whatever they were categorised as.
    transaction_count: int
    #: Distinct categories among them. "Uncategorised" is not one of them.
    category_count: int
    #: Every one of them, biggest first -- not truncated. May include a
    #: null-keyed "Uncategorised" line.
    categories: list[TallyOut]


class PayeeStatsOut(BaseModel):
    """Only payees with transactions, for the same reason as above."""

    payees: list[PayeeStatOut]


def _tallies(rows: list[stats_service.Tally]) -> list[TallyOut]:
    return [
        TallyOut(key=one.key, name=one.name, transaction_count=one.count) for one in rows
    ]


@router.get("/households/{household_id}/stats/categories", response_model=CategoryStatsOut)
def category_stats(household: CurrentHousehold, session: SessionDep) -> CategoryStatsOut:
    """What is behind each category: how many transactions, from how many payees.

    One grouped query for the whole household, not one per category. The
    category screen shows the figure on every row and the payee breakdown when a
    row is opened, and both come out of this single answer -- so opening a
    category costs no request at all.
    """
    found = stats_service.category_stats(session, household.id)
    return CategoryStatsOut(
        categories=[
            CategoryStatOut(
                category_id=stat.category_id,
                transaction_count=stat.transaction_count,
                payee_count=stat.payee_count,
                payees=_tallies(stat.payees),
                more_payees=stat.more_payees,
            )
            for stat in found.values()
        ]
    )


@router.get("/households/{household_id}/stats/payees", response_model=PayeeStatsOut)
def payee_stats(household: CurrentHousehold, session: SessionDep) -> PayeeStatsOut:
    """What is behind each payee: how many transactions, filed under what.

    One grouped query for the whole household. The payee categorisation screen
    shows the first few categories inline and the rest behind the row, so the
    list is whole rather than truncated here.
    """
    found = stats_service.payee_stats(session, household.id)
    return PayeeStatsOut(
        payees=[
            PayeeStatOut(
                payee_id=stat.payee_id,
                transaction_count=stat.transaction_count,
                category_count=stat.category_count,
                categories=_tallies(stat.categories),
            )
            for stat in found.values()
        ]
    )
