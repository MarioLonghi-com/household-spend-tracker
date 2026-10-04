"""A slow file does not stop the server. Issue #84.

`imports.upload`, `imports/recognise` and the agent's binary receipt door are
`async def` routes -- they have to be, to stream the body -- and each used to
do its CPU work inline: sniffing and parsing a statement, reading one for an
account number, encoding a photograph. On the event loop, that is every other
request in the process waiting for one file.

Each test holds the CPU step open with an event, asks a cheap GET while it is
held, and only then lets it finish. With the work inline, the GET cannot even
be dispatched until the held step times out, and the timing assertion fails.
The held request is then checked for what it produced, because a route that
answered promptly by not doing the work would pass the timing half on its own.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.api import offload
from tests.conftest import HEADERS
from tests.test_agent_receipt_doors import V1, _auth, _jpeg, _receipts, world  # noqa: F401
from tests.test_import_queue import CARD_FILE, _upload, _world

#: How long the fake slow step holds before giving up on its own. The GET must
#: answer in well under this, or it was waiting for the hold.
HOLD = 10.0
PROMPT = 3.0


class Held:
    """Wrap `real` so the first call parks until released."""

    def __init__(self, real):
        self.real = real
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        if not self.started.is_set():
            self.started.set()
            self.release.wait(HOLD)
        return self.real(*args, **kwargs)


def _while_held(held: Held, slow_request, cheap_request):
    """Run `slow_request` in a thread, time `cheap_request` while it is held."""
    answer: dict = {}

    def run():
        answer["slow"] = slow_request()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert held.started.wait(HOLD), "the slow request never reached its CPU step"
        began = time.perf_counter()
        cheap = cheap_request()
        took = time.perf_counter() - began
    finally:
        held.release.set()
        worker.join(HOLD * 2)
    return cheap, took, answer["slow"]


def test_a_cheap_get_answers_while_a_statement_is_being_parsed(client, monkeypatch):
    from statements import sniffing

    ours = _world(client)
    held = Held(sniffing.sniff)
    monkeypatch.setattr(sniffing, "sniff", held)

    cheap, took, slow = _while_held(
        held,
        lambda: _upload(client, ours["house"], ours["card"], CARD_FILE, name="oct.ofx"),
        lambda: client.get(f"/api/households/{ours['house']}/identifiers", headers=HEADERS),
    )

    assert cheap.status_code == 200, cheap.text
    assert took < PROMPT, f"the GET waited {took:.1f}s behind somebody else's statement"
    assert slow.status_code == 201, slow.text
    staged = slow.json()
    assert staged["account_id"] == ours["card"]
    assert len(staged["lines"]) == 3, "and the held upload still staged all three rows"


def test_a_cheap_get_answers_while_a_statement_is_being_recognised(client, monkeypatch):
    from app.services import identifiers

    ours = _world(client)
    base = f"/api/households/{ours['house']}"
    made = client.post(
        f"{base}/identifiers",
        json={"kind": "number", "value": "CARD|11223", "account_id": ours["card"]},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    held = Held(identifiers.read_evidence)
    monkeypatch.setattr(identifiers, "read_evidence", held)

    cheap, took, slow = _while_held(
        held,
        lambda: client.post(
            f"{base}/imports/recognise",
            files={"file": ("export.ofx", CARD_FILE.encode(), "application/x-ofx")},
            headers=HEADERS,
        ),
        lambda: client.get(f"{base}/identifiers", headers=HEADERS),
    )

    assert cheap.status_code == 200, cheap.text
    assert took < PROMPT, f"the GET waited {took:.1f}s behind a recogniser"
    assert slow.status_code == 200, slow.text
    assert slow.json()["account_id"] == ours["card"], (
        "and it still read the account number the file states"
    )


def test_a_cheap_get_answers_while_an_agent_receipt_is_encoded(world, monkeypatch):  # noqa: F811
    from app.services import receipts

    held = Held(receipts.prepare)
    monkeypatch.setattr(receipts, "prepare", held)
    client = world["client"]
    raw = _jpeg(3)

    cheap, took, slow = _while_held(
        held,
        lambda: client.post(
            f"{V1}/households/{world['house']['id']}/receipts/binary?filename=till.jpg",
            content=raw,
            headers={**_auth(world), "content-type": "image/jpeg"},
        ),
        lambda: client.get(f"{V1}/manifest", headers=_auth(world)),
    )

    assert cheap.status_code == 200, cheap.text
    assert took < PROMPT, f"the GET waited {took:.1f}s behind a receipt encode"
    assert slow.status_code == 201, slow.text
    assert [one["byte_size"] for one in _receipts(world)] == [len(raw)], (
        "and the held receipt was stored, whole"
    )


def test_the_cpu_pool_runs_two_at_a_time_and_queues_the_rest():
    """The pool *is* the semaphore: a burst waits rather than decodes at once."""
    lock = threading.Lock()
    running = 0
    peak = 0
    release = threading.Event()

    def work():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(HOLD)
        with lock:
            running -= 1
        return threading.current_thread().name

    names: list[str] = []
    callers = [
        threading.Thread(target=lambda: names.append(offload.run_cpu_sync(work)))
        for _ in range(offload.CPU_WORKERS + 3)
    ]
    for one in callers:
        one.start()
    deadline = time.monotonic() + HOLD
    while time.monotonic() < deadline:
        with lock:
            if running == offload.CPU_WORKERS:
                break
        time.sleep(0.01)
    time.sleep(0.2)  # long enough for a third to have started, if it could
    with lock:
        seen = peak
    release.set()
    for one in callers:
        one.join(HOLD)

    assert seen == offload.CPU_WORKERS, f"{seen} ran at once; the cap is {offload.CPU_WORKERS}"
    assert len(names) == offload.CPU_WORKERS + 3, "and every queued one still ran"
    assert all(name.startswith("spendtracker-cpu") for name in names)


@pytest.mark.parametrize("which", ["async", "sync"])
def test_an_error_in_the_pool_reaches_the_caller_as_itself(which):
    """A ValidationError raised in a parse must still be the route's 422."""
    import asyncio

    from app.errors import ValidationError

    def refuse():
        raise ValidationError("that file is not a statement")

    with pytest.raises(ValidationError, match="not a statement"):
        if which == "async":
            asyncio.run(offload.run_cpu(refuse))
        else:
            offload.run_cpu_sync(refuse)


# --------------------------------------------------------------------------- #
# Issue #233: the database half, too
# --------------------------------------------------------------------------- #
#
# #84 moved the parse and the encode off the loop and left what came after --
# staging, the account import, the receipt store, the recogniser's match -- on
# it. 3,000 statement lines of staging was two seconds of nobody else served.
# The same hold, one step later: the held function is the database work.


def test_a_cheap_get_answers_while_a_statement_is_being_staged(client, monkeypatch):
    from app.services import importing

    ours = _world(client)
    held = Held(importing.stage_parsed)
    monkeypatch.setattr(importing, "stage_parsed", held)

    cheap, took, slow = _while_held(
        held,
        lambda: _upload(client, ours["house"], ours["card"], CARD_FILE, name="oct.ofx"),
        lambda: client.get("/api/health"),
    )

    assert cheap.status_code == 200, cheap.text
    assert took < PROMPT, f"the health check waited {took:.1f}s behind somebody's staging"
    assert slow.status_code == 201, slow.text
    assert len(slow.json()["lines"]) == 3, "and the held upload still staged all three rows"


def test_a_cheap_get_answers_while_accounts_are_imported(client, monkeypatch):
    from app.services import account_import

    ours = _world(client)
    held = Held(account_import.import_accounts)
    monkeypatch.setattr(account_import, "import_accounts", held)
    header = ",".join(account_import.COLUMNS)
    raw = f"{header}\nHoliday pot,savings,EUR,,,10,2026-01-15,,\n".encode()

    cheap, took, slow = _while_held(
        held,
        lambda: client.post(
            f"/api/households/{ours['house']}/accounts/import",
            files={"file": ("accounts.csv", raw, "text/csv")},
            data={"dry_run": "false"},
            headers=HEADERS,
        ),
        lambda: client.get("/api/health"),
    )

    assert cheap.status_code == 200, cheap.text
    assert took < PROMPT, f"the health check waited {took:.1f}s behind an account import"
    assert slow.status_code == 201, slow.text
    accounts = client.get(f"/api/households/{ours['house']}/accounts", headers=HEADERS).json()
    assert "Holiday pot" in {one["name"] for one in accounts}, "and the account was made"


def test_a_cheap_get_answers_while_a_receipt_is_stored(client, monkeypatch):
    from app.services import receipts

    from .receipt_fixtures import with_exif

    ours = _world(client)
    held = Held(receipts.store)
    monkeypatch.setattr(receipts, "store", held)
    raw = with_exif()

    cheap, took, slow = _while_held(
        held,
        lambda: client.post(
            f"/api/households/{ours['house']}/receipts",
            files={"file": ("IMG_0042.jpeg", raw, "application/octet-stream")},
            headers=HEADERS,
        ),
        lambda: client.get("/api/health"),
    )

    assert cheap.status_code == 200, cheap.text
    assert took < PROMPT, f"the health check waited {took:.1f}s behind a receipt store"
    assert slow.status_code == 201, slow.text
    inbox = client.get(f"/api/households/{ours['house']}/receipts", headers=HEADERS).json()
    assert [one["id"] for one in inbox] == [slow.json()["receipt"]["id"]], (
        "and the held receipt landed in the inbox"
    )


def test_a_cheap_get_answers_while_a_statement_is_matched_to_an_account(client, monkeypatch):
    from app.services import identifiers

    ours = _world(client)
    base = f"/api/households/{ours['house']}"
    made = client.post(
        f"{base}/identifiers",
        json={"kind": "number", "value": "CARD|11223", "account_id": ours["card"]},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    held = Held(identifiers.recognise_file)
    monkeypatch.setattr(identifiers, "recognise_file", held)

    cheap, took, slow = _while_held(
        held,
        lambda: client.post(
            f"{base}/imports/recognise",
            files={"file": ("export.ofx", CARD_FILE.encode(), "application/x-ofx")},
            headers=HEADERS,
        ),
        lambda: client.get("/api/health"),
    )

    assert cheap.status_code == 200, cheap.text
    assert took < PROMPT, f"the health check waited {took:.1f}s behind a recogniser's match"
    assert slow.status_code == 200, slow.text
    assert slow.json()["account_id"] == ours["card"]
