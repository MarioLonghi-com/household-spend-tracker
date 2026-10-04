"""First boot.

A fresh instance has no accounts and cannot be signed into. It will do exactly
one thing: let the person who deployed it -- proven by a token only they can
read -- create the owner, and it will not let them finish without a working
second factor.

Nothing is written until the last step. Every intermediate answer travels with
the browser in a sealed blob, so an abandoned wizard leaves the instance exactly
as fresh as it was. There is no half-made owner to clean up.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

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


def setup_token_path() -> Path:
    return settings.data_dir / "setup-token"


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
            "store your recovery codes somewhere that is not this browser, then tick the box"
        )


def check_setup_token(presented: str) -> str:
    actual = current_setup_token()
    if not actual:
        raise Conflict("this instance is not waiting to be set up")
    # Bytes, for the reason `totp.code_matches` gives: a non-ASCII str makes
    # compare_digest raise, and this answers a stranger before setup (#208).
    if not secrets.compare_digest(presented.strip().encode(), actual.encode()):
        raise ValidationError("that setup token is not right")
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
        raise ValidationError("that setup session is not valid; start again") from exc

    if blob.invitation_id:
        # An invited blob is tied to its invitation, not to the setup token --
        # which by then no longer exists.
        return blob

    live = current_setup_token()
    if not live or not secrets.compare_digest(blob.token_fp, token_fingerprint(live)):
        raise ValidationError("the server restarted; start again with the new setup token")
    return blob


def begin(presented_token: str, *, email: str, display_name: str, password: str) -> SetupBlob:
    """Steps 1 and 2: prove you deployed this, and name the owner."""
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
) -> SetupBlob:
    canonical = email_canonical.canonical(email)
    problems = passwords.complaints(password, email=email)
    if problems:
        raise ValidationError("that password will not do: " + ", and ".join(problems))
    if not display_name.strip():
        raise ValidationError("a display name is required")

    return SetupBlob(
        token_fp=token_fp,
        email=email.strip(),
        email_canonical=canonical,
        display_name=display_name.strip(),
        password_hash=passwords.hash_password(password),
        totp_secret=totp.new_secret(),
        recovery_codes=tuple(secrets.token_hex(5) for _ in range(RECOVERY_CODE_COUNT)),
        step=2,
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
        raise ValidationError("that code is not right. Check the time on your phone and try again.")
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


def complete(
    session: Session,
    blob: SetupBlob,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str, str]:
    """Step 5: one transaction, or nothing at all.

    The person, their recovery codes, a session and a trusted device all land
    together -- and for the first owner, the instance flag too. The whole thing
    is recorded as one batch whose actor is the user it is creating, which works
    because ids are assigned at construction and foreign keys are deferred to
    the commit.
    """
    if blob.step < 4:
        raise ValidationError("finish enrolling an authenticator first")

    invited = blob.invitation_id is not None
    if not invited and is_configured(session):
        raise Conflict("this instance has already been set up")

    existing = session.execute(
        select(User).where(User.email_canonical == blob.email_canonical)
    ).scalar_one_or_none()
    if existing is not None:
        raise AddressTaken("somebody already uses that email address")

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
    user.totp_secret = crypto.seal_totp_secret(blob.totp_secret, user_id=user.id)

    session.execute(text("PRAGMA defer_foreign_keys=ON"))
    # own_transaction=False: this must be one transaction, so the batch cannot
    # record its own failure -- which is the intent. An abandoned or failed
    # wizard leaves no trace at all.
    with batch(
        session, kind=BatchKind.setup, actor_id=user.id, own_transaction=False
    ) as created:
        session.add(user)
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

    session.commit()
    if not invited:
        # After the commit, never before: a crash mid-commit should leave a
        # usable instance rather than a fresh one with no token.
        setup_token_path().unlink(missing_ok=True)
    return user, session_value, device_value
