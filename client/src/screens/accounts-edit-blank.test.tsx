// @vitest-environment jsdom

/**
 * The account panel's bank and note (#20).
 *
 * The New account panel sends both trimmed and a blank one as null; the edit
 * panel sent them raw, so an emptied field was stored as "" and a padded one
 * kept its spaces. On the PATCH null means "leave it alone", so emptying a
 * field that had a value has to say `clear_institution` / `clear_note`, the
 * way the country says `clear_country`. And a panel that stays open after a
 * save shows what was stored, not what was typed.
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

function account(id: string, currency: string, extra: Partial<Account> = {}): Account {
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
    transaction_count: 2,
    oldest_transaction: "2026-03-01",
    newest_transaction: "2026-04-10",
    opening_balance: 0,
    opening_date: null,
    opening_transaction_id: null,
    warnings: [],
    ...extra,
  };
}

const BANKED = account("Current", "EUR", {
  institution: "Example Bank",
  note: "The one the salaries land in",
  opening_balance: 123456,
  opening_date: "2026-03-01",
  opening_transaction_id: "txn-eur",
});
const PLAIN = account("Pounds", "GBP");

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Accounts household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

async function open(name: string) {
  const row = (await screen.findByText(name)).closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: "Settings" }));
  return {
    bank: screen.getByLabelText("Bank or institution") as HTMLInputElement,
    note: screen.getByLabelText("Note") as HTMLTextAreaElement,
    save: screen.getByRole("button", { name: "Save" }) as HTMLButtonElement,
  };
}

function sent(): Record<string, unknown> {
  return vi.mocked(api.patch).mock.calls.at(-1)![1] as Record<string, unknown>;
}

describe("the bank and the note on the account panel", () => {
  beforeEach(() => {
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(
        async (url: string) => (url.includes("/accounts") ? [BANKED, PLAIN] : []) as never,
      );
    vi.mocked(api.patch).mockReset();
  });

  it("sends a padded bank and note trimmed", async () => {
    vi.mocked(api.patch).mockResolvedValue(PLAIN as never);
    mount();
    const { bank, note, save } = await open("Pounds");

    fireEvent.change(bank, { target: { value: "  Other Bank " } });
    fireEvent.change(note, { target: { value: "\n Rainy days  " } });
    fireEvent.click(save);

    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(vi.mocked(api.patch).mock.calls[0][0]).toBe("/accounts/Pounds");
    expect(sent().institution).toBe("Other Bank");
    expect(sent().note).toBe("Rainy days");
    expect(sent().clear_institution).toBe(false);
    expect(sent().clear_note).toBe(false);
  });

  it("clears a note that had one, rather than sending it empty", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...BANKED, note: null } as never);
    mount();
    const { note, save } = await open("Current");

    fireEvent.change(note, { target: { value: "   " } });
    fireEvent.click(save);

    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(sent().note).toBeNull();
    expect(sent().clear_note).toBe(true);
    // The bank was not touched: it is sent as it was, and not cleared.
    expect(sent().institution).toBe("Example Bank");
    expect(sent().clear_institution).toBe(false);
  });

  it("clears a bank that had one", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...BANKED, institution: null } as never);
    mount();
    const { bank, save } = await open("Current");

    fireEvent.change(bank, { target: { value: "" } });
    fireEvent.click(save);

    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(sent().institution).toBeNull();
    expect(sent().clear_institution).toBe(true);
    expect(sent().clear_note).toBe(false);
  });

  it("sends null and no clear for fields that were empty and stay empty", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...PLAIN, name: "Renamed" } as never);
    mount();
    await open("Pounds");

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(sent().institution).toBeNull();
    expect(sent().note).toBeNull();
    expect(sent().clear_institution).toBe(false);
    expect(sent().clear_note).toBe(false);
  });

  it("shows what was stored once a save keeps the panel open", async () => {
    // A warning on a save that sent the opening date keeps the panel open.
    const warning =
      "the opening date is after this account's earliest transaction (2026-04-10); " +
      "the balance on any day before 2026-04-20 leaves the opening balance out";
    vi.mocked(api.patch).mockResolvedValue({
      ...BANKED,
      institution: "New Bank",
      opening_date: "2026-04-20",
      warnings: [warning],
    } as never);
    mount();
    const { bank, save } = await open("Current");

    fireEvent.change(bank, { target: { value: "  New Bank  " } });
    fireEvent.change(screen.getByLabelText("Opening date"), { target: { value: "2026-04-20" } });
    fireEvent.click(save);

    await screen.findByRole("status");
    expect(sent().institution).toBe("New Bank");
    expect((screen.getByLabelText("Bank or institution") as HTMLInputElement).value).toBe(
      "New Bank",
    );
  });
});
