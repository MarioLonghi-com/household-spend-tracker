"""Accounts from a file: many at once, all or none (#146).

A household moving in has a dozen accounts and a spreadsheet listing them.
This reads that spreadsheet and runs **the same service the one-at-a-time form
runs**, row by row, so an imported account is indistinguishable from a typed
one: the opening balance is a real reconciled transaction, the name is refused
if it folds onto one already there, an IBAN goes through the identifier rules.
Nothing about an account is decided twice.

Stateless, in two visits with the same file. A dry run does all of it inside a
transaction and then throws the transaction away, so the preview is the
verdict of the real code rather than a second copy of its rules; the commit
does it again and keeps it. Between the two, nothing is stored anywhere.

**All or none.** One unreadable row refuses the file. A half-imported list of
accounts is the worst outcome available: the person fixes the file, imports it
again, and every row that went in the first time is now a duplicate.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass, field
from datetime import date

from pydantic import ValidationError as SchemaError
from sqlalchemy.orm import Session

from statements import sniffing

from .. import countries
from ..audit.batch import batch
from ..errors import DomainError, ValidationError
from ..models import AccountType, BatchKind, Household, IdentifierKind
from ..money import parse_exact
from ..schemas import AccountCreate, IdentifierCreate
from . import accounts as account_service
from . import identifiers as identifier_service
from . import payees as payee_service

log = logging.getLogger(__name__)

#: Every column the file may have, in the template's order. The template and
#: the reader both come from this, so they cannot come to disagree.
COLUMNS: tuple[str, ...] = (
    "name",
    "type",
    "currency",
    "institution",
    "country",
    "opening_balance",
    "opening_date",
    "iban",
    "note",
)

#: Without these a row is not an account.
REQUIRED: tuple[str, ...] = ("name", "type")

#: A list of accounts is a few kilobytes. This is a guard against the wrong
#: file, not against an adversary.
MAX_ACCOUNT_CSV_BYTES = 256 * 1024

#: Nobody has five hundred accounts; somebody who picked a statement by
#: mistake has five thousand rows.
MAX_ACCOUNT_ROWS = 500

TEMPLATE_FILENAME = "household-spend-tracker-accounts-template.csv"

_DELIMITERS = (",", ";", "\t")


def template() -> str:
    """The header row and nothing else.

    With a byte-order mark, because that is what makes Excel open a UTF-8 file
    as UTF-8 -- without it "Crédit Agricole" typed into the template comes back
    as mojibake. No example row: one would be imported by whoever forgot to
    delete it.
    """
    return "﻿" + ",".join(COLUMNS) + "\r\n"


@dataclass(slots=True)
class Row:
    """One line's verdict, as plain data -- it has to outlive a rollback."""

    line: int
    name: str
    type: str
    currency: str
    country: str | None = None
    flag: str = ""
    opening_balance: int | None = None
    opening_date: date | None = None
    iban: str | None = None
    problems: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Outcome:
    dry_run: bool
    created: int
    rows: list[Row]


class _Discard(Exception):
    """Raised inside the batch to throw its transaction away on purpose."""


def import_accounts(
    session: Session,
    *,
    household: Household,
    actor_id: str,
    raw: bytes,
    filename: str | None,
    dry_run: bool,
) -> Outcome:
    """Read the file, run every row through the real service, keep it or not.

    A dry run and a commit that finds a problem leave **no trace at all** --
    no batch row, no failed batch, nothing in History. The batch is opened
    with ``own_transaction=False`` so its own row goes with the rollback: a
    preview is not an act, and a refused file was never applied.
    """
    records = _read(raw)
    base_currency = household.base_currency

    # Whatever the request already did (a session touch) is its own business,
    # and should not be thrown away with a preview.
    session.commit()

    rows: list[Row] = []
    try:
        with batch(
            session,
            kind=BatchKind.admin,
            actor_id=actor_id,
            household_id=household.id,
            source={
                "accounts_file": filename or "accounts.csv",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            },
            own_transaction=False,
        ) as opened:
            seen = _Seen()
            for line, cells, extra in records:
                rows.append(_one(session, household, base_currency, seen, line, cells, extra))
            if dry_run or any(row.problems for row in rows):
                raise _Discard
            opened.summary = {"accounts": len(rows)}
    except _Discard:
        pass
    else:
        session.commit()
        return Outcome(dry_run=False, created=len(rows), rows=rows)

    if dry_run:
        return Outcome(dry_run=True, created=0, rows=rows)
    refused = sum(1 for row in rows if row.problems)
    raise ValidationError(
        f"{refused} of {len(rows)} row{'' if len(rows) == 1 else 's'} cannot be imported, "
        "so none were. Nothing has been changed; correct the file and try again."
    )


@dataclass(slots=True)
class _Seen:
    """What earlier lines of this file claimed, folded the way the service folds.

    The service would catch a second "Pot" on its own -- the first one has been
    flushed by then -- but only if the first one got that far, and it would say
    "there is already an account called 'Pot'", which sends a person looking in
    the wrong place. Kept for every line, importable or not, so fixing line 3
    cannot reveal that line 7 was always its twin.
    """

    names: dict[str, int] = field(default_factory=dict)
    ibans: dict[str, int] = field(default_factory=dict)


_TYPES = frozenset(kind.value for kind in AccountType)


def _one(
    session: Session,
    household: Household,
    base_currency: str,
    seen: _Seen,
    line: int,
    cells: dict[str, str],
    extra: bool,
) -> Row:
    """One row through the same code the form goes through, and what it said."""
    name = cells.get("name", "")
    typed_type = cells.get("type", "")
    currency = (cells.get("currency") or base_currency).upper()
    country_text = cells.get("country") or None
    iban = cells.get("iban") or None

    row = Row(line=line, name=name, type=typed_type, currency=currency, iban=iban)
    if extra:
        row.problems.append("this line has more cells than the file has columns")

    if name:
        folded = payee_service.fold(name)
        if folded in seen.names:
            row.problems.append(f"line {seen.names[folded]} of this file is already called {name!r}")
        else:
            seen.names[folded] = line
    if iban:
        # Held to the schema the identifier form is held to, before the
        # service sees it: `identifiers.add` trusts its caller on length, and
        # the column is 120 wide.
        try:
            IdentifierCreate.model_validate({"kind": IdentifierKind.iban, "value": iban})
        except SchemaError as exc:
            row.problems.extend(_identifier_problems(exc))
        compact = identifier_service.normalise(IdentifierKind.iban, iban)
        if compact in seen.ibans:
            row.problems.append(f"line {seen.ibans[compact]} of this file already has the IBAN {iban!r}")
        else:
            seen.ibans[compact] = line

    account_type: AccountType | None = None
    wanted = typed_type.lower().replace(" ", "_")
    if not typed_type:
        row.problems.append("an account needs a type")
    elif wanted in _TYPES:
        account_type = AccountType(wanted)
        row.type = account_type.value
    else:
        row.problems.append(
            f"{typed_type!r} is not an account type -- use one of "
            + ", ".join(kind.value for kind in AccountType)
        )

    if country_text is not None:
        try:
            row.country = countries.check(country_text)
        except DomainError as exc:
            row.country = country_text
            row.problems.append(str(exc))
    row.flag = countries.flag(row.country)

    balance_text = cells.get("opening_balance", "")
    if balance_text:
        try:
            row.opening_balance = parse_exact(balance_text, currency)
        except DomainError as exc:
            row.problems.append(str(exc))

    date_text = cells.get("opening_date", "")
    if date_text:
        try:
            row.opening_date = _iso_date(date_text)
        except ValueError:
            row.problems.append(
                f"{date_text!r} is not a date this can read -- write it as YYYY-MM-DD, "
                "like 2026-01-31"
            )

    # The schema the form is held to, for the lengths and shapes it owns. A
    # type that could not be read is already a problem above; a stand-in
    # keeps the schema from saying so a second time in its own words.
    try:
        body = AccountCreate.model_validate(
            {
                "name": name,
                "type": account_type or AccountType.other_asset,
                "currency": cells.get("currency") or None,
                "note": cells.get("note") or None,
                "institution": cells.get("institution") or None,
                "country": row.country if row.country and len(row.country) == 2 else None,
                "opening_balance": row.opening_balance or 0,
                "opening_date": row.opening_date,
            }
        )
    except SchemaError as exc:
        row.problems.extend(_schema_problems(exc, cells))
        return row

    if row.problems:
        return row

    try:
        account = account_service.create_account(
            session,
            household=household,
            name=body.name,
            type=body.type,
            currency=body.currency,
            note=body.note,
            institution=body.institution,
            country=body.country,
            opening_balance=body.opening_balance,
            opening_date=body.opening_date,
        )
    except DomainError as exc:
        row.problems.append(str(exc))
        return row

    if iban is not None:
        try:
            identifier_service.add(
                session,
                household_id=household.id,
                kind=IdentifierKind.iban,
                value=iban,
                account=account,
            )
        except DomainError as exc:
            row.problems.append(str(exc))
    return row


def _iso_date(text: str) -> date:
    """YYYY-MM-DD and nothing else.

    `31/01/2026` and `01/31/2026` are both real, and `03/04/2026` is either;
    guessing a day-month order for a date that stays in the ledger for good is
    not a guess worth making. `date.fromisoformat` also takes `20260131` and
    week dates since 3.11, which nobody types into a spreadsheet by intent.
    """
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError(text)
    return date.fromisoformat(text)


def _identifier_problems(exc: SchemaError) -> list[str]:
    said = []
    for error in exc.errors():
        limit = (error.get("ctx") or {}).get("max_length")
        if error["type"] == "string_too_long" and limit is not None:
            said.append(f"the IBAN is longer than {limit} characters")
        else:
            said.append(f"the IBAN: {error['msg']}")
    return said


def _schema_problems(exc: SchemaError, cells: dict[str, str]) -> list[str]:
    """The schema's refusals, in this module's words rather than pydantic's."""
    said = []
    for error in exc.errors():
        where = str(error["loc"][0]) if error.get("loc") else ""
        limit = (error.get("ctx") or {}).get("max_length")
        if where == "name" and error["type"] == "string_too_short":
            said.append("an account needs a name")
        elif where == "currency":
            said.append(f"{cells.get('currency', '')!r} is not a three-letter currency code")
        elif error["type"] == "string_too_long" and limit is not None:
            said.append(f"the {where} is longer than {limit} characters")
        else:
            said.append(f"{where}: {error['msg']}" if where else str(error["msg"]))
    return said


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #


def _header_key(cell: str) -> str:
    return "_".join(cell.strip().lower().replace("-", " ").split())


def _read(raw: bytes) -> list[tuple[int, dict[str, str], bool]]:
    """Every data row, keyed by column, with its physical line number.

    File-level refusals are raised here, before any row is looked at: a file
    whose columns cannot be told apart has no row worth a verdict.
    """
    text, _encoding = sniffing.decode(raw)
    text = text.lstrip("﻿")
    lines = text.splitlines(keepends=True)
    first = next((line for line in lines if line.strip()), None)
    if first is None:
        raise ValidationError("there are no accounts in this file")

    delimiter = max(
        _DELIMITERS,
        key=lambda d: sum(_header_key(cell) in COLUMNS for cell in next(csv.reader([first], delimiter=d), [])),
    )

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    records: list[tuple[int, dict[str, str], bool]] = []
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
            # A value under no column is somebody's data with nowhere to go,
            # so it is a problem with the line, not something to drop.
            extra = any(
                cell.strip() for i, cell in enumerate(cells) if i >= len(header) or not header[i]
            )
            values = {key: cells[i].strip() for i, key in enumerate(header) if i < len(cells) and key}
            records.append((start, values, extra))
            if len(records) > MAX_ACCOUNT_ROWS:
                raise ValidationError(
                    f"this file has more than {MAX_ACCOUNT_ROWS} accounts in it, which is more "
                    "than a household has -- it may be a statement rather than a list of accounts"
                )
    except csv.Error as exc:
        # What csv said stays in the log (#224); the sentence names the line.
        log.debug("csv could not read the accounts file", exc_info=True)
        raise ValidationError(
            f"line {reader.line_num} of this file cannot be read as CSV. Save it from "
            "the spreadsheet as CSV again, or start from the template."
        ) from exc

    if not records:
        raise ValidationError("there are no accounts in this file")
    return records


def _header(cells: list[str]) -> list[str]:
    keys = [_header_key(cell) for cell in cells]
    unknown = [cell.strip() for cell, key in zip(cells, keys, strict=True) if key and key not in COLUMNS]
    if unknown:
        raise ValidationError(
            f"this file has a column this does not know: {', '.join(repr(u) for u in unknown)}. "
            f"The columns are {', '.join(COLUMNS)} -- download the template to start from them."
        )
    seen: set[str] = set()
    for key in keys:
        if key and key in seen:
            raise ValidationError(f"this file has the column {key!r} twice")
        seen.add(key)
    missing = [key for key in REQUIRED if key not in seen]
    if missing:
        raise ValidationError(
            f"this file has no {' or '.join(repr(m) for m in missing)} column, and every account "
            "needs one. The first line should name the columns, as the template does."
        )
    return keys
