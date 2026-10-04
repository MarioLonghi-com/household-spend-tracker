"""Links an agent may make: work expenses and transfers (#134, stories 3 and 5).

Two households, two currencies, a read key and a write key. Every write test
reads the rows back from the database: a 200 says the route ran, not what it
did to the ledger.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import (
    AgentScope,
    BatchKind,
    Household,
    LinkSource,
    ReimbursementState,
    Transaction,
    TransferRejection,
    User,
)
from app.services import agent_keys as key_service
from app.services import transactions as txn_service
from app.services import transfers as transfer_service
from tests.conftest import HEADERS, _setup_owner

V1 = "/api/agent/v1"


@pytest.fixture()
def world(client):
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    away = client.post("/api/households", json={"name": "Away"}, headers=HEADERS).json()

    def account(where, name, currency, kind="checking"):
        return client.post(
            f"/api/households/{where['id']}/accounts",
            json={"name": name, "type": kind, "currency": currency},
            headers=HEADERS,
        ).json()["id"]

    accounts = {
        "santander": account(house, "Santander", "EUR"),
        "revolut": account(house, "Revolut", "EUR", "savings"),
        "monzo": account(house, "Monzo", "GBP"),
        "away": account(away, "Elsewhere", "EUR"),
    }

    tokens = {}
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, made["user"]["id"])
        row = own.get(Household, house["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=row.id):
            _key, tokens["write"] = key_service.issue(
                own, user=user, household=row, label="expenses clerk",
                agent_name="Claude Desktop", scope=AgentScope.write,
            )
            _key, tokens["read"] = key_service.issue(
                own, user=user, household=row, label="the analyst",
            )
        own.commit()

    return {
        "client": client, "house": house, "away": away,
        "accounts": accounts, "tokens": tokens, "user_id": made["user"]["id"],
    }


def _row(world, account, day, amount, payee, where="house"):
    made = world["client"].post(
        f"/api/households/{world[where]['id']}/transactions",
        json={
            "account_id": world["accounts"][account], "date": f"2026-07-{day:02d}",
            "amount": amount, "payee_name": payee, "uncategorised": True,
        },
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _call(world, method, path, *, key="write", **kwargs):
    return world["client"].request(
        method,
        f"{V1}/households/{world['house']['id']}{path}",
        headers={"authorization": f"Bearer {world['tokens'][key]}", **HEADERS},
        **kwargs,
    )


def _db(world) -> Session:
    return Session(world["client"].app_module.db_engine, expire_on_commit=False)


def _get(world, txn_id) -> Transaction:
    with _db(world) as own:
        return own.get(Transaction, txn_id)


# --------------------------------------------------------------------------- #
# The door
# --------------------------------------------------------------------------- #


def test_a_read_key_is_refused_every_write_and_nothing_moves(world):
    lunch = _row(world, "santander", 3, -1850, "Client lunch")
    back = _row(world, "revolut", 4, 1850, "Transfer in")

    writes = [
        ("PATCH", "/transactions/reimbursement",
         {"assignments": [{"transaction_id": lunch, "state": "expected"}]}),
        ("POST", "/transfers/link", {"pairs": [{"out_id": lunch, "in_id": back}]}),
        ("POST", "/transfers/reject", {"pairs": [{"out_id": lunch, "in_id": back}]}),
    ]
    for method, path, body in writes:
        refused = _call(world, method, path, key="read", json=body)
        assert refused.status_code == 403, (path, refused.text)

    assert _get(world, lunch).reimbursement is None
    assert _get(world, lunch).transfer_transaction_id is None
    with _db(world) as own:
        assert own.execute(select(TransferRejection)).first() is None

    # And the read route answers for the same key.
    assert _call(world, "GET", "/transfers/findings", key="read").status_code == 200


def test_another_households_path_is_a_404_for_every_route(world):
    other = world["away"]["id"]
    headers = {"authorization": f"Bearer {world['tokens']['write']}", **HEADERS}
    client = world["client"]
    for method, path, body in [
        ("PATCH", "transactions/reimbursement", {"assignments": [{"transaction_id": "x", "state": None}]}),
        ("GET", "transfers/findings", None),
        ("POST", "transfers/link", {"pairs": [{"out_id": "a", "in_id": "b"}]}),
        ("POST", "transfers/reject", {"pairs": [{"out_id": "a", "in_id": "b"}]}),
    ]:
        answer = client.request(
            method, f"{V1}/households/{other}/{path}", headers=headers, json=body
        )
        assert answer.status_code == 404, (path, answer.text)


# --------------------------------------------------------------------------- #
# Story 3: work expenses
# --------------------------------------------------------------------------- #


def test_refused_rows_are_named_and_the_others_still_apply(world):
    hotel = _row(world, "santander", 3, -24000, "Hotel Lisboa")
    taxi = _row(world, "monzo", 4, -3150, "Taxi")
    salary = _row(world, "santander", 5, 300000, "Employer SA")
    elsewhere = _row(world, "away", 6, -900, "Not yours", where="away")

    answer = _call(world, "PATCH", "/transactions/reimbursement", json={"assignments": [
        {"transaction_id": hotel, "state": "expected"},
        {"transaction_id": taxi, "state": "written_off"},
        {"transaction_id": salary, "state": "expected"},
        {"transaction_id": elsewhere, "state": "expected"},
        {"transaction_id": "invented", "state": "expected"},
    ]})
    assert answer.status_code == 200, answer.text
    body = answer.json()

    assert body["changed"] == 2
    assert body["unchanged"] == 0
    assert sorted(body["not_found"]) == sorted([elsewhere, "invented"])
    assert body["refused"] == [{"transaction_id": salary, "reason": txn_service.NOT_MONEY_OUT}]

    assert _get(world, hotel).reimbursement is ReimbursementState.expected
    assert _get(world, taxi).reimbursement is ReimbursementState.written_off
    assert _get(world, salary).reimbursement is None
    assert _get(world, elsewhere).reimbursement is None, "another household's row is untouched"

    again = _call(world, "PATCH", "/transactions/reimbursement", json={"assignments": [
        {"transaction_id": hotel, "state": "expected"},
        {"transaction_id": taxi, "state": None},
    ]}).json()
    assert (again["changed"], again["unchanged"]) == (1, 1)
    assert _get(world, taxi).reimbursement is None


def test_a_transfer_leg_is_refused_with_the_services_sentence(world):
    made = world["client"].post(
        f"/api/households/{world['house']['id']}/transfers",
        json={"from_account_id": world["accounts"]["santander"],
              "to_account_id": world["accounts"]["revolut"],
              "date": "2026-07-02", "amount": 5000},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    with _db(world) as own:
        leg = own.execute(
            select(Transaction).where(
                Transaction.account_id == world["accounts"]["santander"],
                Transaction.amount == -5000,
            )
        ).scalar_one()

    body = _call(world, "PATCH", "/transactions/reimbursement", json={"assignments": [
        {"transaction_id": leg.id, "state": "expected"},
    ]}).json()
    assert body["refused"] == [
        {"transaction_id": leg.id, "reason": txn_service.TRANSFER_IS_NOT_A_WORK_EXPENSE}
    ]
    assert body["changed"] == 0
    assert _get(world, leg.id).reimbursement is None


def test_settled_by_links_the_payment_and_an_agent_cannot_take_it_off(world):
    flight = _row(world, "santander", 3, -18000, "Iberia")
    dinner = _row(world, "santander", 4, -6000, "Dinner")
    refund = _row(world, "santander", 20, 18000, "Employer expenses")
    other_refund = _row(world, "monzo", 21, 2000, "Employer expenses")

    linked = _call(world, "PATCH", "/transactions/reimbursement", json={"assignments": [
        # Not flagged yet: linking a payment flags it `expected`.
        {"transaction_id": flight, "state": "expected", "settled_by": refund},
        {"transaction_id": dinner, "state": "expected", "settled_by": "invented"},
    ]}).json()
    assert linked["changed"] == 1
    assert [r["transaction_id"] for r in linked["refused"]] == [dinner]
    assert "not a transaction in this household" in linked["refused"][0]["reason"]
    assert _get(world, flight).reimbursed_by_id == refund
    assert _get(world, flight).reimbursement is ReimbursementState.expected
    assert _get(world, dinner).reimbursement is None

    # Four ways of taking the payment off; every one refused, link intact.
    for assignment in (
        {"transaction_id": flight, "state": "expected", "settled_by": None},
        {"transaction_id": flight, "state": "expected", "settled_by": other_refund},
        {"transaction_id": flight, "state": None},
        {"transaction_id": flight, "state": "written_off"},
    ):
        body = _call(world, "PATCH", "/transactions/reimbursement",
                     json={"assignments": [assignment]}).json()
        assert body["changed"] == 0, assignment
        assert [r["transaction_id"] for r in body["refused"]] == [flight], assignment
        assert _get(world, flight).reimbursed_by_id == refund, assignment
    assert _get(world, flight).reimbursement is ReimbursementState.expected

    # Naming the payment it already has is not a change, and not refused.
    same = _call(world, "PATCH", "/transactions/reimbursement", json={"assignments": [
        {"transaction_id": flight, "state": "expected", "settled_by": refund},
    ]}).json()
    assert (same["changed"], same["unchanged"], same["refused"]) == (0, 1, [])

    # The service's own sentence for writing off, not one of ours.
    written_off = _call(world, "PATCH", "/transactions/reimbursement", json={"assignments": [
        {"transaction_id": flight, "state": "written_off"},
    ]}).json()
    assert written_off["refused"][0]["reason"] == txn_service.PAID_CANNOT_BE_WRITTEN_OFF


def test_a_repeated_row_is_refused_whole(world):
    hotel = _row(world, "santander", 3, -24000, "Hotel")
    answer = _call(world, "PATCH", "/transactions/reimbursement", json={"assignments": [
        {"transaction_id": hotel, "state": "expected"},
        {"transaction_id": hotel, "state": None},
    ]})
    assert answer.status_code == 422
    assert _get(world, hotel).reimbursement is None


def test_history_shows_the_flag_as_the_keys(world):
    """Read off the change log exactly as a person's edit is: the column's
    own words, with the key beside the person whose authority it used."""
    hotel = _row(world, "santander", 3, -24000, "Hotel")
    batch_id = _call(world, "PATCH", "/transactions/reimbursement", json={"assignments": [
        {"transaction_id": hotel, "state": "expected"},
    ]}).json()["batch_id"]

    shown = world["client"].get(
        f"/api/households/{world['house']['id']}/batches", headers=HEADERS
    ).json()
    row = next(b for b in shown if b["id"] == batch_id)
    assert row["via"] == "Claude Desktop (expenses clerk)"
    assert row["actor_name"], "the person whose authority the key borrowed"
    assert "work expense" in row["detail"] and "expected" in row["detail"], row["detail"]


# --------------------------------------------------------------------------- #
# Story 5: transfers
# --------------------------------------------------------------------------- #


def test_findings_list_a_suggested_pair_with_both_legs(world):
    out = _row(world, "santander", 3, -5000, "To savings")
    into = _row(world, "revolut", 5, 5000, "From checking")

    body = _call(world, "GET", "/transfers/findings").json()
    assert body["pair_count"] == 1 and body["truncated"] is False
    pair = body["pairs"][0]
    assert (pair["out_leg"]["id"], pair["in_leg"]["id"]) == (out, into)
    assert pair["out_leg"]["amount_minor"] == -5000
    assert pair["out_leg"]["amount"] == "-50.00"
    assert pair["out_leg"]["currency"] == "EUR"
    assert pair["in_leg"]["account"] == "Revolut"
    assert pair["strength"] == "suggested"
    assert pair["why"]

    _row(world, "santander", 10, -700, "To savings")
    _row(world, "revolut", 10, 700, "From checking")
    capped = _call(world, "GET", "/transfers/findings", params={"limit": 1}).json()
    assert capped["pair_count"] == 2 and len(capped["pairs"]) == 1
    assert capped["truncated"] is True


def test_an_agents_link_is_unproven_and_is_not_history_for_the_matcher(world):
    out = _row(world, "santander", 3, -5000, "To savings")
    into = _row(world, "revolut", 5, 5000, "From checking")

    answer = _call(world, "POST", "/transfers/link",
                   json={"pairs": [{"out_id": out, "in_id": into}]})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["linked"] == [{"out_id": out, "in_id": into, "fx_rate": None}]

    for leg, other in ((out, into), (into, out)):
        row = _get(world, leg)
        assert row.transfer_transaction_id == other
        assert row.link_source is LinkSource.agent

    unproven = _call(world, "GET", "/transfers/findings").json()["unproven"]
    assert [(u["out_leg"]["id"], u["link_source"]) for u in unproven] == [(out, "agent")]
    assert "program" in unproven[0]["why"] and "not confirmed" in unproven[0]["why"]

    # A second pair between the same two accounts. Were the agent's link
    # history, this would be strong ("linked between them before").
    later_out = _row(world, "santander", 20, -3000, "To savings")
    later_in = _row(world, "revolut", 21, 3000, "From checking")
    with _db(world) as own:
        found = transfer_service.find(own, world["house"]["id"])
        pairs = {(p.out_leg.id, p.in_leg.id): p for p in [*found.strong, *found.suggested]}
    assert pairs[(later_out, later_in)].strength == "suggested"
    assert "before" not in pairs[(later_out, later_in)].why

    # A person keeping it is what makes it history -- the contrast that
    # proves the assertion above is about the source, not the fixture.
    kept = world["client"].post(f"/api/transactions/{out}/confirm-link", headers=HEADERS)
    assert kept.status_code == 204
    assert _get(world, out).link_source is LinkSource.person
    with _db(world) as own:
        found = transfer_service.find(own, world["house"]["id"])
        strong = {(p.out_leg.id, p.in_leg.id) for p in found.strong}
    assert (later_out, later_in) in strong
    assert _call(world, "GET", "/transfers/findings").json()["unproven"] == []


def test_an_agents_link_is_listed_even_when_a_row_names_the_other_account(world):
    """A `named` link whose row still names the other side is off the list;
    an agent's is not. The matcher linking on a name is evidence; a program
    linking what the matcher would not is a claim a person has to look at."""
    added = world["client"].post(
        f"/api/households/{world['house']['id']}/identifiers",
        json={"kind": "number", "value": "33334444", "account_id": world["accounts"]["revolut"]},
        headers=HEADERS,
    )
    assert added.status_code == 201, added.text
    out = _row(world, "santander", 3, -5000, "Savings")
    into = _row(world, "revolut", 4, 5000, "From checking")
    edited = world["client"].patch(
        f"/api/transactions/{out}", json={"memo": "TO A/C 33334444"}, headers=HEADERS
    )
    assert edited.status_code == 200, edited.text

    _call(world, "POST", "/transfers/link", json={"pairs": [{"out_id": out, "in_id": into}]})
    unproven = _call(world, "GET", "/transfers/findings").json()["unproven"]
    assert [(u["out_leg"]["id"], u["link_source"]) for u in unproven] == [(out, "agent")]

    # The contrast: the same link recorded as `named` is vouched for by the
    # row, so it leaves the list -- which proves the row really does name it.
    with _db(world) as own:
        leg = own.get(Transaction, out)
        with batch(own, kind=BatchKind.manual, actor_id=world["user_id"],
                   household_id=leg.household_id):
            for row in (leg, own.get(Transaction, into)):
                row.link_source = LinkSource.named
        own.commit()
    assert _call(world, "GET", "/transfers/findings").json()["unproven"] == []


def test_a_cross_currency_pair_links_with_its_rate(world):
    out = _row(world, "santander", 3, -10000, "To Monzo")
    into = _row(world, "monzo", 4, 8500, "From Santander")

    body = _call(world, "POST", "/transfers/link",
                 json={"pairs": [{"out_id": into, "in_id": out}]}).json()
    # Sent the wrong way round; the legs come back as the money moved.
    assert body["linked"] == [{"out_id": out, "in_id": into, "fx_rate": "0.85"}]
    assert _get(world, out).transfer_fx_rate == "0.85"
    assert _get(world, into).transfer_fx_rate == "0.85"


def test_refused_pairs_are_named_and_the_others_still_link(world):
    good_out = _row(world, "santander", 3, -5000, "To savings")
    good_in = _row(world, "revolut", 4, 5000, "From checking")
    same_account = _row(world, "santander", 5, 5000, "Refund")
    short_in = _row(world, "revolut", 6, 4999, "Almost")
    elsewhere = _row(world, "away", 6, 5000, "Not yours", where="away")

    body = _call(world, "POST", "/transfers/link", json={"pairs": [
        {"out_id": good_out, "in_id": good_in},
        {"out_id": good_out, "in_id": short_in},       # already linked, one line up
        {"out_id": _row(world, "santander", 7, -5000, "X"), "in_id": same_account},
        {"out_id": _row(world, "santander", 8, -5000, "Y"), "in_id": short_in},
        {"out_id": good_out, "in_id": elsewhere},
    ]}).json()

    assert body["linked"] == [{"out_id": good_out, "in_id": good_in, "fx_rate": None}]
    assert body["not_found"] == [elsewhere]
    reasons = [r["reason"] for r in body["refused"]]
    assert len(reasons) == 3
    assert "already a transfer" in reasons[0]
    assert "same account" in reasons[1]
    assert "same amount" in reasons[2]
    assert _get(world, short_in).transfer_transaction_id is None
    assert _get(world, same_account).transfer_transaction_id is None
    assert _get(world, elsewhere).transfer_transaction_id is None


def test_reject_removes_the_suggestion_and_blocks_an_agents_link(world):
    out = _row(world, "santander", 3, -5000, "Refund?")
    into = _row(world, "revolut", 4, 5000, "Refund?")
    assert _call(world, "GET", "/transfers/findings").json()["pair_count"] == 1

    body = _call(world, "POST", "/transfers/reject",
                 json={"pairs": [{"out_id": out, "in_id": into}]}).json()
    assert (body["rejected"], body["unchanged"]) == (1, 0)
    with _db(world) as own:
        stored = own.execute(select(TransferRejection)).scalars().all()
        assert [(r.out_transaction_id, r.in_transaction_id) for r in stored] == [(out, into)]
    assert _call(world, "GET", "/transfers/findings").json()["pairs"] == []

    again = _call(world, "POST", "/transfers/reject",
                  json={"pairs": [{"out_id": into, "in_id": out}]}).json()
    assert (again["rejected"], again["unchanged"]) == (0, 1)

    linking = _call(world, "POST", "/transfers/link",
                    json={"pairs": [{"out_id": out, "in_id": into}]}).json()
    assert linking["linked"] == []
    assert "marked not a transfer" in linking["refused"][0]["reason"]
    assert _get(world, out).transfer_transaction_id is None


def test_a_linked_pair_cannot_be_rejected_by_an_agent(world):
    out = _row(world, "santander", 3, -5000, "To savings")
    into = _row(world, "revolut", 4, 5000, "From checking")
    _call(world, "POST", "/transfers/link", json={"pairs": [{"out_id": out, "in_id": into}]})

    body = _call(world, "POST", "/transfers/reject",
                 json={"pairs": [{"out_id": out, "in_id": into}]}).json()
    assert body["rejected"] == 0
    assert "unlinking" in body["refused"][0]["reason"]
    assert _get(world, out).transfer_transaction_id == into
    with _db(world) as own:
        assert own.execute(select(TransferRejection)).first() is None


def test_the_manifest_lists_the_four_routes(world):
    manifest = world["client"].get(
        f"{V1}/manifest", headers={"authorization": f"Bearer {world['tokens']['read']}"}
    ).json()
    listed = {(e["method"], e["path"]) for e in manifest["endpoints"]}
    base = f"{V1}/households/{{household_id}}"
    assert {
        ("PATCH", f"{base}/transactions/reimbursement"),
        ("GET", f"{base}/transfers/findings"),
        ("POST", f"{base}/transfers/link"),
        ("POST", f"{base}/transfers/reject"),
    } <= listed
