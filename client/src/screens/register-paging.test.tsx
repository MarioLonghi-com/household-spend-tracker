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
import { PAGE_OVERLAP, REGISTER_PAGE, Register } from "./Register";
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
//: Rows another member has deleted since the register first asked.
const gone = new Set<string>();

/** The fake server's order: by date or by amount, largest first under desc. */
function order(params: URLSearchParams) {
  const sorted = LEDGER.filter((one) => !gone.has(one.id));
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
  gone.clear();
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
        total: sorted.length,
        has_running_balance: false,
        capped: sorted.length > offset + transactions.length,
        needs_category: sorted.length,
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

// A thousand rows drawn in jsdom take a few seconds idle, and fifty on a busy
// machine: twice the suite's deadline, because a test here waits on several
// such draws in turn, each of them allowed the suite's longest wait.
vi.setConfig({ testTimeout: 120_000 });

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
    // Each next page reaches back over the last rows in hand (#341).
    const second = String(REGISTER_PAGE - PAGE_OVERLAP);
    expect(asked.map((one) => one.get("offset"))).toEqual(["0", second]);
    expect(asked[1].get("sort")).toBe("date");
    // Five hundred and the 475 new ones the second page brought.
    expect(document.querySelector(".register-count")?.textContent).toContain(
      "the first 975 loaded",
    );

    await drawEverythingInHand();
    fireEvent.click(moreButton()!);
    await screen.findByText("row 1000");
    const third = String(2 * (REGISTER_PAGE - PAGE_OVERLAP));
    expect(asked.map((one) => one.get("offset"))).toEqual(["0", second, third]);
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

  it("loses no row when another member deletes some above the boundary between two pages (#341)", async () => {
    mount();
    await screen.findByText("row 0");
    await drawEverythingInHand();
    // Ten rows of page one go, elsewhere, before page two is asked for: by
    // offset alone, rows 500 to 509 would now be on neither page.
    for (let index = 100; index < 110; index += 1) gone.add(`txn-${index}`);

    fireEvent.click(moreButton()!);
    await screen.findByText("row 500");
    await drawEverythingInHand();
    const shown = new Set(
      [...document.querySelectorAll(".register-card tbody tr")].map((tr) => tr.textContent ?? ""),
    );
    for (let index = 495; index < 520; index += 1) {
      expect([...shown].some((text) => text.includes(`row ${index}`)), `row ${index}`).toBe(true);
    }
    expect(asked).toHaveLength(2);
  });

  it("starts again from the first page when more went than the overlap covers", async () => {
    mount();
    await screen.findByText("row 0");
    await drawEverythingInHand();
    for (let index = 100; index < 100 + 2 * PAGE_OVERLAP; index += 1) gone.add(`txn-${index}`);

    fireEvent.click(moreButton()!);
    // The second page shares no row with the first: the whole register is
    // asked for again, from the top, and row 500 is not skipped.
    await waitFor(() => expect(asked.filter((one) => one.get("offset") === "0")).toHaveLength(2));
    await screen.findByText("row 500");
    await drawEverythingInHand();
    expect(screen.queryByText("row 100")).toBeNull();
    for (const index of [499, 500, 501, 549, 550]) {
      expect(screen.getByText(`row ${index}`)).toBeTruthy();
    }
  });
});
