"""Build a household that looks like one, through the real services.

Every row here goes through the same service calls the API uses, so this
doubles as a smoke test of everything the unit tests exercise in isolation --
which is how the previous build caught things its unit tests mocked out.

    python -m scripts.seed_demo --reset [--months 6] [--force]

``--reset`` refuses a database that holds anybody but the demo owner: it has
wiped a real ledger once, because ``./data`` and ``~/.local/share`` both
resolve depending on where it runs (#226). ``--force`` goes ahead after the
resolved path is typed back.

It prints the credentials it created. They are deliberately weak and obvious:
this is demo data, and anything that looks like a real password would sooner or
later be treated as one.
"""

from __future__ import annotations

import argparse
import pathlib
import random
import sqlite3
from datetime import date, timedelta

import pyotp
from sqlalchemy import select, text

import app.audit.guard  # noqa: F401  -- installs the bulk-statement guard
import app.audit.hook  # noqa: F401  -- installs the audit flush hook
from app.audit.batch import batch
from app.auth import setup as setup_service
from app.auth import totp
from app.config import settings
from app.db import engine, session_scope
from app.models import (
    Account,
    AccountType,
    Base,
    BatchKind,
    ClearedState,
    MatchType,
    ReimbursementState,
    SystemPayee,
    User,
)
from app.money import format_amount
from app.services import accounts as account_service
from app.services import categories as category_service
from app.services import households as household_service
from app.services import payees as payee_service
from app.services import transactions as txn_service

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo password for a demo"

#: Realistic bank strings, because that is what the importer will meet, as
#: ``(raw, pattern, payee, (low, high), category)``.
#:
#: The fifth column is the category each one lands in -- a name from
#: `categories.DEFAULT_TREE`, which every household now starts with. A demo
#: whose every row reads "uncategorised" teaches nothing about the feature and
#: makes every category picker in the app look empty and broken.
#:
#: **The second column is the rule pattern, written out.** It used to be
#: derived as `raw.split()[0]`, which is a heuristic that is wrong in principle
#: and not merely for one row: for `SQ *EL BAR` it produced a rule of `SQ` --
#: the card acquirer, not the shop -- and that one `contains` rule then claimed
#: every Square descriptor in every file the demo imported. Three merchants
#: here share an acquirer prefix precisely so the seeded database shows them
#: resolving to three payees rather than to one.
MERCHANTS = [
    ("MERCADONA 1234", "MERCADONA", "Mercadona", (2_500, 9_500), "Groceries"),
    ("CARREFOUR MADRID 4432", "CARREFOUR", "Carrefour", (1_200, 6_000), "Groceries"),
    ("SQ *EL BAR", "EL BAR", "El Bar", (350, 1_800), "Eating Out"),
    ("SQ *CEDAR ROOMS", "CEDAR ROOMS", "Cedar Rooms", (1_100, 4_400), "Eating Out"),
    (
        "Zettle_*Grey Heron Coffee",
        "GREY HERON",
        "Grey Heron Coffee",
        (250, 900),
        "Eating Out",
    ),
    ("FARMACIA CENTRAL", "FARMACIA", "Farmacia", (600, 3_200), "Health"),
    ("RENFE VIAJEROS", "RENFE", "Renfe", (900, 4_500), "Transport"),
    ("AMZN Mktp ES", "AMZN", "Amazon", (800, 7_000), "Household"),
    ("REPSOL E.S. 447", "REPSOL", "Repsol", (4_000, 8_500), "Transport"),
]


def _seed_owner(session) -> User:
    """Walk the real setup wizard, headlessly.

    This used to build the user by hand and claim in its docstring to be "the
    same path the setup wizard walks". It was nearly that -- user row, sealed
    TOTP secret, `setup` batch, `Instance` row -- and it missed the recovery
    codes, because `complete()` mints those and a hand-built user has nobody to
    mint them. The demo account was therefore the one account that could not use
    the "I've lost my authenticator" link on the sign-in screen, and the admin
    screen showed it a flat `0 left` that looked like a bug in the screen.

    Going through `setup` instead of alongside it means there is one definition
    of what a configured instance is, and the seed cannot drift from it again:
    whatever the wizard starts issuing tomorrow, the demo owner gets too.
    """
    token = setup_service.rotate_setup_token()
    blob = setup_service.begin(
        token, email=DEMO_EMAIL, display_name="Demo", password=DEMO_PASSWORD
    )
    # The wizard will not finish without a code that actually verifies, which is
    # the point of it -- so answer the challenge rather than going around it.
    blob = setup_service.confirm_authenticator(
        blob, pyotp.TOTP(blob.totp_secret).now()
    )

    session.execute(text("PRAGMA defer_foreign_keys=ON"))
    user, _session_value, _device_value = setup_service.complete(
        session, blob, ip=None, user_agent="scripts/seed_demo.py"
    )

    user.demo_totp_secret = blob.totp_secret  # type: ignore[attr-defined]
    user.demo_recovery_codes = blob.recovery_codes  # type: ignore[attr-defined]
    return user


def _database_path() -> pathlib.Path | None:
    """The file ``settings.database_url`` names, when it is SQLite on disk."""
    prefix = "sqlite:///"
    url = settings.database_url
    if not url.startswith(prefix) or ":memory:" in url:
        return None
    return pathlib.Path(url[len(prefix):].split("?", 1)[0]).resolve()


def _people_not_the_demo(path: pathlib.Path) -> int:
    """How many users in the file at ``path`` are not the demo owner.

    Opened read-only, so looking cannot create the file or change it. No file,
    or no ``users`` table, is nobody.
    """
    if not path.exists():
        return 0
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        emails = [row[0] for row in connection.execute("SELECT email FROM users")]
    except sqlite3.OperationalError:
        return 0
    finally:
        connection.close()
    return sum(1 for email in emails if (email or "").strip().lower() != DEMO_EMAIL)


def _refuse_a_real_ledger(*, force: bool) -> None:
    """Stop before ``--reset`` drops a database somebody really uses (#226)."""
    path = _database_path()
    if path is None:
        return
    others = _people_not_the_demo(path)
    if not others:
        return
    print(f"The database is {path}")
    print(f"It holds {others} account(s) that are not the demo's, so it is somebody's ledger.")
    if not force:
        raise SystemExit(
            "Refusing to wipe it. Point SPENDTRACKER_DATA_DIR at a directory of its own, "
            "or run with --force and type the path above to wipe it anyway."
        )
    typed = input("Type the path above to wipe it: ").strip()
    if typed != str(path):
        raise SystemExit("That is not the path. Nothing was changed.")


def _reset() -> None:
    """Drop everything, then rebuild through Alembic.

    `create_all` was quicker and wrong: it builds what the models say today and
    skips every migration, so CI's smoke test ran against a schema no upgrade
    path had ever produced -- which is exactly the drift `app/main.py` refuses
    to allow at boot, for the reason it gives there. Going through `upgrade
    head` means the seed exercises the migrations too.
    """

    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    _schema()


def _schema() -> None:
    """Build the schema the only way the app accepts: through the migrations."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(pathlib.Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")


def seed(months: int) -> None:
    rng = random.Random(20260917)
    start = date.today().replace(day=1) - timedelta(days=31 * months)

    with session_scope() as session:
        owner = _seed_owner(session)
        secret = owner.demo_totp_secret  # type: ignore[attr-defined]
        recovery = owner.demo_recovery_codes  # type: ignore[attr-defined]

        with batch(session, kind=BatchKind.seed, actor_id=owner.id):
            household = household_service.create_household(
                session, name="Demo Household", creator=owner
            )

        with batch(
            session, kind=BatchKind.seed, actor_id=owner.id, household_id=household.id
        ):
            checking = account_service.create_account(
                session, household=household, name="Santander", type=AccountType.checking
            )
            card = account_service.create_account(
                session, household=household, name="Visa", type=AccountType.credit_card
            )
            savings = account_service.create_account(
                session,
                household=household,
                name="UK Savings",
                type=AccountType.savings,
                currency="GBP",
            )

            for _raw, pattern, clean, _amounts, _category in MERCHANTS:
                payee = payee_service.get_or_create(session, household.id, clean)
                payee_service.create_rule(
                    session,
                    household_id=household.id,
                    match_type=MatchType.contains,
                    pattern=pattern,
                    payee=payee,
                )

            txn_service.create(
                session,
                account=checking,
                date=start,
                amount=250_000,
                # Marked, like the one `accounts.create_account` writes. This
                # seed builds its opening balance by hand rather than through
                # that service, and an unmarked one is reported as 2,500 of
                # income in the demo's first month -- which is exactly the
                # defect `SystemPayee` exists to prevent, shipped in the
                # fixture people look at first.
                payee=payee_service.get_or_create(
                    session,
                    household.id,
                    "Opening balance",
                    system=SystemPayee.opening_balance,
                ),
                memo="Starting balance",
                cleared=ClearedState.reconciled,
            )

        # Built after the household exists, because `create_household` is what
        # puts the default tree there. Keyed by name, which is what MERCHANTS
        # carries -- ids are made at creation and nothing here can predict them.
        by_name = {
            row.name: row
            for row in category_service.list_categories(session, household.id)
        }

        # A month at a time, so each one is its own act in the history.
        when = start
        for month in range(months):
            with batch(
                session, kind=BatchKind.seed, actor_id=owner.id, household_id=household.id
            ):
                txn_service.create(
                    session,
                    account=checking,
                    date=when + timedelta(days=1),
                    amount=210_000,
                    payee=payee_service.get_or_create(session, household.id, "Employer"),
                    category=by_name.get("Salary"),
                    memo="Salary",
                    cleared=ClearedState.cleared,
                )
                for _ in range(rng.randint(12, 20)):
                    raw, _pattern, clean, (low, high), category_name = rng.choice(MERCHANTS)
                    account = card if rng.random() < 0.45 else checking
                    txn_service.create(
                        session,
                        account=account,
                        date=when + timedelta(days=rng.randint(1, 27)),
                        amount=-rng.randint(low, high),
                        payee=payee_service.get_or_create(session, household.id, clean),
                        category=by_name.get(category_name),
                        import_payee_original=raw,
                        cleared=ClearedState.cleared,
                    )
                if month % 2 == 0:
                    txn_service.create_transfer(
                        session,
                        source=checking,
                        destination=savings,
                        date=when + timedelta(days=20),
                        amount=30_000,
                        to_amount=25_500,
                        memo="Monthly saving",
                    )
                # Deliberately less than a month's charges, so the card
                # carries a balance like a real one rather than drifting into
                # credit -- an overpaid card is the one case the budget layer
                # will have to think about, and it should not be the default
                # thing a demo shows.
                txn_service.create_transfer(
                    session,
                    source=checking,
                    destination=card,
                    date=when + timedelta(days=26),
                    amount=rng.randint(8_000, 18_000),
                    memo="Card payment",
                )
            when = (when + timedelta(days=32)).replace(day=1)

        # Work expenses, one of every state, so the Reimbursements report and
        # the register's Work expenses filter have something on every line the
        # first time anyone opens them: two repaid by one payment, an advance
        # with money left over, one outstanding in each currency, and one work
        # refused. Dated back from today, so the ages read like a real claim.
        with batch(
            session, kind=BatchKind.seed, actor_id=owner.id, household_id=household.id
        ):
            today = date.today()

            def _row(account, days_ago: int, amount: int, name: str, memo: str | None = None):
                return txn_service.create(
                    session,
                    account=account,
                    date=today - timedelta(days=days_ago),
                    amount=amount,
                    payee=payee_service.get_or_create(session, household.id, name),
                    memo=memo,
                    cleared=ClearedState.cleared,
                )

            train = _row(card, 40, -8_450, "Rail operator", "Client visit")
            hotel = _row(card, 39, -24_000, "Hotel Barcelona", "Client visit")
            repaid = _row(checking, 20, 32_450, "Employer expense tool")
            for expense in (train, hotel):
                txn_service.set_reimbursement(session, expense, settled_by=repaid)

            advance = _row(checking, 30, 50_000, "Employer", "Conference advance")
            fee = _row(card, 28, -34_000, "Conference registration")
            txn_service.set_reimbursement(session, fee, settled_by=advance)

            dinner = _row(card, 25, -6_000, "Client dinner")
            txn_service.set_reimbursement(
                session, dinner, state=ReimbursementState.written_off
            )
            taxi = _row(card, 12, -2_340, "Ride-hailing")
            txn_service.set_reimbursement(session, taxi, state=ReimbursementState.expected)
            london = _row(savings, 9, -15_000, "Hotel London")
            txn_service.set_reimbursement(session, london, state=ReimbursementState.expected)

        totals = account_service.balances_for_household(session, household.id)
        count = session.execute(
            select(Account).where(Account.household_id == household.id)
        ).scalars().all()

    print(f"\nSeeded {months} months into {settings.database_url}\n")
    print(f"  sign in as : {DEMO_EMAIL}")
    print(f"  password   : {DEMO_PASSWORD}")
    print(f"  authenticator secret : {secret}")
    print(f"  add it with          : {totp.provisioning_uri(secret, email=DEMO_EMAIL)}")
    # Printed because the sign-in screen offers "I've lost my authenticator" and
    # a demo you cannot try that on is a button that goes nowhere.
    print("  recovery codes       : " + "  ".join(recovery[:5]))
    print("                         " + "  ".join(recovery[5:]) + "\n")
    for account in count:
        figures = totals[account.id]
        # format_amount, not a float divided by a literal 100: this script
        # creates a GBP account and could just as easily create a JPY one, and
        # `/ 100` prints a yen balance a hundred times too small.
        rendered = format_amount(figures["balance"], account.currency, with_symbol=False)
        print(f"  {account.name:14} {rendered:>12} {account.currency}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop everything first")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --reset, wipe a database that has real users, after typing its path",
    )
    args = parser.parse_args(argv)

    if args.reset:
        _refuse_a_real_ledger(force=args.force)
        _reset()
    else:
        # Not create_all: it builds what the models say today and skips every
        # migration, leaving a database no upgrade path ever produced -- which
        # is the drift `app/main.py` refuses to allow at boot, and which left a
        # demo database with no alembic_version at all.
        _schema()

    with session_scope() as session:
        if session.execute(select(User)).scalars().first() is not None:
            print("This database already has a user. Run with --reset to start over.")
            return 1

    seed(args.months)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
