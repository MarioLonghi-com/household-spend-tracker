"""Passkeys (WebAuthn): whether this instance may offer them, and to whom.

Passkeys are offered **only when the conditions are good for them** (#47,
decision 4). No screen shows a passkey button that can only fail, and every
deployment that cannot use them keeps password + code exactly as it is. The
browser checks its half (`isSecureContext`, `PublicKeyCredential` and its JSON
helpers); this module is the server's half, answered per request by
`GET /api/session/passkey/state`.

Available means all of:

- **an RP ID is configured** -- `SPENDTRACKER_RP_ID`, or the host of
  `SPENDTRACKER_PUBLIC_URL` (`config._rp_id`). Never the request's `Host`;
- **it is a domain name, not an IP literal.** Browsers refuse an IP as an RP
  ID, even over HTTPS;
- **this request's host is that RP ID.** Someone who opened the app at its
  tailnet `100.x` address, or at `make lan`'s LAN address, is on another
  origin, where a passkey for this name cannot be made or used;
- **the request arrived over HTTPS, or the host is `localhost`** -- the
  secure context WebAuthn needs;
- **the expected-origins list is not empty.** An assertion's
  `clientDataJSON` names the origin the browser was at, and it is checked
  against this list, which therefore has to be configured: a proxy does not
  change what the browser says, and nothing about the request can be trusted
  to infer it.

On HTTPS behind a proxy: `tailscale serve` terminates TLS and the app sees
plain HTTP from loopback or the Docker bridge. uvicorn believes the proxy's
`X-Forwarded-Proto` only from `FORWARDED_ALLOW_IPS`, and not every proxy sends
it. So a request also counts as HTTPS when the public URL this instance is
configured with is `https://` and the request reached it by that name -- unless
a header says outright that it arrived over plain HTTP. That is advisory, not
the gate: a browser on a page that is not a secure context has no
`navigator.credentials` to call, and the origin in `clientDataJSON` is checked
against `expected_origins` whatever this answer said.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

import webauthn
from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .. import config
from ..errors import Conflict, NotFound, ValidationError
from ..hosts import _host_of, _is_ip_literal
from ..models import Passkey, User, WebAuthnChallenge, utcnow

LOCALHOST = "localhost"


class Unavailable(StrEnum):
    """Why this instance does not offer passkeys to this request. Stable
    words: the client chooses its sentence by them (#47 §3)."""

    #: Neither `SPENDTRACKER_RP_ID` nor `SPENDTRACKER_PUBLIC_URL` is set.
    NOT_CONFIGURED = "not_configured"
    #: The RP ID is an IP literal, which no browser accepts.
    IP_ADDRESS = "ip_address"
    #: The app was opened at another name or address than the RP ID.
    WRONG_HOST = "wrong_host"
    #: Plain HTTP on a host that is not `localhost`: not a secure context.
    INSECURE = "insecure"
    #: Nothing to check an assertion's origin against.
    NO_ORIGINS = "no_origins"


#: One sentence per reason, for an operator reading the JSON or the log. The
#: client words its own for the member.
DETAIL: dict[Unavailable, str] = {
    Unavailable.NOT_CONFIGURED: "Passkeys are not set up on this server: set SPENDTRACKER_PUBLIC_URL "
    "to the HTTPS address people open it at.",
    Unavailable.IP_ADDRESS: "Passkeys need a host name, and this server is configured with an IP address.",
    Unavailable.WRONG_HOST: "Passkeys work only at this server's own address.",
    Unavailable.INSECURE: "Passkeys need this app opened over HTTPS.",
    Unavailable.NO_ORIGINS: "Passkeys need the server's public address configured.",
}


@dataclass(frozen=True, slots=True)
class PasskeyState:
    available: bool
    reason: Unavailable | None = None
    #: Where passkeys do work, when it is somewhere: the public URL. Sent so the
    #: screen can say "open https://… instead"; `/llms.txt` already publishes it.
    address: str | None = None

    @property
    def detail(self) -> str | None:
        return DETAIL[self.reason] if self.reason else None


def _request_host(request: Request) -> str:
    return _host_of(request.headers.get("host", "")).lower().rstrip(".")


def _public_origin() -> str | None:
    """The configured public URL as an origin, if it names the RP ID."""
    settings = config.settings
    if settings.public_url and urlsplit(settings.public_url).hostname == settings.rp_id:
        return settings.public_url
    return None


def expected_origins(request: Request) -> list[str]:
    """What an assertion's or attestation's origin must be one of.

    The public URL, when its host is the RP ID. In development with the RP ID
    `localhost`, also `http://localhost:<this request's port>`: `make dev`
    takes `PORT=`, and a worktree on 8851 is as much "this machine" as one on
    8848. Only then, and only for `localhost`, is anything read from the
    request -- a host that can only be this machine.
    """
    settings = config.settings
    origins = [one for one in (_public_origin(),) if one]
    if (
        settings.environment == "development"
        and settings.rp_id == LOCALHOST
        and _request_host(request) == LOCALHOST
    ):
        port = request.url.port
        local = f"http://{LOCALHOST}" + (f":{port}" if port and port != 80 else "")
        if local not in origins:
            origins.append(local)
    return origins


def _over_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if forwarded == "http":
        return False
    return config.settings.public_url.startswith("https://") and _public_origin() is not None


def state(request: Request) -> PasskeyState:
    """Whether passkeys may be offered to this request, and if not, why."""
    settings = config.settings
    rp_id = settings.rp_id
    address = _public_origin()
    if not rp_id:
        return PasskeyState(False, Unavailable.NOT_CONFIGURED)
    if _is_ip_literal(rp_id):
        return PasskeyState(False, Unavailable.IP_ADDRESS)
    if _request_host(request) != rp_id:
        return PasskeyState(False, Unavailable.WRONG_HOST, address)
    if rp_id != LOCALHOST and not _over_https(request):
        return PasskeyState(False, Unavailable.INSECURE, address)
    if not expected_origins(request):
        return PasskeyState(False, Unavailable.NO_ORIGINS)
    return PasskeyState(True, address=address)


# --------------------------------------------------------------------------- #
# Challenges (#120)
# --------------------------------------------------------------------------- #

#: How long a browser has between asking for options and answering them. The
#: same figure is the `timeout` the options carry, so the browser's prompt and
#: the server's row give up together.
CHALLENGE_SECONDS = 5 * 60

REGISTER = "register"
SIGN_IN = "sign_in"


def _challenge_hash(challenge: bytes) -> str:
    return hashlib.sha256(challenge).hexdigest()


def issue_challenge(session: Session, *, purpose: str, user_id: str | None) -> bytes:
    """A fresh challenge, recorded so it can be spent once.

    A registration drops the member's earlier unspent ones, like
    `stepup.grant` drops earlier grants: two live challenges means one of them
    is the one somebody forgot about.
    """
    if user_id is not None:
        for old in session.execute(
            select(WebAuthnChallenge).where(
                WebAuthnChallenge.user_id == user_id, WebAuthnChallenge.purpose == purpose
            )
        ).scalars():
            session.delete(old)
    challenge = secrets.token_bytes(32)
    now = utcnow()
    session.add(
        WebAuthnChallenge(
            id_hash=_challenge_hash(challenge),
            user_id=user_id,
            purpose=purpose,
            created_at=now,
            expires_at=now + timedelta(seconds=CHALLENGE_SECONDS),
        )
    )
    session.flush()
    return challenge


def claim_challenge(
    engine: Engine, challenge: bytes, *, purpose: str, user_id: str | None, now: datetime | None = None
) -> bool:
    """Spend a challenge. True only if it was issued for this ceremony and this
    member, and is still live.

    One `DELETE ... RETURNING` on its own transaction, exactly as
    `stepup.claim` (#206): the row count is the verdict, so two requests
    presenting one challenge cannot both be told yes, and the spend is
    committed before the rest of the request runs. The row goes either way.
    """
    table = WebAuthnChallenge.__table__
    with engine.begin() as own:
        row = own.execute(  # audit-exempt: webauthn challenges are not audited
            delete(table)
            .where(table.c.id_hash == _challenge_hash(challenge))
            .returning(table.c.user_id, table.c.purpose, table.c.expires_at)
        ).first()
    if row is None:
        return False
    owner_id, issued_for, expires_at = row
    return issued_for == purpose and owner_id == user_id and expires_at > (now or utcnow())


def sweep(session: Session, *, now: datetime | None = None) -> int:
    """Challenges nobody came back with. Called by `housekeeping.sweep`.

    A bulk statement, allowed because `webauthn_challenges` is not audited --
    the comment is what the CI grep reads.
    """
    table = WebAuthnChallenge.__table__
    return (
        session.execute(  # audit-exempt: webauthn challenges are not audited
            delete(table).where(table.c.expires_at <= (now or utcnow()))
        ).rowcount
        or 0
    )


# --------------------------------------------------------------------------- #
# Registering and managing a passkey (#120)
# --------------------------------------------------------------------------- #

#: The provider an AAGUID names, for a new passkey's default label. Only the
#: common ones a household will meet; anything else is "Passkey", and the
#: member renames it. From the community-maintained AAGUID list.
PROVIDERS: dict[str, str] = {
    "fbfc3007-154e-4ecc-8c0b-6e020557d7bd": "iCloud Keychain",
    "dd4ec289-e01d-41c9-bb89-70fa845d4bf2": "iCloud Keychain",
    "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Password Manager",
    "adce0002-35bc-c60a-648b-0b25f1f05503": "Chrome on Mac",
    "08987058-cadc-4b81-b6e1-30de50dcbe96": "Windows Hello",
    "9ddd1817-af5a-4672-a2b9-3e3dd95000a9": "Windows Hello",
    "6028b017-b1d4-4c02-b4b3-afcdafc96bb2": "Windows Hello",
    "bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
    "d548826e-79b4-db40-a3d8-11116f7e8349": "Bitwarden",
    "53414d53-554e-4700-0000-000000000000": "Samsung Pass",
}
DEFAULT_LABEL = "Passkey"
LABEL_MAX = 80

#: The one sentence for every way a registration answer can be wrong -- a
#: spent, expired or someone else's challenge, the wrong origin or host, a
#: signature that does not verify, no user verification. Which of them it was
#: helps only somebody probing.
_NOT_REGISTERED = "that passkey could not be registered here. Start again."


def default_label(aaguid: str | None) -> str:
    return PROVIDERS.get((aaguid or "").lower(), DEFAULT_LABEL)


def refuse_unless_available(request: Request) -> None:
    """The server's half of decision 4 on every ceremony, not only on the
    state answer: an endpoint that would only fail is refused with the
    reason, rather than reached and failing obscurely."""
    found = state(request)
    if not found.available:
        raise Conflict(found.detail or "passkeys are not available here")


def give_user_handle(user: User) -> bool:
    """Make the member's WebAuthn user handle if they have none yet. True when
    one was made -- `users` is audited, so the caller holds a batch then."""
    if user.webauthn_user_handle is not None:
        return False
    user.webauthn_user_handle = secrets.token_bytes(32)
    return True


def registration_options(session: Session, user: User, request: Request) -> dict:
    """The options for `navigator.credentials.create()`, as JSON the browser's
    `PublicKeyCredential.parseCreationOptionsFromJSON` reads.

    `residentKey: required` -- a passkey has to be discoverable, or it cannot
    sign in from the email field -- and `userVerification: required`, because
    a verified assertion is both factors at once (#121). Every passkey the
    member already has for this RP ID is excluded, so the browser says "already
    registered" instead of making a second one in the same provider.

    The member needs a user handle first (`give_user_handle`).
    """
    refuse_unless_available(request)
    if user.webauthn_user_handle is None:
        raise Conflict("give the member a user handle first")
    settings = config.settings
    existing = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
        for p in user.passkeys
        if p.rp_id == settings.rp_id
    ]
    options = webauthn.generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=settings.app_name,
        user_id=user.webauthn_user_handle,
        user_name=user.email,
        user_display_name=user.display_name,
        challenge=issue_challenge(session, purpose=REGISTER, user_id=user.id),
        timeout=CHALLENGE_SECONDS * 1000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=existing,
    )
    return json.loads(webauthn.options_to_json(options))


def challenge_of(credential: dict) -> bytes:
    """The challenge the browser signed, read from its `clientDataJSON`.

    Read only to find which row to spend. Whether it is the right kind of
    ceremony, the right origin and properly signed is `webauthn`'s to verify
    afterwards against that same value.
    """
    try:
        client_data = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        return base64url_to_bytes(client_data["challenge"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("that is not a passkey answer") from exc


def register(
    session: Session,
    engine: Engine,
    user: User,
    request: Request,
    *,
    credential: dict,
    label: str | None = None,
) -> Passkey:
    """Verify a browser's answer to `registration_options` and store the passkey.

    The challenge is spent first, whatever follows -- one challenge is worth
    one attempt. The caller holds a batch.
    """
    refuse_unless_available(request)
    settings = config.settings
    if not claim_challenge(engine, challenge_of(credential), purpose=REGISTER, user_id=user.id):
        raise ValidationError(_NOT_REGISTERED)
    try:
        verified = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=challenge_of(credential),
            expected_rp_id=settings.rp_id,
            expected_origin=expected_origins(request),
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        raise ValidationError(_NOT_REGISTERED) from exc

    credential_id = bytes_to_base64url(verified.credential_id)
    if session.execute(
        select(Passkey.id).where(Passkey.credential_id == credential_id)
    ).first():
        raise Conflict("that passkey is already registered")

    transports = (credential.get("response") or {}).get("transports")
    passkey = Passkey(
        user_id=user.id,
        credential_id=credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=_transports(transports),
        label=_clean_label(label) or default_label(verified.aaguid),
        rp_id=settings.rp_id,
        aaguid=verified.aaguid,
        backup_eligible=verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE,
        backed_up=verified.credential_backed_up,
        created_at=utcnow(),
    )
    session.add(passkey)
    session.flush()
    return passkey


def _transports(said: object) -> list[str] | None:
    """What the browser said it can reach the authenticator over, kept only
    as the short known words it should be: this is the client's to write, and
    a hint is not worth storing anything else for."""
    if not isinstance(said, list):
        return None
    known = list(dict.fromkeys(one for one in said if isinstance(one, str) and one in TRANSPORTS))
    return known or None


#: The values WebAuthn Level 3 defines for `AuthenticatorTransport`.
TRANSPORTS = frozenset({"usb", "nfc", "ble", "smart-card", "hybrid", "internal", "cable"})


def _clean_label(label: str | None) -> str | None:
    tidy = " ".join((label or "").split())
    return tidy[:LABEL_MAX] or None


def owned(session: Session, user: User, passkey_id: str) -> Passkey:
    """This member's passkey, or 404 -- somebody else's reads exactly like
    one that never existed."""
    passkey = session.get(Passkey, passkey_id)
    if passkey is None or passkey.user_id != user.id:
        raise NotFound("no such passkey")
    return passkey


def rename(passkey: Passkey, label: str) -> Passkey:
    tidy = _clean_label(label)
    if tidy is None:
        raise ValidationError("a passkey needs a name")
    passkey.label = tidy
    return passkey


def for_user(session: Session, user: User) -> list[Passkey]:
    """Newest first, the order the list opens in before anyone sorts it."""
    return list(
        session.execute(
            select(Passkey).where(Passkey.user_id == user.id).order_by(Passkey.created_at.desc())
        ).scalars()
    )


def elsewhere(session: Session) -> dict[str, int]:
    """Passkeys registered under another RP ID than this instance's, counted
    per RP ID: the ones a renamed host or a restore onto another one has
    silently stranded (#47 §1.2). With passkeys off, every one counts."""
    rp_id = config.settings.rp_id
    rows = session.execute(
        select(Passkey.rp_id, func.count()).where(Passkey.rp_id != rp_id).group_by(Passkey.rp_id)
    ).all()
    return {found: count for found, count in rows}


def hosts_in(database: pathlib.Path) -> dict[str, int]:
    """How many passkeys each RP ID holds, read straight from a SQLite file.

    Read-only and standard library, for the operator scripts that look at a
    ledger without booting the app -- `doctor`, `upgrade --check`, `restore`.
    An empty answer from a ledger older than passkeys, which has no table.
    """
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT rp_id, count(*) FROM passkeys GROUP BY rp_id").fetchall()
    except sqlite3.Error:
        return {}
    return {rp_id: count for rp_id, count in rows}


def stranded(counts: dict[str, int], rp_id: str) -> list[str]:
    """One sentence per RP ID whose passkeys this instance cannot use (#47 §1.2):
    "3 passkeys registered for old.example.ts.net, this instance is
    new.example.ts.net". Members with those sign in with password + code and
    register again; the stranded rows are theirs to remove."""
    here = rp_id or "not set up for passkeys"
    return [
        f"{count} passkey{'s' if count != 1 else ''} registered for {found}, this instance is {here}"
        for found, count in sorted(counts.items())
        if found != rp_id
    ]
