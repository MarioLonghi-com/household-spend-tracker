"""An account's bank and note: trimmed, and "none" spelled one way (#20).

The edit panel used to send both raw, so `"  Bank "` kept its spaces and an
emptied field was stored as `""` beside the new-account panel's null. Null on
the PATCH means "leave it alone", so emptying one needs its own word --
`clear_institution` / `clear_note`, the shape `clear_country` has -- and the
server trims and folds a blank to null whatever a client sends.

Two households, two accounts in two currencies, so a write that lands on the
wrong account, or on every account, shows.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from .test_api import HEADERS, _setup_owner


def _two_accounts(client) -> tuple[dict, dict]:
    _setup_owner(client)
    ours = client.post("/api/households", json={"name": "Doe-Smith"}, headers=HEADERS).json()
    theirs = client.post(
        "/api/households", json={"name": "Second", "base_currency": "GBP"}, headers=HEADERS
    ).json()
    euros = client.post(
        f"/api/households/{ours['id']}/accounts",
        json={"name": "Joint current", "type": "checking", "currency": "EUR",
              "institution": "Example Bank", "note": "The one the salaries land in"},
        headers=HEADERS,
    )
    pounds = client.post(
        f"/api/households/{theirs['id']}/accounts",
        json={"name": "Pounds", "type": "savings", "currency": "GBP",
              "institution": "Other Bank", "note": "Rainy days"},
        headers=HEADERS,
    )
    assert euros.status_code == 201, euros.text
    assert pounds.status_code == 201, pounds.text
    return euros.json(), pounds.json()


def _stored(client, account: dict) -> tuple[str | None, str | None]:
    got = client.get(f"/api/accounts/{account['id']}").json()
    return got["institution"], got["note"]


def test_a_new_account_stores_its_bank_and_note_trimmed_and_a_blank_one_as_null(client):
    _setup_owner(client)
    ours = client.post("/api/households", json={"name": "Doe-Smith"}, headers=HEADERS).json()
    theirs = client.post(
        "/api/households", json={"name": "Second", "base_currency": "GBP"}, headers=HEADERS
    ).json()

    padded = client.post(
        f"/api/households/{ours['id']}/accounts",
        json={"name": "Joint current", "type": "checking",
              "institution": "  Example Bank ", "note": "\n  Salaries\tland here \n"},
        headers=HEADERS,
    ).json()
    blank = client.post(
        f"/api/households/{theirs['id']}/accounts",
        json={"name": "Pounds", "type": "savings", "institution": " \t ", "note": ""},
        headers=HEADERS,
    ).json()

    # The inside of a note is the person's; only its edges go.
    assert _stored(client, padded) == ("Example Bank", "Salaries\tland here")
    assert _stored(client, blank) == (None, None)


def test_a_padded_bank_and_note_are_stored_trimmed_on_that_account_only(client):
    euros, pounds = _two_accounts(client)

    saved = client.patch(
        f"/api/accounts/{euros['id']}",
        json={"institution": "  New Bank  ", "note": " Moved in October "},
        headers=HEADERS,
    )

    assert saved.status_code == 200, saved.text
    assert (saved.json()["institution"], saved.json()["note"]) == ("New Bank", "Moved in October")
    assert _stored(client, euros) == ("New Bank", "Moved in October")
    assert _stored(client, pounds) == ("Other Bank", "Rainy days")


def test_the_clear_flags_empty_one_field_to_null_and_leave_the_other(client):
    euros, pounds = _two_accounts(client)

    note_gone = client.patch(
        f"/api/accounts/{euros['id']}", json={"clear_note": True}, headers=HEADERS
    )
    assert note_gone.status_code == 200, note_gone.text
    assert _stored(client, euros) == ("Example Bank", None)

    bank_gone = client.patch(
        f"/api/accounts/{euros['id']}",
        json={"institution": None, "clear_institution": True},
        headers=HEADERS,
    )
    assert bank_gone.status_code == 200, bank_gone.text
    assert _stored(client, euros) == (None, None)
    assert _stored(client, pounds) == ("Other Bank", "Rainy days")


def test_null_still_leaves_both_alone(client):
    euros, pounds = _two_accounts(client)

    renamed = client.patch(
        f"/api/accounts/{euros['id']}",
        json={"name": "Renamed", "institution": None, "note": None},
        headers=HEADERS,
    )

    assert renamed.json()["name"] == "Renamed"
    assert _stored(client, euros) == ("Example Bank", "The one the salaries land in")


def test_a_blank_value_is_stored_as_null_never_as_an_empty_string(client):
    """A client that sends the emptied box rather than the flag still gets null."""
    euros, pounds = _two_accounts(client)

    client.patch(
        f"/api/accounts/{pounds['id']}", json={"institution": "   ", "note": ""}, headers=HEADERS
    )

    assert _stored(client, pounds) == (None, None)
    assert _stored(client, euros) == ("Example Bank", "The one the salaries land in")


def test_a_value_and_its_clear_flag_together_are_refused_and_change_nothing(client):
    euros, pounds = _two_accounts(client)

    set_up = client.patch(
        f"/api/accounts/{euros['id']}",
        json={"country": "ES", "statement_product": "Current"},
        headers=HEADERS,
    )
    assert set_up.status_code == 200, set_up.text

    for body in (
        {"institution": "New Bank", "clear_institution": True},
        {"note": "a note", "clear_note": True},
        # These two let the flag win in silence until #110.
        {"country": "PT", "clear_country": True},
        {"statement_product": "Savings", "clear_statement_product": True},
    ):
        refused = client.patch(f"/api/accounts/{euros['id']}", json=body, headers=HEADERS)
        assert refused.status_code == 422, refused.text
        assert "not both" in refused.text

    assert _stored(client, euros) == ("Example Bank", "The one the salaries land in")
    got = client.get(f"/api/accounts/{euros['id']}").json()
    assert (got["country"], got["statement_product"]) == ("ES", "Current")
    assert client.get(f"/api/accounts/{pounds['id']}").json()["country"] is None


def test_the_length_limit_is_on_the_value_as_sent(client):
    """The panel's `maxLength` counts what is typed, spaces and all; so does the API."""
    euros, pounds = _two_accounts(client)

    fits = client.patch(
        f"/api/accounts/{euros['id']}", json={"institution": " " + "B" * 118 + " "}, headers=HEADERS
    )
    over = client.patch(
        f"/api/accounts/{pounds['id']}", json={"institution": " " + "B" * 119 + " "}, headers=HEADERS
    )

    assert fits.status_code == 200, fits.text
    assert _stored(client, euros)[0] == "B" * 118
    assert over.status_code == 422, over.text
    assert _stored(client, pounds)[0] == "Other Bank"


# --------------------------------------------------------------------------- #
# The migration
# --------------------------------------------------------------------------- #

BEFORE = "d3887ad24c50"
FOLD = "2bec6ce88f3d"


def test_the_migration_folds_blank_banks_and_notes_to_null_and_trims_the_rest(
    tmp_path, monkeypatch
):
    from alembic import command

    from tests.test_migrations import _config

    url = f"sqlite:///{tmp_path / 'before.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _config(url)
    command.upgrade(cfg, BEFORE)

    engine = create_engine(url)
    now = "2026-01-01 00:00:00"
    rows = {
        # id: (household, currency, institution, note)
        "a1": ("h1", "EUR", "", "   "),
        "a2": ("h1", "GBP", "  Example Bank ", "\tKeep\nthe inside\n"),
        "a3": ("h2", "EUR", None, None),
        "a4": ("h2", "GBP", "Other Bank", "Rainy days"),
        "a5": ("h2", "EUR", " ", ""),
    }
    with engine.begin() as conn:
        for house in ("h1", "h2"):
            conn.execute(
                text(
                    "INSERT INTO households (id, name, base_currency, date_format, theme,"
                    " receipts_keep_original, created_at, updated_at)"
                    " VALUES (:id, :id, 'EUR', 'YYYY-MM-DD', 'moss', 0, :now, :now)"
                ),
                {"id": house, "now": now},
            )
        for account_id, (house, currency, institution, note) in rows.items():
            conn.execute(
                text(
                    "INSERT INTO accounts (id, household_id, name, type, currency, closed,"
                    " institution, note, sort_order, created_at, updated_at)"
                    " VALUES (:id, :h, :id, 'checking', :c, 0, :i, :n, 0, :now, :now)"
                ),
                {"id": account_id, "h": house, "c": currency, "i": institution, "n": note,
                 "now": now},
            )

    command.upgrade(cfg, FOLD)
    with engine.connect() as conn:
        after = {
            row[0]: (row[1], row[2])
            for row in conn.execute(text("SELECT id, institution, note FROM accounts"))
        }
    assert after == {
        "a1": (None, None),
        "a2": ("Example Bank", "Keep\nthe inside"),
        "a3": (None, None),
        "a4": ("Other Bank", "Rainy days"),
        "a5": (None, None),
    }

    # Nothing to put back: the downgrade leaves the folded rows as they are.
    command.downgrade(cfg, BEFORE)
    with engine.connect() as conn:
        back = {
            row[0]: (row[1], row[2])
            for row in conn.execute(text("SELECT id, institution, note FROM accounts"))
        }
    engine.dispose()
    assert back == after
