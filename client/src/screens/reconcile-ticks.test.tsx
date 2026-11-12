// @vitest-environment jsdom

/**
 * Reconcile counts and sends only ticks on rows the sheet shows (#445).
 *
 * The sheet is refetched when the ledger moves. A tick on a row it no longer
 * holds was counted in no total on screen and still posted, so the screen
 * said balanced while the server refused the rows. Asserted on the
 * difference, the button, and the ids that reach the server.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Reconcile } from "./Reconcile";
import type { Account } from "../lib/types";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ACCOUNT = { id: "acct-1", name: "Current", currency: "EUR" } as Account;

const FIRST = { id: "r1", date: "2026-07-01", payee: "Corner Shop", memo: "", amount: -1000 };
const SECOND = { id: "r2", date: "2026-07-02", payee: "Market Hall", memo: "", amount: -2500 };

function sheet(candidates: (typeof FIRST)[]) {
  return {
    candidates,
    locked_balance: 10000,
    last_statement_date: null,
    last_statement_balance: null,
  };
}

describe("ticks on the reconcile sheet", () => {
  it("are dropped from the total and the request once their row leaves the sheet", async () => {
    let rows = [FIRST, SECOND];
    vi.mocked(api.get).mockImplementation(((path: string) =>
      path.includes("/reconciliation?")
        ? Promise.resolve(sheet(rows))
        : Promise.resolve([])) as typeof api.get);
    vi.mocked(api.post).mockResolvedValue({});
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Reconcile account={ACCOUNT} onClose={vi.fn()} onDone={vi.fn()} />
      </QueryClientProvider>,
    );

    const ticks = await screen.findAllByRole("checkbox", { name: /^Tick (?!every)/ });
    // The bank says 100.00 - 10.00 - 25.00.
    fireEvent.change(screen.getByLabelText(/Closing balance/), { target: { value: "65.00" } });
    fireEvent.click(ticks[0]);
    fireEvent.click(ticks[1]);
    const finish = () => screen.getByRole("button", { name: /Finish and lock/ }) as HTMLButtonElement;
    expect(finish().disabled).toBe(false);
    expect(finish().textContent).toBe("Finish and lock 2 rows");

    // The first row is changed elsewhere and leaves the sheet.
    rows = [SECOND];
    await act(() => client.invalidateQueries({ queryKey: ["reconciliation", ACCOUNT.id] }));
    await waitFor(() => expect(screen.getAllByRole("checkbox", { name: /^Tick (?!every)/ })).toHaveLength(1));

    expect(finish().disabled).toBe(true);
    expect(finish().textContent).toBe("Finish and lock 1 row");

    // Balanced again on what is shown, and only that is sent.
    fireEvent.change(screen.getByLabelText(/Closing balance/), { target: { value: "75.00" } });
    expect(finish().disabled).toBe(false);
    fireEvent.click(finish());
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.post).mock.calls[0][1]).toMatchObject({
      statement_balance: 7500,
      transaction_ids: ["r2"],
    });
  });
});
