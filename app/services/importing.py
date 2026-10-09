"""Importing a statement, in two phases.

Phase one parses, classifies every line and writes ``import_lines`` — but
touches nothing in the register. Phase two applies what the user approved, in
one batch, so the whole import is one act and undoing it is one click.

That split is the point. The previous build wrote first and offered a review
queue afterwards, so you found out what it had done after it had done it — and
one bad date aborted the entire file at parse time.

Reading the file is not here: that is `statements`, which knows every format and
no database. This module is the half that needs a session — deciding what each
parsed row *means* for the register, and writing it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from statements import (
    DecimalPreference,
    ParsedRow,
    build_balance_import_id,
    build_import_id,
    file_digest,
    parse,
    sniffing,
)

from .. import countries
from ..errors import Conflict, NotFound, ValidationError
from ..models import (
    Account,
    AccountType,
    Batch,
    BatchKind,
    BatchStatus,
    Category,
    CategoryGroup,
    ClearedState,
    ImportLine,
    ImportOutcome,
    MatchType,
    Payee,
    PayeeRule,
    Transaction,
    utcnow,
)
from ..money import MoneyError, format_amount, to_minor
from . import categories as category_service
from . import identifiers as identifier_service
from . import payees as payee_service
from . import transactions as txn_service
from . import transfers as transfer_service

#: How far apart a statement line and a hand-entered row may sit and still be
#: the same purchase.
MATCH_WINDOW_DAYS = 4

#: Re-exported so callers that already speak to this module keep one import.
__all__ = [
    "ParsedRow",
    "build_import_id",
    "file_digest",
    "parse",
    "previous_import_of",
    "stage",
    "reassess",
    "commit",
    "summarise",
    "get_preview",
    "open_previews",
    "discard_preview",
    "MATCH_WINDOW_DAYS",
]


#: An import that has happened, or is waiting for somebody to say yes to it.
#: `undone` is deliberately absent: undoing one is how you make room to do it
#: again, so a guard that still refused afterwards would be a trap.
_LIVE_IMPORTS = (BatchStatus.applied, BatchStatus.preview)


def previous_import_of(session: Session, *, account_id: str, digest: str) -> Batch | None:
    """Has this exact file already been imported into this account, or staged?

    Caught before a single line is parsed, and it catches what per-row dedupe
    cannot: a statement re-downloaded and imported twice by accident.

    > [!warning] Two things were wrong here, and both mattered more once agents
    > could reach this path.

    **`preview` used to be excluded.** It matched only `applied`, so a staged
    import that nobody had committed yet did not count as already-seen. For the
    browser that is a small annoyance -- two previews of one file, and you pick
    one. For an agent it is the whole guard failing open: a key without
    `may_commit` leaves *every* import in `preview` by design, so re-posting
    the same rows staged them again, and again, for as long as it retried.

    **And it scanned in Python.** Every import batch the household had ever
    made was loaded and its JSON walked one row at a time. Fine for somebody
    uploading a file a month; an agent polling hourly makes that grow without
    bound. It is one indexed query now, with the comparison pushed into SQL.
    """
    return session.execute(
        select(Batch)
        .where(
            Batch.kind == BatchKind.imported,
            Batch.status.in_(_LIVE_IMPORTS),
            Batch.source["sha256"].as_string() == digest,
            Batch.source["account_id"].as_string() == account_id,
        )
        .order_by(Batch.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


#: What every row a One-time Import writes carries in `import_source`
#: (`one_time_import.model.Source.import_source`): ``one-time-import:<via>``.
_ONE_TIME_SOURCE = "one-time-import:"


def _absorbable():
    """Rows a statement line may still absorb (#264), as a SQL condition.

    Typed in by hand -- no `import_id` -- or brought in by a One-time Import
    and not yet seen by a statement. Absorbing sets `import_source` to the
    statement's, so a row a statement already absorbed is neither.
    """
    return or_(
        Transaction.import_id.is_(None),
        Transaction.import_source.startswith(_ONE_TIME_SOURCE, autoescape=True),
    )


def _is_absorbable(txn: Transaction) -> bool:
    """:func:`_absorbable`, for a row already in hand."""
    return txn.import_id is None or (txn.import_source or "").startswith(_ONE_TIME_SOURCE)


def _line_keys(parsed: dict) -> set[str]:
    """Every key a staged line answers to: its own, older ones, its nth-that-day."""
    keys = {parsed.get("import_id"), parsed.get("nth_import_id"), *(parsed.get("alt_import_ids") or ())}
    keys.discard(None)
    return keys


def _exactly(txn: Transaction, keys: set[str]) -> bool:
    """Does this row carry one of the line's keys as an alternative id?"""
    return any(alt in keys for alt in txn.import_alt_ids or ())


class _Twins:
    """The existing rows a file's lines could be the same purchase as.

    Read in **one** query per staging or re-assessment: every unlocked row
    of the account a statement may still absorb -- typed by hand, or brought
    in by a One-time Import (#264) -- dated inside the file's span widened by
    the match window. Matching each line against that happens here, in
    Python. It used to be a query per line -- 75% of staging time on a
    50k-row ledger, and re-run on every preview GET (issue #104).

    A second query reads, as two columns, the alternative ids those rows
    carry whatever their date: YNAB's own key for the bank line it imported,
    in the statement key's shape. A line holding that key matches its row
    **exactly**, before the window is consulted, so a row whose date was
    moved in YNAB a week away from the bank's is still found.

    Otherwise the rules are the per-line query's, exactly: amount equal, dates
    at most :data:`MATCH_WINDOW_DAYS` apart, a row already claimed by an
    earlier line is spoken for, and the nearest date wins, then the earlier
    date, then the earlier entry.
    """

    def __init__(self, session: Session, account_id: str, dates: list[date]) -> None:
        self._session = session
        self._by_amount: dict[int, list[Transaction]] = {}
        self._by_id: dict[str, Transaction] = {}
        #: alternative id -> the row carrying it, among absorbable rows.
        self._by_key: dict[str, str] = {}
        if not dates:
            return
        window = timedelta(days=MATCH_WINDOW_DAYS)
        unlocked = (
            Transaction.account_id == account_id,
            Transaction.cleared != ClearedState.reconciled,
            _absorbable(),
        )
        for row in session.execute(
            select(Transaction).where(
                *unlocked,
                Transaction.date >= min(dates) - window,
                Transaction.date <= max(dates) + window,
            )
        ).scalars():
            # Held here, by strong reference, for as long as the matching runs.
            self._by_amount.setdefault(row.amount, []).append(row)
            self._by_id[row.id] = row
        for txn_id, alt_ids in session.execute(
            select(Transaction.id, Transaction.import_alt_ids)
            .where(*unlocked, Transaction.import_alt_ids.is_not(None))
            # Two rows carrying one key is not supposed to happen; when it
            # does, the earlier row wins, and the same one every time.
            .order_by(Transaction.date, Transaction.id)
        ).all():
            for alt in alt_ids or ():
                if isinstance(alt, str):
                    self._by_key.setdefault(alt, txn_id)

    def _exact(self, amount: int, keys: set[str], claimed: set[str]) -> Transaction | None:
        for key in sorted(keys):
            txn_id = self._by_key.get(key)
            if txn_id is None or txn_id in claimed:
                continue
            row = self._by_id.get(txn_id) or self._session.get(Transaction, txn_id)
            if row is not None and row.amount == amount:
                self._by_id[row.id] = row
                return row
        return None

    def match(
        self, wanted: list[tuple[date, int, set[str]]], *, claimed: set[str]
    ) -> list[Transaction | None]:
        """Every line of a file at once: ``(date, amount, keys)`` in file order.

        Exact keys are settled for the **whole file** before any line is
        offered a row by date. Line by line, an earlier line of the same
        amount could take a row through the window before the line that *is*
        that row's bank line was reached -- which then looked new (#264).
        Adds what it matches to ``claimed``.
        """
        found: list[Transaction | None] = [None] * len(wanted)
        for n, (_when, amount, keys) in enumerate(wanted):
            if keys:
                exact = self._exact(amount, keys, claimed)
                if exact is not None:
                    claimed.add(exact.id)
                    found[n] = exact
        for n, (when, amount, _keys) in enumerate(wanted):
            if found[n] is None:
                twin = self.find(when, amount, claimed=claimed)
                if twin is not None:
                    claimed.add(twin.id)
                    found[n] = twin
        return found

    def find(
        self, when: date, amount: int, *, claimed: set[str], keys: set[str] | None = None
    ) -> Transaction | None:
        """An existing row that is the same purchase as this statement line.

        A row carrying one of the line's ``keys`` is the line, whatever its
        date. Otherwise the amount must match exactly and the date may differ
        by a few days, because a card settles later than it is spent. Anything
        matched here is shown in the preview and can be rejected -- which is
        what makes a rule this loose safe.
        """
        if keys:
            exact = self._exact(amount, keys, claimed)
            if exact is not None:
                return exact
        window = timedelta(days=MATCH_WINDOW_DAYS)
        # A twin an earlier line already absorbed is spoken for. Offering it
        # again makes the second line look new, and two hand-entered coffees
        # plus a statement showing both becomes three rows.
        candidates = [
            row
            for row in self._by_amount.get(amount, ())
            if when - window <= row.date <= when + window and row.id not in claimed
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda t: (abs((t.date - when).days), t.date, t.created_at))


def _find_twin(
    session: Session,
    account_id: str,
    when: date,
    amount: int,
    *,
    claimed: set[str] | None = None,
) -> Transaction | None:
    """One line's twin, for a caller matching a single line. See :class:`_Twins`."""
    return _Twins(session, account_id, [when]).find(when, amount, claimed=claimed or set())


def _dates_of(lines: list[ImportLine]) -> list[date]:
    """The dates re-assessment will match on, read the way it reads them."""
    out = []
    for line in lines:
        if line.outcome in _NOT_REASSESSED:
            continue
        text = (line.parsed or {}).get("date")
        if text:
            try:
                out.append(date.fromisoformat(text))
            except ValueError:
                continue
    return out


def _import_ids_in(session: Session, account_id: str) -> set[str]:
    """Every `import_id` this account already carries.

    Read in three places now -- staging, re-assessing a staged import, and
    committing one -- which is why it is a function rather than three copies of
    the same `select`. The set is the whole dedupe: a line whose id is in here
    has already landed, however it got there.
    """
    return set(
        session.execute(
            select(Transaction.import_id).where(
                Transaction.account_id == account_id, Transaction.import_id.is_not(None)
            )
        ).scalars()
    )


#: Lines whose meaning is fixed by the bank's own wording, whatever the
#: household's rules say. Each carries the category it lands in when nothing
#: else decides -- no rule, no history -- and the payee it is filed under, so
#: 340 daily "Net Interest Paid to 'X' for <date>" lines make one payee, not 340.
#: The category is created on commit if the household has none by that name.
@dataclass(frozen=True, slots=True)
class Builtin:
    pattern: re.Pattern[str]
    payee: str | None
    category: str
    group: str


BUILTINS = (
    # Revolut, both files: "Interest earned - <pocket>", "Net Interest Paid to
    # '<pocket>' for <date>". Issue #69.
    Builtin(
        re.compile(r"^(net\s+)?interest\s+(paid|earned)\b", re.IGNORECASE),
        "Interest", "Interest income", "Income",
    ),
    # Revolut's investment and robo-advisor products. Decided for this build
    # (issue #68): these are spending, not accounts of their own.
    Builtin(
        re.compile(r"^to\s+(investment\s+account|robo\s+portfolio)\b", re.IGNORECASE),
        None, "Investments", "Investments",
    ),
)

#: What a fee line lands in (issue #68: a separate row, not netted).
FEES = Builtin(re.compile(r"$^"), "Bank fee", "Bank fees", "Bills")


def builtin_for(payee: str | None) -> Builtin | None:
    text = (payee or "").strip()
    return next((b for b in BUILTINS if b.pattern.search(text)), None)


def _hint(builtin: Builtin) -> dict[str, str]:
    return {"name": builtin.category, "group": builtin.group}


#: The product a checking account takes from a file that holds several, when
#: nobody has said otherwise. Revolut's word for its current account.
DEFAULT_CHECKING_PRODUCT = "Current"


def _product_for(account: Account, rows: list[ParsedRow], notes: list[str]) -> str | None:
    """Which of a multi-account file's products this account takes, if any.

    None when the file holds one product (or none): every row is this
    account's, which is what every file did before issue #68. A file that
    holds several, going into an account that has not said which it takes, is
    refused rather than guessed at -- unless it is a checking account and the
    file has a `Current` product, which is the one guess that is safe.
    """
    products: dict[str, int] = {}
    for row in rows:
        if row.product and not row.problem:
            products[row.product] = products.get(row.product, 0) + 1
    if len(products) < 2:
        return None

    listed = ", ".join(f"{name} ({count})" for name, count in sorted(products.items()))
    chosen = account.statement_product
    if chosen is None and account.type is AccountType.checking and (
        DEFAULT_CHECKING_PRODUCT in products
    ):
        chosen = DEFAULT_CHECKING_PRODUCT
        notes.append(
            f"This file holds more than one account: {listed}. Only the "
            f"{DEFAULT_CHECKING_PRODUCT} rows were read into {account.name}; the others belong "
            "to another account and are listed as skipped. To change which rows this account "
            "takes, set its statement product on the Accounts screen."
        )
    if chosen is None:
        raise ValidationError(
            f"this file holds more than one account ({listed}), and {account.name} has not said "
            "which of them it is. Set its statement product on the Accounts screen, then read "
            "the file again.",
            code="import.choose_product",
            params={"account": account.name, "products": ", ".join(sorted(products))},
        )
    if chosen not in products:
        raise ValidationError(
            f"{account.name} takes the {chosen} rows of a statement, and this file has none: it "
            f"holds {listed}.",
            code="import.product_absent",
            params={"account": account.name, "product": chosen, "products": ", ".join(sorted(products))},
        )
    return chosen


@dataclass(frozen=True, slots=True)
class _Currencies:
    """What a file says about its currencies, against the account's."""

    #: The account's currency, upper-cased the way a stated one is.
    mine: str
    #: Whether a table's currency column holds the account's currency and
    #: another. Then its rows in the other read as another account's rows of a
    #: multi-currency export -- `skipped`. Otherwise a row in another currency
    #: cannot be read into this account -- `rejected`, which is said out loud
    #: and keeps an agent's import from reading as safe to commit.
    several: bool

    def foreign(self, row: ParsedRow) -> str | None:
        """The row's stated currency, when it is not the account's."""
        return row.currency if row.currency and row.currency != self.mine else None

    def belongs_elsewhere(self, row: ParsedRow) -> bool:
        """A row in another currency that is another account's, not a problem.

        Never an OFX row: an OFX file is one account (one `ACCTID`), so a
        transaction there in another currency is still this account's money
        -- skipping it would drop real spending without a word.
        """
        return self.several and row.currency_from == "column"


def _currencies_for(account: Account, rows: list[ParsedRow], notes: list[str]) -> _Currencies:
    """Hold each row's stated currency to the account's (issue #260).

    Every amount is converted with the account's currency, so a row that says
    it is in another one would land as the same figure in the wrong money.
    ``rows`` are the readable rows this account takes -- another product's
    rows are already someone else's. A file in which every one of them states
    another currency is refused, the way a file with no rows for the
    account's statement product is: there is nothing in it for this account.
    """
    mine = (account.currency or "").strip().upper()
    stated: dict[str, int] = {}
    for row in rows:
        if row.currency:
            stated[row.currency] = stated.get(row.currency, 0) + 1
    listed = ", ".join(f"{code} ({count})" for code, count in sorted(stated.items()))
    if rows and all(row.currency and row.currency != mine for row in rows):
        raise ValidationError(
            f"this file is in {listed}, and {account.name} holds {mine}: none of its rows are in "
            f"{mine}. Import it into an account that holds {', '.join(sorted(stated))}, or check "
            "that this is the right file.",
            code="import.wrong_currency",
            params={"account": account.name, "currency": mine, "currencies": ", ".join(sorted(stated))},
        )
    #: Only a table's currency column can hold several accounts' rows.
    in_columns = {row.currency for row in rows if row.currency and row.currency_from == "column"}
    several = mine in in_columns and len(in_columns) > 1
    if several:
        notes.append(
            f"This file holds more than one currency: {listed}. Only the {mine} rows were read "
            f"into {account.name}; the others belong to an account in their own currency and "
            "are listed as skipped."
        )
    return _Currencies(mine=mine, several=several)


def _balance_breaks(rows: list[ParsedRow], currency: str) -> tuple[str, ParsedRow | None]:
    """Does the statement's running balance add up, and which way does it run?

    Returns ``("oldest first" | "newest first", the first row where it stops
    adding up, or None)``. Rows are in file order. A statement is allowed to
    run either way; it is not allowed to run neither.
    """

    def minor(value: Decimal | None) -> int:
        return to_minor(value or Decimal(0), currency)

    def moved(row: ParsedRow) -> int:
        return minor(row.amount) - minor(row.fee)

    up = next(
        (b for a, b in zip(rows, rows[1:], strict=False) if minor(a.balance) + moved(b) != minor(b.balance)),
        None,
    )
    if up is None:
        return "oldest first", None
    down = next(
        (a for a, b in zip(rows, rows[1:], strict=False) if minor(b.balance) + moved(a) != minor(a.balance)),
        None,
    )
    if down is None:
        return "newest first", None
    return "oldest first", up


def _check_balances(
    session: Session, account: Account, rows: list[ParsedRow], notes: list[str]
) -> None:
    """The statement checking its own arithmetic, and against the ledger.

    Two questions, both answered from the running balance a statement prints
    (issues #68, #69):

    - **Does the file add up?** Each row's balance should be the one before it
      plus what this row moved. A break means rows are missing from what was
      read -- or, as with Revolut's account statement before #68, rows from
      another account are mixed in.
    - **Does it carry on from the ledger?** The balance before the file's first
      row should be what the ledger holds for the day before. A difference is a
      gap between this download and the last one.
    """
    rows = [r for r in rows if r.balance is not None and r.amount is not None]
    if len(rows) < 2:
        return
    currency = account.currency
    try:
        direction, broken = _balance_breaks(rows, currency)
        first = rows[0] if direction == "oldest first" else rows[-1]
        opening = to_minor(first.balance, currency) - (
            to_minor(first.amount, currency) - to_minor(first.fee or Decimal(0), currency)
        )
    except MoneyError:
        # A balance, amount or fee too large to record. That row is rejected
        # on its own when it is staged; what it cannot do is take the file
        # with it, which is what an uncaught MoneyError here did (issue #90).
        # Without every figure the arithmetic has nothing to check, so say so.
        notes.append(
            "The statement's running balance was not checked: a balance, amount or fee in it "
            "is too large to record as money."
        )
        return
    if broken is not None:
        notes.append(
            f"The statement's running balance stops adding up at line {broken.line_no} "
            f"({broken.when.isoformat() if broken.when else 'no date'}): a row before it is "
            "missing from what was read, or belongs to another account."
        )
        return

    before = session.execute(
        select(func.count(), func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account.id, Transaction.date < first.when
        )
    ).one()
    if before[0] == 0:
        return
    if int(before[1]) != opening:
        gap = opening - int(before[1])
        notes.append(
            f"This statement starts from {format_amount(opening, currency)} on "
            f"{first.when.isoformat()}, but the ledger holds "
            f"{format_amount(int(before[1]), currency)} for the day before -- "
            f"{format_amount(gap, currency)} apart. A "
            "statement between the last import and this one may be missing: download from the "
            "day after the last one imported."
        )


def _already(parsed: dict, seen: set[str]) -> bool:
    """In the account under its own id, or under the key an older build gave it."""
    return parsed.get("import_id") in seen or any(
        alt in seen for alt in parsed.get("alt_import_ids") or ()
    )


def decimal_preference_for(account: Account) -> DecimalPreference | None:
    """The decimal separator to assume for this account's statements, if any.

    From the country the account is held in -- never its currency, which says
    nothing about where the bank is (a GBP account can be held in Spain). Only
    used when the file cannot say for itself; see `countries.DECIMAL_SEPARATORS`.
    """
    separator = countries.decimal_separator(account.country)
    if separator is None:
        return None
    where = countries.BY_CODE[(account.country or "").strip().upper()]
    return DecimalPreference(separator, f"this account's country is set to {where}")


def stage_file(
    session: Session,
    *,
    account: Account,
    raw: bytes,
    fmt: sniffing.Format,
    batch_row: Batch,
) -> list[ImportLine]:
    """A statement file's rows, classified. The only caller that reads bytes.

    Split from :func:`stage` so that rows arriving as JSON get the same
    treatment as rows arriving as CSV -- the dedupe, the twin matching, the
    payee rules and the per-line verdicts -- without a second write path that
    would drift from this one. Everything below the parse is shared; this
    function is the parse.
    """
    return stage_parsed(session, account=account, rows=parse(raw, fmt), batch_row=batch_row)


def stage_parsed(
    session: Session,
    *,
    account: Account,
    rows: list[ParsedRow],
    batch_row: Batch,
) -> list[ImportLine]:
    """:func:`stage_file` after the parse, for a caller that parsed elsewhere.

    The upload route parses on a worker thread (issue #84) -- `parse` is pure
    CPU over bytes, and doing it on the event loop froze every other request
    for as long as one spreadsheet took -- and then stages here, on the
    session, which must stay on the thread that owns it.
    """
    # A savings statement names its pocket on every row; dropped on the wrong
    # account, say so before staging a single line (issue #69).
    identifier_service.check_pocket(
        session, account, [row.payee or "" for row in rows if not row.problem]
    )
    return stage(session, account=account, rows=rows, batch_row=batch_row)


def stage(
    session: Session,
    *,
    account: Account,
    rows: list[ParsedRow],
    batch_row: Batch,
) -> list[ImportLine]:
    """Classify already-parsed rows. Nothing is written to the register.

    ``rows`` are :class:`statements.parsing.ParsedRow`, whatever produced them.
    That dataclass is a plain carrier with no database and no HTTP, so building
    one from a JSON body costs nothing and the library's rule -- never import
    ``app/`` -- is untouched.
    """
    rules = payee_service.load_rules(session, account.household_id)

    seen_ids = _import_ids_in(session, account.id)

    #: Said about the file as a whole, and kept on the batch so the preview
    #: says it again when reopened from the queue tomorrow.
    notes: list[str] = []
    product = _product_for(account, rows, notes)
    currencies = _currencies_for(
        account,
        [
            row
            for row in rows
            if not row.problem
            and row.when is not None
            and row.amount is not None
            and (product is None or row.product in (None, product))
        ],
        notes,
    )
    _check_balances(
        session,
        account,
        [
            row
            for row in sorted(rows, key=lambda r: r.line_no)
            if not row.problem
            and (product is None or row.product in (None, product))
            # Another currency's running balance is its own, and is not this
            # account's to check.
            and currencies.foreign(row) is None
        ],
        notes,
    )

    # The counter restarts for every file. Seeding it from the database gives
    # the second import of the same statement fresh keys and lets every row
    # through twice -- which is precisely the bug that shipped last time.
    occurrences: dict[tuple[int, date], int] = {}
    #: The same, for the balance-carrying key: rows agreeing on amount, date
    #: *and* the balance after them.
    balance_occurrences: dict[tuple[int, date, int], int] = {}
    claimed_twins: set[str] = set()
    twins = _Twins(session, account.id, [row.when for row in rows if row.when is not None])
    lines: list[ImportLine] = []
    #: rule id -> the lines it claimed, and the rule itself. Counted here
    #: because this is the only place that sees the whole file at once, which is
    #: what "a rule claimed ten of a hundred and forty-three rows" needs.
    claimed_by: dict[str, list[ImportLine]] = {}
    rules_that_fired: dict[str, PayeeRule] = {}
    #: Lines new to the account, decided after the loop: matched to an
    #: existing row or created. Whole-file, so an exact key wins (#264).
    undecided: list[tuple[ImportLine, ParsedRow, int, Builtin | None]] = []

    for row in sorted(
        rows, key=lambda r: (r.when or date.min, r.amount if r.amount and r.amount.is_finite() else 0, r.line_no)
    ):
        line = ImportLine(batch_id=batch_row.id, line_no=row.line_no, raw=row.raw)

        if row.problem or row.when is None or row.amount is None:
            line.outcome = ImportOutcome.rejected
            line.reason = row.problem or "this line has no date or no amount"
            lines.append(line)
            session.add(line)
            continue

        foreign = currencies.foreign(row)
        if foreign is not None and (product is None or row.product in (None, product)):
            # Not converted, and nothing recorded as a figure: the amount is in
            # another currency, and turning it into this account's minor units
            # is the very mistake being refused (issue #260). Its key still
            # advances, for the reason given at `occurrences` below.
            try:
                key = (to_minor(row.amount, account.currency), row.when)
                occurrences[key] = occurrences.get(key, 0) + 1
            except MoneyError:
                pass
            if currencies.belongs_elsewhere(row):
                line.outcome = ImportOutcome.skipped
                line.reason = (
                    f"this row is in {foreign}, and {account.name} holds {currencies.mine}: it "
                    f"belongs to the {foreign} rows of this file"
                )
            elif row.currency_from == "ofx":
                rate = f" (the bank's rate: {row.currency_rate})" if row.currency_rate else ""
                line.outcome = ImportOutcome.rejected
                line.reason = (
                    f"this transaction is in {foreign}{rate}, and {account.name} holds "
                    f"{currencies.mine}. It is this account's, but its amount is not in "
                    f"{currencies.mine} and is not converted here -- enter it by hand in "
                    f"{currencies.mine}"
                )
            else:
                line.outcome = ImportOutcome.rejected
                line.reason = (
                    f"this row is in {foreign}, and {account.name} holds {currencies.mine}, so "
                    f"it cannot be read into it -- it would be recorded as {currencies.mine}"
                )
            lines.append(line)
            session.add(line)
            continue

        try:
            minor = to_minor(row.amount, account.currency)
            # Converted here, with the amount, so a fee or a balance too large
            # to record rejects this one line -- the same as an amount that is
            # -- rather than escaping further down and refusing the whole file
            # (issue #90).
            fee_minor = to_minor(row.fee, account.currency) if row.fee else 0
            balance_minor = (
                to_minor(row.balance, account.currency) if row.balance is not None else None
            )
        except MoneyError as exc:
            line.outcome = ImportOutcome.rejected
            line.reason = str(exc)
            lines.append(line)
            session.add(line)
            continue

        # Bounded where the file meets the ledger: the columns are 300 and 500
        # wide and SQLite will not hold them to it, and a csv cell may be
        # 128 KiB -- stored whole, one planted row made every later read of
        # the identifiers and payee suggestions cost minutes (issue #221).
        line.parsed = {
            "date": row.when.isoformat(),
            "amount": minor,
            "payee": row.payee[:300] if row.payee else row.payee,
            "memo": row.memo[:500] if row.memo else row.memo,
            # What the bank called it, kept beside what we made of it.
            "bank": row.details or None,
        }

        key = (minor, row.when)
        occurrence = occurrences.get(key, 0)
        # Advances for every row, duplicate or not. Skipping it on a duplicate
        # hands the next identical row the same key, so a genuinely new third
        # coffee on a day that already had two is thrown away. It also advances
        # for rows skipped below, because the files imported before those rows
        # were skipped counted them.
        occurrences[key] = occurrence + 1

        # Read fine, and not this account's: the other products of a statement
        # that holds several (issue #68).
        if product is not None and row.product not in (None, product):
            line.outcome = ImportOutcome.skipped
            line.reason = (
                f"this row is {row.product}, and {account.name} takes the {product} rows of "
                "this file"
            )
            lines.append(line)
            session.add(line)
            continue
        if minor == 0 and fee_minor == 0:
            # "Closing transaction 0.00", a day's interest that rounded to
            # nothing: a real line in the statement, and no money in it. Shown,
            # not imported -- thirty a year of them was clutter in the register.
            line.outcome = ImportOutcome.skipped
            line.reason = "this line moves no money"
            lines.append(line)
            session.add(line)
            continue

        # The bank's own id when the file carried one. Otherwise the running
        # balance when the file states one -- stable however the download
        # window was cut (issue #69) -- and the nth-that-day key when it does
        # not. `FITID` is stable across downloads by specification, so
        # overlapping statements dedupe exactly rather than by approximation.
        if row.fitid:
            import_id = row.fitid
            # What the line would be called without its FITID: the shape a
            # One-time Import row's YNAB key is kept in, so it matches exactly
            # (#264). Never consulted for "already in the account".
            line.parsed["nth_import_id"] = build_import_id(minor, row.when, occurrence)
        elif balance_minor is not None:
            balance = balance_minor
            bkey = (minor, row.when, balance)
            nth = balance_occurrences.get(bkey, 0)
            balance_occurrences[bkey] = nth + 1
            import_id = build_balance_import_id(minor, row.when, balance, nth)
            # The key this line had before the balance was read, so a statement
            # imported by an older build is still recognised when it overlaps.
            line.parsed["alt_import_ids"] = [build_import_id(minor, row.when, occurrence)]
        else:
            import_id = build_import_id(minor, row.when, occurrence)
        line.parsed["import_id"] = import_id

        builtin = builtin_for(row.payee)
        if builtin is not None:
            line.parsed["category_hint"] = _hint(builtin)

        if _already(line.parsed, seen_ids):
            line.outcome = ImportOutcome.duplicate_skipped
            line.reason = "this line is already in the account"
            lines.append(line)
            session.add(line)
            if fee_minor:
                lines.append(_fee_line(session, batch_row, row, line, fee_minor, seen_ids))
            continue
        seen_ids.add(import_id)

        undecided.append((line, row, minor, builtin))

        lines.append(line)
        session.add(line)
        if fee_minor:
            lines.append(_fee_line(session, batch_row, row, line, fee_minor, seen_ids))

    twins_found = twins.match(
        [(row.when, minor, _line_keys(line.parsed)) for line, row, minor, _ in undecided],
        claimed=claimed_twins,
    )
    for (line, row, _minor, builtin), twin in zip(undecided, twins_found, strict=True):
        if twin is not None:
            line.outcome = ImportOutcome.matched_existing
            line.transaction_id = twin.id
            line.reason = _match_reason(twin, line.parsed)
        else:
            line.outcome = ImportOutcome.created
            found = rules.resolve(row.payee or "")
            if found.payee is not None:
                line.parsed["payee_id"] = found.payee.id
                line.parsed["payee_resolved"] = found.payee.name
            elif found.rewritten:
                # No payee yet, but a rewrite rule took the rail off. Carried
                # on the line so `commit` creates `Bar Marisol` rather than
                # `PAGO MOVIL BAR MARISOL` -- the rewrite has to survive the trip
                # through the staging table or it only ever affected matching.
                # Issue #58.
                line.parsed["payee_rewritten"] = found.candidate
            elif builtin is not None and builtin.payee:
                # No rule of the household's own: the bank's fixed wording
                # decides, so a year of dated interest lines is one payee.
                line.parsed["payee_rewritten"] = builtin.payee
            if found.rule is not None:
                claimed_by.setdefault(found.rule.id, []).append(line)
                rules_that_fired[found.rule.id] = found.rule

    _warn_about_broad_rules(lines, claimed_by, rules_that_fired)
    _say_which_are_transfers(session, account, lines)

    if notes:
        # Reassigned rather than mutated: a JSON column only notices a new value.
        batch_row.source = {**(batch_row.source or {}), "warnings": notes}
    session.flush()
    return lines


def _match_reason(twin: Transaction, parsed: dict) -> str:
    """What the preview says about a line an existing row will absorb."""
    if _exactly(twin, _line_keys(parsed)):
        return (
            f"the {twin.date.isoformat()} entry your One-time Import brought in is this "
            "bank line; it will be marked as seen by the bank rather than added again"
        )
    if (twin.import_source or "").startswith(_ONE_TIME_SOURCE):
        # Matched by amount and date alone, to a row from another app's
        # history. Near the day that history ends, a new purchase of the same
        # amount looks exactly like the last one it holds.
        return (
            f"matched by amount and date to the {twin.date.isoformat()} entry from your YNAB "
            "history (One-time Import), not by the bank's own key; check it is the same "
            "purchase and not a new one of the same amount before committing"
        )
    return (
        f"looks like the {twin.date.isoformat()} entry you already have; "
        "it will be marked as seen by the bank rather than added again"
    )


def _say_which_are_transfers(session: Session, account: Account, lines: list[ImportLine]) -> None:
    """Tell the preview which new lines are one leg of a transfer (issue #70).

    Nothing is linked here -- a preview writes nothing to the register. Each
    line becomes a stand-in row that is never added to the session, and the
    matcher looks for its other leg among what the ledger already holds. The
    commit asks again, against the rows it actually wrote.
    """
    new = [line for line in lines if line.outcome is ImportOutcome.created and line.parsed]
    if not new:
        return
    stand_ins = {
        f"line:{line.id}": Transaction(
            id=f"line:{line.id}",
            household_id=account.household_id,
            account_id=account.id,
            date=date.fromisoformat(line.parsed["date"]),
            amount=int(line.parsed["amount"]),
            import_payee_original=line.parsed.get("payee"),
            memo=line.parsed.get("memo"),
        )
        for line in new
    }
    by_stand_in = {f"line:{line.id}": line for line in new}
    with session.no_autoflush:
        found = transfer_service.find(
            session, account.household_id, among=list(stand_ins.values())
        )
        accounts = {
            row.id: row.name
            for row in session.execute(
                select(Account).where(Account.household_id == account.household_id)
            ).scalars()
        }

    def other(pair: transfer_service.Pair, mine: str) -> Transaction:
        return pair.in_leg if pair.out_leg.id == mine else pair.out_leg

    for pair in found.strong + found.suggested:
        for leg in (pair.out_leg, pair.in_leg):
            line = by_stand_in.get(leg.id)
            if line is None or line.reason:
                continue
            partner = other(pair, leg.id)
            where = accounts.get(partner.account_id, "another account")
            if pair.strength == "strong":
                line.reason = (
                    f"a transfer with {where} ({partner.date.isoformat()}): {pair.why}. It is "
                    "linked when you commit, and stays out of Income vs Expense."
                )
            else:
                line.reason = (
                    f"may be a transfer with {where} ({partner.date.isoformat()}): {pair.why}. "
                    "Confirm it on the Transfers screen after committing."
                )
    for txn, why in found.awaiting:
        line = by_stand_in.get(txn.id)
        if line is not None and not line.reason:
            line.reason = (
                f"looks like your own money moving -- it {why}. It is linked when that "
                "statement is imported."
            )
    # The stand-ins were never added; make sure nothing cascaded them in.
    for stand_in in stand_ins.values():
        if stand_in in session:
            session.expunge(stand_in)


def _fee_line(
    session: Session,
    batch_row: Batch,
    row: ParsedRow,
    main: ImportLine,
    fee_minor: int,
    seen_ids: set[str],
) -> ImportLine:
    """What the bank charged for a row, as a row of its own (issue #68).

    Its own line rather than netted into the payment, so fees add up to
    something a person can see: a *Bank fees* category, filed under one payee.
    Its id is the payment's plus `:fee`, so it dedupes with the payment.
    """
    parsed = main.parsed or {}
    import_id = f"{parsed['import_id']}:fee"
    fee = ImportLine(
        batch_id=batch_row.id,
        line_no=row.line_no,
        raw=row.raw,
        parsed={
            "date": parsed["date"],
            "amount": -fee_minor,
            "payee": FEES.payee,
            "memo": f"fee on {row.payee}" if row.payee else "fee",
            "bank": parsed.get("bank"),
            "import_id": import_id,
            "alt_import_ids": [f"{alt}:fee" for alt in parsed.get("alt_import_ids") or ()],
            "category_hint": _hint(FEES),
            "payee_rewritten": FEES.payee,
        },
    )
    if _already(fee.parsed, seen_ids):
        fee.outcome = ImportOutcome.duplicate_skipped
        fee.reason = "this fee is already in the account"
    else:
        seen_ids.add(import_id)
        fee.outcome = ImportOutcome.created
        fee.reason = f"the bank charged this on top of line {row.line_no}"
    session.add(fee)
    return fee


#: A literal pattern shorter than this is a landmine whatever it matched today.
#: `SQ` is two characters and catches `BASQUE`; normalising the acquirer out of
#: both sides fixes the Square descriptors and does nothing for that.
MIN_SAFE_PATTERN_LENGTH = 4

#: And a longer pattern that claims this much of one file is worth a second
#: look even though it may be perfectly correct -- half a statement being one
#: payee is either a season ticket or a rule that is too greedy.
BROAD_RULE_SHARE = 0.5

#: ...but only once there are enough rows for a share to mean anything. Two of
#: three rows is not evidence of anything.
BROAD_RULE_FLOOR = 10


def _warn_about_broad_rules(
    lines: list[ImportLine],
    claimed_by: dict[str, list[ImportLine]],
    rules: dict[str, PayeeRule],
) -> None:
    """Say so, on the rows themselves, when one rule claimed too much.

    A warning, never a refusal: the rule may be right, and a preview that
    blocked on a suspicion would be worse than one that does not mention it.
    It is written onto the lines the rule claimed -- so it appears in the "what
    happens" column, beside the payee it produced, rather than in a banner at
    the top that says nothing about which rows it means -- and it is written to
    a column, so it is still there when the preview is opened again tomorrow.

    Generic on purpose. The defect that prompted it was one acquirer prefix;
    this catches the next hand-typed two-character rule as well, whatever it
    abbreviates.
    """
    total = len(lines)
    if not total:
        return

    for rule_id, claimed in claimed_by.items():
        rule = rules[rule_id]
        pattern = (rule.pattern or "").strip()
        literal = rule.match_type in (MatchType.contains, MatchType.prefix)
        too_short = literal and len(pattern) < MIN_SAFE_PATTERN_LENGTH
        too_greedy = len(claimed) >= BROAD_RULE_FLOOR and len(claimed) >= total * BROAD_RULE_SHARE
        if not (too_short or too_greedy):
            continue

        why = (
            f"{len(pattern)} characters is very little for a {rule.match_type.value} rule"
            if too_short
            else "that is most of the file"
        )
        note = (
            f'the "{pattern}" {rule.match_type.value} rule claimed '
            f"{len(claimed)} of {total} rows — {why}. Edit or delete it on the Rules "
            "screen if these are not all the same payee."
        )
        for line in claimed:
            # Never over a verdict: a matched or rejected line has already used
            # this column to say something that cannot be worked out again.
            if not line.reason:
                line.reason = note


#: Outcomes a re-assessment leaves alone. A rejected line failed to *parse* --
#: no date, no amount, an unreadable figure -- and nothing that happens in the
#: register afterwards turns that into a line worth importing.
_NOT_REASSESSED = (ImportOutcome.rejected, ImportOutcome.skipped)


def reassess(session: Session, *, batch_row: Batch, account: Account) -> list[ImportLine]:
    """Re-decide a staged import's lines against the register as it is *now*.

    A preview is a verdict, and the verdict was reached at the moment the file
    was staged. Everything it depends on can move afterwards, and on this app it
    routinely does: a preview sits in the queue for a week while the same
    statement is imported from another machine, while the rows it matched are
    edited or deleted, or while the import that made it a duplicate is undone.
    Reopening it then showed the week-old answer, and the only thing that would
    have corrected it was committing -- which is the point at which being wrong
    costs a duplicated row.

    So the queue's verdicts are derived, not stored: reopening one re-runs the
    two checks that staging ran.

    - **Already in the account.** `parsed["import_id"]` against every id the
      account carries. It catches the same file imported elsewhere, an
      overlapping statement sharing a `FITID`, and -- read in reverse -- an
      import that has since been undone, which puts a `duplicate_skipped` line
      back to `created` rather than leaving the file permanently un-importable.
    - **A hand-entered twin.** Re-matched from scratch, so a match whose row was
      deleted, edited out of the window or claimed by another import stops being
      a match, and a row typed in since staging becomes one.

    Returns the lines whose verdict actually changed, so the caller can say so
    on screen. Nothing else is touched: a category chosen by hand on the preview
    screen is the user's decision and survives, and a `rejected` line is a parse
    failure that no amount of register activity redeems.

    **This writes,** and it is called from a `GET`. That is deliberate and it is
    safe: `import_lines` is not an audited table, so no batch is owed, and the
    row being corrected is the preview's own scratch working rather than
    anything in the ledger. The alternative -- deciding in memory on every read
    -- would leave the stored outcome disagreeing with the screen, and `commit`
    reads the stored one.
    """
    lines = list(
        session.execute(
            select(ImportLine)
            .where(ImportLine.batch_id == batch_row.id)
            .order_by(ImportLine.line_no)
        ).scalars()
    )
    seen_ids = _import_ids_in(session, account.id)
    rules = payee_service.load_rules(session, account.household_id)

    #: Twins this batch has already spoken for, in line order, exactly as
    #: `stage` does it -- otherwise two identical lines both match the one
    #: hand-entered row and the second looks new.
    claimed_twins: set[str] = set()
    twins = _Twins(session, account.id, _dates_of(lines))
    changed: list[ImportLine] = []
    #: Lines not already in the account, with their verdict before this run:
    #: matched or created after the loop, over the whole file (#264).
    undecided: list[tuple[ImportLine, date, int, tuple]] = []

    for line in lines:
        if line.outcome in _NOT_REASSESSED:
            continue
        parsed = line.parsed or {}
        import_id = parsed.get("import_id")
        when_text, amount = parsed.get("date"), parsed.get("amount")
        if not import_id or not when_text or amount is None:
            # Staged before this field existed, or parsed into something this
            # cannot read. Leaving the verdict alone is the honest answer;
            # inventing one from a line we cannot re-derive is not.
            continue
        when = date.fromisoformat(when_text)
        amount = int(amount)

        was, was_reason, was_txn = line.outcome, line.reason, line.transaction_id

        if _already(parsed, seen_ids):
            line.outcome = ImportOutcome.duplicate_skipped
            line.reason = "this line is already in the account"
            line.transaction_id = None
            if (line.outcome, line.reason, line.transaction_id) != (was, was_reason, was_txn):
                changed.append(line)
        else:
            undecided.append((line, when, amount, (was, was_reason, was_txn)))

    twins_found = twins.match(
        [(when, amount, _line_keys(line.parsed or {})) for line, when, amount, _ in undecided],
        claimed=claimed_twins,
    )
    for (line, _when, _amount, before), twin in zip(undecided, twins_found, strict=True):
        parsed = line.parsed or {}
        if twin is not None:
            line.outcome = ImportOutcome.matched_existing
            line.transaction_id = twin.id
            line.reason = _match_reason(twin, parsed)
        else:
            line.outcome = ImportOutcome.created
            line.transaction_id = None
            line.reason = None
            # A line that was a duplicate never had a payee resolved, so
            # becoming new here would otherwise produce a transaction with
            # the bank's raw descriptor and no payee. Resolved once, and
            # only when there is nothing there: what the rules say today is
            # what a line staged today would get.
            if not parsed.get("payee_id"):
                found = rules.resolve(parsed.get("payee") or "")
                if found.payee is not None:
                    line.parsed = {
                        **parsed,
                        "payee_id": found.payee.id,
                        "payee_resolved": found.payee.name,
                    }
                elif found.rewritten:
                    line.parsed = {**parsed, "payee_rewritten": found.candidate}
        if (line.outcome, line.reason, line.transaction_id) != before:
            changed.append(line)

    if changed:
        # Back in line order: the twins were decided after the loop.
        order = {id(line): n for n, line in enumerate(lines)}
        changed.sort(key=lambda line: order[id(line)])
        session.flush()
    return changed


def _load_by_id(session: Session, model: type, ids: set[str]) -> dict:
    """Rows by id, read in as few queries as SQLite's parameter limit allows."""
    found: dict = {}
    wanted = sorted(ids)
    for start in range(0, len(wanted), _IN_CHUNK):
        chunk = wanted[start : start + _IN_CHUNK]
        for row in session.execute(select(model).where(model.id.in_(chunk))).scalars():
            found[row.id] = row
    return found


#: Ids per `IN (...)`. Far under SQLite's limit on bound parameters, which was
#: 999 before 3.32 and is still that on some builds.
_IN_CHUNK = 500


def summarise(lines: list[ImportLine]) -> dict[str, int]:
    counts = {outcome.value: 0 for outcome in ImportOutcome}
    for line in lines:
        counts[line.outcome.value] += 1
    return counts


def commit(
    session: Session,
    *,
    batch_row: Batch,
    account: Account,
    skip_line_ids: set[str] | None = None,
    reject_matches: set[str] | None = None,
) -> dict[str, int]:
    """Apply a staged import.

    ``skip_line_ids`` drops lines the user unticked; ``reject_matches`` turns a
    proposed absorption into a new row instead.
    """
    try:
        return _commit(
            session,
            batch_row=batch_row,
            account=account,
            skip_line_ids=skip_line_ids,
            reject_matches=reject_matches,
        )
    except IntegrityError as exc:
        # The rows are written in one flush now, without a clash query ahead of
        # each (issue #101), so a line that reached the account between the
        # read of its ids and this write is refused by the unique constraint
        # instead. Said the way the clash query said it.
        if "transactions.account_id, transactions.import_id" in str(exc.orig):
            raise Conflict(
            "that statement line is already in this account", code="import.line_already_in_account"
        ) from exc
        raise


def _commit(
    session: Session,
    *,
    batch_row: Batch,
    account: Account,
    skip_line_ids: set[str] | None,
    reject_matches: set[str] | None,
) -> dict[str, int]:
    if batch_row.status is not BatchStatus.preview:
        raise Conflict(
            f"that import is {batch_row.status.value}, not waiting to be committed",
            code="import.not_awaiting_commit",
            params={"status": batch_row.status.value},
        )

    skip_line_ids = skip_line_ids or set()
    reject_matches = reject_matches or set()
    #: payee id -> the category every row of that payee gets in this import.
    decided: dict[str, Category | None] = {}

    lines = list(
        session.execute(
            select(ImportLine)
            .where(ImportLine.batch_id == batch_row.id)
            .order_by(ImportLine.line_no)
        ).scalars()
    )

    #: Read once, here, rather than trusted from the preview's stored verdict.
    #: The preview is re-derived whenever the screen is opened, but a commit can
    #: arrive from a tab that has been sitting open since before another import
    #: landed -- and the cost of being wrong in that direction is a duplicated
    #: row in the register, which is the one thing this whole two-phase split
    #: exists to prevent. Kept up to date as this loop writes, so two lines of
    #: one file carrying the same id cannot both get through either.
    seen_ids = _import_ids_in(session, account.id)

    #: Every row this commit touches or writes, held by strong reference. The
    #: session's identity map is weak: a row written three hundred lines ago
    #: has been dropped by the time the transfer matcher below asks for it,
    #: and asking again was a SELECT per line (issue #101). The rows a staged
    #: line already points at -- the hand-entered twins -- are read in one go.
    held: dict[str, Transaction] = _load_by_id(
        session, Transaction, {line.transaction_id for line in lines if line.transaction_id}
    )
    #: Payees and categories, looked up once each rather than once per line --
    #: the payees the rules already resolved in a single query.
    payees_by_id: dict[str, Payee | None] = _load_by_id(
        session,
        Payee,
        {str((line.parsed or {})["payee_id"]) for line in lines if (line.parsed or {}).get("payee_id")},
    )
    #: Every payee a descriptor names, by folded name, from one query -- and
    #: the payees this commit makes, added pending with no flush each. A
    #: statement of reference-bearing descriptors is a new payee per line,
    #: and asking per line was a SELECT, an INSERT and a flush each (#236).
    payees_by_name: dict[str, Payee] = payee_service.by_folded(
        session,
        account.household_id,
        (
            (line.parsed or {}).get("payee_rewritten") or (line.parsed or {}).get("payee") or ""
            for line in lines
            if not (line.parsed or {}).get("payee_id")
        ),
    )
    #: Payees made by this commit. They have no history to decide a category
    #: from, so `decide` is not asked -- it could only answer None.
    made_here: set[str] = set()
    chosen_categories: dict[str, Category | None] = {}
    ensured: dict[tuple[str, str], Category] = {}
    #: Payees with rows written but not yet flushed. A category decided from a
    #: payee's history must see those, exactly as it did when every row was
    #: flushed as it was written.
    unflushed_payees: set[str] = set()

    created = absorbed = skipped = 0
    for line in lines:
        if line.id in skip_line_ids or line.outcome in {
            ImportOutcome.duplicate_skipped,
            ImportOutcome.rejected,
            ImportOutcome.skipped,
        }:
            skipped += 1
            continue

        parsed = line.parsed or {}
        when = date.fromisoformat(parsed["date"])
        amount = int(parsed["amount"])
        import_id = parsed["import_id"]

        if _already(parsed, seen_ids):
            line.outcome = ImportOutcome.duplicate_skipped
            line.reason = "this line reached the account before this import was committed"
            line.transaction_id = None
            skipped += 1
            continue

        if line.outcome is ImportOutcome.matched_existing and line.id not in reject_matches:
            twin = held.get(line.transaction_id) if line.transaction_id else None
            if twin is None:
                line.outcome = ImportOutcome.rejected
                line.reason = "the transaction this was matched to has since been deleted"
                skipped += 1
                continue
            exact = _exactly(twin, _line_keys(parsed))
            if twin.amount != amount or (
                not exact and abs((twin.date - when).days) > MATCH_WINDOW_DAYS
            ):
                # It was edited between the preview and now, so it is no longer
                # the row the preview matched. Marking it as seen by the bank
                # would make the register disagree with the statement.
                line.outcome = ImportOutcome.needs_review
                line.reason = (
                    "the transaction this matched has changed since the preview, so it was "
                    "left alone and this line was not imported"
                )
                skipped += 1
                continue
            if not _is_absorbable(twin):
                line.outcome = ImportOutcome.needs_review
                line.reason = "that transaction has already been matched to a statement line"
                skipped += 1
                continue
            # The existing row absorbs the statement line. Creating a second row
            # and linking them would double the money: every balance counts
            # rows, not links.
            if twin.import_id is not None:
                # A One-time Import's `ynab:` key, which a repeat of that
                # import recognises its row by (#264). Kept, not overwritten.
                twin.import_alt_ids = [
                    alt for alt in twin.import_alt_ids or () if alt != import_id
                ] + [twin.import_id]
            twin.import_id = import_id
            seen_ids.add(import_id)
            twin.import_payee_original = parsed.get("payee")
            twin.import_source = "file"
            if twin.cleared is ClearedState.uncleared:
                twin.cleared = ClearedState.cleared
            chosen_memo = line_memo(parsed)
            if not twin.memo and chosen_memo:
                twin.memo = chosen_memo
            absorbed += 1
            continue

        payee = None
        if parsed.get("payee_id"):
            payee_id = parsed["payee_id"]
            if payee_id not in payees_by_id:
                # Gone since staging: the same None the lookup always gave.
                payees_by_id[payee_id] = session.get(Payee, payee_id)
            payee = payees_by_id[payee_id]
        elif parsed.get("payee"):
            # What a rewrite rule made of the descriptor, where one fired, and
            # the bank's own words otherwise. Issue #58.
            name = parsed.get("payee_rewritten") or parsed["payee"]
            folded = payee_service.fold(name)
            if folded not in payees_by_name:
                payees_by_name[folded] = payee_service.add_pending(
                    session, account.household_id, name
                )
                made_here.add(payees_by_name[folded].id)
            payee = payees_by_name[folded]

        # Decided once per payee, not once per row. Two reasons, and the second
        # is the important one:
        #
        # - it is one query per distinct payee instead of one per line, which
        #   for a monthly statement is the difference between a handful and
        #   three hundred;
        # - every row of one payee in one import gets the *same* answer. Asking
        #   per row would consult a history this very loop is writing, so the
        #   fiftieth Mercadona line could be categorised differently from the
        #   first, for reasons nobody could see on the preview screen.
        # A category chosen on the preview screen wins outright. That is the
        # whole point of being able to set one: the rule is a guess and this is
        # somebody having looked at the guess and said otherwise.
        #
        # "Uncategorised" chosen there wins the same way, and is checked first:
        # it is the one answer that has to stop the hint below as well as the
        # rule, and a hint category is not created for a line that will not
        # use it. Issue #9.
        uncategorised = line_uncategorised(parsed)
        if uncategorised:
            category = None
        elif line.category_id:
            if line.category_id not in chosen_categories:
                chosen_categories[line.category_id] = session.get(Category, line.category_id)
            category = chosen_categories[line.category_id]
        elif payee is None:
            category = None
        elif payee.id in decided:
            category = decided[payee.id]
        elif payee.id in made_here:
            category = decided[payee.id] = None
        else:
            if payee.id in unflushed_payees:
                session.flush()
                unflushed_payees.clear()
            category = category_service.decide(session, payee)
            decided[payee.id] = category
        if (
            category is None
            and parsed.get("category_hint")
            and not line.category_id
            and not uncategorised
        ):
            # Nothing the household has said decides it; the bank's fixed
            # wording does -- interest, investments, fees. Created on first use,
            # inside this import's batch, so undoing the import takes it back.
            hint = parsed["category_hint"]
            wanted = (hint["name"], hint["group"])
            if wanted not in ensured:
                ensured[wanted] = category_service.ensure(
                    session, account.household_id, hint["name"], group_name=hint["group"]
                )
            category = ensured[wanted]

        txn = txn_service.create(
            session,
            account=account,
            date=when,
            amount=amount,
            payee=payee,
            category=category,
            memo=line_memo(parsed),
            cleared=ClearedState.cleared,
            import_id=import_id,
            import_payee_original=parsed.get("payee"),
            # The filename, or "pasted" -- it used to be the constant "file"
            # even for a paste, so the one column that says where a row came
            # from said the same thing about every row.
            import_source=(batch_row.source or {}).get("filename") or "file",
            # `seen_ids` above is the clash check, and the unique constraint
            # backs it; the rows are flushed together below.
            import_id_checked=True,
            flush=False,
        )
        held[txn.id] = txn
        if payee is not None:
            unflushed_payees.add(payee.id)
        line.transaction_id = txn.id
        seen_ids.add(import_id)
        if line.outcome is ImportOutcome.matched_existing:
            line.outcome = ImportOutcome.created
            line.reason = "you chose to add this as a new transaction"
        created += 1

    # The rows just written, against everything the ledger already holds: a
    # leg whose partner arrived with an earlier statement is linked now, and a
    # partner arriving later links back to these (issue #70).
    session.flush()
    written = [
        txn
        for txn in (
            (held.get(line.transaction_id) or session.get(Transaction, line.transaction_id))
            if line.transaction_id
            else None
            for line in lines
        )
        if txn is not None and txn.account_id == account.id and txn.import_id
        and txn.transfer_transaction_id is None
    ]
    transfers_linked = 0
    if written:
        # Until nothing new is strong (#88): a link this makes can be the
        # history that makes the next pair strong.
        transfers_linked = transfer_service.link_until_settled(
            session, account.household_id, among=written
        )

    summary = {
        "created": created,
        "absorbed": absorbed,
        "skipped": skipped,
        "lines": len(lines),
        "transfers_linked": transfers_linked,
    }
    batch_row.summary = {**(batch_row.source or {}).get("counts", {}), **summary}
    batch_row.finished_at = utcnow()
    session.flush()
    return summary


def get_preview(
    session: Session,
    batch_id: str,
    household_id: str,
    *,
    status: BatchStatus | None = BatchStatus.preview,
) -> Batch:
    """The staged import ``batch_id``, or a refusal.

    Every caller is a *preview* operation -- read and re-check it, edit a line,
    commit it, discard it -- so by default only a ``preview`` is handed back. An
    applied import is a finished act, read from History: re-checking one marked
    every line a duplicate of itself and cut its rows off from their origin,
    and committing one again marked it failed (issue #212). ``status=None`` is
    for the rare caller that means any import at all.
    """
    batch_row = session.get(Batch, batch_id)
    if batch_row is None or batch_row.household_id != household_id:
        raise NotFound("no such import", code="import.not_found")
    if batch_row.kind is not BatchKind.imported:
        raise ValidationError("that batch is not an import", code="import.not_an_import")
    if status is not None and batch_row.status is not status:
        raise Conflict(
            f"that import is {batch_row.status.value}, not staged",
            code="import.not_staged",
            params={"status": batch_row.status.value},
        )
    return batch_row


def open_previews(
    session: Session, household_id: str, *, limit: int = 50
) -> list[tuple[Batch, int]]:
    """Imports staged and never committed, newest first, with their row counts.

    The queue the Import screen opens on. Until this existed, staging a file and
    walking away left it reachable only by its batch id -- and the refusal to
    stage the same file twice told you it was "waiting to be reviewed" with no
    way to reach the thing it was talking about.

    One query for the batches and one for the counts, not one count per batch.
    """
    batches = list(
        session.execute(
            select(Batch)
            .where(
                Batch.household_id == household_id,
                Batch.kind == BatchKind.imported,
                Batch.status == BatchStatus.preview,
            )
            .order_by(Batch.started_at.desc())
            .limit(limit)
        ).scalars()
    )
    counts = dict(
        session.execute(
            select(ImportLine.batch_id, func.count())
            .where(ImportLine.batch_id.in_([one.id for one in batches] or [""]))
            .group_by(ImportLine.batch_id)
        ).all()
    )
    return [(one, counts.get(one.id, 0)) for one in batches]


#: How long an agent's staged-and-forgotten import sits before it is swept.
#:
#: Only an agent's, and only an uncommitted one. Issue #47 is that working out
#: the auth header left two single-row `probe` batches staged forever, and any
#: agent that experiments leaves permanent litter in the Import queue for a
#: person to deal with.
#:
#: The issue offered three answers in order of conservatism, and this is the
#: first: it expires on its own, and no key gains the ability to delete
#: anything. `tests/test_agent_access.py` enforces that ceiling structurally --
#: no route taking `current_agent` may answer DELETE -- and that rule comes
#: from §1.2 of the access-control spec rather than from convenience, so it is
#: not a thing to edit in passing.
#:
#: A person's staged import is never touched. Somebody who queues an import and
#: comes back to it in a fortnight should find it there; the whole reason this
#: is safe is that it cleans up exactly the mess an agent makes.
STAGED_AGENT_IMPORT_RETENTION = timedelta(days=7)


def sweep_stale_agent_previews(session: Session, *, now: datetime | None = None) -> int:
    """Drop agent-staged previews nobody committed. Returns how many went.

    `batches` and `import_lines` are both `__audit__ = False`, so these are
    bulk deletes in the same shape as the five in `housekeeping.sweep` above
    the agent-keys one -- and, like those, nothing in the register is touched.
    A staged import has changed no ledger row, which is what makes deleting it
    honest rather than a hole in "hard deletes only, and the before-image is
    how a row comes back": there is no before-image because there was no after.

    Lines first: the foreign key would cascade in SQLite, but the order is
    written out rather than relied on.
    """
    cutoff = (now or utcnow()) - STAGED_AGENT_IMPORT_RETENTION
    doomed = [
        row.id
        for row in session.execute(
            select(Batch.id).where(
                Batch.status == BatchStatus.preview,
                # An agent's, and only an agent's. `agent_key_id` is what makes
                # that checkable, and it is null for everything a person did.
                Batch.agent_key_id.is_not(None),
                Batch.started_at < cutoff,
            )
        ).all()
    ]
    if not doomed:
        return 0

    session.execute(  # audit-exempt: import_lines is not audited
        delete(ImportLine).where(ImportLine.batch_id.in_(doomed))
    )
    gone = session.execute(  # audit-exempt: batches is not audited
        delete(Batch).where(Batch.id.in_(doomed))
    ).rowcount or 0
    session.flush()
    return gone


def discard_preview(session: Session, batch_row: Batch) -> None:
    """Throw a staged import away, lines and all.

    Only a `preview`: a committed import is put back from History, which
    reverses what it wrote. Deleting the batch of one would take the audit log's
    account of it with it.

    A hard delete, as everything here is -- and it is honest for this one row in
    a way it is not for the ledger, because a staged import has changed nothing
    to keep a before-image of. Neither `batches` nor `import_lines` is audited,
    so no batch is opened for it.

    The lines are deleted through the ORM one at a time rather than in one
    statement: the `ondelete="CASCADE"` on the foreign key would do it in
    SQLite, but a bulk delete is the shortcut this codebase has closed off, and
    a few hundred loaded rows is not worth an exception to it.
    """
    if batch_row.status is not BatchStatus.preview:
        raise Conflict(
            f"that import is {batch_row.status.value}, not staged. A committed import is "
            "put back from History rather than purged.",
            code="import.committed_not_purged",
            params={"status": batch_row.status.value},
        )

    for line in session.execute(
        select(ImportLine).where(ImportLine.batch_id == batch_row.id)
    ).scalars():
        session.delete(line)
    session.delete(batch_row)
    session.flush()


@dataclass(slots=True)
class LineCategory:
    """What a staged line will be categorised as, and whether anybody said so."""

    category_id: str | None
    name: str | None
    #: True when a person chose it on the preview screen, false when it is what
    #: the payee's rule would pick. The screen says which, because "likely" and
    #: "decided" are different promises and only one of them is worth checking.
    chosen: bool
    #: True when the choice was "no category" (issue #9). `chosen` is true
    #: then too, and `category_id` null -- which on its own would read the same
    #: as a rule with nothing to go on.
    uncategorised: bool = False


def preview_categories(
    session: Session, household_id: str, lines: list[ImportLine]
) -> dict[str, LineCategory]:
    """What each staged line would land in, keyed by line id.

    Derived on every read rather than stored at staging time. A payee's history
    can change between the upload and the commit -- you might go and correct
    three transactions in the register precisely *because* the preview showed
    you the wrong guess -- and a figure frozen at staging would still be showing
    the old answer while the commit used the new one.

    Only the *choice* is stored, because only the choice is a deliberate act.
    """
    from . import categories as category_service

    # By today's fold of the name, not the stored key, which a payee made
    # before #268 may still carry in its old form.
    payees = payee_service.keyed(
        session.execute(select(Payee).where(Payee.household_id == household_id)).scalars()
    )
    names = {
        row[0]: f"{row[1]}: {row[2]}"
        for row in session.execute(
            select(Category.id, CategoryGroup.name, Category.name)
            .join(CategoryGroup, CategoryGroup.id == Category.group_id)
            .where(Category.household_id == household_id)
        ).all()
    }

    # One decision per payee rather than per line, for the same two reasons the
    # commit does it: it is one query instead of hundreds, and every line of a
    # payee shows the same answer as every other.
    decided: dict[str, str | None] = {}
    out: dict[str, LineCategory] = {}

    for line in lines:
        if line_uncategorised(line.parsed):
            # Before the rule is asked, not after: the answer would be thrown
            # away, and asking costs a query per payee.
            out[line.id] = LineCategory(
                category_id=None, name=None, chosen=True, uncategorised=True
            )
            continue
        if line.category_id:
            out[line.id] = LineCategory(
                category_id=line.category_id,
                name=names.get(line.category_id),
                chosen=True,
            )
            continue

        parsed = line.parsed or {}
        payee = None
        if parsed.get("payee_id"):
            payee = session.get(Payee, parsed["payee_id"])
        elif parsed.get("payee"):
            # Looked up, never created: a preview that is abandoned should not
            # leave payees behind for statements that were never imported.
            payee = payees.get(payee_service.fold(str(parsed["payee"])))

        chosen_id = None
        if payee is not None:
            if payee.id not in decided:
                guess = category_service.decide(session, payee)
                decided[payee.id] = guess.id if guess else None
            chosen_id = decided[payee.id]
        if chosen_id is None and parsed.get("category_hint"):
            # What the commit will file it under when nothing else decides. The
            # category may not exist yet -- the commit makes it -- so the name is
            # shown and the id is left for the commit to settle.
            hint = parsed["category_hint"]
            out[line.id] = LineCategory(
                category_id=None, name=f"{hint['group']}: {hint['name']}", chosen=False
            )
            continue
        out[line.id] = LineCategory(
            category_id=chosen_id, name=names.get(chosen_id or ""), chosen=False
        )

    return out


#: Where "this line is uncategorised, on purpose" lives. Issue #9.
#:
#: `category_id` being null already means "let the rule decide", so it cannot
#: also mean "nothing at all" -- and without a third state, a payee's usual
#: category or the bank's `category_hint` won every time somebody emptied the
#: cell. Kept in `parsed`, the way the memo override is, so it needs no
#: migration and survives a reload and a preview reopened from the queue.
UNCATEGORISED_KEY = "category_uncategorised"


def line_uncategorised(parsed: dict | None) -> bool:
    """True when somebody said this line has no category, rule or no rule."""
    return bool((parsed or {}).get(UNCATEGORISED_KEY))


def set_line_category(
    session: Session,
    line: ImportLine,
    category: Category | None,
    *,
    uncategorised: bool = False,
) -> ImportLine:
    """Choose a category for one staged line, or hand it back to the rule.

    ``uncategorised`` is the third answer: no category, and neither the payee's
    rule nor the bank's wording is asked. Each of the three replaces the other
    two, so a line never carries a chosen category *and* the override, and
    which of them wins is never a question the commit has to settle.
    """
    # Reassigned rather than mutated, for the reason `set_line_memo` gives.
    parsed = dict(line.parsed or {})
    if uncategorised:
        parsed[UNCATEGORISED_KEY] = True
        line.category_id = None
    else:
        parsed.pop(UNCATEGORISED_KEY, None)
        line.category_id = category.id if category else None
    line.parsed = parsed
    return line


#: Where a memo typed on the preview screen lives, beside -- never over -- the
#: one the bank sent. `parsed["memo"]` stays exactly as it was parsed, so the
#: line detail can always show what arrived and what somebody made of it.
MEMO_OVERRIDE_KEY = "memo_chosen"


def line_memo(parsed: dict | None) -> str | None:
    """The memo a committed row should carry: the typed one if there is one.

    Presence of the key is the decision, not its truthiness -- typing a memo and
    then emptying it is "this row has no memo", which is a different request
    from "use whatever the bank wrote".
    """
    parsed = parsed or {}
    if MEMO_OVERRIDE_KEY in parsed:
        return parsed[MEMO_OVERRIDE_KEY] or None
    return parsed.get("memo") or None


def set_line_memo(
    session: Session, line: ImportLine, memo: str | None, *, clear: bool = False
) -> ImportLine:
    """Type a memo for one staged line, or hand it back to the bank's.

    Stored on the line, like the category and for the same reason: it survives a
    reload, and a preview reopened from the queue tomorrow still has it.

    ``clear`` drops the override. Sending an empty ``memo`` instead keeps the
    override and makes it empty, which is how a row gets *no* memo when the bank
    sent one.
    """
    # Reassigned rather than mutated: `parsed` is a plain JSON column, not a
    # MutableDict, so changing the dict in place is a change SQLAlchemy never
    # sees and the flush writes nothing.
    parsed = dict(line.parsed or {})
    if clear:
        parsed.pop(MEMO_OVERRIDE_KEY, None)
    else:
        cleaned = " ".join((memo or "").split())
        parsed[MEMO_OVERRIDE_KEY] = cleaned or None
    line.parsed = parsed
    return line


def _line_payee_key(line: ImportLine) -> str | None:
    """What identifies "the same payee" across lines of one file.

    The resolved payee when a rule matched one, otherwise the bank's own text
    folded. Folded because a statement writes the same shop three ways --
    `MERCADONA 1234`, `Mercadona 1234`, `MERCADONA  1234` -- and a propagation
    that missed those would leave exactly the lines the user was trying to
    avoid retyping.
    """
    parsed = line.parsed or {}
    if parsed.get("payee_id"):
        return f"id:{parsed['payee_id']}"
    if parsed.get("payee"):
        return f"name:{payee_service.fold(str(parsed['payee']))}"
    return None


def similar_lines(
    session: Session, batch_id: str, line: ImportLine
) -> list[ImportLine]:
    """Other lines in this import with the same payee and no choice of their own.

    The offer this backs is "you just corrected one of these -- there are eleven
    more". Lines that already carry a chosen category are left out: somebody
    decided those individually, and an offer that would quietly overwrite a
    decision is not an offer. A line marked uncategorised on purpose is a
    decision in exactly the same sense, so it is left out too.
    """
    key = _line_payee_key(line)
    if key is None:
        return []

    others = session.execute(
        select(ImportLine).where(
            ImportLine.batch_id == batch_id,
            ImportLine.id != line.id,
            ImportLine.category_id.is_(None),
        )
    ).scalars()

    return [
        other
        for other in others
        # Only lines that would actually write something. A duplicate or a
        # rejected line is not going to become a transaction, so changing its
        # category would be offering to do nothing.
        if other.outcome in (ImportOutcome.created, ImportOutcome.matched_existing)
        and not line_uncategorised(other.parsed)
        and _line_payee_key(other) == key
    ]


def apply_to_similar(
    session: Session, batch_id: str, line: ImportLine
) -> list[ImportLine]:
    """Give this line's category to the rest of its payee in this import.

    "Uncategorised, on purpose" spreads the same way a category does: a rule
    that is wrong for one Corner Shop line is usually wrong for all of them.
    """
    uncategorised = line_uncategorised(line.parsed)
    if not line.category_id and not uncategorised:
        raise ValidationError(
            "that line has no category of its own to apply", code="import.line_no_own_category"
        )

    category = session.get(Category, line.category_id) if line.category_id else None
    changed = similar_lines(session, batch_id, line)
    for other in changed:
        set_line_category(session, other, category, uncategorised=uncategorised)
    session.flush()
    return changed
