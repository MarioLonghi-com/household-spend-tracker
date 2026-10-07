// @vitest-environment jsdom

/**
 * What one register load costs, counted in requests (#101).
 *
 * The server log showed two identical `GET …/transactions` a few milliseconds
 * apart on every load and every refresh. They were not identical: the second
 * was the "Needs a category" badge asking the same path for its count, and the
 * access log drops the query string. The count now rides on the register's own
 * answer, so a load is one request per thing the screen shows -- these count
 * every request and hold that.
 *
 * Two accounts in two currencies, so nothing passes because there was only
 * one of something.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    upload: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Register } from "./Register";
import type { Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;

const ROWS = [
  {
    id: "txn-eur",
    account_id: "acc-eur",
    date: "2026-01-05",
    amount: -1_250,
    payee_id: null,
    payee_name: null,
    category_id: null,
    category_name: null,
    split_id: null,
    memo: "first",
    cleared: "uncleared",
    transfer_account_id: null,
    transfer_transaction_id: null,
    import_id: null,
    running_balance: null,
    has_receipt: false,
    currency: "EUR",
    reimbursement: null,
    reimbursed_by_id: null,
  },
  {
    id: "txn-gbp",
    account_id: "acc-gbp",
    date: "2026-01-06",
    amount: -3_400,
    payee_id: null,
    payee_name: null,
    category_id: null,
    category_name: null,
    split_id: null,
    memo: "second",
    cleared: "uncleared",
    transfer_account_id: null,
    transfer_transaction_id: null,
    import_id: null,
    running_balance: null,
    has_receipt: false,
    currency: "GBP",
    reimbursement: null,
    reimbursed_by_id: null,
  },
];

let paths: string[] = [];

beforeEach(() => {
  window.localStorage.clear();
  paths = [];
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    paths.push(path);
    if (path.includes("/transactions?"))
      return Promise.resolve({
        transactions: ROWS,
        total: ROWS.length,
        has_running_balance: false,
        capped: false,
        needs_category: 2,
      });
    if (path.endsWith("/reports/currencies"))
      return Promise.resolve({ currencies: ["EUR", "GBP"] });
    if (path.endsWith("/accounts"))
      return Promise.resolve([
        { id: "acc-eur", name: "Current", currency: "EUR", type: "checking", country: null },
        { id: "acc-gbp", name: "Savings", currency: "GBP", type: "savings", country: null },
      ]);
    return Promise.resolve([]);
  }) as typeof api.get);
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
  return client;
}

/** Long enough for every query the load starts, and anything they start. */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 600));
  });
}

const counts = () =>
  paths.reduce<Record<string, number>>((acc, path) => {
    const bare = path.split("?")[0];
    acc[bare] = (acc[bare] ?? 0) + 1;
    return acc;
  }, {});

describe("one register load", () => {
  it("asks for each thing once, and for the rows in one request", async () => {
    mount();
    await screen.findByText("second");
    await settle();

    expect(counts()).toEqual({
      "/households/house-1/accounts": 1,
      "/households/house-1/payees": 1,
      "/households/house-1/categories": 1,
      "/households/house-1/reports/currencies": 1,
      "/households/house-1/transactions": 1,
    });
  });

  it("shows the badge's count from that one request", async () => {
    mount();
    await screen.findByText("second");
    fireEvent.click(screen.getByRole("button", { name: /All categories/ }));
    await waitFor(() =>
      expect(
        screen.getByRole("group", { name: "All categories" }).textContent,
      ).toMatch(/Needs a category\s*2/),
    );
    expect(paths.filter((path) => path.includes("/transactions?"))).toHaveLength(1);
  });

  it("refetches the rows once on a refresh, not twice", async () => {
    const client = mount();
    await screen.findByText("second");
    await settle();
    const before = paths.filter((path) => path.includes("/transactions?")).length;

    await act(async () => {
      await client.invalidateQueries({ queryKey: ["register", HOUSEHOLD.id] });
    });
    await settle();

    const after = paths.filter((path) => path.includes("/transactions?")).length;
    expect(before).toBe(1);
    expect(after).toBe(2);
  });
});
