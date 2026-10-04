"""Uploading a receipt, reading one back, and moving it between rows.

Seven routes. The one that matters most is the second one -- the previous build
had no route that served a receipt back at all, for the whole life of the
feature, and `data/uploads/` was empty after 413 real transactions. Nothing
said so, because nothing could.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ...audit.batch import batch
from ...errors import Conflict, NotFound, ValidationError
from ...models import BatchKind, BlobRole, Household, Payee, Receipt, Transaction, User
from ...schemas import ReceiptBulkDelete, ReceiptOut, ReceiptUpdate, ReceiptUpload
from ...services import receipts as receipt_service
from ...services import transactions as txn_service
from ..deps import CurrentHousehold, CurrentUser, SessionDep, load_for
from ..offload import run_cpu
from ..uploads import read_capped, refuse_declared_size

router = APIRouter(tags=["receipts"])

TOO_BIG = (
    "that file is larger than this is meant for. A photograph of a receipt is "
    "a few megabytes; the limit here is 25."
)

#: A year, and `immutable`, which is correct and *safe* here in a way it almost
#: never is: the URL identifies content that cannot change. A different image
#: is a different sha256 and therefore a different receipt id. The browser
#: fetches each thumbnail once, ever.
#:
#: `private`, not `public`, because a tailnet may still have a proxy on it.
CACHE = "private, max-age=31536000, immutable"

#: This route only. Belt to the `attachment` disposition's braces: an
#: attachment disposition does not stop `<img src>` rendering, so the frame
#: costs nothing, but a direct navigation to the URL cannot execute anything
#: either.
BYTES_CSP = "default-src 'none'; sandbox"


def _uploader_names(session, receipts: list[Receipt]) -> dict[str, str]:
    ids = {one.uploaded_by_id for one in receipts if one.uploaded_by_id}
    if not ids:
        return {}
    return {
        row.id: row.display_name
        for row in session.execute(select(User).where(User.id.in_(ids))).scalars()
    }


def _out(session, receipt: Receipt, *, household: Household, index: int = 0) -> ReceiptOut:
    model = ReceiptOut.model_validate(receipt)
    txn = receipt.transaction
    payee_name = None
    if txn is not None and txn.payee_id:
        payee = session.get(Payee, txn.payee_id)
        payee_name = payee.name if payee else None
    # The name is for the link the panel shows, so it carries the extension of
    # the copy that link actually serves: the original where one was kept -- a
    # PDF, whose later pages a page-1 raster throws away -- and the AVIF
    # display copy otherwise. Naming a download `.jpeg` when the bytes are AVIF
    # is the same class of lie as the previous build storing the browser's
    # Content-Type as fact.
    original = receipt_service.blob_for(session, receipt.blob_sha256, BlobRole.original)
    model.has_original = original is not None
    served = original or receipt_service.blob_for(
        session, receipt.blob_sha256, BlobRole.display
    )
    model.download_name = receipt_service.display_name(
        receipt,
        txn,
        household,
        media_type=served.media_type if served else receipt.media_type,
        payee_name=payee_name,
        index=index,
    )
    model.download_bytes = served.byte_size if served else receipt.byte_size
    model.also_on = len(
        receipt_service.elsewhere_in_household(
            session,
            household_id=receipt.household_id,
            sha256=receipt.content_sha256,
            exclude_id=receipt.id,
        )
    )
    names = _uploader_names(session, [receipt])
    model.uploaded_by_name = names.get(receipt.uploaded_by_id or "")
    return model


def _many(session, rows: list[Receipt], household: Household) -> list[ReceiptOut]:
    # The index is per transaction, so the second receipt on one row is "-2"
    # and the first receipt on the next row is not.
    seen: dict[str | None, int] = {}
    out = []
    for row in rows:
        index = seen.get(row.transaction_id, 0)
        seen[row.transaction_id] = index + 1
        out.append(_out(session, row, household=household, index=index))
    return out


def _household_of(session, receipt: Receipt) -> Household:
    house = session.get(Household, receipt.household_id)
    if house is None:  # pragma: no cover - the foreign key is RESTRICT
        raise NotFound("no such receipt")
    return house


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #


@router.post(
    "/households/{household_id}/receipts", response_model=ReceiptUpload, status_code=201
)
async def upload(
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    transaction_id: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
    replaces_id: Annotated[str | None, Form()] = None,
    original_sha256: Annotated[str | None, Form()] = None,
    exif: Annotated[str | None, Form()] = None,
    client_encoded: Annotated[bool, Form()] = False,
    device_lat: Annotated[float | None, Form()] = None,
    device_lon: Annotated[float | None, Form()] = None,
    device_accuracy_m: Annotated[float | None, Form()] = None,
) -> ReceiptUpload:
    """Attach a file to a transaction, or drop it in the inbox.

    Scoped to the household **in the path**, not through `load_for`.
    `create_transaction` carries a comment about exactly this: `load_for` let
    household B's account through a `/households/A/...` call, so the row landed
    in B while the batch was filed under A.

    The three `original_sha256`/`exif`/`client_encoded` fields are for a client
    that compressed before sending -- today the mobile capture page. A request
    carrying none of them is the ordinary case and is the path the desktop uses.

    The `device_*` three are the browser's own position, sent only when
    somebody switched the location toggle on at the capture page. They land in
    the same GPS columns EXIF does and never overwrite them -- see
    `receipt_service.DeviceFix`, which is where that decision is written down.
    """
    refuse_declared_size(file, receipt_service.MAX_RECEIPT_BYTES, TOO_BIG)
    raw = await read_capped(file, receipt_service.MAX_RECEIPT_BYTES, TOO_BIG)

    txn: Transaction | None = None
    if transaction_id:
        txn = txn_service.get_for_household(session, transaction_id, household.id)

    replacing: Receipt | None = None
    if replaces_id:
        # The path's household, not any household the caller is in: the batch
        # below is filed under `household`, so a receipt from elsewhere would
        # be moved under a batch its own members cannot see. Issue #77.
        replacing = session.execute(
            select(Receipt).where(Receipt.id == replaces_id, Receipt.household_id == household.id)
        ).scalar_one_or_none()
        if replacing is None:
            raise NotFound("no such receipt")

    # The encode is 300-1200 ms of CPU. It runs off the event loop and
    # *before* the session is touched, so it never holds SQLite's single
    # writer lock -- two members uploading at once with the lock held would
    # serialise the whole app. On the shared two-wide pool rather than
    # `asyncio.to_thread`, which bounded nothing: twenty uploads at once were
    # twenty full-size decodes in memory at once. Issue #89.
    prepared = await run_cpu(
        receipt_service.prepare,
        raw,
        original_sha256=_checked_hash(original_sha256),
        exif_block=_checked_exif(exif),
        client_encoded=client_encoded,
        device_fix=_checked_fix(device_lat, device_lon, device_accuracy_m),
        # Resolved here, before the thread: `prepare` has no session on
        # purpose, and reading a lazy attribute off `household` from a
        # threadpool worker would reach for one. Issue #62.
        keep_original_bytes=receipt_service.keep_original(household),
    )

    # The duplicate checks, the store -- a blob of up to 25 MB written under
    # SQLite's write lock -- and the answer are the database half, and run on
    # a threadpool worker rather than the loop. Issue #233.
    return await run_in_threadpool(
        _store,
        session,
        household=household,
        user=user,
        prepared=prepared,
        txn=txn,
        replacing=replacing,
        filename=file.filename,
        note=note,
    )


def _store(session, *, household, user, prepared, txn, replacing, filename, note) -> ReceiptUpload:
    clash = receipt_service.existing_attachment(
        session,
        household_id=household.id,
        transaction_id=txn.id if txn else None,
        sha256=prepared.sha256,
    )
    if clash is not None:
        where = (
            "already attached to this transaction"
            if txn is not None
            else "already waiting in the inbox"
        )
        raise Conflict(
            f"that exact file is {where}, since {clash.created_at.date().isoformat()}."
        )

    others = receipt_service.elsewhere_in_household(
        session, household_id=household.id, sha256=prepared.sha256
    )

    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        if replacing is not None:
            # To the inbox, not deleted. The old one stays visible and
            # re-attachable, and one undo puts both back.
            replacing.transaction_id = None
        receipt = receipt_service.store(
            session,
            household_id=household.id,
            prepared=prepared,
            transaction_id=txn.id if txn else None,
            uploaded_by_id=user.id,
            original_filename=filename,
            note=(note or None),
        )

    return ReceiptUpload(
        receipt=_out(session, receipt, household=household),
        warning=_duplicate_sentence(session, others),
    )


def _duplicate_sentence(session, others: list[Receipt]) -> str | None:
    if not others:
        return None
    first = others[0]
    txn = session.get(Transaction, first.transaction_id) if first.transaction_id else None
    where = "another transaction"
    if txn is not None:
        payee = session.get(Payee, txn.payee_id) if txn.payee_id else None
        where = f"{payee.name if payee else 'a transaction'}, {txn.date.isoformat()}"
    return (
        f"This exact file is already attached to {where}. Attaching it here as well "
        "is fine -- a bill can cover two rows -- but if you meant to move it, detach "
        "it there first."
    )


def _checked_hash(value: str | None) -> str | None:
    """64 hex characters, or nothing.

    A lie about this can create a duplicate or block one of your own uploads,
    and can do nothing else -- it never selects a row to return, so it cannot
    be used to read somebody else's receipt. That property is what makes
    accepting it safe, and it has to stay true: the moment this hash is used to
    *look up* a blob rather than to compare against one, this becomes an IDOR.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValidationError("original_sha256 has to be 64 hexadecimal characters")
    return text


def _checked_fix(
    lat: float | None, lon: float | None, accuracy_m: float | None
) -> receipt_service.DeviceFix | None:
    """A coordinate on Earth, or nothing at all.

    Both halves or neither: half a fix is a column pair meaning "somewhere on
    this line of latitude", which no screen can draw and nothing can undo.

    The range check is not decoration. These three floats go into columns the
    *More info* block turns into a Google Maps link, and an unchecked NaN or a
    latitude of 900 is a row the UI cannot render and a link that points
    nowhere. `math.isfinite` rather than the pydantic bounds alone, because a
    NaN compares false against every bound and would sail through them.
    """
    import math

    if lat is None and lon is None:
        return None
    if lat is None or lon is None:
        raise ValidationError("a location needs both a latitude and a longitude")
    if not (math.isfinite(lat) and -90 <= lat <= 90):
        raise ValidationError("that latitude is not a place on Earth")
    if not (math.isfinite(lon) and -180 <= lon <= 180):
        raise ValidationError("that longitude is not a place on Earth")
    if accuracy_m is not None and not (math.isfinite(accuracy_m) and accuracy_m >= 0):
        raise ValidationError("an accuracy is a number of metres, and not a negative one")
    return receipt_service.DeviceFix(lat=lat, lon=lon, accuracy_m=accuracy_m)


def _checked_exif(value: str | None) -> bytes | None:
    """Attacker-supplied binary going into a parser, so: capped, and in a try.

    Real blocks measure 29-32 KiB. A failure means *no metadata*, never a 500 --
    the picture still stores.
    """
    if not value:
        return None
    import base64

    if len(value) > receipt_service.MAX_EXIF_BYTES * 2:
        raise ValidationError("that metadata block is larger than any camera writes")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        return None
    return raw[: receipt_service.MAX_EXIF_BYTES] if raw else None


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


@router.get("/households/{household_id}/receipts", response_model=list[ReceiptOut])
def inbox(
    household: CurrentHousehold,
    session: SessionDep,
    unattached: bool | None = Query(default=None),
    transaction_id: str | None = None,
) -> list[ReceiptOut]:
    """The inbox, and the filters the Receipts screen offers.

    Ordered by `captured_at` falling back to `created_at`: a month of receipts
    uploaded in one batch all share an upload time and nothing else, and
    sorting by when they were *taken* puts them back in the order they happened
    -- which is the order the statement will arrive in.
    """
    stmt = select(Receipt).where(Receipt.household_id == household.id)
    if unattached is True:
        stmt = stmt.where(Receipt.transaction_id.is_(None))
    elif unattached is False:
        stmt = stmt.where(Receipt.transaction_id.is_not(None))
    if transaction_id:
        stmt = stmt.where(Receipt.transaction_id == transaction_id)

    rows = list(
        session.execute(
            stmt.order_by(
                Receipt.captured_at.desc().nullslast(),
                Receipt.created_at.desc(),
            )
        ).scalars()
    )
    return _many(session, rows, household)


@router.get("/transactions/{transaction_id}/receipts", response_model=list[ReceiptOut])
def for_transaction(
    transaction_id: str, session: SessionDep, user: CurrentUser
) -> list[ReceiptOut]:
    """What the side panel opens with."""
    txn = load_for(session, user, Transaction, transaction_id)
    household = session.get(Household, txn.household_id)
    rows = receipt_service.for_transaction(session, txn.id)
    return _many(session, rows, household)


@router.get("/receipts/{receipt_id}/{role}", include_in_schema=False)
def bytes_of(
    receipt_id: str, role: BlobRole, request: Request, session: SessionDep, user: CurrentUser
) -> Response:
    """The bytes. The one route where the headers earn their keep.

    Authentication is the ordinary session cookie -- `SameSite=Lax`, `Secure`,
    `HttpOnly` -- and an `<img>` to a same-origin URL sends it. So no signed
    URLs, no tokens in query strings and no new auth path, which also keeps the
    `Referrer-Policy: no-referrer` guarantee intact.
    """
    receipt = load_for(session, user, Receipt, receipt_id)
    blob = receipt_service.blob_for(session, receipt.blob_sha256, role)
    if blob is None:
        raise NotFound("that receipt has no copy of that kind")

    etag = f'"{blob.sha256}-{role.value}"'
    if request.headers.get("if-none-match") == etag:
        # One indexed lookup and no body at all. A register scrolled through
        # twice costs nothing the second time.
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": CACHE})

    household = _household_of(session, receipt)
    name = receipt_service.display_name(
        receipt,
        receipt.transaction,
        household,
        media_type=blob.media_type,
        payee_name=_payee_name(session, receipt.transaction),
    ).rsplit("/", 1)[-1]

    from urllib.parse import quote

    return Response(
        content=blob.data,
        media_type=blob.media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_ascii(name)}"; '
                f"filename*=UTF-8''{quote(name)}"
            ),
            "Cache-Control": CACHE,
            "ETag": etag,
            "Content-Security-Policy": BYTES_CSP,
        },
    )


def _payee_name(session, txn: Transaction | None) -> str | None:
    if txn is None or not txn.payee_id:
        return None
    payee = session.get(Payee, txn.payee_id)
    return payee.name if payee else None


def _ascii(name: str) -> str:
    """The fallback filename, for a client that does not read `filename*`."""
    return "".join(character if 32 < ord(character) < 127 and character != '"' else "-" for character in name)


# --------------------------------------------------------------------------- #
# Moving and removing
# --------------------------------------------------------------------------- #


@router.patch("/receipts/{receipt_id}", response_model=ReceiptOut)
def update(
    receipt_id: str, body: ReceiptUpdate, session: SessionDep, user: CurrentUser
) -> ReceiptOut:
    """Attach, detach, re-attach, or write a note. One batch either way."""
    receipt = load_for(session, user, Receipt, receipt_id)
    household = _household_of(session, receipt)

    target: Transaction | None = None
    if body.transaction_id and not body.detach:
        target = txn_service.get_for_household(session, body.transaction_id, receipt.household_id)
        clash = receipt_service.existing_attachment(
            session,
            household_id=receipt.household_id,
            transaction_id=target.id,
            sha256=receipt.content_sha256,
        )
        if clash is not None and clash.id != receipt.id:
            raise Conflict("that exact file is already attached to that transaction")

    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=receipt.household_id
    ):
        if body.detach:
            receipt.transaction_id = None
        elif target is not None:
            receipt.transaction_id = target.id
        if body.clear_note:
            receipt.note = None
        elif body.note is not None:
            receipt.note = body.note

    session.refresh(receipt)
    return _out(session, receipt, household=household)


@router.delete("/receipts/{receipt_id}", status_code=204)
def remove(receipt_id: str, session: SessionDep, user: CurrentUser) -> None:
    """Hard delete, like everything else. The before-image is the undo.

    The bytes are left alone. They are reference-counted, not cascaded -- two
    receipts can legitimately share one sha256 -- and the sweep takes them
    twenty-four hours after nothing points at them, which is what makes an undo
    inside that window find its picture still there.
    """
    receipt = load_for(session, user, Receipt, receipt_id)
    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=receipt.household_id
    ):
        session.delete(receipt)
        session.flush()


@router.post("/households/{household_id}/receipts/bulk-delete", response_model=list[str])
def remove_many(
    body: ReceiptBulkDelete,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> list[str]:
    """Many receipts, one act -- so one undo puts the whole selection back.

    Deliberately not the client calling `DELETE /receipts/{id}` in a loop. That
    is one batch each, and taking six back would be six undos in the right
    order; the History screen would show six lines for one gesture. Same
    reasoning, and the same shape, as `POST /transactions/bulk`: rows are loaded
    and deleted one at a time through the ORM rather than issued as one
    statement, because a bulk statement bypasses the audit hook and this would
    be the one place that silently stopped being undoable.

    Scoped by the household in the path and not by `load_for`, so a receipt id
    from another household is simply not found rather than 403'd -- they do not
    get to learn the id is real. A selection that has gone stale in part is
    acted on for the part that is still there and says what it removed; a
    selection where *nothing* matches is a 404 rather than a cheerful empty
    list, because the caller believed it was deleting something.
    """
    rows = list(
        session.execute(
            select(Receipt).where(
                Receipt.id.in_(body.receipt_ids),
                Receipt.household_id == household.id,
            )
        ).scalars()
    )
    if not rows:
        raise NotFound("no such receipt")

    # Read before the delete: an attribute on a deleted instance is expired
    # once the flush has gone through.
    removed = [row.id for row in rows]
    with batch(
        session, kind=BatchKind.bulk_update, actor_id=user.id, household_id=household.id
    ):
        for row in rows:
            session.delete(row)
        session.flush()
    return removed
