// @vitest-environment jsdom

/**
 * "Waiting for the other statement" on the Transfers screen (#125).
 *
 * Asserted on what it sends: a row's own picker is the ordinary transaction
 * update for that row, and the ticked rows go in one bulk update, so History
 * holds one entry for them and one undo takes them all back.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { WaitingSection } from "./TransferSections";
import type { Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as Household;

const CATEGORIES = [
  {
    id: "g-in",
    name: "Income",
    sort_order: 0,
    categories: [
      { id: "c-salary", group_id: "g-in", name: "Salary", full_name: "Income: Salary", sort_order: 0, archived: false, used_by: 0 },
      { id: "c-old", group_id: "g-in", name: "Old", full_name: "Income: Old", sort_order: 1, archived: true, used_by: 3 },
    ],
  },
  {
    id: "g-gift",
    name: "Family",
    sort_order: 1,
    categories: [
      { id: "c-gift", group_id: "g-gift", name: "Gifts", full_name: "Family: Gifts", sort_order: 0, archived: false, used_by: 0 },
    ],
  },
];

function leg(id: string, account: string, date: string, amount: number, currency = "EUR") {
  return { id, account_id: `acct-${account}`, account_name: account, date, amount, currency, description: `words ${id}` };
}

const WAITING = [
  { leg: leg("w1", "Current", "2026-04-02", -15000), why: "names a household member; its other side is not in the ledger yet" },
  { leg: leg("w2", "Saver", "2026-04-01", 2000), why: "names Current; its other side is not in the ledger yet" },
  { leg: leg("w3", "Pounds", "2026-04-03", -900, "GBP"), why: "names Saver; its other side is not in the ledger yet" },
];

function mount(onChanged = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <WaitingSection household={HOUSEHOLD} waiting={WAITING} onChanged={onChanged} />
    </QueryClientProvider>,
  );
  return onChanged;
}

describe("Waiting for the other statement", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset().mockResolvedValue(CATEGORIES);
    vi.mocked(api.patch).mockReset().mockResolvedValue({});
    vi.mocked(api.post).mockReset().mockResolvedValue([]);
  });

  it("says what a category does here", () => {
    mount();
    expect(screen.getByText(/Give a row a category if it isn't a transfer\./)).toBeTruthy();
  });

  it("categorises one row through the ordinary update, and reloads the findings", async () => {
    const onChanged = mount();
    await screen.findAllByRole("option", { name: "Salary" });
    const row = screen.getByText("words w2").closest("tr")!;
    const picker = within(row).getByLabelText("Not a transfer — categorise as…");
    // An archived category is not offered.
    expect(within(picker).queryByRole("option", { name: "Old" })).toBeNull();
    fireEvent.change(picker, { target: { value: "c-gift" } });
    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(api.patch).toHaveBeenCalledWith("/transactions/w2", { category_id: "c-gift" });
    expect(api.post).not.toHaveBeenCalled();
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("categorises the ticked rows in one bulk update", async () => {
    mount();
    await screen.findAllByRole("option", { name: "Salary" });
    const bulk = screen.getByLabelText("Categorise 0 selected as…") as HTMLSelectElement;
    expect(bulk.disabled).toBe(true);

    fireEvent.click(screen.getByLabelText("Select Current 2026-04-02"));
    fireEvent.click(screen.getByLabelText("Select Pounds 2026-04-03"));
    const ready = screen.getByLabelText("Categorise 2 selected as…") as HTMLSelectElement;
    expect(ready.disabled).toBe(false);
    fireEvent.change(ready, { target: { value: "c-salary" } });

    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(api.post).toHaveBeenCalledTimes(1);
    const [url, body] = vi.mocked(api.post).mock.calls[0] as [string, { transaction_ids: string[]; category_id: string }];
    expect(url).toBe("/households/house-1/transactions/bulk");
    expect(body.category_id).toBe("c-salary");
    expect([...body.transaction_ids].sort()).toEqual(["w1", "w3"]);
    expect(api.patch).not.toHaveBeenCalled();
    // The ticks are spent once the update has gone through.
    await screen.findByLabelText("Categorise 0 selected as…");
  });

  it("ticks every row from the heading, and unticks them again", async () => {
    mount();
    const all = screen.getByLabelText("Select every waiting row");
    fireEvent.click(all);
    expect(screen.getByLabelText("Categorise 3 selected as…")).toBeTruthy();
    fireEvent.click(all);
    expect(screen.getByLabelText("Categorise 0 selected as…")).toBeTruthy();
  });

  it("sorts at its headings", () => {
    mount();
    const order = () =>
      screen
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getByText(/^words/).textContent);
    expect(order()).toEqual(["words w2", "words w1", "words w3"]);
    fireEvent.click(screen.getByRole("button", { name: /Date/ }));
    expect(order()).toEqual(["words w3", "words w1", "words w2"]);
  });
});
