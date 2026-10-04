"""Rows an agent posts, through the staging a CSV already goes through.

The point of every test here is that an agent's rows are **not a second write
path**. They meet the same dedupe, the same twin matching, the same payee
rules, and above all the same *one batch, one undo* — which is the difference
between 300 rows being one row in History and being three hundred.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import (
    AgentScope,
    Batch,
    BatchKind,
    BatchStatus,
    Household,
    ImportLine,
    Transaction,
    User,
)
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner

V1 = "/api/agent/v1"


def _key(client, world, household_id, *, scope=AgentScope.write, may_commit=False):
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, world["user"]["id"])
        house = own.get(Household, household_id)
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house.id):
            _row, token = key_service.issue(
                own, user=user, household=house, label="the importer",
                scope=scope, may_commit=may_commit,
            )
        own.commit()
    return token


@pytest.fixture()
def world(client):
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    yen = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Tokyo", "type": "cash", "currency": "JPY"},
        headers=HEADERS,
    ).json()
    token = _key(client, made, house["id"])
    committer = _key(client, made, house["id"], may_commit=True)
    reader = _key(client, made, house["id"], scope=AgentScope.read)
    client.cookies.clear()
    return {
        "house": house, "account": account, "yen": yen,
        "token": token, "committer": committer, "reader": reader,
    }


def _rows(n: int = 3, *, start: int = 1):
    """`n` distinct rows. Dates walk forward properly rather than by day
    number, because the first version of this happily asked for 32 July."""
    from datetime import date, timedelta

    first = date(2026, 7, 1)
    return [
        {
            "date": (first + timedelta(days=i)).isoformat(),
            "amount_minor": -100 * (i + 1),
            "payee": f"Shop {i + 1}",
            "external_id": f"src:{i + 1}",
        }
        for i in range(start - 1, start - 1 + n)
    ]


def _post(client, world, rows, *, token=None, source="amex-api", key=None, account=None):
    headers = {"authorization": f"Bearer {token or world['token']}"}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"{V1}/households/{world['house']['id']}/imports",
        json={"account_id": account or world["account"]["id"], "source": source, "rows": rows},
        headers=headers,
    )


def _count(client, model) -> int:
    with Session(client.app_module.db_engine) as own:
        return own.execute(select(func.count()).select_from(model)).scalar_one()


# --------------------------------------------------------------------------- #
# One batch, one undo
# --------------------------------------------------------------------------- #


def test_fifty_rows_are_one_batch_and_one_undo(client, world):
    """Spec test 11, and the whole reason this goes through the import path.

    The naive design is 50 `POST /transactions`: fifty batches, fifty History
    rows hidden by default, and an undo that has to be done fifty times in
    reverse because only the most recent batch touching a row may come back.
    """
    before = _count(client, Batch)
    staged = _post(client, world, _rows(50))
    assert staged.status_code == 201, staged.text

    assert _count(client, Batch) == before + 1, "fifty rows, one batch"
    assert _count(client, Transaction) == 0, "nothing reaches the register until committed"

    applied = client.post(
        f"{V1}/imports/{staged.json()['batch_id']}/commit",
        headers={"authorization": f"Bearer {world['committer']}"},
    )
    assert applied.status_code == 200, applied.text
    assert _count(client, Transaction) == 50

    # And ONE undo takes all fifty back. Through the audit service rather than
    # the HTTP route, because undo is deliberately a person's button and this
    # client is holding a key -- what is being asserted is that fifty rows
    # arrived as one undoable act, not who is allowed to press it.
    from app.audit.guard import AuditedSession
    from app.audit.undo import undo_batch

    with AuditedSession(bind=client.app_module.db_engine, expire_on_commit=False) as own:
        actor = own.execute(select(User.id)).scalars().first()
        undo_batch(own, staged.json()["batch_id"], actor_id=actor)
        own.commit()

    assert _count(client, Transaction) == 0, "one undo, fifty rows gone"


def test_staging_and_applying_stay_one_batch(client, world):
    """`resume` re-opens the batch rather than starting another."""
    before = _count(client, Batch)
    staged = _post(client, world, _rows(3))
    client.post(
        f"{V1}/imports/{staged.json()['batch_id']}/commit",
        headers={"authorization": f"Bearer {world['committer']}"},
    )
    assert _count(client, Batch) == before + 1, "two visits to one operation"

    with Session(client.app_module.db_engine) as own:
        row = own.get(Batch, staged.json()["batch_id"])
        assert row.status is BatchStatus.applied
        assert row.kind is BatchKind.imported, "not a new BatchKind -- who did it is another axis"


def test_the_batch_names_the_human_and_the_source(client, world):
    staged = _post(client, world, _rows(2), source="amex-api")
    with Session(client.app_module.db_engine) as own:
        row = own.get(Batch, staged.json()["batch_id"])
        assert row.actor_id is not None, "attributed to the person, not the key"
        assert row.source["format"]["source"] == "amex-api"
        assert "agent" in row.source["filename"]


# --------------------------------------------------------------------------- #
# Money, at the edge
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "amount", [-12.50, -1250.0, 12.5], ids=["fractional", "whole-float", "positive-float"]
)
def test_a_json_float_amount_is_refused_and_nothing_is_staged(client, world, amount):
    """Spec test 13. `money.to_minor` accepts floats on purpose, for CSV cells
    that have no type. This edge is different: the caller chose the encoding."""
    before = _count(client, ImportLine)
    refused = _post(client, world, [{"date": "2026-07-05", "amount_minor": amount}])

    assert refused.status_code == 422
    assert _count(client, ImportLine) == before, "nothing was staged"


def test_a_decimal_string_is_converted_with_the_accounts_own_currency(client, world):
    """Spec test 14, and the two-currency fixture earning its keep.

    EUR has two decimal places and JPY has none. An agent that assumed two
    everywhere would be wrong by a factor of a hundred on every yen amount --
    so it does not have to assume: it sends "12.50" and the account decides.
    """
    eur = _post(client, world, [{"date": "2026-07-05", "amount": "-12.50"}])
    assert eur.status_code == 201, eur.text

    jpy = _post(
        client, world, [{"date": "2026-07-06", "amount": "-1250"}],
        account=world["yen"]["id"], source="tokyo",
    )
    assert jpy.status_code == 201, jpy.text

    with Session(client.app_module.db_engine) as own:
        amounts = sorted(
            own.execute(select(ImportLine.parsed)).scalars().all(),
            key=lambda p: p["amount"],
        )
    assert {p["amount"] for p in amounts} == {-1250}, "EUR 12.50 and JPY 1250 are both -1250"


def test_both_amounts_or_neither_is_refused(client, world):
    for row in (
        {"date": "2026-07-05"},
        {"date": "2026-07-05", "amount_minor": -1250, "amount": "-12.50"},
    ):
        refused = _post(client, world, [row])
        assert refused.status_code == 422
        assert "exactly one" in refused.text


def test_an_invented_field_name_is_refused_rather_than_dropped(client, world):
    """A silently ignored `amount_cents` is a row with no money in it."""
    refused = _post(
        client, world, [{"date": "2026-07-05", "amount_minor": -1250, "amount_cents": -1250}]
    )
    assert refused.status_code == 422


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #


def test_the_identical_payload_twice_is_refused_while_the_first_is_still_staged(
    client, world
):
    """Spec test 12 — and the case `previous_import_of` used to miss entirely.

    It matched only `applied`, so a staged import did not count as
    already-seen. A key without `may_commit` leaves every import in `preview`
    by design, so re-posting staged the same rows again, and again.
    """
    rows = _rows(3)
    first = _post(client, world, rows)
    assert first.status_code == 201

    before = _count(client, ImportLine)
    second = _post(client, world, rows)

    assert second.status_code == 409, second.text
    assert "already staged" in second.text
    assert _count(client, ImportLine) == before, "nothing was staged a second time"


def test_an_external_id_repeated_across_two_imports_is_a_duplicate(client, world):
    """Spec test 15. The reuse that matters: `external_id` becomes `fitid`,
    which `stage()` already prefers over its derived key."""
    _post(client, world, _rows(3)).json()
    client.post(
        f"{V1}/imports/{_post(client, world, _rows(1, start=9)).json()['batch_id']}/commit",
        headers={"authorization": f"Bearer {world['committer']}"},
    )

    overlapping = _post(client, world, _rows(1, start=9) + _rows(1, start=10), source="again")
    assert overlapping.status_code == 201, overlapping.text

    outcomes = [line["outcome"] for line in overlapping.json()["lines"]]
    assert "duplicate" in " ".join(outcomes), f"expected a duplicate verdict, got {outcomes}"


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_a_retry_with_the_same_key_gets_the_first_answer(client, world):
    """A timeout on a request that worked must not become a double import.

    The digest guard answers 409 to a repeat, which is right for a mistake and
    wrong for a retry: the agent asking again is the honest thing to do.
    """
    rows = _rows(4)
    first = _post(client, world, rows, key="abc-123")
    assert first.status_code == 201

    staged_lines = _count(client, ImportLine)
    again = _post(client, world, rows, key="abc-123")

    assert again.status_code == 201, again.text
    assert again.json()["batch_id"] == first.json()["batch_id"]
    assert _count(client, ImportLine) == staged_lines, "the retry staged nothing new"


def test_the_same_key_with_different_rows_is_refused(client, world):
    """That is not a retry, and replaying the first answer would discard it."""
    _post(client, world, _rows(2), key="abc-123")
    different = _post(client, world, _rows(2, start=50), key="abc-123")

    assert different.status_code == 409
    assert "different request" in different.text


def test_one_keys_idempotency_header_is_invisible_to_another(client, world):
    """Scoped by key as well as by header.

    Two agents picking `retry-1` independently is not a collision, and one must
    never be handed the other's answer -- which would mean handing it another
    key's staged import.
    """
    rows = _rows(2)
    mine = _post(client, world, rows, key="retry-1")
    assert mine.status_code == 201

    # The same header, the same rows, a different key: the replay must not be
    # found, so the digest guard is what answers -- and it answers 409.
    theirs = _post(client, world, rows, token=world["committer"], key="retry-1")
    assert theirs.status_code == 409, theirs.text
    assert "already staged" in theirs.text, (
        "it must fall through to the dedupe guard, not replay somebody else's answer"
    )


# --------------------------------------------------------------------------- #
# Scope and the hand-off
# --------------------------------------------------------------------------- #


def test_a_read_only_key_cannot_stage_anything(client, world):
    before = _count(client, ImportLine)
    refused = _post(client, world, _rows(2), token=world["reader"])

    assert refused.status_code == 403
    assert "read-only" in refused.text
    assert _count(client, ImportLine) == before


def test_a_key_without_may_commit_is_refused_and_the_import_stays_preview(client, world):
    """Spec test 16, and the refusal has to hand off rather than only fail."""
    staged = _post(client, world, _rows(3))
    batch_id = staged.json()["batch_id"]

    refused = client.post(
        f"{V1}/imports/{batch_id}/commit",
        headers={"authorization": f"Bearer {world['token']}"},
    )
    assert refused.status_code == 403
    assert "preview" in refused.json()["detail"], "the refusal should say where the work went"

    with Session(client.app_module.db_engine) as own:
        assert own.get(Batch, batch_id).status is BatchStatus.preview
    assert _count(client, Transaction) == 0


def test_another_households_import_is_a_404(client, world):
    staged = _post(client, world, _rows(2))
    assert client.post(
        f"{V1}/imports/{staged.json()['batch_id']}/commit",
        headers={"authorization": f"Bearer {world['committer']}"},
    ).status_code == 200

    assert client.post(
        f"{V1}/imports/invented/commit",
        headers={"authorization": f"Bearer {world['committer']}"},
    ).status_code == 404


def test_committing_twice_through_a_key_leaves_the_import_applied_and_undoable(
    client, world
):
    """Issue #212, the agent door. One request per import used to be enough for
    a leaked committer key to lock every applied import against undo."""
    staged = _post(client, world, _rows(3)).json()["batch_id"]
    other = _post(client, world, _rows(2, start=10)).json()["batch_id"]
    headers = {"authorization": f"Bearer {world['committer']}"}

    assert client.post(f"{V1}/imports/{staged}/commit", headers=headers).status_code == 200
    again = client.post(f"{V1}/imports/{staged}/commit", headers=headers)
    assert again.status_code == 409, again.text

    with Session(client.app_module.db_engine) as own:
        assert own.get(Batch, staged).status is BatchStatus.applied
        assert own.get(Batch, other).status is BatchStatus.preview, "the other is untouched"
    assert _count(client, Transaction) == 3

    from app.audit.guard import AuditedSession
    from app.audit.undo import undo_batch

    with AuditedSession(bind=client.app_module.db_engine, expire_on_commit=False) as own:
        actor = own.execute(select(User.id)).scalars().first()
        undo_batch(own, staged, actor_id=actor)
        own.commit()
    assert _count(client, Transaction) == 0, "still an applied batch, so still undoable"


# --------------------------------------------------------------------------- #
# Limits and the log
# --------------------------------------------------------------------------- #


def test_more_than_a_thousand_rows_is_refused(client, world):
    refused = _post(client, world, _rows(1001))
    assert refused.status_code == 422


def test_the_request_log_records_the_import_and_its_batch(client, world):
    from app.models import AgentRequest

    staged = _post(client, world, _rows(5))
    with Session(client.app_module.db_engine) as own:
        line = own.execute(
            select(AgentRequest).order_by(AgentRequest.at.desc())
        ).scalars().first()

    assert line.route.endswith("/imports")
    assert line.rows == 5
    assert line.batch_id == staged.json()["batch_id"], "the join from what was asked to what changed"
