"""`POST …/transactions/match`: finding the row a piece of evidence describes (#134).

Stories 1 and 3: several receipts, or the lines of a corporate expenses
portal, each needing its ledger row. The fixture has two of everything the
matcher could confuse -- two households, two currencies, two accounts -- and
the assertions are about *which rows, in which order*, never only a 200.
"""

from __future__ import annotations

import base64
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import (
    AgentScope,
    Batch,
    BatchKind,
    Change,
    Household,
    Receipt,
    Transaction,
    User,
)
from app.services import agent_keys as key_service
from app.services.matching import DIFFERENT_CURRENCY
from tests.conftest import HEADERS, _setup_owner
from tests.receipt_fixtures import as_bytes, receipt_image

V1 = "/api/agent/v1"
DAY = date(2026, 5, 11)


def _d(offset: int = 0) -> str:
    return (DAY + timedelta(days=offset)).isoformat()


def _account(client, house_id: str, name: str, currency: str, kind: str = "checking") -> dict:
    made = client.post(
        f"/api/households/{house_id}/accounts",
        json={"name": name, "type": kind, "currency": currency},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    return made.json()


@pytest.fixture()
def world(client):
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    other = client.post("/api/households", json={"name": "Elsewhere"}, headers=HEADERS).json()
    eur = _account(client, house["id"], "Santander", "EUR")
    gbp = _account(client, house["id"], "Monzo", "GBP", kind="credit_card")
    theirs = _account(client, other["id"], "Their bank", "EUR")
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, made["user"]["id"])
        row = own.get(Household, house["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=row.id):
            _k, write = key_service.issue(
                own, user=user, household=row, label="the filer", scope=AgentScope.write
            )
            _k2, read = key_service.issue(
                own, user=user, household=row, label="the analyst", scope=AgentScope.read
            )
        own.commit()
    return {
        "client": client, "house": house, "other": other,
        "eur": eur, "gbp": gbp, "theirs": theirs,
        "token": write, "reader": read,
    }


def _txn(world, account: dict, *, when: str, amount: int, payee: str | None = None,
         memo: str | None = None, house: dict | None = None, bank: str | None = None,
         **extra) -> dict:
    client = world["client"]
    body = {"account_id": account["id"], "date": when, "amount": amount, **extra}
    if payee:
        body["payee_name"] = payee
    if memo:
        body["memo"] = memo
    made = client.post(
        f"/api/households/{(house or world['house'])['id']}/transactions",
        json=body, headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    row = made.json()
    if bank:
        # The bank's own words, as a statement import leaves them. Set
        # directly: the subject is matching, not importing.
        with Session(client.app_module.db_engine, expire_on_commit=False) as own:
            txn = own.get(Transaction, row["id"])
            actor = own.execute(select(User.id)).scalars().first()
            with batch(own, kind=BatchKind.imported, actor_id=actor,
                       household_id=txn.household_id):
                txn.import_payee_original = bank
                txn.import_id = f"stmt:{row['id']}"
            own.commit()
    return row


def _bearer(token: str) -> dict:
    # The fixture keeps its session cookie, so the same-site headers go too --
    # see `test_agent_receipts._agent_headers` for why.
    return {"authorization": f"Bearer {token}", **HEADERS}


def _match(world, queries, *, token=None, house=None, **extra):
    return world["client"].post(
        f"{V1}/households/{(house or world['house'])['id']}/transactions/match",
        json={"queries": queries, **extra},
        headers=_bearer(token or world["token"]),
    )


def _ids(result: dict) -> list[str]:
    return [c["transaction_id"] for c in result["candidates"]]


def _counts(client) -> dict[str, int]:
    with Session(client.app_module.db_engine) as own:
        return {
            model.__name__: own.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (Transaction, Change, Batch, Receipt)
        }


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_exact_amount_ranks_first_then_shared_words_then_the_nearer_date(world):
    # Named a day later than the unnamed one: shared words outrank the date.
    exact_named = _txn(world, world["eur"], when=_d(1), amount=-1250, payee="Mercadona")
    exact_other = _txn(world, world["eur"], when=_d(0), amount=-1250, payee="Lidl")
    named_only = _txn(world, world["eur"], when=_d(0), amount=-1310, payee="Mercadona")
    pounds = _txn(world, world["gbp"], when=_d(2), amount=-1250, payee="Mercadona")
    # Noise the answer must not contain: another household's identical row, a
    # row outside the window, money coming in, and an unrelated amount.
    _txn(world, world["theirs"], when=_d(0), amount=-1250, payee="Mercadona",
         house=world["other"])
    _txn(world, world["eur"], when=_d(9), amount=-1250, payee="Mercadona")
    _txn(world, world["eur"], when=_d(0), amount=1250, payee="Mercadona")
    _txn(world, world["eur"], when=_d(0), amount=-4400, payee="Zara")

    answer = _match(world, [{"ref": "receipt 1", "amount": "12.50", "currency": "EUR",
                             "date": _d(0), "text": "MERCADONA S.A."}])
    assert answer.status_code == 200, answer.text
    result = answer.json()["results"][0]

    assert result["ref"] == "receipt 1"
    assert result["amount_minor"] == 1250
    assert (result["since"], result["until"]) == (_d(-4), _d(4))
    assert _ids(result) == [exact_named["id"], exact_other["id"], named_only["id"], pounds["id"]]

    first, second, third, fourth = result["candidates"]
    assert first["amount_matched"] is True and first["matched_words"] == ["MERCADONA"]
    assert first["reason"] == "same amount, posted 1 day later, merchant words MERCADONA"
    assert second["reason"] == "same amount, same day"
    assert third["amount_matched"] is False
    assert third["reason"].startswith("amount differs (-13.10 EUR here)")
    assert fourth["currency"] == "GBP" and fourth["amount_matched"] is False
    assert fourth["reason"].startswith(DIFFERENT_CURRENCY)


def test_a_foreign_currency_receipt_is_matched_on_merchant_and_never_on_a_converted_amount(world):
    """A USD receipt paid on a EUR card. 15.00 USD became 13.89 EUR; the EUR
    row that happens to be 15.00 is a different purchase and must not appear."""
    paid = _txn(world, world["eur"], when=_d(1), amount=-1389, payee="Starbucks",
                bank="COMPRA TARJ 4412 STARBUCKS NEW YORK")
    decoy = _txn(world, world["eur"], when=_d(0), amount=-1500, payee="Taxi")

    result = _match(world, [{"amount": "15.00", "currency": "USD", "date": _d(0),
                             "text": "Starbucks Coffee #1123 New York"}]).json()["results"][0]

    assert _ids(result) == [paid["id"]]
    assert decoy["id"] not in _ids(result)
    only = result["candidates"][0]
    assert only["amount_minor"] == -1389 and only["currency"] == "EUR"
    assert only["amount_matched"] is False
    assert DIFFERENT_CURRENCY in only["reason"]
    assert only["bank_text"] == "COMPRA TARJ 4412 STARBUCKS NEW YORK"
    assert "no account here is in USD" in result["note"]


def test_a_foreign_currency_query_with_no_words_finds_nothing_and_says_why(world):
    _txn(world, world["eur"], when=_d(0), amount=-1500, payee="Taxi")
    result = _match(world, [{"amount_minor": 1500, "currency": "USD",
                             "date": _d(0)}]).json()["results"][0]
    assert result["candidates"] == []
    assert "nothing could be matched" in result["note"]


def test_account_id_narrows_to_that_account_and_direction_in_finds_money_in(world):
    _txn(world, world["eur"], when=_d(0), amount=8000, payee="Acme payroll")
    refund = _txn(world, world["gbp"], when=_d(0), amount=8000, payee="Acme")
    _txn(world, world["gbp"], when=_d(0), amount=-8000, payee="Acme")

    result = _match(world, [{"amount_minor": 8000, "date": _d(0), "direction": "in",
                             "account_id": world["gbp"]["id"]}]).json()["results"][0]
    assert _ids(result) == [refund["id"]]


def test_limit_caps_each_querys_candidates(world):
    for offset in range(4):
        _txn(world, world["eur"], when=_d(offset), amount=-999)
    result = _match(world, [{"amount_minor": 999, "date": _d(0)}], limit=2).json()["results"][0]
    assert [c["days_apart"] for c in result["candidates"]] == [0, 1]


# --------------------------------------------------------------------------- #
# What each candidate carries
# --------------------------------------------------------------------------- #


def test_a_candidate_says_enough_to_decide_without_a_second_read(world):
    client = world["client"]
    categories = client.get(f"{V1}/manifest", headers=_bearer(world["reader"])).json()["categories"]
    groceries = categories[0]

    locked = _txn(world, world["eur"], when=_d(0), amount=-2500, payee="Hotel Ritz",
                  memo="conference", category_id=groceries["id"], cleared="reconciled")
    flagged = client.patch(f"/api/transactions/{locked['id']}/reimbursement",
                           json={"state": "expected"}, headers=HEADERS)
    assert flagged.status_code == 200, flagged.text
    stored = client.post(
        f"{V1}/households/{world['house']['id']}/receipts",
        json={"content_base64": base64.b64encode(as_bytes(receipt_image((70, 90)))).decode(),
              "transaction_id": locked["id"]},
        headers=_bearer(world["token"]),
    )
    assert stored.status_code == 201, stored.text
    moved = client.post(
        f"/api/households/{world['house']['id']}/transfers",
        json={"from_account_id": world["eur"]["id"], "to_account_id": world["gbp"]["id"],
              "date": _d(0), "amount": 2500, "to_amount": 2150},
        headers=HEADERS,
    )
    assert moved.status_code == 201, moved.text

    result = _match(world, [{"amount_minor": 2500, "currency": "EUR", "date": _d(0)}],
                    token=world["reader"]).json()["results"][0]

    by_id = {c["transaction_id"]: c for c in result["candidates"]}
    hotel = by_id[locked["id"]]
    assert hotel["cleared"] == "reconciled", "a locked row is still offered"
    assert hotel["reimbursement"] == "expected"
    assert hotel["receipts"] == 1
    assert hotel["category"] == groceries["full_name"]
    assert hotel["memo"] == "conference"
    assert hotel["payee"] == "Hotel Ritz"
    assert hotel["account"] == "Santander" and hotel["account_id"] == world["eur"]["id"]
    assert hotel["amount"] == "-25.00"
    assert hotel["is_transfer"] is False

    legs = [c for c in result["candidates"] if c["is_transfer"]]
    assert len(legs) == 1 and legs[0]["amount_minor"] == -2500
    assert len(result["candidates"]) == 2


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field,value", [("amount", 12.5), ("amount_minor", 1250.0),
                                         ("amount_minor", 12.5)])
def test_a_json_float_is_refused(world, field, value):
    refused = _match(world, [{field: value, "currency": "EUR", "date": _d(0)}])
    assert refused.status_code == 422


def test_both_or_neither_amount_is_refused_with_a_sentence(world):
    neither = _match(world, [{"date": _d(0)}])
    both = _match(world, [{"date": _d(0), "amount": "1.00", "amount_minor": 100,
                           "currency": "EUR"}])
    assert neither.status_code == both.status_code == 422
    assert "exactly one of amount_minor" in neither.text


def test_a_decimal_string_takes_the_exponent_of_its_currency_or_its_account(world):
    pounds = _txn(world, world["gbp"], when=_d(0), amount=-1250)
    euros = _txn(world, world["eur"], when=_d(0), amount=-1250)

    by_account = _match(world, [{"amount": "12.5", "account_id": world["gbp"]["id"],
                                 "date": _d(0)}]).json()["results"][0]
    assert _ids(by_account) == [pounds["id"]]

    by_currency = _match(world, [{"amount": "12.50", "currency": "eur",
                                  "date": _d(0)}]).json()["results"][0]
    assert by_currency["currency"] == "EUR"
    assert _ids(by_currency) == [euros["id"]], "a GBP row of the same digits is not offered"


def test_a_decimal_string_with_no_currency_to_read_it_by_is_refused(world):
    refused = _match(world, [{"ref": "line 7", "amount": "12.50", "date": _d(0)}])
    assert refused.status_code == 422
    assert 'query 0 ("line 7")' in refused.json()["detail"]
    assert "currency" in refused.json()["detail"]


def test_a_decimal_string_finer_than_its_currency_is_refused_not_rounded(world):
    refused = _match(world, [{"amount": "12.505", "currency": "EUR", "date": _d(0)}])
    assert refused.status_code == 422
    assert "more decimal places than EUR" in refused.json()["detail"]
    nonsense = _match(world, [{"amount": "twelve", "currency": "EUR", "date": _d(0)}])
    assert nonsense.status_code == 422


def test_an_account_in_another_household_is_a_404(world):
    _txn(world, world["theirs"], when=_d(0), amount=-1250, house=world["other"])
    refused = _match(world, [{"amount_minor": 1250, "date": _d(0),
                              "account_id": world["theirs"]["id"]}])
    assert refused.status_code == 404
    assert refused.json()["detail"] == "no such account"


def test_another_households_path_is_a_404(world):
    refused = _match(world, [{"amount_minor": 1250, "date": _d(0)}], house=world["other"])
    assert refused.status_code == 404


def test_fifty_queries_are_answered_and_fifty_one_refused(world):
    row = _txn(world, world["eur"], when=_d(0), amount=-1250)
    fifty = [{"ref": str(i), "amount_minor": 1250, "date": _d(0)} for i in range(50)]

    answered = _match(world, fifty)
    assert answered.status_code == 200, answered.text
    results = answered.json()["results"]
    assert [r["ref"] for r in results] == [str(i) for i in range(50)], "in the order sent"
    assert all(_ids(r) == [row["id"]] for r in results)

    assert _match(world, [*fifty, fifty[0]]).status_code == 422
    assert _match(world, []).status_code == 422


def test_a_window_past_a_fortnight_is_refused(world):
    refused = _match(world, [{"amount_minor": 1, "date": _d(0), "window_days": 15}])
    assert refused.status_code == 422


def test_matching_writes_nothing(world):
    _txn(world, world["eur"], when=_d(0), amount=-1250, payee="Mercadona")
    before = _counts(world["client"])
    answered = _match(world, [{"amount_minor": 1250, "date": _d(0), "text": "mercadona"}],
                      token=world["reader"])
    assert answered.status_code == 200, "a read key may match"
    assert len(answered.json()["results"][0]["candidates"]) == 1
    assert _counts(world["client"]) == before


# --------------------------------------------------------------------------- #
# Story 1, end to end: three receipts, stored, matched, linked
# --------------------------------------------------------------------------- #


def test_three_receipts_are_stored_matched_and_each_lands_on_its_row(world):
    client = world["client"]
    groceries = _txn(world, world["eur"], when=_d(0), amount=-4210, payee="Mercadona",
                     bank="COMPRA MERCADONA VALENCIA")
    dinner = _txn(world, world["eur"], when=_d(2), amount=-6850, payee="Casa Carmen")
    # Paid abroad on the pound card: the receipt says USD, the card says GBP.
    books = _txn(world, world["gbp"], when=_d(4), amount=-1733, payee="Strand Books")
    # Same amount as the groceries two days later: must lose on date.
    _txn(world, world["eur"], when=_d(2), amount=-4210, payee="Mercadona")

    read = [
        {"merchant": "MERCADONA", "total": "42.10", "currency": "EUR", "date": _d(0)},
        {"merchant": "Casa Carmen restaurante", "total": "68.50", "currency": "EUR",
         "date": _d(1)},
        {"merchant": "Strand Book Store", "total": "21.95", "currency": "USD", "date": _d(3)},
    ]
    stored = client.post(
        f"{V1}/households/{world['house']['id']}/receipts/batch",
        json={"receipts": [
            {"content_base64": base64.b64encode(as_bytes(receipt_image((61 + i, 83)))).decode(),
             "filename": f"r{i}.jpg", "extracted": one}
            for i, one in enumerate(read)
        ]},
        headers=_bearer(world["token"]),
    )
    assert stored.status_code == 201, stored.text
    receipt_ids = [one["receipt"]["id"] for one in stored.json()["stored"]]

    matched = _match(world, [
        {"ref": rid, "amount": one["total"], "currency": one["currency"],
         "date": one["date"], "text": one["merchant"]}
        for rid, one in zip(receipt_ids, read, strict=True)
    ])
    assert matched.status_code == 200, matched.text
    chosen = {r["ref"]: r["candidates"][0]["transaction_id"] for r in matched.json()["results"]}
    assert chosen == {
        receipt_ids[0]: groceries["id"],
        receipt_ids[1]: dinner["id"],
        receipt_ids[2]: books["id"],
    }

    for rid, tid in chosen.items():
        linked = client.post(f"{V1}/receipts/{rid}/link", json={"transaction_id": tid},
                             headers=_bearer(world["token"]))
        assert linked.status_code == 200, linked.text

    for rid, tid in chosen.items():
        on_row = client.get(f"/api/transactions/{tid}/receipts", headers=HEADERS).json()
        assert [r["id"] for r in on_row] == [rid]
    inbox = client.get(f"{V1}/households/{world['house']['id']}/receipts?unlinked=true",
                       headers=_bearer(world["token"])).json()
    assert inbox == []
