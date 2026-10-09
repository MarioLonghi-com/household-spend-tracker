"""The one-time import itself: analyse a source, then preview or commit a plan (#183).

Generic over the app it came from. A workflow hands over a :class:`Source`;
this maps its accounts and categories, finds what is already in the ledger,
pairs transfers, and writes the rest -- **in one batch**, so a single undo in
History takes the whole import back out, the accounts, categories and payees
it created included.

Stateless, like the accounts import (#146). The preview runs the real code
inside the batch and throws the transaction away, so what it reports is the
verdict of the code that will commit, not a second copy of its rules.

**Partial, not all-or-none.** A history of ten thousand rows with one bad date
should arrive with one row missing and a sentence saying which. So every row
is checked before anything is written -- amount, date, account, range,
duplicate -- and a service refusal while writing one row is caught for that
row, as is a refusal the database makes when the rows are flushed (#235).
What cannot be partial is the plan itself: an account that cannot be
created, or a mapping that points at another household, refuses the lot.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from statements import build_import_id

from ...audit.batch import batch
from ...errors import Conflict, DomainError, ValidationError
from ...models import (
    Account,
    AccountIdentifier,
    AccountType,
    Batch,
    BatchKind,
    BatchStatus,
    Category,
    CategoryGroup,
    Change,
    ChangeOp,
    ClearedState,
    Household,
    LinkSource,
    Payee,
    Transaction,
)
from ...money import MAX_MINOR, MIN_MINOR, MoneyError, format_amount, from_milliunits, parse_exact
from .. import accounts as account_service
from .. import categories as category_service
from .. import payees as payee_service
from .. import transactions as transaction_service
from .. import transfers as transfer_service
from .model import Source, SourceRow
from .ynab_source import DATE_FORMATS, FIXED_UNCATEGORISED, normalise_amount, parse_date

log = logging.getLogger(__name__)

#: A bank settles a card payment a day or two after YNAB recorded it.
DUPLICATE_WINDOW_DAYS = 3

#: Name similarity at or above which a suggestion is pre-selected.
ACCOUNT_SCORE = 0.6
CATEGORY_SCORE = 0.75

DEFAULT_GROUP = "Imported from YNAB"

#: Rows written per savepoint (#235). A refusal the database makes at flush
#: -- a constraint, not a service check -- costs the rows of its chunk a
#: second, one-at-a-time write, never the whole import.
CHUNK_ROWS = 500

#: The constraint two requests importing the same rows at once collide on.
_IMPORT_ID_CLASH = "transactions.account_id, transactions.import_id"

#: What the second of two simultaneous commits is told (#235).
TAKEN = (
    "some of these rows were imported by another request a moment ago; "
    "open the Import screen and check History before trying again"
)

#: What marks a batch as one of these, and what `import_id` starts with.
MARK = "one_time_import"
IMPORT_ID_PREFIX = "ynab:"

#: What each workflow is called where a person reads it: the panel that says
#: where a row came from, and the Import screen's list of earlier imports.
#: Keyed like `WORKFLOWS` and like `Batch.source[MARK]`.
WORKFLOW_NAMES = {"ynab": "YNAB"}


def fold(name: str) -> str:
    """The payees' fold, not a copy of it: accents, dash style and invisible
    spaces count for a YNAB name exactly as they do for a statement's (#268)."""
    return payee_service.fold(name)


def _closest_category(name: str, folded_live: list[tuple[str, dict]]) -> tuple[float, dict | None]:
    """The live category whose name scores highest against ``name``.

    What ``max(score(name, each))`` answered -- the first of the best, on the
    rounded score -- without a full `ratio()` for every pair (#240): a plan of
    150 source categories against 200 live ones was 30,000 of them on every
    wizard step. `real_quick_ratio` and `quick_ratio` are upper bounds on
    `ratio`, so a category whose bound cannot beat the best so far, or cannot
    reach `CATEGORY_SCORE` at all, is passed over unscored. Not
    `difflib.get_close_matches`: it cuts off on the unrounded ratio and breaks
    ties by name, so it would change which category is suggested.
    """
    matcher = difflib.SequenceMatcher(None, fold(name), "")
    best: tuple[float, dict | None] = (0.0, None)
    for folded, one in folded_live:
        floor = max(best[0], CATEGORY_SCORE) if best[1] is not None else CATEGORY_SCORE
        matcher.set_seq2(folded)
        if round(matcher.real_quick_ratio(), 2) < floor or round(matcher.quick_ratio(), 2) < floor:
            continue
        value = round(matcher.ratio(), 2)
        if best[1] is None or value > best[0]:
            best = (value, one)
    return best


def score(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, fold(a), fold(b)).ratio(), 2)


# --------------------------------------------------------------------------- #
# Reading a row's amount and date
# --------------------------------------------------------------------------- #


def amount_of(row: SourceRow, currency: str, source: Source) -> int:
    """Inflow minus outflow, in minor units, exactly -- or a MoneyError saying why."""
    if row.milliunits is not None:
        value = from_milliunits(row.milliunits, currency)
    else:
        outflow, inflow = row.amount_text or ("", "")
        value = 0
        for text, sign in ((inflow, 1), (outflow, -1)):
            if not text.strip():
                continue
            plain = normalise_amount(text, decimal_comma=source.decimal_comma)
            value += sign * parse_exact(plain, currency)
    if not MIN_MINOR <= value <= MAX_MINOR:
        raise MoneyError("that amount is too large to record as money", code="money.amount_too_large")
    return value


def date_of(row: SourceRow, fmt: str) -> date:
    try:
        return parse_date(row.date_text, fmt)
    except (ValueError, KeyError):
        raise ValidationError(
            f"{row.date_text!r} is not a {fmt} date",
            code="ynab.date_unreadable",
            params={"date": row.date_text, "format": fmt},
        ) from None


# --------------------------------------------------------------------------- #
# Analyse
# --------------------------------------------------------------------------- #


def _category_key(row: SourceRow) -> str:
    return row.category


def is_fixed(category: str) -> bool:
    return not category or category in FIXED_UNCATEGORISED


def analyse(
    session: Session,
    household: Household,
    source: Source,
    *,
    currency: str | None = None,
    date_format: str | None = None,
) -> dict[str, Any]:
    """Everything the wizard shows before a person has decided anything."""
    code = (currency or source.currency or household.base_currency).upper()
    formats = source.date_formats
    fmt = date_format if date_format in DATE_FORMATS else (formats[0] if formats else "YYYY-MM-DD")

    parsed: list[tuple[SourceRow, int | None, date | None]] = []
    for row in source.rows:
        try:
            amount = amount_of(row, code, source)
        except DomainError:
            amount = None
        try:
            when = date_of(row, fmt)
        except DomainError:
            when = None
        parsed.append((row, amount, when))

    dates = [when for _, _, when in parsed if when is not None]
    cleared = Counter(row.cleared for row in source.rows)
    flags = Counter(row.flag for row in source.rows if row.flag)

    per_account: dict[str, list[tuple[SourceRow, int | None, date | None]]] = defaultdict(list)
    for item in parsed:
        per_account[item[0].account_key].append(item)
    per_category: Counter[str] = Counter(_category_key(row) for row in source.rows if row.category)

    targets = _targets(session, household, code)
    eligible = [one for one in targets["accounts"] if one["eligible"]]
    suggested = _suggest_accounts(source, eligible, targets["accounts"])

    accounts_out = []
    for key, account in source.accounts.items():
        items = per_account.get(key, [])
        when = [w for _, _, w in items if w is not None]
        accounts_out.append(
            {
                "key": key,
                "name": account.name,
                "rows": len(items),
                "date_min": min(when) if when else None,
                "date_max": max(when) if when else None,
                "balance_minor": sum(a for _, a, _ in items if a is not None),
                "type_hint": account.type_hint.value if account.type_hint else None,
                "closed": account.closed,
                "suggestion": suggested[key],
            }
        )

    live_categories = [one for one in targets["categories"] if not one["archived"]]
    folded_live = [(fold(one["name"]), one) for one in live_categories]
    categories_out = []
    for name, rows in sorted(per_category.items(), key=lambda kv: fold(kv[0])):
        fixed = is_fixed(name)
        suggestion: dict[str, Any]
        if fixed:
            suggestion = {"kind": "uncategorised"}
        else:
            best = _closest_category(name, folded_live)
            if best[1] is not None and best[0] >= CATEGORY_SCORE:
                suggestion = {"kind": "existing", "category_id": best[1]["id"], "score": best[0]}
            else:
                suggestion = {"kind": "create", "name": name}
        categories_out.append(
            {
                "key": name,
                "name": name,
                "groups": source.category_groups.get(name, []),
                "rows": rows,
                "fixed_uncategorised": fixed,
                "suggestion": suggestion,
            }
        )

    return {
        "source": {"via": source.via, "filename": source.filename, "plan_name": source.plan_name},
        "currency": {
            "detected": source.currency,
            "symbol": source.symbol,
            "confirmed_needed": source.currency_confirmed_needed,
        },
        "date_format": {
            "detected": formats[0] if formats else None,
            "ambiguous": len(formats) > 1,
            "options": formats or list(DATE_FORMATS),
        },
        "totals": {
            "rows": len(source.rows),
            "date_min": min(dates) if dates else None,
            "date_max": max(dates) if dates else None,
            "transfers": sum(1 for row in source.rows if row.transfer_account_key),
            "splits": sum(1 for row in source.rows if row.split),
            "starting_balance_rows": sum(1 for row in source.rows if row.starting_balance),
            "cleared": {
                "reconciled": cleared.get("reconciled", 0),
                "cleared": cleared.get("cleared", 0),
                "uncleared": sum(n for k, n in cleared.items() if k not in ("reconciled", "cleared")),
            },
        },
        "flags": [{"label": label, "count": n} for label, n in flags.most_common()],
        "accounts": accounts_out,
        "categories": categories_out,
        "targets": targets,
        "previous_imports": previous_imports(session, household),
    }


def _mask(value: str) -> str:
    compact = "".join(ch for ch in value if ch.isalnum())
    return "…" + compact[-4:] if len(compact) > 4 else "…" + compact


def _targets(session: Session, household: Household, currency: str) -> dict[str, list[dict]]:
    accounts = account_service.list_for_household(session, household.id, include_closed=True)
    balances = account_service.balances_for_household(session, household.id)
    activity = account_service.activity_for_household(session, household.id)
    identifiers: dict[str, list[str]] = defaultdict(list)
    for ident in session.execute(
        select(AccountIdentifier).where(AccountIdentifier.household_id == household.id)
    ).scalars():
        if ident.account_id and ident.kind.value in ("iban", "number", "card"):
            identifiers[ident.account_id].append(_mask(ident.value))
    out_accounts = []
    for account in accounts:
        seen = activity.get(account.id)
        balance = balances.get(account.id)
        out_accounts.append(
            {
                "id": account.id,
                "name": account.name,
                "type": AccountType(account.type).value,
                "currency": account.currency,
                "institution": account.institution,
                "identifiers_masked": identifiers.get(account.id, []),
                "closed": account.closed,
                "balance_minor": _balance_figure(balance),
                "txn_count": seen.transactions if seen else 0,
                "first_date": seen.oldest if seen else None,
                "last_date": seen.newest if seen else None,
                "eligible": account.currency == currency,
            }
        )
    groups = {group.id: group.name for group in category_service.list_groups(session, household.id)}
    out_categories = [
        {
            "id": category.id,
            "name": category.name,
            "group_name": groups.get(category.group_id),
            "archived": category.archived,
        }
        for category in category_service.list_categories(session, household.id, include_archived=True)
    ]
    return {"accounts": out_accounts, "categories": out_categories}


def _balance_figure(balance: Any) -> int:
    if balance is None:
        return 0
    if isinstance(balance, dict):
        return int(balance.get("balance", 0))
    return int(getattr(balance, "balance", balance))


def _suggest_accounts(
    source: Source, eligible: list[dict], every: list[dict]
) -> dict[str, dict[str, Any]]:
    """One-to-one: the closest pairs first, and each target used once."""
    pairs = sorted(
        (
            (score(account.name, target["name"]), key, target)
            for key, account in source.accounts.items()
            for target in eligible
        ),
        key=lambda one: (-one[0], one[1], one[2]["name"]),
    )
    taken_keys: set[str] = set()
    taken_targets: set[str] = set()
    out: dict[str, dict[str, Any]] = {}
    for value, key, target in pairs:
        if value < ACCOUNT_SCORE or key in taken_keys or target["id"] in taken_targets:
            continue
        out[key] = {"kind": "existing", "account_id": target["id"], "score": value}
        taken_keys.add(key)
        taken_targets.add(target["id"])
    names = {fold(one["name"]) for one in every}
    for key, account in source.accounts.items():
        if key in out:
            continue
        name = account.name
        if fold(name) in names:
            name = f"{name} (YNAB)"
        names.add(fold(name))
        out[key] = {"kind": "create", "name": name, "type": _type_for(account.type_hint, account.name).value}
    return out


def _type_for(hint: AccountType | None, name: str) -> AccountType:
    if hint is not None:
        return hint
    words = fold(name)
    if any(word in words for word in ("card", "credit", "amex", "visa", "mastercard")):
        return AccountType.credit_card
    if any(word in words for word in ("saving", "isa", "pot", "deposit")):
        return AccountType.savings
    if "cash" in words:
        return AccountType.cash
    return AccountType.checking


def previous_imports(
    session: Session, household: Household, workflow: str | None = "ynab"
) -> list[dict[str, Any]]:
    """Earlier one-time imports here, newest first, undone ones included.

    For the repeat-run warning (one workflow's), and with ``workflow=None``
    for the Import screen, which asks whether any has ever been done.
    """
    mark = Batch.source[MARK].as_string()
    rows = session.execute(
        select(Batch)
        .where(
            Batch.household_id == household.id,
            Batch.kind == BatchKind.imported,
            mark.is_not(None) if workflow is None else mark == workflow,
            Batch.status.in_([BatchStatus.applied, BatchStatus.undone]),
        )
        .order_by(Batch.started_at.desc())
    ).scalars()
    return [
        {
            "batch_id": one.id,
            "at": one.started_at,
            "workflow": (one.source or {}).get(MARK),
            "via": (one.source or {}).get("via"),
            "filename": (one.source or {}).get("filename"),
            "plan_name": (one.source or {}).get("plan_name"),
            "status": one.status.value,
        }
        for one in rows
    ]


def batch_that_created(session: Session, transaction_id: str) -> Batch | None:
    """The one-time import that wrote this row, or None when none did.

    Read from the audit log rather than from a column on the row: the batch's
    insert of the row is the record, and its ``source`` keeps the file name or
    the plan's name, which the row itself does not carry.
    """
    return session.execute(
        select(Batch)
        .join(Change, Change.batch_id == Batch.id)
        .where(
            Change.table_name == "transactions",
            Change.row_id == transaction_id,
            Change.op == ChangeOp.insert,
            Batch.kind == BatchKind.imported,
            Batch.source[MARK].as_string().is_not(None),
        )
        .order_by(Change.seq.desc())
        .limit(1)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Plan:
    currency: str
    date_format: str
    accounts: dict[str, dict[str, Any]]
    categories: dict[str, dict[str, Any]]
    flags: str = "memo"
    starting_balance: str = "import"
    date_from: date | None = None
    date_to: date | None = None
    acknowledge_cleared_reset: bool = False
    duplicates_all: str | None = None
    duplicates_import: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Existing:
    """A row already in the ledger, as much of it as duplicate finding and the
    report need -- read as columns, not as an audited object (#240)."""

    id: str
    date: date
    amount: int
    created_at: datetime
    payee_name: str | None


@dataclass(slots=True)
class _Row:
    """One incoming row and what is to become of it."""

    src: SourceRow
    amount: int | None = None
    when: date | None = None
    target_key: str | None = None
    fate: str = "import"  # import | skipped | failed | duplicate
    reason: str = ""
    counted: str = ""
    duplicate_of: _Existing | None = None
    decision: str = ""
    partner: _Row | None = None
    unpaired_reason: str = ""
    txn: Transaction | None = None
    #: The statement key of the bank line YNAB imported this row from, when
    #: this row may carry it -- see `_exact_keys` (#264).
    exact_key: str | None = None


class _Discard(Exception):
    """Raised inside the batch to throw a preview's transaction away."""


def _check_plan(session: Session, household: Household, source: Source, plan: Plan) -> dict[str, Account]:
    """The plan's refusals, all before anything is written. Returns key -> existing account."""
    if plan.date_format not in DATE_FORMATS:
        raise ValidationError(
            f"{plan.date_format!r} is not a date format this import reads",
            code="ynab.date_format_unknown",
            params={"format": plan.date_format},
        )
    if len(plan.currency) != 3 or not plan.currency.isalpha():
        raise ValidationError(
            f"{plan.currency!r} is not a three-letter currency code",
            code="ynab.currency_not_a_code",
            params={"currency": plan.currency},
        )
    if plan.flags not in ("memo", "ignore"):
        raise ValidationError("flags is either 'memo' or 'ignore'", code="ynab.flags_choice")
    if plan.starting_balance not in ("import", "skip"):
        raise ValidationError(
            "starting_balance is either 'import' or 'skip'", code="ynab.starting_balance_choice"
        )
    if plan.date_from and plan.date_to and plan.date_from > plan.date_to:
        raise ValidationError("the date range ends before it starts", code="ynab.range_backwards")

    existing: dict[str, Account] = {}
    used: dict[str, str] = {}
    for key, account in source.accounts.items():
        choice = plan.accounts.get(key)
        if choice is None:
            raise ValidationError(
                f"say what to do with the YNAB account {account.name!r}",
                code="ynab.account_undecided",
                params={"account": account.name},
            )
        kind = choice.get("kind")
        if kind == "existing":
            target = session.execute(
                select(Account).where(
                    Account.id == str(choice.get("account_id") or ""),
                    Account.household_id == household.id,
                )
            ).scalar_one_or_none()
            if target is None:
                raise ValidationError(
                    f"the account chosen for {account.name!r} is not in this household",
                    code="ynab.account_elsewhere",
                    params={"account": account.name},
                )
            if target.currency != plan.currency:
                raise ValidationError(
                    f"{target.name!r} is in {target.currency}, and this plan is in {plan.currency}",
                    code="ynab.account_currency",
                    params={"account": target.name, "account_currency": target.currency, "currency": plan.currency},
                )
            if target.id in used:
                raise ValidationError(
                    f"{used[target.id]!r} and {account.name!r} both go to {target.name!r}; "
                    "each YNAB account needs an account of its own",
                    code="ynab.account_twice",
                    params={"first": used[target.id], "second": account.name, "account": target.name},
                )
            used[target.id] = account.name
            existing[key] = target
        elif kind == "create":
            if not str(choice.get("name") or "").strip():
                raise ValidationError(
                    f"the new account for {account.name!r} needs a name",
                    code="ynab.new_account_needs_name",
                    params={"account": account.name},
                )
            try:
                AccountType(choice.get("type") or "checking")
            except ValueError:
                raise ValidationError(
                    f"{choice.get('type')!r} is not an account type",
                    code="ynab.account_type_unknown",
                    params={"type": str(choice.get("type"))},
                ) from None
        elif kind != "skip":
            raise ValidationError(
                f"{kind!r} is not something an account can be mapped to",
                code="ynab.account_mapping_unknown",
                params={"kind": str(kind)},
            )

    for key in {row.category for row in source.rows if row.category}:
        choice = plan.categories.get(key)
        if choice is None:
            if is_fixed(key):
                continue
            raise ValidationError(
                f"say what to do with the YNAB category {key!r}",
                code="ynab.category_undecided",
                params={"category": key},
            )
        kind = choice.get("kind")
        if kind == "existing":
            found = session.execute(
                select(Category.id).where(
                    Category.id == str(choice.get("category_id") or ""),
                    Category.household_id == household.id,
                )
            ).scalar_one_or_none()
            if found is None:
                raise ValidationError(
                    f"the category chosen for {key!r} is not in this household",
                    code="ynab.category_elsewhere",
                    params={"category": key},
                )
        elif kind == "create":
            if not str(choice.get("name") or "").strip():
                raise ValidationError(
                    f"the new category for {key!r} needs a name",
                    code="ynab.new_category_needs_name",
                    params={"category": key},
                )
        elif kind != "uncategorised":
            raise ValidationError(
                f"{kind!r} is not something a category can be mapped to",
                code="ynab.category_mapping_unknown",
                params={"kind": str(kind)},
            )
    return existing


def _memo(row: SourceRow, plan: Plan) -> str | None:
    memo = row.memo.strip()
    if plan.flags == "memo" and row.flag:
        memo = memo + (" " if memo else "") + f"Flag: {row.flag}"
    return memo or None


def run(
    session: Session,
    household: Household,
    *,
    actor_id: str,
    source: Source,
    plan: Plan,
    commit: bool,
) -> dict[str, Any]:
    """Do the whole import in one batch, and keep it or throw it away."""
    if commit and not plan.acknowledge_cleared_reset:
        raise ValidationError(
            "confirm that YNAB's reconciled and cleared states are reset: everything arrives uncleared",
            code="ynab.confirm_states",
        )
    existing_targets = _check_plan(session, household, source, plan)
    rows = [_Row(src=one) for one in source.rows]
    _triage(rows, source, plan)
    _find_duplicates(session, rows, existing_targets, plan, commit=commit)
    _pair_transfers(rows, source)
    _exact_keys(rows, plan)

    # Whatever the request already did (a session touch) is its own business.
    session.commit()

    counts = {
        "rows_in_file": len(rows),
        "imported": 0,
        "transfers_linked": 0,
        "duplicates_skipped": 0,
        "duplicates_imported": 0,
        "skipped_account": 0,
        "skipped_date_range": 0,
        "skipped_starting_balance": 0,
        "failed": 0,
    }
    created: dict[str, Any] = {"accounts": [], "categories": [], "payees": 0}
    batch_id: str | None = None
    rule_suggestions: int | None = None
    detail: dict[str, Any] = {MARK: "ynab", "via": source.via}
    if source.filename is not None:
        detail["filename"] = source.filename
    if source.plan_name is not None:
        detail["plan_name"] = source.plan_name
    if source.sha256 is not None:
        detail["sha256"] = source.sha256

    try:
        with batch(
            session,
            kind=BatchKind.imported,
            actor_id=actor_id,
            household_id=household.id,
            source=detail,
            own_transaction=False,
        ) as opened:
            batch_id = opened.id
            payees_before = _payee_count(session, household.id)
            categories = _resolve_categories(session, household, source, plan, created)
            targets = _resolve_accounts(session, household, source, plan, rows, existing_targets, created)
            payees = _write(session, household, source, plan, rows, targets, categories)
            _link(session, household, rows, payees)
            session.flush()
            created["payees"] = _payee_count(session, household.id) - payees_before
            _count(rows, counts)
            opened.summary = dict(counts)
            if not commit:
                raise _Discard
    except _Discard:
        batch_id = None
    except IntegrityError as exc:
        # Not a DomainError, so it used to be a 500 with the whole batch
        # rolled back and nothing said. The one a person can cause is a second
        # commit of the same plan -- a double click, a retry after a proxy
        # timeout -- racing the first. #235.
        if _IMPORT_ID_CLASH in str(exc.orig):
            raise Conflict(TAKEN, code="ynab.rows_taken") from exc
        raise
    else:
        session.commit()
        # What the household's bank text now adds up to -- this import's and
        # whatever statements brought before it (#269). The Rules screen's
        # own query, once per commit: one grouped read. Not on a preview,
        # whose rows are already gone.
        #
        # The import is saved by now, so nothing here may turn it into an
        # error: a 500 would invite a retry, and a retry finds the rows
        # already imported. A count that fails is a count left out.
        try:
            rule_suggestions = len(
                payee_service.suggest_rules(session, household.id, limit=_EVERY_SUGGESTION)
            )
        except Exception:
            session.rollback()
            log.exception("rule suggestions not counted after one-time import %s", batch_id)

    names = {key: account.name for key, account in source.accounts.items()}
    differences, unchecked = _balance_differences(rows, source, plan, _balance_scope(source, plan))
    return _report(
        rows, counts, created, names, rule_suggestions=rule_suggestions, batch_id=batch_id,
        committed=commit, differences=differences, unchecked=unchecked,
    )


def _triage(rows: list[_Row], source: Source, plan: Plan) -> None:
    """Read every amount and date, and set aside what will not be imported."""
    for row in rows:
        choice = plan.accounts.get(row.src.account_key) or {"kind": "skip"}
        if choice.get("kind") == "skip":
            row.fate, row.counted, row.reason = "skipped", "skipped_account", "its account is skipped"
            _read_quietly(row, source, plan)
            continue
        try:
            row.amount = amount_of(row.src, plan.currency, source)
            row.when = date_of(row.src, plan.date_format)
        except DomainError as exc:
            row.fate, row.counted, row.reason = "failed", "failed", str(exc)
            continue
        if (plan.date_from and row.when < plan.date_from) or (plan.date_to and row.when > plan.date_to):
            row.fate, row.counted, row.reason = "skipped", "skipped_date_range", "outside the date range"
        elif row.src.starting_balance and plan.starting_balance == "skip":
            row.fate, row.counted, row.reason = (
                "skipped", "skipped_starting_balance", "a YNAB starting balance, skipped"
            )
        else:
            row.target_key = row.src.account_key


def _read_quietly(row: _Row, source: Source, plan: Plan) -> None:
    """The amount and date of a row that is not being imported, for the report."""
    with contextlib.suppress(DomainError):
        row.amount = amount_of(row.src, plan.currency, source)
    with contextlib.suppress(DomainError):
        row.when = date_of(row.src, plan.date_format)


def _find_duplicates(
    session: Session,
    rows: list[_Row],
    existing: dict[str, Account],
    plan: Plan,
    *,
    commit: bool,
) -> None:
    """Rows the ledger already holds: by import id first, then by amount and date.

    Only in accounts that already exist -- a created account has nothing in
    it. Each ledger row can stand for one incoming row, and the nearest date
    wins, then the earlier entry.
    """
    live = [row for row in rows if row.fate == "import" and row.target_key in existing]
    by_account: dict[str, list[_Row]] = defaultdict(list)
    for row in live:
        by_account[existing[row.target_key].id].append(row)

    window = timedelta(days=DUPLICATE_WINDOW_DAYS)
    # Read as columns, the payee's name joined in (#240). A repeat run over a
    # whole history used to load every row of the account as an audited
    # object -- ~25 MiB at 10k rows -- to compare dates and amounts.
    for account_id, incoming in by_account.items():
        dates = [row.when for row in incoming]
        ledger = [
            _Existing(id=one[0], date=one[1], amount=one[2], created_at=one[3], payee_name=one[4])
            for one in session.execute(
                select(
                    Transaction.id,
                    Transaction.date,
                    Transaction.amount,
                    Transaction.created_at,
                    Payee.name,
                )
                .outerjoin(Payee, Payee.id == Transaction.payee_id)
                .where(
                    Transaction.account_id == account_id,
                    Transaction.date >= min(dates) - window,
                    Transaction.date <= max(dates) + window,
                )
            ).all()
        ]
        # Not from the windowed read: a row an earlier run brought in keeps its
        # import_id when someone later moves its date, and missing it here
        # meant `uq_transactions_account_import` refusing the whole import at
        # flush instead of one row being counted as already imported.
        by_import_id = dict(
            session.execute(
                select(Transaction.import_id, Transaction.id).where(
                    Transaction.account_id == account_id,
                    Transaction.import_id.startswith(IMPORT_ID_PREFIX, autoescape=True),
                )
            ).all()
        )
        # And a row a statement line has since absorbed: its `import_id` is
        # the statement's now, and its own key is kept beside it (#264).
        for txn_id, alt_ids in session.execute(
            select(Transaction.id, Transaction.import_alt_ids).where(
                Transaction.account_id == account_id, Transaction.import_alt_ids.is_not(None)
            )
        ).all():
            for alt in alt_ids or ():
                if isinstance(alt, str) and alt.startswith(IMPORT_ID_PREFIX):
                    by_import_id.setdefault(alt, txn_id)
        by_amount: dict[int, list[_Existing]] = defaultdict(list)
        for one in ledger:
            by_amount[one.amount].append(one)
        claimed: set[str] = set()
        for row in incoming:
            # Under its own id, or under the one an older build gave it (#267).
            again = next(
                (
                    found
                    for one in _import_ids_of(row.src)
                    if (found := by_import_id.get(one)) is not None and found not in claimed
                ),
                None,
            )
            if again is not None:
                # Skipped outright, with no decision to offer, so the report
                # never lists it as a duplicate and needs nothing of the row.
                claimed.add(again)
                row.fate, row.counted = "skipped", "duplicates_skipped"
                row.reason = "already imported by an earlier one-time import"
                continue
        for row in incoming:
            if row.fate != "import":
                continue
            candidates = [
                one for one in by_amount.get(row.amount, ())
                if abs((one.date - row.when).days) <= window.days and one.id not in claimed
            ]
            if not candidates:
                continue
            twin = min(candidates, key=lambda t: (abs((t.date - row.when).days), t.date, t.created_at))
            claimed.add(twin.id)
            row.duplicate_of = twin
            if plan.duplicates_all == "import" or row.src.ref in plan.duplicates_import:
                row.decision = "import"
                row.counted = "duplicates_imported"
            else:
                row.decision = "skip" if (commit or plan.duplicates_all == "skip") else "pending"
                row.fate, row.counted = "duplicate", "duplicates_skipped"
                row.reason = "looks like a row already in the ledger"


def _pair_transfers(rows: list[_Row], source: Source) -> None:
    """Both legs importing: they become one linked transfer. Otherwise: plain rows.

    The API says which row is the other leg. The file only says which account,
    so the mirror is found by opposite amount on the same date in that account,
    in file order, each row used once.
    """
    live = [row for row in rows if row.fate == "import" and row.src.transfer_account_key]
    by_ref = {row.src.ref: row for row in live}
    pool: dict[tuple[str, date, int], list[_Row]] = defaultdict(list)
    for row in live:
        pool[(row.src.account_key, row.when, row.amount)].append(row)

    for row in live:
        if row.partner is not None:
            continue
        other: _Row | None = None
        if row.src.transfer_ref is not None:
            other = by_ref.get(row.src.transfer_ref)
        else:
            for candidate in pool.get((row.src.transfer_account_key, row.when, -row.amount), ()):
                if (
                    candidate is not row
                    and candidate.partner is None
                    and candidate.src.transfer_account_key == row.src.account_key
                ):
                    other = candidate
                    break
        if (
            other is not None
            and other.partner is None
            and other is not row
            and row.amount != 0
            and other.target_key != row.target_key
        ):
            row.partner, other.partner = other, row
            # `other` may have looked first and found nobody pointing back.
            row.unpaired_reason = other.unpaired_reason = ""
        else:
            row.unpaired_reason = (
                "a YNAB transfer whose other side is skipped, out of range or not in the "
                "export, so it was imported as an ordinary transaction"
            )


def import_id_for(row: SourceRow) -> str:
    return (IMPORT_ID_PREFIX + row.ref)[:64]


def _import_ids_of(row: SourceRow) -> list[str]:
    """The id this row is written under, then any an older build wrote it under."""
    return [import_id_for(row), *((IMPORT_ID_PREFIX + alt)[:64] for alt in row.alt_refs)]


def statement_key_for(source_import_id: str | None, currency: str) -> str | None:
    """YNAB's id for a bank line, in the shape of our statement key -- or None."""
    line = _bank_line(source_import_id, currency)
    return build_import_id(*line) if line is not None else None


def _bank_line(source_import_id: str | None, currency: str) -> tuple[int, date, int] | None:
    """``(minor, date, nth)`` of the bank line YNAB's id names, or None.

    YNAB's is ``YNAB:<milliunits>:<date>:<occurrence>``, counted from 1;
    ours is ``ST:<minor>:<date>:<nth>`` (`statements.build_import_id`),
    counted from 0. Same line, same day, same count of identical lines before
    it, so a statement of the account YNAB read from builds the same key for
    the same line. Anything that is not exactly that shape, or an amount
    that is not a whole number of the currency's minor units, is None: a key
    that is nearly right would match the wrong row exactly.
    """
    parts = (source_import_id or "").split(":")
    if len(parts) != 4 or parts[0] != "YNAB":
        return None
    _, milli_text, date_text, nth_text = parts
    if not (milli_text.lstrip("-").isdigit() and milli_text.isascii()):
        return None
    if not (nth_text.isdigit() and nth_text.isascii()) or int(nth_text) < 1:
        return None
    try:
        when = date.fromisoformat(date_text)
        minor = from_milliunits(int(milli_text), currency)
    except (ValueError, MoneyError):
        return None
    return minor, when, int(nth_text) - 1


def _exact_keys(rows: list[_Row], plan: Plan) -> None:
    """Which rows carry the statement key of the bank line they came from (#264).

    Only a row whose amount *is* the bank line's. YNAB gives every part of a
    split its parent's id, and a part is not the line the bank sent: keyed,
    the statement's line for the whole amount would be offered a part of it.
    So a split's parts take none -- unless one part is the whole amount,
    which is then the line in all but name. One row per key in an account,
    the first in source order, so two never claim one bank line.
    """
    taken: set[tuple[str | None, str]] = set()
    for row in rows:
        if row.fate != "import" or row.amount is None:
            continue
        line = _bank_line(row.src.source_import_id, plan.currency)
        if line is None or line[0] != row.amount:
            continue
        key = build_import_id(*line)
        if (row.target_key, key) in taken:
            continue
        taken.add((row.target_key, key))
        row.exact_key = key


def _payee_count(session: Session, household_id: str) -> int:
    return session.execute(
        select(func.count()).select_from(Payee).where(Payee.household_id == household_id)
    ).scalar_one()


def _resolve_categories(
    session: Session, household: Household, source: Source, plan: Plan, created: dict[str, Any]
) -> dict[str, Category | None]:
    out: dict[str, Category | None] = {}
    made: dict[str, Category] = {}
    groups: dict[str, CategoryGroup] = {}
    for key in sorted({row.category for row in source.rows if row.category}, key=fold):
        choice = plan.categories.get(key) or {"kind": "uncategorised"}
        kind = choice.get("kind")
        if is_fixed(key) and kind != "existing":
            out[key] = None
        elif kind == "existing":
            out[key] = session.get(Category, choice["category_id"])
        elif kind == "create":
            name = str(choice["name"]).strip()
            if fold(name) in made:
                out[key] = made[fold(name)]
                continue
            found = category_service._category_named(session, household.id, name)
            if found is None:
                group_name = str(choice.get("group_name") or DEFAULT_GROUP).strip() or DEFAULT_GROUP
                group = groups.get(fold(group_name)) or category_service._group_named(
                    session, household.id, group_name
                )
                if group is None:
                    group = category_service.create_group(session, household.id, group_name)
                groups[fold(group_name)] = group
                found = category_service.create_category(
                    session, household.id, group_id=group.id, name=name
                )
                created["categories"].append({"id": found.id, "name": found.name})
            made[fold(name)] = found
            out[key] = found
        else:
            out[key] = None
    return out


def _resolve_accounts(
    session: Session,
    household: Household,
    source: Source,
    plan: Plan,
    rows: list[_Row],
    existing: dict[str, Account],
    created: dict[str, Any],
) -> dict[str, Account]:
    out = dict(existing)
    earliest: dict[str, date] = {}
    for row in rows:
        if row.target_key is not None and row.when is not None and row.fate in ("import", "duplicate"):
            earliest[row.target_key] = min(earliest.get(row.target_key, row.when), row.when)
    for key, account in source.accounts.items():
        choice = plan.accounts.get(key) or {}
        if choice.get("kind") != "create":
            continue
        opened_on = earliest[key] - timedelta(days=1) if key in earliest else date.today()
        try:
            made = account_service.create_account(
                session,
                household=household,
                name=str(choice["name"]).strip(),
                type=AccountType(choice.get("type") or "checking"),
                currency=plan.currency,
                opening_balance=0,
                opening_date=min(opened_on, date.today()),
            )
        except DomainError as exc:
            raise type(exc)(f"the new account for {account.name!r}: {exc}") from None
        out[key] = made
        created["accounts"].append({"id": made.id, "name": made.name, "opening_date": min(opened_on, date.today())})
    return out


@dataclass
class _Payees:
    """Every payee this import may need, read once (#236).

    `by_name` starts as the household's payees for every name in the source,
    from one query, and gains the ones this import makes -- added pending,
    with no flush each. `transfer` is each account's transfer payee, looked up
    once per account rather than once per row and once per linked leg.
    """

    by_name: dict[str, Payee]
    transfer: dict[str, Payee] = field(default_factory=dict)


def _payee_for(
    session: Session,
    household: Household,
    row: _Row,
    targets: dict[str, Account],
    payees: _Payees,
) -> Payee | None:
    text = " ".join(row.src.payee.split())[:200]
    if not text:
        return None
    if row.src.transfer_account_key is not None:
        # A transfer imported as a plain row still names an account. When
        # that account is here, its own transfer payee is the right one --
        # and making a plain payee of the same name would collide with it
        # the day that account's transfer payee is made.
        other = targets.get(row.src.transfer_account_key)
        if other is not None:
            return _transfer_payee(session, household, other, payees)
        for account in targets.values():
            if fold(text) == fold(f"Transfer : {account.name}"):
                return _transfer_payee(session, household, account, payees)
    folded = fold(text)
    if folded not in payees.by_name:
        payees.by_name[folded] = payee_service.add_pending(session, household.id, text)
    return payees.by_name[folded]


def _transfer_payee(session: Session, household: Household, account: Account, payees: _Payees) -> Payee:
    if account.id not in payees.transfer:
        payees.transfer[account.id] = payee_service.transfer_payee(session, household.id, account)
    return payees.transfer[account.id]


def _write(
    session: Session,
    household: Household,
    source: Source,
    plan: Plan,
    rows: list[_Row],
    targets: dict[str, Account],
    categories: dict[str, Category | None],
) -> _Payees:
    """Write the rows, `CHUNK_ROWS` to a savepoint.

    A service refusal is caught for its row as it is written. A refusal the
    database makes only shows at flush, where it used to take every row with
    it (#235): now it rolls back its savepoint, and that chunk is written
    again one row at a time so only the row the database refused is failed.
    Unless the refusal is that another request has just imported these same
    rows -- that is not one bad row but the whole import done twice, and it is
    refused whole with a 409.
    """
    todo = [row for row in rows if row.fate == "import"]
    cache = _Payees(
        by_name=payee_service.by_folded(
            session, household.id, (" ".join(row.src.payee.split())[:200] for row in todo)
        )
    )
    written: set[str] = set()
    for start in range(0, len(todo), CHUNK_ROWS):
        chunk = todo[start : start + CHUNK_ROWS]
        try:
            with session.begin_nested():
                for row in chunk:
                    _write_one(session, household, source, plan, row, targets, categories, cache)
                session.flush()
        except IntegrityError as exc:
            _forget_rolled_back(chunk, cache)
            if _IMPORT_ID_CLASH in str(exc.orig) and _taken_elsewhere(session, chunk, targets, written):
                raise Conflict(TAKEN, code="ynab.rows_taken") from exc
            for row in chunk:
                if row.fate != "import":
                    continue
                try:
                    with session.begin_nested():
                        _write_one(session, household, source, plan, row, targets, categories, cache)
                        session.flush()
                except IntegrityError as one:
                    _forget_rolled_back([row], cache)
                    _fail(row, f"the database refused this row: {_constraint(one)}")
        written.update(import_id_for(row.src) for row in chunk if row.txn is not None)
    return cache


def _write_one(
    session: Session,
    household: Household,
    source: Source,
    plan: Plan,
    row: _Row,
    targets: dict[str, Account],
    categories: dict[str, Category | None],
    cache: _Payees,
) -> None:
    if row.fate != "import":
        return
    account = targets[row.target_key]
    try:
        paired = row.partner is not None
        payee = None if paired else _payee_for(session, household, row, targets, cache)
        category = None if (paired or row.src.transfer_account_key) else categories.get(row.src.category)
        row.txn = transaction_service.create(
            session,
            account=account,
            date=row.when,
            amount=row.amount,
            payee=payee,
            category=category,
            memo=_memo(row.src, plan),
            cleared=ClearedState.uncleared,
            import_id=import_id_for(row.src),
            # The bank's text when the source kept it, and otherwise nothing:
            # the clean name is the payee, and `suggest_rules` reads this
            # column as what a bank sends (#265).
            import_payee_original=(row.src.payee_original or "")[:300] or None,
            import_source=source.import_source,
            import_alt_ids=[row.exact_key] if row.exact_key else None,
            import_id_checked=True,
            flush=False,
        )
    except DomainError as exc:
        _fail(row, str(exc))


def _fail(row: _Row, reason: str) -> None:
    row.txn = None
    row.fate, row.counted, row.reason = "failed", "failed", reason
    if row.partner is not None:
        row.partner.partner = None
        row.partner.unpaired_reason = "the other side of this transfer failed to import"
        row.partner = None


def _forget_rolled_back(chunk: list[_Row], cache: _Payees) -> None:
    """A rolled-back savepoint took its rows and any payee it made with it."""
    for row in chunk:
        row.txn = None
    for known in (cache.by_name, cache.transfer):
        for key, payee in list(known.items()):
            if not inspect(payee).persistent:
                del known[key]


def _taken_elsewhere(
    session: Session, chunk: list[_Row], targets: dict[str, Account], written: set[str]
) -> bool:
    """Whether another request has committed any of these rows' import ids."""
    wanted = {import_id_for(row.src) for row in chunk if row.fate == "import"} - written
    accounts = {targets[row.target_key].id for row in chunk if row.fate == "import"}
    if not wanted:
        return False
    return (
        session.execute(
            select(Transaction.id)
            .where(Transaction.account_id.in_(accounts), Transaction.import_id.in_(wanted))
            .limit(1)
        ).first()
        is not None
    )


def _constraint(exc: IntegrityError) -> str:
    return str(exc.orig).split("\n", 1)[0]


def _link(session: Session, household: Household, rows: list[_Row], payees: _Payees) -> None:
    """Link every pair, with what `link` reads per pair read once for all (#236)."""
    done: set[int] = set()
    pairs: list[tuple[_Row, _Row]] = []
    for row in rows:
        other = row.partner
        if row.txn is None or other is None or other.txn is None or id(row) in done:
            continue
        done.update({id(row), id(other)})
        pairs.append((row, other))
    if not pairs:
        return
    known = transfer_service.prefetch(
        session, household.id, [(row.txn, other.txn) for row, other in pairs], payees.transfer
    )
    for row, other in pairs:
        try:
            transfer_service.link(session, row.txn, other.txn, source=LinkSource.named, known=known)
        except DomainError as exc:
            for leg in (row, other):
                leg.partner = None
                leg.unpaired_reason = f"not linked as a transfer: {exc}"


#: What a row may be counted as and still be part of the account's total in
#: the other app: left out on purpose, not lost (#266).
_LEFT_OUT_ON_PURPOSE = frozenset({"skipped_date_range", "skipped_starting_balance", "duplicates_skipped"})


def _balance_scope(source: Source, plan: Plan) -> set[str]:
    """The source accounts whose total is checked against the other app's (#266).

    Every account the source gives a balance for -- YNAB's API does, its
    Register.csv does not -- that this import writes into, created or
    existing. The check adds up the other app's rows, never the ledger's, so
    an account's own history does not move the answer: a row the ledger
    already holds is a duplicate skipped, and counted in. A skipped account
    is not checked, because nothing of it arrives.
    """
    return {
        key
        for key, account in source.accounts.items()
        if account.balance_milliunits is not None
        and (plan.accounts.get(key) or {}).get("kind") in ("create", "existing")
    }


def _balance_differences(
    rows: list[_Row], source: Source, plan: Plan, scope: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Each checked account whose rows do not add up to the other app's balance,
    and each one whose balance could not be checked at all.

    What adds up is what this import wrote plus what it left out on purpose:
    starting balances skipped, rows outside the date range, duplicates
    skipped. A row that failed is the one thing not counted, so a difference
    points at it -- a balance needs no full date range to be checked.
    """
    accounted: dict[str, int] = defaultdict(int)
    for row in rows:
        key = row.src.account_key
        if key not in scope or row.amount is None:
            continue
        written = row.fate == "import" and row.txn is not None
        if written or row.counted in _LEFT_OUT_ON_PURPOSE:
            accounted[key] += row.amount

    out: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    for key, account in source.accounts.items():
        if key not in scope:
            continue
        try:
            expected = from_milliunits(account.balance_milliunits, plan.currency)
        except MoneyError as exc:
            # Thousandths the currency cannot hold: there is no exact figure
            # to compare, and a rounded one would invent a difference. Said,
            # not skipped, so silence still means "it adds up".
            unchecked.append(
                {
                    "account_key": key,
                    "account": account.name,
                    "reason": str(exc),
                    "sentence": f"{account.name}: YNAB's balance could not be checked: {exc}.",
                }
            )
            continue
        got = accounted.get(key, 0)
        if got == expected:
            continue
        gap = got - expected
        out.append(
            {
                "account_key": key,
                "account": account.name,
                "currency": plan.currency,
                "ynab_balance_minor": expected,
                "imported_minor": got,
                "difference_minor": gap,
                "sentence": (
                    f"{account.name}: YNAB's balance is {format_amount(expected, plan.currency)}, "
                    f"and this import accounts for {format_amount(got, plan.currency)}, "
                    f"{format_amount(abs(gap), plan.currency)} {'more' if gap > 0 else 'less'}."
                ),
            }
        )
    return out, unchecked


def _count(rows: list[_Row], counts: dict[str, int]) -> None:
    for row in rows:
        if row.counted:
            counts[row.counted] += 1
        if row.fate == "import" and row.txn is not None:
            counts["imported"] += 1
            if row.partner is not None and row.partner.txn is not None:
                counts["transfers_linked"] += 1
    # Two legs, one transfer.
    counts["transfers_linked"] //= 2


#: ``suggest_rules`` keeps the largest ``SUGGESTIONS_LISTED`` for the Rules screen; the report
#: counts every group, so its figure is never a cap passed off as a total.
_EVERY_SUGGESTION = 1_000_000

#: Said after a committed import that brought no bank text and left no rule
#: suggestions: a ``Register.csv``, or a plan typed into YNAB by hand (#269).
NO_BANK_TEXT_YET = (
    "Rules can be suggested after the first statement import: "
    "this import brought no bank text to suggest them from."
)


def rule_suggestions_sentence(groups: int) -> str:
    """The report's line about the suggestions waiting on the Rules screen (#269)."""
    if groups == 1:
        return "1 group of bank strings looks like one payee; make a rule?"
    # The Rules screen lists the largest few and does not say "20 of N", so
    # the report says it for the screen.
    listed = payee_service.SUGGESTIONS_LISTED
    cap = f" (the {listed} largest are listed)" if groups > listed else ""
    return f"{groups} groups of bank strings look like one payee each{cap}; make rules?"


def _report(
    rows: list[_Row],
    counts: dict[str, int],
    created: dict[str, Any],
    names: dict[str, str],
    *,
    rule_suggestions: int | None = None,
    batch_id: str | None,
    committed: bool,
    differences: list[dict[str, Any]] | None = None,
    unchecked: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    differences = differences or []
    unchecked = unchecked or []
    duplicates = []
    not_imported = []
    unpaired = []
    for row in rows:
        base = {
            "row_ref": row.src.ref,
            "date": row.when,
            "date_text": row.src.date_text,
            "account": names.get(row.src.account_key, row.src.account_key),
            "payee": row.src.payee,
            "category": row.src.category,
            "memo": row.src.memo,
            "amount_minor": row.amount,
        }
        twin = row.duplicate_of
        if twin is not None and row.decision:
            duplicates.append(
                {
                    **{k: base[k] for k in ("row_ref", "date", "account", "payee", "amount_minor", "memo")},
                    "existing": {
                        "id": twin.id,
                        "date": twin.date,
                        "payee": twin.payee_name,
                        "amount_minor": twin.amount,
                    },
                    "decision": row.decision,
                }
            )
        if row.fate != "import" or row.txn is None:
            if row.fate == "import":  # pragma: no cover - a failure sets the fate
                continue
            not_imported.append({**base, "reason": row.reason})
        elif row.unpaired_reason and row.src.transfer_account_key:
            unpaired.append({**base, "reason": row.unpaired_reason})
    # Source lines -- not rows -- that brought the bank's own words, which the
    # Payee Naming Rules screen turns into rule suggestions (#265). A split is
    # one bank line however many parts it was cut into, so it counts once.
    bank_text_rows = len(
        {
            row.src.parent_ref or row.src.ref
            for row in rows
            if row.fate == "import" and row.txn is not None and row.src.payee_original
        }
    )
    return {
        "committed": committed and batch_id is not None,
        "batch_id": batch_id,
        "counts": counts,
        "created": created,
        "duplicates": duplicates,
        "not_imported": not_imported,
        "unpaired_transfers": unpaired,
        "rule_suggestions": rule_suggestions,
        "bank_text_rows": bank_text_rows,
        "balance_differences": differences,
        "balance_unchecked": unchecked,
        "report_text": _report_text(
            counts, created, not_imported, duplicates, unpaired, bank_text_rows, differences, unchecked,
            rule_suggestions,
        ),
    }


def _money(minor: int | None) -> str:
    if minor is None:
        return "?"
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor)}"


def _report_text(
    counts: dict[str, int],
    created: dict[str, Any],
    not_imported: list[dict],
    duplicates: list[dict],
    unpaired: list[dict],
    bank_text_rows: int = 0,
    differences: list[dict[str, Any]] | None = None,
    unchecked: list[dict[str, Any]] | None = None,
    rule_suggestions: int | None = None,
) -> str:
    lines = ["One-time Import from YNAB", ""]
    for key, value in counts.items():
        lines.append(f"{key.replace('_', ' ')}: {value}")
    lines.append("")
    if differences:
        lines.append("Accounts that do not add up to YNAB's balance:")
        lines.extend(f"  {one['sentence']}" for one in differences)
        lines.append("")
    if unchecked:
        lines.append("Accounts whose balance could not be checked against YNAB's:")
        lines.extend(f"  {one['sentence']}" for one in unchecked)
        lines.append("")
    if bank_text_rows:
        lines.append(
            f"Bank text kept for {bank_text_rows} "
            f"{'transaction' if bank_text_rows == 1 else 'transactions'}: "
            "Payee Naming Rules may now suggest rules from it."
        )
        lines.append("")
    if rule_suggestions:
        lines.append(f"{rule_suggestions_sentence(rule_suggestions)} See Payee Naming Rules.")
        lines.append("")
    elif rule_suggestions == 0 and not bank_text_rows:
        lines.append(NO_BANK_TEXT_YET)
        lines.append("")
    if created["accounts"]:
        lines.append("Accounts created (fix each one's opening balance and date):")
        lines.extend(f"  {one['name']} (opened {one['opening_date']})" for one in created["accounts"])
        lines.append("")
    if created["categories"]:
        lines.append("Categories created:")
        lines.extend(f"  {one['name']}" for one in created["categories"])
        lines.append("")
    lines.append(f"Payees created: {created['payees']}")
    lines.append("")

    def row_line(one: dict) -> str:
        return "\t".join(
            str(part)
            for part in (
                one.get("date") or one.get("date_text") or "",
                one.get("account") or "",
                one.get("payee") or "",
                one.get("category") or "",
                one.get("memo") or "",
                _money(one.get("amount_minor")),
            )
        )

    lines.append("Not imported (date, account, payee, category, memo, amount in minor units, reason):")
    lines.extend(row_line(one) + "\t" + one["reason"] for one in not_imported)
    if not not_imported:
        lines.append("  (none)")
    lines.append("")
    lines.append("Duplicates of rows already in the ledger (decision):")
    lines.extend(row_line(one) + "\t" + one["decision"] for one in duplicates)
    if not duplicates:
        lines.append("  (none)")
    lines.append("")
    lines.append("Transfers imported as ordinary transactions:")
    lines.extend(row_line(one) + "\t" + one["reason"] for one in unpaired)
    if not unpaired:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"
