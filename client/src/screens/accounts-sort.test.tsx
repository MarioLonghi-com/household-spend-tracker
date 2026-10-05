// @vitest-environment jsdom

/**
 * The Accounts list's money columns (#129).
 *
 * Cleared and Balance sort by currency first, then figure, so EUR and GBP never
 * interleave. What this pins down is the order of the currency *groups*: the
 * household's base currency first, then the others A to Z, in both directions.
 * Flipping them with the figures made a descending column read as unsorted.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Accounts } from "./Accounts";
import type { Account, Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD: Household = {
  id: "house-1",
  name: "Ours",
  base_currency: "GBP",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
  colours: null,
};

function account(id: string, currency: string, balance: number): Account {
  return {
    id,
    name: id,
    type: "checking",
    currency,
    closed: false,
    note: null,
    institution: null,
    country: null,
    flag: "",
    is_liability: false,
    sort_order: 0,
    statement_product: null,
    balance,
    cleared: balance,
    uncleared: 0,
    transaction_count: 1,
    oldest_transaction: "2026-01-01",
    newest_transaction: "2026-01-02",
    opening_balance: 0,
    opening_date: null,
    opening_transaction_id: null,
    warnings: [],
  };
}

// A GBP household, so pounds come first even though EUR sorts ahead of GBP
// alphabetically -- two households' worth of currencies in one list.
const ACCOUNTS = [
  account("eur-small", "EUR", 500),
  account("gbp-big", "GBP", 9000),
  account("eur-big", "EUR", 7000),
  account("gbp-small", "GBP", 1500),
];

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Accounts household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

describe("Accounts money columns", () => {
  beforeEach(() => {
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(async (url: string) => (url.includes("/accounts") ? ACCOUNTS : []) as never);
  });

  for (const column of ["Balance", "Cleared"]) {
    it(`keeps the base currency's group first when ${column} turns round`, async () => {
      mount();
      await screen.findByText("gbp-big");
      const table = screen.getAllByRole("table")[0];
      const order = () =>
        within(table)
          .getAllByRole("row")
          .slice(1)
          .map((row) => ACCOUNTS.find((a) => within(row).queryByText(a.name))?.name);
      const heading = within(table).getByRole("button", { name: new RegExp(column) });
      // The first click sorts ascending; the second turns the figures round
      // and leaves the pounds on top.
      fireEvent.click(heading);
      expect(order()).toEqual(["gbp-small", "gbp-big", "eur-small", "eur-big"]);
      fireEvent.click(heading);
      expect(order()).toEqual(["gbp-big", "gbp-small", "eur-big", "eur-small"]);
    });
  }
});
