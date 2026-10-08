"""Self-update, from the owner's side: the endpoints the Updates section reads.

Design notes, Parts 3, 5 and 10. The app never touches the container engine.
It writes one request at a time into the volume it shares with the updater,
and reads back what the updater writes; `services/updates.py` is that half of
the contract, and the updater's checks are the ones that count (5.4).

**Owner-only, 403 for a member** -- `OwnerOnly` on every route, as on the rest
of the Application screen. Roles are instance-wide and these are instance
endpoints, so the household 404-not-403 rule does not apply.

**Nothing here makes an outbound request.** `GET` reads files; every `POST`
writes one. The one request that leaves the instance on this screen is the
check, `POST /admin/application/upstream`, and only when it is pressed.

**What is logged** (10.4): prepared, confirmed (with the lossy revisions
accepted), discarded, at WARNING, with the owner's email. The recovery code is
never logged; only its hash leaves this process, inside the apply request.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Response

from ... import __version__, db
from ...auth import stepup
from ...errors import Conflict, NotFound, ValidationError
from ...schemas import (
    BackupOut,
    UpdateApply,
    UpdateDiscard,
    UpdateHeartbeatOut,
    UpdateOutcomeOut,
    UpdatePrepare,
    UpdateRecoveryCodeOut,
    UpdateReportOut,
    UpdateRequestOut,
    UpdateStateOut,
    UpdateStatusOut,
    UpdateUpdater,
)
from ...services import platform as platform_service
from ...services import updates
from ..deps import OwnerOnly

router = APIRouter(tags=["admin"], prefix="/admin/application/update")

log = logging.getLogger("spendtracker")


def _one_line(value: object) -> str:
    """A value for a log line, with no line breaks: what reaches these lines is
    validated first, but a log line should not depend on that to stay one line."""
    return str(value).replace("\r", "").replace("\n", "")

#: The socket states under which the updater still works (C3): `outdated`
#: refuses prepare and apply but still attempts `update_updater`.
_USABLE = ("ok", "outdated")


def _case(beat: updates.Heartbeat | None) -> str:
    """Which of 3.1's cases this instance is in."""
    if beat is None or not beat.fresh:
        return "no_updater" if updates.in_a_container() else "not_container"
    if beat.socket == "outdated":
        return "outdated"
    if beat.socket != "ok":
        return "refused"
    return "working"


@router.get("", response_model=UpdateStateOut)
def update_state(owner: OwnerOnly) -> UpdateStateOut:
    """Heartbeat, status, the newest current report, the newest outcome not yet
    dismissed, and the update backups. Files only; no outbound request."""
    beat = updates.heartbeat()
    current = updates.status()
    found = updates.newest_report()
    outcome = updates.newest_outcome()
    return UpdateStateOut(
        case=_case(beat),
        running=__version__,
        protocol=updates.PROTOCOL,
        in_flight=updates.in_flight(),
        heartbeat=UpdateHeartbeatOut(**asdict(beat)) if beat else None,
        status=UpdateStatusOut(**asdict(current)) if current else None,
        report=UpdateReportOut(**asdict(found)) if found else None,
        outcome=UpdateOutcomeOut(**asdict(outcome)) if outcome else None,
        backups=[
            BackupOut(**asdict(one)) for one in platform_service.backups() if one.kind == "update"
        ],
    )


# --------------------------------------------------------------------------- #
# What the app checks before writing (5.2, 5.4)
# --------------------------------------------------------------------------- #


def _updater_answers(*, kind: str) -> updates.Heartbeat:
    """A fresh heartbeat from an updater that can do `kind`, or a refusal."""
    beat = updates.heartbeat()
    if beat is None or not beat.fresh:
        raise ValidationError(
            "Updating from this screen needs the updater, and none has answered in the "
            "last two minutes",
            code="update.no_updater",
        )
    if beat.socket not in _USABLE:
        raise ValidationError(
            f"The updater cannot use the container engine ({beat.socket}), so it cannot "
            "update anything",
            code="update.engine_refused",
            params={"socket": str(beat.socket)},
        )
    if beat.socket == "outdated" and kind in ("prepare", "apply"):
        raise ValidationError(
            "The updater is too old for this container engine. Update the updater first, "
            "then try again",
            code="update.updater_outdated",
        )
    return beat


def _nothing_in_flight() -> None:
    if updates.in_flight():
        raise Conflict(
            "Another update request is waiting or running. Wait for it to finish, then try again",
            code="update.in_flight",
        )


def _a_version(value: str) -> str:
    if not updates.is_version(value):
        raise ValidationError(
            f"{value} is not a release version like 1.2.3",
            code="update.not_a_version",
            params={"version": value},
        )
    return value


def _write(doc: dict) -> None:
    """Write the request, turning the volume's refusals into the owner's."""
    try:
        updates.write_request(doc)
    except updates.RequestPending:
        raise Conflict(
            "Another update request is waiting or running. Wait for it to finish, then try again",
            code="update.in_flight",
        ) from None
    except updates.NoVolume:
        raise ValidationError(
            "Updating from this screen needs the updater, and none has answered in the "
            "last two minutes",
            code="update.no_updater",
        ) from None


_NO_REPORT = (
    "There is no prepared update with that id for this version. Check for updates and "
    "prepare it again"
)


# --------------------------------------------------------------------------- #
# The requests
# --------------------------------------------------------------------------- #


@router.post("/prepare", response_model=UpdateRequestOut, status_code=202)
def prepare(body: UpdatePrepare, owner: OwnerOnly) -> UpdateRequestOut:
    """Ask the updater to fetch, verify and dry-run `to_version`. No step-up (A14):
    preparing changes nothing the owner has, and can fail without harm (3.3)."""
    to_version = _a_version(body.to_version)
    if updates.numbers(to_version) <= updates.numbers(__version__):
        raise ValidationError(
            f"{to_version} is not newer than {__version__}, which this instance runs. "
            "An update never goes back",
            code="update.not_newer",
            params={"to_version": to_version, "running": __version__},
        )
    _updater_answers(kind="prepare")
    _nothing_in_flight()
    doc = updates.prepare_request(to_version, requested_by=owner.id)
    _write(doc)
    log.warning(
        "update to %s prepared (request %s) by %s",
        _one_line(to_version),
        doc["id"],
        _one_line(owner.email),
    )
    return UpdateRequestOut(id=doc["id"], kind="prepare", to_version=to_version)


@router.post("/recovery-code", response_model=UpdateRecoveryCodeOut)
def recovery_code(response: Response, owner: OwnerOnly) -> UpdateRecoveryCodeOut:
    """A fresh recovery code for the confirmation of the current report (3.4, 11.2).

    **A `POST`, not a `GET`** (R21): issuing a code replaces the one held, so
    it changes state. As a `GET` a cross-site request could not read the code
    but could rotate it, and the owner's *Update* would then be refused. As a
    `POST` it is behind the same Origin check as every other unsafe method.

    Shown once. Only its scrypt hash is kept, in this process, for ten
    minutes, bound to this report and this owner; *Update* spends it. Drawing
    the confirmation again issues a new one and forgets the last.
    """
    found = updates.newest_report()
    if found is None:
        raise NotFound(_NO_REPORT, code="update.no_report")
    code_id, code, expires = updates.issue_recovery_code(found.id, owner.id)
    response.headers["Cache-Control"] = "no-store"
    return UpdateRecoveryCodeOut(
        id=code_id,
        code=code,
        prepared_id=found.id,
        expires_at=datetime.fromtimestamp(expires, UTC),
    )


@router.post("/apply", response_model=UpdateRequestOut, status_code=202)
def apply(body: UpdateApply, owner: OwnerOnly) -> UpdateRequestOut:
    """Install the prepared update. **Spends the step-up grant first.**

    The grant goes before anything is read, and whether or not the request is
    then written -- one grant, one attempt -- because this stops the service
    and changes the schema. Then the report, the digests and the exact lossy
    set are checked, the recovery code's hash is attached, and the request is
    written. The updater checks all of it again (5.4).
    """
    stepup.require(db.engine, body.step_up_token, user_id=owner.id)
    found = updates.report(body.prepared_id)
    if found is None:
        raise ValidationError(_NO_REPORT, code="update.no_report")
    if (
        not updates.DIGEST.fullmatch(body.digest)
        or not updates.DIGEST.fullmatch(body.updater_digest)
        or body.digest != found.digest
        or body.updater_digest != found.updater_digest
    ):
        raise ValidationError(
            "The image digests are not the ones the prepare report verified. Prepare the "
            "update again",
            code="update.digest_mismatch",
        )
    accepted = body.accepted_lossy
    if len(set(accepted)) != len(accepted) or set(accepted) != set(found.lossy):
        raise ValidationError(
            "Tick every migration that cannot be undone, and only those: the update needs "
            "exactly the ones the report lists",
            code="update.lossy_mismatch",
        )
    _updater_answers(kind="apply")
    _nothing_in_flight()
    hashed = updates.take_recovery_hash(
        body.recovery_code_id, prepared_id=found.id, user_id=owner.id
    )
    if hashed is None:
        raise ValidationError(
            "That recovery code has expired or belongs to another confirmation. Draw the "
            "confirmation again for a new one",
            code="update.recovery_code_unknown",
        )
    doc = updates.apply_request(
        found,
        digest=body.digest,
        updater_digest=body.updater_digest,
        accepted_lossy=list(accepted),
        recovery_hash=hashed,
        requested_by=owner.id,
    )
    _write(doc)
    log.warning(
        "update to %s confirmed (request %s, report %s), accepting lossy migrations [%s], by %s",
        _one_line(found.to_version),
        doc["id"],
        found.id,
        _one_line(", ".join(sorted(accepted)) or "none"),
        _one_line(owner.email),
    )
    return UpdateRequestOut(id=doc["id"], kind="apply", to_version=found.to_version)


@router.post("/discard", response_model=UpdateRequestOut, status_code=202)
def discard(body: UpdateDiscard, owner: OwnerOnly) -> UpdateRequestOut:
    """Ask the updater to delete the report and the images it pulled.

    The report itself is the updater's to delete: its `discard` is refused
    when the report is already gone (`updater.contract.check_report`).
    """
    found = updates.report(body.prepared_id)
    if found is None:
        raise NotFound(_NO_REPORT, code="update.no_report")
    _updater_answers(kind="discard")
    _nothing_in_flight()
    doc = updates.discard_request(found.id, requested_by=owner.id)
    _write(doc)
    log.warning(
        "prepared update to %s discarded (report %s, request %s) by %s",
        _one_line(found.to_version),
        found.id,
        doc["id"],
        _one_line(owner.email),
    )
    return UpdateRequestOut(id=doc["id"], kind="discard", to_version=found.to_version)


@router.post("/updater", response_model=UpdateRequestOut, status_code=202)
def update_updater(owner: OwnerOnly, body: UpdateUpdater | None = None) -> UpdateRequestOut:
    """Replace the updater only: the running release's, or a newer one's (C2).

    No step-up: it can only install a genuine updater, and an updater cannot
    touch the ledger without an apply. `422` when the named updater could not
    take this app's requests, as far as the app can tell: older than this app
    (so outside its protocol window, which only widens), or not newer than the
    updater running. Whether a newer one's window includes protocol 1 is on its
    image, and the updater checks that before it installs anything (R8).
    """
    to_version = _a_version(body.to_version if body and body.to_version else __version__)
    if updates.numbers(to_version) < updates.numbers(__version__):
        raise ValidationError(
            f"The updater of {to_version} is older than this instance, which runs "
            f"{__version__}, so it would not accept its requests",
            code="update.updater_older_than_app",
            params={"to_version": to_version, "running": __version__},
        )
    beat = _updater_answers(kind="update_updater")
    running_updater = beat.updater_version
    if updates.is_version(running_updater) and updates.numbers(to_version) <= updates.numbers(
        str(running_updater)
    ):
        raise ValidationError(
            f"The updater already runs {running_updater}, which is not older than {to_version}",
            code="update.updater_not_newer",
            params={"to_version": to_version, "updater_version": str(running_updater)},
        )
    _nothing_in_flight()
    doc = updates.update_updater_request(to_version, requested_by=owner.id)
    _write(doc)
    log.warning(
        "updater update to %s requested (request %s) by %s",
        _one_line(to_version),
        doc["id"],
        _one_line(owner.email),
    )
    return UpdateRequestOut(id=doc["id"], kind="update_updater", to_version=to_version)


@router.post("/outcome/{record_id}/seen", status_code=204)
def outcome_seen(record_id: str, owner: OwnerOnly) -> Response:
    """Dismiss an outcome from the top of the section (3.8). The record stays."""
    if not updates.dismiss(record_id):
        raise NotFound("There is no update outcome with that id", code="update.outcome_not_found")
    return Response(status_code=204)
