"""Finding rows, categorising them, and not leaving litter behind.

Three things the 2026-09-22 run could not do. It read 520 rows to find 6,
because the only route to the register took a cursor and no filters (#41). It
landed 396 transactions uncategorised, because import rows could not carry a
category and there was no bulk endpoint to fix them afterwards (#45). And the
probe batches it staged while working out the auth header are still there,
because nothing could remove them (#47).

The third one is answered differently from the way the issue proposed, and
`test_no_key_may_delete_anything_including_its_own_litter` is the reason --
see its docstring.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentScope, Batch, BatchStatus, ImportLine, Transaction
from tests.conftest import HEADERS, _setup_owner
from tests.test_agent_imports import V1, _key


@pytest.fixture()
def world(client):
    """Two accounts, two currencies, real categories, and two keys.

    The second key is not decoration: #47's whole question is whose litter an
    agent may clear, and one key cannot show that it is only ever its own.
    """
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    other = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Tokyo", "type": "cash", "currency": "JPY"},
        headers=HEADERS,
    ).json()
    tree = client.get(f"/api/households/{house['id']}/categories", headers=HEADERS).json()
    cats = {one["name"]: one for group in tree for one in group["categories"]}

    token = _key(client, made, house["id"], scope=AgentScope.write, may_commit=True)
    second = _key(client, made, house["id"], scope=AgentScope.write, may_commit=True)
    client.cookies.clear()
    return {
        "client": client, "house": house, "account": account, "other": other,
        "cat": cats, "token": token, "second": second,
    }


def _auth(world, token=None, key=None) -> dict:
    headers = {"authorization": f"Bearer {token or world['token']}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _stage(world, rows, *, token=None, key=None, account=None, query=""):
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports{query}",
        json={
            "account_id": account or world["account"]["id"],
            "source": "Santander August 2026",
            "rows": rows,
        },
        headers=_auth(world, token, key),
    )
    assert answer.status_code == 201, answer.text
    return answer.json()


def _commit(world, preview, *, token=None, query=""):
    answer = world["client"].post(
        f"{V1}/imports/{preview['batch_id']}/commit{query}",
        json={},
        headers=_auth(world, token, key=preview["batch_id"]),
    )
    assert answer.status_code in (200, 201), answer.text
    return answer.json()


def _row(day, minor, payee, external_id, **extra):
    return {
        "date": day, "amount_minor": minor, "payee": payee,
        "external_id": external_id, **extra,
    }


# --------------------------------------------------------------------------- #
# #41 -- 520 rows read to find 6
# --------------------------------------------------------------------------- #


def test_a_transaction_can_be_found_by_the_id_its_source_gave_it(world):
    rows = [_row(f"2026-08-0{i}", -1_000 * i, f"Shop {i}", f"sant-{i}") for i in range(1, 4)]
    _commit(world, _stage(world, rows))

    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_ids": ["sant-1", "sant-3", "sant-never"]},
        headers=_auth(world),
    )
    assert answer.status_code == 200, answer.text

    found = answer.json()
    assert set(found["found"]) == {"sant-1", "sant-3"}, "the two that exist, and only those"
    assert found["missing"] == ["sant-never"], "and it says which did not, rather than dropping it"

    # The ids are real: each one resolves to the row that carries it.
    with Session(world["client"].app_module.db_engine) as own:
        for external_id, txn_id in found["found"].items():
            row = own.get(Transaction, txn_id)
            assert row is not None and row.import_id == external_id


def test_everything_from_one_source_can_be_asked_for_at_once(world):
    _commit(world, _stage(world, [
        _row("2026-08-01", -1_000, "A", "sant-1"),
        _row("2026-08-02", -2_000, "B", "sant-2"),
        _row("2026-08-03", -3_000, "C", "other-1"),
    ]))

    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_id_prefix": "sant-"},
        headers=_auth(world),
    ).json()

    assert set(answer["found"]) == {"sant-1", "sant-2"}
    assert answer["missing"] == [], "a prefix that matches less is not a miss"


def test_a_lookup_reaches_only_this_households_rows(world):
    """The same boundary as everything else here, at a new door."""
    _commit(world, _stage(world, [_row("2026-08-01", -1_000, "A", "sant-1")]))

    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_ids": ["sant-1"]},
        headers=_auth(world),
    ).json()
    assert set(answer["found"]) == {"sant-1"}

    elsewhere = world["client"].post(
        f"{V1}/households/00000000000000000000000000000000/transactions/lookup",
        json={"external_ids": ["sant-1"]},
        headers=_auth(world),
    )
    assert elsewhere.status_code == 404, "and a household this key is not for is not real"


def test_both_ways_of_asking_at_once_is_refused(world):
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_ids": ["a"], "external_id_prefix": "a"},
        headers=_auth(world),
    )
    assert answer.status_code == 422, answer.text


def test_a_commit_can_hand_back_the_mapping_so_there_is_no_second_call(world):
    """The cheapest variant: the answer arrives with the thing it describes."""
    rows = [_row(f"2026-08-0{i}", -1_000 * i, f"Shop {i}", f"sant-{i}") for i in range(1, 4)]
    staged = _stage(world, rows)

    quiet = _commit(world, staged)
    assert quiet["created_ids"] is None, "off by default -- it is 12 KB for a real import"

    # A second import, because the first is committed and cannot be again.
    more = _stage(world, [_row("2026-08-09", -9_000, "Later", "sant-9")], key="x")
    loud = _commit(world, more, query="?include_ids=true")

    assert set(loud["created_ids"]) == {"sant-9"}
    with Session(world["client"].app_module.db_engine) as own:
        row = own.get(Transaction, loud["created_ids"]["sant-9"])
        assert row is not None and row.import_id == "sant-9"


# --------------------------------------------------------------------------- #
# #45 -- 396 rows landed uncategorised
# --------------------------------------------------------------------------- #


def test_a_row_can_say_what_it_is_for(world):
    groceries = world["cat"]["Groceries"]["id"]
    staged = _stage(world, [
        _row("2026-08-01", -4_500, "Mercadona", "c-1", category_id=groceries),
        _row("2026-08-02", -1_200, "Unknown", "c-2"),
    ])

    assert staged["decision"]["uncategorised"] == 1, "one of the two said what it was for"

    _commit(world, staged)
    with Session(world["client"].app_module.db_engine) as own:
        rows = {
            r.import_id: r
            for r in own.execute(select(Transaction)).scalars()
        }
    assert rows["c-1"].category_id == groceries, "the row landed categorised"
    assert rows["c-2"].category_id is None


def test_a_prefix_with_an_underscore_or_percent_is_a_prefix(world):
    """Issue #239. `startswith` does not escape unless asked, so "%" matched
    every id and an agent's "ord_" matched "ord-" and "ordX" too."""
    _commit(world, _stage(world, [
        _row("2026-08-01", -1_000, "A", "ord_1"),
        _row("2026-08-02", -2_000, "B", "ord-2"),
        _row("2026-08-03", -3_000, "C", "ordX3"),
    ]))

    def found(prefix: str) -> set[str]:
        answer = world["client"].post(
            f"{V1}/households/{world['house']['id']}/transactions/lookup",
            json={"external_id_prefix": prefix},
            headers=_auth(world),
        )
        assert answer.status_code == 200, answer.text
        return set(answer.json()["found"])

    assert found("ord_") == {"ord_1"}
    assert found("%") == set(), "no id starts with a percent sign"
    assert found("ord") == {"ord_1", "ord-2", "ordX3"}


def test_a_category_that_is_not_real_is_refused_rather_than_dropped(world):
    """`extra="forbid"` exists for this reason and so does this.

    A category silently ignored is a row that lands uncategorised while the
    caller believes otherwise -- which is the failure this whole issue is
    about, arriving by a different door.
    """
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports",
        json={
            "account_id": world["account"]["id"],
            "source": "x",
            "rows": [_row("2026-08-01", -1_000, "A", "c-1", category_id="not-a-category")],
        },
        headers=_auth(world),
    )
    assert answer.status_code == 422, answer.text
    assert "not categories in this household" in answer.json()["detail"]


def test_how_sure_the_agent_was_is_kept_as_a_claim(world):
    """Kept for a person reading the preview. Nothing decides from it."""
    groceries = world["cat"]["Groceries"]["id"]
    staged = _stage(world, [
        _row("2026-08-01", -4_500, "Mercadona", "c-1",
             category_id=groceries, category_confidence=0.4,
             category_reason="guessed from the name"),
    ], query="?include_lines=all")

    claim = staged["lines"][0]["parsed"]["category_claim"]
    assert claim == {"confidence": 0.4, "reason": "guessed from the name"}


def test_rows_already_in_the_ledger_can_be_categorised_in_one_act(world):
    """396 rows, or 396 clicks. One batch, so one undo puts them all back."""
    rows = [_row(f"2026-08-0{i}", -1_000 * i, f"Shop {i}", f"c-{i}") for i in range(1, 4)]
    _commit(world, _stage(world, rows))

    found = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_id_prefix": "c-"},
        headers=_auth(world),
    ).json()["found"]

    groceries = world["cat"]["Groceries"]["id"]
    eating_out = world["cat"]["Eating Out"]["id"]
    answer = world["client"].patch(
        f"{V1}/households/{world['house']['id']}/transactions",
        json={"assignments": [
            {"transaction_id": found["c-1"], "category_id": groceries},
            {"transaction_id": found["c-2"], "category_id": eating_out},
            {"transaction_id": "not-a-row", "category_id": groceries},
        ]},
        headers=_auth(world),
    )
    assert answer.status_code == 200, answer.text

    did = answer.json()
    assert did["changed"] == 2
    assert did["not_found"] == ["not-a-row"], "a silent drop is how a caller believes a lie"

    with Session(world["client"].app_module.db_engine) as own:
        assert own.get(Transaction, found["c-1"]).category_id == groceries
        assert own.get(Transaction, found["c-2"]).category_id == eating_out
        assert own.get(Transaction, found["c-3"]).category_id is None, "untouched"

    # Two different categories in one call is the whole point: a bulk edit
    # applying one category to a selection cannot express this.
    assert groceries != eating_out


def test_re_categorising_is_one_batch_and_therefore_one_undo(world):
    _commit(world, _stage(world, [_row("2026-08-01", -1_000, "A", "c-1")]))
    found = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_ids": ["c-1"]}, headers=_auth(world),
    ).json()["found"]

    did = world["client"].patch(
        f"{V1}/households/{world['house']['id']}/transactions",
        json={"assignments": [
            {"transaction_id": found["c-1"], "category_id": world["cat"]["Groceries"]["id"]}
        ]},
        headers=_auth(world),
    ).json()

    with Session(world["client"].app_module.db_engine) as own:
        opened = own.get(Batch, did["batch_id"])
        assert opened is not None, "it has to be in History to be undoable"
        assert opened.agent_key_id is not None, "and it has to say a program did it"


def test_setting_the_same_category_twice_writes_nothing(world):
    """`unchanged` is not cosmetic: a no-op write is a History entry about nothing."""
    groceries = world["cat"]["Groceries"]["id"]
    _commit(world, _stage(world, [
        _row("2026-08-01", -1_000, "A", "c-1", category_id=groceries)
    ]))
    found = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_ids": ["c-1"]}, headers=_auth(world),
    ).json()["found"]

    did = world["client"].patch(
        f"{V1}/households/{world['house']['id']}/transactions",
        json={"assignments": [{"transaction_id": found["c-1"], "category_id": groceries}]},
        headers=_auth(world),
    ).json()

    assert did["changed"] == 0
    assert did["unchanged"] == 1


def test_a_transfer_leg_is_left_uncategorised_and_named(world):
    """#124: a transfer is not spending, so no door may give a leg a category.

    Left alone and listed rather than refused: one leg among four hundred rows
    should not sink the rest, and a caller told nothing would believe it had
    categorised every row it sent.
    """
    from app.audit.batch import batch
    from app.models import BatchKind, User
    from app.services import transfers

    _commit(world, _stage(world, [
        _row("2026-08-01", -1_000, "Shop", "c-1"),
        _row("2026-08-02", -2_000, "To Tokyo", "c-2"),
    ]))
    _commit(world, _stage(world, [_row("2026-08-02", 300, "From Santander", "y-1")],
                          account=world["other"]["id"]))
    found = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_ids": ["c-1", "c-2", "y-1"]}, headers=_auth(world),
    ).json()["found"]

    engine = world["client"].app_module.db_engine
    with Session(engine) as own:
        actor = own.execute(select(User)).scalars().first()
        with batch(own, kind=BatchKind.manual, actor_id=actor.id,
                   household_id=world["house"]["id"]):
            transfers.link(own, own.get(Transaction, found["c-2"]), own.get(Transaction, found["y-1"]))
        own.commit()

    groceries = world["cat"]["Groceries"]["id"]
    did = world["client"].patch(
        f"{V1}/households/{world['house']['id']}/transactions",
        json={"assignments": [
            {"transaction_id": found[one], "category_id": groceries}
            for one in ("c-1", "c-2", "y-1")
        ]},
        headers=_auth(world),
    ).json()

    assert did["changed"] == 1
    assert did["transfer_legs"] == sorted([found["c-2"], found["y-1"]])
    with Session(engine) as own:
        assert own.get(Transaction, found["c-1"]).category_id == groceries
        assert own.get(Transaction, found["c-2"]).category_id is None
        assert own.get(Transaction, found["y-1"]).category_id is None


def test_a_reconciled_row_is_left_as_it_is_and_named_locked(world):
    """#215: the register refuses to edit a reconciled row, and so does a key.

    Two rows, one reconciled: the other is categorised, the locked one is named
    and keeps the category it had.
    """
    from app.audit.batch import batch
    from app.models import BatchKind, ClearedState, User

    eating_out = world["cat"]["Eating Out"]["id"]
    _commit(world, _stage(world, [
        _row("2026-08-01", -1_000, "Shop", "c-1"),
        _row("2026-08-02", -2_000, "Bar", "c-2", category_id=eating_out),
    ]))
    found = world["client"].post(
        f"{V1}/households/{world['house']['id']}/transactions/lookup",
        json={"external_ids": ["c-1", "c-2"]}, headers=_auth(world),
    ).json()["found"]

    engine = world["client"].app_module.db_engine
    with Session(engine) as own:
        actor = own.execute(select(User)).scalars().first()
        with batch(own, kind=BatchKind.reconciled, actor_id=actor.id,
                   household_id=world["house"]["id"]):
            own.get(Transaction, found["c-2"]).cleared = ClearedState.reconciled
        own.commit()

    groceries = world["cat"]["Groceries"]["id"]
    did = world["client"].patch(
        f"{V1}/households/{world['house']['id']}/transactions",
        json={"assignments": [
            {"transaction_id": found[one], "category_id": groceries} for one in ("c-1", "c-2")
        ]},
        headers=_auth(world),
    )
    assert did.status_code == 200, did.text

    assert did.json()["changed"] == 1
    assert did.json()["locked"] == [found["c-2"]]
    with Session(engine) as own:
        assert own.get(Transaction, found["c-1"]).category_id == groceries
        locked = own.get(Transaction, found["c-2"])
        assert locked.category_id == eating_out, "the locked row kept its category"
        assert locked.cleared is ClearedState.reconciled


def test_an_unknown_key_cannot_categorise(world):
    """`AgentWriter`, not `CurrentAgent`. The same door as every other write."""
    _commit(world, _stage(world, [_row("2026-08-01", -1_000, "A", "c-1")]))

    answer = world["client"].patch(
        f"{V1}/households/{world['house']['id']}/transactions",
        json={"assignments": [{"transaction_id": "anything", "category_id": None}]},
        headers={"authorization": "Bearer stk_invented"},
    )
    assert answer.status_code == 401, answer.text


# --------------------------------------------------------------------------- #
# #47 -- the litter, and the ceiling that decided how to clear it
# --------------------------------------------------------------------------- #


def test_no_key_may_delete_anything_including_its_own_litter(world):
    """The reason #47 is answered with a sweep and not with a DELETE.

    The issue proposed `DELETE /imports/{batch_id}`, scoped to batches the
    calling key staged, and said it "sits near the line and deserves a
    deliberate decision rather than being waved through". It does, and the line
    is drawn harder than the issue knew: §1.2 of the access-control spec says
    no key may delete, and
    `test_agent_access.test_the_capability_floor_is_structural_and_not_a_check`
    enforces it by walking the route table, so the edit fails there rather than
    passing review.

    So the litter expires instead. This asserts the ceiling is still in place,
    from this side too, because a later session reading only this file should
    not have to rediscover why the obvious endpoint is missing.
    """
    answer = world["client"].delete(f"{V1}/imports/anything", headers=_auth(world))
    assert answer.status_code in (404, 405), (
        f"an agent DELETE route exists again: {answer.status_code}"
    )


def test_an_agents_forgotten_preview_is_swept(world):
    from datetime import timedelta

    from app.services import importing

    staged = _stage(world, [_row("2026-08-01", -1_000, "A", "c-1")])
    engine = world["client"].app_module.db_engine

    with Session(engine, expire_on_commit=False) as own:
        row = own.get(Batch, staged["batch_id"])
        assert row.status is BatchStatus.preview
        assert row.agent_key_id is not None, "the sweep keys off this"
        # Older than the window, by the clock the sweep uses.
        row.started_at = row.started_at - importing.STAGED_AGENT_IMPORT_RETENTION
        row.started_at = row.started_at - timedelta(hours=1)
        own.commit()

    with Session(engine, expire_on_commit=False) as own:
        gone = importing.sweep_stale_agent_previews(own)
        own.commit()

    assert gone == 1
    with Session(engine) as own:
        assert own.get(Batch, staged["batch_id"]) is None
        assert own.execute(
            select(ImportLine).where(ImportLine.batch_id == staged["batch_id"])
        ).first() is None, "its lines went with it"


def test_a_recent_preview_is_left_alone(world):
    """The control. A sweep that takes what an agent staged this morning is worse."""
    from app.services import importing

    staged = _stage(world, [_row("2026-08-01", -1_000, "A", "c-1")])
    engine = world["client"].app_module.db_engine

    with Session(engine, expire_on_commit=False) as own:
        assert importing.sweep_stale_agent_previews(own) == 0
        own.commit()

    with Session(engine) as own:
        assert own.get(Batch, staged["batch_id"]) is not None


def test_a_persons_staged_import_is_never_swept(client):
    """The whole reason this is safe to do on a timer.

    Somebody who queues an import and comes back to it in a fortnight must find
    it there. Only an agent's litter is an agent's to lose.
    """
    import io
    from datetime import timedelta

    from app.services import importing

    # Its own world: this one never touches the agent door, and `_setup_owner`
    # walks a wizard that only runs once per instance.
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Theirs"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Theirs", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    sent = client.post(
        f"/api/households/{house['id']}/imports",
        files={"file": ("s.csv", io.BytesIO(b"date,amount,description\n2026-08-01,-9.99,X\n"),
                        "text/csv")},
        data={"account_id": account["id"]},
        headers=HEADERS,
    )
    assert sent.status_code == 201, sent.text
    theirs = sent.json()["batch_id"]

    engine = client.app_module.db_engine
    with Session(engine, expire_on_commit=False) as own:
        row = own.get(Batch, theirs)
        assert row.agent_key_id is None, "a person staged this"
        row.started_at = row.started_at - importing.STAGED_AGENT_IMPORT_RETENTION
        row.started_at = row.started_at - timedelta(days=365)
        own.commit()

    with Session(engine, expire_on_commit=False) as own:
        assert importing.sweep_stale_agent_previews(own) == 0, "it is not the sweep's to take"
        own.commit()

    with Session(engine) as own:
        assert own.get(Batch, theirs) is not None


def test_the_sweep_runs_on_the_timer_rather_than_in_a_docstring(world):
    """A retention window with no caller is the finding housekeeping exists for."""
    import inspect

    from app.auth import housekeeping

    source = inspect.getsource(housekeeping.sweep)
    assert "sweep_stale_agent_previews" in source, (
        "the window is documented and nothing calls it, which is exactly the "
        "shape of the ratelimit.prune finding"
    )
