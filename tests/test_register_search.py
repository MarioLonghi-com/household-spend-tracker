"""The register's search box as a small query language (#367).

Words AND, ``OR`` or ``|`` between two terms, quotes for a phrase, ``-`` to
exclude -- parsed in ``search_query`` and applied in ``transactions.filtered``,
so the register and the agent's reads mean the same thing by ``search``.

Every assertion is the exact set of rows a query returns. Two payees, two
memos, two accounts in two currencies, and a second household holding the
same words, so a term that leaked across households or matched only one field
would show.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.audit.batch import batch
from app.models import Account, AccountType, BatchKind
from app.services import payees as payee_service
from app.services import transactions as txn_service
from app.services.search_query import Term, parse
from tests.conftest import HEADERS
from tests.test_api import _household_with_accounts

DAY = date(2026, 4, 2)


# --------------------------------------------------------------------------- #
# Reading the text
# --------------------------------------------------------------------------- #


def _read(text):
    return [[(term.text, term.excluded) for term in group] for group in parse(text)]


@pytest.mark.parametrize(
    ("typed", "groups"),
    [
        ("lidl birthday", [[("lidl", False)], [("birthday", False)]]),
        ("lidl AND birthday", [[("lidl", False)], [("birthday", False)]]),
        ("lidl OR aldi", [[("lidl", False), ("aldi", False)]]),
        ("lidl | aldi", [[("lidl", False), ("aldi", False)]]),
        ("lidl|aldi", [[("lidl", False), ("aldi", False)]]),
        # OR binds tighter than the gap between words.
        ("coffee lidl OR aldi", [[("coffee", False)], [("lidl", False), ("aldi", False)]]),
        ('"100% cotton"', [[("100% cotton", False)]]),
        ("coffee -decaf", [[("coffee", False)], [("decaf", True)]]),
        ('-"standing order"', [[("standing order", True)]]),
        # Lower-case "or" is a word like any other.
        ("rock or roll", [[("rock", False)], [("or", False)], [("roll", False)]]),
        # A hyphen inside a word is part of it.
        ("e-mail", [[("e-mail", False)]]),
    ],
)
def test_the_box_reads_into_groups_of_terms(typed, groups):
    assert _read(typed) == groups


@pytest.mark.parametrize(
    ("typed", "groups"),
    [
        # An operator without a term on both sides is the word itself.
        ("OR", [[("OR", False)]]),
        ("lidl OR", [[("lidl", False)], [("OR", False)]]),
        ("OR lidl", [[("OR", False)], [("lidl", False)]]),
        ("|", [[("|", False)]]),
        ("AND", [[("AND", False)]]),
        # An unclosed quote runs to the end.
        ('"standing order', [[("standing order", False)]]),
        ('coffee "standing', [[("coffee", False)], [("standing", False)]]),
        ("-", [[("-", False)]]),
        ('""', []),
        ("   ", []),
        ("", []),
        (None, []),
    ],
)
def test_what_would_be_malformed_has_one_reading_and_never_raises(typed, groups):
    assert _read(typed) == groups


def test_a_term_is_its_text_and_whether_it_is_excluded():
    assert parse("-x") == [[Term("x", excluded=True)]]


# --------------------------------------------------------------------------- #
# What the filter returns
# --------------------------------------------------------------------------- #


@pytest.fixture()
def ledger(session, owner, household, other_household, accounts, member):
    """Two payees, two memos, rows across two currencies, and a neighbour."""
    eur, gbp = accounts["checking"], accounts["pounds"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        lidl = payee_service.get_or_create(session, household.id, "Lidl")
        aldi = payee_service.get_or_create(session, household.id, "Aldi")
        made = {
            "lidl_birthday": txn_service.create(
                session, account=eur, date=DAY, amount=-2_500, payee=lidl, memo="birthday cake"
            ),
            "lidl_plain": txn_service.create(
                session, account=eur, date=DAY, amount=-1_200, payee=lidl, memo="weekly shop"
            ),
            "aldi_birthday": txn_service.create(
                session, account=gbp, date=DAY, amount=-1_800, payee=aldi, memo="birthday candles"
            ),
            "aldi_none": txn_service.create(session, account=gbp, date=DAY, amount=-900, payee=aldi),
            "imported": txn_service.create(
                session, account=eur, date=DAY, amount=-4_000, import_id="line-1",
                import_payee_original="LIDL SAYS 100% COTTON",
            ),
            "underscore": txn_service.create(
                session, account=gbp, date=DAY, amount=-300, memo="ref_2024"
            ),
            "dash": txn_service.create(session, account=gbp, date=DAY, amount=-301, memo="ref-2024"),
            "bare": txn_service.create(session, account=eur, date=DAY, amount=-100),
        }
    # The same words in a household this one cannot see.
    with batch(session, kind=BatchKind.manual, actor_id=member.id, household_id=other_household.id):
        theirs = Account(
            household_id=other_household.id, name="Theirs", type=AccountType.checking, currency="GBP"
        )
        session.add(theirs)
        session.flush()
        their_lidl = payee_service.get_or_create(session, other_household.id, "Lidl")
        txn_service.create(
            session, account=theirs, date=DAY, amount=-5_000, payee=their_lidl, memo="birthday"
        )
    return made


def _found(session, household, ledger, search) -> set[str]:
    rows = session.execute(txn_service.filtered(household.id, search=search)).scalars()
    by_id = {row.id: name for name, row in ledger.items()}
    found = {row.id for row in rows}
    assert found <= set(by_id), "a row from the other household"
    return {by_id[one] for one in found}


@pytest.mark.parametrize(
    ("search", "names"),
    [
        # One word, as it always was: payee, memo or the bank's own text.
        ("lidl", {"lidl_birthday", "lidl_plain", "imported"}),
        # Every word, each in whatever field it is in: payee Lidl, memo birthday.
        ("lidl birthday", {"lidl_birthday"}),
        ("lidl AND birthday", {"lidl_birthday"}),
        ("lidl OR aldi", {"lidl_birthday", "lidl_plain", "imported", "aldi_birthday", "aldi_none"}),
        ("lidl | aldi", {"lidl_birthday", "lidl_plain", "imported", "aldi_birthday", "aldi_none"}),
        ("birthday lidl OR aldi", {"lidl_birthday", "aldi_birthday"}),
        # A phrase is one term: "birthday cake" is in one memo, as written.
        ('"birthday cake"', {"lidl_birthday"}),
        ('"cake birthday"', set()),
        # Excluded: no field mentions it, and a row with no memo at all is
        # kept rather than lost to a NULL.
        ("aldi -birthday", {"aldi_none"}),
        ("lidl -birthday", {"lidl_plain", "imported"}),
        ('-"birthday"', {"lidl_plain", "aldi_none", "imported", "underscore", "dash", "bare"}),
        ("birthday -cake -candles", set()),
        ("-lidl OR birthday", {
            "lidl_birthday", "aldi_birthday", "aldi_none", "underscore", "dash", "bare",
        }),
    ],
)
def test_each_form_returns_exactly_its_rows(session, household, ledger, search, names):
    assert _found(session, household, ledger, search) == names


@pytest.mark.parametrize(
    ("search", "names"),
    [
        ("%", {"imported"}),
        ('"100%"', {"imported"}),
        ('"100% cotton"', {"imported"}),
        ("_", {"underscore"}),
        ("ref_2024", {"underscore"}),
        ("ref -_", {"dash"}),
        ("ref_ OR %", {"underscore", "imported"}),
        # And an ordinary term still matches both spellings.
        ("ref", {"underscore", "dash"}),
    ],
)
def test_percent_and_underscore_stay_literal_in_every_form(session, household, ledger, search, names):
    assert _found(session, household, ledger, search) == names


@pytest.mark.parametrize(
    ("search", "names"),
    [
        ("OR", set()),
        ("lidl OR", set()),  # lidl, and the word "OR" -- which nothing says
        ('"birthday cak', {"lidl_birthday"}),
        ("-", {"dash"}),
        ('""', None),
        ("   ", None),
    ],
)
def test_a_malformed_query_has_a_defined_answer(session, household, ledger, search, names):
    """Never a 500; and where nothing is left to search for, no filter at all."""
    found = _found(session, household, ledger, search)
    assert found == (set(ledger) if names is None else names)


# --------------------------------------------------------------------------- #
# Over HTTP. The agent's read is compared with the register's, row for row, in
# test_agent_review.py's shared-filter test.
# --------------------------------------------------------------------------- #


def test_the_register_takes_the_language_over_http(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    for day, payee, memo in (
        ("05", "Lidl", "birthday cake"),
        ("06", "Lidl", "weekly shop"),
        ("07", "Aldi", "birthday candles"),
    ):
        made = client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": f"2026-01-{day}", "amount": -1_000,
                  "payee_name": payee, "memo": memo},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text

    def memos(search: str) -> list[str]:
        answer = client.get(f"/api/households/{house}/transactions", params={"search": search})
        assert answer.status_code == 200, answer.text
        return sorted(row["memo"] for row in answer.json()["transactions"])

    assert memos("lidl birthday") == ["birthday cake"]
    assert memos("lidl OR aldi") == ["birthday cake", "birthday candles", "weekly shop"]
    assert memos('"birthday cake"') == ["birthday cake"]
    assert memos("birthday -lidl") == ["birthday candles"]
    assert memos("lidl OR") == []
    assert memos('"unclosed') == []
