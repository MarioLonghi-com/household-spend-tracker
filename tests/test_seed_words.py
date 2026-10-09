"""A new household's defaults, seeded in the language asked for (#268).

Three things are held here: English seeding is byte-identical to what it was;
two households seeded in two languages get each its own words, stored as
ordinary names; and the server's words, the client's catalog messages and
`app/seed_catalog.json` agree.

No language is reviewed yet, so the real catalog seeds English for every
locale. The tests that need translated words plant a reviewed catalog.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app import seed_words
from app.audit.batch import batch
from app.models import BatchKind, Category, CategoryGroup, Household, Payee, SystemPayee, Transaction
from app.services import accounts as account_service
from app.services import categories as category_service
from app.services import households as household_service
from scripts import seed_catalog
from tests.conftest import HEADERS
from tests.test_api import _setup_owner

ROOT = Path(__file__).resolve().parent.parent

#: What every household was seeded with before #268, written out rather than
#: read from the module under test.
ENGLISH_TREE = [
    ("Income", ["Salary", "Other Income"]),
    ("Bills", ["Rent / Mortgage", "Electricity", "Water", "Internet", "Phone", "Insurance"]),
    ("Everyday", ["Groceries", "Eating Out", "Transport", "Household", "Health"]),
    ("Quality of Life", ["Travel", "Subscriptions", "Gifts", "Hobbies"]),
    ("Non-Monthly", ["Car Maintenance", "Home Maintenance", "Annual Fees"]),
]

#: A reviewed catalog for two languages, planted. pt-BR is complete; sv-SE has
#: one word, so the rest of its tree falls back to English word by word.
PLANTED = {
    "pt-BR": {
        "seed.opening_balance": "Saldo inicial",
        "seed.group.income": "Receitas",
        "seed.category.salary": "Salário",
        "seed.category.other_income": "Outras receitas",
        "seed.group.bills": "Contas fixas",
        "seed.category.rent_mortgage": "Aluguel / Financiamento",
        "seed.category.electricity": "Luz",
        "seed.category.water": "Água",
        "seed.category.internet": "Internet",
        "seed.category.phone": "Telefone",
        "seed.category.insurance": "Seguros",
        "seed.group.everyday": "Dia a dia",
        "seed.category.groceries": "Supermercado",
        "seed.category.eating_out": "Restaurantes",
        "seed.category.transport": "Transporte",
        "seed.category.household": "Casa",
        "seed.category.health": "Saúde",
        "seed.group.quality_of_life": "Qualidade de vida",
        "seed.category.travel": "Viagens",
        "seed.category.subscriptions": "Assinaturas",
        "seed.category.gifts": "Presentes",
        "seed.category.hobbies": "Hobbies",
        "seed.group.non_monthly": "Não mensais",
        "seed.category.car_maintenance": "Manutenção do carro",
        "seed.category.home_maintenance": "Manutenção da casa",
        "seed.category.annual_fees": "Anuidades",
    },
    "sv-SE": {"seed.category.groceries": "Matvaror", "seed.opening_balance": "Ingående saldo"},
    "es-ES": {},
}


@pytest.fixture()
def planted(monkeypatch):
    monkeypatch.setattr(seed_words, "_catalog", PLANTED)
    return PLANTED


def _tree(session, household_id: str) -> list[tuple[str, list[str]]]:
    return [
        (group.name, [one.name for one in sorted(group.categories, key=lambda c: c.sort_order)])
        for group in category_service.list_groups(session, household_id)
    ]


def _payees(session, household_id: str) -> list[tuple[str, SystemPayee | None]]:
    return [
        (one.name, one.system)
        for one in session.execute(
            select(Payee).where(Payee.household_id == household_id).order_by(Payee.name)
        ).scalars()
    ]


def _make(session, owner, name: str, locale: str | None = None) -> Household:
    with batch(session, kind=BatchKind.manual, actor_id=owner.id):
        return household_service.create_household(
            session, name=name, creator=owner, locale=locale
        )


def _open_account(session, owner, household, name: str, currency: str, amount: int):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session,
            household=household,
            name=name,
            type="checking",
            currency=currency,
            opening_balance=amount,
            opening_date=date(2026, 1, 1),
        )
    row = session.execute(select(Transaction).where(Transaction.account_id == account.id)).scalar_one()
    return account, row


# --------------------------------------------------------------------------- #
# English, unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("locale", [None, "en", "pt-BR", "sv-SE", "es-ES", "fr-FR"])
def test_with_no_reviewed_language_every_locale_seeds_the_english_it_always_did(
    session, owner, locale
):
    """The real catalog holds drafts only, and a draft is never seeded."""
    first = _make(session, owner, "First", locale)
    second = _make(session, owner, "Second")

    assert _tree(session, first.id) == ENGLISH_TREE
    assert _tree(session, second.id) == ENGLISH_TREE
    assert _payees(session, first.id) == [], "nothing is made ahead of the first account in English"

    account, row = _open_account(session, owner, first, "Checking", "EUR", 120_000)
    assert _payees(session, first.id) == [("Opening balance", SystemPayee.opening_balance)]
    assert row.memo == "Opening balance"


def test_an_english_household_is_seeded_in_english_beside_a_translated_one(
    session, owner, member, planted
):
    english = _make(session, member, "Ours")
    _make(session, owner, "Nossa", "pt-BR")

    assert _tree(session, english.id) == ENGLISH_TREE
    assert _payees(session, english.id) == []
    _, row = _open_account(session, member, english, "Visa", "GBP", -5_000)
    assert (_payees(session, english.id), row.memo) == (
        [("Opening balance", SystemPayee.opening_balance)],
        "Opening balance",
    )


# --------------------------------------------------------------------------- #
# Two households, two languages
# --------------------------------------------------------------------------- #


def test_two_households_seeded_in_two_languages_get_their_own_words(
    session, owner, member, planted
):
    brazilian = _make(session, owner, "Nossa casa", "pt-BR")
    swedish = _make(session, member, "Vårt hushåll", "sv-SE")

    portuguese = _tree(session, brazilian.id)
    assert portuguese[0] == ("Receitas", ["Salário", "Outras receitas"])
    assert portuguese[2] == ("Dia a dia", ["Supermercado", "Restaurantes", "Transporte", "Casa", "Saúde"])
    assert len(portuguese) == len(ENGLISH_TREE)

    # sv-SE has one reviewed category word: that one is Swedish, the rest
    # English, each word falling back on its own.
    swedish_tree = _tree(session, swedish.id)
    assert swedish_tree[2] == ("Everyday", ["Matvaror", "Eating Out", "Transport", "Household", "Health"])
    assert [group for group, _ in swedish_tree] == [group for group, _ in ENGLISH_TREE]

    # The opening-balance payee is made now, in the household's language.
    assert _payees(session, brazilian.id) == [("Saldo inicial", SystemPayee.opening_balance)]
    assert _payees(session, swedish.id) == [("Ingående saldo", SystemPayee.opening_balance)]


def test_the_language_is_matched_by_the_language_alone_when_the_region_differs(
    session, owner, planted
):
    portuguese = _make(session, owner, "Casa", "pt-PT")
    assert _tree(session, portuguese.id)[0][0] == "Receitas"


def test_opening_balances_use_the_seeded_payee_and_its_word_for_every_account(
    session, owner, member, planted
):
    brazilian = _make(session, owner, "Nossa casa", "pt-BR")
    swedish = _make(session, member, "Vårt hushåll", "sv-SE")

    _, first = _open_account(session, owner, brazilian, "Itaú", "BRL", 150_000)
    _, second = _open_account(session, owner, brazilian, "Nubank", "EUR", 20_000)
    _, swedish_row = _open_account(session, member, swedish, "Lönekonto", "SEK", 900_000)

    seeded = session.execute(
        select(Payee).where(Payee.household_id == brazilian.id)
    ).scalar_one()
    assert (first.payee_id, second.payee_id) == (seeded.id, seeded.id), "one payee, not a second"
    assert (first.memo, second.memo) == ("Saldo inicial", "Saldo inicial")
    assert _payees(session, brazilian.id) == [("Saldo inicial", SystemPayee.opening_balance)]
    assert swedish_row.memo == "Ingående saldo"
    assert _payees(session, swedish.id) == [("Ingående saldo", SystemPayee.opening_balance)]


def test_seeded_names_are_ordinary_names_and_no_locale_is_stored(session, owner, planted):
    brazilian = _make(session, owner, "Nossa casa", "pt-BR")
    salary = session.execute(
        select(Category).where(Category.household_id == brazilian.id, Category.name == "Salário")
    ).scalar_one()

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=brazilian.id):
        category_service.update_category(session, salary, name="Ordenado")
    assert session.get(Category, salary.id).name == "Ordenado"

    stored = {column.name for column in Household.__table__.columns} | {
        column.name for column in CategoryGroup.__table__.columns
    }
    assert not any("locale" in name or "language" in name for name in stored)


def test_a_renamed_english_payee_is_still_left_alone(session, owner, member, planted):
    """A word of the person's own is not a seeded word: English behaves as it did."""
    english = _make(session, member, "Ours")
    _open_account(session, member, english, "Checking", "EUR", 1_000)
    payee = session.execute(select(Payee).where(Payee.household_id == english.id)).scalar_one()
    with batch(session, kind=BatchKind.manual, actor_id=member.id, household_id=english.id):
        payee.name = "Startsaldo"
        payee.name_folded = "startsaldo"

    _, row = _open_account(session, member, english, "Savings", "GBP", 2_000)
    assert row.memo == "Opening balance"
    assert [name for name, _ in _payees(session, english.id)] == ["Opening balance", "Startsaldo"]


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def _names(client, household_id: str) -> list[str]:
    groups = client.get(f"/api/households/{household_id}/categories").json()
    return [group["name"] for group in groups]


def test_the_routes_that_seed_take_a_locale(client, planted):
    _setup_owner(client)
    english = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    brazilian = client.post(
        "/api/households", json={"name": "Nossa", "locale": "pt-BR"}, headers=HEADERS
    ).json()
    by_owner = client.post(
        "/api/admin/households",
        json={"name": "Vår", "base_currency": "SEK", "locale": "pt-BR"},
        headers=HEADERS,
    )
    assert by_owner.status_code == 201, by_owner.text

    assert _names(client, english["id"])[0] == "Income"
    assert _names(client, brazilian["id"])[0] == "Receitas"
    assert _names(client, by_owner.json()["id"])[0] == "Receitas"
    assert "locale" not in brazilian


def test_a_locale_that_is_not_a_language_tag_is_refused(client):
    _setup_owner(client)
    answer = client.post(
        "/api/households", json={"name": "Ours", "locale": "../../etc"}, headers=HEADERS
    )
    assert answer.status_code == 422
    assert client.get("/api/households").json() == []


# --------------------------------------------------------------------------- #
# The three sources agree
# --------------------------------------------------------------------------- #


def _client_words() -> dict[str, str]:
    text = (ROOT / "client/src/lib/seedWords.ts").read_text(encoding="utf-8")
    return dict(re.findall(r'id: "(seed\.[\w.]+)",\s*message: "([^"]*)"', text))


@pytest.mark.repo_wide
def test_the_client_declares_every_seed_word_with_the_same_english():
    assert _client_words() == seed_words.ENGLISH


def test_the_tree_names_every_word_but_the_payee_once():
    named = [group for group, _ in seed_words.TREE] + [
        one for _, ids in seed_words.TREE for one in ids
    ]
    assert sorted(named + [seed_words.OPENING_BALANCE]) == sorted(seed_words.ENGLISH)
    assert [
        (seed_words.ENGLISH[group], [seed_words.ENGLISH[one] for one in ids])
        for group, ids in seed_words.TREE
    ] == ENGLISH_TREE


@pytest.mark.repo_wide
def test_the_seed_catalog_is_what_the_catalogs_say_today():
    assert seed_catalog.render(seed_catalog.build()) == seed_catalog.OUT.read_text(encoding="utf-8")


def test_the_seed_catalog_carries_reviewed_words_and_never_a_draft(tmp_path):
    """A fuzzy draft is left out; a reviewed word is carried, continued lines and all."""
    for locale, body in {
        "en": 'msgid "seed.group.income"\nmsgstr "Income"\n',
        "pt-BR": (
            '#, fuzzy\nmsgid "seed.group.income"\nmsgstr "Receitas"\n\n'
            'msgid "seed.group.bills"\nmsgstr ""\n"Contas "\n"fixas"\n\n'
            'msgid "Accounts"\nmsgstr "Contas"\n'
        ),
        "sv-SE": '#, fuzzy\nmsgid "seed.group.income"\nmsgstr "Inkomster"\n',
    }.items():
        (tmp_path / locale).mkdir()
        (tmp_path / locale / "messages.po").write_text(body, encoding="utf-8")

    assert seed_catalog.build(tmp_path) == {
        "pt-BR": {"seed.group.bills": "Contas fixas"},
        "sv-SE": {},
    }
