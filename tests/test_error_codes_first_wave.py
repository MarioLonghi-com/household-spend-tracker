"""The first wave of converted refusals (#57): the register, transfers, payees,
categories, money and the profile.

Each test drives a refusal through the API a person's browser uses and holds
three things at once: `detail` is the English sentence it always was, byte for
byte; `code` names it; `params` carry the raw values the sentence was built
from -- names as stored, counts as integers, never formatted text. An agent
still gets the sentence alone.

Two of everything where it can matter: two accounts in two currencies, two
categories in a group, two rows on the category being deleted.
"""

from __future__ import annotations

import re

import pytest
from starlette.requests import Request

from app.money import MoneyError, from_milliunits, to_minor
from tests.conftest import HEADERS
from tests.test_error_codes import _row, _two_currencies


def _house(world) -> str:
    return world["household"]["id"]


def _group_with_two_categories(client, world) -> tuple[dict, dict, dict]:
    group = client.post(
        f"/api/households/{_house(world)}/category-groups",
        json={"name": "Workshop"},
        headers=HEADERS,
    )
    assert group.status_code == 201, group.text
    made = []
    for name in ("Timber", "Varnish"):
        one = client.post(
            f"/api/households/{_house(world)}/categories",
            json={"group_id": group.json()["id"], "name": name},
            headers=HEADERS,
        )
        assert one.status_code == 201, one.text
        made.append(one.json())
    return group.json(), made[0], made[1]


def _categorised(client, world, account, amount, category) -> dict:
    made = client.post(
        f"/api/households/{_house(world)}/transactions",
        json={
            "account_id": world[account]["id"],
            "date": "2026-02-03",
            "amount": amount,
            "category_id": category["id"],
        },
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    assert made.json()["category_id"] == category["id"]
    return made.json()


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #


def test_a_category_name_already_taken_sends_the_name_as_stored(client):
    world = _two_currencies(client)
    group, _timber, _varnish = _group_with_two_categories(client, world)
    answer = client.post(
        f"/api/households/{_house(world)}/categories",
        json={"group_id": group["id"], "name": "  Timber "},
        headers=HEADERS,
    )
    assert answer.status_code == 409
    assert answer.json() == {
        "detail": "there is already a category called 'Timber'",
        "code": "category.name_taken",
        "params": {"name": "Timber"},
    }


def test_deleting_a_category_in_use_sends_the_count_as_a_number(client):
    world = _two_currencies(client)
    _group, timber, varnish = _group_with_two_categories(client, world)
    # Two rows on it, in two currencies, and one on the other category that
    # must not be counted.
    _categorised(client, world, "checking", -2_150, timber)
    _categorised(client, world, "savings", -31_900, timber)
    _categorised(client, world, "checking", -6_000, varnish)

    answer = client.delete(f"/api/categories/{timber['id']}", headers=HEADERS)
    assert answer.status_code == 409
    assert answer.json() == {
        "detail": (
            "2 transactions are categorised as 'Timber'. "
            "Archive it instead, and they keep their category."
        ),
        "code": "category.in_use",
        "params": {"count": 2, "name": "Timber"},
    }


def test_deleting_a_group_that_still_holds_categories_counts_the_archived_ones(client):
    world = _two_currencies(client)
    group, _timber, varnish = _group_with_two_categories(client, world)
    archived = client.patch(f"/api/categories/{varnish['id']}", json={"archived": True}, headers=HEADERS)
    assert archived.json()["archived"] is True

    answer = client.delete(f"/api/category-groups/{group['id']}", headers=HEADERS)
    assert answer.status_code == 409
    assert answer.json() == {
        "detail": (
            "A group can only be deleted when there are no categories under it. "
            "'Workshop' still holds 2 categories, 1 of them archived. Move or delete them first."
        ),
        "code": "category.group_not_empty",
        "params": {"name": "Workshop", "held": 2, "archived": 1},
    }


# --------------------------------------------------------------------------- #
# The register
# --------------------------------------------------------------------------- #


def test_editing_a_locked_row_says_so_with_a_code(client):
    world = _two_currencies(client)
    row = _row(client, world, "savings", "2026-02-10", 52_500)
    locked = client.post(
        f"/api/accounts/{world['savings']['id']}/reconciliation",
        json={"statement_date": "2026-02-28", "statement_balance": 52_500, "transaction_ids": [row["id"]]},
        headers=HEADERS,
    )
    assert locked.status_code in (200, 201), locked.text

    answer = client.patch(f"/api/transactions/{row['id']}", json={"memo": "a note"}, headers=HEADERS)
    assert answer.status_code == 409
    assert answer.json() == {
        "detail": "that transaction is locked; set it back to cleared before editing it",
        "code": "transaction.locked",
        "params": {},
    }


def test_a_split_with_a_zero_part_says_so_and_leaves_the_row(client):
    world = _two_currencies(client)
    row = _row(client, world, "checking", "2026-02-10", -10_000)
    answer = client.post(
        f"/api/transactions/{row['id']}/split",
        json={"parts": [{"amount": -10_000}, {"amount": 0}]},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {
        "detail": "a part of a split cannot be zero",
        "code": "split.zero_part",
        "params": {},
    }
    register = client.get(
        f"/api/households/{_house(world)}/transactions",
        params={"account_id": world["checking"]["id"]},
        headers=HEADERS,
    )
    assert [one["amount"] for one in register.json()["transactions"]] == [-10_000]


# --------------------------------------------------------------------------- #
# Transfers
# --------------------------------------------------------------------------- #


def test_linking_two_rows_in_one_account_says_so_with_a_code(client):
    world = _two_currencies(client)
    out = _row(client, world, "checking", "2026-03-01", -7_500)
    back = _row(client, world, "checking", "2026-03-02", 7_500)
    answer = client.post(
        f"/api/households/{_house(world)}/transfers/link",
        json={"pairs": [{"first_id": out["id"], "second_id": back["id"]}]},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {
        "detail": "both of those are in the same account; a transfer moves between two",
        "code": "transfer.link_same_account",
        "params": {},
    }


def test_a_same_currency_transfer_with_two_amounts_is_one_code_from_either_door(client):
    """`create_transfer` and `link` word it differently and mean the same thing."""
    world = _two_currencies(client)
    created = client.post(
        f"/api/households/{_house(world)}/transfers",
        json={
            "from_account_id": world["checking"]["id"],
            "to_account_id": world["card"]["id"],
            "date": "2026-01-15",
            "amount": 30_000,
            "to_amount": 20_000,
        },
        headers=HEADERS,
    )
    out = _row(client, world, "checking", "2026-03-01", -30_000)
    back = _row(client, world, "card", "2026-03-01", 20_000)
    linked = client.post(
        f"/api/households/{_house(world)}/transfers/link",
        json={"pairs": [{"first_id": out["id"], "second_id": back["id"]}]},
        headers=HEADERS,
    )
    assert (created.status_code, linked.status_code) == (422, 422)
    assert created.json()["detail"] == "both sides of a same-currency transfer must match"
    assert linked.json()["detail"] == "the two sides of a same-currency transfer must be the same amount"
    assert {created.json()["code"], linked.json()["code"]} == {"transfer.sides_differ"}


# --------------------------------------------------------------------------- #
# Payee rules
# --------------------------------------------------------------------------- #


def test_a_rule_that_is_not_a_regex_sends_the_reason_raw(client):
    world = _two_currencies(client)
    answer = client.post(
        f"/api/households/{_house(world)}/payee-rules",
        json={"match_type": "regex", "pattern": "(SQ", "payee_name": "Corner Cafe"},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    try:
        re.compile("(SQ")
    except re.error as exc:
        reason = str(exc)
    assert answer.json() == {
        "detail": f"that is not a valid regular expression: {reason}",
        "code": "payee.rule_not_a_regex",
        "params": {"reason": reason},
    }


@pytest.mark.parametrize(
    ("pattern", "replacement", "detail", "code", "params"),
    [
        (
            r"^SQ \*",
            r"\1",
            "that replacement uses \\1, and the pattern has no bracketed groups. "
            "Put brackets round the part you want to keep.",
            "payee.rule_template_no_groups",
            {"reference": "\\1"},
        ),
        (
            r"^(SQ) \*(.*)",
            r"\3",
            "that replacement uses \\3, and the pattern has only 2. "
            "Put brackets round the part you want to keep.",
            "payee.rule_template_too_few_groups",
            {"reference": "\\3", "groups": 2},
        ),
        (
            r"^SQ \*(?P<shop>.*)",
            r"\g<store>",
            "that replacement uses \\g<store>, and the pattern has no group called store",
            "payee.rule_template_no_such_group",
            {"reference": "\\g<store>", "name": "store"},
        ),
    ],
)
def test_a_rewrite_naming_a_missing_group_sends_what_it_named(
    client, pattern, replacement, detail, code, params
):
    world = _two_currencies(client)
    answer = client.post(
        f"/api/households/{_house(world)}/payee-rules",
        json={"match_type": "regex", "action": "rewrite", "pattern": pattern, "replacement": replacement},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json() == {"detail": detail, "code": code, "params": params}


# --------------------------------------------------------------------------- #
# The profile
# --------------------------------------------------------------------------- #


def test_a_wrong_current_password_is_a_refused_proof_with_a_code(client):
    _two_currencies(client)
    answer = client.post(
        "/api/me/password",
        json={"current_password": "not the password at all", "new_password": "another long passphrase"},
        headers=HEADERS,
    )
    assert answer.status_code == 401
    assert answer.headers["X-Refused"] == "proof"
    assert answer.json() == {
        "detail": "that isn't your current password",
        "code": "profile.wrong_password",
        "params": {},
    }


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("call", "detail", "code", "params"),
    [
        (lambda: to_minor("nan", "EUR"), "not a monetary value: 'nan'", "money.not_a_value", {"value": "nan"}),
        (
            lambda: to_minor("9" * 25, "SEK"),
            f"{'9' * 25!r} is too large to record as money",
            "money.too_large",
            {"value": "9" * 25},
        ),
        (
            lambda: from_milliunits(12_345, "gbp"),
            "12345 thousandths is not a whole number of GBP minor units",
            "money.not_whole_minor_units",
            {"milliunits": 12_345, "currency": "GBP"},
        ),
        (
            lambda: from_milliunits(1_500, "JPY"),
            "1500 thousandths is not a whole number of JPY minor units",
            "money.not_whole_minor_units",
            {"milliunits": 1_500, "currency": "JPY"},
        ),
    ],
)
def test_the_money_refusals_carry_raw_values(call, detail, code, params):
    with pytest.raises(MoneyError) as refused:
        call()
    assert (str(refused.value), refused.value.code, refused.value.params) == (detail, code, params)


# --------------------------------------------------------------------------- #
# Agents: the sentence alone
# --------------------------------------------------------------------------- #


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""})


@pytest.mark.parametrize(
    ("path", "carries_code"),
    [
        ("/api/agent/v1/households/h1/imports", False),
        ("/api/households/h1/imports", True),
    ],
)
def test_an_agent_gets_a_converted_refusal_as_it_always_did(client, path, carries_code):
    with pytest.raises(MoneyError) as refused:
        from_milliunits(12_345, "GBP")
    answer = client.app_module.handle_domain_error(_request(path), refused.value)
    expected: dict = {"detail": "12345 thousandths is not a whole number of GBP minor units"}
    if carries_code:
        expected |= {
            "code": "money.not_whole_minor_units",
            "params": {"milliunits": 12_345, "currency": "GBP"},
        }
    assert answer.body == client.app_module.JSONResponse(expected).body
