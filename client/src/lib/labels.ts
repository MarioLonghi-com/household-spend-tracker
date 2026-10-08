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
      return t`Current account`;
    },
    get blurb() {
      return t`Day-to-day money at a bank. Salary in, card and direct debits out.`;
    },
  },
  savings: {
    get label() {
      return t`Savings`;
    },
    get blurb() {
      return t`Money set aside at a bank. Same as a current account, kept apart so the register reads clearly.`;
    },
  },
  cash: {
    get label() {
      return t`Cash`;
    },
    get blurb() {
      return t`Notes and coins in a wallet or a tin. Nothing imports into it — you enter what you spend.`;
    },
  },
  credit_card: {
    get label() {
      return t`Credit card`;
    },
    get blurb() {
      return t`Money you owe the card issuer. Spending makes the balance more negative; paying the bill is a transfer from the account that pays it.`;
    },
  },
  other_asset: {
    get label() {
      return t`Other asset`;
    },
    get blurb() {
      return t`Something you own that holds value and that you want in the totals — a deposit held by a landlord, an investment you track by hand.`;
    },
  },
  other_liability: {
    get label() {
      return t`Other debt`;
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
  get checking() { return t`Checking`; },
  get savings() { return t`Savings`; },
  get cash() { return t`Cash`; },
  get credit_card() { return t`Credit cards`; },
  get other_asset() { return t`Other assets`; },
  get other_liability() { return t`Other liabilities`; },
};

/** A statement line's outcome on the Import screen's preview. */
export const IMPORT_OUTCOME_WORDS: Record<string, string> = {
  get created() { return t`New`; },
  get matched_existing() { return t`Already have it`; },
  get duplicate_skipped() { return t`Already imported`; },
  get rejected() { return t`Could not read`; },
  get needs_review() { return t`Needs a look`; },
  get skipped() { return t`Not for this account`; },
};

/** The YNAB import's steps, as its progress bar names them. */
export const YNAB_STEP_LABELS = {
  get source() { return t`Source app`; },
  get connect() { return t`Connect`; },
  get plan() { return t`Plan`; },
  get review() { return t`Review`; },
  get accounts() { return t`Accounts`; },
  get categories() { return t`Categories`; },
  get options() { return t`Flags & options`; },
  get preview() { return t`Preview`; },
  get report() { return t`Report`; },
} as const;

type YnabMatch = "matched" | "create" | "unmatched" | "fixed";

/** What the YNAB import will do with one of YNAB's accounts. */
export const YNAB_ACCOUNT_WORDS: Record<YnabMatch, string> = {
  get matched() { return t`Matched`; },
  get create() { return t`New`; },
  get unmatched() { return t`Skipped`; },
  get fixed() { return t`Fixed`; },
};

/** What the YNAB import will do with one of YNAB's categories. */
export const YNAB_CATEGORY_WORDS: Record<YnabMatch, string> = {
  get matched() { return t`Matched`; },
  get create() { return t`New`; },
  get unmatched() { return t`Uncategorised`; },
  get fixed() { return t`Fixed`; },
};

/** A household role, as a member list shows it: lower case, as it always was. */
export const ROLE_LABELS: Record<Role, string> = {
  get owner() { return t`owner`; },
  get member() { return t`member`; },
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
  kind: () => t`kind`,
  encoding: () => t`encoding`,
  delimiter: () => t`delimiter`,
  date_column: () => t`date column`,
  payee_column: () => t`payee column`,
  memo_column: () => t`memo column`,
  amount_column: () => t`amount column`,
  outflow_column: () => t`outflow column`,
  inflow_column: () => t`inflow column`,
  date_format: () => t`date format`,
  decimal_separator: () => t`decimal separator`,
  state_column: () => t`state column`,
  product_column: () => t`product column`,
  currency_column: () => t`currency column`,
  fee_column: () => t`fee column`,
  balance_column: () => t`balance column`,
  skip_rows: () => t`skip rows`,
};

export function detectedLabel(key: string): string {
  return DETECTED[key]?.() ?? key.replace(/_/g, " ");
}
