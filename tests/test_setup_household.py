"""The first-run wizard makes the first household in setup's one transaction (#458).

Each test walks setup through the real endpoints -- token, owner, a second
step, complete -- and then reads what was stored: the household's name, base
currency, date format, week start and colour, the starter categories kept and
the words they were seeded in, and the first account with its back-dated
opening balance. Both second steps (an authenticator and a passkey) and two
households of different shapes, so a value that leaked from one to the other
would show.

The refusals assert the other half: a household or account that cannot be made
leaves no owner, no household and no account behind, and setup can still be
run afterwards.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import pyotp
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    Category,
    CategoryGroup,
    Household,
    HouseholdMember,
    Instance,
    Payee,
    Transaction,
    User,
)
from tests.conftest import HEADERS, PASSWORD
from tests.passkey_world import passkeys_on  # noqa: F401 -- a fixture
from tests.soft_authenticator import SoftAuthenticator

ORIGIN = "https://testserver"


def _db(client) -> Session:
    return Session(client.app_module.db_engine)


def _begin(client, email: str = "owner@example.com") -> dict:
    import app.auth.setup as setup_service

    started = client.post(
        "/api/setup/begin",
        json={
            "token": setup_service.current_setup_token(),
            "email": email,
            "display_name": "Alex",
            "password": PASSWORD,
        },
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    return started.json()


def _with_code(client, extra: dict) -> object:
    body = _begin(client)
    enrolled = client.post(
        "/api/setup/enrol",
        json={"blob": body["blob"], "code": pyotp.TOTP(body["secret"]).at(int(time.time()))},
        headers=HEADERS,
    )
    assert enrolled.status_code == 200, enrolled.text
    return client.post(
        "/api/setup/complete",
        json={"blob": enrolled.json()["blob"], "codes_saved": True, **extra},
        headers=HEADERS,
    )


def _with_passkey(client, extra: dict) -> object:
    body = _begin(client)
    offer = client.post("/api/setup/passkey/options", json={"blob": body["blob"]}, headers=HEADERS)
    assert offer.status_code == 200, offer.text
    made = SoftAuthenticator().create(offer.json()["options"], origin=ORIGIN)
    return client.post(
        "/api/setup/complete",
        json={
            "blob": offer.json()["blob"],
            "codes_saved": True,
            "second_step": "passkey",
            "credential": made,
            **extra,
        },
        headers=HEADERS,
    )


def _households(client) -> list[Household]:
    with _db(client) as own:
        found = list(own.execute(select(Household).order_by(Household.name)).scalars())
        for one in found:
            own.expunge(one)
        return found


def _tree(client, household_id: str) -> dict[str, list[str]]:
    """Group name -> category names, in seeded order."""
    with _db(client) as own:
        groups = own.execute(
            select(CategoryGroup)
            .where(CategoryGroup.household_id == household_id)
            .order_by(CategoryGroup.sort_order)
        ).scalars()
        return {
            group.name: list(
                own.execute(
                    select(Category.name)
                    .where(Category.group_id == group.id)
                    .order_by(Category.sort_order)
                ).scalars()
            )
            for group in groups
        }


def _nothing_written(client) -> None:
    with _db(client) as own:
        for model in (User, Household, HouseholdMember, Account, Category, Instance):
            assert own.execute(select(func.count()).select_from(model)).scalar_one() == 0, model


# --------------------------------------------------------------------------- #
# The household, with each second step
# --------------------------------------------------------------------------- #


def test_setup_with_an_authenticator_makes_the_household_its_categories_and_a_back_dated_account(
    client,
):
    opened = date.today() - timedelta(days=3 * 365)
    done = _with_code(
        client,
        {
            "household": {
                "name": "Alex hushåll",
                "colour": "clay",
                "base_currency": "SEK",
                "date_format": "YYYY-MM-DD",
                "week_start": "sunday",
                "locale": "sv-SE",
                "categories": "essentials",
                "omit": ["seed.category.phone"],
            },
            "account": {
                "name": "Vardagskonto",
                "kind": "current",
                "currency": "EUR",
                "opening_balance": 123456,
                "opening_date": opened.isoformat(),
            },
        },
    )
    assert done.status_code == 200, done.text

    [household] = _households(client)
    assert household.name == "Alex hushåll"
    assert household.base_currency == "SEK"
    assert household.date_format == "YYYY-MM-DD"
    assert household.week_start == "sunday"
    assert household.theme == "clay"

    # Essentials, less Phone, in Swedish: three groups, ten categories.
    assert _tree(client, household.id) == {
        "Inkomster": ["Lön", "Övriga inkomster", "Ersättningar"],
        "Räkningar": ["Hyra / Bolån", "El", "Internet"],
        "Vardag": ["Matvaror", "Äta ute", "Transport", "Hälsa"],
    }

    with _db(client) as own:
        [account] = own.execute(select(Account)).scalars()
        assert (account.name, account.type.value, account.currency) == ("Vardagskonto", "checking", "EUR")
        [row] = own.execute(select(Transaction).where(Transaction.account_id == account.id)).scalars()
        assert (row.date, row.amount) == (opened, 123456)
        payee = own.get(Payee, row.payee_id)
        assert payee.name == "Ingående saldo"
        [member] = own.execute(select(HouseholdMember)).scalars()
        assert member.user_id == done.json()["id"]


def test_setup_with_a_passkey_makes_the_household_and_skipping_the_account_makes_none(
    client, passkeys_on  # noqa: F811
):
    done = _with_passkey(
        client,
        {
            "household": {
                "name": "Nossa casa",
                "base_currency": "BRL",
                "date_format": "DD/MM/YYYY",
                "week_start": "saturday",
                "locale": "pt-BR",
                "categories": "recommended",
                "omit": ["seed.category.gifts", "seed.group.non_monthly",
                         "seed.category.car_maintenance", "seed.category.home_maintenance",
                         "seed.category.annual_fees"],
            },
        },
    )
    assert done.status_code == 200, done.text

    [household] = _households(client)
    assert (household.name, household.base_currency, household.date_format, household.week_start) == (
        "Nossa casa", "BRL", "DD/MM/YYYY", "saturday"
    )
    # No colour chosen: the next free palette, which on a fresh instance is the first.
    assert household.theme == "moss"
    tree = _tree(client, household.id)
    assert list(tree) == ["Receitas", "Contas fixas", "Dia a dia", "Qualidade de vida"]
    assert sum(len(names) for names in tree.values()) == 21 - 1 - 3
    assert "Presentes" not in tree["Qualidade de vida"]
    with _db(client) as own:
        assert own.execute(select(func.count()).select_from(Account)).scalar_one() == 0


def test_start_empty_keeps_only_what_the_reports_need_and_a_later_household_gets_the_whole_tree(client):
    done = _with_code(
        client,
        {"household": {"name": "Solo", "base_currency": "EUR", "categories": "empty"}},
    )
    assert done.status_code == 200, done.text
    [first] = _households(client)
    assert _tree(client, first.id) == {"Income": ["Reimbursements"]}
    with _db(client) as own:
        group = own.execute(select(CategoryGroup)).scalar_one()
        category = own.execute(select(Category)).scalar_one()
        assert group.role.value == "income"
        assert category.role.value == "reimbursement"

    # A second household, made the usual way afterwards, with its own week start.
    second = client.post(
        "/api/households",
        json={"name": "Two", "base_currency": "GBP", "week_start": "sunday"},
        headers=HEADERS,
    )
    assert second.status_code == 201, second.text
    assert second.json()["week_start"] == "sunday"
    assert sum(len(names) for names in _tree(client, second.json()["id"]).values()) == 21


def test_without_a_household_setup_makes_the_owner_alone_as_before(client):
    done = _with_code(client, {})
    assert done.status_code == 200, done.text
    assert _households(client) == []
    # An account without a household is ignored, not an error.
    with _db(client) as own:
        assert own.execute(select(func.count()).select_from(User)).scalar_one() == 1


# --------------------------------------------------------------------------- #
# Refusals leave nothing behind
# --------------------------------------------------------------------------- #


def test_leaving_out_a_locked_category_is_refused_and_nothing_is_written(client):
    for locked in ("seed.group.income", "seed.category.reimbursements"):
        refused = _with_code(
            client,
            {"household": {"name": "H", "base_currency": "EUR", "omit": [locked]}},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "setup.category_locked"
        assert "params" not in refused.json() or not refused.json()["params"]
        _nothing_written(client)


def test_an_opening_date_in_the_future_is_refused_and_leaves_no_owner(client):
    tomorrow = date.today() + timedelta(days=1)
    refused = _with_code(
        client,
        {
            "household": {"name": "H", "base_currency": "EUR"},
            "account": {"name": "A", "opening_balance": 100, "opening_date": tomorrow.isoformat()},
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "account.opened_in_future"
    assert refused.json()["params"] == {"date": tomorrow.isoformat()}
    _nothing_written(client)


def test_a_failing_account_rolls_back_the_owner_and_the_household_and_setup_can_run_again(client):
    refused = _with_code(
        client,
        {
            "household": {"name": "H", "base_currency": "EUR"},
            "account": {"name": "A", "currency": "ZZZ"},
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "currency.not_iso_4217"
    _nothing_written(client)

    # The token still stands: the same wizard finishes now.
    done = _with_code(
        client,
        {
            "household": {"name": "H", "base_currency": "EUR"},
            "account": {"name": "A", "currency": "GBP", "opening_balance": -5000},
        },
    )
    assert done.status_code == 200, done.text
    with _db(client) as own:
        [account] = own.execute(select(Account)).scalars()
        assert account.currency == "GBP"
        [row] = own.execute(select(Transaction)).scalars()
        assert (row.amount, row.date) == (-5000, date.today())


def test_a_failing_household_rolls_back_the_owner(client):
    for household, code in (
        ({"name": "H", "base_currency": "QQQ"}, "currency.not_iso_4217"),
        ({"name": "H", "base_currency": "EUR", "colour": "neon"}, "household.unknown_theme"),
    ):
        refused = _with_code(client, {"household": household})
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == code
        _nothing_written(client)


# --------------------------------------------------------------------------- #
# What the wizard is told
# --------------------------------------------------------------------------- #


def test_setup_state_suggests_a_currency_format_and_week_start_per_country(client):
    state = client.get("/api/setup/state").json()
    by_code = {one["code"]: one for one in state["countries"]}
    assert by_code["ES"] | {"name": None} == {
        "code": "ES", "name": None, "currency": "EUR", "date_format": "DD/MM/YYYY", "week_start": "monday"
    }
    assert (by_code["US"]["currency"], by_code["US"]["date_format"], by_code["US"]["week_start"]) == (
        "USD", "MM/DD/YYYY", "sunday"
    )
    assert (by_code["SE"]["currency"], by_code["SE"]["date_format"]) == ("SEK", "YYYY-MM-DD")
    assert (by_code["BR"]["currency"], by_code["BR"]["week_start"]) == ("BRL", "sunday")
    assert by_code["AE"]["week_start"] == "saturday"
    assert by_code["AQ"]["currency"] is None
    assert "SEK" in state["currencies"] and "HRK" not in state["currencies"]
    assert [one["key"] for one in state["palettes"]][:2] == ["moss", "slate"]
    assert state["next_palette"] == "moss"
    assert state["install"]["edition"] == "web"
    assert state["install"]["data_dir"]


def test_setup_state_says_nothing_of_the_wizard_once_set_up(client):
    assert _with_code(client, {}).status_code == 200
    assert client.get("/api/setup/state").json() == {"setup_required": False}
