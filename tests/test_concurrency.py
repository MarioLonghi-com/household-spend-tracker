"""What happens when requests arrive together rather than one after another.

Every other test in the suite sends one request, waits, and sends the next.
The bugs here only exist when two things overlap: a pool that runs dry because
each request wants a second connection while holding its first, and a single
authenticator code that two requests both believe they were first to spend.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import HEADERS, PASSWORD, _setup_owner

#: More than the old pool's 5 + 10, and fewer than the 40 threadpool workers,
#: so every request really is in flight at once.
PARALLEL = 30


def test_a_burst_of_failed_sign_ins_never_runs_the_pool_dry(client):
    """Each failed sign-in holds the request's connection while `ratelimit.record`
    asks for a second one. With fifteen connections and forty workers, fifteen
    requests each held one and waited for another, and everything -- health
    checks included -- stalled for thirty seconds and then answered 500.
    """
    _setup_owner(client)
    client.cookies.clear()

    def guess(i: int) -> int:
        # A different invented address each time, so the per-account lockout
        # does not answer early with a 429 and every request takes the path
        # that records the failure on its own session.
        return client.post(
            "/api/session",
            json={"email": f"nobody{i}@example.com", "password": f"wrong guess {i}"},
            headers=HEADERS,
        ).status_code

    def health(_: int) -> int:
        return client.get("/api/health").status_code

    with ThreadPoolExecutor(PARALLEL + 5) as pool:
        guesses = [pool.submit(guess, i) for i in range(PARALLEL)]
        checks = [pool.submit(health, i) for i in range(5)]
        codes = [one.result(timeout=60) for one in guesses]
        healthy = [one.result(timeout=60) for one in checks]

    assert set(codes) <= {401, 429}, f"a failed sign-in answered {sorted(set(codes))}"
    assert codes.count(401) >= 1
    assert healthy == [200] * 5, "the health check stalled behind the sign-ins"


def test_one_code_is_spent_once_by_two_requests_that_both_loaded_the_user(client, clock):
    """Two requests carrying the same code both read the user before either
    wrote. A comparison in Python told both of them the step was unused, so
    one code bought two acts -- a sign-in and a step-up, say, which is exactly
    the "one code, one use" guarantee `stepup.py` leans on.

    Two sessions stand in for the two requests: each holds its own copy of
    the user with the counter as it was before either spent anything.
    """
    import pyotp
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.auth import totp
    from app.models import Change, User

    world = _setup_owner(client)
    engine = client.app_module.db_engine
    user_id = world["user"]["id"]
    code = pyotp.TOTP(world["secret"]).at(clock())

    with (
        Session(engine, expire_on_commit=False) as first,
        Session(engine, expire_on_commit=False) as second,
    ):
        mine, theirs = first.get(User, user_id), second.get(User, user_id)
        assert mine.totp_last_counter == theirs.totp_last_counter, "both start from the same view"
        changes_before = first.execute(
            select(func.count()).select_from(Change).where(Change.table_name == "users")
        ).scalar_one()

        answers = [totp.verify_and_consume(mine, code), totp.verify_and_consume(theirs, code)]
        # Neither session has a batch open, and neither needs one to finish.
        first.commit()
        second.commit()

    assert answers.count(True) == 1, f"one code was accepted {answers.count(True)} times"

    with Session(engine) as fresh:
        stored = fresh.get(User, user_id).totp_last_counter
        changes_after = fresh.execute(
            select(func.count()).select_from(Change).where(Change.table_name == "users")
        ).scalar_one()
    assert stored == int(time.time()) // totp.STEP_SECONDS, "the step was not burned"
    # The counter is redacted from the log; burning it must not start writing it there.
    assert changes_after == changes_before


def test_a_burst_of_guesses_at_one_account_gets_no_more_than_the_budget(client):
    """`check` read the failure count and `record` wrote the new failure a whole
    argon2 hash later, so every guess that arrived inside that gap read the
    same count: seven concurrent wrong passwords against a budget of five were
    all evaluated. Reserving the attempt before judging it means the k-th
    committed guess sees at least k failures, so the budget holds under a burst.
    """
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.auth import ratelimit
    from app.models import LoginAttempt

    world = _setup_owner(client)
    client.cookies.clear()
    email = world["user"]["email"]

    def guess(i: int) -> int:
        return client.post(
            "/api/session",
            json={"email": email, "password": f"wrong guess {i}"},
            headers=HEADERS,
        ).status_code

    burst = 3 * ratelimit.FREE_ATTEMPTS
    with ThreadPoolExecutor(burst) as pool:
        codes = list(pool.map(guess, range(burst)))

    assert set(codes) <= {401, 429}, f"a guess answered {sorted(set(codes))}"
    evaluated = codes.count(401)
    assert 1 <= evaluated <= ratelimit.FREE_ATTEMPTS, (
        f"{evaluated} guesses were judged against a budget of {ratelimit.FREE_ATTEMPTS}"
    )

    with Session(client.app_module.db_engine) as fresh:
        failures = fresh.execute(
            select(func.count()).select_from(LoginAttempt).where(LoginAttempt.ok.is_(False))
        ).scalar_one()
    # Every judged guess left its failure behind; no refused one did.
    assert failures == evaluated

    # And the right password now answers exactly as the stored failures say it
    # should. Usually the burst spent the whole budget and it is refused for the
    # window. But two guesses that race at the boundary can both be refused (the
    # safe direction), leaving fewer than the budget judged -- and then the
    # right password must still get in, or a burst of refused guesses would be
    # a lockout with no failures behind it.
    right = client.post(
        "/api/session", json={"email": email, "password": PASSWORD}, headers=HEADERS
    ).status_code
    assert right == (429 if evaluated >= ratelimit.FREE_ATTEMPTS else 200), (
        f"{evaluated} failures stored, and the right password answered {right}"
    )


def test_a_right_answer_does_not_spend_the_budget(client):
    """The reservation is released when the attempt turns out to be right, so
    a person who signs in correctly five times is not locked out on the sixth."""
    from app.auth import ratelimit

    world = _setup_owner(client)
    email = world["user"]["email"]
    for _ in range(ratelimit.FREE_ATTEMPTS + 2):
        client.cookies.clear()
        answer = client.post(
            "/api/session", json={"email": email, "password": PASSWORD}, headers=HEADERS
        )
        assert answer.status_code == 200, answer.text


# --------------------------------------------------------------------------- #
# Single-use tokens, spent by two requests at once (#206)
# --------------------------------------------------------------------------- #


def _race(engine, table: str, claim) -> list:
    """Run ``claim`` on two threads, held at a barrier until both are about to
    run their first statement against ``table``.

    That is the interleaving the read-then-delete lost: pysqlite begins a
    transaction only before DML, so both reads saw the row and both deletes
    "succeeded". With one DELETE ... RETURNING, the second statement waits on
    SQLite's write lock and then finds nothing.
    """
    import threading

    from sqlalchemy import event

    gate = threading.Barrier(2, timeout=10)
    local = threading.local()

    def _hold(conn, cursor, statement, params, context, executemany):
        if table in statement.lower() and not getattr(local, "passed", False):
            local.passed = True
            gate.wait()

    answers: list = [None, None]

    def run(slot: int) -> None:
        answers[slot] = claim()

    event.listen(engine, "before_cursor_execute", _hold)
    try:
        threads = [threading.Thread(target=run, args=(slot,)) for slot in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        event.remove(engine, "before_cursor_execute", _hold)
    return answers


def test_a_pending_sign_in_claimed_twice_at_once_is_spent_once(client):
    """One stolen `st_pending`, two `POST /session/code` at the same moment:
    it used to buy two code guesses."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.auth import sessions
    from app.models import PendingSignIn, User

    world = _setup_owner(client)
    engine = client.app_module.db_engine
    with Session(engine) as own:
        value = sessions.issue_pending(own, own.get(User, world["user"]["id"]), seconds=300)
        own.commit()

    answers = _race(engine, "pending_sign_ins", lambda: sessions.claim_pending(engine, value))

    assert sorted(answers, key=bool) == [None, world["user"]["id"]], answers
    with Session(engine) as own:
        assert own.execute(select(func.count()).select_from(PendingSignIn)).scalar_one() == 0


def test_a_step_up_grant_claimed_twice_at_once_buys_one_act(client, clock):
    """One grant, two `POST /me/keys` at the same moment: it used to mint two."""
    import pyotp
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.auth import stepup
    from app.models import StepUpGrant

    world = _setup_owner(client)
    engine = client.app_module.db_engine
    answer = client.post(
        "/api/me/step-up",
        json={"password": PASSWORD, "code": pyotp.TOTP(world["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    token = answer.json()["token"]

    answers = _race(
        engine,
        "step_up_grants",
        lambda: stepup.claim(engine, token, user_id=world["user"]["id"]),
    )

    assert sorted(answers) == [False, True], answers
    with Session(engine) as own:
        assert own.execute(select(func.count()).select_from(StepUpGrant)).scalar_one() == 0
