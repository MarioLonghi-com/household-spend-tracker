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
 * History's field names are not here: the server writes History's sentences
 * today, and they move to the client with structured History (#57).
 */

import type { AccountType, Role } from "./types";

/** What each account type is called in the form, and what it means. */
export const ACCOUNT_TYPE_LABELS: Record<AccountType, { label: string; blurb: string }> = {
  checking: {
    label: "Current account",
    blurb:
      "Day-to-day money at a bank. Salary in, card and direct debits out.",
  },
  savings: {
    label: "Savings",
    blurb:
      "Money set aside at a bank. Same as a current account, kept apart so the register reads clearly.",
  },
  cash: {
    label: "Cash",
    blurb:
      "Notes and coins in a wallet or a tin. Nothing imports into it — you enter what you spend.",
  },
  credit_card: {
    label: "Credit card",
    blurb:
      "Money you owe the card issuer. Spending makes the balance more negative; paying the bill is a transfer from the account that pays it.",
  },
  other_asset: {
    label: "Other asset",
    blurb:
      "Something you own that holds value and that you want in the totals — a deposit held by a landlord, an investment you track by hand.",
  },
  other_liability: {
    label: "Other debt",
    blurb:
      "Money you owe that isn't a card — a mortgage, a car loan, money owed to a person. Repayments are transfers into it.",
  },
};

/**
 * What a filter heading calls a group of accounts of one type. Plural, and in
 * two cases a different word from the form's: "Checking" for a current
 * account, "Other liabilities" for other debt. The glossary records both, and
 * both translate as one term.
 */
export const ACCOUNT_TYPE_HEADINGS: Record<string, string> = {
  checking: "Checking",
  savings: "Savings",
  cash: "Cash",
  credit_card: "Credit cards",
  other_asset: "Other assets",
  other_liability: "Other liabilities",
};

/** A statement line's outcome on the Import screen's preview. */
export const IMPORT_OUTCOME_WORDS: Record<string, string> = {
  created: "New",
  matched_existing: "Already have it",
  duplicate_skipped: "Already imported",
  rejected: "Could not read",
  needs_review: "Needs a look",
  skipped: "Not for this account",
};

/** The YNAB import's steps, as its progress bar names them. */
export const YNAB_STEP_LABELS = {
  source: "Source app",
  connect: "Connect",
  plan: "Plan",
  review: "Review",
  accounts: "Accounts",
  categories: "Categories",
  options: "Flags & options",
  preview: "Preview",
  report: "Report",
} as const;

type YnabMatch = "matched" | "create" | "unmatched" | "fixed";

/** What the YNAB import will do with one of YNAB's accounts. */
export const YNAB_ACCOUNT_WORDS: Record<YnabMatch, string> = {
  matched: "Matched",
  create: "New",
  unmatched: "Skipped",
  fixed: "Fixed",
};

/** What the YNAB import will do with one of YNAB's categories. */
export const YNAB_CATEGORY_WORDS: Record<YnabMatch, string> = {
  matched: "Matched",
  create: "New",
  unmatched: "Uncategorised",
  fixed: "Fixed",
};

/** A household role, as a member list shows it: lower case, as it always was. */
export const ROLE_LABELS: Record<Role, string> = {
  owner: "owner",
  member: "member",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role as Role] ?? role;
}
