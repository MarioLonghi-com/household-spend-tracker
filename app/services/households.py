"""Households and who is in them.

Membership is binary: you are a member or you are not, and if you are you can do
everything inside. The only two things reserved to the owner are who is in the
household and deleting it -- both rare, both consequential, and both properly
answered for at the level of the instance rather than the ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import currencies, theming
from ..errors import Conflict, Forbidden, NotFound, ValidationError
from ..models import (
    Account,
    Batch,
    Category,
    CategoryGroup,
    Change,
    ChangeOp,
    ClearedState,
    Household,
    HouseholdMember,
    Payee,
    PayeeRule,
    Receipt,
    Reconciliation,
    Role,
    Transaction,
    User,
    utcnow,
)

#: "not given", so that clearing a field and leaving it alone are different
#: requests. Same sentinel idea as `services/transactions.py`.
UNSET = object()

#: Letters only. `Intl.NumberFormat` throws on a code like `€€€` or `12A`,
#: and the client renders money on every screen, so one stored code that is
#: three characters but not three letters blanked the whole household (#193).
CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def create_household(
    session: Session, *, name: str, creator: User, base_currency: str = "EUR", date_format: str = "YYYY-MM-DD"
) -> Household:
    """Create a household. The creator is its first member."""
    if not name.strip():
        raise ValidationError("a household needs a name")
    currency = (base_currency or "").strip().upper()
    if not CURRENCY_CODE.fullmatch(currency):
        raise ValidationError(f"{base_currency!r} is not a three-letter currency code")
    currencies.check_new(currency)

    # A new household opens in a colour nobody else is wearing. The whole point
    # of the palette is telling two ledgers apart, and a second household that
    # arrives looking exactly like the first fails at that on the day it is made.
    taken = list(session.execute(select(Household.theme)).scalars())
    household = Household(
        name=name.strip(),
        base_currency=currency,
        date_format=date_format,
        theme=theming.next_palette(taken),
    )
    session.add(household)
    session.flush()
    session.add(
        HouseholdMember(
            household_id=household.id, user_id=creator.id, added_by_id=creator.id, added_at=utcnow()
        )
    )
    session.flush()

    # A household starts with something to categorise with.
    #
    # `categories.seed_defaults` has existed since categories landed, is exposed
    # on a route, and was called by nothing that creates a household -- so every
    # real household began with an empty list, and its own docstring described
    # the consequence exactly: "an empty category list makes the feature look
    # broken on the first screen somebody opens: every picker is empty and there
    # is nothing to learn from". That is what the split panel's category select
    # was, and what the register's was: not a bug in either screen.
    #
    # Imported here rather than at module scope: `categories` reaches back into
    # this module's models and the pair would import each other at load time.
    from . import categories as category_service

    category_service.seed_defaults(session, household.id)
    session.flush()
    return household


def update_household(
    session: Session,
    household: Household,
    *,
    name: str | None = None,
    base_currency: str | None = None,
    date_format: str | None = None,
    note: str | None | object = UNSET,
    theme: str | None = None,
    accent: str | None | object = UNSET,
    receipts_keep_original: bool | None = None,
) -> Household:
    """Change a household's settings.

    The accent is checked before it is stored, not when it is rendered: a
    colour that cannot be made usable should fail where somebody is looking at
    the form, rather than every time the page loads afterwards.
    """
    if name is not None:
        if not name.strip():
            raise ValidationError("a household needs a name")
        household.name = name.strip()

    if base_currency is not None:
        currency = base_currency.strip().upper()
        if not CURRENCY_CODE.fullmatch(currency):
            raise ValidationError(f"{base_currency!r} is not a three-letter currency code")
        from .accounts import codes_in_use

        currencies.check_new(currency, codes_in_use(session, household))
        household.base_currency = currency

    if date_format is not None:
        household.date_format = date_format

    if note is not UNSET:
        household.note = (note or "").strip() or None

    if theme is not None:
        if theme not in theming.BY_KEY:
            raise ValidationError(f"{theme!r} is not one of the colour schemes")
        household.theme = theme

    if accent is not UNSET:
        if accent in (None, ""):
            household.accent = None
        else:
            # Raises if no shade of this hue can be made to work.
            household.accent = theming.plan_accent(accent, theming.get(household.theme)).chosen

    if receipts_keep_original is not None:
        # Nothing is deleted by turning this off. The originals already stored
        # stay stored -- the toggle decides what happens to the *next* upload,
        # and the screen says so with the count beside it. Issue #62.
        household.receipts_keep_original = receipts_keep_original

    return household


def get_for(session: Session, household_id: str, user: User) -> Household:
    """The household, if this user is in it.

    Raises ``NotFound`` for a household that does not exist **and** for one the
    user is not a member of -- deliberately the same answer, so a non-member
    cannot learn that an id is real.
    """
    household = session.execute(
        select(Household)
        .join(HouseholdMember, HouseholdMember.household_id == Household.id)
        .where(Household.id == household_id, HouseholdMember.user_id == user.id)
    ).scalar_one_or_none()
    if household is None:
        raise NotFound("no such household")
    return household


def list_for(session: Session, user: User) -> list[Household]:
    return list(
        session.execute(
            select(Household)
            .join(HouseholdMember, HouseholdMember.household_id == Household.id)
            .where(HouseholdMember.user_id == user.id)
            .order_by(Household.created_at)
        ).scalars()
    )


def members(session: Session, household_id: str) -> list[HouseholdMember]:
    return list(
        session.execute(
            select(HouseholdMember)
            .where(HouseholdMember.household_id == household_id)
            .order_by(HouseholdMember.added_at)
        ).scalars()
    )


def is_member(session: Session, household_id: str, user_id: str) -> bool:
    return session.execute(
        select(HouseholdMember.id).where(
            HouseholdMember.household_id == household_id, HouseholdMember.user_id == user_id
        )
    ).scalar_one_or_none() is not None


def add_member(session: Session, *, household_id: str, user_id: str, added_by: User) -> HouseholdMember:
    """Owner-only. Membership is the one thing a member cannot change."""
    if added_by.role is not Role.owner:
        raise Forbidden("only the owner can change who is in a household")
    if session.get(Household, household_id) is None:
        raise NotFound("no such household")
    if session.get(User, user_id) is None:
        raise NotFound("no such user")
    if is_member(session, household_id, user_id):
        raise Conflict("they are already in this household")

    row = HouseholdMember(
        household_id=household_id, user_id=user_id, added_by_id=added_by.id, added_at=utcnow()
    )
    session.add(row)
    session.flush()
    return row


def remove_member(session: Session, *, household_id: str, user_id: str, removed_by: User) -> None:
    if removed_by.role is not Role.owner:
        raise Forbidden("only the owner can change who is in a household")

    current = members(session, household_id)
    if not any(m.user_id == user_id for m in current):
        raise NotFound("they are not in this household")
    if len(current) == 1:
        # A household nobody can reach is not a household; it is data with no
        # way back to it.
        raise Conflict("a household needs at least one member")

    for row in current:
        if row.user_id == user_id:
            session.delete(row)
    session.flush()


# --------------------------------------------------------------------------- #
# What a household holds
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CurrencyRows:
    """How much of the ledger is denominated in one currency.

    Counts only. There is deliberately no figure here: the only honest money
    number at household level would be a net of assets against liabilities, and
    the Accounts screen already shows a balance per account -- which is the
    level where the number means something. See `services/insights.py` for why
    nothing in this codebase adds two currencies together.
    """

    currency: str
    accounts: int
    transactions: int


@dataclass(frozen=True, slots=True)
class HouseholdStats:
    """Counted, not summed.

    Every field is one aggregate query scoped to one household. The scoping is
    the whole risk: a count that forgets its `where` reads the instance rather
    than the ledger, and the number looks perfectly plausible either way --
    which is what `tests/test_household_stats.py` exists to catch, with two
    households in the fixture so a leak has somewhere to leak from.
    """

    members: int
    accounts: int
    accounts_closed: int
    transactions: int
    transactions_uncleared: int
    first_transaction: Date | None
    last_transaction: Date | None
    receipts: int
    receipts_unattached: int
    payees: int
    payee_rules: int
    payee_rules_enabled: int
    categories: int
    categories_archived: int
    category_groups: int
    reconciliations: int
    #: The currency codes any of its accounts are kept in, and how much of the
    #: ledger sits in each.
    currencies: list[CurrencyRows]
    #: ISO 3166-1 alpha-2 codes its accounts name, sorted. An account with no
    #: country is not one of these.
    countries: list[str]


def _count(session: Session, model, *where) -> int:
    return int(session.execute(select(func.count()).select_from(model).where(*where)).scalar() or 0)


def stats(session: Session, household_id: str) -> HouseholdStats:
    """Everything the household page counts, scoped to one household.

    Done as aggregates rather than by loading rows: a household with 25 000
    transactions is exactly the one whose owner wants to know how many there
    are, and counting them in Python means sending them all first.
    """
    accounts_here = select(Account.id).where(Account.household_id == household_id)

    span = session.execute(
        select(func.min(Transaction.date), func.max(Transaction.date)).where(
            Transaction.household_id == household_id
        )
    ).one()

    by_currency = session.execute(
        select(
            Account.currency,
            func.count(func.distinct(Account.id)),
            func.count(Transaction.id),
        )
        .select_from(Account)
        .outerjoin(Transaction, Transaction.account_id == Account.id)
        .where(Account.household_id == household_id)
        .group_by(Account.currency)
        .order_by(Account.currency)
    ).all()

    countries = list(
        session.execute(
            select(Account.country)
            .where(Account.household_id == household_id, Account.country.is_not(None))
            .distinct()
            .order_by(Account.country)
        ).scalars()
    )

    return HouseholdStats(
        members=_count(session, HouseholdMember, HouseholdMember.household_id == household_id),
        accounts=_count(session, Account, Account.household_id == household_id),
        accounts_closed=_count(
            session, Account, Account.household_id == household_id, Account.closed.is_(True)
        ),
        transactions=_count(session, Transaction, Transaction.household_id == household_id),
        transactions_uncleared=_count(
            session,
            Transaction,
            Transaction.household_id == household_id,
            Transaction.cleared == ClearedState.uncleared,
        ),
        first_transaction=span[0],
        last_transaction=span[1],
        receipts=_count(session, Receipt, Receipt.household_id == household_id),
        receipts_unattached=_count(
            session,
            Receipt,
            Receipt.household_id == household_id,
            Receipt.transaction_id.is_(None),
        ),
        payees=_count(session, Payee, Payee.household_id == household_id),
        payee_rules=_count(session, PayeeRule, PayeeRule.household_id == household_id),
        payee_rules_enabled=_count(
            session, PayeeRule, PayeeRule.household_id == household_id, PayeeRule.enabled.is_(True)
        ),
        categories=_count(session, Category, Category.household_id == household_id),
        categories_archived=_count(
            session, Category, Category.household_id == household_id, Category.archived.is_(True)
        ),
        category_groups=_count(
            session, CategoryGroup, CategoryGroup.household_id == household_id
        ),
        # Reconciliations hang off an account, not off the household, so this is
        # the one count that has to reach through the join to be scoped at all.
        reconciliations=_count(
            session, Reconciliation, Reconciliation.account_id.in_(accounts_here)
        ),
        currencies=[
            CurrencyRows(currency=code, accounts=int(n_accounts), transactions=int(n_txns))
            for code, n_accounts, n_txns in by_currency
        ],
        countries=[code for code in countries if code],
    )


@dataclass(frozen=True, slots=True)
class Logged:
    """How many transactions one person put into a household's register.

    `by_agent` is the part of `total` a program did while holding one of that
    person's keys. Beside the total rather than instead of it, for the reason
    `Batch.agent_key_id` sits beside `actor_id` rather than replacing it: an
    agent borrows a person's authority, so the work is theirs and the fact that
    a key did it is a second thing worth saying.
    """

    total: int
    by_agent: int


def transactions_logged(session: Session, household_id: str) -> dict[str, Logged]:
    """Per user id, how many of this household's transactions they entered.

    There is no `created_by` on `transactions` and there should not be: the
    audit log already records who did what, and a second copy of the same fact
    on the row is a second thing to keep in step. So this reads the log.

    Three decisions are in the query, and each one is a different wrong answer
    avoided:

    - **The first insert of a row, not every insert.** A transaction that was
      deleted and put back by an undo carries two, and counting both credits
      the person who restored it with having entered it. `earliest` takes the
      lowest `seq` per row, which is the one that actually logged it.
    - **Only rows that are still in the register.** The join to `transactions`
      is an inner join on purpose. The figure sits under "Who is in it" beside
      a count of what the household holds, so it has to be a statement about
      the ledger as it stands, not about everything anyone ever typed.
    - **Grouped by `actor_id`,** which is what makes "including agents under
      their name" fall out rather than being a second query: a batch an agent
      key ran still names the person whose key it is.

    One grouped read, and `changes` is indexed on `(table_name, row_id, seq)`
    and on `household_id`. A user with nothing to their name is absent rather
    than zero -- the caller is filling in a row per member and knows which
    members it has.
    """
    earliest = (
        select(Change.row_id.label("row_id"), func.min(Change.seq).label("seq"))
        .where(
            Change.household_id == household_id,
            Change.table_name == Transaction.__tablename__,
            Change.op == ChangeOp.insert,
        )
        .group_by(Change.row_id)
        .subquery()
    )
    rows = session.execute(
        select(
            Batch.actor_id,
            Batch.agent_key_id.is_not(None),
            func.count(),
        )
        .select_from(earliest)
        .join(Change, Change.seq == earliest.c.seq)
        .join(Batch, Batch.id == Change.batch_id)
        # Scoped twice: `changes.household_id` above is denormalised and the
        # row itself is the authority. A transaction that moved household would
        # otherwise be counted where its log entry was written.
        .join(
            Transaction,
            (Transaction.id == earliest.c.row_id)
            & (Transaction.household_id == household_id),
        )
        .group_by(Batch.actor_id, Batch.agent_key_id.is_not(None))
    ).all()

    out: dict[str, Logged] = {}
    for actor_id, was_agent, count in rows:
        seen = out.get(actor_id, Logged(total=0, by_agent=0))
        out[actor_id] = Logged(
            total=seen.total + int(count),
            by_agent=seen.by_agent + (int(count) if was_agent else 0),
        )
    return out
