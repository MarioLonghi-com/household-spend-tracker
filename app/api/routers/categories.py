"""Managing the category list, and the rule each payee follows.

Two halves that belong together: the categories a household has, and how a payee
picks one. The second is useless without the first, and the first earns nothing
without the second -- categorising three hundred imported rows by hand is how a
category list stops being maintained.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from ...audit.batch import batch
from ...models import BatchKind, Category, CategoryGroup, Payee, Transaction
from ...schemas import (
    CategoryCreate,
    CategoryGroupCreate,
    CategoryGroupOut,
    CategoryGroupUpdate,
    CategoryOut,
    CategoryUpdate,
    PayeeCategorisationOut,
    SeedCategories,
    SetPayeeCategorisation,
)
from ...services import categories as category_service
from ..deps import CurrentHousehold, CurrentUser, SessionDep, load_for

router = APIRouter(tags=["categories"])


def _usage_counts(session, household_id: str) -> dict[str, int]:
    """How many transactions each category carries, in one query.

    One query rather than one per category: the management screen shows the
    count on every row, and asking per row is the N+1 this codebase keeps
    finding in the previous build.
    """
    rows = session.execute(
        select(Transaction.category_id, func.count())
        .where(Transaction.household_id == household_id, Transaction.category_id.is_not(None))
        .group_by(Transaction.category_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def _category_out(category: Category, counts: dict[str, int]) -> CategoryOut:
    out = CategoryOut.model_validate(category)
    out.used_by = counts.get(category.id, 0)
    return out


@router.get("/households/{household_id}/categories", response_model=list[CategoryGroupOut])
def list_categories(
    household: CurrentHousehold, session: SessionDep, include_archived: bool = False
) -> list[CategoryGroupOut]:
    """The whole tree, groups first, each with its categories."""
    counts = _usage_counts(session, household.id)
    out: list[CategoryGroupOut] = []
    for group in category_service.list_groups(session, household.id):
        out.append(
            CategoryGroupOut(
                id=group.id,
                name=group.name,
                sort_order=group.sort_order,
                categories=[
                    _category_out(one, counts)
                    for one in group.categories
                    if include_archived or not one.archived
                ],
            )
        )
    return out


@router.post(
    "/households/{household_id}/categories/defaults",
    response_model=list[CategoryGroupOut],
    status_code=201,
)
def seed_defaults(
    body: SeedCategories, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> list[CategoryGroupOut]:
    """Add the starter tree. Only to a household that has none.

    Refused rather than merged into an existing list: "add the defaults" pressed
    twice should not quietly produce a second Groceries, and a household that
    has already made its own categories has already answered this question.
    """
    if category_service.list_groups(session, household.id):
        from ...errors import Conflict

        raise Conflict("this household already has categories")

    with batch(session, kind=BatchKind.admin, actor_id=user.id, household_id=household.id):
        category_service.seed_defaults(session, household.id, locale=body.locale)
    return list_categories(household, session)


@router.post(
    "/households/{household_id}/category-groups",
    response_model=CategoryGroupOut,
    status_code=201,
)
def create_group(
    body: CategoryGroupCreate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> CategoryGroupOut:
    with batch(session, kind=BatchKind.admin, actor_id=user.id, household_id=household.id):
        group = category_service.create_group(session, household.id, body.name)
    return CategoryGroupOut(
        id=group.id, name=group.name, sort_order=group.sort_order, categories=[]
    )


@router.patch("/category-groups/{group_id}", response_model=CategoryGroupOut)
def rename_group(
    group_id: str, body: CategoryGroupUpdate, session: SessionDep, user: CurrentUser
) -> CategoryGroupOut:
    group = load_for(session, user, CategoryGroup, group_id)
    counts = _usage_counts(session, group.household_id)
    with batch(
        session, kind=BatchKind.admin, actor_id=user.id, household_id=group.household_id
    ):
        category_service.rename_group(session, group, body.name)
    return CategoryGroupOut(
        id=group.id,
        name=group.name,
        sort_order=group.sort_order,
        categories=[_category_out(one, counts) for one in group.categories],
    )


@router.delete("/category-groups/{group_id}", status_code=204)
def delete_group(group_id: str, session: SessionDep, user: CurrentUser) -> None:
    group = load_for(session, user, CategoryGroup, group_id)
    with batch(
        session, kind=BatchKind.admin, actor_id=user.id, household_id=group.household_id
    ):
        category_service.delete_group(session, group)


@router.post(
    "/households/{household_id}/categories", response_model=CategoryOut, status_code=201
)
def create_category(
    body: CategoryCreate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> CategoryOut:
    with batch(session, kind=BatchKind.admin, actor_id=user.id, household_id=household.id):
        category = category_service.create_category(
            session, household.id, group_id=body.group_id, name=body.name
        )
    return _category_out(category, {})


@router.patch("/categories/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: str, body: CategoryUpdate, session: SessionDep, user: CurrentUser
) -> CategoryOut:
    category = load_for(session, user, Category, category_id)
    with batch(
        session, kind=BatchKind.admin, actor_id=user.id, household_id=category.household_id
    ):
        category_service.update_category(
            session,
            category,
            name=body.name,
            group_id=body.group_id,
            archived=body.archived,
            sort_order=body.sort_order,
        )
    return _category_out(category, _usage_counts(session, category.household_id))


@router.delete("/categories/{category_id}", status_code=204)
def delete_category(category_id: str, session: SessionDep, user: CurrentUser) -> None:
    """Only if nothing carries it. Otherwise the answer is archive."""
    category = load_for(session, user, Category, category_id)
    with batch(
        session, kind=BatchKind.admin, actor_id=user.id, household_id=category.household_id
    ):
        category_service.delete_category(session, category)


# --------------------------------------------------------------------------- #
# What a payee chooses
# --------------------------------------------------------------------------- #


def _rule_out(session, payee: Payee) -> PayeeCategorisationOut:
    chosen = category_service.decide(session, payee)
    depth = session.execute(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.payee_id == payee.id, Transaction.category_id.is_not(None))
    ).scalar_one()
    return PayeeCategorisationOut(
        payee_id=payee.id,
        payee_name=payee.name,
        categorisation=payee.categorisation,
        default_category_id=payee.default_category_id,
        current_default_id=chosen.id if chosen else None,
        current_default_name=chosen.full_name if chosen else None,
        history_window=category_service.HISTORY_DEPTH,
        history_available=min(depth, category_service.HISTORY_DEPTH),
    )


@router.get("/payees/{payee_id}/categorisation", response_model=PayeeCategorisationOut)
def get_rule(payee_id: str, session: SessionDep, user: CurrentUser) -> PayeeCategorisationOut:
    """How this payee categorises, and what it would choose right now."""
    payee = load_for(session, user, Payee, payee_id)
    return _rule_out(session, payee)


@router.put("/payees/{payee_id}/categorisation", response_model=PayeeCategorisationOut)
def set_rule(
    payee_id: str, body: SetPayeeCategorisation, session: SessionDep, user: CurrentUser
) -> PayeeCategorisationOut:
    payee = load_for(session, user, Payee, payee_id)
    with batch(
        session, kind=BatchKind.admin, actor_id=user.id, household_id=payee.household_id
    ):
        category_service.set_rule(
            session, payee, mode=body.categorisation, category_id=body.category_id
        )
    return _rule_out(session, payee)


@router.get("/payees/{payee_id}/suggested-category", response_model=CategoryOut | None)
def suggested(payee_id: str, session: SessionDep, user: CurrentUser) -> CategoryOut | None:
    """What a new transaction for this payee would be categorised as.

    Its own endpoint so quick entry can fill the category the moment a payee is
    chosen, rather than the person discovering it after saving.
    """
    payee = load_for(session, user, Payee, payee_id)
    chosen = category_service.decide(session, payee)
    if chosen is None:
        return None
    return _category_out(chosen, {})
