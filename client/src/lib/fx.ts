/**
 * Exchange rates, as the server sends them (#396).
 *
 * The ledger never converts. These are the rates a report may convert with,
 * and how old they are. A rate is always a decimal *string*, never a number:
 * it is shown as it came, and nothing here does arithmetic with it.
 */

import { t } from "@lingui/core/macro";

/** One currency's newest rate, in `rates_status` (`CurrencyStatusOut`). */
export interface CurrencyStatus {
  currency: string;
  rate_date: string | null;
  source: string | null;
  stale: boolean;
  age_days: number | null;
}

/**
 * `RatesStatusOut`: what the "Rates as of" line is drawn from. It comes in the
 * same response as the figures it describes, so the two cannot disagree.
 */
export interface RatesStatus {
  as_of: string | null;
  stale: boolean;
  running: boolean;
  fetched_at: string | null;
  fetched_by: string | null;
  last_fetch_failed: { at: string; error: string } | null;
  chain: string[];
  stale_days: number;
  remote_in_chain: boolean;
  manual_changed_at: string | null;
  manual_changed_by: string | null;
  per_currency: CurrencyStatus[];
  not_converted: string[];
}

export interface FxFetch {
  id: string;
  source: string;
  mode: "missing" | "range";
  requested_since: string;
  requested_until: string;
  currencies: string[];
  started_at: string;
  finished_at: string | null;
  outcome: "running" | "ok" | "failed";
  rates_added: number;
  rates_unchanged: number;
  error: string | null;
  started_by: string | null;
  duration_ms: number | null;
}

export interface FxAccountRef {
  id: string;
  name: string;
}

export interface FxCurrency {
  currency: string;
  accounts: FxAccountRef[];
  source_in_use: string | null;
  first_rate: string | null;
  last_rate: string | null;
  rates: number;
  age_days: number | null;
  transaction_dates: number;
  coverage_percent: number | null;
  gaps: number;
  status: "ok" | "stale" | "not_covered" | "manual_only";
  own_rates: number;
  iso: boolean;
  months: { month: string; state: "rates" | "gap" | "empty" }[];
}

export interface FxStatus {
  base_currency: string;
  chain: string[];
  stale_days: number;
  monthly_policy: string;
  show_own_rates: boolean;
  tiles: {
    last_fetch: FxFetch | null;
    as_of: string | null;
    stale: boolean;
    covered: number;
    total: number;
    missing: string[];
    gaps: number;
    manual_count: number;
    manual_changed_at: string | null;
    manual_changed_by: string | null;
  };
  running: boolean;
  running_fetches: FxFetch[];
  would_fetch: { source: string; mode: string; currencies: string[]; since: string; until: string }[];
  would_fetch_refused: string | null;
  would_fetch_reason: string | null;
  currencies: FxCurrency[];
  not_covered: { currency: string; reason: "not_iso" | "no_source"; accounts: FxAccountRef[] }[];
  history: FxFetch[];
  rates_status: RatesStatus;
}

export interface FxSettings {
  base_currency: string;
  chain: string[];
  stale_days: number;
  monthly_policy: string;
  show_own_rates: boolean;
}

export interface FxRate {
  id: string | null;
  source: string;
  quote_currency: string;
  currency: string;
  date: string;
  period: "day" | "month";
  rate: string;
  rate_in_base: string | null;
  own_rate: string | null;
}

export interface FxRates {
  base_currency: string;
  total: number;
  limit: number;
  rates: FxRate[];
}

export interface FxManualRow {
  line: number;
  currency: string;
  date: string;
  rate: string;
  period: "day" | "month" | null;
  inverse: string | null;
  replaces: string | null;
  unchanged: boolean;
  problems: string[];
}

export interface FxManualOut {
  dry_run: boolean;
  added: number;
  replaced: number;
  unchanged: number;
  rows: FxManualRow[];
}

/** Every source a chain can hold, in the order the settings offer them. */
export const SOURCES = ["manual", "ecb", "own"] as const;

/** A source's name as a person reads it. */
export function sourceLabel(source: string | null | undefined): string {
  switch (source) {
    case "ecb":
      return t({ message: "ECB", comment: "Short name of the European Central Bank, a source of exchange rates" });
    case "manual":
      return t({ message: "entered by hand", comment: "Where an exchange rate came from: a member typed or imported it" });
    case "own":
      return t({ message: "your own transfers", comment: "Where an exchange rate came from: what the household's own currency transfers gave" });
    case "base":
      return t({ message: "base currency", comment: "Where an exchange rate came from: none was needed, the amount is already in the base currency" });
    default:
      return source ?? "—";
  }
}
