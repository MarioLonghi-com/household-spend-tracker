"""Self-update from the app's side: the endpoints, the requests, the check (#165).

Design notes, Parts 3, 5 and 10; the app tests of 15.4 (A1-A4, A6, A7, A11)
at the API level, plus the one that holds the two halves of the contract
together: **every request the app writes is taken by the updater's own
`intake`**, field for field, and every file the updater writes -- through its
own dataclasses and its own writer -- is read back by the app.

The app does not import `updater/` (its image does not contain it); this test
module does, and that is the point of it.

The volume is a real directory, laid out by `updater.volume.Volume.init`. The
updater's ownership check is satisfied with this process's uid, which in the
container is 65532 on both sides.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
import pytest

from app import __version__, config
from app.services import platform as platform_service
from app.services import updates
from tests.conftest import HEADERS, PASSWORD, _setup_owner
from tests.test_invitations import _accept, _invite
from tests.test_outbound import body, local_server
from updater import contract, intake, volume
from updater.contract import Context, RunningApp
from updater.volume import Volume

ROOT = Path(__file__).resolve().parent.parent
BASE = "/api/admin/application/update"
DIGEST = "sha256:" + "3f2a" * 16
UPDATER_DIGEST = "sha256:" + "77b0" * 16
#: The next release after the running one, and the one after that.
#: Releases newer than the one running, counted from it: a version bump must
#: not turn the "newer" release into the running one (0.9.0 did, once).
_MAJOR, _MINOR, _ = (int(part) for part in __version__.split("."))
NEXT = f"{_MAJOR}.{_MINOR + 1}.0"
LATER = f"{_MAJOR}.{_MINOR + 2}.0"
#: Two migrations that need a box (one lossy, one undeclared) and one that
#: does not.
LOSSY = ["b2c3d4e5f6a1", "d4e5f6a1b2c3"]
PENDING = (
    {"revision": "b2c3d4e5f6a1", "reversible": "lossy", "title": "Fold theme colours", "note": "old values go"},
    {"revision": "c3d4e5f6a1b2", "reversible": "clean", "title": "Index receipts", "note": ""},
    {"revision": "d4e5f6a1b2c3", "reversible": "undeclared", "title": "An old one", "note": ""},
)


# --------------------------------------------------------------------------- #
# The world: an owner, a member, a volume, and the updater's files
# --------------------------------------------------------------------------- #


@pytest.fixture()
def world(client):
    updates.forget_recovery_codes()
    owner = _setup_owner(client)
    vol = Volume(config.settings.update_dir)
    vol.init()
    yield {"client": client, "owner": owner, "vol": vol}
    updates.forget_recovery_codes()


def _heartbeat(vol: Volume, **over) -> dict:
    """`updater.json`, as the updater's own dataclass writes it."""
    beat = contract.Heartbeat(
        updater_version=__version__,
        image_digest=UPDATER_DIGEST,
        seen_at=contract.iso(time.time()),
        engine="docker-desktop",
        engine_version="4.48.0",
        rootless=False,
        layout="loopback",
        socket="ok",
        hook=False,
        busy=False,
        role="current",
        api_version="1.47",
        engine_api="1.40-1.51",
        container="spend-tracker-updater-1",
    ).to_dict()
    beat.update(over)
    volume.write_json(vol.heartbeat, beat)
    return beat


def _report(vol: Volume, *, to_version: str = NEXT, hours: float = 24, **over) -> str:
    rid = str(uuid.uuid4())
    doc = contract.PreparedReport(
        id=rid,
        from_version=__version__,
        to_version=to_version,
        digest=DIGEST,
        updater_digest=UPDATER_DIGEST,
        expires_at=contract.iso(time.time() + hours * 3600),
        database_stamp="2de003489b79",
        pending=PENDING,
    ).to_dict()
    doc.update(over)
    volume.write_json(vol.prepared(rid), doc)
    return rid


def _history(vol: Volume, *, state: str, finished: float, backup: str | None = None, kind: str = "apply") -> str:
    rid = str(uuid.uuid4())
    record = contract.History(
        id=rid,
        kind=kind,
        state=state,
        sentence=f"The update {state.replace('_', ' ')}.",
        finished_at=contract.iso(finished),
        backup=backup,
        log_tail=("one", "two"),
    )
    volume.write_json(vol.history(rid), record.to_dict())
    return rid


def _ctx(updater_version: str = __version__) -> Context:
    return Context(
        app=RunningApp(version=__version__, revision="987afefca1d1", published=True),
        updater_version=updater_version,
    )


def _take(vol: Volume, ctx: Context | None = None) -> intake.Outcome:
    """The updater takes the request, exactly as it would in its container."""
    out = intake.take(vol, ctx or _ctx(), time.time(), owner_uid=os.getuid())
    assert out is not None, "there was no request to take"
    return out


def _request(vol: Volume) -> dict | None:
    try:
        return json.loads(vol.request.read_text())
    except FileNotFoundError:
        return None


def _files(where: Path) -> dict[str, bytes]:
    """Every file under `where`, by relative path, with its content."""
    return {
        str(path.relative_to(where)): path.read_bytes()
        for path in sorted(where.rglob("*"))
        if path.is_file()
    }


def _grant(client, secret: str, *, steps_ahead: int = 1) -> str:
    answer = client.post(
        "/api/me/step-up",
        json={"password": PASSWORD, "code": pyotp.TOTP(secret).at(int(time.time()) + 30 * steps_ahead)},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["token"]


def _code(client) -> dict:
    answer = client.post(f"{BASE}/recovery-code", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    assert answer.headers["cache-control"] == "no-store"
    return answer.json()


def _apply_body(rid: str, code_id: str, token: str | None, **over) -> dict:
    return {
        "prepared_id": rid,
        "digest": DIGEST,
        "updater_digest": UPDATER_DIGEST,
        "accepted_lossy": list(LOSSY),
        "recovery_code_id": code_id,
        "step_up_token": token,
        **over,
    }


# --------------------------------------------------------------------------- #
# A1: a member gets 403 everywhere, and no file is written
# --------------------------------------------------------------------------- #


def _routes(rid: str, record: str) -> list[tuple[str, str, dict | None]]:
    return [
        ("get", BASE, None),
        ("post", f"{BASE}/prepare", {"to_version": NEXT}),
        ("post", f"{BASE}/recovery-code", None),
        ("post", f"{BASE}/apply", _apply_body(rid, "x", "y")),
        ("post", f"{BASE}/discard", {"prepared_id": rid}),
        ("post", f"{BASE}/updater", {"to_version": NEXT}),
        ("post", f"{BASE}/outcome/{record}/seen", None),
        ("delete", "/api/admin/application/backups/20261001-101010", None),
    ]


def test_a_member_is_refused_every_update_route_and_nothing_is_written(world):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol)
    rid = _report(vol)
    record = _history(vol, state="succeeded", finished=time.time() - 60)
    created = _invite(client, role="member")
    client.cookies.clear()
    _accept(client, created["token"], email="member@gmail.com", name="Member")
    before = _files(vol.root)

    for method, path, payload in _routes(rid, record):
        answer = getattr(client, method)(path, **({"json": payload} if payload else {}), headers=HEADERS)
        assert answer.status_code == 403, f"{method.upper()} {path} answered {answer.status_code}"
        assert "owner" in answer.json()["detail"].lower()

    assert _request(vol) is None
    assert _files(vol.root) == before, "a refused member changed the volume"
    assert not (config.settings.data_dir / "update-outcomes-seen.json").exists()


def test_signed_out_every_update_route_is_401_and_nothing_is_written(world):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol)
    rid = _report(vol)
    record = _history(vol, state="succeeded", finished=time.time() - 60)
    before = _files(vol.root)
    client.cookies.clear()
    for method, path, payload in _routes(rid, record):
        answer = getattr(client, method)(path, **({"json": payload} if payload else {}), headers=HEADERS)
        assert answer.status_code == 401, f"{method.upper()} {path} answered {answer.status_code}"
    assert _files(vol.root) == before


# --------------------------------------------------------------------------- #
# The state the screen opens with
# --------------------------------------------------------------------------- #


def test_the_state_reads_what_the_updater_wrote_and_ignores_what_it_does_not_know(world, monkeypatch):
    """Cross-contract, the reading half: the updater's own dataclasses and
    writer produce the files; the app reads them. A key protocol 1 does not
    define is ignored, and a state it does not know reads as `running` (C4)."""
    client, vol = world["client"], world["vol"]
    monkeypatch.setenv("SPENDTRACKER_IN_CONTAINER", "1")

    empty = client.get(BASE, headers=HEADERS).json()
    assert (empty["case"], empty["heartbeat"], empty["status"], empty["in_flight"]) == (
        "no_updater",
        None,
        None,
        False,
    )

    beat = _heartbeat(vol, a_key_from_protocol_one_later="ignored")
    status = contract.Status(
        id=str(uuid.uuid4()), kind="prepare", state="running", updated_at=contract.iso(time.time()),
        step="P4", sentences=("found the release image", "there is room for it"),
    ).to_dict()
    status["state"] = "verifying_harder"  # a state a newer updater added
    status["progress"] = 0.4
    volume.write_json(vol.status, status)
    rid = _report(vol)
    older = _history(vol, state="rolled_back", finished=time.time() - 3600)
    newer = _history(vol, state="succeeded", finished=time.time() - 60)
    _history(vol, state="succeeded", finished=time.time(), kind="ping")

    got = client.get(BASE, headers=HEADERS).json()
    assert got["case"] == "working"
    assert got["running"] == __version__ and got["protocol"] == 1
    assert got["heartbeat"]["fresh"] is True
    assert got["heartbeat"]["container"] == beat["container"]
    assert got["heartbeat"]["engine_api"] == "1.40-1.51"
    assert "a_key_from_protocol_one_later" not in got["heartbeat"]
    assert got["status"]["state"] == "running", "an unknown state reads as running"
    assert got["status"]["sentences"] == ["found the release image", "there is room for it"]
    assert got["in_flight"] is True
    assert got["report"]["id"] == rid
    assert got["report"]["lossy"] == sorted(LOSSY)
    assert [m["revision"] for m in got["report"]["pending"]] == [m["revision"] for m in PENDING]
    assert got["outcome"]["id"] == newer, "the newest record, and never a ping"
    assert got["outcome"]["log_tail"] == ["one", "two"]

    # Dismissed, the next one shows; the record itself stays.
    seen = client.post(f"{BASE}/outcome/{newer}/seen", headers=HEADERS)
    assert seen.status_code == 204
    assert client.get(BASE, headers=HEADERS).json()["outcome"]["id"] == older
    assert vol.history(newer).exists()
    assert client.post(f"{BASE}/outcome/{uuid.uuid4()}/seen", headers=HEADERS).status_code == 404


@pytest.mark.parametrize(
    ("over", "case"),
    [
        ({"socket": "eci_blocked"}, "refused"),
        ({"socket": "outdated"}, "outdated"),
        ({"seen_at": contract.iso(time.time() - 300)}, "no_updater"),
    ],
)
def test_the_state_says_which_case_of_three_one_this_is(world, monkeypatch, over, case):
    monkeypatch.setenv("SPENDTRACKER_IN_CONTAINER", "1")
    _heartbeat(world["vol"], **over)
    assert world["client"].get(BASE, headers=HEADERS).json()["case"] == case


def test_a_refused_heartbeat_carries_the_updaters_sentence_for_the_screen(world, monkeypatch):
    """R24: the `refused` case says why in the updater's own sentence."""
    monkeypatch.setenv("SPENDTRACKER_IN_CONTAINER", "1")
    said = "Docker Desktop's Enhanced Container Isolation does not let containers use the Docker socket."
    _heartbeat(world["vol"], socket="eci_blocked", socket_sentence=said)
    got = world["client"].get(BASE, headers=HEADERS).json()
    assert (got["case"], got["heartbeat"]["socket_sentence"]) == ("refused", said)
    _heartbeat(world["vol"])
    assert world["client"].get(BASE, headers=HEADERS).json()["heartbeat"]["socket_sentence"] is None


def test_an_updater_that_cannot_go_on_carries_its_sentence_for_the_screen(world, monkeypatch):
    """#262: the heartbeat's `problem` reaches the screen as the updater wrote it, and goes with it."""
    monkeypatch.setenv("SPENDTRACKER_IN_CONTAINER", "1")
    said = "The updater cannot go on: the container engine will not list this installation's containers."
    _heartbeat(world["vol"], problem=said)
    got = world["client"].get(BASE, headers=HEADERS).json()
    assert (got["case"], got["heartbeat"]["problem"]) == ("working", said)
    _heartbeat(world["vol"])
    assert world["client"].get(BASE, headers=HEADERS).json()["heartbeat"]["problem"] is None


def test_a_report_for_another_version_or_past_its_time_is_not_current(world):
    client, vol = world["client"], world["vol"]
    _report(vol, hours=-1)
    _report(vol, from_version="0.7.1")
    assert client.get(BASE, headers=HEADERS).json()["report"] is None
    fresh = _report(vol)
    assert client.get(BASE, headers=HEADERS).json()["report"]["id"] == fresh


# --------------------------------------------------------------------------- #
# Prepare, and A4: a second one in flight is 409
# --------------------------------------------------------------------------- #


def test_prepare_writes_the_request_the_updater_accepts(world, caplog):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol)
    with caplog.at_level(logging.WARNING, logger="spendtracker"):
        answer = client.post(f"{BASE}/prepare", json={"to_version": NEXT}, headers=HEADERS)
    assert answer.status_code == 202, answer.text
    written = _request(vol)
    assert written == {
        "protocol": 1,
        "id": answer.json()["id"],
        "kind": "prepare",
        "created_at": written["created_at"],
        "requested_by": world["owner"]["user"]["id"],
        "from_version": __version__,
        "to_version": NEXT,
    }
    assert oct(vol.request.stat().st_mode & 0o777) == oct(0o640)
    assert not [p for p in vol.root.iterdir() if p.name.endswith(".tmp")]
    said = [r.getMessage() for r in caplog.records]
    assert any("prepared" in line and "jane.doe@gmail.com" in line.lower() for line in said), said

    taken = _take(vol)
    assert taken.accepted, taken.refusal
    assert taken.request.to_version == NEXT and taken.request.from_version == __version__
    assert taken.request.requested_by == world["owner"]["user"]["id"]


def test_a_second_prepare_in_flight_is_409_and_the_first_request_stands(world):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol)
    first = client.post(f"{BASE}/prepare", json={"to_version": NEXT}, headers=HEADERS)
    assert first.status_code == 202
    second = client.post(f"{BASE}/prepare", json={"to_version": LATER}, headers=HEADERS)
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "update.in_flight"
    assert _request(vol)["id"] == first.json()["id"]
    assert _request(vol)["to_version"] == NEXT

    # Taken by the updater and running: still in flight, from status.json.
    assert _take(vol).accepted
    third = client.post(f"{BASE}/prepare", json={"to_version": LATER}, headers=HEADERS)
    assert third.status_code == 409
    assert _request(vol) is None


@pytest.mark.parametrize(
    ("to_version", "code"),
    [(__version__, "update.not_newer"), ("0.1.0", "update.not_newer"), ("1.0.0-rc1", "update.not_a_version")],
)
def test_prepare_refuses_what_the_updater_would_with_a_sentence_and_writes_nothing(world, to_version, code):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol)
    answer = client.post(f"{BASE}/prepare", json={"to_version": to_version}, headers=HEADERS)
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == code
    assert _request(vol) is None


@pytest.mark.parametrize(
    ("over", "code"),
    [
        (None, "update.no_updater"),
        ({"seen_at": contract.iso(time.time() - 300)}, "update.no_updater"),
        ({"socket": "permission_denied"}, "update.engine_refused"),
        ({"socket": "outdated"}, "update.updater_outdated"),
    ],
)
def test_prepare_needs_an_updater_that_can_use_the_engine(world, over, code):
    client, vol = world["client"], world["vol"]
    if over is not None:
        _heartbeat(vol, **over)
    answer = client.post(f"{BASE}/prepare", json={"to_version": NEXT}, headers=HEADERS)
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == code
    assert _request(vol) is None


def test_a_planted_symlink_where_the_request_goes_is_not_written_through(world, tmp_path):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol)
    target = tmp_path / "elsewhere.json"
    os.symlink(target, vol.request)
    answer = client.post(f"{BASE}/prepare", json={"to_version": NEXT}, headers=HEADERS)
    assert answer.status_code == 409
    assert not target.exists(), "the write followed the symlink"
    assert os.path.islink(vol.request)


# --------------------------------------------------------------------------- #
# The recovery code
# --------------------------------------------------------------------------- #


def test_the_recovery_code_is_128_bits_or_more_of_crockford_and_only_its_hash_is_kept(world):
    client, vol = world["client"], world["vol"]
    rid = _report(vol)
    first, second = _code(client), _code(client)
    assert first["code"] != second["code"]
    assert first["prepared_id"] == rid
    for one in (first, second):
        assert re.fullmatch(r"([0-9A-HJKMNP-TV-Z]{4}-){6}[0-9A-HJKMNP-TV-Z]{4}", one["code"])
        assert contract.RECOVERY_CODE.fullmatch(one["code"]), "the recovery page could not forward it"
    assert len(first["code"].replace("-", "")) * 5 >= 128, "at least 128 bits"

    hashed = updates.hash_recovery_code(first["code"])
    assert contract.recovery_hash_ok(hashed), hashed
    m = contract.RECOVERY_HASH.fullmatch(hashed)
    assert (m.group(1), m.group(2), m.group(3)) == ("15", "8", "1")
    assert "=" not in m.group(4) + m.group(5), "unpadded"
    assert len(base64.b64decode(m.group(5) + "=")) == 32
    assert len(base64.b64decode(m.group(4) + "==")) == 16
    # Typed back in lower case, without its hyphens, it still matches.
    assert updates.recovery_code_matches(first["code"].lower().replace("-", ""), hashed)
    assert not updates.recovery_code_matches(second["code"], hashed)

    # The process holds hashes, never the codes.
    held = repr(updates._held)
    assert first["code"] not in held and second["code"] not in held
    assert len(updates._held) == 1, "drawing the confirmation again forgets the earlier code"


def test_no_recovery_code_without_a_current_report(world):
    answer = world["client"].post(f"{BASE}/recovery-code", headers=HEADERS)
    assert answer.status_code == 404
    assert answer.json()["code"] == "update.no_report"
    assert updates._held == {}


def test_a_cross_site_request_cannot_rotate_the_held_recovery_code(world):
    """R21: issuing a code replaces the held one, so it is a `POST` behind the
    Origin check. A `GET` is no route at all, and a `POST` from another site is
    refused before the held hash is touched."""
    client, vol = world["client"], world["vol"]
    _report(vol)
    issued = _code(client)
    held = dict(updates._held)
    assert set(held) == {issued["id"]}

    # 405 from the router, or 404 where the API's catch-all answers first:
    # either way no code, and the held one stays.
    got = client.get(f"{BASE}/recovery-code", headers=HEADERS)
    assert got.status_code in (404, 405)
    assert "code" not in got.json() or got.json()["code"] != issued["code"]
    assert updates._held == held
    foreign = client.post(f"{BASE}/recovery-code", headers={"Origin": "https://elsewhere.example"})
    assert foreign.status_code == 403
    # `code` here is the refusal's own (#267), never a recovery code.
    assert foreign.json().get("code") == "request.cross_origin"
    assert issued["code"] not in foreign.text
    assert updates._held == held, "a refused request rotated the code"


# --------------------------------------------------------------------------- #
# A3: apply, the lossy set, and the request field for field
# --------------------------------------------------------------------------- #


def test_apply_with_one_lossy_box_short_is_refused_and_the_exact_set_writes_the_request(world, caplog, clock):
    client, vol, owner = world["client"], world["vol"], world["owner"]
    _heartbeat(vol)
    rid = _report(vol)

    code = _code(client)
    short = client.post(
        f"{BASE}/apply",
        json=_apply_body(rid, code["id"], _grant(client, owner["secret"]), accepted_lossy=LOSSY[:1]),
        headers=HEADERS,
    )
    assert short.status_code == 422, short.text
    assert short.json()["code"] == "update.lossy_mismatch"
    assert _request(vol) is None

    clock(2)  # the next grant needs a code the first one did not burn
    code = _code(client)
    with caplog.at_level(logging.WARNING, logger="spendtracker"):
        answer = client.post(
            f"{BASE}/apply",
            json=_apply_body(rid, code["id"], _grant(client, owner["secret"])),
            headers=HEADERS,
        )
    assert answer.status_code == 202, answer.text
    written = _request(vol)
    assert set(written) == set(contract.FIELDS["apply"]) == set(updates.FIELDS["apply"])
    assert {k: v for k, v in written.items() if k not in ("id", "created_at", "recovery_hash")} == {
        "protocol": 1,
        "kind": "apply",
        "requested_by": owner["user"]["id"],
        "from_version": __version__,
        "to_version": NEXT,
        "prepared_id": rid,
        "digest": DIGEST,
        "updater_digest": UPDATER_DIGEST,
        "accepted_lossy": sorted(LOSSY),
    }
    assert written["id"] == answer.json()["id"]
    assert contract.recovery_hash_ok(written["recovery_hash"])
    assert updates.recovery_code_matches(code["code"], written["recovery_hash"])
    assert code["code"] not in vol.request.read_text()
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "confirmed" in said and all(rev in said for rev in LOSSY) and "jane.doe@gmail.com" in said.lower()
    assert code["code"] not in said

    # The updater takes it, and its journal holds the hash -- never the code.
    taken = _take(vol)
    assert taken.accepted, taken.refusal
    assert taken.request.accepted_lossy == tuple(sorted(LOSSY))
    assert taken.request.recovery_hash == written["recovery_hash"]
    everything = b"".join(_files(vol.root).values()) + b"".join(_files(config.settings.data_dir).values())
    for form in (code["code"], code["code"].replace("-", ""), updates.canonical_code(code["code"])):
        assert form.encode() not in everything, "the recovery code reached a file"


def test_the_recovery_code_is_spent_by_an_apply_and_cannot_be_used_twice(world, clock):
    client, vol, owner = world["client"], world["vol"], world["owner"]
    _heartbeat(vol)
    rid = _report(vol)
    code = _code(client)
    first = client.post(
        f"{BASE}/apply", json=_apply_body(rid, code["id"], _grant(client, owner["secret"])), headers=HEADERS
    )
    assert first.status_code == 202
    assert _take(vol).accepted
    volume.write_json(
        vol.status,
        contract.Status(id=first.json()["id"], kind="apply", state="rolled_back", updated_at=contract.iso(time.time())).to_dict(),
    )
    clock(2)
    again = client.post(
        f"{BASE}/apply",
        json=_apply_body(rid, code["id"], _grant(client, owner["secret"])),
        headers=HEADERS,
    )
    assert again.status_code == 422
    assert again.json()["code"] == "update.recovery_code_unknown"
    assert _request(vol) is None


@pytest.mark.parametrize("which", ["digest", "updater_digest"])
def test_apply_with_a_digest_that_is_not_the_reports_is_refused(world, which):
    client, vol, owner = world["client"], world["vol"], world["owner"]
    _heartbeat(vol)
    rid = _report(vol)
    code = _code(client)
    answer = client.post(
        f"{BASE}/apply",
        json=_apply_body(rid, code["id"], _grant(client, owner["secret"]), **{which: "sha256:" + "0" * 64}),
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json()["code"] == "update.digest_mismatch"
    assert _request(vol) is None


# --------------------------------------------------------------------------- #
# A2: a missing, expired, foreign or spent grant writes nothing
# --------------------------------------------------------------------------- #


def _plant_grant(user_id: str, *, expires_in: float) -> str:
    """A grant row as `stepup.grant` leaves one, for a user or a time a test picks."""
    from app import db
    from app.auth import tokens
    from app.models import StepUpGrant, utcnow

    value, id_hash = tokens.issue()
    now = utcnow()
    with db.SessionLocal() as session:
        session.add(
            StepUpGrant(id_hash=id_hash, user_id=user_id, created_at=now, expires_at=now + timedelta(seconds=expires_in))
        )
        session.commit()
    return value


@pytest.mark.parametrize("grant", ["missing", "expired", "foreign", "spent"])
def test_apply_without_a_live_grant_of_this_owners_writes_nothing(world, grant):
    client, vol, owner = world["client"], world["vol"], world["owner"]
    _heartbeat(vol)
    rid = _report(vol)
    code = _code(client)
    if grant == "missing":
        token = None
    elif grant == "expired":
        token = _plant_grant(owner["user"]["id"], expires_in=-1)
    elif grant == "foreign":
        token = _plant_grant(_member_id(client), expires_in=300)
    else:
        token = _grant(client, owner["secret"])
        # Spent on an apply that names no report: the grant goes first.
        spent = client.post(f"{BASE}/apply", json=_apply_body(str(uuid.uuid4()), code["id"], token), headers=HEADERS)
        assert spent.status_code == 422 and spent.json()["code"] == "update.no_report"
    before = _files(vol.root)

    answer = client.post(f"{BASE}/apply", json=_apply_body(rid, code["id"], token), headers=HEADERS)
    assert answer.status_code == 401, answer.text
    assert _request(vol) is None
    assert _files(vol.root) == before


def _member_id(client) -> str:
    """Somebody else with an account: a member, accepted in a second browser."""
    from app import db
    from app.models import Role, User

    with db.SessionLocal() as session:
        others = [u for u in session.query(User).all() if u.role is not Role.owner]
        if others:
            return others[0].id
    created = _invite(client, role="member")
    saved = dict(client.cookies)
    client.cookies.clear()
    _accept(client, created["token"], email="member@gmail.com", name="Member")
    client.cookies.clear()
    for name, value in saved.items():
        client.cookies.set(name, value)
    with db.SessionLocal() as session:
        return next(u.id for u in session.query(User).all() if u.role is not Role.owner)


# --------------------------------------------------------------------------- #
# Discard and the updater
# --------------------------------------------------------------------------- #


def test_discard_writes_a_request_the_updater_accepts_and_leaves_the_report_to_it(world, caplog):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol)
    rid = _report(vol)
    assert client.post(f"{BASE}/discard", json={"prepared_id": str(uuid.uuid4())}, headers=HEADERS).status_code == 404
    with caplog.at_level(logging.WARNING, logger="spendtracker"):
        answer = client.post(f"{BASE}/discard", json={"prepared_id": rid}, headers=HEADERS)
    assert answer.status_code == 202, answer.text
    assert _request(vol)["prepared_id"] == rid and _request(vol)["kind"] == "discard"
    assert any("discarded" in r.getMessage() and rid in r.getMessage() for r in caplog.records)
    assert vol.prepared(rid).exists(), "the updater's discard needs the report to be there"
    taken = _take(vol)
    assert taken.accepted, taken.refusal
    assert taken.request.prepared_id == rid


def test_the_updater_only_names_a_newer_release_and_the_updater_accepts_it(world):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol)
    answer = client.post(f"{BASE}/updater", json={"to_version": NEXT}, headers=HEADERS)
    assert answer.status_code == 202, answer.text
    assert _request(vol) == {
        "protocol": 1,
        "id": answer.json()["id"],
        "kind": "update_updater",
        "created_at": _request(vol)["created_at"],
        "requested_by": world["owner"]["user"]["id"],
        "to_version": NEXT,
    }
    taken = _take(vol)
    assert taken.accepted, taken.refusal
    assert taken.request.to_version == NEXT


@pytest.mark.parametrize(
    ("to_version", "updater_version", "code"),
    [
        ("0.7.1", "0.7.0", "update.updater_older_than_app"),
        ("0.1.0", "0.1.0", "update.updater_older_than_app"),
        (NEXT, NEXT, "update.updater_not_newer"),
        (None, __version__, "update.updater_not_newer"),
    ],
)
def test_the_updater_only_with_an_incompatible_version_is_422(world, to_version, updater_version, code):
    client, vol = world["client"], world["vol"]
    _heartbeat(vol, updater_version=updater_version)
    answer = client.post(f"{BASE}/updater", json={"to_version": to_version}, headers=HEADERS)
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == code
    assert _request(vol) is None


def test_the_updater_only_is_allowed_when_the_engine_has_outgrown_it(world):
    """C3: `outdated` refuses prepare and apply, and still tries this."""
    client, vol = world["client"], world["vol"]
    _heartbeat(vol, socket="outdated", updater_version="0.7.1")
    answer = client.post(f"{BASE}/updater", headers=HEADERS)
    assert answer.status_code == 202, answer.text
    assert _request(vol)["to_version"] == __version__
    ctx = Context(app=_ctx().app, updater_version="0.7.1", socket="outdated")
    assert _take(vol, ctx).accepted


def test_every_request_the_app_writes_has_exactly_the_updaters_keys():
    assert {k: v for k, v in contract.FIELDS.items() if k != "ping"} == updates.FIELDS
    assert updates.PROTOCOL == contract.FROZEN_PROTOCOL
    assert updates.SETTLED | {"accepted", "running", "needs_recovery"} == set(contract.STATES)
    assert updates.VERSION.pattern == contract.VERSION.pattern


# --------------------------------------------------------------------------- #
# A11: the newest five update backups cannot be deleted
# --------------------------------------------------------------------------- #


def _folder(root: Path, stamp: str, *, version: str = "0.7.1", revision: str = "2de003489b79") -> Path:
    folder = root / stamp
    folder.mkdir(parents=True)
    (folder / "spendtracker.sqlite3").write_bytes(b"x" * 100)
    (folder / "manifest.json").write_text(
        json.dumps({"taken_at": datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC).isoformat(),
                    "app_version": version, "alembic_revision": revision})
    )
    return folder


def test_the_fifth_newest_update_backup_is_kept_and_the_sixth_newest_can_go(world, caplog):
    client, vol = world["client"], world["vol"]
    backups = config.settings.data_dir / "backups"
    stamps = [f"202610{day:02d}-101010" for day in range(1, 8)]  # seven, oldest first
    for i, stamp in enumerate(stamps):
        _folder(backups, stamp, version=f"0.{i}.0")
        _history(vol, state="succeeded", finished=time.time() - 100 * (10 - i), backup=f"/var/lib/spend-tracker/backups/{stamp}")
    by_hand = _folder(backups, "20261008-101010")  # newer than all, and not an update's

    listed = {one["name"]: one for one in client.get("/api/admin/application/backups", headers=HEADERS).json()}
    newest_first = list(reversed(stamps))
    assert [listed[s]["protected"] for s in newest_first] == [True] * 5 + [False] * 2
    assert listed[newest_first[0]]["kind"] == "update" and listed[by_hand.name]["kind"] == "folder"
    assert listed[newest_first[0]]["version"] == "0.6.0"
    assert listed[newest_first[0]]["revision"] == "2de003489b79"
    assert listed[newest_first[0]]["bytes"] == 100 + len((backups / newest_first[0] / "manifest.json").read_bytes())
    state = client.get(BASE, headers=HEADERS).json()
    assert sorted(one["name"] for one in state["backups"]) == sorted(stamps)

    fifth, sixth = newest_first[4], newest_first[5]
    refused = client.delete(f"/api/admin/application/backups/{fifth}", headers=HEADERS)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "backup.protected"
    assert "newest five update backups" in refused.json()["detail"]
    assert (backups / fifth / "spendtracker.sqlite3").exists()

    with caplog.at_level(logging.WARNING, logger="spendtracker"):
        gone = client.delete(f"/api/admin/application/backups/{sixth}", headers=HEADERS)
    assert gone.status_code == 204, gone.text
    assert not (backups / sixth).exists()
    assert any(sixth in r.getMessage() and "deleted" in r.getMessage() for r in caplog.records)
    # A folder made by hand is never protected.
    assert client.delete(f"/api/admin/application/backups/{by_hand.name}", headers=HEADERS).status_code == 204
    assert not by_hand.exists()


# --------------------------------------------------------------------------- #
# A6, A7: the check reads published releases, with their notes as text
# --------------------------------------------------------------------------- #


def _release(version: str, *, body_text: str = "", draft: bool = False, prerelease: bool = False, tag: str | None = None) -> dict:
    return {
        "tag_name": tag or f"v{version}",
        "name": f"v{version}",
        "draft": draft,
        "prerelease": prerelease,
        "published_at": "2026-10-20T21:00:00Z",
        "body": body_text,
        "html_url": f"https://github.com/MarioLonghi-com/household-spend-tracker/releases/tag/v{version}",
    }


SECTION = (
    "**Reversible: lossy** — one migration.\n\n### Added\n\n- A thing <script>alert(1)</script> & more.\n"
)
INSTALL = "\n---\n\nNeeds Python 3.12+ and nothing else.\n\n<!-- Release notes generated -->\n\n## What's Changed\n* a PR\n"


def _check(monkeypatch, releases: list[dict]):
    with local_server(body(json.dumps(releases).encode())) as (server, asked):
        monkeypatch.setattr(platform_service, "UPSTREAM_RELEASES", f"{server}/repos/x/releases?per_page=30")
        answer = platform_service.check_upstream()
    assert len(asked) == 1
    assert asked[0]["path"] == "/repos/x/releases?per_page=30"
    return answer


def test_a_newer_tag_draft_or_prerelease_is_not_offered(monkeypatch, tmp_path):
    answer = _check(
        monkeypatch,
        [
            _release(NEXT, draft=True),
            _release(f"{_MAJOR}.{_MINOR + 1}.1", prerelease=True),
            _release(f"{_MAJOR}.{_MINOR + 1}.2", tag=f"v{_MAJOR}.{_MINOR + 1}.2-rc1"),
            _release(f"{_MAJOR}.{_MINOR + 1}.3", tag=f"{_MAJOR}.{_MINOR + 1}.3"),
            _release(__version__),
            _release("0.7.1"),
        ],
    )
    assert (answer.problem, answer.newer, answer.releases) == (None, False, [])
    assert answer.latest == __version__


def test_two_newer_releases_are_both_offered_newest_first_with_their_notes(monkeypatch):
    answer = _check(
        monkeypatch,
        [
            _release(NEXT, body_text=SECTION + INSTALL),
            _release(LATER, body_text=SECTION.replace("one migration", "none") + INSTALL),
            _release("0.7.1", body_text="Needs Python 3.12+ and nothing else."),
        ],
    )
    assert (answer.problem, answer.newer, answer.latest) == (None, True, LATER)
    assert [one.version for one in answer.releases] == [LATER, NEXT], "by number, not as text"
    assert answer.releases[1].notes == SECTION.strip()
    assert answer.releases[1].notes_from == "changelog"
    assert "Needs Python" not in answer.releases[0].notes and "What's Changed" not in answer.releases[0].notes
    assert answer.updater.version == LATER and answer.updater.compatible is None
    assert "checks it" in answer.updater.note


def test_an_older_release_body_is_returned_as_it_is_and_notes_stay_plain_text(monkeypatch, client):
    """A7 at the API: nothing is rendered, escaped or stripped -- the screen
    shows the string as text, so `<script>` is four characters and a word."""
    old_style = "Needs Python 3.12+ and nothing else.\n\n<b>bold</b> & `code`"
    answer = _check(monkeypatch, [_release(NEXT, body_text=old_style), _release(LATER, body_text=SECTION + INSTALL)])
    assert answer.releases[1].notes == old_style
    assert answer.releases[1].notes_from == "release"
    assert "<script>alert(1)</script>" in answer.releases[0].notes

    _setup_owner(client)
    monkeypatch.setattr(platform_service, "check_upstream", lambda: answer)
    got = client.post("/api/admin/application/upstream", headers=HEADERS).json()
    assert got["releases"][1]["notes"] == old_style
    assert got["releases"][0]["notes"] == SECTION.strip()


def test_the_check_asks_github_for_releases_and_not_tags():
    assert platform_service.UPSTREAM_RELEASES == (
        "https://api.github.com/repos/MarioLonghi-com/household-spend-tracker/releases?per_page=30"
    )


# --------------------------------------------------------------------------- #
# The app image does not carry updater/, so app/ must not import it
# --------------------------------------------------------------------------- #


@pytest.mark.repo_wide
def test_the_app_never_imports_the_updater_which_its_image_does_not_contain():
    # The app's stage only: the same Dockerfile builds the updater's own
    # image as its `updater` target (#164), which copies updater/ by design.
    dockerfile = (ROOT / "Dockerfile").read_text()
    app_stage = dockerfile[dockerfile.index("AS runtime\n") :]
    assert re.search(r"^COPY\s+updater", dockerfile, re.M), "the updater target is gone; re-read this test"
    assert not re.search(r"^COPY\s+updater", app_stage, re.M), (
        "the app image now carries updater/; this test's reason has changed"
    )
    importing = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if re.search(r"^\s*(from|import)\s+updater\b", path.read_text(), re.M)
    ]
    assert importing == []


def test_the_stepup_docstring_names_apply():
    from app.auth import stepup

    assert "/admin/application/update/apply" in stepup.__doc__
