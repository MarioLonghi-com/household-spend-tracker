// @vitest-environment jsdom

/**
 * The register asks for a page, not the household (#100).
 *
 * It used to send no limit and get up to 25,000 rows in one answer. Now the
 * first request is five hundred rows, the next five hundred are asked for
 * when the end of what is here is reached, and sorting at a column heading
 * is still the server's: a new sort starts again from the first page, in the
 * new order.
 *
 * The fake server holds 1,234 rows in two accounts and two currencies, and
 * answers `limit`, `offset`, `sort` and `direction` the way the real one does.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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
import { REGISTER_PAGE, Register } from "./Register";
import type { Household } from "../lib/types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;
const COUNT = 1_234;

function row(index: number) {
  const eur = index % 2 === 0;
  return {
    id: `txn-${index}`,
    account_id: eur ? "acc-eur" : "acc-gbp",
    // Newest first is index 0: the date falls as the index rises.
    date: new Date(Date.UTC(2026, 0, 1) - index * 86_400_000).toISOString().slice(0, 10),
    // Shuffled against the dates, so the amount order is not the date order.
    amount: -(100 + ((index * 7_919 + 17) % COUNT)),
    payee_id: null,
    payee_name: null,
    category_id: null,
    category_name: null,
    split_id: null,
    memo: `row ${index}`,
    cleared: "uncleared",
    transfer_account_id: null,
    transfer_transaction_id: null,
    import_id: null,
    running_balance: null,
    has_receipt: false,
    currency: eur ? "EUR" : "GBP",
    reimbursement: null,
    reimbursed_by_id: null,
  };
}
const LEDGER = Array.from({ length: COUNT }, (_, index) => row(index));

let asked: URLSearchParams[] = [];

/** The fake server's order: by date or by amount, largest first under desc. */
function order(params: URLSearchParams) {
  const sorted = [...LEDGER];
  if (params.get("sort") === "amount") sorted.sort((a, b) => b.amount - a.amount);
  if (params.get("direction") === "asc") sorted.reverse();
  return sorted;
}

//: jsdom has no IntersectionObserver. This one never fires, so the rows move
//: only when the sentinel button is pressed -- which is the guarantee the
//: observer is a convenience for.
class Unseen {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords() {
    return [];
  }
}

beforeEach(() => {
  vi.stubGlobal("IntersectionObserver", Unseen);
  window.localStorage.clear();
  asked = [];
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path.includes("/transactions?")) {
      const params = new URLSearchParams(path.split("?")[1]);
      asked.push(params);
      const sorted = order(params);
      const offset = Number(params.get("offset") ?? 0);
      const limit = Number(params.get("limit") ?? 25_000);
      const transactions = sorted.slice(offset, offset + limit);
      return Promise.resolve({
        transactions,
        total: COUNT,
        has_running_balance: false,
        capped: COUNT > offset + transactions.length,
        needs_category: COUNT,
      });
    }
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
  return render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

const moreButton = () => document.querySelector<HTMLButtonElement>(".more-rows");

/** Press the sentinel until it asks the server, or there is none. */
async function drawEverythingInHand() {
  while (moreButton() && !/to load/.test(moreButton()!.textContent ?? "")) {
    fireEvent.click(moreButton()!);
  }
}

// A thousand rows drawn in jsdom take a few seconds.
vi.setConfig({ testTimeout: 30_000 });

describe("the register's pages", () => {
  it("asks for the first five hundred rows, not the whole household", async () => {
    mount();
    await screen.findByText("row 0");
    expect(asked).toHaveLength(1);
    expect(asked[0].get("limit")).toBe(String(REGISTER_PAGE));
    expect(asked[0].get("offset")).toBe("0");
    expect(REGISTER_PAGE).toBe(500);
    expect(document.querySelector(".register-count")?.textContent).toContain(
      "1,234 transactions · the first 500 loaded",
    );
  });

  it("asks for the next page at the end of what is here, and shows its rows", async () => {
    mount();
    await screen.findByText("row 0");
    await drawEverythingInHand();
    expect(moreButton()?.textContent).toContain("734 more rows to load");

    fireEvent.click(moreButton()!);
    await screen.findByText("row 500");
    expect(asked.map((one) => one.get("offset"))).toEqual(["0", "500"]);
    expect(asked[1].get("sort")).toBe("date");
    expect(document.querySelector(".register-count")?.textContent).toContain(
      "the first 1,000 loaded",
    );

    await drawEverythingInHand();
    fireEvent.click(moreButton()!);
    await screen.findByText("row 1000");
    expect(asked.map((one) => one.get("offset"))).toEqual(["0", "500", "1000"]);
    await drawEverythingInHand();
    // Everything in hand: no more to ask for, and the count is the plain one.
    expect(moreButton()).toBeNull();
    expect(document.querySelector(".register-count")?.textContent).toMatch(/^1,234 transactions/);
  });

  it("sorts at the column heading through the server, from the first page", async () => {
    mount();
    await screen.findByText("row 0");
    await drawEverythingInHand();
    fireEvent.click(moreButton()!);
    await screen.findByText("row 500");

    // The EUR Out column's heading: the server's order, from the first page.
    fireEvent.click(screen.getByTitle("Sort by Out EUR"));
    await waitFor(() => expect(asked.at(-1)?.get("sort")).toBe("amount"));
    const resorted = asked.at(-1)!;
    expect(resorted.get("offset")).toBe("0");
    expect(resorted.get("limit")).toBe(String(REGISTER_PAGE));
    // The first row is the server's first under the new order, whichever way
    // the heading turned it -- and it is not the first row of the date order.
    const first = order(resorted)[0];
    expect(first.id).not.toBe("txn-0");
    await waitFor(() =>
      expect(document.querySelector(".register-card tbody tr")?.textContent).toContain(first.memo),
    );
    expect(document.querySelector(".register-count")?.textContent).toContain(
      "the first 500 loaded",
    );
  });
});
