/**
 * The words for each notice the server reads back from an import's sentences
 * (#267): why a line was skipped or matched, what reading a file could not
 * settle, a row of an accounts file that cannot be imported, a One-time
 * Import row that was not.
 *
 * The client's half of `app/notices.py`: one message per notice code, the
 * same id and the same English template; `tests/test_notices.py` fails when
 * the two disagree. A notice whose code is a refusal's (an amount `money.py`
 * refused) is worded from `ERROR_MESSAGES` instead.
 *
 * **In English the screen shows the server's sentence**, byte for byte. In
 * another language `noticeText` words the code, with a sentence inside a
 * sentence worded the same way, money formatted from minor units and dates
 * through `formatDate`; anything it cannot word falls back to the English.
 */

import { msg } from "@lingui/core/macro";
import { ERROR_MESSAGES, type ErrorMessage } from "./errorMessages";
import { i18n, SOURCE_LOCALE } from "./i18n";
import { formatDate } from "./locale";
import { format } from "./money";

/** A notice as the server sends it: a code and raw params, a param maybe a notice itself. */
export interface Coded {
  code: string;
  params?: Record<string, unknown> | null;
}

export const NOTICE_MESSAGES: Record<string, ErrorMessage> = {
  "import.line.no_date_or_amount": {
    message: msg({ id: "notice.import.line.no_date_or_amount", message: "This line has no date or no amount" }),
  },
  "import.line.other_currency_rows": {
    message: msg({ id: "notice.import.line.other_currency_rows", message: "This row is in {foreign}, and {account} holds {currency}: it belongs to the {foreign} rows of this file" }),
  },
  "import.line.foreign_ofx_rate": {
    message: msg({ id: "notice.import.line.foreign_ofx_rate", message: "This transaction is in {foreign} (the bank's rate: {rate}), and {account} holds {currency}. It is this account's, but its amount is not in {currency} and is not converted here -- enter it by hand in {currency}", comment: "{rate} is the exchange rate as the bank wrote it" }),
  },
  "import.line.foreign_ofx": {
    message: msg({ id: "notice.import.line.foreign_ofx", message: "This transaction is in {foreign}, and {account} holds {currency}. It is this account's, but its amount is not in {currency} and is not converted here -- enter it by hand in {currency}" }),
  },
  "import.line.foreign": {
    message: msg({ id: "notice.import.line.foreign", message: "This row is in {foreign}, and {account} holds {currency}, so it cannot be read into it -- it would be recorded as {currency}" }),
  },
  "import.line.other_product": {
    message: msg({ id: "notice.import.line.other_product", message: "This row is {product}, and {account} takes the {taken} rows of this file", comment: "{product} and {taken} are the bank's names for accounts in one file" }),
  },
  "import.line.no_money": {
    message: msg({ id: "notice.import.line.no_money", message: "This line moves no money" }),
  },
  "import.line.already_there": {
    message: msg({ id: "notice.import.line.already_there", message: "This line is already in the account" }),
  },
  "import.line.match_one_time": {
    message: msg({ id: "notice.import.line.match_one_time", message: "The {date} entry your One-time Import brought in is this bank line; it will be marked as seen by the bank rather than added again" }),
    dates: ["date"],
  },
  "import.line.match_ynab_history": {
    message: msg({ id: "notice.import.line.match_ynab_history", message: "Matched by amount and date to the {date} entry from your YNAB history (One-time Import), not by the bank's own key; check it is the same purchase and not a new one of the same amount before committing" }),
    dates: ["date"],
  },
  "import.line.match": {
    message: msg({ id: "notice.import.line.match", message: "Looks like the {date} entry you already have; it will be marked as seen by the bank rather than added again" }),
    dates: ["date"],
  },
  "import.line.transfer": {
    message: msg({ id: "notice.import.line.transfer", message: "A transfer with {account} ({date}): {why}. It is linked when you commit, and stays out of Income v Expense." }),
    dates: ["date"],
  },
  "import.line.maybe_transfer": {
    message: msg({ id: "notice.import.line.maybe_transfer", message: "May be a transfer with {account} ({date}): {why}. Confirm it on the Transfers screen after committing." }),
    dates: ["date"],
  },
  "import.line.own_money": {
    message: msg({ id: "notice.import.line.own_money", message: "Looks like your own money moving -- it {why}. It is linked when that statement is imported." }),
  },
  "import.line.fee_already_there": {
    message: msg({ id: "notice.import.line.fee_already_there", message: "This fee is already in the account" }),
  },
  "import.line.fee_of": {
    message: msg({ id: "notice.import.line.fee_of", message: "The bank charged this on top of line {line}" }),
  },
  "import.line.broad_rule": {
    message: msg({ id: "notice.import.line.broad_rule", message: "The “{pattern}” {match} rule claimed {claimed} of {total} rows — {why}. Edit or delete it on the Rules screen if these are not all the same payee.", comment: "{match} is the kind of rule: contains, prefix or regex. {why} says why it looks too broad" }),
  },
  "import.line.rule_pattern_short": {
    message: msg({ id: "notice.import.line.rule_pattern_short", message: "{length} characters is very little for a {match} rule", comment: "Part of a sentence about a payee rule that claimed too many rows. {match} is contains, prefix or regex" }),
  },
  "import.line.rule_most_of_file": {
    message: msg({ id: "notice.import.line.rule_most_of_file", message: "that is most of the file", comment: "Part of a sentence about a payee rule that claimed too many rows" }),
  },
  "import.line.arrived_before_commit": {
    message: msg({ id: "notice.import.line.arrived_before_commit", message: "This line reached the account before this import was committed" }),
  },
  "import.line.match_deleted": {
    message: msg({ id: "notice.import.line.match_deleted", message: "The transaction this was matched to has since been deleted" }),
  },
  "import.line.match_changed": {
    message: msg({ id: "notice.import.line.match_changed", message: "The transaction this matched has changed since the preview, so it was left alone and this line was not imported" }),
  },
  "import.line.match_taken": {
    message: msg({ id: "notice.import.line.match_taken", message: "That transaction has already been matched to a statement line" }),
  },
  "import.line.added_by_choice": {
    message: msg({ id: "notice.import.line.added_by_choice", message: "You chose to add this as a new transaction" }),
  },
  "transfer.why.names": {
    message: msg({ id: "notice.transfer.why.names", message: "the {account} row names {named}", comment: "Part of a sentence on a transfer: why two rows look like one. {named} is the account the bank text names" }),
  },
  "transfer.why.linked_before": {
    message: msg({ id: "notice.transfer.why.linked_before", message: "these two accounts have had transfers linked between them before", comment: "Part of a sentence on a transfer: why two rows look like one" }),
  },
  "transfer.why.member_named": {
    message: msg({ id: "notice.transfer.why.member_named", message: "a household member's name is on it, and the amounts match", comment: "Part of a sentence on a transfer: why two rows look like one" }),
  },
  "transfer.why.amounts_match": {
    message: msg({ id: "notice.transfer.why.amounts_match", message: "the amounts match and the dates are close", comment: "Part of a sentence on a transfer: why two rows look like one" }),
  },
  "transfer.why.card_purchase": {
    message: msg({ id: "notice.transfer.why.card_purchase", message: "{why}, but money out of a card is a purchase until you say otherwise", comment: "Part of a sentence on a transfer. {why} is the reason so far, this adds a doubt" }),
  },
  "transfer.why.but": {
    message: msg({ id: "notice.transfer.why.but", message: "{why}, but {payer}", comment: "Part of a sentence on a transfer. {why} is the reason so far, {payer} a doubt about it" }),
  },
  "transfer.why.more_than_one": {
    message: msg({ id: "notice.transfer.why.more_than_one", message: "{why}, but there is more than one row it could be", comment: "Part of a sentence on a transfer. {why} is the reason so far" }),
  },
  "transfer.why.both_say": {
    message: msg({ id: "notice.transfer.why.both_say", message: "{why}; both rows say {words}", comment: "Part of a sentence on a transfer. {words} are words the bank wrote on both rows" }),
  },
  "transfer.why.closest": {
    message: msg({ id: "notice.transfer.why.closest", message: "{why}; the closest in date of {count} it could be", comment: "Part of a sentence on a transfer. {count} is how many rows it could be" }),
  },
  "transfer.why.awaiting": {
    message: msg({ id: "notice.transfer.why.awaiting", message: "names {named}; its other side is not in the ledger yet", comment: "Follows “it”: the row names an account whose statement is not imported yet" }),
  },
  "transfer.payer.rule": {
    message: msg({ id: "notice.transfer.payer.rule", message: "{payee} has a category rule", comment: "Part of a sentence on a transfer: a doubt. {payee} is a payee's name" }),
  },
  "transfer.payer.paid_before": {
    message: msg({ id: "notice.transfer.payer.paid_before", message: "{payee} has paid you before", comment: "Part of a sentence on a transfer: a doubt. {payee} is a payee's name" }),
  },
  "transfer.payer.names": {
    message: msg({ id: "notice.transfer.payer.names", message: "it names {named}, who has paid you before", comment: "Part of a sentence on a transfer: a doubt. {named} is a name the bank wrote" }),
  },
  "statement.row.did_not_happen": {
    message: msg({ id: "notice.statement.row.did_not_happen", message: "The bank marked this {state}, so no money moved", comment: "{state} is the bank's own word for the row, like declined or reversed" }),
  },
  "statement.row.not_settled": {
    message: msg({ id: "notice.statement.row.not_settled", message: "The bank marked this {state}: it has not settled, and will import from the next statement once it has", comment: "{state} is the bank's own word for the row, like pending" }),
  },
  "statement.row.no_amount": {
    message: msg({ id: "notice.statement.row.no_amount", message: "This row has no amount, so it is not a transaction" }),
  },
  "statement.row.date_unreadable": {
    message: msg({ id: "notice.statement.row.date_unreadable", message: "Could not read {text} as a date" }),
  },
  "statement.row.amount_unreadable": {
    message: msg({ id: "notice.statement.row.amount_unreadable", message: "Could not read {text} as an amount" }),
  },
  "statement.row.signed_twice": {
    message: msg({ id: "notice.statement.row.signed_twice", message: "{text} is signed twice, so whether it is money in or money out would be a guess" }),
  },
  "statement.row.in_column": {
    message: msg({ id: "notice.statement.row.in_column", message: "{problem}, in the {column} column", comment: "{problem} is why a cell could not be read; {column} is the file's own column name" }),
  },
  "statement.row.ofx_no_date": {
    message: msg({ id: "notice.statement.row.ofx_no_date", message: "No usable date in {text}" }),
  },
  "statement.row.ofx_no_amount": {
    message: msg({ id: "notice.statement.row.ofx_no_amount", message: "No usable amount in {text}" }),
  },
  "statement.row.ofx_pending": {
    message: msg({ id: "notice.statement.row.ofx_pending", message: "The bank marked this {kind}, which is a pending hold rather than a transaction that has happened", comment: "{kind} is the OFX transaction type the bank wrote, like HOLD" }),
  },
  "statement.row.ofx_cancels": {
    message: msg({ id: "notice.statement.row.ofx_cancels", message: "The bank sent this to cancel an earlier transaction ({fitid}), so there is nothing here to add", comment: "{fitid} is the bank's own id for the transaction it cancels" }),
  },
  "statement.read.currency_columns": {
    message: msg({ id: "notice.statement.read.currency_columns", message: "This file has more than one currency column ({columns}), and which one the amounts are in could not be told, so none was used: every row is taken to be in the account's currency" }),
  },
  "statement.read.dates_mixed": {
    message: msg({ id: "notice.statement.read.dates_mixed", message: "This file has dates that read as day-first and others as month-first; check the dates in the preview" }),
  },
  "statement.read.dates_either_way": {
    message: msg({ id: "notice.statement.read.dates_either_way", message: "Every date in this file could be read day-first or month-first, so it was read day-first — check the dates in the preview before importing" }),
  },
  "statement.read.date_format_unknown": {
    message: msg({ id: "notice.statement.read.date_format_unknown", message: "Could not work out the date format from values like {text}" }),
  },
  "statement.read.undecodable": {
    message: msg({ id: "notice.statement.read.undecodable", message: "Some characters in this file could not be decoded; payee names may look wrong" }),
  },
  "statement.read.control_codes": {
    message: msg({ id: "notice.statement.read.control_codes", message: "Some characters in this file are control codes rather than letters -- it may mix two encodings, so check the payee names in the preview" }),
  },
  "statement.read.no_rows": {
    message: msg({ id: "notice.statement.read.no_rows", message: "This file has no rows" }),
  },
  "statement.read.no_date_column": {
    message: msg({ id: "notice.statement.read.no_date_column", message: "No date column found -- check the file has a header row, or add one" }),
  },
  "statement.read.no_amount_column": {
    message: msg({ id: "notice.statement.read.no_amount_column", message: "No amount column found -- check the file has a header row, or add one" }),
  },
  "statement.read.all_positive": {
    message: msg({ id: "notice.statement.read.all_positive", message: "Every amount in this file is positive, and there is no second column saying which rows are money out — check the amounts in the preview, or pick separate money-out and money-in columns" }),
  },
  "statement.read.decimal_from_column": {
    message: msg({ id: "notice.statement.read.decimal_from_column", message: "The amounts in this file read either way (like “{example}”), so the decimal separator “{separator}” was taken from the fee or balance column — which differs from the “{expected}” expected because {because}. Check the amounts in the preview before importing" }),
  },
  "statement.read.decimal_assumed": {
    message: msg({ id: "notice.statement.read.decimal_assumed", message: "The decimal separator could not be worked out from this file — an amount like “{example}” reads either way — so “{separator}” was assumed, because {because}. Check the amounts in the preview before importing" }),
  },
  "statement.read.because_country": {
    message: msg({ id: "notice.statement.read.because_country", message: "this account's country is set to {country}", comment: "Follows “because”: why a decimal separator was expected. {country} is a country's name" }),
  },
  "statement.read.because_default": {
    message: msg({ id: "notice.statement.read.because_default", message: "that is the default when nothing says otherwise", comment: "Follows “because”: why a decimal separator was expected" }),
  },
  "statement.read.balance_unreadable": {
    message: msg({ id: "notice.statement.read.balance_unreadable", message: "The {column} column is not an amount on {count, plural, one {# row} other {# rows}} (such as {text}) -- those rows import without a running balance" }),
  },
  "statement.read.ofx_replaces": {
    message: msg({ id: "notice.statement.read.ofx_replaces", message: "Transaction {fitid} replaces an earlier one ({earlier}); the earlier one is still in the account and should be removed by hand", comment: "{fitid} and {earlier} are the bank's own ids for two transactions" }),
  },
  "statement.read.ofx_empty": {
    message: msg({ id: "notice.statement.read.ofx_empty", message: "This looks like an OFX file but has no transactions in it -- some banks export an empty statement when the date range has nothing in it" }),
  },
  "statement.read.ofx_no_currency": {
    message: msg({ id: "notice.statement.read.ofx_no_currency", message: "This file does not say which currency it is in" }),
  },
  "statement.read.card_signs_reversed": {
    message: msg({ id: "notice.statement.read.card_signs_reversed", message: "Amounts are reversed from the bill's own signs, so a purchase is money out of the card account and a refund is money in" }),
  },
  "import.note.rechecked": {
    message: msg({ id: "notice.import.note.rechecked", message: "{count, plural, one {# of these lines} other {# of these lines}} changed when they were re-checked against the account just now: the register has moved since this import was staged." }),
  },
  "import.note.other_products": {
    message: msg({ id: "notice.import.note.other_products", message: "This file holds more than one account: {products}. Only the {product} rows were read into {account}; the others belong to another account and are listed as skipped. To change which rows this account takes, set its statement product on the Accounts screen." }),
  },
  "import.note.other_currencies": {
    message: msg({ id: "notice.import.note.other_currencies", message: "This file holds more than one currency: {currencies}. Only the {currency} rows were read into {account}; the others belong to an account in their own currency and are listed as skipped." }),
  },
  "import.note.balance_unchecked": {
    message: msg({ id: "notice.import.note.balance_unchecked", message: "The statement's running balance was not checked: a balance, amount or fee in it is too large to record as money." }),
  },
  "import.note.balance_breaks": {
    message: msg({ id: "notice.import.note.balance_breaks", message: "The statement's running balance stops adding up at line {line} ({date}): a row before it is missing from what was read, or belongs to another account." }),
    dates: ["date"],
  },
  "import.note.balance_gap": {
    message: msg({ id: "notice.import.note.balance_gap", message: "This statement starts from {opening} on {date}, but the ledger holds {held} for the day before -- {gap} apart. A statement between the last import and this one may be missing: download from the day after the last one imported." }),
    money: ["opening", "held", "gap"],
    dates: ["date"],
  },
  "account_import.row.too_many_cells": {
    message: msg({ id: "notice.account_import.row.too_many_cells", message: "This line has more cells than the file has columns" }),
  },
  "account_import.row.name_taken": {
    message: msg({ id: "notice.account_import.row.name_taken", message: "Line {line} of this file is already called {name}" }),
  },
  "account_import.row.iban_taken": {
    message: msg({ id: "notice.account_import.row.iban_taken", message: "Line {line} of this file already has the IBAN {iban}" }),
  },
  "account_import.row.needs_type": {
    message: msg({ id: "notice.account_import.row.needs_type", message: "An account needs a type" }),
  },
  "account_import.row.unknown_type": {
    message: msg({ id: "notice.account_import.row.unknown_type", message: "{type} is not an account type -- use one of {types}", comment: "{types} is the list of account types as the file must write them, which stay in English" }),
  },
  "account_import.row.date_unreadable": {
    message: msg({ id: "notice.account_import.row.date_unreadable", message: "{text} is not a date this can read -- write it as YYYY-MM-DD, like 2026-01-31" }),
  },
  "account_import.row.iban_too_long": {
    message: msg({ id: "notice.account_import.row.iban_too_long", message: "The IBAN is longer than {max} characters" }),
  },
  "account_import.row.needs_name": {
    message: msg({ id: "notice.account_import.row.needs_name", message: "An account needs a name" }),
  },
  "account_import.row.currency_not_a_code": {
    message: msg({ id: "notice.account_import.row.currency_not_a_code", message: "{currency} is not a three-letter currency code" }),
  },
  "account_import.row.too_long": {
    message: msg({ id: "notice.account_import.row.too_long", message: "The {column} is longer than {max} characters", comment: "{column} is a column name of the accounts file, which stays in English" }),
  },
  "country.not_a_code": {
    message: msg({ id: "notice.country.not_a_code", message: "{code} is not a country code" }),
  },
  "ynab.row.account_skipped": {
    message: msg({ id: "notice.ynab.row.account_skipped", message: "Its account is skipped" }),
  },
  "ynab.row.outside_range": {
    message: msg({ id: "notice.ynab.row.outside_range", message: "Outside the date range" }),
  },
  "ynab.row.starting_balance": {
    message: msg({ id: "notice.ynab.row.starting_balance", message: "A YNAB starting balance, skipped" }),
  },
  "ynab.row.imported_before": {
    message: msg({ id: "notice.ynab.row.imported_before", message: "Already imported by an earlier one-time import" }),
  },
  "ynab.row.looks_like_existing": {
    message: msg({ id: "notice.ynab.row.looks_like_existing", message: "Looks like a row already in the ledger" }),
  },
  "ynab.row.database_refused": {
    message: msg({ id: "notice.ynab.row.database_refused", message: "The database refused this row: {constraint}", comment: "{constraint} is the database's own name for the rule a row broke" }),
  },
  "ynab.transfer.other_side_failed": {
    message: msg({ id: "notice.ynab.transfer.other_side_failed", message: "The other side of this transfer failed to import" }),
  },
  "ynab.transfer.other_side_missing": {
    message: msg({ id: "notice.ynab.transfer.other_side_missing", message: "A YNAB transfer whose other side is skipped, out of range or not in the export, so it was imported as an ordinary transaction" }),
  },
  "ynab.transfer.not_linked": {
    message: msg({ id: "notice.ynab.transfer.not_linked", message: "Not linked as a transfer: {problem}" }),
  },
  "ynab.balance.differs": {
    message: msg({ id: "notice.ynab.balance.differs", message: "{account}: YNAB's balance is {ynab}, and this import accounts for {imported}, {direction, select, more {{gap} more} other {{gap} less}}.", comment: "{direction} is more or less: whether the import accounts for more than YNAB's balance" }),
    money: ["ynab", "imported", "gap"],
  },
  "ynab.balance.unchecked": {
    message: msg({ id: "notice.ynab.balance.unchecked", message: "{account}: YNAB's balance could not be checked: {problem}." }),
  },
  "unset.category": {
    message: msg({ id: "notice.unset.category", message: "Uncategorised", comment: "The name of the rows with no category, in a report or a breakdown. See GLOSSARY.md (Uncategorised)" }),
  },
  "unset.payee": {
    message: msg({ id: "notice.unset.payee", message: "No payee", comment: "The name of the rows with no payee, in a breakdown of payees" }),
  },
  "statement.unreadable.no_bom": {
    message: msg({ id: "notice.statement.unreadable.no_bom", message: "This file looks like UTF-16 or UTF-32 text without a byte-order mark, so how to read it cannot be told for certain -- save it again as UTF-8 (or CSV UTF-8) and import that" }),
  },
  "statement.unreadable.too_many_rows": {
    message: msg({ id: "notice.statement.unreadable.too_many_rows", message: "This file has more than {max, number} rows, which no statement has -- it may not be a statement. Download a shorter period from your bank." }),
  },
  "statement.unreadable.cell_too_long": {
    message: msg({ id: "notice.statement.unreadable.cell_too_long", message: "One cell in this file is longer than {max, number} characters, which no statement has -- it may not be a statement, or it may be damaged. Download it from your bank again." }),
  },
  "statement.unreadable.not_a_table": {
    message: msg({ id: "notice.statement.unreadable.not_a_table", message: "This file could not be read as a table. Download it from your bank again, or export it as CSV." }),
  },
  "statement.unreadable.undated": {
    message: msg({ id: "notice.statement.unreadable.undated", message: "More than {max, number} rows of this file have no readable date -- it is not a statement, or its date column was not found. Check the header row." }),
  },
  "statement.unreadable.xlsx": {
    message: msg({ id: "notice.statement.unreadable.xlsx", message: "This looks like an .xlsx file, which is not supported yet -- open it and save as CSV, or export CSV from your bank instead" }),
  },
  "statement.unreadable.no_xlrd": {
    message: msg({ id: "notice.statement.unreadable.no_xlrd", message: "Reading .xls files needs the xlrd package, which is not installed", comment: "xlrd is the name of a software package and stays as it is" }),
  },
  "statement.unreadable.not_a_spreadsheet": {
    message: msg({ id: "notice.statement.unreadable.not_a_spreadsheet", message: "This file could not be read as a spreadsheet. It may be damaged; download it from your bank again, or export it as CSV." }),
  },
  "statement.unreadable.no_sheets": {
    message: msg({ id: "notice.statement.unreadable.no_sheets", message: "This workbook has no sheets in it" }),
  },
  "statement.unreadable.sheet_unreadable": {
    message: msg({ id: "notice.statement.unreadable.sheet_unreadable", message: "The first sheet of this workbook could not be read. It may be damaged; download it from your bank again, or export it as CSV." }),
  },
  "statement.unreadable.sheet_too_large": {
    message: msg({ id: "notice.statement.unreadable.sheet_too_large", message: "The first sheet of this workbook reaches row {rows, number} and column {columns, number}, which is far larger than a statement -- this app reads at most {max_rows, number} rows and {max_columns, number} columns. If the statement is in there, export it from your bank as CSV instead." }),
  },
  "statement.unreadable.too_many_cells": {
    message: msg({ id: "notice.statement.unreadable.too_many_cells", message: "The first sheet of this workbook holds more than {max, number} cells, which is far larger than a statement. If the statement is in there, export it from your bank as CSV instead." }),
  },
  "statement.unreadable.pdf_too_long": {
    message: msg({ id: "notice.statement.unreadable.pdf_too_long", message: "This PDF has more than {max, number} pages, which is longer than any statement this app reads. Ask your bank for CSV or OFX, or a statement covering fewer months." }),
  },
  "statement.unreadable.pdf_stream": {
    message: msg({ id: "notice.statement.unreadable.pdf_stream", message: "This PDF contains a compressed block that unpacks to more than {max, number} MB, which no statement needs -- it may be damaged, or built to be read slowly. Ask your bank for CSV or OFX, or a statement covering fewer months." }),
  },
  "statement.unreadable.pdf_decoded": {
    message: msg({ id: "notice.statement.unreadable.pdf_decoded", message: "This PDF unpacks to more than {max, number} MB, which is far more than a statement needs -- it may be damaged, or built to be read slowly. Ask your bank for CSV or OFX, or a statement covering fewer months." }),
  },
  "statement.unreadable.pdf_drawing": {
    message: msg({ id: "notice.statement.unreadable.pdf_drawing", message: "The pages of this PDF hold more drawing instructions than a statement of any length would -- it may be damaged, or built to be read slowly. Ask your bank for CSV or OFX, or a statement covering fewer months." }),
  },
  "statement.unreadable.pdf_objects": {
    message: msg({ id: "notice.statement.unreadable.pdf_objects", message: "This PDF puts more than {max, number} characters and shapes on its pages, which is far more than a statement holds. Ask your bank for CSV or OFX, or a statement covering fewer months." }),
  },
  "statement.unreadable.pdf_unopenable": {
    message: msg({ id: "notice.statement.unreadable.pdf_unopenable", message: "This file could not be opened as a PDF. It may be damaged; download it from your bank again." }),
  },
  "statement.unreadable.pdf_unreadable": {
    message: msg({ id: "notice.statement.unreadable.pdf_unreadable", message: "This PDF could not be read. It may be damaged; download it from your bank again." }),
  },
  "statement.unreadable.no_pdfplumber": {
    message: msg({ id: "notice.statement.unreadable.no_pdfplumber", message: "Reading PDFs needs the pdfplumber package, which is not installed", comment: "pdfplumber is the name of a software package and stays as it is" }),
  },
  "statement.unreadable.pdf_scan": {
    message: msg({ id: "notice.statement.unreadable.pdf_scan", message: "There is no text in this PDF -- it is probably a scan, and reading those needs character recognition this app does not do. Ask your bank for CSV or OFX." }),
  },
  "statement.unreadable.pdf_no_table": {
    message: msg({ id: "notice.statement.unreadable.pdf_no_table", message: "No statement table was found in this PDF. Some documents -- a designed credit-card bill with several tables side by side, for instance -- are not a single table with a header, and importing half of one would be worse than not importing it. Ask your bank for CSV or OFX." }),
  },
  "statement.unreadable.layout_changed": {
    message: msg({ id: "notice.statement.unreadable.layout_changed", message: "This looks like a document this app has a rule for, but the entries it found add up to {found} and the document says “{label}” is {stated}. The layout has probably changed, so nothing has been imported -- importing part of a bill would be worse.", comment: "{found} and {stated} are amounts as the bill wrote them; {label} is the bill's own words for its total" }),
  },
  "agent.warning.already_here": {
    message: msg({ id: "notice.agent.warning.already_here", message: "{count} of {total, plural, one {# row was} other {# rows were}} already in this account and were not staged: {ids}. If any of those are genuinely separate purchases, give each one its own external_id and send them again -- a row with an external_id is deduped on that alone.", comment: "Said to a program that staged rows. external_id is a field name and stays as it is; {ids} are row ids" }),
  },
  "agent.warning.refused": {
    message: msg({ id: "notice.agent.warning.refused", message: "{count} of {total, plural, one {# row was} other {# rows were}} refused and will not land: {reasons}.", comment: "Said to a program that staged rows. {reasons} is a list of reasons with counts" }),
  },
  "agent.warning.matched": {
    message: msg({ id: "notice.agent.warning.matched", message: "{count} of {total, plural, one {# row} other {# rows}} matched entries already in this account. Committing marks those as seen by the bank rather than adding them again." }),
  },
  "agent.warning.sign_convention": {
    message: msg({ id: "notice.agent.warning.sign_convention", message: "{share} of this batch's value is money coming in to a {type} account ({count} of {total, plural, one {# row} other {# rows}}), and it nets positive overall. Confirm these are income rather than, say, credit-card repayments read from a card statement -- on a card those are money in, and in a {type} account the same rows are money out.", comment: "Said to a program that staged rows. {share} is a percentage like 61%; {type} an account type's value" }),
  },
  "agent.warning.row_count": {
    message: msg({ id: "notice.agent.warning.row_count", message: "You declared {declared, plural, one {# row} other {# rows}} and {arrived} arrived. A dropped page or a mis-split line looks exactly like this." }),
  },
  "agent.warning.total": {
    message: msg({ id: "notice.agent.warning.total", message: "You declared a total of {declared} minor units and the rows that would land come to {staged}, a difference of {difference}. A merged row, a missing row or a flipped sign all show up here. Rows that were skipped or refused are not in this figure -- see the other warnings if there are any.", comment: "Said to a program that staged rows. The figures are in minor units, as the program sent them" }),
  },
  "agent.warning.period_start": {
    message: msg({ id: "notice.agent.warning.period_start", message: "You declared the period starting {declared} and the earliest row staged is {earliest}." }),
    dates: ["declared", "earliest"],
  },
  "agent.warning.period_end": {
    message: msg({ id: "notice.agent.warning.period_end", message: "You declared the period ending {declared} and the latest row staged is {latest}." }),
    dates: ["declared", "latest"],
  },
};

function isCoded(value: unknown): value is Coded {
  return typeof value === "object" && value !== null && typeof (value as Coded).code === "string";
}

/** A notice in the active language, or null when this build cannot word all of it. */
export function wordNotice(notice: Coded): string | null {
  const entry = NOTICE_MESSAGES[notice.code] ?? ERROR_MESSAGES[notice.code];
  if (!entry) return null;
  const params = notice.params ?? {};
  const values: Record<string, unknown> = {};
  const currency = typeof params.currency === "string" ? params.currency : null;
  for (const [name, value] of Object.entries(params)) {
    if (isCoded(value)) {
      const inner = wordNotice(value);
      if (inner === null) return null;
      values[name] = inner;
    } else if (entry.money?.includes(name) && typeof value === "number") {
      if (!currency) return null;
      values[name] = format(value, currency);
    } else if (entry.dates?.includes(name) && typeof value === "string") {
      values[name] = formatDate(value);
    } else {
      values[name] = value;
    }
  }
  return i18n._({ ...entry.message, values });
}

/**
 * What to show for one of an import's sentences: the server's English in
 * English, otherwise its code worded here when it can be.
 */
export function noticeText(english: string, notice: Coded | null | undefined): string {
  if (i18n.locale === SOURCE_LOCALE || !notice) return english;
  return wordNotice(notice) ?? english;
}

/** The same, for a sentence sent as `*_code` and `*_params` beside it. */
export function codedText(
  english: string,
  code: string | null | undefined,
  params: Record<string, unknown> | null | undefined,
): string {
  return noticeText(english, code ? { code, params } : null);
}
