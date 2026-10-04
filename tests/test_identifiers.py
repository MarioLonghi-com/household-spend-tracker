"""What banks call an account, and finding it again (issue #66).

Every identifier here is made up: `GB82WEST12345698765432` is the IBAN
registry's own example, and the rest are numbers nobody holds.
"""

from __future__ import annotations

import pytest

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import Conflict, ValidationError
from app.models import Account, AccountIdentifier, AccountType, BatchKind, IdentifierKind
from app.services import identifiers
from tests.conftest import HEADERS, _setup_owner

OFX_FOR = (
    "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>"
    "<CURDEF>EUR<BANKACCTFROM><BANKID>000000<ACCTID>{acct}<ACCTTYPE>CHECKING</BANKACCTFROM>"
    "<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260105<TRNAMT>-4.20<FITID>A1"
    "<NAME>Coffee</STMTTRN></BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n"
)


@pytest.fixture()
def two_more(session, household, owner, accounts):
    """A second checking account and a savings pocket, so every match has a rival."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        other = Account(
            household_id=household.id, name="Second current", type=AccountType.checking,
            currency="EUR",
        )
        pocket = Account(
            household_id=household.id, name="Pocket", type=AccountType.savings, currency="EUR"
        )
        session.add_all([other, pocket])
    return {**accounts, "other": other, "pocket": pocket}


def _add(session, owner, household, kind, value, account=None):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        return identifiers.add(
            session, household_id=household.id, kind=kind, value=value, account=account
        )


def test_one_identifier_cannot_name_two_accounts_however_it_is_spaced(
    session, owner, household, two_more
):
    _add(session, owner, household, "iban", "GB82 WEST 1234 5698 7654 32", two_more["checking"])
    with pytest.raises(Conflict, match="already an identifier on Checking"):
        _add(session, owner, household, "iban", "gb82west12345698765432", two_more["other"])


def test_a_holder_belongs_to_the_household_and_an_account_identifier_to_an_account(
    session, owner, household, two_more
):
    with pytest.raises(ValidationError, match="belongs to the household"):
        _add(session, owner, household, "holder", "Jane Doe", two_more["checking"])
    with pytest.raises(ValidationError, match="says which account"):
        _add(session, owner, household, "number", "12345678", None)
    held = _add(session, owner, household, "holder", "DOE JANE", None)
    assert held.account_id is None
    assert held.normalised == "DOE JANE"


def test_too_short_to_mean_anything_is_refused(session, owner, household, two_more):
    with pytest.raises(ValidationError, match="too short"):
        _add(session, owner, household, "card", "12", two_more["card"])


def test_a_short_number_matches_only_as_its_own_token(session, owner, household, two_more):
    """Four digits live inside every reference number. Only a whole token counts."""
    last_four = _add(session, owner, household, "card", "4242", two_more["card"])
    rows = [last_four]
    assert identifiers.named_in(rows, "ACC-CARD 411111******4242") != []
    assert identifiers.named_in(rows, "REF 20264242001") == []


def test_a_long_number_is_found_glued_to_other_text(session, owner, household, two_more):
    number = _add(session, owner, household, "number", "12345678", two_more["other"])
    found = identifiers.named_in([number], "TO A/C 12345678 JANE DOE/XP")
    assert [n.account_id for n in found] == [two_more["other"].id]
    assert identifiers.named_in([number], "TO A/C 12345679") == []


def test_an_alias_matches_whole_words_only(session, owner, household, two_more):
    alias = _add(session, owner, household, "alias", "Instant Access Savings", two_more["pocket"])
    assert identifiers.named_in([alias], "From Instant Access Savings") != []
    assert identifiers.named_in([alias], "To instant-access savings") != []
    assert identifiers.named_in([alias], "Instant Access Savingsbond") == []


def test_a_file_tag_is_evidence_in_a_file_name_and_nowhere_else(
    session, owner, household, two_more
):
    tag = _add(session, owner, household, "file_tag", "a1b2c3", two_more["checking"])
    assert identifiers.named_in([tag], "Coffee a1b2c3") == []
    assert identifiers.named_in([tag], "stmt_a1b2c3.csv", include_file_tags=True) != []


def test_a_file_is_recognised_by_its_name(session, owner, household, two_more):
    _add(session, owner, household, "file_tag", "a1b2c3", two_more["checking"])
    _add(session, owner, household, "file_tag", "d4e5f6", two_more["pocket"])

    found = identifiers.recognise_file(
        session, household.id, filename="account-statement_2026-01-01_2026-02-01_en_d4e5f6.csv",
        raw=b"Date,Description,Amount\n2026-01-05,Coffee,-4.20\n",
    )
    assert found is not None
    assert found.account.id == two_more["pocket"].id
    assert "file name" in found.how


def test_an_ofx_file_is_recognised_by_the_account_it_states(
    session, owner, household, two_more
):
    _add(session, owner, household, "number", "11112222", two_more["checking"])
    _add(session, owner, household, "number", "33334444", two_more["other"])
    raw = OFX_FOR.format(acct="33334444").encode()

    found = identifiers.recognise_file(session, household.id, filename="export.ofx", raw=raw)
    assert found is not None and found.account.id == two_more["other"].id
    assert "states its account number" in found.how


def test_a_preamble_iban_recognises_a_spreadsheet_style_export(
    session, owner, household, two_more
):
    _add(session, owner, household, "iban", "GB82WEST12345698765432", two_more["other"])
    raw = (
        b"Cuenta;GB82 WEST 1234 5698 7654 32\n\nFecha;Concepto;Importe\n05/01/2026;Cafe;-4,20\n"
    )
    found = identifiers.recognise_file(session, household.id, filename="export.csv", raw=raw)
    assert found is not None and found.account.id == two_more["other"].id


def test_two_accounts_matching_equally_is_no_answer(session, owner, household, two_more):
    """A wrong pre-selection is worse than none."""
    _add(session, owner, household, "file_tag", "a1b2c3", two_more["checking"])
    _add(session, owner, household, "file_tag", "d4e5f6", two_more["pocket"])
    assert (
        identifiers.recognise_file(
            session, household.id, filename="a1b2c3_d4e5f6.csv", raw=None
        )
        is None
    )


def test_a_savings_statement_dropped_on_the_wrong_pocket_is_refused(
    session, owner, household, two_more
):
    _add(session, owner, household, "alias", "Rainy Day", two_more["pocket"])
    _add(session, owner, household, "alias", "Holiday Fund", two_more["other"])
    payees = ["Deposit to 'Rainy Day'", "Net Interest Paid to 'Rainy Day' for May 1, 2026"]

    with pytest.raises(Conflict, match="this statement is for Pocket"):
        identifiers.check_pocket(session, two_more["other"], payees)
    identifiers.check_pocket(session, two_more["pocket"], payees)  # the right one: silent


def test_deleting_an_account_takes_its_identifiers_and_undo_brings_them_back(
    session, owner, household, two_more
):
    from sqlalchemy import select

    _add(session, owner, household, "number", "55556666", two_more["other"])
    with batch(
        session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
    ) as gone:
        session.delete(two_more["other"])
        session.flush()
    assert session.execute(select(AccountIdentifier)).scalars().all() == []

    undo_batch(session, gone.id, actor_id=owner.id)
    back = session.execute(select(AccountIdentifier)).scalar_one()
    assert back.value == "55556666" and back.kind is IdentifierKind.number


def test_identifiers_over_http_and_the_import_screen_asks_which_account(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    checking = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    base = f"/api/households/{house['id']}"

    made = client.post(
        f"{base}/identifiers",
        json={"kind": "file_tag", "value": "a1b2c3", "account_id": checking["id"]},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    assert [row["value"] for row in client.get(f"{base}/identifiers").json()] == ["a1b2c3"]

    asked = client.post(
        f"{base}/imports/recognise",
        files={"file": ("statement_a1b2c3.csv", b"Date,Description,Amount\n", "text/csv")},
        headers=HEADERS,
    ).json()
    assert asked["account_id"] == checking["id"]
    assert asked["account_name"] == "Checking"

    unknown = client.post(
        f"{base}/imports/recognise", data={"filename": "nothing.csv"}, headers=HEADERS
    ).json()
    assert unknown["account_id"] is None

    gone = client.delete(f"{base}/identifiers/{made.json()['id']}", headers=HEADERS)
    assert gone.status_code == 204
    assert client.get(f"{base}/identifiers").json() == []


# --------------------------------------------------------------------------- #
# A holder's name, however the bank writes it (issue #132)
#
# One made-up person, stored as a bank writes her in full: two surnames, then
# three given names. Every form below is one a statement has used for her.
# --------------------------------------------------------------------------- #

FULL_NAME = "QUILLFEATHER BRANTWORTH ODILE MAE ROSALIND"


def _holder_matches(value: str, *texts: str) -> bool:
    row = AccountIdentifier(
        kind=IdentifierKind.holder,
        value=value,
        normalised=identifiers.normalise(IdentifierKind.holder, value),
    )
    return identifiers.named_in([row], *texts) != []


def test_a_holder_matches_in_any_order():
    assert _holder_matches(FULL_NAME, "TRANSFER FROM Odile Mae Rosalind Quillfeather Brantworth")
    assert _holder_matches(FULL_NAME, "Brantworth, Mae Rosalind Odile Quillfeather REF 8812")
    # Its words scattered through the text, not next to each other, still all there.
    assert _holder_matches("DOE JANE", "JANE PAID DOE")


def test_a_long_holder_matches_with_middle_names_dropped():
    assert _holder_matches(FULL_NAME, "BIZUM DE ODILE QUILLFEATHER BRANTWORTH")
    assert _holder_matches(FULL_NAME, "Transfer to Odile Quillfeather")
    # Two of the words, but not next to each other: not enough of her.
    assert not _holder_matches(FULL_NAME, "ODILE PAID A QUILLFEATHER INVOICE")
    # Two short words next to each other: nothing surname-length anchors them.
    assert not _holder_matches("DOE ANN MAE", "MAE ANN")


def test_a_cut_off_last_word_matches_as_the_start_of_a_name_word():
    """The bank's field is 30 characters; the second surname loses its tail."""
    assert _holder_matches(FULL_NAME, "Odile Mae Rosalind Quillfeather Brant")
    assert _holder_matches(FULL_NAME, "ODILE QUILLFEATHER BRANT")
    # Cut off where it is the last word of one of the texts given, too.
    assert _holder_matches(FULL_NAME, "ODILE QUILLFEATHER BRANT", "Groceries")
    # Only at the end: mid-text, a four-letter start is just another word.
    assert not _holder_matches(FULL_NAME, "BRANT QUILLF DIRECT DEBIT")
    # And never shorter than four letters.
    assert not _holder_matches(FULL_NAME, "O M R QUILLFEATHER BRA")
    assert not _holder_matches("DOE JANE", "TO JANE DO")


def test_initials_stand_for_names_when_one_name_is_there_in_full():
    assert _holder_matches(FULL_NAME, "TRF O M R QUILLFEATHER BRANT")
    assert _holder_matches(FULL_NAME, "O M R QUILLFEATHER BRANTWORTH")
    assert _holder_matches("DOE JANE", "PAYMENT FROM J DOE")
    # Nothing in full, nothing matched.
    assert not _holder_matches(FULL_NAME, "O M R Q B")
    assert not _holder_matches("DOE JANE", "J D")
    # An initial alone does not make a subset of a long name.
    assert not _holder_matches(FULL_NAME, "O QUILLFEATHER")


def test_a_one_word_short_holder_still_only_matches_as_a_whole_word():
    """A four-letter first name is inside too many other words to be looser about."""
    assert _holder_matches("JANE", "BIZUM FROM JANE")
    assert not _holder_matches("JANE", "BIZUM FROM JANET")
    assert not _holder_matches("JANE", "BIZUM FROM J")
    assert not _holder_matches("JANE", "PAID JANEWAY LTD")
    # A longer single word is held to the same rule: no cut-off form.
    assert not _holder_matches("ROSALIND", "BIZUM FROM ROSA")


def test_other_kinds_are_still_matched_as_they_were(session, owner, household, two_more):
    """Looser matching is for holders only. An alias stays a whole phrase."""
    alias = _add(session, owner, household, "alias", "Rainy Day Pot", two_more["pocket"])
    assert identifiers.named_in([alias], "TO RAINY DAY POT") != []
    assert identifiers.named_in([alias], "TO POT RAINY DAY") == []
    assert identifiers.named_in([alias], "TO RAINY DAY") == []


def test_a_loosely_matched_holder_only_ever_suggests_a_transfer(
    session, owner, household, two_more
):
    """The looser forms are safe because a holder is never strong evidence."""
    from datetime import date

    from app.services import transactions as txn_service
    from app.services import transfers

    _add(session, owner, household, "holder", FULL_NAME, None)
    when = date(2026, 3, 24)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for account, amount, words in (
            (two_more["checking"], -25_000, "TRF O M R QUILLFEATHER BRANT"),
            (two_more["other"], 25_000, "BIZUM DE ODILE QUILLFEATHER"),
        ):
            txn_service.create(
                session, account=account, date=when, amount=amount, payee=None,
                import_payee_original=words, import_id=f"T:{account.id[:6]}:{amount}",
            )

    found = transfers.find(session, household.id)
    assert found.strong == []
    assert len(found.suggested) == 1
    assert "household member's name" in found.suggested[0].why


def test_holder_samples_show_what_each_name_catches(session, owner, household, other_household, two_more):
    from datetime import date

    from app.services import transactions as txn_service

    her = _add(session, owner, household, "holder", FULL_NAME, None)
    him = _add(session, owner, household, "holder", "DOE JOHN", None)
    rows = (
        (date(2026, 3, 1), "TRF O M R QUILLFEATHER BRANT"),
        (date(2026, 3, 2), "TRF O M R QUILLFEATHER BRANT"),
        (date(2026, 3, 9), "BIZUM DE ODILE QUILLFEATHER"),
        (date(2026, 3, 5), "COFFEE SHOP"),
    )
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for n, (when, words) in enumerate(rows):
            txn_service.create(
                session, account=two_more["checking"], date=when, amount=-100 - n, payee=None,
                import_payee_original=words, import_id=f"S:{n}",
            )
    # The same words in a household she is not in are not hers to show.
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=other_household.id):
        elsewhere = Account(
            household_id=other_household.id, name="Theirs", type=AccountType.checking,
            currency="EUR",
        )
        session.add(elsewhere)
        session.flush()
        txn_service.create(
            session, account=elsewhere, date=date(2026, 3, 20), amount=-5, payee=None,
            import_payee_original="ODILE QUILLFEATHER ELSEWHERE", import_id="S:other",
        )

    found = {one.identifier_id: one for one in identifiers.holder_samples(session, household.id)}
    assert set(found) == {her.id, him.id}
    assert found[her.id].rows == 3
    # Most recent first, each text once.
    assert found[her.id].samples == ["BIZUM DE ODILE QUILLFEATHER", "TRF O M R QUILLFEATHER BRANT"]
    assert found[him.id].rows == 0 and found[him.id].samples == []


def test_holder_samples_over_http_and_404_for_a_household_you_are_not_in(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    base = f"/api/households/{house['id']}"
    checking = client.post(
        f"{base}/accounts", json={"name": "Checking", "type": "checking"}, headers=HEADERS
    ).json()
    holder = client.post(
        f"{base}/identifiers", json={"kind": "holder", "value": FULL_NAME}, headers=HEADERS
    ).json()
    made = client.post(
        f"{base}/transactions",
        json={"account_id": checking["id"], "date": "2026-03-09", "amount": -2_500,
              "payee_name": "Odile Quillfeather Brantworth"},
        headers=HEADERS,
    )
    assert made.status_code in (200, 201), made.text

    answer = client.get(f"{base}/identifiers/holder-samples", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    assert answer.json() == [
        {"identifier_id": holder["id"], "rows": 1, "samples": ["Odile Quillfeather Brantworth"]}
    ]

    # A real household this user is not in answers exactly as a made-up id does.
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
    real = client.get(f"/api/households/{theirs_id}/identifiers/holder-samples", headers=HEADERS)
    fake = client.get(f"/api/households/{'f' * 32}/identifiers/holder-samples", headers=HEADERS)
    assert real.status_code == fake.status_code == 404
    assert real.json() == fake.json()
