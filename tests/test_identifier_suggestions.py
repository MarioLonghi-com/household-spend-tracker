"""Identifiers the ledger suggests, for a person to add or ignore (#130).

Every ledger here is synthetic. The IBANs are the registry's own examples
(`GB82WEST12345698765432`, `ES9121000418450200051332`), the card numbers are
the test numbers card networks publish, and every name is made up.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import (
    Account,
    AccountIdentifier,
    AccountType,
    BatchKind,
    IdentifierKind,
    IgnoredIdentifierSuggestion,
    LinkSource,
    Transaction,
)
from app.services import identifier_suggestions as suggestions
from app.services import identifiers, transfers
from tests.conftest import HEADERS, _setup_owner
from tests.test_transfer_matching import _row

DAY = date(2026, 4, 6)


@pytest.fixture()
def ledger(session, owner, household, accounts):
    """A current account, a savings pocket named after its bank, a card named
    with its last four, and the fixture's GBP account and EUR card."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        made = {
            "current": Account(
                household_id=household.id, name="Current", type=AccountType.checking,
                currency="EUR",
            ),
            "pocket": Account(
                household_id=household.id, name="Rainy Day Fund", type=AccountType.savings,
                currency="EUR",
            ),
            "holiday": Account(
                household_id=household.id, name="Holiday Fund-Examplebank",
                institution="Examplebank", type=AccountType.savings, currency="EUR",
            ),
            "visa": Account(
                household_id=household.id, name="Visa 4242", type=AccountType.credit_card,
                currency="EUR",
            ),
            "pounds": accounts["pounds"],
        }
        session.add_all([made["current"], made["pocket"], made["holiday"], made["visa"]])
    return made


def _rows(session, owner, household, account, words, times, amount=-1000, start=DAY):
    return [
        _row(session, owner, household, account, amount - n, words, start + timedelta(days=7 * n))
        for n in range(times)
    ]


def _by_value(found):
    return {item.value: item for item in found.items}


def _add(session, owner, household, kind, value, account):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        return identifiers.add(
            session, household_id=household.id, kind=kind, value=value, account=account
        )


# --------------------------------------------------------------------------- #
# The validity checks
# --------------------------------------------------------------------------- #


def test_the_checks_tell_a_real_number_from_a_random_one():
    assert suggestions.iban_ok("GB82WEST12345698765432")
    assert not suggestions.iban_ok("GB00WEST12345698765432")
    assert suggestions.luhn_ok("4000056655665556")
    assert not suggestions.luhn_ok("4000056655665557")
    assert suggestions.ccc_ok("21000418450200051332")
    assert not suggestions.ccc_ok("21000418460200051332")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Transferencia a ES91 2100 0418 4502 0005 1332", [("iban", "ES9121000418450200051332")]),
        ("Transfer to GB82 WEST 1234 5698 7654 32 REF 99", [("iban", "GB82WEST12345698765432")]),
        ("Transfer to GB00 WEST 1234 5698 7654 32", []),
        ("Ingreso En Tarjeta Desde Cuenta 4000 0566 5566 5556", [("card", "4000056655665556")]),
        ("Ingreso En Tarjeta Desde Cuenta 4000 0566 5566 5557", []),
        ("CARD PAYMENT *4242 COFFEE", [("card", "4242")]),
        ("Card **** 4242 purchase", [("card", "4242")]),
        ("TO A/C 11112222", [("number", "11112222")]),
        ("FPS 12-34-56 11112222 REF", [("number", "11112222")]),
        ("Recibo 2100 0418 45 0200051332", [("number", "21000418450200051332")]),
        ("Recibo 2100 0418 46 0200051332", []),
        ("Liquidacion Del Contrato 0049 0001 2345 6789", [("number", "0049000123456789")]),
        ("Deposit to 'Rainy Day Fund'", [("alias", "Rainy Day Fund")]),
        ("To Holiday Fund", [("alias", "Holiday Fund")]),
        ("OFX posted 20251001120000", []),
    ],
)
def test_each_extractor_reads_its_shape_and_nothing_else(text, expected):
    assert [(f.kind.value, f.value) for f in suggestions.extract(text)] == expected


# --------------------------------------------------------------------------- #
# Each source, on a synthetic ledger
# --------------------------------------------------------------------------- #


def test_words_on_the_far_side_of_confirmed_links_become_an_alias(
    session, owner, household, ledger
):
    """Three links into the pocket, made by a person, whose checking side says
    TO SAVEPLUS; and three history-only links into Holiday whose checking side
    says TO FARAWAY. Only the first is evidence (#131)."""
    for n in range(3):
        out = _row(session, owner, household, ledger["current"], -2000 - n,
                   f"TO SAVEPLUS REF {n:04d}", DAY + timedelta(days=n))
        into = _row(session, owner, household, ledger["pocket"], 2000 + n, "INCOMING",
                    DAY + timedelta(days=n))
        away = _row(session, owner, household, ledger["current"], -3000 - n,
                    f"TO FARAWAY REF {n:04d}", DAY + timedelta(days=n))
        back = _row(session, owner, household, ledger["holiday"], 3000 + n, "INCOMING",
                    DAY + timedelta(days=n))
        with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
            transfers.link(session, out, into, source=LinkSource.person)
            transfers.link(session, away, back, source=LinkSource.history)

    found = _by_value(suggestions.suggest(session, household.id))
    alias = found["SAVEPLUS"]
    assert (alias.kind, alias.account_id, alias.source) == (
        IdentifierKind.alias, ledger["pocket"].id, "links",
    )
    assert alias.mentions == 3
    assert "3 transfers linked into Rainy Day Fund" in alias.why
    assert "FARAWAY" not in found


def test_an_iban_is_suggested_only_with_valid_check_digits(session, owner, household, ledger):
    _rows(session, owner, household, ledger["current"], "Transfer to GB82 WEST 1234 5698 7654 32", 3)
    _rows(session, owner, household, ledger["current"], "Transfer to GB00 WEST 1234 5698 7654 32", 3,
          amount=-5000)

    found = _by_value(suggestions.suggest(session, household.id))
    iban = found["GB82WEST12345698765432"]
    # It names none of the accounts, and it recurs: offered with no account.
    assert (iban.kind, iban.account_id, iban.mentions) == (IdentifierKind.iban, None, 3)
    assert "not one of your accounts" in iban.why
    assert "GB00WEST12345698765432" not in found


def test_a_ccc_inside_an_accounts_stored_iban_is_suggested_for_that_account(
    session, owner, household, ledger
):
    _add(session, owner, household, "iban", "ES91 2100 0418 4502 0005 1332", ledger["current"])
    _row(session, owner, household, ledger["visa"], 5000, "Recibo cuenta 2100 0418 45 0200051332")

    found = _by_value(suggestions.suggest(session, household.id))
    assert found["21000418450200051332"].account_id == ledger["current"].id


def test_a_card_number_passing_luhn_is_suggested_for_the_card_it_ends_like(
    session, owner, household, ledger
):
    _add(session, owner, household, "card", "5556", ledger["pounds"])
    _row(session, owner, household, ledger["current"], -7000,
         "Ingreso En Tarjeta Desde Cuenta 4000 0566 5566 5556")
    _rows(session, owner, household, ledger["current"],
          "Ingreso En Tarjeta Desde Cuenta 4000 0566 5566 5557", 3, amount=-8000)

    found = _by_value(suggestions.suggest(session, household.id))
    card = found["4000056655665556"]
    assert (card.kind, card.account_id, card.mentions) == (
        IdentifierKind.card, ledger["pounds"].id, 1,
    )
    assert "4000056655665557" not in found


def test_a_masked_card_is_suggested_for_the_account_whose_name_ends_in_it(
    session, owner, household, ledger
):
    _row(session, owner, household, ledger["current"], -900, "CARD PAYMENT *4242 COFFEE")
    _row(session, owner, household, ledger["current"], -901, "Pago tarjeta **** 4242")

    found = _by_value(suggestions.suggest(session, household.id))
    assert (found["4242"].kind, found["4242"].account_id, found["4242"].mentions) == (
        IdentifierKind.card, ledger["visa"].id, 2,
    )


def test_a_uk_account_number_is_offered_once_it_recurs(session, owner, household, ledger):
    _rows(session, owner, household, ledger["current"], "TO A/C 11112222", 3)
    _rows(session, owner, household, ledger["current"], "TO A/C 33334444", 2, amount=-4000)

    found = _by_value(suggestions.suggest(session, household.id))
    number = found["11112222"]
    assert (number.kind, number.account_id, number.mentions) == (IdentifierKind.number, None, 3)
    # Twice is not "keeps coming back": nobody is asked about it.
    assert "33334444" not in found


def test_a_pocket_quoted_by_its_own_statement_is_its_alias(session, owner, household, ledger):
    """The pocket's statement quotes a name its account is not called by."""
    _rows(session, owner, household, ledger["pocket"], "Deposit to 'Umbrella Money'", 2, amount=1500)
    _rows(session, owner, household, ledger["current"], "To Umbrella Money", 2, amount=-1500)

    found = _by_value(suggestions.suggest(session, household.id))
    alias = found["Umbrella Money"]
    assert (alias.kind, alias.account_id) == (IdentifierKind.alias, ledger["pocket"].id)
    assert "own statement quotes it" in alias.why
    # Both forms mention it: the pocket's own rows and the current account's.
    assert alias.mentions == 4


def test_an_account_name_without_its_bank_is_suggested_as_an_alias(
    session, owner, household, ledger
):
    _rows(session, owner, household, ledger["current"], "To Holiday Fund", 2)

    found = _by_value(suggestions.suggest(session, household.id))
    alias = found["Holiday Fund"]
    assert (alias.kind, alias.account_id, alias.source, alias.mentions) == (
        IdentifierKind.alias, ledger["holiday"].id, "account_name", 2,
    )
    # A name nobody's text uses is not offered, and nor is one made only of
    # words every account of its kind could be called.
    assert "Holiday Fund-Examplebank" not in found and "Current" not in found


def test_the_tag_every_statement_file_of_an_account_carries_is_suggested(
    session, owner, household, ledger
):
    for month in (1, 2):
        with batch(
            session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id,
            source={"filename": f"account-statement_2026-0{month}-01_2026-0{month}-28_en-gb_d4e5f6.csv",
                    "account_id": ledger["pocket"].id},
        ):
            pass
    with batch(
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id,
        source={"filename": "account-statement_2026-01-01_2026-01-28_en-gb_a1b2c3.csv",
                "account_id": ledger["current"].id},
    ):
        pass

    found = _by_value(suggestions.suggest(session, household.id))
    assert (found["d4e5f6"].kind, found["d4e5f6"].account_id, found["d4e5f6"].mentions) == (
        IdentifierKind.file_tag, ledger["pocket"].id, 2,
    )
    assert found["a1b2c3"].account_id == ledger["current"].id
    # A file tag is looked for in file names only: it links no transfer.
    assert found["d4e5f6"].would_link == 0 and found["d4e5f6"].unit == "files"


def test_the_file_tag_offered_after_a_manual_pick(session, owner, household, ledger):
    name = "account-statement_2026-01-01_2026-02-01_en-gb_d4e5f6.csv"
    assert suggestions.for_file(session, household.id, name) == (IdentifierKind.file_tag, "d4e5f6")
    # Two candidate tokens: which is the tag would be a guess.
    assert suggestions.for_file(session, household.id, "export_a1b2c3_d4e5f6.csv") is None
    # An IBAN in the name is offered as an IBAN.
    assert suggestions.for_file(session, household.id, "ES9121000418450200051332.csv") == (
        IdentifierKind.iban, "ES9121000418450200051332",
    )

    _add(session, owner, household, "file_tag", "d4e5f6", ledger["pocket"])
    assert suggestions.for_file(session, household.id, name) is None


# --------------------------------------------------------------------------- #
# What is never offered
# --------------------------------------------------------------------------- #


def test_a_value_that_already_matches_an_identifier_is_never_suggested(
    session, owner, household, ledger
):
    _rows(session, owner, household, ledger["current"], "TO A/C 11112222", 3)
    _rows(session, owner, household, ledger["current"], "To Holiday Fund", 2, amount=-2000)
    _row(session, owner, household, ledger["current"], -900, "CARD PAYMENT *4242 COFFEE")
    before = _by_value(suggestions.suggest(session, household.id))
    assert {"11112222", "Holiday Fund", "4242"} <= set(before)

    # Stored as typed, spaced differently, or as a longer identifier that
    # already contains it.
    _add(session, owner, household, "number", "1111 2222", ledger["pounds"])
    _add(session, owner, household, "alias", "HOLIDAY FUND", ledger["holiday"])
    _add(session, owner, household, "card", "4242", ledger["visa"])

    after = _by_value(suggestions.suggest(session, household.id))
    assert not ({"11112222", "Holiday Fund", "4242"} & set(after))


def test_a_file_tag_with_the_same_digits_does_not_hide_the_account_number(
    session, owner, household, ledger
):
    """Statements named after the account's number leave a file tag equal to it.
    Transfer matching never reads file tags, so the number is still worth
    offering -- and the tag says whose it is."""
    _add(session, owner, household, "file_tag", "11112222", ledger["pounds"])
    _rows(session, owner, household, ledger["current"], "TO A/C 11112222", 3)

    found = _by_value(suggestions.suggest(session, household.id))["11112222"]
    assert found.kind is IdentifierKind.number
    assert found.account_id == ledger["pounds"].id


def test_an_ignored_suggestion_stays_ignored(session, owner, household, ledger):
    _rows(session, owner, household, ledger["current"], "TO A/C 11112222", 3)
    _rows(session, owner, household, ledger["current"], "To Holiday Fund", 2, amount=-2000)
    assert "11112222" in _by_value(suggestions.suggest(session, household.id))

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as act:
        first = suggestions.ignore(session, household.id, kind="number", value="1111 2222")
        again = suggestions.ignore(session, household.id, kind="number", value="11112222")
    assert first is again and first.ignored_by_id == owner.id

    # More rows saying it, and a second statement: still not offered.
    _rows(session, owner, household, ledger["pounds"], "FROM A/C 11112222", 4, amount=500)
    found = _by_value(suggestions.suggest(session, household.id))
    assert "11112222" not in found
    assert "Holiday Fund" in found  # ignoring one says nothing about another

    # Undone in History, it is offered again.
    undo_batch(session, act.id, actor_id=owner.id)
    assert session.execute(select(func.count()).select_from(IgnoredIdentifierSuggestion)).scalar() == 0
    assert _by_value(suggestions.suggest(session, household.id))["11112222"].mentions == 7


# --------------------------------------------------------------------------- #
# "Would link N pairs"
# --------------------------------------------------------------------------- #


def test_would_link_is_what_find_links_once_the_identifier_is_added(
    session, owner, household, ledger
):
    """Two top-ups of the pocket whose checking side names it only by the
    name its own statement quotes, and one pair nothing would name."""
    pairs = []
    for n, amount in enumerate((2500, 2600)):
        when = DAY + timedelta(days=10 * n)
        pairs.append((
            _row(session, owner, household, ledger["current"], -amount, "To Umbrella Money", when),
            _row(session, owner, household, ledger["pocket"], amount, "Deposit to 'Umbrella Money'", when),
        ))
    _row(session, owner, household, ledger["current"], -2700, "Coffee beans", DAY)
    _row(session, owner, household, ledger["holiday"], 2700, "Interest", DAY)

    identifiers_before = session.execute(select(func.count()).select_from(AccountIdentifier)).scalar()
    found = suggestions.suggest(session, household.id)
    alias = _by_value(found)["Umbrella Money"]
    assert alias.would_link == 2
    assert found.would_link == 2
    # Nothing was written, and nothing was left in the session.
    assert session.execute(select(func.count()).select_from(AccountIdentifier)).scalar() == identifiers_before
    assert not session.new and not session.dirty

    before = {(p.out_leg.id, p.in_leg.id) for p in transfers.find(session, household.id).strong}
    _add(session, owner, household, alias.kind, alias.value, ledger["pocket"])
    after = {(p.out_leg.id, p.in_leg.id) for p in transfers.find(session, household.id).strong}
    assert after - before == alias.pairs == {(out.id, into.id) for out, into in pairs}
    assert len(after - before) == alias.would_link


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def test_suggestions_are_listed_added_and_ignored_over_http(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    base = f"/api/households/{house['id']}"
    current = client.post(
        f"{base}/accounts", json={"name": "Current", "type": "checking"}, headers=HEADERS
    ).json()
    pocket = client.post(
        f"{base}/accounts",
        json={"name": "Holiday Fund-Examplebank", "institution": "Examplebank", "type": "savings"},
        headers=HEADERS,
    ).json()
    for n, words in enumerate(("To Holiday Fund", "To Holiday Fund", "TO A/C 11112222",
                               "TO A/C 11112222", "TO A/C 11112222")):
        made = client.post(
            f"{base}/transactions",
            json={"account_id": current["id"], "date": f"2026-03-{10 + n}", "amount": -1000 - n,
                  "payee_name": words},
            headers=HEADERS,
        )
        assert made.status_code in (200, 201), made.text

    listed = client.get(f"{base}/identifiers/suggestions", headers=HEADERS)
    assert listed.status_code == 200, listed.text
    items = {one["value"]: one for one in listed.json()["items"]}
    assert items["Holiday Fund"]["account_id"] == pocket["id"]
    assert items["Holiday Fund"]["account_name"] == "Holiday Fund-Examplebank"
    assert (items["11112222"]["account_id"], items["11112222"]["mentions"]) == (None, 3)

    added = client.post(
        f"{base}/identifiers/suggestions/add",
        json={"kind": "alias", "value": "Holiday Fund", "account_id": pocket["id"]},
        headers=HEADERS,
    )
    assert added.status_code == 201, added.text
    stored = client.get(f"{base}/identifiers", headers=HEADERS).json()
    assert [(one["kind"], one["value"], one["account_id"]) for one in stored] == [
        ("alias", "Holiday Fund", pocket["id"])
    ]

    ignored = client.post(
        f"{base}/identifiers/suggestions/ignore",
        json={"kind": "number", "value": "11112222"}, headers=HEADERS,
    )
    assert ignored.status_code == 201, ignored.text
    assert client.get(f"{base}/identifiers/suggestions", headers=HEADERS).json()["items"] == []

    # A file the ledger cannot place: its tag is offered, and once added the
    # next statement of that account is recognised by it.
    name = "account-statement_2026-01-01_2026-02-01_en-gb_d4e5f6.csv"
    first = client.post(f"{base}/imports/recognise", data={"filename": name}, headers=HEADERS)
    assert first.json() == {"account_id": None, "account_name": None, "how": None,
                            "tag_kind": "file_tag", "tag": "d4e5f6"}
    client.post(
        f"{base}/identifiers/suggestions/add",
        json={"kind": "file_tag", "value": "d4e5f6", "account_id": pocket["id"]}, headers=HEADERS,
    )
    second = client.post(
        f"{base}/imports/recognise",
        data={"filename": name.replace("2026-01-01_2026-02-01", "2026-02-01_2026-03-01")},
        headers=HEADERS,
    ).json()
    assert (second["account_id"], second["tag"]) == (pocket["id"], None)


def test_suggestions_are_404_for_a_household_you_are_not_in(client):
    _setup_owner(client)
    import app.db as db
    from app.models import Role
    from app.services import households as household_service
    from tests.conftest import _bootstrap_user

    with db.SessionLocal() as session:
        stranger = _bootstrap_user(session, email="other@example.com", name="Other", role=Role.member)
        with batch(session, kind=BatchKind.admin, actor_id=stranger.id):
            theirs = household_service.create_household(session, name="Theirs", creator=stranger)
        session.commit()
        theirs_id = theirs.id
    for method, path, body in (
        ("get", "identifiers/suggestions", None),
        ("post", "identifiers/suggestions/ignore", {"kind": "number", "value": "11112222"}),
    ):
        real = getattr(client, method)(f"/api/households/{theirs_id}/{path}", json=body, headers=HEADERS) \
            if body else client.get(f"/api/households/{theirs_id}/{path}", headers=HEADERS)
        fake = getattr(client, method)(f"/api/households/{'f' * 32}/{path}", json=body, headers=HEADERS) \
            if body else client.get(f"/api/households/{'f' * 32}/{path}", headers=HEADERS)
        assert real.status_code == fake.status_code == 404
        assert real.json() == fake.json()
    with db.SessionLocal() as session:
        assert session.execute(select(func.count()).select_from(IgnoredIdentifierSuggestion)).scalar() == 0
        assert session.execute(select(func.count()).select_from(Transaction)).scalar() == 0


def test_would_link_counts_what_an_old_link_naming_it_would_vouch_for(
    session, owner, household, ledger
):
    """A link from before links said how (`import`) counts as history only
    while a row names the other account (#131). Adding the alias makes it
    name the pocket, which makes a later pair between the two strong -- and
    the count has to see that as `find` does, without the identifier stored."""
    old_out = _row(session, owner, household, ledger["current"], -1100, "To Umbrella Money", DAY)
    old_in = _row(session, owner, household, ledger["pocket"], 1100, "Deposit to 'Umbrella Money'", DAY)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        transfers.link(session, old_out, old_in, source=LinkSource.imported)
    later = DAY + timedelta(days=30)
    out = _row(session, owner, household, ledger["current"], -1200, "Standing order", later)
    into = _row(session, owner, household, ledger["pocket"], 1200, "Incoming", later)
    # Two more, so the alias recurs on rows no link has claimed.
    _rows(session, owner, household, ledger["pocket"], "Deposit to 'Umbrella Money'", 2, amount=90)

    alias = _by_value(suggestions.suggest(session, household.id))["Umbrella Money"]
    assert alias.pairs == {(out.id, into.id)} and alias.would_link == 1

    before = {(p.out_leg.id, p.in_leg.id) for p in transfers.find(session, household.id).strong}
    _add(session, owner, household, alias.kind, alias.value, ledger["pocket"])
    found = transfers.find(session, household.id)
    after = {(p.out_leg.id, p.in_leg.id) for p in found.strong}
    assert after - before == alias.pairs
    assert found.strong[0].source is LinkSource.history


# --------------------------------------------------------------------------- #
# At the size of a real ledger (#232)
# --------------------------------------------------------------------------- #


def _reference(n: int) -> str:
    """A per-row reference the way banks write them: a letter after a digit,
    and never four digits in a row."""
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    return f"{letters[n % 24]}{n % 10}{letters[(n // 10) % 24]}{letters[(n // 240) % 24]}{(n // 5760) % 10}Q"


def test_a_ledger_of_references_is_read_once_per_merchant(
    session, owner, household, ledger, monkeypatch
):
    """6,000 rows, 200 merchants, a reference on every one -- past the scan
    limit, so the cap is exercised too. Each distinct text used to be read on
    its own, so the work grew with the ledger."""
    from sqlalchemy import event

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for n in range(6_000):
            account = ledger["current"] if n % 2 else ledger["visa"]
            session.add(
                Transaction(
                    household_id=household.id, account_id=account.id,
                    date=DAY - timedelta(days=n % 700), amount=-(100 + n),
                    import_payee_original=f"MERCHANT{n % 200:03d} CARD ****4242 {_reference(n)}",
                )
            )
    session.commit()
    session.expunge_all()

    read: list[str | None] = []
    real = suggestions.extract

    def extract(text):
        read.append(text)
        return real(text)

    monkeypatch.setattr(suggestions, "extract", extract)
    statements: list[str] = []
    engine = session.get_bind()

    def count(conn, cursor, statement, *_):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        found = suggestions.suggest(session, household.id, count_pairs=False)
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(read) <= 400, f"{len(read)} texts read for 200 merchants"
    assert len(statements) <= 6, statements
    card = _by_value(found)["4242"]
    assert card.account_id == ledger["visa"].id
    # Counted over the most recent texts read: every one of them says it.
    assert card.mentions == suggestions.SCAN_LIMIT
    assert card.would_link is None and found.would_link is None


def test_a_reference_is_folded_off_but_never_a_number_an_extractor_reads(
    session, owner, household, ledger
):
    """Two A/C numbers behind one wording, each followed by a reference: the
    numbers have four digits in a row, so they are never taken for one."""
    for n in range(3):
        _row(session, owner, household, ledger["current"], -500 - n,
             f"TO A/C 11112222 {_reference(n)}", DAY + timedelta(days=n))
        _row(session, owner, household, ledger["current"], -600 - n,
             f"TO A/C 33334444 {_reference(n + 3)}", DAY + timedelta(days=n))
    assert suggestions._folded("TO A/C 11112222 A0AA0Q") == "TO A/C 11112222"
    assert suggestions._folded("TO A/C 11112222") == "TO A/C 11112222"

    found = _by_value(suggestions.suggest(session, household.id))
    assert (found["11112222"].mentions, found["33334444"].mentions) == (3, 3)


def test_the_pair_counts_are_asked_for_on_their_own_over_http(client):
    """The list comes back without running the transfer matcher; the counts
    for the badge are a second request."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    base = f"/api/households/{house['id']}"
    current = client.post(
        f"{base}/accounts", json={"name": "Current", "type": "checking"}, headers=HEADERS
    ).json()
    client.post(
        f"{base}/accounts",
        json={"name": "Holiday Fund-Examplebank", "institution": "Examplebank", "type": "savings"},
        headers=HEADERS,
    )
    for n in range(3):
        client.post(
            f"{base}/transactions",
            json={"account_id": current["id"], "date": f"2026-03-{10 + n}", "amount": -1000 - n,
                  "payee_name": "To Holiday Fund"},
            headers=HEADERS,
        )

    listed = client.get(f"{base}/identifiers/suggestions", headers=HEADERS).json()
    counted = client.get(f"{base}/identifiers/suggestions?count_pairs=1", headers=HEADERS).json()
    assert [one["value"] for one in listed["items"]] == ["Holiday Fund"]
    assert listed["would_link"] is None and listed["items"][0]["would_link"] is None
    assert counted["would_link"] == 0 and counted["items"][0]["would_link"] == 0
    assert counted["items"][0]["mentions"] == listed["items"][0]["mentions"] == 3
