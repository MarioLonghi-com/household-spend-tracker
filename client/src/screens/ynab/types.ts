/**
 * The One-time Import · YNAB contract (#183), as the client reads it.
 *
 * Every route is stateless: the source -- the file, or the token and the plan
 * -- goes up again on every call, and so does the whole `plan` once there is
 * one. Nothing the wizard decides is held by the server between steps.
 */

import type { AccountType } from "../../lib/types";

export type Via = "csv" | "api";

/** Where the rows come from. The token lives here and nowhere else. */
export type Source =
  | { via: "csv"; file: File }
  | { via: "api"; token: string; planId: string };

export interface YnabPlan {
  id: string;
  name: string;
  currency: string | null;
  last_modified_on: string | null;
  first_month: string | null;
  last_month: string | null;
}

export type AccountSuggestion =
  | { kind: "existing"; account_id: string; score: number }
  | { kind: "create"; name: string; type: AccountType };

export type CategorySuggestion =
  | { kind: "existing"; category_id: string; score: number }
  | { kind: "uncategorised" }
  | { kind: "create"; name: string };

export interface YnabAccount {
  key: string;
  name: string;
  rows: number;
  date_min: string | null;
  date_max: string | null;
  balance_minor: number;
  type_hint: AccountType | null;
  closed: boolean | null;
  suggestion: AccountSuggestion;
}

export interface YnabCategory {
  key: string;
  name: string;
  groups: string[];
  rows: number;
  fixed_uncategorised: boolean;
  suggestion: CategorySuggestion;
}

export interface TargetAccount {
  id: string;
  name: string;
  type: AccountType;
  currency: string;
  institution: string | null;
  identifiers_masked: string[];
  closed: boolean;
  balance_minor: number;
  txn_count: number;
  first_date: string | null;
  last_date: string | null;
  eligible: boolean;
}

export interface TargetCategory {
  id: string;
  name: string;
  group_name: string;
  archived: boolean;
}

export interface PreviousImport {
  batch_id: string;
  at: string;
  via: Via;
  filename: string | null;
  plan_name: string | null;
  status: string;
}

export interface Analysis {
  source: { via: Via; filename: string | null; plan_name: string | null };
  currency: { detected: string | null; symbol: string | null; confirmed_needed: boolean };
  date_format: { detected: string | null; ambiguous: boolean; options: string[] };
  totals: {
    rows: number;
    date_min: string | null;
    date_max: string | null;
    transfers: number;
    splits: number;
    starting_balance_rows: number;
    cleared: { reconciled: number; cleared: number; uncleared: number };
  };
  flags: { label: string; count: number }[];
  accounts: YnabAccount[];
  categories: YnabCategory[];
  targets: { accounts: TargetAccount[]; categories: TargetCategory[] };
  previous_imports: PreviousImport[];
}

export type AccountChoice =
  | { kind: "existing"; account_id: string }
  | { kind: "create"; name: string; type: AccountType }
  | { kind: "skip" };

export type CategoryChoice =
  | { kind: "existing"; category_id: string }
  | { kind: "create"; name: string; group_name?: string }
  | { kind: "uncategorised" };

export interface ImportPlan {
  currency: string;
  date_format: string | null;
  accounts: Record<string, AccountChoice>;
  categories: Record<string, CategoryChoice>;
  flags: "memo" | "ignore";
  starting_balance: "import" | "skip";
  date_from: string | null;
  date_to: string | null;
  acknowledge_cleared_reset: boolean;
  duplicates: { all: "skip" | "import" | null; import: string[] };
}

export interface Duplicate {
  row_ref: string;
  date: string;
  account: string;
  payee: string | null;
  amount_minor: number;
  memo: string | null;
  existing: { id: string; date: string; payee: string | null; amount_minor: number };
  decision: "skip" | "import" | "pending";
}

export interface NotImported {
  row_ref: string;
  /** Null when the date could not be read; `date_text` is what the source wrote. */
  date: string | null;
  date_text: string | null;
  account: string | null;
  payee: string | null;
  category: string | null;
  memo: string | null;
  amount_minor: number | null;
  reason: string;
  /** `reason` as a code and raw params, for a screen not in English (#267). */
  reason_code?: string | null;
  reason_params?: Record<string, unknown> | null;
}

export interface ImportReport {
  committed: boolean;
  batch_id: string | null;
  counts: {
    rows_in_file: number;
    imported: number;
    transfers_linked: number;
    duplicates_skipped: number;
    duplicates_imported: number;
    skipped_account: number;
    skipped_date_range: number;
    skipped_starting_balance: number;
    failed: number;
  };
  created: {
    accounts: { id: string; name: string; opening_date: string }[];
    categories: { id: string; name: string }[];
    payees: number;
  };
  duplicates: Duplicate[];
  not_imported: NotImported[];
  /**
   * Transfers imported as ordinary transactions because their other side was
   * not imported. They *were* imported, so they are not in `not_imported`.
   */
  unpaired_transfers: NotImported[];
  /**
   * Groups of bank strings that look like one payee each: the Rules screen's
   * suggestions, the whole household's, counted once the import committed
   * (#269). Null on a preview.
   */
  rule_suggestions?: number | null;
  /**
   * Source lines that brought the bank's own text, which rule suggestions read
   * (#265). Per line in the source, not per row: a split counts once.
   */
  bank_text_rows: number;
  /**
   * API imports only: each account whose rows do not add up to YNAB's own
   * balance (#266). Absent or empty when everything adds up.
   */
  balance_differences?: BalanceDifference[];
  /** Accounts whose YNAB balance could not be compared at all (#266). */
  balance_unchecked?: {
    account_key?: string;
    account: string;
    reason: string;
    sentence: string;
    /** `sentence` as a code and raw params (#267). */
    sentence_code?: string | null;
    sentence_params?: Record<string, unknown> | null;
  }[];
  report_text: string;
}

export interface BalanceDifference {
  /** The YNAB account's id: two YNAB accounts may share a name. */
  account_key?: string;
  account: string;
  currency: string;
  ynab_balance_minor: number;
  /** What the import wrote plus what it left out on purpose. */
  imported_minor: number;
  /** `imported_minor - ynab_balance_minor`. */
  difference_minor: number;
  sentence: string;
}
