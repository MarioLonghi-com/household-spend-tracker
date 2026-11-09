"""An agent adds categories and deletes an unused one, behind its own permission (#366).

Two households and four keys: in Home one key that may edit categories, one
that may write but not that, and one read key; in Away a key that may edit
categories there. Every test asserts what the category tree holds afterwards,
not only the status code.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import deps
from app.audit.batch import batch
from app.models import (
    AgentScope,
    Batch,
    BatchKind,
    Category,
    CategoryGroup,
    Household,
    Payee,
    User,
)
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner

V1 = "/api/agent/v1"


@pytest.fixture()
def world(client):
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    away = client.post("/api/households", json={"name": "Away"}, headers=HEADERS).json()

    tokens = {}
    keys = {}
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, made["user"]["id"])
        home = own.get(Household, house["id"])
        there = own.get(Household, away["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=home.id):
            keys["editor"], tokens["editor"] = key_service.issue(
                own, user=user, household=home, label="category tidier",
                scope=AgentScope.write, may_edit_categories=True,
            )
            keys["writer"], tokens["writer"] = key_service.issue(
                own, user=user, household=home, label="categoriser", scope=AgentScope.write,
            )
            keys["reader"], tokens["reader"] = key_service.issue(
                own, user=user, household=home, label="the analyst",
            )
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=there.id):
            keys["theirs"], tokens["theirs"] = key_service.issue(
                own, user=user, household=there, label="away tidier",
                scope=AgentScope.write, may_edit_categories=True,
            )
        own.commit()

    return {
        "client": client, "house": house, "away": away, "tokens": tokens,
        "key_ids": {name: key.id for name, key in keys.items()},
        "user_id": made["user"]["id"],
    }


def _call(world, method, path, *, key="editor", house="house", **kwargs):
    return world["client"].request(
        method,
        f"{V1}/households/{world[house]['id']}{path}",
        headers={"authorization": f"Bearer {world['tokens'][key]}", **HEADERS},
        **kwargs,
    )


def _db(world) -> Session:
    return Session(world["client"].app_module.db_engine, expire_on_commit=False)


def _names(world, where="house") -> set[str]:
    with _db(world) as own:
        return set(
            own.execute(
                select(Category.name).where(Category.household_id == world[where]["id"])
            ).scalars()
        )


def _group_id(world, name, where="house") -> str:
    with _db(world) as own:
        return own.execute(
            select(CategoryGroup.id).where(
                CategoryGroup.household_id == world[where]["id"], CategoryGroup.name == name
            )
        ).scalar_one()


def _undo(world, batch_id):
    return world["client"].post(
        f"/api/households/{world['house']['id']}/batches/{batch_id}/undo", headers=HEADERS
    )


def test_a_key_with_the_permission_adds_a_category_and_history_names_it(world):
    everyday = _group_id(world, "Everyday")
    made = _call(world, "POST", "/categories", json={"name": "Pet food", "group_id": everyday})

    assert made.status_code == 201, made.text
    body = made.json()
    assert body["full_name"] == "Everyday: Pet food"
    assert body["made_group"] is False
    assert "Pet food" in _names(world)
    assert "Pet food" not in _names(world, "away")
    with _db(world) as own:
        acted = own.get(Batch, body["batch_id"])
        assert acted.agent_key_id == world["key_ids"]["editor"]
        assert acted.actor_id == world["user_id"]
        assert acted.kind is BatchKind.admin
    history = world["client"].get(
        f"/api/households/{world['house']['id']}/batches", headers=HEADERS
    ).json()
    listed = next(one for one in history if one["id"] == body["batch_id"])
    assert "category tidier" in str(listed), "History names the key that acted"

    assert _undo(world, body["batch_id"]).status_code == 200
    assert "Pet food" not in _names(world)


def test_a_group_named_that_does_not_exist_is_made_with_it_and_one_undo_takes_both(world):
    made = _call(world, "POST", "/categories", json={"name": "Vet", "group_name": "Pets"})

    assert made.status_code == 201, made.text
    assert made.json()["made_group"] is True
    assert made.json()["full_name"] == "Pets: Vet"
    pets = _group_id(world, "Pets")
    again = _call(world, "POST", "/categories", json={"name": "Grooming", "group_name": "pets"})
    assert again.json()["made_group"] is False
    assert again.json()["group_id"] == pets

    assert _undo(world, again.json()["batch_id"]).status_code == 200
    assert _undo(world, made.json()["batch_id"]).status_code == 200
    assert not {"Vet", "Grooming"} & _names(world)
    with _db(world) as own:
        assert own.execute(
            select(CategoryGroup).where(CategoryGroup.name == "Pets")
        ).first() is None


def test_a_name_already_taken_is_refused_with_the_screens_code(world):
    before = _names(world)
    taken = _call(world, "POST", "/categories", json={"name": "groceries", "group_name": "Food"})

    # An agent is answered in words, not codes (`main._speaks_to_agent`).
    assert taken.status_code == 409, taken.text
    assert "already a category called 'groceries'" in taken.json()["detail"]
    assert _names(world) == before
    both = _call(
        world, "POST", "/categories",
        json={"name": "X", "group_name": "Food", "group_id": _group_id(world, "Bills")},
    )
    assert both.status_code == 422
    assert "group_id or by group_name" in both.json()["detail"]


@pytest.mark.parametrize("key", ["writer", "reader"])
def test_a_key_without_the_permission_is_refused_and_nothing_changes(world, key):
    before = _names(world)
    unused = next(iter(sorted(before)))
    with _db(world) as own:
        unused_id = own.execute(
            select(Category.id).where(
                Category.household_id == world["house"]["id"], Category.name == unused
            )
        ).scalar_one()

    made = _call(world, "POST", "/categories", key=key, json={"name": "Pet food", "group_name": "Pets"})
    gone = _call(world, "POST", f"/categories/{unused_id}/delete", key=key)

    assert made.status_code == gone.status_code == 403
    if key == "writer":
        assert "may not add or delete categories" in made.json()["detail"]
    assert _names(world) == before


def test_another_households_category_or_household_is_a_404(world):
    theirs = _names(world, "away")
    with _db(world) as own:
        their_groceries = own.execute(
            select(Category.id).where(
                Category.household_id == world["away"]["id"], Category.name == "Groceries"
            )
        ).scalar_one()
        their_group = _group_id(world, "Everyday", "away")

    deleting = _call(world, "POST", f"/categories/{their_groceries}/delete")
    into_their_group = _call(world, "POST", "/categories", json={"name": "Odd", "group_id": their_group})
    their_house = _call(world, "POST", "/categories", house="away", json={"name": "Odd", "group_name": "X"})

    assert deleting.status_code == 404
    assert into_their_group.status_code == 404
    assert their_house.status_code == 404
    assert _names(world, "away") == theirs
    assert "Odd" not in _names(world)

    # Their own key does it there, and only there.
    own_key = _call(world, "POST", "/categories", key="theirs", house="away",
                    json={"name": "Odd", "group_name": "Misc"})
    assert own_key.status_code == 201
    assert "Odd" in _names(world, "away") and "Odd" not in _names(world)


def test_deleting_an_unused_category_removes_it_and_undo_brings_it_back(world):
    with _db(world) as own:
        water = own.execute(
            select(Category).where(
                Category.household_id == world["house"]["id"], Category.name == "Water"
            )
        ).scalar_one()
        with batch(own, kind=BatchKind.admin, actor_id=world["user_id"],
                   household_id=world["house"]["id"]):
            fixed = Payee(
                household_id=world["house"]["id"], name="Canal", name_folded="canal",
                categorisation="fixed", default_category_id=water.id,
            )
            own.add(fixed)
        own.commit()

    gone = _call(world, "POST", f"/categories/{water.id}/delete")

    assert gone.status_code == 200, gone.text
    assert gone.json()["full_name"] == "Bills: Water"
    assert "Water" not in _names(world)
    with _db(world) as own:
        assert own.get(Payee, fixed.id).default_category_id is None

    assert _undo(world, gone.json()["batch_id"]).status_code == 200
    assert "Water" in _names(world)
    with _db(world) as own:
        assert own.get(Payee, fixed.id).default_category_id == water.id


def test_deleting_a_category_that_carries_rows_is_refused_and_it_stays(world):
    client = world["client"]
    account = client.post(
        f"/api/households/{world['house']['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()["id"]
    with _db(world) as own:
        groceries = own.execute(
            select(Category.id).where(
                Category.household_id == world["house"]["id"], Category.name == "Groceries"
            )
        ).scalar_one()
    row = client.post(
        f"/api/households/{world['house']['id']}/transactions",
        json={"account_id": account, "date": "2026-07-03", "amount": -1850,
              "payee_name": "Mercadona", "category_id": groceries},
        headers=HEADERS,
    )
    assert row.status_code == 201, row.text

    refused = _call(world, "POST", f"/categories/{groceries}/delete")

    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("1 transaction is categorised as 'Groceries'")
    assert "Archive it instead" in refused.json()["detail"]
    assert "Groceries" in _names(world)


def test_the_permission_is_chosen_at_issue_shown_in_the_list_and_in_the_manifest(world):
    client = world["client"]
    manifests = {
        name: client.get(
            f"{V1}/manifest", headers={"authorization": f"Bearer {world['tokens'][name]}"}
        ).json()["key"]
        for name in ("editor", "writer")
    }
    assert manifests["editor"]["may_edit_categories"] is True
    assert manifests["writer"]["may_edit_categories"] is False

    listed = {key["label"]: key for key in client.get("/api/me/keys", headers=HEADERS).json()}
    assert listed["category tidier"]["may_edit_categories"] is True
    assert listed["categoriser"]["may_edit_categories"] is False

    with _db(world) as own:
        user = own.get(User, world["user_id"])
        home = own.get(Household, world["house"]["id"])
        from app.errors import ValidationError

        with pytest.raises(ValidationError) as refused, batch(
            own, kind=BatchKind.admin, actor_id=user.id, household_id=home.id
        ):
            key_service.issue(own, user=user, household=home, label="odd", may_edit_categories=True)
        assert refused.value.code == "agent_key.read_only_categories"


def test_the_delete_route_is_named_and_held_to_the_permission(client):
    """A method-based walk cannot see that a POST deletes, so the route is named."""
    from tests.test_agent_access import _api_routes, _dependencies_of

    app = client.app_module.app
    deleting = [
        route for route in _api_routes(app)
        if deps.current_agent in _dependencies_of(route)
        and "/categories" in route.served_path
        and route.served_path.rstrip("/").endswith("/delete")
    ]
    assert deleting, "no agent category delete route found; this check is vacuous"
    assert all(deps.agent_may_edit_categories in _dependencies_of(r) for r in deleting)
    creating = [
        route for route in _api_routes(app)
        if deps.current_agent in _dependencies_of(route)
        and route.served_path.rstrip("/").endswith("/categories")
        and "POST" in (route.methods or ())
    ]
    assert creating
    assert all(deps.agent_may_edit_categories in _dependencies_of(r) for r in creating)
