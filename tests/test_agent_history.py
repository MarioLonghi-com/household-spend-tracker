"""What History says a program did, and whose authority it used.

The requirement is one sentence — *consider the logging of these AI/LLM agent
actions in the history* — and the whole design is in how it is answered:
**attribution is a pair, not a substitute.** `actor_id` stays the human,
because a key borrows their authority rather than replacing them.

The test that matters most is the last one. A key is swept thirty days after
revocation and `agent_key_id` is SET NULL; History has to keep reading
correctly afterwards, which is what the denormalised name in `source` is for.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import (
    AgentKey,
    AgentScope,
    BatchKind,
    Household,
    User,
)
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner

V1 = "/api/agent/v1"


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
            key, token = key_service.issue(
                own, user=user, household=row, label="receipt filer",
                agent_name="Claude Desktop", scope=AgentScope.write,
            )
            anon, anon_token = key_service.issue(
                own, user=user, household=row, label="a nameless one",
                scope=AgentScope.write,
            )
        own.commit()
        key_id, anon_id = key.id, anon.id
    return {
        "client": client, "made": made, "house": house, "account": account,
        "token": token, "key_id": key_id,
        "anon_token": anon_token, "anon_id": anon_id,
        "user_name": made["user"]["display_name"],
    }


def _stage(world, *, token=None, rows=2, source="amex-api"):
    body = {
        "account_id": world["account"]["id"],
        "source": source,
        "rows": [
            {"date": f"2026-07-0{i + 1}", "amount_minor": -100 * (i + 1),
             "payee": f"Shop {i + 1}", "external_id": f"{source}:{i + 1}"}
            for i in range(rows)
        ],
    }
    return world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports",
        json=body,
        headers={"authorization": f"Bearer {token or world['token']}", **HEADERS},
    )


def _history(world, **params):
    return world["client"].get(
        f"/api/households/{world['house']['id']}/batches",
        params=params,
        headers=HEADERS,
    ).json()


# --------------------------------------------------------------------------- #
# Both names
# --------------------------------------------------------------------------- #


def test_a_row_names_the_person_and_the_program(world):
    staged = _stage(world)
    assert staged.status_code == 201, staged.text

    row = next(b for b in _history(world) if b["id"] == staged.json()["batch_id"])

    assert row["actor_name"] == world["user_name"], "the human is still the actor"
    assert row["via"] == "Claude Desktop (receipt filer)"
    assert row["agent_key_id"] == world["key_id"]


def test_a_key_that_did_not_name_itself_still_named_its_job(world):
    staged = _stage(world, token=world["anon_token"], source="nameless")
    row = next(b for b in _history(world) if b["id"] == staged.json()["batch_id"])

    assert row["via"] == "a nameless one", "the label is the more useful half anyway"


def test_a_persons_own_work_carries_no_via(world):
    made = world["client"].post(
        f"/api/households/{world['house']['id']}/transactions",
        json={"account_id": world["account"]["id"], "date": "2026-07-09",
              "amount": -500, "payee_name": "By hand"},
        headers=HEADERS,
    )
    assert made.status_code == 201

    rows = _history(world, include_single_edits=True)
    manual = [b for b in rows if b["via"] is None]
    assert manual, "most of History is people, and says nothing about keys"


def test_there_is_no_agent_batch_kind(world):
    """`BatchKind` answers *what was done*; who did it is a different axis.

    A new kind would mean an agent import and a human import sorting into
    different buckets, so every query asking "show me the imports" would have
    to know about both.
    """
    staged = _stage(world)
    row = next(b for b in _history(world) if b["id"] == staged.json()["batch_id"])
    assert row["kind"] == "import", "an agent import is an import"

    from app.models import BatchKind as Kinds

    assert not hasattr(Kinds, "agent")


# --------------------------------------------------------------------------- #
# Never hidden
# --------------------------------------------------------------------------- #


def test_a_single_change_agent_batch_is_not_hidden(world):
    """Spec test 29.

    Single-change *manual* batches are hidden because every keystroke in the
    register is one. A hundred small agent edits are the opposite: exactly what
    somebody opens this screen to find.
    """
    staged = _stage(world, rows=1)
    shown = [b["id"] for b in _history(world)]
    assert staged.json()["batch_id"] in shown


def test_a_single_change_manual_batch_is_still_hidden(world):
    """The other half, so the clause cannot be widened by accident.

    A memo edit, not a new transaction: creating one also creates its payee,
    which is two changes and would be shown anyway. The clause is about batches
    that touched exactly one row.
    """
    made = world["client"].post(
        f"/api/households/{world['house']['id']}/transactions",
        json={"account_id": world["account"]["id"], "date": "2026-07-09",
              "amount": -500, "payee_name": "By hand"},
        headers=HEADERS,
    ).json()
    edited = world["client"].patch(
        f"/api/transactions/{made['id']}",
        json={"memo": "one field, one change"},
        headers=HEADERS,
    )
    assert edited.status_code == 200, edited.text

    default = [b["id"] for b in _history(world)]
    with_them = [b["id"] for b in _history(world, include_single_edits=True)]

    hidden = set(with_them) - set(default)
    assert hidden, "a single-change manual edit is still hidden by default"


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


def test_history_can_be_filtered_to_what_programs_did(world):
    agent_batch = _stage(world).json()["batch_id"]
    world["client"].post(
        f"/api/households/{world['house']['id']}/transactions",
        json={"account_id": world["account"]["id"], "date": "2026-07-09",
              "amount": -500, "payee_name": "By hand"},
        headers=HEADERS,
    )

    agents = _history(world, actor="agent")
    assert [b["id"] for b in agents] == [agent_batch]
    assert all(b["via"] for b in agents)

    humans = _history(world, actor="human")
    assert agent_batch not in [b["id"] for b in humans]
    assert all(b["via"] is None for b in humans)


def test_history_can_be_filtered_to_one_key(world):
    """"Undo everything that key did on Tuesday" starts as a filtered list."""
    mine = _stage(world).json()["batch_id"]
    theirs = _stage(world, token=world["anon_token"], source="nameless").json()["batch_id"]

    only = [b["id"] for b in _history(world, agent_key_id=world["key_id"])]
    assert only == [mine]
    assert theirs not in only


# --------------------------------------------------------------------------- #
# The sentence outlives the key
# --------------------------------------------------------------------------- #


def test_history_still_reads_correctly_after_the_key_is_swept(world):
    """Spec test 28, and the reason the name is denormalised at all.

    `agent_key_id` is SET NULL and a key is swept thirty days after
    revocation. If `via` were read from the foreign key, every History row a
    departed key wrote would degrade to "via a key since removed" — which is a
    worse sentence than the name, and the audit log's whole premise is that it
    stays readable.
    """
    staged = _stage(world)
    batch_id = staged.json()["batch_id"]

    before = next(b for b in _history(world) if b["id"] == batch_id)
    assert before["via"] == "Claude Desktop (receipt filer)"
    assert before["agent_key_id"] == world["key_id"]

    # The key goes, the way housekeeping takes it: through the ORM, in a batch.
    from app.audit.guard import AuditedSession

    with AuditedSession(bind=world["client"].app_module.db_engine, expire_on_commit=False) as own:
        key = own.get(AgentKey, world["key_id"])
        with batch(own, kind=BatchKind.admin, actor_id=key.user_id,
                   household_id=key.household_id):
            own.delete(key)
        own.commit()

    after = next(b for b in _history(world) if b["id"] == batch_id)
    assert after["agent_key_id"] is None, "the live link is gone, as SET NULL intends"
    assert after["via"] == "Claude Desktop (receipt filer)", "and the sentence survives it"
    assert after["actor_name"] == world["user_name"]


def test_the_detail_panel_names_the_program_too(world):
    staged = _stage(world)
    detail = world["client"].get(
        f"/api/households/{world['house']['id']}/batches/{staged.json()['batch_id']}",
        headers=HEADERS,
    ).json()

    assert detail["via"] == "Claude Desktop (receipt filer)"
    assert detail["actor_name"] == world["user_name"]


# --------------------------------------------------------------------------- #
# The two readers that had the column and did not read it
# --------------------------------------------------------------------------- #
#
# History knew who acted. The transaction panel -- which is where somebody
# actually stands when they ask "why is this EUR 45 here" -- did not: its
# change list resolved the actor straight off `Batch.actor_id` and never
# looked at the agent, and *Where did this come from?* described every
# imported row as coming from a file, including the ones that never did.


def _committed(world):
    """One agent import, staged and applied, so there are rows to ask about.

    A committing key of its own: the fixture's two are write-but-not-commit,
    and `may_commit` is a separate grant for a reason.
    """
    with Session(world["client"].app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, world["made"]["user"]["id"])
        house = own.get(Household, world["house"]["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house.id):
            _key, token = key_service.issue(
                own, user=user, household=house, label="the bookkeeper",
                agent_name="Claude Desktop", scope=AgentScope.write, may_commit=True,
            )
        own.commit()

    auth = {"authorization": f"Bearer {token}", **HEADERS}
    staged = world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports",
        json={
            "account_id": world["account"]["id"],
            "source": "Santander card statement, May-June 2026",
            "rows": [{"date": "2026-07-09", "amount_minor": -4_500,
                      "payee": "FALAFEL CORNER", "external_id": "sant-1"}],
        },
        headers=auth,
    )
    assert staged.status_code == 201, staged.text
    batch_id = staged.json()["batch_id"]

    applied = world["client"].post(f"{V1}/imports/{batch_id}/commit", json={}, headers=auth)
    assert applied.status_code in (200, 201), applied.text

    rows = world["client"].get(
        f"/api/households/{world['house']['id']}/transactions", headers=HEADERS
    ).json()["transactions"]
    assert len(rows) == 1, rows
    return rows[0]["id"]


def test_the_transaction_panels_log_names_the_program(world):
    """The panel is where the question is actually asked.

    Its change list resolved the actor off `Batch.actor_id` directly, so a row
    an agent created and a row somebody typed read identically -- in the one
    place whose whole job is telling them apart.
    """
    txn_id = _committed(world)

    changes = world["client"].get(
        f"/api/households/{world['house']['id']}/changes"
        f"?table=transactions&row_id={txn_id}",
        headers=HEADERS,
    )
    assert changes.status_code == 200, changes.text
    entries = changes.json()
    assert entries, "the row was created in a batch, so the log has it"

    made = entries[0]
    assert made["actor_name"] == world["user_name"], "the human is still the actor"
    assert made["via"] == "Claude Desktop (the bookkeeper)", (
        "and the program is named beside them"
    )


def test_a_row_somebody_typed_carries_no_via_in_the_panel(world):
    """The control. `via` everywhere is the same bug as `via` nowhere."""
    made = world["client"].post(
        f"/api/households/{world['house']['id']}/transactions",
        json={"account_id": world["account"]["id"], "date": "2026-07-10",
              "amount": -900, "payee_name": "By hand"},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text

    entries = world["client"].get(
        f"/api/households/{world['house']['id']}/changes"
        f"?table=transactions&row_id={made.json()['id']}",
        headers=HEADERS,
    ).json()

    assert entries[0]["actor_name"] == world["user_name"]
    assert entries[0]["via"] is None, "nobody was holding a key"


def test_where_a_row_came_from_says_which_program_brought_it(world):
    """*Where did this come from?* could not tell an agent row from an upload.

    An agent import goes through the same staging path a file does -- that is
    the point of the agent API being a second front door rather than a second
    write path -- so it has import lines and reaches this endpoint. It already
    carried the agent's own description of what it read, which is the closest
    thing to a file there is. What it could not say was who was holding the
    pen.
    """
    txn_id = _committed(world)

    origin = world["client"].get(f"/api/transactions/{txn_id}/origin", headers=HEADERS)
    assert origin.status_code == 200, origin.text

    found = origin.json()
    assert found["via"] == "Claude Desktop (the bookkeeper)"
    assert "Santander card statement" in (found["filename"] or ""), (
        "the agent's own description of the source is still there"
    )


def test_an_uploaded_statement_carries_no_via(world):
    """The control, at the other reader. A file has no program behind it."""
    import io

    csv = io.BytesIO(b"date,amount,description\n2026-07-11,-12.50,A SHOP\n")
    sent = world["client"].post(
        f"/api/households/{world['house']['id']}/imports",
        files={"file": ("statement.csv", csv, "text/csv")},
        data={"account_id": world["account"]["id"]},
        headers=HEADERS,
    )
    assert sent.status_code == 201, sent.text

    batch_id = sent.json()["batch_id"]
    row = next(b for b in _history(world) if b["id"] == batch_id)
    assert row["via"] is None, "a person uploaded a file; no key was involved"


# --------------------------------------------------------------------------- #
# Who applied it, when that is not who staged it (issue #213)
# --------------------------------------------------------------------------- #


def _bobs_committer(world) -> tuple[str, str, str]:
    """A second member of the household, holding a key that may commit.

    Returns ``(bob's id, the key's id, the token)``.
    """
    from sqlalchemy import text

    from app.models import HouseholdMember, Role, utcnow

    with Session(world["client"].app_module.db_engine, expire_on_commit=False) as own:
        jane = own.get(User, world["made"]["user"]["id"])
        bob = User(
            email="bob@example.com", email_canonical="bob@example.com",
            display_name="Bob", password_hash="argon2-placeholder",
            role=Role.member, totp_secret=b"sealed-placeholder",
        )
        own.execute(text("PRAGMA defer_foreign_keys=ON"))
        with batch(own, kind=BatchKind.setup, actor_id=bob.id, own_transaction=False):
            own.add(bob)
        own.commit()
        house = own.get(Household, world["house"]["id"])
        with batch(own, kind=BatchKind.admin, actor_id=jane.id, household_id=house.id):
            own.add(HouseholdMember(
                household_id=house.id, user_id=bob.id, added_by_id=jane.id, added_at=utcnow()
            ))
        with batch(own, kind=BatchKind.admin, actor_id=bob.id, household_id=house.id):
            key, token = key_service.issue(
                own, user=bob, household=house, label="bob's bookkeeper",
                agent_name="Ledger Bot", scope=AgentScope.write, may_commit=True,
            )
        own.commit()
        return bob.id, key.id, token


def test_a_key_applying_a_persons_staged_import_is_named_on_it(world):
    """Jane stages a file in the browser; a program holding Bob's key commits it.

    The batch used to describe only the staging: Jane, no `via`, invisible to
    `?actor=agent` -- and Bob's key, which is what actually wrote the rows,
    appeared nowhere but a request log swept after thirty days.
    """
    import io

    bob_id, bob_key, bob_token = _bobs_committer(world)
    sent = world["client"].post(
        f"/api/households/{world['house']['id']}/imports",
        files={"file": ("statement.csv", io.BytesIO(
            b"date,amount,description\n2026-07-11,-12.50,A SHOP\n2026-07-12,-3.00,B SHOP\n"
        ), "text/csv")},
        data={"account_id": world["account"]["id"]},
        headers=HEADERS,
    )
    assert sent.status_code == 201, sent.text
    batch_id = sent.json()["batch_id"]
    # A second staged import, which nobody applies: the filter must not list it.
    other = _stage(world).json()["batch_id"]

    assert [b["id"] for b in _history(world, actor="agent")] == [other], (
        "before the commit, only the key-staged import is an agent's"
    )

    applied = world["client"].post(
        f"{V1}/imports/{batch_id}/commit",
        headers={"authorization": f"Bearer {bob_token}", **HEADERS},
    )
    assert applied.status_code == 200, applied.text

    row = next(b for b in _history(world) if b["id"] == batch_id)
    assert row["actor_name"] == world["user_name"], "Jane staged it; the batch is hers"
    assert row["via"] == "Ledger Bot (bob's bookkeeper), which applied it"
    assert row["detail"].endswith("Applied by Bob."), row["detail"]
    assert row["source"]["committed"]["user_id"] == bob_id
    assert row["source"]["committed"]["agent"]["key_id"] == bob_key

    assert batch_id in [b["id"] for b in _history(world, actor="agent")]
    assert batch_id not in [b["id"] for b in _history(world, actor="human")]
    assert [b["id"] for b in _history(world, agent_key_id=bob_key)] == [batch_id], (
        "and filtering to Bob's key finds what it applied, and nothing else"
    )


def test_a_person_applying_their_own_import_adds_nothing_to_it(world):
    """The control: one hand, both visits, and History reads as it always did."""
    import io

    sent = world["client"].post(
        f"/api/households/{world['house']['id']}/imports",
        files={"file": ("statement.csv", io.BytesIO(
            b"date,amount,description\n2026-07-11,-12.50,A SHOP\n"
        ), "text/csv")},
        data={"account_id": world["account"]["id"]},
        headers=HEADERS,
    )
    batch_id = sent.json()["batch_id"]
    done = world["client"].post(
        f"/api/households/{world['house']['id']}/imports/{batch_id}/commit",
        json={}, headers=HEADERS,
    )
    assert done.status_code == 200, done.text

    row = next(b for b in _history(world) if b["id"] == batch_id)
    assert "committed" not in (row["source"] or {})
    assert row["via"] is None
    assert "Applied by" not in row["detail"]
    assert batch_id in [b["id"] for b in _history(world, actor="human")]
