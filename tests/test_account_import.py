"""Accounts from a CSV file, all or none, and the template to fill in (#146).

The IBANs are the registry's own examples and nobody's account.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import ValidationError
from app.models import (
    Account,
    AccountIdentifier,
    AccountType,
    Batch,
    BatchKind,
    Change,
    ClearedState,
    IdentifierKind,
    Payee,
    Transaction,
)
from app.services import account_import, describing, identifiers
from app.services.account_import import COLUMNS, MAX_ACCOUNT_ROWS, import_accounts
from tests.conftest import HEADERS, _setup_owner

ES_IBAN = "ES9121000418450200051332"
GB_IBAN = "GB82WEST12345698765432"

HEADER = ",".join(COLUMNS)
JOINT = f"Joint current,checking,,Banco Ejemplo,es,1234.56,2026-01-15,{ES_IBAN},"
POUNDS = f"Pounds pot,savings,GBP,Example Building Society,GB,250,2026-02-01,{GB_IBAN},rainy day"


def _file(*lines: str, header: str = HEADER, newline: str = "\n") -> bytes:
    return newline.join([header, *lines]).encode()


def _run(session, household, owner, raw: bytes, *, dry_run: bool, filename: str = "accounts.csv"):
    return import_accounts(
        session, household=household, actor_id=owner.id, raw=raw, filename=filename, dry_run=dry_run
    )


def _counts(session) -> dict[str, int]:
    return {
        model.__name__: session.execute(select(func.count()).select_from(model)).scalar_one()
        for model in (Account, Transaction, AccountIdentifier, Batch, Change, Payee)
    }


def _account(session, household, name) -> Account:
    return session.execute(
        select(Account).where(Account.household_id == household.id, Account.name == name)
    ).scalar_one()


def _opening(session, account) -> Transaction:
    return session.execute(select(Transaction).where(Transaction.account_id == account.id)).scalar_one()


# --------------------------------------------------------------------------- #
# The template
# --------------------------------------------------------------------------- #


def test_the_template_is_the_columns_and_imports_once_filled_in(session, owner, household, accounts):
    text = account_import.template()
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows == [list(COLUMNS)]  # the header and nothing a person might forget to delete

    raw = (text + JOINT + "\r\n" + POUNDS + "\r\n").encode("utf-8")
    outcome = _run(session, household, owner, raw, dry_run=False)
    assert outcome.created == 2
    assert _account(session, household, "Joint current").institution == "Banco Ejemplo"
    assert _account(session, household, "Pounds pot").note == "rainy day"


# --------------------------------------------------------------------------- #
# Dry run and commit
# --------------------------------------------------------------------------- #


def test_a_dry_run_says_what_would_happen_and_writes_nothing(session, owner, household, accounts):
    before = _counts(session)
    outcome = _run(session, household, owner, _file(JOINT, POUNDS), dry_run=True)

    assert _counts(session) == before  # not an account, a transaction, a batch or a change
    assert (outcome.dry_run, outcome.created) == (True, 0)
    joint, pounds = outcome.rows
    assert (joint.line, joint.name, joint.type, joint.currency) == (2, "Joint current", "checking", "EUR")
    assert (joint.country, joint.opening_balance, joint.opening_date) == ("ES", 123456, date(2026, 1, 15))
    assert joint.iban == ES_IBAN and joint.problems == []
    assert joint.flag == "\U0001f1ea\U0001f1f8"
    assert (pounds.line, pounds.currency, pounds.opening_balance) == (3, "GBP", 25000)
    assert pounds.problems == []


def test_committing_creates_every_account_with_its_balance_and_iban(
    session, owner, household, other_household, accounts
):
    before = _counts(session)
    outcome = _run(session, household, owner, _file(JOINT, POUNDS), dry_run=False)
    assert (outcome.dry_run, outcome.created) == (False, 2)

    joint = _account(session, household, "Joint current")
    assert (joint.type, joint.currency, joint.country) == (AccountType.checking, "EUR", "ES")
    assert (joint.institution, joint.note, joint.closed) == ("Banco Ejemplo", None, False)
    pounds = _account(session, household, "Pounds pot")
    assert (pounds.type, pounds.currency, pounds.country) == (AccountType.savings, "GBP", "GB")
    assert pounds.note == "rainy day"

    for account, amount, when in ((joint, 123456, date(2026, 1, 15)), (pounds, 25000, date(2026, 2, 1))):
        opening = _opening(session, account)
        assert (opening.amount, opening.date, opening.cleared) == (amount, when, ClearedState.reconciled)
        assert session.get(Payee, opening.payee_id).name == "Opening balance"
        assert opening.household_id == household.id

    # Each IBAN on the account its line named, and in this household only.
    held = {
        row.value: row.account_id
        for row in session.execute(select(AccountIdentifier)).scalars()
    }
    assert held == {ES_IBAN: joint.id, GB_IBAN: pounds.id}
    kinds = {row.kind for row in session.execute(select(AccountIdentifier)).scalars()}
    assert kinds == {IdentifierKind.iban}
    theirs = session.execute(
        select(func.count()).select_from(Account).where(Account.household_id == other_household.id)
    ).scalar_one()
    assert theirs == 0

    # One act: one applied admin batch, which is what undo and History work in.
    after = _counts(session)
    assert after["Batch"] == before["Batch"] + 1
    assert after["Account"] == before["Account"] + 2
    made = session.execute(select(Batch).order_by(Batch.started_at.desc(), Batch.id)).scalars().first()
    assert made.kind is BatchKind.admin and made.household_id == household.id
    assert made.summary == {"accounts": 2}
    assert made.source["accounts_file"] == "accounts.csv"
    assert made.source["bytes"] == len(_file(JOINT, POUNDS))
    assert len(made.source["sha256"]) == 64
    changed = {c.table_name for c in session.execute(select(Change).where(Change.batch_id == made.id)).scalars()}
    assert {"accounts", "transactions", "account_identifiers"} <= changed


def test_one_bad_row_refuses_the_whole_file_and_leaves_no_trace(session, owner, household, accounts):
    before = _counts(session)
    with pytest.raises(ValidationError, match="1 of 3 rows cannot be imported, so none were"):
        _run(session, household, owner, _file(JOINT, "Broken,piggy bank", POUNDS), dry_run=False)
    assert _counts(session) == before
    names = set(session.execute(select(Account.name)).scalars())
    assert names == {"Checking", "Visa", "UK Savings"}


# --------------------------------------------------------------------------- #
# Row verdicts
# --------------------------------------------------------------------------- #


def test_each_row_says_its_own_problems_at_its_own_physical_line(session, owner, household, accounts):
    raw = _file(
        "",  # line 2, blank: skipped, but still a line of the file
        'Holiday fund,savings,,,,,,,"two',  # lines 3-4: a quoted note over two lines
        'lines"',
        "Wallet,Cash",  # line 5
        ",piggy bank,,,Narnia,1.234,31/01/2026,,",  # line 6: five things wrong
        "Spare,checking,,,,,,,,surplus",  # line 7: a value under no column
    )
    outcome = _run(session, household, owner, raw, dry_run=True)
    by_line = {row.line: row for row in outcome.rows}
    assert sorted(by_line) == [3, 5, 6, 7]

    assert by_line[3].problems == [] and by_line[3].name == "Holiday fund"
    assert by_line[5].problems == [] and by_line[5].type == "cash"

    bad = by_line[6].problems
    assert len(bad) == 5, bad
    assert any("'piggy bank' is not an account type" in p and "credit_card" in p for p in bad)
    assert "'Narnia' is not a country code" in bad
    assert any("more decimals than EUR has" in p for p in bad)
    assert any("'31/01/2026' is not a date" in p and "YYYY-MM-DD" in p for p in bad)
    assert "an account needs a name" in bad
    assert by_line[6].opening_balance is None and by_line[6].opening_date is None
    assert by_line[6].type == "piggy bank" and by_line[6].country == "Narnia"

    assert by_line[7].problems == ["this line has more cells than the file has columns"]


def test_a_type_is_read_whatever_its_case_or_spacing(session, owner, household, accounts):
    typeless = _run(session, household, owner, _file("Mystery,"), dry_run=True)
    assert typeless.rows[0].problems == ["an account needs a type"]

    outcome = _run(
        session, household, owner, _file("Card two,Credit Card", "Loan, OTHER_LIABILITY "), dry_run=False
    )
    assert [row.type for row in outcome.rows] == ["credit_card", "other_liability"]
    assert _account(session, household, "Card two").type is AccountType.credit_card
    assert _account(session, household, "Loan").type is AccountType.other_liability


def test_a_name_already_in_the_household_is_refused_however_it_is_spelled(
    session, owner, household, other_household, accounts
):
    outcome = _run(session, household, owner, _file("  CHECKING ,checking", "Pot,savings"), dry_run=True)
    assert outcome.rows[0].problems == ["there is already an account called 'Checking'"]
    assert outcome.rows[1].problems == []


def test_two_lines_of_one_file_cannot_share_a_name_or_an_iban(session, owner, household, accounts):
    spaced = "GB82 WEST 1234 5698 7654 32"
    outcome = _run(
        session,
        household,
        owner,
        _file(
            f"Pot,savings,,,,,,{GB_IBAN},",
            f"pot ,savings,,,,,,{spaced},",
            f"Wallet,cashbox,,,,,,{ES_IBAN},",
            f"Wallet,cash,,,,,,{ES_IBAN},",  # the first Wallet is unimportable, still its twin
        ),
        dry_run=True,
    )
    assert outcome.rows[0].problems == []
    assert "line 2 of this file is already called 'pot'" in outcome.rows[1].problems
    assert f"line 2 of this file already has the IBAN {spaced!r}" in outcome.rows[1].problems
    assert "line 4 of this file is already called 'Wallet'" in outcome.rows[3].problems
    assert f"line 4 of this file already has the IBAN {ES_IBAN!r}" in outcome.rows[3].problems


def test_an_iban_already_on_an_account_is_refused(session, owner, household, other_household, accounts):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        identifiers.add(
            session, household_id=household.id, kind="iban", value=GB_IBAN, account=accounts["pounds"]
        )
    outcome = _run(session, household, owner, _file(POUNDS), dry_run=True)
    assert outcome.rows[0].problems == [f"{GB_IBAN!r} is already an identifier on UK Savings"]

    # Another household's IBAN is theirs, not a clash.
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=other_household.id):
        elsewhere = Account(
            household_id=other_household.id, name="Theirs", type=AccountType.checking, currency="GBP"
        )
        session.add(elsewhere)
        session.flush()
        identifiers.add(
            session, household_id=other_household.id, kind="iban", value=ES_IBAN, account=elsewhere
        )
    assert _run(session, household, owner, _file(JOINT), dry_run=True).rows[0].problems == []


def test_currency_decides_the_units_and_a_blank_one_is_the_households(
    session, owner, household, accounts
):
    outcome = _run(
        session,
        household,
        owner,
        _file(
            "Plain,checking,,,,10.5,2026-01-01,,",
            "Yen,cash,jpy,,,12000,2026-01-01,,",
            "Yen cents,cash,JPY,,,12.5,2026-01-01,,",
            "Three places,checking,EUR,,,12.345,2026-01-01,,",
            'Thousands,checking,EUR,,,"1,234.56",2026-01-01,,',
            "Euro,checking,EURO,,,,,,",
        ),
        dry_run=True,
    )
    plain, yen, yen_cents, three, thousands, euro = outcome.rows
    assert (plain.currency, plain.opening_balance, plain.problems) == ("EUR", 1050, [])
    assert (yen.currency, yen.opening_balance, yen.problems) == ("JPY", 12000, [])
    assert yen_cents.problems == ["'12.5' has decimals, and JPY has none"]
    assert three.problems == ["'12.345' has more decimals than EUR has (2)"]
    assert len(thousands.problems) == 1 and "no thousands separators" in thousands.problems[0]
    assert euro.problems == ["'EURO' is not a three-letter currency code"]


def test_a_blank_balance_opens_nothing_and_a_blank_date_is_today(session, owner, household, accounts):
    _run(
        session, household, owner, _file("Empty,checking,,,,,,,", "Today,savings,,,,42,,,"), dry_run=False
    )
    empty = _account(session, household, "Empty")
    assert session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == empty.id)
    ).scalar_one() == 0
    opening = _opening(session, _account(session, household, "Today"))
    assert (opening.amount, opening.date) == (4200, date.today())


def test_a_date_in_the_future_or_in_the_wrong_order_is_refused(session, owner, household, accounts):
    outcome = _run(
        session,
        household,
        owner,
        _file("Later,checking,,,,5,2999-01-01,,", "Slashed,checking,,,,5,31/01/2026,,"),
        dry_run=True,
    )
    assert outcome.rows[0].problems == ["an account cannot have been opened in the future"]
    assert outcome.rows[1].problems == [
        "'31/01/2026' is not a date this can read -- write it as YYYY-MM-DD, like 2026-01-31"
    ]


def test_a_five_thousand_digit_balance_is_a_row_problem_not_a_crash(session, owner, household, accounts):
    outcome = _run(session, household, owner, _file(f"Huge,checking,,,,{'9' * 5000},2026-01-01,,"), dry_run=True)
    assert outcome.rows[0].problems == ["an amount 5000 digits long is too large to record as money"]
    assert outcome.rows[0].opening_balance is None


def test_an_iban_longer_than_the_identifier_form_allows_is_refused(session, owner, household, accounts):
    before = _counts(session)
    long_iban = GB_IBAN + "1" * 300
    outcome = _run(session, household, owner, _file(f"Long,checking,,,,,,{long_iban},"), dry_run=True)
    assert outcome.rows[0].problems == ["the IBAN is longer than 120 characters"]
    with pytest.raises(ValidationError, match="1 of 1 row cannot be imported"):
        _run(session, household, owner, _file(f"Long,checking,,,,,,{long_iban},"), dry_run=False)
    assert _counts(session) == before


def test_an_over_long_name_is_the_schemas_refusal_in_words(session, owner, household, accounts):
    outcome = _run(session, household, owner, _file(f"{'x' * 121},checking"), dry_run=True)
    assert outcome.rows[0].problems == ["the name is longer than 120 characters"]


# --------------------------------------------------------------------------- #
# Encodings, delimiters, headers
# --------------------------------------------------------------------------- #


def test_a_semicolon_file_in_windows_1252_keeps_its_accents(session, owner, household, accounts):
    header = ";".join(COLUMNS)
    raw = "\r\n".join([header, "Épargne;savings;;Crédit Agricole;FR;10;2026-01-01;;"]).encode("cp1252")
    _run(session, household, owner, raw, dry_run=False)
    saved = _account(session, household, "Épargne")
    assert (saved.institution, saved.country, saved.type) == ("Crédit Agricole", "FR", AccountType.savings)
    assert _opening(session, saved).amount == 1000


def test_a_tab_file_with_a_bom_and_its_columns_in_any_order_and_case(session, owner, household, accounts):
    raw = ("﻿" + "\r\n".join(["Type\tNAME\t Opening Balance \tIBAN", f"savings\tTabbed\t-3.50\t{ES_IBAN}"])).encode()
    outcome = _run(session, household, owner, raw, dry_run=False)
    assert outcome.rows[0].problems == []
    tabbed = _account(session, household, "Tabbed")
    assert tabbed.type is AccountType.savings
    assert _opening(session, tabbed).amount == -350
    assert session.execute(select(AccountIdentifier.account_id)).scalar_one() == tabbed.id


@pytest.mark.parametrize(
    ("raw", "said"),
    [
        (_file("A,checking,x", header="name,type,colour"), "a column this does not know: 'colour'"),
        (_file("A", header="name"), "no 'type' column"),
        (_file("A,checking,checking", header="name,type,type"), "the column 'type' twice"),
        (_file(header=HEADER), "there are no accounts in this file"),
        (b"", "there are no accounts in this file"),
        (b"\n\n  \n", "there are no accounts in this file"),
        (_file(*[f"A{i},cash" for i in range(MAX_ACCOUNT_ROWS + 1)]), f"more than {MAX_ACCOUNT_ROWS} accounts"),
        (_file(f"A,cash,,,,,,,{'n' * 140_000}"), "line 2 of this file cannot be read as CSV"),
    ],
)
def test_a_file_that_is_not_a_list_of_accounts_is_refused_whole(session, owner, household, accounts, raw, said):
    before = _counts(session)
    with pytest.raises(ValidationError, match=said):
        _run(session, household, owner, raw, dry_run=True)
    assert _counts(session) == before


def test_exactly_the_most_rows_is_allowed(session, owner, household, accounts):
    outcome = _run(
        session, household, owner, _file(*[f"A{i},cash" for i in range(MAX_ACCOUNT_ROWS)]), dry_run=True
    )
    assert len(outcome.rows) == MAX_ACCOUNT_ROWS
    assert not any(row.problems for row in outcome.rows)


# --------------------------------------------------------------------------- #
# Undo and History
# --------------------------------------------------------------------------- #


def test_undo_takes_the_whole_import_back(session, owner, household, accounts):
    before = _counts(session)
    _run(session, household, owner, _file(JOINT, POUNDS), dry_run=False)
    made = session.execute(
        select(Batch).where(Batch.source.is_not(None)).order_by(Batch.started_at.desc())
    ).scalars().first()

    undo_batch(session, made.id, actor_id=owner.id)
    session.commit()
    after = _counts(session)
    for table in ("Account", "Transaction", "AccountIdentifier", "Payee"):
        assert after[table] == before[table], table
    assert set(session.execute(select(Account.name)).scalars()) == {"Checking", "Visa", "UK Savings"}


def test_history_calls_it_an_account_import_and_names_the_file(session, owner, household, accounts):
    _run(session, household, owner, _file(JOINT, POUNDS), dry_run=False, filename="our-accounts.csv")
    made = session.execute(
        select(Batch).where(Batch.source.is_not(None)).order_by(Batch.started_at.desc())
    ).scalars().first()
    said = describing.describe(session, made)
    assert said.headline == "Account import"
    assert said.detail == "2 accounts from our-accounts.csv."


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def _post(client, household_id, raw, *, dry_run: bool | None = None, name="accounts.csv"):
    data = {} if dry_run is None else {"dry_run": "true" if dry_run else "false"}
    return client.post(
        f"/api/households/{household_id}/accounts/import",
        files={"file": (name, raw, "text/csv")},
        data=data,
        headers=HEADERS,
    )


def test_over_http_a_preview_then_a_commit(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()

    template = client.get(f"/api/households/{house['id']}/accounts/import-template.csv", headers=HEADERS)
    assert template.status_code == 200
    assert template.headers["content-type"].startswith("text/csv")
    assert template.headers["content-disposition"] == (
        'attachment; filename="household-spend-tracker-accounts-template.csv"'
    )
    assert template.content.decode("utf-8-sig").splitlines() == [HEADER]

    raw = _file(JOINT, POUNDS)
    preview = _post(client, house["id"], raw)  # dry_run is the default
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body == {
        "dry_run": True,
        "created": 0,
        "rows": [
            {
                "line": 2, "name": "Joint current", "type": "checking", "currency": "EUR",
                "country": "ES", "flag": "\U0001f1ea\U0001f1f8", "opening_balance": 123456,
                "opening_date": "2026-01-15", "iban": ES_IBAN, "problems": [],
            },
            {
                "line": 3, "name": "Pounds pot", "type": "savings", "currency": "GBP",
                "country": "GB", "flag": "\U0001f1ec\U0001f1e7", "opening_balance": 25000,
                "opening_date": "2026-02-01", "iban": GB_IBAN, "problems": [],
            },
        ],
    }
    listed = client.get(f"/api/households/{house['id']}/accounts", headers=HEADERS).json()
    assert listed == []

    done = _post(client, house["id"], raw, dry_run=False)
    assert done.status_code == 201, done.text
    assert (done.json()["dry_run"], done.json()["created"]) == (False, 2)
    listed = client.get(f"/api/households/{house['id']}/accounts", headers=HEADERS).json()
    assert {(a["name"], a["currency"], a["balance"]) for a in listed} == {
        ("Joint current", "EUR", 123456),
        ("Pounds pot", "GBP", 25000),
    }
    idents = client.get(f"/api/households/{house['id']}/identifiers", headers=HEADERS).json()
    assert sorted(i["value"] for i in idents) == sorted([ES_IBAN, GB_IBAN])

    # The same file again: every name is taken now, so nothing is written.
    again = _post(client, house["id"], raw, dry_run=False)
    assert again.status_code == 422
    assert again.json()["detail"].startswith("2 of 2 rows cannot be imported, so none were.")
    assert len(client.get(f"/api/households/{house['id']}/accounts", headers=HEADERS).json()) == 2


def test_over_http_a_file_too_big_is_413_and_a_stranger_gets_404(client):
    _setup_owner(client)
    import app.db as db
    from app.models import Role
    from app.services import households as household_service
    from tests.conftest import _bootstrap_user

    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    big = _file(*[f"A{i},cash,,,,,,,{'n' * 600}" for i in range(450)])
    assert account_import.MAX_ACCOUNT_CSV_BYTES < len(big) < 1024 * 1024
    refused = _post(client, house["id"], big)
    assert refused.status_code == 413
    assert "larger than a list of accounts" in refused.json()["detail"]

    with db.SessionLocal() as session:
        stranger = _bootstrap_user(session, email="other@example.com", name="Other", role=Role.member)
        with batch(session, kind=BatchKind.admin, actor_id=stranger.id):
            theirs = household_service.create_household(session, name="Theirs", creator=stranger)
        session.commit()
        theirs_id = theirs.id

    for household_id in (theirs_id, "f" * 32):
        assert _post(client, household_id, _file(JOINT), dry_run=False).status_code == 404
        got = client.get(f"/api/households/{household_id}/accounts/import-template.csv", headers=HEADERS)
        assert got.status_code == 404
    real = _post(client, theirs_id, _file(JOINT), dry_run=False).json()
    fake = _post(client, "f" * 32, _file(JOINT), dry_run=False).json()
    assert real == fake

    with db.SessionLocal() as session:
        assert session.execute(select(func.count()).select_from(Account)).scalar_one() == 0


def test_any_member_may_import_accounts_but_only_into_their_own_households(client):
    """#115: decided that a member, not only the owner, may run this import --
    it only adds accounts, and History undoes it. Two households: the member
    belongs to one, and the other answers them 404 and gains nothing."""
    import time

    import pyotp
    from fastapi.testclient import TestClient

    from tests.conftest import PASSWORD

    _setup_owner(client)
    ours = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    mine = client.post("/api/households", json={"name": "Owner only"}, headers=HEADERS).json()
    invite = client.post(
        "/api/admin/invitations",
        json={"role": "member", "email": None, "household_ids": [ours["id"]], "step_up_token": None},
        headers=HEADERS,
    )
    assert invite.status_code == 201, invite.text
    member = TestClient(client.app_module.app, base_url="https://testserver")
    started = member.post(
        "/api/invite/begin",
        json={
            "token": invite.json()["link"].rsplit("/", 1)[-1],
            "email": "sam@example.com", "display_name": "Sam", "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()
    enrolled = member.post(
        "/api/invite/enrol",
        json={"blob": started["blob"], "code": pyotp.TOTP(started["secret"]).at(int(time.time()))},
        headers=HEADERS,
    ).json()
    done = member.post(
        "/api/invite/complete", json={"blob": enrolled["blob"], "codes_saved": True}, headers=HEADERS
    )
    assert done.status_code == 200, done.text
    assert done.json()["role"] == "member"

    made = _post(member, ours["id"], _file(JOINT, POUNDS), dry_run=False)
    assert made.status_code == 201, made.text
    assert made.json()["created"] == 2
    listed = client.get(f"/api/households/{ours['id']}/accounts", headers=HEADERS).json()
    assert {(a["name"], a["currency"], a["balance"]) for a in listed} == {
        ("Joint current", "EUR", 123456),
        ("Pounds pot", "GBP", 25000),
    }

    refused = _post(member, mine["id"], _file(JOINT), dry_run=False)
    assert refused.status_code == 404
    assert client.get(f"/api/households/{mine['id']}/accounts", headers=HEADERS).json() == []
