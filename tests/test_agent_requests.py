"""The record of what a key asked for, and the guard against a loop.

The failure this file exists to catch is the one `ratelimit` already had once:
a limiter whose attempts never reached the database the request reads, so no
test through HTTP could ever observe it working. Every test here therefore
asserts a **row**, or asserts a refusal that could only arrive if the rows
landed.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AgentRequest, utcnow
from app.services import agent_requests as service
from tests.test_agent_manifest import MANIFEST, keyed  # noqa: F401 -- a fixture


def _lines(client) -> list[AgentRequest]:
    with Session(client.app_module.db_engine) as own:
        return list(own.execute(select(AgentRequest).order_by(AgentRequest.at)).scalars())


def _get(client, token: str):
    return client.get(MANIFEST, headers={"authorization": f"Bearer {token}"})


# --------------------------------------------------------------------------- #
# The log
# --------------------------------------------------------------------------- #


def test_a_served_request_leaves_a_line(client, keyed):  # noqa: F811
    answer = _get(client, keyed["token"])
    assert answer.status_code == 200

    lines = _lines(client)
    assert len(lines) == 1
    assert lines[0].method == "GET"
    assert lines[0].status == 200
    assert lines[0].agent_key_id is not None


def test_the_line_holds_the_route_template_and_not_the_url(client, keyed):  # noqa: F811
    """A log swept on a window should not be accumulating ids."""
    _get(client, keyed["token"])
    route = _lines(client)[0].route

    assert route.endswith("/manifest")
    assert keyed["household"]["id"] not in route
    # And it is the path somebody could actually call. A router carries the
    # paths it was written with, not the ones it is mounted at, so this read
    # `/agent/v1/manifest` -- a template for a URL that does not exist.
    assert route == MANIFEST


def test_a_refused_key_leaves_no_line_to_attribute(client, keyed):  # noqa: F811
    """There is no key to attribute it to, so there is nothing to write.

    Guessing at the door is `login_attempts`' job, and a bad token carries
    nothing that would identify a row here.
    """
    refused = client.get(MANIFEST, headers={"authorization": "Bearer stk_invented"})
    assert refused.status_code == 401
    assert _lines(client) == []


def test_a_key_refused_by_the_csrf_check_still_leaves_a_line(client, keyed):  # noqa: F811
    """The case the recording middleware sits outside the CSRF check *for*.

    It sat there and dropped it anyway: recording was gated on
    `request.state.agent_key_id`, which only `deps.current_agent` assigns, and
    a CSRF refusal returns before any dependency runs. A key being driven from
    a browser context it has no business being in is the most interesting thing
    this table can report, and it was the one thing it did not.
    """
    from app.auth import cookies

    # A browser-shaped write: the bearer token, a session cookie beside it, and
    # a foreign Origin. Exactly what `refuse_cross_origin_writes` is for.
    client.cookies.set(cookies.session_name(), "whatever")
    refused = client.post(
        f"{MANIFEST.rsplit('/', 1)[0]}/households/x/summary",
        headers={
            "authorization": f"Bearer {keyed['token']}",
            "origin": "https://evil.example",
        },
    )
    assert refused.status_code == 403, refused.text

    lines = _lines(client)
    assert len(lines) == 1
    assert lines[0].status == 403
    assert lines[0].method == "POST"
    assert lines[0].agent_key_id is not None, "and it is attributed to the key that did it"


def test_reads_are_visible_although_the_audit_log_cannot_see_them(client, keyed):  # noqa: F811
    """The gap this table exists to close.

    Three reads write nothing to `changes` -- they change nothing -- so without
    this table "what did that key do" has no answer at all.
    """
    from app.models import Change

    def audit_rows() -> int:
        with Session(client.app_module.db_engine) as own:
            return own.execute(select(func.count()).select_from(Change)).scalar_one()

    # Measured around the reads, not against zero: setting the household up
    # wrote plenty of audit rows, and it should have.
    before = audit_rows()
    for _ in range(3):
        _get(client, keyed["token"])

    assert len(_lines(client)) == 3
    assert audit_rows() == before, "reads write no audit rows, which is exactly the point"


# --------------------------------------------------------------------------- #
# The limit
# --------------------------------------------------------------------------- #


def test_a_key_over_its_hourly_ceiling_is_refused_with_a_retry_after(client, keyed):  # noqa: F811
    """The whole limiter, end to end, through HTTP.

    The lines are planted rather than made by six hundred real requests, which
    would take minutes -- but they are planted in the app's own database, so a
    limiter reading somewhere else still fails this.
    """
    key_id = _lines(client)[0].agent_key_id if _lines(client) else None
    if key_id is None:
        _get(client, keyed["token"])
        key_id = _lines(client)[0].agent_key_id

    now = utcnow()
    with Session(client.app_module.db_engine) as own:
        for _ in range(service.PER_HOUR):
            own.add(
                AgentRequest(
                    agent_key_id=key_id, at=now - timedelta(minutes=30),
                    method="GET", route="/api/agent/v1/manifest", status=200,
                )
            )
        own.commit()

    refused = _get(client, keyed["token"])
    assert refused.status_code == 429, refused.text
    assert int(refused.headers["Retry-After"]) > 0
    assert "limit" in refused.json()["detail"]


def test_hammering_a_closed_door_does_not_keep_it_closed(client, keyed, monkeypatch):  # noqa: F811
    """A refusal is logged and never counted, or the window can never drain.

    Refusals used to be invisible here for an incidental reason: a 429 never
    reached the handler, so nothing wrote a line for it. Now that one is
    attributed, the exclusion is what stops the limit amplifying itself -- a
    runaway agent, which is the failure this limit exists to catch, would
    otherwise keep its own lockout alive for as long as it kept retrying.
    """
    monkeypatch.setattr(service, "PER_HOUR", 3)

    for _ in range(3):
        assert _get(client, keyed["token"]).status_code == 200
    for _ in range(4):
        assert _get(client, keyed["token"]).status_code == 429

    lines = _lines(client)
    assert [line.status for line in lines] == [200, 200, 200, 429, 429, 429, 429], \
        "every one of them is on the record"

    with Session(client.app_module.db_engine) as own:
        key_id = lines[0].agent_key_id
        assert service.used_in_window(own, key_id) == 3, \
            "and only the three that did work are counted against the ceiling"


def test_requests_outside_the_window_do_not_count(client, keyed):  # noqa: F811
    """A ceiling that counted forever would lock a key out permanently."""
    _get(client, keyed["token"])
    key_id = _lines(client)[0].agent_key_id

    long_ago = utcnow() - service.WINDOW - timedelta(minutes=5)
    with Session(client.app_module.db_engine) as own:
        for _ in range(service.PER_HOUR * 2):
            own.add(
                AgentRequest(
                    agent_key_id=key_id, at=long_ago, method="GET",
                    route="/api/agent/v1/manifest", status=200,
                )
            )
        own.commit()

    assert _get(client, keyed["token"]).status_code == 200


def test_one_key_cannot_spend_anothers_budget(client, keyed, session):  # noqa: F811
    """Counted per key, so a busy importer does not lock out the analyst."""
    _get(client, keyed["token"])

    now = utcnow()
    with Session(client.app_module.db_engine) as own:
        for _ in range(service.PER_HOUR):
            own.add(
                AgentRequest(
                    agent_key_id="somebody-elses-key", at=now, method="GET",
                    route="/api/agent/v1/manifest", status=200,
                )
            )
        own.commit()

    assert _get(client, keyed["token"]).status_code == 200


def test_the_refusal_is_a_lockout_and_never_a_sleep(client, keyed):  # noqa: F811
    """A sleep ties up a worker per caller, which is the attack the limit
    exists to prevent. So the refusal must come back fast."""
    import time as clock_module

    _get(client, keyed["token"])
    key_id = _lines(client)[0].agent_key_id
    now = utcnow()
    with Session(client.app_module.db_engine) as own:
        for _ in range(service.PER_HOUR):
            own.add(
                AgentRequest(
                    agent_key_id=key_id, at=now, method="GET",
                    route="/api/agent/v1/manifest", status=200,
                )
            )
        own.commit()

    started = clock_module.monotonic()
    refused = _get(client, keyed["token"])
    assert refused.status_code == 429
    assert clock_module.monotonic() - started < 1.0, "the refusal must not sleep"


# --------------------------------------------------------------------------- #
# Sweeping
# --------------------------------------------------------------------------- #


def test_lines_past_the_retention_window_are_swept(client, keyed):  # noqa: F811
    from app.auth import housekeeping

    _get(client, keyed["token"])
    with Session(client.app_module.db_engine) as own:
        own.add(
            AgentRequest(
                agent_key_id="old-key", at=utcnow() - service.RETENTION - timedelta(days=1),
                method="GET", route="/api/agent/v1/manifest", status=200,
            )
        )
        own.commit()
    assert len(_lines(client)) == 2

    removed = housekeeping.sweep(client.app_module.db_engine)

    assert removed["agent_requests"] == 1
    assert len(_lines(client)) == 1


def test_a_swept_key_leaves_its_lines_behind(client, keyed):  # noqa: F811
    """`agent_key_id` is not a foreign key, deliberately.

    A key is swept thirty days after revocation; the record of what it did
    outlives it, because that is the moment somebody most wants to read it.
    """
    _get(client, keyed["token"])
    line = _lines(client)[0]

    # Through the ORM inside a batch, because `agent_keys` is audited and the
    # guard refuses the shortcut -- as it should, in a test as much as anywhere.
    from app.audit.batch import batch
    from app.audit.guard import AuditedSession
    from app.models import AgentKey, BatchKind

    with AuditedSession(bind=client.app_module.db_engine, expire_on_commit=False) as own:
        key = own.get(AgentKey, line.agent_key_id)
        with batch(
            own, kind=BatchKind.admin, actor_id=key.user_id, household_id=key.household_id
        ):
            own.delete(key)
        own.commit()

    survivors = _lines(client)
    assert len(survivors) == 1
    assert survivors[0].agent_key_id == line.agent_key_id, "the id is kept as a plain string"
