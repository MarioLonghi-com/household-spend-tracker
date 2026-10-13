"""What this deployment offers: one list, decided by the profile (#316).

The desktop app is the same code as the server, not a fork (#49). The
difference between them is the set of features that need a network, a second
device or a second person -- capturing a receipt on a phone, an agent with a
key, an invitation link, a reset link, a passkey, the self-updater beside a
container. On the desktop there is nobody and nothing to offer them to.

So each such feature is a **capability**, and this module is the only place
that says which profile has which. Everything else asks here:

- **The server** leaves out the routes of a capability that is off, at import,
  before the app serves anything: a router it is the whole of is never
  included, and a route of the app's own is removed (`prune`). An off feature then answers what a
  route that never existed answers -- not a 403 and not a refusal of its own,
  so nobody learns it is there (the same rule as a household you are not a
  member of: 404, not 403).
- **The client** reads `GET /api/capabilities` once and hides what is off,
  through the same names (`client/src/lib/capabilities.ts`).

A screen never asks which profile it is in. A new feature that only makes
sense on a network gets a capability here and a tag on its routes; the test
holds every route under the agent prefix to it, and every capability that
removes routes to having some.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TypeVar

from .config import Settings

#: The attribute a route's endpoint carries to say which capability it needs.
_TAG = "__spendtracker_capability__"

F = TypeVar("F", bound=Callable)


@dataclass(frozen=True)
class Capabilities:
    #: `/snap`: the capture page for a phone at a till.
    snap: bool
    #: The agent API, agent keys in Profile, `/llms.txt` and the descriptor.
    agents: bool
    #: Inviting someone into a household, and accepting an invitation.
    invitations: bool
    #: An owner issuing a reset link, and the link being redeemed.
    account_resets: bool
    #: Passkeys can be offered at all: a relying-party ID is configured. Whether
    #: this request can use one is `auth.passkeys.state`'s question.
    passkeys: bool
    #: Sign-in asks for an authenticator code after the password. Off on the
    #: desktop, where a password opens the app (#320).
    second_factor: bool
    #: The self-updater beside a container. The stores update the desktop app.
    self_update: bool
    #: The check for a newer release on GitHub, which only reads.
    upstream_check: bool
    #: The read-only database browser at `/db`.
    db_view: bool
    #: The one-time import from YNAB's API, which only reads.
    ynab_import: bool

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


def for_settings(settings: Settings) -> Capabilities:
    server = settings.profile == "server"
    return Capabilities(
        snap=server,
        agents=server,
        invitations=server,
        account_resets=server,
        passkeys=server and bool(settings.rp_id),
        second_factor=server,
        self_update=server,
        upstream_check=True,
        db_view=server and str(settings.db_view).lower() not in {"off", "0", "false", "no"},
        ynab_import=True,
    )


#: Every capability name, for checking a tag against.
NAMES = frozenset(Capabilities.__dataclass_fields__)


def needs(capability: str) -> Callable[[F], F]:
    """Mark a route's endpoint as needing `capability`. Order against the
    router's own decorator does not matter: the mark is on the function."""
    if capability not in NAMES:
        raise ValueError(f"no capability called {capability!r}")

    def mark(endpoint: F) -> F:
        setattr(endpoint, _TAG, capability)
        return endpoint

    return mark


def needed_by(route) -> str | None:
    """The capability a route needs, or None for one every profile serves."""
    return getattr(getattr(route, "endpoint", None), _TAG, None)


def prune(app, capabilities: Capabilities) -> list[str]:
    """Remove every route of the app's own whose capability is off. Returns
    their paths.

    The app's own routes only -- `/snap`, `/llms.txt` -- because those belong
    to this app object, which a test builds afresh by reloading `app.main`. A
    router's routes belong to a module-level router every app shares, and
    FastAPI includes a router by reference; one that is off is simply not
    included (`app.main._ROUTERS`).

    In place, on the app's own route list, so it must run once all routes are
    registered and before the first request: Starlette resolves each request
    against that list as it is then.
    """
    offered = capabilities.as_dict()
    kept, removed = [], []
    for route in app.router.routes:
        need = needed_by(route)
        if need is None or offered[need]:
            kept.append(route)
        else:
            removed.append(getattr(route, "path", repr(route)))
    app.router.routes[:] = kept
    return removed
