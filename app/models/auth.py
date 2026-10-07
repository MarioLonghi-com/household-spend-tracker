"""Users, sessions, devices and the instance's own setup state.

Everything secret here is hashed or encrypted: nothing in this schema lets
someone sign in by reading it. Sessions and trusted devices are random opaque
values whose SHA-256 is stored, so the database lookup *is* the verification --
they carry no signature and therefore no dependency on the server key.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, EnumStr, Timestamped, UUIDPrimaryKey, utcnow
from .enums import AgentScope, InstanceState, Role


class Instance(Base):
    """One row. Which state this deployment is in."""

    __tablename__ = "instance"
    __audit__ = False

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    state: Mapped[InstanceState] = mapped_column(
        EnumStr(InstanceState, 12), default=InstanceState.fresh, nullable=False
    )
    configured_at: Mapped[datetime | None] = mapped_column(DateTime)


class User(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "users"
    __audit__ = True
    #: Never enters a snapshot, in either image. Omitted rather than masked: a
    #: "***" placeholder would be written back verbatim by an undo.
    __audit_redact__ = frozenset({"password_hash", "totp_secret", "totp_last_counter"})

    #: As typed, for display and for anything ever sent to it.
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    #: Normalised. This is the unique one, and the one every lookup uses.
    email_canonical: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: NULL only while an account reset has cleared the password and nobody has
    #: chosen a new one through a link (#284) -- "nobody knows it", stored as
    #: itself rather than as a hash of something nobody was told, so it outlives
    #: the link: once that is withdrawn or swept, the next link can still see
    #: it has a password to set. `check_password` verifies against the dummy
    #: hash then, the same work and the same sentence as an unknown email.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[Role] = mapped_column(EnumStr(Role, 8), default=Role.member, nullable=False)
    #: NULL only between an account reset that cleared the authenticator and
    #: the moment its holder enrols a new one through the link (#284). Every
    #: way in to an account with no secret is shut: the code step refuses it,
    #: its recovery codes were deleted with the secret, and a password alone
    #: never yields a session. The wizard and invitations still cannot finish
    #: without one -- `setup.complete` writes it in the same transaction.
    totp_secret: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: The last TOTP timestep accepted for this user. Monotonic, so a code is
    #: dead the moment it is used -- even while still inside its window.
    totp_last_counter: Mapped[int | None] = mapped_column(Integer)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: WebAuthn's `user.id` for this member's passkeys (#120): 32 random bytes,
    #: made on their first registration and never changed. Not the row id and
    #: not the email, because an authenticator stores it and may show or sync
    #: it, and it must carry nothing about the person. NULL until then.
    webauthn_user_handle: Mapped[bytes | None] = mapped_column(LargeBinary(64), unique=True)

    recovery_codes: Mapped[list[RecoveryCode]] = relationship(
        "RecoveryCode", back_populates="user", cascade="all, delete-orphan"
    )
    #: Declared so removing someone takes their memberships and any outstanding
    #: invitations through the ORM, where the audit log sees each one, rather
    #: than through a database cascade the hook never hears about.
    memberships: Mapped[list[HouseholdMember]] = relationship(  # noqa: F821
        "HouseholdMember",
        primaryjoin="User.id == HouseholdMember.user_id",
        cascade="all, delete-orphan",
        overlaps="household",
    )
    invitations_sent: Mapped[list[Invitation]] = relationship(
        "Invitation",
        primaryjoin="User.id == Invitation.invited_by_id",
        foreign_keys="Invitation.invited_by_id",
        cascade="all, delete-orphan",
    )
    #: At most one pending reset link (#284). Declared for the reason the two
    #: above are: removing the account deletes it through the ORM.
    account_reset: Mapped[AccountReset | None] = relationship(
        "AccountReset",
        primaryjoin="User.id == AccountReset.user_id",
        foreign_keys="AccountReset.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    #: Reset links this person issued. No delete-orphan: removing an owner
    #: must not withdraw a link somebody else is locked out without. The ORM
    #: nulls `created_by_id` instead, where the audit log sees it, and the link
    #: then reads as coming from the server -- the one thing left to say.
    account_resets_issued: Mapped[list[AccountReset]] = relationship(
        "AccountReset",
        primaryjoin="User.id == AccountReset.created_by_id",
        foreign_keys="AccountReset.created_by_id",
    )
    #: A member's keys die with them, which is what the cookie path already does
    #: for their sessions. Declared rather than left to the database's CASCADE
    #: so the hook walks it and each revocation is a logged delete.
    agent_keys: Mapped[list[AgentKey]] = relationship(
        "AgentKey",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    #: A member's passkeys go with them, through the ORM where the audit log
    #: sees each delete, for the reason `agent_keys` is declared here.
    passkeys: Mapped[list[Passkey]] = relationship(
        "Passkey",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class RecoveryCode(Base, UUIDPrimaryKey):
    """Ten single-use codes, generated at enrolment and shown once."""

    __tablename__ = "recovery_codes"
    __audit__ = True
    __audit_redact__ = frozenset({"code_hash"})

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    user: Mapped[User] = relationship("User", back_populates="recovery_codes")


class WebSession(Base):
    """Server-side sessions, not JWTs -- because a session must be revocable."""

    __tablename__ = "sessions"
    __audit__ = False

    #: SHA-256 of the cookie value. The value itself is never stored.
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: Absolute expiry, fixed at issuance. Idle expiry is computed from
    #: last_seen_at rather than stored, so there is one fewer thing to keep true.
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))


class PendingSignIn(Base):
    """The half-finished sign-in: password accepted, second factor still owed.

    It used to live only in a sealed cookie, which made it a **bearer token**:
    replayable, and portable to any browser. Anyone who read it once -- a shared
    machine, a proxy, a log line carrying a Cookie header -- needed only a code
    to finish a sign-in, without ever knowing the password. A row here makes it
    single-use, because finishing the sign-in deletes it.

    Not audited, for the same reason sessions are not: this records an attempt
    at the door, not a change to the ledger.
    """

    __tablename__ = "pending_sign_ins"
    __audit__ = False

    #: SHA-256 of the cookie value, exactly as sessions do it.
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class StepUpGrant(Base):
    """A fresh proof of identity, spent on one privileged act.

    The same shape as :class:`PendingSignIn`, and for the same reason its
    docstring gives: **a sealed token would be a bearer token** -- replayable
    for the whole of its window and portable to any browser. A row makes it
    single-use, because spending it deletes it.

    That matters more here than at the sign-in door. A pending sign-in is worth
    one attempt at a second factor; this is worth one agent key, which is a
    credential that outlives the session that minted it. ``profile.py``'s
    existing acts settle for the current password because what they can do is
    bounded -- changing a password ends every other session, and re-enrolling
    revokes every trusted device, so both are loud. Minting a key is quiet, and
    a borrowed unlocked laptop should not be enough.

    Not audited, like every other row that records what happened at the door
    rather than what happened to the ledger.
    """

    __tablename__ = "step_up_grants"
    __audit__ = False

    #: SHA-256 of the value handed to the client, exactly as sessions do it.
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: Fixed at issue and deliberately non-sliding, like `trusted_devices`. A
    #: grant that renewed itself on inspection would be a session with extra
    #: privileges rather than a proof of a single moment.
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TrustedDevice(Base):
    """"This browser already passed 2FA." Separate from the session on purpose:
    different lifetime, different revocation."""

    __tablename__ = "trusted_devices"
    __audit__ = False

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: Fixed 30 days from issuance and deliberately non-sliding: trust that
    #: renews itself on use means a stolen laptop is trusted forever.
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class LoginAttempt(Base, UUIDPrimaryKey):
    """Rate limiting, and a record of who tried what."""

    __tablename__ = "login_attempts"
    __audit__ = False

    email_canonical: Mapped[str] = mapped_column(String(254), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    #: "password" or "totp" -- each gets its own counter.
    kind: Mapped[str] = mapped_column(String(16), default="password", nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_login_attempts_email_at", "email_canonical", "at"),
        Index("ix_login_attempts_ip_at", "ip", "at"),
    )


class Invitation(Base, UUIDPrimaryKey):
    """A one-time link that lets someone create their own account.

    The owner never sets, sees or learns the new person's password: they send a
    link, and the invitee walks the same enrolment the owner did -- their own
    password, their own authenticator, their own recovery codes.

    The role travels on the invitation, so an owner can invite another owner
    without ever holding their credentials.
    """

    __tablename__ = "invitations"
    __audit__ = True
    #: The token is the credential. Its hash is no more loggable than a
    #: password hash, and an undo must never resurrect a spent invitation.
    #: The address too (#211): `housekeeping` deletes ended invitations so the
    #: invitee's address is not kept for nothing, and a log entry carrying it
    #: in the before-image would keep it forever instead. It is only ever a
    #: prefill for the acceptance screen.
    __audit_redact__ = frozenset({"token_hash", "email"})

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    #: Prefilled on the acceptance screen; the invitee can still correct it.
    email: Mapped[str | None] = mapped_column(String(254))
    role: Mapped[Role] = mapped_column(EnumStr(Role, 8), default=Role.member, nullable=False)
    invited_by_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Households to put them in the moment they accept.
    household_ids: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    accepted_user_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AccountReset(Base, UUIDPrimaryKey):
    """A one-time link that gives one account a new password, a new
    authenticator, or both (#284).

    Modelled on :class:`Invitation`: the token is shown once and only its hash
    is stored, the link expires, and the person holding it chooses their own
    credentials, so whoever issued it never knows them.

    The damage is done at **issue** time, not when the link is followed: the
    account's sessions, trusted browsers and live agent keys end, and the
    password is cleared, or the authenticator is cleared and its recovery
    codes deleted, or both -- whichever of the two switches were set.
    An account that may be in the wrong hands is shut at once, and the link is
    the only way back in.

    Hard-deleted when it is used or withdrawn. One that lapses is kept for
    `account_resets.RETENTION` past its expiry -- so the screen that issued it
    can say "that link expired" rather than nothing -- and then swept by
    `auth/housekeeping.py`. At most one per account: issuing another replaces
    it.
    """

    __tablename__ = "account_resets"
    __audit__ = True
    #: The token is the credential, exactly as `invitations.token_hash` is: no
    #: more loggable than a password hash, and an undo must never resurrect a
    #: link that was spent.
    __audit_redact__ = frozenset({"token_hash"})

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    __mapper_args__ = {
        # Not a version: the token never changes. Naming it here puts it in
        # the WHERE of every DELETE of this row and, with that, makes
        # SQLAlchemy check the row count -- raising `StaleDataError` when the
        # row was already gone, where an unversioned delete only warns and
        # carries on. That check is what keeps a link single-use when two
        # requests spend it at once, and what tells an owner withdrawing or
        # replacing a link that somebody followed it first (#284). It is the
        # compare-and-set `sessions.claim_pending` made of a read-then-delete
        # in #206, kept inside the ORM because this table is audited.
        "version_id_col": token_hash,
        "version_id_generator": False,
    }

    #: The password was cleared; the link sets a new one.
    password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: The authenticator was cleared and the recovery codes deleted; the link
    #: enrols a new one and shows ten new codes.
    authenticator: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: The owner who issued it. NULL means it came from the server -- whoever
    #: can run a command beside the ledger -- which the link says in words.
    created_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    #: Declared from this side, with the cascade on `User.account_reset`, so
    #: removing an account takes its pending reset through the ORM where the
    #: audit log sees it.
    user: Mapped[User] = relationship(
        "User", foreign_keys=[user_id], back_populates="account_reset"
    )


class AgentKey(Base, UUIDPrimaryKey):
    """A credential a member issues to a program acting on their behalf.

    Not a user, and deliberately not given a `users` row. Sign-in here is
    password then TOTP, and nothing signs in on a password alone: an account
    whose authenticator a reset cleared (`users.totp_secret` NULL, #284) is
    refused at the code step and at the recovery codes. So an agent that could
    sign in would have to hold both factors, which is a second factor stored
    beside the first and therefore not one. The door has to be a different
    kind of credential, and this is it.

    Opaque, 256 bits, stored as its SHA-256, exactly as sessions, trusted
    devices and invitations already are: the database lookup *is* the
    verification, so there is no signature and no dependency on `secret.key`.
    That is also what makes revocation instant, which is the property
    `Access Control Investigation` rejected JWTs for.
    """

    __tablename__ = "agent_keys"
    __audit__ = True
    #: `token_hash` for the reason `invitations.token_hash` is redacted: the
    #: token is the credential, and an undo must never resurrect a revoked key.
    #:
    #: `last_used_at` for a different reason, and it is load-bearing rather
    #: than cosmetic. `audit/snapshot.py` omits redacted columns from BOTH
    #: images, and `audit/hook.py` then drops any update where `before ==
    #: after`. So an update touching only `last_used_at` produces no change row
    #: and **demands no open batch** -- which is the only way a credential can
    #: record its own use on a GET without every read opening a batch. The TOTP
    #: counter and a password rehash already take this exact path.
    __audit_redact__ = frozenset({"token_hash", "last_used_at"})

    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    #: What it is for, in the person's own words. "receipt filer".
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    #: What is holding it, if the person cared to say. "Claude Desktop", "n8n".
    #: Shown in History beside the human's name, so a row reads "Jane Doe ·
    #: via Claude (receipt filer)".
    agent_name: Mapped[str | None] = mapped_column(String(80))
    #: Whose authority this borrows. CASCADE: a member's keys die with them,
    #: which is what the cookie path already does for their sessions.
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: One household per key. Not a list and not "all of them": `deps.load_for`
    #: joins through a single `household_id`, and a key holding several would
    #: need its own scoping path beside the one that carries the 404-not-403
    #: rule. Two households is two keys.
    #:
    #: RESTRICT, for the reason `receipts.household_id` and
    #: `transactions.household_id` are: a household reaches its keys through
    #: the ORM relationship on `Household`, which the audit hook walks and
    #: writes a delete row for. A database-level CASCADE would be a second path
    #: that deletes the same rows without the hook hearing about it, and
    #: `test_no_audited_table_cascades_behind_the_orm` exists because exactly
    #: that once destroyed the far leg of every transfer into a deleted account.
    household_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("households.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: One value, not a list. See :class:`AgentScope` for why.
    scope: Mapped[AgentScope] = mapped_column(
        EnumStr(AgentScope, 8), default=AgentScope.read, nullable=False
    )
    #: Whether this key may move a staged import from `preview` to `applied`.
    #: False is the honest default: the preview exists so that nothing is
    #: written before somebody looked, and that is more true of an agent.
    may_commit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: Mandatory and non-sliding, like `trusted_devices.expires_at`. A key that
    #: renews itself on use is a key that lives forever.
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    #: Touched sparingly -- see `sessions.touch` for the same arithmetic -- so a
    #: polling agent does not turn every read into a WAL write.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship("User", back_populates="agent_keys")
    household: Mapped[Household] = relationship(  # noqa: F821
        "Household", back_populates="agent_keys"
    )

    __table_args__ = (Index("ix_agent_keys_user_household", "user_id", "household_id"),)

    def live(self, *, now: datetime | None = None) -> bool:
        """Whether this key would open the door right now.

        Three columns and a clock. A property rather than a stored flag,
        because a stored one would be a second place for "is it revoked" to be
        true -- and the standing rule is to store the deliberate act and
        compute the consequence. `revoked_at` and `expires_at` are the acts.

        It deliberately does **not** check the owning user: `agent_keys.lookup`
        does, because a request has to, but a key list is a list of keys and
        a disabled member's screen is not where they find that out.
        """
        return self.revoked_at is None and self.expires_at > (now or utcnow())


class Passkey(Base, UUIDPrimaryKey):
    """A WebAuthn credential a member registered: a way in that is both
    factors at once (#47, #120).

    Only the public half is here. The private key never leaves the member's
    authenticator, so nothing in this row lets anyone sign in -- which is also
    why it is not encrypted with `secret.key` (#47 §1.4): a member in recovery
    mode after a replaced key can still use a passkey.

    Audited, because adding, renaming and removing a way into an account is
    exactly what History should carry. Undo never touches it
    (`undo.CREDENTIAL_TABLES`): undoing a removal would resurrect a credential
    somebody took away on purpose.
    """

    __tablename__ = "passkeys"
    __audit__ = True
    #: `public_key` so an undo could never put a credential back even if
    #: `CREDENTIAL_TABLES` forgot it. The other three move on every sign-in,
    #: which has no batch open: leaving them out of both images means an
    #: update touching only them produces no change row and demands none --
    #: the path `agent_keys.last_used_at` takes for the same reason.
    __audit_redact__ = frozenset({"public_key", "sign_count", "last_used_at", "backed_up"})

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: base64url, as every client and the JSON API spell it. The spec allows
    #: up to 1023 bytes, which is 1364 characters.
    credential_id: Mapped[str] = mapped_column(String(1400), nullable=False, unique=True, index=True)
    #: COSE-encoded, as `webauthn` hands it over and wants it back.
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: The authenticator's signature counter at its last use. Synced passkeys
    #: mostly report 0 forever; a counter that goes backwards on one that does
    #: count is refused by `webauthn` as a cloned authenticator.
    sign_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: What the browser said it can reach this authenticator over ("internal",
    #: "hybrid", "usb"...), handed back as a hint when signing in.
    transports: Mapped[list | None] = mapped_column(JSON)
    #: The member's own name for it. Defaults to the provider the AAGUID
    #: names ("iCloud Keychain") or "Passkey".
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The host name it was registered under, and the only one it will ever
    #: work for (#47 §1.2). Stored so a changed `SPENDTRACKER_RP_ID` -- a
    #: renamed machine, a restore onto another host -- is reported as such
    #: rather than as passkeys that silently stopped working.
    rp_id: Mapped[str] = mapped_column(String(253), nullable=False)
    #: Which provider made it, as a UUID string; all zeros when it would not say.
    aaguid: Mapped[str | None] = mapped_column(String(36))
    #: May be synced to other devices (the BE flag) ...
    backup_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: ... and is (BS), as of its last use. What "synced" or "this device only"
    #: on the member's list is read from.
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship("User", back_populates="passkeys")


class WebAuthnChallenge(Base):
    """A challenge handed to a browser for one ceremony, and spent by it.

    Keyed by the challenge's SHA-256, so the row is found from the challenge
    the browser signed (`clientDataJSON`) rather than from a cookie, and spent
    with one `DELETE ... RETURNING` the way `stepup.claim` spends a grant
    (#206): two requests presenting one challenge cannot both succeed.

    Not audited: like `step_up_grants` it records what happened at the door.
    Swept by `auth/housekeeping.py` once expired.
    """

    __tablename__ = "webauthn_challenges"
    __audit__ = False

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: Whose registration this is. NULL for a sign-in, where nobody is known
    #: until the passkey says who it belongs to.
    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    #: "register" or "sign_in": a challenge issued for one ceremony is no use
    #: to the other.
    purpose: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class AgentRequest(Base, UUIDPrimaryKey):
    """What a key asked for: a record of what happened at the door.

    The audit log records writes. An analysis agent that pulled the entire
    register leaves no trace in it at all -- so "what did that key actually do
    last Tuesday" had no answer, and *"it read everything"* is the answer worth
    having. This is that answer.

    Not audited, exactly like `login_attempts`, and for the same reason: it
    records attempts at the door rather than changes to the ledger. Auditing it
    would mean every GET demanded a batch, which is the thing
    `agent_keys.last_used_at` is redacted to avoid.

    It doubles as the rate-limit counter, so it is not a table bought for one
    purpose. The shape is not quite `ratelimit`'s, though, and the difference
    is worth naming: that module counts **failures** in a window to slow down
    guessing, while this counts **every request** to catch a runaway loop. Same
    lockout arithmetic, different predicate -- a sibling of that module rather
    than a caller of it.
    """

    __tablename__ = "agent_requests"
    __audit__ = False

    #: Deliberately NOT a foreign key, like `login_attempts.email_canonical`.
    #: Keys are swept thirty days after revocation and this log outlives them:
    #: a RESTRICT would block the sweep, and a CASCADE would erase the record
    #: of what the key did at the moment somebody most wants to read it.
    agent_key_id: Mapped[str | None] = mapped_column(String(32), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    #: The route TEMPLATE, never the URL. "/api/agent/v1/households/{id}/summary".
    #: The real path carries ids, and a log is not a place to accumulate them --
    #: this table is swept on a window precisely so it does not become one.
    route: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Rows read or written. The number that makes the line worth reading:
    #: "GET /summary 200" says nothing, "GET /summary 200, 4,113 rows" says
    #: what the key actually took.
    rows: Mapped[int | None] = mapped_column(Integer)
    #: Which batch it opened, when it wrote. The join from "what was asked for"
    #: to "what changed", and null for every read.
    batch_id: Mapped[str | None] = mapped_column(String(32), index=True)

    __table_args__ = (Index("ix_agent_requests_key_at", "agent_key_id", "at"),)


class AgentReplay(Base, UUIDPrimaryKey):
    """What a key's `Idempotency-Key` produced, so a retry gets it again.

    Agents retry. Without this, a timeout on a request that actually succeeded
    is indistinguishable from one that failed, and the honest thing for the
    agent to do -- try again -- is the thing that imports the rows twice.

    The digest guard in `importing.previous_import_of` catches the *same rows*
    arriving twice and answers 409. That is the right answer to a mistake and
    the wrong one to a retry: a retry should get the first reply, not an error
    about itself. Two mechanisms because they answer two different questions.

    Not audited. Like `agent_requests` it records what happened at the door --
    and auditing it would make replying to a retry demand a batch.
    """

    __tablename__ = "agent_replays"
    __audit__ = False

    #: Not a foreign key, for the reason `agent_requests.agent_key_id` is not:
    #: keys are swept and this outlives them. Scoped BY key as well as by the
    #: header, so one agent's key cannot collide with or read another's.
    agent_key_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: Whatever the client sent. Its own value, never hashed: this is not a
    #: credential, it is a label the caller chose, and a person debugging a
    #: double import needs to be able to match it against their own logs.
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The route it was spent on. The same header on a different endpoint is a
    #: different request, and replaying one endpoint's answer to another would
    #: be worse than not replaying at all.
    route: Mapped[str] = mapped_column(String(120), nullable=False)
    #: SHA-256 of the canonicalised body. A retry that changed the rows is not
    #: a retry, and answering it with the first result would silently discard
    #: what it actually asked for.
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)

    __table_args__ = (
        #: One answer per (key, header, route). The database refuses a second,
        #: which is what makes this true under two requests racing rather than
        #: only under two arriving politely in turn.
        UniqueConstraint(
            "agent_key_id", "idempotency_key", "route", name="uq_agent_replays_key"
        ),
    )
