"""What counts as evidence that two rows are one transfer (#125, #127, #131).

Each section is a way the matcher was wrong on a real ledger, rebuilt with
made-up accounts, names and amounts:

- a row somebody has categorised is not a transfer, and was still paired and
  still sat on the waiting list (#125);
- two transfers of one amount on one day between overlapping accounts were a
  tie, though each row's words said who it was for (#127);
- a purchase and its exact reimbursement were linked as a transfer, and the
  wrong link then vouched for the next one (#131).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import ValidationError
from app.models import Account, AccountType, BatchKind, LinkSource, Transaction, TransferRejection
from app.services import categories as category_service
from app.services import identifiers, transfers
from app.services import payees as payee_service
from app.services import transactions as txn_service
from tests.conftest import HEADERS, _setup_owner
from tests.test_transfer_matching import DAY, _import, _link, _row


@pytest.fixture()
def ledger(session, owner, household, accounts):
    """The same accounts `test_transfer_matching` uses: Current, Saver and a
    card in EUR, the fixture's GBP savings, and their identifiers."""
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


def _categorise(session, owner, household, category, *rows):
    with batch(session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id):
        for row in rows:
            txn_service.update(session, row, category=category)


@pytest.fixture()
def salary(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        return category_service.ensure(session, household.id, "Salary", group_name="Income")


def _pairs(found):
    return {(p.out_leg.id, p.in_leg.id) for p in found.strong}, {
        (p.out_leg.id, p.in_leg.id) for p in found.suggested
    }


# --------------------------------------------------------------------------- #
# #125: a categorised row is not a transfer
# --------------------------------------------------------------------------- #


def test_a_categorised_row_is_never_offered_as_a_pair(session, owner, household, ledger, salary):
    out = _row(session, owner, household, ledger["current"], -4200, "SOMETHING")
    into = _row(session, owner, household, ledger["saver"], 4200, "SOMETHING ELSE")
    strong, suggested = _pairs(transfers.find(session, household.id))
    assert suggested == {(out.id, into.id)}  # uncategorised, they pair

    _categorise(session, owner, household, salary, into)
    found = transfers.find(session, household.id)
    assert not found.strong and not found.suggested
    # Nor when the question is narrowed to the other row, as an import asks it.
    narrowed = transfers.find(session, household.id, among=[out])
    assert not narrowed.strong and not narrowed.suggested

    _categorise(session, owner, household, None, into)
    assert _pairs(transfers.find(session, household.id))[1] == {(out.id, into.id)}


def test_a_categorised_row_naming_another_account_is_not_waiting(
    session, owner, household, ledger, salary
):
    """A payment to a household member's account that isn't tracked here: it
    names somebody of ours, and it is not a transfer."""
    paid = _row(session, owner, household, ledger["current"], -150000, "PAYMENT TO DOE JANE")
    also = _row(session, owner, household, ledger["saver"], 3300, "FROM A/C 11112222 INTEREST")
    found = transfers.find(session, household.id)
    assert {t.id for t, _ in found.awaiting} == {paid.id, also.id}

    _categorise(session, owner, household, salary, paid)
    found = transfers.find(session, household.id)
    assert [t.id for t, _ in found.awaiting] == [also.id]
    assert paid.category_id == salary.id

    _categorise(session, owner, household, None, paid)
    assert {t.id for t, _ in transfers.find(session, household.id).awaiting} == {paid.id, also.id}


@pytest.mark.parametrize("categorised", [False, True], ids=["uncategorised", "categorised"])
def test_a_categorised_row_is_not_auto_linked_at_import(
    session, owner, household, ledger, salary, categorised
):
    waiting = _row(session, owner, household, ledger["saver"], 2500, "FROM A/C 11112222")
    if categorised:
        _categorise(session, owner, household, salary, waiting)
    raw = b"Date,Description,Amount\n2026-03-24,TO A/C 33334444,-25.00\n"
    _, result = _import(session, owner, household, ledger["current"], raw)
    assert result["transfers_linked"] == (0 if categorised else 1)
    assert (waiting.transfer_transaction_id is None) is categorised


def test_revolut_pocket_moves_pair_once_the_salaries_are_categorised(
    session, owner, household, ledger, salary
):
    """The real case, in placeholders. Each checking "To <pocket>" out-leg had
    two in-legs of its amount on its day: the pocket's own "Deposit to", and a
    salary landing on another bank's checking account. Both lanes had links
    before, so each out-leg had two strong partners and went to a person."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        rv_chk = Account(household_id=household.id, name="RV-CHK", type=AccountType.checking, currency="EUR")
        rv_sav = Account(household_id=household.id, name="RV-SAV", type=AccountType.savings, currency="EUR")
        es_chk = Account(household_id=household.id, name="ES-CHK", type=AccountType.checking, currency="EUR")
        session.add_all([rv_chk, rv_sav, es_chk])
    # The lanes: each pair of accounts has had a transfer linked by a person.
    earlier = DAY - timedelta(days=40)
    for amount, destination in ((777, rv_sav), (888, es_chk)):
        _link(
            session, owner, household,
            _row(session, owner, household, rv_chk, -amount, "EARLIER OUT", earlier),
            _row(session, owner, household, destination, amount, "EARLIER IN", earlier),
        )

    day1, day2 = DAY, DAY + timedelta(days=1)
    out1 = _row(session, owner, household, rv_chk, -100000, "To <pocket-1>", day1)
    sav1 = _row(session, owner, household, rv_sav, 100000, "Deposit to '<pocket-2>'", day1)
    pay1 = _row(session, owner, household, es_chk, 100000,
                "Transferencia Inmediata De <employer-1>, Concepto <reference>", day1)
    out2 = _row(session, owner, household, rv_chk, -200000, "To <pocket-3>", day2)
    sav2 = _row(session, owner, household, rv_sav, 200000, "Deposit to '<pocket-4>'", day2)
    pay2 = _row(session, owner, household, es_chk, 200000,
                "Transferencia De <employer-2>, Concepto Salary <month>", day2)

    strong, suggested = _pairs(transfers.find(session, household.id))
    assert not strong
    assert suggested == {(out1.id, sav1.id), (out1.id, pay1.id), (out2.id, sav2.id), (out2.id, pay2.id)}

    _categorise(session, owner, household, salary, pay1, pay2)
    found = transfers.find(session, household.id)
    assert _pairs(found) == ({(out1.id, sav1.id), (out2.id, sav2.id)}, set())

    with batch(session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id):
        assert transfers.link_strong(session, found) == 2
    assert (out1.transfer_transaction_id, out2.transfer_transaction_id) == (sav1.id, sav2.id)
    assert (pay1.transfer_transaction_id, pay2.transfer_transaction_id) == (None, None)
    assert (pay1.category_id, pay2.category_id) == (salary.id, salary.id)


# --------------------------------------------------------------------------- #
# #127: among strong rivals, the pair whose words match
# --------------------------------------------------------------------------- #


@pytest.fixture()
def nationwide(session, owner, household):
    """NW-CUR and two savings accounts, each lane linked by a person before."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        made = {
            name: Account(household_id=household.id, name=name, type=kind, currency="GBP")
            for name, kind in (
                ("NW-CUR", AccountType.checking),
                ("NW-SAV-A", AccountType.savings),
                ("NW-SAV-B", AccountType.savings),
            )
        }
        session.add_all(made.values())
        session.flush()
        identifiers.add(session, household_id=household.id, kind="number", value="55556666", account=made["NW-SAV-A"])
    earlier = DAY - timedelta(days=60)
    for amount, name in ((111, "NW-SAV-A"), (222, "NW-SAV-B")):
        _link(
            session, owner, household,
            _row(session, owner, household, made["NW-CUR"], -amount, "EARLIER", earlier),
            _row(session, owner, household, made[name], amount, "EARLIER", earlier),
        )
    return made


def _two_by_two(session, owner, household, nw, words):
    """Two £100.00 out of NW-CUR on one day, one into each savings account."""
    (out_a, out_b), (in_a, in_b) = words
    return (
        _row(session, owner, household, nw["NW-CUR"], -10000, out_a),
        _row(session, owner, household, nw["NW-CUR"], -10000, out_b),
        _row(session, owner, household, nw["NW-SAV-A"], 10000, in_a),
        _row(session, owner, household, nw["NW-SAV-B"], 10000, in_b),
    )


def test_same_day_rivals_are_told_apart_by_who_each_row_names(session, owner, household, nationwide):
    out_a, out_b, in_a, in_b = _two_by_two(
        session, owner, household, nationwide,
        (("TO 55556666 <HOLDER-1> FPS", "BP <holder-2> REF 01"),
         ("FROM <holder-1>", "Transfer from <HOLDER-2>")),
    )
    found = transfers.find(session, household.id)
    assert _pairs(found) == ({(out_a.id, in_a.id), (out_b.id, in_b.id)}, set())
    why = {p.out_leg.id: p.why for p in found.strong}
    assert why[out_a.id].endswith("; both rows say HOLDER1")
    assert why[out_b.id].endswith("; both rows say HOLDER2")

    with batch(session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id):
        assert transfers.link_strong(session, found) == 2
    assert (out_a.transfer_transaction_id, out_b.transfer_transaction_id) == (in_a.id, in_b.id)


def test_rows_that_share_nothing_stay_a_question(session, owner, household, nationwide):
    """The transfer words, and an account number both rows print, are not a
    shared description: counted, they would hand this pair to the first two
    rows."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        identifiers.add(session, household_id=household.id, kind="number", value="77778888", account=nationwide["NW-CUR"])
    _two_by_two(
        session, owner, household, nationwide,
        (("FPS A/C 77778888", "BP"), ("FROM A/C 77778888", "TRANSFER FROM")),
    )
    found = transfers.find(session, household.id)
    assert not found.strong
    assert len(found.suggested) == 4
    assert all("more than one" in p.why and "both rows say" not in p.why for p in found.suggested)


def test_rivals_sharing_as_many_words_are_still_a_tie(session, owner, household, nationwide):
    out_a, out_b, in_a, in_b = _two_by_two(
        session, owner, household, nationwide,
        (("ALPHA SAVINGS", "BRAVO SAVINGS"), ("SAVINGS", "SAVINGS")),
    )
    found = transfers.find(session, household.id)
    assert not found.strong
    assert {(p.out_leg.id, p.in_leg.id) for p in found.suggested} == {
        (out_a.id, in_a.id), (out_a.id, in_b.id), (out_b.id, in_a.id), (out_b.id, in_b.id)
    }
    assert all(p.why.endswith("; both rows say SAVINGS") for p in found.suggested)


def test_matching_words_put_a_suggestion_first_and_never_link_it(session, owner, household, ledger):
    """No lane, no name: amount and date only. Words say so and sort it first,
    but a suggestion is not made strong by them."""
    far_out = _row(session, owner, household, ledger["current"], -5100, "CARD SHOP")
    far_in = _row(session, owner, household, ledger["saver"], 5100, "INCOMING", DAY + timedelta(days=1))
    near_out = _row(session, owner, household, ledger["current"], -5200, "<holder-1> RENT", DAY + timedelta(days=9))
    near_in = _row(session, owner, household, ledger["saver"], 5200, "<holder-1> RENT", DAY + timedelta(days=12))
    found = transfers.find(session, household.id)
    assert not found.strong
    assert [(p.out_leg.id, p.in_leg.id) for p in found.suggested] == [
        (near_out.id, near_in.id),
        (far_out.id, far_in.id),
    ]
    assert found.suggested[0].why.endswith("; both rows say HOLDER1 RENT")
    assert "both rows say" not in found.suggested[1].why


def test_joining_words_and_spanish_transfer_words_are_not_shared_words(
    session, owner, household, ledger
):
    """A cash withdrawal and "Transferencia Inmediata De <holder-1>" share "DE"
    and nothing else; that is not two rows saying the same thing."""
    _row(session, owner, household, ledger["current"], -30000, "Cash withdrawal at Plaza De Ejemplo 1")
    _row(
        session, owner, household, ledger["saver"], 30000,
        "Transferencia Inmediata De Holder, Concepto Sent", DAY + timedelta(days=1),
    )
    found = transfers.find(session, household.id)
    assert [p.shared for p in found.suggested] == [()]
    assert "both rows say" not in found.suggested[0].why


# --------------------------------------------------------------------------- #
# #131: how a link was made, and what counts as history
# --------------------------------------------------------------------------- #


def _linked(session, owner, household, a, b, source):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        transfers.link(session, a, b, source=source)


@pytest.mark.parametrize(
    ("source", "words", "strength"),
    [
        (LinkSource.person, ("ONE", "TWO"), "strong"),
        (LinkSource.named, ("TO A/C 33334444", "FROM A/C 11112222"), "strong"),
        (LinkSource.history, ("ONE", "TWO"), "suggested"),
        # From before links said how: vouches only while a row names the other.
        (LinkSource.imported, ("ONE", "TWO"), "suggested"),
        (LinkSource.imported, ("TO A/C 33334444", "TWO"), "strong"),
    ],
    ids=["person", "named", "history", "import-unnamed", "import-named"],
)
def test_only_a_link_something_vouched_for_is_history(
    session, owner, household, ledger, source, words, strength
):
    earlier = DAY - timedelta(days=30)
    a = _row(session, owner, household, ledger["current"], -300, words[0], earlier)
    b = _row(session, owner, household, ledger["saver"], 300, words[1], earlier)
    _linked(session, owner, household, a, b, source)
    assert (a.link_source, b.link_source) == (source, source)

    _row(session, owner, household, ledger["current"], -900, "NOTHING USEFUL")
    _row(session, owner, household, ledger["saver"], 900, "NOTHING EITHER")
    found = transfers.find(session, household.id)
    assert [p.strength for p in found.strong + found.suggested] == [strength]


def test_a_wrong_history_only_link_does_not_make_the_next_one_strong(
    session, owner, household, ledger
):
    """The chain from the real ledger: a person's link makes a lane; the next
    pair on it is linked on history and recorded so; a pair between two
    *other* accounts, whose only link was made on history, stays a question."""
    start = DAY - timedelta(days=60)
    _linked(
        session, owner, household,
        _row(session, owner, household, ledger["current"], -100, "ONE", start),
        _row(session, owner, household, ledger["saver"], 100, "TWO", start),
        LinkSource.person,
    )
    second_out = _row(session, owner, household, ledger["current"], -200, "THREE", start + timedelta(days=10))
    second_in = _row(session, owner, household, ledger["saver"], 200, "FOUR", start + timedelta(days=10))
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id):
        assert transfers.link_strong(session, transfers.find(session, household.id)) == 1
    assert (second_out.link_source, second_in.link_source) == (LinkSource.history, LinkSource.history)

    # A wrong link between the card and the current account, made on history.
    _linked(
        session, owner, household,
        _row(session, owner, household, ledger["card"], -500, "RIDE CO", start),
        _row(session, owner, household, ledger["current"], 500, "EXPENSE TOOL", start),
        LinkSource.history,
    )
    later_out = _row(session, owner, household, ledger["card"], -700, "DINER")
    later_in = _row(session, owner, household, ledger["current"], 700, "EXPENSE TOOL")
    found = transfers.find(session, household.id)
    assert not found.strong
    assert _pairs(found)[1] == {(later_out.id, later_in.id)}


def test_a_named_pair_is_linked_as_named(session, owner, household, ledger):
    out = _row(session, owner, household, ledger["saver"], -2000, "TO A/C 11112222")
    into = _row(session, owner, household, ledger["current"], 2000, "INCOMING")
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id):
        transfers.link_strong(session, transfers.find(session, household.id))
    assert (out.link_source, into.link_source) == (LinkSource.named, LinkSource.named)


def test_a_card_purchase_and_its_equal_reimbursement_are_only_suggested(
    session, owner, household, ledger
):
    """Accounts with a person's link between them, a purchase on the card, and
    an expense tool paying exactly that back three days later."""
    earlier = DAY - timedelta(days=30)
    _linked(
        session, owner, household,
        _row(session, owner, household, ledger["current"], -40000, "CARD PAYMENT", earlier),
        _row(session, owner, household, ledger["card"], 40000, "PAYMENT RECEIVED", earlier),
        LinkSource.person,
    )
    buy = _row(session, owner, household, ledger["card"], -1850, "<ride-hailing> TRIP")
    back = _row(session, owner, household, ledger["current"], 1850,
                "Transferencia De <employer>, Concepto <expense-tool>", DAY + timedelta(days=3))
    found = transfers.find(session, household.id)
    assert not found.strong
    assert _pairs(found)[1] == {(buy.id, back.id)}
    assert "money out of a card is a purchase" in found.suggested[0].why
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id):
        assert transfers.link_strong(session, transfers.find(session, household.id, among=[back])) == 0
    assert back.transfer_transaction_id is None


def test_cashback_naming_the_cards_own_contract_is_only_suggested(session, owner, household, ledger):
    """The refund text names the card's contract number, correctly an
    identifier of the card -- and it is still not a transfer."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        identifiers.add(session, household_id=household.id, kind="number", value="9900112233", account=ledger["card"])
    buy = _row(session, owner, household, ledger["card"], -2500, "<restaurant>")
    back = _row(session, owner, household, ledger["current"], 2500,
                "Bonificacion De Campana Del Contrato 9900112233", DAY + timedelta(days=2))
    found = transfers.find(session, household.id)
    assert not found.strong
    assert _pairs(found)[1] == {(buy.id, back.id)}
    assert found.suggested[0].why.startswith("the Current row names Card, but money out of a card")


def test_a_card_payment_into_the_card_still_links_itself(session, owner, household, ledger):
    paid = _row(session, owner, household, ledger["current"], -500000, "ACC-CARDBRAND 411111******0000")
    raw = b"Date,Description,Amount\n2026-03-25,PAYMENT RECEIVED - THANK YOU,5000.00\n"
    _, result = _import(session, owner, household, ledger["card"], raw)
    assert result["transfers_linked"] == 1
    assert paid.transfer_account_id == ledger["card"].id
    assert paid.link_source is LinkSource.named


def test_a_payee_with_a_category_rule_caps_a_pair_at_suggested(session, owner, household, ledger, salary):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        refunds = payee_service.get_or_create(session, household.id, "<expense-tool>")
        category_service.set_rule(session, refunds, mode="fixed", category_id=salary.id)
        into = txn_service.create(
            session, account=ledger["current"], date=DAY, amount=3100, payee=refunds, category=None,
            import_payee_original="<expense-tool> PAYOUT", import_id="T:rule",
        )
    out = _row(session, owner, household, ledger["saver"], -3100, "TO A/C 11112222")
    found = transfers.find(session, household.id)
    assert not found.strong
    assert _pairs(found)[1] == {(out.id, into.id)}
    assert found.suggested[0].why.endswith("but <expense-tool> has a category rule")


def test_a_row_naming_somebody_who_has_paid_you_is_only_suggested(
    session, owner, household, ledger, salary
):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        employer = payee_service.get_or_create(session, household.id, "<Employer> Ltd")
        txn_service.create(
            session, account=ledger["current"], date=DAY - timedelta(days=20), amount=300000,
            payee=employer, category=salary, import_id="T:pay",
        )
    # The same employer's name on another row, with no payee of its own.
    out = _row(session, owner, household, ledger["saver"], -4400, "TO A/C 11112222")
    into = _row(session, owner, household, ledger["current"], 4400, "TRANSFER FROM <EMPLOYER> LTD EXPENSES")
    # And a household member's name on an income row is not a payer's name.
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        jane = payee_service.get_or_create(session, household.id, "DOE JANE")
        txn_service.create(
            session, account=ledger["current"], date=DAY - timedelta(days=20), amount=5000,
            payee=jane, category=salary, import_id="T:gift",
        )
    own_out = _row(session, owner, household, ledger["saver"], -6600, "TO A/C 11112222 DOE JANE")
    own_in = _row(session, owner, household, ledger["current"], 6600, "FROM DOE JANE")

    found = transfers.find(session, household.id)
    assert _pairs(found) == ({(own_out.id, own_in.id)}, {(out.id, into.id)})
    assert found.suggested[0].why.endswith("but it names EMPLOYER LTD, who has paid you before")


# --------------------------------------------------------------------------- #
# #131: a pair a person said is not a transfer stays that way
# --------------------------------------------------------------------------- #


def test_an_unlinked_pair_is_not_offered_again_until_the_unlink_is_undone(
    session, owner, household, ledger
):
    out = _row(session, owner, household, ledger["saver"], -2000, "TO A/C 11112222")
    into = _row(session, owner, household, ledger["current"], 2000, "FROM A/C 33334444")
    other_out = _row(session, owner, household, ledger["saver"], -3000, "MOVE")
    other_in = _row(session, owner, household, ledger["current"], 3000, "MOVE IN")
    _linked(session, owner, household, out, into, LinkSource.named)

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as unlinked:
        transfers.unlink(session, into)
    rows = session.execute(select(TransferRejection)).scalars().all()
    assert [(r.out_transaction_id, r.in_transaction_id, r.rejected_by_id) for r in rows] == [
        (out.id, into.id, owner.id)
    ]
    found = transfers.find(session, household.id)
    assert _pairs(found) == (set(), {(other_out.id, other_in.id)})
    # Nor at import, when the question is asked about one of its rows.
    narrowed = transfers.find(session, household.id, among=[out])
    assert not narrowed.strong and not narrowed.suggested

    undo_batch(session, unlinked.id, actor_id=owner.id)
    session.expire_all()
    assert session.get(Transaction, out.id).transfer_transaction_id == into.id
    assert session.get(Transaction, out.id).link_source is LinkSource.named
    assert session.execute(select(TransferRejection)).scalars().all() == []


def test_not_a_transfer_rejects_a_pair_that_was_never_linked(session, owner, household, ledger):
    out = _row(session, owner, household, ledger["saver"], -2000, "MOVE")
    into = _row(session, owner, household, ledger["current"], 2000, "SOMETHING")
    rival = _row(session, owner, household, ledger["card"], 2000, "SOMETHING ELSE")
    with batch(session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id):
        assert transfers.reject(session, into, out) is not None
        assert transfers.reject(session, out, into) is None  # already said
    found = transfers.find(session, household.id)
    assert _pairs(found) == (set(), {(out.id, rival.id)})

    # Linking it by hand anyway is a person changing their mind.
    _linked(session, owner, household, out, into, LinkSource.person)
    assert session.execute(select(TransferRejection)).scalars().all() == []


def test_a_rejection_needs_a_pair_that_could_have_been_one(session, owner, household, ledger):
    a = _row(session, owner, household, ledger["saver"], -2000, "ONE")
    b = _row(session, owner, household, ledger["current"], -2000, "TWO")
    with batch(session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id):
        with pytest.raises(ValidationError, match="out of one account"):
            transfers.reject(session, a, b)
        with pytest.raises(ValidationError, match="with itself"):
            transfers.reject(session, a, a)


# --------------------------------------------------------------------------- #
# #131: linked by history only
# --------------------------------------------------------------------------- #


def test_linked_by_history_only_lists_exactly_the_links_no_name_vouches_for(
    session, owner, household, ledger
):
    made = {}
    for n, (source, out_words, in_words) in enumerate(
        [
            (LinkSource.person, "ONE", "TWO"),
            (LinkSource.named, "TO A/C 33334444", "TWO"),
            (LinkSource.history, "ONE", "TWO"),
            (LinkSource.imported, "ONE", "TWO"),
            (LinkSource.imported, "ONE", "FROM A/C 11112222"),
        ]
    ):
        out = _row(session, owner, household, ledger["current"], -(100 + n), out_words)
        into = _row(session, owner, household, ledger["saver"], 100 + n, in_words)
        _linked(session, owner, household, out, into, source)
        made[n] = out
    # A named link whose identifier has since gone no longer has a name behind it.
    gone_out = _row(session, owner, household, ledger["current"], -777, "TO CARDBRAND")
    gone_in = _row(session, owner, household, ledger["card"], 777, "THANKS")
    _linked(session, owner, household, gone_out, gone_in, LinkSource.named)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        for row in identifiers.list_for_household(session, household.id):
            if row.value == "CARDBRAND":
                identifiers.remove(session, row)

    listed = transfers.unproven(session, household.id)
    assert {one.out_leg.id: one.source for one in listed} == {
        made[2].id: LinkSource.history,
        made[3].id: LinkSource.imported,
        gone_out.id: LinkSource.named,
    }
    assert all(one.in_leg.transfer_transaction_id == one.out_leg.id for one in listed)

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        transfers.confirm(session, made[2])
    assert made[2].link_source is LinkSource.person
    assert made[2].id not in {one.out_leg.id for one in transfers.unproven(session, household.id)}


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def test_link_all_reject_review_and_confirm_over_http(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    base = f"/api/households/{house['id']}"
    a = client.post(f"{base}/accounts", json={"name": "A", "type": "checking"}, headers=HEADERS).json()
    b = client.post(f"{base}/accounts", json={"name": "B", "type": "savings"}, headers=HEADERS).json()

    def row(account, amount, day, name):
        made = client.post(
            f"{base}/transactions",
            json={"account_id": account["id"], "date": day, "amount": amount, "payee_name": name},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text
        return made.json()["id"]

    out1, in1 = row(a, -5000, "2026-03-24", "Move"), row(b, 5000, "2026-03-25", "Move in")
    out2, in2 = row(a, -6000, "2026-03-24", "Other"), row(b, 6000, "2026-03-25", "Other in")

    # "Link all" is not a person vouching for each pair: nothing names the
    # other account, so it is recorded as history and listed for review.
    linked = client.post(
        f"{base}/transfers/link",
        json={"pairs": [{"first_id": out1, "second_id": in1}], "by": "evidence"},
        headers=HEADERS,
    )
    assert linked.json() == {"linked": 1}
    found = client.get(f"{base}/transfers/findings").json()
    assert [(p["out_leg"]["id"], p["link_source"]) for p in found["unproven"]] == [(out1, "history")]
    assert [p["out_leg"]["id"] for p in found["suggested"]] == [out2]

    rejected = client.post(
        f"{base}/transfers/reject", json={"pairs": [{"first_id": in2, "second_id": out2}]}, headers=HEADERS
    )
    assert rejected.json() == {"rejected": 1}
    assert client.get(f"{base}/transfers/findings").json()["suggested"] == []

    kept = client.post(f"/api/transactions/{in1}/confirm-link", headers=HEADERS)
    assert kept.status_code == 204
    assert client.get(f"{base}/transfers/findings").json()["unproven"] == []


# --------------------------------------------------------------------------- #
# #131: what the migration says about links made before it
# --------------------------------------------------------------------------- #


def _insert(conn, table, **given):
    """A row with only what the test cares about; every other NOT NULL column
    gets a filler of its type, so the test does not restate the schema."""
    values = dict(given)
    for _, name, kind, notnull, default, _pk in conn.exec_driver_sql(f"PRAGMA table_info({table})"):
        if name in values or not notnull or default is not None:
            continue
        kind = kind.upper()
        values[name] = (
            0 if kind.startswith(("INT", "BOOL")) else "2026-01-01 00:00:00" if "DATE" in kind else f"{name}-x"
        )
    names = ", ".join(values)
    marks = ", ".join(f":{n}" for n in values)
    conn.execute(text(f"INSERT INTO {table} ({names}) VALUES ({marks})"), values)


def test_the_backfill_calls_hand_typed_links_a_persons_and_the_rest_import(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import create_engine

    from tests.test_migrations import _config

    url = f"sqlite:///{tmp_path / 'before.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _config(url)
    command.upgrade(cfg, "c3e8a1f05d72")

    engine = create_engine(url)
    with engine.begin() as conn:
        _insert(conn, "households", id="h1", name="Ours")
        for account in ("a1", "a2"):
            _insert(conn, "accounts", id=account, household_id="h1", name=account, type="checking", currency="EUR")

        def leg(id_, account, amount, import_id):
            _insert(
                conn, "transactions", id=id_, household_id="h1", account_id=account, date="2026-03-01",
                amount=amount, import_id=import_id, cleared="uncleared",
            )

        # Typed in the register on both sides.
        leg("typed-out", "a1", -100, None)
        leg("typed-in", "a2", 100, None)
        # One side from a statement.
        leg("half-out", "a1", -200, "I:1")
        leg("half-in", "a2", 200, None)
        # Both from statements.
        leg("both-out", "a1", -300, "I:2")
        leg("both-in", "a2", 300, "I:3")
        # Not a transfer at all.
        leg("plain", "a1", -400, "I:4")
        for out, into in (("typed-out", "typed-in"), ("half-out", "half-in"), ("both-out", "both-in")):
            conn.execute(
                text("UPDATE transactions SET transfer_transaction_id = :o, transfer_account_id = 'a2' WHERE id = :i"),
                {"o": into, "i": out},
            )
            conn.execute(
                text("UPDATE transactions SET transfer_transaction_id = :o, transfer_account_id = 'a1' WHERE id = :i"),
                {"o": out, "i": into},
            )

    command.upgrade(cfg, "db3cf4a5f9d3")
    with engine.connect() as conn:
        got = dict(conn.execute(text("SELECT id, link_source FROM transactions")).all())
        rejections = conn.execute(text("SELECT count(*) FROM transfer_rejections")).scalar()
    engine.dispose()
    assert got == {
        "typed-out": "person", "typed-in": "person",
        "half-out": "import", "half-in": "import",
        "both-out": "import", "both-in": "import",
        "plain": None,
    }
    assert rejections == 0


# --------------------------------------------------------------------------- #
# #132 follow-up: a cut-off name is cut off at the end of the bank's own field
# --------------------------------------------------------------------------- #


def test_a_surname_cut_off_in_the_banks_text_matches_with_a_memo_after_it(
    session, owner, household, ledger
):
    """The bank cut the surname at its field width; the memo came after it.

    Joined into one string, the cut word was no longer the last one and the
    holder did not match. Read field by field, it is the end of the bank's
    text, which is where truncation happens."""
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        identifiers.add(
            session, household_id=household.id, kind="holder", value="ROWANTREE ALEX", account=None
        )
    out = _row(session, owner, household, ledger["current"], -5100, "ALEX ROWANT")
    into = _row(session, owner, household, ledger["saver"], 5100, "PAYMENT RECEIVED")
    other = _row(session, owner, household, ledger["current"], -5200, "ALEX ROWING CLUB")
    other_in = _row(session, owner, household, ledger["saver"], 5200, "PAYMENT RECEIVED")
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        out.memo = "RENT SHARE"
    found = transfers.find(session, household.id)
    why = {(p.out_leg.id, p.in_leg.id): p.why for p in found.suggested}
    assert why[(out.id, into.id)].startswith("a household member's name is on it")
    # A word that merely starts like the surname, not at the end of the
    # bank's text, is still no name.
    assert why[(other.id, other_in.id)] == "the amounts match and the dates are close"
