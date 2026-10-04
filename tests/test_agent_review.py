"""Reading the ledger to review it and analyse it (#134), asserted on the values.

Two households, three currencies -- EUR and GBP, and JPY because its exponent
is 0 and a formatter that assumed two decimals would print yen as cents. Every
assertion names a row by id or a figure computed from the fixture by hand,
because a review endpoint that returned 200 and the wrong row would pass any
test that only looked at the status.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import BatchKind, Household, ReimbursementState, Transaction, User
from app.services import accounts as account_service
from app.services import agent_keys as key_service
from app.services import categories as category_service
from app.services import payees as payee_service
from app.services import receipts as receipt_service
from app.services import transactions as txn_service
from tests.conftest import HEADERS, _setup_owner
from tests.receipt_fixtures import as_bytes, receipt_image


def d(month: int, day: int) -> date:
    return date(2026, month, day)


@pytest.fixture()
def world(client):
    """Household A with four accounts in three currencies; household B beside it.

    Mercadona (EUR) has six rows: four Groceries, one Restaurants -- the
    outlier -- and one uncategorised. 4 of 5 categorised is exactly 80%.
    Corner Shop has three rows and only two categorised, so no usual category.
    Tesco (GBP) and Lawson (JPY) are Groceries throughout.
    """
    owner = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    other = client.post("/api/households", json={"name": "Elsewhere"}, headers=HEADERS).json()

    ids: dict[str, str] = {}
    with Session(client.app_module.db_engine, expire_on_commit=False) as s:
        user = s.get(User, owner["user"]["id"])
        home = s.get(Household, house["id"])
        away = s.get(Household, other["id"])
        with batch(s, kind=BatchKind.manual, actor_id=user.id, household_id=home.id):
            eur = account_service.create_account(
                s, household=home, name="Santander", type="checking", currency="EUR",
                country="ES", institution="Banco Santander",
                opening_balance=100000, opening_date=d(6, 1),
            )
            gbp = account_service.create_account(
                s, household=home, name="Monzo", type="checking", currency="GBP",
                country="GB", institution="Monzo",
            )
            jpy = account_service.create_account(
                s, household=home, name="Rakuten", type="savings", currency="JPY",
                country="JP",
            )
            card = account_service.create_account(
                s, household=home, name="Visa", type="credit_card", currency="EUR",
                country="ES", institution="Banco Santander",
            )
            groceries = category_service.ensure(s, home.id, "Groceries", group_name="Food")
            restaurants = category_service.ensure(s, home.id, "Restaurants", group_name="Food")
            fuel = category_service.ensure(s, home.id, "Fuel", group_name="Transport")

            def payee(name):
                return payee_service.get_or_create(s, home.id, name)

            def row(key, account, when, amount, who, category, **extra):
                made = txn_service.create(
                    s, account=account, date=when, amount=amount,
                    payee=payee(who) if who else None, category=category, **extra,
                )
                ids[key] = made.id
                return made

            for n, day in enumerate((3, 10, 17, 24), start=1):
                row(f"merc_{n}", eur, d(7, day), -2000 - n, "Mercadona", groceries)
            row("merc_outlier", eur, d(8, 2), -4550, "Mercadona", restaurants,
                import_id="line-9", import_payee_original="MERCADONA 0231 MADRID")
            row("merc_uncat", eur, d(8, 9), -3120, "Mercadona", None)
            row("corner_1", eur, d(7, 5), -500, "Corner Shop", groceries)
            row("corner_2", eur, d(7, 6), -600, "Corner Shop", fuel)
            row("corner_uncat", eur, d(7, 7), -700, "Corner Shop", None)
            for n, day in enumerate((4, 11, 18), start=1):
                row(f"tesco_{n}", gbp, d(7, day), -1500 * n, "Tesco", groceries)
            row("lawson", jpy, d(7, 20), -1500, "Lawson", groceries)
            row("salary", eur, d(7, 28), 250000, "Employer", None)
            hotel = row("hotel", card, d(8, 15), -18000, "Hotel", None)
            txn_service.set_reimbursement(s, hotel, state=ReimbursementState.expected)
            taxi = row("taxi", gbp, d(8, 16), -4000, "Taxi", None)
            txn_service.set_reimbursement(s, taxi, state=ReimbursementState.written_off)

            out_leg, in_leg = txn_service.create_transfer(
                s, source=eur, destination=gbp, date=d(8, 20), amount=10000, to_amount=8600
            )
            ids["fx_out"], ids["fx_in"] = out_leg.id, in_leg.id
            yen_out, yen_in = txn_service.create_transfer(
                s, source=eur, destination=jpy, date=d(9, 1), amount=12345, to_amount=20000
            )
            ids["yen_out"], ids["yen_in"] = yen_out.id, yen_in.id
            same_out, same_in = txn_service.create_transfer(
                s, source=eur, destination=card, date=d(8, 30), amount=5000
            )
            ids["same_out"], ids["same_in"] = same_out.id, same_in.id

            for size in ((200, 300), (220, 320)):
                prepared = receipt_service.prepare(as_bytes(receipt_image(size), "JPEG"))
                receipt_service.store(
                    s, household_id=home.id, prepared=prepared,
                    transaction_id=ids["merc_outlier"], uploaded_by_id=user.id,
                )

        with batch(s, kind=BatchKind.manual, actor_id=user.id, household_id=away.id):
            theirs = account_service.create_account(
                s, household=away, name="Elsewhere EUR", type="checking", currency="EUR"
            )
            elsewhere_groceries = category_service.ensure(
                s, away.id, "Groceries", group_name="Food"
            )
            for n in range(4):
                made = txn_service.create(
                    s, account=theirs, date=d(7, 3 + n), amount=-999,
                    payee=payee_service.get_or_create(s, away.id, "Mercadona"),
                    category=elsewhere_groceries,
                )
                ids[f"theirs_{n}"] = made.id

        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=home.id):
            _key, token = key_service.issue(s, user=user, household=home, label="the reviewer")
        s.commit()
        ids.update(
            eur=eur.id, gbp=gbp.id, jpy=jpy.id, card=card.id,
            groceries=groceries.id, restaurants=restaurants.id, fuel=fuel.id,
            groceries_name=groceries.full_name, restaurants_name=restaurants.full_name,
            mercadona=payee("Mercadona").id,
            stored_fx_rate=s.get(Transaction, ids["fx_out"]).transfer_fx_rate,
            stored_yen_rate=s.get(Transaction, ids["yen_out"]).transfer_fx_rate,
        )

    return {"token": token, "house": house, "other": other, "ids": ids}


def _get(client, world, path: str, *, household: str | None = None, **params):
    house = household or world["house"]["id"]
    return client.get(
        f"/api/agent/v1/households/{house}{path}",
        params=params,
        headers={"authorization": f"Bearer {world['token']}"},
    )


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #


def test_a_register_row_carries_what_a_reviewer_needs(client, world):
    ids = world["ids"]
    body = _get(client, world, "/register", payee_id=ids["mercadona"]).json()

    assert body["total"] == 6
    rows = {row["id"]: row for row in body["rows"]}
    outlier = rows[ids["merc_outlier"]]
    assert outlier["amount_minor"] == -4550
    assert outlier["amount"] == "-45.50"
    assert outlier["currency"] == "EUR"
    assert outlier["category_id"] == ids["restaurants"]
    assert outlier["category_name"] == ids["restaurants_name"]
    assert outlier["category_name"].endswith(": Restaurants")
    assert outlier["bank_text"] == "MERCADONA 0231 MADRID"
    assert outlier["receipt_count"] == 2
    assert outlier["is_transfer"] is False
    assert rows[ids["merc_uncat"]]["receipt_count"] == 0


def test_a_yen_row_is_formatted_with_no_decimals(client, world):
    body = _get(client, world, "/register", account_id=world["ids"]["jpy"]).json()
    lawson = next(row for row in body["rows"] if row["id"] == world["ids"]["lawson"])

    assert lawson["amount_minor"] == -1500
    assert lawson["amount"] == "-1,500"
    assert lawson["currency"] == "JPY"


def test_transfer_legs_and_work_expenses_say_what_they_are(client, world):
    ids = world["ids"]
    legs = _get(client, world, "/register", source="transfer").json()
    assert {row["id"] for row in legs["rows"]} == {
        ids["fx_out"], ids["fx_in"], ids["yen_out"], ids["yen_in"],
        ids["same_out"], ids["same_in"],
    }
    assert all(row["is_transfer"] and row["category_id"] is None for row in legs["rows"])

    owed = _get(client, world, "/register", reimbursement="owed").json()
    assert [row["id"] for row in owed["rows"]] == [ids["hotel"]]
    assert owed["rows"][0]["reimbursement"] == "expected"
    off = _get(client, world, "/register", reimbursement="off").json()
    assert [row["id"] for row in off["rows"]] == [ids["taxi"]]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"uncategorised": "true"},
        {"search": "mercadona"},
        {"since": "2026-07-10", "until": "2026-08-09"},
        {"amount": "45.50"},
        {"amount": "1500"},
        {"source": "imported"},
        {"reimbursement": "work"},
        {"cleared": "reconciled"},
    ],
)
def test_the_same_filter_selects_the_same_rows_as_the_persons_register(client, world, params):
    """Same code, not merely the same intentions: the ids are compared whole."""
    house = world["house"]["id"]
    human = client.get(f"/api/households/{house}/transactions", params=params, headers=HEADERS)
    assert human.status_code == 200, human.text
    agent = _get(client, world, "/register", limit=1000, **params).json()

    expected = {row["id"] for row in human.json()["transactions"]}
    assert {row["id"] for row in agent["rows"]} == expected
    assert agent["total"] == len(expected)


def test_the_same_account_filter_repeats_the_way_the_register_does(client, world):
    ids = world["ids"]
    params = [("account_id", ids["gbp"]), ("account_id", ids["jpy"])]
    human = client.get(
        f"/api/households/{world['house']['id']}/transactions", params=params, headers=HEADERS
    ).json()
    agent = _get(client, world, "/register", account_id=[ids["gbp"], ids["jpy"]]).json()

    assert {r["id"] for r in agent["rows"]} == {r["id"] for r in human["transactions"]}
    assert {r["currency"] for r in agent["rows"]} == {"GBP", "JPY"}


def test_category_and_payee_narrow_to_exactly_those_ids(client, world):
    ids = world["ids"]
    body = _get(client, world, "/register", category_id=ids["restaurants"]).json()
    assert [row["id"] for row in body["rows"]] == [ids["merc_outlier"]]


def test_paging_covers_every_row_once_in_a_stable_order(client, world):
    whole = _get(client, world, "/register", limit=1000).json()
    seen: list[str] = []
    offset = 0
    while True:
        page = _get(client, world, "/register", limit=4, offset=offset).json()
        assert page["total"] == whole["total"]
        seen.extend(row["id"] for row in page["rows"])
        if not page["has_more"]:
            assert page["next_offset"] is None
            break
        offset = page["next_offset"]

    assert seen == [row["id"] for row in whole["rows"]]
    assert len(seen) == len(set(seen)) == whole["total"]
    dates = [row["date"] for row in whole["rows"]]
    assert dates == sorted(dates, reverse=True)


# --------------------------------------------------------------------------- #
# Categorisation review
# --------------------------------------------------------------------------- #


def test_the_mis_categorised_row_is_found_by_id(client, world):
    ids = world["ids"]
    body = _get(client, world, "/categorisation/review").json()

    assert [row["transaction_id"] for row in body["outliers"]] == [ids["merc_outlier"]]
    outlier = body["outliers"][0]
    assert outlier["category_id"] == ids["restaurants"]
    assert outlier["usual_category_id"] == ids["groceries"]
    assert outlier["usual_category_name"] == ids["groceries_name"]
    assert outlier["usual_category_name"].endswith(": Groceries")
    assert outlier["usual_share_percent"] == 80
    assert outlier["amount_minor"] == -4550
    assert body["outliers_total"] == 1


def test_an_uncategorised_row_is_offered_its_payees_usual_category(client, world):
    ids = world["ids"]
    body = _get(client, world, "/categorisation/review").json()

    with_usual = {row["transaction_id"]: row for row in body["uncategorised_with_usual"]}
    assert set(with_usual) == {ids["merc_uncat"]}
    assert with_usual[ids["merc_uncat"]]["usual_category_id"] == ids["groceries"]

    without = {row["transaction_id"] for row in body["uncategorised_without_usual"]}
    # Corner Shop has no clear usual; the rest have no categorised history at all.
    assert without == {ids["corner_uncat"], ids["salary"], ids["hotel"], ids["taxi"]}
    assert body["uncategorised_without_usual_total"] == 4


def test_the_payee_distribution_is_counted_exactly(client, world):
    ids = world["ids"]
    body = _get(client, world, "/categorisation/review").json()
    payees = {p["payee_name"]: p for p in body["payees"]}

    merc = payees["Mercadona"]
    assert (merc["row_count"], merc["categorised_count"], merc["uncategorised_count"]) == (6, 5, 1)
    assert [(c["category_id"], c["count"], c["share_percent"]) for c in merc["categories"]] == [
        (ids["groceries"], 4, 80),
        (ids["restaurants"], 1, 20),
    ]
    assert payees["Corner Shop"]["usual_category_id"] is None
    assert payees["Tesco"]["usual_category_id"] == ids["groceries"]
    # Lawson has one row, under min_rows: no profile.
    assert "Lawson" not in payees
    # Largest first.
    assert body["payees"][0]["payee_name"] == "Mercadona"


def test_a_stricter_threshold_takes_the_usual_category_away(client, world):
    body = _get(client, world, "/categorisation/review", dominant_percent=81).json()

    assert body["outliers"] == []
    assert body["uncategorised_with_usual"] == []
    assert world["ids"]["merc_uncat"] in {
        row["transaction_id"] for row in body["uncategorised_without_usual"]
    }


def test_transfer_legs_and_opening_balances_are_left_out_and_counted(client, world):
    ids = world["ids"]
    body = _get(client, world, "/categorisation/review").json()

    assert body["excluded_transfer_legs"] == 6
    assert body["excluded_system_payee_rows"] == 1
    every_id = {
        row["transaction_id"]
        for key in ("outliers", "uncategorised_with_usual", "uncategorised_without_usual")
        for row in body[key]
    }
    assert not every_id & {ids["fx_out"], ids["fx_in"], ids["same_out"], ids["same_in"]}
    names = {p["payee_name"] for p in body["payees"]}
    assert not any(name.startswith("Transfer") for name in names)
    assert "Opening balance" not in names


def test_the_lists_are_capped_and_the_totals_are_not(client, world):
    body = _get(client, world, "/categorisation/review", limit=1).json()

    assert len(body["uncategorised_without_usual"]) == 1
    assert body["uncategorised_without_usual_total"] == 4
    assert len(body["payees"]) == 1
    assert body["payees_total"] == 3


def test_the_window_narrows_the_history_too(client, world):
    # July alone: Mercadona is four Groceries rows and nothing disagrees.
    body = _get(client, world, "/categorisation/review", until="2026-07-31").json()
    assert body["outliers"] == []


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def test_income_expense_is_one_currency_and_the_persons_own_answer(client, world):
    house = world["house"]["id"]
    params = {"currency": "EUR", "since": "2026-07-01", "until": "2026-09-30"}
    agent = _get(client, world, "/reports/income-expense", **params).json()
    human = client.get(
        f"/api/households/{house}/reports/income-expense", params=params, headers=HEADERS
    ).json()
    assert agent == human

    assert agent["currency"] == "EUR"
    expense = -(2001 + 2002 + 2003 + 2004) - 4550 - 3120 - 500 - 600 - 700
    assert agent["expense"]["total_minor"] == expense
    assert agent["income"]["total_minor"] == 250000
    assert agent["net_total_minor"] == 250000 + expense
    # The EUR legs: one each of the GBP and JPY transfers, both of the card
    # one -- and the hotel, a work expense.
    assert agent["excluded"] == {"transfers": 4, "opening_balances": 0, "reimbursements": 1}


def test_income_expense_never_mixes_a_second_currency_in(client, world):
    gbp = _get(client, world, "/reports/income-expense", currency="GBP",
               since="2026-07-01", until="2026-09-30").json()

    assert gbp["currency"] == "GBP"
    assert gbp["expense"]["total_minor"] == -(1500 + 3000 + 4500) - 4000
    assert gbp["income"]["total_minor"] == 0
    # The EUR salary is not here, and nothing is a sum of the two currencies.
    assert "250000" not in str(gbp)


def test_reimbursements_per_currency(client, world):
    eur = _get(client, world, "/reports/reimbursements", currency="EUR").json()
    gbp = _get(client, world, "/reports/reimbursements", currency="GBP").json()

    assert (eur["outstanding"], eur["outstanding_count"], eur["written_off"]) == (18000, 1, 0)
    assert [row["id"] for row in eur["outstanding_rows"]] == [world["ids"]["hotel"]]
    assert (gbp["outstanding"], gbp["written_off"], gbp["written_off_count"]) == (0, 4000, 1)
    assert set(eur["available_currencies"]) == {"EUR", "GBP"}


def test_a_report_needs_a_currency(client, world):
    assert _get(client, world, "/reports/income-expense").status_code == 422


# --------------------------------------------------------------------------- #
# Observed rates
# --------------------------------------------------------------------------- #


def test_a_linked_cross_currency_pair_gives_its_exact_rate(client, world):
    ids = world["ids"]
    body = _get(client, world, "/fx/observed").json()

    assert body["total"] == 2
    by_out = {o["out_transaction_id"]: o for o in body["observations"]}
    pounds = by_out[ids["fx_out"]]
    assert pounds["in_transaction_id"] == ids["fx_in"]
    assert (pounds["from_currency"], pounds["from_amount_minor"]) == ("EUR", 10000)
    assert (pounds["to_currency"], pounds["to_amount_minor"]) == ("GBP", 8600)
    assert (pounds["from_account_id"], pounds["to_account_id"]) == (ids["eur"], ids["gbp"])
    assert pounds["rate"] == "0.86" == ids["stored_fx_rate"]
    assert pounds["from_amount"] == "100.00"

    yen = by_out[ids["yen_out"]]
    expected = str(Decimal(20000) / (Decimal(12345) / 100))
    assert yen["rate"] == expected == ids["stored_yen_rate"]
    assert yen["to_amount"] == "20,000"
    assert isinstance(yen["rate"], str)
    # The same-currency transfer is not evidence of any rate.
    assert ids["same_out"] not in by_out


def test_the_pair_filter_matches_either_direction_and_nothing_else(client, world):
    ids = world["ids"]
    one = _get(client, world, "/fx/observed", pair="gbp/eur").json()
    assert [o["out_transaction_id"] for o in one["observations"]] == [ids["fx_out"]]
    assert one["pair"] == "GBP/EUR"

    none = _get(client, world, "/fx/observed", until="2026-08-19").json()
    assert none["observations"] == [] and none["total"] == 0

    assert _get(client, world, "/fx/observed", pair="EURO").status_code == 422


# --------------------------------------------------------------------------- #
# Accounts: country, institution, liability
# --------------------------------------------------------------------------- #


def test_the_manifest_and_balances_say_where_an_account_is(client, world):
    ids = world["ids"]
    manifest = client.get(
        "/api/agent/v1/manifest", headers={"authorization": f"Bearer {world['token']}"}
    ).json()
    accounts = {a["id"]: a for a in manifest["accounts"]}
    assert (accounts[ids["eur"]]["country"], accounts[ids["eur"]]["institution"]) == (
        "ES", "Banco Santander",
    )
    assert accounts[ids["jpy"]]["institution"] is None
    assert accounts[ids["card"]]["is_liability"] is True
    assert accounts[ids["eur"]]["is_liability"] is False

    balances = {a["account_id"]: a for a in _get(client, world, "/balances").json()["accounts"]}
    assert balances[ids["gbp"]]["country"] == "GB"
    assert balances[ids["card"]]["is_liability"] is True
    # Card: -18000 hotel, +5000 transfer in.
    assert balances[ids["card"]]["balance_minor"] == -13000


# --------------------------------------------------------------------------- #
# Another household
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/register",
        "/categorisation/review",
        "/reports/income-expense?currency=EUR",
        "/reports/reimbursements?currency=EUR",
        "/fx/observed",
    ],
)
def test_another_households_id_is_a_404(client, world, path):
    route, _, query = path.partition("?")
    params = dict([query.split("=")]) if query else {}
    answer = _get(client, world, route, household=world["other"]["id"], **params)
    assert answer.status_code == 404


def test_another_households_rows_never_appear(client, world):
    ids = world["ids"]
    theirs = {ids[f"theirs_{n}"] for n in range(4)}
    rows = _get(client, world, "/register", limit=1000).json()["rows"]
    assert not theirs & {row["id"] for row in rows}
    review = _get(client, world, "/categorisation/review").json()
    merc = next(p for p in review["payees"] if p["payee_name"] == "Mercadona")
    assert merc["row_count"] == 6
