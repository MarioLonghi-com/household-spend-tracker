"""Payee rules against card-acquirer descriptors.

The defect these are written from: `RuleSet.payee_for()` returns the first rule
that matches and `contains` is an unanchored substring test, so a rule whose
pattern is `SQ` claimed every Square descriptor in a file -- `SQ *GULL KITCHEN
LTD`, `SQ *CEDAR ROOMS` and `SQ *EVENTIM APOLLO` all became one payee. The rule
came from `seed_demo`, which derived its patterns as `raw.split()[0]`.

The statement it was found on is a real card export and is not in this
repository -- fixtures here carry nobody real, which `test_data_hygiene.py` is
what enforces. `REWARD_CARD` below is the same shapes with invented merchants:
three descriptors behind one acquirer prefix, one merchant written two ways,
an order reference, a booking reference, a bare prefix with nothing after it,
and a shop whose name happens to contain the two letters of the bad rule.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.audit.batch import batch
from app.models import (
    BatchKind,
    BatchStatus,
    ImportOutcome,
    MatchType,
    PayeeRule,
    Transaction,
)
from app.services import importing
from app.services import payees as payee_service
from app.services.payees import matches, normalise_acquirer
from statements import sniffing

# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #

#: ``(fitid, day, amount, descriptor)``. Every descriptor is invented; the
#: prefixes in front of them are not.
CARD_ROWS = [
    ("RB0001", 2, "-42.50", "SQ *GULL KITCHEN LTD"),
    ("RB0002", 3, "-18.00", "SQ *CEDAR ROOMS"),
    ("RB0003", 4, "-64.00", "SQ *EVENTIM APOLLO"),
    ("RB0004", 5, "-9.40", "SumUp *Riverside Heal"),
    ("RB0005", 6, "-11.20", "SumUp **Riverside Heal"),
    ("RB0006", 7, "-3.85", "Zettle_*Grey Heron Coffee"),
    ("RB0007", 8, "-4.15", "ZETTLE_*Grey Heron Coffee"),
    ("RB0008", 9, "-2.00", "Zettle_*"),
    ("RB0009", 10, "-31.99", "AMZNMktplace*PK0TG5D25"),
    ("RB0010", 11, "-240.00", "AIRBNB * HMAB4XYZ9"),
    ("RB0011", 12, "-7.30", "LSP*THE CORNER SHOP"),
    ("RB0012", 13, "-6.10", "SRT*THE OTHER SHOP"),
    ("RB0013", 14, "-22.45", "BASQUE KITCHEN LTD"),
    ("RB0014", 15, "-45.20", "MERCADONA 1234"),
]


def _ofx(rows: list[tuple[str, int, str, str]], currency: str = "EUR") -> bytes:
    """An OFX 2.x card statement. OFX names its own fields, so nothing is sniffed.

    In the currency of the account it is staged into: a statement that says
    it is in another one is refused (issue #260).
    """
    body = "".join(
        "<STMTTRN>"
        f"<TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>202510{day:02d}000000</DTPOSTED>"
        f"<TRNAMT>{amount}</TRNAMT><FITID>{fitid}</FITID><NAME>{name}</NAME>"
        "</STMTTRN>"
        for fitid, day, amount, name in rows
    )
    return (
        '<?xml version="1.0" standalone="no"?>'
        '<?OFX OFXHEADER="200" VERSION="202" SECURITY="NONE"?>'
        "<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>"
        f"<CURDEF>{currency}</CURDEF><CCACCTFROM><ACCTID>CARD|11223</ACCTID></CCACCTFROM>"
        f"<BANKTRANLIST>{body}</BANKTRANLIST>"
        "</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"
    ).encode()


REWARD_CARD = _ofx(CARD_ROWS)
#: The same statement from the same card's sterling twin.
REWARD_CARD_GBP = _ofx(CARD_ROWS, "GBP")

#: What each descriptor should end up as. The three Square rows are three
#: payees, and the two SumUp spellings are one.
EXPECTED = {
    "SQ *GULL KITCHEN LTD": "Gull Kitchen",
    "SQ *CEDAR ROOMS": "Cedar Rooms",
    "SQ *EVENTIM APOLLO": "Eventim Apollo",
    "SumUp *Riverside Heal": "Riverside Health",
    "SumUp **Riverside Heal": "Riverside Health",
    "Zettle_*Grey Heron Coffee": "Grey Heron Coffee",
    "ZETTLE_*Grey Heron Coffee": "Grey Heron Coffee",
    "Zettle_*": None,
    "AMZNMktplace*PK0TG5D25": "Amazon",
    "AIRBNB * HMAB4XYZ9": "Airbnb",
    "LSP*THE CORNER SHOP": "Corner Shop",
    "SRT*THE OTHER SHOP": "Other Shop",
    "BASQUE KITCHEN LTD": None,
    "MERCADONA 1234": "Mercadona",
}

#: ``(payee, match type, pattern)``. Two of them are written the way somebody
#: who copied a descriptor off a statement writes them.
RULES = [
    ("Gull Kitchen", MatchType.contains, "GULL KITCHEN"),
    ("Cedar Rooms", MatchType.contains, "CEDAR ROOMS"),
    ("Eventim Apollo", MatchType.contains, "EVENTIM"),
    ("Riverside Health", MatchType.contains, "Riverside Heal"),
    # Copied off the statement, acquirer prefix and all, before any of this.
    ("Grey Heron Coffee", MatchType.contains, "Zettle_*Grey Heron"),
    ("Amazon", MatchType.contains, "AMZN"),
    # Anchored, and anchored at the acquirer.
    ("Airbnb", MatchType.prefix, "AIRBNB *"),
    ("Corner Shop", MatchType.contains, "THE CORNER SHOP"),
    ("Other Shop", MatchType.contains, "THE OTHER SHOP"),
    ("Mercadona", MatchType.regex, r"^MERCADONA\b"),
]


@pytest.fixture()
def ruled(session, owner, household):
    """The household's rules, as somebody would have built them up over time."""

    def _make(rules=RULES) -> dict[str, str]:
        made: dict[str, str] = {}
        with batch(
            session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
        ):
            for name, kind, pattern in rules:
                payee = payee_service.get_or_create(session, household.id, name)
                payee_service.create_rule(
                    session,
                    household_id=household.id,
                    match_type=kind,
                    pattern=pattern,
                    payee=payee,
                )
                made[name] = payee.id
        return made

    return _make


def _stage(session, owner, household, account, raw: bytes, *, filename="card.ofx"):
    """Phase one, the way the route does it."""
    sniffed = sniffing.sniff(raw)
    with batch(
        session,
        kind=BatchKind.imported,
        actor_id=owner.id,
        household_id=household.id,
        source={
            "filename": filename,
            "sha256": importing.file_digest(raw),
            "bytes": len(raw),
            "account_id": account.id,
            "format": sniffed.format.describe(),
        },
    ) as staged:
        lines = importing.stage_file(
            session, account=account, raw=raw, fmt=sniffed.format, batch_row=staged
        )
        staged.status = BatchStatus.preview
    return staged, lines


def _resolved(lines) -> dict[str, str | None]:
    """Descriptor -> the payee name the rules made of it."""
    return {
        (line.parsed or {}).get("payee"): (line.parsed or {}).get("payee_resolved")
        for line in lines
        if line.parsed
    }


# --------------------------------------------------------------------------- #
# The function, on its own
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        ("SQ *GULL KITCHEN LTD", "GULL KITCHEN LTD"),
        ("SQ *CEDAR ROOMS", "CEDAR ROOMS"),
        ("SumUp *Riverside Heal", "Riverside Heal"),
        ("SumUp **Riverside Heal", "Riverside Heal"),
        ("Zettle_*Grey Heron Coffee", "Grey Heron Coffee"),
        ("ZETTLE_*Grey Heron Coffee", "Grey Heron Coffee"),
        ("LSP*THE CORNER SHOP", "THE CORNER SHOP"),
        # The acquirer is the merchant here, and the tail is a reference.
        ("AMZNMktplace*PK0TG5D25", "AMZNMktplace"),
        ("AIRBNB * HMAB4XYZ9", "AIRBNB"),
        # Nothing to strip, and nothing is done to it.
        ("MERCADONA 1234", "MERCADONA 1234"),
        ("BASQUE KITCHEN LTD", "BASQUE KITCHEN LTD"),
        # Nothing usable left: the raw string, rather than an empty one that
        # would then match every rule in the household.
        ("Zettle_*", "Zettle_*"),
        ("SQ *", "SQ *"),
        ("SQ *A1", "SQ *A1"),
        ("SQ *12345678", "SQ *12345678"),
        # A space is not a separator, so an ordinary shop is left alone.
        ("SQ FOOT LTD", "SQ FOOT LTD"),
        ("", ""),
    ],
)
def test_the_acquirer_is_taken_off_only_where_there_is_one(descriptor, expected):
    assert normalise_acquirer(descriptor) == expected


def test_the_stored_descriptor_is_never_rewritten(session, owner, household, accounts, ruled):
    """`import_lines.raw` and `import_payee_original` show what the bank sent.

    The normalisation is something comparisons happen *through*. If it ever
    reached a column, the line-detail panel would start showing this app's
    opinion of a descriptor instead of the descriptor.
    """
    ruled()
    staged, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    square = next(line for line in lines if "GULL KITCHEN" in line.raw)
    assert "SQ *GULL KITCHEN LTD" in square.raw
    assert square.parsed["payee"] == "SQ *GULL KITCHEN LTD", "the parsed descriptor is untouched"
    assert square.parsed["payee_resolved"] == "Gull Kitchen"

    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id):
        importing.commit(session, batch_row=staged, account=accounts["card"])
        staged.status = BatchStatus.applied

    txn = session.execute(
        select(Transaction).where(Transaction.import_payee_original == "SQ *GULL KITCHEN LTD")
    ).scalar_one()
    assert txn.import_payee_original == "SQ *GULL KITCHEN LTD"


# --------------------------------------------------------------------------- #
# The file, through the rules
# --------------------------------------------------------------------------- #


def test_each_acquirer_prefix_resolves_to_its_own_payee(
    session, owner, household, accounts, ruled
):
    """The headline: three Square descriptors are three payees, not one."""
    ruled()
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    assert _resolved(lines) == EXPECTED

    square = {
        name: payee
        for name, payee in _resolved(lines).items()
        if name.upper().startswith("SQ *")
    }
    assert sorted(square.values()) == ["Cedar Rooms", "Eventim Apollo", "Gull Kitchen"]


def test_the_same_rules_resolve_the_same_way_in_the_other_account(
    session, owner, household, accounts, ruled
):
    """Rules are the household's, not an account's -- and the two accounts here
    are in two currencies, which is where a per-account guess would show."""
    ruled()
    _, euros = _stage(session, owner, household, accounts["checking"], REWARD_CARD)
    _, pounds = _stage(session, owner, household, accounts["pounds"], REWARD_CARD_GBP)

    assert _resolved(euros) == EXPECTED
    assert _resolved(pounds) == EXPECTED
    # Same descriptors, two currencies, and the money is still read per account.
    by_raw = {line.parsed["payee"]: line.parsed["amount"] for line in euros if line.parsed}
    assert by_raw["SQ *GULL KITCHEN LTD"] == -4_250


def test_the_two_sumup_spellings_are_one_payee(session, owner, household, accounts, ruled):
    """`SumUp *` and `SumUp **` are the same shop on two days."""
    made = ruled()
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    riverside = [
        line for line in lines if "Riverside" in (line.parsed or {}).get("payee", "")
    ]
    assert len(riverside) == 2, "both spellings are in the file"
    assert {line.parsed["payee_resolved"] for line in riverside} == {"Riverside Health"}
    assert {line.parsed["payee_id"] for line in riverside} == {made["Riverside Health"]}


def test_an_order_reference_still_resolves_to_amazon(session, owner, household, accounts, ruled):
    """The other direction: the acquirer is the merchant and the tail is an id."""
    ruled()
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    amazon = next(line for line in lines if "AMZN" in line.parsed.get("payee", ""))
    assert amazon.parsed["payee"] == "AMZNMktplace*PK0TG5D25"
    assert amazon.parsed["payee_resolved"] == "Amazon"


def test_a_bare_acquirer_prefix_falls_back_to_the_raw_string(
    session, owner, household, accounts, ruled
):
    """`Zettle_*` with nothing after it: no payee, no crash, and not everybody's.

    An empty normalisation would be a substring of every descriptor in the
    file, so a rule compared against it would claim the whole statement.
    """
    ruled()
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    bare = next(line for line in lines if line.parsed.get("payee") == "Zettle_*")
    assert bare.outcome is ImportOutcome.created
    assert "payee_resolved" not in bare.parsed, "it belongs to nobody"
    assert bare.parsed["payee"] == "Zettle_*"
    # And the Zettle rule that *does* exist went to the rows it was written for.
    assert _resolved(lines)["Zettle_*Grey Heron Coffee"] == "Grey Heron Coffee"


def test_a_rule_written_against_the_raw_descriptor_still_matches(
    session, owner, household, accounts, ruled
):
    """`Zettle_*Grey Heron` was typed before any of this existed.

    Both sides are normalised, so it is `Grey Heron` against `Grey Heron
    Coffee` now -- and it still claims the two rows it always claimed,
    including the one spelled `ZETTLE_`.
    """
    made = ruled()
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    grey_heron = [
        line for line in lines if "Grey Heron" in (line.parsed or {}).get("payee", "")
    ]
    assert len(grey_heron) == 2
    assert {line.parsed["payee_id"] for line in grey_heron} == {made["Grey Heron Coffee"]}


def test_a_prefix_rule_survives_the_stripping(session, owner, household, accounts, ruled):
    """`AIRBNB *` is anchored at the acquirer, which is also the merchant.

    Normalising the text leaves `AIRBNB` and normalising the pattern leaves it
    alone -- nothing follows its separator -- so the normalised pair misses. A
    pattern that starts with an acquirer prefix is compared raw as well, which
    is what keeps the rule pointed at the only rows it was ever for.
    """
    made = ruled()
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    booking = next(line for line in lines if line.parsed.get("payee", "").startswith("AIRBNB"))
    assert booking.parsed["payee_resolved"] == "Airbnb"
    assert booking.parsed["payee_id"] == made["Airbnb"]


def test_a_regex_rule_is_unaffected(session, owner, household, accounts, ruled):
    """A regex pattern is never rewritten; the text is tried both ways."""
    ruled()
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)
    assert _resolved(lines)["MERCADONA 1234"] == "Mercadona"

    def rule(pattern: str) -> PayeeRule:
        return PayeeRule(
            id=f"rx-{pattern}",
            household_id=household.id,
            match_type=MatchType.regex,
            pattern=pattern,
            payee_id="payee",
            priority=100,
        )

    # Anchored at the acquirer: still matches the raw descriptor, because the
    # raw descriptor is tried first.
    assert matches(rule(r"^SQ \*"), "SQ *GULL KITCHEN LTD") is True
    # Anchored at the merchant: matches through the stripped spelling, which is
    # what a regex written against a cleaned-up name would otherwise miss.
    assert matches(rule(r"^GULL KITCHEN"), "SQ *GULL KITCHEN LTD") is True
    assert matches(rule(r"^GULL KITCHEN"), "SQ *CEDAR ROOMS") is False


def test_a_known_payee_is_found_through_the_acquirer(session, owner, household, accounts):
    """No rule at all: the household already has the payee, typed by hand.

    The fallback in `payee_for` folds the descriptor; through the acquirer it
    folds what is left of it, which is the name somebody actually typed.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        typed = payee_service.get_or_create(session, household.id, "Cedar Rooms")

    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)
    circa = next(line for line in lines if line.parsed.get("payee") == "SQ *CEDAR ROOMS")
    assert circa.parsed["payee_id"] == typed.id
    assert circa.parsed["payee_resolved"] == "Cedar Rooms"

    # And a descriptor with no acquirer and no match is still nobody's.
    basque = next(line for line in lines if line.parsed.get("payee") == "BASQUE KITCHEN LTD")
    assert "payee_id" not in basque.parsed


# --------------------------------------------------------------------------- #
# The defect itself
# --------------------------------------------------------------------------- #


def test_the_bad_seeded_rule_no_longer_swallows_the_file(
    session, owner, household, accounts, ruled
):
    """The reproduction, in the shape `seed_demo` used to produce it.

    A `contains` rule of `SQ` -- the acquirer prefix, taken as a merchant name
    by `raw.split()[0]` -- used to claim every Square row in the file. It now
    claims none of them, because the descriptor it is compared against no
    longer carries the acquirer either.

    What it *does* still claim is `BASQUE KITCHEN LTD`, and that is the honest
    result: two letters as an unanchored substring is a landmine whatever is
    done about acquirers, which is why the preview warns about it below.
    """
    ruled([("El Bar", MatchType.contains, "SQ")])
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    claimed = [
        line.parsed["payee"]
        for line in lines
        if (line.parsed or {}).get("payee_resolved") == "El Bar"
    ]
    assert claimed == ["BASQUE KITCHEN LTD"], (
        "the acquirer prefix no longer collapses three merchants into one payee"
    )
    assert all(
        (line.parsed or {}).get("payee_resolved") is None
        for line in lines
        if (line.parsed or {}).get("payee", "").startswith("SQ *")
    )


def test_a_short_rule_says_so_on_the_rows_it_claimed(
    session, owner, household, accounts, ruled
):
    """The second layer: a warning, on the rows, without blocking anything."""
    ruled([("El Bar", MatchType.contains, "SQ")])
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)

    warned = [line for line in lines if line.reason and "rule claimed" in line.reason]
    assert len(warned) == 1
    assert warned[0].parsed["payee"] == "BASQUE KITCHEN LTD"
    assert 'the "SQ" contains rule claimed 1 of 14 rows' in warned[0].reason
    assert "Rules screen" in warned[0].reason
    # Warned, not blocked: the line still imports.
    assert warned[0].outcome is ImportOutcome.created


def test_a_sound_rule_is_not_warned_about(session, owner, household, accounts, ruled):
    """Every rule in `RULES` is four characters or more and claims a handful of
    rows, so nothing in a normal file says anything."""
    ruled()
    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)
    assert [line.reason for line in lines if line.reason] == []


def test_a_rule_that_claims_most_of_a_file_says_so_too(
    session, owner, household, accounts, ruled
):
    """The generic half of the guard rail: a long pattern, and far too many rows.

    `MERCADONA` is nine characters, so the short-pattern check has nothing to
    say about it. Twelve of sixteen rows does.
    """
    ruled([("Mercadona", MatchType.contains, "MERCADONA")])

    rows = [
        (f"MG{index:04d}", 1 + index, "-9.99", "MERCADONA 1234") for index in range(12)
    ] + [
        ("MG1001", 20, "-5.00", "BASQUE KITCHEN LTD"),
        ("MG1002", 21, "-6.00", "Cedar Rooms"),
        ("MG1003", 22, "-7.00", "Corner Shop"),
        ("MG1004", 23, "-8.00", "Other Shop"),
    ]
    _, lines = _stage(session, owner, household, accounts["card"], _ofx(rows))

    warned = [line for line in lines if line.reason and "rule claimed" in line.reason]
    assert len(warned) == 12, "every row the rule claimed carries the warning"
    assert 'the "MERCADONA" contains rule claimed 12 of 16 rows' in warned[0].reason
    assert "that is most of the file" in warned[0].reason
    assert all(line.parsed["payee_resolved"] == "Mercadona" for line in warned)


def test_a_verdict_is_never_written_over_by_a_warning(
    session, owner, household, accounts, ruled, jan
):
    """`reason` already says why a line was skipped or matched, and that cannot
    be worked out again. The warning only fills the column where it is empty."""
    from app.services import transactions as txn_service

    ruled([("El Bar", MatchType.contains, "SQ")])
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn_service.create(
            session,
            account=accounts["card"],
            date=date(2025, 10, 14),
            amount=-2_245,
            memo="the one I typed",
        )

    _, lines = _stage(session, owner, household, accounts["card"], REWARD_CARD)
    basque = next(line for line in lines if line.parsed.get("payee") == "BASQUE KITCHEN LTD")
    assert basque.outcome is ImportOutcome.matched_existing
    assert "looks like the" in basque.reason, "the verdict survived"
    assert "rule claimed" not in basque.reason
