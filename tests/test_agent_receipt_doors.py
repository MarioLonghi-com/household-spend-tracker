"""Three doors onto one receipt store, and the document an import came from.

Receipts went in one per call, as JSON, with the bytes base64-encoded: seven
receipts meant seven round trips and a third again in size on every one. The
base64 default stays -- an MCP server over stdio cannot stream a file, and the
reporting agent said so explicitly -- so these are additions beside it, not a
replacement. Issue #48.

And an agent that parses its own statement keeps the parsing, deliberately, but
the app never sees the document -- so a year later nothing can be traced back
to what it was read off. Issue #49.

The `_store_receipt` helper is what all three doors go through, and the
assertions here are mostly about them agreeing: the duplicate rule, the batch,
and the claim have to be identical whichever way the bytes arrived, because
three copies of that logic is how two of them drift.
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import AgentScope, Batch, BatchKind, Household, Receipt, User
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner
from tests.receipt_fixtures import as_bytes, receipt_image

V1 = "/api/agent/v1"


def _jpeg(seed: int = 1) -> bytes:
    return as_bytes(receipt_image((60 + seed, 80 + seed)))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@pytest.fixture()
def world(client):
    """One household, two accounts, and a key that may write."""
    made = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    other = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Tokyo", "type": "cash", "currency": "JPY"},
        headers=HEADERS,
    ).json()
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, made["user"]["id"])
        row = own.get(Household, house["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=row.id):
            _k, write = key_service.issue(
                own, user=user, household=row, label="the filer",
                scope=AgentScope.write, may_commit=True,
            )
        own.commit()
    client.cookies.clear()
    return {
        "client": client, "house": house, "account": account, "other": other,
        "token": write,
    }


def _auth(world, key=None) -> dict:
    headers = {"authorization": f"Bearer {world['token']}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _receipts(world) -> list[dict]:
    answer = world["client"].get(
        f"{V1}/households/{world['house']['id']}/receipts", headers=_auth(world)
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


# --------------------------------------------------------------------------- #
# #48 -- bytes, rather than bytes spelled out in base64
# --------------------------------------------------------------------------- #


def test_a_receipt_can_be_posted_as_its_own_bytes(world):
    raw = _jpeg(1)
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts/binary?filename=till.jpg",
        content=raw,
        headers={**_auth(world), "content-type": "image/jpeg"},
    )
    assert answer.status_code == 201, answer.text

    stored = answer.json()
    assert stored["already_had_it"] is False
    assert stored["receipt"]["byte_size"] == len(raw), "the bytes arrived whole"
    assert stored["receipt"]["needs_a_transaction"] is True, "it went to the inbox"
    assert stored["batch_id"], "it is in History, like every other write"


def test_the_two_doors_store_the_identical_thing(world):
    """Same picture, two ways in. The second must recognise the first.

    This is the assertion that makes the refactor safe: if the binary route
    had its own copy of the duplicate rule, this is where it would show.
    """
    raw = _jpeg(2)

    first = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts",
        json={"content_base64": _b64(raw), "filename": "a.jpg"},
        headers=_auth(world),
    ).json()
    assert first["already_had_it"] is False

    second = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts/binary?filename=b.jpg",
        content=raw,
        headers={**_auth(world), "content-type": "image/jpeg"},
    ).json()

    assert second["already_had_it"] is True, "the same bytes twice in the inbox is one receipt"
    assert second["receipt"]["id"] == first["receipt"]["id"]
    assert second["receipt"]["content_sha256"] == first["receipt"]["content_sha256"]
    assert len(_receipts(world)) == 1, "and nothing was stored a second time"


def test_an_empty_binary_body_is_told_so(world):
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts/binary",
        content=b"",
        headers={**_auth(world), "content-type": "image/jpeg"},
    )
    assert answer.status_code == 422, answer.text
    assert "bytes" in answer.json()["detail"]


def test_an_oversized_binary_body_is_refused_on_what_it_declared(world):
    """Refused before the body is read, not after it is in memory.

    The reason `refuse_declared_size` exists one function down: reading it and
    then measuring it spends exactly the memory the ceiling is there to save.
    """
    from app.api.routers.agent import MAX_BASE64_BYTES

    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts/binary",
        content=b"x" * 32,
        headers={
            **_auth(world),
            "content-type": "image/jpeg",
            "content-length": str(MAX_BASE64_BYTES + 1),
        },
    )
    assert answer.status_code == 413, answer.text
    _says_how_to_shrink(answer.json()["detail"])


def _says_how_to_shrink(detail: str) -> None:
    """#39: every refusal over the ceiling says what to do -- shrink, keeping
    the EXIF the app reads -- and no longer points at the multipart route,
    which belongs to a signed-in person and no key can use."""
    assert "multipart" not in detail
    assert "DateTimeOriginal" in detail and "GPS" in detail
    assert '"A photo over 4 MB"' in detail


def test_a_photo_over_the_ceiling_is_told_how_to_shrink_on_every_door(world):
    from app.api.routers.agent import MAX_BASE64_BYTES

    over = b"\xff\xd8" + b"x" * MAX_BASE64_BYTES
    binary = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts/binary",
        content=over,
        headers={**_auth(world), "content-type": "image/jpeg"},
    )
    assert binary.status_code == 413, binary.text
    _says_how_to_shrink(binary.json()["detail"])

    import base64

    encoded = base64.b64encode(over).decode()
    single = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts",
        json={"content_base64": encoded},
        headers=_auth(world),
    )
    assert single.status_code == 413, single.text
    _says_how_to_shrink(single.json()["detail"])
    assert len(_receipts(world)) == 0


def test_a_trips_worth_goes_in_one_call(world):
    """Seven receipts, seven round trips. They arrive a trip at a time."""
    pictures = [_jpeg(seed) for seed in range(10, 17)]

    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts/batch",
        json={"receipts": [{"content_base64": _b64(raw)} for raw in pictures]},
        headers=_auth(world, key="a-trip"),
    )
    assert answer.status_code == 201, answer.text

    did = answer.json()
    assert did["created"] == 7
    assert len(did["stored"]) == 7, "one answer per submitted receipt, in order"
    assert len(_receipts(world)) == 7

    # Each one is a distinct file, not the same one stored seven times.
    assert len({one["receipt"]["content_sha256"] for one in did["stored"]}) == 7


def test_a_part_repeat_keeps_each_receipts_own_answer(world):
    """The normal case when a caller re-sends a trip after a timeout.

    Collapsing the array to one verdict would hide which of the seven were
    already here -- and "you already had this" is a result to report, not an
    error to retry.
    """
    old, new = _jpeg(20), _jpeg(21)
    world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts",
        json={"content_base64": _b64(old)},
        headers=_auth(world),
    )

    did = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts/batch",
        json={"receipts": [{"content_base64": _b64(old)}, {"content_base64": _b64(new)}]},
        headers=_auth(world, key="again"),
    ).json()

    assert [one["already_had_it"] for one in did["stored"]] == [True, False]
    assert did["created"] == 1
    assert len(_receipts(world)) == 2


def test_the_manifest_says_the_cap_is_on_the_decoded_bytes(world):
    """Easy to get wrong when sizing a batch: 4 MB decoded is ~5.5 MB of JSON."""
    manifest = world["client"].get(f"{V1}/manifest", headers=_auth(world)).json()
    said = next(
        e["says"] for e in manifest["endpoints"]
        if e["method"] == "POST" and e["path"].endswith("/receipts")
    )
    assert "DECODED" in said, said
    assert "5.5" in said, "and what that means for the body a caller has to send"


def test_both_new_doors_are_in_the_manifest(world):
    manifest = world["client"].get(f"{V1}/manifest", headers=_auth(world)).json()
    paths = {(e["method"], e["path"]) for e in manifest["endpoints"]}
    base = "/api/agent/v1/households/{household_id}"
    assert ("POST", f"{base}/receipts/binary") in paths
    assert ("POST", f"{base}/receipts/batch") in paths


# --------------------------------------------------------------------------- #
# #49 -- the document the rows were read off
# --------------------------------------------------------------------------- #


def _staged(world) -> str:
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports",
        json={
            "account_id": world["account"]["id"],
            "source": "Santander card statement, May-June 2026",
            "rows": [{"date": "2026-05-23", "amount_minor": -2_000,
                      "payee": "NIGHT OWL", "external_id": "s-1"}],
        },
        headers=_auth(world),
    )
    assert answer.status_code == 201, answer.text
    return answer.json()["batch_id"]


def test_an_import_can_keep_the_statement_it_was_read_off(world):
    """Provenance without taking on a PDF parser.

    The parsing stays with the agent, deliberately -- what is recovered is the
    document, so a year later the rows can be traced back to it.
    """
    batch_id = _staged(world)
    pdf = _jpeg(30)

    answer = world["client"].post(
        f"{V1}/imports/{batch_id}/document",
        json={"content_base64": _b64(pdf), "filename": "santander-may.pdf"},
        headers=_auth(world),
    )
    assert answer.status_code == 201, answer.text
    stored = answer.json()

    with Session(world["client"].app_module.db_engine) as own:
        row = own.get(Batch, batch_id)
        document = (row.source or {}).get("document")
        assert document is not None, "the import has to be what records it"
        assert document["receipt_id"] == stored["receipt"]["id"]
        assert document["sha256"] == stored["receipt"]["content_sha256"]
        assert document["filename"] == "santander-may.pdf"
        assert document["bytes"] == len(pdf)

        # And the bytes are really in the store, reachable and referenced --
        # which is what stops the orphan sweep taking them.
        kept = own.get(Receipt, document["receipt_id"])
        assert kept is not None
        assert kept.household_id == world["house"]["id"]


def test_the_document_says_what_it_is(world):
    """It lands among receipts, so it has to read as a statement and not a till roll."""
    batch_id = _staged(world)
    world["client"].post(
        f"{V1}/imports/{batch_id}/document",
        json={"content_base64": _b64(_jpeg(31))},
        headers=_auth(world),
    )

    with Session(world["client"].app_module.db_engine) as own:
        kept = own.execute(select(Receipt)).scalars().one()
        assert batch_id in (kept.note or ""), kept.note


def test_a_document_cannot_be_attached_to_another_households_import(world, client):
    """404, not 403: a key should not learn the id is real."""
    answer = world["client"].post(
        f"{V1}/imports/00000000000000000000000000000000/document",
        json={"content_base64": _b64(_jpeg(32))},
        headers=_auth(world),
    )
    assert answer.status_code == 404, answer.text


def test_the_import_keeps_everything_else_it_recorded(world):
    """`source` is read by `previous_import_of`, `describing` and the Import
    screen. Adding the document must not take the filename with it."""
    batch_id = _staged(world)
    with Session(world["client"].app_module.db_engine) as own:
        before = dict(own.get(Batch, batch_id).source or {})

    world["client"].post(
        f"{V1}/imports/{batch_id}/document",
        json={"content_base64": _b64(_jpeg(33))},
        headers=_auth(world),
    )

    with Session(world["client"].app_module.db_engine) as own:
        after = own.get(Batch, batch_id).source or {}
    for key, value in before.items():
        assert after[key] == value, f"{key} was lost or changed"
    assert "document" in after


def _receipt_count(world) -> int:
    with Session(world["client"].app_module.db_engine) as own:
        return len(own.execute(select(Receipt)).scalars().all())


def _second_member_import(world, *, commit: bool) -> str:
    """Bob, a second member with a key of his own, stages (and maybe applies) one."""
    from sqlalchemy import text

    from app.models import HouseholdMember, Role, utcnow

    with Session(world["client"].app_module.db_engine, expire_on_commit=False) as own:
        jane = own.execute(select(User)).scalars().first()
        bob = User(
            email="bob@example.com", email_canonical="bob@example.com",
            display_name="Bob", password_hash="argon2-placeholder",
            role=Role.member, totp_secret=b"sealed-placeholder",
        )
        own.execute(text("PRAGMA defer_foreign_keys=ON"))
        with batch(own, kind=BatchKind.setup, actor_id=bob.id, own_transaction=False):
            own.add(bob)
        own.commit()
        house = own.get(Household, world["house"]["id"])
        with batch(own, kind=BatchKind.admin, actor_id=jane.id, household_id=house.id):
            own.add(HouseholdMember(
                household_id=house.id, user_id=bob.id, added_by_id=jane.id, added_at=utcnow()
            ))
        with batch(own, kind=BatchKind.admin, actor_id=bob.id, household_id=house.id):
            _k, token = key_service.issue(
                own, user=bob, household=house, label="bob's filer",
                scope=AgentScope.write, may_commit=True,
            )
        own.commit()

    bobs = {"authorization": f"Bearer {token}"}
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports",
        json={
            "account_id": world["other"]["id"],
            "source": "Bob's cash book",
            "rows": [{"date": "2026-05-24", "amount_minor": -500,
                      "payee": "KIOSK", "external_id": "b-1"}],
        },
        headers=bobs,
    )
    assert answer.status_code == 201, answer.text
    batch_id = answer.json()["batch_id"]
    if commit:
        done = world["client"].post(f"{V1}/imports/{batch_id}/commit", headers=bobs)
        assert done.status_code == 200, done.text
    return batch_id


def test_a_statement_goes_only_on_an_import(world):
    """Issue #214. A key issuance and a typed edit are batches too, in this
    household, and `source` on them was written with no record of it."""
    with Session(world["client"].app_module.db_engine, expire_on_commit=False) as own:
        issuance = own.execute(
            select(Batch).where(Batch.kind == BatchKind.admin)
        ).scalars().first()
        jane = own.get(User, issuance.actor_id)
        with batch(own, kind=BatchKind.manual, actor_id=jane.id,
                   household_id=world["house"]["id"]) as typed:
            pass
        own.commit()
        targets = {issuance.id: dict(issuance.source or {}), typed.id: dict(typed.source or {})}
    before = _receipt_count(world)

    for batch_id, source in targets.items():
        answer = world["client"].post(
            f"{V1}/imports/{batch_id}/document",
            json={"content_base64": _b64(_jpeg(40))},
            headers=_auth(world),
        )
        assert 400 <= answer.status_code < 500, answer.text
        with Session(world["client"].app_module.db_engine) as own:
            assert (own.get(Batch, batch_id).source or {}) == source, "left as it was"
    assert _receipt_count(world) == before, "and nothing was filed in the inbox either"


def test_a_statement_cannot_go_on_somebody_elses_import(world):
    """Bob's applied import is not this key's to annotate, nor his staged one."""
    applied = _second_member_import(world, commit=True)
    before = _receipt_count(world)

    answer = world["client"].post(
        f"{V1}/imports/{applied}/document",
        json={"content_base64": _b64(_jpeg(41))},
        headers=_auth(world),
    )
    assert answer.status_code == 404, answer.text
    with Session(world["client"].app_module.db_engine) as own:
        assert "document" not in (own.get(Batch, applied).source or {})
    assert _receipt_count(world) == before


def test_a_statement_is_attached_once(world):
    """Its own applied import takes one statement, and a second is refused
    before anything is stored."""
    batch_id = _staged(world)
    assert world["client"].post(
        f"{V1}/imports/{batch_id}/commit", headers=_auth(world)
    ).status_code == 200

    first = world["client"].post(
        f"{V1}/imports/{batch_id}/document",
        json={"content_base64": _b64(_jpeg(42)), "filename": "may.pdf"},
        headers=_auth(world),
    )
    assert first.status_code == 201, first.text
    stored = _receipt_count(world)

    second = world["client"].post(
        f"{V1}/imports/{batch_id}/document",
        json={"content_base64": _b64(_jpeg(43)), "filename": "june.pdf"},
        headers=_auth(world),
    )
    assert second.status_code == 409, second.text
    with Session(world["client"].app_module.db_engine) as own:
        assert own.get(Batch, batch_id).source["document"]["filename"] == "may.pdf"
    assert _receipt_count(world) == stored, "the refused one was not filed"


def test_the_row_provenance_convention_is_published(world):
    """`details` had nowhere to say what belonged in it, so everyone invented one."""
    manifest = world["client"].get(f"{V1}/manifest", headers=_auth(world)).json()

    said = manifest["conventions"]["row_provenance"]
    for name in ("source_page", "bank_reference", "raw_description", "value_date"):
        assert name in said, f"{name} is missing from the named shape"


def test_what_an_agent_puts_in_details_survives_to_the_ledger(world):
    """The convention is only worth naming if the field actually carries it."""
    answer = world["client"].post(
        f"{V1}/households/{world['house']['id']}/imports?include_lines=all",
        json={
            "account_id": world["account"]["id"],
            "source": "Santander May 2026",
            "rows": [{
                "date": "2026-05-23", "amount_minor": -2_000, "payee": "NIGHT OWL",
                "external_id": "s-9",
                "details": {"source_page": 3, "bank_reference": "0293841",
                            "raw_description": "PAGO MOVIL NIGHT OWL"},
            }],
        },
        headers=_auth(world),
    )
    assert answer.status_code == 201, answer.text

    bank = answer.json()["lines"][0]["parsed"]["bank"]
    assert bank["source_page"] == 3
    assert bank["bank_reference"] == "0293841"
    assert bank["raw_description"] == "PAGO MOVIL NIGHT OWL"


# Reads agent/README.md: run on every pull request, so a README-only change meets it.
@pytest.mark.repo_wide
def test_the_section_the_refusal_names_is_in_the_agent_readme():
    """The 413 sends an agent to a heading by name; the heading has to exist
    and say the three things the decision asked for (#39)."""
    import pathlib
    import re

    readme = (pathlib.Path(__file__).resolve().parent.parent / "agent" / "README.md").read_text()
    section = re.search(r"^#### A photo over 4 MB\n(.*?)(?=^#)", readme, re.M | re.S)
    assert section, "the heading the 413 names is gone"
    text = section.group(1)
    assert "Under 4 MB" in text and "JPEG or AVIF" in text
    assert "DateTimeOriginal" in text and "GPS" in text
    assert "Send the file as it is when it is 4 MB or\nless" in readme
