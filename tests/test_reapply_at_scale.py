"""Re-applying payee rules on a ledger the size of a real one (#231).

The plan loaded every imported row as an ORM object and ran every rule
against each row's own copy of its descriptor, although the same descriptor
repeats thousands of times; Apply then worked the whole plan out a second
time, and handed every moving id to one `IN (...)`, which SQLite refuses
past 32,766.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import event, select

from app.audit.batch import batch
from app.models import BatchKind, MatchType, Payee, Transaction
from app.services import payees as payee_service
from app.services.payees import Move, ReapplyPlan, RuleSet

from .test_payee_rules_applied import HEADERS, _imported, _rule, world  # noqa: F401

DESCRIPTORS = ["WWW.AMAZON_ MARKETPLACE", "MERCADONA VALENCIA", "REPSOL ESTACION 12"]


@pytest.fixture()
def counted(monkeypatch):
    """How many times the rules were run against a string."""
    calls: list[str] = []
    real = RuleSet.resolve

    def resolve(self, raw_payee):
        calls.append(raw_payee)
        return real(self, raw_payee)

    monkeypatch.setattr(RuleSet, "resolve", resolve)
    return calls


def _ledger(session, owner, household, accounts, *, each: int) -> dict[str, Payee]:
    """`each` imported rows per descriptor, spread over two accounts, every
    one still carrying the payee the bank's words gave it."""
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        payees = {
            raw: payee_service.get_or_create(session, household.id, raw) for raw in DESCRIPTORS
        }
        amazon = payee_service.get_or_create(session, household.id, "Amazon")
        payee_service.create_rule(
            session, household_id=household.id, match_type=MatchType.contains,
            pattern="WWW.AMAZON", payee=amazon,
        )
        start = date(2026, 1, 1)
        for n in range(each):
            for raw in DESCRIPTORS:
                account = accounts["checking"] if n % 2 else accounts["card"]
                session.add(
                    Transaction(
                        household_id=household.id, account_id=account.id,
                        date=start + timedelta(days=n % 300), amount=-(100 + n),
                        payee_id=payees[raw].id, import_payee_original=raw,
                    )
                )
    session.commit()
    return {**payees, "Amazon": amazon}


def test_the_rules_run_once_per_distinct_descriptor_not_once_per_row(
    session, owner, household, accounts, counted
):
    made = _ledger(session, owner, household, accounts, each=1_000)

    plan = payee_service.plan_reapply(session, household.id)

    assert plan.considered == 3_000
    assert plan.changing == 1_000
    assert {(m.raw, m.to_payee_id) for m in plan.moves} == {
        ("WWW.AMAZON_ MARKETPLACE", made["Amazon"].id)
    }
    assert len(counted) <= len(DESCRIPTORS), f"{len(counted)} rule runs for 3 descriptors"


def test_a_plan_asked_for_again_with_nothing_written_is_not_worked_out_again(
    session, owner, household, accounts, counted
):
    made = _ledger(session, owner, household, accounts, each=50)

    first = payee_service.plan_reapply(session, household.id)
    runs = len(counted)
    again = payee_service.plan_reapply(session, household.id)
    assert again is first and len(counted) == runs

    # A different scope is a different plan.
    narrowed = payee_service.plan_reapply(session, household.id, account_id=accounts["card"].id)
    assert narrowed.considered == 75 and len(counted) > runs

    # Anything written in between -- here, one more Amazon row -- and the
    # plan is worked out afresh rather than yesterday's answer applied.
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        session.add(
            Transaction(
                household_id=household.id, account_id=accounts["checking"].id,
                date=date(2026, 12, 1), amount=-999,
                payee_id=made["WWW.AMAZON_ MARKETPLACE"].id,
                import_payee_original="WWW.AMAZON_ MARKETPLACE",
            )
        )
    session.commit()
    fresh = payee_service.plan_reapply(session, household.id)
    assert fresh is not first
    assert (fresh.considered, fresh.changing) == (151, 51)


def test_one_click_is_one_plan(world, counted):  # noqa: F811
    """Preview, then Apply: the rules run over each descriptor once in all."""
    for raw in ("WWW.AMAZON_ BX1EG1ST5", "WWW.AMAZON_ IJ6YX2P45", "Mercadona"):
        _imported(world, raw)
    _rule(world, pattern="WWW.AMAZON", payee="Amazon")
    base = f"/api/households/{world['house']}/payee-rules/reapply"

    counted.clear()
    preview = world["client"].post(f"{base}/preview", json={}, headers=HEADERS)
    assert preview.status_code == 200 and preview.json()["changing"] == 2
    assert len(counted) == 3

    did = world["client"].post(base, json={}, headers=HEADERS)
    assert did.status_code == 200 and did.json()["moved"] == 2
    assert len(counted) == 3, "Apply reused the preview's plan"


def test_a_plan_past_sqlites_variable_limit_applies_in_chunks(
    session, owner, household, accounts
):
    """40,000 moves: 32,766 bound variables is SQLite's ceiling, so one
    `IN (...)` of them all was `too many SQL variables` -- a 500 after the
    whole plan had been worked out."""
    made = _ledger(session, owner, household, accounts, each=2)
    amazon = made["Amazon"]
    real = [
        txn.id
        for txn in session.execute(
            select(Transaction).where(Transaction.import_payee_original == DESCRIPTORS[1])
        ).scalars()
    ]
    # Two rows that exist and 39,998 that do not: the size is what is under
    # test, and the rows that are not there cost nothing to not move.
    moves = [
        Move(transaction_id=txn_id, raw=DESCRIPTORS[1], from_name=DESCRIPTORS[1],
             to_payee_id=amazon.id, to_name="Amazon")
        for txn_id in real + [f"{n:032x}" for n in range(40_000 - len(real))]
    ]
    plan = ReapplyPlan(household_id=household.id, moves=moves, considered=40_000, orphaned=[])

    widest: list[int] = []

    def watch(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and " IN (" in statement:
            widest.append(len(parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", watch)
    try:
        with batch(session, kind=BatchKind.bulk_update, actor_id=owner.id,
                   household_id=household.id):
            moved = payee_service.apply_reapply(session, plan)
        session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", watch)

    assert moved == 2
    assert len(widest) == 80 and max(widest) <= 500
    landed = session.execute(
        select(Transaction.payee_id).where(Transaction.id.in_(real))
    ).scalars().all()
    assert landed == [amazon.id, amazon.id]


def test_which_rows_repaid_an_expense_is_asked_a_chunk_at_a_time(session, owner, household, accounts):
    """The same unchunked shape in `transfers._payments`, fed by an import's rows."""
    from app.services import transfers

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        payment = Transaction(
            household_id=household.id, account_id=accounts["checking"].id,
            date=date(2026, 2, 1), amount=5_000,
        )
        session.add(payment)
        session.flush()
        session.add(
            Transaction(
                household_id=household.id, account_id=accounts["card"].id,
                date=date(2026, 1, 20), amount=-5_000, reimbursed_by_id=payment.id,
            )
        )
    session.commit()

    ids = [f"{n:032x}" for n in range(40_000)] + [payment.id]
    assert transfers._payments(session, ids) == {payment.id}
