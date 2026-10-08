/**
 * History's words, keyed the way the server sends them (#57).
 *
 * The server still says every headline, field and table in English
 * (`app/services/describing.py`), and English shows exactly that. Beside each
 * it now sends a key -- `headline_key`, a field's `column`, a row's
 * `table_key` -- so a translated screen words them itself. Each English
 * message here is the server's word for that key, letter for letter;
 * `tests/test_describing.py` holds the two files to each other.
 */

import { t } from "@lingui/core/macro";
import type { Batch, ChangeDetail, FieldChange } from "./types";
import { i18n, SOURCE_LOCALE } from "./i18n";

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
