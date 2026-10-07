"""A key can read back what it stored: a receipt's note, and its file (#44).

An agent asked to summarise receipts filed the day before had nothing to read:
the listing left out the note sent at upload, and no agent route returned the
bytes. It only worked because the originals happened to be on the same
computer. Two households, each with its own key and its own receipt, so every
"404 for another household" is tested against a receipt that really exists.
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import AgentRequest, AgentScope, BatchKind, Household, User
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner
from tests.receipt_fixtures import as_bytes, receipt_image

V1 = "/api/agent/v1"


def _house(client, name: str, currency: str) -> dict:
    house = client.post("/api/households", json={"name": name}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": f"{name} current", "type": "checking", "currency": currency},
        headers=HEADERS,
    ).json()
    txn = client.post(
        f"/api/households/{house['id']}/transactions",
        json={"account_id": account["id"], "date": "2026-07-05", "amount": -4_312},
        headers=HEADERS,
    ).json()
    return {"house": house, "account": account, "txn": txn}


@pytest.fixture()
def world(client):
    """Two households of one owner, a read key and a write key on each."""
    made = _setup_owner(client)
    homes = [_house(client, "Home", "EUR"), _house(client, "Cottage", "GBP")]
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, made["user"]["id"])
        for home in homes:
            row = own.get(Household, home["house"]["id"])
            with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=row.id):
                _k, home["write"] = key_service.issue(
                    own, user=user, household=row, label="filer", scope=AgentScope.write
                )
                _k, home["read"] = key_service.issue(
                    own, user=user, household=row, label="reader", scope=AgentScope.read
                )
        own.commit()
    # The owner's browser session, kept aside so a test can act as the person.
    client.owner_cookies = dict(client.cookies)
    client.cookies.clear()
    for n, home in enumerate(homes):
        # Big enough that the 320 px thumbnail is really smaller than the file.
        home["raw"] = as_bytes(receipt_image((520 + n, 700 + n)))
        stored = client.post(
            f"{V1}/households/{home['house']['id']}/receipts",
            json={
                "filename": f"till-{n}.jpg",
                "content_base64": base64.b64encode(home["raw"]).decode(),
                "transaction_id": home["txn"]["id"],
                "note": f"Lunch with the team, receipt {n}",
                "extracted": {"merchant": "Example Café", "total_minor": 4_312},
            },
            headers={"authorization": f"Bearer {home['write']}"},
        )
        assert stored.status_code == 201, stored.text
        home["receipt"] = stored.json()["receipt"]
    return client, homes


def _as(home, key: str = "read") -> dict:
    return {"authorization": f"Bearer {home[key]}"}


def test_the_note_written_at_upload_is_read_back(world):
    client, (home, cottage) = world
    listed = client.get(f"{V1}/households/{home['house']['id']}/receipts", headers=_as(home))
    assert listed.status_code == 200, listed.text
    assert [one["note"] for one in listed.json()] == ["Lunch with the team, receipt 0"]

    one = client.get(f"{V1}/receipts/{home['receipt']['id']}", headers=_as(home))
    assert one.status_code == 200, one.text
    assert one.json()["note"] == "Lunch with the team, receipt 0"
    assert one.json()["extracted"]["merchant"] == "Example Café"

    theirs = client.get(f"{V1}/receipts/{cottage['receipt']['id']}", headers=_as(cottage))
    assert theirs.json()["note"] == "Lunch with the team, receipt 1"


def test_a_note_changed_in_the_panel_is_what_the_key_reads(world):
    client, (home, _cottage) = world
    before = client.get(f"{V1}/receipts/{home['receipt']['id']}", headers=_as(home)).json()
    # A person edits the note in the panel, through the browser's own route.
    client.cookies.update(client.owner_cookies)
    edited = client.patch(
        f"/api/receipts/{home['receipt']['id']}",
        json={"note": "Team lunch, two guests"},
        headers=HEADERS,
    )
    assert edited.status_code == 200, edited.text
    client.cookies.clear()

    after = client.get(f"{V1}/receipts/{home['receipt']['id']}", headers=_as(home)).json()
    assert before["note"] == "Lunch with the team, receipt 0"
    assert after["note"] == "Team lunch, two guests"


def test_the_listing_goes_from_a_row_to_its_receipts(world):
    client, (home, _cottage) = world
    house = home["house"]["id"]
    on_row = client.get(
        f"{V1}/households/{house}/receipts?transaction_id={home['txn']['id']}", headers=_as(home)
    )
    assert [one["id"] for one in on_row.json()] == [home["receipt"]["id"]]
    elsewhere = client.get(
        f"{V1}/households/{house}/receipts?transaction_id=no-such-row", headers=_as(home)
    )
    assert elsewhere.json() == []


def test_the_file_is_the_stored_copy(world):
    client, (home, cottage) = world
    answer = client.get(f"{V1}/receipts/{home['receipt']['id']}/file", headers=_as(home))
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] in {"image/avif", "image/jpeg"}
    assert answer.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in answer.headers["content-security-policy"]
    assert len(answer.content) > 100

    theirs = client.get(f"{V1}/receipts/{cottage['receipt']['id']}/file", headers=_as(cottage))
    assert theirs.status_code == 200
    assert theirs.content != answer.content, "each key reads its own household's file"


def test_the_thumbnail_is_smaller_than_the_file(world):
    client, (home, _cottage) = world
    receipt = client.get(f"{V1}/receipts/{home['receipt']['id']}", headers=_as(home)).json()
    file = client.get(f"{V1}/receipts/{home['receipt']['id']}/file", headers=_as(home))
    thumb = client.get(f"{V1}/receipts/{home['receipt']['id']}/thumbnail", headers=_as(home))
    assert receipt["has_thumbnail"] is True, "every picture gets one"
    assert thumb.status_code == 200, thumb.text
    assert thumb.headers["content-type"] == "image/avif"
    assert len(thumb.content) < len(file.content)


@pytest.mark.parametrize("tail", ["", "/file", "/thumbnail"])
def test_another_households_receipt_is_not_there(world, tail):
    """404, not 403: a key must not learn that an id in another household is real."""
    client, (home, cottage) = world
    answer = client.get(f"{V1}/receipts/{cottage['receipt']['id']}{tail}", headers=_as(home))
    assert answer.status_code == 404
    missing = client.get(f"{V1}/receipts/no-such-receipt{tail}", headers=_as(home))
    assert missing.status_code == 404
    assert answer.json() == missing.json(), "the two answers must not differ"


@pytest.mark.parametrize("tail", ["", "/file", "/thumbnail"])
def test_no_key_no_receipt(world, tail):
    client, (home, _cottage) = world
    answer = client.get(f"{V1}/receipts/{home['receipt']['id']}{tail}")
    assert answer.status_code == 401


def test_reading_the_file_is_in_the_request_log(world):
    client, (home, _cottage) = world
    with Session(client.app_module.db_engine) as own:
        before = len(own.execute(select(AgentRequest)).scalars().all())
    client.get(f"{V1}/receipts/{home['receipt']['id']}/file", headers=_as(home))
    with Session(client.app_module.db_engine) as own:
        rows = own.execute(select(AgentRequest).order_by(AgentRequest.at)).scalars().all()
    assert len(rows) == before + 1
    assert rows[-1].method == "GET"
    assert rows[-1].route.endswith("/receipts/{receipt_id}/file")
    assert rows[-1].status == 200
