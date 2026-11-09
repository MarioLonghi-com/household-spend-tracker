"""Income vs Expense: the category group decides the side, and work
reimbursements are a block of their own (#372).

Every test asserts a figure. Two currencies and two households throughout:
the GBP rows and the other household's rows are written alongside, and each
answer is checked to hold exactly the EUR rows of this household.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import (
    Account,
    AccountType,
    Batch,
    BatchKind,
    Category,
    CategoryGroup,
    CategoryGroupRole,
    CategoryRole,
    ReimbursementState,
    Transaction,
)
from app.services import categories as category_service
from app.services import reporting as report_service
from tests.conftest import HEADERS, _setup_owner

JAN = date(2026, 1, 1)


def _tree(session, house, actor_id):
    """Income (Salary, Interest, Reimbursement) and two expense groups."""
    with batch(session, kind=BatchKind.admin, actor_id=actor_id, household_id=house.id):
        income = CategoryGroup(household_id=house.id, name="Income", role=CategoryGroupRole.income)
        everyday = CategoryGroup(household_id=house.id, name="Everyday")
        non_monthly = CategoryGroup(household_id=house.id, name="Non-Monthly")
        session.add_all([income, everyday, non_monthly])
        session.flush()
        made = {
            "income": income,
            "everyday": everyday,
            "non_monthly": non_monthly,
            "salary": Category(household_id=house.id, group_id=income.id, name="Salary"),
            "interest": Category(household_id=house.id, group_id=income.id, name="Interest"),
            "reimbursement": Category(
                household_id=house.id,
                group_id=income.id,
                name="Reimbursement",
                role=CategoryRole.reimbursement,
            ),
            "groceries": Category(household_id=house.id, group_id=everyday.id, name="Groceries"),
            "hotel": Category(household_id=house.id, group_id=non_monthly.id, name="Hotel"),
        }
        session.add_all([v for k, v in made.items() if isinstance(v, Category)])
    return made


@pytest.fixture()
def tree(session, household, owner):
    return _tree(session, household, owner.id)


def _add(session, house, actor_id, account, day, amount, category=None, **extra):
    with batch(session, kind=BatchKind.manual, actor_id=actor_id, household_id=house.id):
        txn = Transaction(
            household_id=house.id,
            account_id=account.id,
            date=date(2026, 1, day),
            amount=amount,
            category_id=category.id if category else None,
            **extra,
        )
        session.add(txn)
    return txn


def _flag(session, house, actor_id, txn, *, paid_by=None, state=ReimbursementState.expected):
    with batch(session, kind=BatchKind.manual, actor_id=actor_id, household_id=house.id):
        txn.reimbursement = state
        txn.reimbursed_by_id = paid_by.id if paid_by else None


def _report(session, house, **kw):
    kw.setdefault("currency", "EUR")
    kw.setdefault("since", JAN)
    kw.setdefault("until", date(2026, 1, 31))
    return report_service.income_expense(session, house.id, **kw)


def _rows(section) -> dict[str, int]:
    return {row.name: row.total_minor for row in section.rows}


def _worked_example(session, house, actor_id, tree, eur, card):
    """The issue's worked example, one month, EUR."""
    add = lambda *a, **k: _add(session, house, actor_id, *a, **k)  # noqa: E731
    add(eur, 25, 300_000, tree["salary"])
    add(eur, 31, 500, tree["interest"])
    add(card, 3, -40_000, tree["groceries"])
    add(card, 9, 2_000, tree["groceries"])
    add(card, 10, -15_000, tree["hotel"])
    add(card, 12, 15_000, tree["hotel"])
    work_hotel = add(card, 14, -30_000, tree["hotel"])
    repayment = add(eur, 28, 30_000, tree["reimbursement"])
    add(eur, 29, 5_500, tree["reimbursement"])
    taxi = add(card, 20, -4_000)
    _flag(session, house, actor_id, work_hotel, paid_by=repayment)
    _flag(session, house, actor_id, taxi)
    return {"work_hotel": work_hotel, "repayment": repayment, "taxi": taxi}


@pytest.fixture()
def elsewhere(session, other_household, member):
    """The other household, with the same example in its own ledger and its
    own tree -- so a leak into this household's answer would change a figure."""
    with batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id):
        eur = Account(
            household_id=other_household.id, name="Theirs", type=AccountType.checking,
            currency="EUR",
        )
        card = Account(
            household_id=other_household.id, name="Their card", type=AccountType.credit_card,
            currency="EUR",
        )
        session.add_all([eur, card])
    theirs = _tree(session, other_household, member.id)
    _worked_example(session, other_household, member.id, theirs, eur, card)
    _add(session, other_household, member.id, card, 15, -99_999, theirs["groceries"])
    return theirs


def test_the_worked_example(session, household, owner, accounts, tree, elsewhere):
    """The figures in #372, exactly -- with a GBP month and another household beside it."""
    _worked_example(session, household, owner.id, tree, accounts["checking"], accounts["card"])
    _add(session, household, owner.id, accounts["pounds"], 5, -7_700, tree["groceries"])
    _add(session, household, owner.id, accounts["pounds"], 6, 9_100, tree["reimbursement"])

    answer = _report(session, household)

    assert answer["income"].total_minor == 300_500
    assert _rows(answer["income"]) == {"Salary": 300_000, "Interest": 500}
    assert _rows(answer["expense"]) == {"Groceries": -38_000, "Hotel": 0}
    assert answer["expense"].total_minor == -38_000
    assert answer["net_total_minor"] == 262_500
    assert answer["net_by_month"] == {"2026-01": 262_500}
    block = answer["reimbursements"]
    assert (block["income"].total_minor, block["income"].count) == (35_500, 2)
    assert (block["spent"].total_minor, block["spent"].count) == (-30_000, 1)
    assert (block["pending"].total_minor, block["pending"].count) == (-4_000, 1)
    assert answer["has_income_group"] is True

    pounds = _report(session, household, currency="GBP")
    assert pounds["expense"].total_minor == -7_700
    assert pounds["income"].total_minor == 0
    assert pounds["reimbursements"]["income"].total_minor == 9_100
    assert pounds["net_total_minor"] == -7_700


def test_a_flagged_row_in_the_reimbursement_category_is_rule_one(
    session, household, owner, accounts, tree
):
    """Rules apply in order: the category wins over the flag."""
    odd = _add(session, household, owner.id, accounts["card"], 4, -1_500, tree["reimbursement"])
    _flag(session, household, owner.id, odd)

    block = _report(session, household)["reimbursements"]

    assert block["income"].total_minor == -1_500
    assert block["pending"].total_minor == 0
    assert block["spent"].total_minor == 0


def test_a_repaid_expense_is_the_spent_line_and_an_unpaid_one_is_pending_only(
    session, household, owner, accounts, tree
):
    """Rules 2 and 3, and pending is in neither expenses nor the spent line."""
    salary = _add(session, household, owner.id, accounts["checking"], 25, 200_000, tree["salary"])
    hotel = _add(session, household, owner.id, accounts["card"], 7, -12_000, tree["hotel"])
    taxi = _add(session, household, owner.id, accounts["card"], 8, -2_500, tree["hotel"])
    _flag(session, household, owner.id, hotel, paid_by=salary)
    _flag(session, household, owner.id, taxi)

    answer = _report(session, household)

    assert answer["reimbursements"]["spent"].total_minor == -12_000
    assert answer["reimbursements"]["pending"].total_minor == -2_500
    assert answer["reimbursements"]["pending"].by_month == {"2026-01": -2_500}
    assert answer["expense"].total_minor == 0
    assert answer["expense"].rows == []


def test_a_payment_categorised_as_salary_stays_in_salary(
    session, household, owner, accounts, tree
):
    """A salary that also carried a claim's repayment is not dropped from Salary."""
    salary = _add(session, household, owner.id, accounts["checking"], 25, 310_000, tree["salary"])
    hotel = _add(session, household, owner.id, accounts["card"], 7, -10_000, tree["hotel"])
    _flag(session, household, owner.id, hotel, paid_by=salary)

    answer = _report(session, household)

    assert _rows(answer["income"]) == {"Salary": 310_000}
    assert answer["reimbursements"]["income"].total_minor == 0
    assert answer["reimbursements"]["spent"].total_minor == -10_000


def test_an_unlinked_reimbursement_row_is_in_the_block_and_not_in_income(
    session, household, owner, accounts, tree
):
    _add(session, household, owner.id, accounts["checking"], 25, 200_000, tree["salary"])
    _add(session, household, owner.id, accounts["checking"], 26, 5_500, tree["reimbursement"])

    answer = _report(session, household)

    assert answer["income"].total_minor == 200_000
    assert "Reimbursement" not in _rows(answer["income"])
    assert answer["reimbursements"]["income"].total_minor == 5_500
    assert answer["net_total_minor"] == 200_000


def test_a_positive_row_in_an_expense_category_reduces_it(
    session, household, owner, accounts, tree
):
    _add(session, household, owner.id, accounts["card"], 3, -6_000, tree["hotel"])
    _add(session, household, owner.id, accounts["card"], 9, 2_000, tree["hotel"])
    _add(session, household, owner.id, accounts["card"], 10, 1_200)

    answer = _report(session, household)

    assert _rows(answer["expense"]) == {"Hotel": -4_000, "Uncategorised": 1_200}
    assert answer["income"].rows == []
    assert answer["income"].total_minor == 0


def test_a_negative_row_in_an_income_category_reduces_income(
    session, household, owner, accounts, tree
):
    _add(session, household, owner.id, accounts["checking"], 25, 200_000, tree["salary"])
    _add(session, household, owner.id, accounts["checking"], 27, -15_000, tree["salary"])

    answer = _report(session, household)

    assert _rows(answer["income"]) == {"Salary": 185_000}
    assert answer["expense"].total_minor == 0


def test_a_written_off_row_stays_in_expenses(session, household, owner, accounts, tree):
    dinner = _add(session, household, owner.id, accounts["card"], 12, -6_000, tree["groceries"])
    _flag(session, household, owner.id, dinner, state=ReimbursementState.written_off)

    answer = _report(session, household)

    assert _rows(answer["expense"]) == {"Groceries": -6_000}
    assert answer["excluded"]["reimbursements"] == 0


def test_net_ignores_all_three_reimbursement_lines(session, household, owner, accounts, tree):
    _add(session, household, owner.id, accounts["checking"], 2, 100_000, tree["salary"])
    _add(session, household, owner.id, accounts["card"], 3, -20_000, tree["groceries"])
    before = _report(session, household)["net_total_minor"]

    back = _add(session, household, owner.id, accounts["checking"], 20, 9_000, tree["reimbursement"])
    hotel = _add(session, household, owner.id, accounts["card"], 4, -9_000, tree["hotel"])
    taxi = _add(session, household, owner.id, accounts["card"], 5, -3_000, tree["hotel"])
    _flag(session, household, owner.id, hotel, paid_by=back)
    _flag(session, household, owner.id, taxi)

    after = _report(session, household)
    assert before == 80_000
    assert after["net_total_minor"] == 80_000
    assert after["net_by_month"]["2026-01"] == 80_000
    assert after["reimbursements"]["income"].total_minor == 9_000


def test_renaming_the_income_group_or_the_reimbursement_category_changes_nothing(
    session, household, owner, accounts, tree
):
    _worked_example(session, household, owner.id, tree, accounts["checking"], accounts["card"])
    before = _report(session, household)

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        category_service.rename_group(session, tree["income"], "Ingresos")
        category_service.update_category(session, tree["reimbursement"], name="Reembolsos")

    after = _report(session, household)
    assert after["income"].total_minor == before["income"].total_minor == 300_500
    assert after["reimbursements"]["income"].total_minor == 35_500
    assert after["net_total_minor"] == before["net_total_minor"]


def test_moving_the_roles_moves_the_rows(session, household, owner, accounts, tree):
    """The marker decides, so moving it moves the side; undo moves it back."""
    _add(session, household, owner.id, accounts["checking"], 25, 200_000, tree["salary"])
    _add(session, household, owner.id, accounts["card"], 3, -7_000, tree["groceries"])

    with batch(
        session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
    ) as moved:
        category_service.set_group_role(session, tree["everyday"], CategoryGroupRole.income)
    session.commit()

    assert tree["income"].role is None, "the role moved rather than being copied"
    answer = _report(session, household)
    assert _rows(answer["income"]) == {"Groceries": -7_000}
    assert _rows(answer["expense"]) == {"Salary": 200_000}

    undo_batch(session, moved.id, actor_id=owner.id)
    session.commit()
    session.expire_all()
    roles = dict(
        session.execute(
            select(CategoryGroup.name, CategoryGroup.role).where(
                CategoryGroup.household_id == household.id
            )
        ).all()
    )
    assert roles == {"Income": CategoryGroupRole.income, "Everyday": None, "Non-Monthly": None}
    assert _rows(_report(session, household)["income"]) == {"Salary": 200_000}


def test_with_no_income_group_every_row_is_an_expense_and_it_says_so(
    session, household, owner, accounts, tree
):
    _add(session, household, owner.id, accounts["checking"], 25, 200_000, tree["salary"])
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        category_service.set_group_role(session, tree["income"], None)

    answer = _report(session, household)

    assert answer["has_income_group"] is False
    assert answer["income"].rows == []
    assert _rows(answer["expense"]) == {"Salary": 200_000}


def test_groups_are_subtotalled_and_ordered_by_spend(session, household, owner, accounts, tree):
    _add(session, household, owner.id, accounts["card"], 3, -4_000, tree["groceries"])
    _add(session, household, owner.id, accounts["card"], 4, -9_000, tree["hotel"])
    _add(session, household, owner.id, accounts["card"], 5, -12_000)

    expense = _report(session, household)["expense"]

    assert [(g.name, g.total_minor, g.count) for g in expense.groups] == [
        ("Non-Monthly", -9_000, 1),
        ("Everyday", -4_000, 1),
    ]
    assert [row.name for row in expense.rows] == ["Hotel", "Groceries", "Uncategorised"]
    assert expense.total_minor == -25_000
    drilled = report_service.behind(
        session, household.id, currency="EUR", since=JAN, until=date(2026, 1, 31),
        direction="out", row_group_id=tree["non_monthly"].id,
    )
    assert drilled["total_minor"] == -9_000


def test_excluded_counts_add_up(session, household, owner, accounts, tree):
    """Every non-zero row in the window is on a side, in the block, or excluded."""
    _worked_example(session, household, owner.id, tree, accounts["checking"], accounts["card"])
    _add(session, household, owner.id, accounts["card"], 15, 0, tree["groceries"])
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        out = Transaction(
            household_id=household.id, account_id=accounts["checking"].id,
            date=date(2026, 1, 18), amount=-5_000, transfer_account_id=accounts["card"].id,
        )
        session.add(out)

    answer = _report(session, household)
    excluded = answer["excluded"]
    on_report = answer["income"].count + answer["expense"].count

    assert excluded == {"transfers": 1, "opening_balances": 0, "reimbursements": 4}
    assert on_report + sum(excluded.values()) == 11, "ten example rows and the transfer"


def test_the_block_reconciles_with_the_rows_behind_it(session, household, owner, accounts, tree):
    _worked_example(session, household, owner.id, tree, accounts["checking"], accounts["card"])
    window = dict(currency="EUR", since=JAN, until=date(2026, 1, 31))
    answer = report_service.income_expense(session, household.id, **window)

    for key, direction in (
        ("income", report_service.REIMBURSEMENT_INCOME),
        ("spent", report_service.REIMBURSEMENT_SPENT),
        ("pending", report_service.REIMBURSEMENT_PENDING),
    ):
        line = answer["reimbursements"][key]
        drilled = report_service.behind(session, household.id, direction=direction, **window)
        assert drilled["total_minor"] == line.total_minor, key
        assert drilled["count"] == line.count, key
    for name, direction in (("income", "in"), ("expense", "out")):
        drilled = report_service.behind(session, household.id, direction=direction, **window)
        assert drilled["total_minor"] == answer[name].total_minor
    net = report_service.behind(session, household.id, **window)
    assert net["total_minor"] == answer["net_total_minor"] == 262_500


def test_seeding_marks_the_income_group(session, household, owner):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        category_service.seed_defaults(session, household.id)
    marked = session.execute(
        select(CategoryGroup.name).where(
            CategoryGroup.household_id == household.id,
            CategoryGroup.role == CategoryGroupRole.income,
        )
    ).scalars().all()
    assert marked == ["Income"]


def test_interest_lands_in_the_group_carrying_the_role_whatever_it_is_called(
    session, household, owner, tree
):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        category_service.rename_group(session, tree["income"], "Ingresos")
        made = category_service.ensure(
            session, household.id, "Interest income", group_name="Income",
            group_role=CategoryGroupRole.income,
        )
    assert made.group_id == tree["income"].id
    assert session.execute(
        select(CategoryGroup).where(
            CategoryGroup.household_id == household.id, CategoryGroup.name == "Income"
        )
    ).first() is None


# --------------------------------------------------------------------------- #
# Over HTTP: the screen's route, the agent's, and moving a role
# --------------------------------------------------------------------------- #


def test_the_role_moves_over_http_and_another_household_is_404(client):

    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()["id"]
    other = client.post("/api/households", json={"name": "Away"}, headers=HEADERS).json()["id"]
    tree = client.get(f"/api/households/{house}/categories", headers=HEADERS).json()
    by_name = {group["name"]: group for group in tree}
    assert by_name["Income"]["role"] == "income", "the seeded group arrives marked"
    bills = by_name["Bills"]

    moved = client.patch(f"/api/category-groups/{bills['id']}", json={"role": "income"}, headers=HEADERS)
    assert moved.status_code == 200
    assert moved.json()["role"] == "income"
    assert moved.json()["name"] == "Bills"
    after = {g["name"]: g["role"] for g in client.get(f"/api/households/{house}/categories", headers=HEADERS).json()}
    assert after["Income"] is None and after["Bills"] == "income"
    # The other household's tree is untouched by the move.
    theirs = {g["name"]: g["role"] for g in client.get(f"/api/households/{other}/categories", headers=HEADERS).json()}
    assert theirs["Income"] == "income"

    salary = next(c for c in by_name["Income"]["categories"] if c["name"] == "Salary")
    marked = client.patch(f"/api/categories/{salary['id']}", json={"role": "reimbursement"}, headers=HEADERS)
    assert marked.json()["role"] == "reimbursement"
    renamed = client.patch(f"/api/categories/{salary['id']}", json={"name": "Wages"}, headers=HEADERS)
    assert renamed.json()["role"] == "reimbursement", "a field left out is left alone"

    assert client.get(
        f"/api/households/{'0' * 32}/reports/income-expense?currency=EUR", headers=HEADERS
    ).status_code == 404


def test_reading_the_report_writes_nothing_and_both_routes_agree(client):
    """The agent's route is the person's, figure for figure, block included."""
    from sqlalchemy import func
    from sqlalchemy.orm import Session

    from app.models import Household, User
    from app.services import agent_keys as key_service

    owner = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()["id"]
    with Session(client.app_module.db_engine, expire_on_commit=False) as s:
        user = s.get(User, owner["user"]["id"])
        home = s.get(Household, house)
        with batch(s, kind=BatchKind.manual, actor_id=user.id, household_id=house):
            eur = Account(household_id=house, name="Main", type=AccountType.checking, currency="EUR")
            card = Account(
                household_id=house, name="Card", type=AccountType.credit_card, currency="EUR"
            )
            s.add_all([eur, card])
        income = s.execute(
            select(CategoryGroup).where(
                CategoryGroup.household_id == house, CategoryGroup.role == CategoryGroupRole.income
            )
        ).scalar_one()
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=house):
            back = Category(
                household_id=house, group_id=income.id, name="Reimbursement",
                role=CategoryRole.reimbursement,
            )
            s.add(back)
        salary = s.execute(
            select(Category).where(Category.household_id == house, Category.name == "Salary")
        ).scalar_one()
        groceries = s.execute(
            select(Category).where(Category.household_id == house, Category.name == "Groceries")
        ).scalar_one()
        _add(s, home, user.id, eur, 25, 250_000, salary)
        _add(s, home, user.id, card, 3, -8_000, groceries)
        _add(s, home, user.id, card, 4, 1_000, groceries)
        _add(s, home, user.id, eur, 26, 4_400, back)
        taxi = _add(s, home, user.id, card, 6, -2_200)
        _flag(s, home, user.id, taxi)
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=house):
            _key, token = key_service.issue(s, user=user, household=home, label="reports")
        s.commit()

    def batches() -> int:
        with Session(client.app_module.db_engine) as own:
            return own.execute(select(func.count()).select_from(Batch)).scalar_one()

    params = {"currency": "EUR", "since": "2026-01-01", "until": "2026-01-31"}
    before = batches()
    human = client.get(
        f"/api/households/{house}/reports/income-expense", params=params, headers=HEADERS
    ).json()
    agent = client.get(
        f"/api/agent/v1/households/{house}/reports/income-expense",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert batches() == before
    assert agent == human
    assert human["income"]["total_minor"] == 250_000
    assert human["expense"]["total_minor"] == -7_000
    assert human["expense"]["groups"][0]["name"] == "Everyday"
    assert human["reimbursements"]["income"]["total_minor"] == 4_400
    assert human["reimbursements"]["pending"]["total_minor"] == -2_200
    assert human["net_total_minor"] == 243_000
    assert human["excluded"]["reimbursements"] == 2
