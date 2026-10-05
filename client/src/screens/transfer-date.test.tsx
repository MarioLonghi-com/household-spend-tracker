// @vitest-environment jsdom

/**
 * The transfer panel's date starts on today's local date (#23).
 *
 * At 00:30 in Madrid the UTC date is still yesterday, so the old default
 * pre-filled -- and sent -- the wrong day for the first two hours of it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Transfer } from "./Transfer";
import type { Account, Household } from "../lib/types";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

const HOUSEHOLD: Household = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
  colours: null,
};

function account(id: string, currency: string): Account {
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
    balance: 0,
    cleared: 0,
    uncleared: 0,
    transaction_count: 0,
    oldest_transaction: null,
    newest_transaction: null,
    opening_balance: 0,
    opening_date: null,
    opening_transaction_id: null,
    warnings: [],
  };
}

describe("a transfer's default date", () => {
  it("is Madrid's today at half past midnight there, and that is what is sent", async () => {
    vi.stubEnv("TZ", "Europe/Madrid");
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-03-30T22:30:00Z"));
    vi.mocked(api.post).mockResolvedValue({});

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Transfer
          household={HOUSEHOLD}
          accounts={[account("Current", "EUR"), account("Saver", "EUR")]}
          onClose={vi.fn()}
          onDone={vi.fn()}
        />
      </QueryClientProvider>,
    );

    const date = screen.getByLabelText("Date") as HTMLInputElement;
    expect(date.value).toBe("2026-03-31");

    fireEvent.change(screen.getByLabelText("Amount leaving (EUR)"), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "Transfer" }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(api.post).mock.calls[0] as [string, { date: string; amount: number }];
    expect(body.date).toBe("2026-03-31");
    expect(body.amount).toBe(1000);
  });
});
