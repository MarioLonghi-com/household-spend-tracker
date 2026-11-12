"""The first-boot wizard.

Reachable only while the instance is fresh. Once an owner exists, the status
read says ``setup_required: false`` and nothing more, and every step answers
409 "this instance has already been set up".
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Request, Response

from ... import db
from ...auth import cookies, totp
from ...auth import setup as setup_service
from ...errors import Conflict
from ...schemas import (
    PasskeyOffer,
    PasskeyOptionsFor,
    SetupBegin,
    SetupComplete,
    SetupEnrol,
    SetupStarted,
    UserOut,
)
from ..deps import SessionDep, client_ip

router = APIRouter(tags=["setup"])


@router.get("/setup/state")
def setup_state(session: SessionDep) -> dict:
    """Whether setup is needed, and -- while it is -- where the token was written.

    The path is sent because the screen used to say `data/setup-token`, which
    has been wrong for every container since the image existed and wrong for
    any install since the default data directory moved out of the checkout. A
    hard-coded path in a instruction is a guess about somebody else's machine.

    It is **not** a secret and the token is: the path says which file to open,
    and opening it still requires being on the box. It is only sent while the
    instance is unclaimed, so a configured deployment answers with nothing
    beyond `setup_required: false`.
    """
    required = not setup_service.is_configured(session)
    if not required:
        return {"setup_required": False}
    wizard = _wizard_facts()
    if not setup_service.token_needed():
        # The desktop: no token to find (#320).
        return {"setup_required": True, "token_needed": False, "token_path": None, **wizard}
    return {
        "setup_required": True,
        "token_needed": True,
        "token_path": str(setup_service.setup_token_path()),
        **wizard,
    }


def _wizard_facts() -> dict:
    """What the first-run wizard draws from the server (#458), sent only while
    setup is needed, like `token_path`.

    - ``countries``: each with the base currency, date format and week start
      the region step suggests (`countries.suggestions`).
    - ``currencies``: every code a new household may be given.
    - ``palettes``: the household colours, with the one a first household
      would get preselected (``next_palette``).
    - ``starter``: the starter categories by seed id, the Essentials preset,
      and the two the reports need; the client words them from its catalogs.
    - ``install``: this installation, for the Technical details panel: where
      the data is, and which build. Paths, not secrets: reading any of them
      still takes being on the machine.
    """
    from ... import __version__, countries, currencies, paths, seed_words, theming
    from ...build import current, short

    if not setup_service.token_needed():
        edition = {"darwin": "mac", "win32": "windows"}.get(sys.platform, "desktop")
    else:
        edition = "web"
    return {
        "countries": countries.suggestions(),
        "currencies": sorted(currencies.CURRENT),
        "palettes": [
            {"key": one.key, "label": one.label, "light": one.light.accent, "dark": one.dark.accent}
            for one in theming.PALETTES
        ],
        "next_palette": theming.next_palette([]),
        "starter": {
            "tree": [[group, list(ids)] for group, ids in seed_words.TREE],
            "essentials": list(seed_words.ESSENTIALS),
            "required": list(seed_words.REQUIRED),
        },
        "install": {
            "edition": edition,
            "data_dir": str(paths.data_dir()),
            "version": __version__,
            "commit": short(current()),
        },
    }


@router.post("/setup/begin", response_model=SetupStarted)
def begin(body: SetupBegin, session: SessionDep) -> SetupStarted:
    """Prove you deployed this, and name the owner. Nothing is written."""
    if setup_service.is_configured(session):
        raise Conflict("this instance has already been set up", code="setup.already_done")

    blob = setup_service.begin(
        body.token, email=body.email, display_name=body.display_name, password=body.password
    )
    return SetupStarted(
        blob=setup_service.seal(blob),
        otpauth_uri=totp.provisioning_uri(blob.totp_secret, email=blob.email) if blob.totp_secret else None,
        secret=blob.totp_secret or None,
        recovery_codes=list(blob.recovery_codes),
    )


@router.post("/setup/enrol")
def enrol(body: SetupEnrol, session: SessionDep) -> dict:
    """Prove the authenticator actually pairs, before it is load-bearing."""
    if setup_service.is_configured(session):
        raise Conflict("this instance has already been set up", code="setup.already_done")
    blob = setup_service.unseal(body.blob)
    confirmed = setup_service.confirm_authenticator(blob, body.code)
    return {"blob": setup_service.seal(confirmed)}


@router.post("/setup/passkey/options", response_model=PasskeyOffer)
def passkey_options(body: PasskeyOptionsFor, request: Request, session: SessionDep) -> PasskeyOffer:
    """A passkey in place of the authenticator (#456): creation options for
    the owner the blob names, under a provisional user handle sealed into the
    blob handed back. Refused, with the reason, where passkeys cannot work."""
    if setup_service.is_configured(session):
        raise Conflict("this instance has already been set up", code="setup.already_done")
    blob = setup_service.unseal(body.blob)
    if blob.invitation_id:
        raise Conflict(
            "that is an invitation; finish it from the link you were sent",
            code="setup.is_an_invitation",
        )
    offered, options = setup_service.offer_passkey(session, request, blob)
    return PasskeyOffer(blob=setup_service.seal(offered), options=options)


@router.post("/setup/complete", response_model=UserOut)
def complete(
    body: SetupComplete, request: Request, response: Response, session: SessionDep
) -> UserOut:
    """One transaction: the owner, their codes, the instance, and a signed-in
    browser. Or nothing at all."""
    blob = setup_service.unseal(body.blob)
    if blob.invitation_id:
        raise Conflict(
            "that is an invitation; finish it from the link you were sent",
            code="setup.is_an_invitation",
        )
    setup_service.require_codes_saved(body.codes_saved)
    if setup_service.is_configured(session):
        raise Conflict("this instance has already been set up", code="setup.already_done")
    passkey = (
        setup_service.verify_passkey(db.engine, request, blob, body.credential)
        if body.second_step == "passkey"
        else None
    )
    user, session_value, device_value = setup_service.complete(
        session,
        blob,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        passkey=passkey,
        household=_first_household(body),
    )
    cookies.set_session(response, request, session_value)
    cookies.set_device(response, request, device_value)
    return UserOut.model_validate(user)


def _first_household(body: SetupComplete) -> setup_service.FirstHousehold | None:
    """The wizard's household and account, checked before anything is written."""
    wanted = body.household
    if wanted is None:
        return None
    return setup_service.FirstHousehold(
        name=wanted.name,
        base_currency=wanted.base_currency,
        date_format=wanted.date_format,
        week_start=wanted.week_start,
        theme=wanted.colour,
        locale=wanted.locale,
        keep=setup_service.starter_categories(wanted.categories, wanted.omit),
        account=body.account.model_dump() if body.account is not None else None,
    )
