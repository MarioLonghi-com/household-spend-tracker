"""Exchange rates a member enters by hand, one at a time or from a file (#396 §3).

A manual rate is the household's own data: audited, written inside a batch,
and an edit or a delete comes back with Undo in History. It is entered as
**units of the currency per one unit of the base currency** -- the ECB's
convention -- and the editor shows the inverse as a check.

One path for both doors. The editor's single rate and a CSV file's hundred go
through :func:`save_rates`, which is **all or none** with a dry run first, the
way the accounts import is: a dry run does everything inside a transaction and
throws it away, so the preview is the verdict of the real code.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from statements import sniffing

from .. import currencies
from ..audit.batch import batch
from ..errors import DomainError, NotFound, ValidationError
from ..models import Batch, BatchKind, Change, FxManualRate, FxPeriod, Household
from .fx import parse_rate, rate_text

log = logging.getLogger(__name__)

#: The file's columns, in the template's order.
COLUMNS: tuple[str, ...] = ("currency", "date", "rate")

#: A guard against the wrong file, not against an adversary: twenty years of
#: daily rates for one currency is about 5,000 lines.
MAX_RATE_CSV_BYTES = 1024 * 1024
MAX_RATE_ROWS = 20_000

#: Digits a typed rate may carry in all. More than any source publishes.
MAX_RATE_DIGITS = 18

TEMPLATE_FILENAME = "household-spend-tracker-exchange-rates-template.csv"

_DELIMITERS = (",", ";", "\t")


def template() -> str:
    """The header row and nothing else, with a BOM so Excel reads it as UTF-8."""
    return "﻿" + ",".join(COLUMNS) + "\r\n"


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def list_rates(
    session: Session,
    household_id: str,
    *,
    currency: str | None = None,
    since: Date | None = None,
    until: Date | None = None,
) -> list[FxManualRate]:
    stmt = select(FxManualRate).where(FxManualRate.household_id == household_id)
    if currency:
        stmt = stmt.where(FxManualRate.currency == currency.upper())
    if since:
        stmt = stmt.where(FxManualRate.date >= since)
    if until:
        stmt = stmt.where(FxManualRate.date <= until)
    return list(
        session.execute(stmt.order_by(FxManualRate.date.desc(), FxManualRate.currency)).scalars()
    )


def last_change(session: Session, household_id: str) -> tuple[datetime | None, str | None]:
    """When this household's manual rates last changed, and who changed them.

    Read from the audit log rather than from the rows, so a deletion counts as
    a change too: the last row standing is not the last thing anyone did.
    """
    row = session.execute(
        select(Change.at, Batch.actor_id)
        .join(Batch, Batch.id == Change.batch_id)
        .where(Change.household_id == household_id, Change.table_name == FxManualRate.__tablename__)
        .order_by(Change.seq.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else (None, None)


# --------------------------------------------------------------------------- #
# Writing: one rate or a file of them, all or none
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Row:
    """One entry's verdict, as plain data -- it has to outlive a rollback."""

    line: int
    currency: str
    #: As typed: ``2026-10-30`` for a day, ``2026-10`` for a month.
    date: str
    rate: str
    period: FxPeriod | None = None
    #: Units of the base per one unit of the currency: the editor's check.
    inverse: str | None = None
    #: The rate this one replaces, when one was stored under the same key.
    replaces: str | None = None
    #: Stored already, with this exact rate: nothing to do.
    unchanged: bool = False
    problems: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Outcome:
    dry_run: bool
    added: int
    replaced: int
    unchanged: int
    rows: list[Row]


class _Discard(Exception):
    """Raised inside the batch to throw its transaction away on purpose."""


def _day_or_month(text: str) -> tuple[Date, FxPeriod]:
    """``YYYY-MM-DD`` is a day; ``YYYY-MM`` is a month. Nothing else."""
    if len(text) == 7 and text[4] == "-":
        return Date.fromisoformat(text + "-01"), FxPeriod.month
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return Date.fromisoformat(text), FxPeriod.day
    raise ValueError(text)


def _check_rate(text: str) -> tuple[Decimal | None, str | None]:
    """A typed rate, or the sentence saying why it is not one."""
    body = text.strip()
    whole, point, fraction = body.partition(".")
    digits_ok = whole.isdigit() and whole.isascii() and (not point or (fraction.isdigit() and fraction.isascii()))
    if not body or not digits_ok:
        return None, (
            f"{text!r} is not a rate this can read -- write it with a '.' before the "
            "decimals and nothing else, like 0.8346"
        )
    if len(whole.lstrip("0") + fraction) > MAX_RATE_DIGITS:
        return None, f"{text!r} has more than {MAX_RATE_DIGITS} digits"
    value = parse_rate(body)
    if value is None:
        return None, "a rate has to be more than zero"
    return value, None


def _one(
    session: Session,
    household: Household,
    codes_in_use: set[str],
    seen: dict[tuple[str, Date, FxPeriod], int],
    today: Date,
    line: int,
    currency_text: str,
    date_text: str,
    rate_text_in: str,
) -> tuple[Row, FxManualRate | None, Decimal | None]:
    base = household.base_currency.upper()
    currency = currency_text.strip().upper()
    row = Row(line=line, currency=currency, date=date_text.strip(), rate=rate_text_in.strip())

    if not currency:
        row.problems.append("a rate needs a currency")
    elif currency == base:
        row.problems.append(f"{currency} is the base currency, which needs no rate")
    else:
        try:
            currencies.check_new(currency, codes_in_use)
        except DomainError as exc:
            row.problems.append(str(exc))

    day: Date | None = None
    if not row.date:
        row.problems.append("a rate needs a date, or a month")
    else:
        try:
            day, row.period = _day_or_month(row.date)
        except ValueError:
            row.problems.append(
                f"{row.date!r} is not a date this can read -- write a day as YYYY-MM-DD, "
                "like 2026-10-30, or a month as YYYY-MM, like 2026-10"
            )
        else:
            if day > today:
                row.problems.append(f"{row.date} is in the future")

    value, problem = _check_rate(row.rate)
    if problem:
        row.problems.append(problem)
    elif value is not None:
        row.inverse = rate_text(Decimal(1) / value)

    if day is not None and row.period is not None and currency:
        key = (currency, day, row.period)
        if key in seen:
            row.problems.append(f"line {seen[key]} already gives a rate for {currency} on {row.date}")
        else:
            seen[key] = line

    if row.problems or day is None or row.period is None or value is None:
        return row, None, None

    existing = session.execute(
        select(FxManualRate).where(
            FxManualRate.household_id == household.id,
            FxManualRate.quote_currency == base,
            FxManualRate.currency == currency,
            FxManualRate.date == day,
            FxManualRate.period == row.period,
        )
    ).scalar_one_or_none()
    if existing is not None:
        old = parse_rate(existing.rate)
        if old == value:
            row.unchanged = True
        else:
            row.replaces = existing.rate
    return row, existing, value


def save_rates(
    session: Session,
    *,
    household: Household,
    actor_id: str,
    entries: list[tuple[int, str, str, str]],
    dry_run: bool,
    source: dict | None = None,
    today: Date | None = None,
) -> Outcome:
    """Add or replace manual rates: every entry, or none of them.

    ``entries`` are ``(line, currency, date, rate)`` as typed. A rate stored
    under the same currency, day or month and base is replaced -- an edit, in
    History as one -- and an identical one is left alone. A dry run, or any
    entry with a problem, leaves no trace at all: no batch row, nothing in
    History.
    """
    from .accounts import codes_in_use as account_codes

    today = today or Date.today()
    codes = set(account_codes(session, household))
    session.commit()

    rows: list[Row] = []
    counts = {"added": 0, "replaced": 0, "unchanged": 0}
    try:
        with batch(
            session,
            kind=BatchKind.manual,
            actor_id=actor_id,
            household_id=household.id,
            source=source,
            own_transaction=False,
        ) as opened:
            seen: dict[tuple[str, Date, FxPeriod], int] = {}
            for line, currency, day, rate in entries:
                row, existing, value = _one(
                    session, household, codes, seen, today, line, currency, day, rate
                )
                rows.append(row)
                if row.problems or value is None or row.unchanged:
                    if row.unchanged:
                        counts["unchanged"] += 1
                    continue
                if existing is not None:
                    existing.rate = row.rate
                    existing.created_by_id = actor_id
                    counts["replaced"] += 1
                else:
                    day_value, period = _day_or_month(row.date)
                    session.add(
                        FxManualRate(
                            household_id=household.id,
                            quote_currency=household.base_currency.upper(),
                            currency=row.currency,
                            date=day_value,
                            period=period,
                            rate=row.rate,
                            created_by_id=actor_id,
                        )
                    )
                    counts["added"] += 1
            session.flush()
            if dry_run or any(row.problems for row in rows):
                raise _Discard
            opened.summary = {"fx_manual_rates": counts["added"] + counts["replaced"]}
    except _Discard:
        pass
    else:
        session.commit()
        return Outcome(dry_run=False, rows=rows, **counts)

    if dry_run:
        return Outcome(dry_run=True, rows=rows, **counts)
    refused = sum(1 for row in rows if row.problems)
    raise ValidationError(
        f"{refused} of {len(rows)} rate{'' if len(rows) == 1 else 's'} cannot be saved, "
        "so none were. Nothing has been changed; correct them and try again.",
        code="fx_manual.rows_refused",
        params={"refused": refused, "rows": len(rows)},
    )


def delete_rate(session: Session, *, household: Household, rate_id: str, actor_id: str) -> str:
    """Delete one manual rate, hard, in a batch of its own. Returns the batch id.

    Undo in History puts it back from the log's before-image. A rate of
    another household is "no such rate", the same as one that never existed.
    """
    row = session.get(FxManualRate, rate_id)
    if row is None or row.household_id != household.id:
        raise NotFound("no such exchange rate", code="fx_manual.not_found")
    with batch(session, kind=BatchKind.manual, actor_id=actor_id, household_id=household.id) as opened:
        session.delete(row)
        session.flush()
    return opened.id


# --------------------------------------------------------------------------- #
# A file of them
# --------------------------------------------------------------------------- #


def _header_key(cell: str) -> str:
    return "_".join(cell.strip().lower().replace("-", " ").split())


def read_csv(raw: bytes) -> list[tuple[int, str, str, str]]:
    """Every data row as ``(line, currency, date, rate)``.

    File-level refusals are raised here, before any row is looked at.
    """
    text, _encoding = sniffing.decode(raw)
    text = text.lstrip("﻿")
    first = next((line for line in text.splitlines() if line.strip()), None)
    if first is None:
        raise ValidationError("there are no rates in this file", code="fx_manual.no_rates")
    delimiter = max(
        _DELIMITERS,
        key=lambda d: sum(_header_key(c) in COLUMNS for c in next(csv.reader([first], delimiter=d), [])),
    )
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    out: list[tuple[int, str, str, str]] = []
    header: list[str] | None = None
    last = 0
    try:
        for cells in reader:
            start, last = last + 1, reader.line_num
            if not any(cell.strip() for cell in cells):
                continue
            if header is None:
                header = _header(cells)
                continue
            values = {key: cells[i].strip() for i, key in enumerate(header) if i < len(cells) and key}
            out.append((start, values.get("currency", ""), values.get("date", ""), values.get("rate", "")))
            if len(out) > MAX_RATE_ROWS:
                raise ValidationError(
                    f"this file has more than {MAX_RATE_ROWS} rates in it -- split it in two",
                    code="fx_manual.too_many_rows",
                    params={"max": MAX_RATE_ROWS},
                )
    except csv.Error as exc:
        log.debug("csv could not read the rates file", exc_info=True)
        raise ValidationError(
            f"line {reader.line_num} of this file cannot be read as CSV. Save it from the "
            "spreadsheet as CSV again, or start from the template.",
            code="fx_manual.not_csv",
            params={"line": reader.line_num},
        ) from exc
    if not out:
        raise ValidationError("there are no rates in this file", code="fx_manual.no_rates")
    return out


def _header(cells: list[str]) -> list[str]:
    keys = [_header_key(cell) for cell in cells]
    unknown = [cell.strip() for cell, key in zip(cells, keys, strict=True) if key and key not in COLUMNS]
    if unknown:
        raise ValidationError(
            f"this file has a column this does not know: {', '.join(repr(u) for u in unknown)}. "
            f"The columns are {', '.join(COLUMNS)}.",
            code="fx_manual.unknown_column",
            params={"columns": ", ".join(unknown), "known": ", ".join(COLUMNS)},
        )
    missing = [key for key in COLUMNS if key not in keys]
    if missing:
        raise ValidationError(
            f"this file has no {' or '.join(repr(m) for m in missing)} column. The first line "
            f"should name the columns: {', '.join(COLUMNS)}.",
            code="fx_manual.missing_column",
            params={"columns": ", ".join(missing), "known": ", ".join(COLUMNS)},
        )
    if len(set(k for k in keys if k)) != len([k for k in keys if k]):
        twice = next(k for k in keys if k and keys.count(k) > 1)
        raise ValidationError(
            f"this file has the column {twice!r} twice",
            code="fx_manual.column_twice",
            params={"column": twice},
        )
    return keys


def import_csv(
    session: Session,
    *,
    household: Household,
    actor_id: str,
    raw: bytes,
    filename: str | None,
    dry_run: bool,
) -> Outcome:
    """A file of rates through :func:`save_rates`: all or none, dry run first."""
    entries = read_csv(raw)
    return save_rates(
        session,
        household=household,
        actor_id=actor_id,
        entries=entries,
        dry_run=dry_run,
        source={
            "fx_rates_file": filename or "rates.csv",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        },
    )
