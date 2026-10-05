// @vitest-environment jsdom

/**
 * The account panel's opening balance and date (#10).
 *
 * Both live on a reconciled row in the register, so what matters here is what
 * the panel *sends*: the figure in the account's own minor units -- a euro
 * account's hundredths and a yen account's yen -- only what changed, and
 * nothing at all while the box holds something that is not an amount. Then
 * the two states the server can answer with: a row to link to, or none, and
 * a save that came back with a warning.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Accounts, localToday, openingText } from "./Accounts";
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

const EUROS = account("Current", "EUR", {
  opening_balance: 123456,
  opening_date: "2026-03-01",
  opening_transaction_id: "txn-eur",
});
const YEN = account("Yen wallet", "JPY", {
  opening_balance: -50000,
  opening_date: "2026-03-01",
  opening_transaction_id: "txn-jpy",
});
const EMPTY = account("Pounds", "GBP");

function mount(onOpenRegister = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Accounts household={HOUSEHOLD} onOpenRegister={onOpenRegister} />
    </QueryClientProvider>,
  );
  return onOpenRegister;
}

async function open(name: string) {
  const row = (await screen.findByText(name)).closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: "Settings" }));
  return {
    figure: screen.getByLabelText("Opening balance") as HTMLInputElement,
    date: screen.getByLabelText("Opening date") as HTMLInputElement,
    save: screen.getByRole("button", { name: "Save" }) as HTMLButtonElement,
  };
}

function sent(): Record<string, unknown> {
  return vi.mocked(api.patch).mock.calls.at(-1)![1] as Record<string, unknown>;
}

describe("the opening balance on the account panel", () => {
  beforeEach(() => {
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(
        async (url: string) => (url.includes("/accounts") ? [EUROS, YEN, EMPTY] : []) as never,
      );
    vi.mocked(api.patch).mockReset();
  });

  it("starts from the row's figure and date, signed and in the account's own digits", async () => {
    mount();
    const euros = await open("Current");
    expect(euros.figure.value).toBe("1234.56");
    expect(euros.date.value).toBe("2026-03-01");
    cleanup();

    mount();
    const yen = await open("Yen wallet");
    expect(yen.figure.value).toBe("-50000");
    expect(openingText(0, "GBP")).toBe("");
  });

  it("sends a changed figure in minor units, and leaves the date alone", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...EUROS, opening_balance: 99999 } as never);
    mount();
    const { figure, save } = await open("Current");

    fireEvent.change(figure, { target: { value: "999.99" } });
    fireEvent.click(save);

    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(vi.mocked(api.patch).mock.calls[0][0]).toBe("/accounts/Current");
    expect(sent().opening_balance).toBe(99999);
    expect(sent().opening_date).toBeNull();
  });

  it("reads a yen figure as yen, not as hundredths", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...YEN, opening_balance: 75000 } as never);
    mount();
    const { figure, date, save } = await open("Yen wallet");

    fireEvent.change(figure, { target: { value: "75000" } });
    fireEvent.change(date, { target: { value: "2026-02-14" } });
    fireEvent.click(save);

    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(sent().opening_balance).toBe(75000);
    expect(sent().opening_date).toBe("2026-02-14");
  });

  it("sends neither when neither changed, so saving a name does not touch the row", async () => {
    vi.mocked(api.patch).mockResolvedValue(EUROS as never);
    mount();
    const { save } = await open("Current");

    fireEvent.click(save);

    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(sent().opening_balance).toBeNull();
    expect(sent().opening_date).toBeNull();
  });

  it("will not save a figure that is not an amount in the account's currency", async () => {
    mount();
    const { figure, save } = await open("Yen wallet");

    fireEvent.change(figure, { target: { value: "1.2.3" } });

    expect(screen.getByText("That isn't an amount in JPY.")).toBeTruthy();
    expect(save.disabled).toBe(true);
  });

  it("will not save a date in the future", async () => {
    mount();
    const { date, save } = await open("Current");

    fireEvent.change(date, { target: { value: "2999-01-01" } });

    expect(screen.getByText("An account cannot have been opened in the future.")).toBeTruthy();
    expect(save.disabled).toBe(true);
  });

  it("links to the row in the register, filtered to this account", async () => {
    const onOpenRegister = mount();
    await open("Current");

    fireEvent.click(screen.getByRole("button", { name: "show it in the register" }));

    expect(onOpenRegister).toHaveBeenCalledWith({ accounts: ["Current"], open: "txn-eur" });
  });

  it("says an account opened empty has no row, and offers no link", async () => {
    mount();
    const { figure, date } = await open("Pounds");

    expect(figure.value).toBe("");
    expect(date.value).toBe("");
    expect(screen.getByText(/started empty, so there is no opening balance row/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "show it in the register" })).toBeNull();
  });

  it("stays open to show a warning the save came back with", async () => {
    const warning =
      "the opening date is after this account's earliest transaction (2026-04-10); " +
      "the balance on any day before 2026-04-20 leaves the opening balance out";
    vi.mocked(api.patch).mockResolvedValue({
      ...EUROS,
      opening_date: "2026-04-20",
      warnings: [warning],
    } as never);
    mount();
    const { date, save } = await open("Current");

    fireEvent.change(date, { target: { value: "2026-04-20" } });
    fireEvent.click(save);

    const banner = await screen.findByRole("status");
    expect(banner.textContent).toContain("The opening date is after this account's earliest");
    // Still open, on the saved account: a second save sends nothing new.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
    expect(sent().opening_date).toBeNull();
  });

  it("closes on a save that only renames, even with a warning standing", async () => {
    const warning =
      "the opening date is after this account's earliest transaction (2026-02-01); " +
      "the balance on any day before 2026-03-01 leaves the opening balance out";
    const warned = { ...EUROS, warnings: [warning] };
    vi.mocked(api.get).mockImplementation(
      async (url: string) => (url.includes("/accounts") ? [warned, YEN, EMPTY] : []) as never,
    );
    vi.mocked(api.patch).mockResolvedValue({ ...warned, name: "Renamed" } as never);
    mount();
    await open("Current");
    expect(screen.getByRole("status").textContent).toContain("The opening date is after");

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(sent().opening_balance).toBeNull();
    // Nothing the save sent could have caused it, and it did not change.
    await vi.waitFor(() => expect(screen.queryByLabelText("Opening balance")).toBeNull());
  });

  it("shows the server's refusal and keeps what was typed", async () => {
    const refusal =
      "this account was reconciled against a statement dated 2026-04-30, on or after the " +
      "opening date, so changing the opening balance or its date would move a balance the " +
      "bank has already confirmed. Undo that reconciliation in History first, then change " +
      "the opening balance";
    vi.mocked(api.patch).mockRejectedValue(new Error(refusal));
    mount();
    const { figure, save } = await open("Current");

    fireEvent.change(figure, { target: { value: "1100" } });
    fireEvent.click(save);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(refusal);
    expect(sent().opening_balance).toBe(110000);
    // Still open, with the figure as typed, to put right or abandon.
    expect((screen.getByLabelText("Opening balance") as HTMLInputElement).value).toBe("1100");
  });
});

describe("today, for the opening date's limit", () => {
  it("is this device's calendar day, not UTC's", () => {
    // Madrid, whatever zone the run is in: at half past midnight there,
    // toISOString still says yesterday.
    vi.stubEnv("TZ", "Europe/Madrid");
    try {
      expect(localToday(new Date(2026, 9, 5, 0, 30))).toBe("2026-10-05");
      expect(localToday(new Date(2026, 0, 9, 23, 59))).toBe("2026-01-09");
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
