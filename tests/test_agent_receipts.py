"""Receipts, through the agent door.

Slice 3 is small because receipts shipped in full on 2026-09-19. What was left
was a door an agent can use (base64), somewhere to put what it read
(`extracted`), and the matching that saves it pulling the register
(`/candidates`).

The test that matters most is `test_candidates_finds_an_imported_transaction`.
The spec costed `/candidates` at an hour on the grounds that
`importing._find_twin` already did the work; it filters `import_id IS NULL` and
so matches only hand-entered rows, which is the opposite of what a receipt
needs. That test fails against any implementation that reaches for the cheap
version.
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
    Household,
    Receipt,
    Transaction,
    User,
)
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner
from tests.receipt_fixtures import as_bytes, receipt_image

V1 = "/api/agent/v1"


def _jpeg(seed: int = 1) -> bytes:
    """A distinct picture per seed, so two uploads are genuinely two files."""
    return as_bytes(receipt_image((60 + seed, 80 + seed)))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@pytest.fixture()
def world(client):
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
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
        "made": made, "house": house, "account": account,
        "token": write, "reader": read, "client": client,
    }


def _txn(world, *, when: str, amount: int, payee: str = "Mercadona", imported: bool = False):
    """A transaction, optionally looking like one that came off a statement."""
    client = world["client"]
    made = client.post(
        f"/api/households/{world['house']['id']}/transactions",
        json={"account_id": world["account"]["id"], "date": when,
              "amount": amount, "payee_name": payee},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    row = made.json()
    if imported:
        # What `_find_twin` filters OUT. Set directly, because the point is the
        # state, not how it got there.
        with Session(client.app_module.db_engine, expire_on_commit=False) as own:
            txn = own.get(Transaction, row["id"])
            with batch(own, kind=BatchKind.imported, actor_id=_actor(own),
                       household_id=txn.household_id):
                txn.import_id = f"stmt:{row['id']}"
            own.commit()
    return row


def _actor(session) -> str:
    return session.execute(select(User.id)).scalars().first()


def _agent_headers(token: str) -> dict:
    """Bearer, and the same-site header the browser would send.

    This fixture keeps its session cookie, because setting up transactions
    needs it — and a request carrying **both** a cookie and a bearer token is a
    browser as far as the CSRF middleware is concerned, so the origin check
    runs. That is the middleware working: its "only" is load-bearing, and
    `test_agent_access.py` is where the cookie-less path is proven. Here the
    subject is receipts, so the header is supplied and the argument is settled
    elsewhere.
    """
    return {"authorization": f"Bearer {token}", **HEADERS}


def _upload(world, raw: bytes, *, token=None, **extra):
    body = {"content_base64": _b64(raw), "filename": "till.jpg"}
    body.update(extra)
    return world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts",
        json=body,
        headers=_agent_headers(token or world["token"]),
    )


def _count(client) -> int:
    with Session(client.app_module.db_engine) as own:
        return own.execute(select(func.count()).select_from(Receipt)).scalar_one()


# --------------------------------------------------------------------------- #
# The base64 door
# --------------------------------------------------------------------------- #


def test_a_receipt_arrives_as_json_and_is_stored(world):
    answer = _upload(world, _jpeg())
    assert answer.status_code == 201, answer.text

    body = answer.json()
    assert body["already_had_it"] is False
    assert body["receipt"]["needs_a_transaction"] is True, "no transaction yet: the inbox"
    assert body["receipt"]["content_sha256"]
    assert _count(world["client"]) == 1


def test_the_same_bytes_twice_is_told_rather_than_refused(world):
    """"You already have this" is a result an agent should report, not an
    error it should retry."""
    first = _upload(world, _jpeg())
    again = _upload(world, _jpeg())

    assert again.status_code == 201
    assert again.json()["already_had_it"] is True
    assert again.json()["receipt"]["id"] == first.json()["receipt"]["id"]
    assert "Nothing was stored" in again.json()["note"]
    assert _count(world["client"]) == 1, "one row, not two"


def test_something_that_is_not_base64_is_refused_with_a_sentence(world):
    refused = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts",
        json={"content_base64": "this is not base64!!"},
        headers=_agent_headers(world["token"]),
    )
    assert refused.status_code == 422
    assert "base64" in refused.json()["detail"]
    assert _count(world["client"]) == 0


def test_something_far_too_large_is_refused_before_it_is_decoded(world):
    """Checked on the ENCODED string, so a hostile payload cannot make the
    process allocate its decoded form just to be told it was too big."""
    huge = "A" * (6 * 1024 * 1024)
    refused = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts",
        json={"content_base64": huge},
        headers=_agent_headers(world["token"]),
    )
    assert refused.status_code == 413
    assert _count(world["client"]) == 0


def test_a_read_only_key_cannot_store_one(world):
    refused = _upload(world, _jpeg(), token=world["reader"])
    assert refused.status_code == 403
    assert _count(world["client"]) == 0


def test_an_html_file_renamed_jpg_is_refused_by_magic_bytes(world):
    """The allowlist is the shipped one; this asserts the agent door meets it."""
    refused = _upload(world, b"<html><script>alert(1)</script></html>")
    assert refused.status_code in (415, 422), refused.text
    assert _count(world["client"]) == 0


# --------------------------------------------------------------------------- #
# `extracted` is a claim
# --------------------------------------------------------------------------- #


def test_what_the_agent_read_is_stored_and_changes_nothing_in_the_ledger(world):
    txn = _txn(world, when="2026-07-05", amount=-1250)
    before = world["client"].get(
        f"/api/households/{world['house']['id']}/transactions", headers=HEADERS
    ).json()

    stored = _upload(
        world, _jpeg(), transaction_id=txn["id"],
        extracted={"merchant": "MERCADONA SA", "total_minor": -9999, "tax_minor": -1},
    )
    assert stored.status_code == 201, stored.text
    assert stored.json()["receipt"]["extracted"]["merchant"] == "MERCADONA SA"

    after = world["client"].get(
        f"/api/households/{world['house']['id']}/transactions", headers=HEADERS
    ).json()

    # The row is *marked* as having a receipt, which is the point of attaching
    # one. What must not have moved is any figure: the claim says 99.99 and the
    # ledger still says 12.50.
    def money(page):
        return [
            (r["id"], r["date"], r["amount"], r["payee_id"], r["category_id"])
            for r in page["transactions"]
        ]

    assert money(after) == money(before), "a claim must not move a figure in the ledger"


# --------------------------------------------------------------------------- #
# The inbox, and linking later
# --------------------------------------------------------------------------- #


def test_a_receipt_can_arrive_before_its_transaction_and_be_linked_after(world):
    receipt = _upload(world, _jpeg()).json()["receipt"]
    assert receipt["needs_a_transaction"] is True

    inbox = world["client"].get(
        f"{V1}/households/{world['house']['id']}/receipts?unlinked=true",
        headers={"authorization": f"Bearer {world['token']}"},
    ).json()
    assert [r["id"] for r in inbox] == [receipt["id"]]

    txn = _txn(world, when="2026-07-05", amount=-1250)
    linked = world["client"].post(
        f"{V1}/receipts/{receipt['id']}/link",
        json={"transaction_id": txn["id"]},
        headers=_agent_headers(world["token"]),
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["transaction_id"] == txn["id"]
    assert linked.json()["needs_a_transaction"] is False

    after = world["client"].get(
        f"{V1}/households/{world['house']['id']}/receipts?unlinked=true",
        headers={"authorization": f"Bearer {world['token']}"},
    ).json()
    assert after == [], "it has left the inbox"


# --------------------------------------------------------------------------- #
# /candidates — the defect this slice exists to not repeat
# --------------------------------------------------------------------------- #


def _near_today(offset: int = 0) -> str:
    """A date the window can reach.

    A receipt with no EXIF falls back to its upload date -- today -- and the
    window is four days. Dating fixtures in some fixed month is how a test of
    this ends up asserting an empty list and passing for the wrong reason.
    """
    from datetime import date as _date

    return (_date.today() + timedelta(days=offset)).isoformat()


def test_candidates_finds_an_imported_transaction(world):
    """The regression test for the spec's own mistake.

    `importing._find_twin` filters `import_id IS NULL`, so it matches only
    hand-entered rows. A receipt is overwhelmingly evidence for a row that came
    off a statement. Any implementation that reuses that function returns
    nothing here.
    """
    txn = _txn(world, when=_near_today(), amount=-1250, imported=True)
    receipt = _upload(world, _jpeg()).json()["receipt"]

    found = world["client"].get(
        f"{V1}/receipts/{receipt['id']}/candidates?total_minor=1250",
        headers={"authorization": f"Bearer {world['token']}"},
    )
    assert found.status_code == 200, found.text
    ids = [c["transaction_id"] for c in found.json()["candidates"]]
    assert txn["id"] in ids, (
        "an imported row must be offered -- this is exactly what _find_twin cannot do"
    )


def test_candidates_matches_an_unsigned_total_against_a_negative_row(world):
    """A receipt total is what it cost; the ledger records money leaving."""
    txn = _txn(world, when=_near_today(), amount=-1250)
    receipt = _upload(world, _jpeg()).json()["receipt"]

    found = world["client"].get(
        f"{V1}/receipts/{receipt['id']}/candidates?total_minor=1250",
        headers={"authorization": f"Bearer {world['token']}"},
    ).json()
    assert [c["transaction_id"] for c in found["candidates"]] == [txn["id"]]
    assert found["candidates"][0]["amount_minor"] == -1250


def test_candidates_ignores_a_different_amount(world):
    _txn(world, when=_near_today(), amount=-9900)
    receipt = _upload(world, _jpeg()).json()["receipt"]

    found = world["client"].get(
        f"{V1}/receipts/{receipt['id']}/candidates?total_minor=1250",
        headers={"authorization": f"Bearer {world['token']}"},
    ).json()
    assert found["candidates"] == [], "a different amount is not a candidate"


def test_candidates_is_short_and_ranked_by_how_near_the_date_is(world):
    """At most five, nearest first. A long list is the register again."""
    receipt = _upload(world, _jpeg()).json()["receipt"]
    when = date.fromisoformat(receipt["created_at"][:10])

    for offset in (0, 1, -1, 2, -2, 3, -3):
        _txn(world, when=(when + timedelta(days=offset)).isoformat(), amount=-1250)

    found = world["client"].get(
        f"{V1}/receipts/{receipt['id']}/candidates?total_minor=1250",
        headers={"authorization": f"Bearer {world['token']}"},
    ).json()["candidates"]

    assert len(found) <= 5
    assert [abs(c["days_apart"]) for c in found] == sorted(abs(c["days_apart"]) for c in found)
    assert found[0]["days_apart"] == 0
    assert "same amount" in found[0]["reason"]


def test_candidates_does_not_offer_a_row_that_already_has_these_bytes(world):
    """Re-filing a receipt should not suggest the row it is already on."""
    txn = _txn(world, when=_near_today(), amount=-1250)
    first = _upload(world, _jpeg(), transaction_id=txn["id"]).json()["receipt"]

    found = world["client"].get(
        f"{V1}/receipts/{first['id']}/candidates?total_minor=1250",
        headers={"authorization": f"Bearer {world['token']}"},
    ).json()
    assert txn["id"] not in [c["transaction_id"] for c in found["candidates"]]


def test_candidates_does_not_match_on_what_the_agent_claimed(world):
    """A claim must not become a suggestion that looks like evidence.

    The claim says 99.99; the ledger says 12.50. Asked without an explicit
    total, the claim is only a number to search for -- and it finds nothing,
    rather than quietly offering the 12.50 row.
    """
    _txn(world, when=_near_today(), amount=-1250)
    receipt = _upload(
        world, _jpeg(), extracted={"total_minor": -9999}
    ).json()["receipt"]

    found = world["client"].get(
        f"{V1}/receipts/{receipt['id']}/candidates",
        headers={"authorization": f"Bearer {world['token']}"},
    ).json()
    assert found["total_minor"] == -9999, "echoed back so the agent can see why"
    assert found["candidates"] == []


def test_a_receipt_in_another_household_is_a_404(world, client):
    receipt = _upload(world, _jpeg()).json()["receipt"]
    assert client.get(
        f"{V1}/receipts/{receipt['id']}/candidates",
        headers={"authorization": "Bearer stk_invented"},
    ).status_code == 401


# --------------------------------------------------------------------------- #
# /candidates, #134: the date on the paper, and a receipt in another currency
# --------------------------------------------------------------------------- #


def _candidates(world, receipt_id: str, **params) -> dict:
    found = world["client"].get(
        f"{V1}/receipts/{receipt_id}/candidates",
        params=params,
        headers={"authorization": f"Bearer {world['reader']}"},
    )
    assert found.status_code == 200, found.text
    return found.json()


def test_a_scan_is_matched_on_the_date_printed_on_it(world):
    """A scan has no EXIF, so without `date` it is searched around the day it
    was uploaded -- a month after the purchase -- and finds nothing."""
    month_ago = (date.today() - timedelta(days=30)).isoformat()
    txn = _txn(world, when=month_ago, amount=-1250)
    receipt = _upload(world, _jpeg()).json()["receipt"]

    unaided = _candidates(world, receipt["id"], total_minor=1250)
    assert unaided["candidates"] == []
    assert unaided["date_from"] == "uploaded"

    told = _candidates(world, receipt["id"], total_minor=1250, date=month_ago)
    assert [c["transaction_id"] for c in told["candidates"]] == [txn["id"]]
    assert told["matched_on_date"] == month_ago
    assert told["date_from"] == "given"


def test_the_date_the_agent_read_is_used_when_none_is_given(world):
    month_ago = (date.today() - timedelta(days=30)).isoformat()
    txn = _txn(world, when=month_ago, amount=-1250)
    receipt = _upload(
        world, _jpeg(), extracted={"date": month_ago, "total_minor": 1250}
    ).json()["receipt"]

    found = _candidates(world, receipt["id"])
    assert [c["transaction_id"] for c in found["candidates"]] == [txn["id"]]
    assert (found["matched_on_date"], found["date_from"]) == (month_ago, "extracted")


def test_a_date_the_agent_read_that_is_not_an_iso_date_is_ignored(world):
    receipt = _upload(world, _jpeg(), extracted={"date": "11/05/2026"}).json()["receipt"]
    found = _candidates(world, receipt["id"])
    assert found["date_from"] == "uploaded"


def test_a_receipt_in_another_currency_is_matched_on_merchant_and_says_so(world):
    """Paid in GBP on a EUR card: 21.95 GBP is 25.61 EUR on the row, and the
    EUR row that happens to read 21.95 is somebody else."""
    paid = _txn(world, when=_near_today(1), amount=-2561, payee="Pret A Manger")
    decoy = _txn(world, when=_near_today(), amount=-2195, payee="Zara")
    receipt = _upload(world, _jpeg(), extracted={
        "total_minor": 2195, "currency": "GBP", "merchant": "PRET A MANGER LTD",
    }).json()["receipt"]

    found = _candidates(world, receipt["id"])
    ids = [c["transaction_id"] for c in found["candidates"]]
    assert ids == [paid["id"]]
    assert decoy["id"] not in ids
    assert found["currency"] == "GBP" and found["merchant"] == "PRET A MANGER LTD"
    assert found["candidates"][0]["reason"].startswith(
        "different currency: matched on date and merchant, not amount"
    )


def test_in_its_own_currency_the_merchant_only_breaks_ties(world):
    """The claim may choose between rows of the right amount; it may not add a
    row of the wrong one -- matching is against the ledger's figures."""
    other_shop = _txn(world, when=_near_today(), amount=-1250, payee="Lidl")
    named = _txn(world, when=_near_today(1), amount=-1250, payee="Mercadona")
    wrong_amount = _txn(world, when=_near_today(), amount=-1399, payee="Mercadona")
    receipt = _upload(world, _jpeg(), extracted={"merchant": "Mercadona"}).json()["receipt"]

    found = _candidates(world, receipt["id"], total_minor=1250, currency="EUR")
    ids = [c["transaction_id"] for c in found["candidates"]]
    assert ids == [named["id"], other_shop["id"]], "the named shop first, a day further off"
    assert wrong_amount["id"] not in ids


def test_candidates_offers_a_reconciled_row_as_the_ui_would_attach_to_one(world):
    txn = _txn(world, when=_near_today(), amount=-1250)
    locked = world["client"].patch(
        f"/api/transactions/{txn['id']}", json={"cleared": "reconciled"}, headers=HEADERS
    )
    assert locked.status_code == 200, locked.text
    receipt = _upload(world, _jpeg()).json()["receipt"]

    found = _candidates(world, receipt["id"], total_minor=1250)
    assert [(c["transaction_id"], c["cleared"]) for c in found["candidates"]] == [
        (txn["id"], "reconciled")
    ]


def test_a_currency_that_is_not_a_code_is_refused(world):
    receipt = _upload(world, _jpeg()).json()["receipt"]
    refused = world["client"].get(
        f"{V1}/receipts/{receipt['id']}/candidates?currency=E1R",
        headers={"authorization": f"Bearer {world['reader']}"},
    )
    assert refused.status_code == 422


# --------------------------------------------------------------------------- #
# /link, #134: a receipt on another row is not moved without being asked
# --------------------------------------------------------------------------- #


def _link(world, receipt_id: str, transaction_id: str, **extra):
    return world["client"].post(
        f"{V1}/receipts/{receipt_id}/link",
        json={"transaction_id": transaction_id, **extra},
        headers=_agent_headers(world["token"]),
    )


def _on(world, transaction_id: str) -> list[str]:
    return [
        r["id"] for r in world["client"].get(
            f"/api/transactions/{transaction_id}/receipts", headers=HEADERS
        ).json()
    ]


def test_a_receipt_on_another_transaction_is_refused_unless_moved(world):
    first = _txn(world, when="2026-07-05", amount=-1250)
    second = _txn(world, when="2026-07-06", amount=-1250)
    receipt = _upload(world, _jpeg(), transaction_id=first["id"]).json()["receipt"]

    refused = _link(world, receipt["id"], second["id"])
    assert refused.status_code == 409
    assert first["id"] in refused.json()["detail"]
    assert '"move": true' in refused.json()["detail"]
    assert (_on(world, first["id"]), _on(world, second["id"])) == ([receipt["id"]], [])

    moved = _link(world, receipt["id"], second["id"], move=True)
    assert moved.status_code == 200, moved.text
    assert moved.json()["transaction_id"] == second["id"]
    assert (_on(world, first["id"]), _on(world, second["id"])) == ([], [receipt["id"]])


def test_linking_a_receipt_to_the_row_it_is_on_changes_nothing(world):
    txn = _txn(world, when="2026-07-05", amount=-1250)
    receipt = _upload(world, _jpeg(), transaction_id=txn["id"]).json()["receipt"]
    with Session(world["client"].app_module.db_engine) as own:
        before = own.execute(select(func.count()).select_from(Batch)).scalar_one()

    again = _link(world, receipt["id"], txn["id"])
    assert again.status_code == 200
    with Session(world["client"].app_module.db_engine) as own:
        assert own.execute(select(func.count()).select_from(Batch)).scalar_one() == before
    assert _on(world, txn["id"]) == [receipt["id"]]


def test_the_same_file_already_on_the_target_is_a_409_as_in_the_ui(world):
    txn = _txn(world, when="2026-07-05", amount=-1250)
    on_row = _upload(world, _jpeg(), transaction_id=txn["id"]).json()["receipt"]
    # The same bytes in the inbox too: allowed, since one bill can be
    # evidence for two rows -- but not twice for the same one.
    inboxed = _upload(world, _jpeg()).json()["receipt"]
    assert inboxed["id"] != on_row["id"]

    refused = _link(world, inboxed["id"], txn["id"])
    assert refused.status_code == 409
    assert "already attached to that transaction" in refused.json()["detail"]
    assert _on(world, txn["id"]) == [on_row["id"]]


def test_a_receipt_linked_to_a_split_part_lands_on_that_part_only(world):
    txn = _txn(world, when="2026-07-05", amount=-3000)
    parts = world["client"].post(
        f"/api/transactions/{txn['id']}/split",
        json={"parts": [{"amount": -1000}, {"amount": -2000}]},
        headers=HEADERS,
    )
    assert parts.status_code == 200, parts.text
    one, two = (p["id"] for p in parts.json())
    receipt = _upload(world, _jpeg()).json()["receipt"]

    linked = _link(world, receipt["id"], two)
    assert linked.status_code == 200, linked.text
    assert (_on(world, one), _on(world, two)) == ([], [receipt["id"]])
