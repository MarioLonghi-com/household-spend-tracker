"""The register, transfers, payees and rules."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic_core import to_json
from sqlalchemy import case, func, select

from ...audit.batch import batch
from ...errors import Conflict
from ...models import (
    Account,
    BatchKind,
    Category,
    CategoryGroup,
    ClearedState,
    Payee,
    PayeeRule,
    RegisterSort,
    RegisterSource,
    ReimbursementView,
    RuleAction,
    SortDirection,
    Transaction,
)
from ...schemas import (
    BulkEdit,
    BulkEditResult,
    DuplicateRequest,
    PayeeCollision,
    PayeeCreate,
    PayeeMerge,
    PayeeOut,
    PayeeRuleCreate,
    PayeeRuleOut,
    PayeeRuleTrial,
    PayeeRuleTrialOut,
    PayeeRuleUpdate,
    PayeeSuggestion,
    ReapplyMove,
    ReapplyPlanOut,
    ReapplyResult,
    ReapplyScope,
    RegisterPage,
    ReimbursementLink,
    ReimbursementUpdate,
    SplitRequest,
    TransactionCreate,
    TransactionOrigin,
    TransactionOut,
    TransactionUpdate,
    TransferCreate,
)
from ...services import accounts as account_service
from ...services import categories as category_service
from ...services import describing
from ...services import payees as payee_service
from ...services import receipts as receipt_service
from ...services import transactions as txn_service
from ..deps import CurrentHousehold, CurrentUser, SessionDep, load_for

router = APIRouter(tags=["register"])


#: How many rows the register will hand over in one response.
#:
#: The preference is that a register loads in one go -- no pages, no "load
#: more" -- and measurement says that is affordable: reading the columns
#: directly rather than building ORM objects and validating each row through
#: pydantic costs 228ms for twenty thousand rows against 863ms for the same
#: rows the slow way. Nine and a half megabytes of JSON is the real limit, not
#: the query.
#:
#: Above this the answer is *not* to quietly return fewer: that is what the old
#: default of 200 did, and it meant looking at a fifth of a ledger with nothing
#: on screen to say so. The response says it was capped and the screen says so
#: too, pointing at the date filter, which is the tool for the job.
REGISTER_CEILING = 25_000


#: The register row's columns, in `TransactionOut`'s field order so the JSON
#: built from them is the JSON the model would have written.
_REGISTER_COLUMNS = (
    Transaction.id,
    Transaction.account_id,
    Transaction.date,
    Transaction.amount,
    Transaction.payee_id,
    Transaction.category_id,
    Transaction.split_id,
    Transaction.memo,
    Transaction.cleared,
    Transaction.transfer_account_id,
    Transaction.transfer_transaction_id,
    Transaction.import_id,
    Transaction.reimbursement,
    Transaction.reimbursed_by_id,
    Transaction.created_at,
)


def _register_rows(session, ordered) -> list:
    """The page's rows as column tuples, `created_at` included for the
    running balance -- one read serves both (#237)."""
    return session.execute(ordered.with_only_columns(*_REGISTER_COLUMNS)).all()


def _row_dicts(
    rows,
    names: dict[str, str],
    categories: dict[str, str],
    currencies: dict[str, str],
    balances: dict[str, int] | None = None,
    receipted: set[str] | None = None,
) -> list[dict]:
    """The register's rows, read as columns rather than as objects.

    The slow path built a `Transaction` for every row and then validated each
    one through pydantic; between them that was four fifths of the time for a
    long register, and neither bought anything -- nothing here mutates a row or
    needs a relationship. So this selects the columns the screen shows and
    builds the dictionaries directly, keyed in `TransactionOut`'s own order.

    Measured on twenty thousand rows: 863ms before, 228ms after, byte-identical
    output.

    `currencies` is the account-to-currency map. `amount` has always been in the
    account's own currency and the response never said which -- the register
    deduced it from the accounts list, which omits closed accounts, so a row on
    a closed account was drawn in the household's base currency. With one
    IN/OUT pair per currency on screen that is not a cosmetic slip: it puts the
    figure in the wrong column. The row carries its currency now.
    """
    return [
        {
            "id": row[0],
            "account_id": row[1],
            "date": row[2],
            "amount": row[3],
            "payee_id": row[4],
            "payee_name": names.get(row[4] or ""),
            "category_id": row[5],
            "category_name": categories.get(row[5] or ""),
            "split_id": row[6],
            "memo": row[7],
            "cleared": row[8],
            "transfer_account_id": row[9],
            "transfer_transaction_id": row[10],
            "import_id": row[11],
            "running_balance": (balances or {}).get(row[0]),
            "has_receipt": row[0] in (receipted or ()),
            "currency": currencies.get(row[1]),
            "reimbursement": row[12],
            "reimbursed_by_id": row[13],
        }
        for row in rows
    ]


def _register_page(
    shown: list[dict], *, total: int, capped: bool, balance: bool, needs_category: int
) -> Response:
    """The page as JSON, written straight from the dictionaries (#237).

    Returned as a `Response`, the route's `response_model` still documents it
    but no longer validates 25,000 dicts into models and then serialises the
    models again: the dicts come from typed columns in the model's own field
    order, and `to_json` writes dates and enums exactly as the model would.
    `test_the_register_json_is_what_the_model_would_write` holds that.
    """
    return Response(
        content=to_json(
            {
                "transactions": shown,
                "total": total,
                "has_running_balance": balance,
                "capped": capped,
                "needs_category": needs_category,
            }
        ),
        media_type="application/json",
    )


def _out(
    txn: Transaction,
    names: dict[str, str],
    running: int | None = None,
    categories: dict[str, str] | None = None,
) -> TransactionOut:
    model = TransactionOut.model_validate(txn)
    model.payee_name = names.get(txn.payee_id or "")
    model.category_name = (categories or {}).get(txn.category_id or "")
    model.running_balance = running
    # The same fact the register's rows carry, for the single-row answers: a
    # PATCH's reply is what the panel keeps as the row, and it must not come
    # back without the currency its amount is in.
    model.currency = txn.account.currency
    return model


def _payee_names(session, household_id: str) -> dict[str, str]:
    return {
        row.id: row.name
        for row in session.execute(
            select(Payee).where(Payee.household_id == household_id)
        ).scalars()
    }


def _category_names(session, household_id: str) -> dict[str, str]:
    """id -> "Group: Category", for a whole page of rows in one query.

    Loaded once per request rather than per row. The previous build resolved
    names row by row and turned a register page into an N+1 by row count.
    """
    rows = session.execute(
        select(Category.id, CategoryGroup.name, Category.name)
        .join(CategoryGroup, CategoryGroup.id == Category.group_id)
        .where(Category.household_id == household_id)
    ).all()
    return {row[0]: f"{row[1]}: {row[2]}" for row in rows}


def _resolve_category(session, household_id: str, category_id: str | None, clear: bool):
    """UNSET when nothing was said, so `create` consults the payee's rule."""
    if clear:
        return None
    if category_id:
        return category_service.get_for_household(session, category_id, household_id)
    return txn_service.UNSET


def _resolve_payee(session, household_id: str, payee_id: str | None, payee_name: str | None):
    """A payee named by id has to be one of this household's.

    Most callers are saved by a currency or household check further in, but
    creating a payee rule was not -- so a rule could point at another
    household's payee, and the import preview then showed its name.
    """
    if payee_id:
        payee = session.execute(
            select(Payee).where(Payee.id == payee_id, Payee.household_id == household_id)
        ).scalar_one_or_none()
        if payee is None:
            from ...errors import NotFound

            raise NotFound("no such payee")
        return payee
    if payee_name and payee_name.strip():
        return payee_service.get_or_create(session, household_id, payee_name)
    return None


def _ordering(stmt, sort: RegisterSort, direction: SortDirection) -> tuple[object, set[str]]:
    """Apply one sort, and name the joins it needs.

    `created_at` is always the tiebreaker: several rows a day share a date, and
    without a stable second key the same query returns them in a different order
    each time, which makes a paginated register skip and repeat rows.

    The joins come back as a set of names rather than a flag per table: it was a
    pair of booleans, and every new sortable column that needed a join made it
    a longer tuple that every caller had to unpack in the right order.
    """
    descending = direction is SortDirection.desc

    def turn(column):
        return column.desc() if descending else column.asc()

    if sort is RegisterSort.account:
        return (
            stmt.order_by(
                turn(Account.name), Transaction.date.desc(), Transaction.created_at.desc()
            ),
            {"account"},
        )
    if sort is RegisterSort.payee:
        # Nulls last either way: a row with no payee is not "before A".
        return (
            stmt.order_by(Payee.name.is_(None), turn(Payee.name), Transaction.created_at.desc()),
            {"payee"},
        )
    if sort is RegisterSort.category:
        # Nulls last either way: an uncategorised row is not "before Annual
        # Fees". Ordered by the full "Group: Category" so the register groups
        # the way the picker does, rather than interleaving two groups that
        # happen to share a category name's first letter.
        return (
            stmt.order_by(
                Category.name.is_(None),
                turn(CategoryGroup.name),
                turn(Category.name),
                Transaction.created_at.desc(),
            ),
            {"category"},
        )
    if sort is RegisterSort.memo:
        return (
            stmt.order_by(
                Transaction.memo.is_(None), turn(Transaction.memo), Transaction.created_at.desc()
            ),
            set(),
        )
    if sort is RegisterSort.amount:
        # Currency first, then the figure -- CLAUDE.md's rule for a money column
        # across mixed currencies, and the register is now the screen where that
        # is visible: it draws one IN/OUT pair per currency, and a sort that
        # interleaved EUR 90 between GBP 80 and GBP 100 would be ordering by a
        # number nobody is comparing.
        #
        # Both keys turn together, the way `sortRows` in the client turns a
        # tuple: reversing the column reverses the whole order rather than
        # leaving the currencies in place and flipping inside them.
        return (
            stmt.order_by(
                turn(Account.currency), turn(Transaction.amount), Transaction.created_at.desc()
            ),
            {"account"},
        )
    if sort is RegisterSort.source:
        # Three states from two nullable columns, ranked so the sort is a real
        # order rather than an alphabetical accident: a transfer first, then an
        # import, then a row somebody typed.
        rank = case(
            (Transaction.transfer_account_id.is_not(None), 0),
            (Transaction.import_id.is_not(None), 1),
            else_=2,
        )
        return (
            stmt.order_by(turn(rank), Transaction.date.desc(), Transaction.created_at.desc()),
            set(),
        )
    if sort is RegisterSort.cleared:
        return (
            stmt.order_by(
                turn(Transaction.cleared),
                Transaction.date.desc(),
                Transaction.created_at.desc(),
            ),
            set(),
        )
    return stmt.order_by(turn(Transaction.date), turn(Transaction.created_at)), set()


@router.get("/households/{household_id}/transactions", response_model=RegisterPage)
def register(
    household: CurrentHousehold,
    session: SessionDep,
    account_id: Annotated[list[str] | None, Query()] = None,
    since: Date | None = None,
    until: Date | None = None,
    search: str | None = None,
    cleared: ClearedState | None = None,
    uncategorised: bool = False,
    #: The category picker (#188): these ids, and with `categorised` every
    #: row that has any category. Both *add* to `uncategorised` rather than
    #: narrowing it -- see `transactions.filtered`.
    category_id: Annotated[list[str] | None, Query()] = None,
    categorised: bool = False,
    #: An unsigned decimal, "45.20" or "45", matched against every money
    #: column at once -- Out and In, in every currency (#123). See
    #: `money.magnitude_span` for what a whole number means.
    amount: Annotated[str | None, Query(max_length=32)] = None,
    source: RegisterSource | None = None,
    #: The Work expenses filter. `work` and `paid` bring the payments along
    #: with the expenses they repaid; see `transactions._reimbursement_is`.
    reimbursement: ReimbursementView | None = None,
    sort: RegisterSort = RegisterSort.date,
    direction: SortDirection = SortDirection.desc,
    limit: int = Query(default=REGISTER_CEILING, ge=1, le=REGISTER_CEILING),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """The register, whole.

    The default is everything the filter matches, not a page of it. It used to
    be 200 and the client never asked for more, so a household with a thousand
    rows saw two hundred of them with nothing on screen to say which two
    hundred. `limit` survives for a caller that wants less; nothing in the app
    sends it.

    `account_id` repeats, the way the income-and-expense report's does: the
    register's account filter is a grouped picker, so "the three Spanish
    accounts" is one request. One `account_id` behaves exactly as it always
    did, which is what lets the existing tests stand as the proof of that.

    A running balance is returned only when the view is **one** account in date
    order -- across accounts, under a filter that hides rows, or sorted by
    anything else, the column would be a plausible-looking lie. Two ticked
    accounts is "across accounts" however the ids arrived, so it withholds the
    balance for the same reason it always did.
    """
    # The service owns the filter, so the register and the agent aggregates
    # narrow rows with the same code rather than the same intentions. It used
    # to be written out here, which is how two endpoints end up disagreeing
    # about what `search` means.
    #
    # Which currency each row's amount is in: read before the filter now,
    # because the amount lookup needs it to know what "45.20" means in each.
    currencies = txn_service.account_currencies(session, household.id)
    typed_amount = (amount or "").strip()
    amount_spans = txn_service.amount_lookup(currencies, typed_amount) if typed_amount else None
    stmt = txn_service.filtered(
        household.id,
        account_ids=account_id,
        since=since,
        until=until,
        search=search,
        cleared=cleared,
        uncategorised=uncategorised,
        category_ids=category_id,
        categorised=categorised,
        amount=amount_spans,
        source=source,
        reimbursement=reimbursement,
    )

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    # The badge beside "Needs a category" (#188): how many of *these* rows --
    # every other filter applied, the category picker set aside -- have no
    # category. It used to be a second request from the screen, to this same
    # path with `uncategorised=true&limit=1`, fired in the same tick as the
    # first. `access.log` drops the query string, so every register load and
    # every refresh showed up as two identical GETs a few milliseconds apart,
    # and each of them ran the filter, the count and the lookups again (#101).
    # One more count here is cheaper than a second request.
    needs_category = session.execute(
        select(func.count()).select_from(
            txn_service.filtered(
                household.id,
                account_ids=account_id,
                since=since,
                until=until,
                search=search,
                cleared=cleared,
                uncategorised=True,
                amount=amount_spans,
                source=source,
                reimbursement=reimbursement,
            ).subquery()
        )
    ).scalar_one()

    ordered, joins = _ordering(stmt, sort, direction)
    if "account" in joins:
        ordered = ordered.outerjoin(Account, Account.id == Transaction.account_id)
    if "payee" in joins:
        ordered = ordered.outerjoin(Payee, Payee.id == Transaction.payee_id)
    if "category" in joins:
        ordered = ordered.outerjoin(Category, Category.id == Transaction.category_id).outerjoin(
            CategoryGroup, CategoryGroup.id == Category.group_id
        )
    names = _payee_names(session, household.id)
    cats = _category_names(session, household.id)
    # One more indexed read, the same shape as the two above: fetch the small
    # lookup whole and resolve against it in Python. Served by
    # ix_receipts_household_txn, this is an index-only scan -- under 4 ms
    # against the 228 ms the register already spends at twenty thousand rows.
    receipted = receipt_service.receipted_ids(session, household.id)
    ordered = ordered.limit(limit).offset(offset)

    # The running balance is a sum down the page, so it only means anything when
    # the page is one account in date order. Any other sort makes it arithmetic
    # over rows in an order the number does not follow.
    only_account = account_id[0] if account_id and len(account_id) == 1 else None
    default_order = sort is RegisterSort.date and direction is SortDirection.desc
    # Every filter that can hide a row, not merely the three that were here.
    # The rule is in the comment above and `cleared` was already outside it:
    # narrowing to uncleared rows left the balance column switched on, summing
    # a subset down the page and presenting the result as the account's
    # balance. `uncategorised` would have been the second one, which is what
    # made the first visible.
    narrowed = bool(
        since
        or until
        or search
        or cleared is not None
        or uncategorised
        or category_id
        or categorised
        or typed_amount
        or source is not None
        or reimbursement is not None
    )
    whole_account = bool(only_account) and default_order and not narrowed
    rows = _register_rows(session, ordered)
    if not whole_account:
        shown = _row_dicts(rows, names, cats, currencies, receipted=receipted)
        return _register_page(
            shown,
            total=total,
            capped=total > len(shown) + offset,
            balance=False,
            needs_category=needs_category,
        )

    # The running balance needs the rows in date order with their created_at,
    # which the one read above carries: it used to read the page a second time
    # for these four fields (#237).
    keyed = [(row[0], row[2], row[3], row[14]) for row in rows]

    # Seeded with everything older than this page. Accumulating from zero gave
    # every figure on page two -- and on page one of a long register -- a
    # plausible-looking wrong number.
    ascending = sorted(keyed, key=lambda row: (row[1], row[3]))
    opening = 0
    if ascending:
        oldest = ascending[0]
        opening = session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.account_id == only_account,
                (Transaction.date < oldest[1])
                | ((Transaction.date == oldest[1]) & (Transaction.created_at < oldest[3])),
            )
        ).scalar_one()

    by_id: dict[str, int] = {}
    running = opening
    for row in ascending:
        running += row[2]
        by_id[row[0]] = running

    shown = _row_dicts(rows, names, cats, currencies, balances=by_id, receipted=receipted)
    return _register_page(
        shown,
        total=total,
        capped=total > len(shown) + offset,
        balance=True,
        needs_category=needs_category,
    )


@router.post(
    "/households/{household_id}/transactions", response_model=TransactionOut, status_code=201
)
def create_transaction(
    body: TransactionCreate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> TransactionOut:
    """One row, one batch. Every change in the system is undoable the same way."""
    # Scoped to the household in the path, not to any household the caller
    # belongs to. `load_for` allowed household B's account through a
    # /households/A/transactions call: not a breach, since membership in both is
    # still required, but the row landed in B while the batch and every change
    # row were filed under A -- so B's History never showed the act that created
    # B's transaction.
    account = account_service.get_for_household(session, body.account_id, household.id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        payee = _resolve_payee(session, household.id, body.payee_id, body.payee_name)
        txn = txn_service.create(
            session,
            account=account,
            date=body.date,
            amount=body.amount,
            payee=payee,
            category=_resolve_category(
                session, household.id, body.category_id, body.uncategorised
            ),
            memo=body.memo,
            cleared=body.cleared,
        )
    return _out(txn, _payee_names(session, household.id), categories=_category_names(session, household.id))


@router.patch("/transactions/{transaction_id}", response_model=TransactionOut)
def update_transaction(
    transaction_id: str, body: TransactionUpdate, session: SessionDep, user: CurrentUser
) -> TransactionOut:
    txn = load_for(session, user, Transaction, transaction_id)
    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=txn.household_id
    ):
        payee: object = txn_service.UNSET
        if body.clear_payee:
            payee = None
        elif body.payee_id or body.payee_name:
            payee = _resolve_payee(session, txn.household_id, body.payee_id, body.payee_name)

        memo: object = txn_service.UNSET
        if body.clear_memo:
            memo = None
        elif body.memo is not None:
            memo = body.memo

        category: object = txn_service.UNSET
        if body.clear_category:
            category = None
        elif body.category_id:
            category = category_service.get_for_household(
                session, body.category_id, txn.household_id
            )

        txn_service.update(
            session,
            txn,
            date=body.date,
            amount=body.amount,
            payee=payee,
            category=category,
            memo=memo,
            cleared=body.cleared,
        )
    return _out(txn, _payee_names(session, txn.household_id), categories=_category_names(session, txn.household_id))


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_transaction(transaction_id: str, session: SessionDep, user: CurrentUser) -> None:
    txn = load_for(session, user, Transaction, transaction_id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=txn.household_id):
        txn_service.delete(session, txn)


@router.patch("/transactions/{transaction_id}/reimbursement", response_model=TransactionOut)
def set_reimbursement(
    transaction_id: str, body: ReimbursementUpdate, session: SessionDep, user: CurrentUser
) -> TransactionOut:
    """Flag a row as a work expense, or say which payment repaid it.

    Its own route rather than two more fields on the PATCH above, because that
    one refuses a reconciled row and this one must not: whether work pays a
    thing back is decided long after the statement was ticked off.

    The payment is looked up in the expense's own household, so another
    household's row is a 404 however the caller came by its id.
    """
    txn = load_for(session, user, Transaction, transaction_id)
    state: object = txn_service.UNSET
    if body.clear_state:
        state = None
    elif body.state is not None:
        state = body.state
    settled_by: object = txn_service.UNSET
    if body.clear_settlement:
        settled_by = None
    elif body.settled_by_id:
        settled_by = txn_service.get_for_household(session, body.settled_by_id, txn.household_id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=txn.household_id):
        txn_service.set_reimbursement(session, txn, state=state, settled_by=settled_by)
    return _out(
        txn,
        _payee_names(session, txn.household_id),
        categories=_category_names(session, txn.household_id),
    )


@router.post(
    "/households/{household_id}/transactions/reimbursements/link",
    response_model=list[TransactionOut],
)
def link_reimbursements(
    body: ReimbursementLink, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> list[TransactionOut]:
    """Many expenses, one payment -- the register's "Link as reimbursement".

    One batch and all or nothing: a refusal on any row leaves every row as it
    was, and says which row it was about. An id that is not this household's
    is a 404, as it is everywhere.
    """
    settlement = txn_service.get_for_household(session, body.settlement_id, household.id)
    expenses = [
        txn_service.get_for_household(session, one, household.id)
        for one in dict.fromkeys(body.expense_ids)
    ]
    with batch(session, kind=BatchKind.bulk_update, actor_id=user.id, household_id=household.id):
        txn_service.link_reimbursements(session, expenses, settlement)
    names = _payee_names(session, household.id)
    cats = _category_names(session, household.id)
    return [_out(one, names, categories=cats) for one in expenses]


@router.post("/transactions/{transaction_id}/duplicate", response_model=TransactionOut, status_code=201)
def duplicate_transaction(
    transaction_id: str, body: DuplicateRequest, session: SessionDep, user: CurrentUser
) -> TransactionOut:
    txn = load_for(session, user, Transaction, transaction_id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=txn.household_id):
        copy = txn_service.duplicate(session, txn, date=body.date)
    return _out(copy, _payee_names(session, txn.household_id), categories=_category_names(session, txn.household_id))


@router.get("/transactions/{transaction_id}/origin", response_model=TransactionOrigin)
def transaction_origin(
    transaction_id: str, session: SessionDep, user: CurrentUser
) -> TransactionOrigin:
    """What the bank wrote on the line this transaction came from.

    404 when it was entered by hand, which is not an error -- it is the answer.

    An agent import *does* have lines, and so reaches here -- it goes through
    the same staging path a file does, which is the whole point of the agent
    API being a second front door rather than a second write path. What it has
    no file, so `via` carries the program's name and the panel says who staged
    it instead of "Imported from a statement" about a statement that never
    existed.

    A one-time import (#183) has no lines either -- it reads another app's
    whole history and keeps none of it -- so it is found through the batch
    that inserted the row, and answers with the workflow and the file or plan
    it read. Without this case every row it brought in read "entered by hand".
    """
    from ...errors import NotFound
    from ...models import Batch, ImportLine
    from ...services.one_time_import import engine as one_time

    txn = load_for(session, user, Transaction, transaction_id)
    line = session.execute(
        select(ImportLine).where(ImportLine.transaction_id == txn.id).limit(1)
    ).scalar_one_or_none()
    if line is None:
        brought = one_time.batch_that_created(session, txn.id)
        if brought is None:
            raise NotFound("this transaction was not imported")
        detail = brought.source or {}
        workflow = detail.get(one_time.MARK)
        return TransactionOrigin(
            kind="one_time_import",
            imported_at=brought.started_at,
            filename=detail.get("filename"),
            payee_original=txn.import_payee_original,
            import_id=txn.import_id,
            batch_id=brought.id,
            workflow=one_time.WORKFLOW_NAMES.get(workflow, workflow),
            workflow_via=detail.get("via"),
            plan_name=detail.get("plan_name"),
        )

    batch_row = session.get(Batch, line.batch_id)
    source = (batch_row.source or {}) if batch_row else {}
    parsed = line.parsed or {}
    return TransactionOrigin(
        imported_at=batch_row.started_at if batch_row else line.created_at,
        filename=source.get("filename"),
        line_no=line.line_no,
        raw=line.raw,
        bank=parsed.get("bank"),
        payee_original=txn.import_payee_original,
        import_id=txn.import_id,
        batch_id=line.batch_id,
        via=describing.via_name(batch_row) if batch_row else None,
    )


@router.post("/households/{household_id}/transfers", status_code=201)
def create_transfer(
    body: TransferCreate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> dict:
    # Same scoping as create_transaction: the URL names the household, so both
    # legs have to belong to it.
    source = account_service.get_for_household(session, body.from_account_id, household.id)
    destination = account_service.get_for_household(session, body.to_account_id, household.id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        out_leg, in_leg = txn_service.create_transfer(
            session,
            source=source,
            destination=destination,
            date=body.date,
            amount=body.amount,
            to_amount=body.to_amount,
            memo=body.memo,
            cleared=body.cleared,
        )
    names = _payee_names(session, household.id)
    cats = _category_names(session, household.id)
    return {"from": _out(out_leg, names, categories=cats), "to": _out(in_leg, names, categories=cats)}


@router.post("/transactions/{transaction_id}/split", response_model=list[TransactionOut])
def split_transaction(
    transaction_id: str, body: SplitRequest, session: SessionDep, user: CurrentUser
) -> list[TransactionOut]:
    """Divide one transaction into the parts it was really made of.

    One batch: the original goes, the parts arrive, and undo reverses the whole
    thing. The parts share a `split_id` so the register can show they belong
    together; the row they replaced lives in the audit log, which is also where
    the undo comes from.
    """
    txn = load_for(session, user, Transaction, transaction_id)
    household_id = txn.household_id

    with batch(session, kind=BatchKind.split, actor_id=user.id, household_id=household_id):
        parts = [
            txn_service.SplitPart(
                amount=one.amount,
                category=(
                    category_service.get_for_household(session, one.category_id, household_id)
                    if one.category_id
                    else None
                ),
                memo=one.memo,
            )
            for one in body.parts
        ]
        made = txn_service.split(session, txn, parts)

    names = _payee_names(session, household_id)
    cats = _category_names(session, household_id)
    return [_out(one, names, categories=cats) for one in made]


@router.get("/transactions/{transaction_id}/split", response_model=list[TransactionOut])
def read_split(
    transaction_id: str, session: SessionDep, user: CurrentUser
) -> list[TransactionOut]:
    """The other parts this one was split alongside."""
    txn = load_for(session, user, Transaction, transaction_id)
    if not txn.split_id:
        return []
    names = _payee_names(session, txn.household_id)
    cats = _category_names(session, txn.household_id)
    return [
        _out(one, names, categories=cats)
        for one in txn_service.parts_of(session, txn.split_id)
    ]


@router.post("/households/{household_id}/transactions/bulk", response_model=BulkEditResult)
def bulk_edit(
    body: BulkEdit, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> BulkEditResult:
    """Many rows, one act.

    Deliberately loaded and changed one at a time rather than issued as a single
    UPDATE: a bulk statement bypasses the audit hook, and this would be the one
    place that silently stopped being undoable.

    Setting a category **skips** transfer legs rather than refusing the whole
    selection (#124). A transfer has no category, and one leg caught up in a
    shift-click should not cost somebody the other forty rows. The rest of the
    edit -- cleared, memo, payee -- still reaches them, and how many were
    skipped comes back so the screen can say so.

    The work-expense flag skips the same way, for the same reason: money
    coming in and transfer legs cannot be one, and are counted in
    `skipped_reimbursement`. Writing off a row that was already repaid is not
    skipped but refused, whole -- skipping it would say it was written off.
    """
    rows = list(
        session.execute(
            select(Transaction).where(
                Transaction.id.in_(body.transaction_ids),
                Transaction.household_id == household.id,
            )
        ).scalars()
    )
    skipped = 0
    skipped_reimbursement = 0
    reimbursement: object = txn_service.UNSET
    if body.clear_reimbursement:
        reimbursement = None
    elif body.reimbursement is not None:
        reimbursement = body.reimbursement
    with batch(session, kind=BatchKind.bulk_update, actor_id=user.id, household_id=household.id):
        payee: object = txn_service.UNSET
        if body.payee_id or body.payee_name:
            payee = _resolve_payee(session, household.id, body.payee_id, body.payee_name)

        category: object = txn_service.UNSET
        if body.clear_category:
            category = None
        elif body.category_id:
            category = category_service.get_for_household(
                session, body.category_id, household.id
            )

        # Every row is changed pending and flushed once below, and which of
        # them repaid something is read once: a flush and two `is_payment`
        # reads per row were 1,958 statements for 1,000 rows (#236). Safe to
        # defer because this edit only sets states, never a link, so nothing
        # it writes changes what `is_payment` answers for the next row.
        payments = (
            txn_service.payments_among(session, [txn.id for txn in rows])
            if reimbursement is not txn_service.UNSET
            else None
        )
        for txn in rows:
            if reimbursement is not txn_service.UNSET:
                # Through the one writer, not `update()`: a reconciled row can
                # still be flagged, and the rules live in one place. A row
                # that cannot be a work expense is skipped and counted, as a
                # transfer leg is for a category.
                if reimbursement is not None and not txn_service.claimable(
                    session, txn, payments=payments
                ):
                    skipped_reimbursement += 1
                else:
                    try:
                        txn_service.set_reimbursement(
                            session, txn, state=reimbursement, flush=False, payments=payments
                        )
                    except Conflict as refused:
                        # Writing off a row that was already repaid. Not
                        # skipped: that would leave the person believing it
                        # was written off. Refused whole, naming the row.
                        raise Conflict(f"{txn_service.named(session, txn)}: {refused}") from None
            own_category = category
            setting = category is not txn_service.UNSET and category is not None
            if setting and txn_service.is_transfer_leg(txn):
                own_category = txn_service.UNSET
                skipped += 1
            nothing_left = (
                own_category is txn_service.UNSET
                and payee is txn_service.UNSET
                and body.memo is None
                and body.cleared is None
            )
            if nothing_left:
                # Skipped means untouched: calling `update` with nothing to do
                # would still refuse a locked leg and sink the whole selection.
                continue
            txn_service.update(
                session,
                txn,
                payee=payee,
                category=own_category,
                memo=body.memo if body.memo is not None else txn_service.UNSET,
                cleared=body.cleared,
                flush=False,
            )
        session.flush()
    names = _payee_names(session, household.id)
    cats = _category_names(session, household.id)
    return BulkEditResult(
        transactions=[_out(txn, names, categories=cats) for txn in rows],
        skipped_transfer_legs=skipped,
        skipped_reimbursement=skipped_reimbursement,
    )


# --------------------------------------------------------------------------- #
# Payees and rules
# --------------------------------------------------------------------------- #


@router.get("/households/{household_id}/payees", response_model=list[PayeeOut])
def list_payees(household: CurrentHousehold, session: SessionDep) -> list[PayeeOut]:
    """Every payee, with what points at it.

    The counts are two grouped reads for the whole list rather than one query
    per payee, the same shape the register's name lookups use. They are what
    makes #59's cheap half work: a payee with one transaction and a name
    ending in twelve digits is obvious the moment the list sorts by count, and
    invisible in several hundred alphabetical names.
    """
    return _payees_out(session, household.id, payee_service.list_for_household(session, household.id))


@router.get(
    "/households/{household_id}/payee-collisions", response_model=list[PayeeCollision]
)
def payee_collisions(household: CurrentHousehold, session: SessionDep) -> list[PayeeCollision]:
    """Which of this household's payees are now one name? Issue #268.

    Accents, dash style and invisible spaces stopped telling payees apart.
    The ones made before that are listed here rather than merged, so the payee
    screen can offer the existing merge for each group and a person decides
    which spelling stays. Nothing is written by asking.
    """
    groups = payee_service.collisions(session, household.id)
    counted = {
        one.id: one
        for one in _payees_out(
            session, household.id, [payee for group in groups for payee in group.payees]
        )
    }
    return [
        PayeeCollision(key=group.key, payees=[counted[payee.id] for payee in group.payees])
        for group in groups
    ]


def _payees_out(session, household_id: str, payees) -> list[PayeeOut]:
    """Payees with what points at each, from two grouped reads."""
    rows = dict(
        session.execute(
            select(Transaction.payee_id, func.count())
            .where(Transaction.household_id == household_id)
            .group_by(Transaction.payee_id)
        ).all()
    )
    ruled = dict(
        session.execute(
            select(PayeeRule.payee_id, func.count())
            .where(PayeeRule.household_id == household_id)
            .group_by(PayeeRule.payee_id)
        ).all()
    )

    out = []
    for payee in payees:
        one = PayeeOut.model_validate(payee)
        one.transaction_count = int(rows.get(payee.id, 0))
        one.rule_count = int(ruled.get(payee.id, 0))
        out.append(one)
    return out


@router.get(
    "/households/{household_id}/payee-suggestions", response_model=list[PayeeSuggestion]
)
def payee_suggestions(household: CurrentHousehold, session: SessionDep) -> list[PayeeSuggestion]:
    """Which payees are one rule apart from being fixed? Issue #59.

    The cheapest win on the payee problem, and it was not being taken because
    the app knew something the person did not and never said it: every one of
    these groups is already handled by today's rule engine.
    """
    return [
        PayeeSuggestion(**asdict(one))
        for one in payee_service.suggest_rules(session, household.id)
    ]


@router.post(
    "/households/{household_id}/payee-rules/try", response_model=PayeeRuleTrialOut
)
def try_rule(
    body: PayeeRuleTrial, household: CurrentHousehold, session: SessionDep
) -> PayeeRuleTrialOut:
    """What would this rule claim, against what is already here? Issue #61.

    The rules screen had no preview: a rule was written blind and the result
    appeared at the next import. A POST rather than a GET because a pattern is
    a regex and a regex in a query string is a quoting argument nobody needs.
    """
    trial = payee_service.try_pattern(
        session,
        household.id,
        match_type=body.match_type,
        pattern=body.pattern,
        action=body.action,
        replacement=body.replacement,
    )
    return PayeeRuleTrialOut(**asdict(trial))


@router.post(
    "/households/{household_id}/payee-rules/reapply/preview", response_model=ReapplyPlanOut
)
def preview_reapply(
    body: ReapplyScope, household: CurrentHousehold, session: SessionDep
) -> ReapplyPlanOut:
    """What re-applying the rules would do. Nothing is written. Issue #60."""
    plan = payee_service.plan_reapply(
        session,
        household.id,
        account_id=body.account_id,
        since=body.since,
        until=body.until,
        only_untouched=body.only_untouched,
    )
    return _plan_out(plan)


@router.post("/households/{household_id}/payee-rules/reapply", response_model=ReapplyResult)
def do_reapply(
    body: ReapplyScope,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
    #: Remove the payees left with nothing pointing at them. Asked for rather
    #: than assumed: after a re-apply the ones that were only ever bank noise
    #: are empty, and a payee list still 300 long is what makes the work feel
    #: like it did not happen.
    delete_orphans: bool = False,
) -> ReapplyResult:
    """Re-apply the rules to rows already in the ledger. Issue #60.

    Rules ran in exactly one place -- `stage()`, as a statement is read -- and
    the answer was frozen onto the line. Nothing ever consulted a rule again,
    so writing one did not touch a single row already in the register, which is
    where the rows a person is looking at when they decide to write it are.

    **One batch, so one undo.** A bulk re-categorisation of a whole ledger is
    precisely the operation somebody wants to take back whole, and that is the
    entire reason the audit log exists. The plan is recomputed here rather than
    taken from the caller: a preview held in a tab since before another import
    landed would otherwise write yesterday's answer.
    """
    plan = payee_service.plan_reapply(
        session,
        household.id,
        account_id=body.account_id,
        since=body.since,
        until=body.until,
        only_untouched=body.only_untouched,
    )
    gone: list[str] = []
    with batch(
        session, kind=BatchKind.bulk_update, actor_id=user.id, household_id=household.id
    ) as acting:
        moved = payee_service.apply_reapply(session, plan)
        if delete_orphans:
            gone = payee_service.delete_orphans(session, household.id)
        batch_id = acting.id

    return ReapplyResult(batch_id=batch_id, moved=moved, deleted_payees=gone)


def _plan_out(plan) -> ReapplyPlanOut:
    return ReapplyPlanOut(
        considered=plan.considered,
        changing=plan.changing,
        moves=[ReapplyMove(**asdict(m)) for m in plan.moves],
        orphaned=plan.orphaned,
    )


@router.post("/households/{household_id}/payees", response_model=PayeeOut, status_code=201)
def create_payee(
    body: PayeeCreate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> PayeeOut:
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        payee = payee_service.get_or_create(session, household.id, body.name)
    return PayeeOut.model_validate(payee)


@router.post("/payees/{payee_id}/merge", response_model=PayeeOut)
def merge_payee(
    payee_id: str, body: PayeeMerge, session: SessionDep, user: CurrentUser
) -> PayeeOut:
    source = load_for(session, user, Payee, payee_id)
    target = load_for(session, user, Payee, body.into_payee_id)
    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=source.household_id
    ):
        merged = payee_service.merge(session, source=source, target=target)
    return PayeeOut.model_validate(merged)


@router.get("/households/{household_id}/payee-rules", response_model=list[PayeeRuleOut])
def list_rules(household: CurrentHousehold, session: SessionDep) -> list[PayeeRuleOut]:
    rows = session.execute(
        select(PayeeRule)
        .where(PayeeRule.household_id == household.id)
        .order_by(PayeeRule.priority, PayeeRule.created_at)
    ).scalars()
    return [PayeeRuleOut.model_validate(r) for r in rows]


@router.post("/households/{household_id}/payee-rules", response_model=PayeeRuleOut, status_code=201)
def create_rule(
    body: PayeeRuleCreate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> PayeeRuleOut:
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        # A rewrite rule has no payee, and asking for one here would create an
        # empty payee row for every rail somebody strips. `create_rule` is what
        # refuses a map rule without one, so the refusal is in one place.
        payee = None
        if body.action is not RuleAction.rewrite:
            payee = _resolve_payee(session, household.id, body.payee_id, body.payee_name)
        rule = payee_service.create_rule(
            session,
            household_id=household.id,
            match_type=body.match_type,
            action=body.action,
            pattern=body.pattern,
            payee=payee,
            replacement=body.replacement,
            priority=body.priority,
        )
    return PayeeRuleOut.model_validate(rule)


@router.patch("/payee-rules/{rule_id}", response_model=PayeeRuleOut)
def update_rule(
    rule_id: str, body: PayeeRuleUpdate, session: SessionDep, user: CurrentUser
) -> PayeeRuleOut:
    """Turn a rule off, or move it in the order, without deleting it.

    A rule that is wrong today may be right again after the bank changes its
    wording, and deleting it loses the pattern somebody worked out.
    """
    rule = load_for(session, user, PayeeRule, rule_id)
    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=rule.household_id
    ):
        if body.enabled is not None:
            rule.enabled = body.enabled
        if body.priority is not None:
            rule.priority = body.priority
        session.flush()
    return PayeeRuleOut.model_validate(rule)


@router.delete("/payee-rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, session: SessionDep, user: CurrentUser) -> None:
    rule = load_for(session, user, PayeeRule, rule_id)
    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=rule.household_id
    ):
        session.delete(rule)
