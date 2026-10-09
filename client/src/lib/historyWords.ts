/**
 * History's words, keyed the way the server sends them (#57, #266).
 *
 * The server still says every headline, field, table and sentence in English
 * (`app/services/describing.py`), and English shows exactly that. Beside each
 * it sends structure -- `headline_key`, a field's `column`, a row's
 * `table_key`, and each sentence as a *phrase*: a key and raw values (money
 * in minor units with its currency, ISO dates, enum values, names as stored)
 * -- so a translated screen words them itself. Each English message here is
 * the server's template for that key, letter for letter;
 * `tests/test_describing.py` holds the two files to each other.
 */

import { msg, t } from "@lingui/core/macro";
import type { MessageDescriptor } from "@lingui/core";
import type { Batch, ChangeDetail, FieldChange, HistoryNode, HistoryPhrase } from "./types";
import { i18n, SOURCE_LOCALE } from "./i18n";
import { dateTimeFormat, formatCount, formatDate } from "./locale";
import { format } from "./money";

/** The source language: the server's English stands, byte for byte. */
const inSource = () => i18n.locale === SOURCE_LOCALE;

/** What kind of act a batch was, by `headline_key`. */
export const HISTORY_HEADLINES: Record<string, string> = {
  get import() {
    return t({ message: "Statement import", comment: "History headline, the kind of act: a statement was imported" });
  },
  get manual() {
    return t({ message: "Edit", comment: "History headline, the kind of act: one edit made by hand" });
  },
  get bulk_update() {
    return t({ message: "Bulk edit", comment: "History headline, the kind of act: several rows changed at once" });
  },
  get bulk_delete() {
    return t({ message: "Bulk delete", comment: "History headline, the kind of act: several rows deleted at once" });
  },
  get undo() {
    return t({ message: "Undo", comment: "History headline, the kind of act: an earlier act was taken back" });
  },
  get admin() {
    return t({ message: "Setup change", comment: "History headline, the kind of act: a change to accounts, categories or settings" });
  },
  get setup() {
    return t({ message: "First setup", comment: "History headline, the kind of act: the instance was set up" });
  },
  get seed() {
    return t({ message: "Demo data", comment: "History headline, the kind of act: example data was added" });
  },
  get reconcile() {
    return t({ message: "Reconciliation", comment: "History headline, the kind of act: an account was checked against a statement. See GLOSSARY.md" });
  },
  get split() {
    return t({ message: "Split", comment: "History headline, the kind of act: noun, a transaction was divided into parts. See GLOSSARY.md" });
  },
  get agent_key() {
    return t({ message: "Agent key", comment: "History headline, the kind of act: a key for a program was issued or changed" });
  },
  get account_import() {
    return t({ message: "Account import", comment: "History headline, the kind of act: accounts were read from a file" });
  },
  get "one-time-import:ynab-csv"() {
    return t({ message: "One-time Import · YNAB (CSV)", comment: "History headline, the kind of act: history brought over from YNAB's export file" });
  },
  get "one-time-import:ynab-api"() {
    return t({ message: "One-time Import · YNAB (API)", comment: "History headline, the kind of act: history brought over through YNAB's API" });
  },
};

/** A changed column, by its name in the database. Lower case: it sits in a table of changes. */
export const HISTORY_FIELDS: Record<string, string> = {
  get amount() {
    return t({ message: "amount", comment: "A changed field in History's detail, lower case. See GLOSSARY.md" });
  },
  get date() {
    return t({ message: "date", comment: "A changed field in History's detail, lower case. See GLOSSARY.md" });
  },
  get payee_id() {
    return t({ message: "payee", comment: "A changed field in History's detail, lower case. See GLOSSARY.md" });
  },
  get category_id() {
    return t({ message: "category", comment: "A changed field in History's detail, lower case. See GLOSSARY.md" });
  },
  get memo() {
    return t({ message: "memo", comment: "A changed field in History's detail, lower case. See GLOSSARY.md" });
  },
  get cleared() {
    return t({ message: "state", comment: "A changed field in History's detail, lower case: whether the bank has the row (cleared, uncleared, locked)" });
  },
  get name() {
    return t({ message: "name", comment: "A changed field in History's detail, lower case" });
  },
  get archived() {
    return t({ message: "archived", comment: "A changed field in History's detail, lower case: whether it is hidden from pickers" });
  },
  get closed() {
    return t({ message: "closed", comment: "A changed field in History's detail, lower case: whether an account is closed" });
  },
  get theme() {
    return t({ message: "colour", comment: "A changed field in History's detail, lower case: the colour palette of a household" });
  },
  get accent() {
    return t({ message: "accent", comment: "A changed field in History's detail, lower case: the accent colour of a household" });
  },
  get note() {
    return t({ message: "note", comment: "A changed field in History's detail, lower case. See GLOSSARY.md" });
  },
  get group_id() {
    return t({ message: "group", comment: "A changed field in History's detail, lower case: the category group it is in" });
  },
  get sort_order() {
    return t({ message: "order", comment: "A changed field in History's detail, lower case: position in a list" });
  },
  get categorisation() {
    return t({ message: "categorisation", comment: "A changed field in History's detail, lower case: how a payee's rows get a category" });
  },
  get default_category_id() {
    return t({ message: "default category", comment: "A changed field in History's detail, lower case" });
  },
  get base_currency() {
    return t({ message: "currency", comment: "A changed field in History's detail, lower case: a household's main currency. See GLOSSARY.md" });
  },
  get institution() {
    return t({ message: "bank", comment: "A changed field in History's detail, lower case: the bank that holds an account. See GLOSSARY.md" });
  },
  get reimbursement() {
    return t({ message: "work expense", comment: "A changed field in History's detail, lower case: whether work should pay it back" });
  },
  get reimbursed_by_id() {
    return t({ message: "reimbursed by", comment: "A changed field in History's detail, lower case" });
  },
};

/** What a changed row is, by its table. Lower case, singular, beside the row's id. */
export const HISTORY_TABLES: Record<string, string> = {
  get transactions() {
    return t({ message: "transaction", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get accounts() {
    return t({ message: "account", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get payees() {
    return t({ message: "payee", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get payee_rules() {
    return t({ message: "payee rule", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get categories() {
    return t({ message: "category", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get category_groups() {
    return t({ message: "category group", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get households() {
    return t({ message: "household", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get household_members() {
    return t({ message: "member", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get reconciliations() {
    return t({ message: "reconciliation", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get users() {
    return t({ message: "user", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get invitations() {
    return t({ message: "invitation", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get receipts() {
    return t({ message: "receipt", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get agent_keys() {
    return t({ message: "agent key", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get transfer_rejections() {
    return t({ message: "pair marked not a transfer", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get ignored_identifier_suggestions() {
    return t({ message: "ignored identifier suggestion", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id" });
  },
  get translation_suggestions() {
    return t({ message: "suggested wording", context: "history table", comment: "What kind of row changed, lower case, singular, in History beside its id: a better translation an owner suggested" });
  },
};

/** The batch's headline: the server's English, or this language's word for its kind. */
export function headlineOf(batch: Pick<Batch, "headline" | "headline_key">): string {
  if (inSource() || !batch.headline_key) return batch.headline;
  return HISTORY_HEADLINES[batch.headline_key] ?? batch.headline;
}

/** A changed field's name, the same way. */
export function fieldOf(change: Pick<FieldChange, "field" | "column">): string {
  if (inSource() || !change.column) return change.field;
  return HISTORY_FIELDS[change.column] ?? change.field;
}

/** A changed row's kind, the same way. */
export function tableOf(row: Pick<ChangeDetail, "table" | "table_key">): string {
  if (inSource() || !row.table_key) return row.table;
  return HISTORY_TABLES[row.table_key] ?? row.table;
}

/**
 * Every History sentence, by the key the server sends. ICU, as the server's
 * `SENTENCES`: a param is a value already worded for this language, except a
 * plural's number.
 */
export const HISTORY_SENTENCES: Record<string, MessageDescriptor> = {
  "history.line.added": msg({
    id: "history.line.added",
    message: "Added {table} {label}",
    comment: "History, one changed row: a row was added. {table} is the kind of row, {label} what it is",
  }),
  "history.line.added_by_splitting": msg({
    id: "history.line.added_by_splitting",
    message: "Added {table} {label} by splitting {origin}",
    comment: "History, one changed row: a part made by splitting a transaction. {origin} is the one divided",
  }),
  "history.line.added_by_workflow": msg({
    id: "history.line.added_by_workflow",
    message: "Added {table} {label} by {workflow}",
    comment: "History, one changed row: added by an import. {workflow} is its name, like One-time Import · YNAB (CSV)",
  }),
  "history.line.removed": msg({
    id: "history.line.removed",
    message: "Removed {table} {label}",
    comment: "History, one changed row: a row was deleted. {table} is the kind of row, {label} what it was",
  }),
  "history.line.touched": msg({
    id: "history.line.touched",
    message: "Touched {table} {label}",
    comment: "History, one changed row: written again with nothing different",
  }),
  "history.line.changed": msg({
    id: "history.line.changed",
    message: "Changed {table} {label}",
    comment: "History, one changed row: changed in columns History does not name",
  }),
  "history.line.fields": msg({
    id: "history.line.fields",
    message: "{label}: {fields}",
    comment: "History, one changed row: what it is, then each change to it",
  }),
  "history.field_change": msg({
    id: "history.field_change",
    message: "{field} {was} → {now}",
    comment: "History: one field's change, from the old value to the new. {field} is lower case, like amount",
  }),
  "history.label.in_account": msg({
    id: "history.label.in_account",
    message: "in {account}",
    comment: "History: which account a transaction is in, after its amount and payee. {account} is its name",
  }),
  "history.payment": msg({
    id: "history.payment",
    message: "{amount} into {account} on {date}",
    comment: "History: the payment that repaid a work expense, by amount, account and date",
  }),
  "history.split.origin": msg({
    id: "history.split.origin",
    message: "{amount} on {date}",
    comment: "History: the transaction a split divided, by amount and date",
  }),
  "history.split.origin_with_payee": msg({
    id: "history.split.origin_with_payee",
    message: "{payee} {amount} on {date}",
    comment: "History: the transaction a split divided, by payee, amount and date",
  }),
  "history.split.part_with_category": msg({
    id: "history.split.part_with_category",
    message: "{amount} ({category})",
    comment: "History: one part of a split, its amount and its category",
  }),
  "history.detail.nothing": msg({
    id: "history.detail.nothing",
    message: "Nothing was changed.",
    comment: "History: an act that changed no rows",
  }),
  "history.detail.tally": msg({
    id: "history.detail.tally",
    message: "{tally}.",
    comment: "History: a count of what an act did, as a sentence, like 12 transactions changed.",
  }),
  "history.tally.added": msg({
    id: "history.tally.added",
    message: "{counts} added",
    comment: "History: {counts} is a list like 3 transactions, 1 payee",
  }),
  "history.tally.removed": msg({
    id: "history.tally.removed",
    message: "{counts} removed",
    comment: "History: {counts} is a list like 3 transactions, 1 payee",
  }),
  "history.tally.changed": msg({
    id: "history.tally.changed",
    message: "{counts} changed",
    comment: "History: {counts} is a list like 3 transactions, 1 payee",
  }),
  "history.detail.put_back": msg({
    id: "history.detail.put_back",
    message: "Put back {tally}.",
    comment: "History: an undo whose original act is gone. {tally} is like 3 transactions changed",
  }),
  "history.detail.reversed": msg({
    id: "history.detail.reversed",
    message: "Reversed: {headline} — {detail}",
    comment: "History: an undo. {headline} is the kind of act undone, lower case; {detail} the sentence about it",
  }),
  "history.detail.import": msg({
    id: "history.detail.import",
    message: "{made} into {account} from {filename}.",
    comment: "History: a statement import. {made} is like 12 new transactions; {filename} the file's name",
  }),
  "history.detail.import_no_file": msg({
    id: "history.detail.import_no_file",
    message: "{made} into {account}.",
    comment: "History: a statement import with no file name. {made} is like 12 new transactions",
  }),
  "history.import.created": msg({
    id: "history.import.created",
    message: "{count, plural, one {# new transaction} other {# new transactions}}",
    comment: "History: how many rows a statement import added",
  }),
  "history.import.matched": msg({
    id: "history.import.matched",
    message: "{count, plural, one {# matched to rows already there} other {# matched to rows already there}}",
    comment: "History: how many rows of a statement were already in the ledger",
  }),
  "history.import.changes": msg({
    id: "history.import.changes",
    message: "{count, plural, one {# change} other {# changes}}",
    comment: "History: how many rows an act changed",
  }),
  "history.detail.applied_by": msg({
    id: "history.detail.applied_by",
    message: "{sentence} Applied by {who}.",
    comment: "History: an import one person prepared and another applied. {who} is that person's name",
  }),
  "history.detail.applied_by_somebody": msg({
    id: "history.detail.applied_by_somebody",
    message: "{sentence} Applied by somebody else.",
    comment: "History: an import applied by a person since removed",
  }),
  "history.detail.one_time.transactions": msg({
    id: "history.detail.one_time.transactions",
    message:
      "{imported, plural, one {# transaction} other {# transactions}}{linked, plural, =0 {} one {, # transfer linked} other {, # transfers linked}}.",
    comment: "History: a one-time import from YNAB, the rows it brought and the transfers it paired",
  }),
  "history.detail.one_time.transactions_from": msg({
    id: "history.detail.one_time.transactions_from",
    message:
      "{imported, plural, one {# transaction} other {# transactions}}{linked, plural, =0 {} one {, # transfer linked} other {, # transfers linked}} from {where}.",
    comment: "History: a one-time import from YNAB. {where} is the file or budget it read",
  }),
  "history.detail.one_time.changes": msg({
    id: "history.detail.one_time.changes",
    message:
      "{changes, plural, one {# change} other {# changes}}{linked, plural, =0 {} one {, # transfer linked} other {, # transfers linked}}.",
    comment: "History: a one-time import from YNAB, counted in changed rows",
  }),
  "history.detail.one_time.changes_from": msg({
    id: "history.detail.one_time.changes_from",
    message:
      "{changes, plural, one {# change} other {# changes}}{linked, plural, =0 {} one {, # transfer linked} other {, # transfers linked}} from {where}.",
    comment: "History: a one-time import from YNAB, counted in changed rows. {where} is the file or budget",
  }),
  "history.detail.account_import": msg({
    id: "history.detail.account_import",
    message: "{count, plural, one {# account} other {# accounts}} from {file}.",
    comment: "History: accounts read from a file. {file} is its name",
  }),
  "history.detail.keys": msg({
    id: "history.detail.keys",
    message: "{acts}.",
    comment: "History: what was done to keys for programs, as a sentence",
  }),
  "history.key.issued": msg({
    id: "history.key.issued",
    message: "Issued the key “{label}”",
    comment: "History: a key for a program was made. {label} is the name it was given",
  }),
  "history.key.removed": msg({
    id: "history.key.removed",
    message: "Removed the key “{label}”",
    comment: "History: a key for a program was deleted. {label} is its name",
  }),
  "history.key.revoked": msg({
    id: "history.key.revoked",
    message: "Revoked the key “{label}”",
    comment: "History: a key for a program stopped working. See GLOSSARY.md",
  }),
  "history.key.changed": msg({
    id: "history.key.changed",
    message: "Changed the key “{label}”",
    comment: "History: a key for a program was changed. {label} is its name",
  }),
  "history.detail.locked": msg({
    id: "history.detail.locked",
    message: "{count, plural, one {# transaction} other {# transactions}} locked.",
    comment: "History: a reconciliation locked these rows. See GLOSSARY.md (Locked)",
  }),
  "history.detail.reconciled": msg({
    id: "history.detail.reconciled",
    message:
      "{account} proved against a statement closing {date} at {balance} — {count, plural, one {# transaction} other {# transactions}} locked.",
    comment: "History: a reconciliation. {balance} is the statement's closing balance. See GLOSSARY.md",
  }),
  "history.detail.split_unknown": msg({
    id: "history.detail.split_unknown",
    message: "Split — {count, plural, one {# transaction} other {# transactions}} changed.",
    comment: "History: a split whose parts cannot be read back. Split is the noun. See GLOSSARY.md",
  }),
  "history.detail.split": msg({
    id: "history.detail.split",
    message: "{amount} on {date} split into {count}: {parts}.",
    comment: "History: a transaction divided into {count} parts, listed after the colon. See GLOSSARY.md",
  }),
  "history.detail.split_with_payee": msg({
    id: "history.detail.split_with_payee",
    message: "{payee} {amount} on {date} split into {count}: {parts}.",
    comment: "History: a transaction divided into {count} parts, listed after the colon. See GLOSSARY.md",
  }),
};

/** How many rows of one table, for a tally: "3 transactions". */
export const HISTORY_COUNTS: Record<string, MessageDescriptor> = {
  transactions: msg({ id: "history.count.transactions", message: "{count, plural, one {# transaction} other {# transactions}}", comment: "History: a count of changed rows of one kind" }),
  accounts: msg({ id: "history.count.accounts", message: "{count, plural, one {# account} other {# accounts}}", comment: "History: a count of changed rows of one kind" }),
  payees: msg({ id: "history.count.payees", message: "{count, plural, one {# payee} other {# payees}}", comment: "History: a count of changed rows of one kind. See GLOSSARY.md" }),
  payee_rules: msg({ id: "history.count.payee_rules", message: "{count, plural, one {# payee rule} other {# payee rules}}", comment: "History: a count of changed rows of one kind: rules that name payees" }),
  categories: msg({ id: "history.count.categories", message: "{count, plural, one {# category} other {# categories}}", comment: "History: a count of changed rows of one kind" }),
  category_groups: msg({ id: "history.count.category_groups", message: "{count, plural, one {# category group} other {# category groups}}", comment: "History: a count of changed rows of one kind" }),
  households: msg({ id: "history.count.households", message: "{count, plural, one {# household} other {# households}}", comment: "History: a count of changed rows of one kind. See GLOSSARY.md" }),
  household_members: msg({ id: "history.count.household_members", message: "{count, plural, one {# member} other {# members}}", comment: "History: a count of changed rows of one kind: people in a household" }),
  reconciliations: msg({ id: "history.count.reconciliations", message: "{count, plural, one {# reconciliation} other {# reconciliations}}", comment: "History: a count of changed rows of one kind. See GLOSSARY.md" }),
  users: msg({ id: "history.count.users", message: "{count, plural, one {# user} other {# users}}", comment: "History: a count of changed rows of one kind: people who sign in" }),
  invitations: msg({ id: "history.count.invitations", message: "{count, plural, one {# invitation} other {# invitations}}", comment: "History: a count of changed rows of one kind" }),
  receipts: msg({ id: "history.count.receipts", message: "{count, plural, one {# receipt} other {# receipts}}", comment: "History: a count of changed rows of one kind. See GLOSSARY.md" }),
  agent_keys: msg({ id: "history.count.agent_keys", message: "{count, plural, one {# agent key} other {# agent keys}}", comment: "History: a count of changed rows of one kind: keys for programs" }),
  transfer_rejections: msg({ id: "history.count.transfer_rejections", message: "{count, plural, one {# pair marked not a transfer} other {# pairs marked not a transfer}}", comment: "History: two rows a person said are not one transfer" }),
  ignored_identifier_suggestions: msg({ id: "history.count.ignored_identifier_suggestions", message: "{count, plural, one {# ignored identifier suggestion} other {# ignored identifier suggestions}}", comment: "History: suggested account numbers a person dismissed" }),
  translation_suggestions: msg({ id: "history.count.translation_suggestions", message: "{count, plural, one {# suggested wording} other {# suggested wordings}}", comment: "History: better translations an owner suggested in review mode" }),
};

/** The words History supplies itself, by set and key: the server's `WORDS`. */
export const HISTORY_WORDS: Record<string, Record<string, MessageDescriptor>> = {
  empty: {
    category_id: msg({ id: "history.empty.category_id", message: "uncategorised", comment: "History: the value of a transaction's category when it has none. See GLOSSARY.md" }),
    default_category_id: msg({ id: "history.empty.default_category_id", message: "no default", comment: "History: the value of a payee's default category when it has none" }),
    payee_id: msg({ id: "history.empty.payee_id", message: "no payee", comment: "History: the value of a transaction's payee when it has none" }),
    memo: msg({ id: "history.empty.memo", message: "no memo", comment: "History: the value of a memo when it is empty. See GLOSSARY.md" }),
    note: msg({ id: "history.empty.note", message: "no note", comment: "History: the value of a note when it is empty. See GLOSSARY.md" }),
    accent: msg({ id: "history.empty.accent", message: "the palette's own", comment: "History: a household's accent colour when none is chosen: the palette's own is used" }),
    institution: msg({ id: "history.empty.institution", message: "no bank", comment: "History: the value of an account's bank when it has none" }),
    reimbursement: msg({ id: "history.empty.reimbursement", message: "not a work expense", comment: "History: a transaction work does not pay back. See GLOSSARY.md" }),
    reimbursed_by_id: msg({ id: "history.empty.reimbursed_by_id", message: "not yet reimbursed", comment: "History: a work expense with no repayment linked yet" }),
    "": msg({ id: "history.empty", message: "nothing", comment: "History: the value of an empty field" }),
  },
  removed: {
    payee: msg({ id: "history.removed.payee", message: "a payee since removed", comment: "History: a value naming a payee that has been deleted" }),
    category: msg({ id: "history.removed.category", message: "a category since removed", comment: "History: a value naming a category that has been deleted" }),
    account: msg({ id: "history.removed.account", message: "an account since removed", comment: "History: a value naming an account that has been deleted" }),
    transaction: msg({ id: "history.removed.transaction", message: "a transaction since removed", comment: "History: a value naming a transaction that has been deleted" }),
  },
  fallback: {
    account: msg({ id: "history.fallback.account", message: "an account", comment: "History: an account that cannot be named, in a sentence about an import" }),
  },
  bool: {
    yes: msg({ id: "history.bool.yes", message: "yes", comment: "History: the value of a yes/no field, like archived" }),
    no: msg({ id: "history.bool.no", message: "no", comment: "History: the value of a yes/no field, like archived" }),
  },
  unknown: {
    "": msg({ id: "history.unknown", message: "?", comment: "History: a name that cannot be read. Keep it short" }),
  },
};

/** Enum values History words, by column: the server's `ENUM_WORDS`. Any other reads as its value. */
export const HISTORY_ENUMS: Record<string, Record<string, MessageDescriptor>> = {
  reimbursement: {
    expected: msg({ id: "history.enum.reimbursement.expected", message: "expected", comment: "History: a work expense work should pay back. See GLOSSARY.md" }),
    written_off: msg({ id: "history.enum.reimbursement.written_off", message: "written off", comment: "History: a work expense work will not pay. See GLOSSARY.md (Written off)" }),
  },
  cleared: {
    uncleared: msg({ id: "history.enum.cleared.uncleared", message: "uncleared", comment: "History: the state of a transaction the bank has not seen. See GLOSSARY.md (Uncleared)" }),
    cleared: msg({ id: "history.enum.cleared.cleared", message: "cleared", comment: "History: the state of a transaction the bank has. See GLOSSARY.md (Cleared)" }),
    reconciled: msg({ id: "history.enum.cleared.reconciled", message: "reconciled", comment: "History: the state of a transaction locked by a reconciliation. See GLOSSARY.md (Locked)" }),
  },
};

const mediumDate = () => dateTimeFormat({ day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

function isPhrase(node: HistoryNode): node is HistoryPhrase {
  return typeof node === "object" && node !== null && "params" in node;
}

/**
 * A phrase or a value in this language, or null when something in it is
 * unknown to this build -- then the caller shows the server's English.
 */
export function wordHistory(node: HistoryNode): string | null {
  if (typeof node === "number") return formatCount(node);
  if (typeof node === "string") return node;
  if (isPhrase(node)) {
    const message = HISTORY_SENTENCES[node.key];
    if (!message) return null;
    const values: Record<string, string | number> = {};
    for (const [name, value] of Object.entries(node.params)) {
      if (typeof value === "number") {
        values[name] = value;
        continue;
      }
      const said = wordHistory(value);
      if (said === null) return null;
      values[name] = said;
    }
    return i18n._({ ...message, values });
  }
  switch (node.type) {
    case "money":
      return format(node.amount, node.currency);
    case "date":
      if (node.style === "medium") return mediumDate().format(new Date(`${node.value}T00:00:00Z`));
      return formatDate(node.value);
    case "name":
    case "text":
      return node.value;
    case "number":
      return formatCount(node.value);
    case "category":
      return `${node.group}: ${node.name}`;
    case "enum": {
      const word = HISTORY_ENUMS[node.column]?.[node.value];
      return word ? i18n._(word) : node.value;
    }
    case "count": {
      const word = HISTORY_COUNTS[node.table];
      return word ? i18n._({ ...word, values: { count: node.count } }) : null;
    }
    case "list": {
      const items = node.items.map(wordHistory);
      return items.some((one) => one === null) ? null : items.join(node.sep);
    }
    case "word": {
      let said: string | undefined;
      if (node.set === "table") said = HISTORY_TABLES[node.key];
      else if (node.set === "field") said = HISTORY_FIELDS[node.key];
      else if (node.set === "headline") said = HISTORY_HEADLINES[node.key];
      else {
        const words = HISTORY_WORDS[node.set];
        const word = words?.[node.key] ?? (node.set === "empty" ? words?.[""] : undefined);
        said = word ? i18n._(word) : undefined;
      }
      if (said === undefined) return null;
      return node.lower ? said.toLocaleLowerCase(i18n.locale) : said;
    }
    default:
      return null;
  }
}

/** Server English in English; otherwise the structure, worded here, when it can be. */
function either(english: string, node: HistoryNode | null | undefined): string {
  if (inSource() || node === null || node === undefined) return english;
  return wordHistory(node) ?? english;
}

/** What a batch did: its `detail`, the same way. */
export function detailOf(batch: Pick<Batch, "detail" | "detail_phrase">): string {
  return either(batch.detail, batch.detail_phrase);
}

/** One changed row's sentence: a `summary`, the same way. */
export function summaryOf(row: { summary: string; summary_phrase?: HistoryPhrase | null }): string {
  return either(row.summary, row.summary_phrase);
}

/** One side of a field's change, the same way. */
export function valueOf(change: FieldChange, side: "was" | "now"): string {
  return either(change[side], side === "was" ? change.was_value : change.now_value);
}
