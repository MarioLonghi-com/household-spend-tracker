"""YNAB, read into a :class:`~.model.Source` -- from its export, or its API (#183).

The export is a zip holding two CSVs. Only the **Register** is transactions;
the **Plan** is YNAB's month-by-month budget, which this app does not have and
will not import. So a zip is searched for its Register and the Plan ignored,
and a Plan on its own is refused with the sentence that says why.

The Register's columns, from a real export (values withheld):
``Account, Flag, Date, Payee, Category Group/Category, Category Group,
Category, Memo, Outflow, Inflow, Cleared``. Amounts carry the plan's currency
symbol and exactly one of Outflow/Inflow is non-zero; dates are in whatever
format the YNAB user chose, which is why they stay text until a person has
confirmed the format.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import zipfile
from collections import Counter
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, StrictInt, TypeAdapter
from pydantic import ValidationError as SchemaError

from statements import sniffing

from ...errors import ValidationError
from ...models import AccountType
from ...money import _SYMBOLS
from . import ynab_api
from .model import Source, SourceAccount, SourceRow

log = logging.getLogger(__name__)

PLAN_NOT_REGISTER = "Only the YNAB Register.csv is needed, not the Plan.csv"

#: The export of a decade is a couple of megabytes; this is a guard against the
#: wrong file, not a size YNAB produces.
MAX_FILE_BYTES = 32 * 1024 * 1024
#: What a zip may unpack to. Checked from the zip's own directory before a
#: byte is inflated, so a small archive cannot claim a lot of memory.
MAX_REGISTER_BYTES = 64 * 1024 * 1024

READY_TO_ASSIGN = "Inflow: Ready to Assign"
UNCATEGORISED = "Uncategorized"
#: YNAB's own buckets, which have no meaning outside a budget: always "no
#: category" here. The older name for Ready to Assign is still in old exports.
FIXED_UNCATEGORISED = frozenset({READY_TO_ASSIGN, UNCATEGORISED, "Inflow: To be Budgeted"})

STARTING_BALANCE = "Starting Balance"
TRANSFER_PREFIX = "Transfer : "
_SPLIT = re.compile(r"^Split \(\d+/\d+\)")

#: Every date format YNAB offers, as the person sees it, and how to read it.
DATE_FORMATS: dict[str, str] = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY/MM/DD": "%Y/%m/%d",
    "DD.MM.YYYY": "%d.%m.%Y",
    "DD-MM-YYYY": "%d-%m-%Y",
    "MM-DD-YYYY": "%m-%d-%Y",
    "YYYY.MM.DD": "%Y.%m.%d",
}

#: YNAB's account types, and the one here that behaves the same way.
API_ACCOUNT_TYPES: dict[str, AccountType] = {
    "checking": AccountType.checking,
    "savings": AccountType.savings,
    "cash": AccountType.cash,
    "creditCard": AccountType.credit_card,
    "lineOfCredit": AccountType.other_liability,
    "otherAsset": AccountType.other_asset,
    "otherLiability": AccountType.other_liability,
    "mortgage": AccountType.other_liability,
    "autoLoan": AccountType.other_liability,
    "studentLoan": AccountType.other_liability,
    "personalLoan": AccountType.other_liability,
    "medicalDebt": AccountType.other_liability,
    "otherDebt": AccountType.other_liability,
}

#: Longer than any amount YNAB writes ("-£1,234,567,890.12" is 18). Every
#: later regex on an amount cell runs on at most this much (#220).
MAX_AMOUNT_CHARS = 64

_REGISTER_COLUMNS = ("account", "date", "payee", "outflow", "inflow")
_PLAN_COLUMNS = ("month", "assigned", "budgeted", "available", "activity")


def parse_date(text: str, fmt: str) -> date:
    return datetime.strptime(text.strip(), DATE_FORMATS[fmt]).date()


def _line_digest(line: int, raw: str) -> str:
    """The id a build before #267 gave a row: its line number and its text.

    Every row below an inserted one moved, so a re-export recognised nothing.
    Still computed, as the row's alternative id, so a ledger imported by that
    build is recognised on a repeat run of the same file.
    """
    return hashlib.sha256(f"{line}\x00{raw}".encode()).hexdigest()[:32]


def _content_digest(cells: tuple[str, ...], occurrence: int) -> str:
    """A row's id from what it says, and which of its identical twins it is.

    Account, date text, outflow, inflow, payee, category and memo: not the
    line, which moves when YNAB exports a row added earlier, and not cleared
    or flag, which a person changes after the import. The amounts are their
    figures (:func:`_figure`), not their text, so a plan re-exported with a
    different currency format keeps its ids. ``occurrence`` counts the rows
    with exactly those cells so far in the file, so two identical bus fares
    on one day stay two rows.
    """
    body = "\x00".join(("content", *cells, str(occurrence)))
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def _figure(text: str, *, decimal_comma: bool) -> str:
    """An amount cell as the number it is, whatever format wrote it.

    ``"£2.50"``, ``"2.50 £"`` and ``"2,50 £"`` (in a decimal-comma file) are
    all ``"2.5"``; an empty cell and ``"£0.00"`` are both ``"0"``. Two cells
    give the same figure exactly when they are the same number of minor
    units in any currency, which is what an id needs while the currency is
    still unconfirmed and its exponent unknown. Digits only, never a float.
    A cell that is not a number keeps its text; that row fails on import
    whatever its id.
    """
    plain = normalise_amount(text, decimal_comma=decimal_comma) if text.strip() else "0"
    negative = plain.startswith("-")
    whole, point, fraction = plain.lstrip("-").partition(".")
    if not whole.isdigit() or (point and not fraction.isdigit()):
        return text
    whole, fraction = whole.lstrip("0") or "0", fraction.rstrip("0")
    figure = whole + ("." + fraction if fraction else "")
    return "-" + figure if negative and figure != "0" else figure


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #


def from_file(raw: bytes, filename: str | None) -> Source:
    """A zip or a Register.csv, read. Anything else is refused with a reason."""
    register, name = _register_bytes(raw, filename)
    text, _encoding = sniffing.decode(register)
    text = text.lstrip("﻿")
    source = _read_register(text)
    source.filename = name
    source.sha256 = hashlib.sha256(raw).hexdigest()
    return source


def _register_bytes(raw: bytes, filename: str | None) -> tuple[bytes, str | None]:
    if not raw.startswith(b"PK\x03\x04"):
        return raw, filename
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValidationError("that zip file cannot be opened") from None
    with archive:
        registers = [
            info for info in archive.infolist()
            if not info.is_dir() and info.filename.lower().endswith("register.csv")
        ]
        if not registers:
            raise ValidationError(
                f"{PLAN_NOT_REGISTER}, and this zip has no Register.csv in it"
            )
        info = registers[0]
        if info.file_size > MAX_REGISTER_BYTES:
            raise ValidationError("the Register.csv in that zip is larger than this import reads")
        with archive.open(info) as handle:
            body = handle.read(MAX_REGISTER_BYTES + 1)
        if len(body) > MAX_REGISTER_BYTES:
            raise ValidationError("the Register.csv in that zip is larger than this import reads")
        return body, filename or info.filename.rsplit("/", 1)[-1]


def _key(cell: str) -> str:
    return " ".join(cell.strip().lower().split())


def _read_register(text: str) -> Source:
    reader = csv.reader(io.StringIO(text))
    lines = text.splitlines()
    header: dict[str, int] | None = None
    rows: list[SourceRow] = []
    accounts: dict[str, SourceAccount] = {}
    groups: dict[str, list[str]] = {}
    amounts: list[str] = []
    dates: list[str] = []
    contents: list[tuple[str, ...]] = []
    last = 0
    try:
        for cells in reader:
            start, last = last + 1, reader.line_num
            if not any(cell.strip() for cell in cells):
                continue
            if header is None:
                header = _header(cells)
                continue
            raw_line = "\n".join(lines[start - 1 : last])

            def cell(name: str, cells=cells, header=header) -> str:
                at = header.get(name)
                return cells[at].strip() if at is not None and at < len(cells) else ""

            account = cell("account")
            if not account:
                continue
            payee = cell("payee")
            group = cell("category group")
            category = cell("category")
            combined = cell("category group/category")
            if not category and combined:
                category = combined.split(":", 1)[-1].strip() if ":" in combined else combined
            if group == "Inflow" or combined.startswith("Inflow:"):
                # One key for YNAB's income bucket, however the export spells it.
                category = READY_TO_ASSIGN
            memo = cell("memo")
            outflow, inflow = cell("outflow"), cell("inflow")
            if len(outflow) > MAX_AMOUNT_CHARS or len(inflow) > MAX_AMOUNT_CHARS:
                raise ValidationError(
                    f"line {start} of this file has an amount longer than "
                    f"{MAX_AMOUNT_CHARS} characters, which YNAB never writes -- "
                    "it may not be a YNAB export, or it may be damaged"
                )
            amounts.extend(value for value in (outflow, inflow) if value)
            dates.append(cell("date"))
            transfer = payee[len(TRANSFER_PREFIX):].strip() if payee.startswith(TRANSFER_PREFIX) else None
            contents.append((account, cell("date"), outflow, inflow, payee, category, memo))
            rows.append(
                SourceRow(
                    ref="",  # from what the row says, once the decimal mark is known
                    alt_refs=(_line_digest(start, raw_line),),
                    line=start,
                    account_key=account,
                    date_text=cell("date"),
                    payee=payee,
                    category=category,
                    group=group,
                    memo=memo,
                    amount_text=(outflow, inflow),
                    cleared=(cell("cleared") or "uncleared").lower(),
                    flag=cell("flag"),
                    transfer_account_key=transfer or None,
                    split=bool(_SPLIT.match(memo)),
                    starting_balance=payee == STARTING_BALANCE,
                )
            )
            accounts.setdefault(account, SourceAccount(key=account, name=account))
            if category and group and group not in groups.setdefault(category, []):
                groups[category].append(group)
    except csv.Error:
        # What csv said stays in the log (#224); the sentence names the line.
        log.debug("csv could not read the register", exc_info=True)
        raise ValidationError(
            f"line {reader.line_num} of this file cannot be read as CSV. Export the plan "
            "from YNAB again and upload the zip or its Register.csv."
        ) from None

    if header is None or not rows:
        raise ValidationError("there are no transactions in this file")

    currency, symbol = _currency_of(amounts)
    decimal_comma = _decimal_comma(amounts)
    seen: Counter[tuple[str, ...]] = Counter()
    for row, (account, when, outflow, inflow, payee, category, memo) in zip(rows, contents, strict=True):
        content = (
            account,
            when,
            _figure(outflow, decimal_comma=decimal_comma),
            _figure(inflow, decimal_comma=decimal_comma),
            payee,
            category,
            memo,
        )
        row.ref = _content_digest(content, seen[content])
        seen[content] += 1
    return Source(
        via="csv",
        rows=rows,
        accounts=accounts,
        currency=currency,
        symbol=symbol,
        currency_confirmed_needed=True,
        date_formats=_date_formats(dates),
        decimal_comma=decimal_comma,
        category_groups=groups,
        import_source="one-time-import:ynab-csv",
        headline="One-time Import · YNAB (CSV)",
    )


def _header(cells: list[str]) -> dict[str, int]:
    keys = [_key(cell).lstrip("﻿") for cell in cells]
    if any(key in _PLAN_COLUMNS for key in keys) and "account" not in keys:
        raise ValidationError(PLAN_NOT_REGISTER)
    missing = [name for name in _REGISTER_COLUMNS if name not in keys]
    if missing or ("category" not in keys and "category group/category" not in keys):
        raise ValidationError(
            "this is not a YNAB Register.csv: it has no "
            + ", ".join(repr(m) for m in missing or ["category"])
            + " column. Export the plan from YNAB and upload the zip or its Register.csv."
        )
    return {key: at for at, key in enumerate(keys) if key}


# --------------------------------------------------------------------------- #
# What the text says about currency, decimals and dates
# --------------------------------------------------------------------------- #

#: Symbol -> code, for the symbols only one currency uses. "$" is in here as
#: USD because YNAB writes it for every dollar; the person confirms it.
_BY_SYMBOL: dict[str, str] = {}
for _code, _symbol in _SYMBOLS.items():
    if list(_SYMBOLS.values()).count(_symbol) == 1:
        _BY_SYMBOL[_symbol] = _code
_BY_SYMBOL["$"] = "USD"
_BY_SYMBOL["¥"] = "JPY"


def symbol_of(text: str) -> str:
    """What is left of an amount once the number is taken out of it."""
    return re.sub(r"[\d.,\s'  +\-()]", "", text)


def _currency_of(amounts: list[str]) -> tuple[str | None, str | None]:
    seen = Counter(symbol_of(value) for value in amounts)
    seen.pop("", None)
    if not seen:
        return None, None
    symbol = seen.most_common(1)[0][0]
    code = _BY_SYMBOL.get(symbol) or (symbol.upper() if re.fullmatch(r"[A-Za-z]{3}", symbol) else None)
    return code, symbol


def _digit_tail(value: str) -> str:
    """``value`` up to its last digit: ``"£1,234.56 "`` -> ``"£1,234.56"``.

    A loop, not a ``\\D+$`` substitution: that pattern restarts at every
    non-digit and runs to the end each time, so a long run of them is
    quadratic -- 40,000 characters took five seconds of a CPU worker (#220).
    """
    end = len(value)
    while end and not value[end - 1].isdigit():
        end -= 1
    return value[:end]


def _decimal_comma(amounts: list[str]) -> bool:
    """True when the amounts are written 1.234,56 rather than 1,234.56."""
    tails = [_digit_tail(value) for value in amounts]
    comma = sum(1 for tail in tails if re.search(r",\d{2}$", tail))
    point = sum(1 for tail in tails if re.search(r"\.\d{2}$", tail))
    return comma > point


def _date_formats(dates: list[str]) -> list[str]:
    """Every format that reads every date, the unambiguous first.

    A file of dates that are all on or before the twelfth reads as both
    DD/MM and MM/DD; both are returned, and the caller asks.
    """
    sample = [one for one in dates if one]
    fits = []
    for name in DATE_FORMATS:
        try:
            for one in sample:
                parse_date(one, name)
        except ValueError:
            continue
        fits.append(name)
    return fits


def normalise_amount(text: str, *, decimal_comma: bool) -> str:
    """"£1,234.56" -> "1234.56": the symbol and the grouping taken out, nothing rounded."""
    body = re.sub(r"[^\d.,+\-]", "", text)
    negative = body.startswith("-") or ("(" in text and ")" in text)
    body = body.lstrip("+-")
    body = body.replace(".", "").replace(",", ".") if decimal_comma else body.replace(",", "")
    return ("-" if negative else "") + body


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


class _Shape(BaseModel):
    """What this import reads from one YNAB record, checked once at the door.

    YNAB's answer is untrusted input like a file is: a string where a number
    belongs, a list where an object does, a record with no id, all raised
    TypeError or KeyError deep in the engine and answered 500 (#223). Checked
    here, every one of them is the same sentence. Unknown fields are ignored;
    YNAB adds them.
    """

    model_config = ConfigDict(extra="ignore")


class YnabCurrencyFormat(_Shape):
    iso_code: str | None = None
    currency_symbol: str | None = None


class YnabPlan(_Shape):
    id: str
    name: str | None = None
    deleted: bool = False
    currency_format: YnabCurrencyFormat | None = None
    last_modified_on: str | None = None
    first_month: str | None = None
    last_month: str | None = None


class YnabAccount(_Shape):
    id: str
    name: str | None = None
    type: str | None = None
    closed: bool = False
    deleted: bool = False
    #: Milliunits, every transaction in the account summed by YNAB (#266).
    #: Strict for the reason a transaction's amount is.
    balance: StrictInt | None = None
    cleared_balance: StrictInt | None = None


class YnabCategory(_Shape):
    id: str
    name: str | None = None
    deleted: bool = False


class YnabCategoryGroup(_Shape):
    name: str | None = None
    deleted: bool = False
    categories: list[YnabCategory] | None = None


class YnabSubtransaction(_Shape):
    id: str
    #: Strict: ``"1000"`` and ``True`` are not milliunits, and ``True % 10``
    #: is arithmetic Python will happily do.
    amount: StrictInt
    memo: str | None = None
    payee_name: str | None = None
    #: YNAB documents these on a transaction, not a part; read here too so a
    #: part that does carry them is not overruled by its parent (#265).
    import_payee_name: str | None = None
    import_payee_name_original: str | None = None
    category_id: str | None = None
    category_name: str | None = None
    transfer_account_id: str | None = None
    transfer_transaction_id: str | None = None
    deleted: bool = False


class YnabTransaction(_Shape):
    id: str
    account_id: str
    date: str
    amount: StrictInt
    memo: str | None = None
    cleared: str | None = None
    flag_color: str | None = None
    flag_name: str | None = None
    #: Three names for one payee (#265): what the person kept (``payee_name``,
    #: the row's payee), what YNAB's rename rules made of the bank's text
    #: (``import_payee_name``, read but not used), and the bank's text itself
    #: (``import_payee_name_original``, kept as ``import_payee_original``).
    #: The last two are null on a row typed into YNAB by hand.
    payee_name: str | None = None
    import_payee_name: str | None = None
    import_payee_name_original: str | None = None
    #: YNAB's id for the bank line it imported: ``YNAB:<milliunits>:<date>:<n>``.
    #: Null on a row typed by hand. Carried, not yet matched on (#264).
    import_id: str | None = None
    category_id: str | None = None
    category_name: str | None = None
    transfer_account_id: str | None = None
    transfer_transaction_id: str | None = None
    deleted: bool = False
    subtransactions: list[YnabSubtransaction] | None = None


def _checked[T](shape: type[T], answer: list) -> list[T]:
    try:
        return TypeAdapter(list[shape]).validate_python(answer)
    except (SchemaError, RecursionError):
        raise ynab_api.YnabError("YNAB's answer could not be read") from None


def _plans(token: str) -> list[YnabPlan]:
    return _checked(YnabPlan, ynab_api.plans(token))


def list_plans(token: str) -> list[dict]:
    return [
        {
            "id": one.id,
            "name": one.name,
            "currency": one.currency_format.iso_code if one.currency_format else None,
            "last_modified_on": one.last_modified_on,
            "first_month": one.first_month,
            "last_month": one.last_month,
        }
        for one in _plans(token)
        if not one.deleted
    ]


def _flag_label(colour: str | None, name: str | None) -> str:
    if not colour:
        return ""
    label = colour.capitalize()
    return f"{label} - {name}" if name else label


def _bank_text(
    txn: YnabTransaction, part: YnabTransaction | YnabSubtransaction, payee: str
) -> str | None:
    """What the bank wrote for this row, or None (#265).

    A part of a split has no bank line of its own, so it borrows its parent's
    -- but only when it kept the parent's payee, the usual split by category.
    A part paid to someone else would put one bank string under two payees,
    and `suggest_rules` reads exactly that as two payees one rule apart, and
    offers to fold them together. Such a part keeps its own text if YNAB gave
    it one, and otherwise none.
    """
    own = (part.import_payee_name_original or "").strip()
    if own or part is txn:
        return own or None
    if payee != (txn.payee_name or ""):
        return None
    return (txn.import_payee_name_original or "").strip() or None


def from_api(token: str, plan_id: str) -> Source:
    """A plan's accounts and transactions, fetched and put in the file's shape.

    Deleted records are skipped: YNAB keeps them in the API so a sync can
    remove them, and they are not history. A split arrives as a parent with
    subtransactions, and each part becomes its own row; the parent is not one.
    """
    plan = next((one for one in _plans(token) if one.id == plan_id), None)
    if plan is None:
        raise ynab_api.YnabError("YNAB has no such plan for this token")
    currency = (plan.currency_format.iso_code if plan.currency_format else None) or None
    symbol = (plan.currency_format.currency_symbol if plan.currency_format else None) or None

    accounts: dict[str, SourceAccount] = {}
    for one in _checked(YnabAccount, ynab_api.accounts(token, plan_id)):
        if one.deleted:
            continue
        accounts[one.id] = SourceAccount(
            key=one.id,
            name=one.name or one.id,
            type_hint=API_ACCOUNT_TYPES.get(one.type or ""),
            closed=one.closed,
            balance_milliunits=one.balance,
            cleared_balance_milliunits=one.cleared_balance,
        )

    group_of: dict[str, str] = {}
    for group in _checked(YnabCategoryGroup, ynab_api.category_groups(token, plan_id)):
        if group.deleted:
            continue
        for category in group.categories or []:
            if not category.deleted:
                group_of[category.id] = group.name or ""

    rows: list[SourceRow] = []
    groups: dict[str, list[str]] = {}
    for txn in _checked(YnabTransaction, ynab_api.transactions(token, plan_id)):
        if txn.deleted or txn.account_id not in accounts:
            continue
        parts = [part for part in txn.subtransactions or [] if not part.deleted]
        flag = _flag_label(txn.flag_color, txn.flag_name)
        cleared = (txn.cleared or "uncleared").lower()
        for part in parts or [txn]:
            payee = part.payee_name or txn.payee_name or ""
            original = _bank_text(txn, part, payee)
            category = part.category_name or ""
            group = group_of.get(part.category_id or "", "")
            if category and group and group not in groups.setdefault(category, []):
                groups[category].append(group)
            rows.append(
                SourceRow(
                    ref=part.id,
                    account_key=txn.account_id,
                    date_text=txn.date,
                    payee=payee,
                    payee_original=original,
                    source_import_id=txn.import_id or None,
                    parent_ref=txn.id if parts else None,
                    category=category,
                    group=group,
                    memo=(part.memo if parts else txn.memo) or "",
                    milliunits=part.amount,
                    cleared=cleared,
                    flag=flag,
                    transfer_account_key=part.transfer_account_id or None,
                    transfer_ref=part.transfer_transaction_id or None,
                    split=bool(parts),
                    starting_balance=payee == STARTING_BALANCE,
                )
            )

    return Source(
        via="api",
        rows=rows,
        accounts=accounts,
        plan_name=plan.name,
        currency=currency,
        symbol=symbol,
        currency_confirmed_needed=False,
        date_formats=["YYYY-MM-DD"],
        category_groups=groups,
        import_source="one-time-import:ynab-api",
        headline="One-time Import · YNAB (API)",
    )
