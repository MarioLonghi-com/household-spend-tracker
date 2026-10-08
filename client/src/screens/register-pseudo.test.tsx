// @vitest-environment jsdom

/**
 * #55's exit for the register: no English left in en-XA, in the table, the
 * filters, the selection dock and a row's panel. The receipt pieces inside
 * the panel (components/Receipts.tsx) are the Receipts screen's, extracted
 * with #56, so the panel's receipt section is left out here.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { activate } from "../lib/i18n";
import { untranslated } from "../test-pseudo";

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
import type { Household, Transaction } from "../lib/types";

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;

function row(
  over: Partial<Transaction> & { id: string; currency?: string },
): Transaction & { currency: string } {
  return {
    account_id: "acc-eur",
    date: "2026-03-24",
    amount: -1200,
    payee_id: null,
    payee_name: "Shop",
    category_id: null,
    category_name: null,
    memo: null,
    cleared: "uncleared",
    transfer_account_id: null,
    transfer_transaction_id: null,
    import_id: null,
    split_id: null,
    running_balance: null,
    has_receipt: false,
    reimbursement: null,
    reimbursed_by_id: null,
    currency: "EUR",
    ...over,
  };
}

const ROWS = [
  row({
    id: "bakery",
    payee_name: "Bakery",
    category_id: "cat-0",
    category_name: "Everyday: Groceries",
  }),
  row({ id: "cinema", payee_name: "Cinema", amount: -900, memo: "Friday night" }),
  row({ id: "newsagent", account_id: "acc-gbp", currency: "GBP", payee_name: "Newsagent" }),
  row({
    id: "taxi",
    account_id: "acc-gbp",
    currency: "GBP",
    payee_name: "Taxi",
    memo: "Airport run for the client visit",
    reimbursement: "expected",
  }),
  row({
    id: "leg-out",
    payee_name: "Transfer : Savings",
    amount: -5000,
    transfer_account_id: "acc-gbp",
    transfer_transaction_id: "leg-in",
  }),
  row({
    id: "leg-in",
    account_id: "acc-gbp",
    currency: "GBP",
    payee_name: "Transfer : Current",
    amount: 4300,
    transfer_account_id: "acc-eur",
    transfer_transaction_id: "leg-out",
  }),
];

const CATEGORY_NAMES = [
  "Groceries", "Eating out", "Health", "Transport", "Clothes", "Gifts",
  "Rent", "Electricity", "Water", "Internet", "Phone", "Insurance",
];
const CATEGORIES = [
  {
    id: "grp-everyday",
    name: "Everyday",
    categories: CATEGORY_NAMES.slice(0, 6).map((name, at) => ({
      id: `cat-${at}`,
      name,
      full_name: `Everyday: ${name}`,
    })),
  },
  {
    id: "grp-bills",
    name: "Bills",
    categories: CATEGORY_NAMES.slice(6).map((name, at) => ({
      id: `cat-${at + 6}`,
      name,
      full_name: `Bills: ${name}`,
    })),
  },
];

let asked: URLSearchParams[] = [];

beforeEach(async () => {
  await activate("en-XA");
  window.localStorage.clear();
  asked = [];
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    // The badge's count beside Needs a category (#188) asks for one row;
    // it is not the register's request.
    if (path.includes("/transactions?") && !path.includes("limit=1")) {
      asked.push(new URLSearchParams(path.split("?")[1]));
      return Promise.resolve({
        transactions: ROWS,
        total: ROWS.length,
        has_running_balance: false,
        capped: false,
      });
    }
    if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR", "GBP"] });
    if (path.endsWith("/categories")) return Promise.resolve(CATEGORIES);
    if (path.endsWith("/accounts"))
      return Promise.resolve([
        { id: "acc-eur", name: "Current", currency: "EUR", type: "checking", country: "ES", flag: "🇪🇸" },
        { id: "acc-gbp", name: "Savings", currency: "GBP", type: "savings", country: "GB", flag: "🇬🇧" },
      ]);
    return Promise.resolve([]);
  }) as typeof api.get);
});

afterEach(async () => {
  cleanup();
  window.localStorage.clear();
  await activate("en");
});

/** The table's own data: payees, memos, category and account names. */
const DATA = /^(Bakery|Cinema|Newsagent|Taxi|Shop|Friday|night|Airport|run|for|the|client|visit|Transfer|Savings|Current|Everyday|Groceries|Eating|out|Health|Clothes|Gifts|Rent|Electricity|Water|Internet|Phone|Insurance|Bills|Transport)$/;

function left(root: HTMLElement): string[] {
  const copy = root.cloneNode(true) as HTMLElement;
  copy.querySelectorAll(".receipt-section, option, optgroup").forEach((one) => one.remove());
  return untranslated(copy).filter((word) => !DATA.test(word));
}

it("the register, its filters and the selection dock", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
  await screen.findByText("Bakery");
  const ticks = document.querySelectorAll<HTMLInputElement>('tbody td[data-select="true"] input');
  fireEvent.click(ticks[0]);
  fireEvent.click(ticks[1]);
  expect(left(document.body)).toEqual([]);
});

it("a row's panel", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
  await screen.findByText("Taxi");
  const row = screen.getByText("Taxi").closest("tr") as HTMLElement;
  fireEvent.click(row.querySelector('td[data-col="date"] button')!);
  await screen.findByRole("dialog");
  expect(left(document.body)).toEqual([]);
});
