"""A category group can be renamed, and deleted while it is empty (#184).

Over HTTP, because what the issue asks for is what the screen can do: press the
heading, change the label, or delete a group with nothing under it and get it
back from History's Undo. The delete is a hard delete -- CLAUDE.md, "Hard
deletes only" -- so "restorable only through Undo" is proven by undoing it and
reading the row back, not by looking for a flag.

Two households throughout: one the caller is in with a group of the same name,
to prove each act touched only its own row, and one the caller is not in, to
prove a stranger's group answers 404.
"""

from __future__ import annotations

from sqlalchemy import select

from app.audit.batch import batch
from app.models import BatchKind, Category, CategoryGroup, Role
from tests.conftest import HEADERS, _bootstrap_user, _setup_owner


def _house(client, name: str) -> dict:
    """A household with the starter tree, which creating one already gives it."""
    house = client.post("/api/households", json={"name": name}, headers=HEADERS).json()
    assert "Everyday" in _tree(client, house)
    return house


def _tree(client, house: dict, *, archived: bool = False) -> dict[str, dict]:
    query = "?include_archived=true" if archived else ""
    got = client.get(f"/api/households/{house['id']}/categories{query}", headers=HEADERS)
    assert got.status_code == 200, got.text
    return {group["name"]: group for group in got.json()}


def _empty_group(client, house: dict, name: str) -> dict:
    made = client.post(
        f"/api/households/{house['id']}/category-groups", json={"name": name}, headers=HEADERS
    )
    assert made.status_code == 201, made.text
    return made.json()


def _stranger_group(name: str) -> str:
    """A group in a household the signed-in owner is not a member of."""
    import app.db as db
    from app.services import households as household_service

    with db.SessionLocal() as session:
        stranger = _bootstrap_user(
            session, email="other@example.com", name="Other", role=Role.member
        )
        with batch(session, kind=BatchKind.admin, actor_id=stranger.id):
            theirs = household_service.create_household(session, name="Theirs", creator=stranger)
            session.flush()
            group = CategoryGroup(household_id=theirs.id, name=name, sort_order=0)
            session.add(group)
        session.commit()
        return group.id


def _group_named(group_id: str) -> str | None:
    import app.db as db

    with db.SessionLocal() as session:
        found = session.get(CategoryGroup, group_id)
        return found.name if found else None


def test_renaming_a_group_changes_its_label_and_nothing_else(client):
    _setup_owner(client)
    ours = _house(client, "Ours")
    twin = _house(client, "Twin")
    bills = _tree(client, ours)["Bills"]
    their_bills = _tree(client, twin)["Bills"]

    renamed = client.patch(
        f"/api/category-groups/{bills['id']}", json={"name": "  Fixed costs "}, headers=HEADERS
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Fixed costs"

    after = _tree(client, ours)
    assert "Bills" not in after
    assert after["Fixed costs"]["id"] == bills["id"], "renamed in place, not replaced"
    assert [one["id"] for one in after["Fixed costs"]["categories"]] == [
        one["id"] for one in bills["categories"]
    ], "its categories stayed under it"
    assert _tree(client, twin)["Bills"]["id"] == their_bills["id"], "the other household's group"


def test_a_rename_onto_another_groups_name_is_refused_and_changes_nothing(client):
    _setup_owner(client)
    ours = _house(client, "Ours")
    bills = _tree(client, ours)["Bills"]

    clash = client.patch(
        f"/api/category-groups/{bills['id']}", json={"name": "everyday"}, headers=HEADERS
    )
    assert clash.status_code == 409
    assert "already a group called" in clash.json()["detail"]
    assert _group_named(bills["id"]) == "Bills"


def test_an_empty_group_is_deleted_and_history_undo_brings_it_back(client):
    _setup_owner(client)
    ours = _house(client, "Ours")
    twin = _house(client, "Twin")
    spare = _empty_group(client, ours, "Spare")
    their_spare = _empty_group(client, twin, "Spare")

    gone = client.delete(f"/api/category-groups/{spare['id']}", headers=HEADERS)
    assert gone.status_code == 204, gone.text
    assert "Spare" not in _tree(client, ours)
    assert _group_named(spare["id"]) is None, "a hard delete: the row is gone, not flagged"
    assert _group_named(their_spare["id"]) == "Spare", "only this household's"

    # On the History screen as it opens -- no `include_single_edits` -- because
    # Undo there is the only way back and it has to be findable.
    history = client.get(f"/api/households/{ours['id']}/batches", headers=HEADERS).json()
    removal = history[0]
    assert "category group" in (removal["headline"] + removal["detail"]).lower(), removal

    undone = client.post(
        f"/api/households/{ours['id']}/batches/{removal['id']}/undo", headers=HEADERS
    )
    assert undone.status_code == 200, undone.text

    back = _tree(client, ours)["Spare"]
    assert back["id"] == spare["id"], "the same row, from its before-image"
    assert back["sort_order"] == spare["sort_order"]
    assert _group_named(spare["id"]) == "Spare"


def test_a_group_with_categories_under_it_is_refused_and_left_whole(client):
    _setup_owner(client)
    ours = _house(client, "Ours")
    before = _tree(client, ours)
    everyday = before["Everyday"]

    refused = client.delete(f"/api/category-groups/{everyday['id']}", headers=HEADERS)
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail.startswith(
        "A group can only be deleted when there are no categories under it."
    ), detail
    assert "'Everyday' still holds 5 categories" in detail

    assert _tree(client, ours) == before, "a refusal leaves every group and category as it was"


def test_archived_categories_count_as_being_under_the_group(client):
    """The screen hides archived categories by default, so a group can look
    empty there and not be. The server is what decides, and says why."""
    _setup_owner(client)
    ours = _house(client, "Ours")
    spare = _empty_group(client, ours, "Spare")
    kept = client.post(
        f"/api/households/{ours['id']}/categories",
        json={"group_id": spare["id"], "name": "Old things"},
        headers=HEADERS,
    ).json()
    client.patch(f"/api/categories/{kept['id']}", json={"archived": True}, headers=HEADERS)
    assert _tree(client, ours)["Spare"]["categories"] == [], "looks empty on the screen"

    refused = client.delete(f"/api/category-groups/{spare['id']}", headers=HEADERS)
    assert refused.status_code == 409
    assert "1 category, 1 of them archived" in refused.json()["detail"]

    import app.db as db

    with db.SessionLocal() as session:
        still = session.execute(
            select(Category).where(Category.group_id == spare["id"])
        ).scalar_one()
        assert still.name == "Old things"
    assert _group_named(spare["id"]) == "Spare"


def test_a_group_in_another_household_is_404_and_untouched(client):
    _setup_owner(client)
    _house(client, "Ours")
    theirs = _stranger_group("Private")

    renamed = client.patch(
        f"/api/category-groups/{theirs}", json={"name": "Mine now"}, headers=HEADERS
    )
    deleted = client.delete(f"/api/category-groups/{theirs}", headers=HEADERS)
    unknown = client.delete(f"/api/category-groups/{'f' * 32}", headers=HEADERS)

    assert renamed.status_code == 404
    assert deleted.status_code == 404
    assert deleted.json() == unknown.json(), "a real id reads exactly like a made-up one"
    assert _group_named(theirs) == "Private"
