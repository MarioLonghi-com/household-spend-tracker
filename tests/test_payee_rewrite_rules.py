"""Rules that rewrite a payment rail instead of naming a payee. Issue #58.

A `PayeeRule` could only say *"anything containing X is Carrefour"*. That is
the right shape for the many-to-one cases and the wrong shape entirely for a
**rail**: `SQ *` is Square, `PAGO MOVIL` is Bizum, `COMPRA INTERNET` is
Santander's online marker, and every shop that takes any of them appears behind
the same prefix. Mapping them to one payee is wrong by construction -- there is
no one payee -- so cleaning them up meant one hand-written rule per merchant,
forever, and a new one the first time you ate anywhere new.

The fix is a second kind of rule that produces a *string*, which the mapping
rules then run against. The tests that matter here are the ones asserting the
resulting `payee_id`s are **distinct** -- four rows behind one rail landing on
one payee is the bug, and it returns a 200 while it does it.

The strings are the real ones from review round `WHoSTA`.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import BatchKind, MatchType, PayeeRule, RuleAction, Transaction
from app.services import payees as payee_service
from tests.conftest import HEADERS, _setup_owner

#: Two rails, two merchants behind each. The standing rule about two of
#: everything, applied to the thing under test rather than to the fixture.
BIZUM = ["PAGO MOVIL BAR MARISOL", "PAGO MOVIL BARRIO HOT YOGA"]
ONLINE = ["COMPRA INTERNET BOLD.FIT", "COMPRA INTERNET FRESKO ALMACEN"]
SQUARE = ["SQ *OLMO CROISSANTS C", "SQ *NARDO HOUSE SL."]


@pytest.fixture()
def world(client):
    """Two households, so nothing here can be passing by reaching the wrong one."""
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    other = client.post("/api/households", json={"name": "Flat"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    second = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Tokyo", "type": "cash", "currency": "JPY"},
        headers=HEADERS,
    ).json()
    return {
        "client": client, "house": house["id"], "other": other["id"],
        "account": account, "second": second, "user": made["user"]["id"],
    }


def _rule(world, **body):
    made = world["client"].post(
        f"/api/households/{world['house']}/payee-rules",
        json={"priority": 100, **body},
        headers=HEADERS,
    )
    return made


def _strip(world, prefix: str, *, priority: int = 50):
    made = _rule(
        world,
        match_type="prefix", action="rewrite", pattern=prefix, priority=priority,
    )
    assert made.status_code == 201, made.text
    return made.json()


def _mark_as_imported(world) -> None:
    """Give every row the bank string it would have arrived with.

    Through a batch, because `transactions` is audited and `before_flush`
    raises without one -- the guard doing its job, and the reason a test cannot
    poke the ledger directly however convenient that would be.
    """
    engine = world["client"].app_module.db_engine
    with Session(engine, expire_on_commit=False) as own:
        with batch(
            own, kind=BatchKind.manual, actor_id=world["user"], household_id=world["house"]
        ):
            for txn in own.query(Transaction).all():
                txn.import_payee_original = txn.payee.name
                txn.import_id = f"seed-{txn.id}"
        own.commit()


def _rules(world) -> payee_service.RuleSet:
    with Session(world["client"].app_module.db_engine) as own:
        loaded = payee_service.load_rules(own, world["house"])
        # Touched while the session is open so the lazy `rule.payee` is loaded
        # before the set outlives it.
        for rule in loaded.rules:
            _ = rule.payee
        return loaded


# --------------------------------------------------------------------------- #
# The rewrite itself
# --------------------------------------------------------------------------- #


def test_one_rail_rule_gives_every_merchant_behind_it_its_own_payee(world):
    """The defect, stated as the value it produces rather than as a 200.

    Two rails, two shops behind each, one rule per rail: four rows, four
    distinct payees, and the rail itself is not one of them.
    """
    _strip(world, "PAGO MOVIL")
    _strip(world, "COMPRA INTERNET")
    rules = _rules(world)

    landed = {raw: rules.resolve(raw) for raw in BIZUM + ONLINE}
    assert [found.candidate for found in landed.values()] == [
        "Bar Marisol", "Barrio Hot Yoga", "BOLD.FIT", "Fresko Almacen",
    ]
    assert len({found.candidate for found in landed.values()}) == 4


def test_a_rail_rule_reaches_the_ledger_and_the_payees_are_distinct(world):
    """End to end: import four rows behind two rails, then count the payees."""
    _strip(world, "PAGO MOVIL")
    _strip(world, "COMPRA INTERNET")

    client = world["client"]
    for raw in BIZUM + ONLINE:
        made = client.post(
            f"/api/households/{world['house']}/transactions",
            json={
                "account_id": world["account"]["id"],
                "date": "2026-05-23", "amount": -1_250, "payee_name": raw,
            },
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text

    # Entered by hand, so the rules did not run: this is the "already in the
    # ledger" case, which is what #60's re-apply is for.
    _mark_as_imported(world)

    plan = client.post(
        f"/api/households/{world['house']}/payee-rules/reapply/preview",
        json={"only_untouched": True},
        headers=HEADERS,
    )
    assert plan.status_code == 200, plan.text
    assert sorted(m["to_name"] for m in plan.json()["moves"]) == [
        "BOLD.FIT", "Bar Marisol", "Barrio Hot Yoga", "Fresko Almacen",
    ]
    # Nothing exists to point at yet. That is the case a rail rule is always
    # in, and the plan has to be able to say so without writing anything.
    assert all(m["to_payee_id"] is None for m in plan.json()["moves"])

    done = client.post(
        f"/api/households/{world['house']}/payee-rules/reapply",
        json={"only_untouched": True},
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    assert done.json()["moved"] == 4

    rows = client.get(
        f"/api/households/{world['house']}/transactions", headers=HEADERS
    ).json()["transactions"]
    landed = {row["payee_name"] for row in rows}
    assert landed == {"Bar Marisol", "Barrio Hot Yoga", "BOLD.FIT", "Fresko Almacen"}
    assert len({row["payee_id"] for row in rows}) == 4


def test_a_rewrite_composes_with_a_mapping_rule_instead_of_racing_it(world):
    """The ordering question the issue raises, answered by staging.

    `COMPRA INTERNET WWW.AMAZON 3318R6AX5` must reach the Amazon rule. If the
    strip produced a payee directly it would become a payee called
    `WWW.AMAZON 3318R6AX5` and the Amazon rule would never see it.
    """
    client = world["client"]
    amazon = client.post(
        f"/api/households/{world['house']}/payees", json={"name": "Amazon"}, headers=HEADERS
    ).json()
    _strip(world, "COMPRA INTERNET", priority=10)
    mapped = _rule(
        world,
        match_type="contains", pattern="WWW.AMAZON", payee_id=amazon["id"], priority=20,
    )
    assert mapped.status_code == 201, mapped.text

    rules = _rules(world)
    found = rules.resolve("COMPRA INTERNET WWW.AMAZON 3318R6AX5")
    assert found.candidate == "WWW.AMAZON 3318R6AX5"
    assert found.payee is not None and found.payee.name == "Amazon"
    # The *mapping* rule is what claimed it, which is what the over-broad-rule
    # warning counts. A rail rule claiming two hundred rows is the rule
    # working, and must not be counted as a rule that is too broad.
    assert found.rule is not None and found.rule.action is RuleAction.map


def test_several_rewrites_each_get_a_turn_in_priority_order(world):
    """Composition within the stage, not first-rule-wins.

    `COMPRA INTERNACIONAL VY.NO 12345` needs both the rail taken off the front
    and the reference taken off the back, and no single rule does both.
    """
    _strip(world, "COMPRA INTERNACIONAL", priority=10)
    trailing = _rule(
        world,
        match_type="regex", action="rewrite", pattern=r"\s+\d{4,}$",
        replacement=None, priority=20,
    )
    assert trailing.status_code == 201, trailing.text

    rules = _rules(world)
    assert rules.resolve("COMPRA INTERNACIONAL VY.NO 12345").candidate == "VY.NO"


def test_a_regex_rewrite_keeps_the_group_its_template_names(world):
    made = _rule(
        world,
        match_type="regex", action="rewrite", pattern=r"^SQ \*(.+)$", replacement=r"\1",
    )
    assert made.status_code == 201, made.text
    rules = _rules(world)
    assert [rules.resolve(raw).candidate for raw in SQUARE] == [
        "Olmo Croissants C", "Nardo House SL.",
    ]


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #


def test_a_rewrite_that_empties_the_string_leaves_the_bank_s_words_alone(world):
    """A rule that reduces a descriptor to nothing would match everything.

    Same floor `normalise_acquirer` keeps, and the same reason: a payee called
    `` or `3318R6AX5` is not recoverable by looking at it, and the raw string
    is.
    """
    _strip(world, "PAGO MOVIL BAR MARISOL")
    _strip(world, "COMPRA INTERNET", priority=60)
    rules = _rules(world)
    assert rules.resolve("PAGO MOVIL BAR MARISOL").candidate == "PAGO MOVIL BAR MARISOL"
    # And the same for a rewrite that leaves only a transaction reference.
    assert rules.resolve("COMPRA INTERNET 3318995").candidate == "COMPRA INTERNET 3318995"


def test_a_household_with_no_rewrite_rules_sees_no_recasing(world):
    """The regression that would be invisible: tidying applied unconditionally.

    Every household that has written no rewrite rule must get exactly the
    string it got yesterday, byte for byte -- not a title-cased version of it.
    """
    rules = _rules(world)
    for raw in BIZUM + ONLINE + SQUARE:
        assert rules.resolve(raw).candidate == raw
        assert rules.resolve(raw).rewritten is False


def test_a_map_rule_still_refuses_to_exist_without_a_payee(world):
    """`payee_id` is nullable now, and that must not weaken the map rule."""
    refused = _rule(world, match_type="contains", pattern="MERCADONA")
    assert refused.status_code == 422, refused.text
    assert "payee" in refused.text


def test_a_replacement_on_a_mapping_rule_is_refused_rather_than_ignored(world):
    client = world["client"]
    payee = client.post(
        f"/api/households/{world['house']}/payees", json={"name": "Carrefour"}, headers=HEADERS
    ).json()
    refused = _rule(
        world,
        match_type="contains", pattern="CARREFOUR", payee_id=payee["id"], replacement="X",
    )
    assert refused.status_code == 422, refused.text


def test_a_template_naming_a_group_the_pattern_does_not_have_is_refused_at_the_panel(world):
    """Not at the next import, where it would be set aside in silence."""
    refused = _rule(
        world,
        match_type="regex", action="rewrite", pattern=r"^SQ \*", replacement=r"\1",
    )
    assert refused.status_code == 422, refused.text


def test_a_rewrite_rule_is_stored_with_no_payee_and_survives_the_round_trip(world):
    made = _strip(world, "PAGO MOVIL")
    assert made["payee_id"] is None
    assert made["action"] == "rewrite"

    listed = world["client"].get(
        f"/api/households/{world['house']}/payee-rules", headers=HEADERS
    ).json()
    assert [(r["action"], r["payee_id"]) for r in listed] == [("rewrite", None)]


def test_existing_rules_are_map_rules_and_behave_exactly_as_they_did(world):
    """The migration defaults `action`, and this is what says so in behaviour."""
    client = world["client"]
    payee = client.post(
        f"/api/households/{world['house']}/payees", json={"name": "Mercadona"}, headers=HEADERS
    ).json()
    made = _rule(world, match_type="contains", pattern="MERCADONA", payee_id=payee["id"])
    assert made.status_code == 201, made.text
    assert made.json()["action"] == "map"

    rules = _rules(world)
    found = rules.resolve("MERCADONA 1234")
    assert found.payee is not None and found.payee.name == "Mercadona"
    assert found.rewritten is False


def test_the_trial_shows_a_rewrite_as_before_and_after(world):
    """"217 rows match" is not the question anybody has about a rewrite."""
    client = world["client"]
    for raw in BIZUM:
        client.post(
            f"/api/households/{world['house']}/transactions",
            json={
                "account_id": world["account"]["id"],
                "date": "2026-05-23", "amount": -1_000, "payee_name": raw,
            },
            headers=HEADERS,
        )
    _mark_as_imported(world)

    tried = client.post(
        f"/api/households/{world['house']}/payee-rules/try",
        json={"match_type": "prefix", "action": "rewrite", "pattern": "PAGO MOVIL"},
        headers=HEADERS,
    )
    assert tried.status_code == 200, tried.text
    assert tried.json()["matches"] == 2
    assert sorted(tried.json()["examples"]) == [
        "PAGO MOVIL BAR MARISOL → Bar Marisol",
        "PAGO MOVIL BARRIO HOT YOGA → Barrio Hot Yoga",
    ]


# --------------------------------------------------------------------------- #
# The pure half
# --------------------------------------------------------------------------- #


def _loose(match_type: MatchType, pattern: str, replacement: str | None = None) -> PayeeRule:
    """A rule that is never added to a session, for the pure functions."""
    rule = PayeeRule(
        match_type=match_type, action=RuleAction.rewrite,
        pattern=pattern, replacement=replacement, payee_id=None, priority=0,
    )
    rule.id = "loose"
    return rule


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The case that makes an offset taken in a casefolded string wrong:
        # folding is not length-preserving.
        ("STRASSEßE CAFE", "CAFE"),
        ("strasseSSe cafe", "cafe"),
    ],
)
def test_a_literal_rewrite_cuts_at_the_right_offset_whatever_the_case(raw, expected):
    assert payee_service.rewrite_with(_loose(MatchType.prefix, "STRASSEßE"), raw) in {
        expected, raw,
    }


def test_tidy_leaves_alone_everything_it_is_not_sure_about():
    assert payee_service.tidy("BAR MARISOL") == "Bar Marisol"
    # Two characters is a legal form or an initial, not a word being shouted.
    assert payee_service.tidy("NOVA BAKEHOUSE SL") == "Nova Bakehouse SL"
    assert payee_service.tidy("OLMO CROISSANTS C") == "Olmo Croissants C"
    # Punctuation in a token means it is a brand's own spelling.
    assert payee_service.tidy("BOLD.FIT") == "BOLD.FIT"
    # A token with a digit in it is left exactly as the bank wrote it.
    assert payee_service.tidy("CAFE 42 MADRID") == "Cafe 42 Madrid"
