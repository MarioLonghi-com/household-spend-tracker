"""Import refusals carry codes (#267, part 2).

A statement import, an accounts CSV and a One-time Import from YNAB refuse in
the sentences they always used, and beside each a ``code`` with raw
``params``: a time as ISO, a line number as a number, column names and
currency codes as the file wrote them.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.errors import ValidationError
from app.services.account_import import COLUMNS, import_accounts
from app.services.one_time_import import ynab_api
from tests.conftest import HEADERS
from tests.test_api import SANTANDER_CSV, _household_with_accounts, _upload


def test_the_same_file_twice_sends_when_and_as_what_raw(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    first = _upload(client, house, checking, SANTANDER_CSV).json()
    client.post(f"/api/households/{house}/imports/{first['batch_id']}/commit", json={}, headers=HEADERS)

    again = _upload(client, house, checking, SANTANDER_CSV, name="enero-copy.csv")
    assert again.status_code == 409
    body = again.json()
    assert body["detail"].startswith("this exact file was already imported into Checking on ")
    assert body["code"] == "import.already_imported"
    assert body["params"]["account"] == "Checking"
    assert body["params"]["filename"] == "enero.csv"
    # A timestamp, not "09 October 2026 at 10:00".
    assert body["params"]["at"][:4].isdigit() and "T" in body["params"]["at"]


def test_a_file_staged_for_one_account_is_named_for_that_account(client):
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    _upload(client, house, world["card"]["id"], SANTANDER_CSV)

    again = _upload(client, house, world["card"]["id"], SANTANDER_CSV)
    other = _upload(client, house, world["checking"]["id"], SANTANDER_CSV)

    assert again.status_code == 409
    assert (again.json()["code"], again.json()["params"]["account"]) == ("import.already_staged", "Visa")
    assert other.status_code == 201, "the other account has not seen it"


def test_nothing_sent_is_refused_with_a_code(client):
    world = _household_with_accounts(client)
    answer = client.post(
        f"/api/households/{world['household']['id']}/imports",
        data={"account_id": world["checking"]["id"]},
        headers=HEADERS,
    )
    assert (answer.status_code, answer.json()) == (
        409,
        {"detail": "send a file or some pasted text", "code": "import.send_something", "params": {}},
    )


def test_an_accounts_file_with_a_column_it_does_not_know_names_it_raw(session, owner, household):
    raw = (b"name,type,colour\nJoint current,checking,red\n")
    with pytest.raises(ValidationError) as refused:
        import_accounts(session, household=household, actor_id=owner.id, raw=raw, filename="a.csv", dry_run=True)
    assert str(refused.value).startswith("this file has a column this does not know: 'colour'.")
    assert refused.value.code == "account_import.unknown_column"
    assert refused.value.params == {"columns": "colour", "known": ", ".join(COLUMNS)}


def test_an_accounts_file_with_refused_rows_counts_them_as_numbers(session, owner, household, other_household):
    raw = (
        ",".join(COLUMNS)
        + "\nJoint current,checking,,,,,,,\n"
        + "No type,,,,,,,,\n"
        + "Pounds pot,savings,GBP,,,,,,\n"
    ).encode()
    with pytest.raises(ValidationError) as refused:
        import_accounts(session, household=household, actor_id=owner.id, raw=raw, filename="a.csv", dry_run=False)
    assert refused.value.code == "account_import.rows_refused"
    assert refused.value.params == {"refused": 1, "rows": 3}
    assert str(refused.value) == (
        "1 of 3 rows cannot be imported, so none were. Nothing has been changed; "
        "correct the file and try again."
    )


@pytest.mark.parametrize(
    ("token", "code"),
    [("", "ynab.token_needed"), ("   ", "ynab.token_needed")],
)
def test_ynab_without_a_token_is_refused_with_a_code_and_no_request(token, code):
    with pytest.raises(ynab_api.YnabError) as refused:
        ynab_api.get(token, "/plans")
    assert (str(refused.value), refused.value.code) == ("a YNAB personal access token is needed", code)


@pytest.mark.parametrize(
    ("change", "code", "params", "detail"),
    [
        ({"date_format": "QQ-MM-YY"}, "ynab.date_format_unknown", {"format": "QQ-MM-YY"},
         "'QQ-MM-YY' is not a date format this import reads"),
        ({"currency": "EURO"}, "ynab.currency_not_a_code", {"currency": "EURO"},
         "'EURO' is not a three-letter currency code"),
        ({"date_from": date(2026, 3, 1), "date_to": date(2026, 2, 1)}, "ynab.range_backwards", {},
         "the date range ends before it starts"),
    ],
)
def test_a_plan_the_engine_cannot_follow_says_why_with_raw_params(
    session, household, change, code, params, detail
):
    from app.services.one_time_import import engine

    plan = engine.Plan(**{"currency": "EUR", "date_format": "DD/MM/YYYY", "accounts": {}, "categories": {}, **change})
    source = engine.Source(accounts={}, rows=[], via="csv")
    with pytest.raises(ValidationError) as refused:
        engine._check_plan(session, household, source, plan)
    assert (str(refused.value), refused.value.code, refused.value.params) == (detail, code, params)
