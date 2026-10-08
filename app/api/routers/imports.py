"""Importing a statement, and the batch history.

Upload previews; nothing reaches the register until the preview is committed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from statements import parsing

from ...audit.batch import batch, resume
from ...audit.registry import audited_models
from ...audit.undo import undo_batch
from ...errors import Conflict, TooLarge
from ...models import (
    Account,
    Batch,
    BatchKind,
    BatchStatus,
    Categorisation,
    Change,
    ImportLine,
    Payee,
    User,
)
from ...schemas import (
    BatchDetail,
    BatchOut,
    ChangeDetailOut,
    ChangeOut,
    FieldChangeOut,
    ImportCommit,
    ImportLineOut,
    ImportPreview,
    ImportResult,
    PayeeRuleFromLine,
    SetLineCategory,
    SetLineMemo,
    StagedImportOut,
)
from ...services import accounts as account_service
from ...services import categories as category_service
from ...services import describing, importing
from ...services import payees as payee_service
from ..deps import CurrentHousehold, CurrentUser, SessionDep
from ..offload import run_cpu
from ..uploads import read_capped, refuse_declared_size

router = APIRouter(tags=["import"])

#: Bank exports are tens of kilobytes. This is a guard against a mistake, not
#: against an adversary -- the tailnet already handles those.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def _preview(session, batch_row: Batch, lines: list[ImportLine], warnings: list[str]) -> ImportPreview:
    source = batch_row.source or {}
    landing = importing.preview_categories(session, batch_row.household_id or "", lines)
    return ImportPreview(
        batch_id=batch_row.id,
        filename=source.get("filename"),
        account_id=source.get("account_id", ""),
        sha256=source.get("sha256", ""),
        detected=source.get("format", {}),
        # What reading the file could not settle (`read_warnings`), and what
        # staging said about it as a whole -- a product it skipped, a running
        # balance that does not add up (`warnings`) -- are both kept on the
        # batch, so they are said again when the preview is reopened from the
        # queue. `warnings` here is only what this one request has to add.
        warnings=[*warnings, *source.get("read_warnings", []), *source.get("warnings", [])],
        counts=importing.summarise(lines),
        lines=[_line_out(line, landing) for line in lines],
    )


def _line_out(line: ImportLine, landing: dict) -> ImportLineOut:
    out = ImportLineOut.model_validate(line)
    where = landing.get(line.id)
    if where is not None:
        out.category_id = where.category_id
        out.category_name = where.name
        out.category_chosen = where.chosen
        out.category_uncategorised = where.uncategorised
    return out


#: The sentence a refusal carries, in one place so the two checks below cannot
#: drift into saying different things about the same limit.
TOO_BIG = "that file is larger than this is meant for"


@router.post("/households/{household_id}/imports", response_model=ImportPreview, status_code=201)
async def upload(
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
    account_id: Annotated[str, Form()],
    force: Annotated[bool, Form()] = False,
    file: Annotated[UploadFile | None, File()] = None,
    pasted: Annotated[str | None, Form()] = None,
) -> ImportPreview:
    """Stage an import. Nothing is written to the register here.

    Takes a file or pasted text through the identical path, so pasting the same
    statement twice is caught by the same check as uploading it twice.
    """
    account = account_service.get_for_household(session, account_id, household.id)

    # Declared size first; the shared helper says why.
    refuse_declared_size(file, MAX_UPLOAD_BYTES, TOO_BIG)

    if file is not None:
        raw = await read_capped(file, MAX_UPLOAD_BYTES, TOO_BIG)
        filename = file.filename
    elif pasted:
        raw = pasted.encode()
        filename = "pasted"
    else:
        raise Conflict("send a file or some pasted text")

    if len(raw) > MAX_UPLOAD_BYTES:
        raise TooLarge(TOO_BIG)

    digest = importing.file_digest(raw)
    if not force:
        already = importing.previous_import_of(session, account_id=account.id, digest=digest)
        if already is not None:
            when = already.started_at.strftime("%d %B %Y at %H:%M")
            name = (already.source or {}).get("filename") or "a file"
            # A staged import and a committed one are both "already seen", but
            # they ask different things of the person reading this: one is
            # waiting for them, the other is done.
            if already.status is BatchStatus.preview:
                raise Conflict(
                    f"this exact file is already staged for {account.name}, from {when} "
                    f"(as {name}), and is waiting to be reviewed. Open that import rather "
                    "than starting a second one, or send this with force set."
                )
            raise Conflict(
                f"this exact file was already imported into {account.name} on {when} "
                f"(as {name}). Nothing has been changed. If you meant to import it again, "
                "send it with force set."
            )

    # Sniffing and parsing are pure CPU over the bytes -- a large spreadsheet
    # is seconds of it -- and this route is `async`, so on the event loop they
    # stopped every other request in the process for as long as one file took.
    # They run on the shared pool instead; the staging below needs the session
    # and stays here. Issue #84.
    # The account's country breaks the tie when no amount in the file says
    # which decimal separator it uses ("1.500" reads either way, #259).
    sniffed, rows = await run_cpu(
        parsing.read, raw, decimal_preference=importing.decimal_preference_for(account)
    )
    # Staging is the database half -- twin matching, rules, the transfer
    # matcher -- and was two seconds for a 3,000-line statement, all of it on
    # the loop. It runs on a threadpool worker instead; the session is used
    # from one thread at a time, the loop handing it over and waiting, as
    # `one_time_import._run` already does. Issue #233.
    source = {
        "filename": filename,
        "sha256": digest,
        "bytes": len(raw),
        "account_id": account.id,
        "format": sniffed.format.describe(),
        # What reading the file could not settle -- a separator assumed, dates
        # that read either way. Kept on the batch rather than handed to this
        # one response, because the person most in need of "the separator was
        # assumed" is the one reopening the import from the queue tomorrow.
        # Its own key: staging writes `warnings` with what *it* noticed.
        "read_warnings": list(sniffed.warnings),
    }
    return await run_in_threadpool(
        _stage_and_preview, session, account, rows, user, household, source
    )


def _stage_and_preview(session, account, rows, user, household, source) -> ImportPreview:
    with batch(
        session,
        kind=BatchKind.imported,
        actor_id=user.id,
        household_id=household.id,
        source=source,
    ) as staged:
        lines = importing.stage_parsed(
            session,
            account=account,
            rows=rows,
            batch_row=staged,
        )
        staged.status = BatchStatus.preview

    return _preview(session, staged, lines, [])


@router.patch(
    "/households/{household_id}/imports/{batch_id}/lines/{line_id}",
    response_model=ImportLineOut,
)
def set_line_category(
    batch_id: str,
    line_id: str,
    body: SetLineCategory,
    household: CurrentHousehold,
    session: SessionDep,
) -> ImportLineOut:
    """Choose where one staged line lands, before anything is written.

    Stored on the line rather than held in the browser, so it survives a reload
    and is still there if you come back to the preview tomorrow. No batch: a
    staged import has not touched the register, so there is nothing to audit
    yet -- the audit records what the commit does, and the commit will record
    the category this produced.

    Three answers: a category, `clear_category` to hand the line back to the
    payee's rule, or `uncategorised` for no category at all -- the one a rule
    or the bank's wording cannot then fill in at commit (issue #9).
    """
    from ...errors import NotFound

    staged = importing.get_preview(session, batch_id, household.id)
    line = session.execute(
        select(ImportLine).where(ImportLine.id == line_id, ImportLine.batch_id == staged.id)
    ).scalar_one_or_none()
    if line is None:
        raise NotFound("no such line on this import")

    category = None
    if not body.clear_category and body.category_id:
        category = category_service.get_for_household(
            session, body.category_id, household.id
        )
    importing.set_line_category(session, line, category, uncategorised=body.uncategorised)
    session.flush()

    landing = importing.preview_categories(session, household.id, [line])
    out = _line_out(line, landing)
    # Only worth counting when there is a decision to spread: a line handed back
    # to the payee's rule has nothing of its own to offer the others.
    # "Uncategorised" is a decision, and it spreads like one.
    if line.category_id or body.uncategorised:
        out.similar_lines = len(importing.similar_lines(session, staged.id, line))
    return out


@router.patch(
    "/households/{household_id}/imports/{batch_id}/lines/{line_id}/memo",
    response_model=ImportLineOut,
)
def set_line_memo(
    batch_id: str,
    line_id: str,
    body: SetLineMemo,
    household: CurrentHousehold,
    session: SessionDep,
) -> ImportLineOut:
    """Type the memo one staged line will carry, before anything is written.

    The same shape as choosing its category, and for the same reasons: stored on
    the line so it survives a reload and is still there when the import is
    reopened from the queue, and no batch, because a staged import has not
    touched the register yet.

    What the bank sent is kept beside it rather than replaced -- the raw line
    and `parsed["memo"]` are both exactly as they arrived.
    """
    from ...errors import NotFound

    staged = importing.get_preview(session, batch_id, household.id)
    line = session.execute(
        select(ImportLine).where(ImportLine.id == line_id, ImportLine.batch_id == staged.id)
    ).scalar_one_or_none()
    if line is None:
        raise NotFound("no such line on this import")

    importing.set_line_memo(session, line, body.memo, clear=body.clear_memo)
    session.flush()

    landing = importing.preview_categories(session, household.id, [line])
    return _line_out(line, landing)


@router.post(
    "/households/{household_id}/imports/{batch_id}/lines/{line_id}/apply-to-payee",
    response_model=ImportPreview,
)
def apply_category_to_payee(
    batch_id: str,
    line_id: str,
    household: CurrentHousehold,
    session: SessionDep,
) -> ImportPreview:
    """Give this line's category to every other line of the same payee here.

    Scoped to this import, deliberately. Teaching the *payee* -- so every future
    statement follows -- is a different and larger decision, and it has its own
    screen. This is "the eleven other Mercadona lines in the file I am looking
    at", which is the one somebody wants while looking at them.
    """
    from ...errors import NotFound

    staged = importing.get_preview(session, batch_id, household.id)
    line = session.execute(
        select(ImportLine).where(ImportLine.id == line_id, ImportLine.batch_id == staged.id)
    ).scalar_one_or_none()
    if line is None:
        raise NotFound("no such line on this import")

    importing.apply_to_similar(session, staged.id, line)
    lines = list(
        session.execute(
            select(ImportLine)
            .where(ImportLine.batch_id == staged.id)
            .order_by(ImportLine.line_no)
        ).scalars()
    )
    return _preview(session, staged, lines, [])


@router.post(
    "/households/{household_id}/imports/{batch_id}/lines/{line_id}/payee-rule",
    response_model=PayeeRuleFromLine,
)
def rule_from_line(
    batch_id: str,
    line_id: str,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> PayeeRuleFromLine:
    """Make this line's category the payee's rule, for every future statement.

    The offer above it is about this file. This is the larger version of the
    same thought -- "stop asking me" -- and it belongs on a button rather than
    in a sentence pointing at another screen.

    The payee is created if it does not exist yet. That is a row in the ledger
    that was not there a moment ago, so the answer says so, but it is what the
    commit would have done anyway and it is plainly what "always categorise
    this payee" asks for.
    """
    from ...errors import NotFound, ValidationError

    staged = importing.get_preview(session, batch_id, household.id)
    line = session.execute(
        select(ImportLine).where(ImportLine.id == line_id, ImportLine.batch_id == staged.id)
    ).scalar_one_or_none()
    if line is None:
        raise NotFound("no such line on this import")
    if not line.category_id:
        raise ValidationError("give the line a category first")

    parsed = line.parsed or {}
    name = (parsed.get("payee") or "").strip()
    payee = None
    if parsed.get("payee_id"):
        payee = session.get(Payee, parsed["payee_id"])
    if payee is None and not name:
        raise ValidationError("this line has no payee to attach a rule to")

    created = False
    with batch(session, kind=BatchKind.admin, actor_id=user.id, household_id=household.id):
        if payee is None:
            # Looked up first only to know whether to say "created" -- there is
            # no `find` on the service and adding one for a toast's wording
            # would be the tail wagging the dog.
            # Through `by_folded`, the lookup `get_or_create` itself makes, so
            # a payee whose stored key predates #268 is not called "created".
            existing = payee_service.by_folded(session, household.id, [name]).get(
                payee_service.fold(name)
            )
            payee = payee_service.get_or_create(session, household.id, name)
            created = existing is None
        category_service.set_rule(
            session, payee, mode=Categorisation.fixed, category_id=line.category_id
        )
        category = category_service.get_for_household(session, line.category_id, household.id)

    return PayeeRuleFromLine(
        payee_id=payee.id,
        payee_name=payee.name,
        category_name=category.full_name,
        payee_created=created,
    )


@router.get("/households/{household_id}/imports", response_model=list[StagedImportOut])
def list_staged(
    household: CurrentHousehold,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[StagedImportOut]:
    """Imports staged and waiting, so one can be opened rather than restarted.

    The upload refusal says a file "is waiting to be reviewed"; before this
    there was no way to reach the thing it was talking about short of finding
    the batch in History. Enough of each to recognise it: who staged it, when,
    what the file was called, and how many rows are in it.
    """
    staged = importing.open_previews(session, household.id, limit=limit)

    accounts = {
        row.id: row.name
        for row in session.execute(
            select(Account).where(Account.household_id == household.id)
        ).scalars()
    }
    # One query for the actors rather than one per batch, like `row_history`.
    actor_ids = {row.actor_id for row, _ in staged}
    actors = {
        row.id: row.display_name
        for row in session.execute(select(User).where(User.id.in_(actor_ids or [""]))).scalars()
    }

    out = []
    for batch_row, rows in staged:
        source = batch_row.source or {}
        account_id = source.get("account_id", "")
        out.append(
            StagedImportOut(
                batch_id=batch_row.id,
                filename=source.get("filename"),
                account_id=account_id,
                account_name=accounts.get(account_id),
                actor_name=actors.get(batch_row.actor_id),
                staged_at=batch_row.started_at,
                row_count=rows,
                sha256=source.get("sha256", ""),
            )
        )
    return out


@router.delete("/households/{household_id}/imports/{batch_id}", status_code=204)
def discard_import(
    batch_id: str, household: CurrentHousehold, session: SessionDep
) -> None:
    """Throw a staged import away without committing any of it.

    The other half of being able to open one. A preview nobody wants is
    otherwise a permanent refusal to import that file again, because the
    duplicate check counts a staged import as already-seen.

    Only a staged one: a committed import is put back from History, which
    reverses what it wrote rather than forgetting that it happened.
    `discard_preview` refuses anything else, and says where to go instead.
    """
    staged = importing.get_preview(session, batch_id, household.id, status=None)
    importing.discard_preview(session, staged)


@router.get("/households/{household_id}/imports/{batch_id}", response_model=ImportPreview)
def read_preview(
    batch_id: str, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> ImportPreview:
    """A staged import, re-decided against the register as it is right now.

    Opening a queued import is not reading a stored answer: `reassess` re-runs
    the duplicate check and the twin match, because everything they depend on
    moves while a preview waits in the queue. See its docstring for what that
    catches in each direction.

    A `GET` that writes, which is worth being explicit about. `import_lines` is
    not audited, so nothing here owes a batch, and what is being corrected is
    the preview's own working rather than the ledger -- the register is not
    touched until the commit. The alternative, deciding in memory on each read,
    would leave the screen and the stored verdict disagreeing, and `commit`
    reads the stored one.
    """
    staged = importing.get_preview(session, batch_id, household.id)
    account = account_service.get_for_household(
        session, (staged.source or {}).get("account_id", ""), household.id
    )
    changed = importing.reassess(session, batch_row=staged, account=account)
    lines = list(
        session.execute(
            select(ImportLine)
            .where(ImportLine.batch_id == staged.id)
            .order_by(ImportLine.line_no)
        ).scalars()
    )
    # Said out loud rather than quietly corrected. A preview that looks
    # different from the one you left is a preview you should be told about --
    # and "four of these have arrived in the account since" is the sentence
    # that explains a count that went down.
    warnings = (
        [
            f"{len(changed)} of these lines changed when they were re-checked against the "
            "account just now: the register has moved since this import was staged."
        ]
        if changed
        else []
    )
    return _preview(session, staged, lines, warnings)


@router.post(
    "/households/{household_id}/imports/{batch_id}/commit", response_model=ImportResult
)
def commit_import(
    batch_id: str,
    body: ImportCommit,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> ImportResult:
    """Apply what the preview showed, as one act."""
    staged = importing.get_preview(session, batch_id, household.id)
    account = account_service.get_for_household(
        session, (staged.source or {}).get("account_id", ""), household.id
    )

    # Re-opened, not replaced: staging and applying are two visits to one
    # operation, and History must show one entry to undo.
    with resume(session, staged, status=BatchStatus.applied, committed_by=user.id):
        result = importing.commit(
            session,
            batch_row=staged,
            account=account,
            skip_line_ids=set(body.skip_line_ids),
            reject_matches=set(body.reject_match_line_ids),
        )

    return ImportResult(batch_id=staged.id, **result)


# --------------------------------------------------------------------------- #
# Batches, and the history of one row
# --------------------------------------------------------------------------- #


@router.get("/households/{household_id}/changes", response_model=list[ChangeOut])
def row_history(
    household: CurrentHousehold,
    session: SessionDep,
    table: str = Query(min_length=1, max_length=64),
    row_id: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ChangeOut]:
    """Everything that has ever happened to one row, newest first.

    This is what `Audit Log Decision.md` sells as the headline capability --
    *"why is this EUR 45 here?"* -- and what `Change.household_id` was
    denormalised for. Scoped by household, so it uses that column and a member
    of one household cannot read another's history.
    """
    from ...models import Change

    if table not in audited_models():
        from ...errors import NotFound

        raise NotFound("no such table in the audit log")

    rows = session.execute(
        select(Change)
        .where(
            Change.household_id == household.id,
            Change.table_name == table,
            Change.row_id == row_id,
        )
        .order_by(Change.seq.desc())
        .limit(limit)
    ).scalars()

    changes = list(rows)

    # A receipt is its own audited row, so attaching one writes a change
    # against the *receipt's* id -- and this query is keyed on the
    # transaction's. The effect was that "what has happened to this" showed
    # every edit to a row except the evidence somebody had just pinned to it.
    #
    # The link lives inside the image rather than in a column of `changes`, so
    # it is read out of the JSON. Both sides: `after` carries the transaction
    # while the receipt is attached, `before` carries it when one is detached,
    # and a history that shows the attaching but not the removing is worse than
    # one that shows neither.
    if table == "transactions":
        from sqlalchemy import or_

        attached = session.execute(
            select(Change)
            .where(
                Change.household_id == household.id,
                Change.table_name == "receipts",
                or_(
                    Change.after["transaction_id"].as_string() == row_id,
                    Change.before["transaction_id"].as_string() == row_id,
                ),
            )
            .order_by(Change.seq.desc())
            .limit(limit)
        ).scalars()
        # One list, newest first, still capped at what the caller asked for.
        changes = sorted(
            [*changes, *attached], key=lambda one: one.seq, reverse=True
        )[:limit]
    # One `_Names` for the whole list rather than one per change: a transaction
    # edited fifty times would otherwise be fifty passes over the household's
    # payees and categories to write fifty sentences.
    sentences = describing.describe_changes(session, household.id, changes)

    # Both maps in one query each. `session.get` would have been one round trip
    # per change for the batch and another for its actor, which is the N+1 this
    # codebase keeps finding in the previous build -- and a row edited fifty
    # times is exactly the row somebody opens this panel on.
    batch_ids = {change.batch_id for change in changes}
    owners = {
        row.id: row
        for row in session.execute(select(Batch).where(Batch.id.in_(batch_ids or [""]))).scalars()
    }
    actor_ids = {owner.actor_id for owner in owners.values() if owner.actor_id}
    actors = {
        row.id: row.display_name
        for row in session.execute(select(User).where(User.id.in_(actor_ids or [""]))).scalars()
    }

    out = []
    for change in changes:
        model = ChangeOut.model_validate(change)
        owner = owners.get(change.batch_id)
        model.batch = BatchOut.model_validate(owner) if owner else None
        model.summary = sentences.get(change.seq, "")
        model.actor_name = actors.get(owner.actor_id) if owner else None
        # Beside the person, never instead of them. Same source as the History
        # list's, so the two cannot describe one batch differently.
        model.via = describing.via_words(owner) if owner else None
        out.append(model)
    return out


@router.get("/households/{household_id}/batches", response_model=list[BatchOut])
def list_batches(
    household: CurrentHousehold,
    session: SessionDep,
    include_single_edits: bool = False,
    #: "agent" or "human". Not a boolean, because a third answer is imaginable
    #: and a boolean named `agent_only` would have to be replaced rather than
    #: extended.
    actor: str | None = None,
    agent_key_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[BatchOut]:
    """What has been done to this household.

    Single-row manual edits are hidden by default: every keystroke in the
    register is a batch, and showing them all would bury the imports and bulk
    edits this screen exists for.
    """
    stmt = select(Batch).where(Batch.household_id == household.id)
    # A key that applied somebody else's staged import acted too, though the
    # batch's own `agent_key_id` names whoever staged it (issue #213). Read as
    # a string: SQLite's JSON extract is NULL for a missing path and for null.
    committed_key = Batch.source["committed"]["agent"]["key_id"].as_string()
    if actor == "agent":
        stmt = stmt.where(Batch.agent_key_id.is_not(None) | committed_key.is_not(None))
    elif actor == "human":
        stmt = stmt.where(Batch.agent_key_id.is_(None) & committed_key.is_(None))
    if agent_key_id:
        stmt = stmt.where((Batch.agent_key_id == agent_key_id) | (committed_key == agent_key_id))
    if not include_single_edits:
        # "More than one change" asked of each manual batch on its own: does it
        # have a second change row. Counting every batch's changes first grouped
        # the whole instance's log on every page load, both households and
        # every undo included (#238).
        second_change = (
            select(Change.seq)
            .where(Change.batch_id == Batch.id)
            .limit(1)
            .offset(1)
            .correlate(Batch)
            .exists()
        )
        # Agent batches are never hidden, however small. The reasoning behind
        # hiding single-change manual ones -- every keystroke in the register
        # is a batch -- does not extend to a program: a hundred small agent
        # edits are exactly what somebody opens this screen to find.
        stmt = stmt.where(
            (Batch.kind != BatchKind.manual)
            | (Batch.agent_key_id.is_not(None))
            | second_change
        )

    rows = list(session.execute(stmt.order_by(Batch.started_at.desc()).limit(limit)).scalars())
    counts = dict(
        session.execute(
            select(Change.batch_id, func.count())
            .where(Change.batch_id.in_([row.id for row in rows] or [""]))
            .group_by(Change.batch_id)
        ).all()
    )
    # One set of name lookups for the page, and the counts above in place of
    # reading every change row: an import's sentence needs only how many.
    names = describing.names_for(session, household.id)
    return [
        _described(session, row, counts.get(row.id, 0), names=names, counted=True)
        for row in rows
    ]


def _described(
    session, batch: Batch, change_count: int, *, names=None, counted: bool = False
) -> BatchOut:
    """`counted` says `change_count` is the batch's real count, safe to describe
    from. The undo route passes 0 without having counted anything."""
    words = describing.describe(
        session, batch, names=names, change_count=change_count if counted else None
    )
    out = BatchOut.model_validate(batch)
    out.headline = words.headline
    out.headline_key = words.headline_key
    out.detail = words.detail
    out.actor_name = words.actor
    out.via = words.via
    out.change_count = change_count
    return out


@router.get(
    "/households/{household_id}/batches/{batch_id}", response_model=BatchDetail
)
def batch_detail(
    batch_id: str,
    household: CurrentHousehold,
    session: SessionDep,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> BatchDetail:
    """One batch, row by row -- a page of rows at a time.

    What the confirmation before an undo reads from: agreeing to put something
    back means knowing what is going back, and a timestamp is not that.

    `change_count` is always the whole batch, which is what the confirmation
    says goes back; `lines` and `changed_rows` are the page from `offset`, in
    the order the rows were changed. Every row at once was 22.6 MiB and most
    of a second for a 2,000-row import, and a multi-megabyte body for a
    10k-row one, to confirm a click (#234).
    """
    from ...errors import NotFound

    target = session.get(Batch, batch_id)
    if target is None or target.household_id != household.id:
        raise NotFound("no such batch")

    count = session.execute(
        select(func.count()).select_from(Change).where(Change.batch_id == target.id)
    ).scalar_one()
    names = describing.names_for(session, household.id)
    words = describing.describe(session, target, names=names, change_count=count)
    changes = list(
        session.execute(
            select(Change)
            .where(Change.batch_id == target.id)
            .order_by(Change.seq)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    out = BatchDetail.model_validate(target)
    out.headline = words.headline
    out.headline_key = words.headline_key
    out.detail = words.detail
    out.actor_name = words.actor
    out.via = words.via
    out.change_count = count
    out.lines = describing.lines_of(changes, names)
    out.changed_rows = [
        ChangeDetailOut(
            seq=one.seq,
            table=one.table,
            row_id=one.row_id,
            op=one.op,
            summary=one.summary,
            # `asdict`, not `vars`: these are slots dataclasses and have no
            # `__dict__`. Third time in this codebase.
            fields=[FieldChangeOut(**asdict(f)) for f in one.fields],
            snapshot=[FieldChangeOut(**asdict(f)) for f in one.snapshot],
            redacted=one.redacted,
            table_key=one.table_key,
        )
        for one in describing.detail_of(session, household.id, changes, names=names)
    ]
    return out


@router.post("/households/{household_id}/batches/{batch_id}/undo", response_model=BatchOut)
def undo(
    batch_id: str, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> BatchOut:
    """Put a whole operation back."""
    target = session.get(Batch, batch_id)
    if target is None or target.household_id != household.id:
        from ...errors import NotFound

        raise NotFound("no such batch")
    undone = undo_batch(session, batch_id, actor_id=user.id)
    return _described(session, undone, 0)
