// @vitest-environment jsdom

/**
 * The shared row picker, and the receipts screen's use of it.
 *
 * The picker was the receipts screen's own `Matcher` until the register's
 * "Find the payment…" needed the same thing, so the first half of this pins
 * what the receipts screen had: the window it asks the server for (ten days
 * either side of when the photo was taken), the rows it shows, the order the
 * headings put them in, and the request an Attach sends. The second half is
 * what the generalisation added -- a wider window and a row filter.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

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
import { RowPicker, shiftDays } from "./RowPicker";
import { Matcher, PICKER_DAYS } from "../screens/Receipts";
import type { Household, Receipt, Transaction } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;

function txn(id: string, date: string, amount: number, payee: string, over: Partial<Transaction> = {}) {
  return {
    id,
    account_id: amount > 0 ? "acc-checking" : "acc-visa",
    date,
    amount,
    payee_id: null,
    payee_name: payee,
    category_id: null,
    category_name: null,
    memo: null,
    cleared: "cleared",
    transfer_account_id: null,
    transfer_transaction_id: null,
    import_id: null,
    split_id: null,
    running_balance: null,
    has_receipt: false,
    reimbursement: null,
    reimbursed_by_id: null,
    currency: amount > 0 ? "EUR" : "GBP",
    ...over,
  } as Transaction & { currency: string };
}

/** Two of everything: two in, two out, two currencies, one transfer leg. */
const ROWS = [
  txn("out-hotel", "2026-09-12", -24000, "Hotel"),
  txn("out-taxi", "2026-09-14", -2340, "Cabify"),
  txn("in-work", "2026-09-30", 32450, "Employer Ltd"),
  txn("in-refund", "2026-09-20", 1500, "Shop refund"),
  txn("in-transfer", "2026-09-21", 50000, "From savings", { transfer_account_id: "acc-savings" }),
];

let asked: string[] = [];

beforeEach(() => {
  asked = [];
  vi.mocked(api.get).mockReset();
  vi.mocked(api.patch).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path.includes("/transactions?")) {
      asked.push(path);
      return Promise.resolve({ transactions: ROWS, total: ROWS.length, has_running_balance: false, capped: false });
    }
    if (path.endsWith("/accounts")) return Promise.resolve([]);
    return Promise.resolve([]);
  }) as typeof api.get);
  vi.mocked(api.patch).mockResolvedValue({});
});

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}

const payees = () =>
  screen.getAllByRole("row").slice(1).map((one) => one.querySelector("[data-primary]")?.textContent);

describe("the receipts screen's matcher, through the shared picker", () => {
  const receipt = {
    id: "rcpt-1",
    captured_at: "2026-09-19T18:42:07Z",
    created_at: "2026-09-25T09:00:00Z",
  } as Receipt;

  it("asks for ten days either side of when the photo was taken", async () => {
    wrap(<Matcher household={HOUSEHOLD} receipt={receipt} onClose={() => {}} onDone={() => {}} />);
    await waitFor(() => expect(asked.length).toBe(1));
    expect(PICKER_DAYS).toBe(10);
    const params = new URLSearchParams(asked[0].split("?")[1]);
    expect(params.get("since")).toBe("2026-09-09");
    expect(params.get("until")).toBe("2026-09-29");
    expect(screen.getByText(/ten days either side/)).toBeTruthy();
  });

  it("offers every row in the window, newest first, and re-sorts at a heading", async () => {
    wrap(<Matcher household={HOUSEHOLD} receipt={receipt} onClose={() => {}} onDone={() => {}} />);
    await screen.findByText("Hotel");
    expect(payees()).toEqual(["Employer Ltd", "From savings", "Shop refund", "Cabify", "Hotel"]);
    fireEvent.click(screen.getByRole("button", { name: /Payee/ }));
    expect(payees()).toEqual(["Cabify", "Employer Ltd", "From savings", "Hotel", "Shop refund"]);
  });

  it("attaches the receipt to the row picked", async () => {
    const done = vi.fn();
    wrap(<Matcher household={HOUSEHOLD} receipt={receipt} onClose={() => {}} onDone={done} />);
    await screen.findByText("Hotel");
    const hotelRow = screen.getByText("Hotel").closest("tr")!;
    fireEvent.click(hotelRow.querySelector("button")!);
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(api.patch).toHaveBeenCalledWith("/receipts/rcpt-1", { transaction_id: "out-hotel" });
  });

  it("opens on the upload time for a receipt with no date of its own", async () => {
    wrap(
      <Matcher
        household={HOUSEHOLD}
        receipt={{ ...receipt, captured_at: null }}
        onClose={() => {}}
        onDone={() => {}}
      />,
    );
    await waitFor(() => expect(asked.length).toBe(1));
    const params = new URLSearchParams(asked[0].split("?")[1]);
    expect(params.get("since")).toBe("2026-09-15");
    expect(params.get("until")).toBe("2026-10-05");
  });
});

describe("the picker for a payment", () => {
  it("opens as wide as it is told and shows only the rows it accepts", async () => {
    const pick = vi.fn();
    wrap(
      <RowPicker
        household={HOUSEHOLD}
        title="Which payment repaid this?"
        anchor="2026-09-12"
        days={45}
        accept={(one) => one.amount > 0 && !one.transfer_account_id}
        action="Link"
        onPick={pick}
        onClose={() => {}}
      />,
    );
    await screen.findByText("Employer Ltd");
    const params = new URLSearchParams(asked[0].split("?")[1]);
    expect(params.get("since")).toBe("2026-07-29");
    expect(params.get("until")).toBe("2026-10-27");
    expect(payees()).toEqual(["Employer Ltd", "Shop refund"]);

    fireEvent.click(screen.getByText("Employer Ltd").closest("tr")!.querySelector("button")!);
    expect(pick).toHaveBeenCalledWith(expect.objectContaining({ id: "in-work" }));
  });

  it("draws each figure in the row's own currency", async () => {
    wrap(
      <RowPicker
        household={HOUSEHOLD}
        title="t"
        anchor="2026-09-12"
        days={45}
        action="Link"
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    await screen.findByText("Hotel");
    const hotel = screen.getByText("Hotel").closest("tr")!.querySelector("[data-figure]")!;
    expect(hotel.textContent).toMatch(/£/);
  });

  it("moves a date by whole days across a daylight-saving change", () => {
    expect(shiftDays("2026-03-28", 2)).toBe("2026-03-30");
    expect(shiftDays("2026-10-27T10:00:00Z", -45)).toBe("2026-09-12");
  });
});
