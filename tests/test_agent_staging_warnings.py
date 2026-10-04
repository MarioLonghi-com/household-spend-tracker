"""The channel that was wired to a constant, and the four things it now says.

`ImportPreview.warnings` is what stage-then-commit exists to carry. On the
agent path it was `_preview(session, batch_row, lines, [])` -- a hard-coded
empty list -- so both statements on the 2026-09-22 run came back with
`warnings: []`, which an agent correctly read as "all clear". It was not.

Asserting the *content* throughout, not that a list is non-empty. A warnings
channel that fires on everything is the same failure as one that fires on
nothing, so every case here has a control beside it proving the thing stays
quiet when it should.

Issues #43 (the channel), #44 (duplicates), #40 (the response shape), #46
(sign conventions) and #50 (declared totals).
"""

from __future__ import annotations

import pytest

from app.models import AgentScope
from tests.conftest import HEADERS, _setup_owner
from tests.test_agent_imports import V1, _key


@pytest.fixture()
def world(client):
    """Two accounts of different kinds, two currencies, one key.

    The card matters as much as the checking account: the sign warning is
    about reading one into the other, so a fixture with only one of them
    cannot show that it stays quiet on the right one.
    """
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    checking = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    card = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Visa", "type": "credit_card", "currency": "GBP"},
        headers=HEADERS,
    ).json()
    # A committing key, because dedupe is against rows that actually LANDED --
    # `_import_ids_in` reads `Transaction.import_id` -- so a test that only
    # stages proves nothing about duplicates.
    token = _key(client, made, house["id"], scope=AgentScope.write, may_commit=True)
    client.cookies.clear()
    return {"client": client, "house": house, "checking": checking, "card": card, "token": token}


def _stage(world, rows, *, account=None, statement=None, query="", key=None):
    body = {
        "account_id": account or world["checking"]["id"],
        "source": "Santander card statement, May-June 2026",
        "rows": rows,
    }
    if statement is not None:
        body["statement"] = statement
    headers = {"authorization": f"Bearer {world['token']}"}
    if key:
        headers["Idempotency-Key"] = key
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports{query}", json=body, headers=headers
    )
    assert answer.status_code == 201, answer.text
    return answer.json()


def _commit(world, preview) -> dict:
    """Apply a staged batch, so its ids are on the account for the next one."""
    answer = world["client"].post(
        f"{V1}/imports/{preview['batch_id']}/commit",
        json={},
        headers={"authorization": f"Bearer {world['token']}", "Idempotency-Key": preview["batch_id"]},
    )
    assert answer.status_code in (200, 201), answer.text
    return answer.json()


def _landed(world, rows, **kw) -> dict:
    """Stage and commit in one move, for setting up "already there"."""
    return _commit(world, _stage(world, rows, **kw))


def _row(day: str, minor: int, payee: str, external_id: str | None = None) -> dict:
    row = {"date": day, "amount_minor": minor, "payee": payee}
    if external_id:
        row["external_id"] = external_id
    return row


def _said(preview, *, about: str) -> str | None:
    return next((w for w in preview["warnings"] if about in w), None)


# --------------------------------------------------------------------------- #
# #43 -- the channel is real
# --------------------------------------------------------------------------- #


def test_a_clean_import_says_nothing(world):
    """The control, and the one that makes every other test here mean something.

    A channel that fires on an ordinary import is noise, and noise is how a
    real warning gets scrolled past.
    """
    preview = _stage(world, [_row("2026-07-01", -1_000, "Carrefour", "a-1")])

    assert preview["warnings"] == [], preview["warnings"]
    assert preview["decision"]["safe_to_commit"] is True


def test_a_refused_row_is_said_out_loud(world):
    """It used to be discoverable only by counting the line detail."""
    preview = _stage(
        world,
        [
            _row("2026-07-01", -1_000, "Carrefour", "b-1"),
            # Past `money.MAX_MINOR`, which `to_minor` checks and the schema
            # does not -- so this is refused by staging rather than by pydantic,
            # which is the path the warning is about.
            _row("2026-07-02", -(10**19), "Too much", "b-2"),
        ],
    )

    said = _said(preview, about="refused")
    assert said is not None, preview["warnings"]
    assert "1 of 2" in said, said
    assert preview["decision"]["rejected"] == 1
    assert preview["decision"]["safe_to_commit"] is False, "a refused row is not safe to commit"


# --------------------------------------------------------------------------- #
# #44 -- duplicate_skipped is the one that can quietly lose money
# --------------------------------------------------------------------------- #


def test_skipped_duplicates_are_named_not_merely_counted(world):
    """`duplicate_skipped` reads like the importer doing its job.

    Sometimes it is. The caller cannot tell from a count, so the ids come back
    with it -- and in `warnings`, not only in line detail the slimmed response
    no longer returns by default.
    """
    first = [_row("2026-07-01", -2_000, "NIGHT OWL", "night-1")]
    _landed(world, first, key="one")

    again = _stage(world, first + [_row("2026-07-02", -500, "New", "night-2")], key="two")

    said = _said(again, about="already in this account")
    assert said is not None, again["warnings"]
    assert "night-1" in said, "the caller needs to know WHICH row was dropped"
    assert "1 of 2" in said, said
    assert again["decision"]["duplicates"] == 1
    assert again["decision"]["created"] == 1


def test_an_external_id_is_the_whole_identity(world):
    """Four EUR 20 rounds at the same bar on one night are four transactions.

    The derived key is amount + date + how-manyth-that-day, and the counter
    restarts per import while the account's ids do not -- so without this, a
    genuinely new purchase identical in amount and date to one already
    imported is skipped, silently. Supplying external_id takes identity out of
    the server's hands, and this asserts that it really is taken.
    """
    _landed(world, [_row("2026-05-23", -2_000, "NIGHT OWL", "round-1")], key="a")

    later = _stage(
        world,
        [
            _row("2026-05-23", -2_000, "NIGHT OWL", "round-2"),
            _row("2026-05-23", -2_000, "NIGHT OWL", "round-3"),
            _row("2026-05-23", -2_000, "NIGHT OWL", "round-4"),
        ],
        key="b",
    )

    assert later["decision"]["duplicates"] == 0, (
        "same bar, same night, same figure, different events -- and the caller said so"
    )
    assert later["decision"]["created"] == 3
    assert later["warnings"] == [], later["warnings"]


def test_without_an_external_id_the_collision_is_at_least_audible(world):
    """The fallback still loses the row. It no longer loses it quietly.

    This is the behaviour the derived key has by design -- it is what makes
    re-importing an overlapping statement safe -- and the point of the warning
    is that the caller can now tell the two cases apart and act.
    """
    _landed(world, [_row("2026-05-23", -2_000, "NIGHT OWL")], key="a")
    # A different payee, so the *payload* differs and the re-upload guard lets
    # it through -- the derived key is amount + date + occurrence and does not
    # include the payee, so this still collides. Which is exactly the shape of
    # the bug: a genuinely different purchase, same figure, same night.
    second = _stage(world, [_row("2026-05-23", -2_000, "LA TERRAZA")], key="b")

    assert second["decision"]["duplicates"] == 1
    said = _said(second, about="already in this account")
    assert said is not None
    assert "external_id" in said, "and it says what to do about it"


def test_the_rule_is_written_where_a_caller_will_read_it(world):
    """It was in the code and nowhere else. Documentation is the whole fix."""
    manifest = world["client"].get(
        f"{V1}/manifest", headers={"authorization": f"Bearer {world['token']}"}
    ).json()

    rule = manifest["conventions"]["duplicates"]
    assert "external_id" in rule
    assert "amount" in rule and "date" in rule, "the derived key has to be stated too"


# --------------------------------------------------------------------------- #
# #46 -- which way the money was supposed to point
# --------------------------------------------------------------------------- #


def _card_statement_shape() -> list[dict]:
    """A card's rows as they read on the card: purchases out, payments in.

    Read into a checking account, the payments become income. On the run that
    prompted this, roughly EUR 23.4k of them did.
    """
    rows = [_row(f"2026-05-{day:02d}", -1_500, f"Shop {day}", f"c-{day}") for day in range(1, 11)]
    rows.append(_row("2026-05-20", 23_400, "INGRESO DE TARJETA", "c-pay"))
    return rows


def test_a_card_statement_read_into_a_checking_account_is_questioned(world):
    preview = _stage(world, _card_statement_shape())

    said = _said(preview, about="coming IN")
    assert said is not None, preview["warnings"]
    assert "checking" in said
    assert "repayments" in said, "it should name the mistake that is actually being made"
    assert preview["decision"]["safe_to_commit"] is True, (
        "it is a legitimate choice and must never be blocked -- only questioned"
    )


def test_a_month_of_spending_with_a_salary_in_it_is_not_questioned(world):
    """The control, and the reason the rule has two conditions rather than one.

    A salary is a small number of large positives in a checking account, which
    is the same shape -- so a share test on its own would fire every month and
    be ignored by the second one. The batch also has to NET positive.
    """
    rows = [_row(f"2026-06-{day:02d}", -4_000, f"Shop {day}", f"s-{day}") for day in range(1, 11)]
    rows.append(_row("2026-06-25", 30_000, "Employer", "s-pay"))

    preview = _stage(world, rows)

    assert _said(preview, about="coming IN") is None, (
        f"a normal spending month must stay quiet: {preview['warnings']}"
    )


def test_a_card_account_is_never_questioned_about_its_own_payments(world):
    """On a card, purchases are negative and payments in are entirely normal."""
    preview = _stage(world, _card_statement_shape(), account=world["card"]["id"])

    assert _said(preview, about="coming IN") is None, preview["warnings"]


def test_the_manifest_says_which_way_money_moves_per_account(world):
    """The gap was not the sign rule -- that was documented -- but what it meant."""
    manifest = world["client"].get(
        f"{V1}/manifest", headers={"authorization": f"Bearer {world['token']}"}
    ).json()

    by_name = {a["name"]: a for a in manifest["accounts"]}
    assert "salary" in by_name["Santander"]["sign_hint"]
    assert "payments you make TO the card positive" in by_name["Visa"]["sign_hint"]


# --------------------------------------------------------------------------- #
# #50 -- the agent's arithmetic, checked against what was staged
# --------------------------------------------------------------------------- #


def test_a_declaration_that_matches_says_nothing(world):
    rows = [_row("2026-07-01", -1_000, "A", "d-1"), _row("2026-07-02", -2_500, "B", "d-2")]

    preview = _stage(
        world,
        rows,
        statement={
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
            "row_count": 2,
            "total_minor": -3_500,
        },
    )

    assert preview["warnings"] == [], preview["warnings"]


def test_a_dropped_page_shows_up_as_a_row_count_mismatch(world):
    preview = _stage(
        world,
        [_row("2026-07-01", -1_000, "A", "e-1")],
        statement={"row_count": 40},
    )

    said = _said(preview, about="declared 40 rows")
    assert said is not None, preview["warnings"]
    assert "1 arrived" in said, said


def test_a_flipped_sign_shows_up_as_a_total_mismatch(world):
    """The failure the dedupe issue describes, caught from the other direction."""
    preview = _stage(
        world,
        [_row("2026-07-01", 1_000, "A", "f-1")],  # should have been negative
        statement={"total_minor": -1_000},
    )

    said = _said(preview, about="difference")
    assert said is not None, preview["warnings"]
    assert "2000" in said, f"the difference is the useful number: {said}"


def test_a_row_from_the_wrong_statement_shows_up_as_a_date(world):
    preview = _stage(
        world,
        [_row("2026-07-01", -1_000, "A", "g-1"), _row("2026-09-15", -1_000, "B", "g-2")],
        statement={"period_start": "2026-07-01", "period_end": "2026-07-31"},
    )

    said = _said(preview, about="period ending")
    assert said is not None, preview["warnings"]
    assert "2026-09-15" in said, said


def test_declaring_nothing_behaves_exactly_as_before(world):
    """Every part of this is optional, and that has to stay true."""
    with_none = _stage(world, [_row("2026-07-01", -1_000, "A", "h-1")], key="a")
    assert with_none["warnings"] == []
    assert with_none["decision"]["created"] == 1


# --------------------------------------------------------------------------- #
# #40 -- the single most expensive thing in the API
# --------------------------------------------------------------------------- #


def test_by_default_only_the_rows_that_need_attention_come_back(world):
    """A 193-row import returned 193 lines and was larger than the request."""
    rows = [_row(f"2026-07-{day:02d}", -1_000, f"Shop {day}", f"i-{day}") for day in range(1, 21)]
    _landed(world, rows[:1], key="seed")

    preview = _stage(world, rows, key="main")

    assert preview["decision"]["created"] == 19
    assert preview["decision"]["duplicates"] == 1
    assert len(preview["lines"]) == 1, (
        "nineteen rows landed cleanly and are fully described by the counts"
    )
    assert preview["lines"][0]["outcome"] == "duplicate_skipped"


def test_the_caller_is_not_sent_its_own_rows_back(world):
    """`raw` is the submitted row, re-serialised as a JSON string."""
    _landed(world, [_row("2026-07-01", -1_000, "A", "j-1")], key="seed")

    # Same external_id, so it is a duplicate and therefore a line the default
    # response returns; a different payee, so the re-upload guard lets it past.
    default = _stage(world, [_row("2026-07-01", -1_000, "A again", "j-1")], key="a")
    assert default["lines"][0]["outcome"] == "duplicate_skipped"
    assert default["lines"][0]["raw"] is None

    asked = _stage(
        world, [_row("2026-07-01", -1_000, "A once more", "j-1")],
        key="b", query="?include_raw=true",
    )
    assert asked["lines"][0]["raw"] is not None, "still available when it is actually wanted"
    assert "j-1" in asked["lines"][0]["raw"]


def test_every_line_is_still_available_for_debugging(world):
    rows = [_row(f"2026-07-{day:02d}", -1_000, f"Shop {day}", f"k-{day}") for day in range(1, 6)]

    preview = _stage(world, rows, query="?include_lines=all")

    assert len(preview["lines"]) == 5
    assert {line["outcome"] for line in preview["lines"]} == {"created"}


def test_lines_can_be_refused_altogether(world):
    rows = [_row(f"2026-07-{day:02d}", -1_000, f"Shop {day}", f"l-{day}") for day in range(1, 6)]

    preview = _stage(world, rows, query="?include_lines=none")

    assert preview["lines"] == []
    assert preview["decision"]["created"] == 5, "the answer is still there"


def test_an_invented_mode_is_refused_rather_than_ignored(world):
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports?include_lines=everything",
        json={
            "account_id": world["checking"]["id"],
            "source": "x",
            "rows": [_row("2026-07-01", -1_000, "A", "m-1")],
        },
        headers={"authorization": f"Bearer {world['token']}"},
    )

    assert answer.status_code == 422, answer.text


def test_the_decision_block_is_the_whole_answer(world):
    rows = [
        _row("2026-07-01", -1_000, "A", "n-1"),
        _row("2026-07-02", -2_500, "B", "n-2"),
    ]

    preview = _stage(world, rows)

    decision = preview["decision"]
    assert decision["created"] == 2
    assert decision["duplicates"] == 0
    assert decision["rejected"] == 0
    assert decision["uncategorised"] == 2, "two rows landing with no category is the backlog"
    # Per currency and never across them, like every other total in this API.
    assert decision["totals"] == {"EUR": "-35.00"}
