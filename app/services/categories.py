"""Categories: what a transaction was for.

Classification, and only classification. There is no budget in this app and
there is not going to be one, so a category carries no assigned amount, no
target and no rollover. It answers "what was this?" so the register can be read
and, later, reported on.

The part worth attention is not the CRUD -- it is deciding a category *for* you,
which this module does in one of three ways depending on what the payee says.
Getting that wrong is quietly expensive: a wrong category on an import of two
hundred rows is two hundred corrections.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import Conflict, NotFound, ValidationError
from ..models import (
    Categorisation,
    Category,
    CategoryGroup,
    Payee,
    Transaction,
)

#: How many of a payee's recent transactions the history rule looks at. Three is
#: enough to outvote a one-off miscategorisation and short enough that
#: correcting a payee's category twice changes what happens next -- a longer
#: window would make the app feel like it was ignoring you.
HISTORY_DEPTH = 3

#: What a new household starts with. Taken from the previous build, minus
#: everything that existed to serve its budget: no "Credit Card Payments", no
#: "Internal", no "FX Difference", and no inflow group holding the money that
#: has not been assigned anywhere yet. Those are budget machinery, and a
#: category list that carries them is a budget with the arithmetic missing.
DEFAULT_TREE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Income", ("Salary", "Other Income")),
    ("Bills", ("Rent / Mortgage", "Electricity", "Water", "Internet", "Phone", "Insurance")),
    ("Everyday", ("Groceries", "Eating Out", "Transport", "Household", "Health")),
    ("Quality of Life", ("Travel", "Subscriptions", "Gifts", "Hobbies")),
    ("Non-Monthly", ("Car Maintenance", "Home Maintenance", "Annual Fees")),
)


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #


def seed_defaults(session: Session, household_id: str) -> list[Category]:
    """Give a new household something to categorise with.

    An empty category list makes the feature look broken on the first screen
    somebody opens: every picker is empty and there is nothing to learn from.
    These are a starting point and every one of them can be renamed or archived.
    """
    made: list[Category] = []
    for group_order, (group_name, names) in enumerate(DEFAULT_TREE):
        group = CategoryGroup(
            household_id=household_id, name=group_name, sort_order=group_order
        )
        session.add(group)
        session.flush()
        for order, name in enumerate(names):
            category = Category(
                household_id=household_id, group_id=group.id, name=name, sort_order=order
            )
            session.add(category)
            made.append(category)
    session.flush()
    return made


def list_groups(session: Session, household_id: str) -> list[CategoryGroup]:
    return list(
        session.execute(
            select(CategoryGroup)
            .where(CategoryGroup.household_id == household_id)
            .order_by(CategoryGroup.sort_order, CategoryGroup.name)
        ).scalars()
    )


def list_categories(
    session: Session, household_id: str, *, include_archived: bool = False
) -> list[Category]:
    stmt = select(Category).where(Category.household_id == household_id)
    if not include_archived:
        stmt = stmt.where(Category.archived.is_(False))
    return list(session.execute(stmt.order_by(Category.sort_order, Category.name)).scalars())


def get_for_household(session: Session, category_id: str, household_id: str) -> Category:
    category = session.execute(
        select(Category).where(
            Category.id == category_id, Category.household_id == household_id
        )
    ).scalar_one_or_none()
    if category is None:
        raise NotFound("no such category")
    return category


def create_group(session: Session, household_id: str, name: str) -> CategoryGroup:
    clean = name.strip()
    if not clean:
        raise ValidationError("a group needs a name")
    if _group_named(session, household_id, clean) is not None:
        raise Conflict(f"there is already a group called {clean!r}")

    highest = session.execute(
        select(func.coalesce(func.max(CategoryGroup.sort_order), -1)).where(
            CategoryGroup.household_id == household_id
        )
    ).scalar_one()
    group = CategoryGroup(household_id=household_id, name=clean, sort_order=highest + 1)
    session.add(group)
    session.flush()
    return group


def create_category(
    session: Session, household_id: str, *, group_id: str, name: str
) -> Category:
    clean = name.strip()
    if not clean:
        raise ValidationError("a category needs a name")

    group = session.execute(
        select(CategoryGroup).where(
            CategoryGroup.id == group_id, CategoryGroup.household_id == household_id
        )
    ).scalar_one_or_none()
    if group is None:
        raise NotFound("no such category group")
    if _category_named(session, household_id, clean) is not None:
        raise Conflict(f"there is already a category called {clean!r}")

    highest = session.execute(
        select(func.coalesce(func.max(Category.sort_order), -1)).where(
            Category.group_id == group_id
        )
    ).scalar_one()
    category = Category(
        household_id=household_id, group_id=group.id, name=clean, sort_order=highest + 1
    )
    session.add(category)
    session.flush()
    return category


def update_category(
    session: Session,
    category: Category,
    *,
    name: str | None = None,
    group_id: str | None = None,
    archived: bool | None = None,
    sort_order: int | None = None,
) -> Category:
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ValidationError("a category needs a name")
        clash = _category_named(session, category.household_id, clean)
        if clash is not None and clash.id != category.id:
            raise Conflict(f"there is already a category called {clean!r}")
        category.name = clean

    if group_id is not None:
        group = session.execute(
            select(CategoryGroup).where(
                CategoryGroup.id == group_id,
                CategoryGroup.household_id == category.household_id,
            )
        ).scalar_one_or_none()
        if group is None:
            raise NotFound("no such category group")
        category.group_id = group.id

    if archived is not None:
        category.archived = archived
    if sort_order is not None:
        category.sort_order = sort_order
    return category


def rename_group(session: Session, group: CategoryGroup, name: str) -> CategoryGroup:
    clean = name.strip()
    if not clean:
        raise ValidationError("a group needs a name")
    clash = _group_named(session, group.household_id, clean)
    if clash is not None and clash.id != group.id:
        raise Conflict(f"there is already a group called {clean!r}")
    group.name = clean
    return group


def usage(session: Session, category_id: str) -> int:
    """How many transactions point at this category."""
    return session.execute(
        select(func.count()).select_from(Transaction).where(
            Transaction.category_id == category_id
        )
    ).scalar_one()


def delete_category(session: Session, category: Category) -> None:
    """Only if nothing is using it. Otherwise archive.

    Deleting a category in use would blank the category on every row that had
    it, which is a silent rewrite of what those transactions said they were.
    Archiving keeps them readable and takes it out of the pickers, which is what
    "I do not use this any more" actually means.
    """
    count = usage(session, category.id)
    if count:
        raise Conflict(
            f"{count} transaction{'s' if count != 1 else ''} "
            f"{'are' if count != 1 else 'is'} categorised as {category.name!r}. "
            "Archive it instead, and they keep their category."
        )
    # `payees.default_category_id` is ON DELETE SET NULL, and nothing on
    # Category points back at payees, so the audit hook never saw the database
    # clear it: undo brought the category back and left every "always this
    # category" payee pointing at nothing (#97). Cleared here, through the
    # loaded rows and inside the caller's batch, each one is an ordinary
    # logged update that undo reverses.
    for payee in session.execute(
        select(Payee).where(
            Payee.household_id == category.household_id,
            Payee.default_category_id == category.id,
        )
    ).scalars():
        payee.default_category_id = None
    session.delete(category)


def delete_group(session: Session, group: CategoryGroup) -> None:
    """Only an empty group, and archived categories count as being in it.

    A hard delete, like every other: the row goes, and the batch the caller
    opened holds its before-image, so History's Undo is how it comes back
    (#184). No flag, no tombstone.

    Archived categories are counted because they still categorise rows; the
    screen hides them by default, which is why the refusal says so rather
    than leaving someone looking at a group that appears to be empty.
    """
    held = len(group.categories)
    if held:
        archived = sum(1 for one in group.categories if one.archived)
        hidden = f", {archived} of them archived" if archived else ""
        raise Conflict(
            "A group can only be deleted when there are no categories under it. "
            f"{group.name!r} still holds {held} categor{'ies' if held != 1 else 'y'}{hidden}. "
            "Move or delete them first."
        )
    session.delete(group)


def _group_named(session: Session, household_id: str, name: str) -> CategoryGroup | None:
    return session.execute(
        select(CategoryGroup).where(
            CategoryGroup.household_id == household_id,
            func.lower(CategoryGroup.name) == name.lower(),
        )
    ).scalar_one_or_none()


def _category_named(session: Session, household_id: str, name: str) -> Category | None:
    return session.execute(
        select(Category).where(
            Category.household_id == household_id,
            func.lower(Category.name) == name.lower(),
        )
    ).scalar_one_or_none()


def ensure(session: Session, household_id: str, name: str, *, group_name: str) -> Category:
    """The household's category by this name, made (and its group) if it has none.

    For categories the importer knows it needs -- *Interest income*, *Bank
    fees*, *Investments* -- rather than ones a person invented. Matched by name
    without regard to case, so a household that already made "Bank Fees" keeps
    using its own.
    """
    found = _category_named(session, household_id, name)
    if found is not None:
        return found
    group = _group_named(session, household_id, group_name)
    if group is None:
        group = CategoryGroup(household_id=household_id, name=group_name, sort_order=100)
        session.add(group)
        session.flush()
    category = Category(household_id=household_id, group_id=group.id, name=name, sort_order=100)
    session.add(category)
    session.flush()
    return category


# --------------------------------------------------------------------------- #
# Deciding a category for you
# --------------------------------------------------------------------------- #


def from_history(session: Session, payee_id: str, *, depth: int = HISTORY_DEPTH) -> Category | None:
    """What this payee's recent transactions were categorised as.

    The most common of the last few, ties going to the most recent. Two rules in
    one sentence, and both matter: *most common* is what makes one stray
    correction not redirect the payee, and *most recent wins a tie* is what
    makes a deliberate change take effect on the second one rather than never.

    Only categorised rows count toward the window. Counting the uncategorised
    ones would mean a payee whose last three rows are blank has "no history",
    when in fact it has plenty -- just further back.
    """
    recent = list(
        session.execute(
            select(Transaction.category_id)
            .where(
                Transaction.payee_id == payee_id,
                Transaction.category_id.is_not(None),
            )
            .order_by(Transaction.date.desc(), Transaction.created_at.desc())
            .limit(depth)
        ).scalars()
    )
    if not recent:
        return None

    counts = Counter(recent)
    best = max(counts.values())
    # `recent` is newest first, so the first tied id in it is the most recent.
    winner = next(one for one in recent if counts[one] == best)
    return session.get(Category, winner)


def decide(session: Session, payee: Payee | None) -> Category | None:
    """The category a new transaction for this payee should get.

    None is a real answer, not a failure: "do not categorise this payee" and
    "nothing to go on yet" both mean leave it blank and let a person choose.
    """
    if payee is None:
        return None
    if payee.categorisation is Categorisation.none:
        return None
    if payee.categorisation is Categorisation.fixed:
        category = payee.default_category
        # A fixed category that has been archived is no longer an answer.
        return None if category is None or category.archived else category

    category = from_history(session, payee.id)
    return None if category is not None and category.archived else category


def set_rule(
    session: Session,
    payee: Payee,
    *,
    mode: Categorisation | str,
    category_id: str | None = None,
) -> Payee:
    """How this payee should be categorised from now on."""
    chosen = Categorisation(mode)
    if chosen is Categorisation.fixed:
        if category_id:
            category = get_for_household(session, category_id, payee.household_id)
            if category.archived:
                raise ValidationError(f"{category.name!r} is archived, so it cannot be a default")
            payee.default_category_id = category.id
        elif payee.default_category_id is None:
            raise ValidationError("choose the category to always use")
        # No category given but one already stored: switching back to "always
        # this one" means the one you picked before. Demanding it again would
        # make trying history for a week cost you the choice.
    payee.categorisation = chosen
    return payee
