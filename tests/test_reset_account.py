"""The server command that resets an account, or makes an owner (#285).

`scripts.reset_account`, driven through its `main` against a real ledger. Two
members (the wizard's owner and a partner with an authenticator and recovery
codes of their own), two households with one of them in each -- so every
test can show that what was aimed at one account, or one household, left the
other exactly as it was. Every assertion reads the rows back; an exit code is
only ever checked beside what it did to them.
"""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from tests.test_operator_scripts import PARTNER, _rows, _two_members

BASE = "https://ledger.example.com"
NEW_OWNER = "New.Owner@example.com"
COUNTED = ("users", "account_resets", "household_members", "recovery_codes", "batches", "changes")


@pytest.fixture()
def world(client) -> dict:
    import app.db as db
    from app.audit.batch import batch
    from app.models import BatchKind, Household, HouseholdMember, utcnow

    world = _two_members(client)
    owner_id, partner_id = world["owner_id"], world["partner_id"]
    with db.session_scope() as session:
        with batch(session, kind=BatchKind.admin, actor_id=owner_id):
            ours = Household(name="Doe-Smith", base_currency="EUR")
            theirs = Household(name="Somebody Else", base_currency="GBP")
            session.add_all([ours, theirs])
            session.flush()
            for house, user_id in ((ours, owner_id), (theirs, partner_id)):
                session.add(
                    HouseholdMember(
                        household_id=house.id, user_id=user_id, added_by_id=owner_id, added_at=utcnow()
                    )
                )
        ids = {"ours": ours.id, "theirs": theirs.id}
    return {**world, **ids}


@pytest.fixture()
def public_url(monkeypatch):
    """Set or clear `SPENDTRACKER_PUBLIC_URL` as the app read it."""
    from app import config

    def to(value: str) -> None:
        monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, public_url=value))

    to("")
    return to


def _counts(client) -> dict:
    return {table: _rows(client, f"SELECT count(*) FROM {table}")[0][0] for table in COUNTED}


def _account(client, user_id: str) -> tuple:
    (row,) = _rows(
        client,
        "SELECT email, display_name, password_hash, totp_secret, totp_last_counter, role, disabled_at "
        "FROM users WHERE id = ?",
        user_id,
    )
    return row


def _doors(client, user_id: str) -> tuple:
    """What an account can still get in with besides its password and secret."""
    return tuple(
        _rows(client, f"SELECT count(*) FROM {table} WHERE user_id = ?", user_id)[0][0]
        for table in ("sessions", "recovery_codes")
    )


def _memberships(client) -> set[tuple]:
    return set(_rows(client, "SELECT household_id, user_id, added_by_id FROM household_members"))


def _token(out: str) -> str:
    found = re.findall(rf"{re.escape(BASE)}/reset/([A-Za-z0-9_-]+)", out)
    assert len(found) == 1, out
    return found[0]


def _resolves_to(token: str) -> str:
    import app.db as db
    from app.services import account_resets

    with db.session_scope() as session:
        return account_resets.lookup(session, token).user_id


def _the_new_batch(client, before: set) -> tuple:
    rows = [
        row for row in _rows(client, "SELECT id, kind, actor_id, source, status FROM batches")
        if row[0] not in before
    ]
    assert len(rows) == 1, rows
    return rows[0]


def _batches(client) -> set:
    return {row[0] for row in _rows(client, "SELECT id FROM batches")}


def _disable(world: dict, *user_ids: str) -> None:
    """Disable accounts through the service *Admin* uses, so their sessions end too."""
    import app.db as db
    from app.audit.batch import batch
    from app.models import BatchKind, User
    from app.services import users

    with db.session_scope() as session, batch(session, kind=BatchKind.admin, actor_id=world["owner_id"]):
        for user_id in user_ids:
            users.set_disabled(session, user=session.get(User, user_id), disabled=True, by=None)


def _main(*argv: str, ask=None) -> int:
    from scripts import reset_account

    if ask is None:
        return reset_account.main([*argv, "--yes"])
    return reset_account.main(list(argv), ask=ask)


# --------------------------------------------------------------------------- #
# A reset link for an account that exists
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "switches", [("--password",), ("--authenticator",), ("--password", "--authenticator")]
)
def test_a_link_for_a_member_shuts_that_account_alone_and_names_nobody(
    client, world, public_url, capsys, switches
):
    from app.services import account_resets

    public_url(BASE)  # no --base-url: the configured address is the default
    partner, owner = world["partner_id"], world["owner_id"]
    partner_before, owner_before = _account(client, partner), _account(client, owner)
    owner_doors = _doors(client, owner)
    assert _doors(client, partner) == (1, 2)
    memberships, batches = _memberships(client), _batches(client)

    assert _main(PARTNER, *switches) == 0
    out = capsys.readouterr().out

    password, authenticator = "--password" in switches, "--authenticator" in switches
    ((user_id, created_by, reset_password, reset_authenticator),) = _rows(
        client, "SELECT user_id, created_by_id, password, authenticator FROM account_resets"
    )
    assert (user_id, created_by) == (partner, None)
    assert (bool(reset_password), bool(reset_authenticator)) == (password, authenticator)

    after = _account(client, partner)
    assert (after[2] != partner_before[2]) is password
    if authenticator:
        assert (after[3], after[4]) == (None, None)
    else:
        assert (after[3], after[4]) == partner_before[3:5] and after[3] is not None
    assert after[5] == "member", "a reset is not a promotion"
    assert _doors(client, partner) == (0, 0 if authenticator else 2)

    # The other account, and who is in which household: not one thing moved.
    assert _account(client, owner) == owner_before
    assert _doors(client, owner) == owner_doors
    assert _memberships(client) == memberships

    batch_id, kind, actor, source, status = _the_new_batch(client, batches)
    assert (kind, actor, json.loads(source), status) == (
        "admin", partner, {"via": "scripts.reset_account"}, "applied"
    )
    assert ("account_resets", "insert") in _rows(
        client, "SELECT table_name, op FROM changes WHERE batch_id = ?", batch_id
    )

    assert _resolves_to(_token(out)) == partner
    assert f"{account_resets.VALID_HOURS} hours" in out
    assert "in the audit log as done from the server" in out and "other owners" not in out


# --------------------------------------------------------------------------- #
# A new owner, and a promoted one
# --------------------------------------------------------------------------- #


def test_a_new_owner_has_no_way_in_but_the_link_and_only_the_household_named(
    client, world, public_url, capsys
):

    people = {world["owner_id"]: _account(client, world["owner_id"]),
              world["partner_id"]: _account(client, world["partner_id"])}
    memberships, before, batches = _memberships(client), _counts(client), _batches(client)

    assert _main(
        NEW_OWNER, "--make-owner", "--name", " New Owner ", "--household", "doe-smith",
        "--base-url", BASE + "/",
    ) == 0
    out = capsys.readouterr().out

    ((new_id, canonical),) = _rows(
        client, "SELECT id, email_canonical FROM users WHERE id NOT IN (?, ?)", *people
    )
    email, name, password_hash, secret, counter, role, disabled = _account(client, new_id)
    assert (email, canonical, name, role) == (NEW_OWNER, "new.owner@example.com", "New Owner", "owner")
    assert (secret, counter, disabled) == (None, None, None)
    assert password_hash is None, "no password until the link sets one, as a reset leaves it"
    assert _doors(client, new_id) == (0, 0)

    assert _rows(client, "SELECT user_id, created_by_id, password, authenticator FROM account_resets") == [
        (new_id, None, 1, 1)
    ]
    # In the household named, added by nobody; not in the other one.
    assert _memberships(client) == memberships | {(world["ours"], new_id, None)}

    assert {user_id: _account(client, user_id) for user_id in people} == people
    after = _counts(client)
    assert {table: after[table] - before[table] for table in COUNTED if table != "changes"} == {
        "users": 1, "account_resets": 1, "household_members": 1, "recovery_codes": 0, "batches": 1,
    }

    # One batch, its actor the account it made, and the insert the owners'
    # notice reads: a users row whose after-image says owner.
    batch_id, kind, actor, source, _ = _the_new_batch(client, batches)
    assert (kind, actor, json.loads(source)) == ("admin", new_id, {"via": "scripts.reset_account"})
    inserted = _rows(
        client, "SELECT after FROM changes WHERE batch_id = ? AND table_name = 'users' AND op = 'insert'",
        batch_id,
    )
    assert [json.loads(row[0])["role"] for row in inserted] == ["owner"]

    assert _resolves_to(_token(out)) == new_id


def test_promoting_a_member_changes_their_role_and_nothing_else(client, world, public_url, capsys):
    partner = world["partner_id"]
    was = _account(client, partner)
    doors, memberships, before = _doors(client, partner), _memberships(client), _counts(client)
    batches = _batches(client)

    # No switch, so no link, so no address needed: public_url is cleared.
    assert _main(PARTNER, "--make-owner") == 0
    out = capsys.readouterr().out

    assert _account(client, partner) == (*was[:5], "owner", was[6])
    assert _doors(client, partner) == doors
    assert _memberships(client) == memberships
    after = _counts(client)
    assert (after["account_resets"], after["users"]) == (before["account_resets"], before["users"])
    batch_id, _, actor, _, _ = _the_new_batch(client, batches)
    assert actor == partner
    assert _rows(client, "SELECT table_name, op FROM changes WHERE batch_id = ?", batch_id) == [
        ("users", "update")
    ]
    assert "/reset/" not in out

    # Again: already an owner, so nothing at all -- not even an empty batch.
    assert _main(PARTNER, "--make-owner") == 0
    assert _counts(client) == after
    assert "already an owner" in capsys.readouterr().out


def test_households_by_id_or_name_and_one_already_joined_is_left_alone(client, world, capsys):
    partner = world["partner_id"]
    memberships = _memberships(client)

    assert _main(
        PARTNER, "--make-owner", "--household", world["ours"], "--household", "Somebody Else"
    ) == 0

    assert _memberships(client) == memberships | {(world["ours"], partner, None)}
    assert "already in Somebody Else" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# A disabled account
# --------------------------------------------------------------------------- #


def test_enable_alone_brings_back_one_disabled_account_and_leaves_the_other_disabled(
    client, world, public_url, capsys
):
    partner, owner = world["partner_id"], world["owner_id"]
    _disable(world, owner, partner)
    partner_was, owner_was = _account(client, partner), _account(client, owner)
    assert partner_was[6] is not None and owner_was[6] is not None
    doors = (_doors(client, owner), _doors(client, partner))
    memberships, before, batches = _memberships(client), _counts(client), _batches(client)

    # No link, so no address needed: public_url is cleared.
    assert _main(PARTNER, "--enable") == 0
    out = capsys.readouterr().out

    # Enabled, and nothing else about them moved: no credential, no role.
    assert _account(client, partner) == (*partner_was[:6], None)
    assert _account(client, owner) == owner_was, "the other disabled account stays disabled"
    assert (_doors(client, owner), _doors(client, partner)) == doors
    assert _memberships(client) == memberships
    after = _counts(client)
    assert {table: after[table] - before[table] for table in COUNTED if table != "changes"} == {
        "users": 0, "account_resets": 0, "household_members": 0, "recovery_codes": 0, "batches": 1,
    }

    batch_id, kind, actor, source, status = _the_new_batch(client, batches)
    assert (kind, actor, json.loads(source), status) == (
        "admin", partner, {"via": "scripts.reset_account"}, "applied"
    )
    ((table, op, row_id, was, now),) = _rows(
        client, "SELECT table_name, op, row_id, before, after FROM changes WHERE batch_id = ?", batch_id
    )
    assert (table, op, row_id) == ("users", "update", partner)
    assert json.loads(was)["disabled_at"] is not None and json.loads(now)["disabled_at"] is None

    # Their credentials are left alone, and both are still there to sign in with.
    assert "enable them; their password and authenticator are left as they are" in out
    assert "re-enabled" in out and "sign them in, as before" in out and "cannot be seen" not in out
    assert "/reset/" not in out


def test_a_run_is_in_the_audit_log_as_from_the_server_and_in_no_households_history(
    client, world, capsys
):
    """Where somebody checking a run should look: the batch and its change
    row, not *History*. The batch has no household, and History lists only a
    household's own batches, so it never shows there."""
    partner, ours = world["partner_id"], world["ours"]
    _disable(world, partner)

    def history() -> list[str]:
        got = client.get(f"/api/households/{ours}/batches", params={"include_single_edits": "true"})
        assert got.status_code == 200, got.text
        return [row["id"] for row in got.json()]

    shown, batches = history(), _batches(client)
    assert _main(PARTNER, "--enable") == 0
    out = capsys.readouterr().out

    batch_id, kind, actor, source, status = _the_new_batch(client, batches)
    assert (kind, actor, json.loads(source), status) == (
        "admin", partner, {"via": "scripts.reset_account"}, "applied"
    )
    assert _rows(client, "SELECT household_id FROM batches WHERE id = ?", batch_id) == [(None,)]
    ((table, row_id, household, was, now),) = _rows(
        client, "SELECT table_name, row_id, household_id, before, after FROM changes WHERE batch_id = ?",
        batch_id,
    )
    assert (table, row_id, household) == ("users", partner, None)
    assert json.loads(was)["disabled_at"] is not None and json.loads(now)["disabled_at"] is None

    assert history() == shown and batch_id not in shown
    assert "in the audit log as done from the server" in out and "other owners" not in out


def test_enable_with_a_link_reopens_a_disabled_owner_in_one_batch_and_the_link_opens(
    client, world, public_url, capsys
):
    owner, partner = world["owner_id"], world["partner_id"]
    _disable(world, owner, partner)
    owner_was, partner_was = _account(client, owner), _account(client, partner)
    batches = _batches(client)

    assert _main(owner_was[0], "--enable", "--password", "--base-url", BASE) == 0
    out = capsys.readouterr().out

    after = _account(client, owner)
    assert after[6] is None, "enabled"
    assert after[2] != owner_was[2], "and the password replaced"
    assert after[3:6] == owner_was[3:6], "the authenticator and the role left alone"
    assert _account(client, partner) == partner_was

    # Both in the one batch: the enable and the link.
    batch_id, kind, actor, source, _ = _the_new_batch(client, batches)
    assert (kind, actor, json.loads(source)) == ("admin", owner, {"via": "scripts.reset_account"})
    changes = _rows(
        client, "SELECT table_name, op, row_id, before, after FROM changes WHERE batch_id = ?", batch_id
    )
    assert ("account_resets", "insert") in {(table, op) for table, op, *_ in changes}
    assert any(
        table == "users" and row_id == owner
        and json.loads(was)["disabled_at"] is not None and json.loads(now)["disabled_at"] is None
        for table, op, row_id, was, now in changes
    ), changes

    # A link for a disabled account does not open; this one does.
    assert _resolves_to(_token(out)) == owner
    assert "re-enabled" in out and "so the link opens" in out


def test_enable_alone_opens_a_link_issued_while_the_account_was_disabled(
    client, world, public_url, capsys
):
    from app.errors import NotFound

    partner, owner = world["partner_id"], world["owner_id"]
    _disable(world, partner, owner)
    assert _main(PARTNER, "--password", "--base-url", BASE) == 0
    token = _token(capsys.readouterr().out)
    with pytest.raises(NotFound):
        _resolves_to(token)
    shut, owner_was = _account(client, partner), _account(client, owner)
    resets = _rows(client, "SELECT id, token_hash FROM account_resets")

    assert _main(PARTNER, "--enable") == 0
    out = capsys.readouterr().out

    # The same link, untouched, now opens; the account is otherwise as it was left.
    assert _rows(client, "SELECT id, token_hash FROM account_resets") == resets
    assert _resolves_to(token) == partner
    assert _account(client, partner) == (*shut[:6], None)
    assert _account(client, owner) == owner_was
    assert "still the only way in" in out and "sign them in" not in out


@pytest.mark.parametrize("switches", [("--enable",), ("--enable", "--make-owner")])
def test_enable_on_an_account_that_is_not_disabled_writes_nothing(client, world, capsys, switches):
    owner, partner = world["owner_id"], world["partner_id"]
    _disable(world, partner)
    people = [_account(client, owner), _account(client, partner)]
    before, memberships = _counts(client), _memberships(client)

    assert _main(people[0][0], *switches) == 0
    out = capsys.readouterr().out

    # Not even an empty batch, and the account that is disabled stays so.
    assert _counts(client) == before
    assert _memberships(client) == memberships
    assert [_account(client, owner), _account(client, partner)] == people
    assert "enabled. Nothing to do; nothing was changed." in out


def test_enable_with_nothing_to_enable_does_not_let_a_household_go_ahead_on_its_own(client, world, capsys):
    """`--household` alone is refused, and an `--enable` that finds the account
    enabled is no reason to let it through. Disabled, the same run does both."""
    partner, ours = world["partner_id"], world["ours"]
    before, memberships = _counts(client), _memberships(client)
    people = [_account(client, world["owner_id"]), _account(client, partner)]

    assert _main(PARTNER, "--household", ours) == 2
    assert "Say what to do" in capsys.readouterr().err
    assert _main(PARTNER, "--enable", "--household", ours) == 2
    assert "--enable has nothing to do, and --household does not go ahead on its own" in (
        capsys.readouterr().err
    )

    # Not a membership, not even an empty batch.
    assert _counts(client) == before
    assert _memberships(client) == memberships
    assert [_account(client, world["owner_id"]), _account(client, partner)] == people

    _disable(world, partner)
    batches = _batches(client)
    assert _main(PARTNER, "--enable", "--household", ours) == 0

    assert _account(client, partner) == (*people[1][:6], None)
    assert _memberships(client) == memberships | {(ours, partner, None)}
    batch_id, kind, actor, source, _ = _the_new_batch(client, batches)
    assert (kind, actor, json.loads(source)) == ("admin", partner, {"via": "scripts.reset_account"})
    assert sorted(_rows(client, "SELECT table_name, op FROM changes WHERE batch_id = ?", batch_id)) == [
        ("household_members", "insert"), ("users", "update")
    ]


@pytest.mark.parametrize("action", ["--enable", "--make-owner"])
@pytest.mark.parametrize("link", ["waiting", "withdrawn", "swept"])
def test_an_account_an_earlier_link_shut_is_never_told_its_old_credentials_work(
    client, world, public_url, capsys, link, action
):
    """A link shuts the account when it is issued. Withdrawn, or lapsed and
    swept by housekeeping, it leaves the account shut with no row to say so --
    but the cleared authenticator still shows, and the command must say so,
    before it asks and after."""
    from datetime import timedelta

    from sqlalchemy import select

    import app.db as db
    from app.audit.batch import batch
    from app.auth import housekeeping, service
    from app.errors import Unauthorized
    from app.models import AccountReset, BatchKind, User, utcnow
    from app.services import account_resets

    partner, owner = world["partner_id"], world["owner_id"]
    assert _main(PARTNER, "--authenticator", "--base-url", BASE) == 0
    if link == "withdrawn":
        with db.session_scope() as session, batch(session, kind=BatchKind.admin, actor_id=owner):
            account_resets.withdraw(session, session.scalars(select(AccountReset)).one(), by=None)
    elif link == "swept":
        lapsed = timedelta(hours=account_resets.VALID_HOURS) + account_resets.RETENTION + timedelta(days=1)
        assert housekeeping._sweep_account_resets(db.engine, now=utcnow() + lapsed) == 1
    assert _rows(client, "SELECT count(*) FROM account_resets") == [(1 if link == "waiting" else 0,)]
    if action == "--enable":
        _disable(world, partner)
    capsys.readouterr()
    shut, owner_was, before = _account(client, partner), _account(client, owner), _counts(client)
    assert shut[3] is None, "the authenticator the link cleared"

    # Said before it asks: answering no writes nothing.
    assert _main(PARTNER, action, ask=lambda _: "no") == 1
    asked = capsys.readouterr().out
    assert _counts(client) == before

    assert _main(PARTNER, action) == 0
    out = capsys.readouterr().out

    if action == "--enable":
        assert _account(client, partner) == (*shut[:6], None)
    else:
        assert _account(client, partner) == (*shut[:5], "owner", shut[6])
    assert _account(client, owner) == owner_was
    # What the command must not promise against: the code step refuses them.
    with db.session_scope() as session, pytest.raises(Unauthorized, match="authenticator was reset"):
        service.check_code(session, db.engine, session.get(User, partner), "000000", ip=None)

    for said in (asked, out):
        assert "sign them in" not in said and "sign in again" not in said, said
    if link == "waiting":
        assert "still the only way in" in out
        return
    assert "no authenticator, and no reset link is waiting" in asked and "enabled or not" in asked
    assert "They still cannot sign in" in out and "--authenticator issues one" in out

    # Asked again, there is nothing to do, and it still says why they are out.
    after = _counts(client)
    assert _main(PARTNER, action) == 0
    again = capsys.readouterr().out
    assert _counts(client) == after
    assert "Nothing to do" in again and "It has no authenticator, though" in again


@pytest.mark.parametrize(
    "switches", [("--make-owner",), ("--password", "--base-url", BASE)], ids=["make-owner", "password"]
)
def test_a_disabled_account_acted_on_without_enable_stays_disabled_and_says_how(
    client, world, capsys, switches
):
    from app.errors import NotFound

    partner, owner = world["partner_id"], world["owner_id"]
    _disable(world, partner, owner)
    partner_was, owner_was = _account(client, partner), _account(client, owner)

    assert _main(PARTNER, *switches) == 0
    out = capsys.readouterr().out

    assert "This account is disabled" in out and "--enable does that here" in out
    after = _account(client, partner)
    assert after[6] == partner_was[6], "still disabled, from the same moment"
    assert _account(client, owner) == owner_was
    if "--make-owner" in switches:
        assert after[5] == "owner", "what was asked for was still done"
        assert "Still disabled" in out and "still sign them in" not in out
    else:
        assert after[2] != partner_was[2], "what was asked for was still done"
        with pytest.raises(NotFound):
            _resolves_to(_token(out))


# --------------------------------------------------------------------------- #
# Refusals, which write nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        ([PARTNER, "--password", "--household", "Nowhere", "--base-url", BASE], "No household"),
        ([PARTNER, "--make-owner", "--household", "Doe-Smith"], "households are called"),
        (["nobody@example.com", "--password", "--base-url", BASE], "--make-owner --name"),
        (["nobody@example.com", "--make-owner", "--base-url", BASE], "give --name"),
        ([PARTNER, "--make-owner", "--name", "Partner Again"], "only for a new account"),
        ([PARTNER, "--password"], "--base-url"),
        ([PARTNER, "--password", "--base-url", "ledger.example.com"], "must be an origin"),
        ([PARTNER, "--household", "Doe-Smith"], "Say what to do"),
        ([PARTNER], "--make-owner, --enable, or more than one"),
        (
            ["nobody@example.com", "--make-owner", "--name", "Nobody", "--enable", "--base-url", BASE],
            "--enable has nothing to do",
        ),
        (["nobody@example.com", "--enable"], "--make-owner --name"),
    ],
    ids=[
        "unknown-household", "ambiguous-household", "unknown-email", "new-owner-without-name",
        "name-for-an-existing-account", "no-base-url", "bad-base-url", "household-alone", "nothing",
        "enable-a-new-account", "enable-an-unknown-email",
    ],
)
def test_a_refusal_writes_nothing(client, world, public_url, capsys, argv, why):
    import app.db as db
    from app.audit.batch import batch
    from app.models import BatchKind, Household

    # A second "Doe-Smith", so the name alone no longer says which.
    with db.session_scope() as session, batch(session, kind=BatchKind.admin, actor_id=world["owner_id"]):
        session.add(Household(name="Doe-Smith", base_currency="GBP"))
    before, memberships = _counts(client), _memberships(client)
    people = [_account(client, world["owner_id"]), _account(client, world["partner_id"])]
    doors = [_doors(client, world["owner_id"]), _doors(client, world["partner_id"])]

    assert _main(*argv) == 2
    assert why in capsys.readouterr().err

    assert _counts(client) == before
    assert _memberships(client) == memberships
    assert [_account(client, world["owner_id"]), _account(client, world["partner_id"])] == people
    assert [_doors(client, world["owner_id"]), _doors(client, world["partner_id"])] == doors


def test_saying_no_at_the_question_writes_nothing(client, world):
    before, was = _counts(client), _account(client, world["partner_id"])

    assert _main(PARTNER, "--password", "--make-owner", "--base-url", BASE, ask=lambda _: "no") == 1

    assert _counts(client) == before
    assert _account(client, world["partner_id"]) == was


def test_an_instance_nobody_has_set_up_gets_its_owner_from_the_wizard_not_from_here(client, capsys):
    before = _counts(client)

    assert _main("first@example.com", "--make-owner", "--name", "First", "--base-url", BASE) == 2

    assert "not been set up" in capsys.readouterr().err
    assert _counts(client) == before and before["users"] == 0


@pytest.mark.parametrize("action", ["--enable", "--make-owner"])
def test_a_withdrawn_password_link_is_named_as_the_missing_half(client, world, capsys, action):
    """A cleared password is NULL, so the command names it and the switch that
    sets it -- not the authenticator, which the link left alone."""
    from sqlalchemy import select

    from app import db
    from app.audit.batch import batch
    from app.models import AccountReset, BatchKind
    from app.services import account_resets

    partner, owner = world["partner_id"], world["owner_id"]
    assert _main(PARTNER, "--password", "--base-url", BASE) == 0
    with db.session_scope() as session, batch(session, kind=BatchKind.admin, actor_id=owner):
        account_resets.withdraw(session, session.scalars(select(AccountReset)).one(), by=None)
    if action == "--enable":
        _disable(world, partner)
    capsys.readouterr()
    shut = _account(client, partner)
    assert shut[2] is None and shut[3] is not None, "the password cleared, the authenticator kept"

    assert _main(PARTNER, action) == 0
    out = capsys.readouterr().out
    assert "no password, and no reset link is waiting" in out and "--password issues" in out
    assert "no authenticator" not in out and "--authenticator issues" not in out
    assert "sign them in" not in out
    assert _account(client, partner)[2] is None, "enabling or promoting sets no password"
