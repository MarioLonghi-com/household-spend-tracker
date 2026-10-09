"""The sentences an import says that are not refusals, read back as codes (#267).

An import explains itself line by line -- why a row was skipped, which row it
matched, that a rule claimed too much -- and warns about a file as a whole.
Those sentences are English, and many of them are *stored*: a staged line's
`reason` is a column, a batch's warnings are in its `source`. A code cannot be
stored beside them without a migration, and the sentence has to stay byte for
byte what it was anyway. So the code is not stored at all. It is *read*: every
sentence an import writes is registered here as the format string that writes
it, and :func:`read` turns a sentence back into its code and raw params.

Reading rather than storing has one cost and one benefit. The cost: a sentence
nobody registered reads as nothing, and the client shows its English. The
benefit: a line staged before this existed reads exactly as well as one staged
today, because the words never changed.

Each entry is:

- ``english``: the format string the code writes, ``{name}`` for a param and
  ``{name!r}`` where it writes ``repr``. `tests/test_notices.py` writes every
  one back from its own reading, and reads what the importers really write.
- ``template``: the client's ICU message (`client/src/lib/noticeMessages.ts`),
  or None where the sentence is a refusal's and its code is already in
  `app/error_codes.py` -- a statement line rejected for an amount `money.py`
  refused says exactly what `money.py` said.
- ``kinds``: how a param is read back. ``int``; ``date`` (ISO); ``money``
  (formatted, read back to minor units in the currency the reader is given);
  ``nested``, a sentence of its own, read recursively; anything else is text
  as written.

The params are raw, as everywhere (CLAUDE.md): a count is a number, a date is
ISO, an amount is minor units beside its currency, a name is as written.
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import cache, lru_cache
from string import Formatter

from .error_codes import REGISTRY


@dataclass(frozen=True)
class Notice:
    #: What the code writes, as a format string.
    english: str
    #: The client's ICU message, or None for a refusal's own code.
    template: str | None
    #: How each param is read back; text unless named.
    kinds: dict[str, str] = field(default_factory=dict)


NOTICES: dict[str, Notice] = {
    # -- A statement line, `app/services/importing.py` ----------------------- #
    "import.line.no_date_or_amount": Notice(
        "this line has no date or no amount", "This line has no date or no amount"
    ),
    "import.line.other_currency_rows": Notice(
        "this row is in {foreign}, and {account} holds {currency}: it belongs to the {foreign} "
        "rows of this file",
        "This row is in {foreign}, and {account} holds {currency}: it belongs to the {foreign} rows of this file",
    ),
    "import.line.foreign_ofx_rate": Notice(
        "this transaction is in {foreign} (the bank's rate: {rate}), and {account} holds "
        "{currency}. It is this account's, but its amount is not in {currency} and is not "
        "converted here -- enter it by hand in {currency}",
        "This transaction is in {foreign} (the bank's rate: {rate}), and {account} holds {currency}. It is this account's, but its amount is not in {currency} and is not converted here -- enter it by hand in {currency}",
    ),
    "import.line.foreign_ofx": Notice(
        "this transaction is in {foreign}, and {account} holds {currency}. It is this "
        "account's, but its amount is not in {currency} and is not converted here -- enter it "
        "by hand in {currency}",
        "This transaction is in {foreign}, and {account} holds {currency}. It is this account's, but its amount is not in {currency} and is not converted here -- enter it by hand in {currency}",
    ),
    "import.line.foreign": Notice(
        "this row is in {foreign}, and {account} holds {currency}, so it cannot be read into it "
        "-- it would be recorded as {currency}",
        "This row is in {foreign}, and {account} holds {currency}, so it cannot be read into it -- it would be recorded as {currency}",
    ),
    "import.line.other_product": Notice(
        "this row is {product}, and {account} takes the {taken} rows of this file",
        "This row is {product}, and {account} takes the {taken} rows of this file",
    ),
    "import.line.no_money": Notice("this line moves no money", "This line moves no money"),
    "import.line.already_there": Notice(
        "this line is already in the account", "This line is already in the account"
    ),
    "import.line.match_one_time": Notice(
        "the {date} entry your One-time Import brought in is this bank line; it will be marked "
        "as seen by the bank rather than added again",
        "The {date} entry your One-time Import brought in is this bank line; it will be marked as seen by the bank rather than added again",
        {"date": "date"},
    ),
    "import.line.match_ynab_history": Notice(
        "matched by amount and date to the {date} entry from your YNAB history (One-time "
        "Import), not by the bank's own key; check it is the same purchase and not a new one of "
        "the same amount before committing",
        "Matched by amount and date to the {date} entry from your YNAB history (One-time Import), not by the bank's own key; check it is the same purchase and not a new one of the same amount before committing",
        {"date": "date"},
    ),
    "import.line.match": Notice(
        "looks like the {date} entry you already have; it will be marked as seen by the bank "
        "rather than added again",
        "Looks like the {date} entry you already have; it will be marked as seen by the bank rather than added again",
        {"date": "date"},
    ),
    "import.line.transfer": Notice(
        "a transfer with {account} ({date}): {why}. It is linked when you commit, and stays out "
        "of Income vs Expense.",
        "A transfer with {account} ({date}): {why}. It is linked when you commit, and stays out of Income vs Expense.",
        {"date": "date", "why": "nested"},
    ),
    "import.line.maybe_transfer": Notice(
        "may be a transfer with {account} ({date}): {why}. Confirm it on the Transfers screen "
        "after committing.",
        "May be a transfer with {account} ({date}): {why}. Confirm it on the Transfers screen after committing.",
        {"date": "date", "why": "nested"},
    ),
    "import.line.own_money": Notice(
        "looks like your own money moving -- it {why}. It is linked when that statement is "
        "imported.",
        "Looks like your own money moving -- it {why}. It is linked when that statement is imported.",
        {"why": "nested"},
    ),
    "import.line.fee_already_there": Notice(
        "this fee is already in the account", "This fee is already in the account"
    ),
    "import.line.fee_of": Notice(
        "the bank charged this on top of line {line}",
        "The bank charged this on top of line {line}",
        {"line": "int"},
    ),
    "import.line.broad_rule": Notice(
        'the "{pattern}" {match} rule claimed {claimed} of {total} rows — {why}. Edit or delete '
        "it on the Rules screen if these are not all the same payee.",
        "The “{pattern}” {match} rule claimed {claimed} of {total} rows — {why}. Edit or delete it on the Rules screen if these are not all the same payee.",
        {"claimed": "int", "total": "int", "why": "nested"},
    ),
    "import.line.rule_pattern_short": Notice(
        "{length} characters is very little for a {match} rule",
        "{length} characters is very little for a {match} rule",
        {"length": "int"},
    ),
    "import.line.rule_most_of_file": Notice("that is most of the file", "that is most of the file"),
    "import.line.arrived_before_commit": Notice(
        "this line reached the account before this import was committed",
        "This line reached the account before this import was committed",
    ),
    "import.line.match_deleted": Notice(
        "the transaction this was matched to has since been deleted",
        "The transaction this was matched to has since been deleted",
    ),
    "import.line.match_changed": Notice(
        "the transaction this matched has changed since the preview, so it was left alone and "
        "this line was not imported",
        "The transaction this matched has changed since the preview, so it was left alone and this line was not imported",
    ),
    "import.line.match_taken": Notice(
        "that transaction has already been matched to a statement line",
        "That transaction has already been matched to a statement line",
    ),
    "import.line.added_by_choice": Notice(
        "you chose to add this as a new transaction", "You chose to add this as a new transaction"
    ),
    # -- Why two rows look like one transfer, `app/services/transfers.py` ---- #
    "transfer.why.names": Notice(
        "the {account} row names {named}", "the {account} row names {named}"
    ),
    "transfer.why.linked_before": Notice(
        "these two accounts have had transfers linked between them before",
        "these two accounts have had transfers linked between them before",
    ),
    "transfer.why.member_named": Notice(
        "a household member's name is on it, and the amounts match",
        "a household member's name is on it, and the amounts match",
    ),
    "transfer.why.amounts_match": Notice(
        "the amounts match and the dates are close", "the amounts match and the dates are close"
    ),
    "transfer.why.card_purchase": Notice(
        "{why}, but money out of a card is a purchase until you say otherwise",
        "{why}, but money out of a card is a purchase until you say otherwise",
        {"why": "nested"},
    ),
    "transfer.why.but": Notice("{why}, but {payer}", "{why}, but {payer}", {"why": "nested", "payer": "nested"}),
    "transfer.why.more_than_one": Notice(
        "{why}, but there is more than one row it could be",
        "{why}, but there is more than one row it could be",
        {"why": "nested"},
    ),
    "transfer.why.both_say": Notice(
        "{why}; both rows say {words}", "{why}; both rows say {words}", {"why": "nested"}
    ),
    "transfer.why.closest": Notice(
        "{why}; the closest in date of {count} it could be",
        "{why}; the closest in date of {count} it could be",
        {"why": "nested", "count": "int"},
    ),
    "transfer.why.awaiting": Notice(
        "names {named}; its other side is not in the ledger yet",
        "names {named}; its other side is not in the ledger yet",
    ),
    "transfer.payer.rule": Notice("{payee} has a category rule", "{payee} has a category rule"),
    "transfer.payer.paid_before": Notice("{payee} has paid you before", "{payee} has paid you before"),
    "transfer.payer.names": Notice(
        "it names {named}, who has paid you before", "it names {named}, who has paid you before"
    ),
    # -- A statement row the library could not take, `statements/` ----------- #
    "statement.row.did_not_happen": Notice(
        "the bank marked this {state}, so no money moved",
        "The bank marked this {state}, so no money moved",
    ),
    "statement.row.not_settled": Notice(
        "the bank marked this {state}: it has not settled, and will import from the next "
        "statement once it has",
        "The bank marked this {state}: it has not settled, and will import from the next statement once it has",
    ),
    "statement.row.no_amount": Notice(
        "this row has no amount, so it is not a transaction",
        "This row has no amount, so it is not a transaction",
    ),
    "statement.row.date_unreadable": Notice(
        "could not read {text!r} as a date", "Could not read {text} as a date"
    ),
    "statement.row.amount_unreadable": Notice(
        "could not read {text!r} as an amount", "Could not read {text} as an amount"
    ),
    "statement.row.signed_twice": Notice(
        "{text!r} is signed twice, so whether it is money in or money out would be a guess",
        "{text} is signed twice, so whether it is money in or money out would be a guess",
    ),
    "statement.row.in_column": Notice(
        "{problem}, in the {column} column", "{problem}, in the {column} column", {"problem": "nested"}
    ),
    "statement.row.ofx_no_date": Notice("no usable date in {text!r}", "No usable date in {text}"),
    "statement.row.ofx_no_amount": Notice("no usable amount in {text!r}", "No usable amount in {text}"),
    "statement.row.ofx_pending": Notice(
        "the bank marked this {kind}, which is a pending hold rather than a transaction that has "
        "happened",
        "The bank marked this {kind}, which is a pending hold rather than a transaction that has happened",
    ),
    "statement.row.ofx_cancels": Notice(
        "the bank sent this to cancel an earlier transaction ({fitid}), so there is nothing "
        "here to add",
        "The bank sent this to cancel an earlier transaction ({fitid}), so there is nothing here to add",
    ),
    # -- What reading a file could not settle, `statements/` ------------------ #
    "statement.read.currency_columns": Notice(
        "this file has more than one currency column ({columns}), and which one the amounts "
        "are in could not be told, so none was used: every row is taken to be in the "
        "account's currency",
        "This file has more than one currency column ({columns}), and which one the amounts are in could not be told, so none was used: every row is taken to be in the account's currency",
    ),
    "statement.read.dates_mixed": Notice(
        "this file has dates that read as day-first and others as month-first; check the "
        "dates in the preview",
        "This file has dates that read as day-first and others as month-first; check the dates in the preview",
    ),
    "statement.read.dates_either_way": Notice(
        "every date in this file could be read day-first or month-first, so it was read "
        "day-first — check the dates in the preview before importing",
        "Every date in this file could be read day-first or month-first, so it was read day-first — check the dates in the preview before importing",
    ),
    "statement.read.date_format_unknown": Notice(
        "could not work out the date format from values like {text!r}",
        "Could not work out the date format from values like {text}",
    ),
    "statement.read.undecodable": Notice(
        "some characters in this file could not be decoded; payee names may look wrong",
        "Some characters in this file could not be decoded; payee names may look wrong",
    ),
    "statement.read.control_codes": Notice(
        "some characters in this file are control codes rather than letters -- it may mix two "
        "encodings, so check the payee names in the preview",
        "Some characters in this file are control codes rather than letters -- it may mix two encodings, so check the payee names in the preview",
    ),
    "statement.read.no_rows": Notice("this file has no rows", "This file has no rows"),
    "statement.read.no_date_column": Notice(
        "no date column found -- check the file has a header row, or add one",
        "No date column found -- check the file has a header row, or add one",
    ),
    "statement.read.no_amount_column": Notice(
        "no amount column found -- check the file has a header row, or add one",
        "No amount column found -- check the file has a header row, or add one",
    ),
    "statement.read.all_positive": Notice(
        "every amount in this file is positive, and there is no second column saying which "
        "rows are money out — check the amounts in the preview, or pick separate money-out and "
        "money-in columns",
        "Every amount in this file is positive, and there is no second column saying which rows are money out — check the amounts in the preview, or pick separate money-out and money-in columns",
    ),
    "statement.read.decimal_from_column": Notice(
        'the amounts in this file read either way (like "{example}"), so the decimal separator '
        '"{separator}" was taken from the fee or balance column — which differs from the '
        '"{expected}" expected because {because}. Check the amounts in the preview before '
        "importing",
        "The amounts in this file read either way (like “{example}”), so the decimal separator “{separator}” was taken from the fee or balance column — which differs from the “{expected}” expected because {because}. Check the amounts in the preview before importing",
        {"because": "nested"},
    ),
    "statement.read.decimal_assumed": Notice(
        "the decimal separator could not be worked out from this file — an amount like "
        '"{example}" reads either way — so "{separator}" was assumed, because {because}. '
        "Check the amounts in the preview before importing",
        "The decimal separator could not be worked out from this file — an amount like “{example}” reads either way — so “{separator}” was assumed, because {because}. Check the amounts in the preview before importing",
        {"because": "nested"},
    ),
    "statement.read.because_country": Notice(
        "this account's country is set to {country}", "this account's country is set to {country}"
    ),
    "statement.read.because_default": Notice(
        "that is the default when nothing says otherwise",
        "that is the default when nothing says otherwise",
    ),
    "statement.read.balance_unreadable": Notice(
        "the {column} column is not an amount on {count} (such as {text!r}) -- those rows import "
        "without a running balance",
        "The {column} column is not an amount on {count, plural, one {# row} other {# rows}} (such as {text}) -- those rows import without a running balance",
        {"count": "int"},
    ),
    "statement.read.ofx_replaces": Notice(
        "transaction {fitid} replaces an earlier one ({earlier}); the earlier one is still in "
        "the account and should be removed by hand",
        "Transaction {fitid} replaces an earlier one ({earlier}); the earlier one is still in the account and should be removed by hand",
    ),
    "statement.read.ofx_empty": Notice(
        "this looks like an OFX file but has no transactions in it -- some banks export an "
        "empty statement when the date range has nothing in it",
        "This looks like an OFX file but has no transactions in it -- some banks export an empty statement when the date range has nothing in it",
    ),
    "statement.read.ofx_no_currency": Notice(
        "this file does not say which currency it is in",
        "This file does not say which currency it is in",
    ),
    "statement.read.card_signs_reversed": Notice(
        "amounts are reversed from the bill's own signs, so a purchase is money out of the card "
        "account and a refund is money in",
        "Amounts are reversed from the bill's own signs, so a purchase is money out of the card account and a refund is money in",
    ),
    "import.note.rechecked": Notice(
        "{count} of these lines changed when they were re-checked against the account just now: "
        "the register has moved since this import was staged.",
        "{count, plural, one {# of these lines} other {# of these lines}} changed when they were re-checked against the account just now: the register has moved since this import was staged.",
        {"count": "int"},
    ),
    # -- What staging said about a file as a whole, `importing.py` ----------- #
    "import.note.other_products": Notice(
        "This file holds more than one account: {products}. Only the {product} rows were read "
        "into {account}; the others belong to another account and are listed as skipped. To "
        "change which rows this account takes, set its statement product on the Accounts "
        "screen.",
        "This file holds more than one account: {products}. Only the {product} rows were read into {account}; the others belong to another account and are listed as skipped. To change which rows this account takes, set its statement product on the Accounts screen.",
    ),
    "import.note.other_currencies": Notice(
        "This file holds more than one currency: {currencies}. Only the {currency} rows were "
        "read into {account}; the others belong to an account in their own currency and are "
        "listed as skipped.",
        "This file holds more than one currency: {currencies}. Only the {currency} rows were read into {account}; the others belong to an account in their own currency and are listed as skipped.",
    ),
    "import.note.balance_unchecked": Notice(
        "The statement's running balance was not checked: a balance, amount or fee in it is "
        "too large to record as money.",
        "The statement's running balance was not checked: a balance, amount or fee in it is too large to record as money.",
    ),
    "import.note.balance_breaks": Notice(
        "The statement's running balance stops adding up at line {line} ({date}): a row before "
        "it is missing from what was read, or belongs to another account.",
        "The statement's running balance stops adding up at line {line} ({date}): a row before it is missing from what was read, or belongs to another account.",
        {"line": "int", "date": "date"},
    ),
    "import.note.balance_gap": Notice(
        "This statement starts from {opening} on {date}, but the ledger holds {held} for the "
        "day before -- {gap} apart. A statement between the last import and this one may be "
        "missing: download from the day after the last one imported.",
        "This statement starts from {opening} on {date}, but the ledger holds {held} for the day before -- {gap} apart. A statement between the last import and this one may be missing: download from the day after the last one imported.",
        {"opening": "money", "held": "money", "gap": "money", "date": "date"},
    ),
    # -- A row of an accounts CSV, `app/services/account_import.py` ---------- #
    "account_import.row.too_many_cells": Notice(
        "this line has more cells than the file has columns",
        "This line has more cells than the file has columns",
    ),
    "account_import.row.name_taken": Notice(
        "line {line} of this file is already called {name!r}",
        "Line {line} of this file is already called {name}",
        {"line": "int"},
    ),
    "account_import.row.iban_taken": Notice(
        "line {line} of this file already has the IBAN {iban!r}",
        "Line {line} of this file already has the IBAN {iban}",
        {"line": "int"},
    ),
    "account_import.row.needs_type": Notice("an account needs a type", "An account needs a type"),
    "account_import.row.unknown_type": Notice(
        "{type!r} is not an account type -- use one of {types}",
        "{type} is not an account type -- use one of {types}",
    ),
    "account_import.row.date_unreadable": Notice(
        "{text!r} is not a date this can read -- write it as YYYY-MM-DD, like 2026-01-31",
        "{text} is not a date this can read -- write it as YYYY-MM-DD, like 2026-01-31",
    ),
    "account_import.row.iban_too_long": Notice(
        "the IBAN is longer than {max} characters",
        "The IBAN is longer than {max} characters",
        {"max": "int"},
    ),
    "account_import.row.needs_name": Notice("an account needs a name", "An account needs a name"),
    "account_import.row.currency_not_a_code": Notice(
        "{currency!r} is not a three-letter currency code",
        "{currency} is not a three-letter currency code",
    ),
    "account_import.row.too_long": Notice(
        "the {column} is longer than {max} characters",
        "The {column} is longer than {max} characters",
        {"max": "int"},
    ),
    "country.not_a_code": Notice("{code!r} is not a country code", "{code} is not a country code"),
    # -- A row of a One-time Import, `app/services/one_time_import/engine.py` - #
    "ynab.row.account_skipped": Notice("its account is skipped", "Its account is skipped"),
    "ynab.row.outside_range": Notice("outside the date range", "Outside the date range"),
    "ynab.row.starting_balance": Notice(
        "a YNAB starting balance, skipped", "A YNAB starting balance, skipped"
    ),
    "ynab.row.imported_before": Notice(
        "already imported by an earlier one-time import",
        "Already imported by an earlier one-time import",
    ),
    "ynab.row.looks_like_existing": Notice(
        "looks like a row already in the ledger", "Looks like a row already in the ledger"
    ),
    "ynab.row.database_refused": Notice(
        "the database refused this row: {constraint}",
        "The database refused this row: {constraint}",
    ),
    "ynab.transfer.other_side_failed": Notice(
        "the other side of this transfer failed to import",
        "The other side of this transfer failed to import",
    ),
    "ynab.transfer.other_side_missing": Notice(
        "a YNAB transfer whose other side is skipped, out of range or not in the export, so it "
        "was imported as an ordinary transaction",
        "A YNAB transfer whose other side is skipped, out of range or not in the export, so it was imported as an ordinary transaction",
    ),
    "ynab.transfer.not_linked": Notice(
        "not linked as a transfer: {problem}", "Not linked as a transfer: {problem}", {"problem": "nested"}
    ),
    "ynab.balance.differs": Notice(
        "{account}: YNAB's balance is {ynab}, and this import accounts for {imported}, {gap} {direction}.",
        "{account}: YNAB's balance is {ynab}, and this import accounts for {imported}, {direction, select, more {{gap} more} other {{gap} less}}.",
        {"ynab": "money", "imported": "money", "gap": "money"},
    ),
    "ynab.balance.unchecked": Notice(
        "{account}: YNAB's balance could not be checked: {problem}.",
        "{account}: YNAB's balance could not be checked: {problem}.",
        {"problem": "nested"},
    ),
    "money.not_whole_minor_units": Notice(
        "{milliunits} thousandths is not a whole number of {currency} minor units",
        None,
        {"milliunits": "int"},
    ),
    # -- What the server calls an empty bucket, `insights.UNSET_NAME` -------- #
    # Sent as `name_code` beside a report's or a breakdown's name, not read.
    "unset.category": Notice("Uncategorised", "Uncategorised"),
    "unset.payee": Notice("No payee", "No payee"),
    # -- A file the statements library refused whole, `statements/` --------- #
    "statement.unreadable.no_bom": Notice(
        "this file looks like UTF-16 or UTF-32 text without a byte-order mark, so how to read it "
        "cannot be told for certain -- save it again as UTF-8 (or CSV UTF-8) and import that",
        "This file looks like UTF-16 or UTF-32 text without a byte-order mark, so how to read it cannot be told for certain -- save it again as UTF-8 (or CSV UTF-8) and import that",
    ),
    "statement.unreadable.too_many_rows": Notice(
        "this file has more than {max} rows, which no statement has -- it may not be a "
        "statement. Download a shorter period from your bank.",
        "This file has more than {max, number} rows, which no statement has -- it may not be a statement. Download a shorter period from your bank.",
        {"max": "count"},
    ),
    "statement.unreadable.cell_too_long": Notice(
        "one cell in this file is longer than {max} characters, which no statement has -- it "
        "may not be a statement, or it may be damaged. Download it from your bank again.",
        "One cell in this file is longer than {max, number} characters, which no statement has -- it may not be a statement, or it may be damaged. Download it from your bank again.",
        {"max": "count"},
    ),
    "statement.unreadable.not_a_table": Notice(
        "this file could not be read as a table. Download it from your bank again, or export "
        "it as CSV.",
        "This file could not be read as a table. Download it from your bank again, or export it as CSV.",
    ),
    "statement.unreadable.undated": Notice(
        "more than {max} rows of this file have no readable date -- it is not a statement, or "
        "its date column was not found. Check the header row.",
        "More than {max, number} rows of this file have no readable date -- it is not a statement, or its date column was not found. Check the header row.",
        {"max": "count"},
    ),
    "statement.unreadable.xlsx": Notice(
        "this looks like an .xlsx file, which is not supported yet -- open it and save as CSV, "
        "or export CSV from your bank instead",
        "This looks like an .xlsx file, which is not supported yet -- open it and save as CSV, or export CSV from your bank instead",
    ),
    "statement.unreadable.no_xlrd": Notice(
        "reading .xls files needs the xlrd package, which is not installed",
        "Reading .xls files needs the xlrd package, which is not installed",
    ),
    "statement.unreadable.not_a_spreadsheet": Notice(
        "this file could not be read as a spreadsheet. It may be damaged; download it from "
        "your bank again, or export it as CSV.",
        "This file could not be read as a spreadsheet. It may be damaged; download it from your bank again, or export it as CSV.",
    ),
    "statement.unreadable.no_sheets": Notice(
        "this workbook has no sheets in it", "This workbook has no sheets in it"
    ),
    "statement.unreadable.sheet_unreadable": Notice(
        "the first sheet of this workbook could not be read. It may be damaged; download it "
        "from your bank again, or export it as CSV.",
        "The first sheet of this workbook could not be read. It may be damaged; download it from your bank again, or export it as CSV.",
    ),
    "statement.unreadable.sheet_too_large": Notice(
        "the first sheet of this workbook reaches row {rows} and column {columns}, which is "
        "far larger than a statement -- this app reads at most {max_rows} rows and "
        "{max_columns} columns. If the statement is in there, export it from your bank as CSV "
        "instead.",
        "The first sheet of this workbook reaches row {rows, number} and column {columns, number}, which is far larger than a statement -- this app reads at most {max_rows, number} rows and {max_columns, number} columns. If the statement is in there, export it from your bank as CSV instead.",
        {"rows": "count", "columns": "count", "max_rows": "count", "max_columns": "count"},
    ),
    "statement.unreadable.too_many_cells": Notice(
        "the first sheet of this workbook holds more than {max} cells, which is far larger than "
        "a statement. If the statement is in there, export it from your bank as CSV instead.",
        "The first sheet of this workbook holds more than {max, number} cells, which is far larger than a statement. If the statement is in there, export it from your bank as CSV instead.",
        {"max": "count"},
    ),
    "statement.unreadable.pdf_too_long": Notice(
        "this PDF has more than {max} pages, which is longer than any statement this app reads. "
        "Ask your bank for CSV or OFX, or a statement covering fewer months.",
        "This PDF has more than {max, number} pages, which is longer than any statement this app reads. Ask your bank for CSV or OFX, or a statement covering fewer months.",
        {"max": "count"},
    ),
    "statement.unreadable.pdf_stream": Notice(
        "this PDF contains a compressed block that unpacks to more than {max} MB, which no "
        "statement needs -- it may be damaged, or built to be read slowly. Ask your bank for "
        "CSV or OFX, or a statement covering fewer months.",
        "This PDF contains a compressed block that unpacks to more than {max, number} MB, which no statement needs -- it may be damaged, or built to be read slowly. Ask your bank for CSV or OFX, or a statement covering fewer months.",
        {"max": "count"},
    ),
    "statement.unreadable.pdf_decoded": Notice(
        "this PDF unpacks to more than {max} MB, which is far more than a statement needs -- it "
        "may be damaged, or built to be read slowly. Ask your bank for CSV or OFX, or a "
        "statement covering fewer months.",
        "This PDF unpacks to more than {max, number} MB, which is far more than a statement needs -- it may be damaged, or built to be read slowly. Ask your bank for CSV or OFX, or a statement covering fewer months.",
        {"max": "count"},
    ),
    "statement.unreadable.pdf_drawing": Notice(
        "the pages of this PDF hold more drawing instructions than a statement of any length "
        "would -- it may be damaged, or built to be read slowly. Ask your bank for CSV or OFX, "
        "or a statement covering fewer months.",
        "The pages of this PDF hold more drawing instructions than a statement of any length would -- it may be damaged, or built to be read slowly. Ask your bank for CSV or OFX, or a statement covering fewer months.",
    ),
    "statement.unreadable.pdf_objects": Notice(
        "this PDF puts more than {max} characters and shapes on its pages, which is far more "
        "than a statement holds. Ask your bank for CSV or OFX, or a statement covering fewer "
        "months.",
        "This PDF puts more than {max, number} characters and shapes on its pages, which is far more than a statement holds. Ask your bank for CSV or OFX, or a statement covering fewer months.",
        {"max": "count"},
    ),
    "statement.unreadable.pdf_unopenable": Notice(
        "this file could not be opened as a PDF. It may be damaged; download it from your bank "
        "again.",
        "This file could not be opened as a PDF. It may be damaged; download it from your bank again.",
    ),
    "statement.unreadable.pdf_unreadable": Notice(
        "this PDF could not be read. It may be damaged; download it from your bank again.",
        "This PDF could not be read. It may be damaged; download it from your bank again.",
    ),
    "statement.unreadable.no_pdfplumber": Notice(
        "reading PDFs needs the pdfplumber package, which is not installed",
        "Reading PDFs needs the pdfplumber package, which is not installed",
    ),
    "statement.unreadable.pdf_scan": Notice(
        "there is no text in this PDF -- it is probably a scan, and reading those needs "
        "character recognition this app does not do. Ask your bank for CSV or OFX.",
        "There is no text in this PDF -- it is probably a scan, and reading those needs character recognition this app does not do. Ask your bank for CSV or OFX.",
    ),
    "statement.unreadable.pdf_no_table": Notice(
        "no statement table was found in this PDF. Some documents -- a designed credit-card "
        "bill with several tables side by side, for instance -- are not a single table with a "
        "header, and importing half of one would be worse than not importing it. Ask your bank "
        "for CSV or OFX.",
        "No statement table was found in this PDF. Some documents -- a designed credit-card bill with several tables side by side, for instance -- are not a single table with a header, and importing half of one would be worse than not importing it. Ask your bank for CSV or OFX.",
    ),
    "statement.unreadable.layout_changed": Notice(
        "this looks like a document this app has a rule for, but the entries it found add up to "
        '{found} and the document says "{label}" is {stated}. The layout has probably changed, '
        "so nothing has been imported -- importing part of a bill would be worse.",
        "This looks like a document this app has a rule for, but the entries it found add up to {found} and the document says “{label}” is {stated}. The layout has probably changed, so nothing has been imported -- importing part of a bill would be worse.",
    ),
    # -- What an agent's staging noticed, `app/services/agent_warnings.py` --- #
    # Read for the preview's `warning_codes`, which the agent route leaves out:
    # an agent is sent no codes until they are documented for it.
    "agent.warning.already_here": Notice(
        "{count} of {total} rows were already in this account and were not staged: {ids}. If "
        "any of those are genuinely separate purchases, give each one its own external_id and "
        "send them again -- a row with an external_id is deduped on that alone.",
        "{count} of {total, plural, one {# row was} other {# rows were}} already in this account and were not staged: {ids}. If any of those are genuinely separate purchases, give each one its own external_id and send them again -- a row with an external_id is deduped on that alone.",
        {"count": "int", "total": "int"},
    ),
    "agent.warning.refused": Notice(
        "{count} of {total} rows were refused and will not land: {reasons}.",
        "{count} of {total, plural, one {# row was} other {# rows were}} refused and will not land: {reasons}.",
        {"count": "int", "total": "int"},
    ),
    "agent.warning.matched": Notice(
        "{count} of {total} rows matched entries already in this account. Committing marks "
        "those as seen by the bank rather than adding them again.",
        "{count} of {total, plural, one {# row} other {# rows}} matched entries already in this account. Committing marks those as seen by the bank rather than adding them again.",
        {"count": "int", "total": "int"},
    ),
    "agent.warning.sign_convention": Notice(
        "{share} of this batch's value is money coming IN to a {type} account ({count} of "
        "{total} rows), and it nets positive overall. Confirm these are income rather than, "
        "say, credit-card repayments read from a card statement -- on a card those are money "
        "in, and in a {type} account the same rows are money out.",
        "{share} of this batch's value is money coming in to a {type} account ({count} of {total, plural, one {# row} other {# rows}}), and it nets positive overall. Confirm these are income rather than, say, credit-card repayments read from a card statement -- on a card those are money in, and in a {type} account the same rows are money out.",
        {"count": "int", "total": "int"},
    ),
    "agent.warning.row_count": Notice(
        "you declared {declared} rows and {arrived} arrived. A dropped page or a mis-split line "
        "looks exactly like this.",
        "You declared {declared, plural, one {# row} other {# rows}} and {arrived} arrived. A dropped page or a mis-split line looks exactly like this.",
        {"declared": "int", "arrived": "int"},
    ),
    "agent.warning.total": Notice(
        "you declared a total of {declared} minor units and the rows that would land come to "
        "{staged}, a difference of {difference}. A merged row, a missing row or a flipped sign "
        "all show up here. Rows that were skipped or refused are not in this figure -- see the "
        "other warnings if there are any.",
        "You declared a total of {declared} minor units and the rows that would land come to {staged}, a difference of {difference}. A merged row, a missing row or a flipped sign all show up here. Rows that were skipped or refused are not in this figure -- see the other warnings if there are any.",
        {"declared": "int", "staged": "int", "difference": "int"},
    ),
    "agent.warning.period_start": Notice(
        "you declared the period starting {declared} and the earliest row staged is {earliest}.",
        "You declared the period starting {declared} and the earliest row staged is {earliest}.",
        {"declared": "date", "earliest": "date"},
    ),
    "agent.warning.period_end": Notice(
        "you declared the period ending {declared} and the latest row staged is {latest}.",
        "You declared the period ending {declared} and the latest row staged is {latest}.",
        {"declared": "date", "latest": "date"},
    ),
    # -- Refusals a line or a row repeats, whose codes are the refusal's ----- #
    "money.not_a_value": Notice("not a monetary value: {value!r}", None),
    "money.too_large": Notice("{value!r} is too large to record as money", None),
    "money.amount_too_large": Notice("that amount is too large to record as money", None),
    "money.not_an_amount": Notice(
        "{value!r} is not an amount this can read -- write it with a '.' before the decimals "
        "and no thousands separators, like 1234.56 or -80",
        None,
    ),
    "money.decimals_in_whole_currency": Notice("{value!r} has decimals, and {currency} has none", None),
    "money.too_many_decimals": Notice(
        "{value!r} has more decimals than {currency} has ({places})", None, {"places": "int"}
    ),
    "money.too_many_digits": Notice(
        "an amount {digits} digits long is too large to record as money", None, {"digits": "int"}
    ),
    "ynab.date_unreadable": Notice("{date!r} is not a {format} date", None),
}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

_QUOTED = r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|Decimal\('[^']*'\)"
_BODIES = {
    "int": r"-?\d+",
    "date": r"\d{4}-\d{2}-\d{2}",
    "money": r"-?\D*?[\d,]+(?:\.\d+)?",
    "count": r"\d[\d,]*",
}


@dataclass(frozen=True)
class _Pattern:
    code: str
    regex: re.Pattern[str]
    #: Literal characters: the more a pattern names, the more specific it is.
    weight: int
    #: Params written with ``!r``.
    quoted: frozenset[str]


@cache
def _patterns() -> tuple[_Pattern, ...]:
    out = []
    for code, notice in NOTICES.items():
        parts: list[str] = []
        seen: set[str] = set()
        quoted: set[str] = set()
        weight = 0
        for literal, name, _spec, conversion in Formatter().parse(notice.english):
            parts.append(re.escape(literal))
            weight += len(literal)
            if name is None:
                continue
            if name in seen:
                parts.append(f"(?P={name})")
                continue
            seen.add(name)
            if conversion == "r":
                quoted.add(name)
                body = _QUOTED
            else:
                body = _BODIES.get(notice.kinds.get(name, ""), r".+?")
            parts.append(f"(?P<{name}>{body})")
        out.append(_Pattern(code, re.compile("".join(parts), re.S), weight, frozenset(quoted)))
    return tuple(out)


def _minor(text: str, currency: str) -> int | None:
    """``-€1,234.56`` back to minor units, as `money.format_amount` wrote it."""
    from .money import exponent

    cleaned = re.sub(r"[^\d.\-]", "", text)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return int(value.scaleb(exponent(currency)))


def read(text: str | None, *, currency: str | None = None) -> dict | None:
    """``{"code", "params"}`` for a sentence an import wrote, or None.

    ``currency`` is the account's, for a sentence that names an amount: the
    figure is read back to minor units in it, and the params carry it beside.
    Where several notices match, the most specific -- the one naming the most
    of the sentence -- wins.
    """
    if not text:
        return None
    found = _read_once(text, currency)
    # A copy: the reading is remembered, and a caller is free to change what
    # it is handed.
    return copy.deepcopy(found[1]) if found else None


@lru_cache(maxsize=4096)
def _read_once(text: str, currency: str | None) -> tuple[int, dict] | None:
    """`_read`, remembered. A statement of two thousand lines says "this line
    is already in the account" a thousand times, and each reading tries every
    pattern; once per distinct sentence is enough."""
    return _read(text, currency)


def _read(text: str | None, currency: str | None) -> tuple[int, dict] | None:
    """The reading, and how much of the sentence it accounts for in words.

    The weight is every literal character a reading names, a nested
    sentence's included: "{why}, but {payer}" with both halves read accounts
    for more of a sentence than "{payee} has paid you before" with a payee
    name that happens to hold a comma, and so it wins.
    """
    if not text:
        return None
    best: tuple[int, dict] | None = None
    for pattern in _patterns():
        found = pattern.regex.fullmatch(text)
        if found is None:
            continue
        read_back = _params(pattern, found.groupdict(), currency)
        if read_back is None:
            continue
        params, inner = read_back
        weight = pattern.weight + inner
        if best is None or weight > best[0]:
            best = (weight, {"code": pattern.code, "params": params})
    return best


def _params(
    pattern: _Pattern, raw: dict[str, str], currency: str | None
) -> tuple[dict, int] | None:
    kinds = NOTICES[pattern.code].kinds
    params: dict[str, object] = {}
    inner_weight = 0
    for name, value in raw.items():
        kind = kinds.get(name)
        if name in pattern.quoted:
            # `repr` of a str, or of the Decimal a parser was handed: the param
            # is the text either way, as the refusal's own params carry it.
            params[name] = value[9:-2] if value.startswith("Decimal(") else ast.literal_eval(value)
        elif kind == "int":
            params[name] = int(value)
        elif kind == "count":
            params[name] = int(value.replace(",", ""))
        elif kind == "money":
            if currency is None:
                return None
            minor = _minor(value, currency)
            if minor is None:
                return None
            params[name] = minor
        elif kind == "nested":
            # A sentence inside a sentence has to be one this module knows.
            # Otherwise "{why}, but {payer}" would claim any sentence with a
            # ", but" in it, and the code would describe a different one.
            inner = _read(value, currency)
            if inner is None:
                return None
            inner_weight += inner[0]
            params[name] = inner[1]
        else:
            params[name] = value
    if any(kinds.get(name) == "money" for name in raw):
        params["currency"] = currency
    return params, inner_weight


def templates() -> dict[str, str]:
    """Every notice's ICU message, by code: what the client's catalog holds."""
    return {code: notice.template for code, notice in NOTICES.items() if notice.template}


def refusal_codes() -> set[str]:
    """The notices whose code is a refusal's, all of them in `REGISTRY`."""
    return {code for code, notice in NOTICES.items() if notice.template is None and code in REGISTRY}
