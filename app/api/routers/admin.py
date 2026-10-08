"""The admin panel: people, the links that reset their sign-in, and every
household on this instance.

Owner-only, and 403 rather than 404 throughout -- someone signed in as a member
knows perfectly well that other people and other households exist, so hiding
them would be theatre rather than privacy.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select
from starlette.background import BackgroundTask

from ... import __version__, config, db, logging_setup
from ...audit.batch import batch
from ...auth import keycheck, stepup
from ...errors import Conflict, NotFound, ValidationError
from ...models import AccountReset, BatchKind, HouseholdMember, Role, User, utcnow
from ...schemas import (
    AdminHouseholdCreate,
    AdminHouseholdOut,
    AdminUserOut,
    BackupOut,
    DownloadBackup,
    InstanceOut,
    LogFileOut,
    LoggingOut,
    LoggingStyleOut,
    LogStreamOut,
    LogTextOut,
    PendingResetOut,
    RecoveryModeOut,
    ResetIssue,
    ResetIssued,
    SetDisabled,
    SetLoggingStyle,
    SetRole,
    SignInChangeOut,
    UpstreamOut,
)
from ...services import account_resets as reset_service
from ...services import backup_bundle, sign_in_changes
from ...services import platform as platform_service
from ...services import users as user_service
from ..deps import OwnerOnly, SessionDep, public_origin

router = APIRouter(tags=["admin"], prefix="/admin")

log = logging.getLogger("spendtracker")

#: When this process came up, for the uptime line on the Application
#: management page. Set at import, which is the closest thing to "when the
#: server started" that a module can observe without being handed it -- and the
#: difference between import and the first request is milliseconds.
_STARTED_AT = datetime.now(UTC)


def _user_out(session, user: User, memberships: dict[str, list[str]]) -> AdminUserOut:
    out = AdminUserOut.model_validate(user)
    out.recovery_codes_left = user_service.unused_recovery_codes(session, user.id)
    out.households = memberships.get(user.id, [])
    return out


def _memberships(session) -> dict[str, list[str]]:
    rows = session.execute(select(HouseholdMember)).scalars()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row.user_id, []).append(row.household_id)
    return out


@router.get("/users", response_model=list[AdminUserOut])
def list_users(session: SessionDep, owner: OwnerOnly) -> list[AdminUserOut]:
    memberships = _memberships(session)
    return [_user_out(session, user, memberships) for user in user_service.list_users(session)]


@router.get("/recovery-mode", response_model=RecoveryModeOut)
def recovery_mode(session: SessionDep, owner: OwnerOnly) -> RecoveryModeOut:
    """How many members' authenticators this server's key cannot open (#287).

    Asked of the key on every request, never stored, so the banner it feeds
    -- which asks every minute -- goes once the original key is back or the
    last member re-enrols.
    One AES-GCM open per enrolled member: a household's worth is nothing.
    It also says where the key came from, so the banner names what to fix.
    """
    found = keycheck.in_session(session)
    return RecoveryModeOut(
        enrolled=found.enrolled,
        locked=len(found.refused),
        key_from_environment=keycheck.key_from_environment(),
    )


@router.post("/users/{user_id}/disabled", response_model=AdminUserOut)
def set_disabled(
    user_id: str, body: SetDisabled, session: SessionDep, owner: OwnerOnly
) -> AdminUserOut:
    """Disabling signs them out everywhere and forgets their trusted browsers.

    It does not remove their work: their name stays on every batch they ran,
    which is the reason an actor is recorded at all.
    """
    user = user_service.get_user(session, user_id)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        user_service.set_disabled(session, user=user, disabled=body.disabled, by=owner)
    return _user_out(session, user, _memberships(session))


@router.post("/users/{user_id}/role", response_model=AdminUserOut)
def set_role(user_id: str, body: SetRole, session: SessionDep, owner: OwnerOnly) -> AdminUserOut:
    """Change somebody's role. Making them an **owner** spends a step-up grant.

    The same reason an owner-role invitation does (#205): an owner outlives the
    session that made them, and nothing the promoting owner does to their own
    password or authenticator afterwards reaches it. Demoting, and leaving an
    owner an owner, need no grant -- taking authority away is the safe
    direction, as revoking a key is.
    """
    user = user_service.get_user(session, user_id)
    if Role(body.role) is Role.owner and user.role is not Role.owner:
        stepup.require(db.engine, body.step_up_token, user_id=owner.id)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        user_service.set_role(session, user=user, role=Role(body.role), by=owner)
    return _user_out(session, user, _memberships(session))


# --------------------------------------------------------------------------- #
# Account resets (#286)
# --------------------------------------------------------------------------- #
#
# Any account, another owner's included, and no step-up: decided in #283. The
# safeguard is that it is seen -- by the person reset, on the link page, and by
# every owner, in `sign-in-changes` below, which reads the audit log.


@router.post("/users/{user_id}/reset", response_model=ResetIssued, status_code=201)
def issue_reset(
    user_id: str, body: ResetIssue, request: Request, session: SessionDep, owner: OwnerOnly
) -> ResetIssued:
    """Shut the account and hand back a one-time link, shown once.

    The account's sessions, trusted browsers and agent keys end now, and the
    password, the authenticator or both are reset now -- not when the link is
    followed. See `services/account_resets.py`. Nothing is sent anywhere: the
    owner hands the link over themselves.
    """
    user = user_service.get_user(session, user_id)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        reset, token = reset_service.issue_by_owner(
            session, user, by=owner, password=body.password, authenticator=body.authenticator
        )
    return ResetIssued(
        link=reset_service.link_for(token, base=public_origin(request)),
        expires_at=reset.expires_at,
        password=reset.password,
        authenticator=reset.authenticator,
    )


def _pending_out(session, reset: AccountReset, now: datetime) -> PendingResetOut:
    by = session.get(User, reset.created_by_id) if reset.created_by_id else None
    return PendingResetOut(
        id=reset.id,
        user_id=reset.user_id,
        display_name=reset.user.display_name,
        email=reset.user.email,
        password=reset.password,
        authenticator=reset.authenticator,
        issued_by=by.display_name if by is not None else None,
        created_at=reset.created_at,
        expires_at=reset.expires_at,
        expired=reset.expires_at <= now,
    )


@router.get("/resets", response_model=list[PendingResetOut])
def list_resets(session: SessionDep, owner: OwnerOnly) -> list[PendingResetOut]:
    """Every link not yet followed or withdrawn, lapsed ones marked."""
    now = utcnow()
    return [_pending_out(session, one, now) for one in reset_service.pending(session)]


@router.delete("/resets/{reset_id}", status_code=204)
def withdraw_reset(reset_id: str, session: SessionDep, owner: OwnerOnly) -> Response:
    """The link stops working. The account stays shut: the way in is a new link."""
    reset = reset_service.get(session, reset_id)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        reset_service.withdraw(session, reset, by=owner)
    return Response(status_code=204)


@router.get("/sign-in-changes", response_model=list[SignInChangeOut])
def list_sign_in_changes(session: SessionDep, owner: OwnerOnly) -> list[SignInChangeOut]:
    """Resets, new owners and the server's acts on an account's way in, over the
    last fourteen days, read from the audit log. See `services/sign_in_changes.py`."""
    return [SignInChangeOut(**asdict(one)) for one in sign_in_changes.recent(session)]


@router.get("/households", response_model=list[AdminHouseholdOut])
def list_households(session: SessionDep, owner: OwnerOnly) -> list[AdminHouseholdOut]:
    """Every household, including ones the owner is not in.

    The owner can see that they exist and who is in them; joining one is still
    an explicit act, recorded like any other.
    """
    memberships: dict[str, list[str]] = {}
    for row in session.execute(select(HouseholdMember)).scalars():
        memberships.setdefault(row.household_id, []).append(row.user_id)

    out = []
    for household in user_service.all_households(session):
        model = AdminHouseholdOut.model_validate(household)
        model.member_ids = memberships.get(household.id, [])
        out.append(model)
    return out


@router.post("/households", response_model=AdminHouseholdOut, status_code=201)
def create_household(
    body: AdminHouseholdCreate, session: SessionDep, owner: OwnerOnly
) -> AdminHouseholdOut:
    """Create a household and put people in it, as one act."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        household = user_service.create_household_for(
            session,
            name=body.name,
            creator=owner,
            base_currency=body.base_currency,
            date_format=body.date_format,
            members=body.member_ids,
        )
    model = AdminHouseholdOut.model_validate(household)
    model.member_ids = [
        row.user_id
        for row in session.execute(
            select(HouseholdMember).where(HouseholdMember.household_id == household.id)
        ).scalars()
    ]
    return model


# --------------------------------------------------------------------------- #
# Application management
# --------------------------------------------------------------------------- #
#
# The instance, rather than any ledger in it: how big it has got, where its
# files are, what it is made of, what it has been saying, and four things an
# owner can do to it.
#
# **Owner-only throughout, and that is the control rather than a courtesy.**
# These answers name every household on the instance and count its rows, say
# where the database and the secret key are on disk, and hand back whatever the
# application has written about a request. `OwnerOnly` is the dependency on
# every route below; `tests/test_application_management.py` walks them and
# asserts a member gets 403 from each.
#
# Nothing here is reachable with an agent key either, and it is structural
# rather than a rule somebody remembered: a key can only reach a route that
# depends on `current_agent`, and none of these does.


@router.get("/application", response_model=InstanceOut)
def application(request: Request, session: SessionDep, owner: OwnerOnly) -> InstanceOut:
    """What this instance is, opened on every visit to the page.

    The port comes off the request so the addresses block can say what to type
    into a phone. It is the one thing here the server cannot work out on its
    own -- uvicorn is given `--port` on a command line this process never sees
    as configuration.
    """
    described = platform_service.describe(
        session, port=request.url.port, started_at=_STARTED_AT
    )
    return InstanceOut.model_validate(asdict(described))


@router.get("/application/tables.csv", response_class=PlainTextResponse)
def table_report(session: SessionDep, owner: OwnerOnly) -> PlainTextResponse:
    """Every table, its rows and its size, as a CSV file.

    A download rather than a block on the page, and not only because the review
    asked for one: without `dbstat` compiled in, the byte figures come from
    scanning every table, which is not a thing to do on a page that redraws.

    The header names which of the two measurements produced the numbers,
    because they are not comparable -- `dbstat` counts SQLite's pages, indexes
    and slack; the fallback counts the bytes of the values themselves and will
    always be the smaller figure.
    """
    report = platform_service.table_report(session)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["table", "rows", "bytes", "measured_by"])
    for row in report.rows:
        writer.writerow([row.name, row.rows, "" if row.bytes is None else row.bytes, report.method])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="household-spend-tracker-tables-{stamp}.csv"'
        },
    )


@router.get("/application/logging", response_model=LoggingOut)
def logging_state(owner: OwnerOnly) -> LoggingOut:
    return LoggingOut(
        current=logging_setup.current().key,
        styles=[
            LoggingStyleOut(
                key=style.key, label=style.label, blurb=style.blurb, echo_sql=style.echo_sql
            )
            for style in logging_setup.STYLES
        ],
        files=[LogFileOut(**asdict(one)) for one in logging_setup.files()],
        directory=str(logging_setup.log_dir()),
        streams=[
            LogStreamOut(
                key=stream.key,
                label=stream.label,
                filename=stream.filename,
                blurb=stream.blurb,
                holds_ledger_values=stream.key == "sql",
            )
            for stream in logging_setup.STREAMS
        ],
    )


@router.post("/application/logging", response_model=LoggingOut)
def set_logging_style(
    body: SetLoggingStyle, session: SessionDep, owner: OwnerOnly
) -> LoggingOut:
    """Change how much this instance writes down, and remember the choice.

    Takes effect on the running process -- the point of putting it here rather
    than in an environment variable is that turning the detail up does not mean
    restarting the thing you were trying to watch.

    **Recorded in every log file, on both sides of the change, and below the
    level checks** -- so turning the detail *down* leaves a record too. It did
    not before: the old line here was written at the new level, and switching
    to `quiet` raised that level first, so the one transition somebody would go
    looking for afterwards was the one that vanished. `logging_setup.mark` has
    the rest.
    """
    try:
        # `by` rather than a second log line: the record has to go through
        # `mark`, and a `log.warning` here would be dropped for exactly the
        # reason above.
        logging_setup.choose(body.style, by=owner.email)
    except KeyError:
        raise ValidationError(
            f"{body.style!r} is not a logging style; "
            f"choose one of {', '.join(one.key for one in logging_setup.STYLES)}"
        ) from None
    return logging_state(owner)


@router.get("/application/logs/{name}", response_model=LogTextOut)
def read_log(name: str, owner: OwnerOnly) -> LogTextOut:
    """The end of one log file.

    `name` is checked against the listing rather than joined onto a directory --
    see `logging_setup.tail`, which is where the reasoning is. A name that is
    not in the listing is a 404 and not a path.
    """
    try:
        text = logging_setup.tail(name)
    except FileNotFoundError:
        raise NotFound("no such log file") from None
    return LogTextOut(name=name, bytes=len(text.encode("utf-8")), text=text)


@router.get("/application/backups", response_model=list[BackupOut])
def list_backups(owner: OwnerOnly) -> list[BackupOut]:
    return [BackupOut(**asdict(one)) for one in platform_service.backups()]


@router.post("/application/backups", response_model=BackupOut, status_code=201)
def make_backup(owner: OwnerOnly) -> BackupOut:
    """Write a consistent copy of the database into `data/backups`.

    On a threadpool worker already -- this is a sync route -- so a large
    database does not stall the event loop while SQLite writes it out.

    `VACUUM INTO`, not a file copy: see `services/platform.make_backup` for why
    copying a WAL database's main file is a backup that silently loses every
    write since the last checkpoint.
    """
    try:
        made = platform_service.make_backup()
    except platform_service.BackupFailed as problem:
        raise ValidationError(str(problem)) from problem
    log.warning("database backed up to %s by %s", made.path, owner.email)
    return BackupOut(**asdict(made))


def _bundle_response(name: str, owner: User, *, with_key: bool) -> FileResponse:
    """One backup as a zip that explains itself: the ledger, a manifest, a README.

    See `services/backup_bundle.py` for what the zip holds and why each half of
    the key choice costs what it does (#133, decided 2026-09-25).

    The file is checked before it is handed out -- `integrity_check`, its
    revision read, its rows counted -- and a copy that fails is a 422 with the
    reason, not a zip somebody keeps for a year believing it is a backup.

    Built into a private temp file beside the backups and removed once the
    response has been sent. A ledger with receipts has been measured at 725
    MiB, which is not a thing to hold in memory; `housekeeping` removes any
    zip a crash left behind.
    """
    found = platform_service.find_backup(name)
    if found is None:
        raise NotFound("no such backup")
    try:
        bundle = backup_bundle.build(
            found.database,
            into=platform_service.backup_dir(),
            version=__version__,
            repository=platform_service.REPOSITORY,
            # Read now, not bound at import: see `platform._settings`.
            secret_key=config.settings.secret_key if with_key else None,
        )
    except backup_bundle.BundleRefused as refused:
        raise ValidationError(str(refused)) from refused
    log.warning(
        "backup %s downloaded %s secret.key by %s",
        found.name,
        "WITH" if bundle.with_key else "without",
        owner.email,
    )
    return FileResponse(
        bundle.path,
        media_type="application/zip",
        filename=bundle.filename,
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(bundle.path.unlink, missing_ok=True),
    )


@router.get("/application/backups/{name}/download", response_class=FileResponse)
def download_backup(name: str, request: Request, owner: OwnerOnly) -> FileResponse:
    """The zip **without** `secret.key`, as a plain link the browser streams.

    A GET can never carry the key (#204). It used to take `?include_key=true`
    on nothing but the session cookie, which made a lifted owner cookie worth
    every member's second factor, portable and forever. The key now goes only
    through the POST below, which spends a step-up grant. An old page still
    asking for it here is refused in words rather than quietly handed a zip
    without the key it asked for.
    """
    if (request.query_params.get("include_key") or "").lower() in {"1", "true", "yes", "on"}:
        raise ValidationError(
            "a zip with secret.key needs your password and a code: reload the page and try again"
        )
    return _bundle_response(name, owner, with_key=False)


@router.post("/application/backups/{name}/download", response_class=FileResponse)
def download_backup_with_choice(
    name: str, body: DownloadBackup, owner: OwnerOnly
) -> FileResponse:
    """The zip, with `secret.key` in it when asked -- which spends a step-up grant.

    The zip then holds every password hash, every recovery-code hash, and the
    key that opens every member's authenticator secret: strictly more than an
    agent key buys, so the bar is the same one `profile.issue_key` sets. The
    grant is spent **before** the file is even located, and spent whether or
    not the zip is then built: one grant, one download.
    """
    if body.include_key:
        stepup.require(db.engine, body.step_up_token, user_id=owner.id)
    return _bundle_response(name, owner, with_key=body.include_key)


@router.delete("/application/backups/{name}", status_code=204)
def delete_backup(name: str, owner: OwnerOnly) -> Response:
    """Remove one backup -- a file, or a folder. The screen asks first; this
    does not ask again.

    A file, not a row, so there is no batch and no undo -- the log line is the
    record, and it names who. The newest and the last one left are deletable
    too (#133): the confirmation says so in words rather than the server
    refusing, because the person may be removing it precisely because it holds
    something that should not exist.

    **Except the newest five update backups** (design notes 8.7): 409, and it
    stays. Those are what a failed update is undone from and what the recovery
    page restores, and the updater prunes the older ones itself.
    """
    try:
        gone = platform_service.delete_backup(name)
    except platform_service.BackupProtected:
        raise Conflict(
            f"{name} is one of the newest five update backups. They are kept so an update "
            "can be undone, and the updater removes older ones itself",
            code="backup.protected",
            params={"name": name},
        ) from None
    if gone is None:
        raise NotFound("no such backup")
    log.warning("backup %s deleted by %s", gone.name, owner.email)
    return Response(status_code=204)


@router.post("/application/upstream", response_model=UpstreamOut)
def check_upstream(owner: OwnerOnly) -> UpstreamOut:
    """Ask the repository for its published releases newer than this one.

    A `POST` for a read, deliberately: it reaches outside the instance, and it
    happens when an owner presses a button and at no other time. Making it a
    `GET` invites a prefetch, a reload or a monitoring check to turn "no
    telemetry" into a periodic call home that nobody chose. `outbound.py` lists
    every request that leaves the instance, and the rule they all keep.

    A failure is an answer, not a 500. An instance on a network with no route
    out is the normal case for this app.
    """
    return UpstreamOut(**asdict(platform_service.check_upstream()))
