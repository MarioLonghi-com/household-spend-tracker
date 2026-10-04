"""Rules that reach what is already here, and a screen that says they exist.

Three issues, and they only pay off together. #61 is that the rule panel never
said what a pattern is matched against -- the bank's original string, not the
payee in the register -- so rules were written blind. #59 is that nothing
surfaced the groups that are one rule away, so four payees became twenty-eight
and the only way to notice was to scroll. #60 is that a rule, once written,
touched nothing already in the ledger: rules ran in exactly one place, as a
statement was read, and the answer was frozen onto the line.

The fixture uses the real strings from the review, because the shapes are what
the clustering has to cope with: a per-transaction reference welded onto a
name, one separated by a space, and a rail prefix that genuinely is several
different shops.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import BatchKind, Payee, Transaction
from tests.conftest import HEADERS, _setup_owner

#: Straight from `WHoSTA`. Each tuple is (what the bank sent, how many rows).
AMAZON = [
    "WWW.AMAZON_ BX1EG1ST5",
    "WWW.AMAZON_ IJ6YX2P45",
    "WWW.AMAZON_ O369Z9454",
    "WWW.AMAZON_ OS0OX6W54",
]
HOTELS = ["HOTELCOM61963202362647", "HOTELCOM61964948503067"]
#: A rail prefix: genuinely different shops behind one marker. Nothing here
#: should offer to collapse these into one payee -- that is #58's problem and
#: it is deferred, so the important thing is that this does not pretend.
SQUARE = ["SQ *OLMO CROISSANTS C", "SQ *NARDO HOUSE SL.", "SQ *PLAZA NUEVA REST"]


@pytest.fixture()
def world(client):
    """Two accounts, two currencies, and a ledger full of bank noise."""
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
    return {
        "client": client, "house": house["id"], "account": account, "other": other,
        "user": made["user"]["id"],
    }


def _as_batch(world, work) -> None:
    """Run `work(session)` inside a batch, as any real write here does.

    `transactions` is audited and `before_flush` raises without one -- which is
    the guard doing its job, and the reason a test cannot poke the ledger
    directly however convenient that would be.
    """
    with Session(world["client"].app_module.db_engine, expire_on_commit=False) as own:
        with batch(
            own, kind=BatchKind.manual, actor_id=world["user"], household_id=world["house"]
        ):
            work(own)
        own.commit()


def _imported(world, raw: str, *, when="2026-05-23", amount=-2_000, account=None) -> str:
    """A transaction that arrived off a statement, carrying the bank's words.

    Set through the ORM because the point is the *state* -- a row whose payee
    is the raw string, which is what `stage` produces when no rule matches --
    and not how it got there.
    """
    client = world["client"]
    made = client.post(
        f"/api/households/{world['house']}/transactions",
        json={
            "account_id": account or world["account"]["id"],
            "date": when, "amount": amount, "payee_name": raw,
        },
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    txn_id = made.json()["id"]

    def mark(own):
        row = own.get(Transaction, txn_id)
        row.import_payee_original = raw
        row.import_id = f"seed-{txn_id}"

    _as_batch(world, mark)
    return txn_id


def _rule(world, *, pattern: str, payee: str, match_type="contains"):
    client = world["client"]
    made_payee = client.post(
        f"/api/households/{world['house']}/payees", json={"name": payee}, headers=HEADERS
    )
    assert made_payee.status_code == 201, made_payee.text
    made = client.post(
        f"/api/households/{world['house']}/payee-rules",
        json={
            "match_type": match_type, "pattern": pattern,
            "payee_id": made_payee.json()["id"], "priority": 100,
        },
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    return made_payee.json()["id"]


def _payees(world) -> dict[str, dict]:
    rows = world["client"].get(
        f"/api/households/{world['house']}/payees", headers=HEADERS
    ).json()
    return {one["name"]: one for one in rows}


# --------------------------------------------------------------------------- #
# #59 -- the app knew, and never said
# --------------------------------------------------------------------------- #


def test_a_payee_carries_how_much_points_at_it(world):
    """The cheap half. Sorting by this makes the noise obvious at a glance."""
    for raw in AMAZON:
        _imported(world, raw)
    _imported(world, "Mercadona")
    _imported(world, "Mercadona", when="2026-05-24")

    payees = _payees(world)
    assert payees["Mercadona"]["transaction_count"] == 2
    for raw in AMAZON:
        assert payees[raw]["transaction_count"] == 1, (
            "one transaction and a name ending in nine characters of reference"
        )


def test_the_groups_one_rule_apart_are_surfaced(world):
    """Four payees where there is one shop, and the app can see it."""
    for raw in AMAZON:
        _imported(world, raw)
    for raw in HOTELS:
        _imported(world, raw, when="2026-06-01")
    _imported(world, "Mercadona", when="2026-06-02")

    found = world["client"].get(
        f"/api/households/{world['house']}/payee-suggestions", headers=HEADERS
    )
    assert found.status_code == 200, found.text
    suggestions = {one["pattern"]: one for one in found.json()}

    amazon = next(s for p, s in suggestions.items() if "AMAZON" in p)
    assert amazon["strings"] == 4, "it gathered all four spellings"
    assert amazon["payees"] == 4, "which are four payees today -- that is the case for the rule"
    assert amazon["transactions"] == 4
    assert set(amazon["examples"]) == set(AMAZON)

    hotels = next(s for p, s in suggestions.items() if "HOTELCOM" in p)
    assert hotels["strings"] == 2, "a reference welded on with no space is still a reference"

    assert not any("Mercadona" in p for p in suggestions), (
        "one string is not a group, and a shop that is already one payee needs no rule"
    )


def test_a_group_already_resolving_to_one_payee_is_not_suggested(world):
    """The control. A rule that changes nothing is noise on the screen."""
    payee_id = _rule(world, pattern="WWW.AMAZON", payee="Amazon")
    for raw in AMAZON:
        txn = _imported(world, raw)
        _as_batch(world, lambda own, t=txn: setattr(own.get(Transaction, t), "payee_id", payee_id))

    found = world["client"].get(
        f"/api/households/{world['house']}/payee-suggestions", headers=HEADERS
    ).json()
    assert not any("AMAZON" in one["pattern"] for one in found), (
        "they already all resolve to Amazon; there is nothing to offer"
    )


# --------------------------------------------------------------------------- #
# #61 -- what would this rule actually do
# --------------------------------------------------------------------------- #


def test_a_pattern_can_be_tried_before_it_is_written(world):
    """No preview at all before this: the result appeared at the next import."""
    for raw in AMAZON:
        _imported(world, raw)
    for raw in SQUARE:
        _imported(world, raw, when="2026-06-01")

    tried = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/try",
        json={"match_type": "contains", "pattern": "WWW.AMAZON"},
        headers=HEADERS,
    )
    assert tried.status_code == 200, tried.text

    answer = tried.json()
    assert answer["matches"] == 4, "the four Amazon rows and nothing else"
    assert answer["considered"] == 7
    assert set(answer["examples"]) == set(AMAZON)
    assert answer["timed_out"] is False


def test_a_pattern_is_matched_against_the_banks_words_not_the_payee(world):
    """The thing the panel never said, asserted where it is decided.

    Somebody writing a rule for Amazon sees `Amazon` in the register and the
    bank sent `WWW.AMAZON_ BX1EG1ST5`. A rule written against what is on screen
    matches nothing, and nothing told them why.
    """
    payee_id = _rule(world, pattern="WWW.AMAZON", payee="Amazon")
    txn = _imported(world, AMAZON[0])
    _as_batch(world, lambda own: setattr(own.get(Transaction, txn), "payee_id", payee_id))

    against_the_payee = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/try",
        json={"match_type": "equals", "pattern": "Amazon"},
        headers=HEADERS,
    ).json()
    assert against_the_payee["matches"] == 0, (
        "the register says Amazon; the bank did not, and the bank is what is matched"
    )

    against_the_bank = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/try",
        json={"match_type": "contains", "pattern": "WWW.AMAZON"},
        headers=HEADERS,
    ).json()
    assert against_the_bank["matches"] == 1


def test_matching_is_case_insensitive_and_the_trial_shows_it(world):
    """Nothing said so, so people typed the bank's shouting capitals to be safe."""
    for raw in AMAZON:
        _imported(world, raw)

    lower = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/try",
        json={"match_type": "contains", "pattern": "www.amazon"},
        headers=HEADERS,
    ).json()
    assert lower["matches"] == 4


def test_a_regex_that_runs_away_says_so_on_the_screen_that_wrote_it(world):
    """Rather than by an import quietly setting the rule aside mid-file.

    `(a|a)*$` is six characters and took 23.8 seconds against a 26-character
    string. The deadline already existed; what did not exist was any way to
    find out you had written one.
    """
    # The trailing B is the point: all A's matches at once and proves nothing.
    # Catastrophic backtracking needs a subject the pattern ALMOST matches.
    _imported(world, "A" * 30 + "B")

    tried = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/try",
        json={"match_type": "regex", "pattern": "(a|a|aa)+$"},
        headers=HEADERS,
    )
    assert tried.status_code == 200, tried.text
    assert tried.json()["timed_out"] is True


def test_an_invalid_regex_is_refused_with_the_reason(world):
    tried = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/try",
        json={"match_type": "regex", "pattern": "([unclosed"},
        headers=HEADERS,
    )
    assert tried.status_code == 422, tried.text
    assert "regular expression" in tried.json()["detail"]


# --------------------------------------------------------------------------- #
# #60 -- a new rule that fixes the rows that made you write it
# --------------------------------------------------------------------------- #


def test_a_new_rule_can_be_applied_to_what_is_already_here(world):
    """The whole point. Without this, #58, #59 and #61 each deliver half.

    A person who does the work of writing the right rules still looks at the
    same register afterwards, and nothing on any screen tells them why.
    """
    for raw in AMAZON:
        _imported(world, raw)
    _imported(world, "Mercadona", when="2026-06-01")
    amazon_id = _rule(world, pattern="WWW.AMAZON", payee="Amazon")

    plan = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply/preview",
        json={}, headers=HEADERS,
    )
    assert plan.status_code == 200, plan.text
    proposed = plan.json()

    assert proposed["considered"] == 5, "every row carrying a bank string"
    assert proposed["changing"] == 4, "and only the four the rule claims"
    assert {m["to_name"] for m in proposed["moves"]} == {"Amazon"}
    assert set(AMAZON) == {m["raw"] for m in proposed["moves"]}
    assert sorted(proposed["orphaned"]) == sorted(AMAZON), (
        "the four noise payees would have nothing left pointing at them"
    )

    # Nothing has been written yet.
    with Session(world["client"].app_module.db_engine) as own:
        assert own.execute(
            select(Transaction).where(Transaction.payee_id == amazon_id)
        ).first() is None

    did = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply?delete_orphans=true",
        json={}, headers=HEADERS,
    )
    assert did.status_code == 200, did.text
    assert did.json()["moved"] == 4
    assert sorted(did.json()["deleted_payees"]) == sorted(AMAZON)

    with Session(world["client"].app_module.db_engine) as own:
        landed = own.execute(
            select(Transaction).where(Transaction.payee_id == amazon_id)
        ).scalars().all()
        assert len(landed) == 4, "the rows already in the ledger moved"
        left = {p.name for p in own.execute(select(Payee)).scalars()}
    assert left.isdisjoint(AMAZON), "and the payee list is not still 300 long"
    assert "Mercadona" in left, "a payee no rule touched is untouched"


def test_a_payee_somebody_corrected_by_hand_is_left_alone(world):
    """The important half of the scope, and the one that would be silent.

    Re-running rules over a row whose payee somebody fixed would undo their
    work, in bulk, with no way to see it happen.
    """
    txn = _imported(world, AMAZON[0])
    _rule(world, pattern="WWW.AMAZON", payee="Amazon")

    # Somebody has been in and said what this actually was.
    mine = world["client"].post(
        f"/api/households/{world['house']}/payees",
        json={"name": "A birthday present"}, headers=HEADERS,
    ).json()
    _as_batch(world, lambda own: setattr(own.get(Transaction, txn), "payee_id", mine["id"]))

    plan = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply/preview",
        json={}, headers=HEADERS,
    ).json()
    assert plan["changing"] == 0, "their correction is not the rules' to overrule"

    # And it CAN be overruled, deliberately, by asking.
    everything = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply/preview",
        json={"only_untouched": False}, headers=HEADERS,
    ).json()
    assert everything["changing"] == 1
    assert everything["moves"][0]["from_name"] == "A birthday present"


def test_a_row_entered_by_hand_is_out_of_scope_entirely(world):
    """No bank string, nothing to match a rule against."""
    made = world["client"].post(
        f"/api/households/{world['house']}/transactions",
        json={"account_id": world["account"]["id"], "date": "2026-05-23",
              "amount": -500, "payee_name": "WWW.AMAZON_ typed by hand"},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    _rule(world, pattern="WWW.AMAZON", payee="Amazon")

    plan = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply/preview",
        json={}, headers=HEADERS,
    ).json()
    assert plan["considered"] == 0
    assert plan["changing"] == 0


def test_a_reapply_can_be_narrowed_to_one_account(world):
    for raw in AMAZON[:2]:
        _imported(world, raw)
    for raw in AMAZON[2:]:
        _imported(world, raw, account=world["other"]["id"])
    _rule(world, pattern="WWW.AMAZON", payee="Amazon")

    narrowed = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply/preview",
        json={"account_id": world["account"]["id"]}, headers=HEADERS,
    ).json()
    assert narrowed["changing"] == 2, "the other account's rows are not in scope"

    whole = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply/preview",
        json={}, headers=HEADERS,
    ).json()
    assert whole["changing"] == 4


def test_a_reapply_is_one_act_and_undo_puts_all_of_it_back(world):
    """The entire reason the audit log exists, at the operation that needs it."""
    for raw in AMAZON:
        _imported(world, raw)
    _rule(world, pattern="WWW.AMAZON", payee="Amazon")

    did = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply",
        json={}, headers=HEADERS,
    ).json()
    assert did["moved"] == 4

    with Session(world["client"].app_module.db_engine) as own:
        before = {
            row.id: row.payee_id
            for row in own.execute(select(Transaction)).scalars()
        }

    undone = world["client"].post(
        f"/api/households/{world['house']}/batches/{did['batch_id']}/undo",
        json={}, headers=HEADERS,
    )
    assert undone.status_code in (200, 201), undone.text

    with Session(world["client"].app_module.db_engine) as own:
        after = {
            row.id: row.payee_id
            for row in own.execute(select(Transaction)).scalars()
        }
        names = {p.id: p.name for p in own.execute(select(Payee)).scalars()}

    assert after != before, "the undo changed something"
    assert all(names.get(payee_id) in AMAZON for payee_id in after.values()), (
        "every row is back on the payee the bank's words made"
    )


def test_nothing_to_do_is_not_an_error(world):
    """A re-apply with no rules written is a legitimate thing to ask for."""
    _imported(world, AMAZON[0])

    did = world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply",
        json={}, headers=HEADERS,
    )
    assert did.status_code == 200, did.text
    assert did.json()["moved"] == 0


def test_another_households_rows_are_never_touched(world, client):
    """Two households, per the standing rule, and the assertion that matters."""
    for raw in AMAZON:
        _imported(world, raw)
    _rule(world, pattern="WWW.AMAZON", payee="Amazon")

    elsewhere = client.post(
        "/api/households", json={"name": "Theirs"}, headers=HEADERS
    ).json()
    their_account = client.post(
        f"/api/households/{elsewhere['id']}/accounts",
        json={"name": "Theirs", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    theirs = client.post(
        f"/api/households/{elsewhere['id']}/transactions",
        json={"account_id": their_account["id"], "date": "2026-05-23",
              "amount": -2_000, "payee_name": AMAZON[0]},
        headers=HEADERS,
    ).json()["id"]
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        with batch(
            own, kind=BatchKind.manual, actor_id=world["user"], household_id=elsewhere["id"]
        ):
            own.get(Transaction, theirs).import_payee_original = AMAZON[0]
        own.commit()

    with Session(client.app_module.db_engine) as own:
        was = own.get(Transaction, theirs).payee_id

    world["client"].post(
        f"/api/households/{world['house']}/payee-rules/reapply",
        json={}, headers=HEADERS,
    )

    with Session(client.app_module.db_engine) as own:
        assert own.get(Transaction, theirs).payee_id == was, (
            "one household's rules reached another household's rows"
        )
