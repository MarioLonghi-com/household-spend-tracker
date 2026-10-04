// @vitest-environment jsdom

/**
 * A transfer leg has no category, on screen (#124).
 *
 * The server refuses one; these check the register never offers one either --
 * not in the cell, not in the panel -- and that a bulk change which skipped
 * legs says how many it skipped. Two accounts, two currencies, two ordinary
 * rows and both legs of one transfer, so a check that only looked at the first
 * row cannot pass by accident.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";

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
import { Register, isTransferLeg, skippedNote } from "./Register";
import type { Household, Transaction } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
} as unknown as Household;

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
  row({ id: "plain-eur", payee_name: "Bakery", category_id: "cat-1", category_name: "Everyday: Groceries" }),
  row({ id: "plain-gbp", account_id: "acc-gbp", currency: "GBP", payee_name: "Newsagent" }),
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

beforeEach(() => {
  // The register remembers its filters (#143); each test starts from none.
  window.localStorage.clear();
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path.includes("/transactions?"))
      return Promise.resolve({ transactions: ROWS, total: ROWS.length, has_running_balance: false, capped: false });
    if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR", "GBP"] });
    if (path.endsWith("/categories"))
      return Promise.resolve([
        { id: "grp-1", name: "Everyday", categories: [{ id: "cat-1", name: "Groceries", full_name: "Everyday: Groceries" }] },
      ]);
    if (path.endsWith("/accounts"))
      return Promise.resolve([
        { id: "acc-eur", name: "Current", currency: "EUR", type: "checking", country: null },
        { id: "acc-gbp", name: "Savings", currency: "GBP", type: "savings", country: null },
      ]);
    return Promise.resolve([]);
  }) as typeof api.get);
});

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
  await screen.findByText("Bakery");
  return view;
}

function rowOf(payee: string): HTMLElement {
  return screen.getByText(payee).closest("tr") as HTMLElement;
}

describe("a transfer leg's category", () => {
  it("is a leg when either link column is set, and not otherwise", () => {
    expect(isTransferLeg({ transfer_account_id: "a", transfer_transaction_id: null })).toBe(true);
    expect(isTransferLeg({ transfer_account_id: null, transfer_transaction_id: "b" })).toBe(true);
    expect(isTransferLeg({ transfer_account_id: null, transfer_transaction_id: null })).toBe(false);
  });

  it("shows 'No category needed' on both legs, with nothing to click", async () => {
    await mount();
    for (const payee of ["Transfer : Savings", "Transfer : Current"]) {
      const cell = within(rowOf(payee)).getByText("No category needed");
      expect(cell.closest("button")).toBeNull();
      expect(cell.className).toContain("muted");
    }
    // The ordinary rows keep their editable cell.
    expect(within(rowOf("Bakery")).getByRole("button", { name: /Everyday: Groceries/ })).toBeTruthy();
    expect(within(rowOf("Newsagent")).getByText("uncategorised").closest("button")).not.toBeNull();
  });

  it("greys the panel's Category field out on a leg, and not on an ordinary row", async () => {
    await mount();
    fireEvent.click(within(rowOf("Transfer : Current")).getByRole("button", { name: "2026-03-24" }));
    const field = (await screen.findByDisplayValue("No category needed")) as HTMLInputElement;
    expect(field.disabled).toBe(true);

    // An ordinary row's panel keeps the chooser, and no greyed field.
    cleanup();
    await mount();
    fireEvent.click(within(rowOf("Bakery")).getByRole("button", { name: "2026-03-24" }));
    await screen.findByDisplayValue("Everyday: Groceries");
    expect(screen.queryByDisplayValue("No category needed")).toBeNull();
  });

  it("says how many legs a bulk category change skipped", async () => {
    vi.mocked(api.post).mockResolvedValue({
      transactions: ROWS,
      skipped_transfer_legs: 2,
    });
    await mount();
    for (const payee of ["Bakery", "Newsagent", "Transfer : Savings", "Transfer : Current"]) {
      fireEvent.click(within(rowOf(payee)).getByRole("checkbox"));
    }
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Set the category on the selected rows"), {
        target: { value: "cat-1" },
      });
    });

    expect(api.post).toHaveBeenCalledWith("/households/house-1/transactions/bulk", {
      transaction_ids: ["plain-eur", "plain-gbp", "leg-out", "leg-in"],
      category_id: "cat-1",
    });
    expect(
      await screen.findByText(/Set the category on 2 rows and skipped 2 transfer legs/),
    ).toBeTruthy();
  });

  it("words the note for one and for none", () => {
    expect(skippedNote(0, 5)).toBeNull();
    expect(skippedNote(1, 3)).toBe(
      "Set the category on 2 rows and skipped 1 transfer leg — a transfer has no category.",
    );
    expect(skippedNote(2, 3)).toBe(
      "Set the category on 1 row and skipped 2 transfer legs — a transfer has no category.",
    );
  });
});
