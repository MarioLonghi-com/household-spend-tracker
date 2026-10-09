"""The refusals made outside a `DomainError`, and the unset bucket's name (#267, part 3).

A schema 422, a cross-site write, a fresh instance's setup gate, a busy
ledger and a statement the library refused whole now carry a code beside the
same ``detail``. The name the server gives the rows with no category or no
payee carries one too. Agents get none of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from tests.conftest import HEADERS, _setup_owner
from tests.test_api import _household_with_accounts

ROOT = Path(__file__).resolve().parent.parent


def test_a_schema_refusal_keeps_its_errors_and_names_the_fields_raw(client):
    _setup_owner(client)
    answer = client.post(
        "/api/households", json={"name": "", "base_currency": "EURO"}, headers=HEADERS
    )
    assert answer.status_code == 422
    body = answer.json()
    assert isinstance(body["detail"], list) and len(body["detail"]) == 2, "pydantic's errors, as before"
    assert all("input" not in error for error in body["detail"])
    assert body["code"] == "request.invalid"
    assert body["params"] == {"fields": "body.name, body.base_currency"}


def test_a_cross_site_write_is_refused_with_a_code(client):
    _setup_owner(client)
    refused = client.post(
        "/api/households", json={"name": "Theirs"}, headers={"origin": "https://evil.example"}
    )
    assert (refused.status_code, refused.json()) == (
        403,
        {"detail": "that request did not come from this app", "code": "request.cross_origin", "params": {}},
    )


def test_a_fresh_instance_answers_with_a_code(client):
    answer = client.get("/api/households")
    assert (answer.status_code, answer.json()) == (
        503,
        {
            "detail": "this instance has not been set up yet; open /setup",
            "code": "setup.required",
            "params": {},
        },
    )


def _request(path: str, authorization: str | None = None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b""})


@pytest.mark.parametrize(
    ("path", "authorization", "coded"),
    [
        ("/api/households/h1/transactions", None, True),
        ("/api/agent/v1/households/h1/imports", None, False),
    ],
)
def test_a_busy_ledger_is_a_409_with_a_code_but_not_for_an_agent(client, path, authorization, coded):
    busy = OperationalError("UPDATE x", {}, Exception("database is locked"))
    answer = client.app_module.handle_operational_error(_request(path, authorization), busy)
    expected = {"detail": client.app_module.LEDGER_BUSY}
    if coded:
        expected |= {"code": "ledger.busy", "params": {}}
    assert answer.status_code == 409
    assert answer.body == client.app_module.JSONResponse(expected).body


def test_a_statement_refused_whole_says_why_as_a_code(client):
    world = _household_with_accounts(client)
    raw = (ROOT / "tests/statement_files/designed_bill.pdf").read_bytes()
    answer = client.post(
        f"/api/households/{world['household']['id']}/imports",
        data={"account_id": world["card"]["id"]},
        files={"file": ("designed_bill.pdf", raw, "application/pdf")},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    body = answer.json()
    assert body["detail"].startswith("no statement table was found in this PDF.")
    assert (body["code"], body["params"]) == ("statement.unreadable.pdf_no_table", {})


def test_a_statement_sentence_nobody_registered_gets_the_generic_code(client):
    from statements import UnreadableStatement

    answer = client.app_module.handle_unreadable_statement(
        _request("/api/households/h1/imports"), UnreadableStatement("a sentence from a newer library")
    )
    assert answer.body == client.app_module.JSONResponse(
        {"detail": "a sentence from a newer library", "code": "statement.unreadable", "params": {}}
    ).body


# --------------------------------------------------------------------------- #
# The unset bucket's name
# --------------------------------------------------------------------------- #


def test_a_breakdown_names_the_unset_bucket_with_a_code(client):
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    groups = client.get(f"/api/households/{house}/categories").json()
    groceries = next(one for group in groups for one in group["categories"] if one["name"] == "Groceries")
    rows = [
        # Filed under a category, with no payee: the category's "No payee" line.
        {"account_id": world["checking"]["id"], "amount": -4_250, "category_id": groceries["id"]},
        # A payee, with no category: the payee's "Uncategorised" line.
        {"account_id": world["card"]["id"], "amount": -1_999, "payee_name": "Kiosk", "uncategorised": True},
    ]
    for row in rows:
        made = client.post(
            f"/api/households/{house}/transactions", json={"date": "2026-01-15", **row}, headers=HEADERS
        )
        assert made.status_code == 201, made.text

    by_category = client.get(f"/api/households/{house}/stats/categories").json()["categories"]
    by_payee = client.get(f"/api/households/{house}/stats/payees").json()["payees"]
    no_payee = [one for stat in by_category for one in stat["payees"] if one["key"] is None]
    uncategorised = [one for stat in by_payee for one in stat["categories"] if one["key"] is None]
    assert [(one["name"], one["name_code"]) for one in no_payee] == [("No payee", "unset.payee")]
    assert [(one["name"], one["name_code"]) for one in uncategorised] == [("Uncategorised", "unset.category")]
    named = [one for stat in by_category for one in stat["payees"] if one["key"] is not None]
    assert all(one["name_code"] is None for one in named)


def test_a_report_names_the_uncategorised_row_with_a_code(client):
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": world["checking"]["id"], "date": "2026-01-15", "amount": -4_250, "uncategorised": True},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    report = client.get(
        f"/api/households/{house}/reports/income-expense?start=2026-01-01&end=2026-01-31&currency=EUR"
    )
    assert report.status_code == 200, report.text
    rows = [row for section in ("income", "expense") for row in report.json()[section]["rows"]]
    unset = [row for row in rows if row["key"] is None]
    assert [(row["name"], row["name_code"]) for row in unset] == [("Uncategorised", "unset.category")]
    assert all(row["name_code"] is None for row in rows if row["key"] is not None)
