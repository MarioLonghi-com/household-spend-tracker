/**
 * The words the screens show for a stored value -- an account type, an
 * import outcome, a role -- in one file (#52, part of #48).
 *
 * They were spread across the screens that first needed them, and two
 * screens wanting the same word kept two copies. Here, each is one entry the
 * catalog extraction (#54-#56) turns into one message, and the glossary
 * (`locales/GLOSSARY.md`) links to. The English is exactly what the screens
 * said before; a test holds every entry to it.
 *
 * Each word is a getter, so it is read in the language active when a screen
 * asks for it, not the one active when this module loaded (#55).
 *
 * History's field names are not here: the server writes History's sentences
 * today, and they move to the client with structured History (#57).
 */

import { t } from "@lingui/core/macro";
import type { AccountType, Role } from "./types";

/** What each account type is called in the form, and what it means. */
export const ACCOUNT_TYPE_LABELS: Record<AccountType, { label: string; blurb: string }> = {
  checking: {
    get label() {
      return t({ message: "Current account", comment: "Account type `checking`: an everyday bank account. See GLOSSARY.md" });
    },
    get blurb() {
      return t`Day-to-day money at a bank. Salary in, card and direct debits out.`;
    },
  },
  savings: {
    get label() {
      return t({ message: "Savings", comment: "Account type `savings`, and the heading of its group. See GLOSSARY.md" });
    },
    get blurb() {
      return t`Money set aside at a bank. Same as a current account, kept apart so the register reads clearly.`;
    },
  },
  cash: {
    get label() {
      return t({ message: "Cash", comment: "Account type `cash`: notes and coins, and the heading of its group. See GLOSSARY.md" });
    },
    get blurb() {
      return t`Notes and coins in a wallet or a tin. Nothing imports into it — you enter what you spend.`;
    },
  },
  credit_card: {
    get label() {
      return t({ message: "Credit card", comment: "Account type `credit_card`, one card. See GLOSSARY.md" });
    },
    get blurb() {
      return t`Money you owe the card issuer. Spending makes the balance more negative; paying the bill is a transfer from the account that pays it.`;
    },
  },
  other_asset: {
    get label() {
      return t({ message: "Other asset", comment: "Account type `other_asset`: something owned that holds value. See GLOSSARY.md" });
    },
    get blurb() {
      return t`Something you own that holds value and that you want in the totals — a deposit held by a landlord, an investment you track by hand.`;
    },
  },
  other_liability: {
    get label() {
      return t({ message: "Other debt", comment: "Account type `other_liability`: money owed that is not a card. See GLOSSARY.md" });
    },
    get blurb() {
      return t`Money you owe that isn't a card — a mortgage, a car loan, money owed to a person. Repayments are transfers into it.`;
    },
  },
};

/**
 * What a filter heading calls a group of accounts of one type. Plural, and in
 * two cases a different word from the form's: "Checking" for a current
 * account, "Other liabilities" for other debt. The glossary records both, and
 * both translate as one term.
 */
export const ACCOUNT_TYPE_HEADINGS: Record<string, string> = {
  get checking() { return t({ message: "Checking", comment: "Heading over the everyday bank accounts on the Accounts screen. See GLOSSARY.md" }); },
  get savings() { return t({ message: "Savings", comment: "Account type `savings`, and the heading of its group. See GLOSSARY.md" }); },
  get cash() { return t({ message: "Cash", comment: "Account type `cash`: notes and coins, and the heading of its group. See GLOSSARY.md" }); },
  get credit_card() { return t({ message: "Credit cards", comment: "Heading over the credit card accounts on the Accounts screen. See GLOSSARY.md" }); },
  get other_asset() { return t({ message: "Other assets", comment: "Heading over the other-asset accounts on the Accounts screen. See GLOSSARY.md" }); },
  get other_liability() { return t({ message: "Other liabilities", comment: "Heading over the other-debt accounts on the Accounts screen. See GLOSSARY.md" }); },
};

/** A statement line's outcome on the Import screen's preview. */
export const IMPORT_OUTCOME_WORDS: Record<string, string> = {
  get created() {
    return t({
      message: "New",
      context: "import outcome",
      comment: "Import outcome: the line becomes a new transaction. See GLOSSARY.md",
    });
  },
  get matched_existing() { return t({ message: "Already have it", comment: "Import outcome: the row is already in the ledger, typed in by hand. See GLOSSARY.md" }); },
  get duplicate_skipped() { return t({ message: "Already imported", comment: "Import outcome: the row came in with an earlier statement. See GLOSSARY.md" }); },
  get rejected() { return t({ message: "Could not read", comment: "Import outcome: the line could not be read. See GLOSSARY.md" }); },
  get needs_review() { return t({ message: "Needs a look", comment: "Import outcome: something changed, so the person has to decide. See GLOSSARY.md" }); },
  get skipped() { return t({ message: "Not for this account", comment: "Import outcome: the line belongs to another account in the file. See GLOSSARY.md" }); },
};

/** The YNAB import's steps, as its progress bar names them. */
export const YNAB_STEP_LABELS = {
  get source() { return t({ message: "Source app", comment: "Step name in the one-time import: which app the history comes from" }); },
  get connect() { return t({ message: "Connect", comment: "Step name in the one-time import: how to reach the other app (file or API key)" }); },
  get plan() { return t({ message: "Plan", comment: "Step name in the one-time import: pick which YNAB plan (budget) to bring over" }); },
  get review() { return t({ message: "Review", comment: "Step name in the one-time import: look over what was read" }); },
  get accounts() { return t({ message: "Accounts", comment: "Step name in the one-time import: map the other app's accounts. See GLOSSARY.md" }); },
  get categories() { return t({ message: "Categories", comment: "Step name in the one-time import: map the other app's categories. See GLOSSARY.md" }); },
  get options() { return t({ message: "Flags & options", comment: "Step name in the one-time import: flags, date range and other choices" }); },
  get preview() { return t({ message: "Preview", comment: "Step name in the one-time import: what the import would do, not yet done" }); },
  get report() { return t({ message: "Report", comment: "Step name in the one-time import: what the import did" }); },
} as const;

type YnabMatch = "matched" | "create" | "unmatched" | "fixed";

/** What the YNAB import will do with one of YNAB's accounts. */
export const YNAB_ACCOUNT_WORDS: Record<YnabMatch, string> = {
  get matched() { return t({ message: "Matched", comment: "Row state in the one-time import's mapping: goes to an existing one" }); },
  get create() {
    return t({
      message: "New",
      context: "new account",
      comment: "Row state in the one-time import's account mapping: a new account will be made",
    });
  },
  get unmatched() { return t({ message: "Skipped", comment: "Row state in the one-time import's account mapping: left out" }); },
  get fixed() { return t({ message: "Fixed", comment: "Row state in the one-time import's mapping: YNAB's own bucket, cannot be changed" }); },
};

/** What the YNAB import will do with one of YNAB's categories. */
export const YNAB_CATEGORY_WORDS: Record<YnabMatch, string> = {
  get matched() { return t({ message: "Matched", comment: "Row state in the one-time import's mapping: goes to an existing one" }); },
  get create() {
    return t({
      message: "New",
      context: "new category",
      comment: "Row state in the one-time import's category mapping: a new category will be made",
    });
  },
  get unmatched() { return t({ message: "Uncategorised", comment: "Row state in the one-time import's category mapping: no category. See GLOSSARY.md" }); },
  get fixed() { return t({ message: "Fixed", comment: "Row state in the one-time import's mapping: YNAB's own bucket, cannot be changed" }); },
};

/** A household role, as a member list shows it: lower case, as it always was. */
export const ROLE_LABELS: Record<Role, string> = {
  get owner() { return t({ message: "owner", comment: "Role: manages the household. Lower case, shown inside a sentence. See GLOSSARY.md" }); },
  get member() { return t({ message: "member", comment: "Role: uses the household, cannot manage it. Lower case, inside a sentence. See GLOSSARY.md" }); },
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role as Role] ?? role;
}

/**
 * What the Import preview calls each thing it worked out about a file -- the
 * reader's `Format.describe()` keys (statements/sniffing.py). An unknown key
 * reads as it always did, underscores as spaces.
 */
const DETECTED: Record<string, () => string> = {
  kind: () => t({ message: "kind", comment: "What the import detected: the kind of file. Lower case, inside a list" }),
  encoding: () => t({ message: "encoding", comment: "What the import detected: the file's text encoding. Lower case, inside a list" }),
  delimiter: () => t({ message: "delimiter", comment: "What the import detected: the character between columns. Lower case, inside a list" }),
  date_column: () => t({ message: "date column", comment: "What the import detected: which column holds the date. Lower case, inside a list" }),
  payee_column: () => t({ message: "payee column", comment: "What the import detected: which column holds the payee. Lower case, inside a list" }),
  memo_column: () => t({ message: "memo column", comment: "What the import detected: which column holds the memo. Lower case, inside a list" }),
  amount_column: () => t({ message: "amount column", comment: "What the import detected: which column holds the amount. Lower case, inside a list" }),
  outflow_column: () => t({ message: "outflow column", comment: "What the import detected: the column of money going out. Lower case, inside a list" }),
  inflow_column: () => t({ message: "inflow column", comment: "What the import detected: the column of money coming in. Lower case, inside a list" }),
  date_format: () => t({ message: "date format", comment: "What the import detected: how dates are written. Lower case, inside a list" }),
  decimal_separator: () => t({ message: "decimal separator", comment: "What the import detected: comma or point before decimals. Lower case, inside a list" }),
  state_column: () => t({ message: "state column", comment: "What the import detected: the column saying if a row happened. Lower case, inside a list" }),
  product_column: () => t({ message: "product column", comment: "What the import detected: the column naming the account. Lower case, inside a list" }),
  currency_column: () => t({ message: "currency column", comment: "What the import detected: which column holds the currency. Lower case, inside a list" }),
  fee_column: () => t({ message: "fee column", comment: "What the import detected: which column holds fees. Lower case, inside a list" }),
  balance_column: () => t({ message: "balance column", comment: "What the import detected: the running balance column. Lower case, inside a list" }),
  skip_rows: () => t({ message: "skip rows", comment: "What the import detected: how many lines above the table are skipped. Lower case" }),
};

export function detectedLabel(key: string): string {
  return DETECTED[key]?.() ?? key.replace(/_/g, " ");
}
