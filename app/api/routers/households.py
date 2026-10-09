"""Households, membership and accounts.

Every route here is household-scoped through ``current_household``, which is
what makes a non-member's answer identical to a household that does not exist.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ... import countries, theming
from ...audit.batch import batch
from ...models import Account, BatchKind, BlobRole, Receipt, ReceiptBlob, User
from ...schemas import (
    AccountCreate,
    AccountImportOut,
    AccountImportRow,
    AccountOut,
    AccountUpdate,
    AddMember,
    CountryOut,
    HouseholdColours,
    HouseholdCreate,
    HouseholdOut,
    HouseholdUpdate,
    MemberOut,
    PaletteOut,
    ReconcileCandidate,
    ReconcileRequest,
    ReconcileWorksheet,
    ReconciliationOut,
    SchemeOut,
)
from ...services import account_import, reconciling
from ...services import accounts as account_service
from ...services import households as household_service
from ...services import receipts as receipt_service
from ...services.households import UNSET
from ..deps import CurrentHousehold, CurrentUser, OwnerOnly, SessionDep, load_for
from ..uploads import read_capped, refuse_declared_size


def _seen(household, session=None) -> HouseholdOut:
    """A household as the client needs it: its settings, and its finished colours."""
    light, dark = theming.resolve(household.theme, household.accent)
    out = HouseholdOut.model_validate(household)
    out.colours = HouseholdColours(light=_scheme(light), dark=_scheme(dark))
    # The operator's floor, so the screen can say "your host has turned this on
    # for every household" rather than showing a switch that does nothing.
    out.receipts_keep_original_forced = receipt_service.keep_original_everywhere()
    out.receipts_keep_original = receipt_service.keep_original(household)
    if session is not None:
        # What the warning needs a number for: turning the toggle off keeps
        # every original already stored and changes only the next upload. One
        # count per household, and a person has one or two. Issue #62.
        #
        # Joined on the hash, not on a receipt id: `receipt_blobs` is
        # content-addressed by `(sha256, role)` and **shared between
        # households** -- two households uploading the same PDF store it once.
        # So this counts *this household's receipts that have an original
        # available*, which is what the sentence beside the switch claims, and
        # is not the same thing as counting blobs.
        out.receipts_with_original = int(
            session.execute(
                select(func.count())
                .select_from(Receipt)
                .join(
                    ReceiptBlob,
                    (ReceiptBlob.sha256 == Receipt.blob_sha256)
                    & (ReceiptBlob.role == BlobRole.original),
                )
                .where(Receipt.household_id == household.id)
            ).scalar_one()
        )
    return out


def _scheme(scheme: theming.Scheme) -> SchemeOut:
    return SchemeOut(
        **{key.lstrip("-").replace("-", "_"): value for key, value in scheme.as_css().items()}
    )


router = APIRouter(tags=["households"])


@router.get("/households", response_model=list[HouseholdOut])
def list_households(session: SessionDep, user: CurrentUser) -> list[HouseholdOut]:
    return [_seen(h, session) for h in household_service.list_for(session, user)]


@router.post("/households", response_model=HouseholdOut, status_code=201)
def create_household(body: HouseholdCreate, session: SessionDep, user: CurrentUser) -> HouseholdOut:
    with batch(session, kind=BatchKind.admin, actor_id=user.id):
        household = household_service.create_household(
            session,
            name=body.name,
            creator=user,
            base_currency=body.base_currency,
            date_format=body.date_format,
            locale=body.locale,
        )
    return _seen(household)


@router.get("/countries", response_model=list[CountryOut])
def list_countries() -> list[CountryOut]:
    """Every ISO 3166-1 country, with its flag.

    Sent rather than shipped in the client bundle for the same reason the
    palettes are: the code is validated here, so the list the picker offers and
    the list the server will accept are the same list by construction.
    """
    return [
        CountryOut(code=code, name=name, flag=countries.flag(code))
        for code, name in countries.COUNTRIES
    ]


@router.get("/themes", response_model=list[PaletteOut])
def list_themes() -> list[PaletteOut]:
    """Every palette, with its colours.

    Sent rather than duplicated in the client bundle: the contrast rules that
    keep these usable are enforced in Python, and a second hand-maintained copy
    of the same hexes is a copy that will eventually disagree with the one that
    was checked.
    """
    return [
        PaletteOut(
            key=palette.key,
            label=palette.label,
            light=_scheme(palette.light),
            dark=_scheme(palette.dark),
        )
        for palette in theming.PALETTES
    ]


@router.patch("/households/{household_id}", response_model=HouseholdOut)
def update_household(
    body: HouseholdUpdate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> HouseholdOut:
    """Change a household's settings, including what colour it wears."""
    with batch(session, kind=BatchKind.admin, actor_id=user.id):
        household_service.update_household(
            session,
            household,
            name=body.name,
            base_currency=body.base_currency,
            date_format=body.date_format,
            note=None if body.clear_note else (body.note if body.note is not None else UNSET),
            theme=body.theme,
            accent=None if body.clear_accent else (body.accent if body.accent is not None else UNSET),
            receipts_keep_original=body.receipts_keep_original,
        )
    return _seen(household, session)


@router.get("/households/{household_id}", response_model=HouseholdOut)
def get_household(household: CurrentHousehold, session: SessionDep) -> HouseholdOut:
    return _seen(household, session)


# --------------------------------------------------------------------------- #
# What a household holds
# --------------------------------------------------------------------------- #
#
# These two models live here rather than in `app/schemas.py` on purpose: they
# are the shape of exactly one route and nothing else validates against them.


class CurrencyRowsOut(BaseModel):
    """One currency this household keeps accounts in, and how much sits in it.

    Counts, never a figure. `services/insights.py` has the long version: this
    ledger does not convert, so an amount at household level would either be
    wrong or would need a rate that does not exist anywhere in this schema.
    """

    currency: str
    accounts: int
    transactions: int


class HouseholdStatsOut(BaseModel):
    """The household page's numbers. Every one of them counted in SQL."""

    members: int
    accounts: int
    accounts_closed: int
    transactions: int
    transactions_uncleared: int
    first_transaction: Date | None = None
    last_transaction: Date | None = None
    receipts: int
    receipts_unattached: int
    payees: int
    payee_rules: int
    payee_rules_enabled: int
    categories: int
    categories_archived: int
    category_groups: int
    reconciliations: int
    currencies: list[CurrencyRowsOut]
    #: With the flag and the name already resolved, for the same reason
    #: `/countries` is a route rather than a bundled table.
    countries: list[CountryOut]


@router.get("/households/{household_id}/stats", response_model=HouseholdStatsOut)
def household_stats(household: CurrentHousehold, session: SessionDep) -> HouseholdStatsOut:
    """How much of everything this household holds.

    Scoped through `CurrentHousehold`, so a household the caller is not in is a
    404 here exactly as it is everywhere else -- and a count that quietly
    reached past the household would be a leak of another ledger's size, which
    is why `tests/test_household_stats.py` has two households in it.
    """
    figures = household_service.stats(session, household.id)
    return HouseholdStatsOut(
        # `asdict` rather than `vars`: the dataclass has `slots=True`, so it has
        # no `__dict__` to read.
        **{
            key: value
            for key, value in asdict(figures).items()
            if key not in ("currencies", "countries")
        },
        currencies=[
            CurrencyRowsOut(
                currency=one.currency, accounts=one.accounts, transactions=one.transactions
            )
            for one in figures.currencies
        ],
        countries=[
            CountryOut(code=code, name=countries.BY_CODE.get(code, code), flag=countries.flag(code))
            for code in figures.countries
        ],
    )


@router.get("/households/{household_id}/members", response_model=list[MemberOut])
def list_members(household: CurrentHousehold, session: SessionDep) -> list[MemberOut]:
    rows = household_service.members(session, household.id)
    users = {
        u.id: u
        for u in session.execute(
            select(User).where(User.id.in_([r.user_id for r in rows]))
        ).scalars()
    }
    # One grouped read for everybody, not one per member. A household has few
    # members, but the query behind each one walks the whole audit log for the
    # household and that is not a thing to do N times.
    logged = household_service.transactions_logged(session, household.id)
    nothing = household_service.Logged(total=0, by_agent=0)
    return [
        MemberOut(
            user_id=row.user_id,
            display_name=users[row.user_id].display_name,
            email=users[row.user_id].email,
            role=users[row.user_id].role,
            added_at=row.added_at,
            transactions_logged=logged.get(row.user_id, nothing).total,
            transactions_by_agent=logged.get(row.user_id, nothing).by_agent,
        )
        for row in rows
        if row.user_id in users
    ]


@router.post("/households/{household_id}/members", response_model=MemberOut, status_code=201)
def add_member(
    body: AddMember, household: CurrentHousehold, session: SessionDep, owner: OwnerOnly
) -> MemberOut:
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        row = household_service.add_member(
            session, household_id=household.id, user_id=body.user_id, added_by=owner
        )
    added = session.get(User, row.user_id)
    return MemberOut(
        user_id=row.user_id,
        display_name=added.display_name,
        email=added.email,
        role=added.role,
        added_at=row.added_at,
    )


@router.delete("/households/{household_id}/members/{user_id}", status_code=204)
def remove_member(
    user_id: str, household: CurrentHousehold, session: SessionDep, owner: OwnerOnly
) -> None:
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        household_service.remove_member(
            session, household_id=household.id, user_id=user_id, removed_by=owner
        )


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


def _account_out(
    account: Account,
    figures: dict[str, int] | None = None,
    activity: account_service.Activity | None = None,
    opening: account_service.Opening | None = None,
) -> AccountOut:
    out = AccountOut.model_validate(account)
    out.flag = countries.flag(account.country)
    out.is_liability = account.type.is_liability
    if figures:
        out.balance = figures["balance"]
        out.cleared = figures["cleared"]
        out.uncleared = figures["uncleared"]
    if activity is not None:
        out.transaction_count = activity.transactions
        out.oldest_transaction = activity.oldest
        out.newest_transaction = activity.newest
    if opening is not None:
        out.opening_balance = opening.amount
        out.opening_date = opening.date
        out.opening_transaction_id = opening.transaction_id
    out.warnings = account_service.opening_warnings(
        opening, activity.oldest if activity is not None else None
    )
    return out


def _one_account_out(session: Session, account: Account) -> AccountOut:
    """Everything `_account_out` takes, read for one account."""
    return _account_out(
        account,
        account_service.balances(session, account.id),
        account_service.activity(session, account.id),
        account_service.opening(session, account.id),
    )


@router.get("/households/{household_id}/accounts", response_model=list[AccountOut])
def list_accounts(
    household: CurrentHousehold, session: SessionDep, include_closed: bool = False
) -> list[AccountOut]:
    rows = account_service.list_for_household(session, household.id, include_closed=include_closed)
    figures = account_service.balances_for_household(session, household.id)
    activity = account_service.activity_for_household(session, household.id)
    openings = account_service.opening_for_household(session, household.id)
    return [
        _account_out(
            account, figures.get(account.id), activity.get(account.id), openings.get(account.id)
        )
        for account in rows
    ]


@router.post("/households/{household_id}/accounts", response_model=AccountOut, status_code=201)
def create_account(
    body: AccountCreate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> AccountOut:
    with batch(session, kind=BatchKind.admin, actor_id=user.id, household_id=household.id):
        account = account_service.create_account(
            session,
            household=household,
            name=body.name,
            type=body.type,
            currency=body.currency,
            note=body.note,
            institution=body.institution,
            country=body.country,
            opening_balance=body.opening_balance,
            opening_date=body.opening_date,
        )
    return _one_account_out(session, account)


#: One sentence for both size checks, so they cannot drift apart.
ACCOUNTS_FILE_TOO_BIG = (
    f"that file is larger than a list of accounts is "
    f"({account_import.MAX_ACCOUNT_CSV_BYTES // 1024} KB) -- it may be a statement rather than one"
)


@router.get("/households/{household_id}/accounts/import-template.csv", response_class=PlainTextResponse)
def account_import_template(household: CurrentHousehold) -> PlainTextResponse:
    """The columns an accounts file may have, as a CSV with nothing under them (#146).

    Household-scoped although it says nothing about the household, so that a
    stranger's id answers 404 here exactly as it does everywhere else.
    """
    return PlainTextResponse(
        account_import.template(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{account_import.TEMPLATE_FILENAME}"'
        },
    )


@router.post("/households/{household_id}/accounts/import", response_model=AccountImportOut)
async def import_accounts(
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
    file: Annotated[UploadFile, File()],
    dry_run: Annotated[bool, Form()] = True,
) -> AccountImportOut:
    """Many accounts from one CSV file, all or none (#146).

    **Any member may run it, not only the owner** -- `CurrentUser`, decided
    2026-10-07 (#115). It only adds accounts, every one is a row in the audit
    log under one batch, and Undo in History takes the whole file back. The
    one-time import is owner-only because it brings a whole ledger in -- other
    people's transactions, categories and payees -- which this does not.

    `dry_run` defaults to true: the file is read and every row run through
    the real service, and then nothing is kept -- 200 with a verdict per row.
    Sent again with `dry_run` false it is kept -- 201 -- unless any row has a
    problem, in which case it is 422 and nothing is written.
    """
    refuse_declared_size(file, account_import.MAX_ACCOUNT_CSV_BYTES, ACCOUNTS_FILE_TOO_BIG)
    raw = await read_capped(file, account_import.MAX_ACCOUNT_CSV_BYTES, ACCOUNTS_FILE_TOO_BIG)
    # The rows go through the real account service, identifier checks and
    # all -- the database half, off the loop. Issue #233.
    outcome = await run_in_threadpool(
        account_import.import_accounts,
        session,
        household=household,
        actor_id=user.id,
        raw=raw,
        filename=file.filename,
        dry_run=dry_run,
    )
    response.status_code = 200 if outcome.dry_run else 201
    return AccountImportOut(
        dry_run=outcome.dry_run,
        created=outcome.created,
        rows=[AccountImportRow.model_validate(asdict(row)) for row in outcome.rows],
    )


@router.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: str, session: SessionDep, user: CurrentUser) -> AccountOut:
    account = load_for(session, user, Account, account_id)
    return _one_account_out(session, account)


@router.patch("/accounts/{account_id}", response_model=AccountOut)
def update_account(
    account_id: str, body: AccountUpdate, session: SessionDep, user: CurrentUser
) -> AccountOut:
    account = load_for(session, user, Account, account_id)
    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=account.household_id
    ):
        if body.name is not None:
            account_service.rename(session, account, body.name)
        # Trimmed, and a blank one stored as null -- the same as clearing it.
        if body.clear_note:
            account.note = None
        elif body.note is not None:
            account.note = account_service.free_text(body.note)
        if body.clear_institution:
            account.institution = None
        elif body.institution is not None:
            account.institution = account_service.free_text(body.institution)
        if body.clear_country:
            account.country = None
        elif body.country is not None:
            account.country = countries.check(body.country)
        if body.closed is not None:
            account_service.set_closed(session, account, body.closed)
        if body.sort_order is not None:
            account.sort_order = body.sort_order
        if body.clear_statement_product:
            account.statement_product = None
        elif body.statement_product is not None:
            account.statement_product = body.statement_product.strip()
        account_service.set_opening(
            session, account, amount=body.opening_balance, when=body.opening_date
        )
        session.flush()
    return _one_account_out(session, account)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


@router.get("/accounts/{account_id}/reconciliation", response_model=ReconcileWorksheet)
def reconcile_worksheet(
    account_id: str, session: SessionDep, user: CurrentUser, until: Date | None = None
) -> ReconcileWorksheet:
    """What is still unaccounted for on this account.

    `until` is the statement's closing date. A row dated after it cannot be on
    that statement, so offering it would only invite ticking one by mistake.
    Sent without one, the sheet stops 45 days after the last statement, when
    there is one (#237) -- the screen always sends it.
    """
    account = load_for(session, user, Account, account_id)
    sheet = reconciling.worksheet(session, account, until=until)
    return ReconcileWorksheet(
        account_id=sheet.account_id,
        currency=sheet.currency,
        locked_balance=sheet.locked_balance,
        last_statement_date=sheet.last_statement_date,
        last_statement_balance=sheet.last_statement_balance,
        candidates=[ReconcileCandidate(**asdict(one)) for one in sheet.candidates],
    )


@router.post(
    "/accounts/{account_id}/reconciliation", response_model=ReconciliationOut, status_code=201
)
def reconcile_account(
    account_id: str, body: ReconcileRequest, session: SessionDep, user: CurrentUser
) -> ReconciliationOut:
    """Prove this account against a statement, and lock what it covered.

    One batch, so History shows it as a single act and undoing it unlocks every
    row it locked. The batch id goes onto the record, which is what makes "undo
    this reconciliation" the same button as undoing an import.
    """
    account = load_for(session, user, Account, account_id)
    with batch(
        session,
        kind=BatchKind.reconciled,
        actor_id=user.id,
        household_id=account.household_id,
    ) as open_batch:
        record = reconciling.reconcile(
            session,
            account,
            statement_date=body.statement_date,
            statement_balance=body.statement_balance,
            transaction_ids=body.transaction_ids,
            batch_id=open_batch.id,
        )
    return ReconciliationOut.model_validate(record)


@router.get("/accounts/{account_id}/reconciliations", response_model=list[ReconciliationOut])
def list_reconciliations(
    account_id: str, session: SessionDep, user: CurrentUser
) -> list[ReconciliationOut]:
    """Every statement this account has been proved against, newest first."""
    account = load_for(session, user, Account, account_id)
    return [
        ReconciliationOut.model_validate(one) for one in reconciling.history(session, account.id)
    ]
