"""Linking rows that already exist as one transfer, and finding them (#70, #71).

Written from the patterns in the real ledger -- account-to-account moves that
name the other account, card payments, a savings pocket topped up from
checking, a transfer and a card payment of one identical amount on one day --
with every number made up.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import Conflict, ValidationError
from app.models import (
    Account,
    AccountType,
    BatchKind,
    BatchStatus,
    ImportOutcome,
    ReimbursementState,
    Transaction,
)
from app.services import identifiers, importing, reporting, transfers
from app.services import transactions as txn_service
from statements import sniffing
from tests.conftest import HEADERS, _setup_owner

DAY = date(2026, 3, 24)


@pytest.fixture()
def ledger(session, owner, household, accounts):
    """Current, a savings account, a card, and the GBP account from the fixture."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        made = {
            "current": Account(household_id=household.id, name="Current", type=AccountType.checking, currency="EUR"),
            "saver": Account(household_id=household.id, name="Saver", type=AccountType.savings, currency="EUR"),
            "card": Account(household_id=household.id, name="Card", type=AccountType.credit_card, currency="EUR"),
            "pounds": accounts["pounds"],
        }
        session.add_all([made["current"], made["saver"], made["card"]])
        session.flush()
        for kind, value, account in (
            ("number", "11112222", made["current"]),
            ("number", "33334444", made["saver"]),
            ("alias", "CARDBRAND", made["card"]),
        ):
            identifiers.add(session, household_id=household.id, kind=kind, value=value, account=account)
        identifiers.add(session, household_id=household.id, kind="holder", value="DOE JANE", account=None)
    return made


def _row(session, owner, household, account, amount, words, when=DAY):
    """An imported row: the bank's words kept, as the importer keeps them."""
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        return txn_service.create(
            session, account=account, date=when, amount=amount, payee=None,
            import_payee_original=words, import_id=f"T:{account.id[:6]}:{amount}:{when}:{words[:8]}",
        )


def _link(session, owner, household, a, b):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as made:
        transfers.link(session, a, b)
    return made


# --------------------------------------------------------------------------- #
# Linking by hand
# --------------------------------------------------------------------------- #


def test_two_imported_rows_become_one_transfer_and_leave_the_report(
    session, owner, household, ledger
):
    out = _row(session, owner, household, ledger["saver"], -200000, "TO A/C 11112222")
    into = _row(session, owner, household, ledger["current"], 200000, "FROM A/C 33334444", DAY + timedelta(days=1))
    flows = lambda: {t.id for t in session.execute(reporting.flow_rows(household.id)).scalars()}  # noqa: E731
    assert {out.id, into.id} <= flows()

    _link(session, owner, household, into, out)

    assert out.transfer_transaction_id == into.id and into.transfer_transaction_id == out.id
    assert out.transfer_account_id == ledger["current"].id
    assert out.payee.name == "Transfer : Current" and into.payee.name == "Transfer : Saver"
    # Each keeps its own bank's date and words.
    assert (out.date, into.date) == (DAY, DAY + timedelta(days=1))
    assert into.import_payee_original == "FROM A/C 33334444"
    assert not ({out.id, into.id} & flows())


@pytest.mark.parametrize(
    ("first", "second", "message"),
    [
        (("current", -100), ("current", 100), "same account"),
        (("current", -100), ("saver", 90), "same amount"),
        (("current", -100), ("saver", -100), "money out of one account"),
    ],
)
def test_what_cannot_be_a_transfer_is_refused(session, owner, household, ledger, first, second, message):
    a = _row(session, owner, household, ledger[first[0]], first[1], "one")
    b = _row(session, owner, household, ledger[second[0]], second[1], "two")
    with pytest.raises(ValidationError, match=message):
        transfers.link(session, a, b)


def test_a_leg_already_linked_cannot_be_linked_again(session, owner, household, ledger):
    a = _row(session, owner, household, ledger["current"], -100, "one")
    b = _row(session, owner, household, ledger["saver"], 100, "two")
    c = _row(session, owner, household, ledger["card"], 100, "three")
    _link(session, owner, household, a, b)
    with pytest.raises(Conflict, match="already a transfer"):
        transfers.link(session, a, c)


def test_unlinking_gives_each_row_back_its_bank_payee(session, owner, household, ledger):
    a = _row(session, owner, household, ledger["current"], -100, "TO A/C 33334444")
    b = _row(session, owner, household, ledger["saver"], 100, "FROM A/C 11112222")
    _link(session, owner, household, a, b)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        transfers.unlink(session, b)
    assert a.transfer_transaction_id is None and b.transfer_account_id is None
    assert (a.payee.name, b.payee.name) == ("TO A/C 33334444", "FROM A/C 11112222")


def test_undoing_a_link_puts_both_rows_back(session, owner, household, ledger):
    a = _row(session, owner, household, ledger["current"], -100, "one")
    b = _row(session, owner, household, ledger["saver"], 100, "two")
    made = _link(session, owner, household, a, b)
    undo_batch(session, made.id, actor_id=owner.id)
    session.expire_all()
    assert session.get(Transaction, a.id).transfer_transaction_id is None
    assert session.get(Transaction, b.id).transfer_transaction_id is None


def test_a_currency_transfer_links_by_hand_and_stores_the_rate(session, owner, household, ledger):
    """Issue #71: amounts that differ by a rate nobody printed."""
    out = _row(session, owner, household, ledger["pounds"], -10000, "TO EUR ACCOUNT")
    into = _row(session, owner, household, ledger["current"], 11500, "FROM GBP ACCOUNT", DAY + timedelta(days=2))
    _link(session, owner, household, out, into)
    assert out.transfer_fx_rate == into.transfer_fx_rate == "1.15"
    flows = {t.id for t in session.execute(reporting.flow_rows(household.id)).scalars()}
    assert not ({out.id, into.id} & flows)
    # And the matcher never proposes it by amount: there is no amount to match.
    assert transfers.find(session, household.id).strong == []


# --------------------------------------------------------------------------- #
# Finding them
# --------------------------------------------------------------------------- #


def test_a_descriptor_naming_the_other_account_is_strong(session, owner, household, ledger):
    _row(session, owner, household, ledger["saver"], -200000, "TO A/C 11112222 JANE DOE/XP")
    _row(session, owner, household, ledger["current"], 200000, "FROM A/C 33334444")
    found = transfers.find(session, household.id)
    assert len(found.strong) == 1 and not found.suggested
    assert "names Current" in found.strong[0].why or "names Saver" in found.strong[0].why


def test_four_rows_of_one_amount_on_one_day_are_two_transfers_not_the_wrong_two(
    session, owner, household, ledger
):
    """Saver -> Current, then Current -> Card, all 5,000. By amount alone the
    saver could pair with the card, which is where the bogus pairs came from."""
    s_out = _row(session, owner, household, ledger["saver"], -500000, "TO A/C 11112222")
    c_in = _row(session, owner, household, ledger["current"], 500000, "FROM A/C 33334444")
    c_out = _row(session, owner, household, ledger["current"], -500000, "ACC-CARDBRAND 411111******0000")
    k_in = _row(session, owner, household, ledger["card"], 500000, "PAYMENT RECEIVED - THANK YOU")

    found = transfers.find(session, household.id)
    pairs = {(p.out_leg.id, p.in_leg.id) for p in found.strong}
    assert pairs == {(s_out.id, c_in.id), (c_out.id, k_in.id)}
    everything = pairs | {(p.out_leg.id, p.in_leg.id) for p in found.suggested}
    assert (s_out.id, k_in.id) not in everything


def test_amount_and_date_alone_is_only_a_suggestion(session, owner, household, ledger):
    _row(session, owner, household, ledger["current"], -4200, "SOMETHING")
    _row(session, owner, household, ledger["saver"], 4200, "SOMETHING ELSE")
    found = transfers.find(session, household.id)
    assert not found.strong and len(found.suggested) == 1


def test_two_strong_partners_equally_close_is_a_question_for_a_person(
    session, owner, household, ledger
):
    _row(session, owner, household, ledger["saver"], -1000, "TO A/C 11112222")
    _row(session, owner, household, ledger["current"], 1000, "FROM A/C 33334444", DAY - timedelta(days=1))
    _row(session, owner, household, ledger["current"], 1000, "FROM A/C 33334444", DAY + timedelta(days=1))
    found = transfers.find(session, household.id)
    assert not found.strong
    assert len(found.suggested) == 2
    assert all("more than one" in p.why for p in found.suggested)


def test_the_same_amount_moved_on_consecutive_days_pairs_day_with_day(
    session, owner, household, ledger
):
    """Two moves of one amount a day apart: each out has two strong partners,
    and the one on its own day is strictly the closest for both legs."""
    first = _row(session, owner, household, ledger["saver"], -1000, "TO A/C 11112222")
    second = _row(session, owner, household, ledger["saver"], -1000, "TO A/C 11112222", DAY + timedelta(days=1))
    one = _row(session, owner, household, ledger["current"], 1000, "FROM A/C 33334444")
    two = _row(session, owner, household, ledger["current"], 1000, "FROM A/C 33334444", DAY + timedelta(days=1))
    found = transfers.find(session, household.id)
    assert {(p.out_leg.id, p.in_leg.id) for p in found.strong} == {
        (first.id, one.id),
        (second.id, two.id),
    }
    assert not found.suggested


def test_the_window_is_five_days(session, owner, household, ledger):
    _row(session, owner, household, ledger["saver"], -7000, "TO A/C 11112222")
    _row(session, owner, household, ledger["current"], 7000, "FROM A/C 33334444", DAY + timedelta(days=5))
    _row(session, owner, household, ledger["saver"], -8000, "TO A/C 11112222")
    _row(session, owner, household, ledger["current"], 8000, "FROM A/C 33334444", DAY + timedelta(days=6))
    found = transfers.find(session, household.id)
    assert [p.out_leg.amount for p in found.strong] == [-7000]


def test_a_lane_linked_before_is_strong_evidence(session, owner, household, ledger):
    a = _row(session, owner, household, ledger["current"], -300, "ONE", DAY - timedelta(days=30))
    b = _row(session, owner, household, ledger["saver"], 300, "TWO", DAY - timedelta(days=30))
    _link(session, owner, household, a, b)
    _row(session, owner, household, ledger["current"], -900, "NOTHING USEFUL")
    _row(session, owner, household, ledger["saver"], 900, "NOTHING EITHER")
    found = transfers.find(session, household.id)
    assert len(found.strong) == 1 and "before" in found.strong[0].why


def test_own_money_with_no_other_side_yet_is_awaiting(session, owner, household, ledger):
    waiting = _row(session, owner, household, ledger["current"], 150000, "PAYMENT FROM DOE JANE")
    found = transfers.find(session, household.id)
    assert [t.id for t, _ in found.awaiting] == [waiting.id]


# --------------------------------------------------------------------------- #
# At import time
# --------------------------------------------------------------------------- #


def _import(session, owner, household, account, raw: bytes):
    sniffed = sniffing.sniff(raw)
    with batch(
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id,
        source={"filename": "s.csv", "sha256": importing.file_digest(raw), "account_id": account.id,
                "format": sniffed.format.describe()},
    ) as staged:
        lines = importing.stage_file(session, account=account, raw=raw, fmt=sniffed.format, batch_row=staged)
        staged.status = BatchStatus.preview
    from app.audit.batch import resume

    with resume(session, staged, status=BatchStatus.applied):
        result = importing.commit(session, batch_row=staged, account=account)
    return lines, result


def test_the_second_statement_links_to_the_first_whichever_arrives_first(
    session, owner, household, ledger
):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        identifiers.add(session, household_id=household.id, kind="alias", value="Saver pocket", account=ledger["saver"])
    checking = b"Date,Description,Amount\n2026-03-24,To Saver pocket,-100.00\n2026-03-25,Coffee,-3.00\n"
    pocket = b"Date,Description,Amount\n2026-03-24,Deposit from current,100.00\n"

    lines, result = _import(session, owner, household, ledger["current"], checking)
    assert result["transfers_linked"] == 0  # the pocket's side is not here yet
    waiting = next(line for line in lines if line.parsed and line.parsed["amount"] == -10000)
    assert "own money" in waiting.reason

    lines, result = _import(session, owner, household, ledger["saver"], pocket)
    assert result["transfers_linked"] == 1
    assert "a transfer with Current" in lines[0].reason
    legs = session.execute(
        select(Transaction).where(Transaction.transfer_transaction_id.is_not(None))
    ).scalars().all()
    assert sorted(t.amount for t in legs) == [-10000, 10000]


def test_a_suggested_pair_is_named_on_the_preview_and_not_linked(session, owner, household, ledger):
    _row(session, owner, household, ledger["saver"], -2500, "MOVE")
    raw = b"Date,Description,Amount\n2026-03-24,Incoming,25.00\n"
    lines, result = _import(session, owner, household, ledger["current"], raw)
    assert result["transfers_linked"] == 0
    assert "may be a transfer with Saver" in lines[0].reason
    assert lines[0].outcome is ImportOutcome.created


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def test_findings_link_and_unlink_over_http(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    base = f"/api/households/{house['id']}"
    a = client.post(f"{base}/accounts", json={"name": "A", "type": "checking"}, headers=HEADERS).json()
    b = client.post(f"{base}/accounts", json={"name": "B", "type": "savings"}, headers=HEADERS).json()
    out = client.post(
        f"{base}/transactions",
        json={"account_id": a["id"], "date": "2026-03-24", "amount": -5000, "payee_name": "Move"},
        headers=HEADERS,
    )
    assert out.status_code == 201, out.text
    into = client.post(
        f"{base}/transactions",
        json={"account_id": b["id"], "date": "2026-03-25", "amount": 5000, "payee_name": "Move in"},
        headers=HEADERS,
    ).json()
    found = client.get(f"{base}/transfers/findings").json()
    assert len(found["suggested"]) == 1 and found["strong"] == []

    linked = client.post(
        f"{base}/transfers/link",
        json={"pairs": [{"first_id": out.json()["id"], "second_id": into["id"]}]},
        headers=HEADERS,
    )
    assert linked.json() == {"linked": 1}
    assert client.get(f"{base}/transfers/findings").json()["suggested"] == []

    gone = client.post(f"/api/transactions/{into['id']}/unlink", headers=HEADERS)
    assert gone.status_code == 204
    # Unlinked is a person saying "not a transfer" (#131): not offered again.
    assert client.get(f"{base}/transfers/findings").json()["suggested"] == []


# --------------------------------------------------------------------------- #
# A work expense and its repayment are not a transfer (#131)
# --------------------------------------------------------------------------- #


def _flag(session, owner, household, txn, **kwargs):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn_service.set_reimbursement(session, txn, **kwargs)


def _offered(found) -> set[frozenset[str]]:
    return {
        frozenset((p.out_leg.id, p.in_leg.id)) for p in [*found.strong, *found.suggested]
    }


def test_a_flagged_purchase_and_its_equal_repayment_are_not_offered_as_a_transfer(
    session, owner, household, ledger
):
    """The real case: an employer repays a card purchase exactly, within days,
    into another of the household's accounts."""
    purchase = _row(session, owner, household, ledger["card"], -18900, "HOTEL CENTRAL")
    repaid = _row(
        session, owner, household, ledger["current"], 18900, "EXPENSES ACME", DAY + timedelta(days=3)
    )
    assert frozenset((purchase.id, repaid.id)) in _offered(transfers.find(session, household.id))

    _flag(session, owner, household, purchase, state=ReimbursementState.expected)
    assert _offered(transfers.find(session, household.id)) == set()
    # And the import-time question, asked about the repayment alone.
    assert _offered(transfers.find(session, household.id, among=[repaid])) == set()

    # Linked: the purchase is still flagged, and the repayment is a payment now.
    _flag(session, owner, household, purchase, settled_by=repaid)
    assert _offered(transfers.find(session, household.id)) == set()
    assert purchase.transfer_transaction_id is None and repaid.transfer_transaction_id is None


def test_a_payment_is_not_offered_against_any_other_row_either(session, owner, household, ledger):
    """Unflagged as it is, a payment is spoken for: it paid something back."""
    expense = _row(session, owner, household, ledger["card"], -6000, "TRAIN")
    payment = _row(session, owner, household, ledger["current"], 6000, "PAYMENT FROM DOE JANE")
    stranger = _row(session, owner, household, ledger["saver"], -6000, "TO A/C 11112222")
    _flag(session, owner, household, expense, settled_by=payment)

    found = transfers.find(session, household.id)
    assert _offered(found) == set()
    assert payment.id not in {t.id for t, _ in found.awaiting}
    assert _offered(transfers.find(session, household.id, among=[payment, stranger])) == set()


def test_linking_refuses_a_work_expense_and_a_payment(session, owner, household, ledger):
    flagged = _row(session, owner, household, ledger["card"], -4400, "TAXI")
    into = _row(session, owner, household, ledger["current"], 4400, "IN")
    _flag(session, owner, household, flagged, state=ReimbursementState.expected)
    with pytest.raises(Conflict, match="work expense"):
        _link(session, owner, household, flagged, into)
    session.refresh(flagged)
    session.refresh(into)
    assert flagged.transfer_transaction_id is None and into.transfer_transaction_id is None

    out = _row(session, owner, household, ledger["saver"], -4400, "OUT")
    _flag(session, owner, household, flagged, settled_by=into)
    with pytest.raises(Conflict, match="repaid a work expense"):
        _link(session, owner, household, out, into)
    session.refresh(out)
    session.refresh(into)
    assert out.transfer_transaction_id is None and into.transfer_transaction_id is None
    assert into.transfer_account_id is None
