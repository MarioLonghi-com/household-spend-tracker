"""Categories an agent may add, and an unused one it may delete (#366).

An agent that meets spend no category fits used to have to stop and ask a
person to make one. These two routes let a key do it itself -- **only a key a
person allowed to**, with `may_edit_categories`, which `write` does not carry:
tagging last month's rows and changing the household's category tree are
different kinds of act (`deps.agent_may_edit_categories`).

Same door as `agent.py`: every route hangs off `current_agent`. Both go
through `app/services/categories.py`, the screen's own writer, inside one
batch attributed to the person and the key, so each is an entry in History
that undo takes back, exactly like `POST /households/{id}/categories` and
`DELETE /categories/{id}` on the Categories screen.

**Delete is a POST, and an exception to §1.2's "no key deletes"** on the
split's argument: nothing is lost. Only a category nothing carries can go --
otherwise the answer is archive, which stays a person's -- and one undo puts
it back. `test_agent_access.py` names the route and holds it to the
permission, because a method-based walk cannot see that a POST deletes.

**Endpoints join `agent.ENDPOINTS` at import**, like the other agent modules.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ...models import BatchKind
from ...schemas import ManifestEndpoint
from ...services import categories as category_service
from ..deps import AgentCategoryEditor
from . import agent as agent_api

router = APIRouter(prefix=f"/agent/v{agent_api.API_VERSION}", tags=["agent"])

_BASE = f"/api/agent/v{agent_api.API_VERSION}"


class AgentCategoryCreate(BaseModel):
    """A new category, in a group named by id or by name.

    `group_name` uses the household's group of that name (case aside) and
    makes it when there is none. Exactly one of the two.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    group_id: str | None = Field(default=None, min_length=1, max_length=64)
    group_name: str | None = Field(default=None, min_length=1, max_length=120)


class AgentCategoryMade(BaseModel):
    batch_id: str
    id: str
    #: "Food: Groceries" -- the form the manifest lists it under.
    full_name: str
    group_id: str
    #: True when this request made the group as well.
    made_group: bool


class AgentCategoryDeleted(BaseModel):
    batch_id: str
    id: str
    full_name: str


@router.post(
    "/households/{household_id}/categories", response_model=AgentCategoryMade, status_code=201
)
def create_category(
    household_id: str, body: AgentCategoryCreate, agent: AgentCategoryEditor, request: Request
) -> AgentCategoryMade:
    """Add a category, and its group if the request names one that is not there.

    One batch, so the category and a group made with it are one entry in
    History and one undo. A name already taken is refused with the screen's
    sentence -- the manifest is where an agent finds the one that is already
    there.
    """
    house = agent_api._house(agent, household_id)
    with agent.batch(kind=BatchKind.admin) as acting:
        category, made = category_service.create_category_in_group(
            agent.session,
            house.id,
            name=body.name,
            group_id=body.group_id,
            group_name=body.group_name,
        )
        agent.session.flush()
        batch_id = acting.id
    request.state.agent_rows = 1
    request.state.agent_batch_id = batch_id
    return AgentCategoryMade(
        batch_id=batch_id,
        id=category.id,
        full_name=category.full_name,
        group_id=category.group_id,
        made_group=made is not None,
    )


@router.post(
    "/households/{household_id}/categories/{category_id}/delete",
    response_model=AgentCategoryDeleted,
)
def delete_category(
    household_id: str, category_id: str, agent: AgentCategoryEditor, request: Request
) -> AgentCategoryDeleted:
    """Delete a category nothing carries. Anything carrying it: refused, archive.

    A 409 with the count when a row carries it (`category.in_use`), and the
    category is left exactly as it was; archiving it is a person's, on the
    Categories screen. Another household's category is a 404, the same as an
    invented id. A payee whose fixed choice it was is cleared in the same
    batch, as on the screen, so the one undo restores that too.
    """
    house = agent_api._house(agent, household_id)
    session = agent.session
    category = category_service.get_for_household(session, category_id, house.id)
    full_name = category.full_name
    with agent.batch(kind=BatchKind.admin) as acting:
        category_service.delete_category(session, category)
        session.flush()
        batch_id = acting.id
    request.state.agent_rows = 1
    request.state.agent_batch_id = batch_id
    return AgentCategoryDeleted(batch_id=batch_id, id=category_id, full_name=full_name)


ENDPOINTS: list[ManifestEndpoint] = [
    ManifestEndpoint(
        method="POST",
        path=f"{_BASE}/households/{{household_id}}/categories",
        says=(
            "Add a category, as one act with one undo. Needs a key with "
            "may_edit_categories. Send {name, group_id} or {name, group_name}: a "
            "group_name that does not exist yet is made too. A name already taken is "
            "refused (409): use the one the manifest lists."
        ),
        returns="{batch_id, id, full_name, group_id, made_group}",
        scope="write",
    ),
    ManifestEndpoint(
        method="POST",
        path=f"{_BASE}/households/{{household_id}}/categories/{{category_id}}/delete",
        says=(
            "Delete a category nothing carries, as one act with one undo. Needs a key "
            "with may_edit_categories. If any transaction carries it, it is refused "
            "(409, with how many rows carry it) and stays: archiving it is a person's."
        ),
        returns="{batch_id, id, full_name}",
        scope="write",
    ),
]

agent_api.ENDPOINTS.extend(ENDPOINTS)
