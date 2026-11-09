// @vitest-environment jsdom

/**
 * The Exchange rates page (#396 §5.3): it draws from one status read, says what
 * Fetch now would do before anyone presses it, sends the press, and every list
 * on it sorts at its headers. Two foreign currencies, so a sort has something
 * to reorder.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { ExchangeRates } from "./ExchangeRates";
import type { FxCurrency, FxStatus } from "../lib/fx";
import type { Household } from "../lib/types";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const HOUSE = { id: "h1", name: "Home", base_currency: "EUR" } as Household;

function currency(code: string, age: number, over: Partial<FxCurrency> = {}): FxCurrency {
  return {
    currency: code,
    accounts: [{ id: `a-${code}`, name: `${code} account`, institution: null, country: null, country_name: null, flag: "", closed: false }],
    source_in_use: "ecb",
    first_rate: "2026-10-29",
    last_rate: "2026-11-04",
    rates: 5,
    age_days: age,
    transaction_dates: 2,
    coverage_percent: 100,
    gaps: 0,
    status: "ok",
    own_rates: 0,
    iso: true,
    months: [{ month: "2026-10", state: "rates" }, { month: "2026-11", state: "gap" }],
    ...over,
  };
}

const STATUS: FxStatus = {
  base_currency: "EUR",
  chain: ["manual", "ecb"],
  stale_days: 7,
  monthly_policy: "month_avg",
  show_own_rates: true,
  tiles: {
    last_fetch: null,
    as_of: "2026-11-04",
    stale: false,
    covered: 2,
    total: 2,
    missing: [],
    gaps: 0,
    manual_count: 0,
    manual_changed_at: null,
    manual_changed_by: null,
  },
  running: false,
  running_fetches: [],
  would_fetch: [{ source: "ecb", mode: "missing", currencies: ["GBP", "SEK"], since: "2026-11-04", until: "2026-11-09" }],
  would_fetch_refused: null,
  would_fetch_reason: null,
  currencies: [currency("GBP", 5), currency("SEK", 2, { status: "stale" })],
  not_covered: [],
  history: [],
  rates_status: {
    as_of: "2026-11-04",
    stale: false,
    running: false,
    fetched_at: null,
    fetched_by: null,
    last_fetch_failed: null,
    chain: ["manual", "ecb"],
    stale_days: 7,
    remote_in_chain: true,
    manual_changed_at: null,
    manual_changed_by: null,
    per_currency: [
      { currency: "GBP", rate_date: "2026-11-04", source: "ecb", stale: false, age_days: 5 },
      { currency: "SEK", rate_date: "2026-11-04", source: "ecb", stale: false, age_days: 5 },
    ],
    not_converted: [],
  },
};

function mount() {
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path.endsWith("/fx/status")) return Promise.resolve(STATUS);
    return Promise.resolve({ base_currency: "EUR", total: 0, limit: 500, rates: [] });
  });
  vi.mocked(api.post).mockResolvedValue([]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ExchangeRates household={HOUSE} />
    </QueryClientProvider>,
  );
}

function perCurrency(): string[] {
  const table = screen.getByRole("heading", { name: "Per currency" }).closest("section")!;
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent!.slice(0, 3));
}

describe("the Exchange rates page", () => {
  it("says how old the rates are and what Fetch now would do, then sends the press", async () => {
    mount();
    expect(await screen.findByText(/Would fetch GBP, SEK from ECB, from 2026-11-04 to today\./)).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("Rates as of 2026-11-04 · ECB");

    fireEvent.click(screen.getByRole("button", { name: "Fetch now" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/households/h1/fx/fetch", { mode: "missing" }));
  });

  it("sorts the currencies at their headers", async () => {
    mount();
    await screen.findByRole("heading", { name: "Per currency" });
    expect(perCurrency()).toEqual(["GBP", "SEK"]);
    const table = screen.getByRole("heading", { name: "Per currency" }).closest("section")!;
    fireEvent.click(within(table).getByRole("button", { name: "Age" }));
    expect(perCurrency()).toEqual(["SEK", "GBP"]);
    fireEvent.click(within(table).getByRole("button", { name: "Age" }));
    expect(perCurrency()).toEqual(["GBP", "SEK"]);
  });

  it("draws a month strip whose cells say their state in words", async () => {
    mount();
    const strip = await screen.findByRole("list", { name: "Months of GBP" });
    const cells = within(strip).getAllByRole("listitem");
    expect(cells.map((cell) => cell.getAttribute("aria-label"))).toEqual([
      "Oct 2026: has rates",
      "Nov 2026: transactions without a rate",
    ]);
  });
});
