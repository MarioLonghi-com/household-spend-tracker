"""The Import screen's two unfinished halves: the queue, and the memo.

**The queue.** Staging a file and walking away left it reachable only by its
batch id. The upload refusal even said so -- *"is waiting to be reviewed. Open
that import rather than starting a second one"* -- while offering no way to
open it and no way to throw it away, so that file could never be imported again
without `force`.

**The memo.** A category could be chosen on the preview and a memo could not,
although the memo is the half that distinguishes two identical rows on the same
day. Both are stored on the staged line, so both survive a reload.
"""

from __future__ import annotations

from tests.conftest import HEADERS, _setup_owner

#: ``(fitid, day, amount, name, memo)``.
CARD_ROWS = [
    ("QQ0001", 2, "-42.50", "SQ *GULL KITCHEN LTD", "card 4411"),
    ("QQ0002", 3, "-18.00", "SQ *CEDAR ROOMS", ""),
    ("QQ0003", 4, "-9.40", "SumUp *Riverside Heal", "contactless"),
]


def _ofx(rows) -> str:
    body = "".join(
        "<STMTTRN>"
        f"<TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>202510{day:02d}000000</DTPOSTED>"
        f"<TRNAMT>{amount}</TRNAMT><FITID>{fitid}</FITID><NAME>{name}</NAME>"
        + (f"<MEMO>{memo}</MEMO>" if memo else "")
        + "</STMTTRN>"
        for fitid, day, amount, name, memo in rows
    )
    return (
        '<?xml version="1.0" standalone="no"?>'
        '<?OFX OFXHEADER="200" VERSION="202" SECURITY="NONE"?>'
        "<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>"
        "<CURDEF>EUR</CURDEF><CCACCTFROM><ACCTID>CARD|11223</ACCTID></CCACCTFROM>"
        f"<BANKTRANLIST>{body}</BANKTRANLIST>"
        "</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"
    )


CARD_FILE = _ofx(CARD_ROWS)
OTHER_FILE = _ofx(
    [
        ("QQ1001", 5, "-12.00", "MERCADONA 1234", "weekly"),
        ("QQ1002", 6, "-30.00", "REPSOL E.S. 447", ""),
    ]
)


def _world(client) -> dict:
    """One owner, one household, two accounts -- and a second household, so a
    queue that ignored its household boundary has something to fail against."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    elsewhere = client.post(
        "/api/households", json={"name": "The Other One"}, headers=HEADERS
    ).json()
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
    far = client.post(
        f"/api/households/{elsewhere['id']}/accounts",
        json={"name": "Theirs", "type": "checking"},
        headers=HEADERS,
    ).json()
    return {
        "house": house["id"],
        "elsewhere": elsewhere["id"],
        "checking": checking["id"],
        "card": card["id"],
        "far": far["id"],
    }


def _upload(client, house: str, account: str, content: str, *, name="card.ofx", force=False):
    return client.post(
        f"/api/households/{house}/imports",
        data={"account_id": account, "force": str(force).lower()},
        files={"file": (name, content.encode(), "application/x-ofx")},
        headers=HEADERS,
    )


def _register(client, house: str, account: str) -> list[dict]:
    answer = client.get(
        f"/api/households/{house}/transactions?account_id={account}", headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["transactions"]


def _memos(client, house: str, account: str) -> dict[str, str | None]:
    """Descriptor -> the memo the committed row carries.

    Keyed by payee name: with no rules in this household the commit makes a
    payee of the descriptor itself, so the name *is* what the bank wrote.
    """
    return {row["payee_name"]: row["memo"] for row in _register(client, house, account)}


def _queue(client, house: str) -> list[dict]:
    answer = client.get(f"/api/households/{house}/imports", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #


def test_a_staged_import_is_waiting_on_the_queue_with_enough_to_recognise_it(client):
    world = _world(client)
    assert _queue(client, world["house"]) == [], "nothing staged, nothing listed"

    staged = _upload(client, world["house"], world["card"], CARD_FILE, name="october.ofx")
    assert staged.status_code == 201, staged.text

    waiting = _queue(client, world["house"])
    assert len(waiting) == 1
    one = waiting[0]
    assert one["batch_id"] == staged.json()["batch_id"]
    assert one["filename"] == "october.ofx"
    assert one["account_id"] == world["card"]
    assert one["account_name"] == "Visa"
    assert one["actor_name"] == "Jane", "who staged it, by name"
    assert one["row_count"] == 3, "three lines are waiting in it"
    assert one["staged_at"], "and when"

    # And it opens: the id on the queue is the id the preview reads by.
    opened = client.get(
        f"/api/households/{world['house']}/imports/{one['batch_id']}", headers=HEADERS
    )
    assert opened.status_code == 200, opened.text
    assert len(opened.json()["lines"]) == 3


def test_the_queue_holds_one_entry_per_staged_file(client):
    """Two files, two accounts, newest first."""
    world = _world(client)
    first = _upload(client, world["house"], world["card"], CARD_FILE, name="card.ofx")
    second = _upload(
        client, world["house"], world["checking"], OTHER_FILE, name="current.ofx"
    )

    waiting = _queue(client, world["house"])
    assert [one["filename"] for one in waiting] == ["current.ofx", "card.ofx"]
    assert [one["batch_id"] for one in waiting] == [
        second.json()["batch_id"],
        first.json()["batch_id"],
    ]
    assert [one["account_name"] for one in waiting] == ["Checking", "Visa"]
    assert [one["row_count"] for one in waiting] == [2, 3]


def test_a_committed_import_is_no_longer_waiting(client):
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()
    assert len(_queue(client, world["house"])) == 1

    done = client.post(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    assert done.json()["created"] == 3
    assert _queue(client, world["house"]) == [], "it is history now, not a queue"


def test_one_households_queue_is_not_anothers(client):
    world = _world(client)
    _upload(client, world["house"], world["card"], CARD_FILE, name="ours.ofx")
    _upload(client, world["elsewhere"], world["far"], OTHER_FILE, name="theirs.ofx")

    assert [one["filename"] for one in _queue(client, world["house"])] == ["ours.ofx"]
    assert [one["filename"] for one in _queue(client, world["elsewhere"])] == ["theirs.ofx"]


def test_purging_a_staged_import_takes_its_lines_and_frees_the_file(client):
    """The other half of being able to open one.

    A preview nobody wants is otherwise a permanent refusal to import that
    file, because the duplicate check counts a staged import as already-seen.
    """
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()

    again = _upload(client, world["house"], world["card"], CARD_FILE)
    assert again.status_code == 409
    assert "waiting to be reviewed" in again.json()["detail"]

    purged = client.delete(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}", headers=HEADERS
    )
    assert purged.status_code == 204, purged.text

    assert _queue(client, world["house"]) == []
    gone = client.get(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}", headers=HEADERS
    )
    assert gone.status_code == 404, "the batch and its lines are gone, not hidden"

    # And the file can be staged again, without force.
    fresh = _upload(client, world["house"], world["card"], CARD_FILE)
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["batch_id"] != staged["batch_id"]
    assert len(fresh.json()["lines"]) == 3


def test_purging_writes_nothing_to_the_register(client):
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()
    client.delete(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}", headers=HEADERS
    )

    assert _register(client, world["house"], world["card"]) == []


def test_a_committed_import_is_undone_from_history_not_purged(client):
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()
    client.post(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )

    refused = client.delete(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}", headers=HEADERS
    )
    assert refused.status_code == 409, refused.text
    assert "put back from History" in refused.json()["detail"]

    # The three rows it wrote are still there.
    assert len(_register(client, world["house"], world["card"])) == 3


def test_another_households_import_cannot_be_purged(client):
    """404, not 403: they should not learn the id is real."""
    world = _world(client)
    theirs = _upload(client, world["elsewhere"], world["far"], OTHER_FILE).json()

    refused = client.delete(
        f"/api/households/{world['house']}/imports/{theirs['batch_id']}", headers=HEADERS
    )
    assert refused.status_code == 404
    assert len(_queue(client, world["elsewhere"])) == 1, "and it is still waiting"


def _batches(client, house: str) -> list[dict]:
    answer = client.get(f"/api/households/{house}/batches", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_an_account_from_another_household_cannot_be_imported_into_through_this_one(client):
    """#77. The caller is a member of both, which is exactly the case that got
    through: the account was loaded by "any household you are in" while the
    batch was filed under the one in the path, so the other household's rows
    ended up in this household's History, readable and undoable by members
    who cannot see that account at all."""
    world = _world(client)
    before = {house: _batches(client, world[house]) for house in ("house", "elsewhere")}

    refused = _upload(client, world["house"], world["far"], OTHER_FILE)

    assert refused.status_code == 404, refused.text
    assert _queue(client, world["house"]) == []
    assert _queue(client, world["elsewhere"]) == []
    assert {house: _batches(client, world[house]) for house in before} == before


def _point_staged_import_at(client, batch_id: str, account_id: str) -> None:
    """A staged import whose recorded account belongs elsewhere -- what an
    upload made before #77 was fixed could have left in the queue."""
    from sqlalchemy.orm import Session

    from app.models import Batch

    with Session(client.app_module.db_engine) as own:
        staged = own.get(Batch, batch_id)
        staged.source = {**staged.source, "account_id": account_id}
        own.commit()


def test_a_staged_import_pointing_at_another_household_is_neither_shown_nor_committed(client):
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()
    _point_staged_import_at(client, staged["batch_id"], world["far"])
    theirs_before = _batches(client, world["elsewhere"])

    opened = client.get(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}", headers=HEADERS
    )
    committed = client.post(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )

    assert opened.status_code == 404, opened.text
    assert committed.status_code == 404, committed.text
    assert _register(client, world["elsewhere"], world["far"]) == []
    assert _register(client, world["house"], world["card"]) == []
    assert _batches(client, world["elsewhere"]) == theirs_before


# --------------------------------------------------------------------------- #
# The memo
# --------------------------------------------------------------------------- #


def _line(preview: dict, descriptor: str) -> dict:
    return next(
        one for one in preview["lines"] if (one["parsed"] or {}).get("payee") == descriptor
    )


def _set_memo(client, house: str, batch_id: str, line_id: str, **body):
    return client.patch(
        f"/api/households/{house}/imports/{batch_id}/lines/{line_id}/memo",
        json=body,
        headers=HEADERS,
    )


def test_a_memo_typed_on_the_preview_is_the_memo_that_lands(client):
    world = _world(client)
    preview = _upload(client, world["house"], world["card"], CARD_FILE).json()
    line = _line(preview, "SQ *GULL KITCHEN LTD")
    assert line["parsed"]["memo"] == "card 4411", "what the bank sent"

    changed = _set_memo(
        client, world["house"], preview["batch_id"], line["id"], memo="Dinner with R"
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["parsed"]["memo_chosen"] == "Dinner with R"
    assert changed.json()["parsed"]["memo"] == "card 4411", "beside it, never over it"
    assert changed.json()["raw"] == line["raw"], "and the raw line is untouched"

    # It survives a reload, like the category: it is on the line, not in the browser.
    reopened = client.get(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}", headers=HEADERS
    ).json()
    assert _line(reopened, "SQ *GULL KITCHEN LTD")["parsed"]["memo_chosen"] == "Dinner with R"

    client.post(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )
    memos = _memos(client, world["house"], world["card"])
    assert memos["SQ *GULL KITCHEN LTD"] == "Dinner with R", "the typed one"
    assert memos["SumUp *Riverside Heal"] == "contactless", "and the bank's, untouched"
    assert memos["SQ *CEDAR ROOMS"] is None


def test_handing_the_memo_back_restores_the_banks(client):
    world = _world(client)
    preview = _upload(client, world["house"], world["card"], CARD_FILE).json()
    line = _line(preview, "SQ *GULL KITCHEN LTD")

    _set_memo(client, world["house"], preview["batch_id"], line["id"], memo="mine")
    handed_back = _set_memo(
        client, world["house"], preview["batch_id"], line["id"], clear_memo=True
    )
    assert handed_back.status_code == 200, handed_back.text
    assert "memo_chosen" not in handed_back.json()["parsed"]

    client.post(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )
    memos = _memos(client, world["house"], world["card"])
    assert memos["SQ *GULL KITCHEN LTD"] == "card 4411"


def test_an_emptied_memo_is_a_decision_not_an_omission(client):
    """Emptying the box means "this row has no memo", which is not the same
    request as "use whatever the bank wrote"."""
    world = _world(client)
    preview = _upload(client, world["house"], world["card"], CARD_FILE).json()
    line = _line(preview, "SQ *GULL KITCHEN LTD")

    emptied = _set_memo(client, world["house"], preview["batch_id"], line["id"], memo="")
    assert emptied.json()["parsed"]["memo_chosen"] is None
    assert emptied.json()["parsed"]["memo"] == "card 4411"

    client.post(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )
    memos = _memos(client, world["house"], world["card"])
    assert memos["SQ *GULL KITCHEN LTD"] is None, "the bank's memo was declined"


def test_a_memo_can_be_typed_onto_a_line_with_none_of_its_own(client):
    world = _world(client)
    preview = _upload(client, world["house"], world["card"], CARD_FILE).json()
    line = _line(preview, "SQ *CEDAR ROOMS")
    assert line["parsed"].get("memo") is None

    _set_memo(client, world["house"], preview["batch_id"], line["id"], memo="  Tickets  ")
    client.post(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )
    memos = _memos(client, world["house"], world["card"])
    assert memos["SQ *CEDAR ROOMS"] == "Tickets", "and the whitespace is collapsed"


def test_a_memo_longer_than_the_column_is_refused(client):
    world = _world(client)
    preview = _upload(client, world["house"], world["card"], CARD_FILE).json()
    line = _line(preview, "SQ *CEDAR ROOMS")

    refused = _set_memo(
        client, world["house"], preview["batch_id"], line["id"], memo="x" * 501
    )
    assert refused.status_code == 422
    unchanged = client.get(
        f"/api/households/{world['house']}/imports/{preview['batch_id']}", headers=HEADERS
    ).json()
    assert "memo_chosen" not in (_line(unchanged, "SQ *CEDAR ROOMS")["parsed"] or {})


def test_a_memo_cannot_be_typed_onto_another_households_line(client):
    world = _world(client)
    theirs = _upload(client, world["elsewhere"], world["far"], OTHER_FILE).json()
    line = _line(theirs, "MERCADONA 1234")

    refused = _set_memo(client, world["house"], theirs["batch_id"], line["id"], memo="nope")
    assert refused.status_code == 404

    still = client.get(
        f"/api/households/{world['elsewhere']}/imports/{theirs['batch_id']}", headers=HEADERS
    ).json()
    assert "memo_chosen" not in (_line(still, "MERCADONA 1234")["parsed"] or {})


# --------------------------------------------------------------------------- #
# A committed import is finished (issue #212)
# --------------------------------------------------------------------------- #


def _batch(client, house: str, batch_id: str) -> dict:
    answer = client.get(f"/api/households/{house}/batches/{batch_id}", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_committing_an_applied_import_again_leaves_it_applied_and_undoable(client):
    """A double-click, or a retried request, used to mark the import `failed`.

    `resume`'s except arm caught the refusal and wrote `failed` on its way out,
    and a failed batch cannot be undone -- with its three rows still in the
    register. Two households in the world, so the second one's staged import
    is there to be left alone.
    """
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()
    theirs = _upload(client, world["elsewhere"], world["far"], OTHER_FILE).json()
    url = f"/api/households/{world['house']}/imports/{staged['batch_id']}/commit"

    first = client.post(url, json={}, headers=HEADERS)
    assert first.status_code == 200, first.text
    assert _batch(client, world["house"], staged["batch_id"])["status"] == "applied"

    again = client.post(url, json={}, headers=HEADERS)
    assert again.status_code == 409, again.text
    assert "applied" in again.json()["detail"]
    assert _batch(client, world["house"], staged["batch_id"])["status"] == "applied", (
        "the refusal must not be what marks it failed"
    )
    assert len(_register(client, world["house"], world["card"])) == 3

    undone = client.post(
        f"/api/households/{world['house']}/batches/{staged['batch_id']}/undo", headers=HEADERS
    )
    assert undone.status_code == 200, undone.text
    assert _register(client, world["house"], world["card"]) == [], "and undo takes them back"

    assert [one["batch_id"] for one in _queue(client, world["elsewhere"])] == [
        theirs["batch_id"]
    ]


def test_a_line_of_an_applied_import_cannot_be_edited(client):
    """The line routes are preview operations too; a committed line is history."""
    world = _world(client)
    staged = _upload(client, world["house"], world["card"], CARD_FILE).json()
    line_id = staged["lines"][0]["id"]
    client.post(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}/commit",
        json={},
        headers=HEADERS,
    )

    refused = client.patch(
        f"/api/households/{world['house']}/imports/{staged['batch_id']}/lines/{line_id}/memo",
        json={"memo": "rewritten after the fact"},
        headers=HEADERS,
    )
    assert refused.status_code == 409, refused.text
    memos = _memos(client, world["house"], world["card"])
    assert "rewritten after the fact" not in memos.values()
