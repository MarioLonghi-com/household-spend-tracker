"""First boot.

A fresh instance has no accounts and cannot be signed into. It will do exactly
one thing: let the person who deployed it -- proven by a token only they can
read -- create the owner, and it will not let them finish without a working
second factor: an authenticator whose code has come back, or a passkey whose
attestation has been verified (#456).

Nothing is written until the last step. Every intermediate answer travels with
the browser in a sealed blob, so an abandoned wizard leaves the instance exactly
as fresh as it was. There is no half-made owner to clean up.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import paths
from ..audit.batch import batch
from ..config import settings
from ..errors import Conflict, ValidationError
from ..models import Instance, InstanceState, RecoveryCode, Role, User, utcnow
from ..models.enums import BatchKind
from ..permissions import private_dir
from ..services import invitations
from . import crypto, devices, email_canonical, passwords, sessions, totp

#: Long enough that the wizard is not a race, short enough to paste.
BLOB_MAX_AGE_SECONDS = 15 * 60
RECOVERY_CODE_COUNT = 10

#: What a desktop setup blob carries instead of a setup token's fingerprint:
#: the shell that started this process is the proof of being on the machine
#: (#320), so there is no token to tie the blob to.
DESKTOP_PROOF = "desktop"


def recovery_code_count() -> int:
    """Ten where they stand in for an authenticator; one on the desktop, where
    the one code replaces a forgotten password (#320)."""
    from .. import capabilities

    return RECOVERY_CODE_COUNT if capabilities.second_factor() else 1


def token_needed() -> bool:
    """Whether setup asks for the setup token. Not on the desktop: nothing but
    its own shell can reach the process (#320)."""
    from .. import capabilities

    return capabilities.second_factor()


def setup_token_path() -> Path:
    return settings.data_dir / paths.SETUP_TOKEN


def instance_state(session: Session) -> InstanceState:
    row = session.execute(select(Instance)).scalar_one_or_none()
    return row.state if row else InstanceState.fresh


def is_configured(session: Session) -> bool:
    return instance_state(session) is InstanceState.configured


def rotate_setup_token() -> str:
    """Generate the token, print it, and write it 0600.

    Regenerated on every start while the instance is unclaimed, so a token that
    leaked into a log or a screenshot is stale by the next restart.
    """
    token = secrets.token_urlsafe(32)
    private_dir(settings.data_dir)
    path = setup_token_path()
    # Mode set at creation: a chmod afterwards leaves a window where the token
    # is world-readable.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as file:
        file.write(token)
    return token


def current_setup_token() -> str | None:
    path = setup_token_path()
    if not path.exists():
        return None
    return path.read_text().strip() or None


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AddressTaken(Conflict):
    """The address folds to one an account already has.

    Its own class so the invitation route can tell it from every other
    conflict: there, it spends the link (#211). See `routers/invites.complete`.
    """


def require_codes_saved(codes_saved: bool) -> None:
    """The last step's tick box, checked here as well as on the screen (#211).

    `SetupComplete.codes_saved` was sent by the client and read by nobody, so
    "I have stored my recovery codes" was a promise only the page kept. It is
    the only moment the codes are ever shown; finishing without them is how a
    lost phone becomes a lost account.
    """
    if not codes_saved:
        raise ValidationError(
            "store your recovery codes somewhere that is not this browser, then tick the box",
            code="setup.store_codes",
        )


def check_setup_token(presented: str) -> str:
    actual = current_setup_token()
    if not actual:
        raise Conflict("this instance is not waiting to be set up", code="setup.not_waiting")
    # Bytes, for the reason `totp.code_matches` gives: a non-ASCII str makes
    # compare_digest raise, and this answers a stranger before setup (#208).
    if not secrets.compare_digest(presented.strip().encode(), actual.encode()):
        raise ValidationError("that setup token is not right", code="setup.token_wrong")
    return actual


@dataclass(frozen=True, slots=True)
class SetupBlob:  # noqa: D101 -- documented below
    """Everything the wizard has collected, and nothing written down yet."""

    #: Ties the blob to the token that was current when it was issued, so a
    #: restart -- which rotates the token -- invalidates it.
    token_fp: str
    email: str
    email_canonical: str
    display_name: str
    #: Hashed at step 2. The plaintext never leaves the browser.
    password_hash: str
    totp_secret: str
    recovery_codes: tuple[str, ...]
    step: int
    #: Set when this is an invited person rather than the first owner.
    invitation_id: str | None = None
    role: str = "owner"
    #: The timestep the enrolment code matched, carried forward so `complete()`
    #: can burn it. Without this the code typed into the wizard stayed valid for
    #: the rest of its drift window -- a working second factor for the account
    #: that was just created, which for `/setup/enrol` is the instance owner.
    enrolled_step: int | None = None
    #: A passkey in place of the authenticator (#456): the provisional
    #: WebAuthn user handle the options were made under, and the hash of the
    #: one challenge an attestation may answer. Set by `offer_passkey`; the
    #: newest offer replaces the last.
    passkey_handle: str | None = None
    passkey_challenge: str | None = None


def seal(blob: SetupBlob) -> str:
    return crypto.seal_blob(json.dumps(asdict(blob)))


def unseal(token: str) -> SetupBlob:
    raw = crypto.open_blob(token, max_age_seconds=BLOB_MAX_AGE_SECONDS)
    try:
        data = json.loads(raw)
        data["recovery_codes"] = tuple(data["recovery_codes"])
        blob = SetupBlob(**data)
    except (ValueError, TypeError, KeyError) as exc:
        # Something that decrypted but is not a wizard blob. It should no longer
        # be reachable now that each purpose has its own key, but a malformed
        # payload must answer like an expired one rather than escaping as a 500.
        raise ValidationError(
            "that setup session is not valid; start again", code="setup.session_invalid"
        ) from exc

    if blob.invitation_id:
        # An invited blob is tied to its invitation, not to the setup token --
        # which by then no longer exists.
        return blob
    if blob.token_fp == DESKTOP_PROOF and not token_needed():
        return blob

    live = current_setup_token()
    if not live or not secrets.compare_digest(blob.token_fp, token_fingerprint(live)):
        raise ValidationError(
            "the server restarted; start again with the new setup token",
            code="setup.server_restarted",
        )
    return blob


def begin(presented_token: str, *, email: str, display_name: str, password: str) -> SetupBlob:
    """Steps 1 and 2: prove you deployed this, and name the owner.

    On the desktop (#320) there is no token and no authenticator: the blob is
    ready to complete at once, with one recovery code for a forgotten password.
    """
    if not token_needed():
        return _collect(
            token_fp=DESKTOP_PROOF,
            email=email,
            display_name=display_name,
            password=password,
            second_factor=False,
        )
    token = check_setup_token(presented_token)
    return _collect(
        token_fp=token_fingerprint(token),
        email=email,
        display_name=display_name,
        password=password,
    )


def begin_invited(
    *, invitation_id: str, role: str, email: str, display_name: str, password: str
) -> SetupBlob:
    """The same steps for an invited person.

    Deliberately the same code: an invited owner must end up with exactly the
    guarantees the first one has, and two enrolment paths would eventually
    differ in one of them.
    """
    return _collect(
        token_fp=f"invite:{invitation_id}",
        email=email,
        display_name=display_name,
        password=password,
        invitation_id=invitation_id,
        role=role,
    )


def _collect(
    *,
    token_fp: str,
    email: str,
    display_name: str,
    password: str,
    invitation_id: str | None = None,
    role: str = "owner",
    second_factor: bool = True,
) -> SetupBlob:
    canonical = email_canonical.canonical(email)
    passwords.refuse_weak(password, email=email)
    if not display_name.strip():
        raise ValidationError("a display name is required", code="setup.needs_display_name")

    return SetupBlob(
        token_fp=token_fp,
        email=email.strip(),
        email_canonical=canonical,
        display_name=display_name.strip(),
        password_hash=passwords.hash_password(password),
        # No authenticator on the desktop: the blob skips step 3 and completes.
        totp_secret=totp.new_secret() if second_factor else "",
        recovery_codes=tuple(
            secrets.token_hex(5) for _ in range(RECOVERY_CODE_COUNT if second_factor else 1)
        ),
        step=2 if second_factor else 4,
        invitation_id=invitation_id,
        role=role,
    )


def confirm_authenticator(blob: SetupBlob, code: str) -> SetupBlob:
    """Step 3, and the one that matters.

    Requiring a working code *before* anything is stored is what proves the
    authenticator actually pairs -- rather than discovering it does not on the
    day it is the only thing between the owner and their ledger.
    """
    step = totp.code_matches(blob.totp_secret, code)
    if step is None:
        raise ValidationError(
            "that code is not right. Check the time on your phone and try again.",
            code="auth.new_code_wrong",
        )
    # Carry the step so `complete()` can burn it: a code that has proved the
    # pairing has been used, and `app/auth/totp.py` promises that a used code is
    # "refused for the rest of its window and forever after".
    return SetupBlob(
        **{
            **asdict(blob),
            "recovery_codes": blob.recovery_codes,
            "step": 4,
            "enrolled_step": step,
        }
    )


def offer_passkey(session: Session, request, blob: SetupBlob) -> tuple[SetupBlob, dict]:
    """Creation options for a passkey in place of the authenticator (#456).

    Step 3's other choice. Needs the owner named first (step 2). Returns the
    blob with the handle and the challenge sealed in, and the options.
    Nothing is written but the challenge.
    """
    from . import passkeys

    if blob.step < 2 or not token_needed():
        raise ValidationError("finish enrolling an authenticator first", code="setup.enrol_first")
    handle = blob.passkey_handle or passkeys.handle_for()
    options, challenge = passkeys.enrolment_options(
        session, request, handle=handle, email=blob.email, display_name=blob.display_name
    )
    return (
        SetupBlob(
            **{
                **asdict(blob),
                "recovery_codes": blob.recovery_codes,
                "passkey_handle": handle,
                "passkey_challenge": challenge,
            }
        ),
        options,
    )


def verify_passkey(engine, request, blob: SetupBlob, credential: dict | None):
    """The attestation answering the blob's own challenge, verified and
    spent (#456). A forged one fails verification; a replayed one finds its
    challenge gone; one answering another wizard's challenge is not this
    blob's."""
    from . import passkeys

    if credential is None or not blob.passkey_handle:
        raise ValidationError(
            "that passkey could not be registered here. Start again.",
            code="passkey.not_registered",
        )
    return passkeys.verify_enrolment(
        engine, request, credential=credential, challenge_hash=blob.passkey_challenge
    )


#: The kinds the first-account step offers, as the account types they are.
ACCOUNT_KINDS = {
    "current": "checking",
    "savings": "savings",
    "credit_card": "credit_card",
    "cash": "cash",
}


@dataclass(frozen=True, slots=True)
class FirstHousehold:
    """The first household the wizard collected (#458), and its first account."""

    name: str
    base_currency: str
    date_format: str = "YYYY-MM-DD"
    week_start: str = "monday"
    theme: str | None = None
    locale: str | None = None
    #: Seed ids to make; None for the whole tree.
    keep: frozenset[str] | None = None
    account: dict | None = None


def starter_categories(preset: str, omit: Iterable[str] = ()) -> frozenset[str]:
    """The seed ids a preset keeps, less the ones unticked (#458).

    An unticked id the preset does not hold is not an error: it is simply not
    made. Leaving out the Income group or Reimbursements is, and
    `categories.seed_defaults` says so with `setup.category_locked`.
    """
    from .. import seed_words

    if preset == "essentials":
        chosen = set(seed_words.ESSENTIALS)
    elif preset == "empty":
        chosen = set(seed_words.REQUIRED)
    else:
        chosen = set(seed_words.TREE_IDS)
    left_out = set(omit)
    for required in seed_words.REQUIRED:
        if required in left_out:
            raise ValidationError(
                "the Income group and Reimbursements stay: the reports rely on them",
                code="setup.category_locked",
            )
    return frozenset(chosen - left_out)


def complete(
    session: Session,
    blob: SetupBlob,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    passkey=None,
    household: FirstHousehold | None = None,
) -> tuple[User, str, str]:
    """Step 5: one transaction, or nothing at all.

    The person, their recovery codes, a session and a trusted device all land
    together -- and for the first owner, the instance flag too. The whole thing
    is recorded as one batch whose actor is the user it is creating, which works
    because ids are assigned at construction and foreign keys are deferred to
    the commit.

    ``passkey`` is a verified attestation (`verify_passkey`), when the second
    step is a passkey rather than the authenticator (#456): it is stored with
    the person in the same transaction, and the authenticator the blob offered
    is never written.

    ``household`` is the first household the wizard collected (#458): made in
    this same transaction, with its starter categories in the wizard's
    language and an optional first account, so a household that fails leaves
    no owner behind either. Not for an invitation, whose household is the
    inviter's.
    """
    if passkey is None and blob.step < 4:
        raise ValidationError("finish enrolling an authenticator first", code="setup.enrol_first")
    if passkey is not None and blob.step < 2:
        raise ValidationError("finish enrolling an authenticator first", code="setup.enrol_first")

    invited = blob.invitation_id is not None
    if not invited and is_configured(session):
        raise Conflict("this instance has already been set up", code="setup.already_done")

    existing = session.execute(
        select(User).where(User.email_canonical == blob.email_canonical)
    ).scalar_one_or_none()
    if existing is not None:
        raise AddressTaken("somebody already uses that email address", code="setup.address_taken")

    user = User(
        email=blob.email,
        email_canonical=blob.email_canonical,
        display_name=blob.display_name,
        password_hash=blob.password_hash,
        role=Role(blob.role),
        totp_secret=b"",
        # The enrolment code is spent. Starting the counter at the step it
        # matched means that code -- the one on screen during the wizard, and in
        # any screenshot or proxy log of it -- cannot be replayed to sign in.
        totp_last_counter=blob.enrolled_step,
    )
    # None on the desktop, where nobody has one; sign-in there asks the
    # profile, not this column (`capabilities.second_factor`, #320).
    # And None with a passkey in its place (#456): a passkey-only account,
    # which `factors` tells from one a reset cleared.
    user.totp_secret = (
        crypto.seal_totp_secret(blob.totp_secret, user_id=user.id)
        if blob.totp_secret and passkey is None
        else None
    )
    if passkey is not None:
        user.totp_last_counter = None

    session.execute(text("PRAGMA defer_foreign_keys=ON"))
    # own_transaction=False: this must be one transaction, so the batch cannot
    # record its own failure -- which is the intent. An abandoned or failed
    # wizard leaves no trace at all.
    with batch(
        session, kind=BatchKind.setup, actor_id=user.id, own_transaction=False
    ) as created:
        session.add(user)
        if passkey is not None:
            from . import passkeys

            passkeys.keep_enrolled(session, user, passkey, handle=blob.passkey_handle or "")
        for code in blob.recovery_codes:
            session.add(RecoveryCode(user_id=user.id, code_hash=passwords.hash_password(code)))
        if not invited:
            session.add(Instance(id=1, state=InstanceState.configured, configured_at=utcnow()))
        else:
            # Through the same check the link itself goes through, rather
            # than a second inline one: this used to test only accepted_at, so
            # a withdrawn or expired invitation still minted an account -- and
            # since the role travels on the invitation, sometimes an owner.
            invitation = invitations.lookup_by_id(session, blob.invitation_id)
            invitations.accept(session, invitation, user)
        session_value = sessions.issue(session, user, ip=ip, user_agent=user_agent)
        device_value = devices.issue(session, user, label=user_agent)
        created.summary = {"users": 1, "recovery_codes": len(blob.recovery_codes)}
        if passkey is not None:
            created.summary["passkeys"] = 1
        if household is not None and not invited:
            _make_first_household(session, user, household)
            created.summary["households"] = 1
            if household.account is not None:
                created.summary["accounts"] = 1

    session.commit()
    if not invited:
        # After the commit, never before: a crash mid-commit should leave a
        # usable instance rather than a fresh one with no token.
        setup_token_path().unlink(missing_ok=True)
    return user, session_value, device_value


def _make_first_household(session: Session, user: User, wanted: FirstHousehold) -> None:
    """The household, its categories and its first account, through the
    services every later one goes through. Inside `complete`'s batch: any
    refusal here undoes the owner too."""
    from ..services import accounts as account_service
    from ..services import households as household_service

    # The user row must exist before the membership names it.
    session.flush()
    made = household_service.create_household(
        session,
        name=wanted.name,
        creator=user,
        base_currency=wanted.base_currency,
        date_format=wanted.date_format,
        locale=wanted.locale,
        week_start=wanted.week_start,
        theme=wanted.theme,
        keep=wanted.keep,
    )
    if wanted.account is not None:
        account = wanted.account
        account_service.create_account(
            session,
            household=made,
            name=account["name"],
            type=ACCOUNT_KINDS[account.get("kind") or "current"],
            currency=account.get("currency"),
            opening_balance=account.get("opening_balance") or 0,
            opening_date=account.get("opening_date"),
        )
