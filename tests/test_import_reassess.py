"""A queued import is re-decided when it is reopened, not when it was staged.

The verdict on a staged line -- new, a duplicate, a match for something already
typed in -- is a statement about the register at the moment the file was staged.
A preview sits on the queue for days, and in that time the same statement gets
imported from another machine, rows get typed in, rows get deleted and imports
get undone. Reopening one showed the old answer, and the only thing that would
ever have corrected it was pressing Commit -- at which point being wrong means a
duplicated row in the ledger.

So both ends are covered here: reopening re-derives every line, and committing
refuses a line whose id reached the account in the meantime even if nobody
reopened it.

Two accounts, two households and two currencies come from `_world`, which is
shared with `test_import_queue` -- a re-assessment that forgot its account would
otherwise have nothing to be wrong about.
"""

from __future__ import annotations

from tests.conftest import HEADERS
from tests.test_import_queue import CARD_FILE, _queue, _register, _upload, _world


def _open(client, house: str, batch_id: str) -> dict:
    answer = client.get(f"/api/households/{house}/imports/{batch_id}", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _outcomes(preview: dict) -> list[str]:
    return [line["outcome"] for line in preview["lines"]]


def _commit(client, house: str, batch_id: str, **body) -> dict:
    answer = client.post(
        f"/api/households/{house}/imports/{batch_id}/commit", json=body, headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def _stage_twice(client, world) -> tuple[str, str]:
    """The same file staged twice into one account. `force` is what allows it.

    This is the shape the whole feature is about: two previews of one statement,
    one of which is about to be committed underneath the other.
    """
    first = _upload(client, world["house"], world["card"], CARD_FILE, name="october.ofx")
    assert first.status_code == 201, first.text
    second = _upload(
        client, world["house"], world["card"], CARD_FILE, name="october.ofx", force=True
    )
    assert second.status_code == 201, second.text
    return first.json()["batch_id"], second.json()["batch_id"]


# --------------------------------------------------------------------------- #
# Reopening re-derives
# --------------------------------------------------------------------------- #


def test_a_queued_import_sees_the_rows_that_landed_while_it_waited(client):
    """Stage, commit the same file from elsewhere, reopen: three duplicates."""
    world = _world(client)
    waiting, other = _stage_twice(client, world)

    staged = _open(client, world["house"], waiting)
    assert _outcomes(staged) == ["created"] * 3
    assert staged["counts"]["created"] == 3

    landed = _commit(client, world["house"], other)
    assert landed["created"] == 3
    assert len(_register(client, world["house"], world["card"])) == 3

    reopened = _open(client, world["house"], waiting)
    assert _outcomes(reopened) == ["duplicate_skipped"] * 3, (
        "every line is already in the account now"
    )
    assert reopened["counts"]["duplicate_skipped"] == 3
    assert reopened["counts"]["created"] == 0
    assert any("re-checked" in warning for warning in reopened["warnings"]), (
        "and the screen says the answer changed rather than quietly showing a new one"
    )
    for line in reopened["lines"]:
        assert line["reason"] == "this line is already in the account"


def test_reopening_again_says_nothing_when_nothing_moved(client):
    """The warning is about a change, not about the re-check having run."""
    world = _world(client)
    waiting, other = _stage_twice(client, world)
    _commit(client, world["house"], other)

    assert _open(client, world["house"], waiting)["warnings"] != []
    settled = _open(client, world["house"], waiting)
    assert settled["warnings"] == []
    assert _outcomes(settled) == ["duplicate_skipped"] * 3, "and the verdict holds"


def test_undoing_the_import_that_made_it_a_duplicate_makes_it_new_again(client):
    """The check reads both ways, which is what makes the file importable again.

    Without this an undone import leaves its twin preview permanently full of
    duplicates -- the rows are gone from the register and the queue still says
    they are there, so the only way back is to discard and re-upload the file.
    """
    world = _world(client)
    waiting, other = _stage_twice(client, world)
    _commit(client, world["house"], other)
    assert _outcomes(_open(client, world["house"], waiting)) == ["duplicate_skipped"] * 3

    undone = client.post(
        f"/api/households/{world['house']}/batches/{other}/undo", headers=HEADERS
    )
    assert undone.status_code == 200, undone.text
    assert _register(client, world["house"], world["card"]) == []

    back = _open(client, world["house"], waiting)
    assert _outcomes(back) == ["created"] * 3, "the rows are gone, so the lines are new again"
    assert back["counts"]["created"] == 3
    for line in back["lines"]:
        assert line["reason"] is None, "and no stale reason is left behind"


def test_a_row_typed_in_after_staging_becomes_a_match(client):
    """Hand-entered while the preview waited: absorbed, not added a second time."""
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()["batch_id"]
    assert _outcomes(_open(client, world["house"], staged)) == ["created"] * 3

    typed = client.post(
        f"/api/households/{world['house']}/transactions",
        json={
            "account_id": world["card"],
            "date": "2025-10-02",
            "amount": -4250,
            "payee_name": "Gull Kitchen",
        },
        headers=HEADERS,
    )
    assert typed.status_code == 201, typed.text

    reopened = _open(client, world["house"], staged)
    assert _outcomes(reopened) == ["matched_existing", "created", "created"]
    matched = reopened["lines"][0]
    assert matched["transaction_id"] == typed.json()["id"]
    assert "already have" in matched["reason"]

    # And committing it absorbs rather than adds: three rows, not four.
    done = _commit(client, world["house"], staged)
    assert (done["created"], done["absorbed"]) == (2, 1)
    assert len(_register(client, world["house"], world["card"])) == 3


def test_a_match_whose_row_was_deleted_goes_back_to_new(client):
    """Otherwise the commit refuses the line and the statement loses a row."""
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()["batch_id"]
    typed = client.post(
        f"/api/households/{world['house']}/transactions",
        json={
            "account_id": world["card"],
            "date": "2025-10-02",
            "amount": -4250,
            "payee_name": "Gull Kitchen",
        },
        headers=HEADERS,
    ).json()
    assert _outcomes(_open(client, world["house"], staged))[0] == "matched_existing"

    gone = client.delete(f"/api/transactions/{typed['id']}", headers=HEADERS)
    assert gone.status_code == 204, gone.text

    reopened = _open(client, world["house"], staged)
    assert _outcomes(reopened) == ["created"] * 3
    assert reopened["lines"][0]["transaction_id"] is None

    done = _commit(client, world["house"], staged)
    assert done["created"] == 3, "all three land, none held back by a row that is gone"


def test_a_category_chosen_by_hand_survives_the_re_check(client):
    """The re-check re-derives the verdict. It does not re-derive a decision."""
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()["batch_id"]
    groups = client.get(
        f"/api/households/{world['house']}/categories", headers=HEADERS
    ).json()
    category = next(one for group in groups for one in group["categories"])

    line = _open(client, world["house"], staged)["lines"][0]
    chosen = client.patch(
        f"/api/households/{world['house']}/imports/{staged}/lines/{line['id']}",
        json={"category_id": category["id"]},
        headers=HEADERS,
    )
    assert chosen.status_code == 200, chosen.text

    # Move the register under it: the *other* two lines get a twin each.
    for day, amount in (("2025-10-03", -1800), ("2025-10-04", -940)):
        client.post(
            f"/api/households/{world['house']}/transactions",
            json={"account_id": world["card"], "date": day, "amount": amount},
            headers=HEADERS,
        )

    reopened = _open(client, world["house"], staged)
    assert _outcomes(reopened) == ["created", "matched_existing", "matched_existing"]
    assert reopened["lines"][0]["category_id"] == category["id"], (
        "the one the user filed by hand is still filed where they put it"
    )
    assert reopened["lines"][0]["category_chosen"] is True


# --------------------------------------------------------------------------- #
# And the commit guards itself
# --------------------------------------------------------------------------- #


def test_committing_a_stale_preview_does_not_duplicate_the_register(client):
    """The browser tab never reloaded. The commit still refuses the lines.

    This is the failure the whole re-assessment exists to prevent, arriving by
    the one route a re-assessment on read cannot cover: a Commit posted from a
    screen that was rendered before the other import landed.
    """
    world = _world(client)
    waiting, other = _stage_twice(client, world)
    stale = _open(client, world["house"], waiting)
    assert _outcomes(stale) == ["created"] * 3, "what that tab is still showing"

    _commit(client, world["house"], other)

    done = _commit(client, world["house"], waiting)
    assert done["created"] == 0
    assert done["skipped"] == 3
    rows = _register(client, world["house"], world["card"])
    assert len(rows) == 3, "three transactions on the statement, three rows in the register"
    assert sorted(row["amount"] for row in rows) == [-4250, -1800, -940]

    # And the refusal is written down on the lines, not only in the counts: the
    # History entry for this commit has to be able to say why it created none.
    lines = client.get(
        f"/api/households/{world['house']}/batches/{waiting}", headers=HEADERS
    )
    assert lines.status_code == 200, lines.text


def test_one_households_import_is_not_re_checked_against_anothers_account(client):
    """The account comes from the batch, and the batch is scoped to the household.

    Two households hold the same statement. Committing one must not turn the
    other's preview into three duplicates -- the ids are identical and the only
    thing keeping them apart is the account the check reads.
    """
    world = _world(client)
    ours = _upload(client, world["house"], world["card"], CARD_FILE).json()["batch_id"]
    theirs = _upload(client, world["elsewhere"], world["far"], CARD_FILE).json()["batch_id"]

    _commit(client, world["elsewhere"], theirs)
    assert len(_register(client, world["elsewhere"], world["far"])) == 3

    reopened = _open(client, world["house"], ours)
    assert _outcomes(reopened) == ["created"] * 3, "another ledger's rows are not ours"
    assert _queue(client, world["house"])[0]["batch_id"] == ours


def test_an_applied_import_is_not_re_checked_and_keeps_its_origin(client):
    """Issue #212. Re-checking a committed import found every line in the account
    -- its own rows -- and rewrote them all as duplicates with no transaction,
    which cut each row off from `/origin`. `import_lines` is not audited, so
    nothing could put that back."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models import ImportLine

    world = _world(client)
    ours = _upload(client, world["house"], world["card"], CARD_FILE).json()["batch_id"]
    _commit(client, world["house"], ours)
    row = _register(client, world["house"], world["card"])[0]

    def lines() -> list[tuple]:
        with Session(client.app_module.db_engine) as own:
            return [
                (line.id, line.outcome, line.reason, line.transaction_id)
                for line in own.execute(
                    select(ImportLine)
                    .where(ImportLine.batch_id == ours)
                    .order_by(ImportLine.line_no)
                ).scalars()
            ]

    before = lines()
    assert all(one[3] is not None for one in before), "every line names its row"
    origin = client.get(f"/api/transactions/{row['id']}/origin", headers=HEADERS)
    assert origin.status_code == 200, origin.text

    refused = client.get(f"/api/households/{world['house']}/imports/{ours}", headers=HEADERS)
    assert refused.status_code == 409, refused.text

    assert lines() == before, "the lines are exactly as the commit left them"
    again = client.get(f"/api/transactions/{row['id']}/origin", headers=HEADERS)
    assert again.status_code == 200, again.text
    assert again.json() == origin.json()
