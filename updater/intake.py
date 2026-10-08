"""Taking a request out of the volume, once, and answering a refusal (5.4).

The updater takes `request.json` by renaming it, so a request is read once,
and only then reads it -- from the name it now holds, so the app cannot swap
the file between the check and the read. The sequence:

1. rename `request.json` to `journal/.intake-<random>.taken`;
2. read it under `volume.read_untrusted`'s rules (no symlink, regular file,
   owned by the app's uid, at most 4 KiB) and parse it;
3. rename it to `journal/<id>.taken`, under the request's own id when that id
   is a fresh UUID4, and otherwise under a fresh one the updater makes up;
4. validate; a refusal writes `history/<id>.json` with state `refused` and its
   one sentence, and nothing else.

**No engine call is made on any of these paths.** Validation answers from a
`Context` taken by detection beforehand and from the volume itself. Nothing
here is even handed the engine client.

A request whose id is malformed or already seen cannot have its record filed
under that id -- a duplicate would overwrite the original's history and its
`.taken` file. Its record goes under a fresh id, with the claimed one in
`request_id`.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from updater import contract, journal, volume
from updater.contract import Context, History, Refusal, Request, Status
from updater.volume import REQUEST_OWNER_UID, UnsafeFile, Volume

INTAKE_PREFIX = ".intake-"


@dataclass(frozen=True)
class Outcome:
    """What became of one request. `record_id` is where its files live."""

    record_id: str
    request: Request | None
    refusal: Refusal | None

    @property
    def accepted(self) -> bool:
        return self.refusal is None


def _strict_json(data: bytes) -> object:
    def no_constants(name: str) -> object:
        raise ValueError(name)

    return json.loads(data.decode("utf-8"), parse_constant=no_constants)


def take(
    vol: Volume,
    ctx: Context,
    now: float,
    owner_uid: int = REQUEST_OWNER_UID,
    me: journal.Owner | None = None,
) -> Outcome | None:
    """Take and answer the pending request, if there is one. Never touches the engine."""
    holding = vol.root / "journal" / f"{INTAKE_PREFIX}{secrets.token_hex(8)}.taken"
    try:
        os.rename(vol.request, holding)  # renames a symlink itself, never its target
    except FileNotFoundError:
        return None
    return _answer(vol, holding, ctx, now, owner_uid, me)


def resume_intake(
    vol: Volume, ctx: Context, now: float, owner_uid: int = REQUEST_OWNER_UID, me: journal.Owner | None = None
) -> list[Outcome]:
    """Answer requests a crash left half-taken. Called on start."""
    outcomes = []
    for holding in sorted((vol.root / "journal").glob(f"{INTAKE_PREFIX}*.taken")):
        outcomes.append(_answer(vol, holding, ctx, now, owner_uid, me))
    return outcomes


def _answer(
    vol: Volume, holding: Path, ctx: Context, now: float, owner_uid: int, me: journal.Owner | None
) -> Outcome:
    raw: object = None
    refusal: Refusal | None = None
    try:
        raw = _strict_json(volume.read_untrusted(holding, owner_uid))
    except UnsafeFile as e:
        refusal = Refusal("unsafe_file", str(e))
    except (ValueError, UnicodeDecodeError):
        refusal = Refusal("not_json", "The request is not valid JSON.")

    claimed = raw.get("id") if isinstance(raw, dict) else None
    if contract.is_uuid4(claimed) and not vol.seen(claimed):  # type: ignore[arg-type]
        record_id: str = claimed  # type: ignore[assignment]
    else:
        record_id = str(uuid.uuid4())
        if refusal is None and contract.is_uuid4(claimed):
            refusal = Refusal("duplicate", "A request with this id was already seen.")

    volume.rename(holding, vol.taken(record_id))

    request: Request | None = None
    if refusal is None:
        try:
            report = None
            if isinstance(raw, dict) and contract.is_uuid4(raw.get("prepared_id")):
                report = volume.read_own_json(vol.prepared(raw["prepared_id"]))
            request = contract.validate(raw, ctx, now, report)
        except Refusal as e:
            refusal = e

    if refusal is not None:
        kind = raw.get("kind") if isinstance(raw, dict) and isinstance(raw.get("kind"), str) else None
        by = raw.get("requested_by") if isinstance(raw, dict) and isinstance(raw.get("requested_by"), str) else None
        record = History(
            id=record_id,
            kind=kind,
            state="refused",
            sentence=refusal.sentence,
            code=refusal.code,
            finished_at=contract.iso(now),
            requested_by=by,
            request_id=claimed if isinstance(claimed, str) and claimed != record_id else None,
        )
        volume.write_json(vol.history(record_id), record.to_dict())
        return Outcome(record_id, None, refusal)

    assert request is not None
    if request.kind == "ping":
        pong = History(
            id=request.id,
            kind="ping",
            state="succeeded",
            sentence="The updater answered.",
            finished_at=contract.iso(now),
        )
        volume.write_json(vol.history(request.id), pong.to_dict())
        return Outcome(record_id, request, None)

    status = Status(id=request.id, kind=request.kind, state="accepted", updated_at=contract.iso(now))
    volume.write_json(vol.status, status.to_dict())
    if request.kind == "apply":
        # Step 0's journal entry. It stores the recovery code's hash -- the code
        # itself never reaches the volume (Part 11).
        journal.begin(vol, request, owner=me, now=now)
    return Outcome(record_id, request, None)
