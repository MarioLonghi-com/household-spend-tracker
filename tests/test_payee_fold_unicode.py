"""Accents, dash style and invisible spaces are not part of a payee's name (#268).

Spanish banks drop accents inconsistently between the app, the CSV and the
PDF, a sort code comes with hyphens from one export and en dashes from
another, and a CSV's first cell can carry a byte-order mark. Each of those made
one merchant two payees, and a rule written for one spelling missed the other.

Every name here is invented. Two households throughout: the fold is global, but
the payees it resolves to and the merge preview are each household's own.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import BatchKind, MatchType, Payee, PayeeRule, Role, RuleAction
from app.services import payees as payee_service
from app.services.one_time_import import engine as one_time_engine
from app.services.payees import fold, matches, rewrite_with
from statements import signs
from tests.conftest import HEADERS, _bootstrap_user, _setup_owner

SORT_CODE = "TO A/C 12-34-56"


# --------------------------------------------------------------------------- #
# The fold itself
# --------------------------------------------------------------------------- #


def test_an_accent_is_not_part_of_a_name():
    assert fold("Café Sol") == fold("CAFE SOL") == "cafe sol"
    # Decomposed as well as precomposed: e + U+0301 is the same é.
    assert fold("Cafe\u0301 Sol") == "cafe sol"


@pytest.mark.parametrize("dash", list(payee_service.UNICODE_DASHES))
def test_every_unicode_dash_reads_as_a_hyphen(dash):
    assert fold(SORT_CODE.replace("-", dash)) == fold(SORT_CODE) == "to a/c 12-34-56"


def test_every_minus_the_statement_reader_knows_is_a_dash_here_too():
    """A character the statement reader takes for a minus in an amount is a
    dash in a payee's name as well. `statements/` never imports `app/`, so
    each keeps its own table, and this is what holds them together. A subset,
    not equal: U+2010 HYPHEN is a dash but never a minus."""
    assert set(signs.MINUS_SIGNS) - set(payee_service.UNICODE_DASHES) == set()
    assert {fold(SORT_CODE.replace("-", minus)) for minus in signs.MINUS_SIGNS} == {
        "to a/c 12-34-56"
    }


@pytest.mark.parametrize("space", list(payee_service.INVISIBLE_SPACES))
def test_an_invisible_space_reads_as_a_space(space):
    assert fold(f"CAFE{space}SOL") == "cafe sol"
    assert fold(f"{space}CAFE {space}SOL{space}") == "cafe sol"


def test_the_one_time_import_folds_the_same_way():
    """`engine.fold` is the payees' fold, not a second copy that can drift."""
    assert one_time_engine.fold("Café\u2013Sol\u00a0Bar") == fold("CAFE-SOL BAR") == "cafe-sol bar"


# --------------------------------------------------------------------------- #
# Resolving a name to a payee
# --------------------------------------------------------------------------- #


def _write(session, user, household_id, work):
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household_id):
        return work()


def test_two_spellings_resolve_to_one_payee_in_each_household(
    session, owner, member, household, other_household
):
    first = _write(
        session, owner, household.id,
        lambda: payee_service.get_or_create(session, household.id, "Café Sol"),
    )
    again = _write(
        session, owner, household.id,
        lambda: payee_service.get_or_create(session, household.id, "CAFE SOL"),
    )
    theirs = _write(
        session, member, other_household.id,
        lambda: payee_service.get_or_create(session, other_household.id, "CAFE\u00a0SOL"),
    )

    assert again.id == first.id
    assert first.name == "Café Sol"  # the spelling that arrived first is kept
    assert theirs.id != first.id
    assert theirs.household_id == other_household.id
    ours = session.execute(
        select(Payee.id).where(Payee.household_id == household.id)
    ).scalars().all()
    assert ours == [first.id]


def test_a_sort_code_with_en_dashes_finds_the_payee_written_with_hyphens(
    session, owner, household
):
    made = _write(
        session, owner, household.id,
        lambda: payee_service.get_or_create(session, household.id, SORT_CODE),
    )
    en_dash = SORT_CODE.replace("-", "\u2013")
    found = payee_service.by_folded(session, household.id, [en_dash])
    assert {key: payee.id for key, payee in found.items()} == {fold(SORT_CODE): made.id}

    rules = payee_service.load_rules(session, household.id)
    assert rules.payee_for(en_dash).id == made.id


def test_invisible_spaces_find_the_payee_written_without_them(session, owner, household):
    made = _write(
        session, owner, household.id,
        lambda: payee_service.get_or_create(session, household.id, "Panaderia Luna"),
    )
    rules = payee_service.load_rules(session, household.id)
    for spelling in ("\ufeffPANADERÍA\u00a0LUNA", "Panadería\u200b Luna", "PANADERIA\u202fLUNA"):
        assert rules.payee_for(spelling).id == made.id, spelling


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def _rule(match_type: MatchType, pattern: str, *, action=RuleAction.map, replacement=None):
    return SimpleNamespace(
        id=f"{match_type.value}:{pattern}", match_type=match_type, pattern=pattern,
        action=action, replacement=replacement,
    )


@pytest.mark.parametrize(
    ("match_type", "pattern", "raw"),
    [
        (MatchType.contains, "cafe sol", "COMPRA CAFÉ\u00a0SOL MADRID"),
        (MatchType.equals, SORT_CODE, SORT_CODE.replace("-", "\u2013")),
        (MatchType.equals, SORT_CODE.replace("-", "\u2212"), SORT_CODE),
        (MatchType.prefix, "PANADERÍA LUNA", "PANADERIA\u200bLUNA 0042"),
    ],
)
def test_literal_rules_compare_folded_spellings(match_type, pattern, raw):
    assert matches(_rule(match_type, pattern), raw)


def test_a_literal_rule_still_misses_a_different_name():
    assert not matches(_rule(MatchType.contains, "cafe sol"), "CAFE LUNA")
    assert not matches(_rule(MatchType.equals, SORT_CODE), "TO A/C 12-34-57")


def test_a_pattern_of_nothing_visible_claims_nothing():
    assert not matches(_rule(MatchType.contains, "\u200b"), "CAFE SOL")


def test_a_rule_resolves_both_spellings_to_its_payee(session, owner, household):
    def make():
        payee = payee_service.get_or_create(session, household.id, "Cafe Sol")
        payee_service.create_rule(
            session, household_id=household.id, match_type=MatchType.contains,
            pattern="CAFÉ SOL", payee=payee,
        )
        return payee

    payee = _write(session, owner, household.id, make)
    rules = payee_service.load_rules(session, household.id)
    assert rules.match_for("TPV CAFE SOL 0031")[1].id == payee.id
    assert rules.match_for("TPV Café\u00a0Sol 0031")[1].id == payee.id


def test_a_regex_with_backslash_d_behaves_as_before():
    r"""Folding the *pattern* would turn `\D` into `\d` -- the opposite.

    The subject is folded, the pattern never is: these are exactly the answers
    the rule gave before #268, and the stored pattern is untouched by matching.
    """
    assert "\\D".casefold() == "\\d"  # why the pattern is left alone

    not_digits = _rule(MatchType.regex, r"^\D+$")
    assert matches(not_digits, "CAFÉ SOL")
    assert matches(not_digits, "Café\u2013Sol")
    assert not matches(not_digits, "CAFE SOL 12")
    assert not_digits.pattern == r"^\D+$"

    separated = _rule(MatchType.regex, r"^TO A/C \d\d\D\d\d\D\d\d$")
    assert matches(separated, SORT_CODE)
    assert matches(separated, SORT_CODE.replace("-", "\u2013"))
    assert not matches(separated, "TO A/C 123456")


def test_a_regex_still_matches_the_raw_text_and_also_the_folded_one():
    # Raw first, so a pattern written with the accent keeps matching it...
    assert matches(_rule(MatchType.regex, r"^Café"), "CAFÉ SOL")
    # ...and a pattern without one now finds the accented row too.
    assert matches(_rule(MatchType.regex, r"^cafe sol$"), "Café\u00a0Sol")
    assert matches(_rule(MatchType.regex, r"\d\d-\d\d-\d\d"), SORT_CODE.replace("-", "\u2013"))
    assert not matches(_rule(MatchType.regex, r"^cafe luna$"), "Café Sol")


def test_a_literal_rewrite_cuts_the_accented_spelling_it_matches():
    rail = _rule(MatchType.prefix, "PAGO MOVIL", action=RuleAction.rewrite)
    assert rewrite_with(rail, "PAGO MÓVIL BAR MARISOL") == "BAR MARISOL"
    # Decomposed: the accent belongs to the O it follows and goes with it.
    assert rewrite_with(rail, "PAGO MO\u0301VIL BAR MARISOL") == "BAR MARISOL"
    middle = _rule(MatchType.contains, "MOVIL", action=RuleAction.rewrite, replacement="MOBILE")
    assert rewrite_with(middle, "PAGO MÓVIL BAR MARISOL") == "PAGO MOBILE BAR MARISOL"
    # A length-changing fold: ß is two letters folded, one in the original.
    street = _rule(MatchType.prefix, "STRASSE", action=RuleAction.rewrite)
    assert rewrite_with(street, "Straße Kiosk Nord") == "Kiosk Nord"


def test_folded_offsets_line_up_with_the_fold():
    for raw in ("Café Sol", "Straße \u2013 Kiosk", "Cafe\u0301 \u200b Sol", "\ufeffA\u00a0B"):
        text_ = " ".join(raw.split())
        folded, origins = payee_service._folded_with_origins(text_)
        assert folded == fold(text_)
        assert len(origins) == len(folded)
        assert origins == sorted(origins)


# --------------------------------------------------------------------------- #
# Payees made before #268: the merge preview
# --------------------------------------------------------------------------- #


def _old_payee(session, household_id: str, name: str) -> Payee:
    """A payee as the old fold stored it: whitespace and case only."""
    payee = Payee(
        household_id=household_id, name=name, name_folded=" ".join(name.split()).casefold()
    )
    session.add(payee)
    session.flush()
    return payee


def test_collisions_list_the_pairs_and_nothing_else(
    session, owner, member, household, other_household
):
    def ours():
        return (
            _old_payee(session, household.id, "Café Sol"),
            _old_payee(session, household.id, "CAFE SOL"),
            _old_payee(session, household.id, "Panadería Luna"),
        )

    accented, plain, alone = _write(session, owner, household.id, ours)
    _write(
        session, member, other_household.id,
        lambda: (
            _old_payee(session, other_household.id, "Café Sol"),
            _old_payee(session, other_household.id, "Cafe\u0301 Sol"),
        ),
    )

    groups = payee_service.collisions(session, household.id)
    assert [(g.key, sorted(p.id for p in g.payees)) for g in groups] == [
        ("cafe sol", sorted([accented.id, plain.id]))
    ]
    assert alone.id not in {p.id for g in groups for p in g.payees}


def test_a_merge_from_the_preview_brings_the_survivors_key_up_to_date(
    session, owner, household
):
    def make():
        return (
            _old_payee(session, household.id, "Café Sol"),
            _old_payee(session, household.id, "Cafè Sol"),
        )

    keep, fold_away = _write(session, owner, household.id, make)
    assert keep.name_folded == "café sol"  # the old key, before the merge

    _write(
        session, owner, household.id,
        lambda: payee_service.merge(session, source=fold_away, target=keep),
    )

    assert keep.name_folded == "cafe sol"
    assert payee_service.collisions(session, household.id) == []
    again = _write(
        session, owner, household.id,
        lambda: payee_service.get_or_create(session, household.id, "CAFE SOL"),
    )
    assert again.id == keep.id


def test_a_stale_key_still_resolves_through_the_rules(session, owner, household):
    """Before a merge, an import still finds the payee by today's fold of its name."""
    old = _write(session, owner, household.id, lambda: _old_payee(session, household.id, "Café Sol"))
    rules = payee_service.load_rules(session, household.id)
    assert rules.payee_for("CAFE SOL").id == old.id


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def _seed_old(client, house_id: str, user_id: str, names: list[str]) -> list[str]:
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        with batch(own, kind=BatchKind.manual, actor_id=user_id, household_id=house_id):
            made = [_old_payee(own, house_id, name) for name in names]
        own.commit()
        return [one.id for one in made]


def test_the_merge_preview_is_this_households_and_merges_through_the_existing_merge(client):
    owner = _setup_owner(client)["user"]["id"]
    ours = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()["id"]
    twin = client.post("/api/households", json={"name": "Twin"}, headers=HEADERS).json()["id"]
    accented, plain, _ = _seed_old(client, ours, owner, ["Café Sol", "CAFE SOL", "Bar Luna"])
    twin_ids = _seed_old(client, twin, owner, ["Panadería Río", "PANADERIA RIO"])

    got = client.get(f"/api/households/{ours}/payee-collisions", headers=HEADERS)
    assert got.status_code == 200, got.text
    assert [
        (group["key"], sorted(one["id"] for one in group["payees"])) for group in got.json()
    ] == [("cafe sol", sorted([accented, plain]))]
    assert not {one["id"] for g in got.json() for one in g["payees"]} & set(twin_ids)

    # Asking wrote nothing: both payees are still there.
    names = {one["name"] for one in client.get(
        f"/api/households/{ours}/payees", headers=HEADERS
    ).json()}
    assert {"Café Sol", "CAFE SOL"} <= names

    merged = client.post(
        f"/api/payees/{accented}/merge", json={"into_payee_id": plain}, headers=HEADERS
    )
    assert merged.status_code == 200, merged.text
    assert client.get(f"/api/households/{ours}/payee-collisions", headers=HEADERS).json() == []
    still = client.get(f"/api/households/{twin}/payee-collisions", headers=HEADERS).json()
    assert [sorted(one["id"] for one in g["payees"]) for g in still] == [sorted(twin_ids)]


def test_the_merge_preview_is_a_404_for_a_household_you_are_not_in(client):
    _setup_owner(client)
    import app.db as db
    from app.services import households as household_service

    with db.SessionLocal() as own:
        stranger = _bootstrap_user(own, email="other@example.com", name="Other", role=Role.member)
        with batch(own, kind=BatchKind.admin, actor_id=stranger.id):
            theirs = household_service.create_household(own, name="Theirs", creator=stranger)
            own.flush()
            _old_payee(own, theirs.id, "Café Sol")
            _old_payee(own, theirs.id, "CAFE SOL")
        own.commit()
        theirs_id = theirs.id

    got = client.get(f"/api/households/{theirs_id}/payee-collisions", headers=HEADERS)
    assert got.status_code == 404
    assert "Café Sol" not in got.text


# --------------------------------------------------------------------------- #
# The migration
# --------------------------------------------------------------------------- #


def test_the_migration_refolds_keys_but_leaves_a_collision_for_a_person(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import create_engine

    from tests.test_migrations import _config

    url = f"sqlite:///{tmp_path / 'before.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _config(url)
    command.upgrade(cfg, "89c099d8239c")

    engine = create_engine(url)
    now = "2026-01-01 00:00:00"
    with engine.begin() as conn:
        for house in ("h1", "h2"):
            conn.execute(
                text(
                    "INSERT INTO households (id, name, base_currency, date_format, theme,"
                    " receipts_keep_original, created_at, updated_at)"
                    " VALUES (:id, :id, 'EUR', 'YYYY-MM-DD', 'default', 0, :now, :now)"
                ),
                {"id": house, "now": now},
            )
        for payee_id, house, name in [
            ("p1", "h1", "Café Sol"),       # collides with p2: kept as it was
            ("p2", "h1", "CAFE SOL"),
            ("p3", "h1", "Panadería Luna"),  # alone: refolded
            ("p4", "h2", "Café Sol"),       # alone in its household: refolded
            ("p5", "h1", SORT_CODE.replace("-", "\u2013")),  # alone: refolded
        ]:
            conn.execute(
                text(
                    "INSERT INTO payees (id, household_id, name, name_folded, categorisation,"
                    " created_at, updated_at) VALUES (:id, :h, :n, :f, 'history', :now, :now)"
                ),
                {"id": payee_id, "h": house, "n": name,
                 "f": " ".join(name.split()).casefold(), "now": now},
            )

    command.upgrade(cfg, "25e73951a565")
    with engine.connect() as conn:
        after = dict(conn.execute(text("SELECT id, name_folded FROM payees")).all())
    assert after == {
        "p1": "café sol",
        "p2": "cafe sol",
        "p3": "panaderia luna",
        "p4": "cafe sol",
        "p5": "to a/c 12-34-56",
    }

    command.downgrade(cfg, "89c099d8239c")
    with engine.connect() as conn:
        back = dict(conn.execute(text("SELECT id, name_folded FROM payees")).all())
    engine.dispose()
    assert back == {
        "p1": "café sol",
        "p2": "cafe sol",
        "p3": "panadería luna",
        "p4": "café sol",
        "p5": "to a/c 12\u201334\u201356",
    }


def test_matching_leaves_a_regex_rules_stored_pattern_alone(session, owner, household):
    """The stored pattern of a regex rule is what its author wrote, after a match."""

    def make():
        return payee_service.create_rule(
            session, household_id=household.id, match_type=MatchType.regex,
            pattern=r"^\D+$", payee=payee_service.get_or_create(session, household.id, "Letters"),
        )

    rule = _write(session, owner, household.id, make)
    rules = payee_service.load_rules(session, household.id)
    assert rules.payee_for("CAFÉ SOL").name == "Letters"
    assert rules.payee_for("CAFE SOL 12") is None
    stored = session.execute(select(PayeeRule.pattern).where(PayeeRule.id == rule.id)).scalar_one()
    assert stored == r"^\D+$"


# --------------------------------------------------------------------------- #
# Review of #275: a shared key has one holder, and lookups agree with the rules
# --------------------------------------------------------------------------- #


def _run_migration(session, direction: str) -> None:
    """Run this change's migration against the fixture's own database."""
    import importlib.util
    import pathlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = next(
        (pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions").glob(
            "25e73951a565_*.py"
        )
    )
    spec = importlib.util.spec_from_file_location("refold_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(session.connection())):
        getattr(module, direction)()
    session.expire_all()


def _household_payees(session, household_id: str) -> list[str]:
    return sorted(
        session.execute(select(Payee.id).where(Payee.household_id == household_id)).scalars()
    )


def test_after_the_migration_a_third_spelling_finds_the_holder_not_a_new_payee(
    session, owner, member, household, other_household, accounts
):
    from datetime import date

    from app.models import Transaction

    def ours():
        quiet = _old_payee(session, household.id, "Cafetería José")
        busy = _old_payee(session, household.id, "CAFETERIA JOSÉ")
        for day in (1, 2):
            session.add(
                Transaction(
                    household_id=household.id, account_id=accounts["checking"].id,
                    date=date(2026, 3, day), amount=-1_250, payee_id=busy.id,
                )
            )
        return quiet, busy

    quiet, busy = _write(session, owner, household.id, ours)
    theirs = _write(
        session, member, other_household.id,
        lambda: _old_payee(session, other_household.id, "Cafetería José"),
    )
    session.commit()

    _run_migration(session, "upgrade")

    # One of the two holds the new key -- the one with the transactions -- and
    # the other keeps its old one. The other household's is simply refolded.
    assert busy.name_folded == "cafeteria jose"
    assert quiet.name_folded == "cafetería josé"
    assert theirs.name_folded == "cafeteria jose"

    before = _household_payees(session, household.id)
    found = _write(
        session, owner, household.id,
        lambda: payee_service.get_or_create(session, household.id, "CAFETERIA JOSE"),
    )
    assert found.id == busy.id
    assert _household_payees(session, household.id) == before == sorted([quiet.id, busy.id])

    spellings = ["Cafetería José", "CAFETERIA JOSE", "cafeteria\u00a0jose\u0301"]
    looked_up = payee_service.by_folded(session, household.id, spellings)
    rules = payee_service.load_rules(session, household.id)
    assert {key: payee.id for key, payee in looked_up.items()} == {"cafeteria jose": busy.id}
    assert {rules.payee_for(one).id for one in spellings} == {busy.id}

    # Still offered for a person to merge.
    groups = payee_service.collisions(session, household.id)
    assert [sorted(p.id for p in g.payees) for g in groups] == [sorted([quiet.id, busy.id])]
    assert payee_service.collisions(session, other_household.id) == []

    _run_migration(session, "downgrade")
    assert busy.name_folded == "cafeteria josé"
    assert quiet.name_folded == "cafetería josé"
    assert theirs.name_folded == "cafetería josé"


def test_the_holder_is_the_lowest_id_when_transactions_tie(session, owner, household):
    def make():
        return (
            _old_payee(session, household.id, "Cafetería José"),
            _old_payee(session, household.id, "CAFETERIA JOSÉ"),
        )

    pair = _write(session, owner, household.id, make)
    session.commit()
    _run_migration(session, "upgrade")
    lowest = min(pair, key=lambda p: p.id)
    assert {p.id: p.name_folded == "cafeteria jose" for p in pair} == {
        p.id: p.id == lowest.id for p in pair
    }


def test_with_no_holder_lookups_still_agree_with_the_rules(session, owner, household):
    """No payee holds the new key (an undo can restore an old one): no third payee."""

    def make():
        return (
            _old_payee(session, household.id, "Cafetería José"),
            _old_payee(session, household.id, "CAFETERIA JOSÉ"),
        )

    first, second = _write(session, owner, household.id, make)
    found = _write(
        session, owner, household.id,
        lambda: payee_service.get_or_create(session, household.id, "Cafeteria Jose"),
    )
    rules = payee_service.load_rules(session, household.id)
    assert found.id in {first.id, second.id}
    assert rules.payee_for("Cafeteria Jose").id == found.id
    assert payee_service.by_folded(session, household.id, ["CAFETERIA JOSE"]) == {
        "cafeteria jose": found
    }
    assert _household_payees(session, household.id) == sorted([first.id, second.id])


def test_a_cut_never_ends_inside_a_character_that_folds_to_two():
    # `STRAS` ends inside the ß of Straße, which folds to `ss`: no cut at all,
    # rather than taking the ß or leaving half of it.
    partial = _rule(MatchType.contains, "STRAS", action=RuleAction.rewrite, replacement="X")
    assert payee_service._literal_span(partial, "Straße Kiosk") is None
    assert rewrite_with(partial, "Straße Kiosk") == "Straße Kiosk"
    # A later occurrence on whole characters is still found.
    assert rewrite_with(partial, "Straße STRASBOURG CAFE") == "Straße XBOURG CAFE"
    # And a whole ß is cut as before.
    whole = _rule(MatchType.contains, "STRASSE", action=RuleAction.rewrite, replacement="ST")
    assert rewrite_with(whole, "Hauptstraße Kiosk") == "HauptST Kiosk"
