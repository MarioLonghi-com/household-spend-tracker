"""Going back from the browser (#173), from the app's side: the offer, what it
would lose, the request, and the updater's intake taking that request.

The offer is the last settled update, when it is a successful apply to this
version whose backup is one of the newest five and whose previous
configuration was saved. The loss is the audit log's batches since that
backup was taken, per household -- two households, as everywhere -- and the
passkeys registered since.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from app import __version__, config, db
from app.audit.batch import batch
from app.models import BatchKind
from app.models.audit import Batch
from app.models.auth import Passkey
from app.models.domain import Household
from app.services import updates
from tests.conftest import HEADERS, _setup_owner
from tests.test_update_endpoints import (
    BASE,
    _ctx,
    _files,
    _folder,
    _grant,
    _heartbeat,
    _history,
    _request,
)
from updater import contract, intake, journal, volume
from updater.volume import Volume

_MAJOR, _MINOR, _ = (int(part) for part in __version__.split("."))
PREVIOUS = f"{_MAJOR}.{_MINOR - 1}.0" if _MINOR else f"{_MAJOR - 1}.9.0"
STAMP = "20261101-101010"


@pytest.fixture()
def world(client):
    """An owner, and the volume, as `tests/test_update_endpoints.py` has them."""
    updates.forget_recovery_codes()
    owner = _setup_owner(client)
    vol = Volume(config.settings.update_dir)
    vol.init()
    yield {"client": client, "owner": owner, "vol": vol}
    updates.forget_recovery_codes()


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def updated(vol, *, taken_at: datetime, previous_json: bool = True, older: int = 0) -> str:
    """A successful apply PREVIOUS -> this version, its backup folder, its saved configuration."""
    backups = config.settings.data_dir / "backups"
    folder = _folder(backups, STAMP, version=PREVIOUS)
    (folder / "manifest.json").write_text(
        json.dumps(
            {"taken_at": taken_at.astimezone(UTC).isoformat(), "app_version": PREVIOUS, "alembic_revision": "2de003489b79"}
        )
    )
    for i in range(older):
        stamp = f"2026102{i}-101010"
        _folder(backups, stamp)
        _history(vol, state="succeeded", finished=time.time() - 10_000 + i, backup=f"/var/lib/spend-tracker/backups/{stamp}")
    rid = _history(
        vol,
        state="succeeded",
        finished=time.time() - 60,
        backup=f"/var/lib/spend-tracker/backups/{STAMP}",
        from_version=PREVIOUS,
        to_version=__version__,
    )
    if previous_json:
        volume.write_json(vol.root / "history" / f"{rid}.previous.json", {"Id": "a" * 64, "Config": {}, "HostConfig": {}})
    # The apply's journal, as the updater left it: what it replaced (the app never reads it).
    journal.save(
        vol,
        journal.Journal(
            id=rid, step="10", owner=None, created_at=contract.iso(time.time() - 120), kind="apply",
            context={
                "app_name": "spend-tracker-app-1",
                "old_ref": f"ghcr.io/mariolonghi-com/household-spend-tracker@sha256:{'1' * 64}",
                "old_version": PREVIOUS,
                "backup": f"/var/lib/spend-tracker/backups/{STAMP}",
            },
        ),
    )  # fmt: skip
    return rid


def two_households_written_since(owner_id: str, since: datetime) -> tuple[str, str]:
    """Two households, with batches before and after `since`, and one passkey after."""
    with db.SessionLocal() as session:
        with batch(session, kind=BatchKind.admin, actor_id=owner_id):
            one, two = Household(name="Second home"), Household(name="Allotment")
            session.add_all([one, two])
            session.flush()
            ids = (one.id, two.id)
            session.add(
                Passkey(
                    user_id=owner_id,
                    credential_id="cred-after",
                    public_key=b"k",
                    label="Laptop",
                    rp_id="localhost",
                    created_at=_naive(since + timedelta(minutes=5)),
                )
            )
        session.commit()
    with db.SessionLocal() as session:
        for hid, minutes in ((ids[0], -5), (ids[0], 2), (ids[0], 3), (ids[1], 4), (None, 6)):
            session.add(
                Batch(kind=BatchKind.admin, actor_id=owner_id, household_id=hid, started_at=_naive(since + timedelta(minutes=minutes)))
            )
        session.commit()
    return ids


def test_the_offer_and_what_going_back_would_lose_per_household(world):
    client, vol, owner = world["client"], world["vol"], world["owner"]
    _heartbeat(vol, protocols="1-2")
    since = datetime.now(UTC) + timedelta(hours=1)  # after everything setup wrote
    rid = updated(vol, taken_at=since)
    one, two = two_households_written_since(owner["user"]["id"], since)

    state = client.get(BASE, headers=HEADERS).json()
    assert state["go_back"] == {
        "update_id": rid,
        "to_version": PREVIOUS,
        "from_version": __version__,
        "updated_at": state["go_back"]["updated_at"],
        "backup": STAMP,
        "updater_can": True,
    }
    seen = client.get(f"{BASE}/go-back", headers=HEADERS)
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body["since"] == since.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Two batches in the second home after the backup (one before it is not counted),
    # one in the allotment, and one instance-wide, listed last.
    assert body["households"] == [
        {"household_id": two, "name": "Allotment", "batches": 1},
        {"household_id": one, "name": "Second home", "batches": 2},
        {"household_id": None, "name": None, "batches": 1},
    ]
    assert body["passkeys"] == 1


@pytest.mark.parametrize(
    "why", ["no saved configuration", "a later rollback", "backup not in the newest five", "another version runs"]
)
def test_no_offer_when_the_volume_says_going_back_cannot_work(world, why):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol, protocols="1-2")
    since = datetime.now(UTC)
    if why == "no saved configuration":
        updated(vol, taken_at=since, previous_json=False)
    elif why == "a later rollback":
        updated(vol, taken_at=since)
        _history(vol, state="rolled_back", finished=time.time(), from_version=__version__, to_version="99.0.0")
    elif why == "backup not in the newest five":
        updated(vol, taken_at=since)
        backups = config.settings.data_dir / "backups"
        for i in range(5):
            stamp = f"2026111{i}-101010"
            _folder(backups, stamp)
            _history(vol, state="refused", finished=time.time() - 3600, backup=f"/var/lib/spend-tracker/backups/{stamp}")
    else:
        rid = updated(vol, taken_at=since)
        path = vol.history(rid)
        doc = volume.read_own_json(path)
        volume.write_json(path, {**doc, "to_version": "99.0.0"})
    assert client.get(BASE, headers=HEADERS).json()["go_back"] is None
    answer = client.get(f"{BASE}/go-back", headers=HEADERS)
    assert answer.status_code == 404 and answer.json()["code"] == "update.go_back_unavailable"


def _code(client) -> dict:
    answer = client.post(f"{BASE}/go-back/recovery-code", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    assert answer.headers["cache-control"] == "no-store"
    return answer.json()


def test_going_back_needs_the_version_typed_and_writes_a_request_the_updater_takes(world, clock, caplog):
    client, vol, owner = world["client"], world["vol"], world["owner"]
    _heartbeat(vol, protocols="1-2")
    rid = updated(vol, taken_at=datetime.now(UTC))

    code = _code(client)
    wrong = client.post(
        f"{BASE}/go-back",
        json={"update_id": rid, "typed_version": __version__, "recovery_code_id": code["id"],
              "step_up_token": _grant(client, owner["secret"])},
        headers=HEADERS,
    )  # fmt: skip
    assert wrong.status_code == 422 and wrong.json()["code"] == "update.go_back_typed_version"
    assert wrong.json()["params"] == {"to_version": PREVIOUS}
    assert _request(vol) is None

    clock(2)
    code = _code(client)
    with caplog.at_level(logging.WARNING, logger="spendtracker"):
        answer = client.post(
            f"{BASE}/go-back",
            json={"update_id": rid, "typed_version": f" {PREVIOUS} ", "recovery_code_id": code["id"],
                  "step_up_token": _grant(client, owner["secret"])},
            headers=HEADERS,
        )  # fmt: skip
    assert answer.status_code == 202, answer.text
    written = _request(vol)
    assert {k: v for k, v in written.items() if k not in ("id", "created_at", "recovery_hash")} == {
        "protocol": 2,
        "kind": "go_back",
        "requested_by": owner["user"]["id"],
        "from_version": __version__,
        "to_version": PREVIOUS,
        "update_id": rid,
    }
    assert updates.recovery_code_matches(code["code"], written["recovery_hash"])
    assert code["code"] not in vol.request.read_text()
    assert any("going back from" in r.getMessage() and PREVIOUS in r.getMessage() for r in caplog.records)

    # The updater's own intake takes it: no refusal, and step 0's journal holds the hash.
    out = intake.take(vol, _ctx(), time.time(), owner_uid=os.getuid())
    assert out.accepted, out.refusal and out.refusal.sentence
    j = journal.load(vol, written["id"])
    assert j.kind == "go_back" and j.recovery_hash == written["recovery_hash"]


def test_an_updater_of_window_1_1_is_asked_to_update_first_and_nothing_is_written(world):
    client, vol, owner = world["client"], world["vol"], world["owner"]
    _heartbeat(vol)  # protocols 1-1 as an older updater writes it
    beat = volume.read_own_json(vol.heartbeat)
    volume.write_json(vol.heartbeat, {**beat, "protocols": "1-1"})
    rid = updated(vol, taken_at=datetime.now(UTC))
    assert client.get(BASE, headers=HEADERS).json()["go_back"]["updater_can"] is False
    code = _code(client)
    before = _files(vol.root)
    answer = client.post(
        f"{BASE}/go-back",
        json={"update_id": rid, "typed_version": PREVIOUS, "recovery_code_id": code["id"],
              "step_up_token": _grant(client, owner["secret"])},
        headers=HEADERS,
    )  # fmt: skip
    assert answer.status_code == 422 and answer.json()["code"] == "update.go_back_updater_too_old"
    assert _files(vol.root) == before


def test_go_back_spends_the_step_up_first(world):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol, protocols="1-2")
    rid = updated(vol, taken_at=datetime.now(UTC))
    code = _code(client)
    answer = client.post(
        f"{BASE}/go-back",
        json={"update_id": rid, "typed_version": PREVIOUS, "recovery_code_id": code["id"], "step_up_token": None},
        headers=HEADERS,
    )
    assert answer.status_code in (401, 403, 422) and _request(vol) is None


def test_the_window_parse_matches_the_updaters_heartbeat():
    assert updates.window_includes(f"{contract.PROTOCOLS[0]}-{contract.PROTOCOLS[1]}", 2)
    assert not updates.window_includes("1-1", 2) and not updates.window_includes(None, 2)
