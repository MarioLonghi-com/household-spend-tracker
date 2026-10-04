"""The owner's screen for account resets (#286).

Two owners (Jane, who walked the wizard, and Alex), two members (Sam and
Robin), two households -- Jane and Sam in one, Alex and Robin in the other.
Every account has a real password, a real sealed authenticator, two recovery
codes, a session and a trusted browser, so a reset aimed at one of them has
something to take away and the other three have something to keep.

What is asserted is what each route did to the rows: the reset account's
credentials and sessions, everybody else's untouched, the `account_resets`
row and who it names, and the audit log the notice is read from. A status
code alone proves nothing here.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pyotp
import pytest
from sqlalchemy import event, func, select

from app.audit.batch import batch
from app.auth import cookies, crypto, devices, passwords, sessions, tokens, totp
from app.models import (
    AccountReset,
    Batch,
    BatchKind,
    BatchStatus,
    Change,
    ChangeOp,
    Household,
    HouseholdMember,
    RecoveryCode,
    Role,
    TrustedDevice,
    User,
    WebSession,
    utcnow,
)
from app.services import account_resets as reset_service
from app.services import invitations as invite_service
from app.services import profile, sign_in_changes
from app.services import users as user_service
from tests.conftest import HEADERS, _setup_owner

OLD_PASSWORD = "the password before any reset"
NOBODY = "f" * 32


# --------------------------------------------------------------------------- #
# The world
# --------------------------------------------------------------------------- #


def _db():
    import app.db as db

    return db


def _person(session, *, email: str, name: str, role: Role) -> User:
    user = User(
        email=email,
        email_canonical=email,
        display_name=name,
        password_hash=passwords.hash_password(OLD_PASSWORD),
        role=role,
        totp_secret=b"",
    )
    session.add(user)
    session.flush()
    user.totp_secret = crypto.seal_totp_secret(totp.new_secret(), user_id=user.id)
    user.totp_last_counter = 1
    for code in ("aaaaa11111", "bbbbb22222"):
        session.add(RecoveryCode(user_id=user.id, code_hash=passwords.hash_password(code)))
    return user


@pytest.fixture()
def world(client) -> dict:
    """Four people, two households, a session cookie each."""
    jane = _setup_owner(client)["user"]["id"]
    jane_cookie = client.cookies.get(cookies.session_name())
    ids = {"jane": jane}
    cookie = {"jane": jane_cookie}

    with _db().session_scope() as session:
        with batch(session, kind=BatchKind.admin, actor_id=jane):
            people = {
                "alex": _person(session, email="alex@example.com", name="Alex", role=Role.owner),
                "sam": _person(session, email="sam@example.com", name="Sam", role=Role.member),
                "robin": _person(session, email="robin@example.com", name="Robin", role=Role.member),
            }
            ours = Household(name="Doe-Smith", base_currency="EUR")
            theirs = Household(name="Somebody Else", base_currency="GBP")
            session.add_all([ours, theirs])
            session.flush()
            for house, user_id in (
                (ours, jane),
                (ours, people["sam"].id),
                (theirs, people["alex"].id),
                (theirs, people["robin"].id),
            ):
                session.add(
                    HouseholdMember(
                        household_id=house.id, user_id=user_id, added_by_id=jane, added_at=utcnow()
                    )
                )
        for key, user in people.items():
            ids[key] = user.id
            cookie[key] = sessions.issue(session, user)
            devices.issue(session, user)
    return {"ids": ids, "cookie": cookie}


def _as(client, world, who: str):
    client.cookies.clear()
    client.cookies.set(cookies.session_name(), world["cookie"][who])
    return client


def _count(session, model, user_id: str) -> int:
    return session.execute(
        select(func.count()).select_from(model).where(model.user_id == user_id)
    ).scalar_one()


def _state(user_id: str) -> dict:
    with _db().session_scope() as session:
        user = session.get(User, user_id)
        return {
            "password_hash": user.password_hash,
            "totp_secret": user.totp_secret,
            "totp_last_counter": user.totp_last_counter,
            "role": user.role,
            "codes": _count(session, RecoveryCode, user_id),
            "sessions": _count(session, WebSession, user_id),
            "devices": _count(session, TrustedDevice, user_id),
        }


def _everyone(world) -> dict:
    return {who: _state(user_id) for who, user_id in world["ids"].items()}


def _resets() -> list[tuple]:
    with _db().session_scope() as session:
        return [
            (row.user_id, row.created_by_id, row.password, row.authenticator)
            for row in session.execute(select(AccountReset)).scalars()
        ]


def _issue(client, world, *, by: str, whom: str, password=True, authenticator=True):
    return _as(client, world, by).post(
        f"/api/admin/users/{world['ids'][whom]}/reset",
        json={"password": password, "authenticator": authenticator},
        headers=HEADERS,
    )


# --------------------------------------------------------------------------- #
# Issuing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("password", "authenticator"), [(True, True), (True, False), (False, True)])
def test_an_owner_resets_a_member_and_nobody_else(client, world, password, authenticator):
    before = _everyone(world)
    assert before["sam"]["sessions"] == before["sam"]["devices"] == 1

    answer = _issue(client, world, by="jane", whom="sam", password=password, authenticator=authenticator)
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["link"].startswith("https://testserver/reset/")
    assert (body["password"], body["authenticator"]) == (password, authenticator)

    after = _everyone(world)
    sam = after["sam"]
    # Everything the old credentials bought has ended, whatever was reset.
    assert (sam["sessions"], sam["devices"]) == (0, 0)
    if password:
        assert sam["password_hash"] != before["sam"]["password_hash"]
        assert not passwords.verify_password(sam["password_hash"], OLD_PASSWORD)
    else:
        assert sam["password_hash"] == before["sam"]["password_hash"]
    if authenticator:
        assert (sam["totp_secret"], sam["totp_last_counter"], sam["codes"]) == (None, None, 0)
    else:
        assert (sam["totp_secret"], sam["codes"]) == (before["sam"]["totp_secret"], 2)
    # The other member, the other owner and the owner who did it: not one column.
    for who in ("robin", "alex", "jane"):
        assert after[who] == before[who], who

    assert _resets() == [(world["ids"]["sam"], world["ids"]["jane"], password, authenticator)]
    token = body["link"].rsplit("/", 1)[-1]
    with _db().session_scope() as session:
        row = session.execute(select(AccountReset)).scalar_one()
        assert row.token_hash == tokens.fingerprint(token)
        assert reset_service.lookup(session, token).user_id == world["ids"]["sam"]
        insert = session.execute(
            select(Change).where(Change.table_name == "account_resets", Change.op == ChangeOp.insert)
        ).scalar_one()
        assert session.get(Batch, insert.batch_id).actor_id == world["ids"]["jane"]


def test_an_owner_resets_the_other_owner_with_no_step_up(client, world):
    before = _everyone(world)
    answer = _issue(client, world, by="jane", whom="alex")
    assert answer.status_code == 201, answer.text

    after = _everyone(world)
    assert (after["alex"]["sessions"], after["alex"]["devices"]) == (0, 0)
    assert after["alex"]["password_hash"] != before["alex"]["password_hash"]
    assert after["alex"]["totp_secret"] is None and after["alex"]["codes"] == 0
    # Still an owner: a reset changes how they sign in, not what they are.
    assert after["alex"]["role"] is Role.owner
    for who in ("sam", "robin", "jane"):
        assert after[who] == before[who], who
    assert _resets() == [(world["ids"]["alex"], world["ids"]["jane"], True, True)]


def test_resetting_yourself_is_refused_and_changes_nothing(client, world):
    before = _everyone(world)
    answer = _issue(client, world, by="jane", whom="jane")
    assert answer.status_code == 409
    assert "profile" in answer.json()["detail"]
    assert _everyone(world) == before
    assert _resets() == []


def test_a_disabled_account_is_refused_and_changes_nothing(client, world):
    with _db().session_scope() as session, batch(
        session, kind=BatchKind.admin, actor_id=world["ids"]["jane"]
    ):
        session.get(User, world["ids"]["robin"]).disabled_at = utcnow()
    before = _everyone(world)
    answer = _issue(client, world, by="jane", whom="robin")
    assert answer.status_code == 409 and "Re-enable" in answer.json()["detail"]
    assert _everyone(world) == before
    assert _resets() == []


def test_nothing_reset_is_refused(client, world):
    before = _everyone(world)
    answer = _issue(client, world, by="jane", whom="sam", password=False, authenticator=False)
    assert answer.status_code == 422 and "choose what to reset" in answer.json()["detail"]
    assert _everyone(world) == before
    assert _resets() == []


def test_unknown_ids_are_404_for_an_owner(client, world):
    _as(client, world, "jane")
    assert (
        client.post(
            f"/api/admin/users/{NOBODY}/reset", json={"password": True}, headers=HEADERS
        ).status_code
        == 404
    )
    assert client.delete(f"/api/admin/resets/{NOBODY}", headers=HEADERS).status_code == 404


def test_a_member_is_refused_every_route_and_nothing_changes(client, world):
    """403 from `require_owner`, as on every admin route: a member knows other
    accounts exist. The refusal comes before any id is looked up, so a real id
    and an invented one get the same answer -- nothing is confirmed."""
    assert _issue(client, world, by="jane", whom="alex").status_code == 201
    pending = _resets()
    before = _everyone(world)
    with _db().session_scope() as session:
        reset_id = session.execute(select(AccountReset.id)).scalar_one()

    _as(client, world, "sam")
    calls = [
        ("post", f"/api/admin/users/{world['ids']['robin']}/reset"),
        ("post", f"/api/admin/users/{NOBODY}/reset"),
        ("get", "/api/admin/resets"),
        ("delete", f"/api/admin/resets/{reset_id}"),
        ("delete", f"/api/admin/resets/{NOBODY}"),
        ("get", "/api/admin/sign-in-changes"),
    ]
    for method, path in calls:
        extra = {"json": {"password": True, "authenticator": True}} if method == "post" else {}
        answer = getattr(client, method)(path, headers=HEADERS, **extra)
        assert answer.status_code == 403, f"{method.upper()} {path} answered {answer.status_code}"

    assert _resets() == pending
    assert _everyone(world) == before


# --------------------------------------------------------------------------- #
# Pending, and withdrawing
# --------------------------------------------------------------------------- #


def test_withdrawing_deletes_the_row_audits_it_and_leaves_the_account_shut(client, world):
    assert _issue(client, world, by="jane", whom="sam").status_code == 201
    shut = _everyone(world)
    with _db().session_scope() as session:
        reset_id = session.execute(select(AccountReset.id)).scalar_one()

    _as(client, world, "alex")
    assert client.delete(f"/api/admin/resets/{reset_id}", headers=HEADERS).status_code == 204

    assert _resets() == []
    assert _everyone(world) == shut
    with _db().session_scope() as session:
        gone = session.execute(
            select(Change).where(Change.table_name == "account_resets", Change.op == ChangeOp.delete)
        ).scalar_one()
        assert gone.row_id == reset_id and gone.before["user_id"] == world["ids"]["sam"]
        assert session.get(Batch, gone.batch_id).actor_id == world["ids"]["alex"]
    # Twice is a 404, not a second delete.
    assert client.delete(f"/api/admin/resets/{reset_id}", headers=HEADERS).status_code == 404


def test_pending_names_who_issued_each_and_marks_the_lapsed_one(client, world):
    assert _issue(client, world, by="jane", whom="sam", authenticator=False).status_code == 201
    with _db().session_scope() as session:
        robin = session.get(User, world["ids"]["robin"])
        with batch(session, kind=BatchKind.admin, actor_id=robin.id, source={"via": "scripts.test"}):
            reset, _ = reset_service.issue(session, robin, password=False, authenticator=True, by=None)
        with batch(session, kind=BatchKind.admin, actor_id=robin.id):
            reset.expires_at = utcnow() - timedelta(minutes=1)

    listed = _as(client, world, "jane").get("/api/admin/resets", headers=HEADERS)
    assert listed.status_code == 200, listed.text
    by_name = {one["display_name"]: one for one in listed.json()}
    assert set(by_name) == {"Sam", "Robin"}
    sam, robin = by_name["Sam"], by_name["Robin"]
    assert (sam["email"], sam["issued_by"], sam["expired"]) == ("sam@example.com", "Jane", False)
    assert (sam["password"], sam["authenticator"]) == (True, False)
    assert (robin["issued_by"], robin["expired"]) == (None, True)
    assert (robin["password"], robin["authenticator"]) == (False, True)
    assert robin["user_id"] == world["ids"]["robin"]


# --------------------------------------------------------------------------- #
# The notice: read from the audit log
# --------------------------------------------------------------------------- #


def _backdate(batch_id: str, *, days: int) -> None:
    """`batches` and `changes` are not audited, so they can be moved directly."""
    with _db().session_scope() as session:
        row = session.get(Batch, batch_id)
        row.started_at = row.started_at - timedelta(days=days)
        for change in session.execute(select(Change).where(Change.batch_id == batch_id)).scalars():
            change.at = change.at - timedelta(days=days)


def test_the_notice_lists_resets_and_new_owners_and_who_did_each(client, world):
    ids = world["ids"]
    # Fifteen days ago: Jane reset Robin's password. Outside the window.
    with _db().session_scope() as session:
        with batch(session, kind=BatchKind.admin, actor_id=ids["jane"]) as old:
            reset_service.issue(
                session,
                session.get(User, ids["robin"]),
                password=True,
                authenticator=False,
                by=session.get(User, ids["jane"]),
            )
        old_batch = old.id
    _backdate(old_batch, days=15)

    # Today: Jane resets Sam from the screen. Then, from the server, in the
    # shape the commands write -- one admin batch whose actor is the account
    # acted on, marked `via: scripts.<command>` -- Robin is made an owner and
    # Alex is given a new authenticator.
    assert _issue(client, world, by="jane", whom="sam").status_code == 201
    with _db().session_scope() as session, batch(
        session,
        kind=BatchKind.admin,
        actor_id=ids["robin"],
        source={"via": "scripts.reset_account"},
    ):
        session.get(User, ids["robin"]).role = Role.owner
    with _db().session_scope() as session, batch(
        session,
        kind=BatchKind.admin,
        actor_id=ids["alex"],
        source={"via": "scripts.reset_authenticator"},
    ):
        secret = totp.new_secret()
        step = totp.code_matches(secret, pyotp.TOTP(secret).now())
        profile.reset_authenticator_from_the_server(
            session, session.get(User, ids["alex"]), secret=secret, step=step
        )

    # Read by Jane: every other account here has been signed out by now.
    answer = _as(client, world, "jane").get("/api/admin/sign-in-changes", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    items = answer.json()
    seen = [
        (one["what"], one["user_name"], one["by_name"], one["from_server"], one["password"], one["authenticator"])
        for one in items
    ]
    assert seen == [
        ("authenticator_replaced", "Alex", None, True, False, True),
        ("promoted", "Robin", None, True, False, False),
        ("reset", "Sam", "Jane", False, True, True),
        # The fixture made Alex an owner, and Jane walked the wizard.
        ("owner_added", "Alex", "Jane", False, False, False),
        ("owner_added", "Jane", "Jane", False, False, False),
    ]
    assert [one["id"] for one in items] == sorted((one["id"] for one in items), reverse=True)
    assert [one["by_id"] for one in items[:3]] == [None, None, ids["jane"]]
    assert [one["user_id"] for one in items[:3]] == [ids["alex"], ids["robin"], ids["sam"]]
    # Nothing stored a second time: the only reset row left is Sam's and
    # Robin's old one, both in `account_resets` where #284 put them.
    assert sorted(row[0] for row in _resets()) == sorted([ids["sam"], ids["robin"]])


def test_an_owner_invited_is_named_as_added_by_whoever_sent_the_link(session, owner, member):
    """The wizard's batch has the new owner as its actor; the invitation in the
    same batch names who chose the role. Built as `setup.complete` builds it."""
    from sqlalchemy import text

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invitation, _ = invite_service.create(session, invited_by=owner, role=Role.owner)
    newcomer = User(
        email="new@example.com",
        email_canonical="new@example.com",
        display_name="Newcomer",
        password_hash="argon2-placeholder",
        role=Role.owner,
        totp_secret=b"sealed-placeholder",
    )
    session.execute(text("PRAGMA defer_foreign_keys=ON"))
    with batch(session, kind=BatchKind.setup, actor_id=newcomer.id, own_transaction=False):
        session.add(newcomer)
        invite_service.accept(session, invitation, newcomer)
    session.commit()
    # And an owner promoting the member, then demoting them: one promotion.
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        user_service.set_role(session, user=member, role=Role.owner, by=owner)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        user_service.set_role(session, user=member, role=Role.member, by=owner)
    session.commit()

    seen = [(one.what, one.user_name, one.by_name, one.from_server) for one in sign_in_changes.recent(session)]
    assert seen == [
        ("promoted", "Partner", "Jane", False),
        ("owner_added", "Newcomer", "Jane", False),
        ("owner_added", "Jane", "Jane", False),
    ]
    # Fourteen days and a minute later, all of it has gone from the notice.
    later = utcnow() + sign_in_changes.WINDOW + timedelta(minutes=1)
    assert sign_in_changes.recent(session, now=later) == []


def test_re_enabling_is_news_only_when_the_server_does_it(session, owner, member):
    """An owner's re-enable is on a screen every owner sees; the server's,
    made the way #285's `--enable` makes it, is what the notice is for."""

    def as_jane(disabled: bool) -> None:
        with batch(session, kind=BatchKind.admin, actor_id=owner.id):
            user_service.set_disabled(session, user=member, disabled=disabled, by=owner)

    def from_the_server(**changes) -> None:
        with batch(
            session,
            kind=BatchKind.admin,
            actor_id=member.id,
            source={"via": "scripts.reset_account"},
        ):
            for name, value in changes.items():
                setattr(member, name, value)

    as_jane(True)
    as_jane(False)
    as_jane(True)
    from_the_server(disabled_at=None)
    as_jane(True)
    # One write that both promotes and re-enables: two items, one position.
    from_the_server(disabled_at=None, role=Role.owner)
    session.commit()

    items = sign_in_changes.recent(session)
    seen = [(one.what, one.user_name, one.by_id, one.from_server) for one in items]
    assert seen == [
        ("reenabled", "Partner", None, True),
        ("promoted", "Partner", None, True),
        ("reenabled", "Partner", None, True),
        ("owner_added", "Jane", owner.id, False),
    ]
    assert items[0].id == items[1].id > items[2].id
    assert len({one.key for one in items}) == len(items)


def test_a_link_from_the_server_is_listed_once_and_its_sweep_not_at_all(engine, session, owner, member):
    """The sweep of a lapsed link adds nothing to the notice, and the issue
    stays listed. A sweep only deletes, which the notice never reads anyway,
    so this alone cannot tell whether housekeeping is excluded: the test after
    it is the one that does."""
    from app.auth import housekeeping

    with batch(
        session, kind=BatchKind.admin, actor_id=member.id, source={"via": "scripts.reset_account"}
    ):
        reset_service.issue(session, member, password=True, authenticator=False, by=None)
    session.commit()
    before = sign_in_changes.recent(session)
    assert [(one.what, one.user_name, one.by_id, one.from_server, one.password) for one in before[:1]] == [
        ("reset", "Partner", None, True, True)
    ]

    later = utcnow() + reset_service.RETENTION + timedelta(days=4)
    assert housekeeping._sweep_account_resets(engine, now=later) == 1
    session.expire_all()
    assert session.execute(select(func.count()).select_from(AccountReset)).scalar_one() == 0
    swept = session.execute(select(Batch).where(Batch.source["housekeeping"].as_string() == "account_resets"))
    assert len(swept.scalars().all()) == 1
    assert sign_in_changes.recent(session) == before


def test_a_housekeeping_batch_says_nothing_whatever_it_writes(session, owner, member):
    """`source["housekeeping"]` is what keeps a sweep out of the notice.

    Every sweep today only deletes, and the notice reads only inserts and
    updates, so a test built on a real sweep passed with the exclusion
    removed. Here a housekeeping batch -- marked from the server too, so that
    without the exclusion every write in it would be read -- inserts a reset
    link, promotes one member and re-enables them. Nothing. Then the very same
    writes, unmarked, to the other member: three items, all theirs.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        robin = User(
            email="robin@example.com",
            email_canonical="robin@example.com",
            display_name="Robin",
            password_hash="argon2-placeholder",
            role=Role.member,
            totp_secret=b"sealed-placeholder",
            disabled_at=utcnow(),
        )
        session.add(robin)
        member.disabled_at = utcnow()
    session.commit()
    before = sign_in_changes.recent(session)
    assert [(one.what, one.user_name) for one in before] == [("owner_added", "Jane")]

    def write(user: User, source: dict) -> None:
        with batch(session, kind=BatchKind.admin, actor_id=user.id, source=source):
            reset_service.issue(session, user, password=True, authenticator=False, by=None)
            user.role = Role.owner
            user.disabled_at = None
        session.commit()

    write(member, {"housekeeping": "a sweep that writes", "via": "scripts.housekeeping"})
    assert session.get(User, member.id).role is Role.owner  # it did write
    assert sign_in_changes.recent(session) == before

    write(robin, {"via": "scripts.reset_account"})
    news = [one for one in sign_in_changes.recent(session) if one not in before]
    assert sorted((one.what, one.user_name, one.from_server) for one in news) == [
        ("promoted", "Robin", True),
        ("reenabled", "Robin", True),
        ("reset", "Robin", True),
    ]


def _plans_of_recent(engine, session) -> tuple[list, list[str]]:
    """What `recent()` answered, and the query plan of each of its reads of
    the log -- every SELECT that names `batches`."""
    seen: list[tuple[str, tuple]] = []

    def keep(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
        if not executemany and statement.lstrip().startswith("SELECT") and "batches" in statement:
            seen.append((statement, params))

    event.listen(engine, "before_cursor_execute", keep)
    try:
        items = sign_in_changes.recent(session)
    finally:
        event.remove(engine, "before_cursor_execute", keep)
    session.commit()
    plans = []
    with engine.connect() as conn:
        for statement, params in seen:
            rows = conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", params).all()
            plans.append("\n".join(str(row[-1]) for row in rows))
    return items, plans


def test_the_notice_reads_the_fortnight_not_the_whole_log(
    engine, session, owner, member, household, other_household
):
    """`recent()` runs on every owner's page load, and read every batch the
    ledger had ever written to find the fortnight's: `GROUP BY batches.id`
    walked `batches` whole (about 220 ms at 200k batches, to return nothing),
    and the row-image query, without statistics, led with the table name and
    looked at the date only after the join.

    Asserted as query plans, never wall-clock time, over a log whose history
    is in both households and in neither, from both people, and all outside
    the window -- once as a fresh database has it, once with the statistics
    housekeeping leaves.
    """
    from app.auth import housekeeping

    long_ago = utcnow() - timedelta(days=400)
    with engine.begin() as conn:
        conn.execute(
            Batch.__table__.insert(),
            [
                {
                    "id": f"{n:032x}",
                    "kind": BatchKind.manual,
                    "household_id": (household.id, other_household.id, None)[n % 3],
                    "actor_id": (owner.id, member.id)[n % 2],
                    "source": {"via": sign_in_changes.REPLACES_AUTHENTICATOR} if n % 50 == 0 else None,
                    "started_at": long_ago + timedelta(minutes=n),
                    "status": BatchStatus.applied,
                }
                for n in range(400)
            ],
        )
        # Old enough to have been news once: a promotion in every other one.
        conn.execute(
            Change.__table__.insert(),
            [
                {
                    "batch_id": f"{n:032x}",
                    "household_id": None,
                    "table_name": ("users", "transactions")[n % 2],
                    "row_id": member.id,
                    "op": ChangeOp.update,
                    "before": {"role": "member"},
                    "after": {"role": "owner"},
                    "at": long_ago + timedelta(minutes=n),
                }
                for n in range(400)
            ],
        )
    # And the fortnight: Jane resets Partner; the server gives Jane a new
    # authenticator.
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        reset_service.issue(session, member, password=True, authenticator=False, by=owner)
    with batch(
        session,
        kind=BatchKind.admin,
        actor_id=owner.id,
        source={"via": sign_in_changes.REPLACES_AUTHENTICATOR},
    ):
        # The secret itself is redacted from the log; its new codes are not.
        owner.totp_secret = b"another-sealed-placeholder"
        session.add(RecoveryCode(user_id=owner.id, code_hash="argon2-placeholder"))
    session.commit()
    expected = [("authenticator_replaced", "Jane"), ("reset", "Partner"), ("owner_added", "Jane")]

    for analysed in (False, True):
        if analysed:
            housekeeping.refresh_planner_statistics(engine)
        items, plans = _plans_of_recent(engine, session)
        assert [(one.what, one.user_name) for one in items] == expected, analysed
        assert len(plans) == 2, plans
        for plan in plans:
            assert "ix_batches_started_at" in plan, (analysed, plan)
            # Neither `batches` nor `changes` read whole, and no account's
            # history read back to the ledger's first day.
            assert "SCAN" not in plan, (analysed, plan)
            assert "ix_changes_row_history" not in plan, (analysed, plan)


def test_from_the_server_is_a_scripts_source_and_nothing_else():
    assert sign_in_changes.from_the_server(
        Batch(actor_id="u1", source={"via": "scripts.reset_authenticator"})
    )
    assert sign_in_changes.from_the_server(Batch(actor_id="u1", source={"via": "scripts.reset_account"}))
    # A One-time Import says `via` too, and is somebody signed in.
    assert not sign_in_changes.from_the_server(Batch(actor_id="u1", source={"via": "api"}))
    assert not sign_in_changes.from_the_server(Batch(actor_id="u1", source={"housekeeping": "invitations"}))
    assert not sign_in_changes.from_the_server(Batch(actor_id="u1", source=None))


@pytest.mark.repo_wide
def test_the_troubleshooting_page_offers_an_owners_reset_before_breaking_glass():
    """`deploy/TROUBLESHOOTING.md`, "in order of preference", went from a
    member with no authenticator and no recovery code straight to the
    operator printing their live secret, and a forgotten password was covered
    nowhere -- with this screen, which exposes nothing, named on no page.

    Pinned as order on the page, and as the words it tells an owner to click
    being the screen's own: if the doc and the code disagree, both are
    suspect.
    """
    root = Path(__file__).resolve().parent.parent
    page = (root / "deploy" / "TROUBLESHOOTING.md").read_text()
    start = page.index("## Getting back into an account")
    section = page[start : page.index("\n## ", start + 1)]
    glass = section.lower().find("break glass")
    assert glass > 0, "the break-glass route has gone from the section"
    before_glass = section[:glass]

    screen = (root / "client" / "src" / "screens" / "Admin.tsx").read_text()
    for label in ("People", "Reset sign-in…", "Pending reset links", "Recent sign-in changes"):
        assert f"**{label}**" in before_glass or f"→ {label}**" in before_glass, label
        assert label in screen, f"the page names {label!r}, which the Admin screen does not say"
    assert "forgotten password" in before_glass
