"""A staged line can be marked uncategorised on purpose. Issue #9.

The preview's category cell had two answers: a category, or an empty box --
and the empty box meant "hand it back to the rule". So a line the payee's
usual category or the bank's wording (interest, fees) had categorised could
not be made to land uncategorised: emptying the cell brought the guess back,
and the only way out was to commit and fix every row in the register.

Every test here asserts the ``category_id`` the committed row actually carries,
because "the PATCH answered 200" is exactly the kind of assertion that let the
rule win unnoticed.
"""

from __future__ import annotations

from tests.conftest import HEADERS, _setup_owner

#: ``(fitid, day, amount, name)``. Synthetic payees and figures throughout.
#: CORNER SHOP has a usual category by the time these are staged; MARKET HALL
#: never has; the interest line carries the bank's `category_hint`.
EUR_ROWS = [
    ("UC0001", 2, "-12.00", "CORNER SHOP"),
    ("UC0002", 3, "-8.00", "CORNER SHOP"),
    ("UC0003", 4, "0.40", "Interest earned - Rainy Day"),
    ("UC0004", 5, "-20.00", "MARKET HALL"),
]
GBP_ROWS = [
    ("UG0001", 6, "-5.00", "CORNER SHOP"),
    ("UG0002", 7, "0.30", "Interest earned - Rainy Day"),
    ("UG0003", 8, "-7.00", "MARKET HALL"),
]


def _ofx(rows, currency: str) -> str:
    body = "".join(
        "<STMTTRN>"
        f"<TRNTYPE>{'CREDIT' if not amount.startswith('-') else 'DEBIT'}</TRNTYPE>"
        f"<DTPOSTED>202510{day:02d}000000</DTPOSTED>"
        f"<TRNAMT>{amount}</TRNAMT><FITID>{fitid}</FITID><NAME>{name}</NAME>"
        "</STMTTRN>"
        for fitid, day, amount, name in rows
    )
    return (
        '<?xml version="1.0" standalone="no"?>'
        '<?OFX OFXHEADER="200" VERSION="202" SECURITY="NONE"?>'
        "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>"
        f"<CURDEF>{currency}</CURDEF>"
        "<BANKACCTFROM><ACCTID>ACCT|55667</ACCTID></BANKACCTFROM>"
        f"<BANKTRANLIST>{body}</BANKTRANLIST>"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    )


def _world(client) -> dict:
    """Two households, two accounts in two currencies, two payees -- one with
    a usual category and one without -- and the bank's interest wording.

    The second household is there so a line can be shown not to be reachable
    from it; the second currency so nothing here is true of one account only.
    """
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    elsewhere = client.post(
        "/api/households", json={"name": "The Other One"}, headers=HEADERS
    ).json()
    checking = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Current", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    sterling = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Sterling", "type": "checking", "currency": "GBP"},
        headers=HEADERS,
    ).json()
    tree = client.get(f"/api/households/{house['id']}/categories", headers=HEADERS).json()
    cats = {one["name"]: one for group in tree for one in group["categories"]}

    # Teach CORNER SHOP its usual category: one row by hand, well away from
    # the statements' dates and figures so it is never mistaken for a twin.
    taught = client.post(
        f"/api/households/{house['id']}/transactions",
        json={
            "account_id": checking["id"],
            "date": "2025-06-01",
            "amount": -9_999,
            "payee_name": "CORNER SHOP",
            "category_id": cats["Groceries"]["id"],
        },
        headers=HEADERS,
    )
    assert taught.status_code == 201, taught.text
    return {
        "house": house["id"],
        "elsewhere": elsewhere["id"],
        "checking": checking["id"],
        "sterling": sterling["id"],
        "cat": cats,
    }


def _stage(client, world, account: str, rows, currency: str) -> dict:
    answer = client.post(
        f"/api/households/{world['house']}/imports",
        data={"account_id": account},
        files={"file": ("statement.ofx", _ofx(rows, currency).encode(), "application/x-ofx")},
        headers=HEADERS,
    )
    assert answer.status_code == 201, answer.text
    return answer.json()


def _line(preview: dict, amount_minor: int) -> dict:
    return next(one for one in preview["lines"] if (one["parsed"] or {})["amount"] == amount_minor)


def _set(client, world, preview: dict, line: dict, *, house=None, **body):
    return client.patch(
        f"/api/households/{house or world['house']}/imports/{preview['batch_id']}"
        f"/lines/{line['id']}",
        json=body,
        headers=HEADERS,
    )


def _reopen(client, world, preview: dict) -> dict:
    answer = client.get(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}", headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def _commit(client, world, preview: dict) -> None:
    answer = client.post(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text


def _landed(client, world, account: str) -> dict[int, str | None]:
    """Amount in minor units -> the category id the committed row carries."""
    answer = client.get(
        f"/api/households/{world['house']}/transactions?account_id={account}", headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    return {row["amount"]: row["category_id"] for row in answer.json()["transactions"]}


def _category_names(client, world) -> set[str]:
    tree = client.get(f"/api/households/{world['house']}/categories", headers=HEADERS).json()
    return {one["name"] for group in tree for one in group["categories"]}


# --------------------------------------------------------------------------- #
# The commit
# --------------------------------------------------------------------------- #


def test_a_line_marked_uncategorised_lands_with_no_category_whatever_the_rule_says(client):
    world = _world(client)
    groceries = world["cat"]["Groceries"]["id"]
    preview = _stage(client, world, world["checking"], EUR_ROWS, "EUR")

    # What the preview would have done before anybody said anything: the rule
    # for one, the bank's wording for the other.
    shop = _line(preview, -1_200)
    interest = _line(preview, 40)
    assert shop["category_id"] == groceries
    assert interest["category_name"] == "Income: Interest income"

    for line in (shop, interest):
        marked = _set(client, world, preview, line, uncategorised=True)
        assert marked.status_code == 200, marked.text
        body = marked.json()
        assert body["category_uncategorised"] is True
        assert body["category_chosen"] is True, "it is somebody's decision, not a gap"
        assert body["category_id"] is None and body["category_name"] is None

    _commit(client, world, preview)
    landed = _landed(client, world, world["checking"])

    assert landed[-1_200] is None, "the payee's rule won over a deliberate choice"
    assert landed[40] is None, "the bank's wording won over a deliberate choice"
    # The lines nobody touched still get what they always did.
    assert landed[-800] == groceries, "the other line of the same payee lost its rule"
    assert landed[-2_000] is None, "a payee with no rule has nothing to go on"
    # And no category was made for a hint that was never used.
    assert "Interest income" not in _category_names(client, world)


def test_the_second_account_in_another_currency_keeps_its_rule_and_its_hint(client):
    """The override is per line: nothing about it leaks into the next import."""
    world = _world(client)
    groceries = world["cat"]["Groceries"]["id"]

    euros = _stage(client, world, world["checking"], EUR_ROWS, "EUR")
    _set(client, world, euros, _line(euros, -1_200), uncategorised=True)
    _set(client, world, euros, _line(euros, 40), uncategorised=True)
    _commit(client, world, euros)

    pounds = _stage(client, world, world["sterling"], GBP_ROWS, "GBP")
    _commit(client, world, pounds)
    landed = _landed(client, world, world["sterling"])

    assert landed[-500] == groceries
    assert landed[-700] is None
    assert landed[30] is not None, "the hint stopped working after the override was used"
    assert "Interest income" in _category_names(client, world)


def test_choosing_a_category_after_uncategorised_wins(client):
    world = _world(client)
    travel = world["cat"]["Travel"]["id"]
    preview = _stage(client, world, world["sterling"], GBP_ROWS, "GBP")
    shop = _line(preview, -500)

    _set(client, world, preview, shop, uncategorised=True)
    chosen = _set(client, world, preview, shop, category_id=travel).json()

    assert chosen["category_id"] == travel
    assert chosen["category_uncategorised"] is False
    assert "category_uncategorised" not in chosen["parsed"], "the override outlived the choice"

    _commit(client, world, preview)
    assert _landed(client, world, world["sterling"])[-500] == travel


def test_handing_it_back_to_the_rule_drops_the_override(client):
    world = _world(client)
    groceries = world["cat"]["Groceries"]["id"]
    preview = _stage(client, world, world["checking"], EUR_ROWS, "EUR")
    shop = _line(preview, -1_200)

    _set(client, world, preview, shop, uncategorised=True)
    back = _set(client, world, preview, shop, clear_category=True).json()

    assert back["category_uncategorised"] is False
    assert back["category_chosen"] is False
    assert back["category_id"] == groceries, "the rule did not take it back"

    _commit(client, world, preview)
    assert _landed(client, world, world["checking"])[-1_200] == groceries


def test_uncategorised_after_a_chosen_category_clears_the_category(client):
    world = _world(client)
    preview = _stage(client, world, world["checking"], EUR_ROWS, "EUR")
    hall = _line(preview, -2_000)

    _set(client, world, preview, hall, category_id=world["cat"]["Travel"]["id"])
    marked = _set(client, world, preview, hall, uncategorised=True).json()
    assert marked["category_id"] is None

    _commit(client, world, preview)
    assert _landed(client, world, world["checking"])[-2_000] is None


# --------------------------------------------------------------------------- #
# Kept on the line
# --------------------------------------------------------------------------- #


def test_the_override_survives_reopening_the_preview(client):
    world = _world(client)
    preview = _stage(client, world, world["checking"], EUR_ROWS, "EUR")
    shop = _line(preview, -1_200)

    _set(client, world, preview, shop, uncategorised=True)

    reopened = _line(_reopen(client, world, preview), -1_200)
    assert reopened["category_uncategorised"] is True
    assert reopened["category_chosen"] is True
    assert reopened["category_id"] is None
    assert reopened["parsed"]["category_uncategorised"] is True
    # The line beside it is still the rule's guess.
    neighbour = _line(_reopen(client, world, preview), -800)
    assert neighbour["category_uncategorised"] is False
    assert neighbour["category_id"] == world["cat"]["Groceries"]["id"]


def test_a_contradictory_request_is_refused_and_changes_nothing(client):
    world = _world(client)
    preview = _stage(client, world, world["checking"], EUR_ROWS, "EUR")
    shop = _line(preview, -1_200)

    both = _set(
        client, world, preview, shop,
        uncategorised=True, category_id=world["cat"]["Travel"]["id"],
    )
    assert both.status_code == 422, both.text
    assert "without category_id" in both.text

    also = _set(client, world, preview, shop, uncategorised=True, clear_category=True)
    assert also.status_code == 422, also.text

    still = _line(_reopen(client, world, preview), -1_200)
    assert still["category_uncategorised"] is False
    assert still["category_id"] == world["cat"]["Groceries"]["id"]


def test_another_household_cannot_mark_this_households_line(client):
    world = _world(client)
    preview = _stage(client, world, world["checking"], EUR_ROWS, "EUR")
    shop = _line(preview, -1_200)

    answer = _set(client, world, preview, shop, house=world["elsewhere"], uncategorised=True)
    assert answer.status_code == 404, answer.text
    assert _line(_reopen(client, world, preview), -1_200)["category_uncategorised"] is False


# --------------------------------------------------------------------------- #
# The rest of its payee
# --------------------------------------------------------------------------- #


def test_uncategorised_spreads_to_the_rest_of_its_payee_in_this_file(client):
    world = _world(client)
    rows = [
        ("US0001", 2, "-12.00", "CORNER SHOP"),
        ("US0002", 3, "-8.00", "Corner Shop"),
        ("US0003", 4, "-3.00", "CORNER  SHOP"),
        ("US0004", 5, "-20.00", "MARKET HALL"),
    ]
    preview = _stage(client, world, world["checking"], rows, "EUR")
    first = _line(preview, -1_200)

    marked = _set(client, world, preview, first, uncategorised=True).json()
    assert marked["similar_lines"] == 2, marked

    applied = client.post(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}"
        f"/lines/{first['id']}/apply-to-payee",
        headers=HEADERS,
    )
    assert applied.status_code == 200, applied.text
    after = applied.json()
    assert _line(after, -800)["category_uncategorised"] is True
    assert _line(after, -300)["category_uncategorised"] is True
    assert _line(after, -2_000)["category_uncategorised"] is False, "it spread to another payee"

    _commit(client, world, preview)
    landed = _landed(client, world, world["checking"])
    assert landed[-1_200] is None and landed[-800] is None and landed[-300] is None


def test_neither_kind_of_decision_is_overwritten_by_the_other(client):
    """An offer that overwrites a decision is not an offer -- in either direction."""
    world = _world(client)
    travel = world["cat"]["Travel"]["id"]
    rows = [
        ("UD0001", 2, "-12.00", "CORNER SHOP"),
        ("UD0002", 3, "-8.00", "CORNER SHOP"),
        ("UD0003", 4, "-3.00", "CORNER SHOP"),
    ]
    preview = _stage(client, world, world["checking"], rows, "EUR")
    one, two, three = _line(preview, -1_200), _line(preview, -800), _line(preview, -300)

    _set(client, world, preview, two, uncategorised=True)
    offered = _set(client, world, preview, one, category_id=travel).json()
    assert offered["similar_lines"] == 1, "it offered to overwrite an uncategorised decision"

    client.post(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}"
        f"/lines/{one['id']}/apply-to-payee",
        headers=HEADERS,
    )
    _set(client, world, preview, three, category_id=world["cat"]["Groceries"]["id"])
    spread = _set(client, world, preview, two, uncategorised=True).json()
    assert spread["similar_lines"] == 0, "it offered to overwrite a chosen category"

    _commit(client, world, preview)
    landed = _landed(client, world, world["checking"])
    assert landed[-1_200] == travel
    assert landed[-800] is None
    assert landed[-300] == world["cat"]["Groceries"]["id"]
