"""Request dependencies: who is asking, and what they are allowed to see.

The asymmetry here is deliberate. ``current_household`` and ``load_for`` answer
**404**, because the caller has not proved the thing exists and must not learn
that it does. ``require_owner`` answers **403**, because by then they have.

There are **two doors**, and they are separate functions rather than two
branches of one. ``current_user`` reads a session cookie; ``current_agent``
reads a bearer token. Keeping them apart is what stops a later edit to the
cookie path from silently widening the token path -- and it is what makes the
capability floor structural: a key can only reach a route that *asks* for
``current_agent``, so "an agent may not delete a transaction" is true because
``DELETE /transactions/{id}`` depends on ``CurrentUser`` and always will.
``test_agent_access.py`` walks the route table and asserts exactly that, so the
floor cannot be widened by accident either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import cookies
from ..auth import sessions as session_service
from ..db import get_session
from ..errors import Forbidden, NotFound, Unauthorized
from ..models import AgentKey, Household, HouseholdMember, Role, User
from ..services import agent_keys as agent_key_service
from ..services import agent_requests as agent_request_service
from ..services import households as household_service

SessionDep = Annotated[Session, Depends(get_session)]


def client_ip(request: Request) -> str | None:
    """The peer uvicorn reports, after its proxy-header middleware.

    That middleware believes `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS`
    (default loopback). Under `make serve` with nothing in front, set it to
    nothing or run with `--no-proxy-headers`, or any local user can name their
    own address; in the container, `compose.yaml` trusts the bridge. See the
    README, *Behind a proxy* (#207).
    """
    return request.client.host if request.client else None


def public_origin(request: Request) -> str:
    """Where people reach this instance: `SPENDTRACKER_PUBLIC_URL` if it is
    set, else what this request says. For links that leave the app (#207)."""
    from .. import config

    return config.settings.public_url or f"{request.url.scheme}://{request.url.netloc}"


def current_user(request: Request, session: SessionDep) -> User:
    row = session_service.lookup(session, cookies.session_value(request.cookies))
    if row is None:
        raise Unauthorized("sign in first")
    user = session.get(User, row.user_id)
    if user is None or user.disabled_at is not None:
        raise Unauthorized("sign in first")
    session_service.touch(session, row)
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_owner(user: CurrentUser) -> User:
    """403 is right here: they are signed in, so they know the thing exists."""
    if user.role is not Role.owner:
        raise Forbidden("only the owner can do that")
    return user


OwnerOnly = Annotated[User, Depends(require_owner)]


def current_household(household_id: str, user: CurrentUser, session: SessionDep) -> Household:
    """The household, if this user is in it -- and 404 in every other case.

    A non-member gets exactly the answer they would get for an id that was never
    real, which is the whole point.
    """
    # The service owns this query. It used to be written out twice, so the four
    # tests that prove the 404-not-403 rule were exercising a copy the API never
    # called, and a fix to either would not have reached the other.
    return household_service.get_for(session, household_id, user)


CurrentHousehold = Annotated[Household, Depends(current_household)]


def load_for[T](session: Session, user: User, model: type[T], object_id: str) -> T:
    """Fetch an object by its own id, but only through a household you are in.

    Every route keyed by an object id rather than a household id uses this
    instead of ``session.get``. One join, and the same 404 as everything else.
    """
    obj = session.execute(
        select(model)
        .join(HouseholdMember, HouseholdMember.household_id == model.household_id)
        .where(model.id == object_id, HouseholdMember.user_id == user.id)
    ).scalar_one_or_none()
    if obj is None:
        raise NotFound(f"no such {model.__tablename__.rstrip('s')}")
    return obj


def device_cookie(request: Request) -> str | None:
    return cookies.device_value(request.cookies)


# --------------------------------------------------------------------------- #
# The other door: a program holding a key
# --------------------------------------------------------------------------- #

#: What a bearer token looks like in the header. Checked as a whole so the CSRF
#: middleware and this dependency agree on what "carries a key" means.
BEARER = "Bearer " + agent_key_service.PREFIX

#: What every agent refusal says and sends, whatever went wrong.
#:
#: `WWW-Authenticate` is how HTTP says *which* credential a route wants, and
#: without it a program pointed at this app has to guess. The sentence points
#: at `/llms.txt`, which is the one page served to somebody holding nothing --
#: so a request that arrives wrong teaches rather than only failing.
#:
#: **It is one constant because it must be one answer.** Malformed, unknown,
#: revoked, expired and owner-disabled all land here, and the whole point of
#: `agent_keys.lookup` returning None for every one of them is that a caller
#: cannot tell which. A second, more helpful sentence for any one of *those*
#: would undo that, so there is nowhere to put one.
#:
#: The two cases `_why_no_key` answers separately are not on that list and
#: never were: "you sent no credential" and "you sent one in a header this API
#: does not read" are facts about the request the caller composed, not about
#: any key. Telling them costs a prober nothing and saves an honest caller a
#: round trip. See `_why_no_key`.
_NO_KEY = "that key is not valid"
_NO_KEY_HEADERS = {
    "WWW-Authenticate": (
        'Bearer realm="spend-tracker", '
        'error="invalid_token", '
        'error_description="see /llms.txt"'
    )
}

def carries_session_cookie(request: Request) -> bool:
    """Whether this request carries a session cookie, under the one name
    `cookies.session_name()` gives it. `main`'s CSRF middleware asks this: the
    cookie's presence is what decides that a request is a browser's, so it and
    `current_user` must agree on the name -- which is why neither spells it."""
    return cookies.session_name() in request.cookies


#: Headers a caller might reasonably have put a key in, and which this API does
#: not read. Naming the one they used is safe in a way naming a key's state is
#: not: it is a fact the caller already holds, so it tells a prober nothing.
_WRONG_HEADERS = ("X-API-Key", "Api-Key", "X-Api-Token", "X-Auth-Token", "Token")

#: The two refusals that are allowed to be specific, because neither is a
#: statement about a key. See `_why_no_key`.
_NO_CREDENTIAL = "no credential. Send `Authorization: Bearer <key>`. See /llms.txt."
_WRONG_HEADER = (
    "found `{header}`; this API reads `Authorization: Bearer <key>`. See /llms.txt."
)


def _presented(request: Request) -> str | None:
    """The token out of the Authorization header, if it looks like one of ours."""
    header = request.headers.get("authorization") or ""
    if not header.startswith(BEARER):
        return None
    return header[len("Bearer ") :].strip() or None


def _why_no_key(request: Request) -> str:
    """Which refusal a request that presented no usable token has earned.

    The uniform answer above is load-bearing only once a credential has arrived
    **in the right place**. `agent_keys.lookup` returning None for absent,
    unknown, revoked, expired and owner-disabled alike is what stops a caller
    probing which -- and nothing here weakens that, because nothing here is a
    statement about a key.

    *Which header the credential arrived in* is information the caller already
    has. Saying "you sent X-API-Key; this API reads Authorization: Bearer"
    reveals nothing a prober did not write themselves, and it saves an honest
    caller a full diagnostic round trip -- which is what it cost during the
    import run that raised issue #38.
    """
    if request.headers.get("authorization"):
        # Present, but not `Bearer stk_...`. That is a claim about the shape of
        # a credential, so it stays on the one answer with all the others.
        return _NO_KEY
    for name in _WRONG_HEADERS:
        if request.headers.get(name):
            return _WRONG_HEADER.format(header=name)
    return _NO_CREDENTIAL


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Who is asking, when the answer is "a program".

    Carries the key *and* the person it acts for, because every batch this
    request opens is attributed to both -- `batches.actor_id` stays the human
    and `agent_key_id` says which key, so History reads "Jane Doe · via
    Claude (receipt filer)" rather than replacing one with the other.
    """

    key: AgentKey
    user: User
    household: Household
    session: Session

    @property
    def may_write(self) -> bool:
        return self.key.scope.may_write

    def batch(self, *, kind, source: dict | None = None):
        """Open a batch attributed to **both** the person and the key.

        Every agent write goes through here rather than calling `batch()`
        directly, so attribution cannot be forgotten at a call site -- which is
        the only way it would ever be missing, and it would be missing silently.

        `actor_id` stays the human: a key borrows their authority and does not
        replace them. The key's name is also written into `source["agent"]`,
        because the foreign key is SET NULL and a key is swept thirty days
        after revocation. The live link answers "which key"; the copy is what
        keeps the sentence readable once the key is gone.
        """
        from ..audit.batch import batch as _batch

        detail = dict(source or {})
        detail["agent"] = {
            "key_id": self.key.id,
            "label": self.key.label,
            "name": self.key.agent_name,
        }
        opened = _batch(
            self.session,
            kind=kind,
            actor_id=self.user.id,
            household_id=self.household.id,
            source=detail,
        )
        return _Attributed(opened, self.key.id)

    def load(self, model: type, object_id: str):
        """Fetch an object by its own id, through *this key's* household.

        The agent twin of `load_for`, and it answers **404** for the same
        reason: a key scoped to household A must not learn that an id in
        household B is real. One household, not the user's membership list --
        a key is narrower than the person holding it, and a key that reached
        everything its owner could reach would not be a capability at all.
        """
        obj = self.session.execute(
            select(model).where(
                model.id == object_id, model.household_id == self.household.id
            )
        ).scalar_one_or_none()
        if obj is None:
            raise NotFound(f"no such {model.__tablename__.rstrip('s')}")
        return obj


def current_agent(request: Request, session: SessionDep) -> AgentContext:
    """The key behind a bearer token, if it is still good.

    One flat 401 for malformed, unknown, revoked, expired and owner-disabled,
    with the same sentence every time -- `agent_keys.lookup` makes them
    indistinguishable and this keeps them that way. The difference is only ever
    useful to somebody guessing.

    A request that presented nothing this API reads is answered by
    `_why_no_key` instead, which names the header it wanted. That is not a
    weakening of the above: it happens strictly *before* any key is looked up,
    and it describes the caller's own request rather than the state of a
    credential.

    Scope is deliberately **not** checked here. By this point the caller has
    proved they hold a real credential, so a scope refusal is a 403 and belongs
    with `require_owner` rather than with the door. (§2.2 of the spec lists it
    as a sixth 401; its own test 5 asks for a 403, and the test is right.)
    """
    presented = _presented(request)
    if presented is None:
        raise Unauthorized(_why_no_key(request), headers=_NO_KEY_HEADERS)

    key = agent_key_service.lookup(session, presented)
    if key is None:
        raise Unauthorized(_NO_KEY, headers=_NO_KEY_HEADERS)

    user = session.get(User, key.user_id)
    household = session.get(Household, key.household_id)
    if user is None or household is None:  # pragma: no cover - both FKs are NOT NULL
        raise Unauthorized(_NO_KEY, headers=_NO_KEY_HEADERS)

    # Before the handler, because the point of a limit is not doing the work.
    # Read-only -- the line for this request is written afterwards, by the
    # middleware that knows what the status turned out to be.
    agent_request_service.check(session, key.id)

    # Sparingly, and without a batch: `last_used_at` is redacted, so the hook
    # sees `before == after` and writes nothing. This is the whole reason that
    # column is in `__audit_redact__`.
    agent_key_service.touch(session, key)

    # Stashed for the recording middleware, which runs after the handler and
    # has no other way to know which key was behind the request.
    request.state.agent_key_id = key.id
    return AgentContext(key=key, user=user, household=household, session=session)


CurrentAgent = Annotated[AgentContext, Depends(current_agent)]


def agent_may_write(agent: CurrentAgent) -> AgentContext:
    """403 is right here: they hold a valid key, so they know the door exists.

    The same asymmetry `require_owner` carries, for the same reason.
    """
    if not agent.may_write:
        raise Forbidden("that key is read-only")
    return agent


AgentWriter = Annotated[AgentContext, Depends(agent_may_write)]


def agent_may_commit(agent: AgentWriter) -> AgentContext:
    """Whether this key may move a staged import from `preview` to `applied`.

    Refused with a 403 whose sentence hands off rather than just failing: an
    agent that cannot commit has still done its work, and the useful answer is
    "it is staged, a person should look at it".
    """
    if not agent.key.may_commit:
        raise Forbidden(
            "that key may stage an import but not apply it. The import is saved as a "
            "preview for somebody to review."
        )
    return agent


AgentCommitter = Annotated[AgentContext, Depends(agent_may_commit)]


def agent_may_split(agent: AgentWriter) -> AgentContext:
    """Whether this key may divide a row into parts.

    A split replaces the row it divides (`transactions.split` ends by deleting
    it), and §1.2 says no key deletes. It is allowed here only because nothing
    is lost -- the parts add up to the row, the before-image is in the audit
    log and one undo puts the original back -- and only for a key a person has
    trusted further: the same `may_commit` that lets a key apply an import
    nobody reviewed (#7, option B). An ordinary write key is told what to hand
    a person instead.
    """
    if not agent.key.may_commit:
        raise Forbidden(
            "that key may not split a transaction: a split replaces the row, so it "
            "needs a key a person has trusted to apply changes without review. Hand "
            "the parts to a person to split in the register instead."
        )
    return agent


AgentSplitter = Annotated[AgentContext, Depends(agent_may_split)]


class _Attributed:
    """`batch()` with the key stamped on the row it opens.

    A thin wrapper rather than a parameter on `audit.batch`: that function is
    the ledger's own, used by every cookie-path write in the app, and it should
    not grow an argument that only one of its two callers can ever supply.
    """

    __slots__ = ("_inner", "_key_id")

    def __init__(self, inner, key_id: str) -> None:
        self._inner = inner
        self._key_id = key_id

    def __enter__(self):
        row = self._inner.__enter__()
        # Only on the batch this call actually opened. A nested `batch()` joins
        # the outer one rather than starting another, and stamping there would
        # relabel somebody else's operation.
        if row.agent_key_id is None:
            row.agent_key_id = self._key_id
        return row

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)
