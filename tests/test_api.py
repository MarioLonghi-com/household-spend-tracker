"""The HTTP surface, end to end.

The two tests that matter most here are the route-coverage pair at the bottom.
The previous build had twenty-eight household-scoped endpoints and nothing
checking that each of them actually scoped; this is what makes forgetting one
impossible to ship rather than merely unlikely.
"""

from __future__ import annotations

import inspect
import re
import time
from pathlib import Path

import pyotp
import pytest
from fastapi.routing import APIRoute

from app.auth import cookies as cookie_names
from tests.conftest import HEADERS, PASSWORD, _setup_owner  # noqa: F401

# --------------------------------------------------------------------------- #
# A fresh instance
# --------------------------------------------------------------------------- #


def test_a_fresh_instance_says_so_and_serves_nothing_else(client):
    assert client.get("/api/health").json()["setup_required"] is True
    assert client.get("/api/setup/state").json()["setup_required"] is True

    locked = client.get("/api/households")
    assert locked.status_code == 503
    assert "not been set up" in locked.json()["detail"]


def test_a_fresh_instance_cannot_be_signed_into(client):
    refused = client.post(
        "/api/session", json={"email": "anyone@gmail.com", "password": "x"}, headers=HEADERS
    )
    # 401, not 503: /api/session is deliberately exempt from the setup gate
    # (main.py PUBLIC_PREFIXES) so that signing in is always reachable, so this
    # request gets as far as the credential check and finds no such user. Both
    # answers were previously accepted, which pinned neither.
    assert refused.status_code == 401
    assert client.get("/api/me").status_code in (401, 503)


def test_the_wizard_creates_the_owner_and_signs_them_in(client):
    result = _setup_owner(client)
    assert result["user"]["role"] == "owner"
    assert result["user"]["email"] == "Jane.Doe@gmail.com"

    # The response set a session cookie, so the very next screen is the app.
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["display_name"] == "Jane"
    assert client.get("/api/health").json()["setup_required"] is False


def test_setup_is_gone_once_it_is_done(client):
    _setup_owner(client)
    again = client.post(
        "/api/setup/begin",
        json={"token": "anything", "email": "b@gmail.com", "display_name": "B", "password": PASSWORD},
        headers=HEADERS,
    )
    assert again.status_code == 409


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #


def test_a_trusted_browser_is_not_asked_for_a_code(client):
    """The wizard trusted this browser, so a fresh sign-in skips the code."""
    _setup_owner(client)
    client.delete("/api/session", headers=HEADERS)

    back = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert back.status_code == 200
    body = back.json()
    assert body["authenticated"] is True
    assert body["needs_code"] is False
    assert body["user"]["email"] == "Jane.Doe@gmail.com"
    assert body["user"]["role"] == "owner"
    assert client.get("/api/me").status_code == 200


def test_an_untrusted_browser_is_asked_for_a_code(client):
    result = _setup_owner(client)
    client.delete("/api/session", headers=HEADERS)
    client.cookies.delete(cookie_names.device_name("testserver"))

    first = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert first.json()["needs_code"] is True
    assert client.get("/api/me").status_code == 401

    # The *next* step, not this one: the code that enrolled the authenticator is
    # burned on completion, and `app/auth/totp.py` refuses a used code "for the
    # rest of its window and forever after" -- a second sign-in inside the same
    # thirty seconds is the intended cost of that rule, not a bug.
    code = pyotp.TOTP(result["secret"]).at(int(time.time()) + 30)
    second = client.post(
        "/api/session/code", json={"code": code, "trust_this_browser": True}, headers=HEADERS
    )
    assert second.status_code == 200, second.text
    assert client.get("/api/me").status_code == 200


def test_a_wrong_password_says_nothing_useful(client):
    _setup_owner(client)
    refused = client.post(
        "/api/session", json={"email": "janedoe@gmail.com", "password": "wrong"}, headers=HEADERS
    )
    assert refused.status_code == 401
    assert refused.json()["detail"] == "that email and password do not match"

    unknown = client.post(
        "/api/session", json={"email": "nobody@gmail.com", "password": "wrong"}, headers=HEADERS
    )
    assert unknown.json()["detail"] == refused.json()["detail"]


def test_a_cross_origin_write_is_refused(client):
    _setup_owner(client)
    refused = client.post(
        "/api/households",
        json={"name": "Theirs"},
        headers={"origin": "https://evil.example"},
    )
    assert refused.status_code == 403
    assert "did not come from this app" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Households and accounts
# --------------------------------------------------------------------------- #


def test_a_household_and_an_account_round_trip(client):
    _setup_owner(client)

    house = client.post(
        "/api/households", json={"name": "Doe-Smith"}, headers=HEADERS
    ).json()
    assert house["base_currency"] == "EUR"

    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    assert account["currency"] == "EUR", "defaults to the household's"
    assert account["balance"] == 0

    listed = client.get(f"/api/households/{house['id']}/accounts").json()
    assert [a["name"] for a in listed] == ["Checking"]


def test_an_account_is_created_with_its_bank_and_a_note(client):
    """The new-account panel sends both (#12); one account says, one does not."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Doe-Smith"}, headers=HEADERS).json()

    banked = client.post(
        f"/api/households/{house['id']}/accounts",
        json={
            "name": "Joint current",
            "type": "checking",
            "currency": "EUR",
            "institution": "Example Bank",
            "note": "The one the salaries land in",
        },
        headers=HEADERS,
    )
    assert banked.status_code in (200, 201), banked.text
    plain = client.post(
        f"/api/households/{house['id']}/accounts",
        json={
            "name": "Pounds",
            "type": "savings",
            "currency": "GBP",
            "institution": None,
            "note": None,
        },
        headers=HEADERS,
    )
    assert plain.status_code in (200, 201), plain.text
    assert (banked.json()["institution"], banked.json()["note"]) == (
        "Example Bank",
        "The one the salaries land in",
    )
    assert (plain.json()["institution"], plain.json()["note"]) == (None, None)

    # Read back, not echoed: what the list and the account itself now hold.
    listed = {a["name"]: a for a in client.get(f"/api/households/{house['id']}/accounts").json()}
    assert {name: (a["currency"], a["institution"]) for name, a in listed.items()} == {
        "Joint current": ("EUR", "Example Bank"),
        "Pounds": ("GBP", None),
    }
    stored = client.get(f"/api/accounts/{banked.json()['id']}").json()
    assert (stored["institution"], stored["note"]) == ("Example Bank", "The one the salaries land in")
    assert client.get(f"/api/accounts/{plain.json()['id']}").json()["note"] is None


@pytest.mark.parametrize("code", ["€€€", "12A", "E1R"])
def test_a_currency_code_that_is_not_three_letters_is_refused(client, code):
    """Three characters is not enough: `Intl` throws on these and blanks every screen (#193)."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Doe-Smith"}, headers=HEADERS).json()
    other = client.post(
        "/api/households", json={"name": "Second", "base_currency": "GBP"}, headers=HEADERS
    ).json()
    for h, currency in ((house, "EUR"), (other, "GBP")):
        made = client.post(
            f"/api/households/{h['id']}/accounts",
            json={"name": "Checking", "type": "checking", "currency": currency},
            headers=HEADERS,
        )
        assert made.status_code in (200, 201), made.text

    refused = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Odd", "type": "checking", "currency": code},
        headers=HEADERS,
    )
    assert refused.status_code == 422, refused.text
    listed = client.get(f"/api/households/{house['id']}/accounts").json()
    assert [(a["name"], a["currency"]) for a in listed] == [("Checking", "EUR")]

    patched = client.patch(
        f"/api/households/{other['id']}", json={"base_currency": code}, headers=HEADERS
    )
    assert patched.status_code == 422, patched.text
    households = {h["id"]: h for h in client.get("/api/households").json()}
    assert households[other["id"]]["base_currency"] == "GBP"
    assert households[house["id"]]["base_currency"] == "EUR"

    made = client.post(
        "/api/households", json={"name": "Third", "base_currency": code}, headers=HEADERS
    )
    assert made.status_code == 422, made.text
    assert {h["name"] for h in client.get("/api/households").json()} == {"Doe-Smith", "Second"}


#: A value for each required query parameter a household read takes. A new
#: required parameter fails the test below until it is given one here -- the
#: walk does not skip what it cannot call.
EMPTY_HOUSEHOLD_QUERY = {
    # The household's own base currency: the reports are per currency.
    "currency": "EUR",
    # The history of one row: asked about a row id that is not there, which an
    # empty household's every id is.
    "table": "transactions",
    "row_id": "f" * 32,
}


def test_every_screen_works_on_a_brand_new_household(client):
    """An empty household is the first thing anyone sees.

    A division by zero or a missing key in any of these is a blank screen on
    day one. Every GET the app serves under `/api/households/{household_id}` is
    called -- walked from the route table, as the membership check below is,
    rather than from a list somebody keeps: the list this replaced had eleven
    paths and missed the reports, receipts, transfers and categories (#106).
    """
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Empty"}, headers=HEADERS).json()

    prefix = "/api/households/{household_id}"
    reads = [
        route
        for route in _api_routes(client.app_module.app)
        if "GET" in route.methods
        and (route.served_path == prefix or route.served_path.startswith(prefix + "/"))
        and set(re.findall(r"\{(\w+)\}", route.served_path)) == {"household_id"}
    ]
    assert len(reads) >= 20, f"the walk found {len(reads)} household reads; it is not walking"

    failed = []
    for route in reads:
        wanted = [p.alias for p in route.dependant.query_params if p.field_info.is_required()]
        unknown = [name for name in wanted if name not in EMPTY_HOUSEHOLD_QUERY]
        if unknown:
            failed.append(f"{route.served_path} needs {unknown}: add them to EMPTY_HOUSEHOLD_QUERY")
            continue
        path = route.served_path.replace("{household_id}", house["id"])
        response = client.get(path, params={name: EMPTY_HOUSEHOLD_QUERY[name] for name in wanted})
        if response.status_code != 200:
            failed.append(f"{route.served_path} -> {response.status_code} {response.text[:200]}")
    assert not failed, "\n".join(failed)

    for path in ("/api/me", "/api/presence", "/api/households"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code} {response.text}"

    # And the register says so in the shape the client expects, rather than
    # merely answering 200 with something it cannot read.
    register = client.get(f"/api/households/{house['id']}/transactions").json()
    assert register["transactions"] == []
    assert register["total"] == 0
    assert client.get(f"/api/households/{house['id']}/accounts").json() == []
    income = client.get(
        f"/api/households/{house['id']}/reports/income-expense", params={"currency": "EUR"}
    )
    assert income.status_code == 200 and income.json()


def test_presence_shows_who_is_here(client):
    _setup_owner(client)
    online = client.get("/api/presence").json()["online"]
    assert [u["display_name"] for u in online] == ["Jane"]


# --------------------------------------------------------------------------- #
# Route coverage -- the two that make forgetting impossible
# --------------------------------------------------------------------------- #


def _api_routes(app) -> list[APIRoute]:
    """Every APIRoute, including those inside included routers.

    The walk in `test_agent_access`, which knows this FastAPI's included
    routers: each is one opaque `_IncludedRouter` whose endpoints hang off
    `original_router`, unprefixed. The walk that used to be here only knew
    `.routes` and `.router`, found the nine routes declared on the app itself,
    and so the membership check below passed by looking at almost nothing
    (found with #106). Each route carries `served_path`, the URL it answers on.
    """
    from tests.test_agent_access import _api_routes as walk

    return walk(app)


#: Path parameters that name an object rather than a household. Each one must be
#: resolved through load_for, which joins membership.
OBJECT_ID_PARAMS = {"account_id", "transaction_id", "payee_id", "rule_id", "batch_id"}


def test_every_household_scoped_route_declares_the_check(client):
    """Structural, so it cannot be fooled by a 422 that never reached the check."""
    from app.api.deps import current_agent, current_household
    from tests.test_agent_access import _dependencies_of

    offenders = []
    routes = _api_routes(client.app_module.app)
    assert len(routes) > 150, f"the walk found {len(routes)} routes; it is not walking"
    for route in routes:
        names = set(re.findall(r"\{(\w+)\}", route.path))
        if route.served_path.startswith("/api/agent/"):
            # A key is bound to one household, and an agent route reaches it
            # through `current_agent` and then `_house(...)` or `.load(...)`,
            # which refuse any other -- not through `current_household`, which
            # reads a session. `test_agent_access` holds the rest of that door.
            source = inspect.getsource(route.endpoint)
            if current_agent not in _dependencies_of(route):
                offenders.append(f"{sorted(route.methods)} {route.served_path} (no current_agent)")
            elif names and not re.search(r"_house\(|\.load\(|\.household\.id", source):
                offenders.append(f"{sorted(route.methods)} {route.served_path} (no household check)")
            continue
        if "household_id" in names:
            declared = {d.call for d in route.dependant.dependencies}
            sub = {
                s.call
                for d in route.dependant.dependencies
                for s in d.dependencies
            }
            if current_household not in declared | sub:
                offenders.append(f"{sorted(route.methods)} {route.path} (no current_household)")
        elif names & OBJECT_ID_PARAMS:
            source = inspect.getsource(route.endpoint)
            if "load_for(" not in source:
                offenders.append(f"{sorted(route.methods)} {route.path} (no load_for)")

    assert not offenders, "these routes resolve someone else's data without a membership check:\n" + "\n".join(offenders)


def test_a_non_member_is_told_it_does_not_exist(client):
    """Behavioural half. A 404 and not a 403, so no id is confirmed as real."""
    _setup_owner(client)
    mine = client.post("/api/households", json={"name": "Mine"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{mine['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()

    # A household id that is real, but not one this user belongs to. Simulated
    # by asking for an id that exists in shape but not in membership.
    stranger = "f" * 32
    assert client.get(f"/api/households/{stranger}").status_code == 404
    assert client.get(f"/api/households/{stranger}/accounts").status_code == 404
    assert client.get(f"/api/accounts/{stranger}").status_code == 404

    # And the real one still works, so the 404 above is about membership rather
    # than about everything being broken.
    assert client.get(f"/api/accounts/{account['id']}").status_code == 200


# --------------------------------------------------------------------------- #
# The register over HTTP
# --------------------------------------------------------------------------- #


def _household_with_accounts(client) -> dict:
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    checking = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    card = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Visa", "type": "credit_card"},
        headers=HEADERS,
    ).json()
    return {"household": house, "checking": checking, "card": card}


def test_entering_a_transaction_names_a_payee_by_typing_it(client):
    world = _household_with_accounts(client)
    created = client.post(
        f"/api/households/{world['household']['id']}/transactions",
        json={
            "account_id": world["checking"]["id"],
            "date": "2026-01-15",
            "amount": -4_250,
            "payee_name": "Carrefour",
            "memo": "Weekly shop",
        },
        headers=HEADERS,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["amount"] == -4_250
    assert body["payee_name"] == "Carrefour", "typing a name creates the payee"

    payees = client.get(f"/api/households/{world['household']['id']}/payees").json()
    assert [p["name"] for p in payees] == ["Carrefour"]


def test_the_register_carries_a_running_balance_for_one_account_in_date_order(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    for day, amount in (("2026-01-05", 100_000), ("2026-01-10", -25_000), ("2026-01-20", -5_000)):
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": day, "amount": amount},
            headers=HEADERS,
        )

    page = client.get(f"/api/households/{house}/transactions?account_id={checking}").json()
    assert page["has_running_balance"] is True
    assert page["total"] == 3
    # Newest first for display; the balance accumulates oldest-first.
    assert [t["running_balance"] for t in page["transactions"]] == [70_000, 75_000, 100_000]


def test_the_running_balance_is_absent_when_it_would_be_a_lie(client):
    """Across accounts, or under a filter that hides rows, the column would look
    plausible and be wrong."""
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": world["checking"]["id"], "date": "2026-01-05", "amount": -1_000},
        headers=HEADERS,
    )

    every_account = client.get(f"/api/households/{house}/transactions").json()
    assert every_account["has_running_balance"] is False
    assert every_account["transactions"][0]["running_balance"] is None

    filtered = client.get(
        f"/api/households/{house}/transactions?account_id={world['checking']['id']}&search=nothing"
    ).json()
    assert filtered["has_running_balance"] is False


def test_the_register_searches_memo_and_payee(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-05", "amount": -1_000, "payee_name": "Mercadona"},
        headers=HEADERS,
    )
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-06", "amount": -2_000, "memo": "dentist"},
        headers=HEADERS,
    )

    by_payee = client.get(f"/api/households/{house}/transactions?search=merca").json()
    assert by_payee["total"] == 1
    by_memo = client.get(f"/api/households/{house}/transactions?search=dent").json()
    assert by_memo["total"] == 1


def test_a_search_for_percent_or_underscore_means_those_characters(client):
    """Issue #239. The term went into LIKE unescaped, so "%" and "_" matched
    every row and "100%" matched "100" followed by anything."""
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    for day, memo in (("05", "100% cotton"), ("06", "1000 nails"), ("07", "ref_2024"),
                      ("08", "ref-2024")):
        made = client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": f"2026-01-{day}", "amount": -1_000,
                  "memo": memo},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text

    def memos(term: str) -> list[str]:
        answer = client.get(f"/api/households/{house}/transactions", params={"search": term})
        assert answer.status_code == 200, answer.text
        return sorted(row["memo"] for row in answer.json()["transactions"])

    assert memos("100%") == ["100% cotton"], "not 1000 nails"
    assert memos("%") == ["100% cotton"]
    assert memos("_") == ["ref_2024"], "only the memo with an underscore in it"
    assert memos("ref") == ["ref-2024", "ref_2024"], "an ordinary term still matches both"


def test_a_transfer_over_http_moves_money_between_accounts(client):
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": world["checking"]["id"], "date": "2026-01-01", "amount": 100_000},
        headers=HEADERS,
    )

    moved = client.post(
        f"/api/households/{house}/transfers",
        json={
            "from_account_id": world["checking"]["id"],
            "to_account_id": world["card"]["id"],
            "date": "2026-01-15",
            "amount": 30_000,
        },
        headers=HEADERS,
    )
    assert moved.status_code == 201, moved.text
    assert moved.json()["from"]["amount"] == -30_000
    assert moved.json()["to"]["amount"] == 30_000

    accounts = {a["name"]: a for a in client.get(f"/api/households/{house}/accounts").json()}
    assert accounts["Checking"]["balance"] == 70_000
    assert accounts["Visa"]["balance"] == 30_000


def test_a_bulk_edit_is_one_act_and_comes_back_in_one_piece(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    ids = [
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": "2026-01-05", "amount": -amount},
            headers=HEADERS,
        ).json()["id"]
        for amount in (1_000, 2_000, 3_000)
    ]

    edited = client.post(
        f"/api/households/{house}/transactions/bulk",
        json={"transaction_ids": ids, "payee_name": "Corrected", "cleared": "cleared"},
        headers=HEADERS,
    )
    assert edited.status_code == 200, edited.text
    rows = edited.json()["transactions"]
    assert {t["payee_name"] for t in rows} == {"Corrected"}
    assert {t["cleared"] for t in rows} == {"cleared"}
    assert edited.json()["skipped_transfer_legs"] == 0


def test_a_payee_rule_points_a_raw_string_at_a_payee(client):
    world = _household_with_accounts(client)
    house = world["household"]["id"]

    rule = client.post(
        f"/api/households/{house}/payee-rules",
        json={"match_type": "contains", "pattern": "CARREFOUR", "payee_name": "Carrefour"},
        headers=HEADERS,
    )
    assert rule.status_code == 201, rule.text

    listed = client.get(f"/api/households/{house}/payee-rules").json()
    assert [r["pattern"] for r in listed] == ["CARREFOUR"]
    assert listed[0]["enabled"] is True

    # The rule points at a payee that now exists by that name.
    payees = {p["name"]: p["id"] for p in client.get(f"/api/households/{house}/payees").json()}
    assert listed[0]["payee_id"] == payees["Carrefour"]


def test_a_broken_regex_rule_is_refused_when_it_is_written(client):
    world = _household_with_accounts(client)
    refused = client.post(
        f"/api/households/{world['household']['id']}/payee-rules",
        json={"match_type": "regex", "pattern": "([unclosed", "payee_name": "Anyone"},
        headers=HEADERS,
    )
    assert refused.status_code == 422
    assert "valid regular expression" in refused.json()["detail"]


def test_deleting_a_transaction_and_undoing_it_is_the_same_everywhere(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    created = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-05", "amount": -9_900, "memo": "mistake"},
        headers=HEADERS,
    ).json()

    assert client.delete(f"/api/transactions/{created['id']}", headers=HEADERS).status_code == 204
    page = client.get(f"/api/households/{house}/transactions").json()
    assert page["total"] == 0

    # The name promises undo, so undo it. Single edits are hidden from the batch
    # list by default, which is why this asks for them explicitly.
    batches = client.get(
        f"/api/households/{house}/batches?include_single_edits=true"
    ).json()
    deletion = next(one for one in batches if one["kind"] == "manual" and one["status"] == "applied")
    undone = client.post(
        f"/api/households/{house}/batches/{deletion['id']}/undo", json={}, headers=HEADERS
    )
    assert undone.status_code == 200, undone.json()

    back = client.get(f"/api/households/{house}/transactions").json()
    assert back["total"] == 1
    restored = back["transactions"][0]
    assert restored["amount"] == -9_900, "the amount came back changed"
    assert restored["memo"] == "mistake", "the memo did not survive the round trip"
    assert restored["date"] == "2026-01-05"
    assert restored["account_id"] == checking


# --------------------------------------------------------------------------- #
# Import over HTTP
# --------------------------------------------------------------------------- #

SANTANDER_CSV = (
    "Fecha;Concepto;Importe;Saldo\r\n"
    "05/01/2026;MERCADONA 1234;-45,20;8.810,07\r\n"
    "07/01/2026;NOMINA ENERO;2.100,00;10.910,07\r\n"
    "09/01/2026;SQ *EL BAR;-12,50;10.897,57\r\n"
)


def _upload(client, house: str, account: str, content: str, *, name="enero.csv", force=False):
    return client.post(
        f"/api/households/{house}/imports",
        data={"account_id": account, "force": str(force).lower()},
        files={"file": (name, content.encode(), "text/csv")},
        headers=HEADERS,
    )


def test_an_unreadable_file_answers_422_with_the_librarys_own_words(client):
    """`statements` refuses a file; the sentence it wrote reaches the person.

    The library has no status codes -- it raises `UnreadableStatement` and the
    app decides that is a 422. This is the only place that pairing can be seen,
    so a 500 with a traceback instead of a sentence fails here.
    """
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]

    answer = client.post(
        f"/api/households/{house}/imports",
        data={"account_id": checking, "force": "false"},
        files={"file": ("scan.pdf", b"%PDF-1.4\n" + b"0" * 200, "application/pdf")},
        headers=HEADERS,
    )

    assert answer.status_code == 422
    detail = answer.json()["detail"]
    assert "pdf" in detail.lower() or "statement" in detail.lower()
    assert "Traceback" not in detail


def test_a_statement_previews_before_it_writes_anything(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]

    preview = _upload(client, house, checking, SANTANDER_CSV)
    assert preview.status_code == 201, preview.text
    body = preview.json()

    assert body["detected"]["delimiter"] == ";"
    assert body["detected"]["date_format"] == "%d/%m/%Y"
    assert body["detected"]["decimal_separator"] == ","
    assert body["counts"]["created"] == 3
    assert len(body["lines"]) == 3

    # Nothing in the register yet.
    assert client.get(f"/api/households/{house}/transactions").json()["total"] == 0

    committed = client.post(
        f"/api/households/{house}/imports/{body['batch_id']}/commit", json={}, headers=HEADERS
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["created"] == 3
    assert client.get(f"/api/households/{house}/transactions").json()["total"] == 3


def test_the_same_file_twice_is_refused_in_words(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]

    first = _upload(client, house, checking, SANTANDER_CSV).json()
    client.post(f"/api/households/{house}/imports/{first['batch_id']}/commit", json={}, headers=HEADERS)

    again = _upload(client, house, checking, SANTANDER_CSV, name="enero-copy.csv")
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert "already imported into Checking" in detail
    assert "Nothing has been changed" in detail
    assert "enero.csv" in detail, "it names the file it was imported as"


def test_the_same_file_can_be_forced_through(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    first = _upload(client, house, checking, SANTANDER_CSV).json()
    client.post(f"/api/households/{house}/imports/{first['batch_id']}/commit", json={}, headers=HEADERS)

    forced = _upload(client, house, checking, SANTANDER_CSV, force=True)
    assert forced.status_code == 201
    # The per-row dedupe still catches every line.
    assert forced.json()["counts"]["duplicate_skipped"] == 3


def test_pasted_text_goes_through_the_same_path(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]

    pasted = client.post(
        f"/api/households/{house}/imports",
        data={"account_id": checking, "pasted": SANTANDER_CSV},
        headers=HEADERS,
    )
    assert pasted.status_code == 201, pasted.text
    assert pasted.json()["counts"]["created"] == 3
    assert pasted.json()["filename"] == "pasted"


def test_an_import_can_be_undone_from_the_batch_list(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]

    preview = _upload(client, house, checking, SANTANDER_CSV).json()
    client.post(
        f"/api/households/{house}/imports/{preview['batch_id']}/commit", json={}, headers=HEADERS
    )
    assert client.get(f"/api/households/{house}/transactions").json()["total"] == 3

    batches = client.get(f"/api/households/{house}/batches").json()
    applied = next(b for b in batches if b["kind"] == "import" and b["status"] == "applied")

    undone = client.post(
        f"/api/households/{house}/batches/{applied['id']}/undo", json={}, headers=HEADERS
    )
    assert undone.status_code == 200, undone.text
    assert undone.json()["kind"] == "undo"
    assert client.get(f"/api/households/{house}/transactions").json()["total"] == 0


def test_the_batch_list_hides_single_manual_edits_by_default(client):
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-05", "amount": -1_000},
        headers=HEADERS,
    )

    default = client.get(f"/api/households/{house}/batches").json()
    assert all(b["kind"] != "manual" for b in default), "one keystroke should not fill this screen"

    everything = client.get(f"/api/households/{house}/batches?include_single_edits=true").json()
    assert any(b["kind"] == "manual" for b in everything)


def test_an_import_is_one_entry_in_history_not_two(client):
    """Staging and applying are two visits to one operation.

    Opening a second batch to apply it would split the act across two rows --
    one holding the per-line verdicts, the other the actual changes -- and
    History would then offer two things to undo, one of which does nothing.
    """
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]

    preview = _upload(client, house, checking, SANTANDER_CSV).json()
    client.post(
        f"/api/households/{house}/imports/{preview['batch_id']}/commit", json={}, headers=HEADERS
    )

    imports = [b for b in client.get(f"/api/households/{house}/batches").json() if b["kind"] == "import"]
    assert len(imports) == 1, "one import, one entry"
    assert imports[0]["id"] == preview["batch_id"], "and it is the one that was previewed"
    assert imports[0]["summary"]["created"] == 3

    undone = client.post(
        f"/api/households/{house}/batches/{imports[0]['id']}/undo", json={}, headers=HEADERS
    )
    assert undone.status_code == 200, undone.text
    assert client.get(f"/api/households/{house}/transactions").json()["total"] == 0


def test_the_running_balance_is_the_accounts_not_the_pages(client):
    """Accumulating from zero made every figure on page two wrong, and every
    figure on page one of a long register too."""
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    for day, amount in (
        ("2026-01-01", 100_000),
        ("2026-01-02", -1_000),
        ("2026-01-03", -2_000),
        ("2026-01-04", -3_000),
    ):
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": day, "amount": amount},
            headers=HEADERS,
        )

    page_one = client.get(
        f"/api/households/{house}/transactions?account_id={checking}&limit=2"
    ).json()
    assert page_one["has_running_balance"] is True
    # Newest first: 94,000 then 97,000 -- the true balances, not 2 rows from zero.
    assert [t["running_balance"] for t in page_one["transactions"]] == [94_000, 97_000]

    page_two = client.get(
        f"/api/households/{house}/transactions?account_id={checking}&limit=2&offset=2"
    ).json()
    assert [t["running_balance"] for t in page_two["transactions"]] == [99_000, 100_000]


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_guessing_at_invented_addresses_does_not_lock_the_real_owner(client):
    """The per-address arm used to be keyed on the address alone, at five.

    So an unauthenticated stranger typed five made-up addresses and every real
    account on the instance was refused its own correct password -- renewably,
    for as long as they cared to keep it up. Behind `tailscale serve` every
    request shares one loopback peer, which made it one person's five typos
    away by accident as well.
    """
    _setup_owner(client)
    client.delete("/api/session", headers=HEADERS)

    for n in range(8):
        refused = client.post(
            "/api/session",
            json={"email": f"nobody{n}@gmail.com", "password": "wrong"},
            headers=HEADERS,
        )
        assert refused.status_code == 401, f"guess {n} answered {refused.status_code}"

    back = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert back.status_code == 200, back.json()


def test_five_wrong_passwords_do_lock_that_one_account(client):
    """The control that is meant to bite still bites, and says when it lifts.

    From a browser that is not trusted for the account: that is the guesser.
    A trusted one is judged against its own, higher ceiling (#203).
    """
    _setup_owner(client)
    client.delete("/api/session", headers=HEADERS)
    client.cookies.delete(cookie_names.device_name("testserver"))

    for _ in range(5):
        assert (
            client.post(
                "/api/session",
                json={"email": "janedoe@gmail.com", "password": "not it"},
                headers=HEADERS,
            ).status_code
            == 401
        )

    locked = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert locked.status_code == 429
    # The old formula always said 2 seconds for what is a fifteen-minute window,
    # so a client honouring the header hammered it 450 times over.
    wait = int(locked.headers["retry-after"])
    assert 840 <= wait <= 901, f"retry-after was {wait}, which is not when it lifts"


def test_attempts_older_than_the_retention_window_are_pruned(client):
    """`Access Control Investigation.md` says 30 days. Nothing was enforcing it."""
    from datetime import timedelta

    from app.auth import ratelimit
    from app.models import LoginAttempt, utcnow

    _setup_owner(client)

    from sqlalchemy.orm import Session

    import app.db as db

    engine = db.engine

    with Session(engine) as session:
        session.add(
            LoginAttempt(
                email_canonical="old@gmail.com",
                ip="127.0.0.1",
                ok=False,
                kind="password",
                at=utcnow() - timedelta(days=31),
            )
        )
        session.add(
            LoginAttempt(
                email_canonical="recent@gmail.com",
                ip="127.0.0.1",
                ok=False,
                kind="password",
                at=utcnow() - timedelta(days=1),
            )
        )
        session.commit()

    assert ratelimit.prune(engine) == 1

    with Session(engine) as session:
        left = [row.email_canonical for row in session.query(LoginAttempt).all()]
    assert "old@gmail.com" not in left
    assert "recent@gmail.com" in left


def test_the_enrolment_code_cannot_be_reused_to_sign_in(client):
    """It proved the pairing, so it is spent.

    `confirm_authenticator` checked the code without consuming it and the new
    user was created with `totp_last_counter` NULL, so for the 60-90 seconds it
    stayed inside its drift window the code shown on the enrolment screen was a
    working second factor for the account just created -- the instance owner,
    for the setup wizard. Contradicted `app/auth/totp.py`'s own promise that a
    used code is "refused for the rest of its window and forever after".
    """
    import app.auth.setup as setup_service

    result = _setup_owner(client)
    used = result["enrolment_code"]

    client.delete("/api/session", headers=HEADERS)
    client.cookies.clear()

    first = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert first.status_code == 200
    assert first.json()["needs_code"] is True, "a fresh browser is asked for a code"

    replayed = client.post("/api/session/code", json={"code": used}, headers=HEADERS)
    assert replayed.status_code == 401, (
        "the code used to enrol the authenticator was accepted a second time"
    )
    assert setup_service  # the import is what pins where the fix lives


def test_a_transaction_cannot_be_filed_under_the_wrong_household(client):
    """The URL names the household, so that is where the row belongs.

    `load_for` resolved the account through *any* household the caller was in,
    so posting household B's account_id to /households/A/transactions wrote the
    row into B while the batch and every change row were filed under A. Not a
    breach -- membership in both is still required -- but B's History never
    showed the act that created B's transaction.
    """
    _setup_owner(client)
    first = client.post("/api/households", json={"name": "First"}, headers=HEADERS).json()
    second = client.post("/api/households", json={"name": "Second"}, headers=HEADERS).json()

    elsewhere = client.post(
        f"/api/households/{second['id']}/accounts",
        json={"name": "Theirs", "type": "checking"},
        headers=HEADERS,
    ).json()

    refused = client.post(
        f"/api/households/{first['id']}/transactions",
        json={"account_id": elsewhere["id"], "date": "2026-01-05", "amount": -1_000},
        headers=HEADERS,
    )
    assert refused.status_code == 404, refused.text
    assert client.get(f"/api/households/{second['id']}/transactions").json()["total"] == 0


# --------------------------------------------------------------------------- #
# Recovery codes
# --------------------------------------------------------------------------- #


def test_a_recovery_code_gets_you_back_in_without_the_authenticator(client):
    """The wizard generated ten of these and nothing could spend them.

    With a NOT NULL totp_secret and no reset path, a lost phone was a permanent
    lockout whose only cure was hand-editing SQLite -- while the admin screen
    reported "recovery codes left", which read as a promise the API could not
    keep.
    """
    result = _setup_owner(client)
    codes = result["recovery_codes"]
    assert len(codes) == 10

    client.delete("/api/session", headers=HEADERS)
    client.cookies.delete(cookie_names.device_name("testserver"))

    first = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert first.json()["needs_code"] is True

    back = client.post("/api/session/recovery", json={"code": codes[0]}, headers=HEADERS)
    assert back.status_code == 200, back.text
    assert back.json()["user"]["email"] == "Jane.Doe@gmail.com"
    assert client.get("/api/me").status_code == 200


def test_a_recovery_code_works_once(client):
    result = _setup_owner(client)
    codes = result["recovery_codes"]

    def spend(code: str):
        client.cookies.delete(cookie_names.device_name("testserver"))
        client.post(
            "/api/session",
            json={"email": "janedoe@gmail.com", "password": PASSWORD},
            headers=HEADERS,
        )
        return client.post("/api/session/recovery", json={"code": code}, headers=HEADERS)

    assert spend(codes[0]).status_code == 200
    client.delete("/api/session", headers=HEADERS)

    again = spend(codes[0])
    assert again.status_code == 401
    assert "already been used" in again.json()["detail"]

    # A different one still works, and nine are left.
    assert spend(codes[1]).status_code == 200
    left = client.get("/api/admin/users").json()[0]["recovery_codes_left"]
    assert left == 8, f"two codes were spent, so eight should remain, not {left}"


def test_redeeming_a_recovery_code_forgets_every_trusted_browser(client):
    """If the phone is gone, "these browsers are still yours" is the assumption
    in doubt."""
    result = _setup_owner(client)

    # The wizard trusted this browser, so a plain sign-in would skip the code.
    client.delete("/api/session", headers=HEADERS)
    assert (
        client.post(
            "/api/session",
            json={"email": "janedoe@gmail.com", "password": PASSWORD},
            headers=HEADERS,
        ).json()["needs_code"]
        is False
    ), "precondition: this browser is trusted"

    client.delete("/api/session", headers=HEADERS)
    client.cookies.delete(cookie_names.device_name("testserver"))
    client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert (
        client.post(
            "/api/session/recovery", json={"code": result["recovery_codes"][0]}, headers=HEADERS
        ).status_code
        == 200
    )

    # Every trusted browser is gone, so even one that still holds its cookie is
    # asked for a factor again.
    client.delete("/api/session", headers=HEADERS)
    asked = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert asked.json()["needs_code"] is True


def test_the_schema_and_doc_viewers_are_off_by_default(client):
    """They leak no data, but they hand an unauthenticated visitor the complete
    route and schema inventory. `/redoc` was mounted although the app never
    asked for it.

    Asserting on the body rather than the status: the SPA fallback answers 200
    with the app shell for any unknown path, which is what it is for.

    Amended when the agent API landed. `/api/openapi.json` now HAS a route --
    it serves the schema to a caller holding a valid agent key, because an
    agent pointed at a deployed instance otherwise has nothing to read and has
    to be hand-fed the route inventory. The property this test exists to
    protect is unchanged and is asserted harder below: an **anonymous** caller
    still gets no inventory, and now gets an explicit 401 rather than a
    fallback page. The three doc viewers remain unregistered entirely.
    """
    for path in ("/api/openapi.json", "/api/docs", "/api/redoc", "/redoc", "/docs"):
        body = client.get(path).text.lower()
        assert '"openapi"' not in body, f"{path} still serves the schema"
        assert "swagger-ui" not in body, f"{path} still serves Swagger"
        assert "redoc.standalone" not in body, f"{path} still serves ReDoc"

    # The schema is refused outright rather than falling through to the shell,
    # so there is no question of it being reachable by another spelling.
    #
    # On a configured instance, because `gate_until_configured` answers 503 to
    # everything under /api before this is reached -- which is a refusal too,
    # but not the one under test.
    _setup_owner(client)
    client.cookies.clear()
    assert client.get("/api/openapi.json").status_code == 401

    # The doc viewers are not registered at all, and `openapi_url` is still off
    # -- what exists is our own route, not FastAPI's.
    paths = {route.path for route in client.app_module.app.routes if hasattr(route, "path")}
    assert not {"/api/docs", "/api/redoc", "/redoc"} & paths
    assert client.app_module.app.openapi_url is None, (
        "the anonymous inventory must stay shut; serve key holders from a route instead"
    )


# --------------------------------------------------------------------------- #
# The SPA fallback's containment guard
# --------------------------------------------------------------------------- #


def test_the_spa_fallback_cannot_be_walked_out_of(client):
    """In the previous build this was an unauthenticated path traversal that
    served the database.

    ASGI hands the raw path through without normalising `..`, so without the
    containment check in `main.py` this route serves any file the process can
    open. The Plan promised a CI test for it and never had one.

    **The request has to be built by hand.** httpx normalises `..` out of the
    path before it ever reaches the app, so the obvious TestClient version of
    this test passes just as happily with the guard deleted -- which is the
    shape of test CLAUDE.md warns about. This drives the ASGI callable directly,
    the way `curl --path-as-is` would.
    """
    import asyncio

    import app.main as main

    static = main.Path(main.__file__).parent / "static" / "dist"
    if not static.exists():
        import pytest

        pytest.skip("the client is not built, so the SPA route is not mounted")

    def raw_get(path: str) -> bytes:
        """One GET, with the path exactly as written."""
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }
        asyncio.run(main.app(scope, receive, send))
        return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")

    # The depths matter, and getting them wrong is how this test passes while
    # proving nothing: `app/static/dist` is three levels below the repo root, so
    # three `..` reaches CLAUDE.md and seven reaches /etc. With the guard
    # deleted, both of these really are served.
    for path, needle in (
        ("/../../../CLAUDE.md", b"# household-spend-tracker"),
        ("/../../../../../../../etc/passwd", b"root:x:"),
        ("/../../../requirements.txt", b"fastapi"),
    ):
        body = raw_get(path)
        assert needle not in body.lower(), f"{path} escaped the static root"
        assert b"<!doctype html>" in body.lower(), f"{path} did not fall through to the app shell"

    # Under /assets the StaticFiles mount answers instead of the SPA route. It
    # has its own containment check and refuses rather than falling through, so
    # only the first assertion applies.
    escaped = raw_get("/assets/../../../../CLAUDE.md")
    assert b"# household-spend-tracker" not in escaped.lower(), "the assets mount escaped its root"


def test_a_half_finished_sign_in_cannot_be_replayed(client):
    """`st_pending` used to be a sealed cookie and nothing else.

    That made it a bearer token: replayable, and portable to any browser. It is
    the password half of two-factor reduced to a five-minute transferable
    credential -- anyone who read it once needed only a code to finish a
    sign-in, without ever knowing the password. Claiming it now spends it.
    """
    result = _setup_owner(client)
    client.delete("/api/session", headers=HEADERS)
    client.cookies.delete(cookie_names.device_name("testserver"))

    first = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert first.json()["needs_code"] is True
    stolen = client.cookies.get(cookie_names.pending_name("testserver"))
    assert stolen, "the half-finished sign-in should set a cookie"

    code = pyotp.TOTP(result["secret"]).at(int(time.time()) + 30)
    assert (
        client.post(
            "/api/session/code", json={"code": code, "trust_this_browser": False}, headers=HEADERS
        ).status_code
        == 200
    )

    # Somebody else's browser, holding a copy of the cookie.
    client.cookies.clear()
    client.cookies.set(cookie_names.pending_name("testserver"), stolen, domain="testserver")
    replayed = client.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(result["secret"]).at(int(time.time()) + 60)},
        headers=HEADERS,
    )
    assert replayed.status_code == 401, "the pending cookie was accepted a second time"
    assert client.get("/api/me").status_code == 401


def test_a_wrong_code_spends_the_pending_sign_in_too(client):
    """One attempt per half-finished sign-in, not five minutes of them."""
    result = _setup_owner(client)
    client.delete("/api/session", headers=HEADERS)
    client.cookies.delete(cookie_names.device_name("testserver"))

    client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert client.post("/api/session/code", json={"code": "000000"}, headers=HEADERS).status_code == 401

    # The right code no longer helps: the pending row is gone, so the password
    # has to be presented again.
    right = pyotp.TOTP(result["secret"]).at(int(time.time()) + 30)
    again = client.post("/api/session/code", json={"code": right}, headers=HEADERS)
    assert again.status_code == 401
    assert "start again" in again.json()["detail"]


def test_a_payee_rule_can_be_turned_off_without_losing_it(client):
    """`payee_rules.enabled` was read by the loader and written by nothing.

    No route, no schema field, no screen control -- so the column and the query
    that filtered on it were both decoration, and the one test that mentioned it
    asserted a constant.
    """
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    payee = client.post(
        f"/api/households/{house['id']}/payees", json={"name": "Carrefour"}, headers=HEADERS
    ).json()
    rule = client.post(
        f"/api/households/{house['id']}/payee-rules",
        json={"match_type": "contains", "pattern": "CARREFOUR", "payee_id": payee["id"]},
        headers=HEADERS,
    ).json()
    assert rule["enabled"] is True

    off = client.patch(f"/api/payee-rules/{rule['id']}", json={"enabled": False}, headers=HEADERS)
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False

    # And it stops applying: the raw bank string survives instead of becoming
    # Carrefour.
    csv = b"Date,Description,Amount\n2026-01-05,CARREFOUR MADRID 4432,-12.34\n"
    preview = client.post(
        f"/api/households/{house['id']}/imports",
        data={"account_id": account["id"], "force": "false"},
        files={"file": ("statement.csv", csv, "text/csv")},
        headers=HEADERS,
    )
    assert preview.status_code == 201, preview.text
    client.post(
        f"/api/households/{house['id']}/imports/{preview.json()['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )
    rows = client.get(f"/api/households/{house['id']}/transactions").json()["transactions"]
    assert rows[0]["payee_name"] != "Carrefour", "a disabled rule still applied"

    # Turning it back on brings the pattern back, which is the point of not
    # having to delete it.
    on = client.patch(f"/api/payee-rules/{rule['id']}", json={"enabled": True}, headers=HEADERS)
    assert on.json()["enabled"] is True


def test_the_history_of_one_row_can_be_read(client):
    """"Why is this €45 here?" is what the audit log was sold on.

    `Change.household_id` was denormalised for this query, with a comment
    naming it, and `ix_changes_row_history` was built for it. Nothing read
    either until now.
    """
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    made = client.post(
        f"/api/households/{house['id']}/transactions",
        json={
            "account_id": account["id"],
            "date": "2026-01-05",
            "amount": -4_500,
            "memo": "as entered",
        },
        headers=HEADERS,
    ).json()
    client.patch(
        f"/api/transactions/{made['id']}", json={"memo": "corrected"}, headers=HEADERS
    )

    history = client.get(
        f"/api/households/{house['id']}/changes",
        params={"table": "transactions", "row_id": made["id"]},
    )
    assert history.status_code == 200, history.text
    entries = history.json()

    assert [e["op"] for e in entries] == ["update", "insert"], "newest first"
    assert entries[0]["before"]["memo"] == "as entered"
    assert entries[0]["after"]["memo"] == "corrected"
    assert entries[1]["after"]["amount"] == -4_500
    # Each entry names the batch it belonged to, so one read answers what
    # happened, when, and as part of what.
    assert entries[0]["batch"]["kind"] == "manual"
    assert entries[0]["batch"]["actor_id"]


def test_one_households_history_is_not_readable_from_another(client):
    _setup_owner(client)
    mine = client.post("/api/households", json={"name": "Mine"}, headers=HEADERS).json()
    theirs = client.post("/api/households", json={"name": "Theirs"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{mine['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    made = client.post(
        f"/api/households/{mine['id']}/transactions",
        json={"account_id": account["id"], "date": "2026-01-05", "amount": -100},
        headers=HEADERS,
    ).json()

    wrong_house = client.get(
        f"/api/households/{theirs['id']}/changes",
        params={"table": "transactions", "row_id": made["id"]},
    )
    assert wrong_house.status_code == 200
    assert wrong_house.json() == [], "a row's history leaked across households"

    unknown = client.get(
        f"/api/households/{mine['id']}/changes",
        params={"table": "not_a_table", "row_id": made["id"]},
    )
    assert unknown.status_code == 404


# --------------------------------------------------------------------------- #
# Register sorting
# --------------------------------------------------------------------------- #


def _register_fixture(client) -> dict:
    """Two accounts, and rows that sort differently under each column."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()

    def account(name: str) -> dict:
        return client.post(
            f"/api/households/{house['id']}/accounts",
            json={"name": name, "type": "checking"},
            headers=HEADERS,
        ).json()

    zebra = account("Zebra Bank")
    acme = account("Acme Bank")

    rows = [
        (zebra, "2026-03-01", -300, "Carrefour", "weekly"),
        (acme, "2026-01-15", -100, "Amazon", "books"),
        (zebra, "2026-02-10", -900, "Repsol", None),
    ]
    for acct, when, amount, payee, memo in rows:
        client.post(
            f"/api/households/{house['id']}/transactions",
            json={
                "account_id": acct["id"],
                "date": when,
                "amount": amount,
                "payee_name": payee,
                "memo": memo,
            },
            headers=HEADERS,
        )
    return {"house": house, "zebra": zebra, "acme": acme}


def _sorted_field(client, house_id, field, sort, direction="asc"):
    page = client.get(
        f"/api/households/{house_id}/transactions",
        params={"sort": sort, "direction": direction},
    ).json()
    return [t[field] for t in page["transactions"]]


def test_the_register_sorts_by_every_column(client):
    world = _register_fixture(client)
    house = world["house"]["id"]

    assert _sorted_field(client, house, "date", "date", "asc") == [
        "2026-01-15",
        "2026-02-10",
        "2026-03-01",
    ]
    assert _sorted_field(client, house, "date", "date", "desc") == [
        "2026-03-01",
        "2026-02-10",
        "2026-01-15",
    ]

    assert _sorted_field(client, house, "payee_name", "payee", "asc") == [
        "Amazon",
        "Carrefour",
        "Repsol",
    ]
    assert _sorted_field(client, house, "amount", "amount", "asc") == [-900, -300, -100]
    assert _sorted_field(client, house, "amount", "amount", "desc") == [-100, -300, -900]

    by_account = _sorted_field(client, house, "account_id", "account", "asc")
    assert by_account[0] == world["acme"]["id"], "Acme sorts before Zebra by name, not by id"
    assert by_account[-1] == world["zebra"]["id"]


def test_a_row_with_no_payee_or_memo_sorts_last_either_way(client):
    """Otherwise an empty cell reads as though it came before "A"."""
    world = _register_fixture(client)
    house = world["house"]["id"]

    for direction in ("asc", "desc"):
        memos = _sorted_field(client, house, "memo", "memo", direction)
        assert memos[-1] is None, f"the empty memo should be last when {direction}, got {memos}"


def test_the_running_balance_is_withheld_under_any_other_sort(client):
    """It is a sum down the page, so it means nothing in another order."""
    world = _register_fixture(client)
    house, zebra = world["house"]["id"], world["zebra"]["id"]

    default = client.get(
        f"/api/households/{house}/transactions", params={"account_id": zebra}
    ).json()
    assert default["has_running_balance"] is True
    assert default["transactions"][0]["running_balance"] is not None

    for sort, direction in (("amount", "asc"), ("payee", "asc"), ("date", "asc")):
        page = client.get(
            f"/api/households/{house}/transactions",
            params={"account_id": zebra, "sort": sort, "direction": direction},
        ).json()
        assert page["has_running_balance"] is False, f"{sort}/{direction} still claimed a balance"
        assert all(t["running_balance"] is None for t in page["transactions"])


# --------------------------------------------------------------------------- #
# The database browser at /db
# --------------------------------------------------------------------------- #


def _build_snapshot(client) -> None:
    """The snapshot the view serves, built the way `make snapshot` builds it."""
    import app.api.dbview as dbview
    import app.db as db
    from scripts.db_view import build

    source = Path(str(db.engine.url).split("///", 1)[1])
    dbview.snapshot_path().parent.mkdir(parents=True, exist_ok=True)
    build(source, dbview.snapshot_path())


def test_the_database_browser_refuses_a_signed_out_browser(client):
    """Datasette has no authentication of its own and will run arbitrary
    read-only SQL for whoever reaches it. Mounted here it gets the app's."""
    _setup_owner(client)
    client.delete("/api/session", headers=HEADERS)
    client.cookies.clear()

    for path in ("/db", "/db/", "/db/snapshot", "/db/snapshot/users.json"):
        refused = client.get(path)
        assert refused.status_code == 401, f"{path} answered {refused.status_code}"
        assert "sign in" in refused.json()["detail"]


def test_the_database_browser_refuses_a_member(client):
    """Raw tables have no notion of household: `transactions` is every
    household at once, so the 404-not-403 scoping the API relies on does not
    apply here. An owner already sees them all; a member must not."""
    from tests.test_invitations import _accept, _invite

    _setup_owner(client)
    created = _invite(client, role="member")
    client.cookies.clear()
    _accept(client, created["token"], email="member@gmail.com", name="Member")

    refused = client.get("/db/snapshot/users.json")
    assert refused.status_code == 403
    assert "only the owner" in refused.json()["detail"]


def test_the_database_browser_serves_the_owner_a_redacted_snapshot(client):
    """And never the live file: an owner reading their own ledger is expected,
    an owner pulling argon2 hashes out of a web console is not."""
    _setup_owner(client)
    _build_snapshot(client)

    page = client.get("/db/snapshot/users.json?_shape=array")
    assert page.status_code == 200, page.text
    rows = page.json()
    assert rows, "the owner should see the users table"
    assert rows[0]["email"] == "Jane.Doe@gmail.com", "real data is still there"
    assert rows[0]["password_hash"] == "-- redacted --"
    assert not rows[0]["totp_secret"], f"the TOTP secret survived: {rows[0]['totp_secret']!r}"

    # Including through the SQL console, which is the whole reason to redact
    # the file rather than hide a page.
    console = client.get(
        "/db/snapshot.json?sql=select+password_hash,totp_secret+from+users&_shape=array"
    )
    assert console.status_code == 200, console.text
    assert all(r["password_hash"] == "-- redacted --" for r in console.json())


def test_the_database_browser_is_not_mounted_without_a_snapshot(client):
    """Nothing is served until somebody deliberately builds one."""
    import app.api.dbview as dbview

    _setup_owner(client)
    dbview.snapshot_path().unlink(missing_ok=True)

    missing = client.get("/db/snapshot")
    assert missing.status_code == 404
    assert "snapshot" in missing.json()["detail"]


def test_the_database_browser_says_so_when_datasette_is_not_installed(client, monkeypatch):
    """`datasette` is a dev dependency, and the /db viewer is the one thing that
    needs it.

    The import is lazy and sits behind the session check and the snapshot check,
    so a normal production install never reaches it -- but an operator who ran
    `make snapshot` and opened /db got a bare 500 with an ImportError behind it,
    which reads as "the app is broken" rather than "install one package".
    """
    import builtins

    import app.api.dbview as dbview

    _setup_owner(client)
    _build_snapshot(client)

    real_import = builtins.__import__

    def without_datasette(name, *args, **kwargs):
        if name.startswith("datasette"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_datasette)
    # The instance caches the built Datasette, so a viewer already constructed
    # by an earlier test would never reach the import at all.
    monkeypatch.setattr(dbview.GuardedDatasette, "_inner", None, raising=False)

    answer = client.get("/db/snapshot")
    assert answer.status_code == 501
    assert "pip install datasette" in answer.json()["detail"]
    assert "the snapshot itself is fine" in answer.json()["detail"]


def test_the_snapshot_redacts_a_database_with_several_of_everything(client, tmp_path):
    """One row of each hides the bug this test exists for.

    `sessions.id_hash` and `invitations.token_hash` are unique, so blanking
    every row to the same literal fails on the *second* row -- and a database
    with one session, or none, never shows you that. This is the two-of-
    everything rule applied to a script.
    """
    import sqlite3

    import app.db as db
    from scripts.db_view import build

    _setup_owner(client)
    # Three sessions, three devices, three invitations, ten recovery codes.
    for _ in range(2):
        client.cookies.delete(cookie_names.session_name("testserver"))
        client.post(
            "/api/session",
            json={"email": "janedoe@gmail.com", "password": PASSWORD},
            headers=HEADERS,
        )
    for _ in range(3):
        client.post(
            "/api/admin/invitations",
            json={"role": "member", "email": None, "household_ids": []},
            headers=HEADERS,
        )

    source = Path(str(db.engine.url).split("///", 1)[1])
    destination = tmp_path / "snap.sqlite3"
    wiped = build(source, destination)

    assert wiped.get("invitations", 0) >= 3, f"expected several invitations, got {wiped}"
    assert wiped.get("recovery_codes", 0) >= 10

    snap = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        # Live auth state is emptied outright, not blanked.
        for table in ("sessions", "trusted_devices", "pending_sign_ins"):
            assert snap.execute(f"select count(*) from {table}").fetchone()[0] == 0, table

        # Rows worth reading survive, with the secret part gone and the unique
        # constraint intact.
        tokens = [r[0] for r in snap.execute("select token_hash from invitations")]
        assert len(tokens) == len(set(tokens)), "redaction collapsed a unique column"
        assert all(t.startswith("-- redacted --") for t in tokens), tokens

        user = snap.execute("select password_hash, totp_secret from users").fetchone()
        assert user[0] == "-- redacted --"
        assert not user[1]
        assert all(
            r[0] == "-- redacted --" for r in snap.execute("select code_hash from recovery_codes")
        )

        # And the ledger is untouched.
        assert snap.execute("select count(*) from households").fetchone()[0] >= 0
    finally:
        snap.close()


def test_an_imported_transaction_can_say_where_it_came_from(client):
    """The statement is gone; the row has to explain itself without it."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()

    csv = (
        b"Bokforingsdag,Referens,Beskrivning,Belopp,Produkt\n"
        b"2026-01-05,ICA NARA,ICA NARA,-200.00,Privatkonto\n"
    )
    preview = client.post(
        f"/api/households/{house['id']}/imports",
        data={"account_id": account["id"], "force": "false"},
        files={"file": ("swedbank-jan.csv", csv, "text/csv")},
        headers=HEADERS,
    )
    assert preview.status_code == 201, preview.text
    client.post(
        f"/api/households/{house['id']}/imports/{preview.json()['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )

    txn = client.get(f"/api/households/{house['id']}/transactions").json()["transactions"][0]
    origin = client.get(f"/api/transactions/{txn['id']}/origin")
    assert origin.status_code == 200, origin.text
    found = origin.json()

    assert found["filename"] == "swedbank-jan.csv"
    assert found["line_no"] == 2
    assert "ICA NARA" in found["raw"]
    # The columns the sniffer never used are the point of keeping this.
    assert found["bank"]["Produkt"] == "Privatkonto"
    assert found["payee_original"] == "ICA NARA"


def test_a_hand_entered_transaction_says_it_was_not_imported(client):
    """Not an error -- it is the answer to the question."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    made = client.post(
        f"/api/households/{house['id']}/transactions",
        json={"account_id": account["id"], "date": "2026-01-05", "amount": -500},
        headers=HEADERS,
    ).json()

    origin = client.get(f"/api/transactions/{made['id']}/origin")
    assert origin.status_code == 404
    assert "not imported" in origin.json()["detail"]


def test_the_register_sorts_by_how_a_row_got_here(client):
    """The Src column: a transfer, an import, or somebody typing.

    Three states out of two nullable columns, so the order has to be declared
    rather than fall out of the data. It is ranked, not alphabetical: sorting by
    a rendered letter would put Imported before Manual before Transfer, which is
    an accident of the alphabet and not a thing anyone wants to see.
    """
    world = _register_fixture(client)
    house = world["house"]["id"]

    # The fixture's three rows are all hand-entered. Add one of each other kind.
    upload = _upload(client, house, world["acme"]["id"], SANTANDER_CSV)
    client.post(
        f"/api/households/{house}/imports/{upload.json()['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )
    client.post(
        f"/api/households/{house}/transfers",
        json={
            "from_account_id": world["acme"]["id"],
            "to_account_id": world["zebra"]["id"],
            "date": "2026-04-01",
            "amount": 5_000,
        },
        headers=HEADERS,
    )

    def sources(direction: str) -> list[str]:
        page = client.get(
            f"/api/households/{house}/transactions?sort=source&direction={direction}",
            headers=HEADERS,
        ).json()
        return [
            "T" if row["transfer_account_id"] else "I" if row["import_id"] else "M"
            for row in page["transactions"]
        ]

    ascending = sources("asc")
    assert ascending == sorted(ascending, key="TIM".index), ascending
    assert ascending[0] == "T", "a transfer ranks first"
    assert ascending[-1] == "M", "a hand-entered row ranks last"

    assert sources("desc") == ascending[::-1]

    # All three kinds are actually present, or the assertions above are vacuous.
    assert set(ascending) == {"T", "I", "M"}


# --------------------------------------------------------------------------- #
# Household colours
# --------------------------------------------------------------------------- #


def _make_household(client, name: str) -> dict:
    return client.post("/api/households", json={"name": name}, headers=HEADERS).json()


def test_a_second_household_does_not_open_wearing_the_first_ones_colour(client):
    """The point of the feature, at the moment it would fail silently.

    Two ledgers that look identical is the thing this exists to prevent, so a
    new household arriving in the colour already on screen is not a cosmetic
    slip -- it is the feature not working.
    """
    _setup_owner(client)
    first = _make_household(client, "Ours")
    second = _make_household(client, "The flat")
    third = _make_household(client, "Parents")

    themes = [first["theme"], second["theme"], third["theme"]]
    assert len(set(themes)) == 3, themes


def test_a_household_arrives_with_its_colours_already_worked_out(client):
    """The browser applies colours; it never computes them."""
    _setup_owner(client)
    house = _make_household(client, "Ours")

    assert house["colours"]["light"]["accent"].startswith("#")
    assert house["colours"]["dark"]["accent"] != house["colours"]["light"]["accent"], (
        "a light and a dark scheme need different weights of the same colour"
    )
    assert set(house["colours"]["light"]) == set(house["colours"]["dark"])


def test_the_theme_can_be_changed_and_it_sticks(client):
    _setup_owner(client)
    house = _make_household(client, "Ours")
    assert house["theme"] == "moss"

    answer = client.patch(
        f"/api/households/{house['id']}", json={"theme": "plum"}, headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["theme"] == "plum"

    again = client.get(f"/api/households/{house['id']}", headers=HEADERS).json()
    assert again["theme"] == "plum"
    assert again["colours"]["light"]["accent"] == "#8d3466", "plum's own accent"


def test_an_accent_changes_the_accent_and_nothing_else(client):
    """A household can tint its own ledger. It cannot make it unreadable."""
    _setup_owner(client)
    house = _make_household(client, "Ours")
    plain = house["colours"]["light"]

    answer = client.patch(
        f"/api/households/{house['id']}", json={"accent": "#b03030"}, headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    tinted = answer.json()["colours"]["light"]

    assert tinted["accent"] != plain["accent"]
    for token in ("ink", "muted", "paper", "surface", "surface_2"):
        assert tinted[token] == plain[token], f"{token} is not a household's to change"


def test_a_hopeless_accent_is_made_to_work_rather_than_breaking_the_page(client):
    """Pure yellow cannot carry white text; it is used at a weight that can."""
    _setup_owner(client)
    house = _make_household(client, "Ours")

    answer = client.patch(
        f"/api/households/{house['id']}", json={"accent": "#ffff00"}, headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()

    assert body["accent"] == "#ffff00", "what was picked is what is stored"
    light = body["colours"]["light"]
    assert light["accent"] != "#ffff00", "and it is not what is shown"


def test_a_colour_that_is_not_a_colour_is_refused(client):
    _setup_owner(client)
    house = _make_household(client, "Ours")
    answer = client.patch(
        f"/api/households/{house['id']}", json={"accent": "chartreuse-ish"}, headers=HEADERS
    )
    assert answer.status_code == 422
    assert "not a colour" in answer.json()["detail"]


def test_an_unknown_palette_is_refused(client):
    _setup_owner(client)
    house = _make_household(client, "Ours")
    answer = client.patch(
        f"/api/households/{house['id']}", json={"theme": "neon"}, headers=HEADERS
    )
    assert answer.status_code == 422


def test_the_accent_can_be_given_back(client):
    _setup_owner(client)
    house = _make_household(client, "Ours")
    palette_accent = house["colours"]["light"]["accent"]

    client.patch(f"/api/households/{house['id']}", json={"accent": "#b03030"}, headers=HEADERS)
    back = client.patch(
        f"/api/households/{house['id']}", json={"clear_accent": True}, headers=HEADERS
    )
    assert back.status_code == 200, back.text
    assert back.json()["accent"] is None
    assert back.json()["colours"]["light"]["accent"] == palette_accent


def test_the_other_household_settings_can_be_changed_too(client):
    _setup_owner(client)
    house = _make_household(client, "Ours")

    answer = client.patch(
        f"/api/households/{house['id']}",
        json={"name": "The flat", "base_currency": "gbp", "note": "  rented  "},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["name"] == "The flat"
    assert body["base_currency"] == "GBP", "normalised, not stored as typed"
    assert body["note"] == "rented"


def test_the_themes_on_offer_are_the_ones_the_app_enforces(client):
    """The picker cannot offer a palette the server would refuse."""
    _setup_owner(client)
    house = _make_household(client, "Ours")

    offered = client.get("/api/themes", headers=HEADERS).json()
    assert len(offered) >= 4

    for palette in offered:
        answer = client.patch(
            f"/api/households/{house['id']}", json={"theme": palette["key"]}, headers=HEADERS
        )
        assert answer.status_code == 200, f"{palette['key']}: {answer.text}"


def test_a_stranger_cannot_repaint_your_household(client):
    """Same rule as every other household route: 404, not 403."""
    _setup_owner(client)
    house = _make_household(client, "Ours")

    client.cookies.clear()
    answer = client.patch(
        f"/api/households/{house['id']}", json={"theme": "plum"}, headers=HEADERS
    )
    assert answer.status_code == 401


# --------------------------------------------------------------------------- #
# Reconciliation, over HTTP
# --------------------------------------------------------------------------- #


def _reconcile_world(client) -> dict:
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    rows = []
    for when, amount in (("2026-01-15", -1_000), ("2026-02-20", -2_500), ("2026-02-20", 8_000)):
        rows.append(
            client.post(
                f"/api/households/{house}/transactions",
                json={"account_id": checking, "date": when, "amount": amount},
                headers=HEADERS,
            ).json()
        )
    return {"house": house, "checking": checking, "rows": rows}


def test_the_worksheet_says_what_is_left_to_prove(client):
    world = _reconcile_world(client)
    sheet = client.get(
        f"/api/accounts/{world['checking']}/reconciliation", headers=HEADERS
    ).json()

    assert sheet["locked_balance"] == 0
    assert sheet["last_statement_date"] is None
    # The opening balance is one of them: this account was made with one.
    ids = {one["id"] for one in sheet["candidates"]}
    assert {row["id"] for row in world["rows"]} <= ids
    assert sheet["currency"] == "EUR"


def test_a_statement_that_balances_locks_its_rows(client):
    world = _reconcile_world(client)
    sheet = client.get(
        f"/api/accounts/{world['checking']}/reconciliation?until=2026-02-20", headers=HEADERS
    ).json()
    total = sum(one["amount"] for one in sheet["candidates"])

    answer = client.post(
        f"/api/accounts/{world['checking']}/reconciliation",
        json={
            "statement_date": "2026-02-20",
            "statement_balance": total,
            "transaction_ids": [one["id"] for one in sheet["candidates"]],
        },
        headers=HEADERS,
    )
    assert answer.status_code == 201, answer.text
    assert answer.json()["statement_balance"] == total
    assert answer.json()["batch_id"], "a reconciliation must be undoable"

    register = client.get(
        f"/api/households/{world['house']}/transactions?account_id={world['checking']}",
        headers=HEADERS,
    ).json()
    assert all(row["cleared"] == "reconciled" for row in register["transactions"])

    # And there is nothing left to prove.
    again = client.get(
        f"/api/accounts/{world['checking']}/reconciliation", headers=HEADERS
    ).json()
    assert again["candidates"] == []
    assert again["locked_balance"] == total


def test_a_statement_that_does_not_balance_is_refused_and_changes_nothing(client):
    world = _reconcile_world(client)
    sheet = client.get(
        f"/api/accounts/{world['checking']}/reconciliation?until=2026-02-20", headers=HEADERS
    ).json()

    answer = client.post(
        f"/api/accounts/{world['checking']}/reconciliation",
        json={
            "statement_date": "2026-02-20",
            "statement_balance": 999_999,
            "transaction_ids": [one["id"] for one in sheet["candidates"]],
        },
        headers=HEADERS,
    )
    assert answer.status_code == 409
    assert "does not balance" in answer.json()["detail"]

    after = client.get(
        f"/api/accounts/{world['checking']}/reconciliation", headers=HEADERS
    ).json()
    assert after["locked_balance"] == 0, "a refused attempt locked something"
    assert len(after["candidates"]) == len(sheet["candidates"])


def test_undoing_a_reconciliation_puts_every_row_back(client):
    """The way out, through the same History screen as everything else."""
    world = _reconcile_world(client)
    sheet = client.get(
        f"/api/accounts/{world['checking']}/reconciliation?until=2026-02-20", headers=HEADERS
    ).json()
    total = sum(one["amount"] for one in sheet["candidates"])

    made = client.post(
        f"/api/accounts/{world['checking']}/reconciliation",
        json={
            "statement_date": "2026-02-20",
            "statement_balance": total,
            "transaction_ids": [one["id"] for one in sheet["candidates"]],
        },
        headers=HEADERS,
    ).json()

    undone = client.post(
        f"/api/households/{world['house']}/batches/{made['batch_id']}/undo", headers=HEADERS
    )
    assert undone.status_code == 200, undone.text

    after = client.get(
        f"/api/accounts/{world['checking']}/reconciliation", headers=HEADERS
    ).json()
    assert after["locked_balance"] == 0
    assert len(after["candidates"]) == len(sheet["candidates"])
    assert after["last_statement_date"] is None, "the record itself survived the undo"


def test_the_reconciliation_history_is_readable(client):
    world = _reconcile_world(client)
    sheet = client.get(
        f"/api/accounts/{world['checking']}/reconciliation?until=2026-01-15", headers=HEADERS
    ).json()
    total = sum(one["amount"] for one in sheet["candidates"])

    client.post(
        f"/api/accounts/{world['checking']}/reconciliation",
        json={
            "statement_date": "2026-01-15",
            "statement_balance": total,
            "transaction_ids": [one["id"] for one in sheet["candidates"]],
        },
        headers=HEADERS,
    )

    history = client.get(
        f"/api/accounts/{world['checking']}/reconciliations", headers=HEADERS
    ).json()
    assert len(history) == 1
    assert history[0]["statement_date"] == "2026-01-15"
    assert history[0]["statement_balance"] == total


def test_a_stranger_cannot_reconcile_your_account(client):
    world = _reconcile_world(client)
    client.cookies.clear()
    answer = client.get(f"/api/accounts/{world['checking']}/reconciliation", headers=HEADERS)
    assert answer.status_code == 401


# --------------------------------------------------------------------------- #
# Categories, over HTTP
# --------------------------------------------------------------------------- #


def _with_categories(client) -> dict:
    """A household and its categories -- which it now simply has.

    This used to POST `categories/defaults`. Creating a household seeds the
    default tree, so that route answers 409 ("this household already has
    categories") and is correct to: pressing "add the defaults" twice should
    not quietly produce a second Groceries. Reading them is what a caller with
    a real household does.
    """
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    tree = client.get(f"/api/households/{house}/categories", headers=HEADERS).json()
    by_name = {
        one["name"]: one for group in tree for one in group["categories"]
    }
    return {**world, "house": house, "tree": tree, "cat": by_name}


def test_a_household_can_start_with_a_common_set(client):
    world = _with_categories(client)
    names = set(world["cat"])
    assert {"Groceries", "Subscriptions", "Rent / Mortgage"} <= names
    # The standing decision, asserted at the boundary the user actually sees:
    # no inflow group, so nowhere for unassigned money to accumulate.
    assert not any(one.startswith("Inflow") for one in names)


def test_the_defaults_cannot_be_added_twice(client):
    world = _with_categories(client)
    again = client.post(
        f"/api/households/{world['house']}/categories/defaults", json={}, headers=HEADERS
    )
    assert again.status_code == 409
    assert "already has categories" in again.json()["detail"]


def test_a_transaction_can_be_categorised_and_reads_back_with_its_group(client):
    world = _with_categories(client)
    made = client.post(
        f"/api/households/{world['house']}/transactions",
        json={
            "account_id": world["checking"]["id"],
            "date": "2026-02-10",
            "amount": -4_520,
            "payee_name": "Mercadona",
            "category_id": world["cat"]["Groceries"]["id"],
        },
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    assert made.json()["category_name"] == "Everyday: Groceries"


def test_the_second_transaction_for_a_payee_categorises_itself(client):
    """The feature, at the HTTP boundary: you type a payee and it fills in."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]

    client.post(
        f"/api/households/{house}/transactions",
        json={
            "account_id": checking, "date": "2026-02-10", "amount": -4_520,
            "payee_name": "Mercadona", "category_id": world["cat"]["Groceries"]["id"],
        },
        headers=HEADERS,
    )
    second = client.post(
        f"/api/households/{house}/transactions",
        json={
            "account_id": checking, "date": "2026-02-17", "amount": -3_310,
            "payee_name": "Mercadona",
        },
        headers=HEADERS,
    ).json()

    assert second["category_id"] == world["cat"]["Groceries"]["id"]
    assert second["category_name"] == "Everyday: Groceries"


def test_asking_for_uncategorised_is_honoured(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={
            "account_id": checking, "date": "2026-02-10", "amount": -4_520,
            "payee_name": "Mercadona", "category_id": world["cat"]["Groceries"]["id"],
        },
        headers=HEADERS,
    )
    blank = client.post(
        f"/api/households/{house}/transactions",
        json={
            "account_id": checking, "date": "2026-02-17", "amount": -3_310,
            "payee_name": "Mercadona", "uncategorised": True,
        },
        headers=HEADERS,
    ).json()
    assert blank["category_id"] is None


def test_a_payee_can_be_pinned_to_one_category(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]

    made = client.post(
        f"/api/households/{house}/transactions",
        json={
            "account_id": checking, "date": "2026-02-10", "amount": -999,
            "payee_name": "Netflix", "category_id": world["cat"]["Eating Out"]["id"],
        },
        headers=HEADERS,
    ).json()

    rule = client.put(
        f"/api/payees/{made['payee_id']}/categorisation",
        json={"categorisation": "fixed", "category_id": world["cat"]["Subscriptions"]["id"]},
        headers=HEADERS,
    )
    assert rule.status_code == 200, rule.text
    assert rule.json()["current_default_name"] == "Quality of Life: Subscriptions"

    later = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-03-10", "amount": -999,
              "payee_name": "Netflix"},
        headers=HEADERS,
    ).json()
    assert later["category_name"] == "Quality of Life: Subscriptions", "the rule was ignored"


def test_a_payee_can_be_told_not_to_categorise(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-10", "amount": -999,
              "payee_name": "Cash", "category_id": world["cat"]["Household"]["id"]},
        headers=HEADERS,
    ).json()

    client.put(
        f"/api/payees/{made['payee_id']}/categorisation",
        json={"categorisation": "none"},
        headers=HEADERS,
    )
    later = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-03-10", "amount": -999, "payee_name": "Cash"},
        headers=HEADERS,
    ).json()
    assert later["category_id"] is None


def test_the_rule_reports_what_it_would_choose(client):
    """The "current default category" line from the design."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-10", "amount": -999,
              "payee_name": "Spotify", "category_id": world["cat"]["Subscriptions"]["id"]},
        headers=HEADERS,
    ).json()

    rule = client.get(f"/api/payees/{made['payee_id']}/categorisation", headers=HEADERS).json()
    assert rule["categorisation"] == "history"
    assert rule["current_default_name"] == "Quality of Life: Subscriptions"
    # The rule's window is fixed; what this payee has to go on is not. They are
    # separate numbers because the sentence describing the rule has to read the
    # same for a payee with nothing behind it.
    assert rule["history_window"] == 3
    assert rule["history_available"] == 1


def test_an_import_categorises_every_row_it_can(client):
    """Where it pays: a statement of many rows, categorised without typing."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]

    # Teach the payee once, by hand.
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-02", "amount": -4_520,
              "payee_name": "MERCADONA 1234", "category_id": world["cat"]["Groceries"]["id"]},
        headers=HEADERS,
    )

    upload = _upload(client, house, checking, SANTANDER_CSV)
    assert upload.status_code == 201, upload.text
    client.post(
        f"/api/households/{house}/imports/{upload.json()['batch_id']}/commit",
        json={}, headers=HEADERS,
    )

    register = client.get(
        f"/api/households/{house}/transactions?account_id={checking}&search=MERCADONA",
        headers=HEADERS,
    ).json()
    imported = [row for row in register["transactions"] if row["import_id"]]
    assert imported, "nothing imported"
    assert all(row["category_name"] == "Everyday: Groceries" for row in imported), [
        row["category_name"] for row in imported
    ]


def test_a_used_category_cannot_be_deleted_but_can_be_archived(client):
    world = _with_categories(client)
    groceries = world["cat"]["Groceries"]["id"]
    client.post(
        f"/api/households/{world['house']}/transactions",
        json={"account_id": world["checking"]["id"], "date": "2026-02-10",
              "amount": -999, "category_id": groceries},
        headers=HEADERS,
    )

    refused = client.delete(f"/api/categories/{groceries}", headers=HEADERS)
    assert refused.status_code == 409
    assert "Archive it instead" in refused.json()["detail"]

    archived = client.patch(
        f"/api/categories/{groceries}", json={"archived": True}, headers=HEADERS
    )
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    listed = client.get(f"/api/households/{world['house']}/categories", headers=HEADERS).json()
    names = {one["name"] for group in listed for one in group["categories"]}
    assert "Groceries" not in names


def test_the_usage_count_is_what_makes_the_two_different(client):
    world = _with_categories(client)
    listed = client.get(f"/api/households/{world['house']}/categories", headers=HEADERS).json()
    by_name = {one["name"]: one for group in listed for one in group["categories"]}
    assert by_name["Groceries"]["used_by"] == 0

    client.post(
        f"/api/households/{world['house']}/transactions",
        json={"account_id": world["checking"]["id"], "date": "2026-02-10",
              "amount": -999, "category_id": by_name["Groceries"]["id"]},
        headers=HEADERS,
    )
    again = client.get(f"/api/households/{world['house']}/categories", headers=HEADERS).json()
    counts = {one["name"]: one["used_by"] for group in again for one in group["categories"]}
    assert counts["Groceries"] == 1


def test_the_register_sorts_by_category(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    for name, amount in (("Subscriptions", -999), ("Groceries", -4_520), ("Travel", -20_000)):
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": "2026-02-10", "amount": amount,
                  "category_id": world["cat"][name]["id"]},
            headers=HEADERS,
        )
    # And one with nothing on it, which is the interesting case.
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-11", "amount": -100,
              "uncategorised": True},
        headers=HEADERS,
    )

    page = client.get(
        f"/api/households/{house}/transactions?sort=category&direction=asc", headers=HEADERS
    ).json()
    order = [row["category_name"] for row in page["transactions"]]
    named = [one for one in order if one]
    assert named == sorted(named), order
    # Nulls last either way: an uncategorised row is not "before Everyday".
    assert order[-1] is None, order

    down = client.get(
        f"/api/households/{house}/transactions?sort=category&direction=desc", headers=HEADERS
    ).json()
    assert [row["category_name"] for row in down["transactions"]][-1] is None


def test_a_stranger_cannot_read_your_categories(client):
    world = _with_categories(client)
    client.cookies.clear()
    answer = client.get(f"/api/households/{world['house']}/categories", headers=HEADERS)
    assert answer.status_code == 401


# --------------------------------------------------------------------------- #
# History reads as sentences
# --------------------------------------------------------------------------- #


def test_the_history_list_describes_what_happened(client):
    """Written after the list endpoint 500'd on a field name.

    `actor` collided with `Batch.actor`, the ORM relationship to the User, and a
    `from_attributes` model picked the object up and failed to coerce it. Every
    test at the time went through the *undo* endpoint and none through the list,
    so the screen was the first thing to find out.
    """
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-10", "amount": -7_978,
              "payee_name": "Repsol"},
        headers=HEADERS,
    )

    listed = client.get(
        f"/api/households/{house}/batches?include_single_edits=true", headers=HEADERS
    )
    assert listed.status_code == 200, listed.text

    entry = next(one for one in listed.json() if one["kind"] == "manual")
    assert entry["headline"] == "Edit"
    assert "79.78" in entry["detail"], entry["detail"]
    assert "Repsol" in entry["detail"]
    assert entry["actor_name"], "nobody is credited with the change"
    assert entry["change_count"] >= 1


def test_one_batch_can_be_read_row_by_row(client):
    """What the undo confirmation is built from."""
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    for amount in (-100, -200, -300):
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": "2026-02-10", "amount": amount},
            headers=HEADERS,
        )

    listed = client.get(
        f"/api/households/{house}/batches?include_single_edits=true", headers=HEADERS
    ).json()
    one = client.get(
        f"/api/households/{house}/batches/{listed[0]['id']}", headers=HEADERS
    )
    assert one.status_code == 200, one.text
    body = one.json()
    assert body["lines"], "no lines to confirm against"
    assert body["change_count"] == len(body["lines"])
    assert any("Added" in line for line in body["lines"])


def test_reading_another_households_batch_is_refused(client):
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    listed = client.get(
        f"/api/households/{house}/batches?include_single_edits=true", headers=HEADERS
    ).json()

    other = client.post("/api/households", json={"name": "Theirs"}, headers=HEADERS).json()
    answer = client.get(
        f"/api/households/{other['id']}/batches/{listed[0]['id']}", headers=HEADERS
    )
    assert answer.status_code == 404


# --------------------------------------------------------------------------- #
# How much of a list comes back
# --------------------------------------------------------------------------- #


def test_the_register_returns_everything_it_matched(client):
    """The bug this replaced: a default of 200 that nothing ever raised.

    The client never sent `limit`, so a household with more than two hundred
    rows saw two hundred of them, `total` said otherwise, and nothing on screen
    reconciled the two. Reading a fifth of a ledger and believing it is all of
    it is the worst kind of wrong this app can be.
    """
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]

    for index in range(260):
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": "2026-02-10", "amount": -(index + 1)},
            headers=HEADERS,
        )

    page = client.get(f"/api/households/{house}/transactions", headers=HEADERS).json()
    assert page["total"] == 260
    assert len(page["transactions"]) == 260, "the register truncated without being asked to"
    assert page["capped"] is False


def test_a_caller_can_still_ask_for_less(client):
    """`limit` survives for anything that wants a slice; nothing in the app does."""
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    for index in range(10):
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": "2026-02-10", "amount": -(index + 1)},
            headers=HEADERS,
        )

    page = client.get(f"/api/households/{house}/transactions?limit=4", headers=HEADERS).json()
    assert len(page["transactions"]) == 4
    assert page["total"] == 10
    assert page["capped"] is True, "holding some back has to be admitted"


def test_the_fast_read_returns_what_the_slow_one_did(client):
    """Reading columns instead of objects must not quietly change a field.

    Every column the register shows, on a row that exercises all of them: a
    payee, a category, a memo, a cleared state and an import id.
    """
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]

    made = client.post(
        f"/api/households/{house}/transactions",
        json={
            "account_id": checking, "date": "2026-02-10", "amount": -4_520,
            "payee_name": "Mercadona", "memo": "weekly",
            "category_id": world["cat"]["Groceries"]["id"], "cleared": "cleared",
        },
        headers=HEADERS,
    ).json()

    page = client.get(f"/api/households/{house}/transactions", headers=HEADERS).json()
    row = next(one for one in page["transactions"] if one["id"] == made["id"])

    for field in (
        "id", "account_id", "date", "amount", "payee_id", "payee_name",
        "category_id", "category_name", "memo", "cleared", "import_id",
        "transfer_account_id", "transfer_transaction_id",
    ):
        assert row[field] == made[field], f"{field}: {row[field]!r} != {made[field]!r}"


def test_the_running_balance_survived_the_rewrite(client):
    """It is computed from a different query now, so it gets checked again."""
    world = _household_with_accounts(client)
    house, checking = world["household"]["id"], world["checking"]["id"]
    for amount in (10_000, -2_500, -1_000):
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": "2026-02-10", "amount": amount},
            headers=HEADERS,
        )

    page = client.get(
        f"/api/households/{house}/transactions?account_id={checking}", headers=HEADERS
    ).json()
    assert page["has_running_balance"] is True

    # Newest first, so the top row carries the balance the account is at now
    # and reading downward walks back in time.
    balances = [row["running_balance"] for row in page["transactions"]]
    amounts = [row["amount"] for row in page["transactions"]]
    assert balances[0] == 6_500, balances
    assert balances[-1] == amounts[-1] == 10_000, "the oldest row's balance is its own amount"

    # The invariant the column exists for: each row's balance is the one below
    # it plus its own amount.
    for index in range(len(balances) - 1):
        assert balances[index] == balances[index + 1] + amounts[index], (balances, amounts)


def test_a_transfers_two_legs_still_point_at_each_other(client):
    """Both columns come through the column read; neither is derived."""
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    client.post(
        f"/api/households/{house}/transfers",
        json={
            "from_account_id": world["checking"]["id"],
            "to_account_id": world["card"]["id"],
            "date": "2026-02-10",
            "amount": 5_000,
        },
        headers=HEADERS,
    )

    page = client.get(f"/api/households/{house}/transactions", headers=HEADERS).json()
    legs = [row for row in page["transactions"] if row["transfer_account_id"]]
    assert len(legs) == 2
    assert {legs[0]["transfer_transaction_id"], legs[1]["transfer_transaction_id"]} == {
        legs[0]["id"], legs[1]["id"]
    }


# --------------------------------------------------------------------------- #
# Where an imported line will land
# --------------------------------------------------------------------------- #


def _stage(client, house: str, account: str, csv: str = SANTANDER_CSV):
    return _upload(client, house, account, csv).json()


def test_the_preview_shows_where_each_line_would_land(client):
    """The guess, before anything is written.

    A statement of three hundred rows is three hundred categories you did not
    type or three hundred you have to fix; either way the moment to find out is
    while nothing has happened yet.
    """
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]

    # Teach the payee once, by hand.
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-02", "amount": -4_520,
              "payee_name": "MERCADONA 1234", "category_id": world["cat"]["Groceries"]["id"]},
        headers=HEADERS,
    )

    preview = _stage(client, house, checking)
    mercadona = next(
        one for one in preview["lines"] if "MERCADONA" in (one["parsed"] or {}).get("payee", "")
    )
    assert mercadona["category_name"] == "Everyday: Groceries"
    assert mercadona["category_chosen"] is False, "the rule's guess is not somebody's decision"

    unknown = next(
        one for one in preview["lines"] if "NOMINA" in (one["parsed"] or {}).get("payee", "")
    )
    assert unknown["category_id"] is None, "a payee with no history has nothing to go on"


def test_a_category_chosen_on_the_preview_sticks_through_the_commit(client):
    """The whole point of being able to set one."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    preview = _stage(client, house, checking)

    line = next(
        one for one in preview["lines"] if "NOMINA" in (one["parsed"] or {}).get("payee", "")
    )
    assert line["category_id"] is None

    set_it = client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{line['id']}",
        json={"category_id": world["cat"]["Salary"]["id"]},
        headers=HEADERS,
    )
    assert set_it.status_code == 200, set_it.text
    assert set_it.json()["category_name"] == "Income: Salary"
    assert set_it.json()["category_chosen"] is True

    committed = client.post(
        f"/api/households/{house}/imports/{preview['batch_id']}/commit",
        json={}, headers=HEADERS,
    )
    assert committed.status_code == 200, committed.text

    register = client.get(
        f"/api/households/{house}/transactions?search=NOMINA", headers=HEADERS
    ).json()
    assert register["transactions"], "the line did not import"
    assert all(
        row["category_name"] == "Income: Salary" for row in register["transactions"]
    ), [row["category_name"] for row in register["transactions"]]


def test_a_chosen_category_survives_reloading_the_preview(client):
    """Held on the line, not in the browser, so coming back to it still shows."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    preview = _stage(client, house, checking)
    line = preview["lines"][0]

    client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{line['id']}",
        json={"category_id": world["cat"]["Travel"]["id"]},
        headers=HEADERS,
    )

    again = client.get(
        f"/api/households/{house}/imports/{preview['batch_id']}", headers=HEADERS
    ).json()
    same = next(one for one in again["lines"] if one["id"] == line["id"])
    assert same["category_name"] == "Quality of Life: Travel"
    assert same["category_chosen"] is True


def test_a_choice_can_be_handed_back_to_the_payees_rule(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-02", "amount": -4_520,
              "payee_name": "MERCADONA 1234", "category_id": world["cat"]["Groceries"]["id"]},
        headers=HEADERS,
    )
    preview = _stage(client, house, checking)
    line = next(
        one for one in preview["lines"] if "MERCADONA" in (one["parsed"] or {}).get("payee", "")
    )

    client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{line['id']}",
        json={"category_id": world["cat"]["Travel"]["id"]},
        headers=HEADERS,
    )
    back = client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{line['id']}",
        json={"clear_category": True},
        headers=HEADERS,
    ).json()

    assert back["category_chosen"] is False
    assert back["category_name"] == "Everyday: Groceries", "the rule did not take it back"


def test_the_guess_follows_the_payee_between_staging_and_committing(client):
    """Derived on read, not frozen at staging.

    You look at the preview, see the wrong guess, go and fix the three
    transactions that taught it, and come back. The preview has to be showing
    what the commit will actually do, not what it would have done an hour ago.
    """
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    preview = _stage(client, house, checking)

    line = next(
        one for one in preview["lines"] if "MERCADONA" in (one["parsed"] or {}).get("payee", "")
    )
    assert line["category_id"] is None, "nothing taught it yet"

    # Now teach it, after the file was staged.
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-02", "amount": -9_999,
              "payee_name": "MERCADONA 1234", "category_id": world["cat"]["Groceries"]["id"]},
        headers=HEADERS,
    )

    again = client.get(
        f"/api/households/{house}/imports/{preview['batch_id']}", headers=HEADERS
    ).json()
    same = next(one for one in again["lines"] if one["id"] == line["id"])
    assert same["category_name"] == "Everyday: Groceries", "the preview froze its guess"


def test_a_line_from_another_import_is_refused(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    first = _stage(client, house, checking)
    second = _stage(client, house, world["card"]["id"], SANTANDER_CSV)

    answer = client.patch(
        f"/api/households/{house}/imports/{second['batch_id']}/lines/{first['lines'][0]['id']}",
        json={"category_id": world["cat"]["Travel"]["id"]},
        headers=HEADERS,
    )
    assert answer.status_code == 404


def test_one_transactions_own_history_reads_as_sentences(client):
    """`Audit Log Decision.md` sold this as the headline: *why is this EUR 45
    here?* The endpoint and the index existed; nothing put it on a screen and
    nothing turned the stored images into words."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]

    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-10", "amount": -7_978,
              "payee_name": "Repsol"},
        headers=HEADERS,
    ).json()
    client.patch(
        f"/api/transactions/{made['id']}",
        json={"category_id": world["cat"]["Transport"]["id"]},
        headers=HEADERS,
    )
    client.patch(f"/api/transactions/{made['id']}", json={"memo": "diesel"}, headers=HEADERS)

    history = client.get(
        f"/api/households/{house}/changes?table=transactions&row_id={made['id']}",
        headers=HEADERS,
    )
    assert history.status_code == 200, history.text
    entries = history.json()
    assert len(entries) == 3, [one["summary"] for one in entries]

    # Newest first, so the last thing done is the first thing read.
    assert "memo" in entries[0]["summary"], entries[0]["summary"]
    assert "diesel" in entries[0]["summary"]
    assert "Everyday: Transport" in entries[1]["summary"], entries[1]["summary"]
    assert "Added transaction" in entries[2]["summary"], entries[2]["summary"]

    for one in entries:
        assert one["actor_name"], "nobody is credited with the change"
        assert one["at"], "no timestamp"


def test_a_rows_history_is_scoped_to_its_household(client):
    """Otherwise a member of one household could walk another's audit log."""
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": world["checking"]["id"], "date": "2026-02-10", "amount": -100},
        headers=HEADERS,
    ).json()

    other = client.post("/api/households", json={"name": "Theirs"}, headers=HEADERS).json()
    answer = client.get(
        f"/api/households/{other['id']}/changes?table=transactions&row_id={made['id']}",
        headers=HEADERS,
    )
    assert answer.status_code == 200
    assert answer.json() == [], "another household's history leaked"


# --------------------------------------------------------------------------- #
# Bulk category, and spreading one across an import
# --------------------------------------------------------------------------- #


def test_many_rows_can_be_categorised_in_one_act(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    ids = [
        client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": checking, "date": "2026-02-10", "amount": -(i + 1) * 100},
            headers=HEADERS,
        ).json()["id"]
        for i in range(3)
    ]

    answer = client.post(
        f"/api/households/{house}/transactions/bulk",
        json={"transaction_ids": ids, "category_id": world["cat"]["Groceries"]["id"]},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    assert all(
        one["category_name"] == "Everyday: Groceries" for one in answer.json()["transactions"]
    )

    # One act, so undo reverses the whole selection rather than three of them.
    batches = client.get(
        f"/api/households/{house}/batches?include_single_edits=true", headers=HEADERS
    ).json()
    bulk = next(one for one in batches if one["kind"] == "bulk_update")
    assert bulk["change_count"] == 3, bulk


def test_a_bulk_category_can_be_emptied(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-10", "amount": -100,
              "category_id": world["cat"]["Groceries"]["id"]},
        headers=HEADERS,
    ).json()

    answer = client.post(
        f"/api/households/{house}/transactions/bulk",
        json={"transaction_ids": [made["id"]], "clear_category": True},
        headers=HEADERS,
    ).json()
    assert answer["transactions"][0]["category_id"] is None


def test_correcting_one_line_offers_the_rest_of_its_payee(client):
    """The offer, and the number behind it.

    A statement has the same shop eleven times. Correcting one and being asked
    about the other ten is the difference between the feature being worth using
    and being eleven pieces of typing.
    """
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]

    csv = (
        "Fecha;Concepto;Importe;Saldo\r\n"
        "05/01/2026;MERCADONA 1234;-45,20;8.810,07\r\n"
        "07/01/2026;Mercadona 1234;-12,00;8.798,07\r\n"
        "09/01/2026;MERCADONA  1234;-31,50;8.766,57\r\n"
        "11/01/2026;REPSOL;-60,00;8.706,57\r\n"
    )
    preview = _upload(client, house, checking, csv).json()
    first = next(
        one for one in preview["lines"]
        if (one["parsed"] or {}).get("payee", "").upper().startswith("MERCADONA")
    )

    set_it = client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{first['id']}",
        json={"category_id": world["cat"]["Groceries"]["id"]},
        headers=HEADERS,
    ).json()
    # Two more Mercadona lines, spelled three different ways; Repsol is not one.
    assert set_it["similar_lines"] == 2, set_it

    applied = client.post(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{first['id']}/apply-to-payee",
        headers=HEADERS,
    )
    assert applied.status_code == 200, applied.text

    by_payee = {}
    for one in applied.json()["lines"]:
        by_payee.setdefault((one["parsed"] or {}).get("payee", ""), one["category_name"])
    assert all(
        name == "Everyday: Groceries"
        for payee, name in by_payee.items()
        if payee.upper().startswith("MERCADONA")
    ), by_payee
    assert by_payee.get("REPSOL") != "Everyday: Groceries", "it spread to another payee"


def test_the_offer_leaves_lines_somebody_already_decided(client):
    """An offer that overwrites a decision is not an offer."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    csv = (
        "Fecha;Concepto;Importe;Saldo\r\n"
        "05/01/2026;MERCADONA 1234;-45,20;8.810,07\r\n"
        "07/01/2026;MERCADONA 1234;-12,00;8.798,07\r\n"
    )
    preview = _upload(client, house, checking, csv).json()
    one, two = preview["lines"][0], preview["lines"][1]

    client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{two['id']}",
        json={"category_id": world["cat"]["Travel"]["id"]},
        headers=HEADERS,
    )
    offered = client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{one['id']}",
        json={"category_id": world["cat"]["Groceries"]["id"]},
        headers=HEADERS,
    ).json()
    assert offered["similar_lines"] == 0, "it offered to overwrite a decision"

    client.post(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{one['id']}/apply-to-payee",
        headers=HEADERS,
    )
    after = client.get(
        f"/api/households/{house}/imports/{preview['batch_id']}", headers=HEADERS
    ).json()
    kept = next(x for x in after["lines"] if x["id"] == two["id"])
    assert kept["category_name"] == "Quality of Life: Travel", "a decision was overwritten"


def test_splitting_over_http_replaces_the_row_and_keeps_the_balance(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-10", "amount": -10_000,
              "payee_name": "Carrefour"},
        headers=HEADERS,
    ).json()
    before = client.get(f"/api/accounts/{checking}", headers=HEADERS).json()["balance"]

    answer = client.post(
        f"/api/transactions/{made['id']}/split",
        json={"parts": [
            {"amount": -6_000, "category_id": world["cat"]["Groceries"]["id"]},
            {"amount": -4_000, "category_id": world["cat"]["Household"]["id"]},
        ]},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    parts = answer.json()
    assert len(parts) == 2
    assert len({one["split_id"] for one in parts}) == 1
    assert all(one["split_id"] for one in parts)

    after = client.get(f"/api/accounts/{checking}", headers=HEADERS).json()["balance"]
    assert after == before, "the split moved the balance"

    register = client.get(f"/api/households/{house}/transactions", headers=HEADERS).json()
    assert made["id"] not in {one["id"] for one in register["transactions"]}


def test_a_split_that_does_not_add_up_is_refused_over_http(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-10", "amount": -10_000},
        headers=HEADERS,
    ).json()

    answer = client.post(
        f"/api/transactions/{made['id']}/split",
        json={"parts": [{"amount": -6_000}, {"amount": -3_000}]},
        headers=HEADERS,
    )
    assert answer.status_code == 409
    assert "add up" in answer.json()["detail"]
    assert client.get(
        f"/api/households/{house}/transactions", headers=HEADERS
    ).json()["total"] >= 1, "a refused split removed the row"


@pytest.mark.parametrize(
    ("schema", "model"),
    [
        ("BatchOut", "Batch"),
        ("BatchDetail", "Batch"),
        ("ChangeOut", "Change"),
        ("TransactionOut", "Transaction"),
        ("AccountOut", "Account"),
        ("CategoryOut", "Category"),
        ("HouseholdOut", "Household"),
        ("ImportLineOut", "ImportLine"),
    ],
)
def test_no_wire_field_shadows_an_orm_relationship(schema: str, model: str):
    """A `from_attributes` field named after a relationship eats the objects.

    Twice now. `BatchOut.actor` picked up the `User` and 500'd the History list;
    `BatchDetail.changes` picked up the `Change` rows and 500'd the panel. Both
    typecheck, both pass every unit test, and both fail the moment real data
    goes through -- because the field is silently fed an ORM object where it
    wanted a string.

    A field *may* legitimately be the relationship, if its type says so. This
    only fails the ones typed as something else.
    """
    from sqlalchemy import inspect as sa_inspect

    from app import models, schemas

    wire = getattr(schemas, schema)
    orm = getattr(models, model)
    relationships = {rel.key for rel in sa_inspect(orm).relationships}

    for name, field in wire.model_fields.items():
        if name not in relationships:
            continue
        # Declared as the relationship on purpose is fine; anything else is the
        # trap, because pydantic will hand it the ORM object.
        annotation = str(field.annotation)
        assert "Out" in annotation or "Detail" in annotation, (
            f"{schema}.{name} shadows {model}.{name}, a relationship. "
            f"It is typed {annotation}, so pydantic will hand it the ORM object "
            f"and fail. Rename the field."
        )


def test_a_batch_spells_out_every_column_that_moved(client):
    """The panel answers "what exactly", which is most of why anybody opens an
    audit log. Both sides of every field that changed, in words."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    made = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-02-10", "amount": -7_978,
              "payee_name": "Repsol", "category_id": world["cat"]["Transport"]["id"]},
        headers=HEADERS,
    ).json()
    client.patch(
        f"/api/transactions/{made['id']}",
        json={"category_id": world["cat"]["Groceries"]["id"], "memo": "diesel"},
        headers=HEADERS,
    )

    listed = client.get(
        f"/api/households/{house}/batches?include_single_edits=true", headers=HEADERS
    ).json()
    edit = client.get(
        f"/api/households/{house}/batches/{listed[0]['id']}", headers=HEADERS
    ).json()

    change = edit["changed_rows"][0]
    assert change["op"] == "update"
    assert change["table"] == "transaction"
    assert change["row_id"] == made["id"]

    moved = {one["field"]: (one["was"], one["now"]) for one in change["fields"]}
    assert moved["category"] == ("Everyday: Transport", "Everyday: Groceries"), moved
    assert moved["memo"] == ("no memo", "diesel"), moved
    # Only what moved: the amount and date were not touched and must not appear.
    assert "amount" not in moved and "date" not in moved, moved


def test_an_insert_shows_the_whole_row_rather_than_a_diff_against_nothing(client):
    """Every column differs from nothing on an insert, so a from/to table would
    read "household id: nothing → 4a05..." fifteen times. True, and noise."""
    world = _household_with_accounts(client)
    house = world["household"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": world["checking"]["id"], "date": "2026-02-10",
              "amount": -100, "payee_name": "Repsol"},
        headers=HEADERS,
    )

    listed = client.get(
        f"/api/households/{house}/batches?include_single_edits=true", headers=HEADERS
    ).json()
    made = client.get(
        f"/api/households/{house}/batches/{listed[0]['id']}", headers=HEADERS
    ).json()

    # Naming a new payee creates the payee in the same batch, so there are two
    # inserts here and the transaction is the one under test.
    row = next(
        one for one in made["changed_rows"]
        if one["op"] == "insert" and one["table"] == "transaction"
    )
    assert row["fields"] == [], "an insert produced a diff against nothing"
    assert row["snapshot"], "an insert recorded no row"
    shown = {one["field"] for one in row["snapshot"]}
    assert {"amount", "date"} <= shown, shown


def test_a_redacted_column_is_named_rather_than_starred(client):
    """They are absent, not masked -- an undo writes back what is stored, and
    "***" would become somebody's password hash."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()

    listed = client.get(
        f"/api/households/{house['id']}/batches?include_single_edits=true", headers=HEADERS
    ).json()
    for entry in listed:
        detail = client.get(
            f"/api/households/{house['id']}/batches/{entry['id']}", headers=HEADERS
        ).json()
        for row in detail["changed_rows"]:
            for field in row["fields"] + row["snapshot"]:
                assert "***" not in field["now"], "a redacted value was masked rather than omitted"
                assert field["field"] not in ("password_hash", "totp_secret"), field


def test_a_line_can_teach_its_payee_for_good(client):
    """The second half of "stop asking me": this file, and every one after it."""
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    csv = (
        "Fecha;Concepto;Importe;Saldo\r\n"
        "05/01/2026;BRAND NEW CAFE;-4,50;100,00\r\n"
    )
    preview = _upload(client, house, checking, csv).json()
    line = preview["lines"][0]

    client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{line['id']}",
        json={"category_id": world["cat"]["Eating Out"]["id"]},
        headers=HEADERS,
    )
    done = client.post(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{line['id']}/payee-rule",
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["payee_name"] == "BRAND NEW CAFE"
    assert body["category_name"] == "Everyday: Eating Out"
    assert body["payee_created"] is True, "the statement brought a payee that did not exist"

    rule = client.get(f"/api/payees/{body['payee_id']}/categorisation", headers=HEADERS).json()
    assert rule["categorisation"] == "fixed"
    assert rule["current_default_name"] == "Everyday: Eating Out"

    # And it takes effect on a transaction that has nothing to do with the file.
    later = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-06-01", "amount": -900,
              "payee_name": "BRAND NEW CAFE"},
        headers=HEADERS,
    ).json()
    assert later["category_name"] == "Everyday: Eating Out", "the rule did not stick"


def test_teaching_a_payee_that_already_exists_does_not_claim_to_have_made_it(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": checking, "date": "2026-01-01", "amount": -100,
              "payee_name": "OLD FRIEND"},
        headers=HEADERS,
    )

    preview = _upload(
        client, house, checking,
        "Fecha;Concepto;Importe;Saldo\r\n05/01/2026;OLD FRIEND;-4,50;100,00\r\n",
    ).json()
    line = preview["lines"][0]
    client.patch(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{line['id']}",
        json={"category_id": world["cat"]["Gifts"]["id"]},
        headers=HEADERS,
    )
    body = client.post(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/{line['id']}/payee-rule",
        headers=HEADERS,
    ).json()
    assert body["payee_created"] is False


def test_a_line_with_no_category_cannot_teach_anything(client):
    world = _with_categories(client)
    house, checking = world["house"], world["checking"]["id"]
    preview = _upload(
        client, house, checking,
        "Fecha;Concepto;Importe;Saldo\r\n05/01/2026;SOMEWHERE;-4,50;100,00\r\n",
    ).json()

    answer = client.post(
        f"/api/households/{house}/imports/{preview['batch_id']}/lines/"
        f"{preview['lines'][0]['id']}/payee-rule",
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert "category first" in answer.json()["detail"]


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def _house_with_two_currencies(client) -> dict:
    """A household holding one EUR account and one GBP account, with spend in both."""
    house = client.post("/api/households", json={"name": "Doe-Smith"}, headers=HEADERS).json()
    euros = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR", "country": "ES"},
        headers=HEADERS,
    ).json()
    pounds = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "UK Savings", "type": "savings", "currency": "GBP", "country": "GB"},
        headers=HEADERS,
    ).json()
    for account, amount in ((euros, -3_000), (pounds, -9_900)):
        client.post(
            f"/api/households/{house['id']}/transactions",
            json={
                "account_id": account["id"],
                "date": "2026-02-10",
                "amount": amount,
                "payee_name": "Shop",
            },
            headers=HEADERS,
        )
    return {"house": house, "euros": euros, "pounds": pounds}


def test_a_report_answers_in_one_currency_and_says_which(client):
    """The currency is required, and the answer names it.

    An answer that did not say which currency it was about would be the same
    defect this whole feature is arranged around, wearing a hat.
    """
    _setup_owner(client)
    made = _house_with_two_currencies(client)
    house = made["house"]

    response = client.get(
        f"/api/households/{house['id']}/reports/income-expense"
        "?currency=EUR&since=2026-01-01&until=2026-03-31"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["currency"] == "EUR"
    assert body["expense"]["total_minor"] == -3_000
    assert body["expense"]["total"] == "-30.00"
    assert body["months"] == ["2026-01", "2026-02", "2026-03"]
    # The GBP figure, and the sum of the two, appear nowhere.
    assert "-129.00" not in response.text
    assert -9_900 not in body["expense"]["by_month"].values()


def test_a_report_without_a_currency_is_refused(client):
    """Not defaulted, because there is no honest default across two."""
    _setup_owner(client)
    house = _house_with_two_currencies(client)["house"]

    response = client.get(
        f"/api/households/{house['id']}/reports/income-expense"
        "?since=2026-01-01&until=2026-03-31"
    )
    assert response.status_code == 422


def test_the_currency_list_is_what_the_household_actually_holds(client):
    _setup_owner(client)
    house = _house_with_two_currencies(client)["house"]

    body = client.get(f"/api/households/{house['id']}/reports/currencies").json()
    assert sorted(body["currencies"]) == ["EUR", "GBP"]


def test_a_non_member_cannot_read_a_report(client):
    """404 and not 403, like every other household-scoped route."""
    _setup_owner(client)
    stranger = "0" * 32

    assert (
        client.get(
            f"/api/households/{stranger}/reports/income-expense"
            "?currency=EUR&since=2026-01-01&until=2026-03-31"
        ).status_code
        == 404
    )
    assert client.get(f"/api/households/{stranger}/reports/currencies").status_code == 404


def test_the_account_filter_repeats_and_narrows(client):
    """Several `account_id` values, which is how the grouped picker sends them."""
    _setup_owner(client)
    made = _house_with_two_currencies(client)
    house, euros = made["house"], made["euros"]

    second = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Visa", "type": "credit_card", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    client.post(
        f"/api/households/{house['id']}/transactions",
        json={
            "account_id": second["id"],
            "date": "2026-02-11",
            "amount": -1_500,
            "payee_name": "Shop",
        },
        headers=HEADERS,
    )

    both = client.get(
        f"/api/households/{house['id']}/reports/income-expense"
        "?currency=EUR&since=2026-01-01&until=2026-03-31"
    ).json()
    one = client.get(
        f"/api/households/{house['id']}/reports/income-expense"
        f"?currency=EUR&since=2026-01-01&until=2026-03-31&account_id={euros['id']}"
    ).json()

    assert both["expense"]["total_minor"] == -4_500
    assert one["expense"]["total_minor"] == -3_000


def test_a_report_writes_nothing(client):
    """It is a question, not an act: no batch, so nothing in History."""
    _setup_owner(client)
    house = _house_with_two_currencies(client)["house"]

    before = client.get(f"/api/households/{house['id']}/batches").json()
    client.get(
        f"/api/households/{house['id']}/reports/income-expense"
        "?currency=EUR&since=2026-01-01&until=2026-03-31"
    )
    after = client.get(f"/api/households/{house['id']}/batches").json()

    assert len(after) == len(before)


def test_all_dates_sends_no_ends_and_gets_real_ones_back(client):
    """The "All dates" preset sends neither end; the answer names both.

    The columns of this report are calendar months, so an open end has no last
    column — the server has to settle on a date, and the screen prints the one
    it settled on rather than leaving somebody to guess what they are looking
    at.
    """
    _setup_owner(client)
    house = _house_with_two_currencies(client)["house"]

    body = client.get(
        f"/api/households/{house['id']}/reports/income-expense?currency=EUR"
    ).json()

    assert body["since"] == "2026-02-10"
    assert body["until"] == "2026-02-10"
    assert body["months"] == ["2026-02"]


def test_a_figure_and_the_rows_behind_it_agree_over_http(client):
    """End to end, because the two are computed by different calls.

    Nothing else checks that the endpoint pair stays in step: the report could
    change its predicate and the drill-through keep the old one, and every
    number would still look plausible on its own.
    """
    _setup_owner(client)
    made = _house_with_two_currencies(client)
    house = made["house"]

    report = client.get(
        f"/api/households/{house['id']}/reports/income-expense"
        "?currency=EUR&since=2026-01-01&until=2026-03-31"
    ).json()
    cell = report["expense"]["by_month"]["2026-02"]

    behind = client.get(
        f"/api/households/{house['id']}/reports/income-expense/behind"
        "?currency=EUR&since=2026-01-01&until=2026-03-31&period=2026-02&direction=out"
    ).json()

    assert cell == -3_000
    assert behind["total_minor"] == cell
    assert behind["count"] == 1
    assert behind["entries"][0]["account_name"] == "Santander"
    assert behind["entries"][0]["payee_name"] == "Shop"
    assert behind["entries"][0]["amount"] == "-30.00"


def test_a_drill_through_is_scoped_like_everything_else(client):
    """404 for a household you are not in, same as the report it came from."""
    _setup_owner(client)
    stranger = "0" * 32

    assert (
        client.get(
            f"/api/households/{stranger}/reports/income-expense/behind?currency=EUR"
        ).status_code
        == 404
    )


def test_a_drill_through_refuses_a_period_that_is_not_one(client):
    """`period` is a month, and a pattern says so rather than a silent no-match."""
    _setup_owner(client)
    house = _house_with_two_currencies(client)["house"]

    assert (
        client.get(
            f"/api/households/{house['id']}/reports/income-expense/behind"
            "?currency=EUR&period=last-tuesday"
        ).status_code
        == 422
    )
